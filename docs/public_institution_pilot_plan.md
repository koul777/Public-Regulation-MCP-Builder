# Public Institution Source-Only Pilot Plan

This public document describes a source-only evaluation plan. It is different from asking an AI directly because it measures deterministic preprocessing, explicit review flags, approval provenance, and evidence-grounded retrieval.

## Pilot Scope

- Use 20 to 100 synthetic or redistributable sample documents.
- Measure article, paragraph, table, appendix, citation, and metadata extraction.
- Keep OCR-required files visible as review candidates.
- Keep Streamlit local-only and treat shared deployment as a separate protected configuration.
- Treat legacy repository records without `tenant_id` as migration candidates, not as silently accepted data.
- Official indexing starts only after human review, approval, and approval-journal validation.

## Public Evidence

Use only redacted or synthetic evidence:

- `public_batch_readiness_*.json/.md`
- `public_batch_quality_*.json/.md`
- public release audit, cleanup plan, test, build, and fresh-clone reports
- review-batch and approval-journal schema examples with synthetic identifiers

Raw `batch_quality_*` exports, source uploads, runtime databases, and institution identifiers are not pilot evidence for this public repository.

## Acceptance Criteria

- `average_quality_score >= 98` for the declared synthetic pilot set.
- `failed_count = 0` and `ocr_required_count = 0` for the release candidate, or every exception is explicitly listed as a review flag.
- `api_call_count=0` for deterministic local validation unless a separate approved integration test is declared.
- every official RAG/MCP record has human review, approval, citation metadata, and tenant scope.
- the result is an auditable preprocessing and review gateway, not a claim that raw preprocessing output is ready for downstream indexing.

## Release Boundary

The public pilot does not claim institution-specific production readiness, SSO, private deployment evidence, or product-public readiness. Product claims require separate strict parser, temporal metadata, reapproval, and release-evidence verification.
