"""Role contracts for the regulation-local-AI agent orchestration layer.

The registry deliberately describes responsibilities and authority separately
from the code that executes a role.  Most roles are deterministic services or
human gates; bounded model roles use distinct local models by capability.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.model_router import (
    QWEN3_ANSWER_MODEL,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_QUERY_MODEL,
    QWEN3_RERANKER_MODEL,
    QWEN3_REVIEW_MODEL,
    model_profile_for_role,
)

QWEN3_8B_MODEL = QWEN3_ANSWER_MODEL


@dataclass(frozen=True)
class AgentRoleSpec:
    """Machine-readable responsibility and authority contract for one role."""

    role_id: str
    display_name: str
    kind: str
    implementation_status: str
    purpose: str
    required_inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    can_mutate: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    failure_policy: str
    primary_model: str | None = None
    model_profile: str | None = None


AGENT_ROLE_REGISTRY: dict[str, AgentRoleSpec] = {
    "orchestrator": AgentRoleSpec(
        role_id="orchestrator",
        display_name="규정 AI 오케스트레이터",
        kind="deterministic_orchestrator",
        implementation_status="implemented_verified",
        purpose="상태 머신에 따라 전문 역할을 순서대로 호출하고 재시도·중단·감사를 조정한다.",
        required_inputs=("workflow_id", "tenant_id", "operation", "payload_reference"),
        outputs=("workflow_state", "role_results", "next_action", "audit_trace"),
        can_mutate=("workflow_state", "audit_trace"),
        forbidden_actions=("approve_chunks", "answer_without_evidence", "bypass_security_guard"),
        failure_policy="중단 가능한 상태로 전환하고 마지막 성공 단계와 복구 행동을 기록한다.",
    ),
    "intake_guard": AgentRoleSpec(
        role_id="intake_guard",
        display_name="문서 접수·안전성 담당",
        kind="deterministic_guard",
        implementation_status="implemented_verified",
        purpose="파일 형식·크기·압축 구조·중복·tenant 범위를 검사하고 처리 가능한 문서만 접수한다.",
        required_inputs=("upload_manifest", "auth_context", "file_metadata"),
        outputs=("admission_decision", "document_id", "source_hash", "failure_reasons"),
        can_mutate=("upload_manifest", "document_state", "audit_trace"),
        forbidden_actions=("parse_untrusted_archive_without_limits", "expose_raw_path", "approve_content"),
        failure_policy="문서를 격리 상태로 유지하고 이유를 반환한다.",
    ),
    "parser_extractor": AgentRoleSpec(
        role_id="parser_extractor",
        display_name="문서 파싱·추출 담당",
        kind="deterministic_parser",
        implementation_status="implemented_verified",
        purpose="PDF·DOCX·HWP·HWPX에서 page·text block·표·이미지 신호와 provenance를 추출한다.",
        required_inputs=("admitted_document", "parser_policy", "artifact_reference"),
        outputs=("parsed_document", "extraction_metrics", "parser_uncertainty"),
        can_mutate=("parsed_artifacts", "processing_trace"),
        forbidden_actions=("rewrite_source_text", "approve_chunks", "expose_raw_path"),
        failure_policy="추출 coverage가 없으면 OCR 후보 또는 blocked 상태로 전환한다.",
    ),
    "ocr_extractor": AgentRoleSpec(
        role_id="ocr_extractor",
        display_name="한국어 OCR 담당",
        kind="specialized_ocr_model",
        implementation_status="implemented_verified",
        purpose="이미지 전용 또는 저 coverage 페이지만 한국어 OCR하고 bbox·confidence를 보존한다.",
        required_inputs=("image_page_artifacts", "ocr_policy"),
        outputs=("ocr_blocks", "ocr_confidence", "review_flags"),
        can_mutate=("ocr_artifacts", "processing_trace"),
        forbidden_actions=("replace_source_without_provenance", "auto_approve_ocr", "use_external_api"),
        failure_policy="저신뢰 결과는 review_required, 결과가 없으면 blocked로 전환한다.",
        primary_model="korean_PP-OCRv5_mobile_rec",
        model_profile="ocr-korean-v5",
    ),
    "normalizer": AgentRoleSpec(
        role_id="normalizer",
        display_name="텍스트 정규화 담당",
        kind="deterministic_normalizer",
        implementation_status="implemented_verified",
        purpose="Unicode·공백·개행·목록 표기를 정리하면서 원문 span과 page provenance를 유지한다.",
        required_inputs=("parsed_document", "normalization_policy"),
        outputs=("normalized_document", "provenance_map"),
        can_mutate=("normalized_artifacts", "processing_trace"),
        forbidden_actions=("invent_text", "drop_source_span", "approve_chunks"),
        failure_policy="provenance를 보존할 수 없으면 failed로 전환한다.",
    ),
    "structure_detector": AgentRoleSpec(
        role_id="structure_detector",
        display_name="규정 계층 탐지 담당",
        kind="deterministic_structure_detector",
        implementation_status="implemented_verified",
        purpose="장·절·조·항·호·목·부칙·별표 계층과 parent path를 생성한다.",
        required_inputs=("normalized_document", "structure_policy"),
        outputs=("structure_nodes", "structure_confidence", "uncertain_spans"),
        can_mutate=("structure_artifacts", "processing_trace"),
        forbidden_actions=("invent_structure", "rewrite_source_text", "approve_chunks"),
        failure_policy="불확실 span만 structure reviewer로 전달하고 나머지 구조는 보존한다.",
    ),
    "structure_reviewer": AgentRoleSpec(
        role_id="structure_reviewer",
        display_name="규정 구조 검수 담당",
        kind="bounded_llm_review",
        implementation_status="implemented_verified",
        purpose="파서가 만든 조·항·호·목·별표·별지 구조의 불확실성을 찾아 검수 후보를 만든다.",
        required_inputs=("parsed_document", "structure_nodes", "chunks", "review_context"),
        outputs=("review_findings", "confidence", "review_flags", "content_hash"),
        can_mutate=("review_findings", "review_queue"),
        forbidden_actions=("rewrite_source_text", "approve_chunks", "change_tenant_scope", "invent_structure"),
        failure_policy="LLM 결과가 없거나 형식이 틀리면 규칙 기반 Validator 결과를 유지하고 검수를 차단한다.",
        primary_model=QWEN3_REVIEW_MODEL,
        model_profile="review-qwen3-4b",
    ),
    "table_reviewer": AgentRoleSpec(
        role_id="table_reviewer",
        display_name="표·별표·별지 검수 담당",
        kind="bounded_llm_review",
        implementation_status="implemented_verified",
        purpose="표 구조와 행·열 의미를 보조 설명하되 원문에 없는 값을 만들지 않는다.",
        required_inputs=("table_record", "table_metadata", "source_provenance"),
        outputs=("table_findings", "table_summary", "review_flags", "content_hash"),
        can_mutate=("review_findings", "review_queue"),
        forbidden_actions=("fabricate_cell_values", "rewrite_table", "approve_chunks", "drop_source_provenance"),
        failure_policy="표 구조가 불명확하면 요약을 폐기하고 사람 검수 대상으로 보낸다.",
        primary_model=QWEN3_REVIEW_MODEL,
        model_profile="review-qwen3-4b",
    ),
    "chunk_builder": AgentRoleSpec(
        role_id="chunk_builder",
        display_name="조문 Chunk 생성 담당",
        kind="deterministic_chunk_builder",
        implementation_status="implemented_verified",
        purpose="조문 의미 단위 Chunk와 계층 context·source page·시행일·버전을 생성한다.",
        required_inputs=("structure_nodes", "normalized_document", "chunk_policy"),
        outputs=("draft_chunks", "chunk_metrics"),
        can_mutate=("chunk_artifacts", "processing_trace"),
        forbidden_actions=("drop_provenance", "merge_unrelated_articles", "approve_chunks"),
        failure_policy="고아 또는 무근거 Chunk가 생기면 quality gate에서 차단한다.",
    ),
    "quality_gate": AgentRoleSpec(
        role_id="quality_gate",
        display_name="품질 게이트 담당",
        kind="deterministic_gate",
        implementation_status="implemented_verified",
        purpose="파싱·구조·Chunk·metadata 품질을 검사하고 승인 전 차단 사유를 확정한다.",
        required_inputs=("chunks", "structure_nodes", "parser_warnings", "review_findings"),
        outputs=("quality_report", "blocking_findings", "review_worklist"),
        can_mutate=("quality_report", "review_worklist", "processing_state"),
        forbidden_actions=("approve_chunks", "silently_drop_findings", "change_source_text"),
        failure_policy="차단 항목이 있으면 pending_review 상태를 유지한다.",
    ),
    "human_approval_gate": AgentRoleSpec(
        role_id="human_approval_gate",
        display_name="사람 승인 담당",
        kind="human_gate",
        implementation_status="implemented_verified",
        purpose="운영자가 원문과 처리 결과를 확인한 뒤 승인·거부·재검토를 확정한다.",
        required_inputs=("review_worklist", "source_provenance", "quality_report", "operator_decision"),
        outputs=("approval_decision", "approval_journal_record", "approved_content_hashes"),
        can_mutate=("approval_journal", "chunk_approval_state"),
        forbidden_actions=("auto_approve_from_llm", "approve_without_provenance", "approve_cross_tenant_data"),
        failure_policy="운영자 결정이 없으면 승인 상태를 변경하지 않는다.",
    ),
    "exporter": AgentRoleSpec(
        role_id="exporter",
        display_name="구조화 Export 담당",
        kind="deterministic_exporter",
        implementation_status="implemented_verified",
        purpose="JSONL·CSV·Markdown·표 export를 같은 승인·품질 snapshot에서 생성한다.",
        required_inputs=("chunks", "quality_report", "export_policy"),
        outputs=("export_artifacts", "export_manifest"),
        can_mutate=("export_directory", "processing_trace"),
        forbidden_actions=("expose_raw_path", "export_secret", "change_chunk_content"),
        failure_policy="부분 export를 완료로 표시하지 않고 failed로 전환한다.",
    ),
    "semantic_embedder": AgentRoleSpec(
        role_id="semantic_embedder",
        display_name="로컬 의미 임베딩 담당",
        kind="specialized_embedding_model",
        implementation_status="implemented_verified",
        purpose="승인된 retrieval text만 Qwen3 Embedding으로 벡터화하고 모델·차원 metadata를 기록한다.",
        required_inputs=("approved_chunks", "embedding_profile", "approval_snapshot"),
        outputs=("embedded_records", "embedding_summary"),
        can_mutate=("temporary_embedding_artifacts", "processing_trace"),
        forbidden_actions=("embed_unapproved_chunk", "mix_tenants", "use_external_api", "write_active_index"),
        failure_policy="모델 미가용 시 degraded를 기록하되 release completion은 차단한다.",
        primary_model=QWEN3_EMBEDDING_MODEL,
        model_profile="embedding-qwen3-0.6b",
    ),
    "index_builder": AgentRoleSpec(
        role_id="index_builder",
        display_name="승인 색인 담당",
        kind="deterministic_writer",
        implementation_status="implemented_verified",
        purpose="승인 journal과 최신본 정책을 확인한 뒤 embedding·BM25·hierarchy 색인을 원자적으로 갱신한다.",
        required_inputs=("approval_journal", "approved_chunks", "index_profile", "tenant_scope"),
        outputs=("index_manifest", "embedding_summary", "index_visibility", "audit_trace"),
        can_mutate=("temporary_index", "approved_vector_index", "index_manifest", "audit_trace"),
        forbidden_actions=("index_unapproved_chunks", "mix_tenants", "overwrite_active_index_before_validation"),
        failure_policy="임시 색인을 폐기하고 직전 정상 색인을 유지한다.",
    ),
    "query_analyst": AgentRoleSpec(
        role_id="query_analyst",
        display_name="질의 분석 담당",
        kind="bounded_query_agent",
        implementation_status="implemented_verified",
        purpose="질문에서 규정명·조문·기간·의도·기준일을 추출하고 검색어를 보정한다.",
        required_inputs=("user_query", "tenant_scope", "available_catalog"),
        outputs=("normalized_query", "query_intent", "locators", "search_terms", "analysis_confidence"),
        can_mutate=(),
        forbidden_actions=("retrieve_before_scope_check", "invent_regulation_title", "answer_user", "expand_without_trace", "approve_chunks"),
        failure_policy="분석 confidence가 낮으면 원문 질문과 보수적 keyword 검색으로 전환한다.",
        primary_model=QWEN3_QUERY_MODEL,
        model_profile="query-qwen3-1.7b",
    ),
    "query_rewriter": AgentRoleSpec(
        role_id="query_rewriter",
        display_name="검색어 보정 담당",
        kind="bounded_query_agent",
        implementation_status="implemented_verified",
        purpose="질의 plan을 조문 표기·규정 alias·띄어쓰기 변형을 보존한 검색 query로 변환한다.",
        required_inputs=("query_plan", "alias_dictionary", "rewrite_policy"),
        outputs=("search_queries", "rewrite_trace"),
        can_mutate=(),
        forbidden_actions=("change_tenant_scope", "invent_regulation_title", "answer_user", "approve_chunks"),
        failure_policy="schema 실패 시 원 질문과 deterministic 확장만 사용한다.",
        primary_model=QWEN3_QUERY_MODEL,
        model_profile="query-qwen3-1.7b",
    ),
    "retrieval_guard": AgentRoleSpec(
        role_id="retrieval_guard",
        display_name="검색·재랭킹·범위 보호 담당",
        kind="deterministic_retrieval_guard",
        implementation_status="implemented_verified",
        purpose="승인·tenant·기관·부서·보안등급·최신본 필터를 먼저 적용하고 후보를 검색·재랭킹한다.",
        required_inputs=("normalized_query", "query_scope", "approved_index", "lifecycle_policy"),
        outputs=("evidence_candidates", "retrieval_trace", "filter_summary"),
        can_mutate=("retrieval_trace",),
        forbidden_actions=("return_unapproved_chunk", "bypass_acl", "mix_inactive_versions", "answer_user"),
        failure_policy="범위 검증 실패 시 검색을 수행하지 않고 denied 또는 no_evidence 상태를 반환한다.",
    ),
    "reranker": AgentRoleSpec(
        role_id="reranker",
        display_name="로컬 관련도 재순위 담당",
        kind="specialized_reranker_model",
        implementation_status="implemented_verified",
        purpose="ACL-filtered 후보를 query-passage 관련도로 재정렬하되 후보 범위를 확장하지 않는다.",
        required_inputs=("query", "evidence_candidates", "reranker_profile"),
        outputs=("reranked_candidates", "reranker_trace"),
        can_mutate=("retrieval_trace",),
        forbidden_actions=("add_candidate", "bypass_acl", "use_external_api", "answer_user"),
        failure_policy="모델 미가용 시 deterministic rank를 사용하고 degraded로 표시한다.",
        primary_model=QWEN3_RERANKER_MODEL,
        model_profile="reranker-qwen3-0.6b",
    ),
    "context_builder": AgentRoleSpec(
        role_id="context_builder",
        display_name="근거 Context 구성 담당",
        kind="deterministic_context_builder",
        implementation_status="implemented_verified",
        purpose="검색 결과를 중복 제거·그룹화하고 Qwen3 8B가 읽을 안전한 근거 문맥을 구성한다.",
        required_inputs=("evidence_candidates", "context_budget", "prompt_policy"),
        outputs=("grounded_context", "evidence_ids", "context_trace"),
        can_mutate=("context_trace",),
        forbidden_actions=("add_unretrieved_facts", "include_raw_paths", "include_secrets", "change_evidence_text"),
        failure_policy="context를 만들 수 없으면 LLM 호출 없이 no_evidence 상태를 반환한다.",
    ),
    "grounded_answerer": AgentRoleSpec(
        role_id="grounded_answerer",
        display_name="Qwen3 8B 근거 답변 담당",
        kind="local_llm_answerer",
        implementation_status="implemented_verified",
        purpose="승인된 Context만 사용해 답변 초안을 만들고 근거 부족 시 확인 불가로 응답한다.",
        required_inputs=("user_query", "grounded_context", "answer_policy", "model_profile"),
        outputs=("answer_draft", "answer_mode", "limitations", "model_trace"),
        can_mutate=("model_trace",),
        forbidden_actions=(
            "use_external_api",
            "invent_citation",
            "claim_unseen_facts",
            "change_approval_state",
            "approve_chunks",
        ),
        failure_policy="Qwen3 8B 장애·빈 응답·형식 오류는 extractive fallback 또는 명확한 unavailable 상태로 처리한다.",
        primary_model=QWEN3_8B_MODEL,
        model_profile="answer-qwen3-8b",
    ),
    "claim_auditor": AgentRoleSpec(
        role_id="claim_auditor",
        display_name="답변 주장·근거 감사 담당",
        kind="bounded_llm_verifier",
        implementation_status="implemented_verified",
        purpose="답변의 핵심 주장을 분리하고 인용 snippet이 semantic support를 제공하는지 보조 판정한다.",
        required_inputs=("answer_draft", "cited_evidence", "claim_policy"),
        outputs=("claim_findings", "supported_claim_ids", "unsupported_claim_ids"),
        can_mutate=("answer_trace",),
        forbidden_actions=("approve_answer", "approve_chunks", "invent_evidence", "change_citation", "use_external_api"),
        failure_policy="미검증 주장이 있으면 deterministic citation verifier가 제거 또는 abstain한다.",
        primary_model=QWEN3_REVIEW_MODEL,
        model_profile="review-qwen3-4b",
    ),
    "citation_verifier": AgentRoleSpec(
        role_id="citation_verifier",
        display_name="인용·답변 검증 담당",
        kind="deterministic_verifier",
        implementation_status="implemented_verified",
        purpose="답변이 실제 승인 evidence를 벗어나지 않았는지 검증하고 공개 citation을 다시 만든다.",
        required_inputs=("answer_draft", "evidence_candidates", "evidence_ids", "citation_policy"),
        outputs=("verified_answer", "citations", "abstained", "verification_findings"),
        can_mutate=("answer_trace",),
        forbidden_actions=("silently_accept_unverified_claim", "invent_source_page", "expose_internal_ids"),
        failure_policy="검증 실패 시 답변을 제한하거나 extractive evidence-only 답변으로 낮춘다.",
    ),
    "security_guard": AgentRoleSpec(
        role_id="security_guard",
        display_name="보안·출력 정책 담당",
        kind="deterministic_security_guard",
        implementation_status="implemented_verified",
        purpose="입력·tenant scope·prompt injection·출력 metadata·외부 endpoint 정책을 검사한다.",
        required_inputs=("auth_context", "query_or_answer", "security_policy"),
        outputs=("security_decision", "sanitized_payload", "security_findings"),
        can_mutate=("security_trace",),
        forbidden_actions=("weaken_policy_for_llm", "expose_secrets", "bypass_tenant_isolation"),
        failure_policy="검사 실패 또는 위험 신호는 차단하고 운영자에게 일반화된 사유만 반환한다.",
    ),
    "evaluation_agent": AgentRoleSpec(
        role_id="evaluation_agent",
        display_name="회귀·품질 평가 담당",
        kind="deterministic_evaluator",
        implementation_status="implemented_verified",
        purpose="파싱·검색·답변·인용·보안 회귀셋을 실행하고 release gate용 증적을 만든다.",
        required_inputs=("evaluation_seedpack", "runtime_profile", "test_scope"),
        outputs=("evaluation_report", "quality_metrics", "blockers"),
        can_mutate=("evaluation_reports",),
        forbidden_actions=("modify_production_data", "approve_release_without_gate", "rewrite_baseline_without_review"),
        failure_policy="blocker를 숨기지 않고 release gate를 실패시킨다.",
    ),
    "release_operator": AgentRoleSpec(
        role_id="release_operator",
        display_name="릴리스·MCP 운영 담당",
        kind="deterministic_operator",
        implementation_status="implemented_verified",
        purpose="Hermes harness를 통해 테스트·빌드·MCP smoke·공개 릴리스 검증을 오케스트레이션한다.",
        required_inputs=("project_root", "release_scope", "tenant_scope", "release_options"),
        outputs=("release_report", "evidence_artifacts", "next_actions"),
        can_mutate=("release_artifacts", "reports"),
        forbidden_actions=("publish_without_gate", "include_runtime_data", "bypass_owner_review"),
        failure_policy="필수 gate 실패 시 release를 중단하고 복구·검토 행동을 제시한다.",
    ),
}


WORKFLOW_ROLE_SEQUENCES: dict[str, tuple[str, ...]] = {
    "ingestion_and_approval": (
        "orchestrator",
        "security_guard",
        "intake_guard",
        "parser_extractor",
        "ocr_extractor",
        "normalizer",
        "structure_detector",
        "structure_reviewer",
        "table_reviewer",
        "chunk_builder",
        "quality_gate",
        "human_approval_gate",
        "exporter",
        "semantic_embedder",
        "index_builder",
        "evaluation_agent",
    ),
    "local_regulation_qa": (
        "orchestrator",
        "security_guard",
        "query_analyst",
        "query_rewriter",
        "retrieval_guard",
        "reranker",
        "context_builder",
        "grounded_answerer",
        "claim_auditor",
        "citation_verifier",
        "security_guard",
    ),
    "release_and_mcp_handoff": (
        "orchestrator",
        "evaluation_agent",
        "release_operator",
    ),
}


def get_agent_role(role_id: str) -> AgentRoleSpec:
    """Return a role contract or raise a clear error for an unknown role."""

    normalized = str(role_id or "").strip()
    try:
        return AGENT_ROLE_REGISTRY[normalized]
    except KeyError:
        known = ", ".join(sorted(AGENT_ROLE_REGISTRY))
        raise ValueError(f"Unknown agent role: {normalized or '<empty>'}. Known roles: {known}") from None


def workflow_roles(workflow_id: str) -> tuple[AgentRoleSpec, ...]:
    """Return validated role contracts for a named workflow."""

    normalized = str(workflow_id or "").strip()
    try:
        role_ids = WORKFLOW_ROLE_SEQUENCES[normalized]
    except KeyError:
        known = ", ".join(sorted(WORKFLOW_ROLE_SEQUENCES))
        raise ValueError(f"Unknown workflow: {normalized or '<empty>'}. Known workflows: {known}") from None
    return tuple(get_agent_role(role_id) for role_id in role_ids)


def validate_role_registry() -> None:
    """Validate registry references at import/test time without side effects."""

    for workflow_id, role_ids in WORKFLOW_ROLE_SEQUENCES.items():
        for role_id in role_ids:
            if role_id not in AGENT_ROLE_REGISTRY:
                raise ValueError(f"Workflow {workflow_id} references unknown role {role_id}.")

    for role_id, spec in AGENT_ROLE_REGISTRY.items():
        if role_id != spec.role_id:
            raise ValueError(f"Role registry key does not match role_id: {role_id}")
        if spec.kind == "local_llm_answerer" and spec.primary_model != QWEN3_8B_MODEL:
            raise ValueError("The grounded answerer must use the configured Qwen3 8B model.")
        routed_profile = model_profile_for_role(role_id)
        if spec.model_profile:
            if routed_profile is None or routed_profile.profile_id != spec.model_profile:
                raise ValueError(f"Role {role_id} model_profile does not match the model router.")
            if spec.primary_model != routed_profile.model:
                raise ValueError(f"Role {role_id} primary_model does not match its model profile.")
        elif routed_profile is not None:
            raise ValueError(f"Role {role_id} is missing its routed model_profile.")


validate_role_registry()
