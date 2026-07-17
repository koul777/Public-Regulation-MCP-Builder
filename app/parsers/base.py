from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from app.schemas.parsed import ParsedDocument


PARSER_UNCERTAINTY_SCHEMA_VERSION = "reg-rag-parser-uncertainty-v1"
PARSER_UNCERTAINTY_METADATA_FIELDS = (
    "parser_uncertainty",
    "parser_uncertainty_schema_version",
    "parser_uncertainty_source",
    "parser_uncertainty_risk_level",
    "parser_uncertainty_confidence",
    "parser_uncertainty_flags",
    "parser_uncertainty_recommendation",
    "parser_uncertainty_remediation_hint",
)


def parser_uncertainty_report(
    *,
    source: str,
    risk_level: str,
    flags: list[str] | tuple[str, ...] | set[str] | None = None,
    confidence: float = 1.0,
    recommendation: str = "none",
    remediation_hint: str = "",
) -> dict[str, Any]:
    normalized_risk = str(risk_level or "medium").strip().lower()
    if normalized_risk not in {"low", "medium", "high", "critical"}:
        normalized_risk = "medium"
    try:
        normalized_confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        normalized_confidence = 0.0
    normalized_flags = sorted(
        {
            str(flag or "").strip()
            for flag in (flags or [])
            if str(flag or "").strip()
        }
    )
    return {
        "schema_version": PARSER_UNCERTAINTY_SCHEMA_VERSION,
        "source": str(source or "").strip().lower(),
        "risk_level": normalized_risk,
        "confidence": round(normalized_confidence, 3),
        "flags": normalized_flags,
        "recommendation": str(recommendation or "none").strip(),
        "remediation_hint": str(remediation_hint or "").strip(),
    }


def parser_uncertainty_metadata(**kwargs: Any) -> dict[str, Any]:
    report = parser_uncertainty_report(**kwargs)
    return {
        "parser_uncertainty": report,
        "parser_uncertainty_schema_version": report["schema_version"],
        "parser_uncertainty_source": report["source"],
        "parser_uncertainty_risk_level": report["risk_level"],
        "parser_uncertainty_confidence": report["confidence"],
        "parser_uncertainty_flags": report["flags"],
        "parser_uncertainty_recommendation": report["recommendation"],
        "parser_uncertainty_remediation_hint": report["remediation_hint"],
    }


class ParserError(RuntimeError):
    pass


class OCRRequiredError(ParserError):
    def __init__(
        self,
        message: str,
        *,
        page_count: int | None = None,
        file_type: str | None = None,
        uncertainty_report: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.ocr_required = True
        self.page_count = page_count
        self.file_type = file_type
        self.uncertainty_report = uncertainty_report or parser_uncertainty_report(
            source=file_type or "unknown",
            risk_level="high",
            flags=["ocr_required", "no_text_extracted"],
            confidence=0.0,
            recommendation="run_ocr",
            remediation_hint="Run OCR and review extracted text before approval.",
        )


class BaseParser(ABC):
    supported_extensions: set[str] = set()

    @abstractmethod
    def parse(self, path: Path, document_id: str) -> ParsedDocument:
        raise NotImplementedError

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.supported_extensions


def document_name_from_path(path: Path) -> str:
    return path.stem.strip() or path.name
