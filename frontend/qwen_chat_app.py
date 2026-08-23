from __future__ import annotations

"""Standalone localhost UI for approved, document-scoped Qwen regulation chat.

This module deliberately does not import the operator/builder Streamlit app.  It
is launched as its own Streamlit process by ``scripts.run_qwen_chat`` and only
reads the repository that the local builder already produced.
"""

from dataclasses import dataclass
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import streamlit as st

from app.agents.model_router import QWEN3_ANSWER_MODEL, require_loopback_endpoint
from app.api.routes_documents import get_index_status
from app.api.routes_rag import RagChatRequest, rag_chat, rag_chat_progress
from app.core.config import Settings, get_settings
from app.core.institution_profiles import (
    InstitutionProfile,
    InstitutionProfileRegistry,
    load_institution_profile_registry,
    normalize_profile_id,
)
from app.core.security_primitives import API_ROLE_ADMIN, AuthContext
from app.rag.local_llm import local_llm_available, probe_local_llm
from app.storage.repository import JsonRepository


LOCAL_APP_ENVS = frozenset({"local", "dev", "development", "test"})
HISTORY_SESSION_KEY = "standalone_qwen_document_histories"
PROBE_SESSION_KEY = "standalone_qwen_runtime_probe"
MAX_HISTORY_MESSAGES = 12

_STAGE_LABELS = {
    "query_analysis": "1/5 질문과 검색 범위를 확인하고 있습니다.",
    "retrieval": "2/5 승인된 규정 조항을 찾고 있습니다.",
    "rerank": "2/5 찾은 조항의 관련도 순서를 정리하고 있습니다.",
    "context_build": "3/5 답변에 사용할 근거를 정리하고 있습니다.",
    "answer_generation": "4/5 Qwen3 8B가 근거 안에서 답변을 만들고 있습니다.",
    "claim_audit": "4/5 답변의 근거 연결을 확인하고 있습니다.",
    "fallback_answer": "4/5 확인 가능한 근거만 남겨 답변을 다듬고 있습니다.",
    "citation_verify": "5/5 답변과 인용 조항을 마지막으로 확인하고 있습니다.",
    "completed": "답변과 인용 확인을 마쳤습니다.",
}


@dataclass(frozen=True)
class DocumentReadiness:
    document: Any
    active_chunk_count: int
    approved_chunk_count: int
    rejected_chunk_count: int
    pending_review_count: int
    gate: dict[str, Any]
    index_status: dict[str, Any] | None
    status_check_error: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.gate.get("ready")) and self.pending_review_count == 0


def protected_or_shared_mode_reason(settings: Settings) -> str | None:
    """Return a beginner-facing reason when this unauthenticated local UI must stop."""

    if str(settings.app_env or "").strip().lower() not in LOCAL_APP_ENVS:
        return (
            "보호된 운영 환경에서는 이 로컬 전용 채팅 화면을 사용할 수 없습니다. "
            "인증이 적용된 FastAPI/MCP 경로를 사용해 주세요."
        )
    if settings.api_auth_required:
        return (
            "API 인증 보호 모드가 켜져 있어 로컬 전용 채팅 화면을 중단했습니다. "
            "이 화면은 API_AUTH_REQUIRED=false인 개인 PC 로컬 모드에서만 사용할 수 있습니다."
        )
    if settings.tenant_storage_isolation:
        return (
            "여러 테넌트를 분리하는 공유 운영 모드에서는 현재 테넌트를 안전하게 인증할 수 없어 "
            "이 화면을 사용할 수 없습니다."
        )
    return None


def local_tenant_id(settings: Settings) -> str:
    tenant_id = str(settings.api_default_tenant_id or "").strip()
    if (
        not tenant_id
        or len(tenant_id) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in tenant_id)
    ):
        raise ValueError("로컬 기본 테넌트 설정이 올바르지 않습니다.")
    return tenant_id


def institution_registry_path(settings: Settings) -> Path:
    configured = str(settings.institution_profiles_path or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(settings.data_dir) / "institution_profiles.json"


def load_local_institution_registry(settings: Settings) -> InstitutionProfileRegistry:
    """Load the configured registry, or the builder's local data-dir registry."""

    return load_institution_profile_registry(institution_registry_path(settings))


def local_profiles(
    registry: InstitutionProfileRegistry,
    tenant_id: str,
) -> dict[str, InstitutionProfile]:
    """Return registry profiles that are local-generic or explicitly tenant-bound."""

    visible: dict[str, InstitutionProfile] = {}
    for profile_id, profile in registry.profiles.items():
        profile_tenant = str(profile.tenant_id or "").strip()
        if profile_tenant and profile_tenant != tenant_id:
            continue
        normalized_id = normalize_profile_id(profile_id)
        if normalized_id:
            visible[normalized_id] = profile
    return dict(sorted(visible.items()))


def completed_documents_for_profile(
    documents: Iterable[Any],
    *,
    tenant_id: str,
    profile_id: str,
) -> list[Any]:
    """Keep only completed documents in the exact local tenant/profile scope."""

    normalized_profile_id = normalize_profile_id(profile_id)
    visible = [
        document
        for document in documents
        if str(getattr(document, "status", "") or "").strip().lower() == "completed"
        and str(getattr(document, "tenant_id", "") or "").strip() == tenant_id
        and normalize_profile_id(getattr(document, "profile_id", None)) == normalized_profile_id
    ]
    return sorted(
        visible,
        key=lambda document: (
            str(getattr(document, "processed_at", "") or ""),
            str(getattr(document, "created_at", "") or ""),
            str(getattr(document, "document_id", "") or ""),
        ),
        reverse=True,
    )


def evaluate_index_gate(
    index_status: dict[str, Any] | None,
    approved_count: int,
) -> dict[str, Any]:
    """Mirror the operator UI's fail-closed approved/indexed consistency gate."""

    payload = index_status if isinstance(index_status, dict) else {}
    vector_summary = payload.get("vector_summary")
    if not isinstance(vector_summary, dict):
        vector_summary = {}
    vector_consistency = payload.get("vector_consistency")
    if not isinstance(vector_consistency, dict):
        vector_consistency = {}

    normalized_approved_count = max(0, _safe_int(approved_count))
    visible_count, visible_count_valid = _gate_count(vector_summary.get("record_count"))
    stale_count, stale_count_valid = _gate_count(vector_consistency.get("stale_count"))
    indexing_status = str(payload.get("indexing_status") or "unknown").strip().lower()
    validation_error = payload.get("validation_error")
    ready = (
        normalized_approved_count > 0
        and indexing_status == "indexed"
        and visible_count == normalized_approved_count
        and stale_count == 0
        and visible_count_valid
        and stale_count_valid
        and not validation_error
    )

    if ready:
        reason = "approved_chunks_indexed"
    elif normalized_approved_count <= 0:
        reason = "no_approved_chunks"
    elif indexing_status != "indexed":
        reason = "document_not_indexed"
    elif visible_count != normalized_approved_count:
        reason = "visible_record_count_mismatch"
    elif stale_count:
        reason = "stale_vector_records"
    elif not visible_count_valid or not stale_count_valid:
        reason = "invalid_index_status"
    elif validation_error:
        reason = "index_validation_error"
    else:
        reason = "not_ready"

    return {
        "ready": ready,
        "reason": reason,
        "approved_count": normalized_approved_count,
        "mcp_visible_count": visible_count,
        "indexing_status": indexing_status,
        "stale_count": stale_count,
        "validation_error": validation_error,
    }


def document_readiness(
    repository: JsonRepository,
    document: Any,
    auth: AuthContext,
    *,
    index_status_getter: Callable[[str, AuthContext], Any] = get_index_status,
) -> DocumentReadiness:
    document_id = str(getattr(document, "document_id", "") or "").strip()
    try:
        chunks = repository.get_chunks(document_id) if document_id else []
    except Exception as exc:
        unavailable_gate = evaluate_index_gate(None, 0)
        unavailable_gate.update({"ready": False, "reason": "chunk_state_unavailable"})
        return DocumentReadiness(
            document=document,
            active_chunk_count=0,
            approved_chunk_count=0,
            rejected_chunk_count=0,
            pending_review_count=0,
            gate=unavailable_gate,
            index_status=None,
            status_check_error=type(exc).__name__,
        )
    active_chunks = [
        chunk
        for chunk in chunks
        if str(getattr(chunk, "approval_status", "") or "").strip().lower() != "superseded"
    ]
    approved_count = sum(
        str(getattr(chunk, "approval_status", "") or "").strip().lower() == "approved"
        for chunk in active_chunks
    )
    rejected_count = sum(
        str(getattr(chunk, "approval_status", "") or "").strip().lower() == "rejected"
        for chunk in active_chunks
    )
    pending_review_count = sum(
        str(getattr(chunk, "approval_status", "") or "").strip().lower()
        not in {"approved", "rejected"}
        for chunk in active_chunks
    )

    status_payload: dict[str, Any] | None = None
    status_error = ""
    try:
        result = index_status_getter(document_id, auth)
        if isinstance(result, dict):
            status_payload = result
        else:
            status_error = "invalid_index_status"
    except Exception as exc:
        # Never echo an exception message here: it can contain a local path.
        status_error = type(exc).__name__

    index_gate = evaluate_index_gate(status_payload, approved_count)
    if pending_review_count:
        index_gate = {
            **index_gate,
            "ready": False,
            "reason": "pending_review",
            "pending_review_count": pending_review_count,
        }
    else:
        index_gate = {**index_gate, "pending_review_count": 0}

    return DocumentReadiness(
        document=document,
        active_chunk_count=len(active_chunks),
        approved_chunk_count=approved_count,
        rejected_chunk_count=rejected_count,
        pending_review_count=pending_review_count,
        gate=index_gate,
        index_status=status_payload,
        status_check_error=status_error,
    )


def qwen_runtime_configuration_issue(settings: Settings) -> str | None:
    backend = str(settings.rag_llm_backend or "").strip().lower()
    if backend != "ollama":
        return "답변 엔진이 Ollama로 설정되지 않았습니다. RAG_LLM_BACKEND=ollama가 필요합니다."
    if str(settings.rag_llm_model or "").strip().lower() != QWEN3_ANSWER_MODEL:
        return "이 전용 화면은 qwen3:8b만 사용합니다. RAG_LLM_MODEL=qwen3:8b로 설정해 주세요."
    try:
        require_loopback_endpoint(settings.rag_llm_endpoint)
    except ValueError:
        return "Ollama 주소는 이 PC의 localhost/loopback HTTP 주소여야 합니다."
    if not local_llm_available(settings):
        return "Ollama 주소는 http://127.0.0.1:11434, http://localhost:11434 또는 ::1 loopback을 사용해 주세요."
    return None


def build_chat_request(
    *,
    question: str,
    messages: Iterable[dict[str, Any]],
    document_id: str,
    profile_id: str,
    top_k: int = 5,
) -> RagChatRequest:
    normalized_document_id = str(document_id or "").strip()
    normalized_profile_id = normalize_profile_id(profile_id)
    if not normalized_document_id or not normalized_profile_id:
        raise ValueError("질문할 규정과 기관이 정확히 선택되지 않았습니다.")
    history: list[dict[str, str]] = []
    for message in list(messages)[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(message, dict) or message.get("error"):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()[:6000]
        if role in {"user", "assistant"} and content:
            history.append({"role": role, "content": content})
    return RagChatRequest(
        query=str(question or "").strip(),
        history=history[-MAX_HISTORY_MESSAGES:],
        top_k=top_k,
        document_id=normalized_document_id,
        profile_id=normalized_profile_id,
        metadata_profile="external",
        llm_backend="ollama",
        # Preserve the repository's normal qwen3:8b orchestration contract:
        # query analysis, grounded answering, claim audit, citation verification,
        # and their existing deterministic/extractive fallback behavior.
        orchestration_mode="auto",
    )


def start_rag_chat_worker(
    request: RagChatRequest,
    auth: AuthContext,
    *,
    chat_callable: Callable[[RagChatRequest, AuthContext], Any] | None = None,
) -> tuple[
    threading.Thread,
    queue.Queue[dict[str, Any]],
    queue.Queue[tuple[str, Any]],
]:
    """Start RAG chat outside Streamlit's render thread and expose real stages."""

    progress_events: queue.Queue[dict[str, Any]] = queue.Queue()
    outcome: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    active_chat = chat_callable or rag_chat

    def _worker() -> None:
        try:
            with rag_chat_progress(progress_events.put):
                response = active_chat(request, auth)
            outcome.put(("ok", response))
        except Exception as exc:
            outcome.put(("error", exc))

    worker = threading.Thread(
        target=_worker,
        name="standalone-qwen-rag-chat",
        daemon=True,
    )
    worker.start()
    return worker, progress_events, outcome


def run_rag_chat_with_visible_progress(
    request: RagChatRequest,
    auth: AuthContext,
) -> dict[str, Any]:
    worker, progress_events, outcome = start_rag_chat_worker(request, auth)
    confirmed_progress = 2
    displayed_progress = 2.0
    current_stage = "질문을 안전하게 접수했습니다."
    started_at = time.monotonic()

    with st.status("Qwen 답변을 준비하고 있습니다.", expanded=True) as status:
        progress_bar = st.progress(confirmed_progress, text=current_stage)
        detail = st.empty()
        while worker.is_alive():
            received = False
            while True:
                try:
                    event = progress_events.get_nowait()
                except queue.Empty:
                    break
                received = True
                confirmed_progress = max(confirmed_progress, _safe_int(event.get("progress")))
                stage_id = str(event.get("stage") or "").strip()
                current_stage = _STAGE_LABELS.get(stage_id, "규정 근거를 처리하고 있습니다.")
            if received:
                displayed_progress = max(displayed_progress, float(confirmed_progress))
            else:
                # A bounded animation shows liveness while a local model call is
                # blocking, without claiming that an unreported stage completed.
                displayed_progress = min(
                    min(95.0, float(confirmed_progress + 10)),
                    displayed_progress + 0.35,
                )
            progress_bar.progress(int(displayed_progress), text=current_stage)
            detail.caption(
                f"현재 단계가 계속 실행 중입니다 · 경과 {time.monotonic() - started_at:.1f}초 · "
                "창을 닫지 않아도 진행 상태가 자동으로 바뀝니다."
            )
            time.sleep(0.2)

        worker.join()
        while True:
            try:
                event = progress_events.get_nowait()
            except queue.Empty:
                break
            confirmed_progress = max(confirmed_progress, _safe_int(event.get("progress")))
            stage_id = str(event.get("stage") or "").strip()
            current_stage = _STAGE_LABELS.get(stage_id, current_stage)

        if outcome.empty():
            status.update(label="답변 작업 결과를 확인하지 못했습니다.", state="error", expanded=True)
            raise RuntimeError("Qwen chat worker ended without a result")
        outcome_type, outcome_value = outcome.get_nowait()
        if outcome_type != "ok":
            progress_bar.progress(max(1, min(99, confirmed_progress)), text="답변 작업을 완료하지 못했습니다.")
            status.update(label="Qwen 답변 생성이 중단되었습니다.", state="error", expanded=True)
            if isinstance(outcome_value, Exception):
                raise outcome_value
            raise RuntimeError("Qwen chat worker returned an invalid error")

        progress_bar.progress(100, text=_STAGE_LABELS["completed"])
        detail.caption(f"완료 · 총 {time.monotonic() - started_at:.1f}초")
        status.update(label="Qwen 답변과 근거 확인 완료", state="complete", expanded=False)

    if not isinstance(outcome_value, dict):
        raise RuntimeError("Qwen chat returned an invalid response")
    return outcome_value


def safe_citation_rows(citations: Any) -> list[dict[str, Any]]:
    """Render human-readable evidence without internal paths or audit identifiers."""

    if not isinstance(citations, list):
        return []
    rows: list[dict[str, Any]] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        page_start = citation.get("source_page_start")
        page_end = citation.get("source_page_end")
        page_label = ""
        if page_start not in (None, ""):
            page_label = str(page_start)
            if page_end not in (None, "", page_start):
                page_label = f"{page_label}–{page_end}"
        row = {
            "규정명": citation.get("regulation_title") or citation.get("document_title"),
            "조문": citation.get("article_no"),
            "조문 제목": citation.get("article_title"),
            "항": citation.get("paragraph_no"),
            "원문 쪽": page_label,
            "근거 인용문": citation.get("support_quote"),
        }
        row = {key: value for key, value in row.items() if value not in (None, "")}
        if row:
            rows.append(row)
    return rows


def readiness_message(readiness: DocumentReadiness) -> str:
    if readiness.ready:
        return "질문 가능"
    reason = str(readiness.gate.get("reason") or "not_ready")
    return {
        "chunk_state_unavailable": "③ 승인·색인에서 조항 상태 확인 필요",
        "pending_review": "③ 승인·색인에서 남은 조항 검토 필요",
        "no_approved_chunks": "사람의 승인 필요",
        "document_not_indexed": "승인 내용 색인 필요",
        "visible_record_count_mismatch": "승인 수와 색인 수가 달라 재색인 필요",
        "stale_vector_records": "이전 색인이 남아 재색인 필요",
        "index_validation_error": "승인·색인 검증 확인 필요",
        "invalid_index_status": "색인 상태 값 확인 필요",
    }.get(reason, "상태 확인 필요")


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _gate_count(value: Any) -> tuple[int, bool]:
    if value in (None, ""):
        return 0, True
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return 0, False
    return converted, converted >= 0


def _profile_label(profile: InstitutionProfile) -> str:
    return str(profile.display_name or profile.institution_name or profile.profile_id).strip()


def _document_label(document: Any) -> str:
    return str(
        getattr(document, "document_name", None)
        or getattr(document, "regulation_id", None)
        or getattr(document, "filename", None)
        or getattr(document, "document_id", "규정")
    ).strip()


def _document_history(profile_id: str, document_id: str) -> list[dict[str, Any]]:
    histories = st.session_state.setdefault(HISTORY_SESSION_KEY, {})
    if not isinstance(histories, dict):
        histories = {}
        st.session_state[HISTORY_SESSION_KEY] = histories
    key = f"{normalize_profile_id(profile_id)}:{document_id}"
    messages = histories.setdefault(key, [])
    if not isinstance(messages, list):
        messages = []
        histories[key] = messages
    return messages


def _render_assistant_message(message: dict[str, Any]) -> None:
    if message.get("error"):
        st.error(str(message.get("content") or "답변을 만들지 못했습니다."))
        return
    st.markdown(str(message.get("content") or "근거에서 답변을 확인하지 못했습니다."))
    citation_rows = safe_citation_rows(message.get("citations"))
    if citation_rows:
        with st.expander(f"근거 인용 {len(citation_rows)}건", expanded=True):
            st.dataframe(citation_rows, hide_index=True, width="stretch")


def _friendly_chat_error(exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return "질문을 너무 빠르게 연속으로 보냈습니다. 잠시 기다린 뒤 다시 질문해 주세요."
    if status_code in {401, 403, 404}:
        return "선택한 기관·규정 또는 승인 범위를 다시 확인해 주세요. 안전을 위해 답변하지 않았습니다."
    if status_code == 503:
        return "Ollama 또는 qwen3:8b가 응답하지 않았습니다. 연결 확인 후 다시 시도해 주세요."
    return "답변을 만들지 못했습니다. 승인·색인 상태와 Ollama 실행 상태를 확인해 주세요."


def _probe_signature(settings: Settings) -> str:
    return "|".join(
        (
            str(settings.rag_llm_backend or "").strip().lower(),
            str(settings.rag_llm_model or "").strip().lower(),
            str(settings.rag_llm_endpoint or "").strip().lower(),
        )
    )


def main() -> None:
    st.set_page_config(page_title="로컬 Qwen 규정 챗봇", page_icon="💬", layout="wide")
    st.title("로컬 Qwen 규정 챗봇")
    st.caption(
        "빌더와 별도로 실행되는 localhost 전용 화면입니다. 선택한 규정의 승인·색인된 조항만 "
        "qwen3:8b에 전달하며 외부 AI 서비스에는 보내지 않습니다."
    )

    settings = get_settings()
    blocked_reason = protected_or_shared_mode_reason(settings)
    if blocked_reason:
        st.error(blocked_reason)
        st.stop()

    runtime_issue = qwen_runtime_configuration_issue(settings)
    if runtime_issue:
        st.error(runtime_issue)
        st.info(
            "처음이라면 터미널에서 `ollama pull qwen3:8b`를 한 번 실행하고, Ollama를 켠 뒤 "
            "RUN_QWEN_CHAT.bat를 다시 실행해 주세요."
        )
        st.stop()

    try:
        tenant_id = local_tenant_id(settings)
        registry = load_local_institution_registry(settings)
    except FileNotFoundError:
        st.error("기관 프로필 파일을 찾지 못했습니다. 먼저 빌더에서 기관을 등록해 주세요.")
        st.stop()
    except (OSError, ValueError):
        st.error("기관 프로필 파일을 안전하게 읽지 못했습니다. 빌더에서 기관 설정을 확인해 주세요.")
        st.stop()

    profiles = local_profiles(registry, tenant_id)
    if not profiles:
        st.warning("현재 로컬 테넌트에서 사용할 수 있는 기관 프로필이 없습니다.")
        st.stop()

    selected_profile_id = st.selectbox(
        "1. 질문할 기관을 선택하세요",
        options=list(profiles),
        format_func=lambda profile_id: _profile_label(profiles[profile_id]),
    )

    try:
        repository = JsonRepository(settings)
        documents = completed_documents_for_profile(
            repository.list_documents(),
            tenant_id=tenant_id,
            profile_id=selected_profile_id,
        )
    except Exception:
        st.error("로컬 규정 저장소를 읽지 못했습니다. 빌더에서 전처리 상태를 확인해 주세요.")
        st.stop()

    if not documents:
        st.info("이 기관에는 전처리가 완료된 규정이 없습니다. 먼저 빌더에서 전처리를 완료해 주세요.")
        st.stop()

    auth = AuthContext(
        actor="standalone-qwen-local-operator",
        tenant_id=tenant_id,
        auth_mode="standalone-streamlit-local",
        role=API_ROLE_ADMIN,
    )
    readiness_items = [document_readiness(repository, document, auth) for document in documents]
    st.markdown("### 2. 규정 준비 상태를 확인하세요")
    st.caption("목록에는 전처리가 끝난 규정만 보입니다. ‘질문 가능’인 규정만 채팅 선택란에 나타납니다.")
    st.dataframe(
        [
            {
                "규정": _document_label(item.document),
                "승인 조항": f"{item.approved_chunk_count}/{item.active_chunk_count}",
                "검토 대기": item.pending_review_count,
                "색인 상태": str(item.gate.get("indexing_status") or "확인 필요"),
                "판정": readiness_message(item),
            }
            for item in readiness_items
        ],
        hide_index=True,
        width="stretch",
    )

    ready_by_id = {
        str(getattr(item.document, "document_id", "") or ""): item
        for item in readiness_items
        if item.ready
    }
    if not ready_by_id:
        st.warning(
            "아직 질문 가능한 규정이 없습니다. 빌더의 ‘③ 승인·색인’에서 남은 조항을 모두 승인 또는 "
            "반려한 뒤 ‘승인된 내용 색인’을 완료해 주세요. "
            "승인 조항 수와 색인 조항 수가 정확히 같아야 합니다."
        )
        st.stop()

    selected_document_id = st.selectbox(
        "3. 질문할 규정 하나를 선택하세요",
        options=list(ready_by_id),
        format_func=lambda document_id: _document_label(ready_by_id[document_id].document),
    )
    selected = ready_by_id[selected_document_id]
    st.success(
        f"질문 범위가 ‘{_document_label(selected.document)}’ 한 건으로 고정되었습니다. "
        f"승인·색인 조항 {selected.approved_chunk_count}개만 검색합니다."
    )

    st.markdown("### 4. Ollama와 Qwen3 8B 연결을 확인하세요")
    probe_signature = _probe_signature(settings)
    probe_state = st.session_state.get(PROBE_SESSION_KEY)
    probe_ok = bool(
        isinstance(probe_state, dict)
        and probe_state.get("signature") == probe_signature
        and probe_state.get("available") is True
    )
    if st.button("Ollama · qwen3:8b 연결 확인", width="stretch"):
        with st.spinner("이 PC의 Ollama에 짧은 확인 질문을 보내고 있습니다."):
            result = probe_local_llm(settings)
        probe_ok = bool(result.get("available"))
        st.session_state[PROBE_SESSION_KEY] = {
            "signature": probe_signature,
            "available": probe_ok,
        }
    if probe_ok:
        st.success("연결되었습니다. 이제 아래 입력창에 규정 질문을 적을 수 있습니다.")
    else:
        st.info(
            "위 버튼으로 연결을 먼저 확인해 주세요. 실패하면 Ollama가 실행 중인지, "
            "`ollama pull qwen3:8b`가 완료되었는지 확인하세요."
        )

    st.markdown("### 5. 질문하고 답변과 근거 인용을 확인하세요")
    st.caption(
        "질문을 보내면 실제 규정 검색·Qwen 답변·인용 검증 단계와 진행 게이지, 현재 단계, "
        "경과 시간이 실시간으로 표시됩니다. 답변 아래의 ‘근거 인용’을 함께 확인하세요."
    )
    messages = _document_history(selected_profile_id, selected_document_id)
    clear_column, scope_column = st.columns([1, 4])
    with clear_column:
        if st.button("이 규정 대화 지우기", disabled=not messages):
            messages.clear()
            st.rerun()
    with scope_column:
        top_k = st.slider("답변에 참고할 승인 조항 수", min_value=1, max_value=10, value=5)

    for message in messages:
        role = str(message.get("role") or "assistant")
        with st.chat_message(role if role in {"user", "assistant"} else "assistant"):
            if role == "assistant":
                _render_assistant_message(message)
            else:
                st.markdown(str(message.get("content") or ""))

    question = st.chat_input(
        "선택한 규정에 대해 질문하세요",
        disabled=not probe_ok,
    )
    if not question:
        return

    request = build_chat_request(
        question=question,
        messages=messages,
        document_id=selected_document_id,
        profile_id=selected_profile_id,
        top_k=top_k,
    )
    messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        try:
            response = run_rag_chat_with_visible_progress(request, auth)
            assistant_message = {
                "role": "assistant",
                "content": str(response.get("answer") or "근거에서 답변을 확인하지 못했습니다."),
                "citations": list(response.get("citations") or []),
                "trace_id": str(response.get("trace_id") or ""),
            }
            messages.append(assistant_message)
            _render_assistant_message(assistant_message)
        except Exception as exc:
            failure = {
                "role": "assistant",
                "content": _friendly_chat_error(exc),
                "error": True,
            }
            messages.append(failure)
            _render_assistant_message(failure)


if __name__ == "__main__":
    main()
