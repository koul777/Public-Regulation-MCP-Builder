from __future__ import annotations

from app.core.config import Settings


SUPPORTED_AGENT_REVIEW_PROVIDERS = (
    "openai",
    "azure-openai",
    "anthropic",
    "openai-compatible",
)

_PROVIDER_ALIASES = {
    "azure": "azure-openai",
    "azure_openai": "azure-openai",
    "claude": "anthropic",
    "openai_compatible": "openai-compatible",
    "compatible": "openai-compatible",
}


def normalize_agent_review_provider(value: str | None) -> str:
    provider = str(value or "").strip().lower()
    return _PROVIDER_ALIASES.get(provider, provider)


def agent_review_api_key(settings: Settings) -> str:
    provider = normalize_agent_review_provider(settings.llm_provider)
    if provider == "azure-openai":
        return str(settings.azure_openai_api_key or "").strip()
    if provider == "anthropic":
        return str(settings.anthropic_api_key or "").strip()
    if provider == "openai-compatible":
        return str(settings.openai_compatible_api_key or "").strip()
    return str(settings.openai_api_key or "").strip()


def agent_review_configuration_reason(settings: Settings) -> str:
    if not settings.enable_agent_review:
        return "agent_review_api_disabled"

    provider = normalize_agent_review_provider(settings.llm_provider)
    if provider not in SUPPORTED_AGENT_REVIEW_PROVIDERS:
        return "agent_review_provider_not_supported"
    if not str(settings.agent_review_model or "").strip():
        return "agent_review_model_missing"

    if provider == "openai" and not agent_review_api_key(settings):
        return "openai_api_key_missing"
    if provider == "azure-openai":
        if not str(settings.azure_openai_endpoint or "").strip():
            return "azure_openai_endpoint_missing"
        if not agent_review_api_key(settings):
            return "azure_openai_api_key_missing"
    if provider == "anthropic" and not agent_review_api_key(settings):
        return "anthropic_api_key_missing"
    if provider == "openai-compatible":
        compatible_base_url = str(settings.agent_review_api_base_url or "").strip().rstrip("/").lower()
        if not compatible_base_url or compatible_base_url == "https://api.openai.com":
            return "openai_compatible_base_url_missing"
    return ""


def agent_review_provider_ready(settings: Settings) -> bool:
    return not agent_review_configuration_reason(settings)
