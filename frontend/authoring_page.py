from __future__ import annotations

from collections.abc import Sequence
from datetime import date
import hashlib
from typing import Any, Literal, cast

import streamlit as st

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant
from app.schemas.authoring import (
    OFFICIAL_BOUNDARY_NOTICE,
    AuthoringExportRequest,
    AuthoringLintFinding,
    AuthoringLintReport,
    AuthoringMode,
    AuthoringProject,
    AuthoringProjectCreateRequest,
    AuthoringProjectFreezeRequest,
    AuthoringProjectStatus,
    AuthoringProjectUpdateRequest,
    AuthoringTransitionRequest,
    BeginnerChecklistItem,
    ClauseDraft,
    DraftNodeType,
)
from app.services.authoring_safety_service import (
    REDACTED_AUTHORING_REASON,
    sanitize_authoring_reason,
)
from app.services.authoring_template_service import AuthoringTemplateService


AUTHORING_NAV_LABEL = "✍️ 규정 새로 작성"
AUTHORING_ACTOR = "local-authoring-operator"
AUTHORING_SELECTED_PROJECT_KEY = "authoring_selected_project_id"
AUTHORING_PENDING_PROJECT_KEY = "authoring_pending_project_id"
AUTHORING_SELECTED_PROFILE_KEY = "authoring_selected_profile_id"
AUTHORING_EXPORT_STATE_PREFIX = "authoring_export_artifact"
AUTHORING_CONFLICT_PROJECT_KEY = "authoring_conflict_project_id"
AUTHORING_SELECTED_SCOPE_KEY = "authoring_selected_project_scope"
AUTHORING_SESSION_PREFIX = "authoring-v2"
AUTHORING_FLASH_MESSAGE_KEY = f"{AUTHORING_SESSION_PREFIX}-flash-message"
REDACTED_REVIEW_REASON = REDACTED_AUTHORING_REASON
AUTHORING_EDITOR_SECTIONS = ("metadata", "clauses", "checklist", "actions")

MODE_LABELS = {
    AuthoringMode.ENACTMENT: "신규 제정",
    AuthoringMode.PARTIAL_REVISION: "일부 개정",
    AuthoringMode.FULL_REVISION: "전부 개정",
}
STATUS_LABELS = {
    AuthoringProjectStatus.PLANNING: "기본정보 작성",
    AuthoringProjectStatus.DRAFTING: "조문 작성",
    AuthoringProjectStatus.REVIEW_REQUESTED: "내용 확인 대기",
    AuthoringProjectStatus.CHANGES_REQUESTED: "수정 필요",
    AuthoringProjectStatus.CONTENT_FROZEN: "내용 동결",
    AuthoringProjectStatus.EXPORTED: "초안 패키지 생성 완료",
    AuthoringProjectStatus.ABANDONED: "작성 중단",
}
NODE_TYPE_LABELS = {
    DraftNodeType.CHAPTER: "장",
    DraftNodeType.SECTION: "절",
    DraftNodeType.ARTICLE: "조",
    DraftNodeType.PARAGRAPH: "항",
    DraftNodeType.ITEM: "호",
    DraftNodeType.SUPPLEMENTARY: "부칙",
}


def authoring_enabled(settings: object) -> bool:
    """Return the fail-closed navigation decision used by the local UI."""

    return bool(getattr(settings, "enable_regulation_authoring", False))


def authoring_settings_for_tenant(settings: Settings, tenant_id: str) -> Settings:
    """Apply the same physical tenant storage boundary as the authoring API."""

    return settings_for_tenant(settings, tenant_id)


def authoring_profile_has_unsaved_state(tenant_id: str, profile_id: str) -> bool:
    """Tell the shell when any draft in an institution needs confirmation."""

    _capture_dirty_widget_buffers()
    normalized_tenant_id = str(tenant_id or "").strip().lower()
    normalized_profile_id = str(profile_id or "").strip().lower()
    conflict_scope = str(
        st.session_state.get(AUTHORING_CONFLICT_PROJECT_KEY) or ""
    )
    for key, value in st.session_state.items():
        key_text = str(key)
        if not key_text.startswith(f"{AUTHORING_SESSION_PREFIX}-dirty:") or not bool(
            value
        ):
            continue
        parts = key_text.split(":", 4)
        if len(parts) != 5 or parts[4] not in AUTHORING_EDITOR_SECTIONS:
            continue
        _, project_tenant_id, project_profile_id, _, _ = parts
        if (
            project_tenant_id == normalized_tenant_id
            and project_profile_id == normalized_profile_id
        ):
            return True
    if conflict_scope:
        return conflict_scope.startswith(
            f"{normalized_tenant_id}:{normalized_profile_id}:"
        )
    return False


def next_authoring_action(
    project: AuthoringProject,
    *,
    dirty_section: str | None = None,
    conflict_pending: bool = False,
    report: AuthoringLintReport | None = None,
) -> str:
    """Give a beginner one primary next action or decision for the current state."""

    if conflict_pending:
        return "미저장 문장을 복사한 뒤 '최신 개정 불러오기'로 충돌을 해결하세요."
    if project.status == AuthoringProjectStatus.PLANNING:
        if dirty_section == "metadata":
            return "바꾼 기본정보를 '기본정보 저장'으로 먼저 저장하세요."
        if dirty_section == "actions":
            return (
                "'잘못 만든 초안 작성 중단하기'에서 중단을 완료하거나 "
                "'작성 중단 입력 지우기'를 누르세요."
            )
        if metadata_ready_for_drafting(project):
            return "저장된 기본정보를 확인한 뒤 '조문 작성 시작'을 누르세요."
        return "기본정보를 먼저 저장한 뒤 '조문 작성 시작'을 누르세요."
    if project.status == AuthoringProjectStatus.DRAFTING:
        dirty_actions = {
            "metadata": "바꾼 기본정보를 먼저 저장하세요.",
            "clauses": "바꾼 조문을 먼저 저장하세요.",
            "checklist": "바꾼 확인 목록을 먼저 저장하세요.",
        }
        if dirty_section in dirty_actions:
            return dirty_actions[dirty_section]
        if report is None or report.revision != project.revision:
            return "빈 필수 조문을 채운 뒤 '작성 검사'로 오류 위치를 확인하세요."
        if report.blocking_findings:
            return "작성 검사의 오류 위치와 수정 방법을 따라 고친 뒤 다시 검사하세요."
        if not project.checklist or not all(
            item.completed for item in project.checklist
        ):
            return "작성자 확인 목록을 실제로 확인해 모두 완료하고 저장하세요."
        return "현재 검사와 확인 목록을 확인한 뒤 '내용 확인 요청'을 누르세요."
    if project.status == AuthoringProjectStatus.CHANGES_REQUESTED:
        return "수정 요청을 확인하고 '조문 다시 작성'을 누르세요."
    if project.status == AuthoringProjectStatus.REVIEW_REQUESTED:
        return "내용을 확인한 뒤 수정을 요청하거나 연습용으로 동결하세요."
    if project.status == AuthoringProjectStatus.CONTENT_FROZEN:
        return "권장 형식인 Markdown을 고르거나 JSON을 선택해 초안 패키지를 만드세요."
    if project.status == AuthoringProjectStatus.EXPORTED:
        return (
            "초안 패키지를 내려받아 규정·법무 담당자와 기관 결재 절차에서 별도로 검토하세요."
        )
    return "필요하면 새 작성 프로젝트를 만드세요."


def editable_clauses_in_document_order(project: AuthoringProject) -> list[ClauseDraft]:
    """Keep the template's intentional reading order in the beginner editor."""

    return [
        clause
        for clause in project.clauses
        if clause.node_type not in {DraftNodeType.CHAPTER, DraftNodeType.SECTION}
    ]


def metadata_ready_for_drafting(project: AuthoringProject) -> bool:
    """Require saved project metadata before exposing the drafting transition."""

    ready = bool(
        project.title.strip()
        and project.purpose.strip()
        and project.scope.strip()
        and project.legal_bases
        and project.responsible_department.strip()
        and project.planned_effective_date is not None
    )
    if project.authoring_mode != AuthoringMode.ENACTMENT:
        ready = bool(
            ready
            and (project.revision_reason or "").strip()
            and (project.predecessor_reference or "").strip()
        )
    return ready


def review_ready_for_training_freeze(project: AuthoringProject) -> bool:
    """Require a current clean report and a fully completed saved checklist."""

    report = project.last_lint_report
    return bool(
        report is not None
        and report.revision == project.revision
        and not report.blocking_findings
        and project.checklist
        and all(item.completed for item in project.checklist)
    )


def _editor_dirty_key(project: AuthoringProject, section: str) -> str:
    return _project_state_key("dirty", project, section)


def _editor_base_revision_key(project: AuthoringProject, section: str) -> str:
    return _project_state_key("base-revision", project, section)


def _editor_buffer_key(project: AuthoringProject, section: str) -> str:
    return _project_state_key("buffer", project, section)


def _project_scope(project: AuthoringProject) -> str:
    return _scope_value(project.tenant_id, project.profile_id, project.project_id)


def _scope_value(tenant_id: object, profile_id: object, project_id: object) -> str:
    return f"{tenant_id}:{profile_id}:{project_id}"


def _scoped_state_key(
    kind: str,
    tenant_id: object,
    profile_id: object,
    project_id: object,
    *parts: object,
) -> str:
    suffix = "".join(f":{part}" for part in parts)
    scope = _scope_value(tenant_id, profile_id, project_id)
    return f"{AUTHORING_SESSION_PREFIX}-{kind}:{scope}{suffix}"


def _project_state_key(
    kind: str,
    project: AuthoringProject,
    *parts: object,
) -> str:
    return _scoped_state_key(
        kind,
        project.tenant_id,
        project.profile_id,
        project.project_id,
        *parts,
    )


def _editor_buffer(
    project: AuthoringProject,
    section: str,
    defaults: dict[str, object],
) -> dict[str, object]:
    key = _editor_buffer_key(project, section)
    stored = st.session_state.get(key)
    if not _editor_is_dirty(project, section) or not isinstance(stored, dict):
        stored = dict(defaults)
        st.session_state[key] = stored
    return dict(stored)


def _set_editor_buffer(
    project: AuthoringProject,
    section: str,
    values: dict[str, object],
) -> None:
    st.session_state[_editor_buffer_key(project, section)] = dict(values)


def _capture_dirty_widget_buffers() -> None:
    """Copy widget values to non-widget state before Streamlit may remove them."""

    for key, value in list(st.session_state.items()):
        key_text = str(key)
        if not key_text.startswith(
            f"{AUTHORING_SESSION_PREFIX}-editor:"
        ) or key_text.endswith(":loaded-revision"):
            continue
        parts = key_text.split(":", 5)
        if len(parts) != 6:
            continue
        _, tenant_id, profile_id, project_id, section, field_name = parts
        if section not in AUTHORING_EDITOR_SECTIONS or not bool(
            st.session_state.get(
                f"{AUTHORING_SESSION_PREFIX}-dirty:{tenant_id}:{profile_id}:"
                f"{project_id}:{section}"
            )
        ):
            continue
        buffer_key = (
            f"{AUTHORING_SESSION_PREFIX}-buffer:{tenant_id}:{profile_id}:"
            f"{project_id}:{section}"
        )
        stored = st.session_state.get(buffer_key)
        buffer = dict(stored) if isinstance(stored, dict) else {}
        buffer[field_name] = value
        st.session_state[buffer_key] = buffer


def _editor_is_dirty(project: AuthoringProject, section: str) -> bool:
    return bool(st.session_state.get(_editor_dirty_key(project, section)))


def _project_has_unsaved_changes(project: AuthoringProject) -> bool:
    return any(
        _editor_is_dirty(project, section) for section in AUTHORING_EDITOR_SECTIONS
    )


def _first_dirty_section(project: AuthoringProject) -> str | None:
    return next(
        (
            section
            for section in AUTHORING_EDITOR_SECTIONS
            if _editor_is_dirty(project, section)
        ),
        None,
    )


def _current_lint_report(project: AuthoringProject) -> AuthoringLintReport | None:
    report = project.last_lint_report
    stored_report = st.session_state.get(_project_state_key("lint", project))
    if isinstance(stored_report, dict):
        report = AuthoringLintReport.model_validate(stored_report)
    return report if report is not None and report.revision == project.revision else None


def _project_has_unresolved_conflict(project: AuthoringProject) -> bool:
    return str(st.session_state.get(AUTHORING_CONFLICT_PROJECT_KEY) or "") == (
        _project_scope(project)
    )


def _mark_editor_dirty(dirty_key: str) -> None:
    st.session_state[dirty_key] = True


def _editor_base_revision(project: AuthoringProject, section: str) -> int:
    key = _editor_base_revision_key(project, section)
    value = st.session_state.get(key)
    if type(value) is not int:
        st.session_state[key] = project.revision
        return project.revision
    return value


def _sync_editor_revision_state(project: AuthoringProject) -> None:
    """Never rebase dirty browser input onto a newer server revision."""

    for section in AUTHORING_EDITOR_SECTIONS:
        key = _editor_base_revision_key(project, section)
        base_revision = st.session_state.get(key)
        if type(base_revision) is not int:
            st.session_state[key] = project.revision
            continue
        if _editor_is_dirty(project, section):
            if base_revision != project.revision:
                st.session_state[AUTHORING_CONFLICT_PROJECT_KEY] = _project_scope(
                    project
                )
        else:
            st.session_state[key] = project.revision


def _editor_widget_key(
    project: AuthoringProject,
    section: str,
    field_name: str,
    value: object,
) -> str:
    """Keep unsaved browser values while syncing clean widgets to a new revision."""

    key = _project_state_key("editor", project, section, field_name)
    revision_key = f"{key}:loaded-revision"
    if key not in st.session_state or (
        not _editor_is_dirty(project, section)
        and st.session_state.get(revision_key) != project.revision
    ):
        st.session_state[key] = value
        st.session_state[revision_key] = project.revision
    return key


def render_authoring_page(
    *,
    settings: object,
    profile_id: str,
    institution_name: str,
    tenant_id: str,
    service: Any | None = None,
) -> None:
    """Render the isolated local beginner authoring workspace."""

    if not authoring_enabled(settings):
        st.error("규정 작성 기능이 꺼져 있습니다.")
        st.caption("관리자가 ENABLE_REGULATION_AUTHORING 설정을 확인해야 합니다.")
        st.stop()
    current_profile_id = str(profile_id or "").strip()
    if not current_profile_id:
        st.error("작성할 규정의 기관을 먼저 선택하세요.")
        st.stop()

    _discard_legacy_authoring_session_state()
    _capture_dirty_widget_buffers()
    _reset_selection_for_profile(current_profile_id)

    if service is None:
        from app.services.authoring_service import AuthoringService

        service = AuthoringService(
            authoring_settings_for_tenant(cast(Settings, settings), tenant_id)
        )

    _render_boundary_notice()
    st.title(AUTHORING_NAV_LABEL)
    _render_flash_message()
    st.caption(
        f"현재 기관: {institution_name or profile_id} · 외부 AI를 호출하지 않는 로컬 초안 작성 공간"
    )
    st.info(
        "이 로컬 화면은 **1인 연습용**입니다. 여기서 내보내는 모든 출력은 "
        "연습용으로 표시되며 공식 승인이 아닙니다. 실무용 작성자·확인자 2인 "
        "흐름은 인증·권한이 적용된 보호 모드 API 운영 경로를 사용하세요."
    )
    st.info("👉 지금 할 일: 이어서 쓸 초안을 고르거나 새 초안을 만드세요.")

    try:
        projects = list(
            service.list_projects(tenant_id=tenant_id, profile_id=current_profile_id)
        )
    except Exception as exc:  # UI must not expose draft text or local paths.
        _render_action_error(exc)
        return

    _render_project_picker(
        projects,
        tenant_id=tenant_id,
        profile_id=current_profile_id,
    )
    has_active_projects = any(
        project.status != AuthoringProjectStatus.ABANDONED for project in projects
    )
    with st.expander("새 초안 만들기", expanded=not has_active_projects):
        _render_create_form(
            service=service,
            tenant_id=tenant_id,
            profile_id=current_profile_id,
        )

    project_id = str(st.session_state.get(AUTHORING_SELECTED_PROJECT_KEY) or "")
    if not project_id:
        return
    try:
        project = service.get_project(
            project_id,
            tenant_id=tenant_id,
            profile_id=current_profile_id,
        )
    except Exception as exc:
        st.session_state.pop(AUTHORING_SELECTED_PROJECT_KEY, None)
        _render_action_error(exc)
        return

    st.session_state[AUTHORING_SELECTED_SCOPE_KEY] = _project_scope(project)

    st.divider()
    _sync_editor_revision_state(project)
    conflict_rendered = _project_has_unresolved_conflict(project)
    _render_conflict_recovery(project)
    _render_project(
        project,
        service=service,
        tenant_id=tenant_id,
        profile_id=current_profile_id,
    )
    if not conflict_rendered and _project_has_unresolved_conflict(project):
        _render_conflict_recovery(project)


def _reset_selection_for_profile(profile_id: str) -> None:
    previous_profile_id = str(
        st.session_state.get(AUTHORING_SELECTED_PROFILE_KEY) or ""
    )
    if previous_profile_id and previous_profile_id != profile_id:
        st.session_state.pop(AUTHORING_SELECTED_PROJECT_KEY, None)
        st.session_state.pop(AUTHORING_PENDING_PROJECT_KEY, None)
        st.session_state.pop(AUTHORING_CONFLICT_PROJECT_KEY, None)
        st.session_state.pop(AUTHORING_SELECTED_SCOPE_KEY, None)
    st.session_state[AUTHORING_SELECTED_PROFILE_KEY] = profile_id


def _discard_legacy_authoring_session_state() -> None:
    """Drop pre-scope browser keys instead of reusing ambiguous draft data."""

    legacy_prefixes = (
        "authoring-editor:",
        "authoring-dirty:",
        "authoring-base-revision:",
        "authoring-buffer:",
        "authoring-lint-",
        f"{AUTHORING_EXPORT_STATE_PREFIX}:",
        "authoring-project-profile:",
    )
    for key in list(st.session_state):
        if any(str(key).startswith(prefix) for prefix in legacy_prefixes):
            st.session_state.pop(key, None)
    conflict_scope = str(
        st.session_state.get(AUTHORING_CONFLICT_PROJECT_KEY) or ""
    )
    if conflict_scope and conflict_scope.count(":") != 2:
        st.session_state.pop(AUTHORING_CONFLICT_PROJECT_KEY, None)


def _render_boundary_notice() -> None:
    st.warning(
        f"⚠️ **{OFFICIAL_BOUNDARY_NOTICE}** · 여기서의 '내용 동결'은 편집 기준본을 고정하는 것일 뿐, "
        "법적 검토·기관 결재·RAG 승인·색인·MCP 공개가 아닙니다."
    )


def _render_flash_message() -> None:
    """Show a successful action once, after Streamlit completes its rerun."""

    message = st.session_state.pop(AUTHORING_FLASH_MESSAGE_KEY, "")
    if message:
        st.success(str(message))


def _render_conflict_recovery(project: AuthoringProject) -> None:
    if not _project_has_unresolved_conflict(project):
        return
    st.error(
        "다른 화면에서 이 초안이 먼저 저장되었습니다. 지금 보이는 미저장 입력은 "
        "브라우저에 임시 보존되어 있지만 그대로 저장할 수 없습니다."
    )
    st.warning(
        "필요한 문장은 먼저 복사해 두세요. 아래 확인 후 최신 개정을 불러오면 "
        "현재 미저장 입력을 버리고 서버의 최신 내용으로 바꿉니다."
    )
    confirmed = st.checkbox(
        "현재 미저장 입력을 필요한 곳에 복사했고, 최신 개정을 불러오겠습니다.",
        key=_project_state_key("control", project, "conflict-confirm"),
    )
    if st.button(
        "최신 개정 불러오기",
        type="primary",
        disabled=not confirmed,
        key=_project_state_key("control", project, "conflict-reload"),
    ):
        _clear_project_editor_state(project)
        st.session_state.pop(AUTHORING_CONFLICT_PROJECT_KEY, None)
        st.rerun()


def _clear_project_editor_state(project: AuthoringProject) -> None:
    scope = _project_scope(project)
    prefixes = (
        f"{AUTHORING_SESSION_PREFIX}-editor:{scope}:",
        f"{AUTHORING_SESSION_PREFIX}-dirty:{scope}:",
        f"{AUTHORING_SESSION_PREFIX}-base-revision:{scope}:",
        f"{AUTHORING_SESSION_PREFIX}-buffer:{scope}:",
        f"{AUTHORING_SESSION_PREFIX}-lint:{scope}",
        f"{AUTHORING_SESSION_PREFIX}-control:{scope}:",
    )
    for key in list(st.session_state):
        if any(str(key).startswith(prefix) for prefix in prefixes):
            st.session_state.pop(key, None)


def _clear_project_editor_section_state(
    project: AuthoringProject,
    section: str,
) -> None:
    scope = _project_scope(project)
    prefixes = (
        f"{AUTHORING_SESSION_PREFIX}-editor:{scope}:{section}:",
        f"{AUTHORING_SESSION_PREFIX}-dirty:{scope}:{section}",
        f"{AUTHORING_SESSION_PREFIX}-base-revision:{scope}:{section}",
        f"{AUTHORING_SESSION_PREFIX}-buffer:{scope}:{section}",
    )
    for key in list(st.session_state):
        if any(str(key).startswith(prefix) for prefix in prefixes):
            st.session_state.pop(key, None)


def _clear_action_inputs(project: AuthoringProject, *control_names: str) -> None:
    """Clear one mutually exclusive decision before Streamlit renders again."""

    _clear_project_editor_section_state(project, "actions")
    for control_name in control_names:
        st.session_state.pop(
            _project_state_key("control", project, control_name),
            None,
        )


def _render_project_picker(
    projects: Sequence[Any],
    *,
    tenant_id: str,
    profile_id: str,
) -> None:
    if not projects:
        st.session_state.pop(AUTHORING_SELECTED_PROJECT_KEY, None)
        st.session_state.pop(AUTHORING_PENDING_PROJECT_KEY, None)
        st.caption("이 기관에서 작성 중인 초안이 아직 없습니다.")
        return
    active_projects = [
        project
        for project in projects
        if project.status != AuthoringProjectStatus.ABANDONED
    ]
    abandoned_projects = [
        project
        for project in projects
        if project.status == AuthoringProjectStatus.ABANDONED
    ]
    show_abandoned = False
    if abandoned_projects:
        show_abandoned = st.checkbox(
            f"중단 초안 보기 ({len(abandoned_projects)}개)",
            key="authoring-show-abandoned-projects",
            help="작성을 중단한 초안은 편집할 수 없습니다.",
        )
    visible_projects = active_projects + (abandoned_projects if show_abandoned else [])
    if not visible_projects:
        st.session_state.pop(AUTHORING_SELECTED_PROJECT_KEY, None)
        st.session_state.pop(AUTHORING_PENDING_PROJECT_KEY, None)
        st.caption("작성 중인 초안이 없습니다. 필요하면 새 초안을 만드세요.")
        return
    options = [str(project.project_id) for project in visible_projects]
    pending = str(st.session_state.pop(AUTHORING_PENDING_PROJECT_KEY, "") or "")
    if pending in options:
        st.session_state[AUTHORING_SELECTED_PROJECT_KEY] = pending
    selected = str(st.session_state.get(AUTHORING_SELECTED_PROJECT_KEY) or "")
    if selected not in options:
        st.session_state[AUTHORING_SELECTED_PROJECT_KEY] = options[0]
    project_map = {str(project.project_id): project for project in visible_projects}
    if any(
        bool(
            st.session_state.get(
                _scoped_state_key(
                    "dirty",
                    tenant_id,
                    profile_id,
                    project.project_id,
                    section,
                )
            )
        )
        for project in visible_projects
        for section in AUTHORING_EDITOR_SECTIONS
    ):
        st.warning(
            "저장하지 않은 값이 있는 초안이 있습니다. 다른 초안으로 이동해도 "
            "브라우저에는 임시 보존되지만, 해당 초안으로 돌아가 저장해야 반영됩니다."
        )
    st.selectbox(
        "초안 선택",
        options,
        key=AUTHORING_SELECTED_PROJECT_KEY,
        format_func=lambda value: _project_label(project_map[value]),
    )


def _project_label(project: AuthoringProject) -> str:
    status = STATUS_LABELS.get(project.status, str(project.status))
    mode = MODE_LABELS.get(project.authoring_mode, str(project.authoring_mode))
    return f"{project.title} · {mode} · {status} · {project.revision}판"


def _render_create_form(*, service: Any, tenant_id: str, profile_id: str) -> None:
    templates = AuthoringTemplateService().list_templates()
    template_map = {template.template_id: template for template in templates}
    with st.form("authoring-create-form", clear_on_submit=False):
        mode = st.radio(
            "작성 방식",
            list(AuthoringMode),
            format_func=lambda value: MODE_LABELS[value],
            horizontal=True,
        )
        template_id = st.selectbox(
            "한국어 템플릿",
            list(template_map),
            format_func=lambda value: template_map[value].name_ko,
        )
        template = template_map[template_id]
        st.caption(f"{template.description_ko} {template.recommended_for_ko}")
        title = st.text_input("규정명", placeholder="예: 임직원 교육훈련 규정")
        submitted = st.form_submit_button(
            "초안 공간 만들기",
            type="primary",
            disabled=not title.strip(),
        )
    if not submitted:
        return
    try:
        project = service.create_project(
            AuthoringProjectCreateRequest(
                profile_id=profile_id,
                authoring_mode=cast(AuthoringMode, mode),
                template_id=template_id,
                title=title,
            ),
            tenant_id=tenant_id,
            actor=AUTHORING_ACTOR,
        )
    except Exception as exc:
        _render_action_error(exc)
        return
    # The selector may already exist when a second project is created. Defer
    # its value change until the next rerun to respect Streamlit's widget-state
    # mutation rule.
    st.session_state[AUTHORING_PENDING_PROJECT_KEY] = str(project.project_id)
    st.success("초안 공간을 만들었습니다. 다음은 기본정보를 채울 차례입니다.")
    st.rerun()


def _render_project(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    status_label = STATUS_LABELS.get(project.status, str(project.status))
    st.subheader(project.title)
    st.caption(
        f"{MODE_LABELS.get(project.authoring_mode, project.authoring_mode)} · {status_label} · {project.revision}판 · {OFFICIAL_BOUNDARY_NOTICE}"
    )
    if project.training_only:
        st.warning(
            "이 프로젝트와 출력물은 **연습용·공식 사용 금지**입니다. "
            "시스템은 이 초안을 계속 연습용으로 표시합니다."
        )
    st.info(
        "👉 지금 할 일: "
        + next_authoring_action(
            project,
            dirty_section=_first_dirty_section(project),
            conflict_pending=_project_has_unresolved_conflict(project),
            report=_current_lint_report(project),
        )
    )

    if project.status in {
        AuthoringProjectStatus.PLANNING,
        AuthoringProjectStatus.DRAFTING,
    }:
        _render_metadata_editor(
            project,
            service=service,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )

    if project.status == AuthoringProjectStatus.PLANNING:
        _render_start_drafting(
            project,
            service=service,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        _render_abandon_planning(
            project,
            service=service,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
    elif project.status == AuthoringProjectStatus.DRAFTING:
        _render_clause_editor(
            project, service=service, tenant_id=tenant_id, profile_id=profile_id
        )
        _render_checklist_editor(
            project, service=service, tenant_id=tenant_id, profile_id=profile_id
        )
        _render_lint_and_review(
            project, service=service, tenant_id=tenant_id, profile_id=profile_id
        )
    elif project.status == AuthoringProjectStatus.CHANGES_REQUESTED:
        _render_resume_drafting(
            project, service=service, tenant_id=tenant_id, profile_id=profile_id
        )
    elif project.status == AuthoringProjectStatus.REVIEW_REQUESTED:
        _render_review_actions(
            project, service=service, tenant_id=tenant_id, profile_id=profile_id
        )
    elif project.status == AuthoringProjectStatus.CONTENT_FROZEN:
        _render_export_actions(
            project, service=service, tenant_id=tenant_id, profile_id=profile_id
        )
        _render_reopen_action(
            project, service=service, tenant_id=tenant_id, profile_id=profile_id
        )
    elif project.status == AuthoringProjectStatus.EXPORTED:
        _render_download_if_available(
            project,
            service=service,
            tenant_id=tenant_id,
            profile_id=profile_id,
        )
        _render_reopen_action(
            project, service=service, tenant_id=tenant_id, profile_id=profile_id
        )
    elif project.status == AuthoringProjectStatus.ABANDONED:
        st.warning(
            "이 초안은 작성 중단된 종료 상태라 다시 편집할 수 없습니다. "
            "위의 '새 초안 만들기'에서 올바른 템플릿과 작성 방식을 골라 시작하세요."
        )

    _render_boundary_notice()


def _render_metadata_editor(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    with st.expander(
        "1. 기본정보", expanded=project.status == AuthoringProjectStatus.PLANNING
    ):
        dirty_key = _editor_dirty_key(project, "metadata")
        buffer = _editor_buffer(
            project,
            "metadata",
            {
                "title": project.title,
                "purpose": project.purpose,
                "scope": project.scope,
                "legal_bases": "\n".join(project.legal_bases),
                "responsible_department": project.responsible_department,
                "planned_effective_date": project.planned_effective_date,
                "revision_reason": project.revision_reason or "",
                "predecessor_reference": project.predecessor_reference or "",
            },
        )
        title = st.text_input(
            "규정명",
            key=_editor_widget_key(
                project,
                "metadata",
                "title",
                str(buffer.get("title") or ""),
            ),
            on_change=_mark_editor_dirty,
            args=(dirty_key,),
        )
        purpose = st.text_area(
            "목적",
            key=_editor_widget_key(
                project,
                "metadata",
                "purpose",
                str(buffer.get("purpose") or ""),
            ),
            help="이 규정으로 해결할 문제와 기대 결과를 한두 문장으로 적으세요.",
            on_change=_mark_editor_dirty,
            args=(dirty_key,),
        )
        scope = st.text_area(
            "적용 범위",
            key=_editor_widget_key(
                project,
                "metadata",
                "scope",
                str(buffer.get("scope") or ""),
            ),
            help="누구의 어떤 업무에 적용되며 예외가 있는지 적으세요.",
            on_change=_mark_editor_dirty,
            args=(dirty_key,),
        )
        legal_bases = st.text_area(
            "법적·내부 근거 (한 줄에 하나)",
            key=_editor_widget_key(
                project,
                "metadata",
                "legal_bases",
                str(buffer.get("legal_bases") or ""),
            ),
            help="적합성을 자동 보장하지 않습니다. 관련 법령·정관·상위 규정을 담당자가 확인하세요.",
            on_change=_mark_editor_dirty,
            args=(dirty_key,),
        )
        department = st.text_input(
            "담당부서",
            key=_editor_widget_key(
                project,
                "metadata",
                "responsible_department",
                str(buffer.get("responsible_department") or ""),
            ),
            on_change=_mark_editor_dirty,
            args=(dirty_key,),
        )
        effective_date = st.date_input(
            "시행 예정일",
            key=_editor_widget_key(
                project,
                "metadata",
                "planned_effective_date",
                buffer.get("planned_effective_date"),
            ),
            on_change=_mark_editor_dirty,
            args=(dirty_key,),
        )
        revision_reason = str(buffer.get("revision_reason") or "")
        predecessor_reference = str(buffer.get("predecessor_reference") or "")
        if project.authoring_mode != AuthoringMode.ENACTMENT:
            predecessor_reference = st.text_input(
                "개정 대상 규정",
                key=_editor_widget_key(
                    project,
                    "metadata",
                    "predecessor_reference",
                    predecessor_reference,
                ),
                placeholder="기존 규정명과 버전 또는 시행일",
                on_change=_mark_editor_dirty,
                args=(dirty_key,),
            )
            revision_reason = st.text_area(
                "개정 사유",
                key=_editor_widget_key(
                    project,
                    "metadata",
                    "revision_reason",
                    revision_reason,
                ),
                help="무엇이 왜 달라져야 하는지 적으세요.",
                on_change=_mark_editor_dirty,
                args=(dirty_key,),
            )
        metadata_dirty = bool(
            title.strip() != project.title
            or purpose.strip() != project.purpose
            or scope.strip() != project.scope
            or _nonempty_lines(legal_bases) != project.legal_bases
            or department.strip() != project.responsible_department
            or (effective_date if isinstance(effective_date, date) else None)
            != project.planned_effective_date
            or (revision_reason or "").strip() != (project.revision_reason or "")
            or (predecessor_reference or "").strip()
            != (project.predecessor_reference or "")
        )
        conflict_pending = _project_has_unresolved_conflict(project)
        st.session_state[dirty_key] = metadata_dirty
        _set_editor_buffer(
            project,
            "metadata",
            {
                "title": title,
                "purpose": purpose,
                "scope": scope,
                "legal_bases": legal_bases,
                "responsible_department": department,
                "planned_effective_date": effective_date,
                "revision_reason": revision_reason,
                "predecessor_reference": predecessor_reference,
            },
        )
        if metadata_dirty:
            st.warning(
                "저장하지 않은 기본정보가 있습니다. '기본정보 저장'을 먼저 누르세요."
            )
        submitted = st.button(
            "기본정보 저장",
            type=(
                "primary"
                if metadata_dirty and _first_dirty_section(project) == "metadata"
                else "secondary"
            ),
            key=_project_state_key("control", project, "save-metadata"),
            disabled=not metadata_dirty or conflict_pending,
            help=(
                "먼저 위에서 최신 개정을 불러오세요."
                if conflict_pending
                else "바뀐 기본정보를 저장합니다."
                if metadata_dirty
                else "저장할 기본정보 변경이 없습니다."
            ),
        )
        if submitted and metadata_dirty and not conflict_pending:
            request = AuthoringProjectUpdateRequest(
                expected_revision=_editor_base_revision(project, "metadata"),
                title=title,
                purpose=purpose,
                scope=scope,
                legal_bases=_nonempty_lines(legal_bases),
                responsible_department=department,
                planned_effective_date=effective_date
                if isinstance(effective_date, date)
                else None,
                revision_reason=revision_reason,
                predecessor_reference=predecessor_reference,
            )
            _run_project_action(
                lambda: service.update_project(
                    str(project.project_id),
                    request,
                    tenant_id=tenant_id,
                    profile_id=profile_id,
                    actor=AUTHORING_ACTOR,
                ),
                success="기본정보를 저장했습니다.",
                clear_dirty_keys=(dirty_key,),
                rebase_other_dirty_sections_for=project,
            )


def _render_start_drafting(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    ready = metadata_ready_for_drafting(project)
    metadata_dirty = _editor_is_dirty(project, "metadata")
    abandon_pending = _editor_is_dirty(project, "actions") or bool(
        st.session_state.get(
            _project_state_key("control", project, "abandon-confirm")
        )
    )
    conflict_pending = _project_has_unresolved_conflict(project)
    if ready and not metadata_dirty:
        st.caption("저장된 기본정보를 확인했습니다. 이제 템플릿 조문을 작성하세요.")
    elif metadata_dirty:
        st.warning(
            "지금 보이는 기본정보를 먼저 저장해야 조문 작성을 시작할 수 있습니다."
        )
    else:
        st.warning(
            "규정명·목적·적용 범위·근거·담당부서·시행 예정일을 모두 입력하고 "
            "'기본정보 저장'을 눌러야 다음 단계로 갈 수 있습니다."
        )
    if abandon_pending:
        st.warning(
            "작성 중단 입력을 시작했습니다. 조문 작성을 계속하려면 아래 "
            "'작성 중단 입력 지우기'를 먼저 누르세요."
        )
    start_clicked = st.button(
        "조문 작성 시작",
        type="primary",
        disabled=(
            not ready or metadata_dirty or abandon_pending or conflict_pending
        ),
    )
    if (
        start_clicked
        and ready
        and not metadata_dirty
        and not abandon_pending
        and not conflict_pending
    ):
        _run_project_action(
            lambda: service.start_drafting(
                str(project.project_id),
                AuthoringTransitionRequest(expected_revision=project.revision),
                tenant_id=tenant_id,
                profile_id=profile_id,
                actor=AUTHORING_ACTOR,
            ),
            success="조문 작성 단계로 이동했습니다.",
            clear_editor_sections=((project, "actions"),),
        )


def _render_abandon_planning(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    with st.expander("잘못 만든 초안 작성 중단하기"):
        action_dirty_key = _editor_dirty_key(project, "actions")
        action_buffer = _editor_buffer(
            project,
            "actions",
            {"abandon_reason": ""},
        )
        st.warning(
            "템플릿이나 제·개정 유형을 잘못 골랐을 때만 사용하세요. "
            "작성을 중단한 초안은 다시 편집할 수 없고, 새 초안을 만들어야 합니다."
        )
        if _project_has_unsaved_changes(project):
            st.caption("저장하지 않은 화면 입력값도 함께 버려집니다.")
        confirmed = st.checkbox(
            "이 초안을 더 이상 작성하지 않고 중단하겠습니다.",
            key=_project_state_key("control", project, "abandon-confirm"),
        )
        reason = st.text_area(
            "작성 중단 사유",
            key=_editor_widget_key(
                project,
                "actions",
                "abandon_reason",
                str(action_buffer.get("abandon_reason") or ""),
            ),
            placeholder="예: 템플릿을 잘못 선택함",
            disabled=not confirmed,
            on_change=_mark_editor_dirty,
            args=(action_dirty_key,),
        )
        st.session_state[action_dirty_key] = bool(confirmed or reason.strip())
        _set_editor_buffer(project, "actions", {"abandon_reason": reason})
        if confirmed or reason.strip():
            st.button(
                "작성 중단 입력 지우기",
                key=_project_state_key("control", project, "clear-abandon-inputs"),
                help="중단 확인과 사유를 지우고 조문 작성을 계속할 수 있게 합니다.",
                on_click=_clear_action_inputs,
                args=(project, "abandon-confirm"),
            )
        abandon_clicked = st.button(
            "이 초안 작성 중단",
            key=_project_state_key("control", project, "abandon"),
            disabled=(
                not confirmed
                or not reason.strip()
                or _project_has_unresolved_conflict(project)
            ),
        )
        if (
            abandon_clicked
            and confirmed
            and reason.strip()
            and not _project_has_unresolved_conflict(project)
        ):
            _run_project_action(
                lambda: service.abandon_project(
                    str(project.project_id),
                    AuthoringTransitionRequest(
                        expected_revision=project.revision,
                        comment=safe_review_reason(reason),
                    ),
                    tenant_id=tenant_id,
                    profile_id=profile_id,
                    actor=AUTHORING_ACTOR,
                ),
                success="이 초안 작성을 중단했습니다. 올바른 템플릿과 유형으로 새 초안을 만드세요.",
                clear_editor_state_for=project,
            )


def _render_clause_editor(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    st.markdown("### 2. 조문 작성")
    st.caption(
        "쉬운말 안내를 보고 실제로 적용할 내용을 쓰세요. 법적 적합성은 별도로 확인해야 합니다."
    )
    editable = editable_clauses_in_document_order(project)
    dirty_key = _editor_dirty_key(project, "clauses")
    buffer = _editor_buffer(
        project,
        "clauses",
        {f"body:{clause.clause_id}": clause.body for clause in editable},
    )
    bodies: dict[str, str] = {}
    for clause in editable:
        label = f"{clause.article_number}"
        if clause.title:
            label += f"({clause.title})"
        if not clause.required:
            label += " · 선택"
        st.markdown(
            f"**{label}** · {NODE_TYPE_LABELS.get(clause.node_type, clause.node_type)}"
        )
        st.caption(clause.beginner_guidance)
        bodies[str(clause.clause_id)] = st.text_area(
            f"{label} 내용",
            key=_editor_widget_key(
                project,
                "clauses",
                f"body:{clause.clause_id}",
                str(buffer.get(f"body:{clause.clause_id}") or ""),
            ),
            on_change=_mark_editor_dirty,
            args=(dirty_key,),
            label_visibility="collapsed",
            height=120,
        )
    clauses_dirty = any(
        bodies.get(str(clause.clause_id), clause.body) != clause.body
        for clause in editable
    )
    conflict_pending = _project_has_unresolved_conflict(project)
    st.session_state[dirty_key] = clauses_dirty
    _set_editor_buffer(
        project,
        "clauses",
        {
            f"body:{clause.clause_id}": bodies.get(
                str(clause.clause_id), clause.body
            )
            for clause in editable
        },
    )
    if clauses_dirty:
        st.warning("저장하지 않은 조문이 있습니다. '조문 저장'을 먼저 누르세요.")
    submitted = st.button(
        "조문 저장",
        type=(
            "primary"
            if clauses_dirty and _first_dirty_section(project) == "clauses"
            else "secondary"
        ),
        key=_project_state_key("control", project, "save-clauses"),
        disabled=not clauses_dirty or conflict_pending,
        help=(
            "먼저 위에서 최신 개정을 불러오세요."
            if conflict_pending
            else "바뀐 조문을 저장합니다."
            if clauses_dirty
            else "저장할 조문 변경이 없습니다."
        ),
    )
    if not submitted or not clauses_dirty or conflict_pending:
        return
    clauses = [
        clause.model_copy(
            update={"body": bodies.get(str(clause.clause_id), clause.body)}
        )
        for clause in project.clauses
    ]
    _run_project_action(
        lambda: service.update_project(
            str(project.project_id),
            AuthoringProjectUpdateRequest(
                expected_revision=_editor_base_revision(project, "clauses"),
                clauses=clauses,
            ),
            tenant_id=tenant_id,
            profile_id=profile_id,
            actor=AUTHORING_ACTOR,
        ),
        success="조문을 저장했습니다. 다음은 작성 검사를 실행하세요.",
        clear_dirty_keys=(dirty_key,),
        rebase_other_dirty_sections_for=project,
    )


def _render_lint_and_review(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    st.markdown("### 4. 작성 검사와 내용 확인")
    report = project.last_lint_report
    has_unsaved_changes = _project_has_unsaved_changes(project)
    conflict_pending = _project_has_unresolved_conflict(project)
    if has_unsaved_changes:
        st.warning(
            "저장하지 않은 값이 있습니다. 기본정보·조문·확인 목록의 "
            "해당 저장 버튼을 먼저 누른 뒤 작성 검사를 실행하세요."
        )
    report = _current_lint_report(project) or report
    has_current_report = report is not None and report.revision == project.revision
    lint_clicked = st.button(
        "작성 검사",
        type="primary" if not has_unsaved_changes and not has_current_report else "secondary",
        disabled=has_unsaved_changes or conflict_pending,
    )
    if lint_clicked and not has_unsaved_changes and not conflict_pending:
        try:
            report = cast(
                AuthoringLintReport,
                service.lint_project(
                    str(project.project_id),
                    tenant_id=tenant_id,
                    profile_id=profile_id,
                ),
            )
            st.session_state[_project_state_key("lint", project)] = (
                report.model_dump(mode="json")
            )
        except Exception as exc:
            _render_action_error(exc)
            return
    stored_report = st.session_state.get(_project_state_key("lint", project))
    if isinstance(stored_report, dict):
        report = AuthoringLintReport.model_validate(stored_report)
    if report is None or report.revision != project.revision:
        st.caption("현재 개정본을 아직 검사하지 않았습니다.")
        return
    _render_lint_report(report)
    if (
        st.button(
            "내용 확인 요청",
            type="primary" if report.can_request_review else "secondary",
            disabled=(
                has_unsaved_changes
                or conflict_pending
                or not report.can_request_review
            ),
        )
        and not has_unsaved_changes
        and not conflict_pending
    ):
        _run_project_action(
            lambda: service.request_review(
                str(project.project_id),
                AuthoringTransitionRequest(expected_revision=project.revision),
                tenant_id=tenant_id,
                profile_id=profile_id,
                actor=AUTHORING_ACTOR,
            ),
            success="내용 확인을 요청했습니다. 이 요청은 공식 승인이 아닙니다.",
        )


def _render_checklist_editor(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    st.markdown("### 3. 작성자 확인 목록")
    st.caption(
        "각 항목을 실제로 확인한 뒤만 표시하세요. 내용 동결 전에는 모두 완료해야 합니다."
    )
    dirty_key = _editor_dirty_key(project, "checklist")
    buffer = _editor_buffer(
        project,
        "checklist",
        {f"completed:{item.item_id}": item.completed for item in project.checklist},
    )
    completed: dict[str, bool] = {}
    for item in project.checklist:
        completed[item.item_id] = st.checkbox(
            item.label,
            help=item.guidance,
            key=_editor_widget_key(
                project,
                "checklist",
                f"completed:{item.item_id}",
                bool(buffer.get(f"completed:{item.item_id}", item.completed)),
            ),
            on_change=_mark_editor_dirty,
            args=(dirty_key,),
        )
        st.caption(item.guidance)
    checklist_dirty = any(
        completed.get(item.item_id, item.completed) != item.completed
        for item in project.checklist
    )
    conflict_pending = _project_has_unresolved_conflict(project)
    st.session_state[dirty_key] = checklist_dirty
    _set_editor_buffer(
        project,
        "checklist",
        {
            f"completed:{item.item_id}": completed.get(
                item.item_id, item.completed
            )
            for item in project.checklist
        },
    )
    if checklist_dirty:
        st.warning(
            "저장하지 않은 확인 표시가 있습니다. '확인 목록 저장'을 먼저 누르세요."
        )
    submitted = st.button(
        "확인 목록 저장",
        type=(
            "primary"
            if checklist_dirty and _first_dirty_section(project) == "checklist"
            else "secondary"
        ),
        key=_project_state_key("control", project, "save-checklist"),
        disabled=not checklist_dirty or conflict_pending,
        help=(
            "먼저 위에서 최신 개정을 불러오세요."
            if conflict_pending
            else "바뀐 확인 표시를 저장합니다."
            if checklist_dirty
            else "저장할 확인 표시 변경이 없습니다."
        ),
    )
    if not submitted or not checklist_dirty or conflict_pending:
        return
    checklist = [
        BeginnerChecklistItem(
            item_id=item.item_id,
            label=item.label,
            guidance=item.guidance,
            completed=completed[item.item_id],
            notes=item.notes,
        )
        for item in project.checklist
    ]
    _run_project_action(
        lambda: service.update_project(
            str(project.project_id),
            AuthoringProjectUpdateRequest(
                expected_revision=_editor_base_revision(project, "checklist"),
                checklist=checklist,
            ),
            tenant_id=tenant_id,
            profile_id=profile_id,
            actor=AUTHORING_ACTOR,
        ),
        success="확인 목록을 저장했습니다.",
        clear_dirty_keys=(dirty_key,),
        rebase_other_dirty_sections_for=project,
    )


def _render_lint_report(report: AuthoringLintReport) -> None:
    counts = report.summary()
    if not report.findings:
        st.success(
            "작성 검사에서 오류나 경고를 찾지 못했습니다. 법적 검토가 완료된 것은 아닙니다."
        )
        return
    st.caption(
        f"오류 {counts['error']}개 · 경고 {counts['warning']}개 · 안내 {counts['info']}개"
    )
    for finding in report.findings:
        _render_lint_finding(finding)


def _render_lint_finding(finding: AuthoringLintFinding) -> None:
    location = finding.article_number or _field_path_label(finding.field_path)
    severity_label = {
        "error": "오류",
        "warning": "경고",
        "info": "안내",
    }.get(finding.severity.value, "안내")
    message = (
        f"**{severity_label} · {location}** · {finding.message}  \n"
        f"🛠️ 수정 방법: {finding.suggestion}"
    )
    if finding.severity.value == "error":
        st.error(message)
    elif finding.severity.value == "warning":
        st.warning(message)
    else:
        st.info(message)


def _field_path_label(path: str) -> str:
    labels = {
        "title": "규정명",
        "purpose": "목적",
        "scope": "적용 범위",
        "legal_bases": "법적·내부 근거",
        "responsible_department": "담당부서",
        "planned_effective_date": "시행 예정일",
        "revision_reason": "개정 사유",
        "predecessor_reference": "개정 대상 규정",
        "clauses": "조문",
        "checklist": "확인 목록",
    }
    return labels.get(path, path)


def _render_resume_drafting(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    st.warning("수정 요청이 기록되었습니다. 조문 작성 단계로 돌아가 반영하세요.")
    reason = safe_review_reason(getattr(project, "change_request_comment", None))
    if reason:
        st.info(f"📌 수정 요청: {reason}")
    else:
        st.caption("저장된 수정 요청 메모가 없습니다. 초안은 변경되지 않았습니다.")
    conflict_pending = _project_has_unresolved_conflict(project)
    if st.button(
        "조문 다시 작성",
        type="primary",
        disabled=conflict_pending,
    ):
        _run_project_action(
            lambda: service.start_drafting(
                str(project.project_id),
                AuthoringTransitionRequest(expected_revision=project.revision),
                tenant_id=tenant_id,
                profile_id=profile_id,
                actor=AUTHORING_ACTOR,
            ),
            success="수정할 수 있도록 조문 작성 단계로 돌아갔습니다.",
        )


def _render_review_actions(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    action_dirty_key = _editor_dirty_key(project, "actions")
    action_buffer = _editor_buffer(
        project,
        "actions",
        {"change_comment": "", "self_freeze_reason": ""},
    )
    st.markdown("### 5. 내용 확인")
    st.caption(
        "아래 검토 시트에서 기본정보, 전체 조문, 근거, 확인 목록, "
        "최신 작성 검사를 먼저 읽으세요."
    )
    _render_review_sheet(project)
    st.warning(
        "이 화면은 1인 연습용이므로 여기서 동결한 결과는 연습용으로만 "
        "내보냅니다. 작성자와 확인자가 다른 실무 2인 흐름은 보호 모드 API에서 "
        "진행하세요."
    )
    conflict_pending = _project_has_unresolved_conflict(project)
    self_freeze_key = _project_state_key("control", project, "self-freeze")
    freeze_intent = bool(
        st.session_state.get(self_freeze_key)
        or str(action_buffer.get("self_freeze_reason") or "").strip()
    )
    change_comment = st.text_area(
        "수정 요청 메모",
        key=_editor_widget_key(
            project,
            "actions",
            "change_comment",
            str(action_buffer.get("change_comment") or ""),
        ),
        placeholder="어디를 왜 고쳐야 하는지 적으세요.",
        disabled=freeze_intent,
        on_change=_mark_editor_dirty,
        args=(action_dirty_key,),
    )
    change_intent = bool(change_comment.strip())
    st.session_state[action_dirty_key] = bool(
        change_comment.strip()
        or str(action_buffer.get("self_freeze_reason") or "").strip()
    )
    _set_editor_buffer(
        project,
        "actions",
        {
            "change_comment": change_comment,
            "self_freeze_reason": str(
                action_buffer.get("self_freeze_reason") or ""
            ),
        },
    )
    if st.button(
        "수정 요청",
        disabled=not change_intent or freeze_intent or conflict_pending,
    ):
        _run_project_action(
            lambda: service.request_changes(
                str(project.project_id),
                AuthoringTransitionRequest(
                    expected_revision=project.revision,
                    comment=safe_review_reason(change_comment),
                ),
                tenant_id=tenant_id,
                profile_id=profile_id,
                actor=AUTHORING_ACTOR,
            ),
            success="수정 요청을 남겼습니다.",
            clear_editor_sections=((project, "actions"),),
        )

    st.divider()
    freeze_ready = review_ready_for_training_freeze(project)
    if not freeze_ready:
        st.warning(
            "현재 개정본의 작성 검사와 확인 목록이 완료되지 않아 "
            "내용을 동결할 수 없습니다. 위의 '수정 요청 메모'에 복귀 사유를 "
            "적고 '수정 요청'을 누른 뒤 작성 단계에서 저장·검사를 다시 하세요."
        )
    self_freeze = st.checkbox(
        "연습용 자체 확인으로만 내용을 동결합니다.",
        key=self_freeze_key,
        disabled=not freeze_ready or change_intent,
    )
    reason = st.text_area(
        "자체 확인 사유",
        key=_editor_widget_key(
            project,
            "actions",
            "self_freeze_reason",
            str(action_buffer.get("self_freeze_reason") or ""),
        ),
        placeholder="예: 로컬 교육용 흐름을 혼자 연습하기 위함",
        disabled=not freeze_ready or not self_freeze or change_intent,
        on_change=_mark_editor_dirty,
        args=(action_dirty_key,),
    )
    st.session_state[action_dirty_key] = bool(
        change_comment.strip() or reason.strip()
    )
    freeze_intent = bool(self_freeze or reason.strip())
    _set_editor_buffer(
        project,
        "actions",
        {
            "change_comment": change_comment,
            "self_freeze_reason": reason,
        },
    )
    if change_intent:
        st.info(
            "수정 요청 경로를 선택했습니다. 연습용 동결로 바꾸려면 먼저 "
            "'검토 결정 입력 지우기'를 누르세요."
        )
    elif freeze_intent:
        st.info(
            "연습용 동결 경로를 선택했습니다. 수정 요청으로 바꾸려면 먼저 "
            "'검토 결정 입력 지우기'를 누르세요."
        )
    if change_intent or freeze_intent:
        st.button(
            "검토 결정 입력 지우기",
            key=_project_state_key("control", project, "clear-review-inputs"),
            help="수정 요청 메모와 자체 확인 사유·확인을 모두 지웁니다.",
            on_click=_clear_action_inputs,
            args=(project, "self-freeze"),
        )
    st.warning(
        f"자체 확인으로 동결하면 **연습용·공식 사용 금지**와 "
        f"'{OFFICIAL_BOUNDARY_NOTICE}' 표시가 출력물에 계속 남습니다. "
        "(시스템에 연습용으로 기록됨)"
    )
    if (
        st.button(
            "연습용으로 내용 동결",
            type="primary",
            disabled=(
                not freeze_ready
                or not self_freeze
                or not reason.strip()
                or change_intent
                or conflict_pending
            ),
        )
        and freeze_ready
        and not conflict_pending
    ):
        _run_project_action(
            lambda: service.freeze_project(
                str(project.project_id),
                AuthoringProjectFreezeRequest(
                    expected_revision=project.revision,
                    comment=safe_review_reason(reason),
                    allow_training_self_freeze=True,
                ),
                tenant_id=tenant_id,
                profile_id=profile_id,
                actor=AUTHORING_ACTOR,
            ),
            success=f"연습용 내용을 동결했습니다. {OFFICIAL_BOUNDARY_NOTICE}. 다음은 초안 패키지를 만드세요.",
            clear_editor_sections=((project, "actions"),),
        )


def _render_export_actions(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    st.markdown("### 6. 초안 패키지 내보내기")
    export_format = cast(
        Literal["json", "markdown"],
        st.radio(
            "파일 형식",
            ["markdown", "json"],
            format_func=lambda value: (
                "Markdown (초보자 권장·읽기 쉬운 문서)"
                if value == "markdown"
                else "JSON (시스템 연동용 구조화 데이터)"
            ),
            horizontal=True,
            key=_project_state_key("control", project, "export-format"),
        ),
    )
    if st.button(
        "초안 패키지 만들기",
        type="primary",
        disabled=_project_has_unresolved_conflict(project),
    ):
        try:
            artifact = service.export_project(
                str(project.project_id),
                AuthoringExportRequest(
                    expected_revision=project.revision,
                    export_format=export_format,
                ),
                tenant_id=tenant_id,
                profile_id=profile_id,
                actor=AUTHORING_ACTOR,
            )
        except Exception as exc:
            _render_action_error(exc)
            return
        st.session_state[_export_key(project)] = {
            "content": artifact.content,
            "filename": artifact.filename,
            "media_type": artifact.media_type,
            "tenant_id": project.tenant_id,
            "profile_id": project.profile_id,
            "project_id": str(artifact.project_id),
            "frozen_revision": artifact.frozen_revision,
            "semantic_content_sha256": artifact.semantic_content_sha256,
            "content_sha256": artifact.content_sha256,
        }
        st.success(f"초안 패키지를 만들었습니다. {OFFICIAL_BOUNDARY_NOTICE}.")
        st.rerun()
    _render_download_if_available(
        project,
        service=service,
        tenant_id=tenant_id,
        profile_id=profile_id,
    )


def _render_review_sheet(project: AuthoringProject) -> None:
    """Render one read-only, self-contained sheet before review actions."""

    st.markdown("#### 검토 시트 (읽기 전용)")
    content_hash = project.semantic_content_hash or "아직 생성되지 않음"
    st.caption(f"개정본: {project.revision}판 · 내용 확인값(SHA-256): {content_hash}")

    st.markdown("##### 기본정보와 근거")
    _render_read_only_value("규정명", project.title)
    _render_read_only_value(
        "제·개정 유형",
        MODE_LABELS.get(project.authoring_mode, str(project.authoring_mode)),
    )
    _render_read_only_value("목적", project.purpose)
    _render_read_only_value("적용 범위", project.scope)
    _render_read_only_value("담당부서", project.responsible_department)
    _render_read_only_value(
        "시행 예정일",
        project.planned_effective_date.isoformat()
        if project.planned_effective_date is not None
        else "입력 안 됨",
    )
    _render_read_only_value(
        "법적·내부 근거",
        "\n".join(f"• {basis}" for basis in project.legal_bases) or "입력 안 됨",
    )
    if project.authoring_mode != AuthoringMode.ENACTMENT:
        _render_read_only_value(
            "개정 대상 규정", project.predecessor_reference or "입력 안 됨"
        )
        _render_read_only_value("개정 사유", project.revision_reason or "입력 안 됨")

    st.markdown("##### 전체 조문")
    for clause in project.clauses:
        node_label = NODE_TYPE_LABELS.get(clause.node_type, str(clause.node_type))
        clause_label = clause.article_number
        if clause.title:
            clause_label += f"({clause.title})"
        st.markdown(f"**{clause_label} · {node_label}**")
        if clause.node_type in {DraftNodeType.CHAPTER, DraftNodeType.SECTION}:
            st.caption("구조 제목·본문 입력 대상 아님")
        else:
            st.text(clause.body.strip() or "내용 없음")

    st.markdown("##### 작성자 확인 목록")
    for item in project.checklist:
        completion = "완료" if item.completed else "미완료"
        st.markdown(f"- **{completion}** · {item.label}")
        st.caption(item.guidance)

    st.markdown("##### 최신 작성 검사")
    report = project.last_lint_report
    if report is None:
        st.warning(
            "최신 작성 검사 결과가 없습니다. 동결하지 말고 작성 단계로 돌아가세요."
        )
    else:
        st.caption(f"검사 대상: {report.revision}판")
        _render_lint_report(report)


def _render_read_only_value(label: str, value: object) -> None:
    st.markdown(f"**{label}**")
    st.text(str(value or "입력 안 됨"))


def _render_download_if_available(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    artifact = st.session_state.get(_export_key(project))
    if not _export_cache_matches_project(project, artifact):
        st.session_state.pop(_export_key(project), None)
        if project.status == AuthoringProjectStatus.EXPORTED:
            st.info(
                "초안 패키지는 생성되어 있습니다. 내용 수정 없이 저장된 파일을 다시 불러올 수 있습니다."
            )
            if st.button(
                "저장된 초안 패키지 다시 불러오기",
                type="primary",
                key=_project_state_key("control", project, "reload-export"),
                disabled=_project_has_unresolved_conflict(project),
            ):
                try:
                    restored = service.get_export(
                        str(project.project_id),
                        tenant_id=tenant_id,
                        profile_id=profile_id,
                    )
                except Exception as exc:
                    _render_action_error(exc)
                    return
                st.session_state[_export_key(project)] = {
                    "content": restored.content,
                    "filename": restored.filename,
                    "media_type": restored.media_type,
                    "tenant_id": project.tenant_id,
                    "profile_id": project.profile_id,
                    "project_id": str(restored.project_id),
                    "frozen_revision": restored.frozen_revision,
                    "semantic_content_sha256": restored.semantic_content_sha256,
                    "content_sha256": restored.content_sha256,
                }
                st.success("저장된 초안 패키지를 무결성 확인 후 다시 불러왔습니다.")
                st.rerun()
        return
    artifact = cast(dict[str, Any], artifact)
    st.download_button(
        "초안 패키지 다운로드",
        data=artifact["content"],
        file_name=str(artifact.get("filename") or "authoring-draft.json"),
        mime=str(artifact.get("media_type") or "application/octet-stream"),
        type="primary",
    )
    st.caption(
        f"다운로드 후 법무·규정 담당자와 결재 절차에서 별도로 확인하세요. {OFFICIAL_BOUNDARY_NOTICE}."
    )


def _render_reopen_action(
    project: AuthoringProject,
    *,
    service: Any,
    tenant_id: str,
    profile_id: str,
) -> None:
    st.caption("내용을 고치면 현재 동결본을 덮어쓰지 않고 새 개정본이 시작됩니다.")
    if st.button(
        "새 개정본으로 다시 편집",
        type="secondary",
        disabled=_project_has_unresolved_conflict(project),
    ):
        _run_project_action(
            lambda: service.reopen_project(
                str(project.project_id),
                AuthoringTransitionRequest(
                    expected_revision=project.revision,
                    comment="로컬 작성 화면에서 새 개정본으로 다시 열기",
                ),
                tenant_id=tenant_id,
                profile_id=profile_id,
                actor=AUTHORING_ACTOR,
            ),
            success="이전 동결본을 덮어쓰지 않고 새 개정본으로 다시 열었습니다.",
            clear_session_keys=(_export_key(project),),
        )


def _run_project_action(
    action: Any,
    *,
    success: str,
    clear_dirty_keys: Sequence[str] = (),
    rebase_other_dirty_sections_for: AuthoringProject | None = None,
    clear_editor_state_for: AuthoringProject | None = None,
    clear_editor_sections: Sequence[tuple[AuthoringProject, str]] = (),
    clear_session_keys: Sequence[str] = (),
) -> None:
    try:
        result = action()
    except Exception as exc:
        _render_action_error(exc)
        return
    cleared = set(clear_dirty_keys)
    new_revision = getattr(result, "revision", None)
    if (
        rebase_other_dirty_sections_for is not None
        and type(new_revision) is int
    ):
        for section in AUTHORING_EDITOR_SECTIONS:
            dirty_key = _editor_dirty_key(
                rebase_other_dirty_sections_for,
                section,
            )
            if dirty_key not in cleared and bool(st.session_state.get(dirty_key)):
                st.session_state[
                    _editor_base_revision_key(
                        rebase_other_dirty_sections_for,
                        section,
                    )
                ] = new_revision
    for key in clear_dirty_keys:
        st.session_state.pop(key, None)
    if clear_editor_state_for is not None:
        _clear_project_editor_state(clear_editor_state_for)
    for project_id, section in clear_editor_sections:
        _clear_project_editor_section_state(project_id, section)
    for key in clear_session_keys:
        st.session_state.pop(key, None)
    st.session_state[AUTHORING_FLASH_MESSAGE_KEY] = success
    st.rerun()


def _render_action_error(exc: Exception) -> None:
    name = type(exc).__name__.casefold()
    if "revision" in name or "conflict" in name:
        selected_scope = str(
            st.session_state.get(AUTHORING_SELECTED_SCOPE_KEY) or ""
        )
        if selected_scope:
            st.session_state[AUTHORING_CONFLICT_PROJECT_KEY] = selected_scope
        st.error(
            "다른 화면에서 초안이 먼저 바뀌었습니다. 현재 입력을 바로 덮어쓰지 않았습니다. "
            "필요한 문장을 복사한 뒤 '최신 개정 불러오기'로 복구하세요."
        )
    elif isinstance(exc, PermissionError):
        st.error("이 확인 작업은 작성자와 다른 확인자가 해야 합니다.")
    elif "integrity" in name:
        st.error(
            "저장 파일의 무결성 확인에 실패했습니다. 이 초안 패키지는 사용하지 말고 "
            "운영자에게 확인을 요청하세요. 원문 경로나 입력 내용은 오류에 표시하지 않았습니다."
        )
    elif isinstance(exc, ValueError):
        st.error("필수 정보와 작성 검사 오류를 먼저 고친 뒤 다시 시도하세요.")
    elif isinstance(exc, KeyError):
        st.error("초안을 찾을 수 없습니다. 현재 기관과 초안 선택을 다시 확인하세요.")
    else:
        st.error(
            "작성 작업을 완료하지 못했습니다. 초안은 변경하지 않았으니 잠시 후 다시 시도하세요."
        )


def _nonempty_lines(value: str) -> list[str]:
    return list(
        dict.fromkeys(line.strip() for line in value.splitlines() if line.strip())
    )


def safe_review_reason(value: object) -> str:
    """Remove controls and local absolute paths from reviewer guidance."""

    return sanitize_authoring_reason(value)


def _export_key(project: AuthoringProject) -> str:
    return _project_state_key("export", project)


def _export_cache_matches_project(
    project: AuthoringProject,
    artifact: object,
) -> bool:
    if project.status != AuthoringProjectStatus.EXPORTED or not isinstance(
        artifact, dict
    ):
        return False
    content = artifact.get("content")
    if not isinstance(content, bytes):
        return False
    return bool(
        artifact.get("tenant_id") == project.tenant_id
        and artifact.get("profile_id") == project.profile_id
        and artifact.get("project_id") == str(project.project_id)
        and artifact.get("frozen_revision") == project.frozen_revision
        and artifact.get("semantic_content_sha256") == project.frozen_content_hash
        and artifact.get("content_sha256") == hashlib.sha256(content).hexdigest()
    )
