from __future__ import annotations

from datetime import date
import unittest
from uuid import uuid4

from app.schemas.authoring import (
    AuthoringMode,
    AuthoringProject,
    BeginnerChecklistItem,
    ClauseDraft,
    DraftNodeType,
)
from app.services.authoring_lint_service import AuthoringLintService
from app.services.authoring_template_service import AuthoringTemplateService


class AuthoringLintServiceTests(unittest.TestCase):
    def test_parent_cycle_is_blocking(self) -> None:
        project = _valid_project()
        first, second, *remaining = project.clauses
        cyclic = project.model_copy(
            update={
                "clauses": [
                    first.model_copy(update={"parent_id": second.clause_id}),
                    second.model_copy(update={"parent_id": first.clause_id}),
                    *remaining,
                ]
            }
        )

        report = AuthoringLintService().lint(cyclic)

        cycle_findings = [
            finding for finding in report.findings if finding.code == "parent_cycle"
        ]
        self.assertEqual(2, len(cycle_findings))
        self.assertTrue(all(finding.severity.value == "error" for finding in cycle_findings))

    def setUp(self) -> None:
        self.service = AuthoringLintService()

    def test_valid_project_has_no_blocking_findings(self) -> None:
        project = _valid_project()
        project.clauses[1] = project.clauses[1].model_copy(
            update={"body": "이 규정은 제4조에 따른 담당자에게 적용한다."}
        )

        report = self.service.lint(project)

        self.assertTrue(report.can_request_review)
        self.assertEqual([], report.blocking_findings)

    def test_incomplete_checklist_blocks_review_before_the_reviewer_step(self) -> None:
        project = _valid_project()
        checklist = list(project.checklist)
        checklist[0] = checklist[0].model_copy(update={"completed": False})

        report = self.service.lint(project.model_copy(update={"checklist": checklist}))

        finding = next(
            finding for finding in report.findings if finding.code == "checklist_incomplete"
        )
        self.assertEqual("error", finding.severity.value)
        self.assertEqual("checklist", finding.field_path)
        self.assertFalse(report.can_request_review)

    def test_missing_checklist_is_blocking(self) -> None:
        report = self.service.lint(_valid_project().model_copy(update={"checklist": []}))

        self.assertIn("checklist_missing", [item.code for item in report.blocking_findings])

    def test_same_input_produces_same_ordered_findings(self) -> None:
        project = _valid_project()
        project = project.model_copy(update={"purpose": "", "scope": "", "legal_bases": []})

        first = self.service.lint(project).model_dump(mode="json")
        second = self.service.lint(project).model_dump(mode="json")

        self.assertEqual(first, second)

    def test_required_metadata_reports_location_and_korean_fix(self) -> None:
        project = _valid_project().model_copy(
            update={
                "purpose": "",
                "scope": "",
                "legal_bases": [],
                "responsible_department": "",
                "planned_effective_date": None,
            }
        )

        report = self.service.lint(project)
        by_code = {finding.code: finding for finding in report.findings}

        self.assertEqual(
            {
                "purpose_missing",
                "scope_missing",
                "legal_basis_missing",
                "responsible_department_missing",
                "effective_date_missing",
            },
            set(by_code).intersection(
                {
                    "purpose_missing",
                    "scope_missing",
                    "legal_basis_missing",
                    "responsible_department_missing",
                    "effective_date_missing",
                }
            ),
        )
        self.assertEqual("purpose", by_code["purpose_missing"].field_path)
        self.assertIn("적으세요", by_code["purpose_missing"].suggestion)
        self.assertFalse(report.can_request_review)

    def test_duplicate_numbers_empty_body_and_broken_reference_are_blocking(self) -> None:
        project = _valid_project()
        project = project.model_copy(
            update={
                "clauses": [
                    ClauseDraft(article_number="제1조", title="목적", body=""),
                    ClauseDraft(article_number="제 1 조", title="범위", body="세부 내용은 제99조에 따른다."),
                ]
            }
        )

        report = self.service.lint(project)
        codes = [finding.code for finding in report.findings]

        self.assertIn("clause_body_empty", codes)
        self.assertIn("duplicate_article_number", codes)
        self.assertIn("internal_article_reference_missing", codes)
        broken = next(finding for finding in report.findings if finding.code == "internal_article_reference_missing")
        self.assertEqual("clauses[1].body", broken.field_path)

    def test_external_law_reference_is_not_treated_as_internal(self) -> None:
        project = _valid_project()
        first_article_index = next(
            index for index, clause in enumerate(project.clauses) if clause.node_type == DraftNodeType.ARTICLE
        )
        clauses = list(project.clauses)
        clauses[first_article_index] = clauses[first_article_index].model_copy(
            update={"body": "「개인정보 보호법」 제15조에 따른 기준을 준수한다."}
        )

        report = self.service.lint(project.model_copy(update={"clauses": clauses}))

        self.assertNotIn("internal_article_reference_missing", [finding.code for finding in report.findings])

    def test_common_external_public_rules_are_not_treated_as_internal(self) -> None:
        project = _valid_project()
        first_article_index = next(
            index
            for index, clause in enumerate(project.clauses)
            if clause.node_type == DraftNodeType.ARTICLE
        )
        clauses = list(project.clauses)
        clauses[first_article_index] = clauses[first_article_index].model_copy(
            update={
                "body": (
                    "정관 제10조와 지방자치단체 조례 제5조, 기관 시행세칙 "
                    "제12조에 따른 기준을 준수한다."
                )
            }
        )

        report = self.service.lint(project.model_copy(update={"clauses": clauses}))

        self.assertNotIn(
            "internal_article_reference_missing",
            [finding.code for finding in report.findings],
        )

    def test_missing_article_in_this_regulation_remains_blocking(self) -> None:
        project = _valid_project()
        first_article_index = next(
            index
            for index, clause in enumerate(project.clauses)
            if clause.node_type == DraftNodeType.ARTICLE
        )
        clauses = list(project.clauses)
        clauses[first_article_index] = clauses[first_article_index].model_copy(
            update={"body": "이 규정 제999조에 따른 절차를 준수한다."}
        )

        report = self.service.lint(project.model_copy(update={"clauses": clauses}))

        missing = [
            finding
            for finding in report.findings
            if finding.code == "internal_article_reference_missing"
        ]
        self.assertEqual(1, len(missing))
        self.assertIn("제999조", missing[0].message)

    def test_revision_requires_reason_and_predecessor(self) -> None:
        project = _valid_project().model_copy(update={"authoring_mode": AuthoringMode.PARTIAL_REVISION})

        report = self.service.lint(project)

        self.assertIn("revision_reason_missing", [finding.code for finding in report.findings])
        self.assertIn("predecessor_reference_missing", [finding.code for finding in report.findings])

    def test_placeholder_is_blocking(self) -> None:
        project = _valid_project()
        clauses = list(project.clauses)
        article_index = next(
            index for index, clause in enumerate(clauses) if clause.node_type == DraftNodeType.ARTICLE and clause.required
        )
        clauses[article_index] = clauses[article_index].model_copy(update={"body": "TODO: 담당자가 나중에 작성"})

        report = self.service.lint(project.model_copy(update={"clauses": clauses}))

        self.assertIn("placeholder_remaining", [finding.code for finding in report.blocking_findings])


def _valid_project() -> AuthoringProject:
    project_id = uuid4()
    clauses = AuthoringTemplateService().instantiate_clauses("general-regulation", project_id=project_id)
    completed = [
        clause.model_copy(update={"body": f"{clause.title or clause.article_number}에 필요한 구체적인 운영 기준을 정한다."})
        if clause.required and clause.node_type not in {DraftNodeType.CHAPTER, DraftNodeType.SECTION}
        else clause
        for clause in clauses
    ]
    return AuthoringProject(
        project_id=project_id,
        tenant_id="tenant-a",
        profile_id="institution-a",
        title="인사 운영 규정",
        purpose="공정하고 일관된 인사 운영 기준을 정한다.",
        scope="이 규정은 모든 임직원의 인사 업무에 적용한다.",
        legal_bases=["정관 제10조"],
        responsible_department="인사부",
        planned_effective_date=date(2027, 1, 1),
        clauses=completed,
        checklist=[
            BeginnerChecklistItem(
                item_id="human_review",
                label="사람의 내용 확인이 필요함을 이해했습니다.",
                guidance="내용 동결은 공식 결재를 대신하지 않습니다.",
                completed=True,
            )
        ],
        created_by="writer-a",
        updated_by="writer-a",
    )


if __name__ == "__main__":
    unittest.main()
