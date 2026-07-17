from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FailureClassification:
    failure_category: str
    ocr_required: bool = False
    ocr_page_count: int | None = None
    retry_recommended: bool = True
    failure_next_action: str = "retry_after_operator_review"

    def as_row_fields(self) -> dict[str, Any]:
        return {
            "failure_category": self.failure_category,
            "ocr_required": self.ocr_required,
            "ocr_page_count": self.ocr_page_count or "",
            "retry_recommended": self.retry_recommended,
            "failure_next_action": self.failure_next_action,
        }


def classify_processing_failure(error: BaseException | str, *, filename: str | None = None) -> FailureClassification:
    page_count = _ocr_page_count(error)
    message = str(error or "")
    lowered = message.lower()
    suffix = _suffix(filename)
    if _is_ocr_required(error, lowered, suffix):
        return FailureClassification(
            failure_category="ocr_required",
            ocr_required=True,
            ocr_page_count=page_count,
            retry_recommended=False,
            failure_next_action="run_ocr_then_reprocess",
        )
    if "unsupported file extension" in lowered:
        return FailureClassification(
            failure_category="unsupported_format",
            retry_recommended=False,
            failure_next_action="convert_to_supported_format",
        )
    if "input path not found" in lowered or "no such file" in lowered or "file not found" in lowered:
        return FailureClassification(
            failure_category="input_missing",
            retry_recommended=False,
            failure_next_action="restore_source_file",
        )
    if "failed to parse" in lowered or "invalid parser input" in lowered:
        return FailureClassification(
            failure_category="parser_error",
            retry_recommended=False,
            failure_next_action="inspect_or_convert_source_file",
        )
    return FailureClassification(failure_category="transient_or_unknown")


def _is_ocr_required(error: BaseException | str, lowered_message: str, suffix: str) -> bool:
    if bool(getattr(error, "ocr_required", False)):
        return True
    if "ocr may be required" in lowered_message:
        return True
    if "no text blocks were extracted" in lowered_message and suffix == ".pdf":
        return True
    return False


def _ocr_page_count(error: BaseException | str) -> int | None:
    value = getattr(error, "page_count", None)
    if isinstance(value, int) and value > 0:
        return value
    match = re.search(r"\b(?:pages?|page_count)\s*[:=]\s*(\d+)\b", str(error or ""), flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _suffix(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()
