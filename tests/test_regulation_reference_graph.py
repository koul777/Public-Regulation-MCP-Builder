from __future__ import annotations

from copy import deepcopy
import unittest

from app.retrieval.regulation_reference_graph import (
    build_regulation_reference_graph,
    canonicalize_article_locator,
)


TENANT = "tenant-a"
PROFILE = "profile-a"


class RegulationReferenceGraphTests(unittest.TestCase):
    def test_resolves_exact_cross_document_article_and_canonicalizes_full_locator(self) -> None:
        raw_reference = {
            "regulation_ref": "대상 규정",
            "article_ref": " 제 16 조의 2 제3항 제2호 가목 ",
            "extractor_evidence": {"offset": 41},
        }
        records = [
            _record(
                "source",
                "출발 규정",
                article="제1조",
                article_refs=[raw_reference],
                record_id="source-chunk",
            ),
            _record(
                "target",
                "대상 규정",
                regulation_no="R-16",
                article="제16조의2제3항제2호가목",
                version="v3",
                effective_from="2026-01-01",
                effective_to="2026-12-31",
                record_id="target-article",
            ),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual(1, len(graph["edges"]))
        edge = graph["edges"][0]
        self.assertEqual("resolved", edge["status"])
        self.assertEqual(
            ["resolved_by_canonical_title"],
            edge["reason_codes"],
        )
        self.assertEqual(["canonical_title"], edge["match_types"])
        self.assertEqual("target", edge["target_unit"]["unit_id"])
        self.assertEqual("R-16", edge["target_unit"]["regulation_no"])
        self.assertEqual("v3", edge["target_unit"]["version"])
        self.assertEqual("2026-01-01", edge["target_unit"]["effective_from"])
        self.assertEqual("2026-12-31", edge["target_unit"]["effective_to"])
        self.assertEqual(
            {
                "locator": "제16조의2제3항제2호가목",
                "article": "제16조의2",
                "paragraph": "제3항",
                "item": "제2호",
                "subitem": "가목",
            },
            edge["target_article"],
        )
        self.assertEqual(edge["target_article"], edge["requested_article"])
        self.assertEqual(raw_reference, edge["raw_mentions"][0]["raw"])
        self.assertEqual(1, edge["mention_count"])

    def test_parallel_article_references_create_distinct_edges(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {"regulation_ref": "인사규정", "article_ref": "제16조"},
                    {"regulation_ref": "인사규정", "article_ref": "제17조제2항"},
                ],
            ),
            _record("personnel", "인사규정", article="제16조"),
            _record(
                "personnel",
                "인사규정",
                article="제17조제2항",
                record_id="personnel-17",
            ),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual(2, len(graph["edges"]))
        self.assertEqual(
            {"제16조", "제17조제2항"},
            {
                edge["target_article"]["locator"]
                for edge in graph["edges"]
            },
        )
        self.assertTrue(
            all(edge["target_unit"]["unit_id"] == "personnel" for edge in graph["edges"])
        )

    def test_same_title_is_ambiguous_but_exact_number_disambiguates(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {"regulation_ref": "운영규정", "article_ref": "제1조"},
                    {
                        "regulation_ref": "운영규정",
                        "regulation_no": "7-2",
                        "article_ref": "제1조",
                    },
                ],
            ),
            _record("rule-a", "운영규정", regulation_no="4-1", article="제1조"),
            _record("rule-b", "운영규정", regulation_no="7-2", article="제1조"),
        ]

        graph = build_regulation_reference_graph(records)

        ambiguous = _only_edge(graph, status="ambiguous")
        self.assertEqual(
            ["ambiguous_canonical_title"],
            ambiguous["reason_codes"],
        )
        self.assertEqual(
            ["rule-a", "rule-b"],
            [candidate["unit_id"] for candidate in ambiguous["candidate_units"]],
        )
        self.assertIsNone(ambiguous["target_unit"])

        resolved = _only_edge(graph, status="resolved")
        self.assertEqual("rule-b", resolved["target_unit"]["unit_id"])
        self.assertEqual(
            ["resolved_by_regulation_no"],
            resolved["reason_codes"],
        )
        self.assertEqual(["regulation_no"], resolved["match_types"])

    def test_alias_conflict_is_ambiguous_and_canonical_title_has_precedence(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {"regulation_ref": "공통별칭", "article_ref": "제1조"},
                    {"regulation_ref": "규정 하나", "article_ref": "제1조"},
                ],
            ),
            _record(
                "rule-a",
                "규정 하나",
                aliases=["공통별칭"],
                article="제1조",
            ),
            _record(
                "rule-b",
                "규정 둘",
                aliases=["공통별칭", "규정 하나"],
                article="제1조",
            ),
        ]

        graph = build_regulation_reference_graph(records)

        ambiguous = _only_edge(graph, status="ambiguous")
        self.assertEqual(["ambiguous_alias"], ambiguous["reason_codes"])
        self.assertEqual(
            ["rule-a", "rule-b"],
            [candidate["unit_id"] for candidate in ambiguous["candidate_units"]],
        )

        resolved = _only_edge(graph, status="resolved")
        self.assertEqual("rule-a", resolved["target_unit"]["unit_id"])
        self.assertEqual(
            ["resolved_by_canonical_title"],
            resolved["reason_codes"],
        )

    def test_exact_alias_resolves_but_near_match_is_not_guessed(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {"regulation_ref": "채용세칙", "article_ref": "제2조"},
                    {"regulation_ref": "채용 세칙 추가", "article_ref": "제2조"},
                ],
            ),
            _record(
                "target",
                "직원채용시행세칙",
                aliases=["채용세칙"],
                article="제2조",
            ),
        ]

        graph = build_regulation_reference_graph(records)

        resolved = _only_edge(graph, status="resolved")
        self.assertEqual(["resolved_by_alias"], resolved["reason_codes"])
        self.assertEqual("target", resolved["target_unit"]["unit_id"])
        unresolved = _only_edge(graph, status="unresolved")
        self.assertEqual(["target_unit_not_found"], unresolved["reason_codes"])
        self.assertIsNone(unresolved["target_unit"])

    def test_missing_target_article_keeps_identified_unit_and_requested_locator(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {
                        "regulation_ref": "대상 규정",
                        "article_ref": "제99조의2제4항",
                    }
                ],
            ),
            _record("target", "대상 규정", article="제1조"),
        ]

        graph = build_regulation_reference_graph(records)

        edge = _only_edge(graph, status="unresolved")
        self.assertEqual(
            ["target_article_not_found"],
            edge["reason_codes"],
        )
        self.assertEqual("target", edge["target_unit"]["unit_id"])
        self.assertEqual(
            "제99조의2제4항",
            edge["requested_article"]["locator"],
        )
        self.assertIsNone(edge["target_article"])

    def test_child_locator_requires_exact_materialized_paragraph_and_item(self) -> None:
        base_records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {"regulation_ref": "대상 규정", "article_ref": "제16조제2항제1호"}
                ],
            ),
            _record("target", "대상 규정", article="제16조"),
        ]

        unresolved_graph = build_regulation_reference_graph(base_records)
        resolved_records = deepcopy(base_records)
        resolved_records[1]["article_locators"] = [
            "제16조",
            "제16조제2항",
            "제16조제2항제1호",
        ]
        resolved_graph = build_regulation_reference_graph(resolved_records)

        unresolved = _only_edge(unresolved_graph, status="unresolved")
        self.assertEqual(["target_article_not_found"], unresolved["reason_codes"])
        self.assertIsNone(unresolved["target_article"])
        resolved = _only_edge(resolved_graph, status="resolved")
        self.assertEqual("제16조제2항제1호", resolved["target_article"]["locator"])

    def test_invalid_and_missing_article_locators_have_explicit_reason_codes(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {"regulation_ref": "대상 규정", "article_ref": "16조쯤"},
                    {"regulation_ref": "대상 규정"},
                ],
            ),
            _record("target", "대상 규정", article="제16조"),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual(2, len(graph["edges"]))
        self.assertEqual(
            {
                "invalid_article_locator",
                "article_locator_missing",
            },
            {
                edge["reason_codes"][0]
                for edge in graph["edges"]
            },
        )
        self.assertTrue(all(edge["status"] == "unresolved" for edge in graph["edges"]))

    def test_tenant_and_profile_isolation_do_not_expose_foreign_candidates(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                tenant_id="tenant-a",
                profile_id="profile-a",
                article_refs=[
                    {"regulation_ref": "격리 대상", "article_ref": "제1조"}
                ],
            ),
            _record(
                "foreign-tenant",
                "격리 대상",
                tenant_id="tenant-b",
                profile_id="profile-a",
                article="제1조",
            ),
            _record(
                "foreign-profile",
                "격리 대상",
                tenant_id="tenant-a",
                profile_id="profile-b",
                article="제1조",
            ),
        ]

        graph = build_regulation_reference_graph(records)

        edge = _only_edge(graph, status="unresolved")
        self.assertEqual(["target_unit_not_found"], edge["reason_codes"])
        self.assertIsNone(edge["target_unit"])
        self.assertEqual([], edge["candidate_units"])
        self.assertEqual(0, graph["stats"]["resolved_unit_arc_count"])

    def test_chunk_style_metadata_and_regulation_unit_id_alias_are_supported(self) -> None:
        records = [
            {
                "chunk_id": "source-chunk",
                "approval_status": "approved",
                "metadata": {
                    "tenant_id": TENANT,
                    "profile_id": PROFILE,
                    "regulation_unit_id": "source",
                    "regulation_title": "출발 규정",
                    "article_no": "제1조",
                    "regulation_article_refs": [
                        {
                            "regulation_ref": "대상 규정",
                            "article_ref": "제2조",
                        }
                    ],
                },
            },
            {
                "chunk_id": "target-chunk",
                "approval_status": "approved",
                "metadata": {
                    "tenant_id": TENANT,
                    "profile_id": PROFILE,
                    "regulation_unit_id": "target",
                    "regulation_title": "대상 규정",
                    "article_no": "제2조",
                },
            },
        ]

        graph = build_regulation_reference_graph(records)

        edge = _only_edge(graph, status="resolved")
        self.assertEqual("source", edge["source_unit"]["unit_id"])
        self.assertEqual("target", edge["target_unit"]["unit_id"])
        self.assertEqual("source-chunk", edge["evidence"][0]["chunk_id"])

    def test_two_unit_cycle_uses_resolved_unit_only_references(self) -> None:
        records = [
            _record("a", "규정 A", unit_refs=["규정 B"]),
            _record("b", "규정 B", unit_refs=["규정 A"]),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual(1, len(graph["cycles"]))
        cycle = graph["cycles"][0]
        self.assertEqual(["a", "b"], cycle["unit_ids"])
        self.assertEqual(2, cycle["size"])
        self.assertFalse(cycle["self_loop"])
        self.assertEqual(2, cycle["internal_unit_edge_count"])
        self.assertTrue(
            all(edge["edge_type"] == "regulation_reference" for edge in graph["edges"])
        )

    def test_three_unit_cycle_is_one_tarjan_component(self) -> None:
        records = [
            _record(
                "a",
                "규정 A",
                article_refs=[{"regulation_ref": "규정 B", "article_ref": "제1조"}],
            ),
            _record(
                "b",
                "규정 B",
                article_refs=[{"regulation_ref": "규정 C", "article_ref": "제1조"}],
            ),
            _record(
                "c",
                "규정 C",
                article_refs=[{"regulation_ref": "규정 A", "article_ref": "제1조"}],
            ),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual(1, len(graph["cycles"]))
        self.assertEqual(["a", "b", "c"], graph["cycles"][0]["unit_ids"])
        self.assertEqual(3, graph["cycles"][0]["internal_unit_edge_count"])

    def test_self_loop_is_reported_as_cycle(self) -> None:
        records = [
            _record(
                "self",
                "자기 규정",
                article_refs=[
                    {"regulation_ref": "자기 규정", "article_ref": "제1조"}
                ],
            )
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual(1, len(graph["cycles"]))
        self.assertEqual(["self"], graph["cycles"][0]["unit_ids"])
        self.assertTrue(graph["cycles"][0]["self_loop"])
        self.assertEqual(1, graph["cycles"][0]["internal_unit_edge_count"])

    def test_dag_has_no_cycles(self) -> None:
        records = [
            _record("a", "규정 A", unit_refs=["규정 B"]),
            _record("b", "규정 B", unit_refs=["규정 C"]),
            _record("c", "규정 C"),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual([], graph["cycles"])
        self.assertEqual(2, graph["stats"]["resolved_unit_arc_count"])

    def test_ambiguous_and_unresolved_edges_are_excluded_from_cycles(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                unit_refs=["공통명", "없는 규정"],
            ),
            _record("one", "공통명"),
            _record("two", "공통명"),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual([], graph["cycles"])
        self.assertEqual(0, graph["stats"]["resolved_unit_arc_count"])
        self.assertEqual(
            {"ambiguous", "unresolved"},
            {edge["status"] for edge in graph["edges"]},
        )

    def test_duplicate_mentions_deduplicate_edge_and_preserve_evidence_counts(self) -> None:
        duplicate = {"regulation_ref": "대상 규정", "article_ref": "제2조"}
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[duplicate, deepcopy(duplicate)],
                record_id="source-chunk-1",
            ),
            _record(
                "source",
                "출발 규정",
                article_refs=[deepcopy(duplicate)],
                record_id="source-chunk-2",
            ),
            _record("target", "대상 규정", article="제2조"),
        ]

        graph = build_regulation_reference_graph(records)

        edge = _only_edge(graph, status="resolved")
        self.assertEqual(1, graph["stats"]["edge_count"])
        self.assertEqual(3, edge["mention_count"])
        self.assertEqual(
            [{"raw": duplicate, "count": 3}],
            edge["raw_mentions"],
        )
        self.assertEqual(2, edge["evidence_count"])
        self.assertEqual(
            [1, 2],
            sorted(item["mention_count"] for item in edge["evidence"]),
        )

    def test_exact_article_reference_suppresses_redundant_unit_reference(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {"regulation_ref": "대상 규정", "article_ref": "제2조"}
                ],
                unit_refs=["대상 규정"],
            ),
            _record("target", "대상 규정", article="제2조"),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual(1, len(graph["edges"]))
        self.assertEqual("regulation_article_reference", graph["edges"][0]["edge_type"])
        self.assertEqual(
            1,
            graph["stats"]["suppressed_redundant_unit_reference_count"],
        )

    def test_graph_is_identical_when_record_and_reference_order_change(self) -> None:
        records = [
            _record(
                "a",
                "규정 A",
                regulation_no="A-1",
                aliases=["A 별칭"],
                article_refs=[
                    {"regulation_ref": "규정 B", "article_ref": "제2조"},
                    {"regulation_ref": "규정 C", "article_ref": "제3조"},
                ],
                record_id="a-1",
            ),
            _record(
                "b",
                "규정 B",
                regulation_no="B-1",
                article="제2조",
                unit_refs=["규정 A"],
                record_id="b-2",
            ),
            _record(
                "c",
                "규정 C",
                regulation_no="C-1",
                article="제3조",
                record_id="c-3",
            ),
        ]
        reordered = deepcopy(list(reversed(records)))
        for record in reordered:
            if record.get("regulation_article_refs"):
                record["regulation_article_refs"].reverse()

        first = build_regulation_reference_graph(records)
        second = build_regulation_reference_graph(reordered)

        self.assertEqual(first, second)

    def test_unapproved_records_are_excluded_as_sources_and_targets(self) -> None:
        records = [
            _record(
                "source",
                "출발 규정",
                article_refs=[
                    {"regulation_ref": "초안 규정", "article_ref": "제1조"}
                ],
            ),
            _record(
                "draft-target",
                "초안 규정",
                article="제1조",
                approval_status="draft",
            ),
            _record(
                "draft-source",
                "다른 초안",
                article_refs=[
                    {"regulation_ref": "출발 규정", "article_ref": "제1조"}
                ],
                approval_status="needs_review",
            ),
        ]

        graph = build_regulation_reference_graph(records)

        self.assertEqual(["source"], [unit["unit_id"] for unit in graph["units"]])
        edge = _only_edge(graph, status="unresolved")
        self.assertEqual(["target_unit_not_found"], edge["reason_codes"])
        self.assertEqual(3, graph["stats"]["input_record_count"])
        self.assertEqual(1, graph["stats"]["approved_record_count"])
        self.assertEqual(2, graph["stats"]["excluded_unapproved_record_count"])
        self.assertEqual(1, graph["stats"]["reference_mention_count"])

    def test_absent_target_preserves_bounded_title_and_article_locator(self) -> None:
        graph = build_regulation_reference_graph(
            [
                _record(
                    "source",
                    "공개 준용규정",
                    article="제1조",
                    article_refs=[
                        {
                            "regulation_ref": "  재무규정  ",
                            "article_ref": "제16조",
                        }
                    ],
                )
            ]
        )

        edge = _only_edge(graph, status="unresolved")
        self.assertEqual("target_unit_not_found", edge["reason_codes"][0])
        self.assertIsNone(edge["target_unit"])
        self.assertEqual("재무규정", edge["requested_target_title"])
        self.assertEqual("제16조", edge["requested_article"]["locator"])
        self.assertLessEqual(len(edge["requested_target_title"]), 300)
        self.assertNotIn("profile_id", edge["requested_target_title"])

    def test_article_locator_canonicalizer_supports_article_paragraph_item_and_subitem(self) -> None:
        self.assertEqual(
            {
                "locator": "제16조의2제3항제4호가목",
                "article": "제16조의2",
                "paragraph": "제3항",
                "item": "제4호",
                "subitem": "가목",
            },
            canonicalize_article_locator(" 제 16 조 의 2 제 3 항 제4호 가 목 "),
        )
        self.assertEqual(
            "제16조제2항제3호제4목",
            canonicalize_article_locator("제16조 2항 3호 4목")["locator"],
        )
        self.assertIsNone(canonicalize_article_locator("제16절"))


def _record(
    unit_id: str,
    title: str,
    *,
    tenant_id: str = TENANT,
    profile_id: str = PROFILE,
    regulation_no: str | None = None,
    aliases: list[str] | None = None,
    article: str | None = "제1조",
    article_refs: list[dict[str, object]] | None = None,
    unit_refs: list[str] | None = None,
    approval_status: str = "approved",
    record_id: str | None = None,
    version: str | None = "current",
    effective_from: str | None = "2026-01-01",
    effective_to: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "unit_id": unit_id,
        "title": title,
        "approval_status": approval_status,
        "record_id": record_id or f"{unit_id}:{article or 'unit'}",
        "version": version,
        "effective_from": effective_from,
    }
    if regulation_no is not None:
        record["regulation_no"] = regulation_no
    if aliases is not None:
        record["aliases"] = aliases
    if article is not None:
        record["article_locator"] = article
    if article_refs is not None:
        record["regulation_article_refs"] = article_refs
    if unit_refs is not None:
        record["internal_regulation_refs"] = unit_refs
    if effective_to is not None:
        record["effective_to"] = effective_to
    return record


def _only_edge(
    graph: dict[str, object],
    *,
    status: str,
) -> dict[str, object]:
    edges = [
        edge
        for edge in graph["edges"]
        if edge["status"] == status
    ]
    if len(edges) != 1:
        raise AssertionError(
            f"Expected exactly one {status!r} edge, found {len(edges)}: {edges!r}"
        )
    return edges[0]


if __name__ == "__main__":
    unittest.main()
