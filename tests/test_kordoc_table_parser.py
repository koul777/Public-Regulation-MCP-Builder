from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.processors.kordoc_table_parser import KordocTableParser, extract_kordoc_table_inventory, split_command


class KordocTableParserTests(unittest.TestCase):
    def test_split_command_repairs_unquoted_windows_executable_path(self) -> None:
        with patch("app.processors.kordoc_table_parser._is_windows", return_value=True):
            parts = split_command(r"C:\Program Files\Kordoc\kordoc.exe --format json")

        self.assertEqual(parts[0], r"C:\Program Files\Kordoc\kordoc.exe")
        self.assertEqual(parts[1:], ["--format", "json"])

    def test_extracts_rows_from_kordoc_table_json(self) -> None:
        payload = {
            "blocks": [
                {
                    "type": "table",
                    "caption": "직무발명 보상금 지급기준",
                    "page": 17,
                    "rows": [
                        {"cells": [{"text": "보상구분"}, {"text": "지급기준"}, {"text": "지급금액"}]},
                        {"cells": [{"text": "출원"}, {"text": "국내"}, {"text": "10만원"}]},
                    ],
                }
            ]
        }

        inventory = extract_kordoc_table_inventory(payload)

        self.assertEqual(inventory["status"], "parsed")
        self.assertEqual(inventory["table_count"], 1)
        self.assertEqual(inventory["stored_table_count"], 1)
        table = inventory["tables"][0]
        self.assertEqual(table["title"], "직무발명 보상금 지급기준")
        self.assertEqual(table["source_page"], 17)
        self.assertEqual(table["row_count"], 2)
        self.assertEqual(table["column_count"], 3)
        self.assertEqual(table["cell_rows"][0]["cells"], ["보상구분", "지급기준", "지급금액"])
        self.assertIn("kordoc_table_parser_used", inventory["review_flags"])

    def test_extracts_rows_from_flat_cell_json(self) -> None:
        payload = {
            "tables": [
                {
                    "kind": "table",
                    "cells": [
                        {"row": 0, "col": 0, "text": "구분"},
                        {"row": 0, "col": 1, "text": "기준"},
                        {"row": 1, "col": 0, "text": "A"},
                        {"row": 1, "col": 1, "text": "1"},
                    ],
                }
            ]
        }

        inventory = extract_kordoc_table_inventory(payload)

        self.assertEqual(inventory["table_count"], 1)
        self.assertEqual(inventory["tables"][0]["cell_rows"][1]["cells"], ["A", "1"])
        self.assertNotIn("| --- | --- |", inventory["tables"][0]["markdown"])

    def test_infers_aks_table_title_when_kordoc_omits_caption(self) -> None:
        inventory = extract_kordoc_table_inventory(
            {
                "tables": [
                    {
                        "kind": "table",
                        "rows": [
                            {"cells": [{"text": "연구직 경력기간 환산율표"}]},
                            {"cells": [{"text": "경력종별"}, {"text": "환산율"}]},
                            {"cells": [{"text": "대학 연구기관"}, {"text": "100%"}]},
                        ],
                    }
                ]
            }
        )

        table = inventory["tables"][0]
        self.assertEqual("연구직 경력기간 환산율표", table["title"])
        self.assertEqual("first_row", table["title_source"])

    def test_preserves_explicit_header_rows_in_markdown(self) -> None:
        payload = {
            "tables": [
                {
                    "kind": "table",
                    "table": {
                        "hasHeader": True,
                        "cells": [
                            [{"text": "구분"}, {"text": "기준"}],
                            [{"text": "A"}, {"text": "1"}],
                        ],
                    },
                }
            ]
        }

        inventory = extract_kordoc_table_inventory(payload)

        self.assertIn("| --- | --- |", inventory["tables"][0]["markdown"])

    def test_extracts_rows_from_wrapped_block_table_json(self) -> None:
        payload = {
            "document": {
                "blocks": [
                    {
                        "type": "table",
                        "caption": "직무발명 보상금 지급기준",
                        "page": 17,
                        "table": {
                            "rows": 2,
                            "cols": 3,
                            "cells": [
                                [
                                    {"text": "보상구분", "cs": 2},
                                    {"text": "지급기준"},
                                    {"text": "지급금액"},
                                ],
                                [
                                    {"text": "출원"},
                                    {"text": "국내"},
                                    {"text": "10만원"},
                                ],
                            ],
                        },
                    }
                ]
            }
        }

        inventory = extract_kordoc_table_inventory(payload)

        self.assertEqual(inventory["table_count"], 1)
        self.assertEqual(inventory["stored_table_count"], 1)
        table = inventory["tables"][0]
        self.assertEqual(table["title"], "직무발명 보상금 지급기준")
        self.assertEqual(table["source_page"], 17)
        self.assertEqual(table["row_count"], 2)
        self.assertEqual(table["column_count"], 3)
        self.assertEqual(table["merged_cell_count"], 1)
        self.assertEqual(table["cell_rows"][0]["cells"], ["보상구분", "지급기준", "지급금액"])

    def test_counts_nested_tables_inside_kordoc_cells_without_storing_as_top_level(self) -> None:
        payload = {
            "blocks": [
                {
                    "type": "table",
                    "table": {
                        "cells": [
                            [
                                {
                                    "text": "외부",
                                    "blocks": [
                                        {
                                            "type": "table",
                                            "table": {
                                                "cells": [[{"text": "내부"}]],
                                            },
                                        }
                                    ],
                                }
                            ]
                        ]
                    },
                }
            ]
        }

        inventory = extract_kordoc_table_inventory(payload)

        self.assertEqual(inventory["table_count"], 1)
        self.assertEqual(inventory["stored_table_count"], 1)
        self.assertEqual(inventory["tables"][0]["nested_table_count"], 1)
        self.assertEqual(inventory["tables"][0]["cell_rows"][0]["cells"], ["외부"])

    def test_extracts_rows_from_kordoc_r_c_cells(self) -> None:
        payload = {
            "blocks": [
                {
                    "type": "table",
                    "table": {
                        "cells": [
                            {"r": 0, "c": 0, "text": "구분"},
                            {"r": 0, "c": 1, "text": "기준"},
                            {"r": 1, "c": 0, "text": "A"},
                            {"r": 1, "c": 1, "text": "B"},
                        ]
                    },
                }
            ]
        }

        inventory = extract_kordoc_table_inventory(payload)

        self.assertEqual(inventory["table_count"], 1)
        self.assertEqual(inventory["tables"][0]["cell_rows"][1]["cells"], ["A", "B"])

    def test_parse_file_passes_silent_json_flags_to_kordoc(self) -> None:
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
            settings = Settings(
                data_dir=root / "data",
                enable_kordoc_table_parser=True,
                kordoc_table_command=f'"{sys.executable}" "{fake}"',
                kordoc_table_timeout_seconds=10,
            )
            result = KordocTableParser(settings).parse_file(root / "sample.pdf")

            argv = json.loads(argv_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(argv[-3:], ["--format", "json", "--silent"])

    def test_parse_file_copies_non_ascii_input_path_before_running_kordoc(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "권한위임전결규정.hwp"
            source.write_bytes(b"dummy")
            settings = Settings(
                data_dir=root / "data",
                enable_kordoc_table_parser=True,
                kordoc_table_command="kordoc",
                kordoc_table_timeout_seconds=10,
            )
            captured: dict[str, str] = {}

            def fake_run(argv, **kwargs):
                input_path = str(argv[-4])
                captured["input_path"] = input_path
                self.assertTrue(input_path.isascii())
                self.assertEqual(Path(input_path).name, "input.hwp")
                self.assertTrue(Path(input_path).exists())
                return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

            with patch("app.processors.kordoc_table_parser.shutil.which", return_value="kordoc"), patch(
                "app.processors.kordoc_table_parser.subprocess.run", side_effect=fake_run
            ):
                result = KordocTableParser(settings).parse_file(source)

        self.assertEqual(result["status"], "parsed")
        self.assertTrue(result["input_path_normalized_for_kordoc"])
        self.assertNotEqual(captured["input_path"], str(source))

    def test_parse_file_uses_ascii_temp_parent_when_default_temp_is_non_ascii(self) -> None:
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "\uad8c\ud55c\uc704\uc784\uc804\uacb0\uaddc\uc815.hwp"
            source.write_bytes(b"dummy")
            non_ascii_temp = root / "\uc784\uc2dc"
            settings = Settings(
                data_dir=root / "data",
                enable_kordoc_table_parser=True,
                kordoc_table_command="kordoc",
                kordoc_table_timeout_seconds=10,
            )
            captured: dict[str, str] = {}

            def fake_run(argv, **kwargs):
                input_path = str(argv[-4])
                captured["input_path"] = input_path
                self.assertTrue(input_path.isascii())
                self.assertEqual(Path(input_path).name, "input.hwp")
                self.assertTrue(Path(input_path).exists())
                return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

            with patch("app.processors.kordoc_table_parser.shutil.which", return_value="kordoc"), patch(
                "app.processors.kordoc_table_parser.tempfile.gettempdir", return_value=str(non_ascii_temp)
            ), patch("app.processors.kordoc_table_parser.subprocess.run", side_effect=fake_run):
                result = KordocTableParser(settings).parse_file(source)

        self.assertEqual(result["status"], "parsed")
        self.assertTrue(result["input_path_normalized_for_kordoc"])
        self.assertNotIn("\uc784\uc2dc", captured["input_path"])

    def test_parse_file_uses_configured_table_inventory_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_kordoc_many_tables.py"
            fake.write_text(
                textwrap.dedent(
                    """
                    import json

                    print(json.dumps({
                        "blocks": [
                            {"type": "table", "rows": [["a"]]},
                            {"type": "table", "rows": [["b"]]},
                            {"type": "table", "rows": [["c"]]},
                        ]
                    }))
                    """
                ).strip(),
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=root / "data",
                enable_kordoc_table_parser=True,
                kordoc_table_command=f'"{sys.executable}" "{fake}"',
                kordoc_table_timeout_seconds=10,
                kordoc_table_max_tables=2,
            )
            result = KordocTableParser(settings).parse_file(root / "sample.pdf")

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["table_count"], 3)
        self.assertEqual(result["stored_table_count"], 2)
        self.assertTrue(result["tables_truncated"])

    def test_parse_file_runs_windows_cmd_shim_through_cmd_exe(self) -> None:
        # npm installs CLIs like `kordoc` as .cmd shims on Windows; CreateProcess
        # cannot launch them in list form, so parse_file must route through cmd.exe.
        from types import SimpleNamespace

        settings = Settings(
            data_dir=Path("data"),
            enable_kordoc_table_parser=True,
            kordoc_table_command="kordoc",
            kordoc_table_timeout_seconds=10,
        )
        captured: dict[str, list[str]] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

        shim = "\\".join(["C:", "Users", "op", "AppData", "Roaming", "npm", "kordoc.CMD"])
        with patch("app.processors.kordoc_table_parser.shutil.which", return_value=shim), patch(
            "app.processors.kordoc_table_parser._is_windows", return_value=True
        ), patch("app.processors.kordoc_table_parser.subprocess.run", side_effect=fake_run):
            result = KordocTableParser(settings).parse_file(Path("sample.hwp"))

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(captured["argv"][:2], ["cmd", "/c"])
        self.assertEqual(captured["argv"][2], shim)
        self.assertEqual(captured["argv"][-3:], ["--format", "json", "--silent"])

    def test_parse_file_runs_windows_ps1_shim_through_powershell(self) -> None:
        from types import SimpleNamespace

        settings = Settings(
            data_dir=Path("data"),
            enable_kordoc_table_parser=True,
            kordoc_table_command="kordoc",
            kordoc_table_timeout_seconds=10,
        )
        captured: dict[str, list[str]] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

        shim = "\\".join(["C:", "Users", "op", "AppData", "Roaming", "npm", "kordoc.ps1"])
        with patch("app.processors.kordoc_table_parser.shutil.which", return_value=shim), patch(
            "app.processors.kordoc_table_parser._is_windows", return_value=True
        ), patch("app.processors.kordoc_table_parser.subprocess.run", side_effect=fake_run):
            result = KordocTableParser(settings).parse_file(Path("sample.hwp"))

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(captured["argv"][:5], ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertEqual(captured["argv"][5], shim)
        self.assertEqual(captured["argv"][-3:], ["--format", "json", "--silent"])

    def test_parse_file_finds_windows_npm_shim_when_path_is_stale(self) -> None:
        # A long-running Streamlit/API process can have a stale PATH after npm
        # installs Kordoc. Fall back to the standard Windows npm global shim dir.
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            npm_dir = root / "npm"
            npm_dir.mkdir()
            shim = npm_dir / "kordoc.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")
            settings = Settings(
                data_dir=root / "data",
                enable_kordoc_table_parser=True,
                kordoc_table_command="kordoc",
                kordoc_table_timeout_seconds=10,
            )
            captured: dict[str, list[str]] = {}

            def fake_run(argv, **kwargs):
                captured["argv"] = argv
                return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

            with patch("app.processors.kordoc_table_parser.shutil.which", return_value=None), patch(
                "app.processors.kordoc_table_parser._is_windows", return_value=True
            ), patch.dict("app.processors.kordoc_table_parser.os.environ", {"APPDATA": str(root)}, clear=False), patch(
                "app.processors.kordoc_table_parser.subprocess.run", side_effect=fake_run
            ):
                result = KordocTableParser(settings).parse_file(root / "sample.hwp")

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(captured["argv"][:2], ["cmd", "/c"])
        self.assertEqual(Path(captured["argv"][2]), shim)
        self.assertEqual(captured["argv"][-3:], ["--format", "json", "--silent"])

    @unittest.skipIf(os.name == "nt", "POSIX command invocation test")
    def test_parse_file_runs_resolved_binary_directly_off_windows(self) -> None:
        # On POSIX (or when the resolved command is a real binary) no cmd.exe wrapper.
        from types import SimpleNamespace

        settings = Settings(
            data_dir=Path("data"),
            enable_kordoc_table_parser=True,
            kordoc_table_command="kordoc",
            kordoc_table_timeout_seconds=10,
        )
        captured: dict[str, list[str]] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout=json.dumps({"blocks": []}), stderr="")

        binary = "/usr/local/bin/kordoc"
        with patch("app.processors.kordoc_table_parser.shutil.which", return_value=binary), patch(
            "app.processors.kordoc_table_parser._is_windows", return_value=False
        ), patch("app.processors.kordoc_table_parser.subprocess.run", side_effect=fake_run):
            result = KordocTableParser(settings).parse_file(Path("sample.hwp"))

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(captured["argv"][0], binary)
        self.assertNotIn("cmd", captured["argv"][:1])

    def test_parse_file_accepts_tool_warning_before_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_kordoc_warning.py"
            fake.write_text(
                textwrap.dedent(
                    """
                    import json

                    print('Warning: Required "glyf" table is not found -- trying to recover.')
                    print(json.dumps({"blocks": [{"type": "table", "rows": [["a", "b"]]}]}))
                    """
                ).strip(),
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=root / "data",
                enable_kordoc_table_parser=True,
                kordoc_table_command=f'"{sys.executable}" "{fake}"',
                kordoc_table_timeout_seconds=10,
            )
            result = KordocTableParser(settings).parse_file(root / "sample.pdf")

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["table_count"], 1)

    def test_parse_file_accepts_bracketed_warning_before_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_kordoc_bracket_warning.py"
            fake.write_text(
                textwrap.dedent(
                    """
                    import json

                    print('[WARN] font table recovery attempted')
                    print(json.dumps({"blocks": [{"type": "table", "rows": [["a", "b"]]}]}))
                    """
                ).strip(),
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=root / "data",
                enable_kordoc_table_parser=True,
                kordoc_table_command=f'"{sys.executable}" "{fake}"',
                kordoc_table_timeout_seconds=10,
            )
            result = KordocTableParser(settings).parse_file(root / "sample.pdf")

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["table_count"], 1)

    def test_parse_file_redacts_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root / "data",
                enable_kordoc_table_parser=True,
                kordoc_table_command=f'"{sys.executable}"',
                kordoc_table_timeout_seconds=10,
            )
            with patch("app.processors.kordoc_table_parser.subprocess.run", side_effect=OSError("SECRET_PATH")):
                result = KordocTableParser(settings).parse_file(root / "sample.pdf")

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(str(root), encoded)


if __name__ == "__main__":
    unittest.main()
