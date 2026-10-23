from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from app.agents.claim_auditor import (
    ClaimAuditAgent, _ModelAuditDraft, _audit_prompt, _audit_quote_candidates,
    _bind_model_audit, _quote_is_exact, _validated_quote_map,
)
from app.agents.grounded_qa import (
    AnswerClaim, GroundedAnswerDraft, TABLE_STRUCTURE_REVIEW_REASON,
    _FastModelAnswerDraft, _validated_fast_model_draft,
)
from app.rag.context_builder import ContextBuilder
from app.rag.extractive_answer import NO_EVIDENCE_ANSWER


class _Runtime:
    def __init__(self, findings, *, answer_supported=True, include_answer_supported=True):
        self.findings = findings
        self.answer_supported = answer_supported
        self.include_answer_supported = include_answer_supported
        self.calls = 0
        self.prompt = ""

    def model_available(self, model):
        return True

    def generate_json(self, **kwargs):
        self.calls += 1
        self.prompt = kwargs["prompt"]
        payload = {"findings": self.findings}
        if self.include_answer_supported:
            payload["answer_supported"] = self.answer_supported
        return payload, SimpleNamespace(duration_ms=1.0)


def _context(*texts):
    return ContextBuilder().build([{
        "chunk_id": f"chunk-{index}", "document_id": f"doc-{index}", "text": text,
        "approval_status": "approved", "approval_id": f"approval-{index}",
        "approved_content_hash": str(index) * 64, "regulation_title": f"Synthetic Rule {index}",
        "article_no": "Article 1", "source_page_start": index, "source_page_end": index,
    } for index, text in enumerate(texts, start=1)])


def _merged_context(*texts):
    """Independent approved bodies belonging to one document and article."""
    return ContextBuilder().build([{
        "chunk_id": f"chunk-{index}", "document_id": "synthetic-merged-document", "text": text,
        "approval_status": "approved", "approval_id": f"approval-{index}",
        "approved_content_hash": str(index) * 64, "regulation_title": "Synthetic merged rule",
        "article_no": "Article 1", "source_page_start": index, "source_page_end": index,
    } for index, text in enumerate(texts, start=1)])


def _draft(*evidence_ids):
    ids = evidence_ids or ("E1",)
    return GroundedAnswerDraft(answer="Synthetic supported claim. " + " ".join(f"[{eid}]" for eid in ids),
                               answer_mode="grounded_local", claims=(AnswerClaim(
                                   claim_id="C1", text="Synthetic supported claim.", evidence_context_ids=ids),))


def _finding(**overrides):
    return {"claim_id": "C1", "supported": True, "evidence_context_ids": ["E1"],
            "support_quote_id": "E1Q1", **overrides}


class ClaimAuditorPromptTests(unittest.TestCase):
    def test_undeclared_answer_markers_fail_preflight_and_direct_binding(self) -> None:
        context = _context("First synthetic source.", "Second synthetic source.")
        for marker in ("E2", "E99"):
            with self.subTest(marker=marker):
                draft = _draft().model_copy(update={"answer": f"Synthetic claim. [E1] Extra claim. [{marker}]"})
                runtime = _Runtime([_finding()])
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                direct = _bind_model_audit(draft, context,
                    _ModelAuditDraft.model_validate({"answer_supported": True, "findings": [_finding()]}), 1.0)
                self.assertEqual(0, runtime.calls)
                for value in (result, direct):
                    self.assertEqual("review_required", value.status)
                    self.assertEqual("answer_marker_set_mismatch", value.reason_code)
                    self.assertEqual("deterministic_gate", value.audit_mode)
                    self.assertIsNone(value.model)
                    self.assertEqual((), value.citations)

    def test_direct_binding_preserves_missing_and_unknown_marker_rejections(self) -> None:
        context = _context("First synthetic source.", "Second synthetic source.")
        for draft, reason in (
            (_draft("E1", "E2").model_copy(update={"answer": "Synthetic claim. [E1]"}), "answer_marker_missing"),
            (_draft("E1", "E99"), "unknown_evidence_context_id"),
        ):
            with self.subTest(reason=reason):
                runtime = _Runtime([_finding()])
                public = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                direct = _bind_model_audit(draft, context,
                    _ModelAuditDraft.model_validate({"answer_supported": True, "findings": [_finding()]}), 1.0)
                self.assertEqual(0, runtime.calls)
                for value in (public, direct):
                    self.assertEqual("rejected", value.status)
                    self.assertEqual(reason, value.findings[0].reason_code)
                    self.assertEqual("deterministic_gate", value.audit_mode)
                    self.assertIsNone(value.model)
                    self.assertEqual((), value.citations)

    def test_multiple_claim_markers_preserve_two_exact_citations(self) -> None:
        context = _context("First synthetic source.", "Second synthetic source.")
        draft = GroundedAnswerDraft(answer="First claim. [E1] Second claim. [E2] [E1]",
            answer_mode="grounded_local", claims=(
                AnswerClaim(claim_id="C1", text="First claim.", evidence_context_ids=("E1",)),
                AnswerClaim(claim_id="C2", text="Second claim.", evidence_context_ids=("E2",)),
            ))
        findings = [_finding(), _finding(claim_id="C2", evidence_context_ids=["E2"], support_quote_id="E2Q1")]
        runtime = _Runtime(findings)
        public = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
        direct = _bind_model_audit(draft, context, _ModelAuditDraft.model_validate({"answer_supported": True, "findings": findings}), 1.0)
        self.assertEqual(1, runtime.calls)
        for result in (public, direct):
            self.assertEqual("verified", result.status)
            self.assertEqual(("C1", "C2"), result.verified_claim_ids)
            self.assertEqual({"E1": "First synthetic source.", "E2": "Second synthetic source."},
                             {citation.context_id: citation.support_quote for citation in result.citations})

    def test_direct_binding_preserves_canonical_abstention(self) -> None:
        draft = GroundedAnswerDraft(answer=NO_EVIDENCE_ANSWER, abstained=True, answer_mode="abstained")
        runtime = _Runtime([])
        context = _context("Synthetic source.")
        public = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
        direct = _bind_model_audit(draft, context,
            _ModelAuditDraft.model_validate({"answer_supported": True, "findings": [_finding()]}), 1.0)
        self.assertEqual(0, runtime.calls)
        for result in (public, direct):
            self.assertEqual("abstained", result.status)
            self.assertEqual("abstained", result.audit_mode)
            self.assertEqual((), result.citations)
            self.assertIsNone(result.model)

    def test_unstable_table_is_blocked_before_model_and_in_direct_binding(self) -> None:
        context = _context("Synthetic ambiguous table source.", "Synthetic safe source.")
        risky = context.items[0].model_copy(update={"table_like": True, "table_review_required": True,
                                                  "table_review_flags": ("unstable_column_count",)})
        context = context.model_copy(update={"items": (risky, context.items[1])})
        drafts = (_draft(), _draft("E1", "E2"),
                  _draft("E2").model_copy(update={"answer": "Unsafe marker [E1]. Safe claim [E2]."}))
        for draft in drafts:
            with self.subTest(draft=draft):
                runtime = _Runtime([_finding()])
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                self.assertEqual("review_required", result.status)
                self.assertEqual(TABLE_STRUCTURE_REVIEW_REASON, result.reason_code)
                self.assertEqual("deterministic_gate", result.audit_mode)
                self.assertIsNone(result.model)
                self.assertEqual(0, runtime.calls)
                direct = _bind_model_audit(draft, context,
                    _ModelAuditDraft.model_validate({"answer_supported": True, "findings": [_finding()]}), 1.0)
                self.assertEqual(TABLE_STRUCTURE_REVIEW_REASON, direct.reason_code)
                self.assertEqual("deterministic_gate", direct.audit_mode)
                self.assertIsNone(direct.model)
                self.assertEqual((), direct.citations)

    def test_unreferenced_unstable_table_does_not_block_safe_claim_audit(self) -> None:
        context = _context("Synthetic ambiguous table source.", "Synthetic safe source.")
        risky = context.items[0].model_copy(update={"table_review_required": True,
                                                  "table_review_flags": ("unstable_column_count",)})
        context = context.model_copy(update={"items": (risky, context.items[1])})
        runtime = _Runtime([_finding(evidence_context_ids=["E2"], support_quote_id="E2Q1")])
        result = ClaimAuditAgent(runtime).audit(draft=_draft("E2"), context=context)
        self.assertEqual("verified", result.status)
        self.assertEqual(1, runtime.calls)
        self.assertEqual(("E2",), result.findings[0].evidence_context_ids)

    def test_display_normalizes_line_wraps_without_mutating_approved_source(self) -> None:
        original_text = "합성 근무시간은 30시간\n으로 한다.\n다음 문장도 보존한다."
        context = ContextBuilder().build([{
            "chunk_id": "synthetic-chunk", "document_id": "synthetic-document",
            "text": original_text, "approval_status": "approved",
            "approval_id": "synthetic-approval", "approved_content_hash": "a" * 64,
            "regulation_title": "합성 규정", "article_no": "제1조",
        }])
        original = context.model_dump()
        draft = GroundedAnswerDraft(
            answer="합성 근무시간은 30시간이다. [E1]", answer_mode="grounded_local",
            claims=(AnswerClaim(claim_id="C1", text="합성 근무시간은 30시간이다.",
                                evidence_context_ids=("E1",)),),
        )

        prompt = _audit_prompt(draft, context)

        self.assertIn("합성 근무시간은 30시간 으로 한다. 다음 문장도 보존한다.", prompt)
        self.assertEqual(original, context.model_dump())
        self.assertEqual(original_text, context.items[0].text)
        self.assertTrue(_quote_is_exact("합성 근무시간은 30시간 으로 한다.", original_text))
        self.assertFalse(_quote_is_exact("합성 근무시간은 30시간으로 한다.", original_text))
        self.assertFalse(_quote_is_exact("합성 근무시간은 130시간 으로 한다.", original_text))

    def test_model_selects_exact_source_span_without_rewriting_wrapped_words(self) -> None:
        source = "합성 통\n상임금은 매월 지급한다."
        context = _context(source, "Unrelated evidence must not enter the audit prompt.")
        before = context.model_dump()
        runtime = _Runtime([_finding()])

        result = ClaimAuditAgent(runtime).audit(draft=_draft(), context=context)

        self.assertEqual("verified", result.status)
        self.assertEqual("합성 통 상임금은 매월 지급한다.", result.citations[0].support_quote)
        self.assertEqual(("approval-1",), result.citations[0].approval_ids)
        self.assertEqual(("1" * 64,), result.citations[0].content_hashes)
        self.assertEqual(before, context.model_dump())
        self.assertNotIn("Unrelated evidence", runtime.prompt)
        self.assertNotIn("Synthetic Rule 2", runtime.prompt)

    def test_valid_quote_id_never_overrides_model_unsupported_verdict(self) -> None:
        result = ClaimAuditAgent(_Runtime([_finding(supported=False)])).audit(
            draft=_draft(), context=_context("Synthetic exact source."))
        self.assertEqual("rejected", result.status)
        self.assertEqual("unsupported", result.findings[0].status)
        self.assertEqual((), result.citations)

    def test_invalid_present_reference_never_falls_back_to_valid_free_quote(self) -> None:
        source = "Synthetic exact source."
        for reference in (None, "", "E1Q999", "E2Q1", " E1Q1"):
            with self.subTest(reference=reference):
                runtime = _Runtime([_finding(support_quote_id=reference, support_quote=source)])
                result = ClaimAuditAgent(runtime).audit(draft=_draft(), context=_context(source, source))
                self.assertEqual("review_required", result.status)
                self.assertEqual("model_quote_reference_invalid", result.reason_code)

    def test_reference_and_conflicting_free_quote_are_rejected(self) -> None:
        result = ClaimAuditAgent(_Runtime([_finding(support_quote="Different asserted source.")])).audit(
            draft=_draft(), context=_context("Synthetic exact source."))
        self.assertEqual("model_quote_reference_conflict", result.reason_code)

    def test_legacy_free_quote_still_requires_exact_source(self) -> None:
        source = "합성 통\n상임금은 매월 지급한다."
        for quote, expected in (("합성 통 상임금은 매월 지급한다.", "verified"),
                                ("합성 통상임금은 매월 지급한다.", "review_required")):
            finding = _finding(support_quote=quote)
            finding.pop("support_quote_id")
            with self.subTest(quote=quote):
                self.assertEqual(expected, ClaimAuditAgent(_Runtime([finding])).audit(
                    draft=_draft(), context=_context(source)).status)

    def test_claim_findings_must_be_exactly_one_per_known_claim(self) -> None:
        context = _context("Synthetic exact source.")
        for findings in ([_finding(), _finding()], [_finding(claim_id="C2")]):
            with self.subTest(findings=findings):
                result = ClaimAuditAgent(_Runtime(findings)).audit(draft=_draft(), context=context)
                self.assertEqual("model_claim_set_mismatch", result.reason_code)
        two_claims = _draft().model_copy(update={"claims": _draft().claims + (
            AnswerClaim(claim_id="C2", text="Second synthetic claim.", evidence_context_ids=("E1",)),)})
        result = ClaimAuditAgent(_Runtime([_finding()])).audit(draft=two_claims, context=context)
        self.assertEqual("model_claim_set_mismatch", result.reason_code)

    def test_duplicate_context_identity_fails_before_model_call(self) -> None:
        context = _context("Synthetic exact source.")
        context = context.model_copy(update={"items": context.items + context.items})
        runtime = _Runtime([_finding()])
        result = ClaimAuditAgent(runtime).audit(draft=_draft(), context=context)
        self.assertEqual("context_identity_collision", result.reason_code)
        self.assertEqual(0, runtime.calls)
        self.assertEqual("deterministic_gate", result.audit_mode)
        self.assertIsNone(result.model)

    def test_duplicate_claim_identity_fails_without_claiming_model_audit(self) -> None:
        draft = _draft()
        draft = draft.model_copy(update={"claims": draft.claims + draft.claims})
        runtime = _Runtime([_finding()])
        result = ClaimAuditAgent(runtime).audit(draft=draft, context=_context("Synthetic exact source."))
        self.assertEqual("model_claim_set_mismatch", result.reason_code)
        self.assertEqual(0, runtime.calls)
        self.assertEqual("deterministic_gate", result.audit_mode)
        self.assertIsNone(result.model)

    def test_identical_source_text_does_not_allow_cross_evidence_reference(self) -> None:
        context = _context("Identical synthetic source.", "Identical synthetic source.")
        candidates = _audit_quote_candidates(context, {"E1", "E2"})
        self.assertEqual(candidates[0].text, candidates[1].text)
        self.assertNotEqual(candidates[0].quote_id, candidates[1].quote_id)
        result = ClaimAuditAgent(_Runtime([_finding(evidence_context_ids=["E2"])])).audit(
            draft=_draft("E1", "E2"), context=context)
        self.assertEqual("model_quote_reference_invalid", result.reason_code)

    def test_one_evidence_quote_is_not_copied_onto_another_citation(self) -> None:
        context = _context("First synthetic source.", "Second synthetic source.")
        result = ClaimAuditAgent(_Runtime([_finding(evidence_context_ids=["E1", "E2"])])).audit(
            draft=_draft("E1", "E2"), context=context)
        quotes = {citation.context_id: citation.support_quote for citation in result.citations}
        self.assertEqual("First synthetic source.", quotes["E1"])
        self.assertEqual("", quotes["E2"])

    def test_two_claims_preserve_distinct_quotes_and_deduplicate_identical_quotes(self) -> None:
        context = _merged_context("First synthetic source.", "Second synthetic source.")
        self.assertEqual(1, len(context.items))
        self.assertEqual(2, len(context.items[0].source_body_spans))
        draft = _draft()
        draft = draft.model_copy(update={"claims": draft.claims + (
            AnswerClaim(claim_id="C2", text="Second synthetic claim.", evidence_context_ids=("E1",)),)})
        for second_id, expected in (("E1Q2", ("First synthetic source.", "Second synthetic source.")),
                                    ("E1Q1", ("First synthetic source.",))):
            with self.subTest(second_id=second_id):
                runtime = _Runtime([_finding(), _finding(claim_id="C2", support_quote_id=second_id)])
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                self.assertEqual("verified", result.status)
                self.assertEqual(("C1", "C2"), result.verified_claim_ids)
                self.assertEqual(expected, tuple(citation.support_quote for citation in result.citations))
                self.assertTrue(all(citation.context_id == "E1" for citation in result.citations))

    def test_candidate_spans_exclude_synthetic_markers_and_merged_boundaries(self) -> None:
        context = _merged_context("First source paragraph.",
            "Second source paragraph.[문맥 예산으로 생략]Third source.[모델 제어 토큰 제거]Fourth source.")
        self.assertEqual(1, len(context.items))
        self.assertEqual(2, len(context.items[0].source_body_spans))
        text = context.items[0].text
        candidates = _audit_quote_candidates(context, {"E1"})
        self.assertEqual(4, len(candidates))
        for candidate in candidates:
            self.assertEqual(text[candidate.start:candidate.end], candidate.text)
            self.assertNotIn("[문맥 예산으로 생략]", candidate.text)
            self.assertNotIn("[모델 제어 토큰 제거]", candidate.text)
            self.assertNotIn("\n\n", candidate.text)
        legacy = _finding(support_quote="[문맥 예산으로 생략]")
        legacy.pop("support_quote_id")
        result = ClaimAuditAgent(_Runtime([legacy])).audit(draft=_draft(), context=context)
        self.assertEqual("support_quote_not_exact", result.reason_code)

    def test_duplicate_map_ids_and_invalid_or_mutated_spans_fail_closed(self) -> None:
        context = _context("a" * 1000)
        candidates = _audit_quote_candidates(context, {"E1"})
        first = candidates[0]
        self.assertEqual(first.text, candidates[1].text)
        self.assertNotEqual(first.quote_id, candidates[1].quote_id)
        for invalid in (replace(first, start=-1), replace(first, start=True), replace(first, end=1001),
                        replace(first, end=first.start), replace(first, text="changed"),
                        replace(first, context_id="E2"), replace(first, quote_id="E2Q1"),
                        replace(first, end=2, text="aa"), replace(first, end=501, text="a" * 501)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _validated_quote_map(context, (invalid,))
        with self.assertRaises(ValueError):
            _validated_quote_map(context, (first, first))
        result = _bind_model_audit(_draft(), context, _ModelAuditDraft.model_validate({"answer_supported": True, "findings": [_finding()]}),
                                   1.0, quote_candidates=(first, first))
        self.assertEqual("quote_candidate_map_invalid", result.reason_code)

    def test_empty_or_short_source_cannot_supply_an_id_quote(self) -> None:
        for source in ("a", "   "):
            context = _context("initial source")
            # This deliberately exercises the legacy context quote-ID path,
            # without retaining stale offsets from a different source text.
            context = context.model_copy(update={"items": (context.items[0].model_copy(
                update={"text": source, "source_body_spans": None}),)})
            runtime = _Runtime([_finding()])
            result = ClaimAuditAgent(runtime).audit(draft=_draft(), context=context)
            self.assertEqual("model_quote_reference_invalid", result.reason_code)
            self.assertEqual(1, runtime.calls)


class CompleteAnswerAuditTests(unittest.TestCase):
    def test_legacy_free_quote_cannot_cross_approved_source_boundaries(self) -> None:
        first = "First independently approved statement."
        second = "Second independently approved statement."
        context = _merged_context(first, second)
        self.assertEqual(1, len(context.items))
        self.assertEqual(2, len(context.items[0].source_body_spans))
        for quote, expected in ((first + " " + second, "review_required"),
                                (first + "\n\n" + second, "review_required"),
                                (first, "verified"), (second, "verified")):
            with self.subTest(quote=quote):
                finding = _finding(support_quote=quote)
                finding.pop("support_quote_id")
                result = ClaimAuditAgent(_Runtime([finding])).audit(draft=_draft(), context=context)
                self.assertEqual(expected, result.status)
                if expected == "review_required":
                    self.assertEqual("support_quote_not_exact", result.reason_code)
                    self.assertEqual((), result.citations)
        self.assertTrue(_quote_is_exact("First wrapped source line.", "First wrapped\nsource line."))

    def test_whole_answer_verdict_rejects_four_supported_claim_mismatches(self) -> None:
        cases = (
            ("wrong_number", "The payment is 10 units.", "The payment is 999 units. [E1]"),
            ("removed_condition", "Only managers with prior written approval may claim expenses.",
             "All managers may claim expenses. [E1]"),
            ("extra_same_marker", "The deadline is 7 days.",
             "The deadline is 7 days. [E1] Late applicants receive 999 units. [E1]"),
            ("extra_unmarked", "The deadline is 7 days.",
             "The deadline is 7 days. [E1] Late applicants receive 999 units."),
        )
        for case_id, source, answer in cases:
            with self.subTest(case=case_id):
                draft = GroundedAnswerDraft(answer=answer, answer_mode="grounded_local", claims=(
                    AnswerClaim(claim_id="C1", text=source, evidence_context_ids=("E1",)),))
                context = _context(source)
                before = draft.model_dump(), context.model_dump()
                runtime = _Runtime([_finding()], answer_supported=False)
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                direct = _bind_model_audit(draft, context, _ModelAuditDraft.model_validate({
                    "answer_supported": False, "findings": [_finding()]}), 1.0)
                self.assertEqual(1, runtime.calls)
                self.assertIn(json.dumps(answer, ensure_ascii=False), runtime.prompt)
                for value in (result, direct):
                    self.assertEqual("rejected", value.status)
                    self.assertEqual("full_answer_not_supported", value.reason_code)
                    self.assertEqual((), value.verified_claim_ids)
                    self.assertEqual((), value.findings)
                    self.assertEqual((), value.rejected_claim_ids)
                    self.assertEqual((), value.citations)
                self.assertEqual(before, (draft.model_dump(), context.model_dump()))

    def test_exact_and_legitimate_paraphrase_are_preserved(self) -> None:
        source = "The payment is 10 units."
        for answer in ("The payment is 10 units. [E1]", "You receive a payment of 10 units. [E1]"):
            with self.subTest(answer=answer):
                draft = GroundedAnswerDraft(answer=answer, answer_mode="grounded_local", claims=(
                    AnswerClaim(claim_id="C1", text=source, evidence_context_ids=("E1",)),))
                encoded = draft.answer.encode("utf-8")
                result = ClaimAuditAgent(_Runtime([_finding()])).audit(draft=draft, context=_context(source))
                self.assertEqual("verified", result.status)
                self.assertEqual(encoded, draft.answer.encode("utf-8"))

    def test_missing_or_nonboolean_answer_verdict_never_verifies(self) -> None:
        runtimes = [_Runtime([_finding()], include_answer_supported=False)] + [
            _Runtime([_finding()], answer_supported=value) for value in (None, "true", "false", 0, 1)]
        for runtime in runtimes:
            with self.subTest(verdict=runtime.answer_supported, included=runtime.include_answer_supported):
                result = ClaimAuditAgent(runtime).audit(draft=_draft(), context=_context("Synthetic exact source."))
                self.assertEqual("review_required", result.status)
                self.assertEqual("model_ValidationError", result.reason_code)
                self.assertEqual((), result.citations)
                self.assertEqual((), result.verified_claim_ids)

    def test_fast_tail_beyond_single_claim_limit_is_included_in_audit(self) -> None:
        source = "The deadline is 7 days."
        answer = "The deadline is 7 days. [E1] " + "Ordinary descriptive wording. " * 43 + "Late applicants receive 999 units."
        self.assertGreater(len(answer), 1200)
        self.assertLessEqual(len(answer), 1600)
        context = _context(source)
        draft = _validated_fast_model_draft(_FastModelAnswerDraft(answer=answer, evidence_context_ids=["E1"]), context, 1.0)
        self.assertNotIn("999", draft.claims[0].text)
        self.assertTrue(draft.answer.endswith("999 units."))
        runtime = _Runtime([_finding()], answer_supported=False)
        result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
        self.assertIn(json.dumps(answer, ensure_ascii=False), runtime.prompt)
        self.assertEqual("rejected", result.status)
        self.assertEqual(answer.encode("utf-8"), draft.answer.encode("utf-8"))

    def test_compound_condition_and_exception_remain_part_of_full_audit(self) -> None:
        source = "Managers may claim expenses only with prior written approval and a receipt, except international travel."
        for answer, supported in (
            ("Managers may claim expenses with prior written approval. [E1]", False),
            ("Managers with prior written approval and a receipt may claim expenses, excluding international travel. [E1]", True),
        ):
            with self.subTest(answer=answer):
                draft = GroundedAnswerDraft(answer=answer, answer_mode="grounded_local", claims=(
                    AnswerClaim(claim_id="C1", text=source, evidence_context_ids=("E1",)),))
                runtime = _Runtime([_finding()], answer_supported=supported)
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=_context(source))
                self.assertEqual("verified" if supported else "rejected", result.status)
                self.assertIn(json.dumps(answer, ensure_ascii=False), runtime.prompt)

    def test_prompt_contains_complete_json_answer_without_changing_unicode_or_whitespace(self) -> None:
        answer = '첫 줄\n  둘째 줄: "인용" \\ 내용 [E1]'
        draft = _draft().model_copy(update={"answer": answer})
        prompt = _audit_prompt(draft, _context("Synthetic exact source."))
        encoded = next(line.split(": ", 1)[1] for line in prompt.splitlines()
                       if line.startswith("감사 대상 최종 답변(전체): "))
        self.assertEqual(answer.encode("utf-8"), json.loads(encoded).encode("utf-8"))
        self.assertIn("answer_supported", _ModelAuditDraft.model_json_schema()["required"])


if __name__ == "__main__":
    unittest.main()
