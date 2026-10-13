"""Beginner Vercel flow with retained destinations and task-only polling."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import streamlit as st
from app.services import vercel_deployment_service as service

ACCOUNT_STATE_KEY = "vercel-account-session"
CHECK_LABELS = {
    "mcp_initialized": "서버 연결",
    "tools_discovered": "AI가 사용할 도구 확인",
    "end_to_end_verified": "규정 검색과 원문 조회",
}
DIAGNOSIS = {
    "access": "Vercel의 접근 보호 설정 때문에 AI가 접속하지 못했습니다. 배포 관리에서 접근 설정을 확인하세요.",
    "connection": "서버에 연결하지 못했습니다. 잠시 뒤 다시 검사하고, 계속 실패하면 배포 로그를 확인하세요.",
    "tools": "필요한 검색 도구를 확인하지 못했습니다. 최신 MCP 묶음을 다시 배포하세요.",
    "search_fetch": "규정 검색·원문 조회가 통과하지 않았습니다. 다시 검사해도 같으면 현재 묶음을 다시 배포하세요.",
}


def _project_error(project: str, *, existing: list[str], replace_existing: bool) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", project) or "---" in project:
        return "영문 소문자·숫자로 시작하는 이름을 입력하세요. 하이픈·밑줄을 함께 쓸 수 있습니다."
    if not replace_existing and project in existing:
        return "이미 사용하는 이름입니다. ‘기존 주소 업데이트’를 선택하거나 다른 이름을 입력하세요."
    return ""


def _finish_job(state: dict[str, Any], runtime: Path) -> None:
    job = state.get("job")
    if not job or not job.done.is_set():
        return
    kind = state.pop("job_kind")
    state.pop("job")
    state["error"] = job.error
    state["error_code"] = getattr(job, "error_code", "")
    if job.result is not None:
        if kind == "connection":
            state.update(job.result)
            st.session_state[ACCOUNT_STATE_KEY] = job.result
        else:
            state[kind] = job.result
            if kind in {"account", "projects"}:
                st.session_state[ACCOUNT_STATE_KEY] = {
                    "account": state.get("account", {}), "projects": state.get("projects", {}),
                }
        state.pop("projects_failed", None)
    elif kind in {"account", "connection"}:
        state.pop("account", None)
        state.pop("projects", None)
        st.session_state.pop(ACCOUNT_STATE_KEY, None)
    elif kind == "projects":
        state["projects_failed"] = True
    elif kind == "deployment":
        state["deployment"] = service.read_deployment(runtime)
    if state.get("error_code") == "login_required":
        state.pop("account", None)
        state.pop("projects", None)
        st.session_state.pop(ACCOUNT_STATE_KEY, None)


@st.fragment(run_every=1)
def _render_job_progress(state: dict[str, Any], runtime: Path) -> None:
    """Only this status panel polls; idle forms do not rerun every second."""
    job = state.get("job")
    if not job:
        return
    if job.done.is_set():
        _finish_job(state, runtime)
        st.rerun()
    seconds = max(0, int(time.monotonic() - job.started))
    st.info(f"**{job.message}**\n\n경과 {seconds // 60:02d}:{seconds % 60:02d}")
    if job.login_url:
        st.link_button("Vercel 로그인·승인 화면 열기", job.login_url, type="primary")
        st.caption("브라우저에서 승인하면 이 화면이 자동으로 이어집니다. 인증 정보는 Vercel에서만 입력합니다.")
    else:
        st.caption("완료되면 다음 화면이 자동으로 나타납니다. 같은 버튼을 다시 누를 필요가 없습니다.")


def _render_result(
    runtime: Path, deployment: dict[str, Any], *, server_name: str, target: str,
    key: str, start: Callable, update_failed: bool,
) -> bool:
    if deployment.get("verified") and not update_failed:
        st.success("배포·검색 검사 완료 — 다음은 AI 앱에 주소 등록입니다.")
    elif update_failed:
        st.warning("이번 작업은 완료되지 않았습니다. 아래는 이전에 배포한 주소입니다.")
    else:
        st.warning("주소는 생성됐지만 연결 검사가 끝나지 않았습니다.")
    st.markdown("**AI 앱에 붙여 넣을 MCP 주소**")
    st.code(deployment["url"], language=None, wrap_lines=True)
    st.caption("주소 오른쪽 복사 버튼을 누르세요. 인증 없이 URL을 아는 누구나 승인된 규정을 조회할 수 있습니다.")
    if deployment.get("verified") and not update_failed:
        if target == "chatgpt-remote":
            st.markdown("1. ChatGPT **Plugins → + → Add custom MCP server**를 엽니다.\n"
                        "2. 이름과 위 주소를 입력하고 인증은 **No authentication**으로 선택해 만듭니다.\n"
                        "3. 개인 플러그인 목록에서 설치한 뒤 **Work** 새 대화에서 `@`로 선택하고 아래 질문을 입력합니다.")
            st.link_button("ChatGPT 열기", "https://chatgpt.com/plugins", type="primary")
            st.caption("계정에 따라 앱·개발자 모드 메뉴로 표시될 수 있습니다. 메뉴가 없으면 워크스페이스 권한을 확인하세요. "
                       "[공식 연결 안내](https://developers.openai.com/plugins/quickstart)")
        else:
            st.markdown("1. Claude의 **Customize → Connectors → Add custom connector**에서 커넥터를 추가합니다.\n"
                        "2. 위 주소를 붙여 넣습니다. 이번 MCP는 별도 인증이 없습니다.\n"
                        "3. 새 대화에서 커넥터를 켜고 아래 질문을 입력합니다.")
            st.link_button("Claude 열기", "https://claude.ai/", type="primary")
            st.caption("조직 계정은 관리자 설정에서 추가할 수 있습니다. "
                       "[공식 연결 안내](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)")
        st.code("연결한 규정 MCP의 search로 인사규정을 찾아줘.\n첫 결과의 id로 fetch를 호출해 원문과 출처를 보여줘.",
                language=None, wrap_lines=True)
        st.caption("서버 검사는 완료됐습니다. AI 앱 등록과 실제 대화 확인은 사용자가 직접 마칩니다.")
    else:
        st.info(DIAGNOSIS.get(deployment.get("diagnosis"), "배포 관리에서 상태를 확인한 뒤 연결 검사를 다시 실행하세요."))
        if st.button("연결 다시 검사", key=key + "-verify", type="primary"):
            start("deployment", lambda progress: service.verify_deployment(runtime, deployment, progress))
    with st.expander("검사 결과·배포 관리", expanded=False):
        for check, label in CHECK_LABELS.items():
            passed = (deployment.get("checks") or {}).get(check) is True
            st.write(f"{'통과' if passed else '확인 필요'} · {label}")
        if deployment.get("checked_at"):
            checked = time.strftime("%Y-%m-%d %H:%M", time.localtime(deployment["checked_at"]))
            st.caption(f"마지막 서버 검사: {checked}")
        st.caption(f"배포 대상: {deployment['scope']} / {deployment['project']}")
        st.link_button("Vercel 배포 상태·로그 보기",
                       f"https://vercel.com/{deployment['scope']}/{deployment['project']}/deployments")
        if deployment.get("verified") and not update_failed:
            if st.button("연결 다시 검사", key=key + "-verify"):
                start("deployment", lambda progress: service.verify_deployment(runtime, deployment, progress))
        st.download_button("HTTPS 연결 설정 다운로드",
                           json.dumps({"mcpServers": {server_name: {"type": "http", "url": deployment["url"]}}},
                                      ensure_ascii=False, indent=2),
                           file_name="vercel_mcp_connection.json", mime="application/json", key=key + "-download")
    return st.checkbox("설정 변경·다시 배포", key=key + "-again")


def render_vercel_deployment(
    *, runtime_data_dir: str, server_name: str, target: str,
    guide: Callable[[str, str, str], None] | None = None,
) -> None:
    runtime = Path(runtime_data_dir)
    try:
        identity = service.deployment_identity(runtime)
    except OSError:
        st.info("먼저 MCP 배포 파일을 준비하세요. 완료되면 여기에 배포 버튼이 나타납니다.")
        return
    key = "vercel-wizard-" + identity
    state = st.session_state.setdefault(key, {})
    _finish_job(state, runtime)
    if "deployment" not in state:
        state["deployment"] = service.read_deployment(runtime)
    previous = service.read_previous_deployment(runtime)
    deployment = state["deployment"]
    shared = st.session_state.get(ACCOUNT_STATE_KEY) or {}
    account = state.get("account") or shared.get("account") or {}
    projects = state.get("projects") or shared.get("projects") or {}
    state.update(account=account, projects=projects)

    def start(kind: str, operation: Callable[[service.Progress], dict[str, Any]]) -> None:
        state["error"] = ""
        state["error_code"] = ""
        state["job_kind"] = kind
        state["job"] = service.DeploymentJob(operation)
        st.rerun()

    def hint(title: str, description: str, control_key: str) -> None:
        if guide:
            guide(title, description, control_key)

    st.markdown("### Vercel에 배포하고 AI에 연결하기")
    st.caption("계정 연결 → 공개 범위 확인·배포 → AI에 주소 등록")
    if state.get("job"):
        _render_job_progress(state, runtime)
        return
    if state.get("error"):
        st.error(state["error"])
        if state.get("error_code") == "login_required":
            if st.button("Vercel 계정 다시 연결", key=key + "-reauth", type="primary"):
                start("connection", lambda progress: service.connect_account(runtime.parent, str(previous.get("scope") or ""), progress))
            return
    if deployment:
        if not _render_result(runtime, deployment, server_name=server_name, target=target, key=key,
                              start=start, update_failed=bool(state.get("error"))):
            return
    elif previous:
        st.info("새 규정 묶음이 준비됐습니다. 이전 배포 대상을 불러왔으며, 업데이트하면 같은 주소에서 새 규정을 조회할 수 있습니다.")
        st.caption("새 묶음은 아직 배포하지 않았습니다.")
    try:
        service.vercel_command()
        tool_error = None
    except service.DeploymentError as exc:
        tool_error = exc
    if tool_error:
        st.info("지금 할 일: " + str(tool_error))
        if tool_error.code == "node_missing":
            st.link_button("Node.js LTS 설치 페이지 열기", "https://nodejs.org/en/download", type="primary")
            st.caption("설치 후 프로그램을 다시 열면 계속 진행할 수 있습니다.")
        else:
            hint("Vercel 배포 도구를 준비하세요", "설치 버튼을 누르면 이 프로그램에서 사용할 배포 도구를 준비합니다.", key + "-install")
            if st.button("Vercel 배포 도구 설치", key=key + "-install", type="primary"):
                start("tool", service.install_vercel)
        return
    if not account:
        st.info("지금 할 일: Vercel 계정을 연결하세요. 계정이 없으면 로그인 화면에서 만들 수 있습니다.")
        st.caption("이미 로그인했다면 저장된 연결을 사용합니다. 처음 한 번만 브라우저에서 승인이 필요합니다.")
        hint("Vercel 계정을 연결하세요", "계정 연결을 누르고 필요한 경우 브라우저에서 로그인하세요. 끝나면 자동으로 이어집니다.", key + "-account")
        if st.button("Vercel 계정 연결", key=key + "-account", type="primary"):
            start("connection", lambda progress: service.connect_account(runtime.parent, str(previous.get("scope") or ""), progress))
        return
    st.caption(f"Vercel 계정 연결됨 · {account['username']}")
    scopes = account["scopes"]
    scope_options = [item["slug"] for item in scopes]
    scope_key = key + "-scope"
    if st.session_state.get(scope_key) not in scope_options:
        st.session_state[scope_key] = previous["scope"] if previous.get("scope") in scope_options else scope_options[0]
    if len(scopes) == 1:
        scope = scope_options[0]
    else:
        scope = st.selectbox("배포할 Vercel 계정·팀", scope_options, key=scope_key,
                             format_func=lambda value: next(item["name"] for item in scopes if item["slug"] == value))
    if projects.get("scope") != scope:
        if not state.get("projects_failed"):
            start("projects", lambda progress: service.load_projects(runtime.parent, scope, progress))
        if st.button("프로젝트 목록 다시 불러오기", key=key + "-projects-retry", type="primary"):
            state.pop("projects_failed", None)
            start("projects", lambda progress: service.load_projects(runtime.parent, scope, progress))
        return
    existing = [item["name"] for item in projects["projects"]]
    saved_project = previous.get("project") if previous.get("scope") == scope else None
    mode_key = key + "-project-mode-" + scope
    if mode_key not in st.session_state or (not existing and st.session_state[mode_key] == "existing"):
        st.session_state[mode_key] = "existing" if saved_project in existing else "new"
    mode = st.radio("배포 방법", ["new", "existing"] if existing else ["new"], key=mode_key,
                    horizontal=True, format_func=lambda value: "새 MCP 주소 만들기" if value == "new" else "기존 주소 업데이트")
    replace_existing = mode == "existing"
    if replace_existing:
        project_key = key + "-project-" + scope
        if st.session_state.get(project_key) not in existing and saved_project in existing:
            st.session_state[project_key] = saved_project
        elif st.session_state.get(project_key) not in existing:
            st.session_state[project_key] = existing[0]
        project = st.selectbox("업데이트할 프로젝트", existing, key=project_key)
    else:
        suggestion = re.sub(r"[^a-z0-9_-]", "-", server_name.lower()).strip("-_")[:80]
        suggestion = re.sub(r"-{3,}", "-", suggestion) or "regulation-mcp"
        project = st.text_input("새 프로젝트 이름", value=suggestion, key=key + "-name",
                                help="알아보기 쉬운 영문 이름을 정하세요. 최종 주소는 배포 후 자동으로 표시됩니다.").strip()
    error = _project_error(project, existing=existing, replace_existing=replace_existing)
    if error:
        st.error(error)
    st.caption(f"배포 대상: {scope} / {project or '이름을 입력하세요'}")
    public_key = key + "-public-" + scope + "-" + project
    if not error and not st.session_state.get(public_key):
        hint("공개할 규정과 배포 대상을 확인하세요", "이 주소를 아는 누구나 규정을 조회할 수 있습니다. 공개 가능한 규정인지 확인하고 선택하세요.", public_key)
    approved = st.checkbox("이 규정을 인터넷에 공개해도 됩니다.", key=public_key)
    st.caption("URL을 아는 누구나 로그인 없이 조회할 수 있습니다. 공개 목록은 만들지 않으며 검색엔진에 색인 제외를 요청합니다. Vercel 요금제에 따른 사용량이 발생할 수 있습니다.")
    replacement = not replace_existing or st.checkbox(
        "이 프로젝트의 기존 서비스를 이번 승인 규정 MCP로 업데이트합니다.",
        key=key + "-replace-" + scope + "-" + project)
    if not approved:
        st.caption("공개 가능한 규정인지 확인하고 위 확인란을 선택하면 배포할 수 있습니다.")
    elif not replacement:
        st.caption("업데이트할 프로젝트가 맞는지 확인하고 기존 서비스 교체 확인란을 선택하세요.")
        hint("업데이트할 프로젝트를 확인하세요", "선택한 프로젝트의 기존 서비스를 이번 승인 규정으로 교체합니다.", key + "-replace-" + scope + "-" + project)
    elif not error:
        hint("Vercel에 배포하세요", "배포 버튼을 누르면 업로드·서버 실행·검색 검사 후 연결 주소가 나타납니다.", key + "-deploy")
    if st.button("Vercel에 배포하고 연결 검사", key=key + "-deploy", type="primary",
                 disabled=bool(error) or not approved or not replacement):
        start("deployment", lambda progress: service.deploy_public_runtime(
            runtime=runtime, server_name=server_name, scope=scope, project=project,
            public_approved=approved, replace_existing=replace_existing, progress=progress))
    st.caption("승인 데이터 업로드 → 서버 실행 → 주소 확인 → 검색·원문 조회까지 자동으로 진행합니다.")
    with st.expander("계정·프로젝트 관리", expanded=False):
        if st.button("계정과 프로젝트 새로고침", key=key + "-refresh"):
            start("connection", lambda progress: service.connect_account(runtime.parent, scope, progress))
        if st.button("다른 Vercel 계정으로 로그인", key=key + "-login"):
            state.pop("account", None)
            state.pop("projects", None)
            st.session_state.pop(ACCOUNT_STATE_KEY, None)
            start("account", lambda progress: service.login(runtime.parent, progress))
        if st.button("Vercel 배포 도구 업데이트", key=key + "-install"):
            start("tool", service.install_vercel)
        if project and not error:
            st.link_button("Vercel 배포 상태·로그 보기", f"https://vercel.com/{scope}/{project}/deployments")
        st.caption("기관 내부·비공개 규정은 아래 고급 안내의 인증 배포 절차를 사용하세요.")
