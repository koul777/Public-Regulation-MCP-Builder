from __future__ import annotations

import re
from typing import Iterable

from app.processors.article_validity import build_article_validity_windows


class MetadataExtractor:
    ARTICLE_REF = re.compile(
        r"제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?"
    )
    ARTICLE_CLAUSE_REF = re.compile(r"제\s*\d+\s*(?:항|호)")
    APPENDIX_REF = re.compile(r"별\s*표\s*(?:제\s*)?\d*(?:\s*-\s*\d+)?(?:\s*호)?")
    FORM_REF = re.compile(r"별\s*지\s*제?\s*\d*(?:\s*-\s*\d+)?\s*호\s*서식?")
    LAW_REF = re.compile(r"[「｢『“\"]([^」｣』”\"]+)[」｣』”\"]")
    INTERNAL_REGULATION_SUFFIXES = ("규정", "지침", "내규", "정관", "세칙", "요령", "기준", "편람", "규칙", "예규")
    REGULATION_ARTICLE_PREFIX = re.compile(
        r"(?P<name>[가-힣A-Za-z0-9·ㆍ\-\(\)（）\s]{1,80}?"
        r"(?:규정|지침|내규|정관|세칙|요령|기준|편람|규칙|예규))\s*$"
    )
    LAW_ARTICLE_PREFIX = re.compile(
        r"(?P<name>[가-힣A-Za-z0-9·ㆍ\-\(\)（）\s]{1,80}?"
        r"(?:법|법률|시행령|시행규칙|시행규정|영|령))\s*$"
    )
    QUOTED_ARTICLE_PREFIX = re.compile(r"[「｢『“\"]\s*(?P<name>[^」｣』”\"]{1,80})\s*[」｣』”\"]\s*$")
    REGULATION_NUMBER_REF = re.compile(r"\b(\d+-\d+-\d+)\.\s*([^\n「」]{1,80})")
    REVISION_EVENT = re.compile(
        r"[<〈]\s*(신\s*설|개\s*정|삭\s*제|제목\s*개정|전문\s*개정|일부\s*개정|전부\s*개정|타법\s*개정|타규정\s*개정)?\s*([^>〉]*)[>〉]"
    )
    REVISION_HISTORY_LINE = re.compile(
        r"^\s*(제정|개정|일부개정|전부개정|전문개정|타법개정|타규정개정)\s+(.+?)\s*$"
    )
    RULE_NO = re.compile(r"(?:규정|내규|지침|세칙)?\s*(제\s*\d+(?:-\d+)?\s*호)")
    DOT_DATE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?")
    KOREAN_DATE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
    DATE_VALUE = r"(?:\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?|\d{4}년\s*\d{1,2}월\s*\d{1,2}일)"
    EFFECTIVE_OVERRIDE = re.compile(
        rf"(?P<article>제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?)"
        rf"\s*의?\s*개정\s*규정\s*은\s*(?P<date>{DATE_VALUE})\s*부터\s*시행(?:한다)?"
    )
    SUPPLEMENTARY_MARKER = re.compile(r"^\s*부\s*칙\b", re.MULTILINE)
    SUPPLEMENTARY_DATE = re.compile(
        rf"부\s*칙\s*(?:[<〈\(\[]\s*)?(?P<date>{DATE_VALUE})", re.MULTILINE
    )

    def extract(
        self,
        text: str,
        current_article_no: str | None = None,
        *,
        supplementary_context: bool = False,
        current_regulation_no: str | None = None,
        current_regulation_title: str | None = None,
    ) -> dict:
        article_refs: list[str] = []
        regulation_article_refs: list[dict[str, str]] = []
        for match in self.ARTICLE_REF.finditer(text):
            if self._is_self_article_ref(match, current_article_no):
                continue
            article_ref = self._normalize_article_ref(match.group(0))
            regulation_prefix = self._other_regulation_prefix_for_article_ref(
                text,
                match,
                current_regulation_no=current_regulation_no,
                current_regulation_title=current_regulation_title,
            )
            if not regulation_prefix:
                regulation_prefix = self._inherited_regulation_prefix_for_article_ref(
                    text,
                    match,
                    current_regulation_no=current_regulation_no,
                    current_regulation_title=current_regulation_title,
                )
            if regulation_prefix:
                regulation_article_refs.append({"regulation_ref": regulation_prefix, "article_ref": article_ref})
                continue
            if self._external_law_prefix_for_article_ref(text, match) or self._inherited_external_law_prefix_for_article_ref(
                text,
                match,
            ):
                continue
            article_refs.append(article_ref)
        article_refs = self._unique(article_refs)
        regulation_article_refs = self._unique_regulation_article_refs(regulation_article_refs)
        appendix_refs = self._unique(match.group(0).replace(" ", "") for match in self.APPENDIX_REF.finditer(text))
        form_refs = self._unique(re.sub(r"\s+", "", match.group(0)) for match in self.FORM_REF.finditer(text))
        external_law_refs = self._unique(
            self._clean_reference_name(match.group(1))
            for match in self.LAW_REF.finditer(text)
            if self._is_reference_name_candidate(match.group(1))
        )
        internal_regulation_refs = self._internal_regulation_refs(
            text,
            external_law_refs,
            [item["regulation_ref"] for item in regulation_article_refs],
        )
        external_law_refs = self._external_law_refs_only(external_law_refs, internal_regulation_refs)
        revision_events = self._revision_events(text)
        revision_history = self._revision_history(text)
        revision_history_spans = self._revision_history_spans(text, revision_history)
        effective_date = self._effective_date(text) or self._first_revision_effective_date(revision_history)
        revision_date = self._latest_revision_date(revision_events, revision_history)
        article_effective_overrides = self._article_effective_overrides(text)
        supplementary_metadata = self._supplementary_metadata(
            text,
            article_effective_overrides,
            supplementary_context=supplementary_context,
        )
        article_validity_windows = build_article_validity_windows(
            effective_date=effective_date,
            article_effective_overrides=article_effective_overrides,
            revision_history=revision_history,
        )
        references = [
            *[{"type": "article", "value": value, "scope": "internal"} for value in article_refs],
            *[{"type": "appendix", "value": value, "scope": "internal"} for value in appendix_refs],
            *[{"type": "form", "value": value, "scope": "internal"} for value in form_refs],
            *[{"type": "regulation", "value": value, "scope": "internal"} for value in internal_regulation_refs],
            *[
                {
                    "type": "regulation_article",
                    "value": f"{item['regulation_ref']} {item['article_ref']}",
                    "scope": "internal",
                    "regulation_ref": item["regulation_ref"],
                    "article_ref": item["article_ref"],
                }
                for item in regulation_article_refs
                if item["regulation_ref"] in internal_regulation_refs
            ],
            *[{"type": "law", "value": value, "scope": "external"} for value in external_law_refs],
        ]
        return {
            "references": references,
            "article_refs": article_refs,
            "appendix_refs": appendix_refs,
            "form_refs": form_refs,
            "internal_regulation_refs": internal_regulation_refs,
            "regulation_article_refs": [
                item for item in regulation_article_refs if item["regulation_ref"] in internal_regulation_refs
            ],
            "external_law_refs": external_law_refs,
            "revision_events": revision_events,
            "revision_history": revision_history,
            "revision_history_spans": revision_history_spans,
            "effective_date": effective_date,
            "revision_date": revision_date,
            "valid_from": effective_date,
            "valid_to": None,
            "article_effective_overrides": article_effective_overrides,
            "article_validity_windows": article_validity_windows,
            **supplementary_metadata,
        }

    def _revision_events(self, text: str) -> list[dict[str, str | None]]:
        events: list[dict[str, str | None]] = []
        for match in self.REVISION_EVENT.finditer(text):
            event_type = self._compact(match.group(1) or "")
            event_text = match.group(2).strip()
            if not event_type and not self._first_date(event_text):
                continue
            events.append(
                {
                    "type": event_type or "개정",
                    "date": self._first_date(event_text),
                    "raw": match.group(0),
                }
            )
        return events

    def _revision_history(self, text: str) -> list[dict[str, str | None]]:
        events: list[dict[str, str | None]] = []
        for line in self._candidate_revision_history_lines(text):
            match = self.REVISION_HISTORY_LINE.match(line)
            if not match:
                continue
            tail = match.group(2).strip()
            date = self._first_date(tail)
            if not date:
                continue
            events.append(
                {
                    "event_type": match.group(1),
                    "date": date,
                    "effective_date": self._line_effective_date(tail),
                    "rule_no": self._rule_no(tail),
                    "raw": line,
                }
            )
        return events

    def _candidate_revision_history_lines(self, text: str) -> Iterable[str]:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            yield line

    def _revision_history_spans(
        self,
        text: str,
        revision_history: list[dict[str, str | None]],
    ) -> list[dict[str, int | str | list[dict[str, str | None]]]]:
        if not revision_history:
            return []
        lines = text.splitlines()
        history_by_raw = {str(event.get("raw") or ""): event for event in revision_history}
        spans: list[dict[str, int | str | list[dict[str, str | None]]]] = []
        current_start: int | None = None
        current_events: list[dict[str, str | None]] = []
        for index, raw_line in enumerate(lines):
            line = raw_line.strip()
            event = history_by_raw.get(line)
            if event:
                if current_start is None:
                    current_start = index
                current_events.append(event)
                continue
            if current_start is not None:
                spans.append(self._revision_history_span(lines, current_start, index - 1, current_events))
                current_start = None
                current_events = []
        if current_start is not None:
            spans.append(self._revision_history_span(lines, current_start, len(lines) - 1, current_events))
        return spans

    def _revision_history_span(
        self,
        lines: list[str],
        start_line: int,
        end_line: int,
        events: list[dict[str, str | None]],
    ) -> dict[str, int | str | list[dict[str, str | None]]]:
        raw = "\n".join(lines[start_line : end_line + 1]).strip()
        return {
            "start_line": start_line + 1,
            "end_line": end_line + 1,
            "line_count": end_line - start_line + 1,
            "event_count": len(events),
            "events": events,
            "raw": raw,
        }

    def _internal_regulation_refs(
        self,
        text: str,
        quoted_refs: list[str],
        prefixed_refs: list[str] | None = None,
    ) -> list[str]:
        refs: list[str] = []
        refs.extend(value for value in prefixed_refs or [] if self._is_internal_regulation_name(value))
        for value in quoted_refs:
            if self._is_internal_regulation_name(value):
                refs.append(value)
        for match in self.REGULATION_NUMBER_REF.finditer(text):
            number = self._compact(match.group(1))
            title = self._regulation_title_from_number_ref(match.group(2))
            if title and self._is_internal_regulation_name(title):
                refs.append(f"{number}.{title}")
        return self._unique(refs)

    def _external_law_refs_only(self, quoted_refs: list[str], internal_regulation_refs: list[str]) -> list[str]:
        internal_set = set(internal_regulation_refs)
        return self._unique(
            value
            for value in quoted_refs
            if value not in internal_set and not self._is_internal_regulation_name(value)
        )

    def _is_internal_regulation_name(self, value: str) -> bool:
        cleaned = self._clean_reference_name(value)
        if not self._is_reference_name_candidate(cleaned):
            return False
        compact = self._compact(cleaned)
        return any(compact.endswith(suffix) for suffix in self.INTERNAL_REGULATION_SUFFIXES)

    def _is_law_name(self, value: str) -> bool:
        cleaned = self._clean_reference_name(value)
        if not self._is_reference_name_candidate(cleaned):
            return False
        if self._is_internal_regulation_name(cleaned):
            return False
        compact = self._compact(cleaned)
        return bool(re.search(r"(법|법률|시행령|시행규칙|영|령)$", compact))

    def _clean_reference_name(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip(" .")

    def _is_reference_name_candidate(self, value: str) -> bool:
        cleaned = self._clean_reference_name(value)
        if not cleaned or len(cleaned) > 80:
            return False
        return not bool(re.search(r"(따른다|준용한다|표기|생략|다음과|경우에는|경우에는|경우|수습임용|직무급)", cleaned))

    def _regulation_title_from_number_ref(self, value: str) -> str:
        cleaned = self._clean_reference_name(value)
        suffixes = "|".join(re.escape(suffix) for suffix in self.INTERNAL_REGULATION_SUFFIXES)
        match = re.search(rf"(.{{1,80}}?(?:{suffixes}))", cleaned)
        if match:
            return self._clean_reference_name(match.group(1))
        return cleaned

    def _effective_date(self, text: str) -> str | None:
        patterns = (
            rf"({self.DATE_VALUE})\s*부터\s*시행",
            rf"시행\s*({self.DATE_VALUE})",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return self._normalize_date(match.group(1))
        if "공포한 날부터 시행" in text:
            return "promulgation_date"
        return None

    def _line_effective_date(self, text: str) -> str | None:
        for pattern in (
            rf"(?:시행|시행일)\s*[:：]?\s*({self.DATE_VALUE})",
            rf"\(\s*시행\s*({self.DATE_VALUE})\s*\)",
        ):
            match = re.search(pattern, text)
            if match:
                return self._normalize_date(match.group(1))
        return None

    def _article_effective_overrides(self, text: str) -> list[dict[str, str]]:
        overrides: list[dict[str, str]] = []
        for match in self.EFFECTIVE_OVERRIDE.finditer(text):
            effective_date = self._normalize_date(match.group("date"))
            if not effective_date:
                continue
            overrides.append(
                {
                    "article_ref": self._normalize_article_ref(match.group("article")),
                    "effective_date": effective_date,
                    "raw": match.group(0).strip().rstrip(".。"),
                }
            )
        return overrides

    def _supplementary_metadata(
        self,
        text: str,
        article_effective_overrides: list[dict[str, str]],
        *,
        supplementary_context: bool = False,
    ) -> dict[str, str | bool | None]:
        is_supplementary = supplementary_context or bool(self.SUPPLEMENTARY_MARKER.search(text))
        identifier_date = None
        if is_supplementary:
            match = self.SUPPLEMENTARY_DATE.search(text)
            if match:
                identifier_date = self._normalize_date(match.group("date"))
        boilerplate = self._is_supplementary_boilerplate(text, article_effective_overrides) if is_supplementary else False
        return {
            "is_supplementary_provision": is_supplementary,
            "supplementary_label": "부칙" if is_supplementary else None,
            "supplementary_identifier_date": identifier_date,
            "supplementary_boilerplate": boilerplate,
        }

    def _is_supplementary_boilerplate(self, text: str, article_effective_overrides: list[dict[str, str]]) -> bool:
        if article_effective_overrides:
            return False
        body = self._supplementary_body_for_boilerplate(text)
        if not body:
            return False
        pattern = re.compile(
            rf"^(?:제\s*\d+\s*조\s*\(\s*시행일\s*\)\s*)?"
            rf"(?:[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]\s*)?"
            rf"(?:[\(（]\s*시행일\s*[\)）]\s*)?"
            rf"이\s*(?:규정|정관|지침|세칙|내규|요령|기준)?\s*은\s*"
            rf"(?:공포한\s*날|{self.DATE_VALUE})\s*부터\s*시행한다\.?$"
        )
        return bool(pattern.match(body))

    def _supplementary_body_for_boilerplate(self, text: str) -> str:
        logical_lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if self.SUPPLEMENTARY_MARKER.match(line):
                remainder = self.SUPPLEMENTARY_MARKER.sub("", line, count=1).strip()
                remainder = re.sub(r"^[<〈\(\[]\s*" + self.DATE_VALUE + r"\s*[>〉\)\]]?", "", remainder).strip()
                if remainder:
                    logical_lines.append(remainder)
                continue
            logical_lines.append(line)
        body = " ".join(logical_lines).strip()
        body = re.sub(r"\s+", " ", body)
        return body.rstrip("。")

    def _latest_revision_date(
        self,
        revision_events: list[dict[str, str | None]],
        revision_history: list[dict[str, str | None]],
    ) -> str | None:
        dated_events = [event["date"] for event in revision_events if event.get("date")]
        if dated_events:
            return dated_events[-1]
        dated_history = [event["date"] for event in revision_history if event.get("date")]
        return dated_history[-1] if dated_history else None

    def _first_revision_effective_date(self, revision_history: list[dict[str, str | None]]) -> str | None:
        for event in reversed(revision_history):
            if event.get("effective_date"):
                return event["effective_date"]
        return None

    def _rule_no(self, text: str) -> str | None:
        match = self.RULE_NO.search(text)
        if not match:
            return None
        return self._compact(match.group(1))

    def _first_date(self, text: str) -> str | None:
        dot = self.DOT_DATE.search(text)
        if dot:
            return self._format_date(dot.groups())
        korean = self.KOREAN_DATE.search(text)
        if korean:
            return self._format_date(korean.groups())
        return None

    def _normalize_date(self, value: str) -> str | None:
        dot = self.DOT_DATE.search(value)
        if dot:
            return self._format_date(dot.groups())
        korean = self.KOREAN_DATE.search(value)
        if korean:
            return self._format_date(korean.groups())
        return None

    def _format_date(self, parts: tuple[str, str, str]) -> str:
        year, month, day = parts
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    def _normalize_article_ref(self, value: str) -> str:
        return self._compact(value)

    def _compact(self, value: str) -> str:
        return re.sub(r"\s+", "", value)

    def _is_self_article_ref(self, match: re.Match[str], current_article_no: str | None) -> bool:
        if not current_article_no:
            return False
        current = self._normalize_article_ref(current_article_no)
        matched = self._normalize_article_ref(match.group(0))
        return current == matched and match.start() <= 10

    def _other_regulation_prefix_for_article_ref(
        self,
        text: str,
        match: re.Match[str],
        *,
        current_regulation_no: str | None,
        current_regulation_title: str | None,
    ) -> str | None:
        prefix = self._regulation_prefix_before(text, match.start())
        if not prefix or self._is_current_regulation_prefix(prefix, current_regulation_no, current_regulation_title):
            return None
        if self._is_internal_regulation_name(prefix) or prefix in self.INTERNAL_REGULATION_SUFFIXES:
            return prefix
        return None

    def _external_law_prefix_for_article_ref(self, text: str, match: re.Match[str]) -> str | None:
        prefix = self._law_prefix_before(text, match.start())
        if not prefix:
            return None
        if self._is_internal_regulation_name(prefix):
            return None
        return prefix

    def _inherited_regulation_prefix_for_article_ref(
        self,
        text: str,
        match: re.Match[str],
        *,
        current_regulation_no: str | None,
        current_regulation_title: str | None,
    ) -> str | None:
        prefix = self._inherited_prefix_for_article_ref(
            text,
            match,
            prefix_resolver=lambda previous_start: self._regulation_prefix_before(text, previous_start),
        )
        if not prefix or self._is_current_regulation_prefix(prefix, current_regulation_no, current_regulation_title):
            return None
        if self._is_internal_regulation_name(prefix) or prefix in self.INTERNAL_REGULATION_SUFFIXES:
            return prefix
        return None

    def _inherited_external_law_prefix_for_article_ref(self, text: str, match: re.Match[str]) -> str | None:
        prefix = self._inherited_prefix_for_article_ref(
            text,
            match,
            prefix_resolver=lambda previous_start: self._law_prefix_before(text, previous_start),
        )
        if not prefix or self._is_internal_regulation_name(prefix):
            return None
        return prefix

    def _inherited_prefix_for_article_ref(
        self,
        text: str,
        match: re.Match[str],
        *,
        prefix_resolver,
    ) -> str | None:
        window_start = max(0, match.start() - 160)
        window = text[window_start : match.start()]
        previous_article_refs = list(self.ARTICLE_REF.finditer(window))
        if not previous_article_refs:
            return None
        for previous in reversed(previous_article_refs):
            prefix = prefix_resolver(window_start + previous.start())
            if not prefix:
                continue
            between = window[previous.end() :]
            if "\n" in between or len(between) > 160:
                continue
            bridge = self.ARTICLE_REF.sub("", between)
            bridge = self.ARTICLE_CLAUSE_REF.sub("", bridge)
            if self._is_shared_article_prefix_bridge(bridge):
                return prefix
        return None

    def _is_shared_article_prefix_bridge(self, value: str) -> bool:
        bridge = re.sub(r"[\(（][^\)）]{0,40}[\)）]", "", value or "")
        bridge = re.sub(r"\s+", "", bridge)
        if not bridge:
            return False
        return bool(re.fullmatch(r"(?:,|，|、|ㆍ|·|및|또는|와|과|부터|까지|내지|및/또는)+", bridge))

    def _regulation_prefix_before(self, text: str, position: int) -> str | None:
        window = text[max(0, position - 120) : position]
        previous_article_refs = list(self.ARTICLE_REF.finditer(window))
        if previous_article_refs:
            window = window[previous_article_refs[-1].end() :]
        quoted = self.QUOTED_ARTICLE_PREFIX.search(window)
        if quoted:
            return self._clean_reference_name(quoted.group("name"))
        unquoted = self.REGULATION_ARTICLE_PREFIX.search(window)
        if unquoted:
            return self._clean_regulation_prefix_candidate(unquoted.group("name"))
        return None

    def _law_prefix_before(self, text: str, position: int) -> str | None:
        window = text[max(0, position - 120) : position]
        previous_article_refs = list(self.ARTICLE_REF.finditer(window))
        if previous_article_refs:
            window = window[previous_article_refs[-1].end() :]
        quoted = self.QUOTED_ARTICLE_PREFIX.search(window)
        if quoted:
            candidate = self._clean_reference_name(quoted.group("name"))
            if self._is_law_name(candidate):
                return candidate
        unquoted = self.LAW_ARTICLE_PREFIX.search(window)
        if unquoted:
            candidate = self._clean_regulation_prefix_candidate(unquoted.group("name"))
            if self._is_law_name(candidate):
                return candidate
        return None

    def _clean_regulation_prefix_candidate(self, value: str) -> str:
        cleaned = self._clean_reference_name(value)
        cleaned = re.sub(r"^(?:[\(（][^\)）]{1,40}[\)）]\s*)+", "", cleaned)
        cleaned = re.sub(r"^(?:및|또는|과|와|및/또는)\s+", "", cleaned)
        last_open = max(cleaned.rfind("("), cleaned.rfind("（"))
        last_close = max(cleaned.rfind(")"), cleaned.rfind("）"))
        if last_open > last_close:
            cleaned = cleaned[last_open + 1 :].strip()
        tokens = cleaned.split()
        cut_index = -1
        for index, token in enumerate(tokens[:-1]):
            normalized = token.strip(" ,.;:·ㆍ()（）[]{}")
            if self._is_regulation_context_token(normalized):
                cut_index = index
        if cut_index >= 0:
            cleaned = " ".join(tokens[cut_index + 1 :])
        return cleaned

    def _is_regulation_context_token(self, value: str) -> bool:
        if not value:
            return False
        if value in {"또는", "및/또는"}:
            return True
        return bool(
            re.search(
                r"(?:은|는|이|가|을|를|에|에서|에게|으로|로|에는|부터|까지|"
                r"따라|따른|의한|의하여|경우|경우에는|경우에|때에는|때|"
                r"위해|위하여|대한|관한|거쳐|취소하거나|말한다|말한다\)에)$",
                value,
            )
        )

    def _is_current_regulation_prefix(
        self,
        prefix: str,
        current_regulation_no: str | None,
        current_regulation_title: str | None,
    ) -> bool:
        prefix_key = self._compact(prefix)
        if not prefix_key:
            return False
        current_keys = {self._compact(value) for value in (current_regulation_no, current_regulation_title) if value}
        if prefix_key in current_keys:
            return True
        current_suffix = self._current_regulation_suffix(current_regulation_title or current_regulation_no or "")
        if not current_suffix:
            return False
        self_aliases = {
            current_suffix,
            f"이{current_suffix}",
            f"본{current_suffix}",
            f"동{current_suffix}",
            f"해당{current_suffix}",
            f"현{current_suffix}",
        }
        return prefix_key in self_aliases

    def _current_regulation_suffix(self, value: str) -> str:
        compact = self._compact(value)
        for suffix in sorted(self.INTERNAL_REGULATION_SUFFIXES, key=len, reverse=True):
            if compact.endswith(suffix):
                return suffix
        return ""

    def _unique_regulation_article_refs(self, values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[tuple[str, str]] = set()
        result: list[dict[str, str]] = []
        for value in values:
            regulation_ref = self._clean_reference_name(value.get("regulation_ref", ""))
            article_ref = self._normalize_article_ref(value.get("article_ref", ""))
            key = (regulation_ref, article_ref)
            if not regulation_ref or not article_ref or key in seen:
                continue
            seen.add(key)
            result.append({"regulation_ref": regulation_ref, "article_ref": article_ref})
        return result

    def _unique(self, values) -> list:
        seen = set()
        result = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result
