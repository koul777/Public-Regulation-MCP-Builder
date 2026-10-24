from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import mcp_build_qa_gate as gate
from scripts.mcp_bundle_contract import SETUP_BUNDLE_FILES


def _tool(name: str, description: str, *, properties=None, required=None, annotations=None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties or {}, "required": required or []},
        "annotations": annotations
        if annotations is not None
        else {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    }


_BOUNDED_STRING = {"type": "string", "maxLength": 100}
_SYNTHETIC_SURFACE = {
    "instructions": "Use search first, then fetch with a returned id.",
    "tools": [
        _tool(
            "search",
            "Search approved regulation evidence and return result ids for fetch.",
            properties={"query": _BOUNDED_STRING},
            required=["query"],
        ),
        _tool(
            "fetch",
            "Fetch approved regulation evidence for an id returned by search.",
            properties={"id": _BOUNDED_STRING},
            required=["id"],
        ),
    ],
}


def _codes(findings: list[dict]) -> list[str]:
    return [item["code"] for item in findings]


class ToolSurfaceTests(unittest.TestCase):
    def test_shipped_profiles_pass_without_runtime_data(self) -> None:
        report = gate.run_mcp_build_qa_gate(data_dir=None, tenant_id="tenant-a")

        self.assertTrue(report["passed"], [item for item in report["findings"] if item["severity"] == "blocker"])
        self.assertEqual("source-only", report["mode"])
        self.assertEqual({"full", "chatgpt-data"}, set(report["profiles"]))
        for profile, summary in report["profiles"].items():
            with self.subTest(profile=profile):
                self.assertTrue(summary["chain_smoke"]["skipped"])
                self.assertLessEqual(summary["tool_count"], gate.RATCHET[profile]["max_tools"])
                self.assertLessEqual(
                    summary["surface_tokens_estimate"],
                    gate.RATCHET[profile]["max_surface_tokens"],
                )
        self.assertIn("search", report["profiles"]["full"]["entry_points"])

    def test_ratchet_and_targets_are_pinned(self) -> None:
        # Lowering a ratchet after consolidating tools updates this test in the
        # same change. Raising it to make a build pass is not allowed.
        self.assertEqual(
            {
                "full": {"max_tools": 16, "max_surface_tokens": 4_650},
                "chatgpt-data": {"max_tools": 7, "max_surface_tokens": 1_300},
            },
            {key: dict(value) for key, value in gate.RATCHET.items()},
        )
        self.assertEqual(12, gate.TARGETS["max_tools"])
        self.assertEqual(4_000, gate.TARGETS["max_surface_tokens"])

    def test_synthetic_surface_passes(self) -> None:
        summary, findings = gate.check_tool_surface("chatgpt-data", _SYNTHETIC_SURFACE)
        self.assertEqual([], findings)
        self.assertEqual(["search"], summary["entry_points"])

    def test_chain_and_instruction_gaps_are_blockers(self) -> None:
        surface = copy.deepcopy(_SYNTHETIC_SURFACE)
        surface["tools"][1]["description"] = "Fetch approved regulation evidence for a result identifier."
        surface["instructions"] = "Use search first."
        _summary, findings = gate.check_tool_surface("chatgpt-data", surface)
        self.assertEqual(["tool-chain-producer-missing", "tool-missing-from-instructions"], _codes(findings))
        self.assertEqual(["id"], findings[0]["params"])

    def test_unsafe_or_unfriendly_inputs_are_blockers(self) -> None:
        surface = copy.deepcopy(_SYNTHETIC_SURFACE)
        surface["tools"][0]["inputSchema"]["properties"].update(
            {
                "as_of_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "page": {"type": "integer", "minimum": 1},
                "filters": {"type": "array", "items": {"type": "string", "maxLength": 10}},
                "options": {"type": "object"},
            }
        )
        surface["tools"][1]["annotations"] = {"readOnlyHint": False, "destructiveHint": True}
        _summary, findings = gate.check_tool_surface("chatgpt-data", surface)
        unbounded = {item["param"]: item["reasons"] for item in findings if item["code"] == "tool-input-unbounded"}
        self.assertEqual(
            {
                "as_of_date": ["string-without-maxLength"],
                "page": ["number-without-maximum"],
                "filters": ["array-without-maxItems"],
            },
            unbounded,
        )
        self.assertIn("tool-input-schema-not-flat", _codes(findings))
        self.assertIn("tool-not-read-only", _codes(findings))

    def test_thin_descriptions_missing_entry_point_and_ratchet_growth_block(self) -> None:
        surface = {
            "instructions": "Use fetch.",
            "tools": [_tool("fetch", "Fetch by id.", properties={"id": _BOUNDED_STRING}, required=["id"])],
        }
        _summary, findings = gate.check_tool_surface("chatgpt-data", surface)
        self.assertIn("tool-description-too-thin", _codes(findings))
        self.assertIn("tool-entry-point-missing", _codes(findings))

        grown = copy.deepcopy(_SYNTHETIC_SURFACE)
        grown["tools"].extend(
            _tool(f"extra_{index}", "Return approved regulation evidence; use search for ids.")
            for index in range(6)
        )
        grown["instructions"] += " " + " ".join(f"extra_{index}" for index in range(6))
        _summary, findings = gate.check_tool_surface("chatgpt-data", grown)
        self.assertEqual(["tool-count-above-ratchet"], _codes(findings))

    def test_target_gaps_do_not_block_unless_required(self) -> None:
        surface = copy.deepcopy(_SYNTHETIC_SURFACE)
        surface["tools"][0]["description"] += " " + "Detail. " * 60
        _summary, findings = gate.check_tool_surface("full", surface)
        self.assertEqual([("tool-description-above-target", "target")], [(f["code"], f["severity"]) for f in findings])


class _FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = {
            "list_regulations": {
                "regulations": [{"regulation_unit_id": "unit-a", "regulation_title": "합성 복무규정"}],
                "total_count": 1,
            },
            "get_regulation_toc": {
                "nodes": [
                    {"node_type": "regulation", "label": "합성 복무규정"},
                    {"node_type": "article", "label": "제1조 목적", "number": "제1조", "title": "목적"},
                ]
            },
            "get_regulation_article": {"articles": [{"id": "r1", "text": "합성 조문"}]},
            "search": {"results": [{"id": "result-1", "title": "합성 복무규정 제1조"}]},
            "fetch": {"id": "result-1", "text": "합성 조문", "metadata": {}},
        }
        self.article_forms_without_result: set[str] = set()
        self.accept_foreign_profile = False

    def __call__(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        if name == "search" and "profile_id" in arguments and not self.accept_foreign_profile:
            raise ValueError("The MCP server is bound to a different institution profile.")
        if name == "get_regulation_article" and arguments["article_no"] in self.article_forms_without_result:
            return {"articles": []}
        response = self.responses[name]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class ChainSmokeTests(unittest.TestCase):
    def test_full_chain_passes_and_checks_locator_forms_and_profile_binding(self) -> None:
        runtime = _FakeRuntime()
        summary, findings = gate.run_chain_smoke(runtime, profile="full", profile_bound=True)
        self.assertEqual([], findings)
        self.assertEqual("enforced", summary["profile_binding"])
        article_forms = [args["article_no"] for name, args in runtime.calls if name == "get_regulation_article"]
        self.assertEqual(["제1조", "제1조 목적", "제1조(목적)", "1"], article_forms)
        self.assertEqual(["search", "fetch"], [name for name, _ in runtime.calls if name in {"search", "fetch"}][:2])

    def test_toc_label_that_finds_nothing_is_a_blocker(self) -> None:
        runtime = _FakeRuntime()
        runtime.article_forms_without_result = {"제1조 목적"}
        _summary, findings = gate.run_chain_smoke(runtime, profile="chatgpt-data", profile_bound=True)
        self.assertEqual(
            [("chain-article-empty", "toc-label")],
            [(item["code"], item.get("locator_form")) for item in findings],
        )

    def test_empty_search_error_and_unbound_profile_are_blockers(self) -> None:
        runtime = _FakeRuntime()
        runtime.responses["search"] = {"results": []}
        runtime.accept_foreign_profile = True
        _summary, findings = gate.run_chain_smoke(runtime, profile="full", profile_bound=True)
        self.assertEqual(["chain-search-empty", "chain-profile-binding-not-enforced"], _codes(findings))

        runtime = _FakeRuntime()
        runtime.responses["get_regulation_toc"] = RuntimeError("hierarchy unavailable")
        _summary, findings = gate.run_chain_smoke(runtime, profile="chatgpt-data", profile_bound=False)
        self.assertEqual(["chain-call-error", "chain-toc-without-articles"], _codes(findings))
        self.assertEqual("RuntimeError", findings[0]["error_type"])

    def test_empty_catalog_is_a_blocker(self) -> None:
        runtime = _FakeRuntime()
        runtime.responses["list_regulations"] = {"regulations": [], "total_count": 0}
        _summary, findings = gate.run_chain_smoke(runtime, profile="full", profile_bound=False)
        self.assertEqual(["chain-catalog-empty"], _codes(findings))

    def test_local_paths_secrets_and_external_locators_in_responses_block(self) -> None:
        runtime = _FakeRuntime()
        runtime.responses["fetch"] = {
            "id": "result-1",
            "text": "합성 조문",
            "url": "govreg://tenant/doc",
            "metadata": {"source": "C:\\Users\\someone\\rules.hwp", "auth": "Bearer abcdefghijklmnopqrstuvwxyz"},
        }
        _summary, findings = gate.run_chain_smoke(runtime, profile="chatgpt-data", profile_bound=False)
        patterns = sorted(item["pattern"] for item in findings if item["code"] == "public-output-unsafe-value")
        self.assertEqual(["bearer-token", "local-govreg-uri", "windows-drive-path"], patterns)
        self.assertTrue(all("someone" not in str(item) for item in findings))

        _summary, findings = gate.run_chain_smoke(runtime, profile="full", profile_bound=False)
        patterns = sorted(item["pattern"] for item in findings if item["code"] == "public-output-unsafe-value")
        self.assertEqual(["bearer-token", "windows-drive-path"], patterns)

    def test_large_responses_are_target_gaps(self) -> None:
        runtime = _FakeRuntime()
        runtime.responses["search"]["results"][0]["title"] = "x" * 20_000
        _summary, findings = gate.run_chain_smoke(runtime, profile="full", profile_bound=False)
        self.assertEqual([("tool-response-tokens-above-target", "target", "search")], [
            (item["code"], item["severity"], item["tool"]) for item in findings
        ])


class PolishAndReportTests(unittest.TestCase):
    def _bundle(self, root: Path, *, readme_ko: str = "설치한 뒤 연결을 확인합니다.\n") -> Path:
        bundle = root / "bundle"
        bundle.mkdir()
        (bundle / SETUP_BUNDLE_FILES["readme"]).write_text("# Bundle\n", encoding="utf-8")
        (bundle / SETUP_BUNDLE_FILES["readme_ko"]).write_text(readme_ko, encoding="utf-8")
        (bundle / SETUP_BUNDLE_FILES["usage_guide"]).write_text("연결 파일을 실행합니다.\n", encoding="utf-8")
        return bundle

    def test_bundle_guides_are_polish_checked_without_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(Path(tmp), readme_ko="<!-- slop-gate: off -->\n설치 — 완료\n")
            summary, findings = gate.check_polish(bundle, {"chatgpt-data": _SYNTHETIC_SURFACE})
        self.assertEqual(["polish-phrase"], _codes(findings))
        self.assertEqual((SETUP_BUNDLE_FILES["readme_ko"], 2, "dash"), (
            findings[0]["source"], findings[0]["line"], findings[0]["rule"],
        ))
        self.assertIn("mcp:chatgpt-data:instructions", summary["checked_sources"])

    def test_korean_tool_descriptions_are_polish_checked(self) -> None:
        surface = copy.deepcopy(_SYNTHETIC_SURFACE)
        surface["tools"][0]["description"] = "승인 규정을 검색합니다 — 결과 id를 fetch에 넘깁니다."
        _summary, findings = gate.check_polish(None, {"full": surface})
        self.assertEqual([("polish-phrase", "mcp:full:search")], [(f["code"], f["source"]) for f in findings])

    def test_missing_guides_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(Path(tmp))
            (bundle / SETUP_BUNDLE_FILES["usage_guide"]).unlink()
            _summary, findings = gate.check_polish(bundle, {})
        self.assertEqual(["polish-guide-missing"], _codes(findings))

    def test_require_targets_promotes_gaps_and_report_is_identifier_free(self) -> None:
        runtime = _FakeRuntime()

        class _Server:
            instructions = _SYNTHETIC_SURFACE["instructions"]

            def __init__(self, **_kwargs) -> None:
                pass

        def factory(**kwargs):
            return _Server(**kwargs)

        surface = copy.deepcopy(_SYNTHETIC_SURFACE)
        surface["tools"][0]["description"] += " " + "Detail. " * 60
        description_chars = len(surface["tools"][0]["description"])
        with (
            patch.object(gate, "collect_tool_surface", return_value=surface),
            patch.object(gate, "server_tool_caller", return_value=runtime),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                bundle = self._bundle(Path(tmp))
                relaxed = gate.run_mcp_build_qa_gate(
                    data_dir=bundle / "data",
                    tenant_id="tenant-secret-name",
                    profile_id="institution-secret-profile",
                    bundle_dir=bundle,
                    tool_profiles=("chatgpt-data",),
                    server_factory=factory,
                )
                strict = gate.run_mcp_build_qa_gate(
                    data_dir=bundle / "data",
                    tenant_id="tenant-secret-name",
                    profile_id="institution-secret-profile",
                    bundle_dir=bundle,
                    tool_profiles=("chatgpt-data",),
                    require_targets=True,
                    server_factory=factory,
                )

        self.assertTrue(relaxed["passed"])
        self.assertFalse(relaxed["targets_met"])
        self.assertEqual(1, relaxed["target_gap_count"])
        self.assertFalse(strict["passed"])
        self.assertEqual(1, strict["blocker_count"])
        rendered = str(relaxed)
        for private in ("tenant-secret-name", "institution-secret-profile", "합성 복무규정", "unit-a", str(tmp)):
            self.assertNotIn(private, rendered)
        self.assertEqual(
            [f"tool-description-above-target ({description_chars}/400) · chatgpt-data · search"],
            gate.describe_findings_ko(strict),
        )

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            gate.run_mcp_build_qa_gate(data_dir=None, tool_profiles=("everything",))


if __name__ == "__main__":
    unittest.main()
