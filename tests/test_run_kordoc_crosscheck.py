from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_kordoc_crosscheck import (
    _ascii_safe_temp_parent,
    build_crosscheck_report,
    compare_metrics,
    extract_kordoc_metrics,
    extract_local_metrics,
    markdown_report,
    review_required_from_comparison,
    run_kordoc_parse,
    split_command,
)


class KordocCrosscheckTests(unittest.TestCase):
    def test_split_command_preserves_quoted_executable_and_script_paths(self) -> None:
        parts = split_command('"python executable" "fake kordoc.py" --flag')

        self.assertEqual(parts, ["python executable", "fake kordoc.py", "--flag"])

    def test_split_command_repairs_unquoted_windows_executable_path(self) -> None:
        parts = split_command(r"C:\Program Files\Kordoc\kordoc.exe --format json")

        self.assertEqual(parts[0], r"C:\Program Files\Kordoc\kordoc.exe")
        self.assertEqual(parts[1:], ["--format", "json"])

    def test_split_command_keeps_py_launcher_arguments(self) -> None:
        parts = split_command(r"py -3 C:\tools\fake_kordoc.py")

        self.assertEqual(parts, ["py", "-3", r"C:\tools\fake_kordoc.py"])

    def test_extract_kordoc_metrics_counts_nested_tables_and_merged_cells(self) -> None:
        payload = {
            "document": {
                "fileType": "hwpx",
                "pageCount": 3,
                "markdown": "sample",
                "warnings": [{"code": "low_confidence"}],
                "outline": [{"title": "A"}],
                "blocks": [
                    {"type": "paragraph", "text": "intro"},
                    {
                        "type": "table",
                        "table": {
                            "rows": 2,
                            "cols": 3,
                            "cells": [
                                [
                                    {
                                        "r": 0,
                                        "c": 0,
                                        "rowSpan": 2,
                                        "colSpan": 1,
                                        "blocks": [
                                            {
                                                "type": "table",
                                                "table": {
                                                    "rows": 1,
                                                    "cols": 1,
                                                    "cells": [[{"r": 0, "c": 0, "text": "nested"}]],
                                                },
                                            }
                                        ],
                                    }
                                ]
                            ],
                        },
                    },
                ],
            }
        }

        metrics = extract_kordoc_metrics(payload)

        self.assertEqual(metrics["file_type"], "hwpx")
        self.assertEqual(metrics["page_count"], 3)
        self.assertEqual(metrics["table_count"], 2)
        self.assertEqual(metrics["nested_table_count"], 1)
        self.assertEqual(metrics["merged_cell_count"], 1)
        self.assertEqual(metrics["max_table_cols"], 3)
        self.assertEqual(metrics["warning_count"], 1)
        self.assertEqual(metrics["outline_count"], 1)

    def test_compare_metrics_flags_sidecar_table_disagreement(self) -> None:
        local = {
            "status": "parsed",
            "pipeline_counts": {
                "table_like_chunk_count": 1,
                "nested_table_candidate_count": 0,
            },
        }
        kordoc = {
            "status": "parsed",
            "table_count": 2,
            "nested_table_count": 1,
            "warning_count": 1,
        }

        comparison = compare_metrics(local, kordoc)

        self.assertEqual(comparison["deltas"]["table_count_delta"], 1)
        self.assertIn("table_count_disagreement", comparison["flags"])
        self.assertIn("nested_table_disagreement", comparison["flags"])
        self.assertIn("kordoc_warnings_present", comparison["flags"])

    def test_missing_kordoc_command_is_non_blocking(self) -> None:
        result = run_kordoc_parse(Path("sample.hwpx"), command="missing-kordoc-command-for-test")

        self.assertEqual(result["status"], "not_available")
        self.assertIn("executable_not_found", result["error"])
        self.assertNotIn("command", result)

    def test_environment_issue_does_not_escalate_to_content_review_required(self) -> None:
        comparison = compare_metrics(
            {"status": "parsed", "pipeline_counts": {"table_like_chunk_count": 0}},
            {"status": "not_available"},
        )

        self.assertFalse(review_required_from_comparison(comparison))
        self.assertEqual(comparison["content_flags"], [])
        self.assertEqual(comparison["operational_flags"], ["kordoc_not_available"])

    def test_run_kordoc_parse_accepts_json_from_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_kordoc.py"
            fake.write_text(
                textwrap.dedent(
                    """
                    import json
                    print(json.dumps({
                        "blocks": [
                            {
                                "type": "table",
                                "table": {
                                    "rows": 1,
                                    "cols": 2,
                                    "cells": [[{"r": 0, "c": 0}, {"r": 0, "c": 1}]]
                                }
                            }
                        ],
                        "markdown": "ok"
                    }))
                    """
                ).strip(),
                encoding="utf-8",
            )
            result = run_kordoc_parse(
                Path("sample.hwpx"),
                command=f'"{sys.executable}" "{fake}"',
                timeout_seconds=10,
            )

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["table_count"], 1)
        self.assertEqual(result["table_cell_count"], 2)
        self.assertEqual(result["command_label"], Path(sys.executable).name)

    def test_run_kordoc_parse_passes_silent_json_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv_path = root / "argv.json"
            fake = root / "fake_kordoc.py"
            fake.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import sys
                    from pathlib import Path

                    Path({str(argv_path)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
                    print(json.dumps({{"blocks": []}}))
                    """
                ).strip(),
                encoding="utf-8",
            )
            result = run_kordoc_parse(
                root / "sample.hwpx",
                command=f'"{sys.executable}" "{fake}"',
                timeout_seconds=10,
            )
            argv = json.loads(argv_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(argv[-3:], ["--format", "json", "--silent"])

    def test_run_kordoc_parse_uses_resolved_windows_cmd_shim(self) -> None:
        from types import SimpleNamespace

        captured: dict[str, list[str]] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

        shim = "\\".join(["C:", "Users", "op", "AppData", "Roaming", "npm", "kordoc.CMD"])
        with patch("scripts.run_kordoc_crosscheck.shutil.which", return_value=shim), patch(
            "scripts.run_kordoc_crosscheck.os.name", "nt"
        ), patch("scripts.run_kordoc_crosscheck.subprocess.run", side_effect=fake_run):
            result = run_kordoc_parse(Path("sample.hwp"), command="kordoc", timeout_seconds=10)

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(captured["argv"][:2], ["cmd", "/c"])
        self.assertEqual(captured["argv"][2], shim)
        self.assertEqual(captured["argv"][-3:], ["--format", "json", "--silent"])

    def test_run_kordoc_parse_uses_resolved_windows_ps1_shim(self) -> None:
        from types import SimpleNamespace

        captured: dict[str, list[str]] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

        shim = "\\".join(["C:", "Users", "op", "AppData", "Roaming", "npm", "kordoc.ps1"])
        with patch("scripts.run_kordoc_crosscheck.shutil.which", return_value=shim), patch(
            "scripts.run_kordoc_crosscheck.os.name", "nt"
        ), patch("scripts.run_kordoc_crosscheck.subprocess.run", side_effect=fake_run):
            result = run_kordoc_parse(Path("sample.hwp"), command="kordoc", timeout_seconds=10)

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(captured["argv"][:5], ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertEqual(captured["argv"][5], shim)
        self.assertEqual(captured["argv"][-3:], ["--format", "json", "--silent"])

    def test_run_kordoc_parse_copies_non_ascii_input_path_before_running(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "권한위임전결규정.hwp"
            source.write_bytes(b"dummy")
            captured: dict[str, str] = {}

            def fake_run(argv, **kwargs):
                input_path = str(argv[-4])
                captured["input_path"] = input_path
                self.assertTrue(input_path.isascii())
                self.assertEqual(Path(input_path).name, "input.hwp")
                self.assertTrue(Path(input_path).exists())
                return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

            with patch("scripts.run_kordoc_crosscheck.shutil.which", return_value=str(sys.executable)), patch(
                "scripts.run_kordoc_crosscheck.subprocess.run", side_effect=fake_run
            ):
                result = run_kordoc_parse(source, command="kordoc", timeout_seconds=10)

        self.assertEqual(result["status"], "parsed")
        self.assertTrue(result["input_path_normalized_for_kordoc"])
        self.assertNotEqual(captured["input_path"], str(source))

    def test_run_kordoc_parse_uses_ascii_temp_parent_when_default_temp_is_non_ascii(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "\uad8c\ud55c\uc704\uc784\uc804\uacb0\uaddc\uc815.hwp"
            source.write_bytes(b"dummy")
            non_ascii_temp = root / "\uc784\uc2dc"
            captured: dict[str, str] = {}

            def fake_run(argv, **kwargs):
                input_path = str(argv[-4])
                captured["input_path"] = input_path
                self.assertTrue(input_path.isascii())
                self.assertEqual(Path(input_path).name, "input.hwp")
                self.assertTrue(Path(input_path).exists())
                return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

            with patch("scripts.run_kordoc_crosscheck.shutil.which", return_value=str(sys.executable)), patch(
                "scripts.run_kordoc_crosscheck.tempfile.gettempdir", return_value=str(non_ascii_temp)
            ), patch("scripts.run_kordoc_crosscheck.subprocess.run", side_effect=fake_run):
                result = run_kordoc_parse(source, command="kordoc", timeout_seconds=10)

        self.assertEqual(result["status"], "parsed")
        self.assertTrue(result["input_path_normalized_for_kordoc"])
        self.assertNotIn("\uc784\uc2dc", captured["input_path"])

    def test_ascii_safe_temp_parent_falls_back_when_existing_parent_rejects_child_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            denied_parent = root / "denied"
            fallback_parent = root / "fallback"
            denied_parent.mkdir()
            fallback_parent.mkdir()
            denied_sentinel = denied_parent / "keep.txt"
            fallback_sentinel = fallback_parent / "keep.txt"
            denied_sentinel.write_text("denied", encoding="utf-8")
            fallback_sentinel.write_text("fallback", encoding="utf-8")
            attempted_probe_parents: list[Path] = []
            original_mkdir = Path.mkdir

            def guarded_mkdir(path: Path, *args, **kwargs) -> None:
                if path.parent in {denied_parent, fallback_parent}:
                    attempted_probe_parents.append(path.parent)
                    if path.parent == denied_parent:
                        raise PermissionError("child creation denied")
                original_mkdir(path, *args, **kwargs)

            with patch(
                "scripts.run_kordoc_crosscheck._ascii_temp_parent_candidates",
                return_value=[denied_parent, fallback_parent],
            ), patch.object(Path, "mkdir", autospec=True, side_effect=guarded_mkdir):
                selected = _ascii_safe_temp_parent()

            self.assertEqual(selected, fallback_parent.resolve())
            self.assertEqual(attempted_probe_parents, [denied_parent, fallback_parent])
            self.assertTrue(denied_parent.is_dir())
            self.assertTrue(fallback_parent.is_dir())
            self.assertEqual(denied_sentinel.read_text(encoding="utf-8"), "denied")
            self.assertEqual(fallback_sentinel.read_text(encoding="utf-8"), "fallback")
            self.assertEqual(list(denied_parent.iterdir()), [denied_sentinel])
            self.assertEqual(list(fallback_parent.iterdir()), [fallback_sentinel])

    def test_run_kordoc_parse_accepts_tool_warning_before_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_kordoc_warning.py"
            fake.write_text(
                textwrap.dedent(
                    """
                    import json

                    print('Warning: Required "glyf" table is not found -- trying to recover.')
                    print(json.dumps({
                        "blocks": [
                            {
                                "type": "table",
                                "table": {
                                    "rows": 1,
                                    "cols": 2,
                                    "cells": [[{"r": 0, "c": 0}, {"r": 0, "c": 1}]]
                                }
                            }
                        ]
                    }))
                    """
                ).strip(),
                encoding="utf-8",
            )
            result = run_kordoc_parse(
                Path("sample.pdf"),
                command=f'"{sys.executable}" "{fake}"',
                timeout_seconds=10,
            )

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["table_count"], 1)

    def test_run_kordoc_parse_accepts_bracketed_warning_before_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_kordoc_bracket_warning.py"
            fake.write_text(
                textwrap.dedent(
                    """
                    import json

                    print('[WARN] font table recovery attempted')
                    print(json.dumps({
                        "blocks": [
                            {
                                "type": "table",
                                "table": {
                                    "rows": 1,
                                    "cols": 2,
                                    "cells": [[{"r": 0, "c": 0}, {"r": 0, "c": 1}]]
                                }
                            }
                        ]
                    }))
                    """
                ).strip(),
                encoding="utf-8",
            )
            result = run_kordoc_parse(
                Path("sample.pdf"),
                command=f'"{sys.executable}" "{fake}"',
                timeout_seconds=10,
            )

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["table_count"], 1)

    def test_run_kordoc_parse_redacts_process_output_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_kordoc_fail.py"
            fake.write_text(
                textwrap.dedent(
                    """
                    import sys
                    print("SECRET_STDOUT")
                    print("SECRET_STDERR", file=sys.stderr)
                    raise SystemExit(7)
                    """
                ).strip(),
                encoding="utf-8",
            )
            result = run_kordoc_parse(
                Path("sample.hwpx"),
                command=f'"{sys.executable}" "{fake}"',
                timeout_seconds=10,
            )

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("stdout_chars", result)
        self.assertNotIn("stdout", result)
        self.assertNotIn("stderr", result)
        self.assertNotIn("SECRET_STDOUT", encoded)
        self.assertNotIn("SECRET_STDERR", encoded)

    def test_run_kordoc_parse_redacts_os_error_paths(self) -> None:
        sensitive_dir = "\\".join(["C:", "Users", "dd", "secret"])
        with patch(
            "scripts.run_kordoc_crosscheck.subprocess.run",
            side_effect=OSError(f"cannot open {sensitive_dir}\\sample.hwpx"),
        ):
            result = run_kordoc_parse(Path("sample.hwpx"), command=f'"{sys.executable}"', timeout_seconds=10)

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("<local-path-redacted>", encoded)
        self.assertNotIn(sensitive_dir, encoded)

    def test_extract_local_metrics_redacts_exception_paths(self) -> None:
        sensitive_dir = "\\".join(["C:", "Users", "dd", "secret"])
        with patch(
            "scripts.run_kordoc_crosscheck.get_parser",
            side_effect=RuntimeError(f"failed at {sensitive_dir}\\source.pdf"),
        ):
            result = extract_local_metrics(Path("source.pdf"))

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("<local-path-redacted>", encoded)
        self.assertNotIn(sensitive_dir, encoded)

    def test_markdown_report_states_diagnostic_scope(self) -> None:
        report = {
            "contract": "kordoc_sidecar_crosscheck_v1",
            "scope": "diagnostic_only_not_indexing_input",
            "counts": {
                "documents": 1,
                "review_required": 1,
                "operational_issue": 0,
                "local_parsed": 1,
                "kordoc_parsed": 1,
            },
            "rows": [
                {
                    "filename": "sample.hwpx",
                    "local": {"pipeline_counts": {"table_like_chunk_count": 0}},
                    "kordoc": {"table_count": 1},
                    "comparison": {
                        "flags": ["kordoc_table_signal_without_local_table"],
                        "content_flags": ["kordoc_table_signal_without_local_table"],
                        "operational_flags": [],
                        "deltas": {"table_count_delta": 1},
                        "basis": "heuristic_table_signal_not_entity_equivalence",
                    },
                }
            ],
        }

        rendered = markdown_report(report)

        self.assertIn("diagnostic_only_not_indexing_input", rendered)
        self.assertIn("Do not index Kordoc output directly.", rendered)
        self.assertIn("kordoc_table_signal_without_local_table", rendered)
        self.assertIn("heuristic_table_signal_not_entity_equivalence", rendered)

    def test_build_report_keeps_missing_kordoc_out_of_review_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unsupported = Path(tmp) / "sample.txt"
            unsupported.write_text("not parsed locally", encoding="utf-8")

            report = build_crosscheck_report(
                [unsupported],
                kordoc_command="missing-kordoc-command-for-test",
                data_dir=Path(tmp) / "data",
                timeout_seconds=1,
            )

        self.assertEqual(report["counts"]["review_required"], 0)
        self.assertEqual(report["counts"]["operational_issue"], 1)
        self.assertFalse(report["rows"][0]["review_required"])
        self.assertTrue(report["rows"][0]["operational_issue"])

    def test_cli_creates_separate_markdown_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_kordoc.py"
            fake.write_text('import json; print(json.dumps({"blocks": []}))', encoding="utf-8")
            input_path = root / "sample.txt"
            input_path.write_text("unsupported local parser", encoding="utf-8")
            out_json = root / "json" / "report.json"
            out_md = root / "md" / "nested" / "report.md"

            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/run_kordoc_crosscheck.py",
                    str(input_path),
                    "--kordoc-command",
                    f'"{sys.executable}" "{fake}"',
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(out_json.exists())
            self.assertTrue(out_md.exists())


if __name__ == "__main__":
    unittest.main()
