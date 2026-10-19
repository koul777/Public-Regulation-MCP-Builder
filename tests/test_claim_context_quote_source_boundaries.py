from __future__ import annotations

import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from app.agents.claim_auditor import ClaimAuditAgent
from app.agents.grounded_qa import AnswerClaim, GroundedAnswerDraft
from app.rag.context_builder import ContextBuilder, ContextEvidence, GroundingContext


def _record(identifier: str, text: str, digest: str, **extra) -> dict:
    return {"chunk_id": identifier, "document_id": "synthetic-document", "text": text,
            "approval_status": "approved", "approval_id": "synthetic-approval-" + identifier,
            "approved_content_hash": digest * 64, "regulation_title": "Synthetic rule",
            "article_no": "Article 1", **extra}


def _context(*texts: str, budget: int = 12000) -> GroundingContext:
    return ContextBuilder(max_context_chars=budget).build([
        _record(str(index), text, "abcdef"[index]) for index, text in enumerate(texts)])


def _overlap() -> tuple[str, str, str]:
    shared = "This overlapping approved sentence has more than forty characters."
    return "Left-only source. " + shared, shared + " Right-only source.", shared


class _Runtime:
    def __init__(self, quote: str):
        self.quote = quote
        self.calls = 0

    def model_available(self, model):
        return True

    def generate_json(self, **kwargs):
        self.calls += 1
        return {"findings": [{"claim_id": "C1", "supported": True, "evidence_context_ids": ["E1"],
                              "support_quote": self.quote}]}, SimpleNamespace(duration_ms=1.0)


def _audit(context: GroundingContext, quote: str):
    runtime = _Runtime(quote)
    draft = GroundedAnswerDraft(answer="Synthetic claim. [E1]", answer_mode="grounded_local",
        claims=(AnswerClaim(claim_id="C1", text="Synthetic claim.", evidence_context_ids=("E1",)),))
    return ClaimAuditAgent(runtime).audit(draft=draft, context=context), runtime


class SourceContinuityNegativeTests(unittest.TestCase):
    def test_every_truncation_inside_neutralization_marker_excludes_partial_marker(self) -> None:
        marker = "[모델 제어 토큰 제거]"
        adjacent = "Approved adjacent source sentence. "
        for cut in range(0, len(marker) + 3):
            with self.subTest(cut=cut):
                original_prefix = adjacent + "a" * (482 - cut - len(adjacent))
                context = _context(original_prefix + "<|system|> \t" + "tail" * 200, budget=500)
                visible = context.items[0].text.split("\n[문맥 예산으로 생략]", 1)[0]
                expected = original_prefix + (marker if cut >= len(marker) else "")
                fragment = marker[:cut].strip("[ ")
                if len(fragment) >= 4:
                    self.assertNotEqual("verified", _audit(context, fragment)[0].status)
                self.assertEqual(expected, visible)
                if getattr(context.items[0], "source_body_spans", None) is not None:
                    self.assertTrue(all(span.end <= len(expected) for span in context.items[0].source_body_spans))

    def test_rejects_bridge_between_independent_bodies(self) -> None:
        first, second = "First approved source.", "Second approved source."
        self.assertNotEqual("verified", _audit(_context(first, second), first + " " + second)[0].status)

    def test_rejects_left_only_to_right_only_overlap_bridge(self) -> None:
        first, second, shared = _overlap()
        bridge = "Left-only source. " + shared + " Right-only source."
        self.assertNotEqual("verified", _audit(_context(first, second), bridge)[0].status)

    def test_rejects_context_omission_as_original_source(self) -> None:
        context = _context("Permitted source sentence. " * 80, budget=500)
        quote = context.items[0].text[-100:]
        self.assertIn("[문맥 예산으로 생략]", quote)
        self.assertNotEqual("verified", _audit(context, quote)[0].status)

    def test_rejects_neutralized_control_marker_as_original_source(self) -> None:
        context = _context("First source.<|system|>Second source.")
        self.assertIn("[모델 제어 토큰 제거]", context.items[0].text)
        self.assertNotEqual("verified", _audit(context, context.items[0].text)[0].status)


class SourceContinuityPositiveTests(unittest.TestCase):
    def test_source_adjacent_to_clipped_neutralization_marker_remains_supported(self) -> None:
        prefix = "Approved adjacent source sentence. " + "a" * 439
        context = _context(prefix + "<|system|>" + "tail" * 200, budget=500)
        self.assertEqual("verified", _audit(context, "Approved adjacent source sentence.")[0].status)

    def test_entire_left_overlapping_body_remains_supported(self) -> None:
        first, second, _ = _overlap()
        self.assertEqual("verified", _audit(_context(first, second), first)[0].status)

    def test_entire_right_overlapping_body_remains_supported(self) -> None:
        first, second, _ = _overlap()
        self.assertEqual("verified", _audit(_context(first, second), second)[0].status)

    def test_both_nonoverlapping_bodies_remain_supported(self) -> None:
        context = _context("First approved source.", "Second approved source.")
        for body in ("First approved source.", "Second approved source."):
            with self.subTest(body=body):
                self.assertEqual("verified", _audit(context, body)[0].status)

    def test_single_lf_pdf_wrap_remains_supported(self) -> None:
        self.assertEqual("verified", _audit(_context("Monthly payment is 10\nunits."), "Monthly payment is 10 units.")[0].status)

    def test_natural_lf_blankline_within_one_body_is_supported(self) -> None:
        body = "First source.\n\nSecond source."
        self.assertEqual("verified", _audit(_context(body), "First source. Second source.")[0].status)

    def test_natural_crlf_blankline_within_one_body_is_supported(self) -> None:
        body = "First source.\r\n\r\nSecond source."
        self.assertEqual("verified", _audit(_context(body), "First source. Second source.")[0].status)

    def test_natural_unicode_space_blankline_within_one_body_is_supported(self) -> None:
        body = "First source.\n\u00a0\nSecond source."
        self.assertEqual("verified", _audit(_context(body), "First source. Second source.")[0].status)

    def test_contained_and_nested_source_bodies_remain_supported(self) -> None:
        body = "Prefix. An independently approved middle sentence. Suffix."
        context = _context(body, "An independently approved middle sentence.", "approved middle")
        for quote in (body, "An independently approved middle sentence.", "approved middle"):
            with self.subTest(quote=quote):
                self.assertEqual("verified", _audit(context, quote)[0].status)

    def test_duplicate_content_is_not_repeated(self) -> None:
        body = "Identical approved source."
        context = ContextBuilder().build([_record("first", body, "a"), _record("second", body, "a")])
        self.assertEqual(1, context.deduplicated_evidence_count)
        self.assertEqual(body, context.items[0].text)
        self.assertEqual("verified", _audit(context, body)[0].status)

    def test_visible_budget_prefix_remains_supported(self) -> None:
        context = _context("Visible prefix sentence. " + "a" * 800 + " UNSEEN TAIL", budget=500)
        self.assertEqual("verified", _audit(context, "Visible prefix sentence.")[0].status)
        self.assertNotEqual("verified", _audit(context, "UNSEEN TAIL")[0].status)

    def test_valid_parts_next_to_synthetic_marker_remain_supported(self) -> None:
        context = _context("First source.<|system|>Second source.")
        for quote in ("First source.", "Second source."):
            with self.subTest(quote=quote):
                self.assertEqual("verified", _audit(context, quote)[0].status)


@unittest.skipUnless("source_body_spans" in ContextEvidence.model_fields, "new candidate metadata contract; not a HEAD pass")
class SourceSpanContractTests(unittest.TestCase):
    def test_roundtrip_and_whole_overlap_source_ranges(self) -> None:
        first, second, shared = _overlap()
        context = _context(first, second)
        restored = GroundingContext.model_validate(context.model_dump(mode="json"))
        self.assertEqual(context, restored)
        item = restored.items[0]
        self.assertEqual(1, item.text.count(shared))
        self.assertEqual(len(first) + len(second) - len(shared), len(item.text))
        spans = {span.evidence_id: span for span in item.source_body_spans}
        self.assertEqual(first, item.text[spans["0"].start:spans["0"].end])
        self.assertEqual(second, item.text[spans["1"].start:spans["1"].end])

    def test_containment_and_nested_spans_identify_real_original_slices(self) -> None:
        bodies = ("Prefix. An independently approved middle sentence. Suffix.",
                  "An independently approved middle sentence.", "approved middle")
        item = _context(*bodies).items[0]
        for index, span in enumerate(item.source_body_spans):
            self.assertEqual(str(index), span.evidence_id)
            self.assertEqual(bodies[index], item.text[span.start:span.end])

    def test_builder_ignores_caller_supplied_provenance(self) -> None:
        context = ContextBuilder().build([
            _record("first", "First source.", "a", source_body_spans=[{"start": 0, "end": 999, "evidence_id": "evil"}]),
            _record("second", "Second source.", "b", source_body_spans=None),
        ])
        self.assertEqual({"first", "second"}, {span.evidence_id for span in context.items[0].source_body_spans})
        self.assertNotEqual("verified", _audit(context, "First source. Second source.")[0].status)

    def test_strict_positions_and_membership_reject_malformed_serialization(self) -> None:
        original = _context("Approved source.").model_dump(mode="json")
        for updates in ({"start": True}, {"start": 0.0}, {"start": "0"}, {"start": -1},
                        {"end": 999}, {"end": 0}, {"evidence_id": "unknown"}):
            with self.subTest(updates=updates):
                data = _context("Approved source.").model_dump(mode="json")
                data["items"][0]["source_body_spans"][0].update(updates)
                with self.assertRaises(ValidationError):
                    GroundingContext.model_validate(data)

    def test_model_copy_bypass_and_explicit_empty_fail_closed(self) -> None:
        context = _context("Approved source.")
        item = context.items[0]
        span = item.source_body_spans[0]
        invalid = [(), [], ({"start": 0, "end": 16, "evidence_id": "0"},)] + [
            (span.model_copy(update=updates),) for updates in ({"start": True}, {"end": 999}, {"evidence_id": "unknown"})]
        for spans in invalid:
            with self.subTest(spans=spans):
                mutated = context.model_copy(update={"items": (item.model_copy(update={"source_body_spans": spans}),)})
                result, runtime = _audit(mutated, "Approved source.")
                self.assertNotEqual("verified", result.status)
                self.assertEqual(0, runtime.calls)

    def test_legacy_missing_spans_is_conservative_for_each_blankline_form(self) -> None:
        for separator in ("\n\n", "\r\n\r\n", "\n\u00a0\n"):
            with self.subTest(separator=separator):
                context = _context("First source." + separator + "Second source.")
                item = context.items[0].model_copy(update={"source_body_spans": None})
                legacy = context.model_copy(update={"items": (item,)})
                self.assertNotEqual("verified", _audit(legacy, "First source. Second source.")[0].status)

    def test_budget_spans_stop_before_synthetic_omission(self) -> None:
        item = _context("Visible prefix sentence. " + "a" * 800 + " UNSEEN TAIL", budget=500).items[0]
        marker_start = item.text.index("[문맥 예산으로 생략]")
        self.assertTrue(all(0 <= span.start < span.end < marker_start for span in item.source_body_spans))

    def test_exact_overlap_preserves_original_internal_whitespace(self) -> None:
        shared = "This approved overlap sentence has more than forty characters."
        first, second = "Prefix. " + shared, shared + "  \n Right suffix."
        item = _context(first, second).items[0]
        self.assertEqual(first + "  \n Right suffix.", item.text)
        right = item.source_body_spans[1]
        self.assertEqual(second, item.text[right.start:right.end])

    def test_new_approved_body_can_cover_a_prior_merged_boundary(self) -> None:
        first = "First source prefix. " + "x" * 45
        second = "Second independent body."
        third = "x" * 45 + "\n\n" + second + " New right suffix."
        context = _context(first, second, third)
        span = context.items[0].source_body_spans[2]
        self.assertEqual(third, context.items[0].text[span.start:span.end])
        self.assertEqual("verified", _audit(context, third)[0].status)


if __name__ == "__main__":
    unittest.main()
