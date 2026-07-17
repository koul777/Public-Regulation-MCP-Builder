from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _default_api_auth_required() -> bool:
    return os.getenv("APP_ENV", "local").lower() not in {"local", "dev", "development", "test"}


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "local")
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data"))
    artifact_root: Path = Path(os.getenv("ARTIFACT_ROOT", "."))
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    api_auth_required: bool = _env_bool("API_AUTH_REQUIRED", _default_api_auth_required())
    api_auth_token: str = os.getenv("API_AUTH_TOKEN", "")
    api_auth_tokens: str = os.getenv("API_AUTH_TOKENS", "")
    api_audit_enabled: bool = _env_bool("API_AUDIT_ENABLED", True)
    api_default_tenant_id: str = os.getenv("API_DEFAULT_TENANT_ID", "default")
    tenant_storage_isolation: bool = _env_bool("TENANT_STORAGE_ISOLATION", False)
    enable_agent_review: bool = _env_bool("ENABLE_AGENT_REVIEW", False)
    llm_provider: str = os.getenv("LLM_PROVIDER", "openai")
    rag_llm_backend: str = os.getenv("RAG_LLM_BACKEND", "extractive")
    rag_llm_endpoint: str = os.getenv("RAG_LLM_ENDPOINT", "")
    rag_llm_model: str = os.getenv("RAG_LLM_MODEL", "")
    rag_llm_timeout_seconds: int = int(os.getenv("RAG_LLM_TIMEOUT_SECONDS", "30"))
    rag_llm_max_output_chars: int = int(os.getenv("RAG_LLM_MAX_OUTPUT_CHARS", "2000"))
    rag_rate_limit_requests_per_window: int = int(os.getenv("RAG_RATE_LIMIT_REQUESTS_PER_WINDOW", "120"))
    rag_rate_limit_window_seconds: int = int(os.getenv("RAG_RATE_LIMIT_WINDOW_SECONDS", "60"))
    agent_review_model: str = os.getenv("AGENT_REVIEW_MODEL", "gpt-4.1-mini")
    agent_review_api_base_url: str = os.getenv("AGENT_REVIEW_API_BASE_URL", "https://api.openai.com")
    agent_review_timeout_seconds: int = int(os.getenv("AGENT_REVIEW_TIMEOUT_SECONDS", "60"))
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_compatible_api_key: str = os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
    azure_openai_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    azure_openai_api_key: str = os.getenv("AZURE_OPENAI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_api_base_url: str = os.getenv("ANTHROPIC_API_BASE_URL", "https://api.anthropic.com")
    agent_review_trigger_score_below: float = float(os.getenv("AGENT_REVIEW_TRIGGER_SCORE_BELOW", "100.0"))
    agent_review_max_chunks_per_document: int = int(os.getenv("AGENT_REVIEW_MAX_CHUNKS_PER_DOCUMENT", "20"))
    agent_review_max_input_tokens_per_document: int = int(os.getenv("AGENT_REVIEW_MAX_INPUT_TOKENS_PER_DOCUMENT", "15000"))
    agent_review_max_documents_per_batch: int = int(os.getenv("AGENT_REVIEW_MAX_DOCUMENTS_PER_BATCH", "0"))
    agent_review_max_input_tokens_per_batch: int = int(os.getenv("AGENT_REVIEW_MAX_INPUT_TOKENS_PER_BATCH", "0"))
    agent_review_max_total_tokens_per_batch: int = int(os.getenv("AGENT_REVIEW_MAX_TOTAL_TOKENS_PER_BATCH", "0"))
    agent_review_max_output_tokens_per_chunk: int = int(os.getenv("AGENT_REVIEW_MAX_OUTPUT_TOKENS_PER_CHUNK", "512"))
    agent_review_token_safety_margin: float = float(os.getenv("AGENT_REVIEW_TOKEN_SAFETY_MARGIN", "1.25"))
    agent_review_chars_per_token: int = int(os.getenv("AGENT_REVIEW_CHARS_PER_TOKEN", "4"))
    agent_review_prompt_input_tokens_per_batch: int = int(os.getenv("AGENT_REVIEW_PROMPT_INPUT_TOKENS_PER_BATCH", "0"))
    agent_review_input_price_per_1m_tokens: float = float(os.getenv("AGENT_REVIEW_INPUT_PRICE_PER_1M_TOKENS", "0"))
    agent_review_output_price_per_1m_tokens: float = float(os.getenv("AGENT_REVIEW_OUTPUT_PRICE_PER_1M_TOKENS", "0"))
    agent_review_max_cost_per_batch: float = float(os.getenv("AGENT_REVIEW_MAX_COST_PER_BATCH", "0"))
    agent_review_budget_currency: str = os.getenv("AGENT_REVIEW_BUDGET_CURRENCY", "USD")
    agent_review_price_version: str = os.getenv("AGENT_REVIEW_PRICE_VERSION", "")
    agent_review_price_effective_at: str = os.getenv("AGENT_REVIEW_PRICE_EFFECTIVE_AT", "")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "1000"))
    max_batch_upload_mb: int = int(os.getenv("MAX_BATCH_UPLOAD_MB", os.getenv("MAX_UPLOAD_MB", "1000")))
    max_batch_upload_files: int = int(os.getenv("MAX_BATCH_UPLOAD_FILES", "100"))
    max_json_request_body_mb: int = int(os.getenv("MAX_JSON_REQUEST_BODY_MB", "192"))
    hwp_max_decompressed_section_mb: int = int(os.getenv("HWP_MAX_DECOMPRESSED_SECTION_MB", "256"))
    hwp_max_decompressed_document_mb: int = int(os.getenv("HWP_MAX_DECOMPRESSED_DOCUMENT_MB", "512"))
    office_archive_max_entries: int = int(os.getenv("OFFICE_ARCHIVE_MAX_ENTRIES", "4096"))
    office_archive_max_file_mb: int = int(os.getenv("OFFICE_ARCHIVE_MAX_FILE_MB", "128"))
    office_archive_max_total_uncompressed_mb: int = int(
        os.getenv("OFFICE_ARCHIVE_MAX_TOTAL_UNCOMPRESSED_MB", "256")
    )
    office_archive_max_entry_uncompressed_mb: int = int(
        os.getenv("OFFICE_ARCHIVE_MAX_ENTRY_UNCOMPRESSED_MB", "64")
    )
    office_archive_max_compression_ratio: float = float(
        os.getenv("OFFICE_ARCHIVE_MAX_COMPRESSION_RATIO", "200")
    )
    office_archive_max_member_name_chars: int = int(
        os.getenv("OFFICE_ARCHIVE_MAX_MEMBER_NAME_CHARS", "512")
    )
    default_max_chunk_chars: int = int(os.getenv("DEFAULT_MAX_CHUNK_CHARS", "1800"))
    default_overlap_chars: int = int(os.getenv("DEFAULT_OVERLAP_CHARS", "120"))
    quality_profiles_path: str = os.getenv("QUALITY_PROFILES_PATH", "")
    quality_profiles_strict: bool = os.getenv("QUALITY_PROFILES_STRICT", "false").lower() == "true"
    institution_profiles_path: str = os.getenv("INSTITUTION_PROFILES_PATH", "")
    institution_profiles_strict: bool = os.getenv("INSTITUTION_PROFILES_STRICT", "false").lower() == "true"
    pdf_ocr_backend: str = os.getenv("PDF_OCR_BACKEND", "")
    pdf_ocr_language: str = os.getenv("PDF_OCR_LANGUAGE", "ko")
    pdf_ocr_render_scale: float = float(os.getenv("PDF_OCR_RENDER_SCALE", "2"))
    pdf_ocr_timeout_seconds: int = int(os.getenv("PDF_OCR_TIMEOUT_SECONDS", "300"))
    pdf_ocr_max_pages: int = int(os.getenv("PDF_OCR_MAX_PAGES", "0"))
    enable_kordoc_table_parser: bool = _env_bool("ENABLE_KORDOC_TABLE_PARSER", True)
    kordoc_table_command: str = os.getenv("KORDOC_TABLE_COMMAND", "kordoc")
    kordoc_table_timeout_seconds: int = int(os.getenv("KORDOC_TABLE_TIMEOUT_SECONDS", "120"))
    kordoc_table_max_tables: int = int(os.getenv("KORDOC_TABLE_MAX_TABLES", "500"))
    # When true, a confident Kordoc table becomes the main table content of a
    # table-like chunk (structure + rendered body) and the primary parser's
    # table is demoted to a review hint. When false, Kordoc stays review-only.
    kordoc_table_as_main: bool = _env_bool("KORDOC_TABLE_AS_MAIN", True)
    # Minimum Kordoc match strength required before promotion overwrites the
    # local table. One of: strong_review_match, medium_review_match, weak_review_match.
    kordoc_table_promote_min_match: str = os.getenv("KORDOC_TABLE_PROMOTE_MIN_MATCH", "medium_review_match")

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


# Operator-supplied overrides applied on top of the env-based base settings.
# Used by the local Streamlit operator console so an operator can enter AI
# connection values (review API key/model, chat LLM endpoint/model) at runtime
# without editing environment variables. Empty by default, so default behavior
# and existing tests are unaffected.
_runtime_overrides: dict[str, Any] = {}


def set_runtime_settings_overrides(**values: Any) -> None:
    """Replace the runtime overrides applied on top of the base settings.

    Only known ``Settings`` fields are kept, and ``None`` values are dropped so
    callers can pass through empty inputs without clobbering configured values.
    """

    valid_fields = {field.name for field in fields(Settings)}
    filtered = {
        key: value
        for key, value in values.items()
        if key in valid_fields and value is not None
    }
    _runtime_overrides.clear()
    _runtime_overrides.update(filtered)


def clear_runtime_settings_overrides() -> None:
    """Drop all runtime overrides and fall back to env-based settings."""

    _runtime_overrides.clear()


@lru_cache
def _base_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    base = _base_settings()
    if not _runtime_overrides:
        return base
    return replace(base, **_runtime_overrides)
