from __future__ import annotations

from functools import lru_cache
import re

from app.schemas.parsed import ParsedBlock


_LABEL_SPACING_PATTERN = re.compile(r"\s+")


@lru_cache(maxsize=8192)
def _normalize_label_spacing_cached(value: str) -> str:
    return _LABEL_SPACING_PATTERN.sub("", value)


def disambiguate_table_headers(headers: list[str]) -> list[str]:
    """Return header keys with exact duplicates suffixed.

    Building a record dict keyed on the header collapses repeated headers
    (common in Korean 별표, e.g. two 금액 columns) and loses every value but
    the last.  Suffixing only the duplicates keeps the record lossless while
    leaving the common no-duplicate case unchanged.
    """
    seen: dict[str, int] = {}
    keys: list[str] = []
    for header in headers:
        if header in seen:
            seen[header] += 1
            keys.append(f"{header} ({seen[header]})")
        else:
            seen[header] = 1
            keys.append(header)
    return keys


class TableExtractor:
    """Conservative table detection that preserves raw rows before deeper parsing."""

    HEADER_TOKENS = {
        "구분",
        "내용",
        "기준",
        "금액",
        "직급",
        "등급",
        "항목",
        "세부",
        "비고",
        "기간",
        "일수",
        "근속년수",
        "연차휴가",
        "점수",
        "배점",
        "수량",
        "평가액",
        "재산명",
        "소재지",
        "지번",
        "면적",
        "학부",
        "전공",
        "입학정원",
        "정원",
        "단가",
        "항공료",
        "숙박비",
        "식비",
        "직종",
        "과정",
        "성명",
        "생년월일",
        "학번",
        "학위",
        "학위취득",
        "예정일",
        "공개유예기간",
        "지도교수",
        "지도교수명",
        "직위",
        "소속",
        "주소",
        "연락처",
        "사업명",
        "발주부서",
        "사업기간",
        "계약방법",
        "사업금액",
        "담당자",
        "연번",
        "기관명",
        "전화번호",
        "기관장",
        "책임자",
        "부책임자",
        "인가일자",
        "홈페이지",
        "합계",
        "평균",
        "순위",
        "평가",
        "위원",
        "접수",
        "번호",
        "종류",
        "사용처",
        "감가상각",
        "보험",
        "계정과목",
        "건물명",
        "점검",
        "주기",
        "일자",
        "근무시간",
        "출퇴근시간",
        "근무유형",
        "휴게시간",
        "월정직책급",
        "특정직무급",
        "가족수당",
        "환산율",
        "경력",
        "자격",
        "서류",
        "명칭",
        "의견",
        "직무",
        "설치장소",
        "제작회사",
        "제작년도",
        "형식",
        "결과",
    }

    def analyze_text(self, text: str, context_type: str | None = None) -> dict:
        rows = self.extract_rows(text)
        cell_rows = self.extract_cell_rows(rows, context_type=context_type)
        appendix_context = self._appendix_context(rows)
        if len(rows) < 3:
            return {
                "table_like": False,
                "table_rows": rows,
                "table_cell_rows": cell_rows,
                "table_confidence": 0.0,
                **appendix_context,
            }

        parse_rows = [row for row in self._appendix_title_stripped_rows(rows) if row]
        joined = " ".join(parse_rows)
        header_hits = max((self._header_hit_count(row) for row in parse_rows), default=0)
        numeric_rows = self._numeric_table_row_count(parse_rows)
        delimiter_rows = sum(1 for row in parse_rows if "|" in row or re.search(r"\S\s{2,}\S", row))
        short_label_rows = sum(1 for row in parse_rows if 2 <= len(row.split()) <= 12)
        structured_rows = sum(1 for row in cell_rows if len(row["cells"]) >= 2)
        appendix_bias = 1 if context_type in {"appendix", "form", "table"} else 0

        score = 0.0
        score += min(header_hits, 4) * 0.18
        score += min(numeric_rows, 4) * 0.12
        score += min(delimiter_rows, 4) * 0.12
        score += min(structured_rows, 4) * 0.08
        score += min(short_label_rows, 5) * 0.04
        score += appendix_bias * 0.15
        confidence = min(1.0, score)
        if context_type == "article":
            table_like = confidence >= 0.55 and delimiter_rows >= 2
        else:
            numeric_table_allowed = context_type in {"appendix", "form", "table"} and numeric_rows >= 3
            structured_table_allowed = context_type in {"appendix", "form", "table", "item", "paragraph", "subitem"} and structured_rows >= 2
            table_like = confidence >= 0.55 and (
                header_hits >= 2 or delimiter_rows >= 2 or numeric_table_allowed or structured_table_allowed
            )
        classification = self._classify_table_like(rows, cell_rows, table_like, context_type)
        if classification["demote"]:
            table_like = False
        review_flags = self._table_review_flags(
            rows=rows,
            cell_rows=cell_rows,
            table_like=table_like,
            classification=classification,
            context_type=context_type,
        )
        citation_label = self._citation_label(appendix_context)
        table_records = self._table_records(cell_rows) if table_like else []

        return {
            "table_like": table_like,
            "table_rows": rows,
            "table_cell_rows": cell_rows if table_like else [],
            "table_header_cells": cell_rows[0]["cells"] if table_like and cell_rows else [],
            "table_column_count": max((len(row["cells"]) for row in cell_rows), default=0) if table_like else 0,
            "table_structured_row_count": structured_rows if table_like else 0,
            "table_records": table_records,
            "table_record_count": len(table_records),
            "table_classification": classification["classification"],
            "table_review_reason": classification["reason"],
            "table_probable_false_positive": classification["probable_false_positive"],
            "table_probable_extraction_failed": classification["probable_extraction_failed"],
            "table_false_positive_stability": classification["false_positive_stability"],
            "table_confidence": round(confidence, 3),
            "table_header_hits": header_hits,
            "table_numeric_rows": numeric_rows,
            "table_delimiter_rows": delimiter_rows,
            "table_review_required": bool(review_flags),
            "table_review_flags": review_flags,
            "table_appendix_no": appendix_context.get("table_appendix_no"),
            "table_appendix_title": appendix_context.get("table_appendix_title"),
            "table_title": appendix_context.get("table_title"),
            "table_citation_label": citation_label,
            "table_markdown": self.cell_rows_to_markdown(cell_rows) if table_like and cell_rows else None,
        }

    def extract_rows(self, text: str) -> list[str]:
        rows: list[str] = []
        for line in text.splitlines():
            clean = re.sub(r"\s+", " ", line).strip()
            if not clean:
                continue
            if clean.startswith("[위치]") or clean.startswith("[본문]"):
                continue
            if re.fullmatch(r"-\s*\d+\s*-", clean):
                continue
            rows.append(clean)
        return rows

    def to_markdown_table(self, block: ParsedBlock) -> str:
        rows = self.extract_rows(block.text)
        if not rows:
            return ""
        return self.rows_to_markdown(rows)

    def extract_cell_rows(self, rows: list[str], context_type: str | None = None) -> list[dict]:
        parse_rows = self._appendix_title_stripped_rows(rows)
        late_table_rows = self._extract_late_aks_table_cell_rows(parse_rows)
        if late_table_rows:
            # Specialized handlers already perform their own continuation
            # joining.  Running the generic wrapped-row merger again can
            # incorrectly combine adjacent retention bullets or form headers.
            return late_table_rows
        if self._is_late_aks_ambiguous_table(parse_rows):
            # Do not let the generic whitespace splitter manufacture columns
            # for flattened multi-level sanction tables.  Raw rows plus an
            # explicit review flag are safer than a plausible but wrong grid.
            return []
        cell_rows: list[dict] = self._extract_performance_grade_cell_rows(parse_rows)
        cell_rows.extend(self._extract_vertical_compact_cell_rows(parse_rows))
        cell_rows.extend(self._extract_named_area_cell_rows(parse_rows))
        cell_rows.extend(self._extract_salary_group_cell_rows(parse_rows))
        cell_rows.extend(self._extract_salary_assessment_cell_rows(parse_rows))
        cell_rows.extend(self._extract_dense_numeric_cell_rows(parse_rows, context_type=context_type))
        cell_rows.extend(self._extract_vertical_checklist_cell_rows(parse_rows, context_type=context_type))
        if context_type in {"appendix", "form", "table"}:
            cell_rows.extend(self._extract_code_description_cell_rows(parse_rows))
        cell_rows.extend(self._extract_checkbox_question_cell_rows(parse_rows))
        cell_rows.extend(self._extract_disciplinary_sanction_cell_rows(parse_rows))
        if context_type in {"appendix", "form", "table"}:
            cell_rows.extend(self._extract_numbered_colon_cell_rows(parse_rows))
            cell_rows.extend(self._extract_outline_condition_cell_rows(parse_rows))
        if not cell_rows and context_type in {"appendix", "form", "table"}:
            cell_rows.extend(self._extract_parallel_value_tail_cell_rows(parse_rows))
        for row_index, row in enumerate(parse_rows):
            if not row:
                continue
            if self._looks_like_table_caption_or_note_row(row):
                continue
            cells = self._split_row(row)
            if len(cells) < 2:
                continue
            cell_rows.append(
                {
                    "row_index": row_index,
                    "cells": cells,
                    "raw": row,
                    "numeric_cell_count": sum(1 for cell in cells if re.search(r"\d", cell)),
                }
            )
        return self._merge_wrapped_cell_rows(cell_rows, context_type=context_type)

    def _extract_late_aks_table_cell_rows(self, rows: list[str]) -> list[dict]:
        """Recover high-confidence rows from flattened AKS appendix/form tables.

        Legacy HWP extraction often preserves table text order but loses cell
        geometry.  These handlers only reconstruct layouts with stable labels
        and explicit row/value ordering.  Ambiguous tables deliberately return
        no rows so the existing review-required path remains fail-closed.
        """
        compact = " ".join(self._normalize_label_spacing(row) for row in rows[:8])
        if self._has_record_retention_continuation_marker(rows):
            return self._extract_record_retention_continuation_rows(rows)
        if "교수직임용자격기준표" in compact:
            return self._extract_qualification_rows(rows, ("교수", "부교수", "조교수"))
        if "연구직임용자격기준표" in compact:
            return self._extract_qualification_rows(rows, ("수석연구원", "책임연구원", "선임연구원", "정연구원"))
        if "연구직경력기간환산율표" in compact:
            return self._extract_career_conversion_rows(rows)
        if "기록물보존기간기준표" in compact:
            return self._extract_record_retention_rows(rows)
        if "계정과목" in compact or "계정과목" in self._normalize_label_spacing(" ".join(rows[:4])):
            return self._extract_account_title_rows(rows)
        if "겸직자명부" in compact:
            return self._extract_known_header_rows(rows, ("번호", "소속", "직위", "성명", "기간", "겸직기관", "겸직직위", "비고"))
        if "기록물평가심의서" in compact:
            return self._extract_record_review_form_rows(rows)
        if "서류전형평가표" in compact:
            return self._extract_screening_score_rows(rows)
        if "기타수당지급기준표" in compact:
            return self._extract_allowance_rows(rows)
        return []

    def _is_late_aks_ambiguous_table(self, rows: list[str]) -> bool:
        compact = " ".join(self._normalize_label_spacing(row) for row in rows[:8])
        return "채용비리처리기준" in compact or "기타수당지급기준" in compact

    def _extract_qualification_rows(self, rows: list[str], roles: tuple[str, ...]) -> list[dict]:
        header_index = next(
            (index for index, row in enumerate(rows) if self._normalize_label_spacing(row) in {"구분자격", "구분자격기준"}),
            None,
        )
        if header_index is None:
            header_index = next((index for index, row in enumerate(rows) if "구분" in row and "자격" in row), None)
        if header_index is None:
            return []
        role_map = {self._normalize_label_spacing(role): role for role in roles}
        result = [self._cell_row(header_index, ["구분", "자격"])]
        current_role: str | None = None
        current_parts: list[str] = []

        def flush() -> None:
            if current_role and current_parts:
                result.append(
                    self._reconstructed_cell_row(
                        len(result), [current_role, " ".join(current_parts)], "qualification_row"
                    )
                )

        for index, raw in enumerate(rows[header_index + 1 :], start=header_index + 1):
            row = raw.strip()
            normalized = self._normalize_label_spacing(row)
            if normalized in role_map:
                flush()
                current_role = role_map[normalized]
                current_parts = []
                continue
            inline_role = next(
                (role for normalized_role, role in role_map.items() if normalized.startswith(normalized_role) and normalized != normalized_role),
                None,
            )
            if inline_role:
                flush()
                current_role = inline_role
                current_parts = [row[len(inline_role) :].strip()]
                continue
            if not current_role or self._looks_like_table_caption_or_note_row(row):
                continue
            if re.match(r"^\d+[\.)]\s*", row):
                current_parts.append(row)
            elif current_parts and not re.match(r"^\d+(?:-\d+)+\.", row):
                current_parts[-1] = f"{current_parts[-1]} {row}".strip()
        flush()
        return result if len(result) == len(roles) + 1 else []

    def _extract_career_conversion_rows(self, rows: list[str]) -> list[dict]:
        header_index = next((i for i, row in enumerate(rows) if "경력종별" in self._normalize_label_spacing(row)), None)
        if header_index is None:
            return []
        groups: list[str] = []
        current: str | None = None
        rates: list[str] = []
        in_rates = False
        for raw in rows[header_index + 1 :]:
            row = raw.strip()
            if not row or re.match(r"^\d+(?:-\d+)+\.", row):
                continue
            if in_rates:
                rate = row.replace(" ", "")
                if re.fullmatch(r"\d+%/?\d*%?", rate):
                    rates.append(rate)
                    continue
                if "동일" in row and rates:
                    rates[-1] = f"{rates[-1]} {row}".strip()
                if row.startswith("비고"):
                    break
                continue
            match = re.match(r"^(\d+)\.\s*(.*)$", row)
            if match and int(match.group(1)) <= 5:
                if current:
                    groups.append(current.strip())
                current = match.group(2).strip()
                continue
            rate = row.replace(" ", "")
            if re.fullmatch(r"\d+%/?\d*%?", rate):
                in_rates = True
                rates.append(rate)
                continue
            if current and not in_rates and not row.startswith("비고"):
                current = f"{current} {row}".strip()
        if current:
            groups.append(current.strip())
        if len(groups) != 5 or len(rates) < 5:
            return []
        result = [self._cell_row(header_index, ["경력종별", "환산율"])]
        for index, (group, rate) in enumerate(zip(groups, rates[:5]), start=1):
            result.append(self._reconstructed_cell_row(index, [group, rate], "career_conversion_row"))
        return result

    def _extract_record_retention_rows(self, rows: list[str]) -> list[dict]:
        header_index = next((i for i, row in enumerate(rows) if "기록물보존기간기준표" in self._normalize_label_spacing(row)), None)
        if header_index is None:
            return []
        categories = {"영구보존": "영구 보존", "준영구": "준 영구", "30년이상": "30년 이상"}
        result = [self._cell_row(header_index, ["보존기간", "보존대상"])]
        current_category: str | None = None
        last_row: dict | None = None
        for index, raw in enumerate(rows[header_index + 1 :], start=header_index + 1):
            row = raw.strip()
            normalized = self._normalize_label_spacing(row)
            if normalized in categories:
                current_category = categories[normalized]
                last_row = None
                continue
            if not current_category or re.match(r"^\d+(?:-\d+)+\.", row):
                continue
            parts = [part.strip() for part in row.split("▪")]
            prefix = parts.pop(0).strip()
            if prefix and last_row and not prefix.startswith(("※", "비고")):
                last_row["cells"][1] = f"{last_row['cells'][1]} {prefix}".strip()
                last_row["raw"] = " | ".join(last_row["cells"])
            for part in parts:
                if not part:
                    continue
                last_row = self._reconstructed_cell_row(
                    len(result), [current_category, part], "record_retention_bullet"
                )
                result.append(last_row)
        return result if len(result) >= 3 else []

    def _has_record_retention_continuation_marker(self, rows: list[str]) -> bool:
        has_header = any(
            re.search(r"4-5-2\.?기록물관리규정", self._normalize_label_spacing(row))
            for row in rows
        )
        if not has_header:
            return False
        has_three_year = any(self._normalize_label_spacing(row) == "3년보존" for row in rows)
        has_one_year = any(self._normalize_label_spacing(row) == "1년보존" for row in rows)
        return has_three_year and has_one_year

    def _extract_record_retention_continuation_rows(self, rows: list[str]) -> list[dict]:
        year_three_index = next(
            (
                index
                for index, row in enumerate(rows)
                if self._normalize_label_spacing(row) == "3년보존"
            ),
            None,
        )
        year_one_index = next(
            (
                index
                for index, row in enumerate(rows)
                if self._normalize_label_spacing(row) == "1년보존"
            ),
            None,
        )
        if year_three_index is None or year_one_index is None:
            return []

        earliest_body_index = min(year_three_index, year_one_index)
        header_index = None
        for index in range(earliest_body_index - 1, -1, -1):
            if re.search(r"4-5-2\.?기록물관리규정", self._normalize_label_spacing(rows[index])):
                header_index = index
                break
        if header_index is None:
            return []

        return [
            self._reconstructed_cell_row(
                header_index,
                ["분류번호", "4-5-2", "기록물관리규정"],
                "possible_truncated_cell",
            ),
            self._reconstructed_cell_row(year_three_index, ["보존기간", "3년보존"], "record_retention_bullet"),
            self._reconstructed_cell_row(year_one_index, ["보존기간", "1년보존"], "record_retention_bullet"),
        ]

    def _extract_known_header_rows(self, rows: list[str], labels: tuple[str, ...]) -> list[dict]:
        for index, row in enumerate(rows):
            compact = self._normalize_label_spacing(row)
            positions: list[int] = []
            cursor = 0
            for label in labels:
                position = compact.find(self._normalize_label_spacing(label), cursor)
                if position < 0:
                    break
                positions.append(position)
                cursor = position + len(self._normalize_label_spacing(label))
            if len(positions) == len(labels):
                return [self._cell_row(index, [*labels],)]
        return []

    def _extract_record_review_form_rows(self, rows: list[str]) -> list[dict]:
        result: list[dict] = []
        first = ("기록물철 분류번호", "생산 연도", "기록물철 제목", "보존기간", "만료일")
        second = ("처리과", "기록물관리담당자", "심의회 의견", "처리 의견", "사유", "평가의견", "사유")
        for index, labels in enumerate((first, second)):
            if any(self._normalize_label_spacing(label) in self._normalize_label_spacing(" ".join(rows)) for label in labels):
                result.append(self._reconstructed_cell_row(index, list(labels), "record_review_form_header"))
        return result if len(result) == 2 else []

    def _extract_screening_score_rows(self, rows: list[str]) -> list[dict]:
        labels = (
            "응시 요건의 적합성",
            "직무수행 요건의 적합성",
            "조직(연구원) 목적 달성의 적합성",
            "의사소통 및 문제 해결 능력",
        )
        value_row = next((row for row in rows if len(re.findall(r"\d+점", row)) >= 4), "")
        values = re.findall(r"\d+점", value_row)
        description_start = next(
            (index for index, row in enumerate(rows) if "채용 기준" in row and "심사" in row),
            None,
        )
        descriptions = rows[description_start : description_start + 4] if description_start is not None else []
        if len(values) != 4 or len(descriptions) < 4:
            return []
        result = [self._cell_row(0, ["심사 항목", "배점", "심사 기준"])]
        for index, (label, value, description) in enumerate(zip(labels, values, descriptions[:4]), start=1):
            result.append(self._reconstructed_cell_row(index, [label, value, description], "screening_score_row"))
        return result

    def _extract_account_title_rows(self, rows: list[str]) -> list[dict]:
        labels = tuple(
            sorted(
                {
                    "현금", "당좌예금", "정기예금", "유가증권", "전도금", "가지급금", "미수금", "선급비용",
                    "저장품", "재료", "장기예치금", "보험증권", "기타투자자산", "토지", "건물", "구축물",
                    "입목", "전기시설", "마이크로시설", "통신시설", "차량운반구", "장서", "서화공예품",
                    "시청각시설", "공기구비품", "건설중인자산", "기타의유형자산", "감가상각누계액", "장기선급비용",
                    "미결산계정", "저작권", "미지급금", "선수금", "예수금", "미지급비용", "예수유가증권",
                    "장기차입금", "퇴직급여충당금", "기본순자산", "잉여금", "적립금", "순자산조정",
                },
                key=len,
                reverse=True,
            )
        )
        categories = {"자산", "유동자산", "재고자산", "투자자산", "유형자산", "무형자산", "부채", "유동부채", "비유동부채", "순자산", "보통순자산"}
        result: list[dict] = []
        for index, raw in enumerate(rows):
            compact = self._normalize_label_spacing(raw)
            if compact == "관항목해설":
                result.append(self._cell_row(index, ["관", "항", "목", "해설"]))
                continue
            if compact == "계정과목":
                # The flattened source often repeats the caption immediately
                # before the real column header.  Keep one canonical header;
                # Kordoc remains the authority for the actual grid geometry.
                continue
            if compact in categories:
                result.append(self._reconstructed_cell_row(index, [raw.strip(), ""], "account_hierarchy_row"))
                continue
            matched = next((label for label in labels if compact.startswith(label)), None)
            if not matched:
                continue
            description = compact[len(matched) :].strip()
            result.append(self._reconstructed_cell_row(index, [matched, description], "account_title_row"))
        return result if len(result) >= 5 else []

    def _extract_allowance_rows(self, rows: list[str]) -> list[dict]:
        header_index = next((i for i, row in enumerate(rows) if "월정직책급" in self._normalize_label_spacing(row)), None)
        if header_index is None:
            return []
        # Text extraction preserves the values but not their horizontal
        # coordinates.  For this four-column allowance table, assigning the
        # flattened values to columns would create a plausible but potentially
        # wrong legal record.  Let Kordoc/PDF geometry decide the row; until
        # then the table must remain review-required and unstructured.
        return []

    def _reconstructed_cell_row(self, row_index: int, cells: list[str], flag: str) -> dict:
        row = self._cell_row(row_index, cells)
        row["review_required"] = True
        row["row_quality_flags"] = [flag]
        return row

    def _appendix_context(self, rows: list[str]) -> dict:
        for row in rows[:8]:
            stripped = row.strip()
            match = re.match(r"^[\[<【]?\s*(별\s*(?:표|지)\s*\d*(?:\s*-\s*\d+)?)\s*[\]>】]?\s*(.*)$", stripped)
            if not match:
                continue
            appendix_no = re.sub(r"\s+", "", match.group(1))
            title = match.group(2).strip(" -:\t")
            title = re.sub(r"^<[^>]+>\s*", "", title).strip()
            return {
                "table_appendix_no": appendix_no or None,
                "table_appendix_title": title or None,
                "table_title": title or appendix_no or None,
            }
        caption = self._first_caption_row(rows)
        return {
            "table_appendix_no": None,
            "table_appendix_title": None,
            "table_title": caption,
        }

    def _first_caption_row(self, rows: list[str]) -> str | None:
        for row in rows[:8]:
            stripped = row.strip()
            if re.fullmatch(r"<\s*[^<>]{2,80}\s*>", stripped):
                return stripped.strip("<> ").strip() or None
        return None

    def _citation_label(self, appendix_context: dict) -> str | None:
        appendix_no = appendix_context.get("table_appendix_no")
        title = appendix_context.get("table_appendix_title") or appendix_context.get("table_title")
        if appendix_no and title:
            return f"{appendix_no} {title}"
        if appendix_no:
            return str(appendix_no)
        return title

    def _table_records(self, cell_rows: list[dict]) -> list[dict]:
        if len(cell_rows) < 2:
            return []
        header_cells = [str(cell).strip() for cell in cell_rows[0].get("cells") or []]
        if not self._looks_like_table_header_cells(header_cells):
            return []
        records: list[dict] = []
        for row in cell_rows[1:]:
            cells = [str(cell).strip() for cell in row.get("cells") or []]
            if len(cells) > len(header_cells) or len(cells) < 2:
                continue
            if len(cells) < len(header_cells):
                cells = [*cells, *([""] * (len(header_cells) - len(cells)))]
            keys = disambiguate_table_headers(header_cells)
            record = {
                key: value
                for key, header, value in zip(keys, header_cells, cells)
                if header and value
            }
            if not record:
                continue
            records.append(
                {
                    "row_index": row.get("row_index"),
                    "header_cells": header_cells,
                    "record": record,
                }
            )
        return records

    def _looks_like_table_header_cells(self, cells: list[str]) -> bool:
        headers = [cell.strip() for cell in cells if cell and cell.strip()]
        if len(headers) < 2:
            return False
        joined = " ".join(headers)
        if self._header_hit_count(joined) > 0:
            return True
        if any(len(header) > 24 for header in headers):
            return False
        numeric_headers = sum(1 for header in headers if re.search(r"\d", header))
        return numeric_headers <= max(1, len(headers) // 3)

    def _merge_wrapped_cell_rows(self, cell_rows: list[dict], context_type: str | None = None) -> list[dict]:
        if context_type not in {"appendix", "table"} or len(cell_rows) < 2:
            return cell_rows
        merged: list[dict] = []
        index = 0
        while index < len(cell_rows):
            current = dict(cell_rows[index])
            next_row = cell_rows[index + 1] if index + 1 < len(cell_rows) else None
            if next_row and self._should_merge_wrapped_cell_rows(current, next_row):
                current_cells = [str(cell).strip() for cell in current.get("cells") or []]
                next_cells = [str(cell).strip() for cell in next_row.get("cells") or []]
                current["cells"] = [
                    self._join_wrapped_cell_text(left, right)
                    for left, right in zip(current_cells, next_cells)
                ]
                current["raw"] = " ".join(cell for cell in current["cells"] if cell)
                current["numeric_cell_count"] = sum(1 for cell in current["cells"] if re.search(r"\d", cell))
                current["merged_from_row_indices"] = [
                    current.get("row_index"),
                    next_row.get("row_index"),
                ]
                current["review_required"] = True
                current["row_quality_flags"] = sorted(
                    set(current.get("row_quality_flags") or []) | {"wrapped_cell_merge"}
                )
                merged.append(current)
                index += 2
                continue
            current.setdefault("row_quality_flags", self._cell_row_quality_flags(current))
            current["review_required"] = bool(current.get("row_quality_flags"))
            merged.append(current)
            index += 1
        return merged

    def _should_merge_wrapped_cell_rows(self, left: dict, right: dict) -> bool:
        left_cells = [str(cell).strip() for cell in left.get("cells") or []]
        right_cells = [str(cell).strip() for cell in right.get("cells") or []]
        if not (2 <= len(left_cells) <= 4 and len(left_cells) == len(right_cells)):
            return False
        if int(left.get("numeric_cell_count") or 0) or int(right.get("numeric_cell_count") or 0):
            return False
        if self._looks_like_short_header_cell_row(left_cells) or self._looks_like_short_header_cell_row(right_cells):
            return False
        if any(self._looks_like_standalone_row_start(cell) for cell in right_cells):
            return False
        left_index = left.get("row_index")
        right_index = right.get("row_index")
        if isinstance(left_index, int) and isinstance(right_index, int) and right_index - left_index > 3:
            return False
        return all(self._looks_like_wrapped_cell_fragment(a, b) for a, b in zip(left_cells, right_cells))

    def _looks_like_short_header_cell_row(self, cells: list[str]) -> bool:
        if not cells:
            return False
        joined = " ".join(cells)
        return self._header_hit_count(joined) > 0 and all(len(cell.strip()) <= 8 for cell in cells)

    def _looks_like_wrapped_cell_fragment(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        if len(left) < 4 or len(right) < 4:
            return False
        if re.search(r"[.!?。]$", left):
            return False
        return bool(re.search(r"[가-힣]", left + right))

    def _looks_like_standalone_row_start(self, value: str) -> bool:
        stripped = value.strip()
        return bool(
            re.match(r"^(?:제\s*\d+\s*조|\d+[\.\)]|[가-하]\.|[①-⑳])", stripped)
            or stripped.startswith(("[", "<", "별표", "별지"))
        )

    def _join_wrapped_cell_text(self, left: str, right: str) -> str:
        left = left.strip()
        right = right.strip()
        if not left:
            return right
        if not right:
            return left
        return f"{left} {right}"

    def _cell_row_quality_flags(self, row: dict) -> list[str]:
        cells = [str(cell).strip() for cell in row.get("cells") or []]
        flags: list[str] = []
        if not cells:
            return flags
        joined = " ".join(cells)
        if (
            any(0 < len(cell) <= 2 for cell in cells)
            and not self._header_hit_count(joined)
            and int(row.get("numeric_cell_count") or 0) == 0
        ):
            flags.append("short_cell")
        if any(self._looks_like_truncated_korean_cell(cell) for cell in cells):
            flags.append("possible_truncated_cell")
        if len(cells) >= 6 and int(row.get("numeric_cell_count") or 0) == 0:
            flags.append("many_text_cells_without_numeric_signal")
        return flags

    def _looks_like_truncated_korean_cell(self, cell: str) -> bool:
        stripped = cell.strip()
        if len(stripped) < 5:
            return False
        if not re.search(r"[가-힣]", stripped):
            return False
        if re.search(r"[.!?。]$|다$|요$|음$|함$|됨$|임$", stripped):
            return False
        return bool(re.search(r"[가-힣]$", stripped))

    def _table_review_flags(
        self,
        *,
        rows: list[str],
        cell_rows: list[dict],
        table_like: bool,
        classification: dict,
        context_type: str | None,
    ) -> list[str]:
        flags: set[str] = set()
        if not table_like:
            return []
        if classification.get("probable_extraction_failed"):
            flags.add("probable_extraction_failed")
        if not cell_rows:
            flags.add("table_like_without_cell_rows")
        if context_type in {"appendix", "table"} and len(rows) >= 8 and len(cell_rows) < 2:
            flags.add("appendix_table_low_structured_row_count")
        column_counts = [len(row.get("cells") or []) for row in cell_rows if row.get("cells")]
        if column_counts and max(column_counts) - min(column_counts) >= 3:
            flags.add("unstable_column_count")
        for row in cell_rows:
            for flag in row.get("row_quality_flags") or []:
                flags.add(flag)
            if row.get("review_required"):
                flags.add("row_review_required")
        return sorted(flags)

    def _appendix_title_stripped_rows(self, rows: list[str]) -> list[str]:
        return [self._strip_appendix_title_prefix(row) for row in rows]

    def _strip_appendix_title_prefix(self, row: str) -> str:
        stripped = row.strip()
        match = re.match(r"^\[(?:별표|별지)[^\]]*\]\s*(.*)$", stripped)
        if not match:
            return stripped
        return match.group(1).strip()

    def _extract_performance_grade_cell_rows(self, rows: list[str]) -> list[dict]:
        grade_index = self._find_normalized_row(rows, "등급")
        if grade_index is None:
            return []
        rate_index = self._find_normalized_row(rows, "지급률", start=grade_index + 1, stop=grade_index + 12)
        headcount_index = self._find_normalized_row(rows, "인원", start=(rate_index or grade_index) + 1, stop=grade_index + 20)
        if rate_index is None:
            return []
        categories = [
            item.strip()
            for item in rows[grade_index + 1 : rate_index]
            if re.fullmatch(r"[A-Z가-힣0-9]+", item.strip()) and len(item.strip()) <= 4
        ]
        if len(categories) < 2:
            return []
        rate_values = [
            item.strip()
            for item in rows[rate_index + 1 : headcount_index]
            if self._percentage_cell(item.strip())
        ]
        headcount_values: list[str] = []
        if headcount_index is not None:
            for item in rows[headcount_index + 1 : headcount_index + 1 + len(categories)]:
                stripped = item.strip()
                if not self._percentage_cell(stripped):
                    break
                headcount_values.append(stripped)
        result = [
            self._cell_row(grade_index, ["등급", *categories]),
        ]
        if len(rate_values) >= 2:
            result.append(self._cell_row(rate_index, ["지급률", *rate_values]))
        if len(headcount_values) >= 2:
            result.append(self._cell_row(headcount_index, ["인원", *headcount_values]))
        return result if len(result) >= 2 else []

    def _find_normalized_row(
        self,
        rows: list[str],
        expected: str,
        start: int = 0,
        stop: int | None = None,
    ) -> int | None:
        normalized_expected = self._normalize_label_spacing(expected)
        for index in range(start, min(len(rows), stop if stop is not None else len(rows))):
            if self._normalize_label_spacing(rows[index]) == normalized_expected:
                return index
        return None

    def _percentage_cell(self, value: str) -> bool:
        return bool(re.fullmatch(r"\d+(?:\.\d+)?\s*[%％]", value.strip()))

    def _cell_row(self, row_index: int, cells: list[str]) -> dict:
        return {
            "row_index": row_index,
            "cells": cells,
            "raw": " | ".join(cells),
            "numeric_cell_count": sum(1 for cell in cells if re.search(r"\d", cell)),
        }

    def _extract_checkbox_question_cell_rows(self, rows: list[str]) -> list[dict]:
        cell_rows: list[dict] = []
        pending: list[str] = []
        pending_start = 0
        index = 0
        while index < len(rows):
            stripped = rows[index].strip()
            if not stripped or self._looks_like_table_caption_or_note_row(stripped):
                index += 1
                continue
            if re.match(r"^[◯○]\s*\d+", stripped) and pending:
                pending = []
                pending_start = index
            if not pending:
                pending_start = index
            pending.append(stripped)
            if len(pending) > 4 and not self._has_checkbox_yes_no(" ".join(pending)):
                pending = pending[-3:]
                pending_start = index - len(pending) + 1
            combined = " ".join(pending)
            if self._has_checkbox_yes_no(combined):
                if not re.search(r"\[\s*\]\s*N\s*/?\s*A", combined, re.IGNORECASE):
                    next_index = index + 1
                    if next_index < len(rows) and re.fullmatch(r"\[\s*\]\s*N\s*/?\s*A", rows[next_index].strip(), re.IGNORECASE):
                        index = next_index
                        pending.append(rows[index].strip())
                        combined = " ".join(pending)
                parsed = self._checkbox_question_row(pending_start, combined)
                if parsed:
                    if not cell_rows:
                        cell_rows.append(
                            {
                                "row_index": -1,
                                "cells": ["항목", "질문", "Yes", "No", "N/A"],
                                "raw": "항목 질문 Yes No N/A",
                                "numeric_cell_count": 0,
                            }
                        )
                    cell_rows.append(parsed)
                pending = []
            index += 1
        return cell_rows

    def _has_checkbox_yes_no(self, value: str) -> bool:
        return bool(
            re.search(r"\[\s*\]\s*Yes", value, re.IGNORECASE)
            and re.search(r"\[\s*\]\s*No", value, re.IGNORECASE)
        )

    def _checkbox_question_row(self, row_index: int, value: str) -> dict | None:
        options = {
            "Yes": bool(re.search(r"\[\s*\]\s*Yes", value, re.IGNORECASE)),
            "No": bool(re.search(r"\[\s*\]\s*No", value, re.IGNORECASE)),
            "N/A": bool(re.search(r"\[\s*\]\s*N\s*/?\s*A", value, re.IGNORECASE)),
        }
        if not options["Yes"] or not options["No"]:
            return None
        question = re.sub(r"\[\s*\]\s*(?:Yes|No|N\s*/?\s*A)", " ", value, flags=re.IGNORECASE)
        question = re.sub(r"\s+", " ", question).strip(" -")
        item = ""
        match = re.match(r"^[◯○]?\s*(\d+)\s+(.*)$", question)
        if match:
            item = match.group(1)
            question = match.group(2).strip()
        if not question:
            return None
        cells = [item, question, "Yes" if options["Yes"] else "", "No" if options["No"] else "", "N/A" if options["N/A"] else ""]
        return {
            "row_index": row_index,
            "cells": cells,
            "raw": value,
            "numeric_cell_count": sum(1 for cell in cells if re.search(r"\d", cell)),
            "review_required": True,
            "row_quality_flags": ["checkbox_question_row"],
        }

    def _extract_numbered_colon_cell_rows(self, rows: list[str]) -> list[dict]:
        data_rows: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for row_index, row in enumerate(rows):
            match = re.match(r"^\s*(\d+|[가-힣])\.\s+([^:：]{1,120}?)\s*[:：]\s*(.{1,80})\s*$", row.strip())
            if not match:
                continue
            item, criterion, value = (part.strip() for part in match.groups())
            if not re.search(r"(이상|이하|미만|초과|원|만원|천원|명|점|%|년|개월|월|일)", f"{criterion} {value}"):
                continue
            key = (item, criterion, value)
            if key in seen:
                continue
            seen.add(key)
            data_rows.append(
                {
                    "row_index": row_index,
                    "cells": [item, criterion, value],
                    "raw": row,
                    "numeric_cell_count": sum(1 for cell in [item, criterion, value] if re.search(r"\d", cell)),
                }
            )
        if len(data_rows) < 2:
            return []
        return [
            {
                "row_index": -1,
                "cells": ["항목", "기준", "값"],
                "raw": "항목 기준 값",
                "numeric_cell_count": 0,
            },
            *data_rows,
        ]

    def _extract_outline_condition_cell_rows(self, rows: list[str]) -> list[dict]:
        section = ""
        item = ""
        data_rows: list[dict] = []
        for row_index, row in enumerate(rows):
            stripped = row.strip()
            section_match = re.match(r"^\d+\.\s+(.{2,80})$", stripped)
            if section_match:
                section = section_match.group(1).strip()
                item = ""
                continue
            item_match = re.match(r"^([가-힣])\.\s+(.{2,100})$", stripped)
            if item_match:
                item = item_match.group(2).strip()
                continue
            condition_match = re.match(r"^\((\d+)\)\s+(.{2,140})$", stripped)
            if not condition_match or not section:
                continue
            condition = condition_match.group(2).strip()
            if not re.search(r"(이상|이하|미만|초과|원|만원|천원|소송|계약|심판|사건|경비)", condition):
                continue
            cells = [section, item, condition_match.group(1), condition]
            data_rows.append(
                {
                    "row_index": row_index,
                    "cells": cells,
                    "raw": row,
                    "numeric_cell_count": sum(1 for cell in cells if re.search(r"\d", cell)),
                }
            )
        if len(data_rows) < 2:
            return []
        return [
            {
                "row_index": -1,
                "cells": ["대항목", "세부항목", "순번", "기준"],
                "raw": "대항목 세부항목 순번 기준",
                "numeric_cell_count": 0,
            },
            *data_rows,
        ]

    def _extract_disciplinary_sanction_cell_rows(self, rows: list[str]) -> list[dict]:
        normalized_rows = [self._normalize_label_spacing(row) for row in rows]
        if not any("임원징계제재조치직원징계양정과비교" in row for row in normalized_rows):
            return []
        data_rows: list[dict] = []
        index = 0
        while index < len(rows):
            row = rows[index].strip()
            if not row or row.startswith("*"):
                index += 1
                continue
            if row.startswith("해임"):
                action, comparison = self._split_sanction_action_comparison(row.removeprefix("해임").strip())
                data_rows.append(self._disciplinary_row(index, "해임", action, comparison))
            elif row.startswith("연임제한"):
                action, comparison = self._split_sanction_action_comparison(row.removeprefix("연임제한").strip())
                if not comparison and index + 1 < len(rows) and self._looks_like_sanction_comparison(rows[index + 1]):
                    index += 1
                    comparison = rows[index].strip()
                data_rows.append(self._disciplinary_row(index, "연임제한", action, comparison))
            elif row == "업무배제":
                action_lines, comparison, index = self._collect_sanction_block(rows, index + 1, stop_labels={"기본연봉"})
                data_rows.append(self._disciplinary_row(index, "업무배제", " ".join(action_lines), comparison))
            elif row == "기본연봉" and index + 1 < len(rows) and rows[index + 1].strip() == "감액":
                action_lines, comparison, index = self._collect_sanction_block(rows, index + 2, stop_labels=set())
                data_rows.append(self._disciplinary_row(index, "기본연봉 감액", " ".join(action_lines), comparison))
            index += 1
        if len(data_rows) < 2:
            return []
        return [
            {
                "row_index": -1,
                "cells": ["임원 징계", "제재조치", "직원 징계양정과 비교"],
                "raw": "임원 징계 제재조치 직원 징계양정과 비교",
                "numeric_cell_count": 0,
            },
            *data_rows,
        ]

    def _split_sanction_action_comparison(self, value: str) -> tuple[str, str]:
        comparison_match = re.search(r"(“[^”]+”\s*일\s*경우\s*준용)", value)
        if not comparison_match:
            return value.strip(), ""
        action = value[: comparison_match.start()].strip()
        comparison = comparison_match.group(1).strip()
        return action, comparison

    def _looks_like_sanction_comparison(self, value: str) -> bool:
        return bool(re.fullmatch(r"“[^”]+”\s*일\s*경우\s*준용", value.strip()))

    def _collect_sanction_block(
        self,
        rows: list[str],
        start_index: int,
        *,
        stop_labels: set[str],
    ) -> tuple[list[str], str, int]:
        action_lines: list[str] = []
        comparison = ""
        index = start_index
        while index < len(rows):
            stripped = rows[index].strip()
            if not stripped or stripped.startswith("*") or stripped in stop_labels:
                break
            if self._looks_like_sanction_comparison(stripped):
                comparison = stripped
                break
            action_lines.append(stripped)
            index += 1
        return action_lines, comparison, max(start_index, index - 1)

    def _disciplinary_row(self, row_index: int, sanction: str, action: str, comparison: str) -> dict:
        cells = [sanction, action, comparison]
        return {
            "row_index": row_index,
            "cells": cells,
            "raw": " ".join(cell for cell in cells if cell),
            "numeric_cell_count": sum(1 for cell in cells if re.search(r"\d", cell)),
            "review_required": True,
            "row_quality_flags": ["disciplinary_sanction_vertical_row"],
        }

    def rows_to_markdown(self, rows: list[str]) -> str:
        split_rows = [row["cells"] for row in self.extract_cell_rows(rows)]
        if not split_rows:
            split_rows = [[row.strip()] for row in rows if row.strip()]
        return self._cells_to_markdown(split_rows)

    def cell_rows_to_markdown(self, cell_rows: list[dict]) -> str:
        split_rows = [row["cells"] for row in cell_rows if row.get("cells")]
        return self._cells_to_markdown(split_rows)

    def _cells_to_markdown(self, split_rows: list[list[str]]) -> str:
        if not split_rows:
            return ""
        max_width = max(len(row) for row in split_rows)
        normalized = [row + [""] * (max_width - len(row)) for row in split_rows]
        header = "| " + " | ".join(normalized[0]) + " |"
        divider = "| " + " | ".join(["---"] * max_width) + " |"
        body = ["| " + " | ".join(row) + " |" for row in normalized[1:]]
        return "\n".join([header, divider, *body])

    def _split_row(self, row: str) -> list[str]:
        if re.match(r"^\s*제\s*\d+\s*조", row):
            return [row.strip()]
        if self._looks_like_numbered_clause_prose_row(row):
            return [row.strip()]
        if self._looks_like_bullet_prose_row(row):
            return [row.strip()]
        if self._looks_like_table_caption_or_note_row(row):
            return [row.strip()]
        compact_leave_header = self._split_leave_days_header_row(row)
        if len(compact_leave_header) >= 2:
            return compact_leave_header
        if "|" in row:
            return [cell.strip() for cell in row.split("|")]
        cells = [cell.strip() for cell in re.split(r"\s{2,}", row) if cell.strip()]
        if len(cells) >= 2:
            return cells
        colon_cells = self._split_colon_label_row(row)
        if len(colon_cells) >= 2:
            return colon_cells
        if self._looks_like_time_schedule_row(row):
            return row.split()
        retention_cells = self._split_retention_period_row(row)
        if len(retention_cells) >= 2:
            return retention_cells
        leave_days_cells = self._split_leave_days_value_row(row)
        if len(leave_days_cells) >= 2:
            return leave_days_cells
        record_code_cells = self._split_record_code_row(row)
        if len(record_code_cells) >= 2:
            return record_code_cells

        tokens = row.split()
        header_hits = self._header_hit_count(row)
        numbered_cells = self._split_numbered_quantity_row(row)
        if len(numbered_cells) >= 2:
            return numbered_cells
        if len(tokens) >= 2 and len(tokens) <= 20 and (header_hits >= 2 or self._looks_like_compact_form_header(tokens)):
            return tokens
        role_cells = self._split_role_qualification_row(tokens)
        if len(role_cells) >= 2:
            return role_cells
        if 2 <= header_hits and 12 < len(tokens) <= 45 and self._looks_like_dense_header_row(row):
            return tokens
        if len(tokens) >= 3:
            first_numeric = self._first_numeric_token_index(tokens)
            numeric_count = self._numeric_token_count(tokens)
            if (
                first_numeric is not None
                and first_numeric > 0
                and len(tokens) <= 20
                and self._numeric_row_can_split(row, numeric_count)
            ):
                return [" ".join(tokens[:first_numeric]), *tokens[first_numeric:]]
            if header_hits >= 2 and len(tokens) <= 12:
                return tokens
        return [row.strip()]

    def _looks_like_table_row(self, row: str) -> bool:
        if "|" in row or re.search(r"\S\s{2,}\S", row):
            return True
        if self._header_hit_count(row) >= 2:
            return True
        if len(re.findall(r"\d[\d,.\-㎡m2%]*", row)) >= 2:
            return True
        return self._looks_like_compact_form_header(row.split())

    def _looks_like_bullet_prose_row(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped.startswith(("□", "ㅇ", "◇", "▪")):
            return False
        if len(stripped) >= 45 and re.search(r"(한다|있다|된다|수 없다|수 있다|따른다|노력한다)\.?", stripped):
            return True
        return len(stripped) >= 80 and bool(re.search(r"(추진|개선|관리|창출|준수|편성|집행|노력)", stripped))

    def _looks_like_numbered_clause_prose_row(self, row: str) -> bool:
        stripped = row.strip()
        if not re.match(r"^[①-⑳]\s+", stripped):
            return False
        if "|" in stripped or re.search(r"\S\s{2,}\S", stripped):
            return False
        if self._has_table_numeric_signal(stripped):
            return False
        if re.search(r"(한다|아니한다|된다|있다|없다|부여한다|운용한다|사용한다|따른다|준용한다)\.?", stripped):
            return True
        return len(stripped) >= 35 and bool(re.search(r"(경우|계산|공제|포함|승인|휴가|규정)", stripped))

    def _looks_like_table_caption_or_note_row(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped:
            return False
        if stripped.startswith("※"):
            return True
        if stripped.startswith("*") and not self._has_table_numeric_signal(stripped):
            return True
        if re.match(r"^\(?\s*예시\s*\)?", stripped):
            return True
        if "예시" in stripped and self._header_hit_count(stripped) >= 1 and not self._has_table_numeric_signal(stripped):
            return True
        if (
            stripped.endswith("기준")
            and not self._has_table_numeric_signal(stripped)
            and not re.search(r"(구분|일수|금액|비고|등급|점수|배점)", stripped)
        ):
            return True
        if re.fullmatch(r"<\s*[^<>]{1,80}\s*>", stripped):
            return True
        if re.match(r"^\[(?:별표|별지)[^\]]*\]", stripped):
            return True
        if re.search(r"\(\s*(?:단위|주)\s*[:：]", stripped):
            return True
        return False

    def _classify_table_like(
        self,
        rows: list[str],
        cell_rows: list[dict],
        table_like: bool,
        context_type: str | None,
    ) -> dict:
        if not table_like:
            if context_type in {"appendix", "form"} and self._revision_article_prose_false_positive(rows):
                return self._classification(
                    "probable_false_positive_prose_revision",
                    "article_or_revision_prose_dominates",
                    demote=True,
                    stable_false_positive=True,
                )
            if context_type in {"appendix", "form"} and self._organization_chart_score(rows) >= 2:
                return self._classification(
                    "probable_false_positive_org_chart",
                    "organization_chart_without_structured_cells",
                    demote=True,
                    stable_false_positive=True,
                )
            if context_type in {"appendix", "form"} and self._organization_profile_score(rows) >= 2:
                return self._classification(
                    "probable_false_positive_org_chart",
                    "organization_profile_without_structured_cells",
                    demote=True,
                    stable_false_positive=True,
                )
            return self._classification("not_table_like", "below_table_threshold")
        if context_type in {"paragraph", "item", "subitem"} and len(cell_rows) == 1:
            return self._classification(
                "probable_false_positive_single_row",
                "single_structured_row_without_records",
                demote=True,
                stable_false_positive=True,
            )
        if (
            cell_rows
            and context_type in {"appendix", "form"}
            and self._organization_profile_score(rows) >= 4
        ):
            return self._classification(
                "probable_false_positive_org_chart",
                "organization_profile_without_structured_cells",
                demote=True,
                stable_false_positive=True,
            )
        if cell_rows:
            if context_type in {"appendix", "form"} and self._looks_like_outline_prose_cell_rows(cell_rows):
                return self._classification(
                    "probable_false_positive_prose_list",
                    "outline_list_with_truncated_cells",
                    demote=True,
                    stable_false_positive=True,
                )
            return self._classification("structured_table", "structured_cell_rows_present")

        compact_score = self._compact_table_candidate_score(rows)
        prose_score = self._prose_revision_score(rows)
        prose_list_score = self._prose_list_score(rows)
        appendix_bullet_score = self._appendix_bullet_list_score(rows)
        cover_score = self._cover_or_guideline_prose_score(rows)
        form_score = self._form_template_score(rows)
        article_prose_score = self._article_prose_fragment_score(rows)
        repeated_article_heading_score = self._repeated_article_heading_score(rows)
        long_prose_score = self._long_prose_fragment_score(rows)
        org_chart_score = self._organization_chart_score(rows)
        org_profile_score = self._organization_profile_score(rows)
        if prose_score >= 3 and compact_score < 4:
            return self._classification(
                "probable_false_positive_prose_revision",
                "article_or_revision_prose_dominates",
                demote=True,
                stable_false_positive=True,
            )
        if prose_list_score >= 4 and compact_score < 3:
            return self._classification(
                "probable_false_positive_prose_list",
                "sentence_or_list_prose_dominates",
                demote=True,
                stable_false_positive=True,
            )
        if context_type in {"paragraph", "item", "subitem"} and prose_list_score >= 5:
            return self._classification(
                "probable_false_positive_budget_prose",
                "budget_guideline_bullet_prose_dominates",
                demote=True,
                stable_false_positive=True,
            )
        if context_type in {"paragraph", "item", "subitem"} and self._short_clause_fragment_score(rows) >= 2:
            return self._classification(
                "probable_false_positive_article_prose",
                "short_clause_fragment_without_structured_cells",
                demote=True,
                stable_false_positive=True,
            )
        if cover_score >= 4 and compact_score < 4:
            return self._classification(
                "probable_false_positive_cover_prose",
                "cover_or_guideline_intro_prose",
                demote=True,
                stable_false_positive=True,
            )
        if context_type == "form" and form_score >= 2 and compact_score < 2:
            return self._classification(
                "probable_false_positive_form_template",
                "form_signature_or_application_markers",
                demote=True,
                stable_false_positive=True,
            )
        if context_type in {"appendix", "form"} and appendix_bullet_score >= 4 and compact_score < 2:
            return self._classification(
                "probable_false_positive_prose_list",
                "appendix_bullet_list_without_structured_cells",
                demote=True,
                stable_false_positive=True,
            )
        if context_type in {"appendix", "form"} and article_prose_score >= 5 and compact_score < 4:
            return self._classification(
                "probable_false_positive_article_prose",
                "article_prose_fragment_without_structured_cells",
                demote=True,
                stable_false_positive=True,
            )
        if context_type in {"appendix", "form"} and repeated_article_heading_score >= 2:
            return self._classification(
                "probable_false_positive_article_prose",
                "repeated_article_heading_fragment_without_structured_cells",
                demote=True,
                stable_false_positive=True,
            )
        if context_type in {"appendix", "form"} and org_chart_score >= 2:
            return self._classification(
                "probable_false_positive_org_chart",
                "organization_chart_without_structured_cells",
                demote=True,
                stable_false_positive=True,
            )
        if context_type in {"appendix", "form"} and org_profile_score >= 2:
            return self._classification(
                "probable_false_positive_org_chart",
                "organization_profile_without_structured_cells",
                demote=True,
                stable_false_positive=True,
            )
        if context_type in {"appendix", "form"} and long_prose_score >= 3:
            return self._classification(
                "probable_false_positive_article_prose",
                "long_sentence_fragment_without_structured_cells",
                demote=True,
                stable_false_positive=True,
            )
        if compact_score >= 2:
            return self._classification("probable_table_extraction_failed", "compact_table_signals_without_cell_rows")
        return self._classification("probable_table_extraction_failed", "raw_table_like_rows_without_cell_rows")

    def _classification(
        self,
        classification: str,
        reason: str,
        demote: bool = False,
        stable_false_positive: bool = False,
    ) -> dict:
        is_false_positive = classification.startswith("probable_false_positive")
        return {
            "classification": classification,
            "reason": reason,
            "demote": demote,
            "probable_false_positive": is_false_positive,
            "probable_extraction_failed": classification == "probable_table_extraction_failed",
            "false_positive_stability": "stable" if is_false_positive and stable_false_positive else "attention",
        }

    def _looks_like_outline_prose_cell_rows(self, cell_rows: list[dict]) -> bool:
        if len(cell_rows) < 2 or len(cell_rows) > 4:
            return False
        for row in cell_rows:
            raw = str(row.get("raw") or "").strip()
            cells = [str(cell).strip() for cell in row.get("cells") or []]
            flags = set(row.get("row_quality_flags") or [])
            if not raw or not re.match(r"^\d+\.\s+", raw):
                return False
            if len(cells) < 5:
                return False
            if int(row.get("numeric_cell_count") or 0) > 1:
                return False
            if not flags.intersection({"possible_truncated_cell", "many_text_cells_without_numeric_signal"}):
                return False
        return True

    def _compact_table_candidate_score(self, rows: list[str]) -> int:
        score = 0
        for row in rows:
            normalized = self._normalize_label_spacing(row)
            if self._header_hit_count(row) >= 2:
                score += 1
            if self._has_table_numeric_signal(row):
                score += 1
            compact_tokens = ["학부", "전공", "입학정원", "직종", "직급", "등급", "단가", "항공료", "숙박비", "식비", "점수"]
            if sum(1 for token in compact_tokens if token in normalized) >= 2:
                score += 1
            if self._looks_like_time_schedule_row(row) or self._split_role_qualification_row(row.split()):
                score += 1
        return score

    def _prose_revision_score(self, rows: list[str]) -> int:
        score = 0
        for row in rows:
            stripped = row.strip()
            if re.search(r"제\s*\d+\s*조", stripped):
                score += 1
            if re.match(r"^(제정|개정|일부개정|전부개정|신설|삭제|시행)\b", stripped):
                score += 1
            if re.search(r"<\s*(개정|신설|삭제|시행|전문개정|일부개정)", stripped):
                score += 1
            if len(stripped) >= 70 and re.search(r"(한다|있다|된다|따른다)\.?", stripped):
                score += 1
        return score

    def _revision_article_prose_false_positive(self, rows: list[str]) -> bool:
        if self._prose_revision_score(rows) < 3:
            return False
        if self._article_prose_fragment_score(rows) >= 2:
            return True
        if self._repeated_article_heading_score(rows) >= 1:
            return True
        return any(
            re.search(r"(議|조|목적|적용|한다|한다\.)", row)
            and len(row.strip()) >= 20
            for row in rows
        )

    def _prose_list_score(self, rows: list[str]) -> int:
        score = 0
        for row in rows:
            stripped = row.strip()
            if stripped.startswith(("□", "ㅇ", "◇", "▪")) and len(stripped) >= 80 and re.search(
                r"(추진|개선|관리|창출|준수|편성|집행|노력)",
                stripped,
            ):
                score += 2
            if stripped.startswith(("□", "ㅇ", "◇", "▪")) and len(stripped) >= 45 and re.search(
                r"(한다|있다|된다|수 없다|수 있다|하여야 한다|노력한다)\.?",
                stripped,
            ):
                score += 2
            if re.match(r"^[가-힣]\.\s+", stripped) and re.search(r"(다|경우|단체|공직자)\.?", stripped):
                score += 1
            if re.match(r"^[\u2460-\u2473]\s*[\"“]", stripped) and len(stripped) >= 45:
                score += 2
            if "[예]" in stripped and re.search(r"(이동|개정|경우)", stripped):
                score += 2
            if re.match(r"^\d+\)\s+\[별표", stripped) and re.search(r"(이동|개정)", stripped):
                score += 1
            if "일반용지" in stripped and re.search(r"\d+\s*mm", stripped):
                score += 1
            if len(stripped) >= 45 and re.search(r"(한다|있다|된다|따른다|하여야 한다|수 있다)\.?", stripped):
                score += 1
            if re.match(r"^[가-힣]\.\s+", stripped) and re.search(r"(한다|있다|된다|따른다|경우|때에는)", stripped):
                score += 1
            if re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]", stripped) and len(stripped) >= 45:
                score += 1
            if stripped.startswith(("▪", "□")) and len(stripped) >= 45:
                score += 1
            if "[예]" in stripped and re.search(r"(이동|개정|경우)", stripped):
                score += 1
            if re.match(r"^\d+\.\s+", stripped) and len(stripped) >= 40 and not self._has_table_numeric_signal(stripped):
                score += 1
        return score

    def _appendix_bullet_list_score(self, rows: list[str]) -> int:
        score = 0
        for row in rows:
            stripped = row.strip()
            if not stripped:
                continue
            if re.match(r"^\d+\.\s+", stripped) and not self._has_table_numeric_signal(stripped):
                score += 1
            if stripped.startswith(("▪", "-", "*")) and not self._has_table_numeric_signal(stripped):
                score += 1
            if stripped.startswith(("▪", "-", "*")) and re.search(r"(사항|계획|시정|공시|감사|평가)", stripped):
                score += 1
        return score

    def _cover_or_guideline_prose_score(self, rows: list[str]) -> int:
        joined = " ".join(rows)
        score = 0
        if re.search(r"\d{4}\s*년도", joined):
            score += 1
        if re.search(r"(예산집행지침|예산편성지침|예산운용지침|작성\s*가이드)", joined):
            score += 1
        if "기획재정부" in self._normalize_label_spacing(joined):
            score += 1
        if any(re.match(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\.", row.strip()) for row in rows):
            score += 1
        if sum(1 for row in rows if row.strip().startswith("◇")) >= 2:
            score += 1
        if any(row.strip().startswith("□") and len(row.strip()) >= 120 for row in rows):
            score += 1
        return score

    def _form_template_score(self, rows: list[str]) -> int:
        joined = " ".join(rows)
        markers = ["년 월 일", "(인)", "(직인)", "직인", "귀하", "신청자", "신청인", "위임인", "대리인", "성명", "주소"]
        return sum(1 for marker in markers if marker in joined)

    def _article_prose_fragment_score(self, rows: list[str]) -> int:
        score = 0
        for row in rows:
            stripped = row.strip()
            if not stripped:
                continue
            if re.search(r"\uc81c\s*\d+\s*\uc870", stripped):
                score += 2
            if re.match(r"^[\u2460-\u2473]", stripped):
                score += 1
            if re.search(r"(\ub2e4\uc74c\s*\uac01\s*\ud638|\uc5b4\ub290\s*\ud558\ub098)", stripped):
                score += 1
            if re.search(r"(\ud558\uc5ec\uc57c\s*\ud55c\ub2e4|\ud55c\ub2e4|\.?\ub2e4\.)$", stripped):
                score += 1
            if len(stripped) >= 35 and re.search(r"(\ud558\uc5ec\uc57c|\ud55c\ub2e4|\uc788\ub2e4|\uc5c6\ub2e4|\uacbd\uc6b0|\ub530\ub77c)", stripped):
                score += 1
        return score

    def _short_clause_fragment_score(self, rows: list[str]) -> int:
        nonempty = [row.strip() for row in rows if row.strip()]
        if not (2 <= len(nonempty) <= 5):
            return 0
        joined = " ".join(nonempty)
        score = 0
        if re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]", nonempty[0]):
            score += 1
        if re.search(r"제\s*\d+\s*조(?:\s*제\s*\d+\s*항)?", joined):
            score += 1
        if re.search(r"(한다|아니한다|된다|포함|계산|공제|따른다)\.?", joined):
            score += 1
        if sum(1 for row in nonempty if len(row) < 80) >= len(nonempty) - 1:
            score += 1
        return score

    def _repeated_article_heading_score(self, rows: list[str]) -> int:
        article_numbers: set[str] = set()
        heading_count = 0
        for row in rows:
            stripped = row.strip()
            match = re.match(r"^제\s*(\d+(?:\s*의\s*\d+)?)\s*조(?:\s*\([^)]{1,80}\))?", stripped)
            if not match:
                continue
            article_numbers.add(self._normalize_label_spacing(match.group(1)))
            heading_count += 1
        return heading_count if len(article_numbers) >= 2 else 0

    def _long_prose_fragment_score(self, rows: list[str]) -> int:
        score = 0
        content_rows = [row.strip() for row in rows if row.strip()]
        if len(content_rows) > 5:
            return 0
        for row in content_rows:
            if len(row) >= 70 and re.search(r"(하여야\s*한다|한다|있다|없다|않는다|된다)", row):
                score += 2
            if re.search(r"(제정|개정|신설|삭제).{0,20}\d{4}\.\s*\d{1,2}\.\s*\d{1,2}", row):
                score += 1
            if re.search(r"(다만|경우에는|관계법령|불가항력)", row):
                score += 1
        return score

    def _organization_chart_score(self, rows: list[str]) -> int:
        joined = " ".join(rows[:8])
        score = 0
        if "기구표" in joined:
            score += 2
        if re.search(r"(본부|처|실|부|팀|담당|사업단|연구원)", joined):
            score += 1
        if sum(1 for row in rows if len(row.split()) >= 5 and re.search(r"(부|처|실|팀|담당)", row)) >= 2:
            score += 1
        return score

    def _organization_profile_score(self, rows: list[str]) -> int:
        normalized_rows = [self._normalize_label_spacing(row) for row in rows]
        joined = " ".join(normalized_rows)
        score = 0
        if "기구표" in joined or "<조직>" in joined or "조직" in joined:
            score += 1
        if "<명칭>" in joined or "명칭" in joined:
            score += 1
        if "<소재지>" in joined or "소재지" in joined or "주소" in joined:
            score += 1
        department_like = sum(
            1
            for row in normalized_rows
            if re.search(r"(이사장|감사|실|처|본부|팀|센터|사업운영|경영지원|자산관리)", row)
        )
        if department_like >= 3:
            score += 1
        return score

    def _header_hit_count(self, row: str) -> int:
        normalized = self._normalize_label_spacing(row)
        return sum(1 for token in self._normalized_header_tokens() if token in normalized)

    def _normalize_label_spacing(self, value: str) -> str:
        return _normalize_label_spacing_cached(value)

    @classmethod
    @lru_cache(maxsize=1)
    def _normalized_header_tokens(cls) -> tuple[str, ...]:
        return tuple(_normalize_label_spacing_cached(token) for token in cls.HEADER_TOKENS)

    def _first_numeric_token_index(self, tokens: list[str]) -> int | None:
        for index, token in enumerate(tokens):
            if re.search(r"\d", token):
                return index
        return None

    def _numeric_token_count(self, tokens: list[str]) -> int:
        return sum(1 for token in tokens if re.search(r"\d", token))

    def _numeric_table_row_count(self, rows: list[str]) -> int:
        return sum(
            1
            for row in rows
            if len(re.findall(r"\d[\d,.\-㎡m%]*", row)) >= 2
            and not self._looks_like_non_table_numeric_row(row)
            and not self._date_revision_row(row)
        )

    def _numeric_row_can_split(self, row: str, numeric_count: int) -> bool:
        if self._looks_like_non_table_numeric_row(row):
            return False
        has_table_numeric_signal = self._has_table_numeric_signal(row)
        if numeric_count >= 2:
            return has_table_numeric_signal
        total_labels = ["합계", "총계", "총 계", "소계", "계"]
        if numeric_count == 1 and has_table_numeric_signal and len(row.split()) <= 8:
            return True
        return numeric_count == 1 and has_table_numeric_signal and any(label in row for label in total_labels)

    def _has_table_numeric_signal(self, row: str) -> bool:
        return bool(
            re.search(r"\d{1,3}(?:,\d{3})+", row)
            or re.search(r"\d+\.\d+", row)
            or re.search(r"\d{1,2}\s*:\s*\d{2}", row)
            or re.search(r"\d+\s*(?:원|만원|천원|㎡|m2|%|점|명)", row)
        )

    def _looks_like_non_table_numeric_row(self, row: str) -> bool:
        stripped = row.strip()
        if stripped.startswith(("[별표", "[별지", "별표", "별지")):
            return True
        if re.search(r"(제정|개정|신설|삭제|시행).{0,20}\d{4}\.\s*\d{1,2}\.\s*\d{1,2}", stripped):
            return True
        if re.search(r"제\s*\d+\s*조", stripped) and "관련" in stripped and len(stripped.split()) <= 8:
            return True
        return False

    def _looks_like_compact_form_header(self, tokens: list[str]) -> bool:
        if not (2 <= len(tokens) <= 8):
            return False
        joined = self._normalize_label_spacing(" ".join(tokens))
        known = ["구분", "성명", "생년월일", "직위", "소속", "주소", "연락처", "비고", "직무", "의견", "건물명"]
        return sum(1 for token in known if self._normalize_label_spacing(token) in joined) >= 2

    def _split_colon_label_row(self, row: str) -> list[str]:
        if ":" not in row:
            return [row.strip()]
        label, value = row.split(":", 1)
        label = re.sub(r"^[◦ㆍ\-•\s]+", "", label).strip()
        if not label or re.match(r"^[가-힣]\.", label) or re.match(r"^\d+\.", label):
            return [row.strip()]
        if len(label.split()) > 6 or len(label) > 30:
            return [row.strip()]
        return [label, value.strip()]

    def _looks_like_time_schedule_row(self, row: str) -> bool:
        stripped = row.strip()
        if not re.search(r"\d{1,2}\s*:\s*\d{2}", stripped):
            return False
        return bool(re.match(r"^(?:[A-Z]-?\d+|[월화수목금토일])\s+", stripped))

    def _split_role_qualification_row(self, tokens: list[str]) -> list[str]:
        if len(tokens) < 2:
            return []
        first_numeric = self._first_numeric_token_index(tokens)
        role_labels = {
            "교수",
            "부교수",
            "조교수",
            "수석연구원",
            "책임연구원",
            "선임연구원",
            "정연구원",
            "연구원",
        }
        if first_numeric == 1 and self._normalize_label_spacing(tokens[0]) in role_labels:
            return [tokens[0], " ".join(tokens[1:])]
        return []

    def _split_numbered_quantity_row(self, row: str) -> list[str]:
        match = re.match(
            r"^\s*(\d+\.)\s+(.+?)\s+(\d+\s*(?:매|통|부|명|원|만원|천원|점|%))\.?\s*$",
            row.strip(),
        )
        if not match:
            return []
        return [match.group(1), match.group(2).strip(), match.group(3).strip()]

    def _split_retention_period_row(self, row: str) -> list[str]:
        match = re.match(r"^\s*((?:영구|\d+\s*년)\s*보존)\s*$", row.strip())
        if not match:
            return []
        return ["보존기간", match.group(1).replace(" ", "")]

    def _split_leave_days_header_row(self, row: str) -> list[str]:
        normalized = self._normalize_label_spacing(row)
        if normalized in {"근속년수연차휴가일수", "근속연수연차휴가일수"}:
            return ["근속년수", "연차휴가 일수"]
        return []

    def _split_leave_days_value_row(self, row: str) -> list[str]:
        stripped = row.strip()
        match = re.match(
            r"^((?:\d+\s*개월\s*이상\s+\d+\s*년\s*미만)|(?:\d+\s*년\s*이상\s+\d+\s*년\s*미만)|(?:\d+\s*년\s*이상))\s+(\d{1,3})\s*$",
            stripped,
        )
        if not match:
            return []
        return [re.sub(r"\s+", " ", match.group(1)).strip(), match.group(2)]

    def _split_record_code_row(self, row: str) -> list[str]:
        match = re.match(r"^\s*(\d+(?:-\d+){1,4})\s+(.{2,40})\s*$", row.strip())
        if not match:
            return []
        title = match.group(2).strip()
        if re.search(r"(규정|지침|기준|요령|편람)$", title):
            return ["분류번호", match.group(1), title]
        return []

    def _extract_vertical_compact_cell_rows(self, rows: list[str]) -> list[dict]:
        for header_index, row in enumerate(rows):
            if self._normalize_label_spacing(row) != "구분":
                continue
            label_index = self._find_vertical_value_label_index(rows, header_index)
            if label_index is None:
                continue
            categories = [item for item in rows[header_index + 1 : label_index] if self._vertical_cell_candidate(item)]
            values = [
                item
                for item in rows[label_index + 1 : label_index + 1 + max(2, len(categories) + 1)]
                if self._vertical_value_candidate(item)
            ]
            if len(categories) < 2 or len(values) < 2:
                continue
            return [
                {
                    "row_index": header_index,
                    "cells": [row.strip(), *categories],
                    "raw": " | ".join([row.strip(), *categories]),
                    "numeric_cell_count": sum(1 for cell in [row.strip(), *categories] if re.search(r"\d", cell)),
                },
                {
                    "row_index": label_index,
                    "cells": [rows[label_index].strip(), *values],
                    "raw": " | ".join([rows[label_index].strip(), *values]),
                    "numeric_cell_count": sum(1 for cell in [rows[label_index].strip(), *values] if re.search(r"\d", cell)),
                },
            ]
        return []

    def _extract_vertical_checklist_cell_rows(
        self,
        rows: list[str],
        *,
        context_type: str | None,
    ) -> list[dict]:
        if context_type not in {"appendix", "form", "table"} or len(rows) < 6:
            return []
        for header_index in range(len(rows) - 1):
            header = rows[header_index].strip()
            subheader = rows[header_index + 1].strip()
            if not self._vertical_checklist_header_candidate(header, subheader):
                continue
            data_rows: list[dict] = []
            current_label: str | None = None
            current_row_index: int | None = None
            current_values: list[str] = []
            for row_index, raw in enumerate(rows[header_index + 2 :], start=header_index + 2):
                stripped = raw.strip()
                if not stripped or self._looks_like_table_caption_or_note_row(stripped):
                    continue
                if self._vertical_checklist_label_candidate(stripped):
                    if current_label and current_values and current_row_index is not None:
                        data_rows.append(
                            self._reconstructed_cell_row(
                                current_row_index,
                                [current_label, " ".join(current_values).strip()],
                                "vertical_checklist_row",
                            )
                        )
                    current_label = stripped
                    current_row_index = row_index
                    current_values = []
                    continue
                if current_label is None:
                    continue
                current_values.append(stripped)
            if current_label and current_values and current_row_index is not None:
                data_rows.append(
                    self._reconstructed_cell_row(
                        current_row_index,
                        [current_label, " ".join(current_values).strip()],
                        "vertical_checklist_row",
                    )
                )
            if len(data_rows) >= 2:
                return [
                    self._cell_row(header_index, [header, subheader]),
                    *data_rows,
                ]
        return []

    def _vertical_checklist_header_candidate(self, first: str, second: str) -> bool:
        if not self._vertical_checklist_label_candidate(first):
            return False
        if not self._vertical_checklist_label_candidate(second):
            return False
        header_text = self._normalize_label_spacing(f"{first} {second}")
        return bool(
            re.search(r"(유형|수칙|기준|항목|현황|내용|실적|평가)", header_text)
            or (len(first) <= 12 and len(second) <= 18)
        )

    def _vertical_checklist_label_candidate(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped or len(stripped) > 18:
            return False
        if self._looks_like_table_caption_or_note_row(stripped):
            return False
        if re.match(r"^(?:\d+[\.\)]|[IVXLCDM]+\.|[가-힣]\.)", stripped):
            return False
        if re.search(r"\d", stripped):
            return False
        return True

    def _extract_named_area_cell_rows(self, rows: list[str]) -> list[dict]:
        normalized_rows = [self._normalize_label_spacing(row) for row in rows]
        for header_index in range(0, max(0, len(rows) - 2)):
            if (
                "명칭" not in normalized_rows[header_index]
                or "구역" not in normalized_rows[header_index + 1]
                or "비고" not in normalized_rows[header_index + 2]
            ):
                continue

            header_cells = [
                rows[header_index].strip(),
                rows[header_index + 1].strip(),
                rows[header_index + 2].strip(),
            ]
            data_rows: list[dict] = [
                {
                    "row_index": header_index,
                    "cells": header_cells,
                    "raw": " | ".join(header_cells),
                    "numeric_cell_count": 0,
                }
            ]
            index = header_index + 3
            while index < len(rows):
                current = rows[index].strip()
                if not current:
                    index += 1
                    continue
                if self._looks_like_table_caption_or_note_row(current):
                    break
                if current.startswith("계"):
                    summary = current.removeprefix("계").strip()
                    if summary and re.search(r"\d", summary):
                        cells = ["계", summary, ""]
                        data_rows.append(
                            {
                                "row_index": index,
                                "cells": cells,
                                "raw": " | ".join(cells),
                                "numeric_cell_count": 1,
                                "review_required": True,
                                "row_quality_flags": ["named_area_summary_row"],
                            }
                        )
                    break
                if index + 2 >= len(rows):
                    break
                name = rows[index].strip()
                area = rows[index + 1].strip()
                note = rows[index + 2].strip()
                if (
                    self._named_area_name_candidate(name)
                    and self._named_area_region_candidate(area)
                    and self._named_area_note_candidate(note)
                ):
                    cells = [name, area, note]
                    data_rows.append(
                        {
                            "row_index": index,
                            "cells": cells,
                            "raw": " | ".join(cells),
                            "numeric_cell_count": sum(1 for cell in cells if re.search(r"\d", cell)),
                            "review_required": True,
                            "row_quality_flags": ["named_area_vertical_reconstruction"],
                        }
                    )
                    index += 3
                    continue
                index += 1

            if len(data_rows) >= 3:
                return data_rows
        return []

    def _extract_salary_group_cell_rows(self, rows: list[str]) -> list[dict]:
        for header_index, row in enumerate(rows):
            tokens = row.split()
            if "연봉그룹" not in tokens or "그룹" not in row:
                continue
            try:
                grade_index = tokens.index("직")
                if tokens[grade_index + 1] != "급":
                    continue
                group_headers = tokens[grade_index + 2 :]
            except (ValueError, IndexError):
                if "직급" not in tokens:
                    continue
                grade_index = tokens.index("직급")
                group_headers = tokens[grade_index + 1 :]
            if len(group_headers) < 2:
                continue
            header_cells = ["직급", *group_headers]
            data_rows: list[dict] = [
                {
                    "row_index": header_index,
                    "cells": header_cells,
                    "raw": " | ".join(header_cells),
                    "numeric_cell_count": 0,
                }
            ]
            for row_index in range(header_index + 1, len(rows)):
                stripped = rows[row_index].strip()
                match = re.match(r"^(\d+)\s*급\s+(.+)$", stripped)
                if not match:
                    if data_rows and len(data_rows) > 1:
                        break
                    continue
                values = match.group(2).split()
                if len(values) != len(group_headers) or not all(re.fullmatch(r"\d[\d,]*", value) for value in values):
                    continue
                cells = [f"{match.group(1)}급", *values]
                data_rows.append(
                    {
                        "row_index": row_index,
                        "cells": cells,
                        "raw": " | ".join(cells),
                        "numeric_cell_count": len(values),
                        "review_required": True,
                        "row_quality_flags": ["salary_group_row"],
                    }
                )
            if len(data_rows) >= 3:
                return data_rows
        return []

    def _extract_salary_assessment_cell_rows(self, rows: list[str]) -> list[dict]:
        joined = " ".join(rows)
        if not ("연봉" in joined and "최저연봉" in joined and "환산율" in joined):
            return []

        result: list[dict] = [
            {
                "row_index": -1,
                "cells": ["구분", "기준", "값"],
                "raw": "구분 | 기준 | 값",
                "numeric_cell_count": 0,
                "review_required": True,
                "row_quality_flags": ["salary_assessment_reconstruction"],
            }
        ]
        for index, row in enumerate(rows):
            stripped = row.strip()
            match = re.match(r"^(?P<label>.+경력)\s+(?P<rate>\d{1,3}\s*%)$", stripped)
            if not match or "직무" not in match.group("label"):
                continue
            label = match.group("label").strip()
            rate = match.group("rate").replace(" ", "")
            result.append(
                {
                    "row_index": index,
                    "cells": ["경력환산기준", label, rate],
                    "raw": f"경력환산기준 | {label} | {rate}",
                    "numeric_cell_count": 1,
                    "review_required": True,
                    "row_quality_flags": ["salary_assessment_reconstruction"],
                }
            )

        for index, row in enumerate(rows):
            if "년미만" not in row or "최저연봉" in row:
                continue
            period_rows = [row]
            if index + 1 < len(rows) and "년이상" in rows[index + 1]:
                period_rows.append(rows[index + 1])
            periods = self._salary_assessment_periods(" ".join(period_rows))
            value_row = self._salary_assessment_value_row(rows[index + len(period_rows) : index + len(period_rows) + 4])
            if len(periods) >= 2 and value_row and len(value_row) >= len(periods):
                for offset, period in enumerate(periods):
                    value = value_row[offset]
                    result.append(
                        {
                            "row_index": index + offset,
                            "cells": ["연봉 사정기준", period, value],
                            "raw": f"연봉 사정기준 | {period} | {value}",
                            "numeric_cell_count": 1,
                            "review_required": True,
                            "row_quality_flags": ["salary_assessment_reconstruction"],
                        }
                    )
                break

        return result if len(result) >= 4 else []

    def _salary_assessment_periods(self, value: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", value.replace("비 고", "").replace("비고", "")).strip()
        matches = re.findall(
            r"\d+\s*년\s*이상\s+\d+\s*년\s*미만|\d+\s*년\s*미만|\d+\s*년\s*이상",
            normalized,
        )
        return [re.sub(r"\s+", " ", match).strip() for match in matches]

    def _salary_assessment_value_row(self, rows: list[str]) -> list[str]:
        for row in rows:
            values = [token.strip() for token in row.split() if token.strip().startswith("최저연봉")]
            if len(values) >= 2:
                return values
        return []

    def _extract_code_description_cell_rows(self, rows: list[str]) -> list[dict]:
        pairs: list[tuple[int, str, str]] = []
        index = 0
        while index < len(rows) - 1:
            first = rows[index].strip()
            second = rows[index + 1].strip()
            if self._looks_like_description_row(first) and self._looks_like_classification_code_row(second):
                pairs.append((index, second, first))
                index += 2
                continue
            if self._looks_like_classification_code_row(first) and self._looks_like_description_row(second):
                pairs.append((index, first, second))
                index += 2
                continue
            index += 1
        if len(pairs) < 2:
            return []

        result = [
            {
                "row_index": pairs[0][0],
                "cells": ["분류코드", "대상"],
                "raw": "분류코드 | 대상",
                "numeric_cell_count": 0,
            }
        ]
        for row_index, code, description in pairs:
            result.append(
                {
                    "row_index": row_index,
                    "cells": [code, description],
                    "raw": f"{code} | {description}",
                    "numeric_cell_count": 1 if re.search(r"\d", code) else 0,
                }
            )
        return result

    def _extract_dense_numeric_cell_rows(
        self,
        rows: list[str],
        *,
        context_type: str | None,
    ) -> list[dict]:
        if context_type not in {"appendix", "form", "table"}:
            return []

        data_rows: list[dict] = []
        for row_index, row in enumerate(rows):
            stripped = row.strip()
            if not stripped or self._looks_like_table_caption_or_note_row(stripped):
                continue
            data_rows.extend(self._dense_numeric_segments(row_index, stripped))

        if len(data_rows) < 3:
            return []
        max_numeric_count = max((len(row["cells"]) - 1 for row in data_rows), default=0)
        if max_numeric_count < 2:
            return []

        header = ["구분", *[f"수치{i}" for i in range(1, max_numeric_count + 1)]]
        return [
            {
                "row_index": -1,
                "cells": header,
                "raw": " | ".join(header),
                "numeric_cell_count": 0,
                "review_required": True,
                "row_quality_flags": ["dense_numeric_row_reconstruction"],
            },
            *data_rows,
        ]

    def _dense_numeric_segments(self, row_index: int, row: str) -> list[dict]:
        tokens = row.split()
        if len(tokens) < 3:
            return []
        if self._looks_like_non_table_numeric_row(row):
            return []
        if self._date_revision_row(row):
            return []

        segments: list[dict] = []
        label_tokens: list[str] = []
        numeric_tokens: list[str] = []
        for token in tokens:
            if self._dense_numeric_value_token(token):
                if not label_tokens:
                    continue
                numeric_tokens.append(token)
                continue
            if numeric_tokens:
                self._append_dense_numeric_segment(segments, row_index, label_tokens, numeric_tokens)
                label_tokens = []
                numeric_tokens = []
            label_tokens.append(token)
        if numeric_tokens:
            self._append_dense_numeric_segment(segments, row_index, label_tokens, numeric_tokens)
        return segments

    def _append_dense_numeric_segment(
        self,
        segments: list[dict],
        row_index: int,
        label_tokens: list[str],
        numeric_tokens: list[str],
    ) -> None:
        if len(numeric_tokens) < 2:
            return
        label = " ".join(label_tokens).strip()
        if not self._dense_numeric_label_candidate(label):
            return
        cells = [label, *numeric_tokens]
        segments.append(
            {
                "row_index": row_index,
                "cells": cells,
                "raw": " | ".join(cells),
                "numeric_cell_count": len(numeric_tokens),
                "review_required": True,
                "row_quality_flags": ["dense_numeric_row_reconstruction"],
            }
        )

    def _dense_numeric_value_token(self, token: str) -> bool:
        return bool(re.fullmatch(r"\d{1,4}", token.strip()))

    def _dense_numeric_label_candidate(self, label: str) -> bool:
        stripped = label.strip()
        if not stripped or len(stripped) > 40:
            return False
        if re.match(r"^제\s*\d+\s*조", stripped):
            return False
        if re.search(r"\d{4}\.\s*\d{1,2}\.\s*\d{1,2}", stripped):
            return False
        return bool(re.search(r"[가-힣A-Za-z]", stripped))

    def _date_revision_row(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped:
            return False
        date_like = re.findall(r"\d{4}\.\s*\d{1,2}\.\s*\d{1,2}", stripped)
        return len(date_like) >= 2 and not re.search(r"[가-힣A-Za-z]{2,}", stripped)

    def _extract_parallel_value_tail_cell_rows(self, rows: list[str]) -> list[dict]:
        indexed_rows = [
            (index, row.strip())
            for index, row in enumerate(rows)
            if row.strip() and not self._looks_like_table_caption_or_note_row(row)
        ]
        if len(indexed_rows) < 4:
            return []

        tail: list[tuple[int, str]] = []
        for index, row in reversed(indexed_rows):
            if self._parallel_tail_value_candidate(row):
                tail.append((index, row))
                continue
            if tail:
                break
        tail.reverse()
        if len(tail) < 2 or len(tail) > 20:
            return []

        first_tail_index = tail[0][0]
        label_pool = [(index, row) for index, row in indexed_rows if index < first_tail_index]
        label_candidates = self._parallel_tail_label_candidates(label_pool, len(tail))
        if len(label_candidates) != len(tail):
            return []

        value_header = self._parallel_tail_value_header([value for _, value in tail])
        result = [
            {
                "row_index": label_candidates[0][0],
                "cells": ["기준", value_header],
                "raw": f"기준 | {value_header}",
                "numeric_cell_count": 0,
            }
        ]
        for (row_index, label), (_, value) in zip(label_candidates, tail):
            result.append(
                {
                    "row_index": row_index,
                    "cells": [label, value],
                    "raw": f"{label} | {value}",
                    "numeric_cell_count": 1 if re.search(r"\d", value) else 0,
                    "review_required": True,
                    "row_quality_flags": ["parallel_value_tail_reconstruction"],
                }
            )
        return result

    def _parallel_tail_label_candidates(
        self,
        label_pool: list[tuple[int, str]],
        value_count: int,
    ) -> list[tuple[int, str]]:
        if not label_pool:
            return []
        numeric_subitems = [
            item for item in label_pool if re.match(r"^\s*\d+\)\s+", item[1])
        ]
        if len(numeric_subitems) >= value_count:
            return numeric_subitems[-value_count:]
        alpha_items = [
            item for item in label_pool if re.match(r"^\s*[가-하]\.\s+", item[1])
        ]
        if len(alpha_items) >= value_count:
            return alpha_items[-value_count:]
        marked_items = [
            item
            for item in label_pool
            if re.match(r"^\s*(?:\d+[\.\)]|[가-하]\.|[①-⑳])\s*", item[1])
            and not self._parallel_tail_value_candidate(item[1])
        ]
        if len(marked_items) >= value_count:
            return marked_items[-value_count:]
        descriptive_items = [
            item
            for item in label_pool
            if self._parallel_tail_label_candidate(item[1])
        ]
        if len(descriptive_items) >= value_count:
            return descriptive_items[-value_count:]
        return []

    def _parallel_tail_label_candidate(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped or len(stripped) > 120:
            return False
        if self._parallel_tail_value_candidate(stripped):
            return False
        if stripped.startswith(("※", "*", "<", "[")):
            return False
        if not re.search(r"[가-힣]", stripped):
            return False
        return True

    def _parallel_tail_value_candidate(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped or len(stripped) > 40:
            return False
        if stripped in {"-", "해당없음", "해당 없음"}:
            return True
        if re.search(r"(제정|개정|신설|삭제|시행|제\s*\d+\s*조)", stripped):
            return False
        if re.search(r"(한다|있다|된다|따른다|하여야 한다)\.?", stripped):
            return False
        if not re.search(r"\d+\s*(?:년|개월|일)", stripped):
            return False
        return bool(re.fullmatch(r"[0-9년개월일\s,~\-()가-힣]+", stripped))

    def _parallel_tail_value_header(self, values: list[str]) -> str:
        joined = " ".join(values)
        if re.search(r"\d+\s*(?:년|개월)", joined):
            return "기간"
        if re.search(r"\d+\s*일", joined):
            return "소요일수"
        return "값"

    def _looks_like_classification_code_row(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped or len(stripped) > 30:
            return False
        if re.search(r"[가-힣]", stripped):
            return False
        if not re.search(r"\d", stripped):
            return False
        return bool(re.fullmatch(r"[0-9A-Za-z.,*\-\s]+", stripped))

    def _looks_like_description_row(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped or len(stripped) > 120:
            return False
        if stripped.startswith(("[주", "※", "*", "<", "[")):
            return False
        if self._looks_like_classification_code_row(stripped):
            return False
        return bool(re.search(r"[가-힣]", stripped))

    def _find_vertical_value_label_index(self, rows: list[str], header_index: int) -> int | None:
        for index in range(header_index + 1, min(len(rows), header_index + 12)):
            normalized = self._normalize_label_spacing(rows[index])
            if normalized in {"분량", "금액", "기준", "내용", "점수", "배점"}:
                return index
        return None

    def _vertical_cell_candidate(self, row: str) -> str | None:
        stripped = row.strip()
        if not stripped or len(stripped) > 35:
            return None
        if stripped.startswith(("*", "※", "□", "ㅇ", "◇", "▪", "<")):
            return None
        if not re.search(r"[가-힣A-Za-z0-9]", stripped):
            return None
        if re.search(r"(한다|있다|된다|따른다|수 있다)\.?", stripped):
            return None
        return stripped

    def _vertical_value_candidate(self, row: str) -> str | None:
        stripped = row.strip()
        if not stripped or len(stripped) > 35:
            return None
        if stripped.startswith(("*", "※", "□", "ㅇ", "◇", "▪", "<")):
            return None
        if not re.search(r"\d", stripped):
            return None
        return stripped

    def _named_area_name_candidate(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped or len(stripped) > 40:
            return False
        if self._looks_like_table_caption_or_note_row(stripped):
            return False
        if not re.search(r"[가-힣]", stripped):
            return False
        return bool(re.search(r"(본부|센터|처|실|팀|지사|사무소|원)$", stripped))

    def _named_area_region_candidate(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped or len(stripped) > 120:
            return False
        if not re.search(r"[가-힣]", stripped):
            return False
        return "," in stripped or "ㆍ" in stripped or bool(re.search(r"\s", stripped))

    def _named_area_note_candidate(self, row: str) -> bool:
        stripped = row.strip()
        if not stripped or len(stripped) > 80:
            return False
        if not re.search(r"\d", stripped):
            return False
        return bool(re.search(r"(공원|사무소|탐방원|센터|지사|본부|개)", stripped))

    def _looks_like_dense_header_row(self, row: str) -> bool:
        if re.search(r"(한다|있다|된다|따른다)\.?", row):
            return False
        normalized = self._normalize_label_spacing(row)
        return bool(re.search(r"(□|［\s*］|점검|평가|시간|직급|성명|비고|결과|장소|번호)", normalized))
