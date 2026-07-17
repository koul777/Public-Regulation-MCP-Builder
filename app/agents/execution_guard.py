from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

from app.agents.review_policy import agent_review_content_hash
from app.agents.provider_config import (
    SUPPORTED_AGENT_REVIEW_PROVIDERS,
    agent_review_api_key,
    normalize_agent_review_provider,
)
from app.core.config import Settings
from app.schemas.chunk import Chunk


MILLION = Decimal("1000000")


def preflight_agent_review_execution(
    plans: list[dict[str, Any]],
    settings: Settings,
    *,
    actor: str | None = None,
    approval_reference: str | None = None,
    approved_model: str | None = None,
    prompt: str | None = None,
    prompt_hash_value: str | None = None,
    prompt_input_tokens: int | None = None,
) -> dict[str, Any]:
    """Reserve an AI review budget before a billable provider call.

    This function intentionally does not call a provider. A future executor should
    require an allowed preflight result before making network calls.
    """

    selected_plans = [
        plan for plan in plans if int(plan.get("selected_count") or 0) > 0 or _selected_candidate_id_list(plan)
    ]
    selected_documents = len(selected_plans)
    chunk_input_tokens = sum(int(plan.get("estimated_input_tokens") or 0) for plan in selected_plans)
    output_tokens = sum(int(plan.get("estimated_output_tokens") or 0) for plan in selected_plans)
    planned_total_tokens = sum(int(plan.get("estimated_total_tokens") or 0) for plan in selected_plans)
    errors: list[str] = []
    max_documents = max(0, int(settings.agent_review_max_documents_per_batch))
    max_input_tokens = max(0, int(settings.agent_review_max_input_tokens_per_batch))
    max_total_tokens = max(0, int(settings.agent_review_max_total_tokens_per_batch))
    max_cost = _decimal_setting(settings.agent_review_max_cost_per_batch)
    input_price = _decimal_setting(settings.agent_review_input_price_per_1m_tokens)
    output_price = _decimal_setting(settings.agent_review_output_price_per_1m_tokens)
    prompt_envelope = _resolve_prompt_envelope(
        settings,
        prompt=prompt,
        prompt_hash_value=prompt_hash_value,
        prompt_input_tokens=prompt_input_tokens,
    )
    if selected_documents:
        errors.extend(prompt_envelope["errors"])
    resolved_prompt_tokens = int(prompt_envelope["prompt_input_tokens"]) if selected_documents else 0
    input_tokens = chunk_input_tokens + resolved_prompt_tokens
    total_tokens = max(planned_total_tokens + resolved_prompt_tokens, _with_safety_margin(input_tokens + output_tokens, settings))
    input_cost = _token_cost(input_tokens, input_price)
    output_cost = _token_cost(output_tokens, output_price)
    total_cost = _money(input_cost + output_cost)
    provider = normalize_agent_review_provider(settings.llm_provider)
    model = approved_model or settings.agent_review_model
    selected_chunk_ids = _selected_chunk_ids(selected_plans)
    selected_content_hashes = _selected_content_hashes_by_chunk_id(selected_plans)

    if selected_documents > 0 and not prompt_envelope["prompt_hash"]:
        errors.append("prompt or prompt_hash_value must be set before provider execution.")
    if selected_documents > 0 and resolved_prompt_tokens <= 0:
        errors.append("prompt_input_tokens must be positive before provider execution.")
    for index, plan in enumerate(selected_plans):
        candidate_ids = _selected_candidate_id_list(plan)
        selected_count = int(plan.get("selected_count") or 0)
        if selected_count > 0 and not candidate_ids:
            errors.append(f"Review plan {index} has selected_count but no selected candidate chunk ids.")
        if selected_count != len(candidate_ids):
            errors.append(
                f"Review plan {index} selected_count {selected_count} does not match "
                f"{len(candidate_ids)} selected candidate chunk ids."
            )
        if len(candidate_ids) != len(set(candidate_ids)):
            errors.append(f"Review plan {index} contains duplicate selected candidate chunk ids.")
        for candidate_id in candidate_ids:
            content_hash = selected_content_hashes.get(candidate_id)
            if not content_hash:
                errors.append(f"Review plan {index} selected candidate {candidate_id} is missing content_hash.")
            elif not _looks_like_sha256(content_hash):
                errors.append(f"Review plan {index} selected candidate {candidate_id} has invalid content_hash.")

    if selected_documents > 0 and not settings.enable_agent_review:
        errors.append("ENABLE_AGENT_REVIEW must be true before provider execution.")
    for index, plan in enumerate(selected_plans):
        if str(plan.get("status") or "").strip() != "planned":
            errors.append(f"Review plan {index} must have status planned before provider execution.")
    if selected_documents > 0 and provider not in SUPPORTED_AGENT_REVIEW_PROVIDERS:
        errors.append(
            "LLM_PROVIDER must be one of openai, azure-openai, anthropic, or openai-compatible."
        )
    if selected_documents > 0 and provider == "openai" and not agent_review_api_key(settings):
        errors.append("OPENAI_API_KEY must be set before provider execution.")
    if selected_documents > 0 and provider == "azure-openai":
        if not str(settings.azure_openai_endpoint or "").strip():
            errors.append("AZURE_OPENAI_ENDPOINT must be set before provider execution.")
        if not agent_review_api_key(settings):
            errors.append("AZURE_OPENAI_API_KEY must be set before provider execution.")
    if selected_documents > 0 and provider == "anthropic" and not agent_review_api_key(settings):
        errors.append("ANTHROPIC_API_KEY must be set before provider execution.")
    if selected_documents > 0 and provider == "openai-compatible" and not str(
        settings.agent_review_api_base_url or ""
    ).strip():
        errors.append("AGENT_REVIEW_API_BASE_URL must be set before provider execution.")
    if selected_documents > 0 and not actor:
        errors.append("actor must be set before provider execution.")
    if selected_documents > 0 and not approval_reference:
        errors.append("approval_reference must be set before provider execution.")
    if selected_documents > 0 and not model:
        errors.append("AGENT_REVIEW_MODEL or approved_model must be set before provider execution.")
    if selected_documents > 0 and not settings.agent_review_price_version:
        errors.append("AGENT_REVIEW_PRICE_VERSION must be set before provider execution.")
    if selected_documents > 0 and not settings.agent_review_price_effective_at:
        errors.append("AGENT_REVIEW_PRICE_EFFECTIVE_AT must be set before provider execution.")
    if selected_documents > 0 and max_documents <= 0:
        errors.append("AGENT_REVIEW_MAX_DOCUMENTS_PER_BATCH must be set before provider execution.")
    elif max_documents > 0 and selected_documents > max_documents:
        errors.append(f"Selected {selected_documents} documents above provider execution cap {max_documents}.")
    if input_tokens > 0 and max_input_tokens <= 0:
        errors.append("AGENT_REVIEW_MAX_INPUT_TOKENS_PER_BATCH must be set before provider execution.")
    elif max_input_tokens > 0 and input_tokens > max_input_tokens:
        errors.append(f"Selected {input_tokens} input tokens above provider execution cap {max_input_tokens}.")
    if total_tokens > 0 and max_total_tokens <= 0:
        errors.append("AGENT_REVIEW_MAX_TOTAL_TOKENS_PER_BATCH must be set before provider execution.")
    elif max_total_tokens > 0 and total_tokens > max_total_tokens:
        errors.append(f"Selected {total_tokens} total tokens above provider execution cap {max_total_tokens}.")
    if selected_documents > 0 and max_cost <= 0:
        errors.append("AGENT_REVIEW_MAX_COST_PER_BATCH must be set before provider execution.")
    if input_tokens > 0 and input_price <= 0:
        errors.append("AGENT_REVIEW_INPUT_PRICE_PER_1M_TOKENS must be set before provider execution.")
    if output_tokens > 0 and output_price <= 0:
        errors.append("AGENT_REVIEW_OUTPUT_PRICE_PER_1M_TOKENS must be set before provider execution.")
    if max_cost > 0 and total_cost > max_cost:
        errors.append(f"Estimated provider cost {total_cost} exceeds batch cost cap {max_cost}.")

    return {
        "reservation_id": f"agent_budget_{uuid4().hex[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "approved_model": model,
        "actor": actor or "",
        "approval_reference": approval_reference or "",
        "mode": "pre_call_budget_reservation",
        "allowed": not errors,
        "errors": errors,
        "selected_chunk_ids": selected_chunk_ids,
        "selected_content_hashes": selected_content_hashes,
        "selected_chunk_count": len(selected_chunk_ids),
        "selected_documents": selected_documents,
        "prompt_hash": prompt_envelope["prompt_hash"] if selected_documents else "",
        "prompt_input_tokens": resolved_prompt_tokens,
        "chunk_input_tokens": chunk_input_tokens,
        "chars_per_token": max(1, int(settings.agent_review_chars_per_token)),
        "token_safety_margin": max(1.0, float(settings.agent_review_token_safety_margin)),
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_total_tokens": total_tokens,
        "currency": settings.agent_review_budget_currency,
        "price_version": settings.agent_review_price_version,
        "price_effective_at": settings.agent_review_price_effective_at,
        "input_price_per_1m_tokens": _decimal_to_string(input_price),
        "output_price_per_1m_tokens": _decimal_to_string(output_price),
        "estimated_input_cost": _decimal_to_string(input_cost),
        "estimated_output_cost": _decimal_to_string(output_cost),
        "estimated_total_cost": _decimal_to_string(total_cost),
        "max_cost_per_batch": _decimal_to_string(max_cost),
        "api_call_count": 0,
    }


def _decimal_setting(value: float | int | str) -> Decimal:
    return Decimal(str(value))


def _token_cost(tokens: int, price_per_1m_tokens: Decimal) -> Decimal:
    return _money(Decimal(max(0, tokens)) / MILLION * price_per_1m_tokens)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), ROUND_HALF_UP)


def _decimal_to_string(value: Decimal) -> str:
    return format(value.normalize(), "f")


def build_minimal_provider_payload(
    plan: dict[str, Any],
    chunks: list[Chunk],
    *,
    budget_reservation: dict[str, Any],
) -> dict[str, Any]:
    """Build a source-minimized payload for a future provider executor."""

    if not budget_reservation.get("allowed"):
        raise ValueError("Agent review provider payload requires an allowed budget reservation.")
    selected_candidate_ids = _selected_candidate_id_list(plan)
    selected_count = int(plan.get("selected_count") or 0)
    if selected_count > 0 and not selected_candidate_ids:
        raise ValueError("Agent review provider payload requires selected candidate chunk ids.")
    if selected_count != len(selected_candidate_ids):
        raise ValueError("Agent review provider payload selected_count does not match selected candidate chunk ids.")
    if len(selected_candidate_ids) != len(set(selected_candidate_ids)):
        raise ValueError("Agent review provider payload contains duplicate selected candidate chunk ids.")

    allowed_ids = set(str(chunk_id) for chunk_id in budget_reservation.get("selected_chunk_ids", []) or [])
    unreserved_ids = set(selected_candidate_ids) - allowed_ids
    if unreserved_ids:
        raise ValueError(
            "Provider payload contains chunks outside the budget reservation: "
            + ", ".join(sorted(unreserved_ids))
        )

    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    missing_chunks = set(selected_candidate_ids) - set(chunks_by_id)
    if missing_chunks:
        raise ValueError(
            "Provider payload is missing selected chunks from the provided chunk list: "
            + ", ".join(sorted(missing_chunks))
        )

    items: list[dict[str, Any]] = []
    payload_input_tokens = 0
    chars_per_token = max(1, int(budget_reservation.get("chars_per_token") or 4))
    for candidate in plan.get("selected_candidates", []) or []:
        chunk_id = str(candidate.get("chunk_id"))
        if chunk_id not in selected_candidate_ids:
            continue
        chunk = chunks_by_id[chunk_id]
        text = chunk.normalized_text or chunk.text
        reasons = candidate.get("reasons", [])
        content_hash = agent_review_content_hash(chunk_type=chunk.chunk_type, text=text, reasons=reasons)
        expected_candidate_hash = str(candidate.get("content_hash") or "").strip()
        if expected_candidate_hash and expected_candidate_hash != content_hash:
            raise ValueError("Provider payload selected chunk content hash does not match the review plan.")
        payload_input_tokens += _estimate_tokens(text, chars_per_token)
        items.append(
            {
                "chunk_id": chunk.chunk_id,
                "chunk_type": chunk.chunk_type,
                "source_page_start": chunk.source_page_start,
                "source_page_end": chunk.source_page_end,
                "reasons": reasons,
                "content_hash": content_hash,
                "text": text,
            }
        )
    payload = {
        "mode": "minimal_provider_payload",
        "budget_reservation_id": budget_reservation.get("reservation_id"),
        "source_metadata_included": False,
        "text_basis": "normalized_text",
        "item_count": len(items),
        "payload_input_tokens": payload_input_tokens,
        "estimated_total_input_tokens_with_prompt": int(budget_reservation.get("prompt_input_tokens") or 0)
        + payload_input_tokens,
        "items": items,
    }
    _validate_selected_chunks_against_reservation(payload, budget_reservation)
    _validate_payload_content_hashes_against_reservation(payload, budget_reservation)
    _validate_payload_token_envelope(payload, budget_reservation)
    payload["payload_hash"] = payload_hash(payload)
    return payload


def validate_provider_execution_request(
    *,
    budget_reservation: dict[str, Any],
    payload: dict[str, Any],
    provider: str,
    model: str,
    prompt: str | None = None,
    prompt_hash_value: str | None = None,
    prompt_input_tokens: int | None = None,
) -> None:
    if not budget_reservation.get("allowed"):
        raise ValueError("Provider execution requires an allowed budget reservation.")
    if payload.get("budget_reservation_id") != budget_reservation.get("reservation_id"):
        raise ValueError("Provider payload budget reservation id does not match the reservation.")
    if provider != budget_reservation.get("provider"):
        raise ValueError("Provider does not match the approved budget reservation.")
    if model != budget_reservation.get("approved_model"):
        raise ValueError("Model does not match the approved budget reservation.")
    _validate_prompt_against_reservation(
        budget_reservation,
        prompt=prompt,
        prompt_hash_value=prompt_hash_value,
        prompt_input_tokens=prompt_input_tokens,
    )
    expected_hash = payload_hash(payload)
    if payload.get("payload_hash") != expected_hash:
        raise ValueError("Provider payload hash does not match the payload content.")
    _validate_selected_chunks_against_reservation(payload, budget_reservation)
    _validate_payload_content_hashes_against_reservation(payload, budget_reservation)
    _validate_payload_token_envelope(payload, budget_reservation)


def payload_hash(payload: dict[str, Any]) -> str:
    payload_without_hash = {key: value for key, value in payload.items() if key != "payload_hash"}
    canonical = json.dumps(payload_without_hash, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prompt_hash(prompt: str) -> str:
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _resolve_prompt_envelope(
    settings: Settings,
    *,
    prompt: str | None,
    prompt_hash_value: str | None,
    prompt_input_tokens: int | None,
) -> dict[str, Any]:
    errors: list[str] = []
    chars_per_token = max(1, int(settings.agent_review_chars_per_token))
    if prompt is not None:
        resolved_hash = prompt_hash(prompt)
        resolved_tokens = _estimate_tokens(prompt, chars_per_token)
        if prompt_hash_value and prompt_hash_value != resolved_hash:
            errors.append("prompt_hash_value does not match the provided prompt.")
        if prompt_input_tokens is not None and int(prompt_input_tokens) != resolved_tokens:
            errors.append("prompt_input_tokens does not match the provided prompt.")
        return {"prompt_hash": resolved_hash, "prompt_input_tokens": resolved_tokens, "errors": errors}
    if prompt_hash_value and not _looks_like_sha256(prompt_hash_value):
        errors.append("prompt_hash_value must be a sha256: hash.")
    if prompt_input_tokens is not None and int(prompt_input_tokens) < 0:
        errors.append("prompt_input_tokens must be non-negative.")
    return {
        "prompt_hash": prompt_hash_value or "",
        "prompt_input_tokens": int(prompt_input_tokens or 0),
        "errors": errors,
    }


def _looks_like_sha256(value: str) -> bool:
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and value.startswith("sha256:") and all(char in "0123456789abcdef" for char in digest.lower())


def _estimate_tokens(text: str | None, chars_per_token: int) -> int:
    if not text:
        return 0
    return max(1, (len(text) + chars_per_token - 1) // chars_per_token)


def _with_safety_margin(tokens: int, settings: Settings) -> int:
    margin = max(1.0, float(settings.agent_review_token_safety_margin))
    return int((tokens * margin) + 0.999999)


def _selected_chunk_ids(plans: list[dict[str, Any]]) -> list[str]:
    chunk_ids: set[str] = set()
    for plan in plans:
        chunk_ids.update(_selected_candidate_id_list(plan))
    return sorted(chunk_ids)


def _selected_candidate_id_list(plan: dict[str, Any]) -> list[str]:
    return [
        str(candidate.get("chunk_id"))
        for candidate in plan.get("selected_candidates", []) or []
        if candidate.get("chunk_id")
    ]


def _selected_content_hashes_by_chunk_id(plans: list[dict[str, Any]]) -> dict[str, str]:
    content_hashes: dict[str, str] = {}
    for plan in plans:
        for candidate in plan.get("selected_candidates", []) or []:
            chunk_id = str(candidate.get("chunk_id") or "").strip()
            content_hash = str(candidate.get("content_hash") or "").strip()
            if chunk_id and content_hash and chunk_id not in content_hashes:
                content_hashes[chunk_id] = content_hash
    return content_hashes


def _validate_selected_chunks_against_reservation(payload: dict[str, Any], budget_reservation: dict[str, Any]) -> None:
    allowed_ids = set(str(chunk_id) for chunk_id in budget_reservation.get("selected_chunk_ids", []) or [])
    payload_id_list = [str(item.get("chunk_id")) for item in payload.get("items", []) or [] if item.get("chunk_id")]
    payload_ids = set(payload_id_list)
    if allowed_ids and not payload_ids:
        raise ValueError("Provider payload must include at least one reserved chunk.")
    if len(payload_id_list) != len(payload_ids):
        raise ValueError("Provider payload contains duplicate chunk ids.")
    unexpected = payload_ids - allowed_ids
    if unexpected:
        raise ValueError(f"Provider payload contains chunks outside the budget reservation: {', '.join(sorted(unexpected))}")


def _validate_payload_content_hashes_against_reservation(payload: dict[str, Any], budget_reservation: dict[str, Any]) -> None:
    reserved_hashes = budget_reservation.get("selected_content_hashes")
    if not isinstance(reserved_hashes, dict):
        raise ValueError("Budget reservation must include selected content hashes.")
    payload_hashes = {
        str(item.get("chunk_id")): str(item.get("content_hash") or "")
        for item in payload.get("items", []) or []
        if item.get("chunk_id")
    }
    for chunk_id, expected_hash in reserved_hashes.items():
        if chunk_id not in payload_hashes:
            continue
        if payload_hashes[chunk_id] != expected_hash:
            raise ValueError("Provider payload content hash does not match the budget reservation.")


def _validate_payload_token_envelope(payload: dict[str, Any], budget_reservation: dict[str, Any]) -> None:
    payload_input_tokens = int(payload.get("payload_input_tokens") or 0)
    reserved_chunk_tokens = int(budget_reservation.get("chunk_input_tokens") or 0)
    reserved_prompt_tokens = int(budget_reservation.get("prompt_input_tokens") or 0)
    reserved_input_tokens = int(budget_reservation.get("estimated_input_tokens") or 0)
    if payload_input_tokens > reserved_chunk_tokens:
        raise ValueError("Provider payload input tokens exceed the reserved chunk input tokens.")
    if payload_input_tokens + reserved_prompt_tokens > reserved_input_tokens:
        raise ValueError("Provider payload and prompt input tokens exceed the reserved input tokens.")


def _validate_prompt_against_reservation(
    budget_reservation: dict[str, Any],
    *,
    prompt: str | None,
    prompt_hash_value: str | None,
    prompt_input_tokens: int | None,
) -> None:
    reserved_hash = budget_reservation.get("prompt_hash") or ""
    if not reserved_hash:
        return
    chars_per_token = max(1, int(budget_reservation.get("chars_per_token") or 4))
    if prompt is not None:
        resolved_hash = prompt_hash(prompt)
        resolved_tokens = _estimate_tokens(prompt, chars_per_token)
    else:
        if not prompt_hash_value:
            raise ValueError("Provider execution requires the approved prompt or prompt hash.")
        resolved_hash = prompt_hash_value
        if prompt_input_tokens is None:
            raise ValueError("Provider execution requires prompt_input_tokens.")
        resolved_tokens = int(prompt_input_tokens)
    if resolved_hash != reserved_hash:
        raise ValueError("Prompt hash does not match the approved budget reservation.")
    if resolved_tokens != int(budget_reservation.get("prompt_input_tokens") or 0):
        raise ValueError("Prompt input tokens do not match the approved budget reservation.")
