from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from app.api import routes_authoring
from app.api.authoring_request_guard import (
    install_authoring_request_handlers,
    make_authoring_body_limit_observer,
)
from app.core.config import Settings, get_settings as config_get_settings
from app.core.request_body_limit import JsonRequestBodyLimitMiddleware
from app.core.security import AuthContext, get_auth_context
from app.core.tenant_access import settings_for_tenant
from app.main import app, readiness_checks
from app.schemas.authoring import (
    OFFICIAL_BOUNDARY_NOTICE,
    AuthoringExportRequest,
    AuthoringProjectCreateRequest,
    AuthoringProjectFreezeRequest,
    AuthoringProjectUpdateRequest,
    AuthoringTransitionRequest,
    DraftNodeType,
)
from app.services.authoring_service import AuthoringService
from app.storage import authoring_repository as repository_module


class RoutesAuthoringTests(unittest.TestCase):
    def test_http_malformed_and_field_oversize_payloads_are_rejected_before_storage(self) -> None:
        sensitive_marker = "malformed private clause must not be persisted"
        cases = {
            "malformed_json": {
                "content": b'{"profile_id":"institution-a","title":',
                "headers": {"Content-Type": "application/json"},
            },
            "unknown_field": {
                "json": {
                    "profile_id": "institution-a",
                    "title": "Rule",
                    "draft_body": sensitive_marker,
                },
            },
            "invalid_mode": {
                "json": {
                    "profile_id": "institution-a",
                    "title": "Rule",
                    "authoring_mode": "official_approval",
                },
            },
            "oversize_profile": {
                "json": {
                    "profile_id": "p" * 129,
                    "title": "Rule",
                },
            },
            "invalid_profile_characters": {
                "json": {
                    "profile_id": "institution/a",
                    "title": "Rule",
                },
            },
            "oversize_title": {
                "json": {
                    "profile_id": "institution-a",
                    "title": "t" * 301,
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with self._http_client(settings, auth=self._auth()) as client:
                responses = {
                    name: client.post("/api/authoring/projects", **request)
                    for name, request in cases.items()
                }

            tenant_settings = settings_for_tenant(settings, "tenant-a")
            projects_dir = tenant_settings.authoring_dir / "projects"
            projects_exist = projects_dir.exists()

        self.assertEqual(
            {name: 422 for name in cases},
            {name: response.status_code for name, response in responses.items()},
        )
        self.assertFalse(projects_exist)

    def test_http_body_byte_limit_rejects_large_authoring_payload_before_parsing(self) -> None:
        sensitive_marker = "private-draft-" * 100
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with self._http_client(
                settings,
                auth=self._auth(),
                max_body_bytes=256,
            ) as client:
                response = client.post(
                    "/api/authoring/projects",
                    json={
                        "profile_id": "institution-a",
                        "title": "Rule",
                        "purpose": sensitive_marker,
                    },
                )

        self.assertEqual(413, response.status_code)
        self.assertIn("256-byte limit", response.json()["detail"])
        self.assertNotIn(sensitive_marker, response.text)

    def test_http_body_byte_limit_rejects_multipart_on_authoring_route(self) -> None:
        sensitive_marker = b"private-multipart-draft-" * 250
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with self._http_client(
                settings,
                auth=self._auth(),
                max_body_bytes=256,
            ) as client:
                response = client.post(
                    "/api/authoring/projects",
                    files={"file": ("draft.txt", sensitive_marker, "text/plain")},
                )
            records = self._audit_records(settings, "default")

        self.assertEqual(413, response.status_code)
        self.assertNotIn(sensitive_marker.decode("ascii"), response.text)
        self.assertEqual("authoring.request.body_limit", records[-1]["action"])
        self.assertEqual(413, records[-1]["status_code"])

    def test_http_validation_and_body_limit_are_content_free_and_audited(self) -> None:
        sensitive_value = "private-clause-value"
        sensitive_key = "private-clause-field"
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with self._http_client(
                settings,
                auth=self._auth(),
                max_body_bytes=256,
            ) as client:
                invalid = client.post(
                    "/api/authoring/projects",
                    json={
                        "profile_id": "institution-a",
                        "title": "Rule",
                        sensitive_key: sensitive_value,
                    },
                )
                too_large = client.post(
                    "/api/authoring/projects",
                    json={
                        "profile_id": "institution-a",
                        "title": "Rule",
                        "purpose": sensitive_value * 100,
                    },
                )
            records = self._audit_records(settings, "default")

        self.assertEqual(422, invalid.status_code)
        self.assertNotIn(sensitive_key, invalid.text)
        self.assertNotIn(sensitive_value, invalid.text)
        self.assertEqual(413, too_large.status_code)
        serialized_audit = json.dumps(records, ensure_ascii=False)
        self.assertNotIn(sensitive_key, serialized_audit)
        self.assertNotIn(sensitive_value, serialized_audit)
        self.assertEqual(
            ["authoring.request.validation", "authoring.request.body_limit"],
            [record["action"] for record in records],
        )

    def test_invalid_route_identifiers_are_not_copied_into_audit_records(self) -> None:
        project_marker = "PRIVATE_DRAFT_PROJECT_MARKER"
        template_marker = "PRIVATE_DRAFT_TEMPLATE_MARKER"
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with self._http_client(settings, auth=self._auth()) as client:
                project_response = client.get(
                    f"/api/authoring/projects/{project_marker}",
                    params={"profile_id": "institution-a"},
                )
                template_response = client.get(
                    f"/api/authoring/templates/{template_marker}"
                )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(422, project_response.status_code)
        self.assertEqual(404, template_response.status_code)
        serialized_audit = json.dumps(records, ensure_ascii=False)
        self.assertNotIn(project_marker, serialized_audit)
        self.assertNotIn(template_marker, serialized_audit)
        self.assertEqual(["", ""], [record["source_record_id"] for record in records])

    def test_authoring_guard_uses_a_url_segment_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            api = FastAPI()

            @api.get("/api/authoring-public")
            def non_authoring_route(required: str = Query()):
                return {"required": required}

            install_authoring_request_handlers(
                api,
                settings_provider=lambda: settings,
            )
            with TestClient(api) as client:
                response = client.get("/api/authoring-public")

        self.assertEqual(422, response.status_code)
        self.assertIn("msg", response.json()["detail"][0])
        self.assertIn("input", response.json()["detail"][0])

    def test_http_invalid_revision_values_are_rejected_before_service_dispatch(self) -> None:
        invalid_payloads = (
            {"expected_revision": 0},
            {"expected_revision": -1},
            {"expected_revision": None},
            {"expected_revision": "not-an-integer"},
            {"expected_revision": {"value": 1}},
            {"expected_revision": "1"},
            {"expected_revision": True},
            {"expected_revision": 1, "draft_body": "extra draft field"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(
                routes_authoring.AuthoringService,
                "start_drafting",
            ) as start_drafting, self._http_client(
                settings,
                auth=self._auth(),
            ) as client:
                responses = [
                    client.post(
                        "/api/authoring/projects/00000000-0000-0000-0000-000000000001/start-drafting",
                        params={"profile_id": "institution-a"},
                        json=payload,
                    )
                    for payload in invalid_payloads
                ]

        self.assertEqual([422] * len(invalid_payloads), [item.status_code for item in responses])
        start_drafting.assert_not_called()

    def test_http_checklist_definition_tampering_is_content_free_and_non_mutating(self) -> None:
        deleted_marker = "deleted-checklist-private-purpose"
        changed_marker = "changed-checklist-private-label"
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with self._http_client(settings, auth=self._auth()) as client:
                created_response = client.post(
                    "/api/authoring/projects",
                    json=self._create_request().model_dump(mode="json"),
                )
                self.assertEqual(201, created_response.status_code)
                created = created_response.json()
                project_id = created["project_id"]
                checklist = created["checklist"]
                changed_checklist = [dict(item) for item in checklist]
                changed_checklist[0]["label"] = changed_marker

                rejected = (
                    client.patch(
                        f"/api/authoring/projects/{project_id}",
                        params={"profile_id": "institution-a"},
                        json={
                            "expected_revision": created["revision"],
                            "purpose": deleted_marker,
                            "checklist": checklist[:-1],
                        },
                    ),
                    client.patch(
                        f"/api/authoring/projects/{project_id}",
                        params={"profile_id": "institution-a"},
                        json={
                            "expected_revision": created["revision"],
                            "checklist": changed_checklist,
                        },
                    ),
                )
                unchanged_response = client.get(
                    f"/api/authoring/projects/{project_id}",
                    params={"profile_id": "institution-a"},
                )
                events_response = client.get(
                    f"/api/authoring/projects/{project_id}/events",
                    params={"profile_id": "institution-a"},
                )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual([422, 422], [response.status_code for response in rejected])
        self.assertTrue(
            all(
                response.json()["detail"]
                == "Authoring transition or lint validation failed."
                for response in rejected
            )
        )
        self.assertEqual(200, unchanged_response.status_code)
        unchanged = unchanged_response.json()
        self.assertEqual(created["revision"], unchanged["revision"])
        self.assertEqual(created["checklist"], unchanged["checklist"])
        self.assertEqual(200, events_response.status_code)
        events = events_response.json()
        self.assertEqual(["created"], [event["event_type"] for event in events])

        serialized_failures = json.dumps(
            {
                "responses": [response.text for response in rejected],
                "audit": records,
                "events": events,
            },
            ensure_ascii=False,
        )
        for forbidden in (deleted_marker, changed_marker, "checklist"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized_failures)
        update_records = [
            record
            for record in records
            if record["action"] == "authoring.project.update"
        ]
        self.assertEqual(2, len(update_records))
        self.assertTrue(
            all(
                record["outcome"] == "failure"
                and record["status_code"] == 422
                and record["detail"]
                == "authoring transition or lint validation failed"
                for record in update_records
            )
        )

    def test_http_start_drafting_requires_metadata_without_mutation(self) -> None:
        marker = "missing-metadata-private-comment"
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with self._http_client(settings, auth=self._auth()) as client:
                created_response = client.post(
                    "/api/authoring/projects",
                    json={
                        "profile_id": "institution-a",
                        "title": "Incomplete Regulation",
                    },
                )
                self.assertEqual(201, created_response.status_code)
                created = created_response.json()
                project_id = created["project_id"]
                rejected = client.post(
                    f"/api/authoring/projects/{project_id}/start-drafting",
                    params={"profile_id": "institution-a"},
                    json={
                        "expected_revision": created["revision"],
                        "comment": marker,
                    },
                )
                unchanged_response = client.get(
                    f"/api/authoring/projects/{project_id}",
                    params={"profile_id": "institution-a"},
                )
                events_response = client.get(
                    f"/api/authoring/projects/{project_id}/events",
                    params={"profile_id": "institution-a"},
                )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(422, rejected.status_code)
        self.assertEqual(
            "Authoring transition or lint validation failed.",
            rejected.json()["detail"],
        )
        unchanged = unchanged_response.json()
        self.assertEqual("planning", unchanged["status"])
        self.assertEqual(created["revision"], unchanged["revision"])
        events = events_response.json()
        self.assertEqual(["created"], [event["event_type"] for event in events])
        self.assertNotIn(marker, rejected.text)
        self.assertNotIn(marker, json.dumps(records, ensure_ascii=False))
        self.assertNotIn(marker, json.dumps(events, ensure_ascii=False))
        drafting_records = [
            record
            for record in records
            if record["action"] == "authoring.project.start_drafting"
        ]
        self.assertEqual(1, len(drafting_records))
        self.assertEqual("failure", drafting_records[0]["outcome"])
        self.assertEqual(422, drafting_records[0]["status_code"])

    def test_http_unexpected_exception_hides_body_from_response_and_audit(self) -> None:
        sensitive_marker = "unexpected private draft content"
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(
                routes_authoring.AuthoringService,
                "create_project",
                side_effect=RuntimeError(sensitive_marker),
            ), self._http_client(
                settings,
                auth=self._auth(),
                raise_server_exceptions=False,
            ) as client:
                response = client.post(
                    "/api/authoring/projects",
                    json=self._create_request(draft_marker=sensitive_marker).model_dump(mode="json"),
                )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(500, response.status_code)
        self.assertNotIn(sensitive_marker, response.text)
        self.assertNotIn(sensitive_marker, json.dumps(records, ensure_ascii=False))
        self.assertEqual("unexpected authoring failure", records[-1]["detail"])

    def test_http_auth_rejects_noncanonical_tenant_before_authoring_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                self._settings(tmp),
                api_auth_required=True,
                api_auth_tokens=json.dumps(
                    {
                        "author-token": {
                            "role": "admin",
                            "actor": "author-a",
                            "tenant_ids": ["tenant-a"],
                        }
                    }
                ),
            )
            with self._http_client(settings, auth=None) as client:
                response = client.post(
                    "/api/authoring/projects",
                    headers={
                        "Authorization": "Bearer author-token",
                        "X-Actor": "author-a",
                        "X-Tenant-Id": "Tenant-A",
                    },
                    json=self._create_request().model_dump(mode="json"),
                )
            audit_path = settings.data_dir / "repository" / "api_audit.jsonl"
            records = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(400, response.status_code)
        self.assertEqual("auth.denied", records[-1]["action"])
        self.assertEqual("denied", records[-1]["outcome"])

    def test_bound_http_actors_enforce_protected_four_eyes_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                self._settings(tmp),
                app_env="production",
                api_auth_required=True,
                api_auth_tokens=json.dumps(
                    {
                        "author-token": {
                            "role": "operator",
                            "actor": "author-a",
                            "tenant_ids": ["tenant-a"],
                        },
                        "reviewer-token": {
                            "role": "operator",
                            "actor": "reviewer-b",
                            "tenant_ids": ["tenant-a"],
                        },
                    }
                ),
            )
            author_headers = {
                "Authorization": "Bearer author-token",
                "X-Actor": "author-a",
                "X-Tenant-Id": "tenant-a",
            }
            reviewer_headers = {
                "Authorization": "Bearer reviewer-token",
                "X-Actor": "reviewer-b",
                "X-Tenant-Id": "tenant-a",
            }
            with self._http_client(settings, auth=None) as client:
                created = client.post(
                    "/api/authoring/projects",
                    headers=author_headers,
                    json=self._create_request().model_dump(mode="json"),
                )
                project = created.json()
                started = client.post(
                    f"/api/authoring/projects/{project['project_id']}/start-drafting",
                    params={"profile_id": "institution-a"},
                    headers=author_headers,
                    json={"expected_revision": project["revision"]},
                )
                project = started.json()
                clauses = [
                    {
                        **clause,
                        "body": "담당자, 절차와 기록 기준을 구체적으로 정합니다.",
                    }
                    if clause["required"]
                    and clause["node_type"] not in {"chapter", "section"}
                    else clause
                    for clause in project["clauses"]
                ]
                checklist = [
                    {**item, "completed": True} for item in project["checklist"]
                ]
                updated = client.patch(
                    f"/api/authoring/projects/{project['project_id']}",
                    params={"profile_id": "institution-a"},
                    headers=author_headers,
                    json={
                        "expected_revision": project["revision"],
                        "clauses": clauses,
                        "checklist": checklist,
                    },
                )
                project = updated.json()
                review = client.post(
                    f"/api/authoring/projects/{project['project_id']}/request-review",
                    params={"profile_id": "institution-a"},
                    headers=author_headers,
                    json={"expected_revision": project["revision"]},
                )
                project = review.json()
                spoofed_actor = client.post(
                    f"/api/authoring/projects/{project['project_id']}/freeze",
                    params={"profile_id": "institution-a"},
                    headers={**author_headers, "X-Actor": "reviewer-b"},
                    json={"expected_revision": project["revision"]},
                )
                self_freeze = client.post(
                    f"/api/authoring/projects/{project['project_id']}/freeze",
                    params={"profile_id": "institution-a"},
                    headers=author_headers,
                    json={"expected_revision": project["revision"]},
                )
                frozen = client.post(
                    f"/api/authoring/projects/{project['project_id']}/freeze",
                    params={"profile_id": "institution-a"},
                    headers=reviewer_headers,
                    json={"expected_revision": project["revision"]},
                )

        self.assertEqual(201, created.status_code)
        self.assertEqual(200, started.status_code)
        self.assertEqual(200, updated.status_code)
        self.assertEqual(200, review.status_code)
        self.assertEqual(403, spoofed_actor.status_code)
        self.assertEqual(403, self_freeze.status_code)
        self.assertEqual(200, frozen.status_code)
        self.assertEqual("reviewer-b", frozen.json()["frozen_by"])

    def test_http_profile_scope_is_canonicalized_for_create_and_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            request = self._create_request().model_copy(
                update={"profile_id": "  Institution-A  "}
            )
            with self._http_client(settings, auth=self._auth()) as client:
                created = client.post(
                    "/api/authoring/projects",
                    json=request.model_dump(mode="json"),
                )
                project_id = created.json()["project_id"]
                loaded = client.get(
                    f"/api/authoring/projects/{project_id}",
                    params={"profile_id": "INSTITUTION-A"},
                )

        self.assertEqual(201, created.status_code)
        self.assertEqual("institution-a", created.json()["profile_id"])
        self.assertEqual(200, loaded.status_code)
        self.assertEqual(project_id, loaded.json()["project_id"])

    def test_unexpected_exception_is_generic_and_does_not_echo_draft_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            sensitive_marker = "private clause content must not escape"
            with (
                patch.object(routes_authoring, "get_settings", return_value=settings),
                patch.object(
                    routes_authoring.AuthoringService,
                    "create_project",
                    side_effect=RuntimeError(sensitive_marker),
                ),
            ):
                with self.assertRaises(HTTPException) as raised:
                    routes_authoring.create_authoring_project(
                        self._create_request(draft_marker=sensitive_marker),
                        self._auth(),
                    )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("Internal authoring error.", raised.exception.detail)
        self.assertNotIn(sensitive_marker, json.dumps(records, ensure_ascii=False))

    def test_feature_flag_fails_closed_and_audits_mutation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp, enabled=False)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as raised:
                    routes_authoring.create_authoring_project(
                        self._create_request(draft_marker="secret draft body"),
                        self._auth(),
                    )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("failure", records[-1]["outcome"])
        self.assertEqual("authoring.project.create", records[-1]["action"])
        self.assertNotIn("secret draft body", json.dumps(records, ensure_ascii=False))

    def test_viewer_cannot_read_or_write_drafts_and_denials_are_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            viewer = self._auth(role="viewer", actor="viewer")
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                with self.assertRaises(HTTPException) as create_raised:
                    routes_authoring.create_authoring_project(self._create_request(), viewer)
                with self.assertRaises(HTTPException) as read_raised:
                    routes_authoring.get_authoring_project(
                        "00000000-0000-0000-0000-000000000001",
                        "institution-a",
                        viewer,
                    )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(403, create_raised.exception.status_code)
        self.assertEqual(403, read_raised.exception.status_code)
        self.assertEqual(["denied", "denied"], [record["outcome"] for record in records])

    def test_tenant_scoping_hides_an_existing_project_as_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                created = routes_authoring.create_authoring_project(
                    self._create_request(),
                    self._auth(),
                )
                loaded = routes_authoring.get_authoring_project(
                    str(created.project_id),
                    "institution-a",
                    self._auth(),
                )
                with self.assertRaises(HTTPException) as raised:
                    routes_authoring.get_authoring_project(
                        str(created.project_id),
                        "institution-a",
                        self._auth(tenant_id="tenant-b"),
                    )
                tenant_b_projects = routes_authoring.list_authoring_projects(
                    "institution-a",
                    self._auth(tenant_id="tenant-b"),
                )

        self.assertEqual(created.project_id, loaded.project_id)
        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual([], tenant_b_projects)

    def test_same_tenant_different_profile_is_404_for_project_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                created = routes_authoring.create_authoring_project(
                    self._create_request(),
                    self._auth(),
                )
                operations = {
                    "get": lambda: routes_authoring.get_authoring_project(
                        str(created.project_id),
                        "institution-b",
                        self._auth(),
                    ),
                    "patch": lambda: routes_authoring.update_authoring_project(
                        str(created.project_id),
                        AuthoringProjectUpdateRequest(
                            expected_revision=created.revision,
                            title="Cross-profile title",
                        ),
                        "institution-b",
                        self._auth(),
                    ),
                    "freeze": lambda: routes_authoring.freeze_authoring_project(
                        str(created.project_id),
                        AuthoringProjectFreezeRequest(
                            expected_revision=created.revision,
                            comment="Cross-profile freeze",
                        ),
                        "institution-b",
                        self._auth(actor="reviewer-b"),
                    ),
                    "export": lambda: routes_authoring.export_authoring_project(
                        str(created.project_id),
                        AuthoringExportRequest(
                            expected_revision=created.revision,
                            export_format="json",
                        ),
                        "institution-b",
                        self._auth(),
                    ),
                }
                status_codes: dict[str, int] = {}
                for operation, invoke in operations.items():
                    with self.subTest(operation=operation):
                        with self.assertRaises(HTTPException) as raised:
                            invoke()
                        status_codes[operation] = raised.exception.status_code

                own_profile = routes_authoring.get_authoring_project(
                    str(created.project_id),
                    "INSTITUTION-A",
                    self._auth(),
                )

        self.assertEqual(
            {"get": 404, "patch": 404, "freeze": 404, "export": 404},
            status_codes,
        )
        self.assertEqual(created.project_id, own_profile.project_id)
        self.assertEqual(1, own_profile.revision)

    def test_project_list_requires_profile_query_and_filters_same_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                first = routes_authoring.create_authoring_project(
                    self._create_request(),
                    self._auth(),
                )
                second_request = self._create_request().model_copy(
                    update={"profile_id": "institution-b", "title": "Profile B draft"}
                )
                routes_authoring.create_authoring_project(second_request, self._auth())
                first_profile = routes_authoring.list_authoring_projects(
                    "INSTITUTION-A",
                    self._auth(),
                )

                with TestClient(app) as client:
                    response = client.get("/api/authoring/projects")

        self.assertEqual(422, response.status_code)
        self.assertEqual([first.project_id], [item.project_id for item in first_profile])

    def test_stale_revision_returns_409_without_echoing_draft_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                created = routes_authoring.create_authoring_project(
                    self._create_request(),
                    self._auth(),
                )
                with self.assertRaises(HTTPException) as raised:
                    routes_authoring.update_authoring_project(
                        str(created.project_id),
                        AuthoringProjectUpdateRequest(
                            expected_revision=99,
                            purpose="do not place this draft sentence in the audit",
                        ),
                        "institution-a",
                        self._auth(),
                    )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(409, raised.exception.status_code)
        serialized = json.dumps(records, ensure_ascii=False)
        self.assertNotIn("do not place this draft sentence", serialized)
        self.assertEqual("authoring revision conflict", records[-1]["detail"])

    def test_invalid_review_transition_returns_422_and_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                created = routes_authoring.create_authoring_project(
                    self._create_request(),
                    self._auth(),
                )
                drafting = routes_authoring.start_authoring_drafting(
                    str(created.project_id),
                    AuthoringTransitionRequest(expected_revision=created.revision),
                    "institution-a",
                    self._auth(),
                )
                with self.assertRaises(HTTPException) as raised:
                    routes_authoring.request_authoring_review(
                        str(created.project_id),
                        AuthoringTransitionRequest(expected_revision=drafting.revision),
                        "institution-a",
                        self._auth(),
                    )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(422, raised.exception.status_code)
        self.assertEqual("failure", records[-1]["outcome"])
        self.assertEqual("authoring transition or lint validation failed", records[-1]["detail"])

    def test_self_freeze_denial_returns_403_without_auditing_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings), patch.object(
                AuthoringService,
                "freeze_project",
                side_effect=PermissionError("secret reviewer comment"),
            ):
                with self.assertRaises(HTTPException) as raised:
                    routes_authoring.freeze_authoring_project(
                        "00000000-0000-0000-0000-000000000001",
                        AuthoringProjectFreezeRequest(expected_revision=1),
                        "institution-a",
                        self._auth(),
                    )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(403, raised.exception.status_code)
        self.assertEqual("denied", records[-1]["outcome"])
        self.assertNotIn("secret reviewer comment", json.dumps(records, ensure_ascii=False))

    def test_export_returns_in_memory_download_and_audits_only_metadata(self) -> None:
        content = b"draft export bytes"
        digest = "a" * 64
        artifact = SimpleNamespace(
            filename="draft.md",
            media_type="text/markdown; charset=utf-8",
            content=content,
            content_sha256=digest,
            semantic_content_sha256="b" * 64,
            project_id="00000000-0000-0000-0000-000000000001",
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings), patch.object(
                AuthoringService,
                "export_project",
                return_value=artifact,
            ):
                response = routes_authoring.export_authoring_project(
                    artifact.project_id,
                    AuthoringExportRequest(expected_revision=1, export_format="markdown"),
                    "institution-a",
                    self._auth(),
                )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(content, response.body)
        self.assertEqual(digest, response.headers["x-content-sha256"])
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertNotIn(content.decode(), json.dumps(records, ensure_ascii=False))
        self.assertEqual("markdown", records[-1]["export_format"])

    def test_complete_route_lifecycle_exports_only_a_verified_frozen_draft(self) -> None:
        draft_sentence = "The responsible department records each decision and retention period."
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                created = routes_authoring.create_authoring_project(
                    self._create_request(),
                    self._auth(),
                )
                drafting = routes_authoring.start_authoring_drafting(
                    str(created.project_id),
                    AuthoringTransitionRequest(expected_revision=created.revision),
                    "institution-a",
                    self._auth(),
                )
                clauses = [
                    clause.model_copy(update={"body": draft_sentence})
                    if clause.required
                    and clause.node_type not in {DraftNodeType.CHAPTER, DraftNodeType.SECTION}
                    else clause
                    for clause in drafting.clauses
                ]
                checklist = [
                    item.model_copy(update={"completed": True})
                    for item in drafting.checklist
                ]
                updated = routes_authoring.update_authoring_project(
                    str(created.project_id),
                    AuthoringProjectUpdateRequest(
                        expected_revision=drafting.revision,
                        clauses=clauses,
                        checklist=checklist,
                    ),
                    "institution-a",
                    self._auth(),
                )
                review = routes_authoring.request_authoring_review(
                    str(created.project_id),
                    AuthoringTransitionRequest(expected_revision=updated.revision),
                    "institution-a",
                    self._auth(),
                )
                frozen = routes_authoring.freeze_authoring_project(
                    str(created.project_id),
                    AuthoringProjectFreezeRequest(expected_revision=review.revision),
                    "institution-a",
                    self._auth(actor="reviewer-b"),
                )
                response = routes_authoring.export_authoring_project(
                    str(created.project_id),
                    AuthoringExportRequest(
                        expected_revision=frozen.revision,
                        export_format="json",
                    ),
                    "institution-a",
                    self._auth(actor="reviewer-b"),
                )
                redownload = routes_authoring.get_authoring_export(
                    str(created.project_id),
                    "institution-a",
                    self._auth(actor="reviewer-b"),
                )
                events = routes_authoring.list_authoring_events(
                    str(created.project_id),
                    "institution-a",
                    self._auth(actor="reviewer-b"),
                )
            records = self._audit_records(settings, "tenant-a")

        self.assertIn(OFFICIAL_BOUNDARY_NOTICE.encode("utf-8"), response.body)
        self.assertEqual(response.body, redownload.body)
        self.assertEqual(
            response.headers["x-content-sha256"],
            redownload.headers["x-content-sha256"],
        )
        self.assertEqual(
            [1, 2, 3, 4, 5, 6],
            [event["revision"] for event in events],
        )
        self.assertNotIn(draft_sentence, json.dumps(records, ensure_ascii=False))

    def test_tampered_event_metadata_returns_503_without_leaking_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                created = routes_authoring.create_authoring_project(
                    self._create_request(),
                    self._auth(),
                )
                data_dir = settings_for_tenant(settings, "tenant-a").data_dir
                event_path = (
                    data_dir
                    / "authoring"
                    / "events"
                    / str(created.project_id)
                    / "00000000000000000001.json"
                )
                manifest_path = (
                    data_dir
                    / "authoring"
                    / "projects"
                    / f"{created.project_id}.json"
                )
                event = json.loads(event_path.read_text(encoding="utf-8"))
                event["metadata"] = {"draft_text": "must not leak through events"}
                unsigned = dict(event)
                unsigned.pop("event_sha256", None)
                event["event_sha256"] = repository_module._record_sha256(unsigned)
                event_path.write_text(
                    json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["event_sha256"] = event["event_sha256"]
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(HTTPException) as raised:
                    routes_authoring.list_authoring_events(
                        str(created.project_id),
                        "institution-a",
                        self._auth(),
                    )
            records = self._audit_records(settings, "tenant-a")

        self.assertEqual(503, raised.exception.status_code)
        serialized = json.dumps(records, ensure_ascii=False)
        self.assertNotIn("must not leak through events", serialized)
        self.assertEqual("authoring storage unavailable", records[-1]["detail"])

    def test_templates_are_write_role_only_and_feature_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            with patch.object(routes_authoring, "get_settings", return_value=settings):
                templates = routes_authoring.list_authoring_templates(self._auth(role="operator"))
                with self.assertRaises(HTTPException) as raised:
                    routes_authoring.list_authoring_templates(self._auth(role="viewer"))

        self.assertGreaterEqual(len(templates), 1)
        self.assertEqual(403, raised.exception.status_code)

    def test_route_surface_has_no_import_approval_or_index_bridge(self) -> None:
        published_paths = {
            str(path)
            for path in app.openapi().get("paths", {})
            if str(path).startswith("/api/authoring")
        }
        declared_paths = {
            str(getattr(route, "path", ""))
            for route in routes_authoring.router.routes
            if str(getattr(route, "path", "")).startswith("/api/authoring")
        }
        expected = {
            "/api/authoring/templates",
            "/api/authoring/templates/{template_id}",
            "/api/authoring/projects",
            "/api/authoring/projects/{project_id}",
            "/api/authoring/projects/{project_id}/lint",
            "/api/authoring/projects/{project_id}/start-drafting",
            "/api/authoring/projects/{project_id}/request-review",
            "/api/authoring/projects/{project_id}/request-changes",
            "/api/authoring/projects/{project_id}/freeze",
            "/api/authoring/projects/{project_id}/reopen",
            "/api/authoring/projects/{project_id}/abandon",
            "/api/authoring/projects/{project_id}/export",
            "/api/authoring/projects/{project_id}/events",
        }

        self.assertTrue(expected.issubset(published_paths))
        self.assertFalse(
            any(
                "import" in path or "approve" in path or "index" in path
                for path in declared_paths
            )
        )

    def test_readiness_checks_isolated_authoring_storage_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            checks = readiness_checks(settings)

            authoring = next(
                check
                for check in checks
                if check["name"] == "authoring_storage_writeable_when_enabled"
            )

            self.assertTrue(authoring["passed"])
            self.assertEqual(settings.data_dir / "tenants", Path(str(authoring["path"])))

    @staticmethod
    @contextmanager
    def _http_client(
        settings: Settings,
        *,
        auth: AuthContext | None,
        max_body_bytes: int = 1024 * 1024,
        raise_server_exceptions: bool = True,
    ):
        api = FastAPI()
        api.add_middleware(
            JsonRequestBodyLimitMiddleware,
            max_body_bytes=max_body_bytes,
            rejection_observer=make_authoring_body_limit_observer(
                settings_provider=lambda: settings,
            ),
        )
        install_authoring_request_handlers(
            api,
            settings_provider=lambda: settings,
        )
        api.include_router(routes_authoring.router)
        api.dependency_overrides[config_get_settings] = lambda: settings
        if auth is not None:
            api.dependency_overrides[get_auth_context] = lambda: auth
        with patch.object(routes_authoring, "get_settings", return_value=settings):
            with TestClient(
                api,
                raise_server_exceptions=raise_server_exceptions,
            ) as client:
                yield client

    @staticmethod
    def _settings(tmp: str, *, enabled: bool = True) -> Settings:
        return Settings(
            app_env="test",
            data_dir=Path(tmp) / "data",
            artifact_root=Path(tmp),
            api_audit_enabled=True,
            enable_regulation_authoring=enabled,
            tenant_storage_isolation=True,
        )

    @staticmethod
    def _create_request(
        *, draft_marker: str = "Define a safe operating procedure."
    ) -> AuthoringProjectCreateRequest:
        return AuthoringProjectCreateRequest(
            profile_id="institution-a",
            title="Operations Regulation",
            purpose=draft_marker,
            scope="All regulation operators.",
            legal_bases=["Internal governance rule"],
            responsible_department="Governance Office",
            planned_effective_date=date(2027, 1, 1),
        )

    @staticmethod
    def _auth(
        *,
        role: str = "admin",
        actor: str = "author-a",
        tenant_id: str = "tenant-a",
    ) -> AuthContext:
        return AuthContext(
            actor=actor,
            tenant_id=tenant_id,
            auth_mode="api_token",
            role=role,
        )

    @staticmethod
    def _audit_records(settings: Settings, tenant_id: str) -> list[dict[str, object]]:
        scoped = settings_for_tenant(settings, tenant_id)
        path = scoped.data_dir / "repository" / "api_audit.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
