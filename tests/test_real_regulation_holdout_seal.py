from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.run_real_regulation_e2e import (
    EvidenceError,
    _ledger_lock,
    check_holdout_gate,
    open_holdout,
)
from scripts.seal_real_regulation_holdout import main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Query text, ids and chunk ids that must never reach the ledger.
DEVELOPMENT_QUERY = "synthetic development question about leave"
HOLDOUT_QUERY = "synthetic holdout question about travel"


def _case(case_id: str, query: str, *chunks: str, **extra) -> dict:
    return {"id": case_id, "query": query, "target_document_ids": ["synthetic-document"],
            "target_chunk_ids": list(chunks), **extra}


class HoldoutSealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="holdout_seal_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.development = self.root / "development.json"
        self.holdout = self.root / "holdout.json"
        self.ledger = self.root / "private" / "holdout-ledger.jsonl"
        self.write_specs()

    def write_specs(self, *, development: dict | None = None, holdout: dict | None = None) -> None:
        self.development.write_text(json.dumps(development if development is not None else {
            "split": "development",
            "queries": [_case("dev-1", DEVELOPMENT_QUERY, "chunk-dev-1"), _case("dev-2", "another development question", "chunk-dev-2")],
        }), encoding="utf-8")
        self.holdout.write_text(json.dumps(holdout if holdout is not None else {
            "split": "holdout",
            "queries": [_case("hold-1", HOLDOUT_QUERY, "chunk-hold-1")],
        }), encoding="utf-8")

    def seal(self, *extra: str) -> tuple[int, dict]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["--development", str(self.development), "--holdout", str(self.holdout),
                         "--ledger", str(self.ledger), *extra])
        return code, json.loads(stream.getvalue())

    def sha(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def ledger_records(self) -> list[dict]:
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]

    def assert_refused(self, reason: str) -> None:
        existed = self.ledger.exists()
        before = self.ledger.read_bytes() if existed else None
        code, summary = self.seal()
        self.assertEqual((2, False, reason), (code, summary["sealed"], summary["reason_code"]))
        self.assertEqual(existed, self.ledger.exists())
        if existed:
            self.assertEqual(before, self.ledger.read_bytes())

    def test_disjoint_specs_are_sealed_with_hashes_and_counts_only(self) -> None:
        code, summary = self.seal()
        self.assertEqual((0, True), (code, summary["sealed"]))
        (record,) = self.ledger_records()
        self.assertEqual("sealed", record["event"])
        self.assertEqual(self.sha(self.holdout), record["query_spec_sha256"])
        self.assertEqual(self.sha(self.development), record["development_spec_sha256"])
        self.assertEqual((1, 2), (record["query_count"], record["development_query_count"]))
        self.assertEqual({"event", "schema_version", "ledger_id", "recorded_at", "query_spec_sha256", "query_count",
                          "development_spec_sha256", "development_query_count"}, set(record))
        self.assertRegex(record["ledger_id"], "^[0-9a-f]{32}$")
        self.assertEqual((self.sha(self.holdout), record["ledger_id"]), (summary["holdout_spec_sha256"], summary["ledger_id"]))

        text = self.ledger.read_text(encoding="utf-8")
        for private in (DEVELOPMENT_QUERY, HOLDOUT_QUERY, "another development question", "chunk-dev-1", "chunk-hold-1",
                        "dev-1", "hold-1", "synthetic-document", str(self.root), self.root.name, "development.json", "holdout.json"):
            self.assertNotIn(private, text)

    def test_run_gate_accepts_the_sealed_hash_and_no_other(self) -> None:
        self.seal()
        ledger_id = self.ledger_records()[0]["ledger_id"]
        self.assertEqual({"authorized_rerun": False, "ledger_id": ledger_id},
                         check_holdout_gate(self.ledger, self.sha(self.holdout)))
        for other in (self.sha(self.development), "0" * 64):
            with self.assertRaisesRegex(EvidenceError, "holdout_not_sealed"):
                check_holdout_gate(self.ledger, other)
        open_holdout(self.ledger, self.sha(self.holdout), query_count=1)
        with self.assertRaisesRegex(EvidenceError, "holdout_already_opened"):
            check_holdout_gate(self.ledger, self.sha(self.holdout))

    def test_overlapping_specs_are_refused_without_creating_the_ledger(self) -> None:
        cases = {
            "development_and_holdout_share_query_ids": (
                [_case("dev-1", DEVELOPMENT_QUERY, "chunk-dev-1")], [_case("dev-1", HOLDOUT_QUERY, "chunk-hold-1")]),
            "development_and_holdout_share_target_chunk_ids": (
                [_case("dev-1", DEVELOPMENT_QUERY, "chunk-shared")], [_case("hold-1", HOLDOUT_QUERY, "chunk-a", "chunk-shared")]),
            "development_and_holdout_share_query_text": (
                [_case("dev-1", DEVELOPMENT_QUERY, "chunk-dev-1")], [_case("hold-1", "  Synthetic   DEVELOPMENT question about LEAVE ", "chunk-hold-1")]),
        }
        for reason, (development, holdout) in cases.items():
            self.write_specs(development={"split": "development", "queries": development},
                             holdout={"split": "holdout", "queries": holdout})
            with self.subTest(reason=reason):
                self.assert_refused(reason)

    def test_chunks_named_only_in_allowed_citations_count_as_shared(self) -> None:
        citation = {"allowed_citations": [{"document_id": "synthetic-document", "chunk_id": "chunk-cited"}]}
        self.write_specs(
            development={"split": "development", "queries": [_case("dev-1", DEVELOPMENT_QUERY, "chunk-dev-1", **citation)]},
            holdout={"split": "holdout", "queries": [_case("hold-1", HOLDOUT_QUERY, "chunk-cited")]})
        self.assert_refused("development_and_holdout_share_target_chunk_ids")

    def test_wrong_or_missing_split_labels_are_refused(self) -> None:
        development = {"queries": [_case("dev-1", DEVELOPMENT_QUERY, "chunk-dev-1")]}
        holdout = {"queries": [_case("hold-1", HOLDOUT_QUERY, "chunk-hold-1")]}
        variants = (
            ("development_spec_split_mismatch", {**development, "split": "holdout"}, {**holdout, "split": "holdout"}),
            ("development_spec_split_mismatch", development, {**holdout, "split": "holdout"}),
            ("holdout_spec_split_mismatch", {**development, "split": "development"}, {**holdout, "split": "development"}),
            ("holdout_spec_split_mismatch", {**development, "split": "development"}, holdout),
            ("query_split_invalid", {**development, "split": "dev"}, {**holdout, "split": "holdout"}),
            ("query_split_invalid", {**development, "split": "development"}, {**holdout, "split": None}),
        )
        for reason, development_spec, holdout_spec in variants:
            self.write_specs(development=development_spec, holdout=holdout_spec)
            with self.subTest(reason=reason, development=development_spec.get("split"), holdout=holdout_spec.get("split")):
                self.assert_refused(reason)

    def test_malformed_specs_are_refused(self) -> None:
        good = {"split": "holdout", "queries": [_case("hold-1", HOLDOUT_QUERY, "chunk-hold-1")]}
        variants = (
            ("seal_spec_has_no_cases", {**good, "queries": []}),
            ("seal_spec_has_no_cases", {**good, "queries": ["not an object"]}),
            ("seal_spec_query_ids_invalid", {**good, "queries": [_case("same", "q1"), _case("same", "q2")]}),
            ("seal_spec_query_ids_invalid", {**good, "queries": [_case("", "q1")]}),
            ("case_level_split_not_supported", {**good, "queries": [_case("hold-1", HOLDOUT_QUERY, split="development")]}),
        )
        for reason, holdout in variants:
            self.write_specs(holdout=holdout)
            with self.subTest(reason=reason):
                self.assert_refused(reason)
        self.holdout.write_text("[]", encoding="utf-8")
        self.assert_refused("expected_json_object")

    def test_same_file_or_ledger_inside_an_input_is_refused(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["--development", str(self.holdout), "--holdout", str(self.holdout), "--ledger", str(self.ledger)])
        self.assertEqual((2, "development_and_holdout_must_be_different_files"), (code, json.loads(stream.getvalue())["reason_code"]))
        for path in (self.development, self.holdout):
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main(["--development", str(self.development), "--holdout", str(self.holdout), "--ledger", str(path)])
            self.assertEqual((2, "holdout_ledger_overlaps_spec"), (code, json.loads(stream.getvalue())["reason_code"]))
        self.assertFalse(self.ledger.exists())

    def test_public_checkout_and_relative_paths_are_refused(self) -> None:
        for ledger, reason in ((PROJECT_ROOT / "holdout-ledger.jsonl", "private_path_inside_public_checkout"),
                               (Path("holdout-ledger.jsonl"), "private_path_must_be_absolute")):
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main(["--development", str(self.development), "--holdout", str(self.holdout), "--ledger", str(ledger)])
            self.assertEqual((2, reason), (code, json.loads(stream.getvalue())["reason_code"]))
            self.assertFalse(ledger.exists())

    def test_ledger_is_append_only_and_a_hash_is_sealed_once(self) -> None:
        self.assertEqual(0, self.seal()[0])
        first = self.ledger.read_bytes()
        self.assert_refused("holdout_already_sealed")

        second_holdout = {"split": "holdout", "queries": [_case("hold-2", "second holdout question", "chunk-hold-2")]}
        self.write_specs(holdout=second_holdout)
        self.assertEqual(0, self.seal()[0])
        grown = self.ledger.read_bytes()
        self.assertTrue(grown.startswith(first))
        self.assertGreater(len(grown), len(first))
        self.assertEqual(["sealed", "sealed"], [record["event"] for record in self.ledger_records()])
        self.assertEqual(1, len({record["ledger_id"] for record in self.ledger_records()}))
        self.assertEqual(self.sha(self.holdout), self.ledger_records()[-1]["query_spec_sha256"])

    def test_resealing_an_opened_holdout_does_not_reset_it(self) -> None:
        self.seal()
        open_holdout(self.ledger, self.sha(self.holdout), query_count=1)
        self.assert_refused("holdout_already_sealed")
        with self.assertRaisesRegex(EvidenceError, "holdout_already_opened"):
            check_holdout_gate(self.ledger, self.sha(self.holdout))

    def test_damaged_ledger_is_never_extended_and_a_lock_blocks_sealing(self) -> None:
        self.ledger.parent.mkdir()
        self.ledger.write_bytes(b'{"event": "sealed"')
        self.assert_refused("holdout_ledger_corrupt")
        self.ledger.unlink()
        self.assertEqual(0, self.seal()[0])
        self.write_specs(holdout={"split": "holdout", "queries": [_case("hold-2", "second holdout question", "chunk-hold-2")]})
        with _ledger_lock(self.ledger):
            self.assert_refused("holdout_ledger_locked")
        self.assertEqual(1, len(self.ledger_records()))


    def test_expected_chunk_id_aliases_count_as_shared_chunks(self) -> None:
        for development_key, holdout_key in (("expected_chunk_ids", "target_chunk_ids"),
                                             ("target_chunk_ids", "expected_chunk_ids"),
                                             ("expected_chunk_ids", "expected_chunk_ids"),
                                             ("expected_chunk_id", "target_chunk_id")):
            development_case = {"id": "dev-1", "query": DEVELOPMENT_QUERY}
            holdout_case = {"id": "hold-1", "query": HOLDOUT_QUERY}
            development_case[development_key] = ["chunk-shared"] if development_key.endswith("ids") else "chunk-shared"
            holdout_case[holdout_key] = ["chunk-shared"] if holdout_key.endswith("ids") else "chunk-shared"
            self.write_specs(development={"split": "development", "queries": [development_case]},
                             holdout={"split": "holdout", "queries": [holdout_case]})
            with self.subTest(development=development_key, holdout=holdout_key):
                self.assert_refused("development_and_holdout_share_target_chunk_ids")

    def test_query_text_is_compared_after_unicode_normalization(self) -> None:
        question = "synthetic test question about leave"
        variants = (
            "\uff53\uff59\uff4e\uff54\uff48\uff45\uff54\uff49\uff43\u3000\uff54\uff45\uff53\uff54\u3000\uff51\uff55\uff45\uff53\uff54\uff49\uff4f\uff4e\u3000\uff41\uff42\uff4f\uff55\uff54\u3000\uff4c\uff45\uff41\uff56\uff45",  # full-width forms
            "Synthetic\u00a0test\tquestion  about\nLEAVE",  # no-break space, tab, newline, case
            "synthetic test question about leave\u3000",  # ideographic space
        )
        for variant in variants:
            self.write_specs(development={"split": "development", "queries": [_case("dev-1", question, "chunk-dev-1")]},
                             holdout={"split": "holdout", "queries": [_case("hold-1", variant, "chunk-hold-1")]})
            with self.subTest(variant=variant.strip()[:12]):
                self.assert_refused("development_and_holdout_share_query_text")

    def test_question_key_text_is_compared_with_query_text(self) -> None:
        holdout_case = {"id": "hold-1", "question": DEVELOPMENT_QUERY.upper(), "target_chunk_ids": ["chunk-hold-1"]}
        self.write_specs(holdout={"split": "holdout", "queries": [holdout_case]})
        self.assert_refused("development_and_holdout_share_query_text")

    def test_hard_linked_ledger_is_never_extended(self) -> None:
        self.assertEqual(0, self.seal()[0])
        alias = self.ledger.with_name("alias.jsonl")
        try:
            os.link(self.ledger, alias)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("the filesystem refuses hard links")
        self.write_specs(holdout={"split": "holdout", "queries": [_case("hold-2", "second holdout question", "chunk-hold-2")]})
        self.assert_refused("holdout_ledger_hard_linked")
        self.assertEqual(self.ledger.read_bytes(), alias.read_bytes())


if __name__ == "__main__":
    unittest.main()
