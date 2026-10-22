from __future__ import annotations

"""Synthetic query specs shaped like the private goldsets (development, holdout, seed).

The real specs have no top-level split. Every query carries its own, and the
top-level keys are the ones below. The content is synthetic.
"""

from typing import Any


def real_shaped_cases(split: str, *, count: int = 2, document_id: str = "synthetic-document") -> list[dict[str, Any]]:
    return [{"id": f"{split}-case-{index}", "query": f"synthetic {split} question {index}",
             "target_document_ids": [document_id], "target_chunk_ids": [f"chunk-{split}-{index}"]}
            for index in range(count)]


def real_shaped_spec(split: str | None, cases: list[dict[str, Any]] | None = None, **top_level: Any) -> dict[str, Any]:
    """Top-level keys of the real specs, no top-level split, and the given split on every query."""
    queries = real_shaped_cases(split or "development") if cases is None else cases
    spec: dict[str, Any] = {
        "schema_version": 1, "label_provenance": "synthetic", "goldset_status": "synthetic_for_tests",
        "human_label_review_complete": False, "product_release_ready": False, "source_document_count": 1,
        "scope": {"tenant_id": "synthetic-tenant", "profile_id": None, "tenant_storage_isolation": False},
        "split_policy": {"unit": "document_group", "holdout_use": "final comparison only"},
        "queries": [{**case, "split": split} for case in queries] if split else queries,
    }
    spec.update(top_level)
    return spec
