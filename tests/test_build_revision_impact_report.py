import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_revision_impact_report import (
    compare_chunk_units,
    make_markdown,
    write_revision_impact_report,
)


class BuildRevisionImpactReportTests(unittest.TestCase):
    def test_compares_units_and_marks_approval_work(self) -> None:
        before = [
            article("before-a1", "10", "Leave", "Leave may be used for one year."),
            article("before-a2", "20", "Pay", "Pay is unchanged."),
            appendix("before-table", "leave-table", "Leave table v1"),
        ]
        after = [
            article("after-a1", "10", "Leave", "Leave may be used for two years."),
            article("after-a2", "20", "Pay", "Pay is unchanged."),
            article("after-a3", "30", "Procedure", "Apply through HR."),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["added_count"])
        self.assertEqual(1, report["summary"]["removed_count"])
        self.assertEqual(1, report["summary"]["unchanged_count"])
        self.assertEqual(3, report["summary"]["approval_required_count"])
        self.assertEqual(1, report["summary"]["approval_reuse_candidate_count"])
        self.assertEqual(
            ["changed", "added", "removed"],
            [row["change_type"] for row in report["review_queue"]],
        )
        self.assertEqual("review_changed_unit_before_reindex", report["review_queue"][0]["review_action"])
        self.assertTrue(report["review_queue"][0]["approval_required"])
        self.assertTrue(report["unchanged"][0]["approval_reuse_candidate"])

    def test_groups_multiple_chunks_for_same_article_before_hashing(self) -> None:
        before = [
            article("before-a1-part1", "10", "Leave", "Part one."),
            article("before-a1-part2", "10", "Leave", "Part two."),
        ]
        after = [
            article("after-a1-part1", "10", "Leave", "Part one."),
            article("after-a1-part2", "10", "Leave", "Part two changed."),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(1, report["summary"]["before_unit_count"])
        self.assertEqual(1, report["summary"]["after_unit_count"])
        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertIn("before-a1-part1", report["changed"][0]["before_chunk_ids"])
        self.assertIn("before-a1-part2", report["changed"][0]["before_chunk_ids"])

    def test_supplementary_article_does_not_collapse_into_main_article(self) -> None:
        before = [
            article("main-a1", "제1조", "목적", "본칙 목적은 그대로 둔다."),
            article(
                "supp-a1",
                "제1조",
                "시행일",
                "이 규정은 2026년 1월 1일부터 시행한다.",
                is_supplementary_provision=True,
                supplementary_identifier_date="2026-01-01",
                supplementary_label="부칙",
            ),
        ]
        after = [
            article("main-a1-after", "제1조", "목적", "본칙 목적은 그대로 둔다."),
            article(
                "supp-a1-after",
                "제1조",
                "시행일",
                "이 규정은 2026년 2월 1일부터 시행한다.",
                is_supplementary_provision=True,
                supplementary_identifier_date="2026-01-01",
                supplementary_label="부칙",
            ),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(2, report["summary"]["before_unit_count"])
        self.assertEqual(2, report["summary"]["after_unit_count"])
        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["unchanged_count"])
        self.assertEqual("supplementary_article", report["changed"][0]["unit_type"])
        self.assertEqual("main-a1", report["unchanged"][0]["before_chunk_ids"])

    def test_same_article_number_under_different_regulations_is_scoped(self) -> None:
        before = [
            article("reg-a-article-1", "1", "Purpose", "Regulation A purpose.", regulation_title="Regulation A"),
            article("reg-b-article-1", "1", "Purpose", "Regulation B purpose.", regulation_title="Regulation B"),
        ]
        after = [
            article("reg-a-article-1-after", "1", "Purpose", "Regulation A purpose.", regulation_title="Regulation A"),
            article(
                "reg-b-article-1-after",
                "1",
                "Purpose",
                "Regulation B purpose changed.",
                regulation_title="Regulation B",
            ),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(2, report["summary"]["before_unit_count"])
        self.assertEqual(2, report["summary"]["after_unit_count"])
        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["unchanged_count"])
        self.assertEqual("reg-b-article-1", report["changed"][0]["before_chunk_ids"])
        self.assertEqual("reg-a-article-1", report["unchanged"][0]["before_chunk_ids"])

    def test_same_appendix_number_under_different_regulations_is_scoped(self) -> None:
        before = [
            appendix("reg-a-app-1", "leave-table", "Regulation A appendix.", regulation_title="Regulation A"),
            appendix("reg-b-app-1", "leave-table", "Regulation B appendix.", regulation_title="Regulation B"),
        ]
        after = [
            appendix("reg-a-app-1-after", "leave-table", "Regulation A appendix.", regulation_title="Regulation A"),
            appendix("reg-b-app-1-after", "leave-table", "Regulation B appendix changed.", regulation_title="Regulation B"),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(2, report["summary"]["before_unit_count"])
        self.assertEqual(2, report["summary"]["after_unit_count"])
        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["unchanged_count"])
        self.assertEqual("reg-b-app-1", report["changed"][0]["before_chunk_ids"])
        self.assertEqual("reg-a-app-1", report["unchanged"][0]["before_chunk_ids"])

    def test_reordered_chunks_for_same_article_are_stable(self) -> None:
        before = [
            article("a1-part2", "10", "Leave", "Part two."),
            article("a1-part1", "10", "Leave", "Part one."),
        ]
        after = [
            article("a1-part1", "10", "Leave", "Part one."),
            article("a1-part2", "10", "Leave", "Part two."),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(0, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["unchanged_count"])
        self.assertEqual("a1-part1;a1-part2", report["unchanged"][0]["before_chunk_ids"])
        self.assertEqual("a1-part1;a1-part2", report["unchanged"][0]["after_chunk_ids"])

    def test_metadata_only_change_requires_review(self) -> None:
        before = [
            article(
                "before-a1",
                "10",
                "Effective Date",
                "This article text is unchanged.",
                effective_date="2026-01-01",
                revision_history=[{"event_type": "revision", "date": "2025-12-01"}],
            ),
        ]
        after = [
            article(
                "after-a1",
                "10",
                "Effective Date",
                "This article text is unchanged.",
                effective_date="2026-02-01",
                revision_history=[{"event_type": "revision", "date": "2026-01-15"}],
            ),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["metadata_only_changed_count"])
        self.assertEqual(0, report["summary"]["unchanged_count"])
        self.assertEqual(1, report["summary"]["approval_required_count"])
        row = report["changed"][0]
        self.assertTrue(row["metadata_only_change"])
        self.assertFalse(row["approval_reuse_candidate"])
        self.assertEqual("review_changed_unit_before_reindex", row["review_action"])
        self.assertIn("effective_date", row["metadata_change_fields"])
        self.assertIn("revision_history", row["metadata_change_fields"])

    def test_security_metadata_change_blocks_approval_reuse(self) -> None:
        before = [
            article(
                "before-a1",
                "10",
                "Security Scope",
                "Access rules are unchanged.",
                security_level="internal",
                approval_status="approved",
            ),
        ]
        after = [
            article(
                "after-a1",
                "10",
                "Security Scope",
                "Access rules are unchanged.",
                security_level="confidential",
                approval_status="pending_review",
            ),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["metadata_only_changed_count"])
        self.assertEqual(0, report["summary"]["approval_reuse_candidate_count"])
        self.assertIn("security_level", report["changed"][0]["metadata_change_fields"])
        self.assertIn("approval_status", report["changed"][0]["metadata_change_fields"])

    def test_department_acl_change_blocks_approval_reuse(self) -> None:
        before = [
            article(
                "before-a1",
                "10",
                "Security Scope",
                "Access rules are unchanged.",
                department_acl=["hr"],
            ),
        ]
        after = [
            article(
                "after-a1",
                "10",
                "Security Scope",
                "Access rules are unchanged.",
                department_acl=["finance"],
            ),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["metadata_only_changed_count"])
        self.assertEqual(0, report["summary"]["approval_reuse_candidate_count"])
        self.assertIn("department_acl", report["changed"][0]["metadata_change_fields"])

    def test_source_identity_is_reported_without_forcing_reapproval(self) -> None:
        before = [
            article(
                "before-a1",
                "10",
                "Leave",
                "Leave rules are unchanged.",
                institution_name="KOGAS",
                apba_id="C0147",
                profile_id="public_portal-c0147",
                source_system="PUBLIC_PORTAL",
                source_record_id="rule-10",
                source_file_id="file-v1",
            ),
        ]
        after = [
            article(
                "after-a1",
                "10",
                "Leave",
                "Leave rules are unchanged.",
                institution_name="KOGAS",
                apba_id="C0147",
                profile_id="public_portal-c0147",
                source_system="PUBLIC_PORTAL",
                source_record_id="rule-10",
                source_file_id="file-v2",
            ),
        ]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")

        self.assertEqual(0, report["summary"]["changed_count"])
        self.assertEqual(1, report["summary"]["unchanged_count"])
        self.assertEqual(1, report["summary"]["approval_reuse_candidate_count"])
        row = report["unchanged"][0]
        self.assertEqual("C0147", row["apba_id"])
        self.assertEqual("public_portal-c0147", row["profile_id"])
        self.assertEqual("rule-10", row["before_source_record_id"])
        self.assertEqual("rule-10", row["after_source_record_id"])
        self.assertEqual("file-v1", row["before_source_file_id"])
        self.assertEqual("file-v2", row["after_source_file_id"])

    def test_writes_json_markdown_and_csv_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before_path = root / "before_chunks.json"
            after_path = root / "after_chunks.json"
            before_path.write_text(
                json.dumps([article("before-a1", "10", "Leave", "One year.", source_file_id="file-v1")]),
                encoding="utf-8",
            )
            after_path.write_text(
                json.dumps([article("after-a1", "10", "Leave", "Two years.", source_file_id="file-v2")]),
                encoding="utf-8",
            )

            outputs = write_revision_impact_report(before_path, after_path, root / "revision_impact")

            report = json.loads(outputs["json"].read_text(encoding="utf-8"))
            markdown = outputs["markdown"].read_text(encoding="utf-8")
            csv_text = outputs["csv"].read_text(encoding="utf-8-sig")

        self.assertEqual(1, report["summary"]["changed_count"])
        self.assertIn("Revision Impact Report", markdown)
        self.assertIn("review_changed_unit_before_reindex", csv_text)
        self.assertIn("before_source_file_id,after_source_file_id", csv_text)
        self.assertIn("file-v1,file-v2", csv_text)

    def test_markdown_includes_all_review_queue_rows(self) -> None:
        before = [article(f"before-a{index}", str(index), "Rule", f"Before {index}.") for index in range(81)]
        after = [article(f"after-a{index}", str(index), "Rule", f"After {index}.") for index in range(81)]

        report = compare_chunk_units(before, after, before_label="v1", after_label="v2")
        markdown = make_markdown(report)

        self.assertEqual(81, report["summary"]["changed_count"])
        self.assertEqual(81, markdown.count("| changed | article |"))


def article(chunk_id: str, article_no: str, title: str, text: str, **metadata: object) -> dict:
    base_metadata = {
        "article_no": article_no,
        "article_title": title,
    }
    base_metadata.update(metadata)
    return {
        "chunk_id": chunk_id,
        "chunk_type": "article",
        "text": text,
        "metadata": base_metadata,
    }


def appendix(chunk_id: str, table_id: str, text: str, **metadata: object) -> dict:
    base_metadata = {
        "table_like": True,
        "table_id": table_id,
        "table_title": "Leave table",
    }
    base_metadata.update(metadata)
    return {
        "chunk_id": chunk_id,
        "chunk_type": "appendix",
        "text": text,
        "metadata": base_metadata,
    }
