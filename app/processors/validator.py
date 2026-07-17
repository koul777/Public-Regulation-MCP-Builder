from __future__ import annotations

import re

from app.schemas.chunk import Chunk, ChunkOptions
from app.schemas.structure import StructureNode
from app.schemas.validation import ValidationIssue


class Validator:
    def validate(
        self,
        nodes: list[StructureNode],
        chunks: list[Chunk],
        document_id: str,
        options: ChunkOptions | None = None,
    ) -> list[ValidationIssue]:
        options = options or ChunkOptions()
        issues: list[ValidationIssue] = []
        issues.extend(self._validate_article_sequence(nodes, document_id))
        issues.extend(self._validate_chunks(chunks, document_id, options))
        return issues

    def _validate_article_sequence(self, nodes: list[StructureNode], document_id: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        mixed_order_parents = self._mixed_article_order_parents(nodes)
        previous_by_parent: dict[str, int] = {}
        for node in nodes:
            if node.node_type != "article" or not node.number:
                continue
            current = self._article_number(node.number)
            if current is None:
                continue
            parent_key = node.parent_id or "__root__"
            if parent_key in mixed_order_parents:
                previous_by_parent[parent_key] = current
                continue
            previous = previous_by_parent.get(parent_key)
            if previous is not None and current > previous + 1 and not self._is_expected_sequence_gap(node):
                issues.append(
                    self._issue(
                        document_id,
                        node.node_id,
                        "warning",
                        "article_sequence_gap",
                        f"제{previous}조 다음에 제{current}조가 나타났습니다.",
                        "원문에서 누락된 조문 또는 PDF 추출 순서를 확인하세요.",
                    )
                )
            previous_by_parent[parent_key] = current
        return issues

    def _mixed_article_order_parents(self, nodes: list[StructureNode]) -> set[str]:
        sequences: dict[str, list[int]] = {}
        for node in nodes:
            if node.node_type != "article" or not node.number:
                continue
            current = self._article_number(node.number)
            if current is None:
                continue
            parent_key = node.parent_id or "__root__"
            sequences.setdefault(parent_key, []).append(current)
        mixed: set[str] = set()
        for parent_key, numbers in sequences.items():
            if len(numbers) < 4:
                continue
            has_backward_jump = any(current < previous for previous, current in zip(numbers, numbers[1:]))
            has_repeated_articles = len(set(numbers)) < len(numbers)
            if has_backward_jump or has_repeated_articles:
                mixed.add(parent_key)
        return mixed

    def _is_expected_sequence_gap(self, node: StructureNode) -> bool:
        title = (node.title or "").replace("\u200b", "").strip()
        text = (node.text or "").replace("\u200b", "").strip()
        combined = f"{title} {text}"
        compact = re.sub(r"\s+", "", combined)
        expected_markers = [
            "다른 법령의 개정",
            "다른법령의 개정",
            "다른 규정의 개정",
            "다른규정의 개정",
            "경과조치",
            "적용례",
            "특례",
        ]
        amendment_markers = ["생략", "개정한다", "중 다음과 같이 개정", "부터 시행", "공포한 날부터 시행"]
        if any(marker in combined or re.sub(r"\s+", "", marker) in compact for marker in expected_markers):
            return True
        return any(marker in combined for marker in amendment_markers) and node.page_start is not None

    def _validate_chunks(
        self,
        chunks: list[Chunk],
        document_id: str,
        options: ChunkOptions,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not chunks:
            return [
                self._issue(
                    document_id,
                    None,
                    "error",
                    "no_chunks",
                    "생성된 chunk가 없습니다.",
                    "구조 탐지 패턴과 입력 텍스트 추출 결과를 확인하세요.",
                )
            ]

        required_metadata = ["document_name", "source_file", "hierarchy_path", "chunk_type"]
        for chunk in chunks:
            score = 1.0
            if not chunk.text.strip():
                issues.append(self._issue(document_id, chunk.chunk_id, "error", "empty_chunk", "빈 chunk가 생성되었습니다."))
                score -= 0.4
            if len(chunk.text) > options.max_chunk_chars + 500:
                if self._long_structured_table_chunk(chunk):
                    issues.append(
                        self._issue(
                            document_id,
                            chunk.chunk_id,
                            "info",
                            "long_structured_table_chunk",
                            f"Structured table chunk is longer than the default chunk target: {len(chunk.text)} chars.",
                            "Keep as one reviewable table unless retrieval latency becomes a problem.",
                        )
                    )
                else:
                    issues.append(
                        self._issue(
                            document_id,
                            chunk.chunk_id,
                            "warning",
                            "very_long_chunk",
                            f"chunk length exceeds the recommended limit: {len(chunk.text)} chars.",
                            "Adjust chunk_mode or max_chunk_chars.",
                        )
                    )
                    score -= 0.15
            if chunk.source_page_start is None and self._source_page_unavailable_is_declared(chunk):
                issues.append(
                    self._issue(
                        document_id,
                        chunk.chunk_id,
                        "info",
                        "source_page_unavailable",
                        "source_page_start is unavailable from the parser output.",
                        str(chunk.metadata.get("source_page_unavailable_reason") or "review source location manually"),
                    )
                )
            elif chunk.source_page_start is None:
                issues.append(
                    self._issue(
                        document_id,
                        chunk.chunk_id,
                        "warning",
                        "page_number_missing",
                        "source_page_start가 비어 있습니다.",
                    )
                )
                score -= 0.1
            for field in required_metadata:
                if not chunk.metadata.get(field):
                    issues.append(
                        self._issue(
                            document_id,
                            chunk.chunk_id,
                            "warning",
                            "metadata_missing",
                            f"필수 metadata가 비어 있습니다: {field}",
                        )
                    )
                    score -= 0.1
            chunk.confidence = max(0.0, min(chunk.confidence, score))
        return issues

    @staticmethod
    def _source_page_unavailable_is_declared(chunk: Chunk) -> bool:
        return bool((chunk.metadata or {}).get("source_page_unavailable_reason"))

    @staticmethod
    def _long_structured_table_chunk(chunk: Chunk) -> bool:
        metadata = chunk.metadata or {}
        return bool(
            metadata.get("kordoc_table_promoted")
            and metadata.get("table_cell_rows")
            and (chunk.chunk_type in {"table", "appendix"} or metadata.get("table_like"))
        )

    def _article_number(self, number: str) -> int | None:
        match = re.search(r"제\s*(\d+)\s*조", number)
        return int(match.group(1)) if match else None

    def _issue(
        self,
        document_id: str,
        target_id: str | None,
        severity: str,
        issue_type: str,
        message: str,
        suggested_action: str | None = None,
    ) -> ValidationIssue:
        safe_target = re.sub(r"\W+", "_", target_id or "document", flags=re.UNICODE).strip("_")
        issue_id = f"{document_id}_{issue_type}_{safe_target}"
        return ValidationIssue(
            issue_id=issue_id,
            document_id=document_id,
            target_id=target_id,
            severity=severity,  # type: ignore[arg-type]
            issue_type=issue_type,
            message=message,
            suggested_action=suggested_action,
        )
