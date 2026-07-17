from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_temporal_ambiguity_review_scope import build_temporal_ambiguity_review_scope, main


class TemporalAmbiguityReviewScopeTests(unittest.TestCase):
    def test_builds_review_scope_from_temporal_report_and_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            temporal = root / "temporal.json"
            _write_jsonl(
                vectors,
                [
                    _record("article-1", "article", ["effective_date", "revision_date"]),
                    _record("article-2", "article", ["effective_date"]),
                    _record("form-1", "form", ["effective_date"]),
                    _record("clean-1", "article", []),
                ],
            )
            _write_json(temporal, _temporal_payload(vectors))

            report = build_temporal_ambiguity_review_scope(
                temporal_report=temporal,
                sample_limit_per_slice=2,
            )

        self.assertEqual("temporal_ambiguity_review_scope", report["report_type"])
        self.assertEqual("temporal_ambiguity_policy_required", report["status"])
        self.assertFalse(report["passed"])
        self.assertEqual(3, report["record_analysis"]["ambiguous_record_count"])
        self.assertEqual({"article": 2, "form": 1}, report["record_analysis"]["ambiguous_by_chunk_type"])
        self.assertEqual(
            {"effective_date": 3, "revision_date": 1},
            report["record_analysis"]["ambiguous_by_field_from_records"],
        )
        by_type_field = report["record_analysis"]["ambiguous_by_chunk_type_and_field"]
        self.assertEqual(2, by_type_field["article"]["effective_date"])
        self.assertEqual(1, by_type_field["article"]["revision_date"])
        decision_ids = {item["decision_id"] for item in report["decision_requirements"]}
        self.assertIn("temporal_ambiguity_index_policy", decision_ids)
        self.assertIn("temporal_ambiguity_answer_policy", decision_ids)
        self.assertEqual("article:effective_date", report["recommended_review_sequence"][0]["slice_id"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vectors = root / "approved_vectors.jsonl"
            temporal = root / "temporal.json"
            out_json = root / "scope.json"
            out_md = root / "scope.md"
            _write_jsonl(vectors, [_record("article-1", "article", ["effective_date"])])
            _write_json(temporal, _temporal_payload(vectors, ambiguous_count=1))
            stdout = io.StringIO()

            exit_code = main(
                [
                    "--temporal-report",
                    str(temporal),
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ],
                stdout=stdout,
            )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual("temporal_ambiguity_review_scope", payload["report_type"])
        self.assertIn("Temporal Ambiguity Review Scope", markdown)
        self.assertIn("temporal_ambiguity_index_policy", markdown)
        self.assertIn('"record_analysis"', stdout.getvalue())


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _temporal_payload(vector_path: Path, *, ambiguous_count: int = 3) -> dict[str, object]:
    return {
        "report_type": "temporal_backfill_shadow_runtime",
        "passed": True,
        "shadow_runtime_written": True,
        "write_blocked": False,
        "vector_path": str(vector_path),
        "output_chunk_count": 4,
        "before": {
            "chunk_count": 4,
            "temporal_metadata_count": 1,
            "temporal_metadata_ratio": 0.25,
            "conflict_chunk_count": 0,
            "ambiguous_chunk_count": 0,
            "ambiguous_field_counts": {},
        },
        "after": {
            "chunk_count": 4,
            "temporal_metadata_count": 2,
            "temporal_metadata_ratio": 0.5,
            "conflict_chunk_count": 0,
            "ambiguous_chunk_count": ambiguous_count,
            "ambiguous_field_counts": {"effective_date": ambiguous_count, "revision_date": 1},
        },
        "delta": {
            "temporal_metadata_count": 1,
            "conflict_chunk_count": 0,
            "ambiguous_chunk_count": ambiguous_count,
        },
    }


def _record(chunk_id: str, chunk_type: str, ambiguous_fields: list[str]) -> dict[str, object]:
    metadata: dict[str, object] = {
        "chunk_id": chunk_id,
        "document_id": "doc-demo",
        "chunk_type": chunk_type,
        "regulation_title": "Demo Regulation",
        "article_no": "Article 1",
        "source_page_start": 1,
    }
    if ambiguous_fields:
        metadata["temporal_metadata_ambiguous_fields"] = ambiguous_fields
    return {
        "id": f"doc-demo:{chunk_id}",
        "document_id": "doc-demo",
        "chunk_id": chunk_id,
        "text": f"Text for {chunk_id}",
        "metadata": metadata,
    }


if __name__ == "__main__":
    unittest.main()
