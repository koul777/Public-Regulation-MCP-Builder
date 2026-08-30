from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from app.core.config import Settings
from app.schemas.authoring import (
    AuthoringExportRequest,
    AuthoringProjectCreateRequest,
    AuthoringProjectFreezeRequest,
    AuthoringProjectUpdateRequest,
    AuthoringTransitionRequest,
    DraftNodeType,
)
from app.services.authoring_service import AuthoringService


class AuthoringOfficialIsolationTests(unittest.TestCase):
    def test_complete_authoring_export_writes_only_under_authoring_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            service = AuthoringService(
                Settings(
                    app_env="local",
                    api_auth_required=True,
                    data_dir=data_dir,
                )
            )
            created = service.create_project(
                AuthoringProjectCreateRequest(
                    profile_id="public-institution",
                    title="기록 관리 규정",
                    purpose="기관 기록의 작성과 보관 책임을 명확히 정합니다.",
                    scope="기관의 모든 부서와 기록 담당자에게 적용합니다.",
                    legal_bases=["공공기록물 관리 관련 법령 및 내부 방침"],
                    responsible_department="기록관리부",
                    planned_effective_date=date(2026, 10, 1),
                ),
                tenant_id="tenant-a",
                actor="author-a",
            )
            clauses = [
                clause.model_copy(
                    update={
                        "body": (
                            f"{clause.title or clause.article_number}에 필요한 담당자, 절차와 "
                            "기록 기준을 구체적으로 정합니다."
                        )
                    }
                )
                if clause.required
                and clause.node_type not in {DraftNodeType.CHAPTER, DraftNodeType.SECTION}
                else clause
                for clause in created.clauses
            ]
            updated = service.update_project(
                created.project_id,
                AuthoringProjectUpdateRequest(
                    expected_revision=created.revision,
                    clauses=clauses,
                    checklist=[
                        item.model_copy(update={"completed": True})
                        for item in created.checklist
                    ],
                ),
                tenant_id="tenant-a",
                profile_id="public-institution",
                actor="author-a",
            )
            drafting = service.start_drafting(
                created.project_id,
                AuthoringTransitionRequest(expected_revision=updated.revision),
                tenant_id="tenant-a",
                profile_id="public-institution",
                actor="author-a",
            )
            review = service.request_review(
                created.project_id,
                AuthoringTransitionRequest(expected_revision=drafting.revision),
                tenant_id="tenant-a",
                profile_id="public-institution",
                actor="author-a",
            )
            service.freeze_project(
                created.project_id,
                AuthoringProjectFreezeRequest(
                    expected_revision=review.revision,
                    comment="작성자와 다른 담당자가 초안 내용을 확인했습니다.",
                ),
                tenant_id="tenant-a",
                profile_id="public-institution",
                actor="reviewer-b",
            )
            frozen = service.get_project(
                created.project_id,
                tenant_id="tenant-a",
                profile_id="public-institution",
            )
            service.export_project(
                created.project_id,
                AuthoringExportRequest(
                    expected_revision=frozen.revision,
                    export_format="json",
                ),
                tenant_id="tenant-a",
                profile_id="public-institution",
                actor="reviewer-b",
            )

            files = [path for path in data_dir.rglob("*") if path.is_file()]
            relative_files = [path.relative_to(data_dir).as_posix() for path in files]

        self.assertTrue(relative_files)
        self.assertTrue(all(path.startswith("authoring/") for path in relative_files))
        self.assertFalse(any(path.endswith("approvals.jsonl") for path in relative_files))
        self.assertFalse(any(path.endswith("approved_vectors.jsonl") for path in relative_files))
        self.assertFalse(any("mcp" in path.lower() for path in relative_files))


if __name__ == "__main__":
    unittest.main()
