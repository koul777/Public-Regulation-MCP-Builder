from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

try:
    from streamlit.testing.v1 import AppTest
except Exception:  # pragma: no cover - optional in minimal environments
    AppTest = None

from app.core.config import Settings, clear_runtime_settings_overrides
from app.core.institution_profiles import (
    InstitutionProfile,
    InstitutionProfileRegistry,
    institution_profile_registry_to_bytes,
)
from app.schemas.authoring import (
    AuthoringMode,
    AuthoringProject,
    AuthoringProjectStatus,
)
from frontend.authoring_page import (
    AUTHORING_FLASH_MESSAGE_KEY,
    AUTHORING_NAV_LABEL,
    OFFICIAL_BOUNDARY_NOTICE,
    REDACTED_REVIEW_REASON,
    authoring_enabled,
    authoring_settings_for_tenant,
    editable_clauses_in_document_order,
    metadata_ready_for_drafting,
    next_authoring_action,
    safe_review_reason,
    _export_cache_matches_project,
    _export_key,
)
from app.services.authoring_template_service import AuthoringTemplateService


REPO_ROOT = Path(__file__).resolve().parents[1]

_PLANNING_DIRTY_APP = """
from datetime import date
from uuid import UUID
from app.schemas.authoring import AuthoringProject, AuthoringProjectStatus
from frontend.authoring_page import _render_metadata_editor, _render_start_drafting

project = AuthoringProject(
    project_id=UUID("00000000-0000-0000-0000-000000000011"),
    tenant_id="default",
    profile_id="test-profile",
    title="Policy",
    purpose="Saved purpose",
    scope="Saved scope",
    legal_bases=["Saved basis"],
    responsible_department="Saved department",
    planned_effective_date=date(2026, 10, 1),
    status=AuthoringProjectStatus.PLANNING,
    created_by="tester",
    updated_by="tester",
)
service = object()
_render_metadata_editor(
    project,
    service=service,
    tenant_id="default",
    profile_id="test-profile",
)
_render_start_drafting(
    project,
    service=service,
    tenant_id="default",
    profile_id="test-profile",
)
"""

_DRAFTING_DIRTY_APP = """
from datetime import date
from uuid import UUID
from app.schemas.authoring import (
    AuthoringLintReport,
    AuthoringProject,
    AuthoringProjectStatus,
    BeginnerChecklistItem,
    ClauseDraft,
)
from frontend.authoring_page import (
    _render_checklist_editor,
    _render_clause_editor,
    _render_lint_and_review,
    _render_metadata_editor,
)

project = AuthoringProject(
    project_id=UUID("00000000-0000-0000-0000-000000000012"),
    tenant_id="default",
    profile_id="test-profile",
    title="Policy",
    purpose="Saved purpose",
    scope="Saved scope",
    legal_bases=["Saved basis"],
    responsible_department="Saved department",
    planned_effective_date=date(2026, 10, 1),
    clauses=[
        ClauseDraft(
            clause_id=UUID("00000000-0000-0000-0000-000000000112"),
            article_number="Article1",
            body="Saved clause",
        )
    ],
    checklist=[
        BeginnerChecklistItem(
            item_id="checked",
            label="Saved check",
            guidance="Check the saved draft.",
            completed=True,
        )
    ],
    status=AuthoringProjectStatus.DRAFTING,
    created_by="tester",
    updated_by="tester",
)
project = project.model_copy(
    update={
        "last_lint_report": AuthoringLintReport(
            project_id=project.project_id,
            revision=project.revision,
        )
    }
)
service = object()
_render_metadata_editor(
    project,
    service=service,
    tenant_id="default",
    profile_id="test-profile",
)
_render_clause_editor(
    project,
    service=service,
    tenant_id="default",
    profile_id="test-profile",
)
_render_checklist_editor(
    project,
    service=service,
    tenant_id="default",
    profile_id="test-profile",
)
_render_lint_and_review(
    project,
    service=service,
    tenant_id="default",
    profile_id="test-profile",
)
"""

_REVIEW_NOT_READY_APP = """
from uuid import UUID
from app.schemas.authoring import AuthoringProject, AuthoringProjectStatus
from frontend.authoring_page import _render_review_actions

project = AuthoringProject(
    project_id=UUID("00000000-0000-0000-0000-000000000013"),
    tenant_id="default",
    profile_id="test-profile",
    title="Policy",
    status=AuthoringProjectStatus.REVIEW_REQUESTED,
    created_by="tester",
    updated_by="tester",
)
_render_review_actions(
    project,
    service=object(),
    tenant_id="default",
    profile_id="test-profile",
)
"""

_REVIEW_ACTION_BUFFER_APP = """
from uuid import UUID
import streamlit as st
from types import SimpleNamespace
from app.schemas.authoring import (
    AuthoringLintReport,
    AuthoringProject,
    AuthoringProjectStatus,
    BeginnerChecklistItem,
)
from frontend.authoring_page import (
    authoring_profile_has_unsaved_state,
    render_authoring_page,
)

def project(project_id, title):
    draft = AuthoringProject(
        project_id=UUID(project_id),
        tenant_id="default",
        profile_id="test-profile",
        title=title,
        checklist=[BeginnerChecklistItem(
            item_id="checked",
            label="Saved check",
            guidance="Check the saved draft.",
            completed=True,
        )],
        status=AuthoringProjectStatus.REVIEW_REQUESTED,
        created_by="tester",
        updated_by="tester",
    )
    return draft.model_copy(update={
        "last_lint_report": AuthoringLintReport(
            project_id=draft.project_id,
            revision=draft.revision,
        )
    })

projects = [
    project("00000000-0000-0000-0000-000000000021", "Review A"),
    project("00000000-0000-0000-0000-000000000022", "Review B"),
]

class Service:
    def list_projects(self, *, tenant_id, profile_id):
        return projects
    def get_project(self, project_id, *, tenant_id, profile_id):
        return next(item for item in projects if str(item.project_id) == project_id)

render_authoring_page(
    settings=SimpleNamespace(enable_regulation_authoring=True),
    profile_id="test-profile",
    institution_name="Test Institution",
    tenant_id="default",
    service=Service(),
)
st.caption(
    f"PROFILE_DIRTY={authoring_profile_has_unsaved_state('default', 'test-profile')}"
)
"""

_CROSS_TENANT_BUFFER_APP = """
from uuid import UUID
import streamlit as st
from app.schemas.authoring import AuthoringProject
from frontend.authoring_page import (
    _editor_buffer,
    _editor_dirty_key,
    _set_editor_buffer,
    authoring_profile_has_unsaved_state,
)

project_id = UUID("00000000-0000-0000-0000-000000000023")
project_a = AuthoringProject(
    project_id=project_id,
    tenant_id="tenant-a",
    profile_id="profile-a",
    title="Tenant A",
    created_by="tester",
    updated_by="tester",
)
project_b = AuthoringProject(
    project_id=project_id,
    tenant_id="tenant-b",
    profile_id="profile-b",
    title="Tenant B",
    created_by="tester",
    updated_by="tester",
)
st.session_state[_editor_dirty_key(project_a, "metadata")] = True
_set_editor_buffer(
    project_a,
    "metadata",
    {"purpose": "TENANT_A_PRIVATE_BUFFER"},
)
buffer_b = _editor_buffer(project_b, "metadata", {"purpose": "tenant-b-default"})
st.caption(f"B_VALUE={buffer_b['purpose']}")
st.caption(
    f"A_DIRTY={authoring_profile_has_unsaved_state('tenant-a', 'profile-a')}"
)
st.caption(
    f"B_DIRTY={authoring_profile_has_unsaved_state('tenant-b', 'profile-b')}"
)
"""

_CONFLICT_RECOVERY_APP = """
from uuid import UUID
import streamlit as st
from app.schemas.authoring import AuthoringProject, AuthoringProjectStatus
from app.services.authoring_service import AuthoringConflictError
from frontend.authoring_page import (
    AUTHORING_SELECTED_SCOPE_KEY,
    AUTHORING_SELECTED_PROJECT_KEY,
    _project_scope,
    _project_state_key,
    _project_has_unresolved_conflict,
    _render_action_error,
    _render_conflict_recovery,
)

project = AuthoringProject(
    project_id=UUID("00000000-0000-0000-0000-000000000014"),
    tenant_id="default",
    profile_id="test-profile",
    title="Policy",
    status=AuthoringProjectStatus.DRAFTING,
    created_by="tester",
    updated_by="tester",
)
project_id = str(project.project_id)
st.session_state.setdefault(AUTHORING_SELECTED_PROJECT_KEY, project_id)
st.session_state.setdefault(AUTHORING_SELECTED_SCOPE_KEY, _project_scope(project))
conflict_rendered = _project_has_unresolved_conflict(project)
_render_conflict_recovery(project)
if not st.session_state.get("conflict-triggered"):
    st.session_state[
        _project_state_key("editor", project, "metadata", "purpose")
    ] = "Unsaved local purpose"
    st.session_state[_project_state_key("dirty", project, "metadata")] = True
    _render_action_error(AuthoringConflictError(expected_revision=1, actual_revision=2))
    st.session_state["conflict-triggered"] = True
if not conflict_rendered and _project_has_unresolved_conflict(project):
    _render_conflict_recovery(project)
"""

_STALE_DIRTY_REVISION_APP = """
from datetime import date
from uuid import UUID
import streamlit as st
from app.schemas.authoring import (
    AuthoringProject,
    AuthoringProjectStatus,
    BeginnerChecklistItem,
    ClauseDraft,
)
from frontend.authoring_page import (
    AUTHORING_SELECTED_PROJECT_KEY,
    _project_state_key,
    _render_checklist_editor,
    _render_clause_editor,
    _render_conflict_recovery,
    _render_metadata_editor,
    _sync_editor_revision_state,
)

project = AuthoringProject(
    project_id=UUID("00000000-0000-0000-0000-000000000015"),
    tenant_id="default",
    profile_id="test-profile",
    title="Policy",
    purpose="Server revision two",
    scope="Saved scope",
    legal_bases=["Saved basis"],
    responsible_department="Saved department",
    planned_effective_date=date(2026, 10, 1),
    clauses=[ClauseDraft(
        clause_id=UUID("00000000-0000-0000-0000-000000000115"),
        article_number="Article1",
        body="Server clause revision two",
    )],
    checklist=[BeginnerChecklistItem(
        item_id="checked",
        label="Saved check",
        guidance="Check the saved draft.",
        completed=False,
    )],
    status=AuthoringProjectStatus.DRAFTING,
    revision=2,
    created_by="tester",
    updated_by="other-window",
)
project_id = str(project.project_id)
section = str(st.session_state.get("section-under-test") or "metadata")
st.session_state.setdefault(AUTHORING_SELECTED_PROJECT_KEY, project_id)
st.session_state.setdefault(_project_state_key("dirty", project, section), True)
st.session_state.setdefault(_project_state_key("base-revision", project, section), 1)
if section == "metadata":
    st.session_state.setdefault(
        _project_state_key("editor", project, "metadata", "purpose"),
        "Unsaved local purpose",
    )
elif section == "clauses":
    st.session_state.setdefault(
        _project_state_key(
            "editor", project, "clauses", f"body:{project.clauses[0].clause_id}"
        ),
        "Unsaved local clause",
    )
else:
    st.session_state.setdefault(
        _project_state_key("editor", project, "checklist", "completed:checked"),
        True,
    )

class Service:
    def update_project(self, *args, **kwargs):
        st.session_state["mutation-called"] = True

service = Service()
_sync_editor_revision_state(project)
_render_conflict_recovery(project)
if section == "metadata":
    _render_metadata_editor(project, service=service, tenant_id="default", profile_id="test-profile")
elif section == "clauses":
    _render_clause_editor(project, service=service, tenant_id="default", profile_id="test-profile")
else:
    _render_checklist_editor(project, service=service, tenant_id="default", profile_id="test-profile")
"""


def _project(status: AuthoringProjectStatus) -> AuthoringProject:
    return AuthoringProject(
        tenant_id="default",
        profile_id="test-profile",
        title="여비 규정",
        status=status,
        created_by="tester",
        updated_by="tester",
    )


def _session_key(
    kind: str,
    project_id: object,
    *parts: object,
    tenant_id: str = "default",
    profile_id: str = "test-profile",
) -> str:
    suffix = "".join(f":{part}" for part in parts)
    return (
        f"authoring-v2-{kind}:{tenant_id}:{profile_id}:{project_id}{suffix}"
    )


def _seed_app(
    app,
    *,
    enabled: bool,
    data_dir: Path,
    nav_page: str,
    profile_ids: tuple[str, ...] = ("test-profile",),
    selected_profile_id: str = "test-profile",
    tenant_id: str | None = "default",
) -> None:
    registry = InstitutionProfileRegistry(
        profiles={
            profile_id: InstitutionProfile(
                profile_id=profile_id,
                display_name=f"테스트 {profile_id}",
                institution_name=f"테스트 기관 {profile_id}",
                tenant_id=tenant_id,
            )
            for profile_id in profile_ids
        },
        default_profile_id=profile_ids[0],
    )
    app.session_state["institution_profile_registry_bytes"] = (
        institution_profile_registry_to_bytes(registry)
    )
    app.session_state["selected_institution_profile_id"] = selected_profile_id
    app.session_state["beginner_guide_choice_made"] = True
    app.session_state["beginner_guide_enabled"] = False
    app.session_state["nav_page"] = nav_page
    app.session_state["ai_connection_overrides"] = {
        "data_dir": data_dir,
        "artifact_root": data_dir.parent,
        "enable_regulation_authoring": enabled,
    }


def _create_drafting_project(app, *, title: str) -> str:
    """Drive the production page to a persisted drafting project."""

    app.run()
    next(item for item in app.text_input if item.label == "규정명").set_value(title)
    next(
        button for button in app.button if button.label == "초안 공간 만들기"
    ).click().run()
    next(area for area in app.text_area if area.label == "목적").set_value(
        "저장된 목적"
    )
    next(area for area in app.text_area if area.label == "적용 범위").set_value(
        "저장된 적용 범위"
    )
    next(
        area
        for area in app.text_area
        if area.label == "법적·내부 근거 (한 줄에 하나)"
    ).set_value("저장된 내부 근거")
    next(item for item in app.text_input if item.label == "담당부서").set_value(
        "저장된 담당부서"
    )
    next(item for item in app.date_input if item.label == "시행 예정일").set_value(
        date(2026, 10, 1)
    )
    next(
        button for button in app.button if button.label == "기본정보 저장"
    ).click().run()
    next(
        button for button in app.button if button.label == "조문 작성 시작"
    ).click().run()
    return str(app.session_state["authoring_selected_project_id"])


class StreamlitAuthoringTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_runtime_settings_overrides()

    def test_feature_gate_is_fail_closed(self) -> None:
        self.assertFalse(authoring_enabled(SimpleNamespace()))
        self.assertFalse(
            authoring_enabled(SimpleNamespace(enable_regulation_authoring=False))
        )
        self.assertTrue(
            authoring_enabled(SimpleNamespace(enable_regulation_authoring=True))
        )

    def test_drafting_requires_all_saved_metadata(self) -> None:
        incomplete = _project(AuthoringProjectStatus.PLANNING)
        complete = incomplete.model_copy(
            update={
                "purpose": "여비 지급 원칙을 정한다.",
                "scope": "모든 임직원에게 적용한다.",
                "legal_bases": ["이사회 운영 규정"],
                "responsible_department": "경영지원부",
                "planned_effective_date": date(2026, 10, 1),
            }
        )

        self.assertFalse(metadata_ready_for_drafting(incomplete))
        self.assertTrue(metadata_ready_for_drafting(complete))
        revision = complete.model_copy(
            update={"authoring_mode": AuthoringMode.PARTIAL_REVISION}
        )
        self.assertFalse(metadata_ready_for_drafting(revision))
        self.assertTrue(
            metadata_ready_for_drafting(
                revision.model_copy(
                    update={
                        "revision_reason": "집행 기준을 명확히 한다.",
                        "predecessor_reference": "여비 규정 2025-01판",
                    }
                )
            )
        )

    def test_unsaved_metadata_disables_start_drafting(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        app = AppTest.from_string(_PLANNING_DIRTY_APP, default_timeout=30)
        app.run()
        start_button = next(
            button for button in app.button if button.label == "조문 작성 시작"
        )
        self.assertFalse(start_button.disabled)

        next(area for area in app.text_area if area.label == "목적").set_value(
            "Unsaved purpose"
        ).run()

        start_button = next(
            button for button in app.button if button.label == "조문 작성 시작"
        )
        self.assertFalse(app.exception)
        self.assertTrue(start_button.disabled)
        self.assertTrue(
            any("기본정보를 먼저 저장" in warning.value for warning in app.warning)
        )

    def test_each_unsaved_draft_section_disables_lint_and_review(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        def assert_blocked_after_change(section: str) -> None:
            app = AppTest.from_string(_DRAFTING_DIRTY_APP, default_timeout=30)
            app.run()
            if section == "metadata":
                next(area for area in app.text_area if area.label == "목적").set_value(
                    "Unsaved purpose"
                ).run()
            elif section == "clauses":
                next(
                    area
                    for area in app.text_area
                    if ":clauses:body:" in str(area.key or "")
                ).set_value("Unsaved clause").run()
            else:
                next(
                    item
                    for item in app.checkbox
                    if ":checklist:completed:" in str(item.key or "")
                ).set_value(False).run()

            lint_button = next(
                button for button in app.button if button.label == "작성 검사"
            )
            review_button = next(
                button for button in app.button if button.label == "내용 확인 요청"
            )
            self.assertFalse(app.exception, section)
            self.assertTrue(lint_button.disabled, section)
            self.assertTrue(review_button.disabled, section)
            self.assertTrue(
                any("저장하지 않은" in warning.value for warning in app.warning),
                section,
            )

        for section in ("metadata", "clauses", "checklist"):
            with self.subTest(section=section):
                assert_blocked_after_change(section)

    def test_clean_sections_cannot_create_noop_revision_or_clear_lint(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        app = AppTest.from_string(_DRAFTING_DIRTY_APP, default_timeout=30)
        app.run()
        save_buttons = {
            button.label: button
            for button in app.button
            if button.label in {"기본정보 저장", "조문 저장", "확인 목록 저장"}
        }
        self.assertEqual(
            {"기본정보 저장", "조문 저장", "확인 목록 저장"},
            set(save_buttons),
        )
        self.assertTrue(all(button.disabled for button in save_buttons.values()))

        next(
            area
            for area in app.text_area
            if ":clauses:body:" in str(area.key or "")
        ).set_value("Changed clause").run()
        save_buttons = {
            button.label: button
            for button in app.button
            if button.label in {"기본정보 저장", "조문 저장", "확인 목록 저장"}
        }
        self.assertTrue(save_buttons["기본정보 저장"].disabled)
        self.assertFalse(save_buttons["조문 저장"].disabled)
        self.assertTrue(save_buttons["확인 목록 저장"].disabled)

    def test_revision_conflict_preserves_input_until_confirmed_reload(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        project_id = "00000000-0000-0000-0000-000000000014"
        editor_key = _session_key("editor", project_id, "metadata", "purpose")
        dirty_key = _session_key("dirty", project_id, "metadata")
        app = AppTest.from_string(_CONFLICT_RECOVERY_APP, default_timeout=30)
        app.run()
        reload_button = next(
            button for button in app.button if button.label == "최신 개정 불러오기"
        )
        self.assertTrue(reload_button.disabled)
        self.assertEqual("Unsaved local purpose", app.session_state[editor_key])
        self.assertTrue(app.session_state[dirty_key])

        next(
            item
            for item in app.checkbox
            if item.label
            == "현재 미저장 입력을 필요한 곳에 복사했고, 최신 개정을 불러오겠습니다."
        ).set_value(True).run()
        next(
            button for button in app.button if button.label == "최신 개정 불러오기"
        ).click().run()

        self.assertFalse(app.exception)
        with self.assertRaises(KeyError):
            _ = app.session_state[editor_key]
        with self.assertRaises(KeyError):
            _ = app.session_state[dirty_key]

    def test_dirty_sections_never_rebase_onto_a_new_server_revision(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        project_id = "00000000-0000-0000-0000-000000000015"
        save_labels = {
            "metadata": "기본정보 저장",
            "clauses": "조문 저장",
            "checklist": "확인 목록 저장",
        }
        local_values = {
            "metadata": (
                _session_key("editor", project_id, "metadata", "purpose"),
                "Unsaved local purpose",
            ),
            "clauses": (
                _session_key(
                    "editor",
                    project_id,
                    "clauses",
                    "body:00000000-0000-0000-0000-000000000115",
                ),
                "Unsaved local clause",
            ),
            "checklist": (
                _session_key(
                    "editor", project_id, "checklist", "completed:checked"
                ),
                True,
            ),
        }
        for section, save_label in save_labels.items():
            with self.subTest(section=section):
                app = AppTest.from_string(
                    _STALE_DIRTY_REVISION_APP,
                    default_timeout=30,
                )
                app.session_state["section-under-test"] = section
                app.run()
                save_button = next(
                    button for button in app.button if button.label == save_label
                )
                value_key, expected_value = local_values[section]
                self.assertFalse(app.exception)
                self.assertTrue(save_button.disabled)
                self.assertEqual(
                    1,
                    app.session_state[
                        _session_key("base-revision", project_id, section)
                    ],
                )
                self.assertEqual(expected_value, app.session_state[value_key])
                with self.assertRaises(KeyError):
                    _ = app.session_state["mutation-called"]
                self.assertTrue(
                    any("다른 화면" in item.value for item in app.error)
                )

    def test_review_freeze_is_disabled_without_current_clean_saved_checks(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        app = AppTest.from_string(_REVIEW_NOT_READY_APP, default_timeout=30)
        app.run()

        self_freeze = next(
            item
            for item in app.checkbox
            if item.label == "연습용 자체 확인으로만 내용을 동결합니다."
        )
        freeze_button = next(
            button for button in app.button if button.label == "연습용으로 내용 동결"
        )
        self.assertFalse(app.exception)
        self.assertTrue(self_freeze.disabled)
        self.assertTrue(freeze_button.disabled)
        self.assertTrue(
            any("내용을 동결할 수 없습니다" in item.value for item in app.warning)
        )

    def test_unsaved_review_action_reasons_survive_project_round_trip(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        app = AppTest.from_string(_REVIEW_ACTION_BUFFER_APP, default_timeout=30)
        app.run()
        project_a = "00000000-0000-0000-0000-000000000021"
        project_b = "00000000-0000-0000-0000-000000000022"
        next(
            area for area in app.text_area if area.label == "수정 요청 메모"
        ).set_value("A의 아직 저장하지 않은 상세 검토 의견").run()
        next(item for item in app.selectbox if item.label == "초안 선택").set_value(
            project_b
        ).run()
        self.assertTrue(
            any("PROFILE_DIRTY=True" in item.value for item in app.caption)
        )
        next(item for item in app.selectbox if item.label == "초안 선택").set_value(
            project_a
        ).run()
        change_comment = next(
            area for area in app.text_area if area.label == "수정 요청 메모"
        )
        self.assertEqual(
            "A의 아직 저장하지 않은 상세 검토 의견",
            change_comment.value,
        )

        change_comment.set_value("").run()
        next(
            item
            for item in app.checkbox
            if item.label == "연습용 자체 확인으로만 내용을 동결합니다."
        ).set_value(True).run()
        next(
            area for area in app.text_area if area.label == "자체 확인 사유"
        ).set_value("A의 아직 저장하지 않은 자체 확인 사유").run()
        next(item for item in app.selectbox if item.label == "초안 선택").set_value(
            project_b
        ).run()
        next(item for item in app.selectbox if item.label == "초안 선택").set_value(
            project_a
        ).run()
        freeze_reason = next(
            area for area in app.text_area if area.label == "자체 확인 사유"
        )
        self_freeze = next(
            item
            for item in app.checkbox
            if item.label == "연습용 자체 확인으로만 내용을 동결합니다."
        )

        self.assertFalse(app.exception)
        self.assertEqual(
            "A의 아직 저장하지 않은 자체 확인 사유",
            freeze_reason.value,
        )
        self.assertFalse(self_freeze.value)
        self.assertFalse(self_freeze.disabled)

    def test_review_decision_paths_are_mutually_exclusive_and_resettable(
        self,
    ) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        app = AppTest.from_string(_REVIEW_ACTION_BUFFER_APP, default_timeout=30)
        app.run()
        change_comment = next(
            area for area in app.text_area if area.label == "수정 요청 메모"
        )
        change_comment.set_value("제3조를 고쳐야 합니다.").run()

        request_changes = next(
            button for button in app.button if button.label == "수정 요청"
        )
        freeze = next(
            button
            for button in app.button
            if button.label == "연습용으로 내용 동결"
        )
        self_freeze = next(
            item
            for item in app.checkbox
            if item.label == "연습용 자체 확인으로만 내용을 동결합니다."
        )
        self.assertEqual(1, sum(not item.disabled for item in (request_changes, freeze)))
        self.assertFalse(request_changes.disabled)
        self.assertTrue(freeze.disabled)
        self.assertTrue(self_freeze.disabled)

        next(
            button
            for button in app.button
            if button.label == "검토 결정 입력 지우기"
        ).click().run()
        self.assertEqual(
            "",
            next(
                area for area in app.text_area if area.label == "수정 요청 메모"
            ).value,
        )
        self_freeze = next(
            item
            for item in app.checkbox
            if item.label == "연습용 자체 확인으로만 내용을 동결합니다."
        )
        self.assertFalse(self_freeze.disabled)
        self.assertFalse(self_freeze.value)

        self_freeze.set_value(True).run()
        next(
            area for area in app.text_area if area.label == "자체 확인 사유"
        ).set_value("연습용 동결을 선택했습니다.").run()
        request_changes = next(
            button for button in app.button if button.label == "수정 요청"
        )
        freeze = next(
            button
            for button in app.button
            if button.label == "연습용으로 내용 동결"
        )
        change_comment = next(
            area for area in app.text_area if area.label == "수정 요청 메모"
        )

        self.assertFalse(app.exception)
        self.assertEqual(1, sum(not item.disabled for item in (request_changes, freeze)))
        self.assertTrue(request_changes.disabled)
        self.assertFalse(freeze.disabled)
        self.assertTrue(change_comment.disabled)

        next(
            button
            for button in app.button
            if button.label == "검토 결정 입력 지우기"
        ).click().run()
        request_changes = next(
            button for button in app.button if button.label == "수정 요청"
        )
        freeze = next(
            button
            for button in app.button
            if button.label == "연습용으로 내용 동결"
        )
        self.assertTrue(request_changes.disabled)
        self.assertTrue(freeze.disabled)
        self.assertEqual(
            "",
            next(
                area for area in app.text_area if area.label == "자체 확인 사유"
            ).value,
        )

    def test_streamlit_authoring_uses_physical_tenant_storage_when_enabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp) / "data",
                tenant_storage_isolation=True,
            )

            scoped = authoring_settings_for_tenant(settings, "tenant-a")

            self.assertEqual(
                settings.data_dir / "tenants" / "tenant-a",
                scoped.data_dir,
            )
            self.assertEqual(
                settings.data_dir / "tenants" / "tenant-a" / "authoring",
                scoped.authoring_dir,
            )

    def test_browser_buffers_are_scoped_by_tenant_profile_and_project(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        app = AppTest.from_string(_CROSS_TENANT_BUFFER_APP, default_timeout=30)
        app.run()
        captions = [item.value for item in app.caption]

        self.assertFalse(app.exception)
        self.assertIn("B_VALUE=tenant-b-default", captions)
        self.assertIn("A_DIRTY=True", captions)
        self.assertIn("B_DIRTY=False", captions)

    def test_export_cache_requires_current_frozen_identity_and_bytes(self) -> None:
        content = b"verified export"
        content_sha256 = hashlib.sha256(content).hexdigest()
        frozen_hash = "a" * 64
        project = _project(AuthoringProjectStatus.EXPORTED).model_copy(
            update={
                "tenant_id": "tenant-a",
                "profile_id": "profile-a",
                "frozen_revision": 5,
                "frozen_content_hash": frozen_hash,
                "semantic_content_hash": frozen_hash,
            }
        )
        artifact = {
            "content": content,
            "filename": "draft.md",
            "media_type": "text/markdown",
            "tenant_id": "tenant-a",
            "profile_id": "profile-a",
            "project_id": str(project.project_id),
            "frozen_revision": 5,
            "semantic_content_sha256": frozen_hash,
            "content_sha256": content_sha256,
        }

        self.assertTrue(_export_cache_matches_project(project, artifact))
        self.assertFalse(
            _export_cache_matches_project(
                project.model_copy(
                    update={"status": AuthoringProjectStatus.CONTENT_FROZEN}
                ),
                artifact,
            )
        )
        self.assertFalse(
            _export_cache_matches_project(
                project.model_copy(update={"frozen_revision": 6}),
                artifact,
            )
        )
        self.assertFalse(
            _export_cache_matches_project(
                project,
                {**artifact, "content": b"tampered export"},
            )
        )
        same_uuid_other_tenant = project.model_copy(update={"tenant_id": "tenant-b"})
        self.assertNotEqual(_export_key(project), _export_key(same_uuid_other_tenant))

    def test_every_authoring_state_has_one_plain_language_next_action_or_decision(
        self,
    ) -> None:
        actions = {
            status: next_authoring_action(_project(status))
            for status in AuthoringProjectStatus
        }

        self.assertEqual(set(AuthoringProjectStatus), set(actions))
        self.assertTrue(all(action.strip() for action in actions.values()))
        self.assertIn("작성 검사", actions[AuthoringProjectStatus.DRAFTING])
        self.assertIn("Markdown", actions[AuthoringProjectStatus.CONTENT_FROZEN])
        self.assertIn("별도", actions[AuthoringProjectStatus.EXPORTED])

    def test_general_template_editor_keeps_document_reading_order(self) -> None:
        project = _project(AuthoringProjectStatus.DRAFTING).model_copy(
            update={
                "clauses": AuthoringTemplateService().instantiate_clauses(
                    "general-regulation",
                    project_id=UUID("00000000-0000-0000-0000-000000000001"),
                )
            }
        )

        article_numbers = [
            clause.article_number
            for clause in editable_clauses_in_document_order(project)
        ]

        self.assertEqual(
            ["제1조", "제2조", "제3조", "제4조", "제5조", "제6조", "부칙"],
            article_numbers,
        )

    def test_ui_module_has_no_official_approval_or_index_mutation_imports(self) -> None:
        source = (REPO_ROOT / "frontend" / "authoring_page.py").read_text(
            encoding="utf-8"
        )
        forbidden = (
            "approve_review_chunks",
            "index_document",
            "JsonRepository",
            "write_mcp_setup_bundle",
            "app.schemas.document",
            "app.schemas.chunk",
        )

        self.assertEqual("공식 승인 아님", OFFICIAL_BOUNDARY_NOTICE)
        self.assertIn("OFFICIAL_BOUNDARY_NOTICE", source)
        for token in forbidden:
            self.assertNotIn(token, source)

    def test_review_reason_redacts_entire_value_for_spaced_absolute_paths(self) -> None:
        cases = (
            "확인 자료 C:" + r"\Users\Jane Doe\secret draft.docx 참고",
            r"확인 자료 \\fileserver\Policy Share\secret draft.docx 참고",
            "확인 자료 /" + "home/Jane Doe/secret draft.docx 참고",
            "확인 자료 /opt/Policy Files/secret draft.docx 참고",
        )

        for reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(REDACTED_REVIEW_REASON, safe_review_reason(reason))

    def test_review_reason_keeps_https_reference_links(self) -> None:
        reason = "근거는 https://example.org/rules/42 문서를 확인하세요."

        self.assertEqual(reason, safe_review_reason(reason))

    def test_content_frozen_branch_offers_non_destructive_reopen(self) -> None:
        source = (REPO_ROOT / "frontend" / "authoring_page.py").read_text(
            encoding="utf-8"
        )
        frozen_branch = source.split(
            "elif project.status == AuthoringProjectStatus.CONTENT_FROZEN:", 1
        )[1].split("elif project.status == AuthoringProjectStatus.EXPORTED:", 1)[0]

        self.assertIn("_render_export_actions", frozen_branch)
        self.assertIn("_render_reopen_action", frozen_branch)
        self.assertIn("새 개정본", source)
        self.assertEqual("", safe_review_reason(None))

    def test_disabled_flag_hides_menu_and_blocks_direct_navigation(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            home = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(home, enabled=False, data_dir=data_dir, nav_page="🏠 시작하기")
            home.run()
            home_labels = [button.label for button in home.button]

            direct = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                direct,
                enabled=False,
                data_dir=data_dir,
                nav_page=AUTHORING_NAV_LABEL,
            )
            direct.run()

        self.assertFalse(home.exception)
        self.assertNotIn(AUTHORING_NAV_LABEL, home_labels)
        self.assertFalse(direct.exception)
        self.assertTrue(
            any("꺼져 있어" in error.value for error in direct.error),
            [error.value for error in direct.error],
        )

    def test_enabled_home_and_sidebar_offer_authoring_entry(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=Path(tmp) / "data",
                nav_page="🏠 시작하기",
            )
            app.run()
            labels = [button.label for button in app.button]

        self.assertFalse(app.exception)
        self.assertGreaterEqual(labels.count(AUTHORING_NAV_LABEL), 2)

    def test_authoring_screen_is_clearly_practice_only_and_hides_official_guide(
        self,
    ) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=Path(tmp) / "data",
                nav_page=AUTHORING_NAV_LABEL,
            )
            app.session_state["beginner_guide_enabled"] = True
            app.run()

            visible_text = " ".join(
                str(element.value)
                for collection in (app.markdown, app.caption, app.info, app.warning)
                for element in collection
            )
            radio_labels = [item.label for item in app.radio]
            toggle_labels = [item.label for item in app.toggle]
            button_labels = [item.label for item in app.button]

        self.assertFalse(app.exception)
        self.assertIn("1인 연습용", visible_text)
        self.assertIn("모든 출력은 연습용", visible_text)
        self.assertIn("보호 모드 API", visible_text)
        self.assertIn("본문의 1~6단계", visible_text)
        self.assertNotIn("기본 작업 순서", radio_labels)
        self.assertNotIn("Qwen 또는 MCP 선택", radio_labels)
        self.assertNotIn("초보자 안내 모드", toggle_labels)
        self.assertNotIn("승인 RAG", visible_text)
        self.assertNotIn("④ 메뉴", visible_text)
        self.assertFalse(any("Qwen" in label for label in button_labels))

    def test_lint_findings_spell_out_severity_without_relying_on_color(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        app = AppTest.from_string(
            """
from app.schemas.authoring import (
    AuthoringLintFinding,
    AuthoringLintSeverity,
)
from frontend.authoring_page import _render_lint_finding

for severity in AuthoringLintSeverity:
    _render_lint_finding(
        AuthoringLintFinding(
            code=f\"test_{severity.value}\",
            severity=severity,
            message=\"Test message.\",
            field_path=\"unknown_field\",
            suggestion=\"Fix it.\",
        )
    )
""",
            default_timeout=30,
        )
        app.run()

        self.assertFalse(app.exception)
        self.assertTrue(any("오류 · unknown_field" in item.value for item in app.error))
        self.assertTrue(
            any("경고 · unknown_field" in item.value for item in app.warning)
        )
        self.assertTrue(any("안내 · unknown_field" in item.value for item in app.info))

    def test_authoring_screen_creates_isolated_beginner_project(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=data_dir,
                nav_page=AUTHORING_NAV_LABEL,
            )
            app.run()
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "여비 규정"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()

            manifests = list((data_dir / "authoring" / "projects").glob("*.json"))

        self.assertFalse(app.exception)
        self.assertEqual(1, len(manifests))
        self.assertFalse((data_dir / "vectors").exists())
        self.assertFalse((data_dir / "approval_journal.jsonl").exists())

    def test_planning_project_can_be_explicitly_abandoned_after_wrong_choice(
        self,
    ) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=Path(tmp) / "data",
                nav_page=AUTHORING_NAV_LABEL,
            )
            app.run()
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "Wrong template"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()
            project_id = str(app.session_state["authoring_selected_project_id"])
            next(area for area in app.text_area if area.label == "목적").set_value(
                "중단과 함께 지워질 미저장 목적"
            ).run()

            confirmation = next(
                item
                for item in app.checkbox
                if item.label == "이 초안을 더 이상 작성하지 않고 중단하겠습니다."
            )
            abandon_button = next(
                button for button in app.button if button.label == "이 초안 작성 중단"
            )
            self.assertTrue(abandon_button.disabled)
            confirmation.set_value(True).run()
            next(
                area for area in app.text_area if area.label == "작성 중단 사유"
            ).set_value("Wrong template selected").run()
            abandon_button = next(
                button for button in app.button if button.label == "이 초안 작성 중단"
            )
            self.assertFalse(abandon_button.disabled)
            abandon_button.click().run()

            visible_text = " ".join(
                str(element.value)
                for collection in (app.caption, app.info, app.success)
                for element in collection
            )
            try:
                selected_after_abandon = str(
                    app.session_state["authoring_selected_project_id"] or ""
                )
            except KeyError:
                selected_after_abandon = ""
            abandoned_editor_keys = [
                str(key)
                for key in app.session_state.filtered_state
                if str(key).startswith(
                    (
                        f"authoring-v2-editor:default:test-profile:{project_id}:",
                        f"authoring-v2-dirty:default:test-profile:{project_id}:",
                        f"authoring-v2-base-revision:default:test-profile:{project_id}:",
                        f"authoring-v2-buffer:default:test-profile:{project_id}:",
                    )
                )
            ]
            flash_message_pending = (
                AUTHORING_FLASH_MESSAGE_KEY in app.session_state.filtered_state
            )

        self.assertFalse(app.exception)
        self.assertIn("중단했습니다", visible_text)
        self.assertFalse(flash_message_pending)
        self.assertEqual("", selected_after_abandon)
        self.assertEqual([], abandoned_editor_keys)
        self.assertTrue(
            any(item.label.startswith("중단 초안 보기") for item in app.checkbox)
        )

    def test_planning_transition_paths_are_mutually_exclusive_and_resettable(
        self,
    ) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=Path(tmp) / "data",
                nav_page=AUTHORING_NAV_LABEL,
            )
            app.run()
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "전이 선택 규정"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()
            next(area for area in app.text_area if area.label == "목적").set_value(
                "저장된 목적"
            )
            next(
                area for area in app.text_area if area.label == "적용 범위"
            ).set_value("저장된 적용 범위")
            next(
                area
                for area in app.text_area
                if area.label == "법적·내부 근거 (한 줄에 하나)"
            ).set_value("저장된 내부 근거")
            next(
                item for item in app.text_input if item.label == "담당부서"
            ).set_value("저장된 담당부서")
            next(
                item for item in app.date_input if item.label == "시행 예정일"
            ).set_value(date(2026, 10, 1))
            next(
                button for button in app.button if button.label == "기본정보 저장"
            ).click().run()

            next(
                item
                for item in app.checkbox
                if item.label == "이 초안을 더 이상 작성하지 않고 중단하겠습니다."
            ).set_value(True).run()
            next(
                area for area in app.text_area if area.label == "작성 중단 사유"
            ).set_value("템플릿을 잘못 선택했습니다.").run()
            start = next(
                button for button in app.button if button.label == "조문 작성 시작"
            )
            abandon = next(
                button for button in app.button if button.label == "이 초안 작성 중단"
            )
            self.assertEqual(1, sum(not item.disabled for item in (start, abandon)))
            self.assertTrue(start.disabled)
            self.assertFalse(abandon.disabled)

            next(
                button
                for button in app.button
                if button.label == "작성 중단 입력 지우기"
            ).click().run()
            start = next(
                button for button in app.button if button.label == "조문 작성 시작"
            )
            abandon = next(
                button for button in app.button if button.label == "이 초안 작성 중단"
            )
            confirmation = next(
                item
                for item in app.checkbox
                if item.label == "이 초안을 더 이상 작성하지 않고 중단하겠습니다."
            )
            reason = next(
                area for area in app.text_area if area.label == "작성 중단 사유"
            )

        self.assertFalse(app.exception)
        self.assertEqual(1, sum(not item.disabled for item in (start, abandon)))
        self.assertFalse(start.disabled)
        self.assertTrue(abandon.disabled)
        self.assertFalse(confirmation.value)
        self.assertEqual("", reason.value)

    def test_institution_switch_clears_stale_authoring_project_selection(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=data_dir,
                nav_page=AUTHORING_NAV_LABEL,
                profile_ids=("test-profile", "other-profile"),
            )
            app.run()
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "기관 A 비공개 초안"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()
            stale_project_id = str(app.session_state["authoring_selected_project_id"])

            app.session_state["selected_institution_profile_id"] = "other-profile"
            app.run()

            try:
                selected_after_switch = str(
                    app.session_state["authoring_selected_project_id"] or ""
                )
            except KeyError:
                selected_after_switch = ""
            visible_text = " ".join(
                str(element.value)
                for collection in (app.title, app.subheader, app.caption, app.info)
                for element in collection
            )

        self.assertFalse(app.exception)
        self.assertTrue(stale_project_id)
        self.assertEqual("", selected_after_switch)
        self.assertNotIn("기관 A 비공개 초안", visible_text)

    def test_dirty_authoring_requires_confirmation_before_institution_switch(
        self,
    ) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=Path(tmp) / "data",
                nav_page=AUTHORING_NAV_LABEL,
                profile_ids=("test-profile", "other-profile"),
            )
            app.run()
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "기관 전환 확인 초안"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()
            next(area for area in app.text_area if area.label == "목적").set_value(
                "아직 저장하지 않은 목적"
            ).run()

            app.session_state["institution_switcher"] = "other-profile"
            app.run()
            continue_button = next(
                button for button in app.button if button.label == "기관 전환 계속"
            )
            self.assertEqual(
                "test-profile",
                app.session_state["selected_institution_profile_id"],
            )
            self.assertTrue(continue_button.disabled)
            self.assertTrue(
                any("저장하지 않은 입력" in item.value for item in app.warning)
            )

            next(
                item
                for item in app.checkbox
                if item.label
                == "미저장 입력이 있는 현재 기관을 떠나 다른 기관으로 전환하겠습니다."
            ).set_value(True).run()
            next(
                button for button in app.button if button.label == "기관 전환 계속"
            ).click().run()

            selected_profile_id = str(
                app.session_state["selected_institution_profile_id"]
            )

        self.assertFalse(app.exception)
        self.assertEqual("other-profile", selected_profile_id)

    def test_missing_profile_tenant_uses_default_for_dirty_switch_guard(
        self,
    ) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=Path(tmp) / "data",
                nav_page=AUTHORING_NAV_LABEL,
                profile_ids=("test-profile", "other-profile"),
                tenant_id=None,
            )
            app.run()
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "기본 tenant 기관 전환 초안"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()
            next(area for area in app.text_area if area.label == "목적").set_value(
                "아직 저장하지 않은 목적"
            ).run()

            app.session_state["institution_switcher"] = "other-profile"
            app.run()
            continue_button = next(
                button for button in app.button if button.label == "기관 전환 계속"
            )
            selected_profile_id = str(
                app.session_state["selected_institution_profile_id"]
            )

        self.assertFalse(app.exception)
        self.assertEqual("test-profile", selected_profile_id)
        self.assertTrue(continue_button.disabled)
        self.assertTrue(
            any("저장하지 않은 입력" in item.value for item in app.warning)
        )

    def test_unsaved_project_survives_new_project_and_project_round_trip(
        self,
    ) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=Path(tmp) / "data",
                nav_page=AUTHORING_NAV_LABEL,
                profile_ids=("test-profile", "other-profile"),
            )
            app.run()
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "초안 A"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()
            project_a = str(app.session_state["authoring_selected_project_id"])
            next(area for area in app.text_area if area.label == "목적").set_value(
                "A의 아직 저장하지 않은 목적"
            ).run()

            # Creating another project must not discard the first project's
            # browser-only input, even though its widgets disappear.
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "초안 B"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()
            project_b = str(app.session_state["authoring_selected_project_id"])
            self.assertNotEqual(project_a, project_b)

            picker = next(item for item in app.selectbox if item.label == "초안 선택")
            picker.set_value(project_a).run()
            restored_purpose = next(
                area for area in app.text_area if area.label == "목적"
            )
            self.assertEqual("A의 아직 저장하지 않은 목적", restored_purpose.value)
            self.assertTrue(
                app.session_state[_session_key("dirty", project_a, "metadata")]
            )

            # A deselected dirty project must still guard an institution switch.
            next(
                item for item in app.selectbox if item.label == "초안 선택"
            ).set_value(project_b).run()
            app.session_state["institution_switcher"] = "other-profile"
            app.run()
            continue_button = next(
                button for button in app.button if button.label == "기관 전환 계속"
            )

        self.assertFalse(app.exception)
        self.assertTrue(continue_button.disabled)
        self.assertTrue(
            any("저장하지 않은 입력" in item.value for item in app.warning)
        )

    def test_partial_section_saves_preserve_other_unsaved_sections(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=Path(tmp) / "data",
                nav_page=AUTHORING_NAV_LABEL,
            )
            project_id = _create_drafting_project(app, title="부분 저장 보존 규정")

            purpose = next(area for area in app.text_area if area.label == "목적")
            clause = next(
                area
                for area in app.text_area
                if ":clauses:body:" in str(area.key or "")
            )
            purpose.set_value("부분 저장 뒤 유지될 새 목적")
            clause.set_value("기본정보 저장 뒤에도 남아야 할 미저장 조문").run()
            next(
                button for button in app.button if button.label == "기본정보 저장"
            ).click().run()

            clause = next(
                area
                for area in app.text_area
                if ":clauses:body:" in str(area.key or "")
            )
            self.assertEqual(
                "기본정보 저장 뒤에도 남아야 할 미저장 조문",
                clause.value,
            )
            self.assertTrue(
                app.session_state[_session_key("dirty", project_id, "clauses")]
            )
            next(
                button for button in app.button if button.label == "조문 저장"
            ).click().run()

            clause = next(
                area
                for area in app.text_area
                if ":clauses:body:" in str(area.key or "")
            )
            checklist_item = next(
                item
                for item in app.checkbox
                if ":checklist:completed:" in str(item.key or "")
            )
            clause.set_value("확인 목록보다 먼저 저장할 두 번째 조문")
            checklist_item.set_value(True).run()
            next(
                button for button in app.button if button.label == "조문 저장"
            ).click().run()

            checklist_item = next(
                item
                for item in app.checkbox
                if ":checklist:completed:" in str(item.key or "")
            )

        self.assertFalse(app.exception)
        self.assertTrue(checklist_item.value)
        self.assertTrue(
            app.session_state[_session_key("dirty", project_id, "checklist")]
        )

    def test_beginner_can_review_sheet_and_download_recommended_markdown(self) -> None:
        if AppTest is None:
            self.skipTest("streamlit.testing.v1.AppTest is not available")

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                app,
                enabled=True,
                data_dir=data_dir,
                nav_page=AUTHORING_NAV_LABEL,
            )
            app.run()
            next(item for item in app.text_input if item.label == "규정명").set_value(
                "여비 규정"
            )
            next(
                button for button in app.button if button.label == "초안 공간 만들기"
            ).click().run()

            next(area for area in app.text_area if area.label == "목적").set_value(
                "임직원의 여비 지급 기준을 정한다."
            )
            next(area for area in app.text_area if area.label == "적용 범위").set_value(
                "모든 임직원의 공무 출장에 적용한다."
            )
            next(
                area
                for area in app.text_area
                if area.label == "법적·내부 근거 (한 줄에 하나)"
            ).set_value("이사회 운영 규정")
            next(item for item in app.text_input if item.label == "담당부서").set_value(
                "경영지원부"
            )
            next(
                item for item in app.date_input if item.label == "시행 예정일"
            ).set_value(date(2026, 10, 1))
            next(
                button for button in app.button if button.label == "기본정보 저장"
            ).click().run()
            next(
                button for button in app.button if button.label == "조문 작성 시작"
            ).click().run()

            clause_areas = [
                area
                for area in app.text_area
                if ":clauses:body:" in str(area.key or "")
            ]
            self.assertTrue(clause_areas)
            for area in clause_areas:
                area.set_value(
                    "담당부서는 기준에 따라 업무를 처리하고 결과를 기록한다."
                )
            next(
                button for button in app.button if button.label == "조문 저장"
            ).click().run()

            checklist = [
                item
                for item in app.checkbox
                if ":checklist:completed:" in str(item.key or "")
            ]
            self.assertTrue(checklist)
            checklist_labels = [item.label for item in checklist]
            for item in checklist:
                item.set_value(True)
            next(
                button for button in app.button if button.label == "확인 목록 저장"
            ).click().run()

            next(
                button for button in app.button if button.label == "작성 검사"
            ).click().run()
            next(
                button for button in app.button if button.label == "내용 확인 요청"
            ).click().run()

            review_text = " ".join(
                str(element.value)
                for collection in (
                    app.markdown,
                    app.text,
                    app.caption,
                    app.success,
                    app.warning,
                )
                for element in collection
            )
            self.assertIn("검토 시트 (읽기 전용)", review_text)
            self.assertIn("개정본:", review_text)
            self.assertRegex(
                review_text,
                r"내용 확인값\(SHA-256\): [0-9a-f]{64}",
            )
            self.assertIn("임직원의 여비 지급 기준을 정한다.", review_text)
            self.assertIn("이사회 운영 규정", review_text)
            self.assertIn("담당부서는 기준에 따라 업무를 처리", review_text)
            for clause_label in (
                "제1장(총칙)",
                "제1조(목적)",
                "제2조(적용 범위)",
                "제3조(용어의 정의)",
                "제2장(운영)",
                "제4조(책임과 역할)",
                "제5조(업무 절차)",
                "제6조(기록과 관리)",
                "부칙(시행일)",
            ):
                self.assertIn(clause_label, review_text)
            self.assertEqual(
                2,
                review_text.count("구조 제목·본문 입력 대상 아님"),
            )
            for checklist_label in checklist_labels:
                self.assertIn(f"완료** · {checklist_label}", review_text)
            self.assertIn("최신 작성 검사", review_text)
            next(
                item
                for item in app.checkbox
                if item.label == "연습용 자체 확인으로만 내용을 동결합니다."
            ).set_value(True).run()
            next(
                area for area in app.text_area if area.label == "자체 확인 사유"
            ).set_value("로컬 교육용 흐름을 혼자 연습한다.").run()
            next(
                button
                for button in app.button
                if button.label == "연습용으로 내용 동결"
            ).click().run()
            project_id = str(app.session_state["authoring_selected_project_id"])

            # Start a fresh browser session at the frozen draft. This also
            # verifies that the isolated project is durable across sessions.
            export_app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                export_app,
                enabled=True,
                data_dir=data_dir,
                nav_page=AUTHORING_NAV_LABEL,
            )
            export_app.session_state["authoring_selected_project_id"] = project_id
            export_app.run()
            export_format = next(
                item for item in export_app.radio if item.label == "파일 형식"
            )
            self.assertEqual("markdown", export_format.value)
            next(
                button
                for button in export_app.button
                if button.label == "초안 패키지 만들기"
            ).click().run()

            artifact = export_app.session_state[
                _session_key("export", project_id)
            ]

            download_app = AppTest.from_file(
                str(REPO_ROOT / "frontend" / "streamlit_app.py"),
                default_timeout=30,
            )
            _seed_app(
                download_app,
                enabled=True,
                data_dir=data_dir,
                nav_page=AUTHORING_NAV_LABEL,
            )
            download_app.session_state["authoring_selected_project_id"] = project_id
            download_app.run()
            next(
                button
                for button in download_app.button
                if button.label == "저장된 초안 패키지 다시 불러오기"
            ).click().run()
            restored_artifact = download_app.session_state[
                _session_key("export", project_id)
            ]

        self.assertFalse(export_app.exception)
        self.assertFalse(download_app.exception)
        self.assertIsInstance(artifact["content"], bytes)
        self.assertEqual(artifact["content"], restored_artifact["content"])
        self.assertTrue(str(artifact["filename"]).endswith(".md"))
        markdown_text = artifact["content"].decode("utf-8")
        self.assertIn(OFFICIAL_BOUNDARY_NOTICE, markdown_text)
        self.assertIn("연습용 표시: 예", markdown_text)
        self.assertFalse((data_dir / "vectors").exists())
        self.assertFalse((data_dir / "approval_journal.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
