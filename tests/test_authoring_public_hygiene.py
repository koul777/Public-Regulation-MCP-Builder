from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AuthoringPublicHygieneTests(unittest.TestCase):
    def test_local_authoring_runtime_data_is_gitignored(self) -> None:
        gitignore_path = PROJECT_ROOT / ".gitignore"
        if not gitignore_path.is_file():
            self.skipTest("source-control metadata is intentionally absent from the sdist")
        ignore_lines = {
            line.strip()
            for line in gitignore_path.read_text(encoding="utf-8").splitlines()
        }

        self.assertIn("data/authoring/", ignore_lines)

    def test_docs_keep_draft_and_official_approval_distinct(self) -> None:
        quickstart = (PROJECT_ROOT / "docs" / "authoring_quickstart_ko.md").read_text(
            encoding="utf-8"
        )
        contract = (PROJECT_ROOT / "docs" / "authoring_mvp_contract_ko.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("공식 승인 아님", quickstart)
        self.assertIn("공식 승인 아님", contract)
        self.assertIn("자동", quickstart)
        self.assertIn("Document", contract)

    def test_beginner_docs_separate_local_practice_from_protected_review(self) -> None:
        quickstart = (PROJECT_ROOT / "docs" / "authoring_quickstart_ko.md").read_text(
            encoding="utf-8"
        )
        contract = (PROJECT_ROOT / "docs" / "authoring_mvp_contract_ko.md").read_text(
            encoding="utf-8"
        )

        for document in (quickstart, contract):
            with self.subTest(document=document[:40]):
                self.assertIn("로컬 1인 연습", document)
                self.assertIn("보호된 2인 실무 흐름", document)
                self.assertIn("자체 확인", document)
                self.assertIn("대신하지 않", document)

        self.assertNotIn("다음 행동 한 가지", quickstart)
        self.assertNotIn("다음 행동 한 가지", contract)

    def test_local_practice_change_request_copy_is_role_neutral(self) -> None:
        page_source = (PROJECT_ROOT / "frontend" / "authoring_page.py").read_text(
            encoding="utf-8"
        )
        contract = (PROJECT_ROOT / "docs" / "authoring_mvp_contract_ko.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("수정 요청이 기록되었습니다.", page_source)
        self.assertNotIn("확인자가 수정을 요청했습니다.", page_source)
        self.assertIn("수정 요청이 기록되어 다시 작성해야 하는 단계", contract)
        self.assertNotIn("확인자가 수정을 요청한 단계", contract)

    def test_source_distribution_includes_beginner_authoring_docs(self) -> None:
        manifest = (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        for filename in (
            "authoring_beginner_improvement_backlog_ko.md",
            "authoring_beginner_pilot_facilitator_script_ko.md",
            "authoring_beginner_usability_test_plan_ko.md",
            "authoring_claude_audit_ko.md",
            "authoring_go_nogo_memo_template_ko.md",
            "authoring_mvp_contract_ko.md",
            "authoring_quickstart_ko.md",
            "authoring_security_model_ko.md",
            "authoring_verification_report_ko.md",
            "authoring_workspace_rollout_plan_ko.md",
        ):
            with self.subTest(filename=filename):
                self.assertIn(f"include docs/{filename}", manifest)

    def test_pilot_plan_and_facilitator_script_use_the_same_core_scenario(self) -> None:
        plan = (
            PROJECT_ROOT / "docs" / "authoring_beginner_usability_test_plan_ko.md"
        ).read_text(encoding="utf-8")
        script = (
            PROJECT_ROOT / "docs" / "authoring_beginner_pilot_facilitator_script_ko.md"
        ).read_text(encoding="utf-8")

        for document in (plan, script):
            with self.subTest(document=document[:40]):
                self.assertIn("필수 조문 하나를", document)
                self.assertIn("제99조", document)
                self.assertIn("30분", document)
        self.assertIn("전체 45분", script)
        self.assertNotIn("1인당 20~30분", script)


if __name__ == "__main__":
    unittest.main()
