from __future__ import annotations

from datetime import date
import json
import unittest
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.schemas.authoring import (
    OFFICIAL_BOUNDARY_NOTICE,
    AuthoringMode,
    AuthoringProject,
    AuthoringProjectFreezeRequest,
    AuthoringProjectStatus,
    AuthoringProjectUpdateRequest,
    AuthoringTransitionRequest,
    ClauseDraft,
)


class AuthoringSchemaTests(unittest.TestCase):
    def test_project_and_nested_identifiers_are_uuids(self) -> None:
        project = _project()

        self.assertIsInstance(project.project_id, UUID)
        self.assertIsInstance(project.clauses[0].clause_id, UUID)
        with self.assertRaises(ValidationError):
            _project(project_id="guessable-project-id")
        with self.assertRaises(ValidationError):
            ClauseDraft(clause_id="clause-1", article_number="제1조")

    def test_all_contract_states_are_distinct_from_official_workflow(self) -> None:
        self.assertEqual(
            {
                "planning",
                "drafting",
                "review_requested",
                "changes_requested",
                "content_frozen",
                "exported",
                "abandoned",
            },
            {item.value for item in AuthoringProjectStatus},
        )
        schema_text = json.dumps(AuthoringProject.model_json_schema(), ensure_ascii=False).lower()
        for forbidden in ("approved", "approval_id", "approved_content_hash"):
            self.assertNotIn(forbidden, schema_text)

    def test_boundary_notice_is_fixed_and_cannot_be_overridden(self) -> None:
        project = _project(training_only=True)

        self.assertEqual(OFFICIAL_BOUNDARY_NOTICE, project.boundary_notice)
        with self.assertRaises(ValidationError):
            _project(boundary_notice="검토 완료")

    def test_unknown_fields_are_rejected_at_every_input_level(self) -> None:
        with self.assertRaises(ValidationError):
            AuthoringProjectUpdateRequest(expected_revision=1, unexpected=True)
        with self.assertRaises(ValidationError):
            ClauseDraft.model_validate(
                {
                    "article_number": "제1조",
                    "body": "본문",
                    "hidden_payload": "not allowed",
                }
            )

    def test_profile_scope_uses_the_institution_identifier_contract(self) -> None:
        for invalid in ("institution/a", "institution a", "기관-a", "p" * 129):
            with self.subTest(profile_id=invalid):
                with self.assertRaises(ValidationError):
                    _project(profile_id=invalid)

    def test_revision_inputs_are_strict_positive_integers(self) -> None:
        for invalid in (True, "1", 1.0, 0, -1):
            with self.subTest(expected_revision=invalid):
                with self.assertRaises(ValidationError):
                    AuthoringTransitionRequest(expected_revision=invalid)

    def test_update_rejects_explicit_null_for_required_project_fields(self) -> None:
        for field_name in (
            "title",
            "purpose",
            "scope",
            "legal_bases",
            "responsible_department",
            "clauses",
            "references",
            "checklist",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValidationError):
                    AuthoringProjectUpdateRequest.model_validate(
                        {"expected_revision": 1, field_name: None}
                    )

        clearing_optional_values = AuthoringProjectUpdateRequest(
            expected_revision=1,
            planned_effective_date=None,
            revision_reason=None,
            predecessor_reference=None,
        )
        self.assertIsNone(clearing_optional_values.planned_effective_date)

    def test_legal_basis_entries_are_nonempty_and_bounded(self) -> None:
        for legal_bases in ([""], ["x" * 501]):
            with self.subTest(length=len(legal_bases[0])):
                with self.assertRaises(ValidationError):
                    _project(legal_bases=legal_bases)
                with self.assertRaises(ValidationError):
                    AuthoringProjectUpdateRequest(
                        expected_revision=1,
                        legal_bases=legal_bases,
                    )

    def test_training_self_freeze_requires_explicit_reason(self) -> None:
        default_request = AuthoringProjectFreezeRequest(expected_revision=3)
        self.assertFalse(default_request.allow_training_self_freeze)

        with self.assertRaises(ValidationError):
            AuthoringProjectFreezeRequest(
                expected_revision=3,
                allow_training_self_freeze=True,
            )
        request = AuthoringProjectFreezeRequest(
            expected_revision=3,
            allow_training_self_freeze=True,
            comment="로컬 교육용 자체 확인임을 이해했습니다.",
        )
        self.assertTrue(request.allow_training_self_freeze)
        with self.assertRaises(ValidationError):
            AuthoringTransitionRequest(
                expected_revision=3,
                allow_training_self_freeze=True,
            )

    def test_frozen_revision_requires_matching_content_hash(self) -> None:
        with self.assertRaises(ValidationError):
            _project(frozen_revision=2)

        project = _project(frozen_revision=2, frozen_content_hash="a" * 64)
        self.assertEqual(2, project.frozen_revision)


def _project(**updates: object) -> AuthoringProject:
    payload: dict[str, object] = {
        "project_id": uuid4(),
        "tenant_id": "tenant-a",
        "profile_id": "institution-profile-a",
        "authoring_mode": AuthoringMode.ENACTMENT,
        "title": "인사 운영 규정",
        "purpose": "공정한 인사 운영 기준을 정한다.",
        "scope": "모든 임직원에게 적용한다.",
        "legal_bases": ["정관 제10조"],
        "responsible_department": "인사부",
        "planned_effective_date": date(2027, 1, 1),
        "clauses": [ClauseDraft(article_number="제1조", title="목적", body="운영 목적을 정한다.")],
        "created_by": "writer-a",
        "updated_by": "writer-a",
    }
    payload.update(updates)
    return AuthoringProject.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
