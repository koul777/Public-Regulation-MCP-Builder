"""Deterministic regulation metadata detection for local uploads.

The detector intentionally uses only the filename and extracted document text.
It does not call an external model, so batch uploads remain reproducible and do
not require an API key.  Explicit operator/API values still take precedence in
``DocumentService``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
from pathlib import Path
import re
import unicodedata
from typing import Iterable

from app.schemas.document import Document


_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})\s*[.\-/년]\s*"
    r"(?P<month>0?[1-9]|1[0-2])\s*[.\-/월]\s*"
    r"(?P<day>0?[1-9]|[12]\d|3[01])\s*일?(?!\d)"
)
_COMPACT_FILENAME_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<compact>(?:(?:19|20)\d{6}|\d{6}))(?!\d)"
)
_LEADING_CLASSIFICATION_PATTERN = re.compile(r"^\s*\d+(?:\s*-\s*\d+){1,5}\s*[.)]?\s*")
_TRAILING_COPY_NUMBER_PATTERN = re.compile(
    r"((?:\uaddc\uc815|\uaddc\uce59|\uc9c0\uce68|\uc694\ub839|\uc608\uaddc|\uae30\uc900|\uc9c0\uce68\uc11c|\uac15\ub839|\uaddc\uc57d))\s*\d+$"
)
_VERSION_PATTERN = re.compile(r"(?i)(?:^|[\s_\-([])(?:v|ver(?:sion)?)\s*(\d+(?:\.\d+)*)")
_REVISION_SEQUENCE_PATTERN = re.compile(r"제\s*(\d+)\s*차\s*(?:일부\s*|전부\s*)?개정")
_TITLE_END_PATTERN = re.compile(r"(?:규정|규칙|지침|요령|세칙|정관|내규|기준|편람|강령|규약)$")
_GENERIC_TITLES = {
    "document",
    "regulation",
    "scan",
    "untitled",
    "문서",
    "규정",
    "스캔",
    "제목 없음",
}


@dataclass(frozen=True, slots=True)
class RegulationMetadataGuess:
    document_name: str
    regulation_id: str
    regulation_version: str
    revision_date: str
    effective_from: str
    supersedes_document_id: str | None
    title_source: str
    revision_date_source: str
    effective_from_source: str
    version_source: str


def infer_regulation_metadata(
    filename: str,
    *,
    text: str | None = None,
    existing_documents: Iterable[Document] = (),
    profile_id: str | None = None,
    tenant_id: str | None = None,
    today: date | None = None,
) -> RegulationMetadataGuess:
    """Infer a stable regulation family, version, dates, and predecessor.

    Filename signals are available before the upload is stored.  Extracted text
    is used later by ``ProcessingService`` to improve generic filenames and to
    prefer explicit enactment/revision dates found inside the regulation.
    """

    reference_date = today or date.today()
    filename_title = regulation_title_from_filename(filename)
    content_title = regulation_title_from_text(text or "")
    if content_title:
        title = content_title
        title_source = "content"
    else:
        title = filename_title or content_title or Path(filename).stem.strip() or "regulation"
        title_source = "filename" if filename_title else "content"
    regulation_id = regulation_id_for_title(title)

    scoped_existing = _documents_for_family(
        existing_documents,
        regulation_id=regulation_id,
        regulation_title=title,
        profile_id=profile_id,
        tenant_id=tenant_id,
    )
    predecessor = latest_approved_predecessor(scoped_existing)
    if predecessor is not None and str(predecessor.regulation_id or "").strip():
        regulation_id = str(predecessor.regulation_id).strip()
    filename_dates = _extract_filename_iso_dates(Path(filename).stem)
    text_dates = extract_iso_dates(text or "")
    history_revision_dates, history_effective_dates = _leading_regulation_history_dates(text or "")
    revision_context_dates = history_revision_dates or _context_dates(
        text or "",
        ("\uac1c\uc815", "\uc77c\ubd80\uac1c\uc815", "\uc804\ubb38\uac1c\uc815", "\uc804\ubd80\uac1c\uc815", "\ud0c0\uaddc\uc815\uac1c\uc815"),
    )
    effective_context_dates = history_effective_dates or _context_dates(
        text or "",
        ("\uc2dc\ud589\uc77c", "\uc2dc\ud589", "\ud6a8\ub825", "\uc801\uc6a9\uc77c"),
    )

    if revision_context_dates:
        revision_date = max(revision_context_dates)
        revision_source = "content"
    elif text_dates:
        revision_date = max(text_dates)
        revision_source = "content"
    elif filename_dates:
        revision_date = filename_dates[-1]
        revision_source = "filename"
    else:
        revision_date = reference_date.isoformat()
        revision_source = "upload_date"

    if effective_context_dates:
        effective_from = max(effective_context_dates)
        effective_source = "content"
    elif revision_context_dates:
        effective_from = max(revision_context_dates)
        effective_source = "content"
    elif text_dates:
        effective_from = max(text_dates)
        effective_source = "content"
    elif filename_dates:
        effective_from = filename_dates[-1]
        effective_source = "filename"
    else:
        effective_from = revision_date
        effective_source = "revision_date"

    explicit_version = _explicit_version(Path(filename).stem, text or "")
    if explicit_version:
        version, version_source = explicit_version
    elif revision_date and revision_source != "upload_date":
        version = f"rev-{revision_date.replace('-', '')}"
        version_source = revision_source
    else:
        version = _next_sequence_version(scoped_existing)
        version_source = "sequence"

    return RegulationMetadataGuess(
        document_name=title,
        regulation_id=regulation_id,
        regulation_version=version,
        revision_date=revision_date,
        effective_from=effective_from,
        supersedes_document_id=predecessor.document_id if predecessor is not None else None,
        title_source=title_source,
        revision_date_source=revision_source,
        effective_from_source=effective_source,
        version_source=version_source,
    )


def regulation_title_from_filename(filename: str) -> str:
    stem = unicodedata.normalize("NFKC", Path(filename).stem).strip()
    if not stem:
        return "regulation"
    value = _LEADING_CLASSIFICATION_PATTERN.sub("", stem)
    value = _DATE_PATTERN.sub(" ", value)
    value = re.sub(r"(?<!\d)(?:(?:19|20)\d{6}|\d{6})(?!\d)", " ", value)
    value = _VERSION_PATTERN.sub(" ", value)
    value = _REVISION_SEQUENCE_PATTERN.sub(" ", value)
    value = re.sub(r"(?:\uc77c\ubd80|\uc804\ubd80)?\s*\uac1c\uc815(?:\ubcf8|\ud310)?", " ", value)
    value = re.sub(r"(?i)\b(?:final|latest|revised|revision|copy)\b", " ", value)
    value = re.sub(
        r"(?:\ud1b5\ud569\ubcf8|\ucd5c\uc2e0\ubcf8|\uac1c\uc815\ubcf8|\uac1c\uc815\ud310|\ucd5c\uc885\ubcf8)",
        " ",
        value,
    )
    value = re.sub(r"(?:일부\s*|전부\s*)?개정(?:본|안)?", " ", value)
    value = re.sub(r"(?:최종|최신)(?:본|안)?", " ", value)
    value = re.sub(r"\(\s*\d+\s*\)$", " ", value)
    value = re.sub(r"[\[\](){}]", " ", value)
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    value = _TRAILING_COPY_NUMBER_PATTERN.sub(r"\1", value)
    value = re.sub(r"(?:\uc77c\ubd80|\uc804\ubd80)$", "", value).strip()
    return value or stem


def regulation_title_from_text(text: str) -> str | None:
    """Return the first plausible regulation title near the document head."""

    for raw_line in str(text or "").splitlines()[:80]:
        line = unicodedata.normalize("NFKC", raw_line).strip()
        line = re.sub(r"\s+", " ", line).strip(" -·:[]()")
        if not 2 <= len(line) <= 100:
            continue
        if line.startswith(("제정", "개정", "시행", "부칙", "제1조")):
            continue
        if _TITLE_END_PATTERN.search(line):
            return line
    return None


def regulation_id_for_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(title or "")).casefold().strip()
    normalized = re.sub(r"[^0-9a-z가-힣]+", "-", normalized).strip("-")
    if not normalized:
        normalized = hashlib.sha256(str(title or "regulation").encode("utf-8")).hexdigest()[:16]
    if len(normalized) > 64:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
        normalized = f"{normalized[:53].rstrip('-')}-{digest}"
    return f"reg-{normalized}"


def regulation_family_key(value: str | None) -> str:
    """Return a date/edition-insensitive key used to connect revisions."""
    title = regulation_title_from_filename(str(value or "regulation"))
    normalized = unicodedata.normalize("NFKC", title).casefold().strip()
    return re.sub(r"[^0-9a-z\uac00-\ud7a3]+", "", normalized)


def is_generic_regulation_title(value: str | None) -> bool:
    normalized = re.sub(r"[\s_\-\d]+", " ", str(value or "").casefold()).strip()
    return not normalized or normalized in _GENERIC_TITLES or normalized.startswith(("scan ", "스캔 "))


def extract_iso_dates(value: str) -> list[str]:
    dates: list[str] = []
    for match in _DATE_PATTERN.finditer(str(value or "")):
        try:
            parsed = date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
        iso = parsed.isoformat()
        if iso not in dates:
            dates.append(iso)
    return dates


def _extract_filename_iso_dates(value: str) -> list[str]:
    dated_positions: list[tuple[int, str]] = []
    for match in _DATE_PATTERN.finditer(str(value or "")):
        parsed_values = extract_iso_dates(match.group(0))
        if parsed_values:
            dated_positions.append((match.start(), parsed_values[0]))
    for match in _COMPACT_FILENAME_DATE_PATTERN.finditer(str(value or "")):
        compact = match.group("compact")
        if len(compact) == 8:
            year, month, day = int(compact[:4]), int(compact[4:6]), int(compact[6:8])
        else:
            short_year = int(compact[:2])
            year = 2000 + short_year if short_year <= 69 else 1900 + short_year
            month, day = int(compact[2:4]), int(compact[4:6])
        try:
            parsed = date(year, month, day).isoformat()
        except ValueError:
            continue
        dated_positions.append((match.start(), parsed))
    dates: list[str] = []
    for _position, parsed in sorted(dated_positions):
        if parsed not in dates:
            dates.append(parsed)
    return dates


def _leading_regulation_history_dates(text: str) -> tuple[list[str], list[str]]:
    """Extract revision and effective dates from the history near the document head."""
    revision_dates: list[str] = []
    effective_dates: list[str] = []
    revision_markers = ("\uc81c\uc815", "\uac1c\uc815")
    effective_markers = ("\uc2dc\ud589", "\ud6a8\ub825", "\uc801\uc6a9\uc77c")
    for raw_line in str(text or "").splitlines()[:300]:
        line = unicodedata.normalize("NFKC", raw_line).strip()
        dates = extract_iso_dates(line)
        if not dates:
            continue
        if any(marker in line for marker in revision_markers):
            revision_date = _closest_date_after_marker(line, revision_markers) or dates[0]
            if revision_date not in revision_dates:
                revision_dates.append(revision_date)
        if any(marker in line for marker in effective_markers):
            effective_date = _closest_date_after_marker(line, effective_markers) or dates[-1]
            if effective_date not in effective_dates:
                effective_dates.append(effective_date)
    return revision_dates, effective_dates


def _closest_date_after_marker(line: str, markers: tuple[str, ...]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for marker in markers:
        for marker_match in re.finditer(re.escape(marker), line):
            for date_match in _DATE_PATTERN.finditer(line, marker_match.end()):
                parsed = extract_iso_dates(date_match.group(0))
                if parsed:
                    candidates.append((date_match.start() - marker_match.end(), parsed[0]))
                    break
    return min(candidates, default=(0, None), key=lambda item: item[0])[1]


def regulation_upload_sort_key(filename: str) -> tuple[str, str, tuple[int, ...], str]:
    guess = infer_regulation_metadata(filename)
    version_numbers = tuple(int(value) for value in re.findall(r"\d+", guess.regulation_version))
    return (guess.regulation_id, guess.effective_from, version_numbers, Path(filename).name.casefold())


def latest_approved_predecessor(documents: Iterable[Document]) -> Document | None:
    approved = [
        document
        for document in documents
        if str(document.regulation_status or "").strip().casefold() == "approved"
    ]
    if not approved:
        return None
    return max(approved, key=_document_version_key)


def _documents_for_family(
    documents: Iterable[Document],
    *,
    regulation_id: str,
    regulation_title: str,
    profile_id: str | None,
    tenant_id: str | None,
) -> list[Document]:
    normalized_family = regulation_id.casefold()
    normalized_profile = str(profile_id or "").strip().casefold()
    normalized_tenant = str(tenant_id or "").strip().casefold()
    normalized_title = regulation_family_key(regulation_title)
    return [
        document
        for document in documents
        if (
            str(document.regulation_id or "").strip().casefold() == normalized_family
            or (
                normalized_title
                and regulation_family_key(document.document_name or document.filename) == normalized_title
            )
        )
        and (not normalized_profile or str(document.profile_id or "").strip().casefold() == normalized_profile)
        and (not normalized_tenant or str(document.tenant_id or "").strip().casefold() == normalized_tenant)
    ]


def _context_dates(text: str, keywords: tuple[str, ...]) -> list[str]:
    matches: list[tuple[int, str]] = []
    source = str(text or "")
    for keyword in keywords:
        for keyword_match in re.finditer(re.escape(keyword), source):
            window_start = max(0, keyword_match.start() - 90)
            window_end = min(len(source), keyword_match.end() + 90)
            candidates: list[tuple[int, str]] = []
            for date_match in _DATE_PATTERN.finditer(source[window_start:window_end]):
                absolute_start = window_start + date_match.start()
                absolute_end = window_start + date_match.end()
                if absolute_end <= keyword_match.start():
                    distance = keyword_match.start() - absolute_end
                elif absolute_start >= keyword_match.end():
                    distance = absolute_start - keyword_match.end()
                else:
                    distance = 0
                parsed = extract_iso_dates(date_match.group(0))
                if parsed:
                    candidates.append((distance, parsed[0]))
            if candidates:
                _distance, closest = min(candidates, key=lambda item: item[0])
                matches.append((keyword_match.start(), closest))
    return [value for _offset, value in sorted(matches)]


def _explicit_version(filename_stem: str, text: str) -> tuple[str, str] | None:
    match = _REVISION_SEQUENCE_PATTERN.search(text[:5000])
    if match:
        return f"rev-{int(match.group(1))}", "content"
    match = _VERSION_PATTERN.search(text[:5000])
    if match:
        return f"v{match.group(1)}", "content"
    match = _VERSION_PATTERN.search(filename_stem)
    if match:
        return f"v{match.group(1)}", "filename"
    match = _REVISION_SEQUENCE_PATTERN.search(filename_stem)
    if match:
        return f"rev-{int(match.group(1))}", "filename"
    return None


def _next_sequence_version(documents: Iterable[Document]) -> str:
    highest = 0
    for document in documents:
        values = re.findall(r"\d+", str(document.regulation_version or ""))
        if values:
            highest = max(highest, int(values[0]))
    return f"v{highest + 1}"


def _document_version_key(document: Document) -> tuple[str, tuple[int, ...], object]:
    version_numbers = tuple(int(value) for value in re.findall(r"\d+", str(document.regulation_version or "")))
    return (str(document.effective_from or document.revision_date or ""), version_numbers, document.created_at)
