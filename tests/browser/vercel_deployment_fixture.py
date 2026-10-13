"""Synthetic deployment UI fixture: never contacts Vercel or publishes data."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import streamlit as st
from app.services import vercel_deployment_service as service
from frontend.vercel_deployment import render_vercel_deployment

base = Path(os.environ["RR_DEPLOYMENT_FIXTURE_ROOT"]).resolve()
if not base.is_relative_to(ROOT / "tmp") or base == ROOT / "tmp":
    raise RuntimeError("Fixture must use a unique repository tmp directory.")
runtime = base / "bundle/data"
runtime.mkdir(parents=True, exist_ok=True)
manifest = runtime / "mcp_runtime_manifest.json"
if not manifest.exists():
    manifest.write_text(json.dumps({"tenant_id": "demo", "profile_id": "synthetic",
                                   "record_count": 120, "regulation_count": 8}), encoding="utf-8")

account = {"username": "demo-operator", "scopes": [{"slug": "demo-team", "name": "나의 Vercel 계정"}]}
projects = {"scope": "demo-team", "projects": [{"id": "demo-project", "name": "previous-regulations"}]}
receipt = {"identity": service.deployment_identity(runtime), "server_name": "regulation-demo",
           "scope": "demo-team", "project": "regulation-demo", "url": "https://regulation-demo.vercel.app/mcp",
           "verified": True, "deployed_at": 1791856821, "checked_at": 1791856822,
           "checks": {"mcp_initialized": True, "tools_discovered": True, "end_to_end_verified": True}}


def operation(value, progress, message):
    progress(message)
    time.sleep(1.5)
    return value


service.vercel_command = lambda: ["synthetic-vercel"]
service.load_account = lambda cwd, progress: operation(account, progress, "계정 확인 중")
service.login = lambda cwd, progress: operation(account, progress, "계정 연결 중")
service.load_projects = lambda cwd, scope, progress: operation(projects, progress, "프로젝트 확인 중")
service.read_deployment = lambda runtime: {}
service.read_previous_deployment = lambda runtime: (
    {**receipt, "identity": "older-bundle", "project": "previous-regulations"}
    if st.query_params.get("scenario") == "changed" else {}
)
service.verify_deployment = lambda runtime, result, progress: operation(receipt, progress, "검색·본문 조회 검사 중")


def deploy(**kwargs):
    with (base / "deployment_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({key: kwargs[key] for key in ("project", "scope", "public_approved", "replace_existing")}) + "\n")
    return operation(receipt, kwargs["progress"], "합성 배포 검사 중")


service.deploy_public_runtime = deploy
scenario = st.query_params.get("scenario", "first")
key = "vercel-wizard-" + service.deployment_identity(runtime)
if st.session_state.get("fixture-scenario") != scenario:
    st.session_state["fixture-scenario"] = scenario
    state = {"deployment": {}}
    if scenario in {"ready", "complete", "failed", "changed"}:
        state.update(account=account, projects=projects)
    if scenario in {"complete", "failed"}:
        state["deployment"] = {**receipt, "verified": scenario == "complete"}
        if scenario == "failed":
            state["deployment"].update(diagnosis="connection", checks={})
    st.session_state[key] = state

st.set_page_config(page_title="규정 MCP 배포 검증", layout="centered")
st.caption("합성 테스트 화면 · 실제 Vercel 업로드 없음")
render_vercel_deployment(runtime_data_dir=str(runtime), server_name="regulation-demo", target="chatgpt-remote")
