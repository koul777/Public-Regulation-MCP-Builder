from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.agents.claim_auditor import (
    ClaimAuditAgent, _ModelAuditDraft, _ModelFinding, _ModelSentence, _answer_has_number_outside_evidence,
    _answer_sentences, _audit_prompt, _audit_quote_candidates, _bind_model_audit, _gate_text, _quote_is_exact,
    _validated_quote_map,
)
from app.agents.grounded_qa import (
    AnswerClaim, GroundedAnswerDraft, TABLE_STRUCTURE_REVIEW_REASON,
    _FastModelAnswerDraft, _validated_fast_model_draft,
)
from app.rag.context_builder import ContextBuilder
from app.rag.extractive_answer import NO_EVIDENCE_ANSWER


def _prompt_json(prompt, label):
    return json.loads(next(line.split(": ", 1)[1] for line in prompt.splitlines() if line.startswith(label + ": ")))


class _Runtime:
    """Fake local model. By default every prompted answer sentence is supported."""

    def __init__(self, findings, *, sentences=None, sentence_supported=True, unsupported_ids=(),
                 include_sentences=True):
        self.findings = findings
        self.sentences = sentences
        self.sentence_supported = sentence_supported
        self.unsupported_ids = set(unsupported_ids)
        self.include_sentences = include_sentences
        self.calls = 0
        self.prompt = ""
        self.schema = {}

    def model_available(self, model):
        return True

    def _prompt_sentences(self):
        quotes = _prompt_json(self.prompt, "승인 근거의 원문 연속구간")
        quote_id = quotes[0]["quote_id"] if quotes else ""
        return [{"sentence_id": item["sentence_id"], "support_quote_id": quote_id,
                 "supported": self.sentence_supported and item["sentence_id"] not in self.unsupported_ids}
                for item in _prompt_json(self.prompt, "감사 대상 답변 문장")]

    def generate_json(self, **kwargs):
        self.calls += 1
        self.prompt = kwargs["prompt"]
        self.schema = kwargs["schema"]
        payload = {"findings": self.findings}
        if self.include_sentences:
            payload["sentences"] = self._prompt_sentences() if self.sentences is None else self.sentences
        return payload, SimpleNamespace(duration_ms=1.0)


class _LegacyRuntime(_Runtime):
    """Returns an already built draft whose findings carry a free quote and no quote id.

    model_construct bypasses the required support_quote_id, which keeps the retained
    exact-copy gate for stored older payloads under test.
    """

    def generate_json(self, **kwargs):
        payload, generation = super().generate_json(**kwargs)
        legacy = _ModelAuditDraft.model_construct(
            findings=[_ModelFinding.model_construct(**finding) for finding in self.findings],
            sentences=[_ModelSentence.model_validate(item) for item in payload["sentences"]])
        return legacy, generation


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
    # Its words occur in every helper context's regulation title, so the lexical gate passes.
    ids = evidence_ids or ("E1",)
    return GroundedAnswerDraft(answer="Synthetic rule " + " ".join(f"[{eid}]" for eid in ids),
                               answer_mode="grounded_local", claims=(AnswerClaim(
                                   claim_id="C1", text="Synthetic rule", evidence_context_ids=ids),))


def _finding(**overrides):
    return {"claim_id": "C1", "supported": True, "evidence_context_ids": ["E1"],
            "support_quote_id": "E1Q1", **overrides}


def _model_draft(draft, context, findings=None, *, sentences=None, supported=True):
    """Direct-binding payload: one verdict per answer sentence citing the first quote candidate."""
    if sentences is None:
        wanted = {context_id for claim in draft.claims for context_id in claim.evidence_context_ids}
        candidates = _audit_quote_candidates(context, wanted)
        quote_id = candidates[0].quote_id if candidates else ""
        sentences = [{"sentence_id": sentence_id, "support_quote_id": quote_id, "supported": supported}
                     for sentence_id, _ in _answer_sentences(draft.answer)]
    return _ModelAuditDraft.model_validate({"findings": [_finding()] if findings is None else findings,
                                            "sentences": sentences})


def _single_claim_draft(answer, source):
    return GroundedAnswerDraft(answer=answer, answer_mode="grounded_local", claims=(
        AnswerClaim(claim_id="C1", text=source, evidence_context_ids=("E1",)),))


class ClaimAuditorPromptTests(unittest.TestCase):
    def test_undeclared_answer_markers_fail_preflight_and_direct_binding(self) -> None:
        context = _context("First synthetic source.", "Second synthetic source.")
        for marker in ("E2", "E99"):
            with self.subTest(marker=marker):
                draft = _draft().model_copy(update={"answer": f"Synthetic claim. [E1] Extra claim. [{marker}]"})
                runtime = _Runtime([_finding()])
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                direct = _bind_model_audit(draft, context, _model_draft(draft, context), 1.0)
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
                direct = _bind_model_audit(draft, context, _model_draft(draft, context), 1.0)
                self.assertEqual(0, runtime.calls)
                for value in (public, direct):
                    self.assertEqual("rejected", value.status)
                    self.assertEqual(reason, value.findings[0].reason_code)
                    self.assertEqual("deterministic_gate", value.audit_mode)
                    self.assertIsNone(value.model)
                    self.assertEqual((), value.citations)

    def test_multiple_claim_markers_preserve_two_exact_citations(self) -> None:
        context = _context("First synthetic source.", "Second synthetic source.")
        draft = GroundedAnswerDraft(answer="First synthetic source. [E1] Second synthetic source. [E2] [E1]",
            answer_mode="grounded_local", claims=(
                AnswerClaim(claim_id="C1", text="First synthetic source.", evidence_context_ids=("E1",)),
                AnswerClaim(claim_id="C2", text="Second synthetic source.", evidence_context_ids=("E2",)),
            ))
        findings = [_finding(), _finding(claim_id="C2", evidence_context_ids=["E2"], support_quote_id="E2Q1")]
        runtime = _Runtime(findings)
        public = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
        direct = _bind_model_audit(draft, context, _model_draft(draft, context, findings), 1.0)
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
        direct = _bind_model_audit(draft, context, _model_draft(draft, context), 1.0)
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
                direct = _bind_model_audit(draft, context, _model_draft(draft, context), 1.0)
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
        for reference in ("", "E1Q999", "E2Q1", " E1Q1"):
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
                self.assertEqual(expected, ClaimAuditAgent(_LegacyRuntime([finding])).audit(
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
        result = ClaimAuditAgent(_LegacyRuntime([legacy])).audit(draft=_draft(), context=context)
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
        result = _bind_model_audit(_draft(), context, _model_draft(_draft(), context), 1.0,
                                   quote_candidates=(first, first))
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
            # Without any selectable span the sentence verdicts are rejected before the claim quote.
            self.assertEqual("sentence_quote_reference_invalid", result.reason_code)
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
                result = ClaimAuditAgent(_LegacyRuntime([finding])).audit(draft=_draft(), context=context)
                self.assertEqual(expected, result.status)
                if expected == "review_required":
                    self.assertEqual("support_quote_not_exact", result.reason_code)
                    self.assertEqual((), result.citations)
        self.assertTrue(_quote_is_exact("First wrapped source line.", "First wrapped\nsource line."))

    def test_prompt_has_full_answer_then_numbered_sentences_without_changing_unicode_or_whitespace(self) -> None:
        answer = '첫 줄\n  둘째 줄: "인용" \\ 내용 [E1]'
        draft = _draft().model_copy(update={"answer": answer})
        prompt = _audit_prompt(draft, _context("Synthetic exact source."))
        self.assertEqual(answer.encode("utf-8"),
                         _prompt_json(prompt, "감사 대상 최종 답변(전체)").encode("utf-8"))
        self.assertEqual([{"sentence_id": "S1", "text": "첫 줄"},
                          {"sentence_id": "S2", "text": '둘째 줄: "인용" \\ 내용 [E1]'}],
                         _prompt_json(prompt, "감사 대상 답변 문장"))
        positions = [prompt.index(label + ": ") for label in (
            "감사 대상 최종 답변(전체)", "감사 대상 답변 문장", "감사 대상 주장", "승인 근거 위치",
            "승인 근거의 원문 연속구간")]
        self.assertEqual(sorted(positions), positions)
        self.assertNotIn("answer_supported", prompt)

    def test_prompt_does_not_exempt_article_number_and_title_mentions(self) -> None:
        prompt = _audit_prompt(_draft(), _context("Synthetic exact source."))
        self.assertNotIn("조문 번호", prompt)
        self.assertNotIn("제목 언급", prompt)
        for kept in ("뜻이 같은 바꿔쓰기", "표 행을 문장으로 옮긴 것", "문장을 잇는 표현"):
            self.assertIn(kept, prompt)

    def test_schema_requires_findings_then_sentences_and_required_quote_fields(self) -> None:
        schema = _ModelAuditDraft.model_json_schema()
        self.assertEqual(["findings", "sentences"], list(schema["properties"]))
        self.assertEqual(["findings", "sentences"], schema["required"])
        self.assertNotIn("answer_supported", schema["properties"])
        self.assertEqual(24, schema["properties"]["sentences"]["maxItems"])
        self.assertNotIn("minItems", schema["properties"]["sentences"])
        finding = schema["$defs"]["_ModelFinding"]
        self.assertEqual(["claim_id", "supported", "evidence_context_ids", "support_quote_id"], finding["required"])
        sentence = schema["$defs"]["_ModelSentence"]
        self.assertEqual(["sentence_id", "support_quote_id", "supported"], list(sentence["properties"]))
        self.assertEqual(["sentence_id", "support_quote_id", "supported"], sentence["required"])
        runtime = _Runtime([_finding()])
        ClaimAuditAgent(runtime).audit(draft=_draft(), context=_context("Synthetic exact source."))
        self.assertEqual(schema, runtime.schema)

    def test_findings_without_required_quote_fields_fail_validation(self) -> None:
        for field in ("support_quote_id", "evidence_context_ids"):
            missing = _finding()
            missing.pop(field)
            with self.subTest(missing=field):
                with self.assertRaises(ValidationError):
                    _ModelFinding.model_validate(missing)
                result = ClaimAuditAgent(_Runtime([missing])).audit(
                    draft=_draft(), context=_context("Synthetic exact source."))
                self.assertEqual("review_required", result.status)
                self.assertEqual("model_ValidationError", result.reason_code)
        with self.assertRaises(ValidationError):
            _ModelFinding.model_validate(_finding(support_quote_id=None))
        self.assertEqual("", _ModelFinding.model_validate(_finding(support_quote="")).support_quote)

    def test_missing_or_nonboolean_sentence_verdict_never_verifies(self) -> None:
        good = {"sentence_id": "S1", "support_quote_id": "E1Q1", "supported": True}
        incomplete = [{key: value for key, value in good.items() if key != dropped}
                      for dropped in ("supported", "support_quote_id", "sentence_id")]
        wrong_type = [{**good, "supported": value} for value in (None, "true", "false", 0, 1)]
        runtimes = [_Runtime([_finding()], include_sentences=False)] + [
            _Runtime([_finding()], sentences=[sentence]) for sentence in incomplete + wrong_type
            + [{**good, "sentence_id": "first"}, {**good, "extra": 1}]]
        for runtime in runtimes:
            with self.subTest(sentences=runtime.sentences, included=runtime.include_sentences):
                result = ClaimAuditAgent(runtime).audit(draft=_draft(), context=_context("Synthetic exact source."))
                self.assertEqual("review_required", result.status)
                self.assertEqual("model_ValidationError", result.reason_code)
                self.assertEqual((), result.citations)
                self.assertEqual((), result.verified_claim_ids)

    def test_unsupported_sentence_verdict_rejects_answer_without_claim_findings(self) -> None:
        source = "Only managers with prior written approval may claim expenses."
        answer = "All managers may claim expenses. [E1]"
        draft = _single_claim_draft(answer, source)
        context = _context(source)
        before = draft.model_dump(), context.model_dump()
        runtime = _Runtime([_finding()], sentence_supported=False)
        result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
        direct = _bind_model_audit(draft, context, _model_draft(draft, context, supported=False), 1.0)
        self.assertEqual(1, runtime.calls)
        self.assertEqual([{"sentence_id": "S1", "text": answer}], _prompt_json(runtime.prompt, "감사 대상 답변 문장"))
        for value in (result, direct):
            self.assertEqual("rejected", value.status)
            self.assertEqual("answer_sentence_not_supported", value.reason_code)
            self.assertEqual("local_model", value.audit_mode)
            self.assertEqual((), value.verified_claim_ids)
            self.assertEqual((), value.findings)
            self.assertEqual((), value.rejected_claim_ids)
            self.assertEqual((), value.citations)
        self.assertEqual(before, (draft.model_dump(), context.model_dump()))

    def test_fast_tail_beyond_single_claim_limit_is_included_in_audit(self) -> None:
        source = "The deadline is 7 days."
        filler = "The deadline is 7 days and the deadline is 7 days and the deadline is 7 days. "
        context = _context(source)
        for tail, expected_reason, model_calls in (
            ("Late applicants receive 999 units.", "answer_number_not_in_evidence", 0),
            ("Late applicants are exempt from the fee.", "answer_sentence_terms_not_in_evidence", 0),
            ("The deadline is 7 days from the deadline.", "answer_sentence_not_supported", 1),
        ):
            with self.subTest(tail=tail):
                answer = "The deadline is 7 days. [E1] " + filler * 18 + tail
                self.assertGreater(len(answer), 1200)
                self.assertLessEqual(len(answer), 1600)
                draft = _validated_fast_model_draft(
                    _FastModelAnswerDraft(answer=answer, evidence_context_ids=["E1"]), context, 1.0)
                self.assertNotIn(tail, draft.claims[0].text)
                self.assertTrue(draft.answer.endswith(tail))
                runtime = _Runtime([_finding()], unsupported_ids={"S20"})
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                self.assertEqual(model_calls, runtime.calls)
                self.assertEqual("rejected", result.status)
                self.assertEqual(expected_reason, result.reason_code)
                if model_calls:
                    self.assertEqual({"sentence_id": "S20", "text": tail},
                                     _prompt_json(runtime.prompt, "감사 대상 답변 문장")[-1])
                self.assertEqual(answer.encode("utf-8"), draft.answer.encode("utf-8"))

    def test_compound_condition_and_exception_remain_part_of_full_audit(self) -> None:
        source = "Managers may claim expenses only with prior written approval and a receipt, except international travel."
        for answer, supported in (
            ("Managers may claim expenses with prior written approval. [E1]", False),
            ("Managers with prior written approval and a receipt may claim expenses, excluding international travel. [E1]", True),
        ):
            with self.subTest(answer=answer):
                draft = _single_claim_draft(answer, source)
                runtime = _Runtime([_finding()], sentence_supported=supported)
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=_context(source))
                self.assertEqual("verified" if supported else "rejected", result.status)
                self.assertEqual([{"sentence_id": "S1", "text": answer}],
                                 _prompt_json(runtime.prompt, "감사 대상 답변 문장"))


class SyntheticAnswerControlTests(unittest.TestCase):
    """Answer-level controls: claims can be true while the final answer is not."""

    def _gate_rejects(self, answer: str, source: str, reason: str) -> None:
        draft = _single_claim_draft(answer, source)
        context = _context(source)
        before = draft.model_dump(), context.model_dump()
        runtime = _Runtime([_finding()])
        result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
        direct = _bind_model_audit(draft, context, _model_draft(draft, context), 1.0)
        self.assertEqual(0, runtime.calls)
        for value in (result, direct):
            self.assertEqual("rejected", value.status)
            self.assertEqual(reason, value.reason_code)
            self.assertEqual("deterministic_gate", value.audit_mode)
            self.assertIsNone(value.model)
            self.assertEqual((), value.findings)
            self.assertEqual((), value.verified_claim_ids)
            self.assertEqual((), value.rejected_claim_ids)
            self.assertEqual((), value.citations)
        self.assertEqual(before, (draft.model_dump(), context.model_dump()))

    def test_changed_number_is_rejected_by_number_gate_before_model(self) -> None:
        self._gate_rejects("The payment is 999 units. [E1]", "The payment is 10 units.",
                           "answer_number_not_in_evidence")

    def test_added_numeric_sentence_with_same_marker_is_rejected_by_number_gate(self) -> None:
        self._gate_rejects("The deadline is 7 days. [E1] Late applicants receive 999 units. [E1]",
                           "The deadline is 7 days.", "answer_number_not_in_evidence")

    def test_added_unmarked_sentence_without_numbers_is_rejected_by_lexical_gate(self) -> None:
        self._gate_rejects("The deadline is 7 days. [E1] Late applicants are exempt from the fee.",
                           "The deadline is 7 days.", "answer_sentence_terms_not_in_evidence")

    def test_exact_and_legitimate_paraphrase_are_preserved(self) -> None:
        # The paraphrase keeps 5 of 8 analyzer tokens (0.625), close to the 0.6 floor;
        # Korean paraphrases keep their nouns and sit well above it.
        source = "The payment is 10 units."
        for answer in ("The payment is 10 units. [E1]", "You receive a payment of 10 units. [E1]"):
            with self.subTest(answer=answer):
                draft = _single_claim_draft(answer, source)
                encoded = draft.answer.encode("utf-8")
                runtime = _Runtime([_finding()])
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=_context(source))
                self.assertEqual("verified", result.status)
                self.assertEqual(1, runtime.calls)
                self.assertEqual(encoded, draft.answer.encode("utf-8"))

    def test_gate_only_reads_cited_evidence(self) -> None:
        context = _context("The payment is 10 units.", "The payment is 999 units.")
        draft = _single_claim_draft("The payment is 999 units. [E1]", "The payment is 10 units.")
        runtime = _Runtime([_finding()])
        result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
        self.assertEqual(0, runtime.calls)
        self.assertEqual("answer_number_not_in_evidence", result.reason_code)
        second = draft.model_copy(update={"claims": (AnswerClaim(
            claim_id="C1", text="The payment is 999 units.", evidence_context_ids=("E2",)),),
            "answer": "The payment is 999 units. [E2]"})
        verified = ClaimAuditAgent(_Runtime([_finding(evidence_context_ids=["E2"], support_quote_id="E2Q1")])).audit(
            draft=second, context=context)
        self.assertEqual("verified", verified.status)

    def test_number_gate_digit_boundaries_and_decimal_skipping(self) -> None:
        source = "The fee is 1.5 units for 100 days."
        for answer, status in (("The fee is 1.5 units for 100 days. [E1]", "verified"),
                               ("The fee is 1.5 units for 10 days. [E1]", "rejected"),
                               ("The fee is 1.5 units for 1000 days. [E1]", "rejected")):
            with self.subTest(answer=answer):
                result = ClaimAuditAgent(_Runtime([_finding()])).audit(
                    draft=_single_claim_draft(answer, source), context=_context(source))
                self.assertEqual(status, result.status)


class KoreanAnswerGateTests(unittest.TestCase):
    source = "제3조(휴가 신청) 근로자는 휴가 사용일의 7일 전까지 신청서를 제출하여야 한다."

    def _context(self, text: str | None = None):
        return ContextBuilder().build([{
            "chunk_id": "synthetic-chunk", "document_id": "synthetic-document", "text": text or self.source,
            "approval_status": "approved", "approval_id": "synthetic-approval", "approved_content_hash": "a" * 64,
            "regulation_title": "합성 휴가 규정", "article_no": "제3조", "article_title": "휴가 신청",
        }])

    def _audit(self, answer: str, text: str | None = None):
        runtime = _Runtime([_finding()])
        draft = _single_claim_draft(answer, text or self.source)
        return ClaimAuditAgent(runtime).audit(draft=draft, context=self._context(text)), runtime

    def test_answer_copying_article_numbers_and_nouns_passes_both_gates(self) -> None:
        for answer in ("근로자는 휴가 사용일의 7일 전까지 신청서를 제출해야 합니다. [E1]",
                       "휴가 신청서는 사용일 7일 전까지 내면 된다. [E1]"):
            with self.subTest(answer=answer):
                result, runtime = self._audit(answer)
                self.assertEqual("verified", result.status)
                self.assertEqual(1, runtime.calls)

    def test_appended_exception_without_matching_nouns_is_rejected_by_lexical_gate(self) -> None:
        answer = ("근로자는 휴가 사용일의 7일 전까지 신청서를 제출해야 합니다. [E1] "
                  "다만 소속 부서장이 따로 승인하면 이 조항을 적용하지 않는다. [E1]")
        result, runtime = self._audit(answer)
        self.assertEqual(0, runtime.calls)
        self.assertEqual("rejected", result.status)
        self.assertEqual("answer_sentence_terms_not_in_evidence", result.reason_code)
        self.assertEqual("deterministic_gate", result.audit_mode)

    def test_changed_number_is_rejected_by_number_gate(self) -> None:
        result, runtime = self._audit("근로자는 휴가 사용일의 10일 전까지 신청서를 제출해야 합니다. [E1]")
        self.assertEqual(0, runtime.calls)
        self.assertEqual("answer_number_not_in_evidence", result.reason_code)

    def test_changed_decimal_is_rejected_by_number_gate(self) -> None:
        text = "휴가는 1.5일이다."
        same, same_runtime = self._audit("휴가는 1.5일이다. [E1]", text)
        changed, changed_runtime = self._audit("휴가는 2.5일이다. [E1]", text)
        self.assertEqual("verified", same.status)
        self.assertEqual(1, same_runtime.calls)
        self.assertEqual("rejected", changed.status)
        self.assertEqual("answer_number_not_in_evidence", changed.reason_code)
        self.assertEqual(0, changed_runtime.calls)


class AnalyzerAvailabilityTests(unittest.TestCase):
    def _assert_unavailable(self, kiwi) -> None:
        source = "The deadline is 7 days."
        draft = _single_claim_draft("The deadline is 7 days. [E1]", source)
        context = _context(source)
        runtime = _Runtime([_finding()])
        with patch("app.agents.claim_auditor._kiwi", return_value=kiwi):
            result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
            direct = _bind_model_audit(draft, context, _model_draft(draft, context), 1.0)
        self.assertEqual(0, runtime.calls)
        for value in (result, direct):
            self.assertEqual("review_required", value.status)
            self.assertEqual("lexical_gate_unavailable", value.reason_code)
            self.assertEqual("deterministic_gate", value.audit_mode)
            self.assertIsNone(value.model)
            self.assertEqual(("lexical_gate_unavailable",), tuple(f.reason_code for f in value.findings))
            self.assertEqual((), value.citations)

    def test_missing_analyzer_fails_closed_before_model(self) -> None:
        self._assert_unavailable(None)

    def test_failing_analyzer_fails_closed_before_model(self) -> None:
        def broken(_text):
            raise RuntimeError("analyzer failed")

        self._assert_unavailable(SimpleNamespace(tokenize=broken))

    def test_number_gate_does_not_need_the_analyzer(self) -> None:
        source = "The payment is 10 units."
        runtime = _Runtime([_finding()])
        with patch("app.agents.claim_auditor._kiwi", return_value=None):
            result = ClaimAuditAgent(runtime).audit(
                draft=_single_claim_draft("The payment is 999 units. [E1]", source), context=_context(source))
        self.assertEqual("answer_number_not_in_evidence", result.reason_code)
        self.assertEqual(0, runtime.calls)

    def test_unexpected_gate_failure_is_reported_like_other_audit_failures(self) -> None:
        context = _context("Synthetic exact source.")
        runtime = _Runtime([_finding()])
        with patch("app.agents.claim_auditor._answer_gate", side_effect=RuntimeError("gate failed")):
            result = ClaimAuditAgent(runtime).audit(draft=_draft(), context=context)
            self.assertEqual("review_required", result.status)
            self.assertEqual("model_RuntimeError", result.reason_code)
            self.assertEqual("deterministic_gate", result.audit_mode)
            self.assertEqual((), result.citations)
            with self.assertRaises(RuntimeError):
                ClaimAuditAgent(runtime).audit(draft=_draft(), context=context, strict_model=True)
        self.assertEqual(0, runtime.calls)


class AnswerSentenceTests(unittest.TestCase):
    def test_split_on_sentence_ends_and_newlines(self) -> None:
        self.assertEqual((("S1", "첫째 문장입니다."), ("S2", "둘째 문장입니다!"), ("S3", "셋째 문장입니까?"),
                          ("S4", "넷째 문장입니다。")),
                         _answer_sentences("첫째 문장입니다. 둘째 문장입니다! 셋째 문장입니까? 넷째 문장입니다。"))
        self.assertEqual((("S1", "첫째 줄입니다"), ("S2", "둘째 줄입니다. [E1]")),
                         _answer_sentences("첫째 줄입니다\n\n  둘째 줄입니다. [E1]"))
        self.assertEqual((), _answer_sentences(""))

    def test_markers_close_the_previous_sentence(self) -> None:
        self.assertEqual((("S1", "첫째 문장입니다. [E1]"), ("S2", "둘째 문장입니다. [E2]")),
                         _answer_sentences("첫째 문장입니다. [E1] 둘째 문장입니다. [E2]"))
        self.assertEqual((("S1", "신청서를 냅니다. [E1] [E2]"),), _answer_sentences("신청서를 냅니다. [E1] [E2]"))
        self.assertEqual((("S1", "신청서를 냅니다. [E1]"), ("S2", "기한은 7일입니다. [E2] [E3]")),
                         _answer_sentences("신청서를 냅니다.\n[E1]\n기한은 7일입니다. [E2]\n[E3]"))
        self.assertEqual((), _answer_sentences("[E1]"))
        self.assertEqual((), _answer_sentences("... [E1]"))

    def test_decimals_and_dotted_dates_do_not_split(self) -> None:
        self.assertEqual((("S1", "휴가는 1.5일입니다. [E1]"),), _answer_sentences("휴가는 1.5일입니다. [E1]"))
        self.assertEqual((("S1", "시행일은 2018. 8. 31이다."), ("S2", "다음 문장이다.")),
                         _answer_sentences("시행일은 2018. 8. 31이다. 다음 문장이다."))
        self.assertEqual((("S1", "시행일은 2018. 8. 31."),), _answer_sentences("시행일은 2018. 8. 31."))
        self.assertEqual((("S1", "항목 1."), ("S2", "항목 둘이다.")), _answer_sentences("항목 1. 항목 둘이다."))

    def test_full_width_ends_and_ellipsis_split(self) -> None:
        self.assertEqual(("첫째 문장입니다．", "둘째 문장입니까？", "셋째 문장입니다！", "넷째 문장입니다…", "다섯째 문장입니다"),
                         tuple(text for _, text in _answer_sentences(
                             "첫째 문장입니다． 둘째 문장입니까？ 셋째 문장입니다！ 넷째 문장입니다… 다섯째 문장입니다")))

    def test_fragments_with_one_word_character_are_kept_and_joined_to_a_neighbor(self) -> None:
        self.assertEqual((("S1", "7. [E1]"),), _answer_sentences("7. [E1]"))
        self.assertEqual((("S1", "예 [E1]"),), _answer_sentences("예 [E1]"))
        self.assertEqual((("S1", "O. [E1]"),), _answer_sentences("O. [E1]"))
        # A leading fragment joins the following sentence, a trailing one the previous sentence.
        self.assertEqual((("S1", "네. 신청서를 냅니다. [E1]"),), _answer_sentences("네. 신청서를 냅니다. [E1]"))
        self.assertEqual((("S1", "가. 나. 신청서를 냅니다."),), _answer_sentences("가. 나. 신청서를 냅니다."))
        self.assertEqual((("S1", "신청서를 냅니다. 예"),), _answer_sentences("신청서를 냅니다. 예"))
        self.assertEqual((("S1", "7일 [E1]"),), _answer_sentences("7일 [E1]"))

    def test_sentence_limit_needs_review_before_model(self) -> None:
        source = "The deadline is 7 days."
        context = _context(source)
        for count, status, calls in ((24, "verified", 1), (25, "review_required", 0)):
            with self.subTest(count=count):
                draft = _single_claim_draft(" ".join(["The deadline is 7 days."] * count) + " [E1]", source)
                runtime = _Runtime([_finding()])
                result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                self.assertEqual(count, len(_answer_sentences(draft.answer)))
                self.assertEqual(status, result.status)
                self.assertEqual(calls, runtime.calls)
                if status == "review_required":
                    self.assertEqual("answer_sentence_limit_exceeded", result.reason_code)
                    self.assertEqual("deterministic_gate", result.audit_mode)
                    self.assertIsNone(result.model)
                    direct = _bind_model_audit(draft, context, _model_draft(draft, context, sentences=[]), 1.0)
                    self.assertEqual("answer_sentence_limit_exceeded", direct.reason_code)


class SentenceBindingTests(unittest.TestCase):
    good = {"sentence_id": "S1", "support_quote_id": "E1Q1", "supported": True}

    def _both(self, sentences, draft=None, context=None):
        draft = draft or _draft()
        context = context or _context("Synthetic exact source.", "Second synthetic source.")
        public = ClaimAuditAgent(_Runtime([_finding()], sentences=sentences)).audit(draft=draft, context=context)
        direct = _bind_model_audit(draft, context, _model_draft(draft, context, sentences=sentences), 1.0)
        return public, direct

    def test_sentence_set_must_match_numbered_answer_sentences(self) -> None:
        two = _draft().model_copy(update={"answer": "Synthetic rule\nSynthetic rule [E1]"})
        cases = (("missing", [], None), ("unknown", [{**self.good, "sentence_id": "S2"}], None),
                 ("extra", [self.good, {**self.good, "sentence_id": "S2"}], None),
                 ("duplicate", [self.good, self.good], None), ("duplicate_of_two", [self.good, self.good], two),
                 ("partial_of_two", [self.good], two))
        for name, sentences, draft in cases:
            with self.subTest(case=name):
                for value in self._both(sentences, draft):
                    self.assertEqual("review_required", value.status)
                    self.assertEqual("model_sentence_set_mismatch", value.reason_code)
                    self.assertEqual("local_model", value.audit_mode)
                    self.assertEqual((), value.citations)
                    self.assertEqual((), value.verified_claim_ids)

    def test_sentence_quote_reference_must_be_a_known_candidate(self) -> None:
        for reference in ("", "E9Q1", "E1Q999", " E1Q1", "E2Q1"):
            with self.subTest(reference=reference):
                for value in self._both([{**self.good, "support_quote_id": reference}]):
                    self.assertEqual("review_required", value.status)
                    self.assertEqual("sentence_quote_reference_invalid", value.reason_code)
                    self.assertEqual((), value.citations)

    def test_unsupported_sentence_may_leave_quote_empty(self) -> None:
        for value in self._both([{**self.good, "support_quote_id": "", "supported": False}]):
            self.assertEqual("rejected", value.status)
            self.assertEqual("answer_sentence_not_supported", value.reason_code)

    def test_one_unsupported_sentence_rejects_the_whole_answer(self) -> None:
        source = "The deadline is 7 days."
        draft = _single_claim_draft("The deadline is 7 days. [E1]\nThe deadline is 7 days from the deadline.", source)
        runtime = _Runtime([_finding()], unsupported_ids={"S2"})
        result = ClaimAuditAgent(runtime).audit(draft=draft, context=_context(source))
        self.assertEqual(["S1", "S2"], [item["sentence_id"] for item in _prompt_json(runtime.prompt, "감사 대상 답변 문장")])
        self.assertEqual("rejected", result.status)
        self.assertEqual("answer_sentence_not_supported", result.reason_code)
        self.assertEqual((), result.findings)

    def test_valid_sentence_verdicts_keep_claim_binding_and_citations(self) -> None:
        source = "The payment is 10 units."
        draft = _single_claim_draft("The payment is 10 units. [E1]", source)
        public, direct = self._both([self.good], draft, _context(source))
        for value in (public, direct):
            self.assertEqual("verified", value.status)
            self.assertEqual(("C1",), value.verified_claim_ids)
            self.assertEqual(source, value.citations[0].support_quote)
            self.assertEqual("local_model", value.audit_mode)

    def test_sentence_quote_must_belong_to_its_own_markers_or_any_cited_context(self) -> None:
        context = _context("First synthetic source.", "Second synthetic source.")
        draft = GroundedAnswerDraft(
            answer="First synthetic source. [E1] Second synthetic source. [E2]\nSynthetic rule",
            answer_mode="grounded_local", claims=(
                AnswerClaim(claim_id="C1", text="First synthetic source.", evidence_context_ids=("E1",)),
                AnswerClaim(claim_id="C2", text="Second synthetic source.", evidence_context_ids=("E2",))))
        findings = [_finding(), _finding(claim_id="C2", evidence_context_ids=["E2"], support_quote_id="E2Q1")]

        def verdicts(*quote_ids):
            return [{"sentence_id": f"S{index}", "support_quote_id": quote_id, "supported": True}
                    for index, quote_id in enumerate(quote_ids, start=1)]

        for quotes, reason in ((("E1Q1", "E2Q1", "E1Q1"), None), (("E1Q1", "E2Q1", "E2Q1"), None),
                               (("E2Q1", "E2Q1", "E1Q1"), "sentence_quote_reference_invalid"),
                               (("E1Q1", "E1Q1", "E1Q1"), "sentence_quote_reference_invalid")):
            with self.subTest(quotes=quotes):
                public = ClaimAuditAgent(_Runtime(findings, sentences=verdicts(*quotes))).audit(
                    draft=draft, context=context)
                direct = _bind_model_audit(draft, context, _model_draft(
                    draft, context, findings, sentences=verdicts(*quotes)), 1.0)
                for value in (public, direct):
                    self.assertEqual("verified" if reason is None else "review_required", value.status)
                    self.assertEqual(reason, value.reason_code)


def _ko_context(text, *, article_no="제3조", paragraph_no=""):
    return ContextBuilder().build([{
        "chunk_id": "synthetic-chunk", "document_id": "synthetic-document", "text": text,
        "approval_status": "approved", "approval_id": "synthetic-approval", "approved_content_hash": "a" * 64,
        "regulation_title": "합성 휴가 규정", "article_no": article_no, "paragraph_no": paragraph_no,
        "article_title": "휴가 신청",
    }])


def _ko_audit(answer, text, **locator):
    runtime = _Runtime([_finding()])
    result = ClaimAuditAgent(runtime).audit(
        draft=_single_claim_draft(answer, text), context=_ko_context(text, **locator))
    return result, runtime


class AnswerFragmentTests(unittest.TestCase):
    def test_numeric_fragment_is_checked_by_the_number_gate(self) -> None:
        source = "Synthetic exact source."
        draft = _single_claim_draft("7. [E1]", source)
        context = _context(source)
        runtime = _Runtime([_finding()])
        result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
        direct = _bind_model_audit(draft, context, _model_draft(draft, context), 1.0)
        self.assertEqual(0, runtime.calls)
        for value in (result, direct):
            self.assertEqual("rejected", value.status)
            self.assertEqual("answer_number_not_in_evidence", value.reason_code)

    def test_fragment_without_content_tokens_is_judged_by_the_model(self) -> None:
        source = "Synthetic exact source."
        for answer in ("예 [E1]", "네. [E1]"):
            with self.subTest(answer=answer):
                runtime = _Runtime([_finding()], sentence_supported=False)
                result = ClaimAuditAgent(runtime).audit(
                    draft=_single_claim_draft(answer, source), context=_context(source))
                self.assertEqual(1, runtime.calls)
                self.assertEqual([{"sentence_id": "S1", "text": answer}],
                                 _prompt_json(runtime.prompt, "감사 대상 답변 문장"))
                self.assertEqual("rejected", result.status)
                self.assertEqual("answer_sentence_not_supported", result.reason_code)

    def test_answer_without_a_sentence_needs_review_before_model(self) -> None:
        source = "Synthetic exact source."
        context = _context(source)
        for answer in ("[E1]", "... [E1]"):
            with self.subTest(answer=answer):
                draft = _single_claim_draft(answer, source)
                runtime = _Runtime([_finding()], sentences=[])
                with patch("app.agents.claim_auditor._kiwi", return_value=None):
                    result = ClaimAuditAgent(runtime).audit(draft=draft, context=context)
                    direct = _bind_model_audit(draft, context, _model_draft(draft, context, sentences=[]), 1.0)
                self.assertEqual(0, runtime.calls)
                for value in (result, direct):
                    self.assertEqual("review_required", value.status)
                    self.assertEqual("answer_sentences_empty", value.reason_code)
                    self.assertEqual("deterministic_gate", value.audit_mode)
                    self.assertIsNone(value.model)
                    self.assertEqual(("answer_sentences_empty",), tuple(f.reason_code for f in value.findings))
                    self.assertEqual((), value.citations)


class AnswerReferenceGateTests(unittest.TestCase):
    def test_article_reference_must_come_from_the_cited_article_number_or_text(self) -> None:
        # "3 조건" lets the number gate pass the pair (3, 조), so only the article gate can stop it.
        text = "제4조(휴가) 휴가 신청은 3 조건을 갖춘 경우 7일 전까지 한다."
        for answer in ("제3조에 따라 휴가 신청은 7일 전까지 한다. [E1]",
                       "제 3 조에 따라 휴가 신청은 7일 전까지 한다. [E1]"):
            with self.subTest(answer=answer):
                result, runtime = _ko_audit(answer, text, article_no="제4조")
                self.assertEqual(0, runtime.calls)
                self.assertEqual("rejected", result.status)
                self.assertEqual("answer_article_reference_not_in_evidence", result.reason_code)
                self.assertEqual("deterministic_gate", result.audit_mode)
                draft = _single_claim_draft(answer, text)
                context = _ko_context(text, article_no="제4조")
                direct = _bind_model_audit(draft, context, _model_draft(draft, context), 1.0)
                self.assertEqual("answer_article_reference_not_in_evidence", direct.reason_code)

    def test_article_reference_matching_metadata_or_text_passes_with_any_spacing(self) -> None:
        text = "휴가 신청은 7일 전까지 한다."
        for answer, article_no in (("제16조에 따라 휴가 신청은 7일 전까지 한다. [E1]", "제16조"),
                                   ("제 16 조에 따라 휴가 신청은 7일 전까지 한다. [E1]", "제16조"),
                                   ("제16조에 따라 휴가 신청은 7일 전까지 한다. [E1]", "제 16 조")):
            with self.subTest(answer=answer, article_no=article_no):
                result, runtime = _ko_audit(answer, text, article_no=article_no)
                self.assertEqual("verified", result.status)
                self.assertEqual(1, runtime.calls)
        mentioned = "제5조에 따른 신청서는 7일 전까지 낸다."
        result, runtime = _ko_audit("제5조에 따른 신청서는 7일 전까지 낸다. [E1]", mentioned, article_no="제3조")
        self.assertEqual("verified", result.status)

    def test_branch_article_must_match_exactly(self) -> None:
        text = "제3조의2(특례) 휴가는 7일이다."
        for answer, status in (("제3조의2에 따라 휴가는 7일이다. [E1]", "verified"),
                               ("제3조에 따라 휴가는 7일이다. [E1]", "rejected"),
                               ("제3조의3에 따라 휴가는 7일이다. [E1]", "rejected")):
            with self.subTest(answer=answer):
                result, _ = _ko_audit(answer, text, article_no="제3조의2")
                self.assertEqual(status, result.status)
                if status == "rejected":
                    self.assertEqual("deterministic_gate", result.audit_mode)


class NumberGateTokenTests(unittest.TestCase):
    def _reason(self, text, answer, **locator):
        result, runtime = _ko_audit(answer, text, **locator)
        if result.reason_code == "answer_number_not_in_evidence":
            self.assertEqual(0, runtime.calls)
        return result.status, result.reason_code

    def test_numbers_match_as_whole_tokens(self) -> None:
        number = (("rejected", "answer_number_not_in_evidence"))
        self.assertEqual(number, self._reason("수당은 1,500원이다.", "수당은 500원이다. [E1]"))
        self.assertEqual(number, self._reason("제3조(기간) 계약 기간은 5년으로 한다.", "계약 기간은 3년이다. [E1]"))
        self.assertEqual(number, self._reason("휴가는 1.5일이다.", "휴가는 2.5일이다. [E1]"))
        self.assertEqual(number, self._reason("휴가는 10일이다.", "휴가는 100일이다. [E1]"))
        self.assertEqual(("verified", None), self._reason("휴가는 1.5일이다.", "휴가는 1.5일이다. [E1]"))

    def test_thousands_separators_and_full_width_digits_match(self) -> None:
        verified = ("verified", None)
        self.assertEqual(verified, self._reason("수당은 1,000원이다.", "수당은 1000원이다. [E1]"))
        self.assertEqual(verified, self._reason("수당은 1000원이다.", "수당은 1,000원이다. [E1]"))
        self.assertEqual(verified, self._reason("휴가는 7일이다.", "휴가는 ７일이다. [E1]"))
        self.assertEqual(verified, self._reason("휴가는 ７일이다.", "휴가는 7일이다. [E1]"))

    def test_circled_paragraph_numbers_match_paragraph_references(self) -> None:
        text = "② 휴가는 7일이다."
        self.assertEqual(("verified", None), self._reason(text, "제2항에 따라 휴가는 7일이다. [E1]"))
        self.assertEqual(("verified", None), self._reason(text, "② 휴가는 7일이다. [E1]"))
        self.assertEqual(("rejected", "answer_number_not_in_evidence"),
                         self._reason(text, "제3항에 따라 휴가는 7일이다. [E1]"))
        # Paragraph and article numbers from the locator metadata count as evidence.
        self.assertEqual(("verified", None), self._reason("휴가는 7일이다.", "제2항에 따라 휴가는 7일이다. [E1]",
                                                         paragraph_no="제2항"))

    def test_dotted_dates_with_spaces_match_compact_dates(self) -> None:
        verified = ("verified", None)
        self.assertEqual(verified, self._reason("시행일은 2018. 7. 31.이다.", "시행일은 2018.7.31.이다. [E1]"))
        self.assertEqual(verified, self._reason("시행일은 2018.7.31.이다.", "시행일은 2018. 7. 31.이다. [E1]"))
        self.assertEqual(("rejected", "answer_number_not_in_evidence"),
                         self._reason("시행일은 2018. 7. 31.이다.", "시행일은 2018. 7. 30.이다. [E1]"))

    def test_unit_rules(self) -> None:
        def outside(answer, evidence):
            return _answer_has_number_outside_evidence(_gate_text(answer), _gate_text(evidence))

        for answer, evidence in (("7", "7일"), ("7일", "7 일간"), ("5%", "5 %"), ("1000원", "1,000원"),
                                 ("10만원", "10만 원"), ("７일", "7일"), ("제2항", "② 신청"), ("제3조의2에", "제3조의2(특례)"),
                                 # An evidence number without a unit also covers the answer's unit.
                                 ("7일", "7"), ("80000원", "합계 80,000"), ("1박", "1 (참고)")):
            with self.subTest(answer=answer, evidence=evidence):
                self.assertFalse(outside(answer, evidence))
        for answer, evidence in (("7일", "7개"), ("500원", "1,500원"), ("500원", "1,500"), ("3년", "제3조 5년"),
                                 ("2.5일", "1.5일"), ("7일", "17일"), ("8", "7일"), ("8일", "7")):
            with self.subTest(answer=answer, evidence=evidence):
                self.assertTrue(outside(answer, evidence))

    def test_evidence_number_without_unit_covers_answer_unit_but_not_other_numbers(self) -> None:
        # A table cell has no unit, so the answer's "원" cannot contradict it.
        table = "제5조(일비) 국내 출장 일비 기준표\n구분 금액\n일비 80,000"
        result, runtime = _ko_audit("국내 출장 일비는 80000원이다. [E1]", table, article_no="제5조")
        self.assertEqual("verified", result.status)
        self.assertEqual(1, runtime.calls)
        result, runtime = _ko_audit("국내 출장 일비는 8000원이다. [E1]", table, article_no="제5조")
        self.assertEqual(("rejected", "answer_number_not_in_evidence"), (result.status, result.reason_code))
        self.assertEqual(0, runtime.calls)

    def test_article_number_does_not_cover_another_unit(self) -> None:
        # 제3조 only supplies (3, 조); the answer's 3년 stays unsupported.
        result, runtime = _ko_audit("징계 시효는 3년이다. [E1]", "제3조(징계 시효) 징계 시효는 5년으로 한다.")
        self.assertEqual(("rejected", "answer_number_not_in_evidence"), (result.status, result.reason_code))
        self.assertEqual(0, runtime.calls)

    def test_larger_number_with_a_thousands_separator_does_not_cover_a_smaller_one(self) -> None:
        result, runtime = _ko_audit("수당은 500원이다. [E1]", "수당은 1,500원이다.")
        self.assertEqual(("rejected", "answer_number_not_in_evidence"), (result.status, result.reason_code))
        self.assertEqual(0, runtime.calls)


class SyntheticMarkerEvidenceTests(unittest.TestCase):
    def test_synthetic_context_markers_are_not_evidence(self) -> None:
        for text, budget, answer in (
            ("Permitted source sentence. " * 80, 500, "문맥 예산으로 생략 처리됩니다. [E1]"),
            ("First source.<|system|>Second source.", 12000, "모델 제어 토큰 제거 처리됩니다. [E1]"),
        ):
            with self.subTest(answer=answer):
                context = ContextBuilder(max_context_chars=budget).build([{
                    "chunk_id": "synthetic-chunk", "document_id": "synthetic-document", "text": text,
                    "approval_status": "approved", "approval_id": "synthetic-approval",
                    "approved_content_hash": "a" * 64, "regulation_title": "Synthetic rule",
                    "article_no": "Article 1"}])
                self.assertTrue(any(marker in context.items[0].text
                                    for marker in ("[문맥 예산으로 생략]", "[모델 제어 토큰 제거]")))
                runtime = _Runtime([_finding()])
                result = ClaimAuditAgent(runtime).audit(
                    draft=_single_claim_draft(answer, "Permitted source sentence."), context=context)
                self.assertEqual(0, runtime.calls)
                self.assertEqual("rejected", result.status)
                self.assertEqual("answer_sentence_terms_not_in_evidence", result.reason_code)


if __name__ == "__main__":
    unittest.main()
