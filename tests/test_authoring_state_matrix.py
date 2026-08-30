from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_authoring
from app.core.config import Settings, get_settings as config_get_settings
from app.core.tenant_access import settings_for_tenant
from app.schemas.authoring import (
    AuthoringExportRequest,
    AuthoringProject,
    AuthoringProjectCreateRequest,
    AuthoringProjectFreezeRequest,
    AuthoringProjectStatus,
    AuthoringProjectUpdateRequest,
    AuthoringTransitionRequest,
    DraftNodeType,
)
from app.services.authoring_service import (
    AuthoringService,
    AuthoringStateTransitionError,
)


TENANT_ID = "tenant-a"
PROFILE_ID = "institution-a"
AUTHOR = "author-a"
REVIEWER = "reviewer-b"

MUTATIONS = (
    "update",
    "start_drafting",
    "request_review",
    "request_changes",
    "freeze",
    "reopen",
    "abandon",
    "export",
)

ALLOWED_TARGETS = {
    AuthoringProjectStatus.PLANNING: {
        "update": AuthoringProjectStatus.PLANNING,
        "start_drafting": AuthoringProjectStatus.DRAFTING,
        "abandon": AuthoringProjectStatus.ABANDONED,
    },
    AuthoringProjectStatus.DRAFTING: {
        "update": AuthoringProjectStatus.DRAFTING,
        "request_review": AuthoringProjectStatus.REVIEW_REQUESTED,
        "abandon": AuthoringProjectStatus.ABANDONED,
    },
    AuthoringProjectStatus.REVIEW_REQUESTED: {
        "request_changes": AuthoringProjectStatus.CHANGES_REQUESTED,
        "freeze": AuthoringProjectStatus.CONTENT_FROZEN,
    },
    AuthoringProjectStatus.CHANGES_REQUESTED: {
        "update": AuthoringProjectStatus.CHANGES_REQUESTED,
        "start_drafting": AuthoringProjectStatus.DRAFTING,
        "abandon": AuthoringProjectStatus.ABANDONED,
    },
    AuthoringProjectStatus.CONTENT_FROZEN: {
        "reopen": AuthoringProjectStatus.DRAFTING,
        "export": AuthoringProjectStatus.EXPORTED,
    },
    AuthoringProjectStatus.EXPORTED: {
        "reopen": AuthoringProjectStatus.DRAFTING,
    },
    AuthoringProjectStatus.ABANDONED: {},
}


class AuthoringStateMatrixTests(unittest.TestCase):
    def test_every_exposed_mutation_matches_the_seven_state_matrix(self) -> None:
        self.assertEqual(set(AuthoringProjectStatus), set(ALLOWED_TARGETS))
        self.assertEqual(7, len(ALLOWED_TARGETS))
        self.assertEqual(8, len(MUTATIONS))

        for initial_status in AuthoringProjectStatus:
            for mutation in MUTATIONS:
                with self.subTest(state=initial_status.value, mutation=mutation):
                    with tempfile.TemporaryDirectory() as tmp:
                        service = _service(Path(tmp))
                        project = _project_in_state(service, initial_status)
                        before_events = _events(service, project)
                        expected_target = ALLOWED_TARGETS[initial_status].get(mutation)

                        if expected_target is None:
                            with self.assertRaises(AuthoringStateTransitionError):
                                _invoke_mutation(service, project, mutation)
                            current = _get(service, project)
                            after_events = _events(service, project)
                            self.assertEqual(initial_status, current.status)
                            self.assertEqual(project.revision, current.revision)
                            self.assertEqual(before_events, after_events)
                            continue

                        _invoke_mutation(service, project, mutation)
                        current = _get(service, project)
                        after_events = _events(service, project)
                        self.assertEqual(expected_target, current.status)
                        self.assertEqual(project.revision + 1, current.revision)
                        self.assertEqual(len(before_events) + 1, len(after_events))

    def test_abandon_happy_paths_and_terminal_state(self) -> None:
        for initial_status in (
            AuthoringProjectStatus.PLANNING,
            AuthoringProjectStatus.DRAFTING,
            AuthoringProjectStatus.CHANGES_REQUESTED,
        ):
            with self.subTest(initial_status=initial_status.value):
                with tempfile.TemporaryDirectory() as tmp:
                    service = _service(Path(tmp))
                    project = _project_in_state(service, initial_status)
                    abandoned = _invoke_mutation(service, project, "abandon")

                    self.assertEqual(AuthoringProjectStatus.ABANDONED, abandoned.status)
                    for mutation in MUTATIONS:
                        with self.subTest(
                            initial_status=initial_status.value,
                            terminal_mutation=mutation,
                        ):
                            with self.assertRaises(AuthoringStateTransitionError):
                                _invoke_mutation(service, abandoned, mutation)
                    terminal = _get(service, abandoned)
                    self.assertEqual(AuthoringProjectStatus.ABANDONED, terminal.status)
                    self.assertEqual(abandoned.revision, terminal.revision)

    def test_actor_and_tenant_bound_tokens_enforce_http_four_eyes_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_settings = Settings(
                app_env="production",
                api_auth_required=True,
                api_auth_tokens=json.dumps(
                    {
                        "author-token": {
                            "role": "operator",
                            "actor": AUTHOR,
                            "tenant_id": TENANT_ID,
                        },
                        "reviewer-token": {
                            "role": "operator",
                            "actor": REVIEWER,
                            "tenant_id": TENANT_ID,
                        },
                    }
                ),
                api_audit_enabled=True,
                tenant_storage_isolation=True,
                enable_regulation_authoring=True,
                data_dir=Path(tmp) / "data",
                artifact_root=Path(tmp),
            )
            scoped_settings = settings_for_tenant(base_settings, TENANT_ID)
            service = AuthoringService(scoped_settings)
            review = _project_in_state(
                service,
                AuthoringProjectStatus.REVIEW_REQUESTED,
            )
            api = FastAPI()
            api.include_router(routes_authoring.router)
            api.dependency_overrides[config_get_settings] = lambda: base_settings
            headers = {"X-Tenant-Id": TENANT_ID}
            url = f"/api/authoring/projects/{review.project_id}/freeze"
            payload = {
                "expected_revision": review.revision,
                "comment": "Independent content review completed.",
            }

            with patch.object(
                routes_authoring,
                "get_settings",
                return_value=base_settings,
            ), TestClient(api) as client:
                author_response = client.post(
                    url,
                    params={"profile_id": PROFILE_ID},
                    headers={
                        **headers,
                        "Authorization": "Bearer author-token",
                    },
                    json=payload,
                )
                after_denial = _get(service, review)
                reviewer_response = client.post(
                    url,
                    params={"profile_id": PROFILE_ID},
                    headers={
                        **headers,
                        "Authorization": "Bearer reviewer-token",
                    },
                    json=payload,
                )

            frozen = _get(service, review)

        self.assertEqual(403, author_response.status_code)
        self.assertEqual(AuthoringProjectStatus.REVIEW_REQUESTED, after_denial.status)
        self.assertEqual(review.revision, after_denial.revision)
        self.assertEqual(200, reviewer_response.status_code)
        self.assertEqual(REVIEWER, reviewer_response.json()["frozen_by"])
        self.assertFalse(reviewer_response.json()["training_only"])
        self.assertEqual(AuthoringProjectStatus.CONTENT_FROZEN, frozen.status)
        self.assertEqual(REVIEWER, frozen.frozen_by)


def _service(root: Path) -> AuthoringService:
    return AuthoringService(
        Settings(
            app_env="local",
            api_auth_required=True,
            data_dir=root / "data",
            artifact_root=root,
        )
    )


def _project_in_state(
    service: AuthoringService,
    target: AuthoringProjectStatus,
) -> AuthoringProject:
    project = service.create_project(
        AuthoringProjectCreateRequest(
            profile_id=PROFILE_ID,
            title="Records Management Regulation",
            purpose="Define responsibilities and procedures for managing records.",
            scope="Apply to every department and records operator.",
            legal_bases=["Internal records-management policy"],
            responsible_department="Records Office",
            planned_effective_date=date(2027, 1, 1),
        ),
        tenant_id=TENANT_ID,
        actor=AUTHOR,
    )
    project = service.update_project(
        project.project_id,
        AuthoringProjectUpdateRequest(
            expected_revision=project.revision,
            clauses=[
                clause.model_copy(
                    update={
                        "body": "The responsible department records each decision and retention period."
                    }
                )
                if clause.required
                and clause.node_type not in {DraftNodeType.CHAPTER, DraftNodeType.SECTION}
                else clause
                for clause in project.clauses
            ],
            checklist=[
                item.model_copy(update={"completed": True})
                for item in project.checklist
            ],
        ),
        tenant_id=TENANT_ID,
        profile_id=PROFILE_ID,
        actor=AUTHOR,
    )
    if target == AuthoringProjectStatus.PLANNING:
        return project

    project = service.start_drafting(
        project.project_id,
        AuthoringTransitionRequest(expected_revision=project.revision),
        tenant_id=TENANT_ID,
        profile_id=PROFILE_ID,
        actor=AUTHOR,
    )
    if target == AuthoringProjectStatus.DRAFTING:
        return project
    if target == AuthoringProjectStatus.ABANDONED:
        return service.abandon_project(
            project.project_id,
            AuthoringTransitionRequest(
                expected_revision=project.revision,
                comment="Authoring work was intentionally stopped.",
            ),
            tenant_id=TENANT_ID,
            profile_id=PROFILE_ID,
            actor=AUTHOR,
        )

    project = service.request_review(
        project.project_id,
        AuthoringTransitionRequest(expected_revision=project.revision),
        tenant_id=TENANT_ID,
        profile_id=PROFILE_ID,
        actor=AUTHOR,
    )
    if target == AuthoringProjectStatus.REVIEW_REQUESTED:
        return project
    if target == AuthoringProjectStatus.CHANGES_REQUESTED:
        return service.request_changes(
            project.project_id,
            AuthoringTransitionRequest(
                expected_revision=project.revision,
                comment="Clarify the records-retention responsibility.",
            ),
            tenant_id=TENANT_ID,
            profile_id=PROFILE_ID,
            actor=REVIEWER,
        )

    artifact = service.freeze_project(
        project.project_id,
        AuthoringProjectFreezeRequest(
            expected_revision=project.revision,
            comment="Independent content review completed.",
        ),
        tenant_id=TENANT_ID,
        profile_id=PROFILE_ID,
        actor=REVIEWER,
    )
    project = _get(service, project)
    if target == AuthoringProjectStatus.CONTENT_FROZEN:
        return project
    if target == AuthoringProjectStatus.EXPORTED:
        service.export_project(
            project.project_id,
            AuthoringExportRequest(
                expected_revision=artifact.revision,
                export_format="json",
            ),
            tenant_id=TENANT_ID,
            profile_id=PROFILE_ID,
            actor=REVIEWER,
        )
        return _get(service, project)
    raise AssertionError(f"Unsupported authoring fixture state: {target}")


def _invoke_mutation(
    service: AuthoringService,
    project: AuthoringProject,
    mutation: str,
):
    common = {
        "tenant_id": TENANT_ID,
        "profile_id": PROFILE_ID,
    }
    if mutation == "update":
        return service.update_project(
            project.project_id,
            AuthoringProjectUpdateRequest(
                expected_revision=project.revision,
                title=f"{project.title} updated",
            ),
            actor=AUTHOR,
            **common,
        )
    if mutation == "start_drafting":
        return service.start_drafting(
            project.project_id,
            AuthoringTransitionRequest(expected_revision=project.revision),
            actor=AUTHOR,
            **common,
        )
    if mutation == "request_review":
        return service.request_review(
            project.project_id,
            AuthoringTransitionRequest(expected_revision=project.revision),
            actor=AUTHOR,
            **common,
        )
    if mutation == "request_changes":
        return service.request_changes(
            project.project_id,
            AuthoringTransitionRequest(
                expected_revision=project.revision,
                comment="Clarify the records-retention responsibility.",
            ),
            actor=REVIEWER,
            **common,
        )
    if mutation == "freeze":
        return service.freeze_project(
            project.project_id,
            AuthoringProjectFreezeRequest(
                expected_revision=project.revision,
                comment="Independent content review completed.",
            ),
            actor=REVIEWER,
            **common,
        )
    if mutation == "reopen":
        return service.reopen_project(
            project.project_id,
            AuthoringTransitionRequest(
                expected_revision=project.revision,
                comment="Begin the next authoring revision.",
            ),
            actor=AUTHOR,
            **common,
        )
    if mutation == "abandon":
        return service.abandon_project(
            project.project_id,
            AuthoringTransitionRequest(
                expected_revision=project.revision,
                comment="Authoring work was intentionally stopped.",
            ),
            actor=AUTHOR,
            **common,
        )
    if mutation == "export":
        return service.export_project(
            project.project_id,
            AuthoringExportRequest(
                expected_revision=project.revision,
                export_format="json",
            ),
            actor=REVIEWER,
            **common,
        )
    raise AssertionError(f"Unknown authoring mutation: {mutation}")


def _get(
    service: AuthoringService,
    project: AuthoringProject,
) -> AuthoringProject:
    return service.get_project(
        project.project_id,
        tenant_id=TENANT_ID,
        profile_id=PROFILE_ID,
    )


def _events(
    service: AuthoringService,
    project: AuthoringProject,
) -> list[dict[str, object]]:
    return service.list_events(
        project.project_id,
        tenant_id=TENANT_ID,
        profile_id=PROFILE_ID,
    )


if __name__ == "__main__":
    unittest.main()
