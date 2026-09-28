"""Isolated real-app fixture for the public beginner guide recording.

Run with Streamlit on localhost. All runtime data and the synthetic document
stay under ignored tmp/. This fixture never opens an existing institution.
"""
from pathlib import Path
import runpy
import sys
from uuid import uuid4

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.synthetic_sample_service import build_synthetic_regulation_docx

sample = ROOT / "tmp" / "beginner-guide-sample.docx"
sample.parent.mkdir(exist_ok=True)
if not sample.exists():
    sample.write_bytes(build_synthetic_regulation_docx())
st.session_state.setdefault("guide_test_root", ROOT / "tmp" / ("guide-demo-" + uuid4().hex))
st.session_state.setdefault("ai_connection_overrides", {
    "data_dir": st.session_state["guide_test_root"],
    "artifact_root": st.session_state["guide_test_root"],
    "institution_profiles_path": "",
    "app_env": "test",
    "api_auth_required": False,
    "tenant_storage_isolation": False,
    "enable_agent_review": False,
    "local_structure_review_enabled": False,
    "enable_kordoc_table_parser": False,
    "openai_api_key": "",
    "anthropic_api_key": "",
})
runpy.run_path(str(ROOT / "frontend" / "streamlit_app.py"), run_name="__main__")
