from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from app.api.routes_documents import (
    ApprovalRequest,
    IndexRequest,
    RegulationLifecycleRequest,
    approve_review_chunks,
    chunk_review_attention_reasons,
    get_index_status,
    index_document,
    reindex_document,
    transition_regulation_status,
)
from app.api.routes_rag import RagChatRequest, rag_chat
from app.core.api_audit import redact_sensitive_paths
from app.core.config import Settings, get_settings, set_runtime_settings_overrides
from app.core.pipeline import kordoc_table_command_status
from app.agents.provider_config import (
    SUPPORTED_AGENT_REVIEW_PROVIDERS,
    agent_review_configuration_reason,
    normalize_agent_review_provider,
)
from app.core.security import AuthContext
from app.core.institution_profiles import (
    ALLOWED_REQUIRED_ROW_FIELDS,
    InstitutionProfileRegistry,
    apply_institution_profile_to_metadata,
    delete_institution_profile,
    institution_profile_registry_to_bytes,
    load_institution_profile_registry,
    load_institution_profile_registry_from_bytes,
    save_institution_profile_registry,
    upsert_institution_profile,
)
from app.processors.exporter import Exporter
from app.processors.quality_gate import (
    QualityProfileConfig,
    load_quality_gate_profile_config,
    load_quality_gate_profile_config_from_bytes,
    quality_profile_config_to_bytes,
    save_quality_profile_config,
    upsert_quality_profile,
)
from app.schemas.chunk import ChunkOptions
from app.services.document_service import DocumentService
from app.services.kordoc_reprocessing_service import (
    KordocReprocessingError,
    KordocReprocessingResult,
    KordocReprocessingService,
)
from app.services.processing_service import ProcessingService
from app.services.regulation_catalog_service import group_documents_by_regulation, latest_active_version, read_regulation_metadata
from app.services.regulation_metadata_service import infer_regulation_metadata, regulation_upload_sort_key
from app.services.review_workflow_service import review_batch_chunk_fingerprint, review_content_hash
from app.services.approval_governance import (
    apply_ai_review_decisions_to_preview_text,
    approval_review_completion_state,
    build_approval_review_events,
)
from app.storage.repository import JsonRepository
from scripts.generate_mcp_client_config import (
    KORDOC_TABLE_REQUIRED_FILE_TYPES,
    _kordoc_table_parser_evidence_summary,
    build_mcp_client_config,
    render_agent_connect_prompt_for_program,
    write_mcp_runtime_data_bundle,
    write_mcp_setup_bundle,
    write_mcp_setup_bundle_zip,
)
from scripts.mcp_connection_diagnostic import (
    STAGE_ORDER as MCP_CONNECTION_STAGE_ORDER,
    diagnostic_from_bundle_status,
)
from scripts.refresh_mcp_client_connection import run as refresh_mcp_client_connection
from scripts.analyze_regulation_corpus import (
    GOLDSET_COMPLETE_LABEL_STATUSES,
    GOLDSET_SCORE_SPECS,
    optional_int,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _streamlit_dialog(*args, **kwargs):
    """Use Streamlit dialogs when available and remain import-safe in guards."""

    decorator = getattr(st, "dialog", None)
    if callable(decorator):
        return decorator(*args, **kwargs)

    def passthrough(function):
        return function

    return passthrough

OFFICIAL_RAG_MCP_REVIEW_REQUIRED_KEY = "official-rag-mcp-review-required"
UNREVIEWED_POC_REVIEW_ACK_KEY = "unreviewed-poc-review-acknowledged"
MCP_REQUIRED_SOURCE_METADATA_FIELDS = (
    "institution_name",
    "profile_id",
    "source_system",
    "source_url",
    "regulation_id",
    "regulation_version",
    "regulation_status",
    "effective_from",
)
APPROVABLE_CHUNK_STATUSES = frozenset(
    {"draft", "needs_review", "pending", "pending_human_review", "reviewed", "human_reviewed"}
)
UNREVIEWED_PREVIEW_WARNING = (
    "UNREVIEWED_POC_REVIEW (legacy UNREVIEWED_PREVIEW) is an isolated PoC Review mode "
    "for quick parsing, search, quality, and connection UX checks only. "
    "It must not write to official approved vectors, release evidence, or deployment handoff outputs. "
    "Official RAG/MCP remains blocked until human review, approval, index/reindex, "
    "and MCP visibility audit are complete."
)
UNREVIEWED_PREVIEW_WARNING_KO = (
    "지금은 '미검수 미리보기' 모드입니다. 이 상태의 결과는 참고용일 뿐이며, "
    "사람 검수와 승인을 마치기 전에는 공식 AI 답변 근거로 사용할 수 없습니다."
)

GOLDSET_STRUCTURE_LABELS = {
    "article": "조문",
    "paragraph_item": "항/호/목",
    "appendix_form": "별표/서식",
    "table": "표",
    "nested_table": "표 안의 표",
    "supplementary_effective_date": "부칙/시행일",
    "footnote_caption": "각주/캡션",
}
GOLDSET_STRUCTURE_GUIDANCE = {
    "article": "제1조, 제2조처럼 조문 번호와 제목이 원문과 전처리 결과에서 같이 보이는지 확인합니다.",
    "paragraph_item": "①, 1., 가.처럼 항·호·목으로 나뉜 부분이 빠지거나 합쳐지지 않았는지 확인합니다.",
    "appendix_form": "별표, 별지, 서식, 붙임이 원문 개수와 전처리 결과 개수에서 맞는지 확인합니다.",
    "table": "표의 행·열과 중요한 셀 내용이 유지됐는지 확인합니다. 표가 깨졌으면 수정 필요로 둡니다.",
    "nested_table": "표 안에 다시 들어간 표가 있으면 별도로 확인합니다. 없으면 해당 없음으로 둡니다.",
    "supplementary_effective_date": "부칙과 시행일·적용일 정보가 답변 근거로 쓸 수 있게 잡혔는지 확인합니다.",
    "footnote_caption": "각주, 미주, 표 제목, 그림 제목이 본문·표와 끊기지 않았는지 확인합니다.",
}
GOLDSET_DETAIL_FIELDS = {
    "paragraph_item": [
        ("①형", "pipeline_paragraph_marker_count_circled"),
        ("1.형", "pipeline_numbered_item_count"),
        ("가.형", "pipeline_hangul_item_count"),
        ("(1)형", "pipeline_parenthesized_item_count"),
    ],
    "appendix_form": [
        ("별표", "pipeline_annex_count"),
        ("별지/서식", "pipeline_form_count"),
        ("붙임", "pipeline_sheet_count"),
    ],
    "supplementary_effective_date": [
        ("부칙 블록", "pipeline_supplementary_block_count"),
        ("시행일 포함", "pipeline_supplementary_blocks_with_effective_date_count"),
        ("제N조(시행일)", "pipeline_explicit_effective_article_count"),
        ("직접 시행문", "pipeline_direct_effective_clause_count"),
        ("적용례", "pipeline_application_clause_count"),
    ],
}
GOLDSET_DECISION_OPTIONS = ["아직 안 봄", "문제없음", "수정 필요", "해당 없음", "판단 불가"]
GOLDSET_STATUS_LABELS = {
    "pending_human_review": "검수 전",
    "reviewed": "검수 완료",
    "human_reviewed": "사람 검수 완료",
    "approved": "승인됨",
    "completed": "완료",
}

SECURITY_LEVEL_LABELS = {
    "internal": "내부용 (internal)",
    "public": "공개 (public)",
    "sensitive": "민감 (sensitive)",
    "confidential": "기밀 (confidential)",
}
# AI 연결 설정 화면에서 운영자가 입력한 연결값을 담아 두는 세션 키.
# 저장 시 Settings 필드 이름을 그대로 키로 쓴 dict을 넣고, 스크립트 최상단에서
# set_runtime_settings_overrides로 적용한다. API 키는 이 세션 메모리에만 있고
# 디스크에는 저장하지 않는다(영구 설정은 .env 사용).
AI_CONNECTION_STATE_KEY = "ai_connection_overrides"
MCP_BUNDLE_STATE_PREFIX = "mcp_setup_bundle_written"
MCP_CONNECTION_STAGE_LABELS = {
    "registration": "1. 설정 등록",
    "loader": "2. 로더 확인",
    "transport": "3. 직접 통신",
    "fresh_app_server": "4. 새 app-server",
    "desktop_reload": "5. Desktop 재시작",
    "desktop_surface": "6. Desktop 도구 노출",
    "conversation": "7. 현재 대화 호출",
}
MCP_CONNECTION_STATE_LABELS = {
    "not_applicable": "해당 없음",
    "not_checked": "미확인",
    "pending": "확인 대기",
    "verified": "확인됨",
    "failed": "실패",
    "stale": "이전 증거",
}
MCP_EXTERNAL_DATA_TARGETS = frozenset({"chatgpt-remote", "chatgpt-tunnel", "claude-api"})

NAV_HOME = "🏠 시작하기"
NAV_PREPROCESS = "① 문서 올려서 전처리"
NAV_RESULTS = "② 결과 확인"
NAV_APPROVAL = "③ 검수하고 승인"
LEGACY_NAV_CONNECT = "시범 질의응답"
NAV_MCP = "④ MCP 생성·AI 연결"
NAV_GOLDSET = "🔍 정확도 검수(골드셋)"
NAV_ADMIN = "⚙️ 관리자 설정"
NAV_PAGES = [NAV_HOME, NAV_PREPROCESS, NAV_RESULTS, NAV_APPROVAL, NAV_MCP, NAV_GOLDSET, NAV_ADMIN]
PRIMARY_NAV_PAGES = [NAV_HOME, NAV_PREPROCESS, NAV_RESULTS, NAV_APPROVAL, NAV_MCP]
ADVANCED_NAV_PAGES = [NAV_GOLDSET, NAV_ADMIN]

# 전처리 기본 로직: 파서 초안 → 선택적 AI 검수 → 휴먼 승인.
PIPELINE_STAGES: list[tuple[str, str]] = [
    ("파서 초안", "프로그램이 문서를 조문·표·별표 단위로 1차 정리합니다."),
    ("AI 검수(선택)", "기능을 켠 경우에만 외부 AI가 검토 초안을 만듭니다."),
    ("휴먼 승인", "사람이 최종 확인하고 승인·색인합니다."),
]
PIPELINE_STAGE_PARSER = 1
PIPELINE_STAGE_AI_REVIEW = 2
PIPELINE_STAGE_HUMAN_APPROVAL = 3

# AI 검수 요약 상태를 행정직도 이해할 수 있는 문구로 옮긴다(과장 없이).
AI_REVIEW_STATUS_MESSAGES: dict[tuple[str, str], str] = {
    ("executed", ""): "AI API가 검수 초안을 만들었습니다. 사람은 표시된 위험 구간을 최종 확인하면 됩니다.",
    ("planned", ""): "AI API 실행 대상이 준비되었습니다. 전처리 흐름에서 곧 검수 초안을 생성합니다.",
    ("api_configuration_needed", ""): "AI 검수 대상은 골랐지만 API 키나 모델 설정이 없어 초안 생성은 아직 실행되지 않았습니다.",
    ("api_configuration_needed", "openai_api_key_missing"): "OPENAI_API_KEY를 설정하면 AI 검수 초안이 전처리 중 자동 생성됩니다.",
    ("api_configuration_needed", "azure_openai_endpoint_missing"): "Azure OpenAI 엔드포인트를 입력해야 AI 검수를 실행할 수 있습니다.",
    ("api_configuration_needed", "azure_openai_api_key_missing"): "Azure OpenAI API 키를 입력해야 AI 검수를 실행할 수 있습니다.",
    ("api_configuration_needed", "anthropic_api_key_missing"): "Anthropic API 키를 입력해야 AI 검수를 실행할 수 있습니다.",
    ("api_configuration_needed", "openai_compatible_base_url_missing"): "OpenAI 호환 API 주소를 입력해야 AI 검수를 실행할 수 있습니다.",
    ("api_configuration_needed", "agent_review_provider_not_supported"): "지원하는 AI 공급자를 다시 선택하세요.",
    ("api_configuration_needed", "agent_review_model_missing"): "AGENT_REVIEW_MODEL을 설정하면 AI 검수 초안이 전처리 중 자동 생성됩니다.",
    ("api_configuration_needed", "agent_review_api_disabled"): "AI 검수 기능이 꺼져 있어 외부 API 호출 없이 로컬 파서 결과와 사람 검수로 진행합니다.",
    ("skipped", "quality_gate_clean"): "품질 검사가 깨끗해 AI가 추가로 볼 부분이 없었습니다.",
    ("skipped", "no_review_candidates"): "AI가 따로 짚어야 할 의심 구간이 없었습니다.",
    ("skipped", "review_candidates_cached"): "같은 내용을 이전에 이미 AI가 검토해 그 결과를 재사용했습니다.",
    ("skipped", "review_budget_exhausted"): "확인이 필요한 부분이 예산 한도를 넘어, 일부만 검토 대상으로 올랐습니다.",
}


def _render_pipeline_stages(active: int) -> None:
    """파서 초안 → AI 검수 → 휴먼 승인 진행 띠. active는 1~3(현재 단계)."""
    cells: list[str] = []
    for index, (title, desc) in enumerate(PIPELINE_STAGES, start=1):
        state = "done" if index < active else ("active" if index == active else "")
        cells.append(
            f'<div class="rr-stage {state}">'
            f'<div class="rr-stage-k">{index}단계</div>'
            f'<div class="rr-stage-t">{title}</div>'
            f'<div class="rr-stage-d">{desc}</div>'
            "</div>"
        )
    strip = '<div class="rr-stage-arrow">→</div>'.join(cells)
    st.markdown(f'<div class="rr-stages">{strip}</div>', unsafe_allow_html=True)


def _ai_review_status_text(agent_review_summary: dict | None) -> tuple[str, str, bool]:
    """AI 검수 요약 → (태그 라벨, 안내 문구, 실행 완료 여부)."""
    summary = agent_review_summary if isinstance(agent_review_summary, dict) else {}
    status = str(summary.get("status") or "").strip()
    skip_reason = str(summary.get("skip_reason") or "").strip()
    executed = status == "executed"
    message = AI_REVIEW_STATUS_MESSAGES.get((status, skip_reason))
    if message is None:
        message = AI_REVIEW_STATUS_MESSAGES.get((status, ""))
    if message is None:
        message = "AI 검수 단계가 실행됐습니다. 자세한 내용은 아래 상세를 확인하세요."
    tag = "AI 검수 초안 완료" if executed else "AI 검수 준비/설정 확인"
    return tag, message, executed


AI_REVIEW_REASON_LABELS = {
    "chunk_warnings": ("청크 경고 확인", "중간", "청크 경고가 실제 원문 오류인지 확인하고 필요한 경우 전처리 결과를 보정합니다."),
    "replacement_character": ("문자 깨짐 가능성", "높음", "깨진 문자나 인코딩 오류가 있으면 원문과 대조해 수정합니다."),
    "table_extraction_failed": ("표 추출 실패 가능성", "높음", "원본 표와 전처리 표를 대조하고 Kordoc 표 결과를 우선 적용합니다."),
    "table_like_without_cell_rows": ("표 행·열 누락 가능성", "높음", "표처럼 보이는 내용이 행·열 구조로 보존됐는지 확인합니다."),
    "kordoc_table_match_review": ("Kordoc 표 매칭 확인", "중간", "Kordoc 표와 기존 파서 표가 같은 표를 가리키는지 확인합니다."),
    "kordoc_table_structure_review": ("Kordoc 표 구조 확인", "중간", "Kordoc이 보존한 셀/열 정보를 기준으로 표 구조를 확인합니다."),
    "kordoc_nested_table_review": ("중첩 표 확인", "중간", "표 안의 표나 병합 셀 때문에 의미가 빠지지 않았는지 확인합니다."),
    "parser_uncertainty": ("파서 불확실성 확인", "높음", "원문과 비교해 누락·합침·분리 오류가 없는지 확인합니다."),
    "hwp_parser_ai_review_required": ("HWP 추출 방식 확인", "중간", "HWP 레거시 추출 결과와 Kordoc/전처리 결과를 비교합니다."),
    "hwpx_parser_review_flag": ("HWPX 구조 경고 확인", "중간", "HWPX 표·각주·캡션 구조가 원문과 맞는지 확인합니다."),
    "hwpx_complex_structure": ("복잡 구조 확인", "중간", "중첩 표·병합 셀 같은 복잡 구조가 검색 본문에서 보존됐는지 확인합니다."),
    "document_inventory_boundary_review": ("문서 경계 확인", "중간", "조문·별표·부칙 경계가 잘못 합쳐지거나 빠지지 않았는지 확인합니다."),
}


def _approval_tab_badge(confirmed: bool) -> str:
    return "✅ 확인함" if confirmed else "⬜ 미확인"


def _approval_chunk_state_key(document_id: str, chunk_id: str, name: str) -> str:
    return f"approval:{document_id}:{chunk_id}:{name}"


def _approval_status(chunk) -> str:
    return str(getattr(chunk, "approval_status", "") or "draft").strip().lower() or "draft"


def _is_chunk_pending_approval(chunk) -> bool:
    return _approval_status(chunk) in APPROVABLE_CHUNK_STATUSES


def _approval_chunk_location(chunk) -> str:
    metadata = chunk.metadata or {}
    return str(
        metadata.get("hierarchy_path")
        or metadata.get("article_no")
        or metadata.get("appendix_label")
        or chunk.chunk_type
        or ""
    )


def _approval_ai_review_items(chunk, review_reasons: list[str], agent_review_summary: dict | None) -> list[dict[str, object]]:
    summary = agent_review_summary if isinstance(agent_review_summary, dict) else {}
    candidate_reasons: list[str] = []
    for item in (summary.get("selected_candidates") or summary.get("candidates") or []):
        if not isinstance(item, dict) or str(item.get("chunk_id") or "") != str(chunk.chunk_id):
            continue
        for reason in item.get("reasons") or []:
            if str(reason).strip():
                candidate_reasons.append(str(reason).strip())
    if not candidate_reasons:
        candidate_reasons = [str(reason).strip() for reason in review_reasons if str(reason).strip()]
    if not candidate_reasons and chunk.warnings:
        candidate_reasons = ["chunk_warnings"]

    items: list[dict[str, object]] = []
    for index, reason in enumerate(dict.fromkeys(candidate_reasons), start=1):
        title, severity, suggestion = AI_REVIEW_REASON_LABELS.get(
            reason,
            ("AI 검수 항목 확인", "중간", f"{reason} 항목을 원문과 비교해 반영 여부를 결정합니다."),
        )
        items.append(
            {
                "item_id": f"{chunk.chunk_id}:{reason}:{index}",
                "reason": reason,
                "title": title,
                "severity": severity,
                "location": f"{chunk.chunk_id} · {_approval_chunk_location(chunk)}",
                "suggestion": suggestion,
            }
        )
    return items


def _approval_set_bulk_ai_decisions(
    *,
    document_id: str,
    chunks: list[object],
    review_attention: dict,
    agent_review_summary: dict | None,
    decision: str,
    only_remaining: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    if decision not in {"reflect", "skip"}:
        raise ValueError("decision must be reflect or skip.")
    changed_chunk_count = 0
    changed_item_count = 0
    total = len(chunks)
    for position, chunk in enumerate(chunks, start=1):
        if progress_callback is not None:
            progress_callback(position, total)
        if not _is_chunk_pending_approval(chunk):
            continue
        chunk_id = str(getattr(chunk, "chunk_id", "") or "")
        if not chunk_id:
            continue
        review_reasons = review_attention.get(chunk_id) or chunk_review_attention_reasons(chunk)
        review_items = _approval_ai_review_items(chunk, review_reasons, agent_review_summary)
        if not review_items:
            continue
        ai_decisions_key = _approval_chunk_state_key(document_id, chunk_id, "ai_decisions")
        ai_logged_key = _approval_chunk_state_key(document_id, chunk_id, "ai_logged")
        existing_decisions = {
            str(item_id): str(existing_decision)
            for item_id, existing_decision in dict(st.session_state.get(ai_decisions_key) or {}).items()
            if str(existing_decision) in {"reflect", "skip"}
        }
        item_ids = [
            str(item["item_id"])
            for item in review_items
            if str(item.get("item_id") or "").strip()
        ]
        pending_item_ids = [item_id for item_id in item_ids if item_id not in existing_decisions]
        if only_remaining and not pending_item_ids:
            continue
        if only_remaining:
            st.session_state[ai_decisions_key] = {
                **existing_decisions,
                **{item_id: decision for item_id in pending_item_ids},
            }
            changed_item_count += len(pending_item_ids)
        else:
            st.session_state[ai_decisions_key] = {item_id: decision for item_id in item_ids}
            changed_item_count += len(item_ids)
        st.session_state[ai_logged_key] = False
        changed_chunk_count += 1
    return {"chunk_count": changed_chunk_count, "item_count": changed_item_count}


def _approval_set_bulk_human_confirmations(
    *,
    document_id: str,
    chunks: list[object],
    confirmed: bool = True,
    only_remaining: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    changed_chunk_count = 0
    total = len(chunks)
    for position, chunk in enumerate(chunks, start=1):
        if progress_callback is not None:
            progress_callback(position, total)
        if not _is_chunk_pending_approval(chunk):
            continue
        chunk_id = str(getattr(chunk, "chunk_id", "") or "")
        if not chunk_id:
            continue
        human_confirmed_key = _approval_chunk_state_key(document_id, chunk_id, "human_confirmed")
        human_confirmed_widget_key = _approval_chunk_state_key(document_id, chunk_id, "human_confirmed_widget")
        human_logged_key = _approval_chunk_state_key(document_id, chunk_id, "human_logged")
        if only_remaining and bool(st.session_state.get(human_confirmed_key)):
            continue
        st.session_state[human_confirmed_key] = bool(confirmed)
        # Streamlit removes state for widgets that are not rendered after the
        # operator opens another chunk.  Keep the approval decision in the
        # durable key above and let each checkbox be rebuilt from it.
        st.session_state.pop(human_confirmed_widget_key, None)
        st.session_state[human_logged_key] = False
        changed_chunk_count += 1
    return {"chunk_count": changed_chunk_count}


def _approval_sync_human_confirmation_from_widget(
    *,
    human_confirmed_key: str,
    human_confirmed_widget_key: str,
) -> None:
    """Copy the visible checkbox value into durable approval state."""
    st.session_state[human_confirmed_key] = bool(st.session_state.get(human_confirmed_widget_key))


def _approval_chunk_review_state_from_session(
    *,
    document_id: str,
    chunk: object,
    review_attention: dict,
    agent_review_summary: dict | None,
) -> dict[str, object]:
    chunk_id = str(getattr(chunk, "chunk_id", "") or "")
    review_reasons = review_attention.get(chunk_id) or chunk_review_attention_reasons(chunk)
    review_items = _approval_ai_review_items(chunk, review_reasons, agent_review_summary)
    item_ids = [str(item["item_id"]) for item in review_items]
    ai_decisions_key = _approval_chunk_state_key(document_id, chunk_id, "ai_decisions")
    human_confirmed_key = _approval_chunk_state_key(document_id, chunk_id, "human_confirmed")
    ai_decisions = {
        str(item_id): str(decision)
        for item_id, decision in dict(st.session_state.get(ai_decisions_key) or {}).items()
        if str(decision) in {"reflect", "skip"}
    }
    human_confirmed = bool(st.session_state.get(human_confirmed_key))
    state = approval_review_completion_state(item_ids, ai_decisions, human_confirmed=human_confirmed)
    edited_text = _approval_edited_text_from_session(document_id, chunk)
    if not edited_text.strip():
        state = {**state, "approve_enabled": False}
    return {
        "chunk": chunk,
        "chunk_id": chunk_id,
        "review_items": review_items,
        "item_ids": item_ids,
        "ai_decisions": ai_decisions,
        "human_confirmed": human_confirmed,
        "edited_text": edited_text,
        "state": state,
    }


def _approval_source_file_path(document) -> Path:
    return DocumentService(settings=settings, repository=repository).path_for(document)


def _approval_source_context(document, chunk) -> dict[str, object]:
    metadata = chunk.metadata or {}
    file_type = str(document.file_type or Path(document.filename).suffix.lstrip(".") or "").lower()
    source_page = metadata.get("source_page") or chunk.source_page_start
    return {
        "file_type": file_type,
        "document_id": document.document_id,
        "filename": document.filename,
        "source_path": _approval_source_file_path(document),
        "source_page": source_page,
        "source_bbox": metadata.get("source_bbox") or metadata.get("bbox"),
        "raw_text": str(metadata.get("raw_text") or chunk.text or "").strip(),
        "table_source": str(metadata.get("table_source") or metadata.get("primary_parser_table_source") or ""),
        "kordoc_table_promoted": bool(metadata.get("kordoc_table_promoted")),
    }


def _approval_kordoc_raw_rows(chunk) -> list[str]:
    metadata = chunk.metadata or {}
    rows = metadata.get("table_cell_rows") or []
    raw_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("raw") or " | ".join(str(cell) for cell in row.get("cells") or [])).strip()
        if raw:
            raw_rows.append(raw)
    return raw_rows[:80]


def _approval_processed_preview_text(chunk, review_items: list[dict[str, object]], ai_decisions: dict[str, str]) -> str:
    metadata = chunk.metadata or {}
    base_text = str(chunk.text or "").strip()
    table_markdown = str(metadata.get("table_markdown") or "").strip()
    if table_markdown and table_markdown not in base_text:
        base_text = f"{base_text}\n\n[표]\n{table_markdown}".strip()
    return apply_ai_review_decisions_to_preview_text(base_text, review_items, ai_decisions)


def _approval_edited_text_key(document_id: str, chunk_id: str) -> str:
    return _approval_chunk_state_key(document_id, chunk_id, "edited_text")


def _approval_edited_text_widget_key(document_id: str, chunk_id: str) -> str:
    return _approval_chunk_state_key(document_id, chunk_id, "edited_text_widget")


def _approval_edited_text_from_session(document_id: str, chunk: object) -> str:
    key = _approval_edited_text_key(document_id, str(getattr(chunk, "chunk_id", "") or ""))
    if key not in st.session_state:
        st.session_state[key] = str(getattr(chunk, "text", "") or "")
    return str(st.session_state.get(key) or "")


def _approval_sync_edited_text_from_widget(*, edited_text_key: str, widget_key: str) -> None:
    st.session_state[edited_text_key] = str(st.session_state.get(widget_key) or "")


def _approval_save_text_edits(
    *,
    document_id: str,
    chunks: list[object],
    entries: list[dict[str, object]],
    target_repository: JsonRepository,
) -> int:
    """Persist operator edits before approval evidence and content hashes are built."""

    changed = 0
    target_ids = {str(entry.get("chunk_id") or "") for entry in entries}
    for chunk in chunks:
        chunk_id = str(getattr(chunk, "chunk_id", "") or "")
        if chunk_id not in target_ids:
            continue
        edited_text = _approval_edited_text_from_session(document_id, chunk).strip()
        original_text = str(getattr(chunk, "text", "") or "")
        if not edited_text or edited_text == original_text:
            continue
        original_sha256 = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
        chunk.text = edited_text
        chunk.normalized_text = edited_text
        chunk.retrieval_text = edited_text
        chunk.metadata = {
            **dict(getattr(chunk, "metadata", {}) or {}),
            "human_review_edited": True,
            "human_review_original_sha256": original_sha256,
        }
        changed += 1
    if changed:
        target_repository.save_chunks(document_id, chunks)
    return changed


def _render_pdf_source_preview(source_context: dict[str, object]) -> None:
    source_path = source_context.get("source_path")
    page_value = source_context.get("source_page")
    bbox = source_context.get("source_bbox")
    if isinstance(source_path, Path) and source_path.is_file() and page_value:
        try:
            import fitz  # type: ignore

            page_number = max(1, int(page_value))
            with fitz.open(source_path) as pdf:
                page = pdf.load_page(min(page_number - 1, pdf.page_count - 1))
                if isinstance(bbox, list) and len(bbox) == 4:
                    page.draw_rect(fitz.Rect([float(value) for value in bbox]), color=(0.9, 0.1, 0.1), width=1.5)
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                st.image(pix.tobytes("png"), caption=f"PDF 원본 {page_number}쪽")
                return
        except Exception as exc:
            st.caption(f"PDF 페이지 이미지를 만들 수 없어 추출 원문으로 대체합니다: {exc}")
    raw_text = str(source_context.get("raw_text") or "")
    st.code(raw_text or "저장된 PDF 원문 텍스트가 없습니다.", language="text")


def _render_original_source_preview(document, chunk) -> None:
    source_context = _approval_source_context(document, chunk)
    file_type = str(source_context["file_type"])
    metadata = chunk.metadata or {}
    if file_type == "pdf":
        st.markdown("**원본 규정 (PDF 페이지)**")
        _render_pdf_source_preview(source_context)
        return
    if metadata.get("table_source") == "kordoc" or metadata.get("kordoc_table_promoted"):
        st.markdown("**원본 규정 (Kordoc 표 원문 셀)**")
        raw_rows = _approval_kordoc_raw_rows(chunk)
        st.code("\n".join(raw_rows) or str(source_context.get("raw_text") or ""), language="text")
        return
    st.markdown(f"**원본 규정 ({file_type.upper()} 추출 원문)**")
    st.code(str(source_context.get("raw_text") or "저장된 원문 텍스트가 없습니다."), language="text")


def _render_processed_result_preview(chunk, processed_text: str) -> None:
    metadata = chunk.metadata or {}
    st.markdown("**수정 후 전처리 결과**")
    if metadata.get("table_source") == "kordoc" or metadata.get("kordoc_table_promoted"):
        st.caption("◆ Kordoc 표 · 열 위치 보존")
    table_rows = metadata.get("table_cell_rows") or []
    if table_rows:
        preview_rows = []
        for row in table_rows[:100]:
            if isinstance(row, dict):
                preview_rows.append({"행": row.get("row_index"), "셀": " | ".join(str(cell) for cell in row.get("cells") or [])})
        if preview_rows:
            st.dataframe(pd.DataFrame(preview_rows), width="stretch", hide_index=True)
    st.code(processed_text or "전처리 결과 본문이 없습니다.", language="text")


def _approval_audit_preview_entry(message: str) -> dict[str, str]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "message": message,
    }


REGISTRY_STATE_KEY = "institution_profile_registry_bytes"
REGISTRY_SOURCE_STATE_KEY = "institution_profile_registry_source"
QUALITY_PROFILE_STATE_KEY = "quality_profile_config_bytes"
QUALITY_PROFILE_SOURCE_STATE_KEY = "quality_profile_config_source"
SELECTED_INSTITUTION_PROFILE_KEY = "selected_institution_profile_id"
OPERATOR_PROJECT_NAME_KEY = "operator_project_name"
OPERATOR_PROJECT_NAME_PENDING_KEY = "operator_project_name_pending"
OPERATOR_PROJECT_DIRECTORY_KEY = "operator_project_directory"
OPERATOR_PROJECT_CHECKPOINT_VERSION = 1
OPEN_OPERATOR_PROJECT_DIALOG_KEY = "open_operator_project_dialog_page"
OPEN_API_KEY_DIALOG_KEY = "open_api_key_dialog"
PENDING_INSTITUTION_DELETE_KEY = "pending_institution_profile_delete"
WORKFLOW_TRANSITION_STATE_KEY = "workflow_transition_state"
WORKFLOW_DOCUMENT_IDS_KEY = "workflow_document_ids"
WORKFLOW_SELECTED_DOCUMENT_IDS_KEY = "workflow_selected_document_ids"
WORKFLOW_MCP_GATE_CACHE_KEY = "workflow_mcp_gate_cache"
DOCUMENT_CONTEXT_CACHE_KEY = "document_context_cache"
KORDOC_REPROCESS_NOTICE_KEY = "kordoc_reprocess_notice"
KORDOC_AUTO_REPROCESS_ATTEMPT_PREFIX = "kordoc_auto_reprocess_attempted"
DOCUMENT_CONTEXT_NAV_PAGES = {NAV_HOME, NAV_RESULTS, NAV_APPROVAL, NAV_MCP}


def _queue_workflow_navigation(page: str, *, label: str | None = None) -> None:
    document_id = str(st.session_state.get("document_id") or "").strip()
    if document_id and page in DOCUMENT_CONTEXT_NAV_PAGES:
        st.session_state[WORKFLOW_TRANSITION_STATE_KEY] = {
            "label": label or page,
            "target": page,
            "document_id": document_id,
        }
    else:
        st.session_state["_nav_target"] = page


def _go(page: str) -> None:
    _queue_workflow_navigation(page)


@_streamlit_dialog("다음 단계로 이동 중", width="small", on_dismiss="ignore")
def _render_workflow_transition_dialog() -> None:
    transition = dict(st.session_state.get(WORKFLOW_TRANSITION_STATE_KEY) or {})
    target = str(transition.get("target") or NAV_HOME)
    label = str(transition.get("label") or target)
    document_id = str(transition.get("document_id") or st.session_state.get("document_id") or "").strip()
    progress = st.progress(3, text=f"{label} 준비 · 3%")
    message = st.empty()
    message.caption("현재 작업 상태를 확인하고 있습니다. 창을 닫지 마세요.")
    try:
        if document_id and target in DOCUMENT_CONTEXT_NAV_PAGES:
            selected_profile_id = _selected_institution_profile_id()
            result_path = repository._result_path(document_id, "chunks")
            try:
                result_mb = result_path.stat().st_size / (1024 * 1024)
            except OSError:
                result_mb = 0.0
            estimated_seconds = max(8.0, min(900.0, 8.0 + (result_mb * 0.18)))
            loaded_context = _run_background_operation_with_progress(
                lambda report: _load_document_context_with_progress(
                    document_id,
                    selected_profile_id=selected_profile_id,
                    progress_callback=report,
                ),
                progress_bar=progress,
                detail_box=message,
                start_percent=5,
                end_percent=96,
                label="대량 규정 결과 불러오기",
                estimated_seconds=estimated_seconds,
            )
            _store_document_context_cache(document_id, loaded_context)
        else:
            progress.progress(55, text=f"{label} · 화면 구성 55%")
            message.caption("다음 화면을 구성하고 있습니다.")
            time.sleep(0.15)
        progress.progress(100, text=f"{label} · 준비 완료 100%")
        message.caption("준비됐습니다. 다음 화면으로 이동합니다.")
        time.sleep(0.2)
        st.session_state.pop(WORKFLOW_TRANSITION_STATE_KEY, None)
        st.session_state["_nav_target"] = target
        st.rerun()
    except Exception as exc:
        st.session_state.pop(WORKFLOW_TRANSITION_STATE_KEY, None)
        st.error(f"다음 화면을 준비하지 못했습니다: {exc}")


def _render_workflow_next_button(
    label: str,
    target: str,
    *,
    key: str,
    disabled: bool = False,
    width: str = "stretch",
) -> None:
    if st.button(label, type="primary", key=key, disabled=disabled, width=width):
        _queue_workflow_navigation(target, label=label)
        st.rerun()


def _go_primary_nav() -> None:
    _queue_workflow_navigation(st.session_state.get("primary_nav_page", NAV_HOME))


def _selected_institution_profile_id() -> str:
    return str(st.session_state.get(SELECTED_INSTITUTION_PROFILE_KEY) or "").strip().lower()


def _select_institution_profile(profile_id: str) -> None:
    """Set the current institution context and discard document-local navigation."""
    selected = str(profile_id or "").strip().lower()
    st.session_state[SELECTED_INSTITUTION_PROFILE_KEY] = selected
    st.session_state.pop("document_id", None)
    st.session_state.pop(WORKFLOW_DOCUMENT_IDS_KEY, None)
    st.session_state.pop(WORKFLOW_SELECTED_DOCUMENT_IDS_KEY, None)
    st.session_state.pop(WORKFLOW_MCP_GATE_CACHE_KEY, None)
    st.session_state.pop(DOCUMENT_CONTEXT_CACHE_KEY, None)
    st.session_state.pop("unreviewed_preview_requested", None)
    st.session_state.pop("_nav_target", None)
    st.session_state["nav_page"] = NAV_HOME


def _document_belongs_to_institution_profile(document: object, profile_id: str) -> bool:
    selected_profile_id = str(profile_id or "").strip().lower()
    if not selected_profile_id or not institution_registry:
        return False
    profile = institution_registry.profiles.get(selected_profile_id)
    if profile is None:
        return False
    document_profile_id = str(getattr(document, "profile_id", "") or "").strip().lower()
    if document_profile_id:
        return document_profile_id == selected_profile_id
    institution_names = {
        str(value or "").strip()
        for value in (profile.institution_name, profile.display_name)
        if str(value or "").strip()
    }
    return str(getattr(document, "institution_name", "") or "").strip() in institution_names


def _operator_projects_dir(profile_id: str | None = None) -> Path:
    selected_profile_id = str(profile_id or _selected_institution_profile_id()).strip().lower()
    if not selected_profile_id:
        raise ValueError("기관을 먼저 선택하세요.")
    profile_digest = hashlib.sha256(selected_profile_id.encode("utf-8")).hexdigest()[:16]
    return Path(settings.data_dir) / "operator_projects" / f"institution-{profile_digest}"


def _operator_project_path(
    project_name: str,
    profile_id: str | None = None,
    projects_dir: Path | None = None,
) -> Path:
    cleaned_project_name = str(project_name or "").strip()
    if not cleaned_project_name:
        raise ValueError("프로젝트 이름을 입력하세요. 규정명이 아니라 작업을 구분할 이름입니다.")
    project_digest = hashlib.sha256(cleaned_project_name.casefold().encode("utf-8")).hexdigest()[:20]
    target_dir = Path(projects_dir).expanduser().resolve() if projects_dir is not None else _operator_projects_dir(profile_id)
    return target_dir / f"project-{project_digest}.json"


def _operator_project_session_values(document_id: str) -> dict[str, object]:
    """Keep JSON-safe document review state without persisting API keys or uploads."""
    exact_keys = {
        OFFICIAL_RAG_MCP_REVIEW_REQUIRED_KEY,
        "unreviewed_preview_requested",
        WORKFLOW_DOCUMENT_IDS_KEY,
        WORKFLOW_SELECTED_DOCUMENT_IDS_KEY,
    }
    blocked_fragments = ("api_key", "token", "secret", "password", "upload")
    saved: dict[str, object] = {}
    for raw_key in list(st.session_state):
        key = str(raw_key)
        if any(fragment in key.casefold() for fragment in blocked_fragments):
            continue
        if key.startswith("workflow-document-selected-"):
            continue
        if key.startswith(("run-", "write-", "select-", "open-", "load-", "save-", "index-", "reindex-")):
            continue
        if key not in exact_keys and (not document_id or document_id not in key):
            continue
        value = st.session_state.get(raw_key)
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        saved[key] = value
    return saved


def _save_operator_project(project_name: str, page: str, projects_dir: Path | None = None) -> Path:
    cleaned_project_name = str(project_name or "").strip()
    project_path = _operator_project_path(cleaned_project_name, projects_dir=projects_dir)
    project_path.parent.mkdir(parents=True, exist_ok=True)
    document_id = str(st.session_state.get("document_id") or "").strip()
    payload = {
        "report_type": "streamlit_operator_project_checkpoint",
        "schema_version": OPERATOR_PROJECT_CHECKPOINT_VERSION,
        "project_name": cleaned_project_name,
        "institution_profile_id": _selected_institution_profile_id(),
        "document_id": document_id,
        "page": page if page in PRIMARY_NAV_PAGES else NAV_HOME,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_values": _operator_project_session_values(document_id),
    }
    temporary_path = project_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(project_path)
    return project_path


def _list_operator_projects(projects_dir: Path | None = None) -> list[dict[str, object]]:
    try:
        target_dir = Path(projects_dir).expanduser().resolve() if projects_dir is not None else _operator_projects_dir()
    except ValueError:
        return []
    projects: list[dict[str, object]] = []
    for project_path in target_dir.glob("project-*.json") if target_dir.exists() else []:
        try:
            payload = json.loads(project_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("report_type") != "streamlit_operator_project_checkpoint":
            continue
        if str(payload.get("institution_profile_id") or "").strip().lower() != _selected_institution_profile_id():
            continue
        payload["_path"] = str(project_path)
        projects.append(payload)
    return sorted(projects, key=lambda item: str(item.get("saved_at") or ""), reverse=True)


def _load_operator_project(project_path_text: str, projects_dir: Path | None = None) -> dict[str, object]:
    target_dir = (
        Path(projects_dir).expanduser().resolve()
        if projects_dir is not None
        else _operator_projects_dir().resolve()
    )
    project_path = Path(project_path_text).expanduser().resolve()
    try:
        project_path.relative_to(target_dir)
    except ValueError as exc:
        raise ValueError("선택한 프로젝트 파일이 현재 기관의 저장 폴더 밖에 있습니다.") from exc
    payload = json.loads(project_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("report_type") != "streamlit_operator_project_checkpoint":
        raise ValueError("올바른 프로젝트 저장 파일이 아닙니다.")
    if str(payload.get("institution_profile_id") or "").strip().lower() != _selected_institution_profile_id():
        raise ValueError("다른 기관에서 저장한 프로젝트는 이 기관 화면에서 불러올 수 없습니다.")
    document_id = str(payload.get("document_id") or "").strip()
    if document_id:
        project_document = repository.get_document(document_id)
        if project_document is None:
            raise ValueError("저장된 프로젝트의 문서 데이터를 찾을 수 없습니다.")
        if not _document_belongs_to_institution_profile(
            project_document,
            _selected_institution_profile_id(),
        ):
            raise ValueError("저장된 프로젝트의 문서가 현재 기관에 속하지 않아 불러올 수 없습니다.")
    for existing_key in list(st.session_state):
        if str(existing_key).startswith("workflow-document-selected-"):
            st.session_state.pop(existing_key, None)
    for key, value in dict(payload.get("session_values") or {}).items():
        session_key = str(key)
        # Streamlit forbids assigning session_state for button widgets after
        # they have been created.  Project checkpoints may contain those keys
        # from older versions, so restore durable data only.
        if session_key.startswith((
            "run-rag-chat-", "repair-mcp-source-metadata-", "select-mcp-bundle-dir-",
            "write-mcp-bundle-", "open-mcp-bundle-dir-", "index-", "reindex-",
            "open-", "load-", "save-", "select-",
        )):
            continue
        st.session_state[session_key] = value
    restored_document_ids = st.session_state.get(WORKFLOW_DOCUMENT_IDS_KEY)
    restored_selected_ids = {
        str(value or "").strip()
        for value in (st.session_state.get(WORKFLOW_SELECTED_DOCUMENT_IDS_KEY) or [])
        if str(value or "").strip()
    }
    if isinstance(restored_document_ids, list):
        for restored_document_id in restored_document_ids:
            normalized_document_id = str(restored_document_id or "").strip()
            if normalized_document_id:
                st.session_state[f"workflow-document-selected-{normalized_document_id}"] = (
                    normalized_document_id in restored_selected_ids
                )
    if document_id:
        st.session_state["document_id"] = document_id
    else:
        st.session_state.pop("document_id", None)
    page = str(payload.get("page") or NAV_HOME)
    if page not in PRIMARY_NAV_PAGES:
        page = NAV_HOME
    st.session_state[OPERATOR_PROJECT_NAME_PENDING_KEY] = str(payload.get("project_name") or "")
    st.session_state["_nav_target"] = page
    return payload


def _dismiss_operator_project_dialog() -> None:
    st.session_state.pop(OPEN_OPERATOR_PROJECT_DIALOG_KEY, None)


@_streamlit_dialog(
    "프로젝트 저장·불러오기",
    width="large",
    on_dismiss=_dismiss_operator_project_dialog,
)
def _render_operator_project_dialog(page: str) -> None:
    """Open project checkpoint save/load controls in a modal dialog."""
    control_key = hashlib.sha256(page.encode("utf-8")).hexdigest()[:10]
    if OPERATOR_PROJECT_NAME_PENDING_KEY in st.session_state:
        st.session_state[OPERATOR_PROJECT_NAME_KEY] = st.session_state.pop(OPERATOR_PROJECT_NAME_PENDING_KEY)
    if OPERATOR_PROJECT_DIRECTORY_KEY not in st.session_state:
        st.session_state[OPERATOR_PROJECT_DIRECTORY_KEY] = str(_operator_projects_dir().resolve())

    st.caption("규정명이 아니라 사람이 작업을 구분할 프로젝트 이름으로 저장합니다. API Key는 저장하지 않습니다.")
    project_name = st.text_input(
        "프로젝트 이름",
        key=OPERATOR_PROJECT_NAME_KEY,
        placeholder="예: 2026 인사규정 정비 작업",
    )
    project_directory = st.text_input(
        "저장 폴더 위치",
        key=OPERATOR_PROJECT_DIRECTORY_KEY,
    )
    picker_col, open_col = st.columns(2)
    with picker_col:
        st.button(
            "Windows 탐색기에서 저장 폴더 선택",
            key=f"select-project-directory-{control_key}",
            on_click=_select_windows_output_directory,
            args=(OPERATOR_PROJECT_DIRECTORY_KEY, project_directory),
            width="stretch",
        )
    with open_col:
        if st.button(
            "Windows 탐색기에서 현재 폴더 열기",
            key=f"open-project-directory-{control_key}",
            width="stretch",
        ):
            try:
                _open_directory_in_explorer(_resolve_operator_output_path(project_directory))
            except (OSError, ValueError) as exc:
                st.error(str(exc))
    picker_error = st.session_state.get(f"{OPERATOR_PROJECT_DIRECTORY_KEY}:picker_error")
    if picker_error:
        st.error(picker_error)

    try:
        projects_dir = _resolve_operator_output_path(project_directory)
    except ValueError as exc:
        projects_dir = None
        st.error(str(exc))

    if st.button(
        "💾 이 폴더에 프로젝트 저장",
        key=f"save-operator-project-{control_key}",
        type="primary",
        disabled=projects_dir is None,
        width="stretch",
    ):
        try:
            saved_path = _save_operator_project(project_name, page, projects_dir=projects_dir)
            st.success(f"프로젝트 '{str(project_name).strip()}'을 저장했습니다: {saved_path}")
        except (OSError, ValueError) as exc:
            st.error(str(exc))

    projects = _list_operator_projects(projects_dir=projects_dir) if projects_dir is not None else []
    project_path_by_option: dict[str, str] = {}
    for project in projects:
        project_path = str(project.get("_path") or "")
        if not project_path:
            continue
        project_option = f"{project.get('project_name')} · {project.get('saved_at')}"
        project_path_by_option[project_option] = project_path
    selected_project_option = st.selectbox(
        "저장한 프로젝트",
        options=[""] + list(project_path_by_option),
        key=f"load-operator-project-choice-{control_key}",
    )
    selected_project_path = project_path_by_option.get(selected_project_option, "")
    if st.button(
        "저장한 프로젝트 불러오기",
        key=f"load-operator-project-{control_key}",
        type="primary",
        disabled=not bool(selected_project_path) or projects_dir is None,
        width="stretch",
    ):
        try:
            with st.status("저장한 프로젝트 불러오는 중…", expanded=True) as load_status:
                load_progress = st.progress(0, text="프로젝트 파일 읽기 0%")
                load_progress.progress(30, text="프로젝트 파일 읽기 30%")
                time.sleep(0.15)
                loaded = _load_operator_project(selected_project_path, projects_dir=projects_dir)
                load_progress.progress(70, text="화면 상태 복원 70%")
                time.sleep(0.15)
                load_progress.progress(100, text="불러오기 완료 100%")
                time.sleep(0.25)
                load_status.update(label="저장한 프로젝트 불러오기 완료", state="complete")
            st.success(f"프로젝트 '{loaded.get('project_name')}'을 불러왔습니다.")
            st.session_state.pop(OPEN_OPERATOR_PROJECT_DIALOG_KEY, None)
            st.rerun()
        except (OSError, ValueError, TypeError) as exc:
            st.error(str(exc))


def _render_operator_project_controls(page: str) -> None:
    """Keep one save action at the upper-right of every main workflow screen."""
    control_key = hashlib.sha256(page.encode("utf-8")).hexdigest()[:10]
    if OPERATOR_PROJECT_NAME_PENDING_KEY in st.session_state:
        st.session_state[OPERATOR_PROJECT_NAME_KEY] = st.session_state.pop(OPERATOR_PROJECT_NAME_PENDING_KEY)
    save_spacer_col, save_button_col, load_button_col = st.columns([7, 1, 1], vertical_alignment="top")
    with save_spacer_col:
        st.caption(" ")
    with save_button_col:
        if st.button(
            "💾 저장하기",
            key=f"open-project-dialog-{control_key}",
            type="primary",
            width="stretch",
        ):
            st.session_state[OPEN_OPERATOR_PROJECT_DIALOG_KEY] = page
    with load_button_col:
        if st.button(
            "📂 불러오기",
            key=f"open-project-load-dialog-{control_key}",
            type="secondary",
            width="stretch",
        ):
            st.session_state[OPEN_OPERATOR_PROJECT_DIALOG_KEY] = page
    if st.session_state.get(OPEN_OPERATOR_PROJECT_DIALOG_KEY) == page:
        _render_operator_project_dialog(page)


def _profile_visible_to_local_tenant(profile) -> bool:
    assigned_tenant = str(getattr(profile, "tenant_id", "") or "").strip()
    if assigned_tenant:
        return assigned_tenant == _local_operator_tenant_id()
    return str(settings.app_env or "").strip().lower() in {"local", "dev", "development", "test"}


def _institution_profiles_storage_path(current_settings) -> str:
    """Use an explicit registry path, or a data-local default for local UI runs."""
    configured_path = str(current_settings.institution_profiles_path or "").strip()
    if configured_path:
        return configured_path
    if str(current_settings.app_env or "").strip().lower() in {"local", "dev", "development", "test"}:
        return str(Path(current_settings.data_dir) / "institution_profiles.json")
    return ""


def _quality_profiles_storage_path(current_settings) -> str:
    """Use an explicit quality path, or a data-local default for local UI runs."""
    configured_path = str(current_settings.quality_profiles_path or "").strip()
    if configured_path:
        return configured_path
    if str(current_settings.app_env or "").strip().lower() in {"local", "dev", "development", "test"}:
        return str(Path(current_settings.data_dir) / "quality_profiles.json")
    return ""


def _render_institution_registration_form(registry: InstitutionProfileRegistry) -> None:
    st.markdown("### 기관 등록")
    st.caption("기관명을 먼저 등록하면 해당 기관을 선택한 뒤 규정을 추가할 수 있습니다.")
    registry_path = _institution_profiles_storage_path(settings)
    if not registry_path:
        st.error("INSTITUTION_PROFILES_PATH가 설정되지 않아 기관을 저장할 수 없습니다.")
        return
    institution_name = st.text_input("기관명", placeholder="예: 한국공공기관")
    submitted = st.button("기관 생성", type="primary")
    if not submitted:
        return
    cleaned_institution_name = " ".join(
        unicodedata.normalize("NFKC", str(institution_name or "")).split()
    )
    if not cleaned_institution_name:
        st.error("기관명을 입력하세요.")
        return
    profile_digest = hashlib.sha256(cleaned_institution_name.casefold().encode("utf-8")).hexdigest()[:16]
    profile_id = f"institution-{profile_digest}"
    try:
        updated_registry = upsert_institution_profile(
            registry,
            profile_id,
            display_name=cleaned_institution_name,
            institution_name=cleaned_institution_name,
            tenant_id=_local_operator_tenant_id(),
            required_row_fields=["profile_id"],
            make_default=not registry.profiles,
        )
        save_institution_profile_registry(registry_path, updated_registry)
        st.session_state[REGISTRY_STATE_KEY] = institution_profile_registry_to_bytes(updated_registry)
        st.session_state[REGISTRY_SOURCE_STATE_KEY] = "local institution registration"
        _select_institution_profile(profile_id)
        st.success(f"'{cleaned_institution_name}' 기관을 생성했습니다.")
        st.rerun()
    except (OSError, ValueError) as exc:
        st.error(str(exc))


def _delete_registered_institution(registry: InstitutionProfileRegistry, profile_id: str) -> None:
    """Delete only an institution profile; regulation documents remain untouched."""
    registry_path = _institution_profiles_storage_path(settings)
    if not registry_path:
        raise ValueError("INSTITUTION_PROFILES_PATH가 설정되지 않아 기관을 삭제할 수 없습니다.")
    updated_registry = delete_institution_profile(registry, profile_id)
    save_institution_profile_registry(registry_path, updated_registry)
    st.session_state[REGISTRY_STATE_KEY] = institution_profile_registry_to_bytes(updated_registry)
    st.session_state[REGISTRY_SOURCE_STATE_KEY] = "local institution deletion"
    st.session_state.pop(PENDING_INSTITUTION_DELETE_KEY, None)
    if _selected_institution_profile_id() == str(profile_id or "").strip().lower():
        st.session_state.pop(SELECTED_INSTITUTION_PROFILE_KEY, None)
        st.session_state.pop("document_id", None)


def _page_institution_select(registry) -> None:
    """Require a deliberate institution choice before opening operator workspaces."""
    _render_hero("먼저 작업할 기관을 선택하세요. 이후 문서·승인·RAG 검색은 선택한 기관 범위로 관리됩니다.")
    st.markdown("## 기관 선택")
    st.caption("기관을 클릭하면 해당 기관의 규정 관리 화면으로 들어갑니다.")
    # The first screen only creates/selects an institution.  API settings are
    # available after entering an institution, where their scope is clear.
    st.session_state.pop(OPEN_API_KEY_DIALOG_KEY, None)

    # The institution name field must always be available on the first screen.
    # Existing profiles are shown below it, but they must not hide registration.
    _render_institution_registration_form(registry)

    profiles = sorted(
        (
            profile
            for profile in registry.profiles.values()
            if _profile_visible_to_local_tenant(profile)
        ),
        key=lambda profile: (profile.display_name or profile.profile_id).lower(),
    )
    if not profiles:
        st.warning("등록된 기관 프로필이 없습니다. 관리자 설정에서 기관을 먼저 등록하세요.")
        # Bare-mode imports used by helpers/tests swallow ``st.stop``; return
        # as well so an empty registry never reaches ``st.columns(0)``.
        return

    columns = st.columns(min(3, len(profiles)))
    for index, profile in enumerate(profiles):
        with columns[index % len(columns)]:
            institution_name = profile.institution_name or profile.display_name or profile.profile_id
            display_name = profile.display_name or institution_name
            st.markdown(
                f"""
                <div class="rr-institution-card">
                  <div class="rr-institution-kicker">기관 프로필</div>
                  <h3>{html.escape(display_name)}</h3>
                  <p>{html.escape(institution_name)}</p>
                  <small>프로필 ID: {html.escape(profile.profile_id)}</small>
                </div>
                """,
                unsafe_allow_html=True,
            )
            institution_action_col, institution_delete_col = st.columns([2, 1])
            with institution_action_col:
                if st.button(
                    "이 기관으로 시작",
                    key=f"select-institution-{profile.profile_id}",
                    type="primary",
                    width="stretch",
                    on_click=_select_institution_profile,
                    args=(profile.profile_id,),
                ):
                    st.rerun()
            with institution_delete_col:
                if st.button(
                    "기관 삭제",
                    key=f"delete-institution-{profile.profile_id}",
                    width="stretch",
                ):
                    st.session_state[PENDING_INSTITUTION_DELETE_KEY] = profile.profile_id
                    st.rerun()

            if st.session_state.get(PENDING_INSTITUTION_DELETE_KEY) == profile.profile_id:
                st.warning(
                    f"'{display_name}' 기관 프로필을 삭제합니다. "
                    "업로드한 규정·승인 데이터는 자동 삭제하지 않습니다."
                )
                confirm_delete_col, cancel_delete_col = st.columns(2)
                with confirm_delete_col:
                    if st.button(
                        "삭제 확인",
                        key=f"confirm-delete-institution-{profile.profile_id}",
                        type="primary",
                        width="stretch",
                    ):
                        try:
                            _delete_registered_institution(registry, profile.profile_id)
                            st.success(f"'{display_name}' 기관을 삭제했습니다.")
                            st.rerun()
                        except (OSError, ValueError) as exc:
                            st.error(str(exc))
                with cancel_delete_col:
                    if st.button(
                        "취소",
                        key=f"cancel-delete-institution-{profile.profile_id}",
                        width="stretch",
                    ):
                        st.session_state.pop(PENDING_INSTITUTION_DELETE_KEY, None)
                        st.rerun()


def _apply_operator_deep_link() -> None:
    """Allow local operators and smoke tests to reopen an existing document view."""
    try:
        query_params = st.query_params
    except Exception:
        return

    def _query_value(name: str) -> str:
        value = query_params.get(name, "")
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "").strip()

    query_document_id = _query_value("document_id")
    if query_document_id and repository.get_document(query_document_id) is not None:
        st.session_state["document_id"] = query_document_id
        query_chunk_id = _query_value("chunk_id")
        if query_chunk_id:
            chunk_ids = {str(chunk.chunk_id) for chunk in repository.get_chunks(query_document_id)}
            if query_chunk_id in chunk_ids:
                st.session_state[f"approval-compare-chunk-{query_document_id}"] = query_chunk_id

    query_nav = _query_value("nav").lower()
    nav_map = {
        "home": NAV_HOME,
        "preprocess": NAV_PREPROCESS,
        "results": NAV_RESULTS,
        "approval": NAV_APPROVAL,
        "connect": NAV_MCP,
        "mcp": NAV_MCP,
        "goldset": NAV_GOLDSET,
        "admin": NAV_ADMIN,
    }
    if query_nav in nav_map:
        st.session_state["nav_page"] = nav_map[query_nav]


def _apply_ai_connection_overrides() -> None:
    """세션에 저장된 AI 연결값을 Settings 런타임 오버라이드로 적용한다.

    저장된 값이 없으면 오버라이드를 비워 env 기반 기본 설정을 그대로 쓴다.
    스크립트 최상단에서 get_settings() 호출 전에 실행해야 한다.
    """

    overrides = st.session_state.get(AI_CONNECTION_STATE_KEY)
    if isinstance(overrides, dict) and overrides:
        set_runtime_settings_overrides(**overrides)
    else:
        set_runtime_settings_overrides()


def _blank_to_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _local_operator_tenant_id() -> str:
    tenant_id = str(settings.api_default_tenant_id or "default").strip()
    return tenant_id or "default"


def _uploaded_file_list(uploaded: object) -> list[object]:
    if not uploaded:
        return []
    if isinstance(uploaded, list):
        return [item for item in uploaded if item]
    return [uploaded]


def _uploaded_file_size(uploaded_file: object) -> int:
    size = getattr(uploaded_file, "size", None)
    if isinstance(size, int) and size >= 0:
        return size
    tell = getattr(uploaded_file, "tell", None)
    seek = getattr(uploaded_file, "seek", None)
    if not callable(tell) or not callable(seek):
        return 0
    position = tell()
    try:
        seek(0, 2)
        return int(tell())
    finally:
        seek(position)


def _pending_upload_dir(profile_id: str) -> Path:
    digest = hashlib.sha256(str(profile_id).strip().lower().encode("utf-8")).hexdigest()[:16]
    path = Path(settings.data_dir) / "pending_uploads" / f"institution-{digest}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pending_upload_paths(profile_id: str) -> list[Path]:
    if not str(profile_id or "").strip():
        return []
    return sorted(
        (path for path in _pending_upload_dir(profile_id).iterdir() if path.is_file() and not path.name.endswith(".tmp")),
        key=lambda path: path.name.casefold(),
    )


def _pending_upload_display_name(path: Path) -> str:
    marker = "__"
    return path.name.split(marker, 1)[1] if marker in path.name else path.name


def _persist_pending_upload(profile_id: str, uploaded_file: object) -> Path:
    directory = _pending_upload_dir(profile_id)
    filename = Path(str(getattr(uploaded_file, "name", "pending_upload"))).name or "pending_upload"
    source = uploaded_file
    seek = getattr(source, "seek", None)
    read = getattr(source, "read", None)
    if not callable(seek) or not callable(read):
        raise ValueError("업로드 파일을 임시 저장할 수 없습니다.")
    seek(0)
    digest = hashlib.sha256()
    temporary = directory / f".{filename}.{time.time_ns()}.tmp"
    try:
        with temporary.open("wb") as handle:
            while True:
                block = read(8 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                handle.write(block)
        target = directory / f"{digest.hexdigest()}__{filename}"
        if target.exists() and target.stat().st_size == temporary.stat().st_size:
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(target)
        return target
    finally:
        seek(0)
        temporary.unlink(missing_ok=True)


def _format_upload_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):,.1f}MB"


def _format_elapsed_seconds(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _heartbeat_label(tick: int) -> str:
    return "작업 중" + "." * ((tick % 3) + 1)


def _render_upload_file_progress(container, rows: list[dict[str, object]]) -> None:
    table_rows = []
    for row in rows:
        filename = html.escape(str(row.get("filename") or ""))
        status = html.escape(str(row.get("status") or "대기"))
        percent = max(0, min(100, int(row.get("percent") or 0)))
        table_rows.append(
            "<tr>"
            f"<td style='padding:6px 10px;word-break:break-all'>{filename}</td>"
            f"<td style='padding:6px 10px;white-space:nowrap'>{status}</td>"
            f"<td style='padding:6px 10px;text-align:right;white-space:nowrap'>{percent}%</td>"
            "</tr>"
        )
    container.markdown(
        "<table style='width:100%;border-collapse:collapse;font-size:0.9rem'>"
        "<thead><tr>"
        "<th style='text-align:left;padding:6px 10px'>파일명</th>"
        "<th style='text-align:left;padding:6px 10px'>상태</th>"
        "<th style='text-align:right;padding:6px 10px'>진행률</th>"
        "</tr></thead>"
        "<tbody>"
        + "".join(table_rows)
        + "</tbody></table>",
        unsafe_allow_html=True,
    )


def _render_selected_upload_files(uploaded_files: list[object]) -> None:
    rows = []
    for uploaded_file in uploaded_files:
        filename = html.escape(str(getattr(uploaded_file, "name", "업로드 파일")))
        size = html.escape(_format_upload_mb(_uploaded_file_size(uploaded_file)))
        rows.append(
            "<tr>"
            f"<td style='padding:6px 10px;word-break:break-all'>{filename}</td>"
            f"<td style='padding:6px 10px;text-align:right;white-space:nowrap'>{size}</td>"
            "<td style='padding:6px 10px;white-space:nowrap'>탑재됨</td>"
            "</tr>"
        )
    st.markdown(
        "<table style='width:100%;border-collapse:collapse;font-size:0.9rem;margin:.25rem 0 .8rem 0'>"
        "<thead><tr>"
        "<th style='text-align:left;padding:6px 10px'>파일명</th>"
        "<th style='text-align:right;padding:6px 10px'>용량</th>"
        "<th style='text-align:left;padding:6px 10px'>상태</th>"
        "</tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>",
        unsafe_allow_html=True,
    )


def _profile_label(profile_id: str, display_name: str) -> str:
    return f"{profile_id} - {display_name}" if display_name else profile_id


def _quality_report_to_markdown(report) -> str:
    data = report.model_dump(mode="json")
    lines = [
        "# 품질 보고서",
        "",
        f"- 통과 여부: {data.get('passed')}",
        f"- 점수: {data.get('score')}",
        f"- 청크 수: {data.get('chunk_count')}",
        f"- 이슈 수: {data.get('issue_count')}",
        "",
        "```json",
        json.dumps(data, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def _powershell_arg(value: str) -> str:
    if not value:
        return '""'
    if any(char.isspace() for char in value) or any(char in value for char in ['"', "'"]):
        return '"' + value.replace("`", "``").replace('"', '`"') + '"'
    return value


def _powershell_command(command: str, args: list[object] | tuple[object, ...] | None = None) -> str:
    parts = [command, *(str(arg) for arg in (args or []))]
    return " ".join(_powershell_arg(part) for part in parts)


def _build_mcp_http_url(*, host: str, port: int, public_url: str = "") -> str:
    """Build the client-facing Streamable HTTP /mcp URL shown in the operator UI."""
    cleaned_public_url = public_url.strip()
    if cleaned_public_url:
        from urllib.parse import urlsplit, urlunsplit

        candidate = (
            cleaned_public_url
            if "://" in cleaned_public_url
            else f"https://{cleaned_public_url}"
        )
        try:
            parsed = urlsplit(candidate)
            port_value = parsed.port
        except ValueError:
            return ""
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port_value is not None and not 1 <= port_value <= 65535)
        ):
            return ""
        path = parsed.path.rstrip("/")
        if not path:
            path = "/mcp"
        elif not path.endswith("/mcp"):
            path = f"{path}/mcp"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))

    client_host = host.strip() or "127.0.0.1"
    if client_host in {"0.0.0.0", "::"}:
        client_host = "127.0.0.1"
    if ":" in client_host and not client_host.startswith("["):
        client_host = f"[{client_host}]"
    return f"http://{client_host}:{int(port)}/mcp"


def _direct_python_mcp_config(payload: dict, *, tenant_storage_isolation: bool = False) -> dict:
    config = json.loads(json.dumps(payload, ensure_ascii=False))
    server_script = str((PROJECT_ROOT / "scripts" / "run_regulation_mcp.py").resolve())
    python_executable = sys.executable or "python"

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("command") == "reg-rag-mcp-server":
                args = [str(arg) for arg in (value.get("args") or [])]
                value["command"] = python_executable
                value["args"] = [server_script, *args]
                if not tenant_storage_isolation and "--flat-storage" not in value["args"]:
                    value["args"].append("--flat-storage")
                if "--no-warm-cache" not in value["args"]:
                    value["args"].append("--no-warm-cache")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(config)
    quickstart = config.get("quickstart") if isinstance(config, dict) else None
    copy_paste = quickstart.get("copy_paste") if isinstance(quickstart, dict) else None
    local_server = quickstart.get("run_local_stdio_server") if isinstance(quickstart, dict) else None
    if isinstance(copy_paste, dict) and isinstance(local_server, dict):
        copy_paste["run_local_stdio_server_ps"] = _powershell_command(
            str(local_server.get("command") or python_executable),
            [str(arg) for arg in (local_server.get("args") or [])],
        )
    return config


def _write_direct_python_quickstart_scripts(
    files: dict[str, str],
    *,
    server_name: str,
    claude_code_config: dict,
    stdio_command: str,
    stdio_args: list[str],
) -> None:
    claude_code_path = files.get("claude_code_stdio")
    if claude_code_path:
        config_json = json.dumps(claude_code_config, ensure_ascii=False, indent=2)
        Path(claude_code_path).write_text(
            "\n".join(
                [
                    '$ErrorActionPreference = "Stop"',
                    "$Config = @'",
                    config_json,
                    "'@",
                    _powershell_command("claude", ["mcp", "add-json", server_name, "$Config"]),
                    "",
                ]
            ),
            encoding="utf-8",
        )
    run_stdio_path = files.get("run_stdio")
    if run_stdio_path:
        Path(run_stdio_path).write_text(
            _powershell_command(stdio_command, stdio_args) + "\n",
            encoding="utf-8",
        )


def _mcp_connection_gate(index_status: dict | None, approved_count: int) -> dict[str, object]:
    vector_summary = (index_status or {}).get("vector_summary") or {}
    vector_consistency = (index_status or {}).get("vector_consistency") or {}
    mcp_visible_count = int(vector_summary.get("record_count") or 0)
    stale_count = int(vector_consistency.get("stale_count") or 0)
    indexing_status = str((index_status or {}).get("indexing_status") or "unknown")
    validation_error = (index_status or {}).get("validation_error")

    ready = (
        approved_count > 0
        and indexing_status == "indexed"
        and mcp_visible_count == approved_count
        and stale_count == 0
        and not validation_error
    )
    if ready:
        reason = "approved_chunks_indexed"
    elif approved_count <= 0:
        reason = "no_approved_chunks"
    elif indexing_status != "indexed":
        reason = "document_not_indexed"
    elif mcp_visible_count != approved_count:
        reason = "visible_record_count_mismatch"
    elif stale_count:
        reason = "stale_vector_records"
    elif validation_error:
        reason = "index_validation_error"
    else:
        reason = "not_ready"

    return {
        "ready": ready,
        "reason": reason,
        "approved_count": approved_count,
        "mcp_visible_count": mcp_visible_count,
        "indexing_status": indexing_status,
        "stale_count": stale_count,
        "validation_error": validation_error,
    }


def _workflow_mcp_gate_summary(document_ids: list[str], current_ctx: dict) -> dict[str, object]:
    normalized_ids = [str(document_id or "").strip() for document_id in document_ids if str(document_id or "").strip()]
    signature = tuple((document_id, _document_context_revision(document_id)) for document_id in normalized_ids)
    cached = st.session_state.get(WORKFLOW_MCP_GATE_CACHE_KEY)
    if isinstance(cached, dict) and cached.get("signature") == signature:
        return dict(cached["summary"])

    rows: list[dict[str, object]] = []
    all_ready = bool(normalized_ids)
    for document_id in normalized_ids:
        document = repository.get_document(document_id)
        if document_id == str(current_ctx.get("document_id") or ""):
            gate = dict(current_ctx.get("mcp_connection_gate") or {})
        elif document is None:
            gate = _mcp_connection_gate(None, 0)
        else:
            chunks = repository.get_chunks(document_id)
            approved_count = sum(1 for chunk in chunks if chunk.approval_status == "approved")
            tenant_id = str(getattr(document, "tenant_id", "") or _local_operator_tenant_id()).strip()
            auth = AuthContext(
                actor="streamlit-local-operator",
                tenant_id=tenant_id or _local_operator_tenant_id(),
                auth_mode="streamlit-local",
            )
            try:
                gate = _mcp_connection_gate(get_index_status(document_id, auth), approved_count)
            except Exception:
                gate = _mcp_connection_gate(None, approved_count)
        ready = bool(gate.get("ready"))
        all_ready = all_ready and ready
        rows.append(
            {
                "규정": _workflow_document_label(document) if document is not None else document_id,
                "승인 청크": int(gate.get("approved_count") or 0),
                "MCP 노출 기록": int(gate.get("mcp_visible_count") or 0),
                "상태": "준비 완료" if ready else str(gate.get("reason") or "확인 필요"),
            }
        )
    summary = {"ready": all_ready, "rows": rows}
    st.session_state[WORKFLOW_MCP_GATE_CACHE_KEY] = {"signature": signature, "summary": summary}
    return summary


def _missing_mcp_source_metadata(document: object) -> list[str]:
    return [
        field
        for field in MCP_REQUIRED_SOURCE_METADATA_FIELDS
        if getattr(document, field, None) in (None, "")
    ]


def _default_mcp_source_metadata(document: object, tenant_id: str) -> dict[str, str]:
    document_id = str(getattr(document, "document_id", "") or "document")
    safe_document_id = _safe_report_key(document_id)
    safe_tenant_id = _safe_report_key(tenant_id or "default")
    return {
        "institution_name": "Local Upload",
        "profile_id": f"local-{safe_tenant_id}",
        "source_system": "LOCAL_UPLOAD",
        "source_url": f"local-upload://{safe_document_id}",
    }


def _ensure_mcp_source_metadata(document: object, *, tenant_id: str, target_repository: JsonRepository) -> tuple[object, dict[str, str]]:
    missing = _missing_mcp_source_metadata(document)
    if not missing or not hasattr(document, "model_copy"):
        return document, {}
    defaults = _default_mcp_source_metadata(document, tenant_id)
    patch = {field: defaults[field] for field in missing if field in defaults}
    if not patch:
        return document, {}
    updated_document = document.model_copy(update=patch)
    target_repository.upsert_document(updated_document)
    return updated_document, patch


def _resolve_operator_artifact_path(raw_path: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError("검수 묶음 파일(JSON) 경로를 입력해 주세요.")
    path = Path(text)
    candidates = [path] if path.is_absolute() else [path, PROJECT_ROOT / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"검수 묶음 파일을 찾을 수 없습니다: {text}")


def _safe_relative_approval_artifact_path(path: Path, raw_path: str) -> str:
    text = str(raw_path or "").strip().replace("\\", "/")
    if text and not Path(text).is_absolute() and ".." not in text.split("/") and not text.startswith("/"):
        return text
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("승인 증빙 파일은 이 작업 폴더 안의 경로여야 합니다.") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_goldset_artifact_path(raw_path: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError("검수 기록 파일(CSV) 경로를 입력해 주세요.")
    path = Path(text)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _resolve_operator_output_path(raw_path: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError("출력 폴더 경로를 입력해 주세요.")
    path = Path(text)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _mcp_bundle_zip_output_path(bundle_dir: Path) -> Path:
    bundle_name = bundle_dir.name.strip() or "mcp_connection_bundle"
    return bundle_dir / f"{bundle_name}.zip"


def _normalize_mcp_server_name(value: str) -> str:
    normalized: list[str] = []
    for char in str(value or "").strip().lower():
        if char.isascii() and char.isalnum():
            normalized.append(char)
        elif char in {"-", "_", "."}:
            normalized.append(char)
        elif normalized and normalized[-1] != "_":
            normalized.append("_")
    return "".join(normalized).strip("-_.")


def _default_mcp_server_name(bundle_dir: Path, profile_id: str) -> str:
    bundle_name = _normalize_mcp_server_name(bundle_dir.name)
    if bundle_name not in {"", "bundle", "mcp_bundle", "mcp_connection_bundle"}:
        return bundle_name
    profile_name = _normalize_mcp_server_name(profile_id)
    return f"{profile_name}_mcp" if profile_name else "local_regulation_mcp"


def _ensure_mcp_output_directory_writable(bundle_dir: Path) -> None:
    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        probe_path = bundle_dir / f".mcp-write-test-{time.time_ns()}.tmp"
        probe_path.write_text("ok", encoding="ascii")
        probe_path.unlink()
    except OSError as exc:
        if "probe_path" in locals():
            probe_path.unlink(missing_ok=True)
        raise OSError(
            f"선택한 폴더에 MCP 파일을 저장할 수 없습니다: {bundle_dir}. "
            "문서 또는 사용자 폴더 아래의 쓰기 가능한 위치를 선택해 주세요."
        ) from exc


def _run_background_operation_with_progress(
    operation: Callable[[Callable[[int, str, int | None, int | None], None]], object],
    *,
    progress_bar: object,
    detail_box: object,
    start_percent: int,
    end_percent: int,
    label: str,
    estimated_seconds: float,
) -> object:
    """Run blocking work while rendering measured progress and a live heartbeat."""
    events: queue.Queue[tuple[int, str, int | None, int | None]] = queue.Queue()
    result: dict[str, object] = {}

    def _report(percent: int, message: str, current: int | None = None, total: int | None = None) -> None:
        events.put((max(0, min(100, int(percent))), str(message or label), current, total))

    def _worker() -> None:
        try:
            result["value"] = operation(_report)
        except Exception as exc:  # pragma: no cover - surfaced in the Streamlit main thread
            result["error"] = exc

    thread = threading.Thread(target=_worker, name="pr-mcp-ui-long-operation", daemon=True)
    thread.start()
    started = time.monotonic()
    last_percent = max(0, min(100, int(start_percent)))
    last_message = label
    last_current: int | None = None
    last_total: int | None = None
    tick = 0
    while thread.is_alive() or not events.empty():
        while True:
            try:
                measured_percent, last_message, last_current, last_total = events.get_nowait()
            except queue.Empty:
                break
            mapped = start_percent + int((end_percent - start_percent) * measured_percent / 100)
            last_percent = max(last_percent, min(end_percent, mapped))

        elapsed_seconds = time.monotonic() - started
        if thread.is_alive():
            elapsed_fraction = elapsed_seconds / max(float(estimated_seconds) + elapsed_seconds, 1.0)
            estimated_percent = start_percent + int((end_percent - start_percent) * min(0.92, elapsed_fraction))
            last_percent = max(last_percent, min(end_percent - 1, estimated_percent))
        count_text = ""
        if last_total is not None and int(last_total) > 0:
            count_text = f" · {int(last_current or 0):,}/{int(last_total):,}"
        elapsed_text = _format_elapsed_seconds(elapsed_seconds)
        heartbeat = _heartbeat_label(tick)
        tick += 1
        progress_bar.progress(last_percent, text=f"{last_message}{count_text} · {last_percent}%")
        detail_box.caption(f"{heartbeat} · 경과 {elapsed_text} · {last_message}{count_text}")
        time.sleep(0.5)

    thread.join()
    error = result.get("error")
    if isinstance(error, BaseException):
        raise error
    progress_bar.progress(end_percent, text=f"{label} 완료 · {end_percent}%")
    detail_box.caption(f"완료 · 경과 {_format_elapsed_seconds(time.monotonic() - started)}")
    return result.get("value")


def _write_operator_mcp_bundle_zip(
    bundle_dir: Path,
    preferred_zip_path: Path,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[str, bool]:
    try:
        return write_mcp_setup_bundle_zip(
            bundle_dir,
            preferred_zip_path,
            progress_callback=progress_callback,
        ), False
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        fallback_zip_path = bundle_dir / f"{preferred_zip_path.stem}-{timestamp}.zip"
        return write_mcp_setup_bundle_zip(
            bundle_dir,
            fallback_zip_path,
            progress_callback=progress_callback,
        ), True


def _open_directory_in_explorer(path: Path) -> None:
    """Open a local output directory in Windows Explorer."""
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        raise OSError("Windows 탐색기 열기는 Windows 로컬 실행에서만 지원합니다.")
    subprocess.Popen(["explorer.exe", str(resolved)])


def _select_windows_output_directory(state_key: str, initial_path: str) -> None:
    """Open a native Windows folder picker and store the selected directory."""
    if sys.platform != "win32":
        st.session_state[f"{state_key}:picker_error"] = "폴더 선택은 Windows 로컬 실행에서만 지원합니다."
        return
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        st.session_state[f"{state_key}:picker_error"] = f"Windows 폴더 선택 기능을 불러올 수 없습니다: {exc}"
        return

    try:
        initial_directory = _resolve_operator_output_path(initial_path)
        initial_directory.mkdir(parents=True, exist_ok=True)
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(
                parent=root,
                initialdir=str(initial_directory),
                title="저장 폴더 선택",
                mustexist=True,
            )
        finally:
            root.destroy()
        if selected:
            st.session_state[state_key] = selected
            st.session_state.pop(f"{state_key}:picker_error", None)
    except (OSError, RuntimeError, tk.TclError) as exc:
        st.session_state[f"{state_key}:picker_error"] = f"Windows 폴더 선택 창을 열 수 없습니다: {exc}"


def _load_goldset_label_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise ValueError("검수 기록 파일에 내용이 없습니다.")
    if "document_id" not in rows[0]:
        raise ValueError("검수 기록 파일에 document_id 열이 필요합니다.")
    return rows


def _write_goldset_label_rows(path: Path, rows: list[dict[str, str]]) -> Path:
    if not rows:
        raise ValueError("저장할 검수 기록이 없습니다.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.stem}.bak-{timestamp}{path.suffix}")
    shutil.copy2(path, backup_path)
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return backup_path


def _goldset_row_missing_fields(row: dict[str, str]) -> list[str]:
    missing: list[str] = []
    status = str(row.get("label_status") or "").strip().lower()
    if status not in GOLDSET_COMPLETE_LABEL_STATUSES:
        missing.append("label_status")
    for field in ("reviewer", "reviewed_at"):
        if not str(row.get(field) or "").strip():
            missing.append(field)
    for spec in GOLDSET_SCORE_SPECS.values():
        if optional_int(row.get(spec["manual_field"])) is None:
            missing.append(spec["manual_field"])
        if optional_int(row.get(spec["match_field"])) is None:
            missing.append(spec["match_field"])
    return missing


def _goldset_row_validation_issues(row: dict[str, str]) -> list[str]:
    issues: list[str] = []
    for structure_type, spec in GOLDSET_SCORE_SPECS.items():
        label = GOLDSET_STRUCTURE_LABELS.get(structure_type, structure_type)
        pipeline = optional_int(row.get(spec["pipeline_field"]))
        manual = optional_int(row.get(spec["manual_field"]))
        matched = optional_int(row.get(spec["match_field"]))
        for kind, value in (("직접 센 개수", manual), ("일치 개수", matched)):
            if value is not None and value < 0:
                issues.append(f"{label}: {kind}는 0 이상이어야 합니다.")
        if matched is not None and manual is not None and matched > manual:
            issues.append(
                f"{label}: 일치 개수는 직접 센 개수보다 클 수 없습니다 "
                "(matched count cannot exceed manual count)."
            )
        if matched is not None and pipeline is not None and matched > pipeline:
            issues.append(
                f"{label}: 일치 개수는 자동 추출 개수보다 클 수 없습니다 "
                "(matched count cannot exceed pipeline count)."
            )
    status = str(row.get("label_status") or "").strip().lower()
    if status in GOLDSET_COMPLETE_LABEL_STATUSES and _goldset_row_missing_fields(row):
        issues.append("'검수 완료' 상태로 저장하려면 모든 개수와 검수자 이름, 검수 일시를 빠짐없이 입력해야 합니다.")
    return issues


def _goldset_metric_summary(
    pipeline: int | None,
    manual: int | None,
    matched: int | None,
) -> dict[str, str]:
    if pipeline is None or manual is None or matched is None:
        return {
            "false_positive": "-",
            "false_negative": "-",
            "precision": "-",
            "recall": "-",
            "status": "미입력",
        }
    if pipeline < 0 or manual < 0 or matched < 0 or matched > pipeline or matched > manual:
        return {
            "false_positive": "-",
            "false_negative": "-",
            "precision": "-",
            "recall": "-",
            "status": "확인 필요",
        }
    precision = matched / pipeline if pipeline else None
    recall = matched / manual if manual else None
    return {
        "false_positive": str(pipeline - matched),
        "false_negative": str(manual - matched),
        "precision": f"{precision:.1%}" if precision is not None else "해당 없음",
        "recall": f"{recall:.1%}" if recall is not None else "해당 없음",
        "status": "일치" if pipeline == manual == matched else "차이 있음",
    }


def _goldset_detail_text(row: dict[str, str], structure_type: str) -> str:
    fields = GOLDSET_DETAIL_FIELDS.get(structure_type) or []
    parts: list[str] = []
    for label, field in fields:
        value = optional_int(row.get(field))
        if value is not None:
            parts.append(f"{label}={value:,}")
    return " / ".join(parts)


def _goldset_progress(rows: list[dict[str, str]]) -> dict[str, int | bool]:
    expected_structure_rows = len(rows) * len(GOLDSET_SCORE_SPECS)
    completed_structure_rows = 0
    ready_rows = 0
    missing_manual = 0
    missing_matched = 0
    missing_reviewer_metadata = 0
    for row in rows:
        row_complete = not _goldset_row_missing_fields(row) and not _goldset_row_validation_issues(row)
        ready_rows += 1 if row_complete else 0
        for spec in GOLDSET_SCORE_SPECS.values():
            manual_ready = optional_int(row.get(spec["manual_field"])) is not None
            matched_ready = optional_int(row.get(spec["match_field"])) is not None
            pipeline_ready = optional_int(row.get(spec["pipeline_field"])) is not None
            if manual_ready and matched_ready and pipeline_ready:
                completed_structure_rows += 1
            if not manual_ready:
                missing_manual += 1
            if not matched_ready:
                missing_matched += 1
        if not str(row.get("reviewer") or "").strip() or not str(row.get("reviewed_at") or "").strip():
            missing_reviewer_metadata += 1
    return {
        "document_count": len(rows),
        "ready_document_count": ready_rows,
        "expected_structure_rows": expected_structure_rows,
        "completed_structure_rows": completed_structure_rows,
        "missing_manual_count": missing_manual,
        "missing_matched_count": missing_matched,
        "missing_reviewer_metadata_count": missing_reviewer_metadata,
        "ready_for_quality_claim": bool(rows) and ready_rows == len(rows),
    }


def _goldset_review_sort_key(row: dict[str, str]) -> tuple[int, int, str]:
    if not _goldset_row_missing_fields(row) and not _goldset_row_validation_issues(row):
        complete_rank = 1
    else:
        complete_rank = 0
    table_load = (
        (optional_int(row.get("pipeline_table_count")) or 0)
        + (optional_int(row.get("pipeline_nested_table_count")) or 0) * 3
        + (optional_int(row.get("pipeline_appendix_form_count")) or 0)
    )
    review_order = optional_int(row.get("review_order")) or 999_999
    return (complete_rank, -table_load, review_order, str(row.get("document_id") or ""))


def _find_goldset_packet_path(document_id: str) -> Path | None:
    if not document_id:
        return None
    packet_dirs = sorted(
        (PROJECT_ROOT / "reports").glob("parsing_goldset_review_packets*"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    for packet_dir in packet_dirs:
        if not packet_dir.is_dir():
            continue
        matches = sorted(packet_dir.glob(f"*{document_id}*.md"))
        if matches:
            return matches[0]
    return None


def _open_local_artifact(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(str(path))
    escaped = str(path).replace("'", "''")
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Command", f"Invoke-Item -LiteralPath '{escaped}'"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _load_approval_template_from_manifest(
    raw_path: str,
    document_id: str,
    *,
    review_batch_id: str = "",
) -> dict[str, object]:
    manifest_path = _resolve_operator_artifact_path(raw_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("report_type") != "approval_review_batch_manifest":
        raise ValueError("선택한 파일이 검수 묶음(approval_review_batch_manifest) JSON이 아닙니다.")
    batches = payload.get("batches") if isinstance(payload.get("batches"), list) else []
    matching_batches = [
        batch for batch in batches
        if isinstance(batch, dict) and str(batch.get("document_id") or "") == document_id
    ]
    if not matching_batches:
        raise ValueError(f"이 문서(document_id={document_id})의 검수 묶음이 파일에 없습니다.")
    selected_batch_id = str(review_batch_id or "").strip()
    if selected_batch_id:
        matching_batches = [
            batch for batch in matching_batches
            if str(batch.get("review_batch_id") or "") == selected_batch_id
        ]
        if not matching_batches:
            raise ValueError(f"입력한 묶음 번호(review_batch_id={selected_batch_id})를 찾을 수 없습니다.")
    batch = matching_batches[0]
    template = batch.get("approval_request_template")
    if not isinstance(template, dict):
        raise ValueError("선택한 검수 묶음에 approval_request_template이 없습니다.")
    chunk_ids = [str(chunk_id) for chunk_id in (template.get("chunk_ids") or batch.get("chunk_ids") or [])]
    if not chunk_ids:
        raise ValueError("선택한 검수 묶음에 청크 목록(chunk_ids)이 없습니다.")
    safe_manifest_path = str(
        template.get("review_batch_manifest_path")
        or payload.get("approval_request_path")
        or _safe_relative_approval_artifact_path(manifest_path, raw_path)
    )
    manifest_sha256 = str(template.get("review_batch_manifest_sha256") or payload.get("approval_request_sha256") or "")
    if not manifest_sha256:
        manifest_sha256 = _sha256_file(manifest_path)
    return {
        "worklist_report_path": str(template.get("worklist_report_path") or ""),
        "worklist_report_sha256": str(template.get("worklist_report_sha256") or ""),
        "review_batch_manifest_path": safe_manifest_path,
        "review_batch_manifest_sha256": manifest_sha256,
        "review_batch_id": str(template.get("review_batch_id") or batch.get("review_batch_id") or ""),
        "review_batch_chunk_fingerprint": str(
            template.get("review_batch_chunk_fingerprint")
            or batch.get("review_batch_chunk_fingerprint")
            or ""
        ),
        "review_strategy": str(template.get("review_strategy") or batch.get("review_strategy") or ""),
        "security_level": str(template.get("security_level") or ""),
        "review_flags_acknowledged_required": bool(batch.get("review_flags_acknowledged_required")),
        "chunk_count": int(batch.get("chunk_count") or 0),
        "chunk_ids": chunk_ids,
        "available_batch_count": len(matching_batches),
        "available_review_batch_ids": [
            str(item.get("review_batch_id") or "")
            for item in matching_batches[:10]
            if isinstance(item, dict)
        ],
    }


def _load_all_approval_templates_from_manifest(raw_path: str, document_id: str) -> list[dict[str, object]]:
    manifest_path = _resolve_operator_artifact_path(raw_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("report_type") != "approval_review_batch_manifest":
        raise ValueError("선택한 파일이 검수 묶음(approval_review_batch_manifest) JSON이 아닙니다.")
    batches = payload.get("batches") if isinstance(payload.get("batches"), list) else []
    batch_ids = [
        str(batch.get("review_batch_id") or "")
        for batch in batches
        if isinstance(batch, dict) and str(batch.get("document_id") or "") == document_id
    ]
    if not batch_ids:
        raise ValueError(f"이 문서(document_id={document_id})의 검수 묶음이 파일에 없습니다.")
    return [
        _load_approval_template_from_manifest(str(manifest_path), document_id, review_batch_id=batch_id)
        for batch_id in batch_ids
    ]


def _safe_report_key(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or ""))
    return safe[:80] or "document"


def _build_current_document_approval_templates(
    ctx: dict,
    *,
    security_level: str,
    candidate_chunk_ids: list[str] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    document = ctx["document"]
    document_id = ctx["document_id"]
    chunks = ctx["chunks"]
    candidate_chunks = [chunk for chunk in chunks if _is_chunk_pending_approval(chunk)]
    requested_chunk_ids = {
        str(chunk_id).strip()
        for chunk_id in candidate_chunk_ids or []
        if str(chunk_id or "").strip()
    }
    if requested_chunk_ids:
        candidate_chunks = [chunk for chunk in candidate_chunks if str(chunk.chunk_id) in requested_chunk_ids]
        found_ids = {str(chunk.chunk_id) for chunk in candidate_chunks}
        missing_ids = sorted(requested_chunk_ids - found_ids)
        if missing_ids:
            status_by_id = {
                str(chunk.chunk_id): _approval_status(chunk)
                for chunk in chunks
                if str(chunk.chunk_id) in requested_chunk_ids
            }
            details = ", ".join(f"{chunk_id}({status_by_id.get(chunk_id, 'missing')})" for chunk_id in missing_ids[:20])
            raise ValueError(f"Approval target chunks are not pending review: {details}")
    if not candidate_chunks:
        raise ValueError("새로 승인할 청크가 없습니다. 이미 승인된 내용만 AI에 등록하려면 오른쪽 버튼을 사용하세요.")

    artifact_root = settings.artifact_root.resolve()
    reports_dir = artifact_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    safe_document_id = _safe_report_key(document_id)
    worklist_path = reports_dir / f"streamlit_{safe_document_id}_approval_worklist.json"
    manifest_path = reports_dir / f"streamlit_{safe_document_id}_approval_review_batches.json"
    worklist_relative = worklist_path.resolve().relative_to(artifact_root).as_posix()
    manifest_relative = manifest_path.resolve().relative_to(artifact_root).as_posix()
    generated_at = datetime.now(timezone.utc).isoformat()
    tenant_id = str(ctx["document_tenant_id"] or "default")
    attention_by_chunk = {chunk.chunk_id: chunk_review_attention_reasons(chunk) for chunk in candidate_chunks}
    requires_ack = any(attention_by_chunk.values())
    review_type = "manual_attention" if requires_ack else "low_risk_batch"
    review_strategy = "operator_manual_review" if requires_ack else "human_bulk_review"

    worklist = {
        "report_type": "approval_worklist",
        "generated_at": generated_at,
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": bool(settings.tenant_storage_isolation),
        "document_count": 1,
        "total_chunks": len(candidate_chunks),
        "approval_status_totals": {
            status: sum(1 for chunk in candidate_chunks if _approval_status(chunk) == status)
            for status in sorted({_approval_status(chunk) for chunk in candidate_chunks})
        },
        "documents": [
            {
                "rank": 1,
                "suggested_action": "manual_review_first" if requires_ack else "bulk_review_candidate",
                "document_id": document_id,
                "document_name": getattr(document, "document_name", "") or "",
                "filename": getattr(document, "filename", "") or "",
                "institution_name": getattr(document, "institution_name", "") or "",
                "apba_id": getattr(document, "apba_id", "") or "",
                "profile_id": getattr(document, "profile_id", "") or "",
                "source_system": getattr(document, "source_system", "") or "",
                "source_record_id": getattr(document, "source_record_id", "") or "",
                "source_file_id": getattr(document, "source_file_id", "") or "",
                "total_chunks": len(chunks),
                "approved_chunks": int(ctx.get("approved_count") or 0),
                "draft_chunks": sum(1 for chunk in chunks if _approval_status(chunk) == "draft"),
                "needs_review_chunks": sum(1 for chunk in chunks if _approval_status(chunk) == "needs_review"),
                "pending_approval_chunks": sum(1 for chunk in chunks if _is_chunk_pending_approval(chunk)),
            }
        ],
        "safety_note": "Generated by Streamlit simple approval flow. It does not approve or index by itself.",
    }
    worklist_path.write_text(json.dumps(worklist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    worklist_sha256 = _sha256_file(worklist_path)

    batch_chunks = [
        {
            "chunk_id": chunk.chunk_id,
            "review_content_hash": review_content_hash(chunk),
            "approval_status": str(chunk.approval_status or "").strip().lower(),
            "review_priority_tier": "domain_attention" if attention_by_chunk.get(chunk.chunk_id) else "no_signal",
            "review_category": "manual_review" if attention_by_chunk.get(chunk.chunk_id) else "low_risk",
            "attention_reasons": attention_by_chunk.get(chunk.chunk_id) or [],
        }
        for chunk in candidate_chunks
    ]
    fingerprint = review_batch_chunk_fingerprint(batch_chunks, review_type)
    review_batch_id = f"approval-{worklist_sha256[:12]}-{safe_document_id[:32]}-{fingerprint[:12]}"
    chunk_ids = [chunk.chunk_id for chunk in candidate_chunks]
    manifest = {
        "report_type": "approval_review_batch_manifest",
        "generated_at": generated_at,
        "data_dir": str(settings.data_dir),
        "effective_data_dir": str(settings.data_dir),
        "tenant_id": tenant_id,
        "tenant_storage_isolation": bool(settings.tenant_storage_isolation),
        "worklist_report": {
            "path": str(worklist_path),
            "approval_request_path": worklist_relative,
            "sha256": worklist_sha256,
            "effective_data_dir": str(settings.data_dir),
            "tenant_id": tenant_id,
            "tenant_storage_isolation": bool(settings.tenant_storage_isolation),
            "document_count": 1,
            "total_chunks": len(candidate_chunks),
        },
        "approval_request_path": manifest_relative,
        "approval_request_sha256": "",
        "batch_count": 1,
        "approval_chunk_count": len(candidate_chunks),
        "manual_attention_chunks": len(candidate_chunks) if requires_ack else 0,
        "low_risk_batch_review_candidate_chunks": 0 if requires_ack else len(candidate_chunks),
        "review_type_batch_counts": {review_type: 1},
        "blocker_count": 0,
        "warning_count": 0,
        "passed": True,
        "findings": [],
        "batches": [
            {
                "batch_rank": 1,
                "review_batch_id": review_batch_id,
                "review_batch_chunk_fingerprint": fingerprint,
                "review_type": review_type,
                "review_strategy": review_strategy,
                "document_id": document_id,
                "document_name": getattr(document, "document_name", "") or "",
                "filename": getattr(document, "filename", "") or "",
                "institution_name": getattr(document, "institution_name", "") or "",
                "apba_id": getattr(document, "apba_id", "") or "",
                "source_system": getattr(document, "source_system", "") or "",
                "source_record_id": getattr(document, "source_record_id", "") or "",
                "source_file_id": getattr(document, "source_file_id", "") or "",
                "chunk_count": len(candidate_chunks),
                "chunk_ids": chunk_ids,
                "chunks": batch_chunks,
                "review_priority_tier_counts": {
                    "domain_attention": sum(1 for item in batch_chunks if item["review_priority_tier"] != "no_signal"),
                    "no_signal": sum(1 for item in batch_chunks if item["review_priority_tier"] == "no_signal"),
                },
                "top_attention_reasons": {},
                "review_flags_acknowledged_required": requires_ack,
                "approval_request_template": {
                    "chunk_ids": chunk_ids,
                    "security_level": security_level,
                    "review_flags_acknowledged": False,
                    "worklist_report_path": worklist_relative,
                    "worklist_report_sha256": worklist_sha256,
                    "review_batch_manifest_path": manifest_relative,
                    "review_batch_manifest_sha256": "",
                    "review_batch_id": review_batch_id,
                    "review_batch_chunk_fingerprint": fingerprint,
                    "review_strategy": review_strategy,
                },
            }
        ],
        "safety_note": "Generated by Streamlit simple approval flow. Operator confirmation is still required.",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence = {
        "report_type": "streamlit_current_document_approval_evidence",
        "generated_at": generated_at,
        "document_id": document_id,
        "tenant_id": tenant_id,
        "artifacts": {
            "worklist_json": str(worklist_path),
            "worklist_sha256": worklist_sha256,
            "review_batches_json": str(manifest_path),
            "review_batches_sha256": _sha256_file(manifest_path),
        },
    }
    templates = _load_all_approval_templates_from_manifest(manifest_path, document_id)
    return evidence, templates


def _selected_approval_contexts(selected_document_ids: list[str], current_ctx: dict) -> list[dict]:
    """Load the minimum review context for every selected regulation without merging chunks."""
    contexts: list[dict] = []
    current_document_id = str(current_ctx.get("document_id") or "")
    for document_id in selected_document_ids:
        normalized_document_id = str(document_id or "").strip()
        if not normalized_document_id:
            continue
        if normalized_document_id == current_document_id:
            contexts.append(current_ctx)
            continue
        document = repository.get_document(normalized_document_id)
        if document is None:
            continue
        chunks = repository.get_chunks(normalized_document_id)
        tenant_id = str(getattr(document, "tenant_id", None) or _local_operator_tenant_id()).strip()
        latest_run = repository.latest_completed_run(normalized_document_id)
        agent_review_summary = (latest_run.stats or {}).get("agent_review") if latest_run else {}
        if not isinstance(agent_review_summary, dict):
            agent_review_summary = {}
        contexts.append(
            {
                "document_id": normalized_document_id,
                "document": document,
                "chunks": chunks,
                "document_tenant_id": tenant_id or _local_operator_tenant_id(),
                "local_auth": AuthContext(
                    actor="streamlit-local-operator",
                    tenant_id=tenant_id or _local_operator_tenant_id(),
                    auth_mode="streamlit-local",
                ),
                "approved_count": sum(1 for chunk in chunks if _approval_status(chunk) == "approved"),
                "review_attention": {
                    chunk.chunk_id: chunk_review_attention_reasons(chunk)
                    for chunk in chunks
                    if chunk_review_attention_reasons(chunk)
                },
                "agent_review_summary": agent_review_summary,
            }
        )
    return contexts


def _approval_pending_entries(ctx: dict) -> list[dict[str, object]]:
    document_id = str(ctx["document_id"])
    review_attention = dict(ctx.get("review_attention") or {})
    agent_review_summary = dict(ctx.get("agent_review_summary") or {})
    return [
        _approval_chunk_review_state_from_session(
            document_id=document_id,
            chunk=chunk,
            review_attention=review_attention,
            agent_review_summary=agent_review_summary,
        )
        for chunk in ctx["chunks"]
        if _is_chunk_pending_approval(chunk)
    ]


def _prepare_reviewed_document_approval_plan(ctx: dict, *, security_level: str = "internal") -> dict[str, object]:
    """Build one regulation's approval requests while preserving its own evidence and hierarchy."""
    document_id = str(ctx["document_id"])
    chunks = list(ctx["chunks"])
    pending_entries = _approval_pending_entries(ctx)
    incomplete_entries = [
        entry for entry in pending_entries if not bool(dict(entry["state"]).get("approve_enabled"))
    ]
    if incomplete_entries:
        document_label = _workflow_document_label(ctx.get("document"))
        raise ValueError(
            f"{document_label}: 아직 AI·사람 검수가 끝나지 않은 청크가 {len(incomplete_entries):,}개 있습니다."
        )

    edited_chunk_total = 0
    approval_requests: list[ApprovalRequest] = []
    evidence: dict[str, object] = {}
    if pending_entries:
        edited_chunk_total = _approval_save_text_edits(
            document_id=document_id,
            chunks=chunks,
            entries=pending_entries,
            target_repository=repository,
        )
        evidence, templates = _build_current_document_approval_templates(
            ctx,
            security_level=security_level,
            candidate_chunk_ids=[str(entry["chunk_id"]) for entry in pending_entries],
        )
        review_events: list[dict[str, object]] = []
        for entry in pending_entries:
            target_chunk = entry["chunk"]
            target_chunk_id = str(entry["chunk_id"])
            hold_events_key = _approval_chunk_state_key(document_id, target_chunk_id, "hold_events")
            review_events.extend(list(st.session_state.get(hold_events_key) or []))
            review_events.extend(
                build_approval_review_events(
                    chunk_id=target_chunk_id,
                    actor=ctx["local_auth"].actor,
                    item_ids=list(entry["item_ids"]),
                    ai_decisions=dict(entry["ai_decisions"]),
                    human_confirmed=bool(entry["human_confirmed"]),
                    table_source=str(target_chunk.metadata.get("table_source") or ""),
                    kordoc_table_promoted=bool(target_chunk.metadata.get("kordoc_table_promoted")),
                    approve_event="approved",
                )
            )
        for template in templates:
            chunk_ids = [str(chunk_id) for chunk_id in template["chunk_ids"]]
            template_chunk_ids = set(chunk_ids)
            approval_requests.append(
                ApprovalRequest(
                    chunk_ids=chunk_ids,
                    security_level=security_level,
                    review_flags_acknowledged=True,
                    worklist_report_path=str(template["worklist_report_path"]),
                    worklist_report_sha256=str(template["worklist_report_sha256"]),
                    review_batch_manifest_path=str(template["review_batch_manifest_path"]),
                    review_batch_manifest_sha256=str(template["review_batch_manifest_sha256"]),
                    review_batch_id=str(template["review_batch_id"]),
                    review_batch_chunk_fingerprint=str(template["review_batch_chunk_fingerprint"]),
                    review_strategy=str(template["review_strategy"]),
                    review_decision_events=[
                        event
                        for event in review_events
                        if str(event.get("chunk_id") or "") in template_chunk_ids
                    ],
                    note="approval_screen_selected_regulations_batch",
                )
            )
    return {
        "document_id": document_id,
        "document": ctx["document"],
        "local_auth": ctx["local_auth"],
        "approval_requests": approval_requests,
        "pending_chunk_count": len(pending_entries),
        "edited_chunk_count": edited_chunk_total,
        "evidence": evidence,
    }


def _execute_reviewed_document_approval_plan(plan: dict[str, object]) -> dict[str, object]:
    document_id = str(plan["document_id"])
    local_auth = plan["local_auth"]
    approved_chunk_count = 0
    for approval_request in plan["approval_requests"]:
        approve_review_chunks(document_id, approval_request, local_auth)
        approved_chunk_count += len(approval_request.chunk_ids)
    index_result = index_document(
        document_id,
        IndexRequest(target_type="local-jsonl", embedding_dimensions=384),
        local_auth,
    )
    return {
        "document_id": document_id,
        "approved_chunk_count": approved_chunk_count,
        "edited_chunk_count": int(plan.get("edited_chunk_count") or 0),
        "indexed_record_count": int(index_result.get("record_count") or 0),
    }


def _render_operator_theme() -> None:
    st.markdown(
        """
        <style>
        .block-container {padding-top: 2.2rem; padding-bottom: 3rem;}
        h1, h2, h3, p, span, label, div {
            word-break: keep-all;
            letter-spacing: 0;
        }
        h1, h2, h3 {
            line-height: 1.35;
            padding-top: .18rem;
            overflow: visible;
        }
        .rr-hero {
            padding: 1.2rem 1.5rem;
            border-radius: 1.1rem;
            background: linear-gradient(135deg, #103b34 0%, #1f6f5b 48%, #e6b857 100%);
            color: #fff;
            box-shadow: 0 16px 40px rgba(16, 59, 52, 0.16);
            margin-bottom: 1rem;
        }
        .rr-hero h1 {font-size: 1.6rem; line-height: 1.7 !important; margin: 0 0 .2rem 0; padding: .25rem 0 .1rem 0; font-weight: 650; overflow: visible; color: #fff;}
        .rr-hero p {font-size: .98rem; margin: 0; max-width: 58rem; color: #fff;}
        .rr-pill-row {display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .7rem;}
        .rr-pill {
            border: 1px solid rgba(255,255,255,.42);
            border-radius: 999px;
            padding: .22rem .7rem;
            font-size: .8rem;
            background: rgba(255,255,255,.12);
        }
        div[data-testid="stMetric"] {
            background: #fbfaf5;
            border: 1px solid #ede3c9;
            border-radius: 1rem;
            padding: .7rem .8rem;
        }
        .rr-section-note {
            padding: .75rem .9rem;
            border-left: .28rem solid #1f6f5b;
            background: #f5fbf8;
            border-radius: .6rem;
            margin: .5rem 0 1rem 0;
        }
        .rr-step-card {
            border: 1px solid #e3ddc9;
            border-radius: 1rem;
            padding: .9rem 1rem;
            background: #fffdf7;
            min-height: 9.5rem;
        }
        .rr-step-card.done {border-color: #1f6f5b; background: #f2faf6;}
        .rr-step-card.current {border: 2px solid #e6b857; background: #fffaf0;}
        .rr-step-num {
            display: inline-block;
            font-size: .78rem;
            font-weight: 700;
            color: #1f6f5b;
            border: 1px solid #1f6f5b;
            border-radius: 999px;
            padding: .05rem .6rem;
            margin-bottom: .4rem;
        }
        .rr-step-card h4 {margin: 0 0 .35rem 0; font-size: 1.02rem; line-height: 1.5;}
        .rr-step-card p {margin: 0; font-size: .87rem; color: #4c554f; line-height: 1.55;}
        .rr-step-state {font-size: .8rem; font-weight: 700; margin-top: .5rem;}
        .rr-step-state.done {color: #1f6f5b;}
        .rr-step-state.current {color: #b8860b;}
        .rr-step-state.todo {color: #8a8f8b;}
        div[class*="st-key-api-key-setup-cta"] button {
            background: #c62828 !important;
            border-color: #a61f1f !important;
            color: #ffffff !important;
            font-weight: 800 !important;
            box-shadow: 0 6px 16px rgba(198, 40, 40, .22);
        }
        div[class*="st-key-api-key-setup-cta"] button:hover {
            background: #a61f1f !important;
            border-color: #861919 !important;
        }
        .rr-institution-card {
            min-height: 10rem;
            border: 1px solid #d8e3dc;
            border-radius: 1rem;
            padding: 1rem 1.1rem;
            background: linear-gradient(145deg, #fbfdfb 0%, #f1f8f4 100%);
            box-shadow: 0 8px 20px rgba(31, 111, 91, .07);
            margin-bottom: .55rem;
        }
        .rr-institution-card h3 {margin: .3rem 0 .25rem 0; color: #14453a;}
        .rr-institution-card p {margin: 0 0 .7rem 0; color: #4c554f;}
        .rr-institution-card small {color: #7a837d;}
        .rr-institution-kicker {font-size: .72rem; font-weight: 700; color: #1f6f5b; letter-spacing: .04em;}
        .rr-next-box {
            border: 2px solid #1f6f5b;
            border-radius: 1rem;
            padding: 1rem 1.2rem;
            background: #f2faf6;
            margin: 1rem 0;
            font-size: 1.02rem;
        }
        .rr-help {
            padding: .7rem .9rem;
            border-radius: .7rem;
            background: #f7f6f0;
            border: 1px dashed #cfc7ac;
            font-size: .88rem;
            color: #4c554f;
            margin-bottom: .8rem;
        }
        .rr-stages {display: flex; align-items: stretch; flex-wrap: wrap; gap: .3rem; margin: .2rem 0 1.1rem 0;}
        .rr-stage {
            flex: 1 1 0; min-width: 9rem;
            border: 1px solid #e3ddc9; border-radius: .8rem;
            padding: .55rem .8rem; background: #fffdf7;
        }
        .rr-stage .rr-stage-k {font-size: .72rem; font-weight: 700; color: #8a8f8b;}
        .rr-stage .rr-stage-t {font-size: .96rem; font-weight: 700; margin: .12rem 0; color: #2b322e;}
        .rr-stage .rr-stage-d {font-size: .78rem; color: #6b726c; line-height: 1.45;}
        .rr-stage.done {border-color: #1f6f5b; background: #f2faf6;}
        .rr-stage.done .rr-stage-k {color: #1f6f5b;}
        .rr-stage.active {border: 2px solid #e6b857; background: #fffaf0;}
        .rr-stage.active .rr-stage-k {color: #b8860b;}
        .rr-stage-arrow {align-self: center; color: #c9c1a6; font-weight: 700; padding: 0 .15rem;}
        .rr-ai-panel {
            border: 1px solid #cfe0d8; border-radius: 1rem;
            padding: .9rem 1.1rem; background: #f4faf7; margin: .3rem 0 1rem 0;
        }
        .rr-ai-panel h4 {margin: 0 0 .35rem 0; font-size: 1.02rem; color: #14453a;}
        .rr-ai-panel p {margin: 0; font-size: .9rem; color: #33413b; line-height: 1.55;}
        .rr-ai-tag {
            display: inline-block; font-size: .74rem; font-weight: 700;
            border-radius: 999px; padding: .08rem .6rem; margin-bottom: .35rem;
        }
        .rr-ai-tag.ok {background: #dff1e8; color: #1f6f5b; border: 1px solid #bfe0d1;}
        .rr-ai-tag.draft {background: #fdf1d8; color: #b8860b; border: 1px solid #ecd9a8;}
        div[data-testid="stFileUploader"] {
            border: 2px dashed #9fb5aa;
            border-radius: .9rem;
            background: #f6faf8;
            padding: .35rem .45rem .55rem .45rem;
            margin-bottom: .45rem;
        }
        div[data-testid="stFileUploader"] section,
        div[data-testid="stFileUploaderDropzone"] {
            min-height: 5.6rem;
            border-radius: .7rem;
        }
        div[data-testid="stFileUploader"] section:hover,
        div[data-testid="stFileUploaderDropzone"]:hover {
            border-color: #287765;
            background: #edf6f1;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_hero(subtitle: str) -> None:
    st.markdown(
        f"""
        <div class="rr-hero">
          <h1>공공기관 규정 MCP 빌더</h1>
          <p>{subtitle}</p>
          <div class="rr-pill-row">
            <span class="rr-pill">로컬 전용 화면</span>
            <span class="rr-pill">승인 handoff 준비</span>
            <span class="rr-pill">품질 근거</span>
            <span class="rr-pill">기관 전달용 산출물</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _document_context_revision(document_id: str) -> tuple[tuple[str, int, int], ...]:
    paths = [
        repository._result_path(document_id, result_type)
        for result_type in ("chunks", "issues", "nodes", "quality")
    ]
    paths.append(Path(settings.data_dir) / "repository" / "manifest.json")
    revision: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            revision.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
        except OSError:
            revision.append((str(path), 0, 0))
    return tuple(revision)


def _store_document_context_cache(document_id: str, context: dict | None) -> None:
    if context is None:
        st.session_state.pop(DOCUMENT_CONTEXT_CACHE_KEY, None)
        return
    st.session_state[DOCUMENT_CONTEXT_CACHE_KEY] = {
        "document_id": document_id,
        "revision": _document_context_revision(document_id),
        "context": context,
    }


def _cached_document_context(document_id: str) -> dict | None:
    cached = st.session_state.get(DOCUMENT_CONTEXT_CACHE_KEY)
    if not isinstance(cached, dict) or cached.get("document_id") != document_id:
        return None
    if cached.get("revision") != _document_context_revision(document_id):
        st.session_state.pop(DOCUMENT_CONTEXT_CACHE_KEY, None)
        return None
    context = cached.get("context")
    return context if isinstance(context, dict) else None


def _invalidate_document_context_cache(document_id: str | None = None) -> None:
    st.session_state.pop(WORKFLOW_MCP_GATE_CACHE_KEY, None)
    cached = st.session_state.get(DOCUMENT_CONTEXT_CACHE_KEY)
    if document_id and isinstance(cached, dict) and cached.get("document_id") != document_id:
        return
    st.session_state.pop(DOCUMENT_CONTEXT_CACHE_KEY, None)


def _load_document_context(document_id: str) -> dict | None:
    return _load_document_context_for_profile(
        document_id,
        selected_profile_id=_selected_institution_profile_id(),
    )


def _load_document_context_with_progress(
    document_id: str,
    *,
    selected_profile_id: str,
    progress_callback: Callable[[int, str, int | None, int | None], None],
) -> dict | None:
    progress_callback(4, "문서와 기관 범위 확인", 0, 5)
    context = _load_document_context_for_profile(document_id, selected_profile_id=selected_profile_id)
    progress_callback(100, "문서·청크·목차·색인 상태 불러오기 완료", 5, 5)
    return context


def _load_document_context_for_profile(document_id: str, *, selected_profile_id: str) -> dict | None:
    document = repository.get_document(document_id)
    if document is None:
        return None
    if selected_profile_id and not _document_belongs_to_institution_profile(document, selected_profile_id):
        return None
    document_tenant_id = str(getattr(document, "tenant_id", None) or _local_operator_tenant_id()).strip()
    document_tenant_id = document_tenant_id or _local_operator_tenant_id()
    local_auth = AuthContext(
        actor="streamlit-local-operator",
        tenant_id=document_tenant_id,
        auth_mode="streamlit-local",
    )
    chunk_result_path = repository._result_path(document_id, "chunks")
    oversized_result_warning = None
    try:
        if chunk_result_path.stat().st_size > 512 * 1024 * 1024:
            oversized_result_warning = (
                f"청크 결과 파일이 {chunk_result_path.stat().st_size / (1024 * 1024 * 1024):.1f}GB입니다. "
                "이전 버전에서 중복 메타데이터가 저장된 결과일 수 있어 전체 내용을 메모리에 불러오지 않았습니다. "
                "① 문서 올려서 전처리 화면에서 원본을 새 버전으로 다시 처리하세요."
            )
    except OSError:
        pass
    if oversized_result_warning:
        quality_report = repository.get_quality_report(document_id)
        latest_run = repository.latest_completed_run(document_id)
        agent_review_summary = (latest_run.stats or {}).get("agent_review") if latest_run else {}
        if not isinstance(agent_review_summary, dict):
            agent_review_summary = {}
        return {
            "document_id": document_id,
            "document": document,
            "chunks": [],
            "issues": [],
            "nodes": [],
            "quality_report": quality_report,
            "document_tenant_id": document_tenant_id,
            "local_auth": local_auth,
            "approval_counts": {},
            "approved_count": 0,
            "review_attention": {},
            "index_status": None,
            "index_status_error": None,
            "mcp_connection_gate": _mcp_connection_gate(None, 0),
            "agent_review_summary": agent_review_summary,
            "large_result_warning": oversized_result_warning,
        }
    chunks = repository.get_chunks(document_id)
    issues = repository.get_issues(document_id)
    nodes = repository.get_nodes(document_id)
    quality_report = repository.get_quality_report(document_id)
    document_tenant_id = getattr(document, "tenant_id", None) or (
        chunks[0].metadata.get("tenant_id") if chunks else None
    ) or _local_operator_tenant_id()
    document_tenant_id = str(document_tenant_id or "").strip() or _local_operator_tenant_id()
    if getattr(document, "tenant_id", None) != document_tenant_id:
        document = document.model_copy(update={"tenant_id": document_tenant_id})
        repository.upsert_document(document)
    elif JsonRepository(settings).get_document(document_id) is None:
        repository.upsert_document(document)
    local_auth = AuthContext(
        actor="streamlit-local-operator",
        tenant_id=document_tenant_id,
        auth_mode="streamlit-local",
    )
    approval_counts: dict[str, int] = {}
    for chunk in chunks:
        approval_counts[chunk.approval_status] = approval_counts.get(chunk.approval_status, 0) + 1
    approved_count = int(approval_counts.get("approved") or 0)
    review_attention = {
        chunk.chunk_id: chunk_review_attention_reasons(chunk)
        for chunk in chunks
        if chunk_review_attention_reasons(chunk)
    }
    index_status = None
    index_status_error = None
    try:
        index_status = get_index_status(document_id, local_auth)
    except Exception as exc:
        index_status_error = exc
    latest_run = repository.latest_completed_run(document_id)
    agent_review_summary = (latest_run.stats or {}).get("agent_review") if latest_run else {}
    if not isinstance(agent_review_summary, dict):
        agent_review_summary = {}
    return {
        "document_id": document_id,
        "document": document,
        "chunks": chunks,
        "issues": issues,
        "nodes": nodes,
        "quality_report": quality_report,
        "document_tenant_id": document_tenant_id,
        "local_auth": local_auth,
        "approval_counts": approval_counts,
        "approved_count": approved_count,
        "review_attention": review_attention,
        "index_status": index_status,
        "index_status_error": index_status_error,
        "mcp_connection_gate": _mcp_connection_gate(index_status, approved_count),
        "agent_review_summary": agent_review_summary,
    }


def _unreviewed_preview_requested() -> bool:
    if st.session_state.get("unreviewed_preview_requested"):
        return True
    return st.session_state.get(OFFICIAL_RAG_MCP_REVIEW_REQUIRED_KEY) is False


def _mcp_bundle_state_key(document_id: str, scope: str = "document") -> str:
    if scope == "document":
        return f"{MCP_BUNDLE_STATE_PREFIX}:{document_id}"
    return f"{MCP_BUNDLE_STATE_PREFIX}:{scope}:{document_id}"


def _mcp_final_verification_prompts(connection_target: str, server_name: str) -> list[str]:
    """Return only prompts supported by the selected client's MCP tool profile."""

    if connection_target in MCP_EXTERNAL_DATA_TARGETS:
        return [
            f"{server_name} MCP의 search 도구로 인사규정을 찾고, 반환된 첫 번째 id를 "
            "fetch 도구로 조회해 조문 원문과 출처를 보여줘."
        ]
    return [
        f"{server_name} MCP의 get_index_status를 실행하고 사용 가능한 규정 도구를 보여줘.",
        f"{server_name} MCP의 list_regulations 도구를 사용해서 등록된 규정 목록을 보여줘.",
    ]


def _read_mcp_connection_diagnostic(
    bundle_dir: str | Path,
    connection_target: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Read bundle_status on every call and return a conservative diagnostic."""

    status_path = Path(bundle_dir) / "bundle_status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except OSError:
        return (
            diagnostic_from_bundle_status({}, connection_target=connection_target),
            "bundle_status_unavailable",
        )
    except (UnicodeError, json.JSONDecodeError):
        return (
            diagnostic_from_bundle_status({}, connection_target=connection_target),
            "bundle_status_invalid",
        )
    if not isinstance(payload, dict):
        return (
            diagnostic_from_bundle_status({}, connection_target=connection_target),
            "bundle_status_invalid",
        )

    v5_connections = (
        payload.get("client_connections")
        if payload.get("schema_version") == "mcp-bundle-status-v5"
        and isinstance(payload.get("client_connections"), dict)
        else None
    )
    selected_record = (
        v5_connections.get(connection_target)
        if isinstance(v5_connections, dict)
        and isinstance(v5_connections.get(connection_target), dict)
        else None
    )
    selected_effective = (
        selected_record.get("effective")
        if isinstance(selected_record, dict)
        and isinstance(selected_record.get("effective"), dict)
        else {}
    )
    selected_last_attempt = (
        selected_record.get("last_attempt")
        if isinstance(selected_record, dict)
        and isinstance(selected_record.get("last_attempt"), dict)
        else {}
    )
    if selected_record is not None:
        attempt_id = str(
            selected_effective.get("attempt_id")
            or selected_last_attempt.get("id")
            or ""
        ).strip() or None
    else:
        attempt_id = str(
            payload.get("installation_attempt_id")
            or payload.get("attempt_id")
            or ""
        ).strip() or None
    is_claude_desktop = connection_target == "claude-desktop"
    is_claude_code = connection_target == "claude-code"
    if is_claude_desktop:
        fingerprint_field = "claude_desktop_config_fingerprint"
        path_field: str | None = "claude_desktop_config_path"
        registration_field = "claude_desktop_config_registered"
    elif is_claude_code:
        fingerprint_field = "claude_code_config_fingerprint"
        path_field = None
        registration_field = "claude_code_registered"
    else:
        fingerprint_field = "installed_config_fingerprint"
        path_field = "direct_config_path"
        registration_field = "direct_config_registered"
    if selected_record is not None:
        config_fingerprint = str(
            selected_effective.get("config_entry_fingerprint") or ""
        ).strip() or None
    else:
        config_fingerprint = str(
            payload.get(fingerprint_field)
            or payload.get("config_fingerprint")
            or ""
        ).strip() or None
    legacy_projection_matches_target = (
        selected_record is None or payload.get("legacy_projection_target") == connection_target
    )
    if (
        path_field
        and legacy_projection_matches_target
        and payload.get(registration_field) is True
    ):
        installed_config_path = str(payload.get(path_field) or "").strip()
        try:
            current_config_path = Path(installed_config_path)
            if not installed_config_path or not current_config_path.is_file():
                config_fingerprint = None
            else:
                config_fingerprint = "sha256:" + hashlib.sha256(
                    current_config_path.read_bytes()
                ).hexdigest()
        except OSError:
            config_fingerprint = None
    diagnostic = diagnostic_from_bundle_status(
        payload,
        attempt_id=attempt_id,
        config_fingerprint=config_fingerprint,
        checked_at=payload.get("updated_at") or payload.get("generated_at"),
        connection_target=connection_target,
    )
    return diagnostic, None


def _refresh_mcp_connection_observation(
    bundle_dir: str | Path,
    connection_target: str,
    server_name: str,
) -> tuple[bool, str]:
    """Run a path-free, read-only Desktop observation and refresh its status fields."""

    if connection_target not in {"chatgpt-desktop-local", "claude-desktop"}:
        return False, "target_not_observable"
    status_path = Path(bundle_dir) / "bundle_status.json"
    output = io.StringIO()
    refresh_args = [
            "--target",
            connection_target,
            "--server-name",
            server_name,
            "--bundle-status",
            str(status_path),
            "--bundle-dir",
            str(Path(bundle_dir)),
        ]
    if connection_target == "chatgpt-desktop-local":
        refresh_args.append("--adopt-manual-registration")
    exit_code = refresh_mcp_client_connection(
        refresh_args,
        stdout=output,
    )
    try:
        result = json.loads(output.getvalue())
    except (TypeError, json.JSONDecodeError):
        return False, "refresh_report_invalid"
    if not isinstance(result, dict) or result.get("status_updated") is not True:
        return False, str(result.get("error_code") or "refresh_failed")
    if exit_code == 0 and result.get("ok") is True:
        return True, "observation_ready"
    return True, "observation_recorded_pending"


def _mcp_connection_diagnostic_rows(diagnostic: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a compact, path-free table for the operator screen."""

    stages = diagnostic.get("stages") if isinstance(diagnostic.get("stages"), dict) else {}
    rows: list[dict[str, Any]] = []
    stage_order = diagnostic.get("stage_order")
    if not isinstance(stage_order, list):
        stage_order = list(MCP_CONNECTION_STAGE_ORDER)
    for stage_name in stage_order:
        stage = stages.get(stage_name) if isinstance(stages.get(stage_name), dict) else {}
        evidence = stage.get("evidence") if isinstance(stage.get("evidence"), dict) else {}
        safe_evidence_keys = sorted(
            key
            for key, value in evidence.items()
            if key != "config_fingerprint" and value not in (None, False, "", [], {})
        )
        state = str(stage.get("state") or "not_checked")
        rows.append(
            {
                "단계": MCP_CONNECTION_STAGE_LABELS.get(stage_name, stage_name),
                "상태": MCP_CONNECTION_STATE_LABELS.get(state, state),
                "시도 ID": str(stage.get("attempt_id") or "없음"),
                "확인 시각": str(stage.get("checked_at") or "미확인"),
                "사유 코드": str(stage.get("reason_code") or "not_checked"),
                "증거 항목": ", ".join(safe_evidence_keys) if safe_evidence_keys else "없음",
            }
        )
    return rows


def _mcp_kordoc_preflight(
    target_repository: JsonRepository,
    document_ids: list[str],
    *,
    command: str,
) -> dict[str, Any]:
    """Return the non-mutating Kordoc evidence gate used by the bundle UI.

    Bundle generation can be expensive (especially for large approved runtimes),
    so the UI must check persisted parser evidence before starting the export.
    Installing Kordoc does not retroactively change a document that was
    preprocessed while the command was unavailable; those documents must be
    reprocessed and reviewed again.
    """

    normalized_ids = list(dict.fromkeys(str(value or "").strip() for value in document_ids if str(value or "").strip()))
    summary = _kordoc_table_parser_evidence_summary(target_repository, normalized_ids)
    missing = [
        item
        for item in summary.get("documents", [])
        if item.get("required") and not (item.get("status") == "parsed" and item.get("parser") == "kordoc")
    ]
    command_status = kordoc_table_command_status(str(command or ""))
    return {
        "ready": not missing,
        "required_document_count": int(summary.get("required_document_count") or 0),
        "parsed_document_count": int(summary.get("parsed_document_count") or 0),
        "missing": missing,
        "documents": summary.get("documents") or [],
        "required_file_types": sorted(KORDOC_TABLE_REQUIRED_FILE_TYPES),
        "command_status": command_status,
    }


def _safe_kordoc_reprocess_documents(
    target_settings: Settings,
    target_repository: JsonRepository,
    document_ids: list[str],
    *,
    quality_profile: QualityProfileConfig | None = None,
    progress_callback: Callable[[int, str, int | None, int | None], None] | None = None,
) -> list[KordocReprocessingResult]:
    """Reprocess source documents as isolated drafts and verify Kordoc evidence."""

    normalized_ids = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in document_ids
            if str(value or "").strip()
        )
    )
    service = KordocReprocessingService(
        target_settings,
        target_repository,
        quality_profile_config=quality_profile,
    )
    results: list[KordocReprocessingResult] = []
    total = len(normalized_ids)
    for index, source_document_id in enumerate(normalized_ids):
        def report(
            percent: int,
            message: str,
            current: int | None = None,
            current_total: int | None = None,
            *,
            offset: int = index,
        ) -> None:
            mapped = int(((offset + max(0, min(100, int(percent))) / 100) / max(total, 1)) * 100)
            if progress_callback is not None:
                progress_callback(
                    mapped,
                    f"{offset + 1}/{total} · {message}",
                    current,
                    current_total,
                )

        results.append(service.recover(source_document_id, progress_callback=report))
    return results


def _replace_workflow_document_id(source_document_id: str, draft_document_id: str) -> None:
    """Switch the UI to a verified draft while preserving unrelated batch entries."""

    source_id = str(source_document_id or "").strip()
    draft_id = str(draft_document_id or "").strip()
    if not source_id or not draft_id or source_id == draft_id:
        return

    def replaced(values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        output: list[str] = []
        for value in values:
            current = str(value or "").strip()
            if not current:
                continue
            current = draft_id if current == source_id else current
            if current not in output:
                output.append(current)
        return output

    workflow_ids = replaced(st.session_state.get(WORKFLOW_DOCUMENT_IDS_KEY))
    if draft_id not in workflow_ids:
        workflow_ids.append(draft_id)
    st.session_state[WORKFLOW_DOCUMENT_IDS_KEY] = workflow_ids

    selected_ids = replaced(st.session_state.get(WORKFLOW_SELECTED_DOCUMENT_IDS_KEY))
    if not selected_ids or source_id in {
        str(value or "").strip()
        for value in (st.session_state.get(WORKFLOW_SELECTED_DOCUMENT_IDS_KEY) or [])
    }:
        if draft_id not in selected_ids:
            selected_ids.append(draft_id)
    st.session_state[WORKFLOW_SELECTED_DOCUMENT_IDS_KEY] = selected_ids
    if str(st.session_state.get("document_id") or "").strip() == source_id:
        st.session_state["document_id"] = draft_id
    _invalidate_document_context_cache()


def _kordoc_auto_reprocess_attempt_key(
    target_repository: JsonRepository,
    document_id: str,
) -> str:
    document = target_repository.get_document(document_id)
    file_hash = str(getattr(document, "file_hash", "") or "")[:16]
    return f"{KORDOC_AUTO_REPROCESS_ATTEMPT_PREFIX}:{document_id}:{file_hash}"


def _kordoc_installer_candidates() -> list[Path]:
    """Return source and portable locations for the explicit Kordoc setup script."""

    candidates: list[Path] = []
    try:
        executable_dir = Path(sys.executable).resolve().parent
        candidates.append(executable_dir / "INSTALL_KORDOC_KO.ps1")
    except OSError:
        executable_dir = None
    try:
        candidates.append(Path(sys.prefix).resolve() / "INSTALL_KORDOC_KO.ps1")
    except OSError:
        pass
    if executable_dir is not None:
        candidates.append(executable_dir.parent / "INSTALL_KORDOC_KO.ps1")
    candidates.extend(
        (
            PROJECT_ROOT / "INSTALL_KORDOC_KO.ps1",
            PROJECT_ROOT / "packaging" / "INSTALL_KORDOC_KO.ps1",
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            unique.append(candidate)
    return unique


def _run_kordoc_installer() -> dict[str, Any]:
    """Run the explicit Windows installer and return redacted operator output."""

    if sys.platform != "win32":
        return {"ok": False, "error": "windows_only", "output": ""}
    candidates = _kordoc_installer_candidates()
    if not candidates:
        return {"ok": False, "error": "installer_missing", "output": ""}
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(candidates[0]),
                "-PersistUserPath",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "installer_timeout", "output": ""}
    except OSError:
        return {"ok": False, "error": "installer_unavailable", "output": ""}
    output = redact_sensitive_paths("\n".join(part for part in (completed.stdout, completed.stderr) if part))
    return {
        "ok": completed.returncode == 0,
        "error": "" if completed.returncode == 0 else "installer_failed",
        "output": output[-4000:],
    }


def _candidate_operator_paths(raw_path: object) -> list[Path]:
    text = str(raw_path or "").strip()
    if not text:
        return []
    path = Path(text)
    if path.is_absolute():
        return [path]
    return [path, PROJECT_ROOT / path]


def _mcp_bundle_created(ctx: dict | None) -> bool:
    if not ctx:
        return False
    document_id = str(ctx["document_id"])
    try:
        state = st.session_state[_mcp_bundle_state_key(document_id)]
    except KeyError:
        state = None
    if not isinstance(state, dict) or not state.get("written"):
        return False
    if state.get("document_id") != document_id:
        return False
    bundle_dirs = _candidate_operator_paths(state.get("bundle_dir"))
    zip_paths = _candidate_operator_paths(state.get("zip"))
    has_connect_script = any((path / "connect_mcp_client.ps1").exists() for path in bundle_dirs)
    has_zip = any(path.exists() for path in zip_paths)
    return has_connect_script and has_zip


def _workflow_states(ctx: dict | None) -> list[bool]:
    """각 단계(1~5)의 완료 여부."""
    step1 = ctx is not None
    step2 = bool(ctx and ctx["quality_report"] and ctx["quality_report"].passed)
    step3 = bool(ctx and ctx["approved_count"] > 0)
    step4 = bool(ctx and ctx["mcp_connection_gate"].get("ready"))
    step5 = _mcp_bundle_created(ctx)
    return [step1, step2, step3, step4, step5]


def _next_action(ctx: dict | None) -> tuple[str, str]:
    """(안내 문구, 이동할 화면)"""
    if ctx is None:
        return ("규정 문서 파일을 올리고 '전처리 시작'을 누르세요.", NAV_PREPROCESS)
    if not ctx["quality_report"] or not ctx["quality_report"].passed:
        return ("전처리 결과와 품질 검사 내용을 확인하세요.", NAV_RESULTS)
    if ctx["approved_count"] <= 0:
        return ("결과를 사람이 확인한 뒤 '승인'을 진행하세요.", NAV_APPROVAL)
    if not ctx["mcp_connection_gate"].get("ready"):
        return ("승인한 내용을 색인(AI에 등록)하세요. ③ 화면의 마지막 단계입니다.", NAV_APPROVAL)
    if not _mcp_bundle_created(ctx):
        return ("승인 데이터 검색 점검 후 MCP 설정 묶음을 생성하세요. Claude, ChatGPT, 내부 AI 연결용 ④ 단계입니다.", NAV_MCP)
    return ("MCP 설정 묶음까지 생성됐습니다. ④ 화면에서 검색 점검과 연결 상태를 확인해 보세요.", NAV_MCP)


# ---------------------------------------------------------------------------
# 페이지: 시작하기(홈)
# ---------------------------------------------------------------------------

def _documents_for_selected_institution() -> list[object]:
    if not institution_registry or not institution_registry.profiles:
        return []
    profile_id = _selected_institution_profile_id()
    profile = institution_registry.profiles.get(profile_id)
    if profile is None:
        return []
    documents = []
    for document in repository.list_documents():
        document_tenant_id = str(getattr(document, "tenant_id", "") or "").strip()
        if document_tenant_id and document_tenant_id != _local_operator_tenant_id():
            continue
        if _document_belongs_to_institution_profile(document, profile_id):
            documents.append(document)
    return documents


def _workflow_document_label(document: object) -> str:
    filename = str(getattr(document, "filename", "") or "").strip()
    title = str(getattr(document, "document_name", "") or "").strip()
    if not title and filename:
        title = Path(filename).stem
    title = title or str(getattr(document, "document_id", "") or "규정")[:12]
    version = str(getattr(document, "regulation_version", "") or "").strip()
    revision_date = str(getattr(document, "revision_date", "") or "").strip()
    details = " · ".join(value for value in (version, revision_date) if value)
    return f"{title} · {details}" if details else title


def _workflow_documents() -> list[object]:
    raw_document_ids = st.session_state.get(WORKFLOW_DOCUMENT_IDS_KEY)
    document_ids = [
        str(value or "").strip()
        for value in raw_document_ids
        if str(value or "").strip()
    ] if isinstance(raw_document_ids, list) else []
    active_document_id = str(st.session_state.get("document_id") or "").strip()
    if active_document_id and active_document_id not in document_ids:
        document_ids.append(active_document_id)

    selected_profile_id = _selected_institution_profile_id()
    documents: list[object] = []
    for document_id in document_ids:
        document = repository.get_document(document_id)
        if document is None:
            continue
        if selected_profile_id and not _document_belongs_to_institution_profile(document, selected_profile_id):
            continue
        documents.append(document)
    return documents


def _selected_workflow_document_ids() -> list[str]:
    documents = _workflow_documents()
    candidate_ids = [str(getattr(document, "document_id", "") or "") for document in documents]
    raw_selected_ids = st.session_state.get(WORKFLOW_SELECTED_DOCUMENT_IDS_KEY)
    if not isinstance(raw_selected_ids, list):
        return candidate_ids
    selected_ids = {str(value or "").strip() for value in raw_selected_ids}
    return [document_id for document_id in candidate_ids if document_id in selected_ids]


def _render_workflow_document_directory(*, page_key: str) -> list[str]:
    """Keep one upload batch together while loading only the opened document in detail."""
    documents = _workflow_documents()
    if not documents:
        return []

    candidate_ids = [str(getattr(document, "document_id", "") or "") for document in documents]
    raw_selected_ids = st.session_state.get(WORKFLOW_SELECTED_DOCUMENT_IDS_KEY)
    selected_ids = (
        {str(value or "").strip() for value in raw_selected_ids}
        if isinstance(raw_selected_ids, list)
        else set(candidate_ids)
    )
    active_document_id = str(st.session_state.get("document_id") or "").strip()
    clicked_document_id = ""
    current_selected_ids: list[str] = []

    st.markdown("### 함께 처리할 규정 디렉터리")
    st.caption(
        "올린 규정은 모두 기본 선택됩니다. 규정 열기를 누르면 해당 규정의 청크와 원문·전처리 결과, 개정 이력을 확인합니다."
    )
    for document in documents:
        document_id = str(getattr(document, "document_id", "") or "")
        label = _workflow_document_label(document)
        include_col, open_col, state_col = st.columns([0.08, 0.72, 0.20], vertical_alignment="center")
        with include_col:
            included = st.checkbox(
                f"{label} 포함",
                value=document_id in selected_ids,
                key=f"workflow-document-selected-{document_id}",
                label_visibility="collapsed",
            )
        if included:
            current_selected_ids.append(document_id)
        with open_col:
            if st.button(
                f"규정 열기 · {label}",
                key=f"workflow-document-open-{page_key}-{document_id}",
                width="stretch",
                type="primary" if document_id == active_document_id else "secondary",
                disabled=not included,
            ):
                clicked_document_id = document_id
        with state_col:
            st.caption("현재 상세 보기" if document_id == active_document_id else "함께 처리")

    st.session_state[WORKFLOW_DOCUMENT_IDS_KEY] = candidate_ids
    st.session_state[WORKFLOW_SELECTED_DOCUMENT_IDS_KEY] = current_selected_ids
    if not current_selected_ids:
        st.warning("다음 단계로 넘길 규정을 한 개 이상 선택해 주세요.")
        return []

    target_document_id = clicked_document_id
    if active_document_id not in current_selected_ids:
        target_document_id = current_selected_ids[0]
    if target_document_id and target_document_id != active_document_id:
        st.session_state["document_id"] = target_document_id
        _invalidate_document_context_cache()
        _queue_workflow_navigation(
            st.session_state.get("nav_page", NAV_RESULTS),
            label=f"{_workflow_document_label(repository.get_document(target_document_id))} 불러오기",
        )
        st.rerun()

    st.caption(f"선택된 규정 {len(current_selected_ids):,}개 / 작업 묶음 {len(documents):,}개")
    return current_selected_ids


def _regulation_version_history_rows(document: object) -> list[dict[str, object]]:
    regulation_id = str(getattr(document, "regulation_id", "") or "").strip()
    if not regulation_id:
        return []
    versions = repository.find_documents_by_regulation(
        regulation_id,
        profile_id=getattr(document, "profile_id", None),
        tenant_id=getattr(document, "tenant_id", None),
    )
    current_document_id = str(getattr(document, "document_id", "") or "")
    current_index = next(
        (index for index, item in enumerate(versions) if item.document_id == current_document_id),
        -1,
    )
    rows: list[dict[str, object]] = []
    for index, item in enumerate(versions):
        if index == current_index:
            relation = "현재 선택"
        elif current_index >= 0 and index == current_index - 1:
            relation = "직전 개정판"
        elif current_index >= 0 and index < current_index:
            relation = "이전 개정판"
        elif current_index >= 0 and index == current_index + 1:
            relation = "다음 개정판"
        else:
            relation = "이후 개정판"
        rows.append(
            {
                "관계": relation,
                "규정": _workflow_document_label(item),
                "문서 ID": item.document_id,
                "상태": item.regulation_status,
                "개정일": item.revision_date or "",
                "효력 시작": item.effective_from or "",
                "효력 종료": item.effective_to or "",
            }
        )
    return rows


def _render_regulation_version_history(document: object) -> None:
    rows = _regulation_version_history_rows(document)
    if not rows:
        st.info("이 규정은 아직 연결된 개정 이력이 없습니다.")
        return
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _page_home(ctx: dict | None) -> None:
    _render_hero(
        "규정 문서(PDF·HWP·HWPX·DOCX)를 올리면 조문·표·별표를 AI 검색에 맞게 정리해 드립니다. "
        "아래 순서대로 한 단계씩 진행하시면 됩니다."
    )
    _render_operator_project_controls(NAV_HOME)
    _render_api_key_setup_cta("home")

    selected_profile_id = _selected_institution_profile_id()
    selected_profile = institution_registry.profiles.get(selected_profile_id) if institution_registry else None
    if selected_profile is not None:
        institution_documents = _documents_for_selected_institution()
        institution_label = selected_profile.institution_name or selected_profile.display_name or selected_profile_id
        st.markdown(f"### {html.escape(institution_label)} 규정 현황")
        metric_col1, metric_col2, metric_col3 = st.columns(3)
        with metric_col1:
            st.metric("등록 문서", len(institution_documents))
        with metric_col2:
            st.metric("현재 선택 기관", selected_profile.display_name or selected_profile_id)
        with metric_col3:
            st.metric("검색 범위", "기관별 최신 승인본")
        if institution_documents:
            st.markdown("#### 규정 트리")
            regulation_groups = group_documents_by_regulation(institution_documents)
            for group_key, group_documents in sorted(
                regulation_groups.items(),
                key=lambda item: str(item[0][1] or getattr(item[1][0], "document_name", "")),
            ):
                _group_profile_id, regulation_id = group_key
                regulation_label = regulation_id or getattr(group_documents[0], "document_name", "미지정 규정")
                current_candidates = []
                for candidate in group_documents:
                    candidate_metadata = read_regulation_metadata(candidate)
                    if (
                        candidate_metadata.profile_id
                        and candidate_metadata.regulation_id
                        and candidate_metadata.version
                        and candidate_metadata.effective_from
                        and candidate_metadata.status == "approved"
                    ):
                        current_candidates.append(candidate)
                latest_document = latest_active_version(current_candidates, active_statuses={"approved"})
                with st.expander(f"{regulation_label} ({len(group_documents)}개 버전)", expanded=True):
                    for version_document in sorted(
                        group_documents,
                        key=lambda item: str(getattr(item, "created_at", "")),
                    ):
                        metadata = read_regulation_metadata(version_document)
                        version_label = metadata.version or "버전 미지정"
                        active_label = "현재 활성 후보" if latest_document is version_document else "이력"
                        st.caption(
                            f"{version_label} · {active_label} · 상태: {metadata.status or '미지정'} "
                            f"· 효력: {metadata.effective_from or '-'} ~ {metadata.effective_to or '-'}"
                        )
            rows = []
            for document in sorted(
                institution_documents,
                key=lambda item: str(getattr(item, "document_name", "") or getattr(item, "filename", "")),
            ):
                rows.append(
                    {
                        "규정 문서": getattr(document, "document_name", "") or getattr(document, "filename", ""),
                        "상태": "현재 작업 문서" if ctx and document.document_id == ctx["document_id"] else "등록됨",
                        "문서 ID": str(getattr(document, "document_id", ""))[:12],
                    }
                )
            st.markdown("| 규정 문서 | 상태 | 문서 ID | 작업 |\n|---|---|---|---|")
            for document in sorted(
                institution_documents,
                key=lambda item: str(getattr(item, "document_name", "") or getattr(item, "filename", "")),
            ):
                name = getattr(document, "document_name", "") or getattr(document, "filename", "")
                status = "현재 작업 문서" if ctx and document.document_id == ctx["document_id"] else "등록됨"
                row_cols = st.columns([4, 2, 2, 1])
                row_cols[0].write(name)
                row_cols[1].write(status)
                row_cols[2].write(str(getattr(document, "document_id", ""))[:12])
                if row_cols[3].button("삭제", key=f"home-delete-{document.document_id}"):
                    source_path = DocumentService(settings=settings, repository=repository).path_for(document)
                    deleted = repository.delete_document(document.document_id)
                    if source_path.exists():
                        source_path.unlink()
                    if ctx and document.document_id == ctx.get("document_id"):
                        st.session_state.pop("document_id", None)
                    st.success(f"{name} 작업을 삭제했습니다.")
                    st.rerun()
        else:
            st.info("이 기관에 등록된 규정 문서가 없습니다. 문서 업로드부터 시작하세요.")

    message, target = _next_action(ctx)
    st.markdown(f'<div class="rr-next-box">👉 <b>지금 할 일:</b> {message}</div>', unsafe_allow_html=True)
    _render_workflow_next_button(f"바로가기: {target}", target, key="home-next-action")

    st.markdown("### 전처리 진행 방식")
    st.caption("모든 문서는 항상 아래 3단계로 처리됩니다: 파서 초안 → AI 검수 → 휴먼 승인.")
    _render_pipeline_stages(0)

    st.markdown("### 작업 순서")
    states = _workflow_states(ctx)
    current_index = next((i for i, done in enumerate(states) if not done), None)
    steps = [
        ("1단계", "문서 올려서 전처리", "규정 파일을 올리면 파서가 조문 단위로 1차 정리하고, 이어서 AI 검수가 함께 실행됩니다.", NAV_PREPROCESS),
        ("2단계", "결과 확인", "정리 결과와 품질 검사, 그리고 AI 검수가 짚은 부분을 화면에서 확인합니다.", NAV_RESULTS),
        ("3단계", "검수하고 승인", "사람이 최종 확인을 마친 내용에만 '승인'을 하고 AI에 등록(색인)합니다.", NAV_APPROVAL),
        ("4단계", "MCP 생성·AI 연결", "Claude, ChatGPT, 내부 AI에 붙일 MCP 설정 JSON과 setup bundle을 생성합니다.", NAV_MCP),
    ]
    cols = st.columns(len(steps))
    for i, (num, title, desc, nav) in enumerate(steps):
        done = states[i]
        current = current_index == i
        card_class = "done" if done else ("current" if current else "")
        state_class = "done" if done else ("current" if current else "todo")
        state_text = "✅ 완료" if done else ("🟡 지금 할 차례" if current else "대기")
        with cols[i]:
            st.markdown(
                f"""
                <div class="rr-step-card {card_class}">
                  <span class="rr-step-num">{num}</span>
                  <h4>{title}</h4>
                  <p>{desc}</p>
                  <div class="rr-step-state {state_class}">{state_text}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.button("이동", key=f"home-goto-{i}", on_click=_go, args=(nav,), width="stretch")

    st.markdown(
        '<div class="rr-section-note">이 화면은 로컬 운영자 전용입니다. '
        "인증이 필요한 공유/테넌트 분리 배포에서는 FastAPI 경로를 사용하세요.</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# 페이지: ① 문서 올려서 전처리
# ---------------------------------------------------------------------------

def _page_preprocess() -> None:
    st.markdown("## ① 문서 올려서 전처리")
    _render_operator_project_controls(NAV_PREPROCESS)
    _render_pipeline_stages(PIPELINE_STAGE_PARSER)
    st.markdown(
        '<div class="rr-help">규정 파일을 올리고 문서 정보를 확인한 뒤 <b>전처리 시작</b> 버튼만 누르면 됩니다. '
        "전처리를 시작하면 <b>파서 초안</b>과 <b>AI 검수</b>가 함께 실행됩니다. 나머지 설정은 그대로 두셔도 됩니다.</div>",
        unsafe_allow_html=True,
    )

    _render_api_key_setup_cta("preprocess")

    st.markdown("### 1. 파일 올리기")
    uploaded = st.file_uploader(
        "문서 업로드",
        type=["pdf", "docx", "hwpx", "hwp"],
        accept_multiple_files=True,
        key="regulation_document_upload",
        help="PDF, HWP, HWPX, DOCX 파일을 이 영역으로 드래그하거나 Browse files 버튼으로 선택하세요.",
    )
    uploaded_files = sorted(
        _uploaded_file_list(uploaded),
        key=lambda item: regulation_upload_sort_key(str(item.name)),
    )
    selected_upload_bytes = sum(_uploaded_file_size(uploaded_file) for uploaded_file in uploaded_files)
    st.caption(
        "PDF, HWP, HWPX, DOCX 규정 문서를 위 점선 박스 안으로 끌어놓거나 Browse files 버튼으로 선택하세요. "
        "드롭이 성공하면 아래에 파일명이 바로 표시됩니다. 여러 파일을 한 번에 끌어오면 순서대로 저장하고 전처리합니다."
    )
    if uploaded_files:
        st.caption(f"선택된 파일: {len(uploaded_files)}개, 총 {_format_upload_mb(selected_upload_bytes)}")
        _render_selected_upload_files(uploaded_files)
    st.markdown("### 2. 문서 정보 확인")
    profile_id = ""
    profile_defaults: dict[str, object] = {}
    if institution_registry_error:
        st.error(institution_registry_error)
        if settings.institution_profiles_strict:
            st.stop()
    if institution_registry and institution_registry.profiles:
        profile_ids = sorted(institution_registry.profiles)
        profile_id = _selected_institution_profile_id()
        if profile_id not in profile_ids:
            profile_id = institution_registry.default_profile_id or profile_ids[0]
            st.session_state[SELECTED_INSTITUTION_PROFILE_KEY] = profile_id
        selected_profile = institution_registry.profiles[profile_id]
        st.info(
            f"현재 기관: **{selected_profile.display_name or selected_profile.institution_name or profile_id}** "
            "(기관을 변경하려면 왼쪽 사이드바의 기관 전환을 사용하세요.)"
        )
        profile_defaults = apply_institution_profile_to_metadata(
            {"profile_id": profile_id},
            institution_registry,
            strict=False,
            enforce_required=False,
        )
        required_fields = institution_registry.required_row_fields_for(profile_id, strict=False)
        if required_fields:
            st.caption("필수 입력 항목: " + ", ".join(required_fields))
    else:
        profile_id = st.text_input("기관 프로필 ID", value="")

    institution_name = str(profile_defaults.get("institution_name") or "")
    st.caption(f"기관명: {institution_name or '선택한 기관 프로필에서 자동 적용'}")
    document_name = ""

    upload_sources: list[dict[str, object]] = []
    pending_paths: list[Path] = []
    selected_pending_paths: list[Path] = []
    if profile_id:
        try:
            current_pending_by_name: dict[str, Path] = {}
            for uploaded_file in uploaded_files:
                current_pending_by_name[str(getattr(uploaded_file, "name", ""))] = _persist_pending_upload(
                    profile_id,
                    uploaded_file,
                )
            pending_paths = _pending_upload_paths(profile_id)
            current_pending_paths = set(current_pending_by_name.values())
            pending_only = [
                path for path in pending_paths
                if path not in current_pending_paths
            ]
            if pending_only:
                st.markdown("#### 저장된 대기 파일")
                for path in pending_only:
                    if st.checkbox(
                        f"{_pending_upload_display_name(path)} · {_format_upload_mb(path.stat().st_size)}",
                        key=f"pending-upload-{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]}",
                    ):
                        selected_pending_paths.append(path)
                delete_options = {
                    f"{_pending_upload_display_name(path)} · {_format_upload_mb(path.stat().st_size)}": path
                    for path in pending_only
                }
                delete_labels = st.multiselect(
                    "삭제할 대기 작업",
                    options=list(delete_options),
                    key="pending-upload-delete-selection",
                    help="아직 전처리하지 않은 대기 파일만 삭제합니다. 이미 전처리된 규정 결과는 삭제하지 않습니다.",
                )
                if st.button(
                    "선택한 대기 작업 삭제",
                    key="pending-upload-delete-button",
                    disabled=not delete_labels,
                ):
                    deleted_count = 0
                    for label in delete_labels:
                        path = delete_options.get(label)
                        if path is not None and path.exists():
                            path.unlink()
                            deleted_count += 1
                    st.session_state.pop("pending-upload-delete-selection", None)
                    st.success(f"대기 작업 {deleted_count}개를 삭제했습니다.")
                    st.rerun()
            upload_sources.extend(
                {
                    "kind": "current",
                    "file": uploaded_file,
                    "filename": str(getattr(uploaded_file, "name", "pending_upload")),
                    "size": _uploaded_file_size(uploaded_file),
                    "pending_path": current_pending_by_name.get(str(getattr(uploaded_file, "name", ""))),
                }
                for uploaded_file in uploaded_files
            )
            upload_sources.extend(
                {
                    "kind": "pending",
                    "path": path,
                    "filename": _pending_upload_display_name(path),
                    "size": path.stat().st_size,
                    "pending_path": path,
                }
                for path in selected_pending_paths
            )
        except (OSError, ValueError) as exc:
            st.error(f"대기 파일을 저장할 수 없습니다: {exc}")
            upload_sources = []
        if pending_paths:
            st.info(
                f"이 기관의 대기 중 규정 파일 {len(pending_paths)}개가 저장되어 있습니다. "
                "현재 화면에서 선택한 파일은 바로 전처리할 수 있고, 이전에 저장한 파일은 아래 목록에서 골라 처리할 수 있습니다."
            )

    with st.expander("추가 정보 입력 (선택 사항 — 몰라도 됩니다)", expanded=False):
        source_system = st.text_input("출처 시스템", value=profile_defaults.get("source_system") or "")
        source_url = st.text_input("출처 URL", value=profile_defaults.get("source_url") or "")
        source_record_id = st.text_input("출처 레코드 ID", value="")
        source_file_id = st.text_input("출처 파일 ID", value="")
        source_disclosure_date = st.text_input("공개일", value="")
        source_posted_date = st.text_input("게시일", value="")

    existing_institution_documents = _documents_for_selected_institution()
    if existing_institution_documents:
        st.markdown("#### 이전 전처리 작업 관리")
        document_options = {
            f"{getattr(document, 'document_name', '') or getattr(document, 'filename', '')} · {str(getattr(document, 'document_id', ''))[:12]}": document
            for document in existing_institution_documents
        }
        delete_document_labels = st.multiselect(
            "삭제할 이전 전처리 작업",
            options=list(document_options),
            key="processed-document-delete-selection",
            help="선택한 문서의 전처리 결과와 작업 기록을 삭제합니다. 원본 업로드 파일도 함께 삭제됩니다.",
        )
        if st.button(
            "선택한 전처리 작업 삭제",
            key="processed-document-delete-button",
            disabled=not delete_document_labels,
        ):
            deleted_count = 0
            for label in delete_document_labels:
                document = document_options.get(label)
                if document is None:
                    continue
                source_path = DocumentService(settings=settings, repository=repository).path_for(document)
                if repository.delete_document(document.document_id):
                    deleted_count += 1
                if source_path.exists():
                    source_path.unlink()
            st.session_state.pop("processed-document-delete-selection", None)
            if st.session_state.get("document_id") in {
                getattr(document_options.get(label), "document_id", "") for label in delete_document_labels
            }:
                st.session_state.pop("document_id", None)
            st.success(f"이전 전처리 작업 {deleted_count}개를 삭제했습니다.")
            st.rerun()
    if upload_sources:
        detected_rows = []
        for source in upload_sources:
            detected = infer_regulation_metadata(
                str(source["filename"]),
                existing_documents=existing_institution_documents,
                profile_id=profile_id,
                tenant_id=_local_operator_tenant_id(),
            )
            detected_rows.append(
                {
                    "파일": str(source["filename"]),
                    "인식한 규정명": detected.document_name,
                    "규정 식별자": detected.regulation_id,
                    "버전": detected.regulation_version,
                    "개정일": detected.revision_date,
                    "시행일": detected.effective_from,
                    "이전 승인본": detected.supersedes_document_id or "신규",
                }
            )
        st.markdown("#### 자동 인식한 규정 정보")
        st.dataframe(detected_rows, width="stretch", hide_index=True)
        st.caption(
            "파일별로 규정명·버전·날짜를 자동 인식합니다. 전처리할 때 본문의 개정일·시행일도 다시 확인하며, "
            "같은 규정의 승인된 이전 버전이 있으면 자동으로 개정 관계를 연결합니다."
        )

    regulation_id = ""
    regulation_version = ""
    revision_date = ""
    effective_from = ""
    effective_to = ""
    repealed_at = ""
    regulation_status = "draft"
    supersedes_document_id = ""
    manual_regulation_override = st.checkbox(
        "자동 인식값을 직접 수정",
        value=False,
        help="특수한 파일명 때문에 자동 인식이 맞지 않을 때만 사용하세요.",
    )
    if manual_regulation_override:
        st.warning("여러 파일을 올린 경우 아래 수정값이 모든 파일에 공통 적용됩니다.")
        with st.expander("규정 정보 직접 수정", expanded=True):
            existing_regulation_ids = sorted(
                {
                    str(getattr(item, "regulation_id", "") or "").strip()
                    for item in existing_institution_documents
                    if str(getattr(item, "regulation_id", "") or "").strip()
                }
            )
            if existing_regulation_ids:
                registration_mode = st.radio(
                    "등록 방식",
                    ["new", "revision"],
                    format_func=lambda value: "신규 규정" if value == "new" else "기존 규정 개정본",
                    horizontal=True,
                )
            else:
                registration_mode = "new"
                st.caption("이 기관에 연결된 규정 식별자가 없어 신규 규정으로 등록합니다.")
            if registration_mode == "revision":
                regulation_id = st.selectbox("개정할 규정", existing_regulation_ids)
            else:
                regulation_id = st.text_input(
                    "규정 식별자",
                    value="",
                    help="같은 규정의 개정본을 등록할 때 재사용하는 안정적인 식별자입니다.",
                )
            existing_versions = []
            if regulation_id.strip():
                existing_versions = repository.find_documents_by_regulation(
                    regulation_id,
                    profile_id=profile_id,
                    tenant_id=_local_operator_tenant_id(),
                )
                if existing_versions:
                    st.info(
                        "기존 버전: "
                        + ", ".join(
                            f"{getattr(item, 'regulation_version', None) or '미지정'} ({item.document_id[:12]})"
                            for item in existing_versions
                        )
                    )
            regulation_version = st.text_input("규정 버전", value="", placeholder="예: v1.0")
            revision_date = st.text_input("개정일", value="", placeholder="YYYY-MM-DD")
            effective_from = st.text_input("효력 시작일", value="", placeholder="YYYY-MM-DD")
            effective_to = st.text_input("효력 종료일", value="", placeholder="YYYY-MM-DD")
            repealed_at = st.text_input("폐지일", value="", placeholder="YYYY-MM-DD")
            regulation_status = st.selectbox(
                "규정 상태",
                ["draft", "pending_approval"],
                format_func=lambda value: {
                    "draft": "초안",
                    "pending_approval": "승인 대기",
                }.get(value, value),
            )
            if existing_versions:
                previous_version_options = [""] + [
                    item.document_id
                    for item in reversed(existing_versions)
                ]
                supersedes_document_id = st.selectbox(
                    "대체하는 이전 버전 (선택)",
                    previous_version_options,
                    format_func=lambda value: "선택 안 함" if not value else value,
                )
            else:
                supersedes_document_id = st.text_input(
                    "대체하는 이전 문서 ID (선택)",
                    value="",
                    help="개정본인 경우 이전 문서의 ID를 기록합니다.",
                )

    with st.expander("전문가 설정 (기본값 사용을 권장합니다)", expanded=False):
        max_chunk_chars = st.number_input("최대 청크 글자 수", min_value=500, max_value=10000, value=1800, step=100)
        overlap_chars = st.number_input("청크 겹침 글자 수", min_value=0, max_value=1000, value=120, step=20)
        chunk_mode = st.selectbox(
            "청크 방식",
            ["article", "paragraph", "hybrid"],
            index=0,
            format_func=lambda value: {
                "article": "조문 중심",
                "paragraph": "문단 중심",
                "hybrid": "혼합",
            }.get(value, value),
        )
        include_context_header = st.checkbox("위치/본문 헤더 포함", value=True)
        enable_table_extraction = st.checkbox("표/별표 추출 활성화", value=False)
        st.caption("AI 검수 초안은 기본 전처리 단계입니다. 실제 API 실행은 운영 설정, 예산 한도, 승인 기준을 만족할 때만 진행됩니다.")
        official_review_checkbox_kwargs: dict[str, object] = {
            "key": OFFICIAL_RAG_MCP_REVIEW_REQUIRED_KEY,
            "help": "끄면 품질과 연결 UX 확인용 미검수 프리뷰로만 취급합니다.",
        }
        if OFFICIAL_RAG_MCP_REVIEW_REQUIRED_KEY not in st.session_state:
            official_review_checkbox_kwargs["value"] = True
        official_review_required = st.checkbox(
            "휴먼리뷰 후 공식 RAG/MCP 사용",
            **official_review_checkbox_kwargs,
        )
        unreviewed_poc_review_acknowledged = True
        if not official_review_required:
            st.warning(UNREVIEWED_PREVIEW_WARNING_KO + "\n\n" + UNREVIEWED_PREVIEW_WARNING)
            unreviewed_poc_review_acknowledged = st.checkbox(
                "I understand this is Unreviewed PoC Review only and not official RAG/MCP.",
                value=False,
                key=UNREVIEWED_POC_REVIEW_ACK_KEY,
            )

    st.markdown("### 3. 전처리 시작")
    poc_review_needs_ack = bool(upload_sources and not official_review_required and not unreviewed_poc_review_acknowledged)
    if poc_review_needs_ack:
        st.warning("미검수 미리보기(Unreviewed PoC Review) 확인란에 체크해야 전처리를 시작할 수 있습니다.")
    if not upload_sources:
        st.info("먼저 위에서 문서 파일을 올려 주세요.")

    if upload_sources and st.button("전처리 시작", type="primary", disabled=poc_review_needs_ack):
        if quality_profile_error:
            st.error(f"품질 프로필 설정이 올바르지 않습니다: {quality_profile_error}")
            st.stop()

        upload_metadata = {
            "document_name": _blank_to_none(document_name),
            "institution_name": _blank_to_none(institution_name),
            "source_system": _blank_to_none(source_system),
            "source_url": _blank_to_none(source_url),
            "source_record_id": _blank_to_none(source_record_id),
            "source_file_id": _blank_to_none(source_file_id),
            "source_disclosure_date": _blank_to_none(source_disclosure_date),
            "source_posted_date": _blank_to_none(source_posted_date),
            "profile_id": _blank_to_none(profile_id),
            "regulation_id": _blank_to_none(regulation_id),
            "regulation_version": _blank_to_none(regulation_version),
            "revision_date": _blank_to_none(revision_date),
            "effective_from": _blank_to_none(effective_from),
            "effective_to": _blank_to_none(effective_to),
            "repealed_at": _blank_to_none(repealed_at),
            "regulation_status": regulation_status,
            "supersedes_document_id": _blank_to_none(supersedes_document_id),
        }
        upload_settings = settings
        if institution_registry:
            try:
                upload_metadata = apply_institution_profile_to_metadata(
                    upload_metadata,
                    institution_registry,
                    strict=settings.institution_profiles_strict,
                    enforce_required=settings.institution_profiles_strict,
                )
                profile = institution_registry.resolve(
                    upload_metadata.get("profile_id"),
                    strict=settings.institution_profiles_strict,
                )
            except ValueError as exc:
                st.error(str(exc))
                st.stop()
            if profile is not None and profile.max_upload_mb:
                upload_settings = replace(settings, max_upload_mb=profile.max_upload_mb)

        upload_repository = JsonRepository(upload_settings)
        upload_document_service = DocumentService(upload_settings, upload_repository)
        upload_processing_service = ProcessingService(
            upload_settings,
            upload_repository,
            quality_profile_config=quality_profile_config,
        )
        options = ChunkOptions(
            max_chunk_chars=max_chunk_chars,
            overlap_chars=overlap_chars,
            chunk_mode=chunk_mode,
            include_context_header=include_context_header,
            enable_table_extraction=enable_table_extraction,
            enable_agent_review=True,
        )
        max_single_upload_bytes = int(upload_settings.max_upload_mb) * 1024 * 1024
        max_batch_upload_bytes = int(getattr(upload_settings, "max_batch_upload_mb", upload_settings.max_upload_mb)) * 1024 * 1024
        oversized_files = [
            f"{source['filename']} ({_format_upload_mb(int(source['size']))})"
            for source in upload_sources
            if int(source["size"]) > max_single_upload_bytes
        ]
        if oversized_files:
            st.error(
                f"파일당 업로드 한도는 {upload_settings.max_upload_mb}MB입니다. "
                + ", ".join(oversized_files)
            )
            st.stop()
        selected_source_bytes = sum(int(source["size"]) for source in upload_sources)
        selected_upload_bytes = selected_source_bytes
        if selected_source_bytes > max_batch_upload_bytes:
            st.error(
                f"한 번에 올릴 수 있는 총 용량은 {getattr(upload_settings, 'max_batch_upload_mb', upload_settings.max_upload_mb)}MB입니다. "
                f"현재 선택 용량은 {_format_upload_mb(selected_upload_bytes)}입니다."
            )
            st.stop()
        max_batch_upload_files = int(getattr(upload_settings, "max_batch_upload_files", 100))
        if len(upload_sources) > max_batch_upload_files:
            st.error(
                f"한 번에 올릴 수 있는 파일은 최대 {max_batch_upload_files}개입니다. "
                f"현재 선택한 파일은 {len(uploaded_files)}개입니다. 기관 전체 규정은 여러 묶음으로 나눠 올려 주세요."
            )
            st.stop()

        completed_documents = []
        total_files = len(upload_sources)
        with st.status(f"{total_files}개 문서를 전처리하는 중입니다...", expanded=True) as status:
            progress_bar = st.progress(0, text="Saving uploaded file")
            progress_text = st.empty()
            regulation_progress_box = st.empty()
            file_status_rows = [
                {"filename": str(source["filename"]), "status": "대기", "percent": 0}
                for source in upload_sources
            ]
            file_status_box = st.empty()
            _render_upload_file_progress(file_status_box, file_status_rows)

            def _update_file_progress(
                file_index: int,
                filename: str,
                file_fraction: float,
                message: str,
                *,
                status_label: str = "처리 중",
            ) -> None:
                safe_fraction = max(0.0, min(1.0, float(file_fraction)))
                safe_progress = max(0, min(100, int(((file_index + safe_fraction) / total_files) * 100)))
                file_percent = max(0, min(100, int(safe_fraction * 100)))
                file_status_rows[file_index] = {
                    "filename": filename,
                    "status": status_label,
                    "percent": file_percent,
                }
                _render_upload_file_progress(file_status_box, file_status_rows)
                text = f"{file_index + 1}/{total_files} {filename}: {message}"
                progress_bar.progress(safe_progress, text=text)
                progress_text.caption(f"{safe_progress}% - {text}")

            def _process_document_with_live_status(
                *,
                document_id: str,
                file_index: int,
                filename: str,
            ):
                progress_events: queue.Queue[object] = queue.Queue()
                result: dict[str, object] = {}

                def _worker_progress(current_job) -> None:
                    progress_events.put(current_job)

                def _worker() -> None:
                    try:
                        result["job"] = upload_processing_service.process(
                            document_id,
                            options,
                            progress_callback=_worker_progress,
                        )
                    except Exception as exc:  # pragma: no cover - surfaced in the Streamlit main thread
                        result["error"] = exc
                    finally:
                        progress_events.put(None)

                thread = threading.Thread(
                    target=_worker,
                    name=f"reg-rag-process-{document_id}",
                    daemon=True,
                )
                thread.start()

                started = time.monotonic()
                last_fraction = 0.2
                last_message = "Preprocessing started"
                tick = 0
                while thread.is_alive() or not progress_events.empty():
                    received_progress = False
                    while True:
                        try:
                            current = progress_events.get_nowait()
                        except queue.Empty:
                            break
                        if current is None:
                            continue
                        received_progress = True
                        last_fraction = 0.2 + (0.8 * max(0, min(100, current.progress)) / 100)
                        last_message = str(current.message or "Preprocessing")
                        current_unit = int(getattr(current, "current_unit", 0) or 0)
                        total_units = int(getattr(current, "total_units", 0) or 0)
                        unit_label = str(getattr(current, "unit_label", "") or "규정")
                        if total_units > 0:
                            regulation_progress_box.progress(
                                min(100, int((current_unit / total_units) * 100)),
                                text=f"작업 진행 · {unit_label} {current_unit}/{total_units}",
                            )
                        _update_file_progress(
                            file_index,
                            filename,
                            last_fraction,
                            last_message,
                            status_label="전처리 중",
                        )
                    if not received_progress:
                        elapsed = _format_elapsed_seconds(time.monotonic() - started)
                        safe_progress = max(0, min(100, int(((file_index + last_fraction) / total_files) * 100)))
                        file_percent = max(0, min(100, int(last_fraction * 100)))
                        file_status_rows[file_index] = {
                            "filename": filename,
                            "status": f"전처리 중 {elapsed}",
                            "percent": file_percent,
                        }
                        _render_upload_file_progress(file_status_box, file_status_rows)
                        heartbeat = _heartbeat_label(tick)
                        tick += 1
                        text = (
                            f"{file_index + 1}/{total_files} {filename}: {last_message} "
                            f"· {heartbeat} · 경과 {elapsed}"
                        )
                        progress_bar.progress(safe_progress, text=text)
                        progress_text.caption(f"{safe_progress}% - {text}")
                    time.sleep(0.7)
                thread.join()
                error = result.get("error")
                if isinstance(error, BaseException):
                    raise error
                return result["job"]

            for file_index, source in enumerate(upload_sources):
                filename = str(source["filename"])
                file_size = int(source["size"])

                def _upload_progress(
                    bytes_written: int,
                    expected_size: int | None,
                    *,
                    current_index: int = file_index,
                    current_filename: str = filename,
                    current_size: int = file_size,
                ) -> None:
                    denominator = expected_size or current_size or max(bytes_written, 1)
                    uploaded_fraction = min(1.0, bytes_written / max(denominator, 1))
                    _update_file_progress(
                        current_index,
                        current_filename,
                        uploaded_fraction * 0.2,
                        f"Saving uploaded file ({_format_upload_mb(bytes_written)} / {_format_upload_mb(denominator)})",
                        status_label="탑재 중",
                    )

                file_upload_metadata = dict(upload_metadata)
                _update_file_progress(file_index, filename, 0.0, "Saving uploaded file", status_label="탑재 중")
                pending_stream = None
                if source["kind"] == "pending":
                    pending_stream = Path(source["path"]).open("rb")
                    input_stream = pending_stream
                else:
                    input_stream = source["file"]
                    input_stream.seek(0)
                try:
                    document = upload_document_service.upload_stream(
                        filename,
                        input_stream,
                        tenant_id=_local_operator_tenant_id(),
                        expected_size=file_size,
                        progress_callback=_upload_progress,
                        **file_upload_metadata,
                    )
                finally:
                    if pending_stream is not None:
                        pending_stream.close()
                _update_file_progress(
                    file_index,
                    filename,
                    0.2,
                    "Upload saved; preprocessing queued",
                    status_label="전처리 대기",
                )
                job = _process_document_with_live_status(
                    document_id=document.document_id,
                    file_index=file_index,
                    filename=filename,
                )
                _update_file_progress(file_index, filename, 1.0, job.message, status_label="완료")
                if int(getattr(job, "total_units", 0) or 0) > 0:
                    total_units = int(job.total_units)
                    unit_label = str(getattr(job, "unit_label", "") or "작업")
                    regulation_progress_box.progress(
                        100,
                        text=f"{unit_label} {total_units}/{total_units} 완료",
                    )
                completed_documents.append(document)
                pending_path = source.get("pending_path")
                if isinstance(pending_path, Path):
                    pending_path.unlink(missing_ok=True)

            document = completed_documents[-1]
            status.update(label=f"{len(completed_documents)}개 문서 전처리 완료", state="complete")
        completed_document_ids = [item.document_id for item in completed_documents]
        st.session_state[WORKFLOW_DOCUMENT_IDS_KEY] = completed_document_ids
        st.session_state[WORKFLOW_SELECTED_DOCUMENT_IDS_KEY] = completed_document_ids
        for completed_document_id in completed_document_ids:
            st.session_state[f"workflow-document-selected-{completed_document_id}"] = True
        st.session_state["document_id"] = document.document_id
        st.session_state["unreviewed_preview_requested"] = not official_review_required
        st.success(f"{len(completed_documents)}개 문서 전처리가 끝났습니다. 이제 '② 결과 확인' 화면에서 내용을 확인하세요.")

    if st.session_state.get("document_id"):
        _render_workflow_next_button("② 결과 확인으로 이동", NAV_RESULTS, key="preprocess-goto-results")


# ---------------------------------------------------------------------------
# 페이지: ② 결과 확인
# ---------------------------------------------------------------------------

def _require_document_context(ctx: dict | None) -> bool:
    if ctx is None:
        st.info("아직 전처리한 문서가 없습니다. 먼저 '① 문서 올려서 전처리'를 진행해 주세요.")
        st.button("① 문서 올려서 전처리로 이동", on_click=_go, args=(NAV_PREPROCESS,), key="need-doc-goto")
        return False
    if ctx.get("large_result_warning"):
        st.error(ctx["large_result_warning"])
        st.info("원본 파일은 삭제되지 않았습니다. 새 버전에서 같은 원본을 다시 전처리하면 중복 메타데이터 없이 저장됩니다.")
        st.button(
            "① 문서 올려서 전처리로 이동",
            on_click=_go,
            args=(NAV_PREPROCESS,),
            key="large-result-goto-preprocess",
        )
        return False
    return True


def _render_quality_banner(quality_report) -> None:
    if quality_report and quality_report.passed:
        st.success("품질 검사를 통과했습니다. '③ 검수하고 승인' 단계로 넘어가셔도 됩니다.")
    elif quality_report:
        st.warning("품질 검사에서 확인이 필요한 항목이 있습니다. 아래 '이슈' 탭에서 내용을 확인해 주세요.")
    else:
        st.info("아직 이 문서의 품질 검사 결과가 없습니다.")


def _page_results(ctx: dict | None) -> None:
    st.markdown("## ② 결과 확인")
    _render_operator_project_controls(NAV_RESULTS)
    _render_pipeline_stages(PIPELINE_STAGE_AI_REVIEW)
    if not _require_document_context(ctx):
        return
    selected_document_ids = _render_workflow_document_directory(page_key="results")
    document_id = ctx["document_id"]
    kordoc_notice = st.session_state.get(KORDOC_REPROCESS_NOTICE_KEY)
    if isinstance(kordoc_notice, dict) and kordoc_notice.get("document_id") == document_id:
        st.session_state.pop(KORDOC_REPROCESS_NOTICE_KEY, None)
        st.success(
            f"설치된 Kordoc으로 새 초안 {int(kordoc_notice.get('count') or 1):,}개를 재전처리하고 "
            "표 파싱 증거를 확인했습니다. 이제 결과를 검토·승인한 뒤 색인해 주세요."
        )
    chunks = ctx["chunks"]
    issues = ctx["issues"]
    nodes = ctx["nodes"]
    quality_report = ctx["quality_report"]
    preview_limit = 500

    st.markdown(
        '<div class="rr-help">프로그램이 문서를 어떻게 정리했는지 확인하는 화면입니다. '
        "<b>품질</b>이 '통과'면 다음 단계로 넘어가면 됩니다.</div>",
        unsafe_allow_html=True,
    )

    summary_cols = st.columns(5)
    summary_cols[0].metric("문서 ID", document_id[:12], help="문서를 구별하는 번호입니다.")
    summary_cols[1].metric("품질", "통과" if quality_report and quality_report.passed else "검토 필요")
    summary_cols[2].metric("점수", f"{quality_report.score:.3f}" if quality_report else "-")
    summary_cols[3].metric("청크", f"{len(chunks):,}", help="청크 = AI가 검색하기 좋게 나눈 문서 조각입니다.")
    summary_cols[4].metric("이슈", f"{len(issues):,}", help="자동 검사에서 발견된 확인 필요 항목 수입니다.")
    _render_quality_banner(quality_report)
    if _unreviewed_preview_requested():
        st.warning(UNREVIEWED_PREVIEW_WARNING_KO + "\n\n" + UNREVIEWED_PREVIEW_WARNING)

    st.markdown("### 개정 전후 버전")
    st.caption("현재 연 규정을 기준으로 직전·이전·이후 개정판을 표시합니다.")
    _render_regulation_version_history(ctx["document"])

    summary_tab, structure_tab, chunks_tab, tables_tab, issues_tab, downloads_tab = st.tabs(
        ["요약", "문서 구조", "정리된 내용(청크)", "표·별표", "이슈", "내려받기"]
    )

    with summary_tab:
        agent_review_summary = ctx.get("agent_review_summary") or {}
        review_attention = ctx.get("review_attention") or {}
        ai_tag, ai_message, ai_executed = _ai_review_status_text(agent_review_summary)
        ai_tag_class = "ok" if ai_executed else "draft"
        st.markdown(
            f'<div class="rr-ai-panel">'
            f'<span class="rr-ai-tag {ai_tag_class}">AI 검수 · {ai_tag}</span>'
            "<h4>AI 검수 결과</h4>"
            f"<p>{ai_message}</p>"
            "</div>",
            unsafe_allow_html=True,
        )
        ai_cols = st.columns(3)
        ai_cols[0].metric(
            "AI가 살펴본 후보",
            f"{int(agent_review_summary.get('candidate_count') or 0):,}",
            help="품질 검사에서 확인이 필요하다고 본, AI 검토 후보 청크 수입니다.",
        )
        ai_cols[1].metric(
            "AI가 검토 대상으로 고른 청크",
            f"{int(agent_review_summary.get('selected_count') or 0):,}",
            help="예산 한도 안에서 실제 AI 검토 초안 대상으로 선정된 청크 수입니다.",
        )
        ai_cols[2].metric(
            "사람이 꼭 볼 청크",
            f"{len(review_attention):,}",
            help="경고가 있어 다음 ③ 검수·승인 단계에서 사람이 반드시 확인해야 하는 청크 수입니다.",
        )
        st.caption(
            "AI 검수는 파서 초안에서 확인이 필요한 부분을 골라 검토 초안을 만드는 단계입니다. "
            "실제 승인·색인은 다음 '③ 검수하고 승인' 단계에서 사람이 결정합니다."
        )

        st.markdown("### 문서 요약")
        st.write(
            {
                "문서 ID": document_id,
                "구조 노드 수": len(nodes),
                "청크 수": len(chunks),
                "이슈 수": len(issues),
                "품질 점수": quality_report.score if quality_report else None,
                "품질 통과": quality_report.passed if quality_report else None,
            }
        )
        if chunks:
            st.markdown("#### 첫 번째 청크 미리보기")
            st.caption("문서 맨 앞부분이 어떻게 정리됐는지 보여줍니다.")
            st.code(chunks[0].text[:1200], language="text")
        if agent_review_summary:
            with st.expander("AI 검수 비용·설정 상세 (전산 담당자용)", expanded=False):
                st.markdown("#### AI review API and cost guard")
                st.write(
                    {
                        "status": agent_review_summary.get("status"),
                        "skip_reason": agent_review_summary.get("skip_reason"),
                        "candidate_count": agent_review_summary.get("candidate_count"),
                        "cached_candidate_count": agent_review_summary.get("cached_candidate_count"),
                        "new_candidate_count": agent_review_summary.get("new_candidate_count"),
                        "selected_count": agent_review_summary.get("selected_count"),
                        "estimated_input_tokens": agent_review_summary.get("estimated_input_tokens"),
                        "estimated_output_tokens": agent_review_summary.get("estimated_output_tokens"),
                        "estimated_total_tokens": agent_review_summary.get("estimated_total_tokens"),
                        "cost_estimate_status": agent_review_summary.get("cost_estimate_status"),
                        "estimated_cost": agent_review_summary.get("estimated_cost"),
                        "api_call_count": agent_review_summary.get("api_call_count"),
                    }
                )

    with structure_tab:
        st.markdown("### 문서 구조 미리보기")
        st.caption("프로그램이 파악한 문서의 차례(조문·별표 등)입니다.")
        tree_rows = [
            {
                "노드 ID": node.node_id,
                "유형": node.node_type,
                "번호": node.number,
                "제목": node.title,
                "페이지": node.page_start,
                "상위 노드 ID": node.parent_id,
            }
            for node in nodes
        ]
        st.caption(f"구조 노드 {len(tree_rows):,}개 중 앞에서 {min(preview_limit, len(tree_rows)):,}개 표시")
        st.dataframe(pd.DataFrame(tree_rows[:preview_limit]), width="stretch")

    with chunks_tab:
        st.markdown("### 청크 미리보기")
        st.caption("청크는 AI가 검색하기 좋게 나눈 문서 조각입니다. '경고' 칸에 내용이 있으면 검수 때 눈여겨보세요.")
        chunk_rows = [
            {
                "청크 ID": chunk.chunk_id,
                "청크 유형": chunk.chunk_type,
                "문서 내 위치": chunk.metadata.get("hierarchy_path"),
                "원문 페이지": chunk.source_page_start,
                "본문 미리보기": chunk.text[:180],
                "신뢰도": chunk.confidence,
                "경고": ", ".join(chunk.warnings),
            }
            for chunk in chunks
        ]
        st.caption(f"청크 {len(chunk_rows):,}개 중 앞에서 {min(preview_limit, len(chunk_rows)):,}개 표시")
        st.dataframe(pd.DataFrame(chunk_rows[:preview_limit]), width="stretch")
        if chunks:
            st.markdown("### 선택 청크 원문·전처리 결과")
            chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
            selected_chunk_id = st.selectbox(
                "상세 확인할 청크",
                list(chunk_by_id),
                format_func=lambda chunk_id: (
                    f"{chunk_by_id[chunk_id].metadata.get('hierarchy_path') or '위치 미지정'} · "
                    f"{chunk_by_id[chunk_id].source_page_start or '-'}쪽 · {chunk_id[:12]}"
                ),
                key=f"results-detail-chunk-{document_id}",
            )
            selected_chunk = chunk_by_id[selected_chunk_id]
            detail_cols = st.columns(4)
            detail_cols[0].metric("청크 유형", selected_chunk.chunk_type)
            detail_cols[1].metric("원문 페이지", selected_chunk.source_page_start or "-")
            detail_cols[2].metric("신뢰도", f"{selected_chunk.confidence:.3f}")
            detail_cols[3].metric("경고", len(selected_chunk.warnings))
            original_col, processed_col = st.columns(2)
            with original_col:
                _render_original_source_preview(ctx["document"], selected_chunk)
            with processed_col:
                _render_processed_result_preview(selected_chunk, selected_chunk.text)

            selected_chunk_index = next(
                index for index, chunk in enumerate(chunks) if chunk.chunk_id == selected_chunk_id
            )
            previous_chunk = chunks[selected_chunk_index - 1] if selected_chunk_index > 0 else None
            next_chunk = chunks[selected_chunk_index + 1] if selected_chunk_index + 1 < len(chunks) else None
            st.markdown("#### 선택 청크 전후 문맥")
            st.caption("현재 청크가 규정의 어느 흐름에 놓였는지 직전·현재·다음 청크를 이어서 확인합니다.")
            context_tabs = st.tabs(["직전 청크", "현재 청크", "다음 청크"])
            for context_tab, context_chunk, empty_message in (
                (context_tabs[0], previous_chunk, "문서의 첫 청크이므로 직전 청크가 없습니다."),
                (context_tabs[1], selected_chunk, ""),
                (context_tabs[2], next_chunk, "문서의 마지막 청크이므로 다음 청크가 없습니다."),
            ):
                with context_tab:
                    if context_chunk is None:
                        st.info(empty_message)
                        continue
                    st.caption(
                        f"{context_chunk.metadata.get('hierarchy_path') or '위치 미지정'} · "
                        f"{context_chunk.source_page_start or '-'}쪽 · {context_chunk.chunk_id}"
                    )
                    st.code(context_chunk.text[:2400], language="text")

    with tables_tab:
        st.markdown("### 표/별표 검토")
        st.caption("문서 안의 표와 별표가 잘 추출됐는지 확인합니다.")
        table_rows = exporter.table_rows(chunks)
        if quality_report:
            metrics = quality_report.table_metrics
            tcol1, tcol2, tcol3, tcol4 = st.columns(4)
            tcol1.metric("표 후보 청크", f"{int(metrics.get('table_like_chunks') or 0):,}")
            tcol2.metric("구조화 행", f"{int(metrics.get('table_cell_row_count') or 0):,}")
            tcol3.metric("인용 가능 청크", f"{int(metrics.get('table_citation_ready_chunks') or 0):,}")
            tcol4.metric("검수 필요", f"{int(metrics.get('table_review_required_chunks') or 0):,}")
            if int(metrics.get("table_review_required_chunks") or 0):
                st.warning("인용 가능한 운영형 RAG에 사용하기 전에 일부 표/별표 행은 수동 검수가 필요합니다.")
            elif int(metrics.get("table_like_chunks") or 0):
                st.success("감지된 표/별표 행에 자동 검수 플래그가 없습니다.")
            else:
                st.info("표 후보 행이 감지되지 않았습니다. 조문 중심 문서라면 정상일 수 있습니다.")
        if table_rows:
            preview_rows = []
            for row in table_rows[:50]:
                preview_rows.append(
                    {
                        "인용 근거": row.get("citation_label"),
                        "행 유형": row.get("row_kind"),
                        "행 번호": row.get("row_index"),
                        "셀 수": row.get("cell_count"),
                        "검수 필요": row.get("review_required"),
                        "검수 사유": ", ".join(row.get("review_flags") or row.get("row_quality_flags") or []),
                        "원문 행": row.get("raw"),
                    }
                )
            st.dataframe(pd.DataFrame(preview_rows), width="stretch")

    with issues_tab:
        st.markdown("### 검증 이슈")
        st.caption("자동 검사에서 발견된 확인 필요 항목입니다. 없으면 좋은 상태입니다.")
        if issues:
            st.dataframe(pd.DataFrame([issue.model_dump() for issue in issues]), width="stretch")
        else:
            st.success("기록된 검증 이슈가 없습니다.")
        if quality_report:
            st.markdown("### 품질 요약")
            qcol1, qcol2, qcol3, qcol4 = st.columns(4)
            qcol1.metric("통과 여부", str(quality_report.passed))
            qcol2.metric("점수", f"{quality_report.score:.3f}")
            qcol3.metric("청크 수", f"{quality_report.chunk_count:,}")
            qcol4.metric("이슈 수", f"{quality_report.issue_count:,}")

    with downloads_tab:
        st.markdown("### 전달용 산출물 내려받기")
        st.caption("RAG 인덱싱 또는 시범 검토에 사용할 청크, 표 추출물, 품질 근거 파일을 내려받습니다.")
        if st.button("💾 저장하기 — Windows 탐색기에서 산출물 폴더 열기", key=f"open-exports-{document_id}"):
            try:
                _open_directory_in_explorer(settings.exports_dir)
                st.success(f"산출물 저장 폴더를 열었습니다: {settings.exports_dir}")
            except OSError as exc:
                st.error(str(exc))
        col1, col2, col3 = st.columns(3)
        with col1:
            jsonl_path = settings.exports_dir / f"{document_id}.jsonl"
            st.download_button(
                "JSONL 다운로드",
                jsonl_path.read_text(encoding="utf-8") if jsonl_path.exists() else exporter.to_jsonl(chunks),
                file_name=f"{document_id}.jsonl",
                help="AI 인덱싱용 파일입니다.",
            )
        with col2:
            csv_path = settings.exports_dir / f"{document_id}.csv"
            st.download_button(
                "CSV 다운로드",
                csv_path.read_text(encoding="utf-8") if csv_path.exists() else exporter.to_csv(chunks),
                file_name=f"{document_id}.csv",
                help="엑셀에서 열어볼 수 있는 파일입니다.",
            )
        with col3:
            md_path = settings.exports_dir / f"{document_id}.md"
            st.download_button(
                "Markdown 다운로드",
                md_path.read_text(encoding="utf-8") if md_path.exists() else exporter.to_markdown(chunks),
                file_name=f"{document_id}.md",
                help="사람이 읽기 좋은 문서 파일입니다.",
            )

        table_col1, table_col2, quality_col1, quality_col2 = st.columns(4)
        with table_col1:
            tables_jsonl_path = settings.exports_dir / f"{document_id}.tables.jsonl"
            st.download_button(
                "표 JSONL 다운로드",
                tables_jsonl_path.read_text(encoding="utf-8") if tables_jsonl_path.exists() else exporter.to_tables_jsonl(chunks),
                file_name=f"{document_id}.tables.jsonl",
            )
        with table_col2:
            tables_csv_path = settings.exports_dir / f"{document_id}.tables.csv"
            st.download_button(
                "표 CSV 다운로드",
                tables_csv_path.read_text(encoding="utf-8") if tables_csv_path.exists() else exporter.to_tables_csv(chunks),
                file_name=f"{document_id}.tables.csv",
            )
        with quality_col1:
            quality_json_path = settings.exports_dir / f"{document_id}.quality.json"
            quality_json = ""
            if quality_json_path.exists():
                quality_json = quality_json_path.read_text(encoding="utf-8")
            elif quality_report:
                quality_json = json.dumps(quality_report.model_dump(mode="json"), ensure_ascii=False, indent=2)
            st.download_button("품질 JSON 다운로드", quality_json, file_name=f"{document_id}.quality.json")
        with quality_col2:
            quality_md_path = settings.exports_dir / f"{document_id}.quality.md"
            quality_md = ""
            if quality_md_path.exists():
                quality_md = quality_md_path.read_text(encoding="utf-8")
            elif quality_report:
                quality_md = _quality_report_to_markdown(quality_report)
            st.download_button("품질 Markdown 다운로드", quality_md, file_name=f"{document_id}.quality.md")

    st.divider()
    _render_workflow_next_button(
        f"선택한 {len(selected_document_ids):,}개 규정을 ③ 검수하고 승인으로 이동",
        NAV_APPROVAL,
        key="results-goto-approval",
        disabled=not selected_document_ids,
    )


# ---------------------------------------------------------------------------
# 페이지: ③ 검수하고 승인
# ---------------------------------------------------------------------------

def _page_approval(ctx: dict | None) -> None:
    st.markdown("## ③ 검수하고 승인")
    _render_operator_project_controls(NAV_APPROVAL)
    _render_pipeline_stages(PIPELINE_STAGE_HUMAN_APPROVAL)
    if not _require_document_context(ctx):
        return
    selected_document_ids = _render_workflow_document_directory(page_key="approval")
    document_id = ctx["document_id"]
    chunks = ctx["chunks"]
    approval_counts = ctx["approval_counts"]
    approved_count = ctx["approved_count"]
    review_attention = ctx["review_attention"]
    index_status = ctx["index_status"]
    index_status_error = ctx["index_status_error"]
    mcp_connection_gate = ctx["mcp_connection_gate"]
    local_auth = ctx["local_auth"]

    st.markdown(
        '<div class="rr-help">사람이 확인을 마친 내용에만 <b>승인</b>을 하고, 승인한 내용을 <b>색인</b>(AI에 등록)하는 화면입니다. '
        "순서: 검수 증빙 불러오기 → 승인 → 색인.</div>",
        unsafe_allow_html=True,
    )
    st.caption("Secure RAG review gate — 승인·색인된 내용만 AI가 답변 근거로 사용합니다.")

    selected_approval_contexts = _selected_approval_contexts(selected_document_ids, ctx)
    if len(selected_document_ids) > 1:
        st.markdown(f"### 선택한 규정 {len(selected_document_ids):,}개 일괄 처리")
        st.caption(
            "청크를 한 문서로 합치지 않습니다. 규정별 문서 ID·규정 ID·목차 계층을 유지한 채 "
            "각 규정을 차례로 검수·승인·색인합니다."
        )
        workflow_review_entries: list[tuple[dict, list[dict[str, object]]]] = []
        workflow_review_rows: list[dict[str, object]] = []
        for approval_ctx in selected_approval_contexts:
            pending_entries = _approval_pending_entries(approval_ctx)
            workflow_review_entries.append((approval_ctx, pending_entries))
            ai_complete = sum(bool(dict(entry["state"]).get("ai_confirmed")) for entry in pending_entries)
            human_complete = sum(bool(entry.get("human_confirmed")) for entry in pending_entries)
            ready_count = sum(bool(dict(entry["state"]).get("approve_enabled")) for entry in pending_entries)
            approval_ctx_chunks = list(approval_ctx["chunks"])
            approved_chunks = sum(1 for chunk in approval_ctx_chunks if _approval_status(chunk) == "approved")
            workflow_review_rows.append(
                {
                    "규정": _workflow_document_label(approval_ctx["document"]),
                    "전체 청크": len(approval_ctx_chunks),
                    "미승인": len(pending_entries),
                    "AI 검수": f"{ai_complete}/{len(pending_entries)}",
                    "사람 확인": f"{human_complete}/{len(pending_entries)}",
                    "승인 청크": approved_chunks,
                    "상태": (
                        "승인 완료"
                        if approval_ctx_chunks and approved_chunks == len(approval_ctx_chunks)
                        else "승인·색인 가능"
                        if pending_entries and ready_count == len(pending_entries)
                        else "검수 필요"
                    ),
                }
            )
        st.dataframe(pd.DataFrame(workflow_review_rows), width="stretch", hide_index=True)

        workflow_pending_count = sum(len(entries) for _, entries in workflow_review_entries)
        workflow_ai_complete_count = sum(
            bool(dict(entry["state"]).get("ai_confirmed"))
            for _, entries in workflow_review_entries
            for entry in entries
        )
        workflow_human_complete_count = sum(
            bool(entry.get("human_confirmed"))
            for _, entries in workflow_review_entries
            for entry in entries
        )
        workflow_ready_count = sum(
            bool(dict(entry["state"]).get("approve_enabled"))
            for _, entries in workflow_review_entries
            for entry in entries
        )
        workflow_contexts_complete = len(selected_approval_contexts) == len(selected_document_ids) and all(
            approval_ctx.get("chunks") for approval_ctx in selected_approval_contexts
        )
        workflow_review_task_total = workflow_pending_count * 2
        workflow_review_task_complete = workflow_ai_complete_count + workflow_human_complete_count
        st.progress(
            100
            if workflow_review_task_total == 0
            else int(workflow_review_task_complete * 100 / workflow_review_task_total),
            text=(
                f"선택 규정 전체 검수 {workflow_review_task_complete:,}/{workflow_review_task_total:,} · "
                f"AI {workflow_ai_complete_count:,}/{workflow_pending_count:,} · "
                f"사람 {workflow_human_complete_count:,}/{workflow_pending_count:,}"
            ),
        )
        workflow_security_level = st.selectbox(
            "선택 규정 일괄 보안 등급",
            ["internal", "public", "sensitive", "confidential"],
            key=f"workflow-security-level-{document_id}",
            format_func=lambda value: SECURITY_LEVEL_LABELS.get(value, value),
        )
        st.caption("전체 완료는 기존 개별 결정을 다시 일괄 적용합니다.")
        batch_review_cols = st.columns(2)
        if batch_review_cols[0].button(
            f"전체 규정 자료 AI 검수 완료 (선택 {len(selected_document_ids):,}개)",
            key=f"workflow-ai-review-{document_id}",
            disabled=(
                not workflow_contexts_complete
                or workflow_pending_count == 0
            ),
            width="stretch",
        ):
            batch_progress = st.progress(0, text="선택 규정 AI 검수 0%")
            completed_chunks = 0
            total_chunks = sum(len(approval_ctx["chunks"]) for approval_ctx, _ in workflow_review_entries)
            changed_chunks = 0
            for approval_ctx, _ in workflow_review_entries:
                current_total = len(approval_ctx["chunks"])
                summary = _approval_set_bulk_ai_decisions(
                    document_id=str(approval_ctx["document_id"]),
                    chunks=list(approval_ctx["chunks"]),
                    review_attention=dict(approval_ctx.get("review_attention") or {}),
                    agent_review_summary=dict(approval_ctx.get("agent_review_summary") or {}),
                    decision="reflect",
                    progress_callback=lambda current, _total, offset=completed_chunks: batch_progress.progress(
                        min(100, int((offset + current) * 100 / max(total_chunks, 1))),
                        text=f"선택 규정 AI 검수 {offset + current:,}/{total_chunks:,}",
                    ),
                )
                changed_chunks += int(summary["chunk_count"])
                completed_chunks += current_total
            st.success(f"선택한 규정 전체에서 미승인 청크 {changed_chunks:,}개의 AI 검수를 완료했습니다.")
            st.rerun()
        if batch_review_cols[1].button(
            f"전체 규정 자료 사람 확인 완료 (선택 {len(selected_document_ids):,}개)",
            key=f"workflow-human-review-{document_id}",
            disabled=(
                not workflow_contexts_complete
                or workflow_pending_count == 0
            ),
            width="stretch",
        ):
            batch_progress = st.progress(0, text="선택 규정 사람 확인 0%")
            completed_chunks = 0
            total_chunks = sum(len(approval_ctx["chunks"]) for approval_ctx, _ in workflow_review_entries)
            changed_chunks = 0
            for approval_ctx, _ in workflow_review_entries:
                current_total = len(approval_ctx["chunks"])
                summary = _approval_set_bulk_human_confirmations(
                    document_id=str(approval_ctx["document_id"]),
                    chunks=list(approval_ctx["chunks"]),
                    confirmed=True,
                    progress_callback=lambda current, _total, offset=completed_chunks: batch_progress.progress(
                        min(100, int((offset + current) * 100 / max(total_chunks, 1))),
                        text=f"선택 규정 사람 확인 {offset + current:,}/{total_chunks:,}",
                    ),
                )
                changed_chunks += int(summary["chunk_count"])
                completed_chunks += current_total
            st.success(f"선택한 규정 전체에서 미승인 청크 {changed_chunks:,}개의 사람 확인을 완료했습니다.")
            st.rerun()

        st.caption("나머지 부분 완료는 이미 사람이 체크·수정한 결정은 유지하고 아직 미확인인 부분만 처리합니다.")
        batch_remaining_cols = st.columns(2)
        if batch_remaining_cols[0].button(
            f"나머지 부분 AI 점검 전체 완료 (선택 {len(selected_document_ids):,}개)",
            key=f"workflow-ai-remaining-review-{document_id}",
            disabled=(
                not workflow_contexts_complete
                or workflow_pending_count == 0
                or workflow_ai_complete_count >= workflow_pending_count
            ),
            width="stretch",
        ):
            batch_progress = st.progress(0, text="선택 규정 남은 AI 점검 0%")
            completed_chunks = 0
            total_chunks = sum(len(approval_ctx["chunks"]) for approval_ctx, _ in workflow_review_entries)
            changed_items = 0
            for approval_ctx, _ in workflow_review_entries:
                current_total = len(approval_ctx["chunks"])
                summary = _approval_set_bulk_ai_decisions(
                    document_id=str(approval_ctx["document_id"]),
                    chunks=list(approval_ctx["chunks"]),
                    review_attention=dict(approval_ctx.get("review_attention") or {}),
                    agent_review_summary=dict(approval_ctx.get("agent_review_summary") or {}),
                    decision="skip",
                    only_remaining=True,
                    progress_callback=lambda current, _total, offset=completed_chunks: batch_progress.progress(
                        min(100, int((offset + current) * 100 / max(total_chunks, 1))),
                        text=f"선택 규정 남은 AI 점검 {offset + current:,}/{total_chunks:,}",
                    ),
                )
                changed_items += int(summary["item_count"])
                completed_chunks += current_total
            st.success(f"기존 결정을 유지하고 아직 미확인인 AI 제안 {changed_items:,}개를 확인했습니다.")
            st.rerun()
        if batch_remaining_cols[1].button(
            f"나머지 부분 사람 점검 전체 완료 (선택 {len(selected_document_ids):,}개)",
            key=f"workflow-human-remaining-review-{document_id}",
            disabled=(
                not workflow_contexts_complete
                or workflow_pending_count == 0
                or workflow_human_complete_count >= workflow_pending_count
            ),
            width="stretch",
        ):
            batch_progress = st.progress(0, text="선택 규정 남은 사람 점검 0%")
            completed_chunks = 0
            total_chunks = sum(len(approval_ctx["chunks"]) for approval_ctx, _ in workflow_review_entries)
            changed_chunks = 0
            for approval_ctx, _ in workflow_review_entries:
                current_total = len(approval_ctx["chunks"])
                summary = _approval_set_bulk_human_confirmations(
                    document_id=str(approval_ctx["document_id"]),
                    chunks=list(approval_ctx["chunks"]),
                    confirmed=True,
                    only_remaining=True,
                    progress_callback=lambda current, _total, offset=completed_chunks: batch_progress.progress(
                        min(100, int((offset + current) * 100 / max(total_chunks, 1))),
                        text=f"선택 규정 남은 사람 점검 {offset + current:,}/{total_chunks:,}",
                    ),
                )
                changed_chunks += int(summary["chunk_count"])
                completed_chunks += current_total
            st.success(f"기존 확인을 유지하고 아직 미확인인 청크 {changed_chunks:,}개를 확인했습니다.")
            st.rerun()

        if st.button(
            f"선택한 규정 {len(selected_document_ids):,}개 승인·색인",
            type="primary",
            key=f"workflow-approve-index-{document_id}",
            disabled=(
                not workflow_contexts_complete
                or workflow_pending_count == 0
                or workflow_ready_count < workflow_pending_count
            ),
            width="stretch",
        ):
            try:
                plans = [
                    _prepare_reviewed_document_approval_plan(
                        approval_ctx,
                        security_level=workflow_security_level,
                    )
                    for approval_ctx, _ in workflow_review_entries
                ]
                batch_status = st.status("선택한 규정별 승인·색인 중…", expanded=True)
                batch_progress = st.progress(0, text="규정별 승인·색인 준비 0%")
                batch_detail = st.empty()
                batch_results: list[dict[str, object]] = []
                for plan_index, plan in enumerate(plans, start=1):
                    segment_start = int((plan_index - 1) * 100 / max(len(plans), 1))
                    segment_end = int(plan_index * 100 / max(len(plans), 1))
                    document_label = _workflow_document_label(plan["document"])
                    batch_status.write(f"{plan_index:,}/{len(plans):,} · {document_label}")
                    result = _run_background_operation_with_progress(
                        lambda _report, approval_plan=plan: _execute_reviewed_document_approval_plan(approval_plan),
                        progress_bar=batch_progress,
                        detail_box=batch_detail,
                        start_percent=segment_start,
                        end_percent=segment_end,
                        label=f"{document_label} 승인·색인",
                        estimated_seconds=max(8.0, int(plan["pending_chunk_count"]) / 60.0),
                    )
                    batch_results.append(result)
                    _invalidate_document_context_cache(str(plan["document_id"]))
                batch_status.update(label="선택한 모든 규정 승인·색인 완료", state="complete")
                st.success(
                    f"규정 {len(batch_results):,}개를 각각 승인·색인했습니다. "
                    f"MCP에는 규정별 계층과 청크가 분리되어 포함됩니다."
                )
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        if workflow_ready_count < workflow_pending_count:
            st.info("선택한 모든 규정의 AI 검수와 사람 확인을 완료하면 규정별 일괄 승인·색인 버튼이 활성화됩니다.")

    if not chunks:
        st.info("이 문서에는 승인할 청크가 없습니다. 전처리를 다시 실행해 주세요.")
        return

    current_document = ctx["document"]
    if getattr(current_document, "regulation_id", None):
        st.markdown("### 규정 버전 상태")
        lifecycle_cols = st.columns(4)
        lifecycle_cols[0].metric("규정 ID", str(current_document.regulation_id))
        lifecycle_cols[1].metric("버전", current_document.regulation_version or "미지정")
        lifecycle_cols[2].metric("상태", current_document.regulation_status)
        lifecycle_cols[3].metric("효력 시작", current_document.effective_from or "미지정")
        version_history = repository.find_documents_by_regulation(
            current_document.regulation_id,
            profile_id=current_document.profile_id,
            tenant_id=current_document.tenant_id,
        )
        if version_history:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "문서 ID": item.document_id,
                            "버전": item.regulation_version or "미지정",
                            "상태": item.regulation_status,
                            "개정일": item.revision_date or "",
                            "효력 시작": item.effective_from or "",
                            "효력 종료": item.effective_to or "",
                            "폐지일": item.repealed_at or "",
                        }
                        for item in version_history
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        current_regulation_status = str(getattr(current_document, "regulation_status", "") or "draft").strip().lower()
        lifecycle_targets = {
            "approved": ("superseded", "repealed"),
            "superseded": ("repealed",),
        }.get(current_regulation_status, ())
        if current_document.regulation_version and lifecycle_targets:
            st.markdown("#### 규정 생명주기 수동 전환")
            st.caption("버전이 확정된 규정만 전환할 수 있으며, 전환 사유는 감사 기록에 남습니다.")
            lifecycle_labels = {
                "superseded": "대체됨 (superseded)",
                "repealed": "폐지됨 (repealed)",
            }
            with st.form(f"regulation-lifecycle-form-{document_id}"):
                lifecycle_target = st.selectbox(
                    "전환 상태",
                    lifecycle_targets,
                    format_func=lambda value: lifecycle_labels.get(value, value),
                )
                lifecycle_reason = st.text_area(
                    "전환 사유",
                    placeholder="예: 신규 개정본 v2.0 시행으로 기존 버전을 대체함",
                    help="superseded 또는 repealed 전환에는 사유가 필요합니다.",
                )
                lifecycle_submitted = st.form_submit_button("규정 상태 전환", type="secondary")
            if lifecycle_submitted:
                lifecycle_reason_text = str(lifecycle_reason or "").strip()
                if not lifecycle_reason_text:
                    st.error("규정 상태를 전환하려면 사유를 입력해 주세요.")
                else:
                    try:
                        transition_result = transition_regulation_status(
                            document_id,
                            RegulationLifecycleRequest(
                                status=lifecycle_target,
                                reason=lifecycle_reason_text,
                            ),
                            local_auth,
                        )
                        updated_status = (
                            transition_result.get("document", {}).get("regulation_status")
                            if isinstance(transition_result, dict)
                            else lifecycle_target
                        ) or lifecycle_target
                        st.success(f"규정 상태를 {lifecycle_labels.get(updated_status, updated_status)}로 전환했습니다.")
                        lifecycle_event = transition_result.get("lifecycle_event", {}) if isinstance(transition_result, dict) else {}
                        vector_sync = lifecycle_event.get("vector_sync", {}) if isinstance(lifecycle_event, dict) else {}
                        if vector_sync.get("status") == "failed":
                            st.error("규정 상태는 전환됐지만 색인 동기화에 실패했습니다. 승인 화면에서 다시 색인해 주세요.")
                        else:
                            st.rerun()
                    except Exception as exc:
                        st.error(f"규정 상태 전환에 실패했습니다: {exc}")

    st.markdown("### 현재 상태")
    total_chunks = len(chunks)
    status_cols = st.columns(4)
    status_cols[0].metric("전체 청크", f"{total_chunks:,}")
    status_cols[1].metric("승인된 청크 (Approved chunks)", f"{approved_count:,}")
    status_cols[2].metric("검수 주의 청크", f"{len(review_attention):,}", help="파서·표 관련 경고가 있어 사람이 꼭 봐야 하는 청크입니다.")
    if index_status_error:
        status_cols[3].metric("색인 상태", "확인 불가")
        st.warning(f"MCP index status could not be checked: {index_status_error}")
    else:
        status_cols[3].metric(
            "AI에 보이는 기록 (MCP-visible records)",
            f"{int(mcp_connection_gate.get('mcp_visible_count') or 0):,}",
        )
        if index_status and index_status.get("validation_error"):
            st.warning(f"Index validation: {index_status['validation_error']}")
        if not mcp_connection_gate.get("ready"):
            st.warning(
                "AI는 '승인 후 색인된' 내용만 볼 수 있습니다. 승인과 색인을 마친 뒤에도 숫자가 맞지 않으면 아래 '다시 색인하기'를 눌러 주세요.\n\n"
                "Claude/MCP can answer only from approved chunks that are currently indexed. "
                "If Claude sees smoke-test documents or fewer records than expected, approve the intended chunks "
                "and run Reindex approved chunks with the same data directory and tenant."
            )
        else:
            st.success("승인된 모든 청크가 색인되어 AI에서 사용할 수 있습니다.")
    with st.expander("승인 상태 상세 (전산 담당자용)", expanded=False):
        st.write({"approval_status_counts": approval_counts})
        st.write(
            {
                "indexing_status": mcp_connection_gate.get("indexing_status"),
                "stale_count": mcp_connection_gate.get("stale_count"),
                "gate_reason": mcp_connection_gate.get("reason"),
            }
        )
    if review_attention:
        st.warning(
            f"검수 주의 청크가 {len(review_attention):,}개 있습니다. 승인 전에 반드시 내용을 확인하고 아래 확인란에 체크해 주세요."
        )

    worklist_path_key = f"approval-worklist-path-{document_id}"
    worklist_sha_key = f"approval-worklist-sha256-{document_id}"
    batch_manifest_path_key = f"approval-review-batch-manifest-path-{document_id}"
    batch_manifest_sha_key = f"approval-review-batch-manifest-sha256-{document_id}"
    batch_id_key = f"approval-review-batch-{document_id}"
    batch_fingerprint_key = f"approval-review-batch-fingerprint-{document_id}"
    review_strategy_key = f"approval-review-strategy-{document_id}"
    security_level_key = f"security-level-{document_id}"
    review_ack_key = f"review-flags-ack-{document_id}"
    approval_chunk_ids_key = f"approval-selected-chunk-ids-{document_id}"
    for state_key in (
        worklist_path_key,
        worklist_sha_key,
        batch_manifest_path_key,
        batch_manifest_sha_key,
        batch_id_key,
        batch_fingerprint_key,
    ):
        st.session_state.setdefault(state_key, "")
    st.session_state.setdefault(security_level_key, "internal")
    selected_approval_chunk_ids = [
        str(chunk_id)
        for chunk_id in st.session_state.get(approval_chunk_ids_key, [])
        if str(chunk_id).strip()
    ]

    document = ctx["document"]
    agent_review_summary = ctx.get("agent_review_summary") or {}

    st.markdown("### 검증 시트")
    st.caption("AI 제안은 반영 여부만 결정하고, 최종 승인은 사람이 별도로 누릅니다. 탭 이동 순서는 자유입니다.")
    chunk_by_id = {str(chunk.chunk_id): chunk for chunk in chunks}
    attention_ids = {str(chunk_id) for chunk_id in review_attention}
    original_order = {cid: index for index, cid in enumerate(chunk_by_id)}
    ordered_compare_ids = sorted(
        chunk_by_id,
        key=lambda cid: (
            not _is_chunk_pending_approval(chunk_by_id[cid]),
            cid not in attention_ids,
            original_order[cid],
        ),
    )
    pending_compare_ids = [cid for cid in ordered_compare_ids if _is_chunk_pending_approval(chunk_by_id[cid])]

    pending_review_states = [
        _approval_chunk_review_state_from_session(
            document_id=document_id,
            chunk=chunk_by_id[cid],
            review_attention=review_attention,
            agent_review_summary=agent_review_summary,
        )
        for cid in pending_compare_ids
    ]
    ai_complete_count = sum(bool(dict(entry["state"]).get("ai_confirmed")) for entry in pending_review_states)
    human_complete_count = sum(bool(entry.get("human_confirmed")) for entry in pending_review_states)
    review_task_total = len(pending_review_states) * 2
    review_task_complete = ai_complete_count + human_complete_count
    st.markdown("### 현재 연 규정 한 번에 검수")
    st.caption("현재 규정 디렉터리에서 열어 둔 규정 한 개의 모든 미승인 청크에만 적용됩니다.")
    st.progress(
        100 if review_task_total == 0 else int((review_task_complete / review_task_total) * 100),
        text=(
            f"전체 검수 {review_task_complete}/{review_task_total} · "
            f"AI {ai_complete_count}/{len(pending_review_states)} · "
            f"사람 {human_complete_count}/{len(pending_review_states)}"
        ),
    )
    full_review_cols = st.columns(2)
    if full_review_cols[0].button(
        "현재 규정 AI 검수 완료",
        type="primary",
        key=f"ai-full-review-{document_id}",
        disabled=not pending_review_states,
        width="stretch",
    ):
        with st.status("전체 규정 AI 검수 완료 처리 중…", expanded=True) as bulk_status:
            bulk_status.write(f"{len(chunks):,}개 청크를 순회하는 중입니다.")
            bulk_progress = st.progress(0, text="AI 검수 0%")
            summary = _approval_set_bulk_ai_decisions(
                document_id=document_id,
                chunks=chunks,
                review_attention=review_attention,
                agent_review_summary=agent_review_summary,
                decision="reflect",
                progress_callback=lambda current, total: bulk_progress.progress(
                    min(100, int(current * 100 / max(total, 1))),
                    text=f"AI 검수 {current:,}/{total:,}",
                ),
            )
            bulk_status.update(label="전체 AI 검수 완료", state="complete")
        st.session_state.pop(approval_chunk_ids_key, None)
        st.success(
            f"전체 AI 검수 완료: {summary['chunk_count']:,}개 규정·조항, 제안 {summary['item_count']:,}개를 반영으로 표시했습니다."
        )
        st.rerun()
    if full_review_cols[1].button(
        "현재 규정 사람 확인 완료",
        type="primary",
        key=f"human-full-review-{document_id}",
        disabled=not pending_review_states,
        width="stretch",
    ):
        with st.status("전체 규정 사람 확인 처리 중…", expanded=True) as bulk_status:
            bulk_status.write(f"{len(chunks):,}개 청크를 확인 완료로 표시하는 중입니다.")
            bulk_progress = st.progress(0, text="사람 확인 0%")
            summary = _approval_set_bulk_human_confirmations(
                document_id=document_id,
                chunks=chunks,
                confirmed=True,
                progress_callback=lambda current, total: bulk_progress.progress(
                    min(100, int(current * 100 / max(total, 1))),
                    text=f"사람 확인 {current:,}/{total:,}",
                ),
            )
            bulk_status.update(label="전체 사람 확인 완료", state="complete")
        st.session_state.pop(approval_chunk_ids_key, None)
        st.success(f"전체 사람 확인 완료: {summary['chunk_count']:,}개 미승인 규정·조항을 확인 완료로 표시했습니다.")
        st.rerun()

    st.caption(
        "일부 항목을 먼저 검수했다면 아래 버튼을 사용하세요. 기존 결정은 유지하고 아직 미확인인 항목만 완료합니다."
    )
    remaining_review_cols = st.columns(2)
    if remaining_review_cols[0].button(
        "나머지 부분 AI 점검 전체 완료",
        key=f"ai-remaining-review-{document_id}",
        disabled=not pending_review_states or ai_complete_count >= len(pending_review_states),
        width="stretch",
    ):
        with st.status("남은 AI 점검 처리 중…", expanded=True) as bulk_status:
            bulk_status.write("기존 반영·미반영 결정은 유지하고 미결정 제안만 확인 완료로 표시합니다.")
            bulk_progress = st.progress(0, text="남은 AI 점검 0%")
            summary = _approval_set_bulk_ai_decisions(
                document_id=document_id,
                chunks=chunks,
                review_attention=review_attention,
                agent_review_summary=agent_review_summary,
                decision="skip",
                only_remaining=True,
                progress_callback=lambda current, total: bulk_progress.progress(
                    min(100, int(current * 100 / max(total, 1))),
                    text=f"남은 AI 점검 {current:,}/{total:,}",
                ),
            )
            bulk_status.update(label="나머지 AI 점검 완료", state="complete")
        st.session_state.pop(approval_chunk_ids_key, None)
        st.success(
            f"나머지 AI 점검 완료: 기존 결정을 유지하고 미결정 제안 {summary['item_count']:,}개를 확인했습니다."
        )
        st.rerun()
    if remaining_review_cols[1].button(
        "나머지 부분 사람 점검 전체 완료",
        key=f"human-remaining-review-{document_id}",
        disabled=not pending_review_states or human_complete_count >= len(pending_review_states),
        width="stretch",
    ):
        with st.status("남은 사람 점검 처리 중…", expanded=True) as bulk_status:
            bulk_status.write("이미 확인한 청크는 유지하고 미확인 청크만 확인 완료로 표시합니다.")
            bulk_progress = st.progress(0, text="남은 사람 점검 0%")
            summary = _approval_set_bulk_human_confirmations(
                document_id=document_id,
                chunks=chunks,
                confirmed=True,
                only_remaining=True,
                progress_callback=lambda current, total: bulk_progress.progress(
                    min(100, int(current * 100 / max(total, 1))),
                    text=f"남은 사람 점검 {current:,}/{total:,}",
                ),
            )
            bulk_status.update(label="나머지 사람 점검 완료", state="complete")
        st.session_state.pop(approval_chunk_ids_key, None)
        st.success(
            f"나머지 사람 점검 완료: 기존 확인을 유지하고 미확인 규정·조항 {summary['chunk_count']:,}개를 확인했습니다."
        )
        st.rerun()

    compare_key = f"approval-compare-chunk-{document_id}"
    selected_compare_id = str(st.session_state.get(compare_key) or "")
    if ordered_compare_ids and selected_compare_id not in chunk_by_id:
        st.session_state[compare_key] = pending_compare_ids[0] if pending_compare_ids else ordered_compare_ids[0]
    elif pending_compare_ids and selected_compare_id and not _is_chunk_pending_approval(chunk_by_id[selected_compare_id]):
        st.session_state[compare_key] = pending_compare_ids[0]

    def _compare_chunk_label(cid: str) -> str:
        chunk = chunk_by_id[cid]
        location = chunk.metadata.get("hierarchy_path") or chunk.chunk_type
        mark = " ⚠️ 검수 주의" if cid in attention_ids else ""
        return f"{cid} · {location}{mark}"

    compare_id = st.selectbox(
        "검수할 청크 선택",
        options=ordered_compare_ids,
        format_func=_compare_chunk_label,
        key=compare_key,
    )
    compare_chunk = chunk_by_id[str(compare_id)]
    compare_chunk_approvable = _is_chunk_pending_approval(compare_chunk)
    if not compare_chunk_approvable:
        st.info(
            "Selected chunk is already finalized or blocked "
            f"(status={_approval_status(compare_chunk)}). Choose a pending review chunk to approve."
        )
    compare_reasons = review_attention.get(compare_chunk.chunk_id) or chunk_review_attention_reasons(compare_chunk)
    review_items = _approval_ai_review_items(compare_chunk, compare_reasons, agent_review_summary)
    item_ids = [str(item["item_id"]) for item in review_items]
    ai_decisions_key = _approval_chunk_state_key(document_id, compare_chunk.chunk_id, "ai_decisions")
    human_confirmed_key = _approval_chunk_state_key(document_id, compare_chunk.chunk_id, "human_confirmed")
    human_confirmed_widget_key = _approval_chunk_state_key(
        document_id,
        compare_chunk.chunk_id,
        "human_confirmed_widget",
    )
    ai_logged_key = _approval_chunk_state_key(document_id, compare_chunk.chunk_id, "ai_logged")
    human_logged_key = _approval_chunk_state_key(document_id, compare_chunk.chunk_id, "human_logged")
    audit_preview_key = _approval_chunk_state_key(document_id, compare_chunk.chunk_id, "audit_preview")
    hold_events_key = _approval_chunk_state_key(document_id, compare_chunk.chunk_id, "hold_events")
    override_reason_key = _approval_chunk_state_key(document_id, compare_chunk.chunk_id, "override_reason")
    st.session_state.setdefault(ai_decisions_key, {})
    st.session_state.setdefault(human_confirmed_key, False)
    st.session_state.setdefault(audit_preview_key, [])
    st.session_state.setdefault(hold_events_key, [])

    ai_decisions = {
        str(item_id): str(decision)
        for item_id, decision in dict(st.session_state.get(ai_decisions_key) or {}).items()
        if str(decision) in {"reflect", "skip"}
    }
    review_state = approval_review_completion_state(
        item_ids,
        ai_decisions,
        human_confirmed=bool(st.session_state.get(human_confirmed_key)),
    )

    ai_tab, human_tab = st.tabs(
        [
            f"1. AI 검증 확인 {_approval_tab_badge(bool(review_state['ai_confirmed']))}",
            f"2. 사람 검증 확인 {_approval_tab_badge(bool(review_state['human_confirmed']))}",
        ]
    )

    with ai_tab:
        st.markdown("#### AI가 확인이 필요하다고 표시한 부분")
        counter_cols = st.columns(3)
        if st.button("전체 AI 제안 반영 안 함", key=f"ai-bulk-skip-{document_id}"):
            summary = _approval_set_bulk_ai_decisions(
                document_id=document_id,
                chunks=chunks,
                review_attention=review_attention,
                agent_review_summary=agent_review_summary,
                decision="skip",
            )
            st.success(
                f"AI 제안 {summary['item_count']:,}개를 {summary['chunk_count']:,}개 청크에 모두 반영 안 함으로 표시했습니다."
            )
            st.rerun()
        counter_cols[0].metric("표시", f"{int(review_state['total']):,}")
        counter_cols[1].metric("반영", f"{int(review_state['reflected']):,}")
        counter_cols[2].metric("남음", f"{int(review_state['remaining']):,}")
        if not review_items:
            st.success("AI가 따로 제안한 항목이 없습니다. 이 탭은 확인 완료 상태입니다.")
        for item in review_items:
            item_id = str(item["item_id"])
            severity = str(item.get("severity") or "중간")
            current_decision = ai_decisions.get(item_id)
            with st.container(border=True):
                top_cols = st.columns([4, 1])
                top_cols[0].markdown(f"**{item.get('title')}**")
                top_cols[0].caption(str(item.get("location") or ""))
                top_cols[1].markdown(f"**위험도: {severity}**")
                st.info(f"AI 제안: {item.get('suggestion')}")
                action_cols = st.columns([1, 1, 3])
                if action_cols[0].button("반영", key=f"ai-reflect-{item_id}"):
                    ai_decisions[item_id] = "reflect"
                    st.session_state[ai_decisions_key] = ai_decisions
                    st.rerun()
                if action_cols[1].button("반영 안 함", key=f"ai-skip-{item_id}"):
                    ai_decisions[item_id] = "skip"
                    st.session_state[ai_decisions_key] = ai_decisions
                    st.rerun()
                if current_decision == "reflect":
                    action_cols[2].success("반영함")
                elif current_decision == "skip":
                    action_cols[2].caption("반영 안 함")
                else:
                    action_cols[2].warning("아직 결정하지 않음")

        st.markdown("#### AI 표시 부분 수정 전·후 비교")
        st.caption("왼쪽은 저장된 전처리 원문이며, 오른쪽 내용은 직접 고칠 수 있습니다. 수정본은 승인할 때 저장됩니다.")
        edit_cols = st.columns(2)
        with edit_cols[0]:
            st.markdown("**수정 전**")
            st.code(str(compare_chunk.text or ""), language="text")
        with edit_cols[1]:
            st.markdown("**수정 후**")
            _approval_edited_text_from_session(document_id, compare_chunk)
            edited_text_key = _approval_edited_text_key(document_id, compare_chunk.chunk_id)
            edited_text_widget_key = _approval_edited_text_widget_key(document_id, compare_chunk.chunk_id)
            if edited_text_widget_key not in st.session_state:
                st.session_state[edited_text_widget_key] = st.session_state[edited_text_key]
            st.text_area(
                "수정 후 내용",
                key=edited_text_widget_key,
                height=320,
                disabled=not compare_chunk_approvable,
                label_visibility="collapsed",
                on_change=_approval_sync_edited_text_from_widget,
                kwargs={"edited_text_key": edited_text_key, "widget_key": edited_text_widget_key},
            )

    ai_decisions = {
        str(item_id): str(decision)
        for item_id, decision in dict(st.session_state.get(ai_decisions_key) or {}).items()
        if str(decision) in {"reflect", "skip"}
    }
    review_state = approval_review_completion_state(
        item_ids,
        ai_decisions,
        human_confirmed=bool(st.session_state.get(human_confirmed_key)),
    )
    if review_state["ai_confirmed"] and not st.session_state.get(ai_logged_key):
        st.session_state[audit_preview_key].append(
            _approval_audit_preview_entry(
                f"AI 검증 확인 완료 — 반영 {review_state['reflected']} / 반영 안 함 {review_state['skipped']}"
            )
        )
        st.session_state[ai_logged_key] = True

    processed_preview_text = _approval_edited_text_from_session(document_id, compare_chunk)
    with human_tab:
        st.markdown("#### 실제 규정 ↔ 전처리 결과 비교")
        compare_cols = st.columns(2)
        with compare_cols[0]:
            _render_original_source_preview(document, compare_chunk)
        with compare_cols[1]:
            _render_processed_result_preview(compare_chunk, processed_preview_text)
        if compare_reasons:
            st.warning("이 청크의 검수 주의 사유: " + ", ".join(str(reason) for reason in compare_reasons))
        elif compare_chunk.warnings:
            st.caption("이 청크의 경고: " + ", ".join(compare_chunk.warnings))
        if human_confirmed_widget_key not in st.session_state:
            st.session_state[human_confirmed_widget_key] = bool(st.session_state.get(human_confirmed_key))
        st.checkbox(
            "원본과 전처리 결과를 확인했습니다.",
            key=human_confirmed_widget_key,
            on_change=_approval_sync_human_confirmation_from_widget,
            kwargs={
                "human_confirmed_key": human_confirmed_key,
                "human_confirmed_widget_key": human_confirmed_widget_key,
            },
        )

    review_state = approval_review_completion_state(
        item_ids,
        ai_decisions,
        human_confirmed=bool(st.session_state.get(human_confirmed_key)),
    )
    if review_state["human_confirmed"] and not st.session_state.get(human_logged_key):
        st.session_state[audit_preview_key].append(
            _approval_audit_preview_entry("사람 검증 확인 — 원본과 전처리 결과 좌우 비교 완료")
        )
        st.session_state[human_logged_key] = True

    if approved_count >= total_chunks:
        st.info("이 문서는 이미 모든 청크가 승인되어 있습니다. 아래 '이미 승인된 내용 AI에 등록만 실행' 버튼을 사용하세요.")

    st.markdown("### 승인")
    pending_review_entries = [
        _approval_chunk_review_state_from_session(
            document_id=document_id,
            chunk=chunk,
            review_attention=review_attention,
            agent_review_summary=agent_review_summary,
        )
        for chunk in chunks
        if _is_chunk_pending_approval(chunk)
    ]
    reviewed_approval_entries = [
        entry for entry in pending_review_entries if bool(dict(entry["state"]).get("approve_enabled"))
    ]
    if reviewed_approval_entries:
        st.info(
            f"현재 선택한 조항과 관계없이 검수 완료된 전체 미승인 규정·조항 "
            f"{len(reviewed_approval_entries):,}개가 한 번에 승인·색인됩니다."
        )
    approve_enabled = bool(review_state["approve_enabled"])
    override_reason = ""
    if approve_enabled:
        st.success("두 검증 시트를 모두 확인했습니다. 승인하고 색인할 수 있습니다.")
    else:
        st.warning("두 검증 시트가 아직 모두 확인되지 않았습니다. 필요하면 사유를 남기고 확인 없이 승인할 수 있습니다.")
        with st.expander("확인 없이 승인해야 하는 경우", expanded=False):
            override_reason = st.text_area(
                "확인 생략 승인 사유",
                key=override_reason_key,
                placeholder="예: 긴급 배포 필요, 별도 결재 문서에서 원문 대조 완료 등",
            )
    hold_col, approve_col, index_col = st.columns([1, 2, 2])
    if hold_col.button("보류", key=f"approval-hold-{document_id}-{compare_chunk.chunk_id}"):
        hold_event = {
            "event": "held",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": local_auth.actor,
            "chunk_id": compare_chunk.chunk_id,
        }
        st.session_state[hold_events_key].append(hold_event)
        st.session_state[audit_preview_key].append(_approval_audit_preview_entry("보류 — 추가 확인 필요"))
        st.info("보류로 기록했습니다. 승인 전 미리보기 감사 기록에 남습니다.")

    override_reason_text = str(override_reason or "").strip()
    approval_target_entries = reviewed_approval_entries
    if not approval_target_entries and compare_chunk_approvable and (approve_enabled or override_reason_text):
        approval_target_entries = [
            _approval_chunk_review_state_from_session(
                document_id=document_id,
                chunk=compare_chunk,
                review_attention=review_attention,
                agent_review_summary=agent_review_summary,
            )
        ]
    can_approve = bool(approval_target_entries) and (
        all(bool(dict(entry["state"]).get("approve_enabled")) for entry in approval_target_entries)
        or bool(override_reason_text)
    )
    if approve_col.button(
        "승인하고 색인",
        type="primary",
        key=f"approval-approve-index-{document_id}",
        disabled=not can_approve or approved_count >= total_chunks,
    ):
        try:
            selected_security_level = str(st.session_state.get(security_level_key) or "internal")
            edited_chunk_total = _approval_save_text_edits(
                document_id=document_id,
                chunks=chunks,
                entries=approval_target_entries,
                target_repository=repository,
            )
            evidence, templates = _build_current_document_approval_templates(
                ctx,
                security_level=selected_security_level,
                candidate_chunk_ids=[str(entry["chunk_id"]) for entry in approval_target_entries],
            )
            review_events = []
            for entry in approval_target_entries:
                target_chunk = entry["chunk"]
                target_chunk_id = str(entry["chunk_id"])
                target_hold_events_key = _approval_chunk_state_key(document_id, target_chunk_id, "hold_events")
                review_events.extend(list(st.session_state.get(target_hold_events_key) or []))
                review_events.extend(
                    build_approval_review_events(
                        chunk_id=target_chunk_id,
                        actor=local_auth.actor,
                        item_ids=list(entry["item_ids"]),
                        ai_decisions=dict(entry["ai_decisions"]),
                        human_confirmed=bool(entry["human_confirmed"]),
                        table_source=str(target_chunk.metadata.get("table_source") or ""),
                        kordoc_table_promoted=bool(target_chunk.metadata.get("kordoc_table_promoted")),
                        approve_event="approved",
                        override_reason=override_reason_text or None,
                    )
                )
            approved_chunk_total = 0
            approval_progress = st.progress(0, text="승인 0%")
            approval_detail = st.empty()
            for template_index, template in enumerate(templates, start=1):
                segment_start = int((template_index - 1) * 58 / max(len(templates), 1))
                segment_end = int(template_index * 58 / max(len(templates), 1))
                approval_progress.progress(
                    segment_start,
                    text=f"승인 묶음 {template_index - 1:,}/{len(templates):,}",
                )
                chunk_ids = [str(chunk_id) for chunk_id in template["chunk_ids"]]
                template_chunk_ids = set(chunk_ids)
                template_review_events = [
                    event for event in review_events
                    if str(event.get("chunk_id") or "") in template_chunk_ids
                ]
                approval_request = ApprovalRequest(
                        chunk_ids=chunk_ids,
                        security_level=selected_security_level,
                        review_flags_acknowledged=all(
                            bool(dict(entry["state"]).get("approve_enabled")) for entry in approval_target_entries
                        ),
                        worklist_report_path=str(template["worklist_report_path"]),
                        worklist_report_sha256=str(template["worklist_report_sha256"]),
                        review_batch_manifest_path=str(template["review_batch_manifest_path"]),
                        review_batch_manifest_sha256=str(template["review_batch_manifest_sha256"]),
                        review_batch_id=str(template["review_batch_id"]),
                        review_batch_chunk_fingerprint=str(template["review_batch_chunk_fingerprint"]),
                        review_strategy=str(template["review_strategy"]),
                        review_decision_events=template_review_events,
                        approval_override_reason=str(override_reason or "").strip() or None,
                        note="approval_screen_tabs",
                )
                _run_background_operation_with_progress(
                    lambda _report, request=approval_request: approve_review_chunks(
                        document_id,
                        request,
                        local_auth,
                    ),
                    progress_bar=approval_progress,
                    detail_box=approval_detail,
                    start_percent=segment_start,
                    end_percent=segment_end,
                    label=f"승인 묶음 {template_index:,}/{len(templates):,}",
                    estimated_seconds=max(5.0, len(chunk_ids) / 70.0),
                )
                approved_chunk_total += len(chunk_ids)
            result = _run_background_operation_with_progress(
                lambda _report: index_document(
                    document_id,
                    IndexRequest(target_type="local-jsonl", embedding_dimensions=384),
                    local_auth,
                ),
                progress_bar=approval_progress,
                detail_box=approval_detail,
                start_percent=58,
                end_percent=100,
                label="승인 내용 검색 색인",
                estimated_seconds=max(8.0, approved_chunk_total / 65.0),
            )
            _invalidate_document_context_cache(document_id)
            st.success(
                f"승인 {approved_chunk_total:,}개, 수정 저장 {edited_chunk_total:,}개, "
                f"AI 등록 {result.get('record_count', 0):,}개가 완료됐습니다."
            )
            st.caption(f"자동 생성된 증빙: {evidence.get('artifacts', {}).get('review_batches_json', '')}")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if index_col.button(
        "이미 승인된 내용 AI에 등록만 실행",
        key=f"quick-index-only-{document_id}",
        disabled=approved_count <= 0,
    ):
        try:
            with st.status("승인된 내용 검색 색인 중…", expanded=True) as quick_index_status:
                quick_index_progress = st.progress(0, text="색인 준비 · 0%")
                quick_index_detail = st.empty()
                result = _run_background_operation_with_progress(
                    lambda _report: index_document(
                        document_id,
                        IndexRequest(target_type="local-jsonl", embedding_dimensions=384),
                        local_auth,
                    ),
                    progress_bar=quick_index_progress,
                    detail_box=quick_index_detail,
                    start_percent=0,
                    end_percent=100,
                    label="승인 내용 검색 색인",
                    estimated_seconds=max(8.0, approved_count / 65.0),
                )
                quick_index_status.update(label="검색 색인 완료", state="complete")
            _invalidate_document_context_cache(document_id)
            st.success(f"승인된 청크 {result.get('record_count', 0):,}개를 AI에 등록했습니다.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.markdown("### 감사 기록(미리보기)")
    audit_preview = list(st.session_state.get(audit_preview_key) or [])
    if not audit_preview:
        st.caption("아직 기록된 결정이 없습니다.")
    else:
        for event in audit_preview[-10:]:
            st.caption(f"{event.get('timestamp')} · {event.get('message')}")

    show_advanced_approval = st.checkbox(
        "전산 담당자용 고급 승인 절차 보기",
        value=False,
        key=f"show-advanced-approval-{document_id}",
    )
    if not show_advanced_approval:
        st.divider()
        _render_workflow_next_button(
            "④ MCP 생성·AI 연결로 이동",
            NAV_MCP,
            key="approval-goto-connect-simple",
        )
        return

    st.markdown("### 고급 옵션 A. 검수 증빙 직접 관리")
    with st.expander("검수 묶음 파일에서 자동으로 채우기 — Approval worklist evidence", expanded=False):
        st.caption(
            "검수 담당자가 만들어 준 검수 묶음 파일(JSON)의 경로를 붙여 넣고 버튼을 누르면 "
            "아래 증빙 항목이 자동으로 채워집니다."
        )
        approval_template_path = st.text_input(
            "검수 묶음 파일 경로",
            value="",
            placeholder="reports/approval_review_batches_current.json",
            key=f"approval-template-manifest-{document_id}",
            help="Approval review batch manifest JSON",
        )
        approval_template_batch_id = st.text_input(
            "묶음 번호 (선택)",
            value="",
            placeholder="비워 두면 이 문서의 첫 번째 묶음을 불러옵니다",
            key=f"approval-template-batch-id-{document_id}",
            help="Review batch ID to load",
        )
        if st.button(
            "증빙 자동으로 채우기 (Load approval evidence from review batch manifest)",
            key=f"load-approval-template-{document_id}",
        ):
            try:
                template = _load_approval_template_from_manifest(
                    approval_template_path,
                    document_id,
                    review_batch_id=approval_template_batch_id,
                )
                st.session_state[worklist_path_key] = template["worklist_report_path"]
                st.session_state[worklist_sha_key] = template["worklist_report_sha256"]
                st.session_state[batch_manifest_path_key] = template["review_batch_manifest_path"]
                st.session_state[batch_manifest_sha_key] = template["review_batch_manifest_sha256"]
                st.session_state[batch_id_key] = template["review_batch_id"]
                st.session_state[batch_fingerprint_key] = template["review_batch_chunk_fingerprint"]
                st.session_state[approval_chunk_ids_key] = template["chunk_ids"]
                if template["review_strategy"] in {
                    "",
                    "operator_manual_review",
                    "human_bulk_review",
                    "sampled_low_risk_batch_review",
                    "reapproval_after_reprocess",
                }:
                    st.session_state[review_strategy_key] = template["review_strategy"]
                if template["security_level"] in {"internal", "public", "sensitive", "confidential"}:
                    st.session_state[security_level_key] = template["security_level"]
                st.session_state[review_ack_key] = False
                st.success(
                    "증빙을 불러왔습니다. 승인 전에 검수 내용을 다시 확인해 주세요. 확인란은 자동으로 체크되지 않습니다. "
                    "(Approval evidence loaded. Review the batch before approving; acknowledgement was not auto-checked.)"
                )
                st.write(
                    {
                        "review_batch_id": template["review_batch_id"],
                        "chunk_count": template["chunk_count"],
                        "review_flags_acknowledged_required": template["review_flags_acknowledged_required"],
                        "selected_chunk_count": len(template["chunk_ids"]),
                        "available_batch_count": template["available_batch_count"],
                    }
                )
                if int(template["available_batch_count"] or 0) > 1 and not approval_template_batch_id.strip():
                    st.warning(
                        "이 문서에는 검수 묶음이 여러 개 있습니다. 첫 번째 묶음을 불러왔으니, 다른 묶음이 필요하면 묶음 번호를 입력하세요. "
                        "(Multiple approval review batches exist for this document.)"
                    )
            except Exception as exc:
                st.error(str(exc))

    selected_approval_chunk_ids = [
        str(chunk_id)
        for chunk_id in st.session_state.get(approval_chunk_ids_key, [])
        if str(chunk_id).strip()
    ]
    if selected_approval_chunk_ids:
        st.info(f"승인 요청은 불러온 검수 묶음의 청크 {len(selected_approval_chunk_ids):,}개에만 적용됩니다.")

    with st.expander("2. 증빙 직접 입력 (전산 담당자용)", expanded=False):
        st.caption("검수 묶음 파일이 없을 때만 직접 입력합니다. 일반적으로는 위의 자동 채우기를 사용하세요.")
        evidence_col1, evidence_col2 = st.columns(2)
        with evidence_col1:
            worklist_report_path = st.text_input(
                "Worklist report path",
                placeholder="reports/approval_worklist_current.json",
                key=worklist_path_key,
            )
            review_batch_manifest_path = st.text_input(
                "Review batch manifest path",
                placeholder="reports/approval_review_batches_current.json",
                key=batch_manifest_path_key,
            )
            review_batch_id = st.text_input(
                "Review batch ID",
                placeholder="batch-YYYYMMDD-01",
                key=batch_id_key,
            )
            review_batch_chunk_fingerprint = st.text_input(
                "Review batch chunk fingerprint",
                placeholder="64-character batch chunk digest",
                key=batch_fingerprint_key,
            )
        with evidence_col2:
            worklist_report_sha256 = st.text_input(
                "Worklist report SHA-256",
                placeholder="64-character artifact digest",
                key=worklist_sha_key,
            )
            review_batch_manifest_sha256 = st.text_input(
                "Review batch manifest SHA-256",
                placeholder="64-character artifact digest",
                key=batch_manifest_sha_key,
            )
            review_strategy = st.selectbox(
                "Review strategy",
                [
                    "",
                    "operator_manual_review",
                    "human_bulk_review",
                    "sampled_low_risk_batch_review",
                    "reapproval_after_reprocess",
                ],
                key=review_strategy_key,
            )
    required_approval_evidence = {
        "검수 목록 파일 경로 (Worklist report path)": worklist_report_path,
        "검수 목록 파일 확인값 (Worklist report SHA-256)": worklist_report_sha256,
        "검수 묶음 파일 경로 (Review batch manifest path)": review_batch_manifest_path,
        "묶음 번호 (Review batch ID)": review_batch_id,
        "묶음 지문 (Review batch chunk fingerprint)": review_batch_chunk_fingerprint,
    }
    approval_evidence_missing = [
        label for label, value in required_approval_evidence.items() if not str(value or "").strip()
    ]
    official_approval_disabled = bool(approval_evidence_missing)
    if official_approval_disabled:
        st.warning(
            "승인하려면 검수 증빙이 필요합니다. 위 1번에서 '증빙 자동으로 채우기'를 먼저 실행하세요. "
            "(Official RAG/MCP approval requires approval worklist evidence.) "
            f"비어 있는 항목: {', '.join(approval_evidence_missing)}."
        )

    st.markdown("### 고급 옵션 B. 수동 승인하기")
    gate_col1, gate_col2 = st.columns(2)
    with gate_col1:
        selected_security_level = st.selectbox(
            "보안 등급",
            ["internal", "public", "sensitive", "confidential"],
            key=security_level_key,
            format_func=lambda value: SECURITY_LEVEL_LABELS.get(value, value),
            help="이 문서 내용의 보안 수준을 선택하세요. 잘 모르면 '내부용'을 선택합니다.",
        )
        review_flags_acknowledged = st.checkbox(
            "검수 주의 청크(파서/표 경고)를 직접 확인했습니다",
            value=False,
            key=review_ack_key,
            disabled=not bool(review_attention),
        )
    with gate_col2:
        approve_button_label = (
            "선택한 검수 묶음 승인 (Approve selected review batch for RAG)"
            if selected_approval_chunk_ids
            else "모든 청크 승인 (Approve all chunks for RAG)"
        )
        if st.button(
            approve_button_label,
            type="primary",
            key=f"approve-all-{document_id}",
            disabled=official_approval_disabled,
        ):
            try:
                approval_chunk_ids = selected_approval_chunk_ids or [chunk.chunk_id for chunk in chunks]
                approve_review_chunks(
                    document_id,
                    ApprovalRequest(
                        chunk_ids=approval_chunk_ids,
                        security_level=selected_security_level,
                        review_flags_acknowledged=review_flags_acknowledged,
                        worklist_report_path=worklist_report_path,
                        worklist_report_sha256=worklist_report_sha256,
                        review_batch_manifest_path=review_batch_manifest_path,
                        review_batch_manifest_sha256=review_batch_manifest_sha256,
                        review_batch_id=review_batch_id,
                        review_batch_chunk_fingerprint=review_batch_chunk_fingerprint,
                        review_strategy=review_strategy,
                    ),
                    local_auth,
                )
                st.success("승인이 완료됐습니다. 이제 아래 4번에서 색인을 실행하세요.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    st.markdown("### 고급 옵션 C. AI에 등록(색인)하기")
    st.caption("승인한 내용을 AI가 검색할 수 있게 등록하는 단계입니다. 처음이면 왼쪽, 내용을 바꿨으면 오른쪽 버튼을 누르세요.")
    indexing_disabled = approved_count <= 0
    if indexing_disabled:
        st.warning(
            "아직 승인된 청크가 없어 색인할 수 없습니다. 위 3번에서 검수한 내용을 먼저 승인한 뒤 "
            "이 단계로 돌아와 AI에 등록하세요."
        )
    index_col1, index_col2 = st.columns(2)
    with index_col1:
        if st.button(
            "승인된 내용 색인하기 (Index approved chunks)",
            key=f"index-approved-{document_id}",
            disabled=indexing_disabled,
        ):
            try:
                with st.status("승인된 전체 규정 색인 중…", expanded=True) as index_status:
                    index_status.write(f"승인된 청크 {approved_count:,}개를 색인하는 중입니다.")
                    index_progress = st.progress(0, text="색인 준비 · 0%")
                    index_detail = st.empty()
                    result = _run_background_operation_with_progress(
                        lambda _report: index_document(
                            document_id,
                            IndexRequest(target_type="local-jsonl", embedding_dimensions=384),
                            local_auth,
                        ),
                        progress_bar=index_progress,
                        detail_box=index_detail,
                        start_percent=0,
                        end_percent=100,
                        label="승인 규정 색인",
                        estimated_seconds=max(5.0, min(180.0, approved_count / 80.0)),
                    )
                    index_status.update(label="전체 규정 색인 완료", state="complete")
                _invalidate_document_context_cache(document_id)
                st.success(f"승인된 청크 {result.get('record_count', 0)}개를 색인했습니다.")
            except Exception as exc:
                st.error(str(exc))
    with index_col2:
        if st.button(
            "다시 색인하기 (Reindex approved chunks)",
            key=f"reindex-approved-{document_id}",
            disabled=indexing_disabled,
        ):
            try:
                with st.status("승인된 전체 규정 재색인 중…", expanded=True) as index_status:
                    index_status.write(f"승인된 청크 {approved_count:,}개를 다시 색인하는 중입니다.")
                    index_progress = st.progress(0, text="재색인 준비 · 0%")
                    index_detail = st.empty()
                    result = _run_background_operation_with_progress(
                        lambda _report: reindex_document(
                            document_id,
                            IndexRequest(target_type="local-jsonl", embedding_dimensions=384),
                            local_auth,
                        ),
                        progress_bar=index_progress,
                        detail_box=index_detail,
                        start_percent=0,
                        end_percent=100,
                        label="승인 규정 재색인",
                        estimated_seconds=max(5.0, min(180.0, approved_count / 70.0)),
                    )
                    index_status.update(label="전체 규정 재색인 완료", state="complete")
                _invalidate_document_context_cache(document_id)
                removed = result.get("upsert_summary", {}).get("removed_count", 0)
                st.success(f"청크 {result.get('record_count', 0)}개를 다시 색인하고 오래된 기록 {removed}개를 정리했습니다.")
            except Exception as exc:
                st.error(str(exc))

    st.divider()
    _render_workflow_next_button(
        f"선택한 {len(selected_document_ids):,}개 규정을 ④ MCP 생성·AI 연결로 이동",
        NAV_MCP,
        key="approval-goto-connect",
        disabled=not selected_document_ids,
    )


# ---------------------------------------------------------------------------
# 페이지: 시범 질의응답 / ④ MCP 생성·AI 연결
# ---------------------------------------------------------------------------

AI_REVIEW_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "azure-openai": "Azure OpenAI",
    "anthropic": "Anthropic Claude",
    "openai-compatible": "OpenAI 호환 API (사내·로컬)",
}
AI_REVIEW_MODEL_PRESETS = {
    "openai": (
        ("gpt-4.1-mini", "gpt-4.1-mini (권장: 속도·비용·정확도 균형)"),
        ("gpt-4.1", "gpt-4.1 (더 정밀한 검수)"),
        ("gpt-4o-mini", "gpt-4o-mini (경제형)"),
    ),
    "anthropic": (
        ("claude-sonnet-5", "Claude Sonnet 5 (정밀 검수)"),
        ("claude-haiku-4-5", "Claude Haiku 4.5 (빠른 검수)"),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
    ),
}


def _review_provider_key(settings_snapshot, provider: str) -> str:
    if provider == "azure-openai":
        return str(settings_snapshot.azure_openai_api_key or "")
    if provider == "anthropic":
        return str(settings_snapshot.anthropic_api_key or "")
    if provider == "openai-compatible":
        return str(settings_snapshot.openai_compatible_api_key or "")
    return str(settings_snapshot.openai_api_key or "")


def _review_api_connection_status(s) -> tuple[str, str]:
    """외부 검수 API 연결 상태를 (수준, 안내문)으로 돌려준다."""

    if not s.enable_agent_review:
        return ("off", "AI 검수는 꺼져 있습니다. 외부 전송 없이 로컬 파서와 사람 검수만 사용합니다.")
    provider = normalize_agent_review_provider(s.llm_provider)
    reason = agent_review_configuration_reason(s)
    reason_messages = {
        "agent_review_provider_not_supported": "지원하는 AI 공급자를 선택하세요.",
        "agent_review_model_missing": "검수 모델 또는 Azure 배포 이름을 입력하세요.",
        "openai_api_key_missing": "OpenAI API 키를 입력하세요.",
        "azure_openai_endpoint_missing": "Azure OpenAI 엔드포인트를 입력하세요.",
        "azure_openai_api_key_missing": "Azure OpenAI API 키를 입력하세요.",
        "anthropic_api_key_missing": "Anthropic API 키를 입력하세요.",
        "openai_compatible_base_url_missing": "OpenAI 호환 API 주소를 입력하세요.",
    }
    if reason:
        return ("warn", reason_messages.get(reason, f"AI 검수 설정을 확인하세요: {reason}"))
    provider_label = AI_REVIEW_PROVIDER_LABELS.get(provider, provider)
    return ("ok", f"AI 검수 준비됨 · {provider_label} · {s.agent_review_model}")


def _render_status_line(level: str, message: str) -> None:
    if level == "ok":
        st.success(message)
    elif level == "warn":
        st.warning(message)
    else:
        st.info(message)


def _render_ai_connection_status_banner(settings_snapshot, *, context: str) -> None:
    """전처리·시범 질의응답·MCP 화면에 AI 검수 연결 상태를 요약한다.

    입력·수정은 '⚙️ 관리자 설정 → AI 연결'에서만 한다(단일 관리 지점).
    이 API 연결은 전처리(AI 검수) 전용이고, 실제 질의응답은 MCP로 외부 AI가 한다.
    """

    review_level, review_message = _review_api_connection_status(settings_snapshot)
    st.markdown("**AI 검수 연결 상태**")
    if context == "preprocess":
        st.caption("문서를 올리면 이 설정으로 AI 검수 초안을 만듭니다. (실제 사용량만큼 과금)")
    else:
        st.caption("이 API 연결은 전처리(검수) 전용입니다. 실제 질의응답은 '④ MCP 생성·AI 연결'로 외부 AI를 붙여서 합니다.")
    _render_status_line(review_level, review_message)
    st.button(
        "⚙️ 관리자 설정에서 AI 연결 입력·수정하기",
        key=f"ai-connection-goto-admin-{context}",
        on_click=_go,
        args=(NAV_ADMIN,),
    )


def _render_api_key_setup_cta(context: str) -> None:
    """Keep the API setup action unmistakable while secrets remain outside project saves."""
    review_level, review_message = _review_api_connection_status(settings)
    _render_status_line(review_level, review_message)
    if st.button(
        "AI 검수 공급자·모델·API 키 설정",
        key=f"api-key-setup-cta-{context}",
        type="primary",
        width="stretch",
    ):
        st.session_state[OPEN_API_KEY_DIALOG_KEY] = True
    if st.session_state.get(OPEN_API_KEY_DIALOG_KEY):
        _render_api_key_setup_dialog()


def _render_ai_connection_settings(settings_snapshot) -> None:
    """'⚙️ 관리자 설정 → AI 연결' 탭. 검수 API와 챗봇 LLM 접속 정보를 입력받는다."""

    st.markdown("### AI 연결 설정")
    st.caption(
        "AI 검수는 선택 기능입니다. 기능을 켠 경우에만 선택한 공급자로 의심 구간을 전송해 검수 초안을 만듭니다. "
        "키는 이 세션(현재 실행) 메모리에만 저장되고 디스크에는 남지 않습니다. "
        "PC를 재시작해도 유지하려면 전산 담당자가 .env 파일에 넣어 두면 됩니다."
    )
    st.info(
        "이 API 연결은 **전처리(AI 검수)** 에만 씁니다. 승인된 규정으로 실제 질의응답은 "
        "이 앱이 직접 답을 만드는 게 아니라, '④ MCP 생성·AI 연결'에서 만든 설정으로 "
        "외부 범용 AI(Claude·ChatGPT·Codex)를 붙여서 합니다."
    )

    review_level, review_message = _review_api_connection_status(settings_snapshot)
    st.markdown("**검수용 외부 AI (문서 검수 초안 생성)**")
    _render_status_line(review_level, review_message)

    configured_provider = normalize_agent_review_provider(settings_snapshot.llm_provider)
    if configured_provider not in SUPPORTED_AGENT_REVIEW_PROVIDERS:
        configured_provider = "openai"
    review_provider = st.selectbox(
        "AI 공급자",
        options=list(SUPPORTED_AGENT_REVIEW_PROVIDERS),
        index=list(SUPPORTED_AGENT_REVIEW_PROVIDERS).index(configured_provider),
        format_func=lambda value: AI_REVIEW_PROVIDER_LABELS.get(value, value),
        key="ai-review-provider-choice",
    )

    model_presets = list(AI_REVIEW_MODEL_PRESETS.get(review_provider, ()))
    configured_model = str(settings_snapshot.agent_review_model or "").strip()
    if review_provider != configured_provider:
        configured_model = model_presets[0][0] if model_presets else ""
    if model_presets:
        model_ids = [model_id for model_id, _label in model_presets]
        model_labels = dict(model_presets)
        model_options = [*model_ids, "__custom__"]
        current_model_option = configured_model if configured_model in model_ids else "__custom__"
        model_choice = st.selectbox(
            "검수 모델",
            options=model_options,
            index=model_options.index(current_model_option),
            format_func=lambda value: model_labels.get(value, "직접 입력"),
            key=f"ai-review-model-preset-{review_provider}",
        )
        if model_choice == "__custom__":
            review_model = st.text_input(
                "모델 ID 직접 입력",
                value=configured_model if configured_model not in model_ids else "",
                key=f"ai-review-model-custom-{review_provider}",
            )
        else:
            review_model = model_choice
    else:
        model_label = "Azure 배포 이름" if review_provider == "azure-openai" else "모델 ID"
        review_model = st.text_input(
            model_label,
            value=configured_model,
            placeholder="예: review-deployment" if review_provider == "azure-openai" else "예: local-model",
            key=f"ai-review-model-direct-{review_provider}",
        )

    if review_provider == "openai":
        st.info("OpenAI 검수 기본 권장 모델은 gpt-4.1-mini입니다. 실제 승인 전에는 사람이 원문을 확인합니다.")

    with st.form("ai-connection-form"):
        st.markdown(f"#### {AI_REVIEW_PROVIDER_LABELS[review_provider]} 검수 설정")
        st.caption("켜면 실제 사용량에 따라 비용이 발생하고 선택한 의심 구간이 외부 공급자로 전송될 수 있습니다.")
        review_enabled = st.checkbox(
            "AI 검수 사용 (켠 경우에만 외부 API 호출)",
            value=bool(settings_snapshot.enable_agent_review),
        )
        review_api_key = st.text_input(
            "API 키" + (" (로컬 무인증 서버는 비워도 됨)" if review_provider == "openai-compatible" else ""),
            value=_review_provider_key(settings_snapshot, review_provider),
            type="password",
            help="이 값은 현재 실행 중인 세션 메모리에만 저장됩니다.",
        )
        if review_provider == "azure-openai":
            review_base_url = st.text_input(
                "Azure OpenAI 엔드포인트",
                value=str(settings_snapshot.azure_openai_endpoint or ""),
                placeholder="https://YOUR-RESOURCE.openai.azure.com",
            )
        elif review_provider == "anthropic":
            review_base_url = st.text_input(
                "Anthropic API 주소",
                value=str(settings_snapshot.anthropic_api_base_url or "https://api.anthropic.com"),
            )
        else:
            default_base_url = "https://api.openai.com"
            if review_provider == "openai-compatible" and configured_provider != "openai-compatible":
                default_base_url = "http://127.0.0.1:11434/v1"
            review_base_url = st.text_input(
                "API 주소",
                value=(
                    str(settings_snapshot.agent_review_api_base_url or default_base_url)
                    if review_provider == configured_provider
                    else default_base_url
                ),
                placeholder=default_base_url,
            )

        connection_saved = st.form_submit_button("API 연결 저장하기", type="primary")

    if connection_saved:
        overrides = {
            "enable_agent_review": bool(review_enabled),
            "llm_provider": review_provider,
            "agent_review_model": str(review_model or ""),
            "openai_api_key": str(settings_snapshot.openai_api_key or ""),
            "openai_compatible_api_key": str(settings_snapshot.openai_compatible_api_key or ""),
            "azure_openai_api_key": str(settings_snapshot.azure_openai_api_key or ""),
            "azure_openai_endpoint": str(settings_snapshot.azure_openai_endpoint or ""),
            "anthropic_api_key": str(settings_snapshot.anthropic_api_key or ""),
            "anthropic_api_base_url": str(settings_snapshot.anthropic_api_base_url or "https://api.anthropic.com"),
            "agent_review_api_base_url": str(settings_snapshot.agent_review_api_base_url or "https://api.openai.com"),
        }
        if review_provider == "openai":
            overrides["openai_api_key"] = str(review_api_key or "")
            overrides["agent_review_api_base_url"] = _blank_to_none(review_base_url) or "https://api.openai.com"
        elif review_provider == "azure-openai":
            overrides["azure_openai_api_key"] = str(review_api_key or "")
            overrides["azure_openai_endpoint"] = str(review_base_url or "")
        elif review_provider == "anthropic":
            overrides["anthropic_api_key"] = str(review_api_key or "")
            overrides["anthropic_api_base_url"] = _blank_to_none(review_base_url) or "https://api.anthropic.com"
        else:
            overrides["openai_compatible_api_key"] = str(review_api_key or "")
            overrides["agent_review_api_base_url"] = str(review_base_url or "")
        st.session_state[AI_CONNECTION_STATE_KEY] = overrides
        st.session_state.pop(OPEN_API_KEY_DIALOG_KEY, None)
        set_runtime_settings_overrides(**overrides)
        st.success("AI 검수 연결 정보를 저장했습니다. 이제 전처리 시 이 설정으로 검수 초안을 만듭니다.")
        st.rerun()

    if st.button("연결 초기화 (.env 값으로 되돌리기)", key="ai-connection-reset"):
        st.session_state.pop(AI_CONNECTION_STATE_KEY, None)
        st.session_state.pop(OPEN_API_KEY_DIALOG_KEY, None)
        set_runtime_settings_overrides()
        st.success("화면에서 입력한 연결값을 지웠습니다. .env/환경변수 값으로 되돌립니다.")
        st.rerun()

    with st.expander("전산 담당자용 — 환경변수로 영구 설정하기", expanded=False):
        st.markdown(
            """
            - 이 화면 입력값은 현재 실행 중인 프로그램에만 적용되고 재시작하면 사라집니다.
            - PC 재시작 후에도 유지하려면 아래 항목을 `.env`(또는 환경변수)에 넣으세요:
              `ENABLE_AGENT_REVIEW`, `LLM_PROVIDER`, `AGENT_REVIEW_MODEL`, `AGENT_REVIEW_API_BASE_URL`,
              `OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
              `ANTHROPIC_API_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY`.
            - 승인된 규정으로 실제 질의응답을 하려면 '④ MCP 생성·AI 연결'에서 만든 설정으로 외부 범용 AI를 붙입니다(이 앱이 답을 생성하지 않음).
            """
        )


def _dismiss_api_key_setup_dialog() -> None:
    st.session_state.pop(OPEN_API_KEY_DIALOG_KEY, None)


@_streamlit_dialog(
    "AI 검수 설정",
    width="large",
    on_dismiss=_dismiss_api_key_setup_dialog,
)
def _render_api_key_setup_dialog() -> None:
    """Show provider, model, key, and API address without leaving the current page."""
    _render_ai_connection_settings(settings)


def _page_connect(ctx: dict | None, *, mcp_first: bool = False) -> None:
    heading = "④ MCP 생성·AI 연결" if mcp_first else "승인 데이터 검색 점검"
    st.markdown(f"## {heading}")
    _render_operator_project_controls(NAV_MCP)
    st.markdown(
        '<div class="rr-help">AI 연결 정보(API 키·모델·주소)는 <b>⚙️ 관리자 설정 → AI 연결</b>에서 한 번만 입력하면 됩니다. '
        "이 화면은 승인된 규정의 <b>검색 결과를 점검</b>하고 <b>④ MCP 생성·AI 연결</b> 설정 묶음을 만드는 곳입니다.</div>",
        unsafe_allow_html=True,
    )

    # 입력은 관리자 설정에서 하고, 여기서는 연결 상태만 확인한다.
    _render_ai_connection_status_banner(settings, context="connect")

    st.divider()
    if not _require_document_context(ctx):
        st.info("승인 데이터 검색 점검과 MCP 생성·AI 연결은 '① 문서 올려서 전처리'를 마친 뒤 이 화면에서 이어집니다.")
        return
    selected_document_ids = _render_workflow_document_directory(page_key="mcp")
    document_id = ctx["document_id"]
    document = ctx["document"]
    document_tenant_id = ctx["document_tenant_id"]
    local_auth = ctx["local_auth"]
    mcp_connection_gate = ctx["mcp_connection_gate"]
    mcp_connection_ready = bool(mcp_connection_gate.get("ready"))
    missing_mcp_source_metadata = _missing_mcp_source_metadata(document)
    selected_profile_id = _selected_institution_profile_id()
    document_profile_id = str(getattr(document, "profile_id", "") or "").strip().lower()
    mcp_profile_scope_mismatch = bool(
        selected_profile_id and document_profile_id and document_profile_id != selected_profile_id
    )
    if mcp_profile_scope_mismatch:
        st.error(
            "현재 선택한 기관과 문서의 기관 프로필이 다릅니다. "
            "기관을 전환하거나 해당 기관의 문서를 선택한 뒤 MCP를 생성하세요."
        )

    if _unreviewed_preview_requested():
        st.warning(UNREVIEWED_PREVIEW_WARNING_KO + "\n\n" + UNREVIEWED_PREVIEW_WARNING)

    chat_label = "승인 데이터 검색 점검 (발췌·과금 없음)"
    mcp_label = "AI 프로그램 연결 — MCP 연결 (전산 담당자용)"
    first_tab, second_tab = st.tabs([mcp_label, chat_label] if mcp_first else [chat_label, mcp_label])
    if mcp_first:
        mcp_tab, chat_tab = first_tab, second_tab
    else:
        chat_tab, mcp_tab = first_tab, second_tab

    with chat_tab:
        st.markdown("### 승인 데이터 검색 점검 (발췌 답변)")
        st.caption(
            "승인된 규정에서 근거를 찾아 그대로 발췌해 보여 주는 시범입니다. 검색이 제대로 되는지 확인하는 용도이며, "
            "외부 AI 호출 없이 동작합니다(과금 없음). 실제 활용은 '④ MCP 생성·AI 연결'로 범용 AI를 붙여서 하세요."
        )
        if not mcp_connection_ready:
            st.warning(
                "검색 점검은 승인·색인이 끝난 내용만 사용합니다. 먼저 '③ 검수하고 승인'을 완료해 주세요.\n\n"
                "Local RAG demo uses approved and indexed chunks only. "
                "Complete human review, approval, index/reindex, and the MCP visibility gate before running official RAG."
            )
        chat_col1, chat_col2 = st.columns(2)
        with chat_col1:
            chat_security_levels = st.multiselect(
                "검색 허용 보안 등급",
                ["public", "internal", "sensitive", "confidential"],
                default=["internal"],
                key=f"rag-chat-security-{document_id}",
                format_func=lambda value: SECURITY_LEVEL_LABELS.get(value, value),
            )
        with chat_col2:
            chat_top_k = st.slider("근거로 보여줄 조각 수", 1, 10, 5, key=f"rag-chat-top-k-{document_id}")
            chat_historical_mode = st.checkbox(
                "과거 효력일 기준으로 조회",
                key=f"rag-chat-historical-{document_id}",
            )
            chat_as_of_date = st.date_input(
                "기준일",
                value=date.today(),
                disabled=not chat_historical_mode,
                key=f"rag-chat-as-of-date-{document_id}",
            )
        chat_query = st.text_area("질문", key=f"rag-chat-query-{document_id}", height=96, placeholder="예: 출장비 정산 기한은 언제까지인가요?")
        if st.button("시범 실행 (Run demo)", key=f"run-rag-chat-{document_id}", disabled=not mcp_connection_ready):
            if not chat_query.strip():
                st.warning("먼저 질문을 입력해 주세요.")
            else:
                try:
                    response = rag_chat(
                        RagChatRequest(
                            query=chat_query,
                            top_k=chat_top_k,
                            security_levels=chat_security_levels or None,
                            document_id=document_id,
                            profile_id=selected_profile_id,
                            as_of_date=chat_as_of_date.isoformat() if chat_historical_mode else None,
                            llm_backend="extractive",
                        ),
                        local_auth,
                    )
                    st.markdown("#### 발췌 답변")
                    st.write(response.get("answer", ""))
                    citations = response.get("citations") or []
                    if citations:
                        st.markdown("#### 근거 (Citations)")
                        st.dataframe(pd.DataFrame(citations), width="stretch")
                except Exception as exc:
                    st.error(str(exc))

    with mcp_tab:
        st.markdown("### MCP client connection")
        st.caption("승인·인덱싱 후 Claude Desktop, Claude Code, ChatGPT, Claude API에 붙일 설정을 생성합니다.")
        with st.expander("MCP/AI connection guide", expanded=False):
            st.markdown(
                """
                - 이 프로그램은 승인된 규정 데이터베이스와 MCP 서버 명령, 클라이언트 설정 묶음을 만들어 줍니다. 실제 연결 등록은 담당자가 각 AI 프로그램에서 직접 승인해야 합니다.
                - This program creates the approved local regulation database, MCP server command, and client setup bundle.
                - Operator action is still required: register or approve the generated MCP connection in Claude Desktop, Claude Code, ChatGPT, Codex, or an internal AI platform.
                - AI review draft generation is part of the main preprocessing flow. Set `OPENAI_API_KEY` and `AGENT_REVIEW_MODEL` to run it through the API.
                - If the API key is empty, preprocessing still records the selected AI review targets and waits for configuration instead of publishing unreviewed output.
                - Codex can connect as an MCP client, but it is not a replacement API key for this product runtime.
                """
            )
        if mcp_connection_ready:
            st.success("MCP setup bundle gate passed: approved chunks are indexed and visible.")
        else:
            st.warning(
                "승인·색인이 끝나야 설정 묶음을 만들 수 있습니다. "
                "MCP setup bundle can be written only after approved chunks are indexed and visible. "
                f"Current gate: {mcp_connection_gate.get('reason')}"
            )
        if missing_mcp_source_metadata:
            st.warning(
                "MCP handoff bundle requires citation/source metadata before export. "
                "Missing fields: "
                + ", ".join(missing_mcp_source_metadata)
                + ". Click the bundle button to auto-fill local provenance and reindex approved chunks."
            )
            if st.button(
                "출처 메타데이터 자동 보완 후 다시 색인",
                key=f"repair-mcp-source-metadata-{document_id}",
            ):
                try:
                    document, source_metadata_patch = _ensure_mcp_source_metadata(
                        document,
                        tenant_id=document_tenant_id,
                        target_repository=repository,
                    )
                    if not source_metadata_patch:
                        st.info("보완할 출처 메타데이터가 없습니다.")
                    elif int(mcp_connection_gate.get("approved_count") or 0) > 0:
                        result = index_document(
                            document_id,
                            IndexRequest(target_type="local-jsonl", embedding_dimensions=384),
                            local_auth,
                        )
                        st.success(
                            "출처 메타데이터를 보완하고 승인된 내용을 다시 색인했습니다. "
                            f"AI 등록 {result.get('record_count', 0):,}개."
                        )
                        st.rerun()
                    else:
                        st.success(
                            "출처 메타데이터를 보완했습니다. 검수·승인 후 색인을 실행하면 MCP 생성 버튼이 활성화됩니다."
                        )
                        st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        status_cols = st.columns(4)
        status_cols[0].metric("승인 청크", int(mcp_connection_gate.get("approved_count") or 0))
        status_cols[1].metric("MCP 노출 기록", int(mcp_connection_gate.get("mcp_visible_count") or 0))
        status_cols[2].metric("색인 상태", str(mcp_connection_gate.get("indexing_status") or "-"))
        status_cols[3].metric("오래된 기록", int(mcp_connection_gate.get("stale_count") or 0))
        st.caption("아래 버튼을 누르면 Claude Desktop/Claude Code/ChatGPT/Claude API 연결에 필요한 파일 묶음이 생성됩니다.")
        mcp_scope = st.radio(
            "MCP 데이터 범위",
            ["selected_documents", "current_document", "selected_institution"],
            format_func=lambda value: {
                "selected_documents": f"선택한 규정 {len(selected_document_ids):,}개",
                "current_document": "현재 연 규정만",
                "selected_institution": "선택 기관의 승인 규정 전체",
            }[value],
            key=f"mcp-data-scope-{document_id}",
            horizontal=True,
        )
        workflow_documents_by_id = {
            str(getattr(item, "document_id", "") or ""): item
            for item in _workflow_documents()
        }
        if mcp_scope == "selected_documents":
            scope_documents = [
                workflow_documents_by_id[document_id]
                for document_id in selected_document_ids
                if document_id in workflow_documents_by_id
            ]
        elif mcp_scope == "current_document":
            scope_documents = [document]
        else:
            scope_documents = _documents_for_selected_institution()
        missing_mcp_source_metadata = sorted(
            {
                field
                for scope_document in scope_documents
                for field in _missing_mcp_source_metadata(scope_document)
            }
        )
        scope_document_ids = [
            str(getattr(scope_document, "document_id", "") or "")
            for scope_document in scope_documents
        ]
        kordoc_command = str(getattr(settings, "kordoc_table_command", "") or "")
        command_refresh_key = f"kordoc_command_status_refreshed:{kordoc_command}"
        if not st.session_state.get(command_refresh_key):
            kordoc_table_command_status.cache_clear()
            st.session_state[command_refresh_key] = True
        kordoc_preflight = _mcp_kordoc_preflight(
            repository,
            scope_document_ids,
            command=kordoc_command,
        )
        if kordoc_preflight["required_document_count"]:
            st.markdown("#### Kordoc 표 파싱 사전 점검")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "document_id": item.get("document_id"),
                            "file_type": item.get("file_type"),
                            "required": item.get("required"),
                            "status": item.get("status") or "missing",
                            "parser": item.get("parser") or "missing",
                            "table_count": item.get("table_count", 0),
                        }
                        for item in kordoc_preflight["documents"]
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
            if kordoc_preflight["ready"]:
                st.success(
                    "Kordoc 표 파싱 증거가 모두 확인되었습니다. "
                    f"{kordoc_preflight['parsed_document_count']:,}/"
                    f"{kordoc_preflight['required_document_count']:,} 문서"
                )
            else:
                command_status = kordoc_preflight["command_status"]
                command_label = str(command_status.get("label") or "kordoc")
                missing_document_ids = [
                    str(item.get("document_id") or "").strip()
                    for item in kordoc_preflight["missing"]
                    if str(item.get("document_id") or "").strip()
                ]
                if command_status.get("available"):
                    version = str(command_status.get("version") or "unknown")
                    st.warning(
                        "설치된 Kordoc으로 증거가 없는 원본을 새 초안에서 재전처리합니다. "
                        f"기존 승인본·승인 기록·색인은 그대로 보존됩니다 (명령={command_label}, 버전={version})."
                    )
                    institution_history_scope = mcp_scope == "selected_institution"
                    automatic_single = len(missing_document_ids) == 1 and not institution_history_scope
                    attempt_key = (
                        _kordoc_auto_reprocess_attempt_key(repository, missing_document_ids[0])
                        if automatic_single
                        else ""
                    )
                    automatic_trigger = bool(
                        automatic_single
                        and attempt_key
                        and not st.session_state.get(attempt_key)
                    )
                    if automatic_trigger:
                        st.session_state[attempt_key] = True
                    retry_trigger = False
                    if institution_history_scope:
                        st.info(
                            "기관 전체 범위에는 과거 superseded 이력도 포함됩니다. "
                            "먼저 '현재 연 규정만' 또는 '선택한 규정' 범위에서 각 규정을 안전 재전처리·승인·색인해 주세요."
                        )
                    else:
                        retry_trigger = st.button(
                            (
                                "설치된 Kordoc으로 안전 재전처리 다시 실행"
                                if automatic_single
                                else f"증거 없는 규정 {len(missing_document_ids):,}개 안전 재전처리"
                            ),
                            key=f"kordoc-safe-reprocess-{document_id}-{mcp_scope}",
                            type="primary",
                            help="기존 문서를 덮어쓰지 않고 새 draft 문서 ID에서 처리합니다.",
                        )
                    if automatic_trigger or retry_trigger:
                        if retry_trigger and attempt_key:
                            st.session_state[attempt_key] = True
                        try:
                            with st.status("Kordoc 안전 재전처리 중…", expanded=True) as reprocess_status:
                                reprocess_progress = st.progress(0, text="새 초안 준비 · 0%")
                                reprocess_detail = st.empty()
                                reprocess_results = _run_background_operation_with_progress(
                                    lambda report: _safe_kordoc_reprocess_documents(
                                        settings,
                                        repository,
                                        missing_document_ids,
                                        quality_profile=quality_profile_config,
                                        progress_callback=report,
                                    ),
                                    progress_bar=reprocess_progress,
                                    detail_box=reprocess_detail,
                                    start_percent=0,
                                    end_percent=100,
                                    label="Kordoc 재전처리·증거 검증",
                                    estimated_seconds=max(60.0, 120.0 * len(missing_document_ids)),
                                )
                                reprocess_status.update(
                                    label="Kordoc 재전처리와 증거 검증 완료",
                                    state="complete",
                                )
                            for result in reprocess_results:
                                _replace_workflow_document_id(
                                    result.source_document_id,
                                    result.draft_document_id,
                                )
                            st.session_state[KORDOC_REPROCESS_NOTICE_KEY] = {
                                "document_id": str(st.session_state.get("document_id") or ""),
                                "count": len(reprocess_results),
                                "draft_document_ids": [result.draft_document_id for result in reprocess_results],
                            }
                            _go(NAV_RESULTS)
                            st.rerun()
                        except KordocReprocessingError as exc:
                            st.error(str(exc))
                            if exc.draft_document_id:
                                st.caption(
                                    f"실패한 새 초안 {exc.draft_document_id}은 승인·색인되지 않았으며 기존 문서는 변경되지 않았습니다."
                                )
                        except (FileNotFoundError, KeyError, ValueError) as exc:
                            st.error(redact_sensitive_paths(str(exc)))
                            st.info("저장된 원본이 없으면 ① 단계에서 원본 파일을 다시 올려 주세요.")
                        except Exception as exc:
                            st.error(redact_sensitive_paths(str(exc)))
                            st.info("기존 승인본은 변경되지 않았습니다. 원본과 Kordoc 상태를 확인한 뒤 다시 시도해 주세요.")
                else:
                    auto_install_result: dict[str, Any] | None = None
                    auto_install_key = "kordoc_auto_install_attempted"
                    if not bool(st.session_state.get(auto_install_key)):
                        st.session_state[auto_install_key] = True
                        with st.spinner("Kordoc이 없어 자동 설치·검증을 시도하는 중..."):
                            auto_install_result = _run_kordoc_installer()
                        if auto_install_result.get("ok"):
                            kordoc_table_command_status.cache_clear()
                            st.success("Kordoc 자동 설치·검증이 완료됐습니다. 명령 상태를 다시 확인합니다.")
                            st.rerun()
                    st.error(
                        f"Kordoc 명령({command_label})을 현재 실행 환경에서 찾을 수 없습니다. "
                        "자동 설치가 실패했으면 Node.js LTS/npm을 확인한 뒤 다시 시도하세요."
                    )
                    if st.button(
                        "Kordoc 설치·검증 다시 실행",
                        key=f"kordoc-install-run-{document_id}-{mcp_scope}",
                        help="Node.js/npm이 설치된 Windows PC에서 Kordoc을 설치하고 사용자 PATH를 확인합니다.",
                    ):
                        with st.spinner("Kordoc 설치·검증 중..."):
                            install_result = _run_kordoc_installer()
                        if install_result.get("ok"):
                            kordoc_table_command_status.cache_clear()
                            st.success("Kordoc 설치·검증이 완료됐습니다. 화면을 새로 고쳐 명령 상태를 다시 확인합니다.")
                            if install_result.get("output"):
                                st.code(str(install_result["output"]), language="text")
                            st.rerun()
                        else:
                            error_code = str(install_result.get("error") or "installer_failed")
                            st.error(
                                f"Kordoc 설치·검증을 완료하지 못했습니다 ({error_code}). "
                                "Node.js LTS 설치 여부와 npm 오류를 확인한 뒤 README의 수동 명령을 실행하세요."
                            )
                            if install_result.get("output"):
                                st.code(str(install_result["output"]), language="text")
                    elif auto_install_result and auto_install_result.get("output"):
                        st.caption("자동 설치 시도 결과")
                        st.code(str(auto_install_result["output"]), language="text")
                missing_ids = ", ".join(missing_document_ids[:10])
                if missing_ids:
                    st.info(
                        f"Kordoc 증거가 없는 문서: {missing_ids}. "
                        "기존 approved chunk를 직접 수정하거나 게이트를 끄지 마세요."
                    )
        mcp_export_document_id = document_id if mcp_scope == "current_document" else None
        mcp_export_document_ids = selected_document_ids if mcp_scope == "selected_documents" else None
        selected_scope_gate = (
            _workflow_mcp_gate_summary(selected_document_ids, ctx)
            if mcp_scope == "selected_documents"
            else None
        )
        if selected_scope_gate is not None:
            st.markdown("#### 선택 규정 MCP 준비 상태")
            st.dataframe(pd.DataFrame(selected_scope_gate["rows"]), width="stretch", hide_index=True)
            if not selected_scope_gate["ready"]:
                st.warning("선택한 모든 규정의 검수·승인·색인을 완료해야 규정이 빠지지 않은 MCP를 만들 수 있습니다.")
        mcp_bundle_ready = bool(scope_documents) and bool(kordoc_preflight["ready"]) and (
            bool(selected_scope_gate and selected_scope_gate["ready"])
            if mcp_scope == "selected_documents"
            else mcp_connection_ready
        )
        mcp_connection_target_labels = {
            "claude-code": "Claude Code",
            "codex": "Codex CLI",
            "claude-desktop": "Claude Desktop",
            "chatgpt-desktop-local": "ChatGPT Desktop",
            "chatgpt-remote": "ChatGPT 원격 MCP (HTTPS)",
            "chatgpt-tunnel": "ChatGPT 웹 (보안 Tunnel MCP)",
            "claude-api": "Claude (HTTPS MCP)",
        }
        mcp_connection_target_options = [
            "claude-code",
            "codex",
            "claude-desktop",
            "chatgpt-desktop-local",
            "chatgpt-remote",
            "chatgpt-tunnel",
            "claude-api",
        ]
        mcp_connection_target_key = f"mcp-connection-target-{document_id}"
        if st.session_state.get(mcp_connection_target_key) not in {None, *mcp_connection_target_options}:
            del st.session_state[mcp_connection_target_key]
        mcp_connection_target = st.radio(
            "연결할 AI 앱",
            mcp_connection_target_options,
            format_func=lambda value: mcp_connection_target_labels.get(value, value),
            key=mcp_connection_target_key,
            horizontal=True,
        )
        st.caption(
            "ChatGPT Desktop에는 Settings > MCP servers 내장 등록 안내와 보조 BAT를 만듭니다. "
            "Codex CLI·Claude Code에는 에이전트 연결 요청문과 보조 BAT를 만들며, Claude Desktop은 전용 BAT를 기본으로 사용합니다."
        )
        if mcp_scope == "selected_institution":
            st.info(
                f"선택 기관 '{selected_profile_id}'의 승인·색인된 규정을 하나의 MCP runtime bundle로 묶습니다. "
                "기관 범위 밖 문서는 포함하지 않습니다."
            )
        elif mcp_scope == "selected_documents":
            st.info(f"앞 단계에서 선택한 규정 {len(scope_documents):,}개만 하나의 MCP로 묶습니다.")
        mcp_mode = (
            "tunnel"
            if mcp_connection_target == "chatgpt-tunnel"
            else "http"
            if mcp_connection_target in {"chatgpt-remote", "claude-api"}
            else "local"
        )
        mcp_mode_labels = {
            "http": "HTTPS URL",
            "tunnel": "보안 Tunnel",
            "local": "로컬 stdio",
        }
        st.caption(f"선택된 연결 방식: {mcp_mode_labels[mcp_mode]}")
        if mcp_mode == "http":
            st.info(
                "MCP HTTP는 아래에서 접속 URL을 자동으로 만든 뒤 연결 설정에 포함합니다. 외부 AI에서 연결하려면 "
                "기관 서버에 배포하고 접근 가능한 HTTPS /mcp 주소를 사용해야 합니다. GitHub에는 소스와 배포 산출물을 올릴 수 있지만, "
                "실제 답변에는 승인된 색인 데이터가 배포 서버에도 있어야 합니다."
            )
            mcp_profile_options = ["bundle", "chatgpt-remote", "claude-api", "claude-code"]
            mcp_transport = "streamable-http"
        elif mcp_mode == "tunnel":
            st.info(
                "보안 Tunnel은 공개 HTTPS 주소나 인바운드 방화벽 개방 없이 ChatGPT에 연결합니다. "
                "생성 후 OpenAI Tunnel ID와 API 키를 설정하고 전용 연결 준비 버튼을 실행한 다음, "
                "아래 ChatGPT 등록·최종 도구 호출 확인까지 완료하세요."
            )
            mcp_profile_options = ["bundle", "chatgpt-remote"]
            mcp_transport = "stdio"
        else:
            st.info(
                "MCP 로컬은 이 PC에서 stdio로 실행됩니다. ChatGPT Desktop은 Settings > MCP servers에서 생성된 값을 등록하고, "
                "Codex CLI·Claude Code는 압축을 푼 번들을 로컬 작업공간으로 열어 대상별 연결 요청문을 실행합니다. "
                "ChatGPT Desktop 내장 등록이 어렵거나 로컬 에이전트 실행 권한이 없을 때만 대상별 BAT를 사용합니다. "
                "Claude Desktop은 전용 BAT가 기본입니다. 등록과 현재 대화의 도구 노출은 서로 다른 상태입니다."
            )
            mcp_profile_options = ["bundle", "chatgpt-desktop-local", "claude-desktop", "claude-code"]
            mcp_transport = "stdio"
        st.caption(f"Selected MCP transport: {mcp_transport}")
        mcp_profile = "bundle"
        if mcp_connection_target in {"claude-desktop", "claude-code", "claude-api"}:
            mcp_profile = mcp_connection_target
        elif mcp_connection_target == "chatgpt-desktop-local":
            mcp_profile = "chatgpt-desktop-local"
        elif mcp_connection_target in {"chatgpt-remote", "chatgpt-tunnel"}:
            mcp_profile = "chatgpt-remote"
        if mcp_profile not in mcp_profile_options:
            mcp_profile = "bundle"
        mcp_host = "127.0.0.1"
        mcp_port = 8000
        mcp_public_url_input = ""
        mcp_target_ready = True
        if mcp_mode == "http":
            st.markdown("#### 1. MCP HTTP 접속 URL 만들기")
            mcp_http_cols = st.columns(2)
            with mcp_http_cols[0]:
                mcp_host = st.text_input("HTTP host", value="127.0.0.1", key=f"mcp-host-{document_id}")
            with mcp_http_cols[1]:
                mcp_port = st.number_input(
                    "HTTP port",
                    min_value=1,
                    max_value=65535,
                    value=8000,
                    key=f"mcp-port-{document_id}",
                )
            mcp_public_url_input = st.text_input(
                "연결할 공개 HTTPS 주소 (필수)",
                value="",
                placeholder="https://mcp.example.go.kr 또는 https://mcp.example.go.kr/mcp",
                key=f"mcp-public-url-{document_id}",
            )
            mcp_http_url = _build_mcp_http_url(
                host=mcp_host,
                port=int(mcp_port),
                public_url=mcp_public_url_input,
            )
            st.markdown("**생성된 MCP HTTP URL**")
            st.code(mcp_http_url, language=None)
            if mcp_http_url.startswith("https://"):
                st.success("공개 HTTPS MCP URL이 연결 설정과 생성 파일에 포함됩니다.")
            else:
                mcp_target_ready = False
                st.warning(
                    "HTTPS 연결에는 외부에서 접근 가능한 https:// 주소가 필요합니다. 공개 주소가 없다면 "
                    "ChatGPT (보안 Tunnel MCP)를 선택하세요."
                )
        elif mcp_mode == "tunnel":
            mcp_http_url = ""
            st.markdown("#### 보안 Tunnel 연결 만들기")
            st.caption(
                "공개 URL 입력은 필요 없습니다. ChatGPT용 검색·근거 조회 도구와 Tunnel 실행 설정을 자동으로 만듭니다."
            )
        else:
            mcp_http_url = ""
            st.markdown("#### 2. MCP 로컬 연결")
            st.caption(
                "생성되는 stdio 실행 파일로 같은 PC의 ChatGPT Desktop 로컬 direct MCP, "
                "Codex CLI, Claude Desktop, Claude Code에 연결합니다. HTTPS 주소는 필요하지 않습니다."
            )

        with st.expander("고급 설정: 연결 프로그램 선택", expanded=False):
            mcp_profile = st.selectbox(
                "연결할 프로그램 (Client profile)",
                mcp_profile_options,
                index=mcp_profile_options.index(mcp_profile),
                key=f"mcp-client-profile-{document_id}-{mcp_mode}",
            )
        mcp_public_url = mcp_http_url if mcp_mode == "http" else ""
        mcp_bundle_dir_key = f"mcp-bundle-dir-{document_id}"
        if mcp_bundle_dir_key not in st.session_state:
            st.session_state[mcp_bundle_dir_key] = "reports/mcp_connection_bundle"
        st.button(
            "Windows 탐색기에서 저장 폴더 선택",
            key=f"select-mcp-bundle-dir-{document_id}",
            on_click=_select_windows_output_directory,
            args=(mcp_bundle_dir_key, st.session_state[mcp_bundle_dir_key]),
        )
        picker_error = st.session_state.get(f"{mcp_bundle_dir_key}:picker_error")
        if picker_error:
            st.error(picker_error)
        if not str(st.session_state.get(mcp_bundle_dir_key) or "").strip():
            st.session_state[mcp_bundle_dir_key] = "reports/mcp_connection_bundle"
        mcp_bundle_dir = st.text_input(
            "MCP 파일 묶음을 만들 폴더",
            key=mcp_bundle_dir_key,
        )
        mcp_bundle_output_dir = _resolve_operator_output_path(mcp_bundle_dir)
        mcp_save_mode = st.radio(
            "저장 방식",
            ["folder-and-zip", "folder-only"],
            format_func=lambda value: {
                "folder-and-zip": "폴더 + 전달용 ZIP (권장)",
                "folder-only": "이 PC에 폴더만 저장",
            }[value],
            key=f"mcp-save-mode-{document_id}",
            horizontal=True,
            help="AI 앱 연결은 생성된 폴더를 사용합니다. ZIP은 다른 PC나 담당자에게 전달할 때만 필요합니다.",
        )
        if st.button("Windows 탐색기에서 현재 저장 폴더 열기", key=f"open-mcp-bundle-dir-{document_id}"):
            try:
                _open_directory_in_explorer(mcp_bundle_output_dir)
                st.success(f"MCP 저장 폴더를 열었습니다: {mcp_bundle_output_dir}")
            except OSError as exc:
                st.error(str(exc))
        mcp_runtime_data_dir = mcp_bundle_output_dir / "data"
        mcp_bundle_zip = _mcp_bundle_zip_output_path(mcp_bundle_output_dir)
        if mcp_save_mode == "folder-and-zip":
            st.caption(f"최종 ZIP 저장 위치: {mcp_bundle_zip}")
        else:
            st.caption(f"최종 폴더 저장 위치: {mcp_bundle_output_dir}")
        suggested_mcp_server_name = _default_mcp_server_name(mcp_bundle_output_dir, selected_profile_id)
        mcp_server_name_key = f"mcp-server-name-{document_id}"
        mcp_server_name = st.text_input(
            "생성할 MCP 이름 (필수 입력)",
            key=mcp_server_name_key,
            placeholder=f"예: {suggested_mcp_server_name}",
            help="사용자가 입력한 이름만 AI 앱에 등록됩니다. 예시는 자동으로 적용되지 않습니다.",
        ).strip()
        normalized_mcp_server_name = _normalize_mcp_server_name(mcp_server_name)
        if not mcp_server_name:
            mcp_target_ready = False
            st.info("MCP 이름을 직접 입력해야 파일 묶음과 연결 설정을 생성할 수 있습니다.")
        elif not normalized_mcp_server_name or normalized_mcp_server_name != mcp_server_name:
            mcp_target_ready = False
            st.error("MCP 이름에는 영문 소문자, 숫자, 하이픈, 밑줄, 점만 사용할 수 있습니다.")
        config_server_name = (
            mcp_server_name
            if normalized_mcp_server_name and normalized_mcp_server_name == mcp_server_name
            else suggested_mcp_server_name
        )
        mcp_config = _direct_python_mcp_config(
            build_mcp_client_config(
                server_name=config_server_name,
                data_dir=str(mcp_runtime_data_dir),
                tenant_id=document_tenant_id,
                profile_id=selected_profile_id,
                tenant_storage_isolation=False,
                transport=mcp_transport,
                host=mcp_host,
                port=int(mcp_port),
                client_profile=mcp_profile,
                public_url=mcp_public_url.strip() or None,
            ),
            tenant_storage_isolation=False,
        )
        mcp_payload = json.dumps(mcp_config, ensure_ascii=False, indent=2)
        mcp_quickstart = mcp_config.get("quickstart") if isinstance(mcp_config, dict) else None
        bundle_args = [
            "--client-profile",
            "bundle",
            "--server-name",
            mcp_server_name,
            "--data-dir",
            str(mcp_runtime_data_dir),
            "--tenant-id",
            document_tenant_id,
            "--profile-id",
            selected_profile_id,
            "--transport",
            mcp_transport,
            "--host",
            mcp_host,
            "--port",
            str(int(mcp_port)),
            "--out-dir",
            str(mcp_bundle_output_dir),
        ]
        if mcp_export_document_id:
            bundle_args.extend(["--document-id", mcp_export_document_id])
        for selected_document_id in mcp_export_document_ids or []:
            bundle_args.extend(["--document-id", selected_document_id])
        if mcp_public_url:
            bundle_args.extend(["--public-url", mcp_public_url])
        if mcp_save_mode == "folder-and-zip":
            bundle_args.extend(["--zip-out", str(mcp_bundle_zip)])
        visibility_precheck_args = [
            "--data-dir",
            str(settings.data_dir),
            "--tenant-id",
            document_tenant_id,
            "--profile-id",
            selected_profile_id,
            "--forbid-smoke-docs",
            "--require-indexed",
            "--fail-on-issue",
        ]
        if settings.tenant_storage_isolation:
            visibility_precheck_args.append("--tenant-storage-isolation")
        connect_script_path = mcp_bundle_output_dir / "connect_mcp_client.ps1"
        mcp_target_file_keys = {
            "codex": "codex_agent_prompt",
            "claude-desktop": "connect_claude_desktop_bat",
            "claude-code": "claude_code_agent_prompt",
            "chatgpt-desktop-local": "chatgpt_desktop_agent_prompt",
            "chatgpt-remote": "connect_chatgpt_https_bat",
            "chatgpt-tunnel": "connect_chatgpt_tunnel_bat",
            "claude-api": "connect_claude_https_bat",
        }
        mcp_target_file_key = mcp_target_file_keys.get(mcp_connection_target, "connect")
        st.markdown("#### 최종 산출물 생성")
        st.caption("일반 사용자는 아래 버튼만 누르면 됩니다. JSON과 명령어는 아래 전산 담당자용 영역에 숨겨져 있습니다.")
        if st.button(
            "MCP로 쓸 파일 묶음 만들기",
            key=f"write-mcp-bundle-{document_id}",
            type="primary",
            disabled=not mcp_bundle_ready or mcp_profile_scope_mismatch or not mcp_target_ready,
        ):
            try:
                _ensure_mcp_output_directory_writable(mcp_bundle_output_dir)
                bundle_progress = st.progress(0, text="MCP 묶음 생성 준비 0%")
                bundle_status = st.status("MCP 파일 묶음 생성 중…", expanded=True)

                def _bundle_stage(percent: int, message: str) -> None:
                    bundle_progress.progress(percent, text=f"{message} · {percent}%")
                    bundle_status.write(f"{percent}% · {message}")
                    time.sleep(0.12)

                _bundle_stage(10, "승인 데이터와 출처 정보 확인")
                bundle_detail = st.empty()
                source_metadata_patch = {}
                if missing_mcp_source_metadata:
                    _bundle_stage(18, "누락된 로컬 출처 정보 자동 보완")
                    documents_to_patch = [
                        item for item in scope_documents if _missing_mcp_source_metadata(item)
                    ]
                    for patch_index, scope_document in enumerate(documents_to_patch, start=1):
                        scope_document_id = str(getattr(scope_document, "document_id", "") or "")
                        updated_document, current_patch = _ensure_mcp_source_metadata(
                            scope_document,
                            tenant_id=document_tenant_id,
                            target_repository=repository,
                        )
                        if not current_patch:
                            continue
                        source_metadata_patch[scope_document_id] = current_patch
                        if scope_document_id == document_id:
                            document = updated_document
                        patch_start = 20 + int(((patch_index - 1) / max(len(documents_to_patch), 1)) * 10)
                        patch_end = 20 + int((patch_index / max(len(documents_to_patch), 1)) * 10)
                        _run_background_operation_with_progress(
                            lambda _report, target_document_id=scope_document_id: index_document(
                                target_document_id,
                                IndexRequest(target_type="local-jsonl", embedding_dimensions=384),
                                local_auth,
                            ),
                            progress_bar=bundle_progress,
                            detail_box=bundle_detail,
                            start_percent=patch_start,
                            end_percent=patch_end,
                            label=f"출처 정보 재색인 {patch_index}/{len(documents_to_patch)}",
                            estimated_seconds=15.0,
                        )
                _bundle_stage(32, "기관별 규정·개정판·목차 고속 색인 생성")
                runtime_data = _run_background_operation_with_progress(
                    lambda report: write_mcp_runtime_data_bundle(
                        source_data_dir=settings.data_dir,
                        out_dir=mcp_bundle_output_dir,
                        tenant_id=document_tenant_id,
                        profile_id=selected_profile_id,
                        document_id=mcp_export_document_id,
                        document_ids=mcp_export_document_ids,
                        scope="document" if mcp_scope == "current_document" else mcp_scope,
                        tenant_storage_isolation=settings.tenant_storage_isolation,
                        progress_callback=report,
                    ),
                    progress_bar=bundle_progress,
                    detail_box=bundle_detail,
                    start_percent=32,
                    end_percent=74,
                    label="기관 전체 규정 계층 색인",
                    estimated_seconds=90.0 if mcp_scope != "current_document" else 20.0,
                )
                runtime_fingerprint = str(runtime_data.get("logical_corpus_sha256") or "")
                if runtime_fingerprint:
                    st.caption(f"재생성 확인값: {runtime_fingerprint[:20]}")
                st.caption(
                    f"기관 규정 {runtime_data.get('regulation_count', 0)}개 · "
                    f"개정판 {runtime_data.get('regulation_version_count', 0)}개 · "
                    f"목차 노드 {runtime_data.get('toc_node_count', 0)}개를 색인했습니다."
                )
                _bundle_stage(77, "MCP 연결 설정 JSON 생성")
                bundle_config = _direct_python_mcp_config(
                    build_mcp_client_config(
                        server_name=mcp_server_name,
                        data_dir=str(mcp_runtime_data_dir),
                        tenant_id=document_tenant_id,
                        profile_id=selected_profile_id,
                        tenant_storage_isolation=False,
                        transport=mcp_transport,
                        host=mcp_host,
                        port=int(mcp_port),
                        client_profile="bundle",
                        public_url=mcp_public_url.strip() or None,
                    ),
                    tenant_storage_isolation=False,
                )
                _bundle_stage(83, "클라이언트별 연결 파일 생성")
                files = write_mcp_setup_bundle(
                    bundle_config,
                    mcp_bundle_output_dir,
                    server_name=mcp_server_name,
                    preferred_python=sys.executable,
                    preferred_project_root=PROJECT_ROOT,
                )
                selected_target_file = files.get(mcp_target_file_key)
                local_server = (bundle_config.get("quickstart") or {}).get("run_local_stdio_server") or {}
                _write_direct_python_quickstart_scripts(
                    files,
                    server_name=mcp_server_name,
                    claude_code_config=bundle_config.get("claude_code") or {},
                    stdio_command=str(local_server.get("command") or sys.executable or "python"),
                    stdio_args=[str(arg) for arg in (local_server.get("args") or [])],
                )
                zip_path = None
                zip_fallback_used = False
                if mcp_save_mode == "folder-and-zip":
                    _bundle_stage(88, "최종 ZIP 파일 압축")

                    def _zip_progress(current_bytes: int, total_bytes: int, current_name: str) -> None:
                        fraction = current_bytes / max(total_bytes, 1)
                        percent = 88 + min(11, int(fraction * 11))
                        bundle_progress.progress(
                            percent,
                            text=(
                                f"ZIP 압축 {current_bytes / (1024 * 1024):,.1f}/"
                                f"{total_bytes / (1024 * 1024):,.1f}MB · {percent}%"
                            ),
                        )
                        bundle_detail.caption(f"압축 중 · {current_name}")

                    zip_path, zip_fallback_used = _write_operator_mcp_bundle_zip(
                        mcp_bundle_output_dir,
                        mcp_bundle_zip,
                        progress_callback=_zip_progress,
                    )
                else:
                    _bundle_stage(88, "최종 폴더 저장 확인")
                _bundle_stage(100, "MCP 파일 묶음 생성 완료")
                bundle_status.update(label="MCP 파일 묶음 생성 완료", state="complete")
                if zip_fallback_used:
                    st.warning(
                        f"기존 ZIP 파일이 사용 중이어서 새 이름으로 저장했습니다: {Path(str(zip_path)).name}"
                    )
                st.session_state[_mcp_bundle_state_key(document_id, mcp_scope)] = {
                    "written": True,
                    "document_id": document_id,
                    "scope": mcp_scope,
                    "export_document_id": mcp_export_document_id,
                    "export_document_ids": mcp_export_document_ids or [],
                    "profile_id": selected_profile_id,
                    "server_name": mcp_server_name,
                    "tenant_id": document_tenant_id,
                    "bundle_dir": str(mcp_bundle_output_dir),
                    "zip": str(zip_path) if zip_path else "",
                    "save_mode": mcp_save_mode,
                    "runtime_data_dir": str(mcp_runtime_data_dir),
                    "runtime_record_count": runtime_data.get("record_count"),
                    "runtime_regulation_count": runtime_data.get("regulation_count"),
                    "runtime_regulation_version_count": runtime_data.get("regulation_version_count"),
                    "runtime_toc_node_count": runtime_data.get("toc_node_count"),
                    "logical_corpus_sha256": runtime_data.get("logical_corpus_sha256"),
                    "hierarchical_index_status": runtime_data.get("hierarchical_index_status"),
                    "runtime_manifest": runtime_data.get("files", {}).get("runtime_manifest"),
                    "source_metadata_patch": source_metadata_patch,
                    "connection_target": mcp_connection_target,
                    "connection_target_label": mcp_connection_target_labels.get(mcp_connection_target),
                    "connection_target_file": selected_target_file,
                    "connect_wizard": files.get("connect"),
                    "install_script": files.get("install"),
                    "usage_guide": files.get("usage_guide"),
                    "usage_guide_bat": files.get("usage_guide_bat"),
                    "chatgpt_desktop_agent_prompt": files.get("chatgpt_desktop_agent_prompt"),
                    "codex_agent_prompt": files.get("codex_agent_prompt"),
                    "claude_code_agent_prompt": files.get("claude_code_agent_prompt"),
                    "agent_connect_prompt": (
                        selected_target_file
                        if mcp_target_file_key.endswith("_agent_prompt")
                        else None
                    ),
                    "codex_plugin_guide": files.get("codex_plugin_guide"),
                }
                if source_metadata_patch:
                    st.info(
                        f"누락된 로컬 출처 정보를 규정 {len(source_metadata_patch):,}개에 보완한 뒤 다시 색인했습니다."
                    )
                st.success("MCP 실행 데이터와 연결 파일 묶음을 만들었습니다.")
                if selected_target_file:
                    if mcp_connection_target == "chatgpt-desktop-local":
                        st.success(
                            "ChatGPT Desktop 내장 MCP 서버 등록 안내를 만들었습니다. "
                            "아래 Name·STDIO·Command·Arguments를 Settings > MCP servers > Add server에 입력하세요: "
                            f"{Path(str(selected_target_file)).name}"
                        )
                    elif mcp_target_file_key.endswith("_agent_prompt"):
                        st.success(
                            f"{mcp_connection_target_labels.get(mcp_connection_target)} 에이전트 연결 요청문을 만들었습니다. "
                            f"아래 내용을 복사해 해당 에이전트에 붙여넣으세요: {Path(str(selected_target_file)).name}"
                        )
                    elif mcp_connection_target in {
                        "codex",
                        "chatgpt-desktop-local",
                        "claude-desktop",
                        "claude-code",
                        "chatgpt-remote",
                        "chatgpt-tunnel",
                        "claude-api",
                    }:
                        st.success(
                            f"{mcp_connection_target_labels.get(mcp_connection_target)} 연결 준비 버튼을 만들었습니다. "
                            "파일을 실행한 뒤 아래 대상별 등록·재시작·최종 확인 절차까지 완료하세요: "
                            f"{Path(str(selected_target_file)).name}"
                        )
                    else:
                        st.success(
                            f"{mcp_connection_target_labels.get(mcp_connection_target)} 연결 설정 파일을 만들었습니다: "
                            f"{Path(str(selected_target_file)).name}"
                        )
                generated_file_lines = [
                    "생성된 파일:",
                    f"- 폴더: `{mcp_bundle_output_dir.name}`",
                    f"- MCP 데이터: `{mcp_runtime_data_dir.name}`",
                    f"- 포함된 승인 기록: `{runtime_data.get('record_count', 0):,}`개",
                ]
                if zip_path:
                    generated_file_lines.append(f"- 압축 파일: `{Path(str(zip_path)).name}`")
                generated_file_lines.extend(
                    [
                        f"- 연결 마법사: `{Path(str(files.get('connect'))).name}`",
                        f"- 설치 확인 스크립트: `{Path(str(files.get('install'))).name}`",
                        f"- 설치 후 사용 안내: `{Path(str(files.get('usage_guide_bat'))).name}`",
                        f"- ChatGPT Desktop 내장 MCP 등록 안내: `{Path(str(files.get('chatgpt_desktop_agent_prompt'))).name}`",
                        f"- Codex 에이전트 연결 요청문: `{Path(str(files.get('codex_agent_prompt'))).name}`",
                        f"- Claude Code 에이전트 연결 요청문: `{Path(str(files.get('claude_code_agent_prompt'))).name}`",
                        f"- Codex CLI 호환 수동 입력값: `{Path(str(files.get('codex_plugin_guide'))).name}`",
                        f"- 한국어 안내문: `{Path(str(files.get('readme_ko'))).name}`",
                    ]
                )
                st.markdown("\n".join(generated_file_lines))
            except Exception as exc:
                if "bundle_status" in locals():
                    bundle_status.update(label="MCP 파일 묶음 생성 실패", state="error")
                st.error(str(exc))
        bundle_state = st.session_state.get(_mcp_bundle_state_key(document_id, mcp_scope))
        if isinstance(bundle_state, dict) and bundle_state.get("connection_target_file"):
            st.success(
                f"선택한 AI 앱: {bundle_state.get('connection_target_label')}. "
                f"사용자용 연결 파일: {Path(str(bundle_state.get('connection_target_file'))).name}"
            )
        if isinstance(bundle_state, dict) and bundle_state.get("written"):
            recent_output = Path(str(bundle_state.get("bundle_dir") or ".")).name
            if bundle_state.get("zip"):
                recent_output += f" / {Path(str(bundle_state.get('zip'))).name}"
            st.info(f"최근 생성한 MCP 파일 묶음: {recent_output}")
            installed_server_name = str(bundle_state.get("server_name") or mcp_server_name)
            installed_target = str(bundle_state.get("connection_target") or "")
            agent_prompt_paths = []
            if bundle_state.get("agent_connect_prompt"):
                agent_prompt_paths = [bundle_state.get("agent_connect_prompt")]
            for agent_prompt_path in [path for path in agent_prompt_paths if path]:
                prompt_path = Path(str(agent_prompt_path))
                prompt_label = prompt_path.stem.replace("_", " ")
                is_chatgpt_desktop_guide = prompt_path.name == "CHATGPT_DESKTOP_CONNECT_GUIDE.md"
                st.markdown(f"#### {prompt_label}")
                if is_chatgpt_desktop_guide:
                    st.caption(
                        "ChatGPT Desktop의 Settings > MCP servers > Add server에 아래 실제 값을 입력하세요. "
                        "프로그램이 현재 번들의 폴더 이름·절대경로·핵심 파일 구조와 전체 Arguments를 자동으로 넣었습니다. "
                        "저장 후 Desktop을 Restart하고 새 대화에서 `/mcp`와 실제 도구 호출을 확인합니다."
                    )
                else:
                    st.caption(
                        "프로그램이 현재 번들의 폴더 이름·정확한 절대경로·핵심 파일 구조를 아래 요청문에 자동으로 넣습니다. "
                        "가능하면 그 폴더를 해당 AI의 로컬 프로젝트/작업공간으로 열고, "
                        "코드 상자 오른쪽 위의 복사 아이콘으로 요청문 전체를 복사해 에이전트에 붙여넣고, "
                        "경로 접근 승인이 필요하면 표시된 그 폴더만 작업공간으로 열거나 추가한 뒤 "
                        "설치 검증이 끝날 때까지 실행하세요. "
                        "그 후 해당 앱을 완전히 종료·재실행하고 새 대화 또는 task에서 `/mcp`로 확인합니다. "
                        "일반 채팅처럼 로컬 파일·터미널 권한이 없는 화면에서는 실행되지 않습니다."
                    )
                try:
                    agent_prompt_text = Path(str(agent_prompt_path)).read_text(encoding="utf-8")
                except OSError as exc:
                    st.warning(f"연결 요청문을 읽을 수 없습니다: {exc}")
                else:
                    agent_prompt_text = render_agent_connect_prompt_for_program(
                        agent_prompt_text,
                        bundle_dir=Path(str(agent_prompt_path)).parent,
                    )
                    st.code(agent_prompt_text, language=None)
            if installed_target in {
                "chatgpt-desktop-local",
                "codex",
                "claude-code",
                "claude-desktop",
            }:
                diagnostic_title = {
                    "chatgpt-desktop-local": "ChatGPT Desktop 연결 진단",
                    "codex": "Codex CLI 연결 진단",
                    "claude-code": "Claude Code 연결 진단",
                    "claude-desktop": "Claude Desktop 연결 진단",
                }[installed_target]
                st.markdown(f"#### {diagnostic_title}")
                st.caption(
                    "이 표는 화면이 다시 실행될 때마다 번들의 bundle_status.json을 새로 읽습니다. "
                    "설정·서버 검증과 실제 Desktop 연결 완료를 별도 상태로 표시하며, "
                    "이전 실행에서 남은 성공 값만으로 연결 완료라고 표시하지 않습니다. "
                    "이 프로그램은 다른 앱의 현재 대화 결과를 자동으로 읽을 수 없으므로, "
                    "아래 최종 도구 호출 성공은 해당 대화에서 직접 확인해야 합니다."
                )
                diagnostic_refreshed = st.button(
                    "MCP 연결 상태 새로고침",
                    key=f"refresh-mcp-connection-diagnostic-{document_id}-{mcp_scope}",
                )
                refresh_succeeded = False
                refresh_message = ""
                if diagnostic_refreshed and installed_target in {
                    "chatgpt-desktop-local",
                    "claude-desktop",
                }:
                    refresh_succeeded, refresh_message = _refresh_mcp_connection_observation(
                        str(bundle_state.get("bundle_dir") or ""),
                        installed_target,
                        installed_server_name,
                    )
                connection_diagnostic, diagnostic_read_error = _read_mcp_connection_diagnostic(
                    str(bundle_state.get("bundle_dir") or ""),
                    installed_target,
                )
                if diagnostic_refreshed:
                    if installed_target in {"codex", "claude-code"}:
                        client_label = (
                            "Claude Code" if installed_target == "claude-code" else "Codex CLI"
                        )
                        st.caption(
                            f"bundle_status.json을 다시 읽어 {client_label} 진단을 갱신했습니다."
                        )
                    elif refresh_succeeded and refresh_message == "observation_ready":
                        st.caption(
                            "앱 프로세스·재시작 이후 로그를 읽기 전용으로 다시 관찰했습니다. "
                            "이 결과만으로 현재 대화의 도구 연결 완료를 주장하지 않습니다."
                        )
                    elif refresh_succeeded:
                        st.caption(
                            "현재 관찰 결과를 기록했습니다. 앱 재시작 또는 제품 화면 확인이 아직 필요합니다."
                        )
                    else:
                        st.warning(
                            f"연결 관찰을 갱신하지 못했습니다: {refresh_message or 'refresh_failed'}"
                        )

                diagnostic_state = str(connection_diagnostic.get("overall_state") or "pending")
                if diagnostic_state == "connected":
                    st.success(
                        "Desktop 연결 완료 — 현재 시도의 Desktop 도구 노출과 대화 도구 호출 증명까지 확인했습니다."
                    )
                elif diagnostic_state == "configured":
                    st.info(
                        "MCP 구성 확인 완료 · Desktop 연결 확인 대기 — 서버 실행 준비는 확인됐지만 "
                        "Desktop 도구 노출과 현재 대화 호출은 아직 별도 확인이 필요합니다."
                    )
                else:
                    st.warning(
                        "MCP 연결 진단 대기 — 현재 시도에서 설정·실행 검증이 아직 모두 끝나지 않았습니다."
                    )
                if diagnostic_read_error == "bundle_status_unavailable":
                    st.warning("연결 상태 파일을 아직 읽을 수 없습니다. 파일 묶음을 다시 확인하세요.")
                elif diagnostic_read_error == "bundle_status_invalid":
                    st.warning("연결 상태 파일 형식이 올바르지 않아 보수적으로 미확인 처리했습니다.")

                st.dataframe(
                    pd.DataFrame(_mcp_connection_diagnostic_rows(connection_diagnostic)),
                    hide_index=True,
                    use_container_width=True,
                )
                st.caption("지원 요청에 아래 코드 블록을 복사하세요. 로컬 경로와 비밀값은 포함하지 않습니다.")
                st.code(
                    "\n".join(
                        [
                            f"support_summary: {connection_diagnostic.get('support_summary') or 'Connection evidence is incomplete.'}",
                            f"next_action: {connection_diagnostic.get('next_action') or 'Run the connection diagnostic again.'}",
                        ]
                    ),
                    language=None,
                )
                st.markdown("#### 재시작 후 최종 확인 프롬프트")
                restart_target_label = (
                    "ChatGPT Desktop"
                    if installed_target == "chatgpt-desktop-local"
                    else "Claude Desktop"
                    if installed_target == "claude-desktop"
                    else "Claude Code"
                    if installed_target == "claude-code"
                    else "Codex CLI"
                )
                if installed_target == "claude-desktop":
                    st.caption(
                        f"{restart_target_label}를 완전히 종료·재실행한 뒤 새 대화의 Connectors에서 "
                        f"`{installed_server_name}`을 확인하고, 아래 문장을 그대로 복사해 실행하세요."
                    )
                elif installed_target == "claude-code":
                    st.caption(
                        f"새 Claude Code task를 연 뒤 `claude mcp get {installed_server_name}`으로 "
                        "user scope와 현재 번들 경로를 확인하고, 아래 문장을 그대로 실행하세요."
                    )
                else:
                    st.caption(
                        f"{restart_target_label}를 완전히 종료·재실행한 뒤 새 대화나 task에서 먼저 `/mcp`로 "
                        f"`{installed_server_name}`을 확인하고, 아래 문장을 그대로 복사해 실행하세요."
                    )
                st.code(
                    f"{installed_server_name} MCP의 get_index_status를 실행하고 사용 가능한 규정 도구를 보여줘.",
                    language=None,
                )
            st.markdown(
                "#### Claude Desktop 기본 BAT 연결 방식"
                if installed_target == "claude-desktop"
                else "#### 원격 MCP 연결 준비 및 최종 확인"
                if installed_target in MCP_EXTERNAL_DATA_TARGETS
                else "#### BAT 보조 연결 방식"
            )
            st.markdown(f"**등록할 MCP 이름:** `{installed_server_name}`")
            if installed_target == "chatgpt-desktop-local":
                st.info(
                    "ChatGPT Desktop의 Settings > MCP servers > Add server가 기본 연결 방식입니다. "
                    "위 안내에 표시된 Name·STDIO·Command·Working directory·Arguments를 입력하고 Save한 뒤 Restart하세요. "
                    "메뉴가 없거나 수동 입력이 어려울 때만 ChatGPT Desktop 전용 BAT를 보조 수단으로 사용합니다. "
                    f"새 대화에서 먼저 `/mcp`로 {installed_server_name}을 확인하세요. `@이름` 반복 입력은 설치나 연결 확인을 대신하지 않습니다."
                )
            elif installed_target == "codex":
                st.info("Codex CLI를 다시 시작하고 새 task에서 `/mcp`로 등록 이름을 확인하세요.")
            elif installed_target == "claude-code":
                st.info("Claude Code를 다시 시작하고 대화에서 `/mcp` 또는 터미널의 `claude mcp list`로 확인하세요.")
            elif installed_target == "claude-desktop":
                st.info(
                    "Claude Desktop 전용 BAT는 설치된 사용자 설정으로 stdio 도구 호출까지 검증합니다. "
                    "그 성공은 Desktop 자체 인식 완료가 아니므로 앱을 완전히 종료·재실행한 뒤 "
                    "새 대화의 Connectors에서 서버를 확인하고 실제 get_index_status 호출을 요청하세요."
                )
            elif installed_target == "chatgpt-remote":
                st.info(
                    "ChatGPT 웹의 Settings > Apps > Advanced Settings에서 Developer mode를 켠 뒤 "
                    "Settings > Apps > Create에서 공개 HTTPS MCP 앱을 같은 이름으로 등록하고, "
                    "새 대화의 tools 메뉴에서 앱을 선택해 아래 실제 search/fetch 도구 호출로 확인하세요."
                )
            elif installed_target == "chatgpt-tunnel":
                st.info(
                    "ChatGPT 웹의 Settings > Security and login에서 Developer mode를 켠 뒤 "
                    "Settings > Plugins 또는 https://chatgpt.com/plugins 에서 MCP 앱을 같은 이름으로 등록하고, "
                    "새 대화에서 + > More를 열어 앱을 선택하고 아래 실제 search/fetch 도구 호출로 확인하세요."
                )
            elif installed_target == "claude-api":
                st.info(
                    "생성된 mcp_servers·tools·betas 값을 Claude Messages API 요청에 넣고, "
                    "아래 실제 search·fetch 도구 호출이 성공하는지 확인하세요."
                )
            st.caption("새 대화 또는 새 task에 아래 문장을 그대로 입력합니다.")
            for verification_prompt in _mcp_final_verification_prompts(
                installed_target,
                installed_server_name,
            ):
                st.code(verification_prompt, language=None)
            if installed_target == "claude-desktop":
                st.caption(
                    "같은 이름으로 다시 생성하고 Claude Desktop 전용 BAT를 실행하면 기존 연결 설정을 교체합니다. "
                    "현재 승인된 전체 청크와 추가·개정 청크가 같은 MCP에 반영됩니다."
                )
            elif installed_target == "chatgpt-desktop-local":
                st.caption(
                    "같은 이름으로 다시 생성하면 Settings > MCP servers의 기존 항목을 새 안내 값으로 갱신합니다. "
                    "현재 승인된 전체 청크와 추가·개정 청크가 같은 MCP에 반영됩니다."
                )
            elif installed_target in {"codex", "claude-code"}:
                st.caption(
                    "같은 이름으로 다시 생성하고 대상별 에이전트 프롬프트를 실행하면 기존 연결 설정을 교체하며, "
                    "Codex CLI·Claude Code에서 로컬 에이전트를 사용할 수 없을 때만 보조 BAT를 실행합니다. "
                    "Claude Desktop은 전용 BAT가 기본입니다. 현재 승인된 전체 청크와 추가·개정 청크가 같은 MCP에 반영됩니다."
                )
            elif installed_target == "chatgpt-remote":
                st.caption(
                    "같은 이름으로 번들을 다시 생성했다면 원격 서버를 다시 준비하고, "
                    "Settings > Apps의 custom app을 갱신하거나 다시 만든 뒤 새 대화의 tools 메뉴에서 선택해 확인하세요."
                )
            elif installed_target == "chatgpt-tunnel":
                st.caption(
                    "같은 이름으로 번들을 다시 생성했다면 원격 서버 또는 Tunnel을 다시 준비하고, "
                    "ChatGPT Plugins 설정에서 앱을 Refresh하거나 다시 생성한 뒤 새 대화의 + > More에서 선택해 확인하세요."
                )
            elif installed_target == "claude-api":
                st.caption(
                    "같은 이름으로 번들을 다시 생성했다면 배포 서버와 Messages API 요청의 MCP 설정을 "
                    "새 산출물로 갱신한 뒤 search·fetch 호출을 다시 확인하세요."
                )
            if bundle_state.get("usage_guide_bat"):
                st.caption(f"전체 사용 안내: {Path(str(bundle_state.get('usage_guide_bat'))).name}")

        with st.expander("전산 담당자용 JSON/명령어 보기", expanded=False):
            if isinstance(mcp_quickstart, dict):
                for warning in mcp_quickstart.get("warnings") or []:
                    st.warning(warning)
                for note in (mcp_quickstart.get("chatgpt") or {}).get("notes") or []:
                    st.info(note)
            elif isinstance(mcp_config, dict):
                for note in mcp_config.get("notes") or []:
                    st.info(note)
            st.caption("MCP visibility precheck before client registration")
            st.code(_powershell_command("reg-rag-mcp-index-visibility", visibility_precheck_args), language="powershell")
            st.caption("Generate a copy/paste setup bundle")
            st.code(_powershell_command("reg-rag-mcp-config", bundle_args), language="powershell")
            st.caption("Fastest ChatGPT/Claude connection wizard")
            st.code(
                f'powershell -ExecutionPolicy Bypass -File "{connect_script_path}"',
                language="powershell",
            )
            if isinstance(mcp_quickstart, dict):
                st.markdown("#### MCP Quickstart")
                copy_paste = mcp_quickstart.get("copy_paste") or {}
                quick_cols = st.columns(2)
                with quick_cols[0]:
                    st.markdown("**1. MCP HTTP**")
                    http_server = mcp_quickstart.get("run_http_server") or {}
                    if http_server:
                        st.caption("ChatGPT/Claude API HTTP MCP server")
                        st.code(
                            _powershell_command(
                                str(http_server.get("command") or ""),
                                http_server.get("args") or [],
                            ),
                            language="powershell",
                        )
                    chatgpt_data_server = mcp_quickstart.get("run_chatgpt_data_server") or {}
                    if chatgpt_data_server:
                        st.caption("ChatGPT data-only search/fetch server")
                        st.code(
                            _powershell_command(
                                str(chatgpt_data_server.get("command") or ""),
                                chatgpt_data_server.get("args") or [],
                            ),
                            language="powershell",
                        )
                    if copy_paste.get("openai_secure_tunnel_ps"):
                        st.caption("OpenAI Secure MCP Tunnel for ChatGPT")
                        st.code(copy_paste["openai_secure_tunnel_ps"], language="powershell")
                    if copy_paste.get("claude_code_http_ps"):
                        st.caption("Claude Code remote HTTP command")
                        st.code(copy_paste["claude_code_http_ps"], language="powershell")
                    chatgpt_info = mcp_quickstart.get("chatgpt") or {}
                    claude_api_info = mcp_quickstart.get("claude_api") or {}
                    if chatgpt_info or claude_api_info:
                        st.caption("Remote client values")
                        st.code(
                            json.dumps(
                                {
                                    "chatgpt_connector_url": chatgpt_info.get("connector_url"),
                                    "chatgpt_requires_https": chatgpt_info.get("requires_reachable_https"),
                                    "chatgpt_https_endpoint_ready": chatgpt_info.get("https_endpoint_ready"),
                                    "claude_api_mcp_server_url": claude_api_info.get("mcp_server_url"),
                                    "claude_api_copy_fields": claude_api_info.get("copy_fields"),
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                            language="json",
                        )
                with quick_cols[1]:
                    st.markdown("**2. MCP 로컬**")
                    local_server = mcp_quickstart.get("run_local_stdio_server") or {}
                    if local_server:
                        st.caption("Claude Desktop/Claude Code local stdio")
                        st.code(
                            _powershell_command(
                                str(local_server.get("command") or ""),
                                local_server.get("args") or [],
                            ),
                            language="powershell",
                        )
                    claude_code = mcp_quickstart.get("claude_code") or {}
                    if claude_code:
                        st.caption("Claude Code add-json arguments")
                        st.code(json.dumps(claude_code.get("args") or [], ensure_ascii=False, indent=2), language="json")
                    if copy_paste.get("claude_code_stdio_ps"):
                        st.caption("Claude Code copy/paste command")
                        st.code(copy_paste["claude_code_stdio_ps"], language="powershell")
            st.download_button(
                "MCP 설정 JSON 다운로드",
                mcp_payload,
                file_name=f"{document_id}.mcp.{mcp_profile}.json",
                mime="application/json",
                disabled=not mcp_bundle_ready or bool(missing_mcp_source_metadata) or mcp_profile_scope_mismatch,
            )
            st.caption(
                "MCP server command is ready for connection."
                if mcp_connection_ready
                else "Draft MCP server command; approve and index chunks before connecting a client."
            )
            st.code(
                (
                    "reg-rag-mcp-server --data-dir "
                    f"{settings.data_dir} --tenant-id {document_tenant_id} "
                    + (
                        f"--profile-id {selected_profile_id} "
                        if getattr(document, "profile_id", None)
                        else ""
                    )
                    + f"--transport {mcp_transport}"
                    + (
                        f" --host {mcp_host} --port {int(mcp_port)} --http-bearer-token-env MCP_AUTH_TOKEN"
                        if mcp_transport == "streamable-http"
                        else ""
                    )
                ),
                language="powershell",
            )
            st.caption("Generated MCP config preview")
            st.code(mcp_payload, language="json")


# ---------------------------------------------------------------------------
# 페이지: 정확도 검수(골드셋)
# ---------------------------------------------------------------------------

def _render_parsing_goldset_review_panel() -> None:
    st.markdown("## 🔍 정확도 검수 (골드셋)")
    st.markdown(
        '<div class="rr-help"><b>이 작업은 무엇인가요?</b> 프로그램이 문서를 얼마나 정확하게 읽었는지 '
        "사람이 직접 채점하는 작업입니다. 원본 문서를 열어 조문·표·별표 개수를 세고, 아래에 입력한 뒤 저장하면 됩니다.<br><br>"
        "<b>순서:</b> ① 검수할 문서 선택 → ② 원본과 검수 안내문 열기 → ③ 항목별 개수 입력 → ④ 저장</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Parsing goldset review gate — 이 검수는 파서 정확도 측정용입니다. 운영 청크 승인이나 MCP 증빙 발행과는 별개입니다. "
        "(Goldset review measures parser accuracy. It does not approve operational chunks or publish MCP evidence.)"
    )

    with st.expander("검수 기록 파일 위치 (기본값을 그대로 쓰면 됩니다)", expanded=False):
        labels_path_text = st.text_input(
            "검수 기록 파일 (Goldset label CSV)",
            value="reports/parsing_manual_goldset_labels_20260710-current.csv",
            key="goldset-label-csv-path",
        )
    try:
        labels_path = _resolve_goldset_artifact_path(labels_path_text)
        rows = _load_goldset_label_rows(labels_path)
    except Exception as exc:
        st.warning(str(exc))
        return

    progress = _goldset_progress(rows)
    st.markdown("### 검수 진행 현황")
    expected = int(progress["expected_structure_rows"]) or 1
    st.progress(
        int(progress["completed_structure_rows"]) / expected,
        text=f"항목 입력 {progress['completed_structure_rows']} / {progress['expected_structure_rows']}건 완료",
    )
    metric_cols = st.columns(4)
    metric_cols[0].metric("검수 끝난 문서", f"{progress['ready_document_count']} / {progress['document_count']}")
    metric_cols[1].metric("직접 센 개수 미입력", f"{progress['missing_manual_count']}", help="아직 입력하지 않은 '직접 센 개수' 칸의 수입니다.")
    metric_cols[2].metric("일치 개수 미입력", f"{progress['missing_matched_count']}")
    metric_cols[3].metric("검수자 정보 누락", f"{progress['missing_reviewer_metadata_count']}", help="검수자 이름이나 검수 일시가 비어 있는 문서 수입니다.")
    if progress["ready_for_quality_claim"]:
        st.success("모든 문서의 검수가 끝났습니다. 정확도 점수를 계산할 수 있는 상태입니다.")
    else:
        st.warning("아직 검수가 끝나지 않았습니다. 모든 문서를 검수해야 정확도 점수를 말할 수 있습니다.")

    priority_rows = sorted(rows, key=_goldset_review_sort_key)
    with st.expander("검수 순서 추천 목록 (표가 많은 문서부터)", expanded=False):
        overview_rows = []
        for row in priority_rows[:12]:
            missing = _goldset_row_missing_fields(row)
            status_raw = str(row.get("label_status") or "")
            overview_rows.append(
                {
                    "문서 ID": row.get("document_id"),
                    "상태": GOLDSET_STATUS_LABELS.get(status_raw, status_raw),
                    "파일명": row.get("filename"),
                    "형식": row.get("extension"),
                    "남은 입력 수": len(missing),
                    "표(자동 추출)": optional_int(row.get("pipeline_table_count")) or 0,
                    "별표·서식(자동 추출)": optional_int(row.get("pipeline_appendix_form_count")) or 0,
                }
            )
        st.dataframe(pd.DataFrame(overview_rows), width="stretch", hide_index=True)

    st.markdown("### 1. 검수할 문서 선택")
    document_options = [str(row.get("document_id") or "") for row in priority_rows if row.get("document_id")]
    row_by_document_id = {str(row.get("document_id") or ""): row for row in rows}

    def _document_option_label(doc_id: str) -> str:
        row = row_by_document_id.get(doc_id, {})
        status_raw = str(row.get("label_status") or "")
        status = GOLDSET_STATUS_LABELS.get(status_raw, status_raw or "?")
        filename = str(row.get("filename") or doc_id)
        return f"{filename} — {status}"

    selected_document_id = st.selectbox(
        "검수할 문서",
        document_options,
        key="goldset-review-document",
        format_func=_document_option_label,
    )
    row_index = next(
        index for index, row in enumerate(rows) if str(row.get("document_id") or "") == selected_document_id
    )
    selected_row = dict(rows[row_index])
    source_path = _resolve_goldset_artifact_path(selected_row.get("source_path") or "")
    packet_path = _find_goldset_packet_path(selected_document_id)

    st.markdown("### 2. 원본 문서와 검수 안내문 열기")
    st.caption("두 파일을 나란히 띄워 놓고 비교하면서 개수를 세면 편합니다.")
    open_cols = st.columns(3)
    with open_cols[0]:
        if st.button("원본 문서 열기 (Open source file)", key=f"goldset-open-source-{selected_document_id}"):
            try:
                _open_local_artifact(source_path)
                st.success("원본 문서를 열었습니다.")
            except Exception as exc:
                st.error(str(exc))
        if not source_path.exists():
            st.warning("원본 파일을 찾을 수 없습니다.")
    with open_cols[1]:
        if packet_path:
            if st.button("검수 안내문 열기 (Open review packet)", key=f"goldset-open-packet-{selected_document_id}"):
                try:
                    _open_local_artifact(packet_path)
                    st.success("검수 안내문을 열었습니다.")
                except Exception as exc:
                    st.error(str(exc))
        else:
            st.caption("이 문서의 검수 안내문이 없습니다.")
    with open_cols[2]:
        if st.button("검수 기록 파일 열기 (Open label CSV)", key="goldset-open-label-csv"):
            try:
                _open_local_artifact(labels_path)
                st.success("검수 기록 파일을 열었습니다.")
            except Exception as exc:
                st.error(str(exc))
    with st.expander("파일 경로 보기 (전산 담당자용)", expanded=False):
        st.write(
            {
                "document_id": selected_document_id,
                "filename": selected_row.get("filename"),
                "source_exists": source_path.exists(),
                "packet_exists": bool(packet_path and packet_path.exists()),
            }
        )
        st.code(str(source_path), language="text")
        st.code(str(packet_path or ""), language="text")
        st.code(f"Invoke-Item -LiteralPath '{labels_path}'", language="powershell")
        st.code(
            "python scripts\\build_parsing_goldset_completion_board.py "
            "--labels-csv reports\\parsing_manual_goldset_labels_20260710-current.csv "
            "--packet-dir reports\\parsing_goldset_review_packets_current_20260710 "
            "--out-json reports\\parsing_goldset_completion_board_current_20260710.json "
            "--out-csv reports\\parsing_goldset_completion_board_current_20260710.csv "
            "--out-md reports\\parsing_goldset_completion_board_current_20260710.md "
            "--fail-on-incomplete",
            language="powershell",
        )

    st.markdown("### 3. 항목별 개수 입력")
    st.caption(
        "각 항목마다 세 칸이 있습니다 — [자동 추출]: 프로그램이 찾은 개수(수정 불가) / "
        "[직접 센 개수]: 원본에서 직접 센 개수 / [맞게 추출된 개수]: 둘을 비교해서 맞게 잡힌 개수."
    )
    current_status = str(selected_row.get("label_status") or "pending_human_review")
    status_options = ["pending_human_review", "reviewed", "human_reviewed", "approved", "completed"]
    if current_status not in status_options:
        status_options.insert(0, current_status)
    with st.form(f"goldset-review-form-{selected_document_id}"):
        status_col, reviewer_col, reviewed_at_col = st.columns(3)
        with status_col:
            label_status = st.selectbox(
                "검수 상태",
                status_options,
                index=status_options.index(current_status),
                format_func=lambda value: GOLDSET_STATUS_LABELS.get(value, value),
                help="개수 입력을 마쳤으면 '검수 완료'로 바꿔 주세요.",
            )
        with reviewer_col:
            reviewer = st.text_input("검수자 이름", value=str(selected_row.get("reviewer") or ""))
        with reviewed_at_col:
            reviewed_at = st.text_input(
                "검수 일시",
                value=str(selected_row.get("reviewed_at") or ""),
                placeholder="예: 2026-07-11",
            )

        updated_row = dict(selected_row)
        updated_row["label_status"] = label_status
        updated_row["reviewer"] = reviewer.strip()
        updated_row["reviewed_at"] = reviewed_at.strip()
        header_cols = st.columns([1.5, 0.9, 0.9, 0.9, 0.7, 0.7, 0.9, 0.9, 1.0])
        for col, label in zip(
            header_cols,
            ["항목", "자동", "직접", "일치", "FP", "FN", "정밀도", "재현율", "상태"],
            strict=False,
        ):
            col.markdown(f"**{label}**")
        for structure_type, spec in GOLDSET_SCORE_SPECS.items():
            structure_label = GOLDSET_STRUCTURE_LABELS.get(structure_type, structure_type)
            pipeline_value = optional_int(selected_row.get(spec["pipeline_field"]))
            manual_key = f"{selected_document_id}-{spec['manual_field']}"
            match_key = f"{selected_document_id}-{spec['match_field']}"
            metric_cols = st.columns([1.5, 0.9, 0.9, 0.9, 0.7, 0.7, 0.9, 0.9, 1.0])
            with metric_cols[0]:
                st.markdown(f"**{structure_label}**")
                detail_text = _goldset_detail_text(selected_row, structure_type)
                guidance = GOLDSET_STRUCTURE_GUIDANCE.get(structure_type, "")
                st.caption(f"{guidance} 자동 세부값: {detail_text}" if detail_text else guidance)
            with metric_cols[1]:
                st.text_input(
                    "자동",
                    value=str(selected_row.get(spec["pipeline_field"]) or ""),
                    disabled=True,
                    key=f"{selected_document_id}-{spec['pipeline_field']}",
                    label_visibility="collapsed",
                )
            with metric_cols[2]:
                updated_row[spec["manual_field"]] = st.text_input(
                    "직접",
                    value=str(selected_row.get(spec["manual_field"]) or ""),
                    key=manual_key,
                    label_visibility="collapsed",
                ).strip()
            with metric_cols[3]:
                updated_row[spec["match_field"]] = st.text_input(
                    "일치",
                    value=str(selected_row.get(spec["match_field"]) or ""),
                    key=match_key,
                    label_visibility="collapsed",
                ).strip()
            summary = _goldset_metric_summary(
                pipeline_value,
                optional_int(updated_row[spec["manual_field"]]),
                optional_int(updated_row[spec["match_field"]]),
            )
            metric_cols[4].markdown(summary["false_positive"])
            metric_cols[5].markdown(summary["false_negative"])
            metric_cols[6].markdown(summary["precision"])
            metric_cols[7].markdown(summary["recall"])
            status_value = summary["status"]
            if status_value == "일치":
                metric_cols[8].success(status_value)
            elif status_value == "차이 있음":
                metric_cols[8].warning(status_value)
            elif status_value == "확인 필요":
                metric_cols[8].error(status_value)
            else:
                metric_cols[8].caption(status_value)
        st.markdown("**메모 (선택 사항)**")
        updated_row["table_preservation_notes"] = st.text_area(
            "표 관련 메모",
            value=str(selected_row.get("table_preservation_notes") or ""),
            help="표가 깨졌거나 셀 내용이 빠진 경우 적어 주세요.",
        )
        updated_row["footnote_caption_connection_notes"] = st.text_area(
            "각주·캡션 관련 메모",
            value=str(selected_row.get("footnote_caption_connection_notes") or ""),
            help="각주나 표 제목이 본문과 끊긴 경우 적어 주세요.",
        )
        updated_row["parser_miss_false_positive_notes"] = st.text_area(
            "누락·오탐 메모",
            value=str(selected_row.get("parser_miss_false_positive_notes") or ""),
            help="프로그램이 놓쳤거나 잘못 찾은 부분을 적어 주세요.",
        )
        save_goldset_row = st.form_submit_button("검수 결과 저장 (Save goldset review row)", type="primary")

    if save_goldset_row:
        issues = _goldset_row_validation_issues(updated_row)
        if issues:
            for issue in issues:
                st.error(issue)
        else:
            rows[row_index].update(updated_row)
            try:
                backup_path = _write_goldset_label_rows(labels_path, rows)
                st.success(f"검수 결과를 저장했습니다. 이전 내용은 백업해 두었습니다: {backup_path.name}")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


# ---------------------------------------------------------------------------
# 페이지: 관리자 설정
# ---------------------------------------------------------------------------

def _page_admin() -> None:
    st.markdown("## ⚙️ 관리자 설정")
    st.markdown(
        '<div class="rr-help">AI 연결, 기관 프로필, 품질 기준을 관리하는 화면입니다. '
        "AI 검수에 쓸 <b>API 키·모델·주소는 여기 'AI 연결' 탭에서</b> 입력합니다. "
        "기관 프로필과 품질 기준은 처음 설정을 마친 뒤에는 평소에 열 필요가 없습니다.</div>",
        unsafe_allow_html=True,
    )
    if institution_registry_source or quality_profile_source:
        st.caption(
            f"기관 프로필 출처: {institution_registry_source or '기본값/세션'} | "
            f"품질 프로필 출처: {quality_profile_source or '기본값/세션'}"
        )

    connection_tab, profile_tab, quality_tab = st.tabs(["AI 연결", "기관 프로필", "품질 기준"])

    with connection_tab:
        _render_ai_connection_settings(settings)

    with profile_tab:
        st.markdown("### 기관 프로필 관리")
        st.caption("기관별 기본 메타데이터와 필수 입력 항목을 불러오거나 편집합니다.")
        current_registry = institution_registry
        if institution_registry_error:
            st.error(institution_registry_error)
        registry_upload = st.file_uploader(
            "institution_profiles.json",
            type=["json"],
            key="institution_profile_registry_upload",
        )
        if registry_upload:
            try:
                registry_upload.seek(0)
                current_registry = load_institution_profile_registry_from_bytes(registry_upload.read())
                st.session_state[REGISTRY_STATE_KEY] = institution_profile_registry_to_bytes(current_registry)
                st.session_state[REGISTRY_SOURCE_STATE_KEY] = registry_upload.name
            except (OSError, ValueError) as exc:
                st.error(str(exc))
                st.stop()

        if current_registry:
            registry_summary = current_registry.summary()
            st.caption(
                f"{st.session_state.get(REGISTRY_SOURCE_STATE_KEY, institution_registry_source) or 'registry'} | "
                f"profiles={registry_summary['profile_count']} | "
                f"sha256={registry_summary['sha256'][:12]}"
            )
            registry_path = _institution_profiles_storage_path(settings)
            if registry_path:
                if st.button("기관 프로필 저장"):
                    try:
                        save_result = save_institution_profile_registry(
                            registry_path,
                            current_registry,
                            backup_existing=True,
                        )
                    except (OSError, ValueError) as exc:
                        st.error(str(exc))
                        st.stop()
                    backup_note = f" backup={save_result['backup_path']}" if save_result["backup_path"] else ""
                    st.success(
                        f"기관 프로필 {save_result['profile_count']}개를 {save_result['path']}에 저장했습니다. "
                        f"sha256={save_result['sha256'][:12]}{backup_note}"
                    )
            elif registry_upload:
                st.warning("검증된 기관 프로필을 저장하려면 INSTITUTION_PROFILES_PATH를 설정하세요.")

        st.markdown("#### 기관 프로필 편집")
        editable_registry = current_registry or InstitutionProfileRegistry(profiles={})
        editable_profile_ids = sorted(editable_registry.profiles)
        editor_options = ["<새 프로필>"] + editable_profile_ids
        editor_choice = st.selectbox("편집할 기관 프로필", editor_options, index=0)
        selected_profile = None if editor_choice == "<새 프로필>" else editable_registry.profiles[editor_choice]
        with st.form("institution_profile_editor_form"):
            editor_profile_id = st.text_input(
                "프로필 ID",
                value=selected_profile.profile_id if selected_profile else "",
            )
            editor_display_name = st.text_input(
                "표시 이름",
                value=selected_profile.display_name if selected_profile else "",
            )
            editor_institution_name = st.text_input(
                "기관명",
                value=selected_profile.institution_name if selected_profile and selected_profile.institution_name else "",
            )
            editor_tenant_id = st.text_input(
                "연결 tenant ID (공유 API 환경에서 선택)",
                value=selected_profile.tenant_id if selected_profile and selected_profile.tenant_id else "",
                help="비워 두면 기존 로컬 프로필 동작을 유지합니다.",
            )
            editor_source_system = st.text_input(
                "출처 시스템",
                value=selected_profile.source_system if selected_profile and selected_profile.source_system else "",
            )
            editor_source_url = st.text_input(
                "출처 URL",
                value=selected_profile.source_url if selected_profile and selected_profile.source_url else "",
            )
            editor_required_fields = st.multiselect(
                "필수 입력 항목",
                sorted(ALLOWED_REQUIRED_ROW_FIELDS),
                default=list(selected_profile.required_row_fields) if selected_profile else ["profile_id"],
            )
            editor_max_upload_mb = st.number_input(
                "최대 업로드 용량(MB)",
                min_value=0,
                max_value=100000,
                value=selected_profile.max_upload_mb if selected_profile and selected_profile.max_upload_mb else 0,
                step=10,
            )
            editor_notes = st.text_area(
                "메모",
                value=selected_profile.notes if selected_profile else "",
            )
            editor_make_default = st.checkbox(
                "기본 프로필로 지정",
                value=bool(
                    selected_profile
                    and editable_registry.default_profile_id
                    and selected_profile.profile_id.lower() == editable_registry.default_profile_id.lower()
                ),
            )
            editor_submitted = st.form_submit_button("기관 프로필 적용")

        if editor_submitted:
            try:
                updated_registry = upsert_institution_profile(
                    editable_registry,
                    editor_profile_id,
                    display_name=editor_display_name,
                    institution_name=editor_institution_name,
                    tenant_id=editor_tenant_id,
                    source_system=editor_source_system,
                    source_url=editor_source_url,
                    required_row_fields=editor_required_fields,
                    max_upload_mb=editor_max_upload_mb or None,
                    notes=editor_notes,
                    make_default=editor_make_default,
                )
                st.session_state[REGISTRY_STATE_KEY] = institution_profile_registry_to_bytes(updated_registry)
                st.session_state[REGISTRY_SOURCE_STATE_KEY] = "세션에서 편집한 기관 프로필"
                st.success(f"적용된 프로필: {editor_profile_id.strip()}")
            except ValueError as exc:
                st.error(str(exc))
                st.stop()

    with quality_tab:
        st.markdown("### 품질 기준 관리")
        st.caption("기관별 품질 기준을 불러오거나 조정합니다.")
        current_quality_config = quality_profile_config
        quality_upload = st.file_uploader(
            "quality_profiles.json",
            type=["json"],
            key="quality_profile_config_upload",
        )
        if quality_upload:
            try:
                quality_upload.seek(0)
                current_quality_config = load_quality_gate_profile_config_from_bytes(quality_upload.read())
                st.session_state[QUALITY_PROFILE_STATE_KEY] = quality_profile_config_to_bytes(current_quality_config)
                st.session_state[QUALITY_PROFILE_SOURCE_STATE_KEY] = quality_upload.name
            except (OSError, ValueError) as exc:
                st.error(str(exc))
                st.stop()

        if quality_profile_error:
            st.error(quality_profile_error)
        editable_quality_config = current_quality_config or QualityProfileConfig()
        st.caption(
            f"{st.session_state.get(QUALITY_PROFILE_SOURCE_STATE_KEY, quality_profile_source) or '기본 품질 프로필'} | "
            f"profiles={len(editable_quality_config.profiles or {})} | "
            f"sha256={(editable_quality_config.sha256 or 'default')[:12]}"
        )
        quality_profiles_path = _quality_profiles_storage_path(settings)
        if quality_profiles_path:
            if st.button("품질 프로필 저장"):
                try:
                    quality_save_result = save_quality_profile_config(
                        quality_profiles_path,
                        editable_quality_config,
                        backup_existing=True,
                    )
                except (OSError, ValueError) as exc:
                    st.error(str(exc))
                    st.stop()
                backup_note = (
                    f" backup={quality_save_result['backup_path']}" if quality_save_result["backup_path"] else ""
                )
                st.success(
                    f"품질 프로필 {quality_save_result['profile_count']}개를 "
                    f"{quality_save_result['path']} sha256={quality_save_result['sha256'][:12]}{backup_note}"
                )
        elif quality_upload:
            st.warning("검증된 품질 프로필을 저장하려면 QUALITY_PROFILES_PATH를 설정하세요.")

        quality_profile_ids = sorted(editable_quality_config.profiles or {})
        quality_editor_options = ["<기본값>", "<새 프로필>"] + quality_profile_ids
        quality_editor_choice = st.selectbox("품질 프로필", quality_editor_options, index=0)
        if quality_editor_choice in {"<기본값>", "<새 프로필>"}:
            selected_quality_profile = editable_quality_config.default_profile
        else:
            selected_quality_profile = editable_quality_config.profiles[quality_editor_choice]
        with st.form("quality_profile_editor_form"):
            quality_profile_id = st.text_input(
                "품질 프로필 ID",
                value="" if quality_editor_choice in {"<기본값>", "<새 프로필>"} else quality_editor_choice,
            )
            quality_coverage_min = st.number_input(
                "최소 원문 보존 비율",
                min_value=0.0,
                max_value=10.0,
                value=float(selected_quality_profile.coverage_ratio_min),
                step=0.01,
            )
            quality_coverage_max = st.number_input(
                "최대 원문 보존 비율",
                min_value=0.0,
                max_value=10.0,
                value=float(selected_quality_profile.coverage_ratio_max),
                step=0.01,
            )
            quality_table_count = st.number_input(
                "표 오탐 주의 최대 건수",
                min_value=0,
                max_value=100000,
                value=int(selected_quality_profile.table_false_positive_attention_max_count),
                step=1,
            )
            quality_table_ratio = st.number_input(
                "표 오탐 주의 최대 비율",
                min_value=0.0,
                max_value=1.0,
                value=float(selected_quality_profile.table_false_positive_attention_max_ratio),
                step=0.01,
            )
            quality_submitted = st.form_submit_button("품질 프로필 적용")

        if quality_submitted:
            try:
                updated_quality_config = upsert_quality_profile(
                    editable_quality_config,
                    quality_profile_id,
                    coverage_ratio_min=quality_coverage_min,
                    coverage_ratio_max=quality_coverage_max,
                    table_false_positive_attention_max_count=quality_table_count,
                    table_false_positive_attention_max_ratio=quality_table_ratio,
                    update_default=quality_editor_choice == "<기본값>",
                )
                st.session_state[QUALITY_PROFILE_STATE_KEY] = quality_profile_config_to_bytes(updated_quality_config)
                st.session_state[QUALITY_PROFILE_SOURCE_STATE_KEY] = "세션에서 편집한 품질 프로필"
                st.success("품질 프로필을 적용했습니다.")
            except ValueError as exc:
                st.error(str(exc))
                st.stop()


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------

st.set_page_config(page_title="공공기관 규정 MCP 빌더", layout="wide")

_apply_ai_connection_overrides()
settings = get_settings()
if settings.api_auth_required or settings.tenant_storage_isolation:
    st.error("보호 모드 또는 테넌트 분리 배포에서는 Streamlit 화면을 사용할 수 없습니다.")
    st.info(
        "공유 배포에서는 FastAPI 엔드포인트를 사용하세요. 이 로컬 운영 화면은 신뢰할 수 있는 PC에서 "
        "API_AUTH_REQUIRED=false 및 TENANT_STORAGE_ISOLATION=false 상태일 때만 실행하세요."
    )
    st.stop()

repository = JsonRepository(settings)
exporter = Exporter()
institution_registry = None
institution_registry_error = None
institution_registry_source = ""
quality_profile_config = None
quality_profile_error = None
quality_profile_source = ""
institution_profiles_path = _institution_profiles_storage_path(settings)
if institution_profiles_path:
    try:
        institution_registry = load_institution_profile_registry(institution_profiles_path)
        institution_registry_source = institution_profiles_path
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        institution_registry_error = str(exc)
quality_profiles_path = _quality_profiles_storage_path(settings)
if quality_profiles_path:
    try:
        quality_profile_config = load_quality_gate_profile_config(quality_profiles_path)
        quality_profile_source = quality_profiles_path
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        quality_profile_error = str(exc)
if st.session_state.get(REGISTRY_STATE_KEY):
    try:
        institution_registry = load_institution_profile_registry_from_bytes(st.session_state[REGISTRY_STATE_KEY])
        institution_registry_source = st.session_state.get(REGISTRY_SOURCE_STATE_KEY, "session registry")
        institution_registry_error = None
    except (OSError, ValueError) as exc:
        institution_registry_error = str(exc)
if st.session_state.get(QUALITY_PROFILE_STATE_KEY):
    try:
        quality_profile_config = load_quality_gate_profile_config_from_bytes(st.session_state[QUALITY_PROFILE_STATE_KEY])
        quality_profile_source = st.session_state.get(QUALITY_PROFILE_SOURCE_STATE_KEY, "session quality profiles")
        quality_profile_error = None
    except (OSError, ValueError) as exc:
        quality_profile_error = str(exc)

_render_operator_theme()

if institution_registry_error:
    st.error(institution_registry_error)
    st.stop()

if institution_registry is None or not institution_registry.profiles:
    _page_institution_select(institution_registry or InstitutionProfileRegistry(profiles={}))
    st.stop()

if institution_registry and institution_registry.profiles:
    selected_profile_id = _selected_institution_profile_id()
    if selected_profile_id not in institution_registry.profiles:
        _page_institution_select(institution_registry)
        st.stop()

if st.session_state.get(WORKFLOW_TRANSITION_STATE_KEY):
    _render_workflow_transition_dialog()
    st.stop()

if "_nav_target" in st.session_state:
    st.session_state["nav_page"] = st.session_state.pop("_nav_target")
_apply_operator_deep_link()

current_nav_page = str(st.session_state.get("nav_page") or NAV_HOME)
if current_nav_page == LEGACY_NAV_CONNECT:
    current_nav_page = NAV_MCP
    st.session_state["nav_page"] = NAV_MCP
document_id = st.session_state.get("document_id")
# Large result files are preloaded in the visible transition dialog and reused
# across widget reruns. Pages that do not show document details skip the read.
ctx = None
if document_id and current_nav_page in DOCUMENT_CONTEXT_NAV_PAGES:
    ctx = _cached_document_context(document_id)
    if ctx is None:
        ctx = _load_document_context(document_id)
        _store_document_context_cache(document_id, ctx)

with st.sidebar:
    if institution_registry and institution_registry.profiles:
        profile_ids = sorted(institution_registry.profiles)
        current_profile_id = _selected_institution_profile_id()
        if current_profile_id not in institution_registry.profiles:
            current_profile_id = profile_ids[0]
        current_index = profile_ids.index(current_profile_id) if current_profile_id in profile_ids else 0
        st.markdown("**현재 기관**")
        switched_profile_id = st.selectbox(
            "기관 전환",
            profile_ids,
            index=current_index,
            key="institution_switcher",
            format_func=lambda profile_id: (
                institution_registry.profiles[profile_id].institution_name
                or institution_registry.profiles[profile_id].display_name
                or profile_id
            ),
            label_visibility="collapsed",
        )
        if switched_profile_id != current_profile_id:
            _select_institution_profile(switched_profile_id)
            st.rerun()
        current_profile = institution_registry.profiles[current_profile_id]
        st.caption(current_profile.institution_name or current_profile.display_name or current_profile_id)
        st.divider()
    st.markdown("### 공공기관 규정 MCP 빌더")
    st.caption("아래 ①~④ 순서대로 진행하세요. 보조 기능은 고급 메뉴에 있습니다.")
    stored_primary_page = str(st.session_state.get("primary_nav_page") or "")
    desired_primary_page = (
        current_nav_page
        if current_nav_page in PRIMARY_NAV_PAGES
        else stored_primary_page if stored_primary_page in PRIMARY_NAV_PAGES else NAV_HOME
    )
    if stored_primary_page != desired_primary_page:
        st.session_state["primary_nav_page"] = desired_primary_page
    st.radio(
        "기본 작업 순서",
        PRIMARY_NAV_PAGES,
        key="primary_nav_page",
        on_change=_go_primary_nav,
    )
    with st.expander("고급 기능·관리자 메뉴", expanded=current_nav_page in ADVANCED_NAV_PAGES):
        st.caption("일반 작업에서는 열 필요가 없습니다.")
        for advanced_page in ADVANCED_NAV_PAGES:
            if st.button(advanced_page, key=f"advanced-nav-{advanced_page}", width="stretch"):
                _queue_workflow_navigation(advanced_page)
                st.rerun()
    nav_page = current_nav_page
    st.divider()
    if ctx:
        quality_report = ctx["quality_report"]
        st.markdown("**현재 작업 중인 문서**")
        st.caption(f"문서 ID: {ctx['document_id'][:12]}")
        st.caption(f"품질: {'통과' if quality_report and quality_report.passed else '검토 필요'}")
        st.caption(f"승인된 청크: {ctx['approved_count']:,} / {len(ctx['chunks']):,}")
        st.caption(f"AI 사용 준비: {'완료' if ctx['mcp_connection_gate'].get('ready') else '아직'}")
        st.caption(f"MCP 생성: {'완료' if _mcp_bundle_created(ctx) else '아직'}")
    else:
        st.caption("아직 전처리한 문서가 없습니다.")
    st.divider()
    st.caption("이 화면은 로컬 운영자 전용입니다.")

if nav_page == NAV_HOME:
    _page_home(ctx)
elif nav_page == NAV_PREPROCESS:
    _page_preprocess()
elif nav_page == NAV_RESULTS:
    _page_results(ctx)
elif nav_page == NAV_APPROVAL:
    _page_approval(ctx)
elif nav_page == NAV_MCP:
    _page_connect(ctx, mcp_first=True)
elif nav_page == NAV_GOLDSET:
    _render_parsing_goldset_review_panel()
elif nav_page == NAV_ADMIN:
    _page_admin()
