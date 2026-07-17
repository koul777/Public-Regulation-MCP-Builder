from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_relation_graph import (
    build_relation_edges,
    clean_internal_reference_name,
    clean_law_reference_name,
    candidate_terms,
    clean_term,
    export_relation_graph,
    relation_manifest,
)


class ExportRelationGraphTests(unittest.TestCase):
    def test_term_cleaning_strips_particles_and_article_suffix_noise(self) -> None:
        self.assertEqual(clean_term("계약담당자는"), "계약담당자")
        self.assertEqual(clean_term("PF보증에"), "PF보증")
        self.assertEqual(clean_term("교육훈련규칙제"), "교육훈련규칙")
        self.assertEqual(clean_term("근로기준법"), "근로기준법")
        self.assertEqual(clean_term("및전자입찰특별유의서"), "전자입찰특별유의서")
        self.assertEqual(clean_term("계약기준별표33"), "계약기준")
        self.assertEqual(clean_term("위반하여계약사무규칙"), "계약사무규칙")
        self.assertEqual(clean_term("통해계약사무규칙"), "계약사무규칙")
        self.assertEqual(clean_term("정하여계약사무규칙"), "계약사무규칙")
        self.assertEqual(clean_term("계약직관리지침계약직관리지침계약직관리지침"), "계약직관리지침")
        self.assertEqual(clean_term("재단은임신중인직원이유산의경험등근로기준법시행령제43조"), "근로기준법시행령")
        self.assertIsNone(clean_term("규정"))
        self.assertIsNone(clean_term("내규"))
        self.assertIsNone(clean_term("시행령"))
        self.assertIsNone(clean_term("법시행령"))
        self.assertIsNone(clean_term("정관"))
        self.assertIsNone(clean_term("인사0100"))
        self.assertIsNone(clean_term("경영조직0700"))

        terms = set(candidate_terms("계약담당자는 계약의 방법을 따르고 교육훈련규칙제12조를 확인한다."))

        self.assertIn("계약담당자", terms)
        self.assertIn("교육훈련규칙", terms)
        self.assertNotIn("계약의", terms)
        self.assertNotIn("인사0100", set(candidate_terms("인사0100 인사관리규정 복무규정")))
        self.assertIn("인사관리규정", set(candidate_terms("인사0100 인사관리규정 복무규정")))

    def test_clean_internal_reference_name_strips_leading_clause_noise(self) -> None:
        noisy = (
            "\ubaa9\uc758 \uace0\uc2dc \uae08\uc561\uc5d0\ub3c4 \ubd88\uad6c\ud558\uace0 "
            "\uacf5\uae30\uc5c5\u318d\uc900\uc815\ubd80\uae30\uad00 \uacc4\uc57d\uc0ac\ubb34\uaddc\uce59"
        )

        self.assertEqual(
            clean_internal_reference_name(noisy),
            "\uacf5\uae30\uc5c5\u318d\uc900\uc815\ubd80\uae30\uad00 \uacc4\uc57d\uc0ac\ubb34\uaddc\uce59",
        )
        self.assertEqual(clean_internal_reference_name("통해 계약사무규칙"), "계약사무규칙")
        self.assertEqual(clean_internal_reference_name("위반하여 계약사무규칙"), "계약사무규칙")
        self.assertEqual(clean_internal_reference_name("정하여 계약사무규칙"), "계약사무규칙")
        self.assertEqual(
            clean_internal_reference_name(
                "계약업무규정 -> 계약업무규정 계약업무규정 제1조 목적 "
                "제2조(다른 사규의 개정) ① ｢물품관리규정"
            ),
            "물품관리규정",
        )

    def test_split_public_contract_rule_is_external_law_article(self) -> None:
        self.assertEqual(
            clean_law_reference_name("공 기업·준정부기관계약사무규 칙"),
            "공기업·준정부기관계약사무규칙",
        )
        chunks = [
            {
                "document_id": "doc_contract",
                "chunk_id": "article_12",
                "chunk_type": "article",
                "article_no": "제12조",
                "article_title": "수의계약의 제한",
                "regulation_no": "계약및검사검수규정",
                "text": "제12조의2(수의계약의제한) 「공 기업·준정부기관계약사무규 칙」제8조제3항에 해당하는 경우 수의계약을 체결하여서는 아니된다.",
                "article_refs": ["제8조제3항"],
                "reference_edges": [{"type": "article", "value": "제8조제3항", "resolved": False}],
            }
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        law_edges = [edge for edge in edges if edge["relation_type"] == "article_cites_law_article"]
        self.assertEqual(len(law_edges), 1)
        self.assertEqual(law_edges[0]["target_label"], "공기업·준정부기관계약사무규칙 제8조제3항")
        self.assertEqual(law_edges[0]["metadata"]["scope"], "external")
        self.assertFalse(any(edge["relation_type"] == "article_cites_regulation_article" for edge in edges))

    def test_metadata_public_contract_rule_refs_are_external_law_edges(self) -> None:
        chunks = [
            {
                "document_id": "doc_contract",
                "chunk_id": "article_12",
                "chunk_type": "article",
                "article_no": "제12조",
                "article_title": "수의계약의 제한",
                "regulation_no": "계약및검사검수규정",
                "text": "공기업·준정부기관계약사무규칙에 따른다.",
                "regulation_article_refs": [
                    {"regulation_ref": "공 기업·준정부기관계약사무규칙", "article_ref": "제8조제3항"}
                ],
                "internal_regulation_refs": ["공기업·준정부기관계약사무규 칙"],
            }
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        self.assertTrue(
            any(
                edge["relation_type"] == "article_cites_law_article"
                and edge["target_label"] == "공기업·준정부기관계약사무규칙 제8조제3항"
                for edge in edges
            )
        )
        self.assertTrue(
            any(
                edge["relation_type"] == "article_cites_law"
                and edge["target_label"] == "공기업·준정부기관계약사무규칙"
                for edge in edges
            )
        )
        self.assertFalse(any(edge["relation_type"] == "article_cites_regulation_article" for edge in edges))

    def test_builds_reference_table_and_term_edges(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "chunk_1",
                "chunk_type": "article",
                "source_page_start": 3,
                "source_page_end": 3,
                "text": "제3조는 제5조를 따른다. 「인사규정」 제7조와 「국가계약법 시행령」 제12조 및 “민원처리예규” 제15조를 적용한다.",
                "article_no": "제3조",
                "article_title": "계약 처리",
                "regulation_no": "계약규정",
                "regulation_title": "계약규정",
                "article_refs": ["제5조", "제7조", "제12조"],
                "internal_regulation_refs": ["인사규정"],
                "external_law_refs": ["국가계약법 시행령"],
                "reference_edges": [
                    {
                        "type": "article",
                        "value": "제5조",
                        "resolved": True,
                        "target_document_id": "doc_a",
                        "target_regulation_no": "계약규정",
                        "target_article_no": "제5조",
                        "target_article_title": "계약 방법",
                    }
                ],
                "table_like": True,
                "table_id": "table_1",
                "table_title": "계약 제한 기간",
                "table_confidence": 0.9,
                "table_cell_rows": [
                    {"row_index": 0, "cells": ["기준", "기간"], "raw": "기준 기간"},
                    {"row_index": 1, "cells": ["뇌물 제공", "2년"], "raw": "뇌물 제공 2년"},
                ],
                "table_records": [
                    {
                        "row_index": 1,
                        "record": {"기준": "뇌물 제공", "기간": "2년"},
                    }
                ],
            }
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=8)
        relation_types = {edge["relation_type"] for edge in edges}

        self.assertIn("article_cites_article", relation_types)
        self.assertIn("article_cites_regulation_article", relation_types)
        self.assertIn("article_cites_law_article", relation_types)
        self.assertIn("article_cites_regulation", relation_types)
        self.assertIn("article_cites_law", relation_types)
        self.assertIn("chunk_has_table", relation_types)
        self.assertIn("table_has_column", relation_types)
        self.assertIn("table_row_has_cell", relation_types)
        self.assertIn("table_row_has_field", relation_types)
        self.assertIn("table_row_has_value", relation_types)
        self.assertIn("table_field_has_value", relation_types)
        self.assertIn("term_cooccurs_with_term", relation_types)

        article_edge = next(edge for edge in edges if edge["relation_type"] == "article_cites_article")
        self.assertTrue(article_edge["metadata"]["resolved"])
        self.assertIn("제5조", article_edge["target_label"])
        law_article_edge = next(edge for edge in edges if edge["relation_type"] == "article_cites_law_article")
        self.assertEqual(law_article_edge["target_label"], "국가계약법 시행령 제12조")
        self.assertTrue(
            any(edge["relation_type"] == "article_cites_regulation_article" and edge["target_label"] == "민원처리예규 제15조" for edge in edges)
        )
        self.assertFalse(
            any(edge["relation_type"] == "article_cites_article" and edge["evidence_text"] == "제12조" for edge in edges)
        )

        table_field_edges = [edge for edge in edges if edge["relation_type"] == "table_row_has_field"]
        self.assertTrue(any(edge["target_label"] == "기간" and edge["metadata"]["value"] == "2년" for edge in table_field_edges))
        value_edges = [edge for edge in edges if edge["relation_type"] == "table_row_has_value"]
        self.assertTrue(any(edge["target_label"] == "2년" and edge["metadata"]["field"] == "기간" for edge in value_edges))

    def test_resolves_article_clause_reference_to_base_article(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "article_44",
                "chunk_type": "article",
                "article_no": "제44조",
                "article_title": "휴직",
                "regulation_no": "취업규칙",
                "text": "제44조(휴직) 휴직 기준을 정한다.",
            },
            {
                "document_id": "doc_a",
                "chunk_id": "article_45",
                "chunk_type": "article",
                "article_no": "제45조",
                "article_title": "휴직기간",
                "regulation_no": "취업규칙",
                "text": "제44조제1항제1호에 따른 휴직기간은 별도로 정한다.",
                "article_refs": ["제44조제1항제1호"],
                "reference_edges": [
                    {
                        "type": "article",
                        "value": "제44조제1항제1호",
                        "resolved": False,
                    }
                ],
            },
        ]

        edges = build_relation_edges(chunks)

        clause_edge = next(edge for edge in edges if edge["relation_type"] == "article_cites_article_clause")
        self.assertTrue(clause_edge["metadata"]["resolved"])
        self.assertTrue(clause_edge["metadata"]["base_article_resolved"])
        self.assertEqual(clause_edge["metadata"]["base_article_ref"], "제44조")
        self.assertIn("제44조", clause_edge["target_label"])

    def test_builds_unquoted_regulation_article_reference_edges(self) -> None:
        chunks = [
            {
                "document_id": "doc_hiring",
                "chunk_id": "article_8",
                "chunk_type": "article",
                "article_no": "제8조",
                "article_title": "자격요건",
                "regulation_no": "채용업무지침",
                "regulation_title": "채용업무지침",
                "text": "규정 제20조에 따른 임용 결격사유에 해당하지 않는 자",
                "internal_regulation_refs": ["규정"],
                "regulation_article_refs": [{"regulation_ref": "규정", "article_ref": "제20조"}],
            }
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        edge = next(edge for edge in edges if edge["relation_type"] == "article_cites_regulation_article")
        self.assertEqual(edge["target_label"], "규정 제20조")
        self.assertEqual(edge["evidence_type"], "regulation_article_ref")
        self.assertEqual(edge["metadata"]["reference_name"], "규정")
        self.assertEqual(edge["metadata"]["article_ref"], "제20조")

    def test_resolves_same_law_enforcement_rule_aliases(self) -> None:
        chunks = [
            {
                "document_id": "doc_contract",
                "chunk_id": "article_11",
                "chunk_type": "article",
                "article_no": "제11조",
                "article_title": "경쟁입찰",
                "regulation_no": "계약규정",
                "text": "「국가계약법」, 「동시행령」 및 「동시행규칙」제24조제2항에 따라 정한다.",
                "external_law_refs": ["국가계약법", "동시행령"],
                "internal_regulation_refs": ["동시행규칙"],
                "regulation_article_refs": [{"regulation_ref": "동시행규칙", "article_ref": "제24조제2항"}],
            }
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        law_edges = [
            edge
            for edge in edges
            if edge["relation_type"] == "article_cites_law_article"
            and edge["target_label"] == "국가계약법 시행규칙 제24조제2항"
        ]
        self.assertTrue(law_edges)
        self.assertTrue(all(edge["metadata"]["alias_resolved"] for edge in law_edges))
        self.assertFalse(
            any(
                edge["relation_type"] == "article_cites_regulation_article"
                and "동시행규칙" in edge["target_label"]
                for edge in edges
            )
        )
        self.assertFalse(
            any(
                edge["relation_type"] == "article_cites_regulation"
                and edge["target_label"] == "동시행규칙"
                for edge in edges
            )
        )

    def test_infers_carried_regulation_context_for_unqualified_article_refs(self) -> None:
        personnel_rule = "\uc778\uc0ac\uaddc\uc815"
        guide = "\ubcf5\ubb34\ud3b8\ub78c"
        article_36 = "\uc81c36\uc870"
        article_36_clause = "\uc81c36\uc870\uc81c1\ud56d"
        chunks = [
            {
                "document_id": "doc_guide",
                "chunk_id": "context_chunk",
                "chunk_type": "paragraph",
                "source_page_start": 31,
                "source_page_end": 31,
                "regulation_no": guide,
                "text": f"{personnel_rule} {article_36}(\uadfc\ubb34\uc2dc\uac04)",
                "internal_regulation_refs": [personnel_rule],
                "regulation_article_refs": [{"regulation_ref": personnel_rule, "article_ref": article_36}],
            },
            {
                "document_id": "doc_guide",
                "chunk_id": "child_chunk",
                "chunk_type": "paragraph",
                "source_page_start": 32,
                "source_page_end": 32,
                "regulation_no": guide,
                "text": f"\uc9c1\uc6d0\uc740 {article_36_clause} \ubc0f \uc81c2\ud56d\uc5d0 \ub530\ub978 \uadfc\ubb34\uc2dc\uac04\uc744 \ubcc0\uacbd\ud560 \uc218 \uc788\ub2e4.",
                "article_refs": [article_36_clause],
                "reference_edges": [{"type": "article", "value": article_36_clause, "resolved": False}],
            },
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        inferred_edge = next(
            edge
            for edge in edges
            if edge["relation_type"] == "chunk_cites_regulation_article"
            and edge["evidence_text"] == article_36_clause
        )
        self.assertEqual(inferred_edge["target_label"], f"{personnel_rule} {article_36_clause}")
        self.assertEqual(inferred_edge["evidence_type"], "contextual_article_ref")
        self.assertEqual(inferred_edge["metadata"]["context_scope"], "carried")
        self.assertEqual(inferred_edge["metadata"]["resolution_status"], "context_regulation_inferred")
        self.assertFalse(
            any(
                edge["relation_type"] == "article_cites_article"
                and edge["evidence_text"] == article_36_clause
                for edge in edges
            )
        )

    def test_infers_same_page_lookahead_regulation_context_for_unqualified_refs(self) -> None:
        personnel_rule = "\uc778\uc0ac\uaddc\uc815"
        guide = "\ubcf5\ubb34\ud3b8\ub78c"
        article_47_clause = "\uc81c47\uc870\uc81c2\ud56d"
        chunks = [
            {
                "document_id": "doc_guide",
                "chunk_id": "previous_paragraph",
                "chunk_type": "paragraph",
                "source_page_start": 104,
                "source_page_end": 104,
                "regulation_no": guide,
                "text": f"\uc9c8\ubcd1\uc774\ub098 \ubd80\uc0c1\uc73c\ub85c \uc778\ud55c \uc9c0\uac01\uc740 {article_47_clause}\uc5d0 \ub530\ub77c \uacf5\uc81c\ud55c\ub2e4.",
                "article_refs": [article_47_clause],
                "reference_edges": [{"type": "article", "value": article_47_clause, "resolved": False}],
            },
            {
                "document_id": "doc_guide",
                "chunk_id": "next_context",
                "chunk_type": "paragraph",
                "source_page_start": 104,
                "source_page_end": 104,
                "regulation_no": guide,
                "text": f"{personnel_rule} \uc81c51\uc870(\ubcd1\uac00)",
                "internal_regulation_refs": [personnel_rule],
                "regulation_article_refs": [{"regulation_ref": personnel_rule, "article_ref": "\uc81c51\uc870"}],
            },
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        inferred_edge = next(
            edge
            for edge in edges
            if edge["relation_type"] == "chunk_cites_regulation_article"
            and edge["evidence_text"] == article_47_clause
        )
        self.assertEqual(inferred_edge["target_label"], f"{personnel_rule} {article_47_clause}")
        self.assertEqual(inferred_edge["metadata"]["context_scope"], "lookahead")
        self.assertFalse(
            any(
                edge["relation_type"] == "article_cites_article"
                and edge["evidence_text"] == article_47_clause
                for edge in edges
            )
        )

    def test_treats_inline_article_headings_in_appendix_as_definitions(self) -> None:
        chunks = [
            {
                "document_id": "doc_rules",
                "chunk_id": "appendix_1",
                "chunk_type": "appendix",
                "source_page_start": 8,
                "source_page_end": 8,
                "regulation_no": "업무규정",
                "text": "제18조(승진의 원칙) 승진은 능력에 따른다. 제19조 <삭제 2024. 1. 1.> 제20조(사직) 직원은 사직할 수 있다. 제21조의3 삭제<2026. 7. 6.>",
                "article_refs": ["제18조", "제19조", "제20조", "제21조의3"],
                "reference_edges": [
                    {"type": "article", "value": "제18조", "resolved": False},
                    {"type": "article", "value": "제19조", "resolved": False},
                    {"type": "article", "value": "제20조", "resolved": False},
                    {"type": "article", "value": "제21조의3", "resolved": False},
                ],
            }
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        definition_edges = [edge for edge in edges if edge["relation_type"] == "chunk_defines_inline_article"]
        self.assertEqual(
            {edge["metadata"]["article_ref"] for edge in definition_edges},
            {"제18조", "제19조", "제20조", "제21조의3"},
        )
        self.assertFalse(any(edge["relation_type"] == "article_cites_article" for edge in edges))

    def test_infers_form_article_refs_from_external_law_block(self) -> None:
        law_name = "국가를 당사자로 하는 계약에 관한 법률 시행령"
        chunks = [
            {
                "document_id": "doc_contract",
                "chunk_id": "article_43",
                "chunk_type": "article",
                "article_no": "제43조",
                "article_title": "계약체결",
                "regulation_no": "계약사무처리지침",
                "text": "제43조(계약체결) 계약체결 기준을 정한다.",
            },
            {
                "document_id": "doc_contract",
                "chunk_id": "form_4",
                "chunk_type": "form",
                "regulation_no": "계약사무처리지침",
                "text": (
                    f"「{law_name}」\n"
                    "제35조(입찰공고의 시기) ④ 각 호에 따른다. "
                    "⑤ 제43조에 따른 협상에 의한 계약 또는 제43조의3에 따른 경쟁적 대화에 의한 계약."
                ),
                "external_law_refs": [law_name],
                "article_refs": ["제43조", "제43조의3"],
                "reference_edges": [
                    {
                        "type": "article",
                        "value": "제43조",
                        "resolved": True,
                        "target_document_id": "doc_contract",
                        "target_chunk_id": "article_43",
                        "target_regulation_no": "계약사무처리지침",
                        "target_article_no": "제43조",
                    },
                    {"type": "article", "value": "제43조의3", "resolved": False},
                ],
            },
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        law_edges = [
            edge
            for edge in edges
            if edge["relation_type"] == "chunk_cites_law_article"
            and edge["evidence_type"] == "contextual_law_article_ref"
        ]
        self.assertEqual({edge["metadata"]["article_ref"] for edge in law_edges}, {"제43조", "제43조의3"})
        self.assertTrue(all(edge["metadata"]["reference_name"] == law_name for edge in law_edges))
        self.assertFalse(
            any(
                edge["relation_type"] == "article_cites_article"
                and edge["evidence_text"] in {"제43조", "제43조의3"}
                for edge in edges
            )
        )

    def test_infers_attached_external_law_article_ref_in_article_chunk(self) -> None:
        chunks = [
            {
                "document_id": "doc_work_rule",
                "chunk_id": "article_55",
                "chunk_type": "article",
                "article_no": "제55조",
                "article_title": "출산전후휴가",
                "regulation_no": "취업규칙",
                "text": "제55조(출산전후휴가) 정상적인사업운영에중대한지장을초래하는경우등근로기준\n법시행령제43조의3 제2항에 따라 휴가를 부여한다.",
                "external_law_refs": ["임신기간"],
                "article_refs": ["제43조의3제2항"],
                "reference_edges": [{"type": "article", "value": "제43조의3제2항", "resolved": False}],
            }
        ]

        edges = build_relation_edges(chunks, max_terms_per_chunk=4)

        law_edge = next(edge for edge in edges if edge["relation_type"] == "article_cites_law_article")
        self.assertEqual(law_edge["target_label"], "근로기준법시행령 제43조의3제2항")
        self.assertEqual(law_edge["metadata"]["reference_name"], "근로기준법시행령")
        self.assertEqual(law_edge["evidence_type"], "contextual_law_article_ref")
        self.assertFalse(
            any(
                edge["relation_type"] == "article_cites_article"
                and edge["evidence_text"] == "제43조의3제2항"
                for edge in edges
            )
        )

    def test_manifest_summarizes_unresolved_article_statuses(self) -> None:
        chunks = [
            {
                "document_id": "doc_a",
                "chunk_id": "form_1",
                "chunk_type": "form",
                "regulation_no": "계약규정",
                "reference_edges": [{"type": "article", "value": "제99조", "resolved": False}],
                "article_refs": ["제99조"],
                "text": "별지 서식에서 제99조를 참조한다.",
            }
        ]

        edges = build_relation_edges(chunks)
        manifest = relation_manifest(
            edges,
            chunks=chunks,
            source_label="fixture",
            source_batch_generated_at=None,
            input_count=1,
            successful_count=1,
            max_terms_per_chunk=12,
        )

        self.assertEqual(manifest["unresolved_article_edge_count"], 1)
        self.assertEqual(manifest["unresolved_article_by_status"]["form_context_target_not_indexed"], 1)
        self.assertEqual(manifest["unresolved_article_by_chunk_type"]["form"], 1)

    def test_exports_relation_graph_from_batch_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk_path = root / "data" / "exports" / "doc_a.jsonl"
            chunk_path.parent.mkdir(parents=True)
            chunk_path.write_text(
                json.dumps(
                    {
                        "document_id": "doc_a",
                        "chunk_id": "chunk_a",
                        "article_no": "제1조",
                        "regulation_no": "복무규정",
                        "internal_regulation_refs": ["인사규정"],
                        "external_law_refs": ["근로기준법"],
                        "text": "복무규정은 인사규정과 근로기준법을 참고한다.",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            batch_path = root / "batch.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-07T00:00:00+00:00",
                        "input_count": 1,
                        "successful_count": 1,
                        "rows": [
                            {
                                "document_id": "doc_a",
                                "filename": "sample.hwp",
                                "status": "completed",
                                "quality_json": str(chunk_path.with_suffix(".quality.json")),
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            out_jsonl = root / "relations.jsonl"
            out_manifest = root / "relations.manifest.json"

            manifest = export_relation_graph(
                batch_report_path=batch_path,
                out_jsonl=out_jsonl,
                out_manifest=out_manifest,
            )

            self.assertTrue(out_jsonl.is_file())
            self.assertTrue(out_manifest.is_file())
            self.assertGreater(manifest["edge_count"], 0)
            self.assertIn("article_cites_regulation", manifest["relation_type_counts"])

    def test_exports_relation_graph_from_multiple_batch_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_dir = root / "data" / "exports"
            export_dir.mkdir(parents=True)
            batch_paths = []
            for index, article_no in enumerate(["제1조", "제2조"], start=1):
                document_id = f"doc_{index}"
                chunk_path = export_dir / f"{document_id}.jsonl"
                chunk_path.write_text(
                    json.dumps(
                        {
                            "document_id": document_id,
                            "chunk_id": f"chunk_{index}",
                            "article_no": article_no,
                            "regulation_no": "복무규정",
                            "text": "복무규정은 인사규정과 근로기준법을 참고한다.",
                            "internal_regulation_refs": ["인사규정"],
                            "external_law_refs": ["근로기준법"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                batch_path = root / f"batch_{index}.json"
                batch_path.write_text(
                    json.dumps(
                        {
                            "generated_at": f"2026-07-07T00:00:0{index}+00:00",
                            "input_count": 1,
                            "successful_count": 1,
                            "rows": [
                                {
                                    "document_id": document_id,
                                    "filename": f"sample_{index}.hwp",
                                    "status": "completed",
                                    "quality_json": str(chunk_path.with_suffix(".quality.json")),
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                batch_paths.append(batch_path)

            manifest = export_relation_graph(
                batch_report_path=batch_paths,
                out_jsonl=root / "relations.jsonl",
                out_manifest=root / "relations.manifest.json",
            )

            self.assertEqual(manifest["input_count"], 2)
            self.assertEqual(manifest["successful_count"], 2)
            self.assertEqual(manifest["chunk_count"], 2)
            self.assertIn("batch_1.json", manifest["source"])
            self.assertIn("batch_2.json", manifest["source"])


if __name__ == "__main__":
    unittest.main()
