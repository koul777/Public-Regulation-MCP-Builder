from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from app.agents.review_context import review_context_for_metadata
from app.agents.provider_config import (
    agent_review_configuration_reason,
    agent_review_provider_ready,
)
from app.core.config import Settings
from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.quality import QualityReport


MILLION = Decimal("1000000")
AGENT_REVIEW_CONTENT_HASH_VERSION = "agent-review-content-v2"
AGENT_REVIEW_POLICY_VERSION = "main-parser-ai-review-v1"
HWPX_COMPLEX_STRUCTURE_METADATA_KEYS = (
    "source_hwpx_nested_table_count",
    "source_hwpx_table_image_count",
    "source_hwpx_table_note_count",
    "source_hwpx_merged_cell_count",
)
HWPX_COMPLEX_STRUCTURE_FLAGS = {"nested_table", "table_image", "table_note", "merged_cell"}


def agent_review_content_hash(
    *,
    chunk_type: str,
    text: str | None,
    reasons: list[str],
    review_context: dict | None = None,
) -> str:
    payload = {
        "schema_version": AGENT_REVIEW_CONTENT_HASH_VERSION,
        "chunk_type": chunk_type,
        "reasons": sorted(reasons),
        "review_context": review_context or {},
        "text": text or "",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AgentReviewPolicy:
    """Builds the main parser AI-review draft plan before provider execution."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def plan(
        self,
        chunks: list[Chunk],
        quality_report: QualityReport,
        options: ChunkOptions,
        *,
        cached_content_hashes: set[str] | None = None,
    ) -> dict:
        provider_execution_enabled = self.provider_execution_configured()
        enabled = True
        limits = self._limits()
        cache = set(cached_content_hashes or set())
        cache_scope_hash = self.cache_scope_hash()
        base = {
            "enabled": enabled,
            "pipeline_stage": "parser_ai_review_draft",
            "pipeline_stage_required": True,
            "provider_execution_enabled": provider_execution_enabled,
            "provider_execution_ready": provider_execution_enabled,
            "settings_enabled": bool(self.settings.enable_agent_review),
            "request_enabled": True,
            "provider": self.settings.llm_provider,
            "model": self.settings.agent_review_model,
            "mode": "main_pipeline_review_draft",
            "api_call_count": 0,
            "limits": limits,
            "cache_basis": "content_hash",
            "cache_scope_hash": cache_scope_hash,
            "cached_content_hash_count": len(cache),
            "quality_score": quality_report.score,
            "quality_passed": quality_report.passed,
            "status": "skipped",
            "skip_reason": None,
            "candidate_count": 0,
            "cached_candidate_count": 0,
            "new_candidate_count": 0,
            "selected_count": 0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "estimated_total_tokens": 0,
            "currency": self.settings.agent_review_budget_currency,
            "cost_estimate_status": "not_selected",
            "estimated_input_cost": "0",
            "estimated_output_cost": "0",
            "estimated_cost": "0",
            "price_version": self.settings.agent_review_price_version,
            "price_effective_at": self.settings.agent_review_price_effective_at,
            "review_policy_version": AGENT_REVIEW_POLICY_VERSION,
            "budget_exhausted": False,
            "candidates": [],
            "selected_candidates": [],
        }
        if self._is_clean_quality_report(quality_report):
            candidates = self._candidate_chunks(chunks, cached_content_hashes=cache)
            if not candidates:
                base["skip_reason"] = "quality_gate_clean"
                return base
        else:
            candidates = self._candidate_chunks(chunks, cached_content_hashes=cache)

        base["candidate_count"] = len(candidates)
        base["cached_candidate_count"] = sum(1 for candidate in candidates if candidate.get("cache_status") == "reused")
        base["new_candidate_count"] = sum(1 for candidate in candidates if candidate.get("cache_status") == "new")
        base["candidates"] = candidates
        if not candidates:
            base["skip_reason"] = "no_review_candidates"
            return base
        new_candidates = [candidate for candidate in candidates if candidate.get("cache_status") == "new"]
        if not new_candidates:
            base["skip_reason"] = "review_candidates_cached"
            return base

        selected: list[dict] = []
        token_total = 0
        output_token_total = 0
        for candidate in new_candidates:
            estimated = int(candidate["estimated_input_tokens"])
            if len(selected) >= limits["max_chunks_per_document"]:
                base["budget_exhausted"] = True
                break
            if token_total + estimated > limits["max_input_tokens_per_document"]:
                base["budget_exhausted"] = True
                break
            selected.append(candidate)
            token_total += estimated
            output_token_total += int(candidate["estimated_output_tokens"])

        base.update(
            {
                "status": self._planned_status(selected, provider_execution_enabled),
                "skip_reason": self._planned_skip_reason(selected, provider_execution_enabled),
                "selected_count": len(selected),
                "estimated_input_tokens": token_total,
                "estimated_output_tokens": output_token_total,
                "estimated_total_tokens": self._with_safety_margin(token_total + output_token_total),
                "selected_candidates": selected,
                **self._cost_estimate(token_total, output_token_total),
            }
        )
        return base

    def _planned_status(self, selected: list[dict], provider_execution_enabled: bool) -> str:
        if not selected:
            return "skipped"
        if not provider_execution_enabled:
            return "api_configuration_needed"
        return "planned"

    def _planned_skip_reason(self, selected: list[dict], provider_execution_enabled: bool) -> str | None:
        if not selected:
            return "review_budget_exhausted"
        if not provider_execution_enabled:
            return self._provider_configuration_skip_reason()
        return None

    def provider_execution_configured(self) -> bool:
        return agent_review_provider_ready(self.settings)

    def _provider_configuration_skip_reason(self) -> str:
        return agent_review_configuration_reason(self.settings)

    def _limits(self) -> dict[str, int | float]:
        return {
            "trigger_score_below": self.settings.agent_review_trigger_score_below,
            "max_chunks_per_document": max(0, self.settings.agent_review_max_chunks_per_document),
            "max_input_tokens_per_document": max(0, self.settings.agent_review_max_input_tokens_per_document),
            "max_output_tokens_per_chunk": max(0, self.settings.agent_review_max_output_tokens_per_chunk),
            "token_safety_margin": max(1.0, self.settings.agent_review_token_safety_margin),
            "chars_per_token": max(1, self.settings.agent_review_chars_per_token),
        }

    def cache_scope_hash(self) -> str:
        payload = {
            "policy_version": AGENT_REVIEW_POLICY_VERSION,
            "content_hash_version": AGENT_REVIEW_CONTENT_HASH_VERSION,
            "provider": self.settings.llm_provider,
            "model": self.settings.agent_review_model,
            "trigger_score_below": self.settings.agent_review_trigger_score_below,
            "max_output_tokens_per_chunk": self.settings.agent_review_max_output_tokens_per_chunk,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _is_clean_quality_report(self, report: QualityReport) -> bool:
        return (
            report.passed
            and report.score >= self.settings.agent_review_trigger_score_below
            and report.error_count == 0
            and report.warning_count == 0
            and report.failed_error_check_count == 0
            and report.failed_warning_check_count == 0
        )

    def _candidate_chunks(self, chunks: list[Chunk], *, cached_content_hashes: set[str]) -> list[dict]:
        candidates: list[dict] = []
        for chunk in chunks:
            reasons = self._review_reasons(chunk)
            if not reasons:
                continue
            text = chunk.normalized_text or chunk.text
            review_context = review_context_for_metadata(chunk.metadata or {})
            content_hash = agent_review_content_hash(
                chunk_type=chunk.chunk_type,
                text=text,
                reasons=reasons,
                review_context=review_context,
            )
            candidates.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "chunk_type": chunk.chunk_type,
                    "source_page_start": chunk.source_page_start,
                    "source_page_end": chunk.source_page_end,
                    "reasons": reasons,
                    "content_hash": content_hash,
                    "review_context_hash": self._review_context_hash(review_context),
                    "cache_status": "reused" if content_hash in cached_content_hashes else "new",
                    "estimated_input_tokens": self._estimate_tokens(text),
                    "estimated_output_tokens": max(0, self.settings.agent_review_max_output_tokens_per_chunk),
                    "text_chars": len(text or ""),
                }
            )
        return candidates

    def _review_context_hash(self, review_context: dict) -> str:
        canonical = json.dumps(review_context or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _review_reasons(self, chunk: Chunk) -> list[str]:
        metadata = chunk.metadata or {}
        reasons: list[str] = []
        if chunk.warnings:
            reasons.append("chunk_warnings")
        if "\ufffd" in (chunk.normalized_text or chunk.text):
            reasons.append("replacement_character")
        if metadata.get("table_probable_extraction_failed"):
            reasons.append("table_extraction_failed")
        if metadata.get("table_like") and not metadata.get("table_cell_rows"):
            reasons.append("table_like_without_cell_rows")
        if metadata.get("kordoc_table_match"):
            reasons.append("kordoc_table_match_review")
        if metadata.get("structure_fallback"):
            reasons.append("structure_fallback")
        hwpx_review_flags = set(metadata.get("source_hwpx_parser_review_flags") or [])
        if hwpx_review_flags:
            reasons.append("hwpx_parser_review_flag")
        if hwpx_review_flags.intersection(HWPX_COMPLEX_STRUCTURE_FLAGS) or any(
            self._positive_metadata_int(metadata, key) for key in HWPX_COMPLEX_STRUCTURE_METADATA_KEYS
        ):
            reasons.append("hwpx_complex_structure")
        parser_risk = str(metadata.get("parser_uncertainty_risk_level") or "").strip().lower()
        parser_source = str(metadata.get("parser_uncertainty_source") or "").strip().lower()
        if self._parser_uncertainty_requires_review(metadata, parser_source, parser_risk):
            reasons.append("parser_uncertainty")
        if metadata.get("source_hwp_extraction_modes"):
            reasons.append("hwp_parser_ai_review_required")
        kordoc_inventory = metadata.get("kordoc_table_inventory")
        if isinstance(kordoc_inventory, dict):
            if str(kordoc_inventory.get("status") or "") == "parsed" and self._positive_metadata_int(
                kordoc_inventory,
                "table_count",
            ):
                reasons.append("kordoc_table_structure_review")
            nested_count = 0
            for table in kordoc_inventory.get("tables") or []:
                if isinstance(table, dict):
                    nested_count += int(table.get("nested_table_count") or 0)
            if nested_count > 0:
                reasons.append("kordoc_nested_table_review")
        inventory = metadata.get("document_inventory")
        if isinstance(inventory, dict):
            hierarchy = inventory.get("hierarchy") if isinstance(inventory.get("hierarchy"), dict) else {}
            attachments = inventory.get("attachments") if isinstance(inventory.get("attachments"), dict) else {}
            tables = inventory.get("tables") if isinstance(inventory.get("tables"), dict) else {}
            supplements = inventory.get("supplements") if isinstance(inventory.get("supplements"), dict) else {}
            if any(
                self._positive_metadata_int(source, key)
                for source, key in (
                    (hierarchy, "articles"),
                    (attachments, "total"),
                    (tables, "total"),
                    (supplements, "blocks"),
                )
            ):
                reasons.append("document_inventory_boundary_review")
        return reasons

    def _positive_metadata_int(self, metadata: dict, key: str) -> bool:
        try:
            return int(metadata.get(key) or 0) > 0
        except (TypeError, ValueError):
            return False

    def _parser_uncertainty_requires_review(self, metadata: dict, parser_source: str, parser_risk: str) -> bool:
        if parser_risk not in {"medium", "high", "critical"}:
            return False
        # HWPX already emits chunk-level structural review flags for the impacted
        # tables, captions, notes, and merged cells. Keep the generic parser
        # uncertainty flag for higher-severity cases, but do not duplicate the
        # medium-risk review load across every chunk.
        if parser_source == "hwpx" and parser_risk == "medium":
            return False
        # Legacy HWP and HWPML chunks already get a dedicated review reason via
        # source_hwp_extraction_modes, so the generic parser uncertainty flag is
        # redundant there unless severity is escalated beyond medium.
        if parser_source == "hwp" and parser_risk == "medium" and metadata.get("source_hwp_extraction_modes"):
            return False
        return True

    def _estimate_tokens(self, text: str | None) -> int:
        if not text:
            return 0
        chars_per_token = max(1, self.settings.agent_review_chars_per_token)
        return max(1, (len(text) + chars_per_token - 1) // chars_per_token)

    def _with_safety_margin(self, tokens: int) -> int:
        margin = max(1.0, self.settings.agent_review_token_safety_margin)
        return int((tokens * margin) + 0.999999)

    def _cost_estimate(self, input_tokens: int, output_tokens: int) -> dict[str, str]:
        if input_tokens <= 0 and output_tokens <= 0:
            return {
                "cost_estimate_status": "not_selected",
                "estimated_input_cost": "0",
                "estimated_output_cost": "0",
                "estimated_cost": "0",
            }
        input_price = Decimal(str(self.settings.agent_review_input_price_per_1m_tokens))
        output_price = Decimal(str(self.settings.agent_review_output_price_per_1m_tokens))
        if input_price <= 0 or output_price <= 0:
            return {
                "cost_estimate_status": "missing_price",
                "estimated_input_cost": "",
                "estimated_output_cost": "",
                "estimated_cost": "",
            }
        input_cost = self._money(Decimal(max(0, input_tokens)) / MILLION * input_price)
        output_cost = self._money(Decimal(max(0, output_tokens)) / MILLION * output_price)
        return {
            "cost_estimate_status": "estimated",
            "estimated_input_cost": self._decimal_to_string(input_cost),
            "estimated_output_cost": self._decimal_to_string(output_cost),
            "estimated_cost": self._decimal_to_string(self._money(input_cost + output_cost)),
        }

    def _money(self, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.0001"), ROUND_HALF_UP)

    def _decimal_to_string(self, value: Decimal) -> str:
        return format(value.normalize(), "f")
