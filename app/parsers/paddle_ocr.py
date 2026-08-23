from __future__ import annotations

"""Local Korean PP-OCRv5 adapter with a stable, path-free result contract."""

import importlib.util
import json
import os
from pathlib import Path
from statistics import mean
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


KOREAN_PPOCRV5_MODEL = "korean_PP-OCRv5_mobile_rec"
PADDLE_OCR_BACKEND = "paddleocr"
SUPPORTED_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


class OcrLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: tuple[float, float, float, float] | None = None


class OcrPageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_no: int = Field(ge=1)
    text: str
    lines: tuple[OcrLine, ...] = ()
    mean_confidence: float = Field(ge=0.0, le=1.0)
    dropped_low_confidence_lines: int = Field(ge=0)
    backend: str = PADDLE_OCR_BACKEND
    model_id: str = KOREAN_PPOCRV5_MODEL


def paddle_ocr_available() -> bool:
    return importlib.util.find_spec("paddleocr") is not None


class PaddleKoreanOcrAdapter:
    """Run PP-OCRv5 only on explicitly supplied local page images.

    Model output is normalized here so parser code does not depend on a
    particular PaddleOCR result-object version.  Source filesystem paths are
    deliberately excluded from the public result model.
    """

    def __init__(
        self,
        *,
        model_name: str = KOREAN_PPOCRV5_MODEL,
        min_confidence: float = 0.35,
        cache_dir: Path | None = None,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self.model_name = str(model_name or "").strip()
        if self.model_name != KOREAN_PPOCRV5_MODEL:
            raise ValueError(f"Unsupported Korean OCR model: {self.model_name}")
        self.min_confidence = float(min_confidence)
        configured_cache = str(os.environ.get("REG_RAG_PADDLEX_CACHE_DIR") or "").strip()
        self.cache_dir = Path(cache_dir or configured_cache or "data/local_ai_runtime/paddlex")
        self._engine: Any | None = None

    def recognize_pages(
        self,
        image_paths: list[Path],
        *,
        page_numbers: list[int] | None = None,
    ) -> list[OcrPageResult]:
        paths = [Path(path) for path in image_paths]
        numbers = list(page_numbers or range(1, len(paths) + 1))
        if len(paths) != len(numbers):
            raise ValueError("image_paths and page_numbers must have the same length")
        if len(set(numbers)) != len(numbers) or any(number < 1 for number in numbers):
            raise ValueError("page_numbers must be unique positive integers")
        for path in paths:
            if not path.is_file():
                raise ValueError("OCR input image does not exist")
            if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                raise ValueError(f"Unsupported OCR image extension: {path.suffix.lower()}")

        engine = self._load_engine()
        results: list[OcrPageResult] = []
        for page_no, path in zip(numbers, paths, strict=True):
            prediction = self._predict(engine, path)
            results.append(
                normalize_paddle_prediction(
                    prediction,
                    page_no=page_no,
                    model_id=self.model_name,
                    min_confidence=self.min_confidence,
                )
            )
        return results

    def _load_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if not paddle_ocr_available():
            raise RuntimeError("PaddleOCR is not installed in the local Python environment")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        resolved_cache = self.cache_dir.resolve()
        os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(resolved_cache))
        os.environ.setdefault("PADDLE_HOME", str(resolved_cache / "paddle"))
        os.environ.setdefault("XDG_CACHE_HOME", str(resolved_cache / "xdg"))
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        from paddleocr import PaddleOCR

        self._engine = PaddleOCR(
            lang="korean",
            ocr_version="PP-OCRv5",
            text_recognition_model_name=self.model_name,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
        )
        return self._engine

    @staticmethod
    def _predict(engine: Any, path: Path) -> Any:
        predict = getattr(engine, "predict", None)
        if callable(predict):
            try:
                return predict(input=str(path))
            except TypeError:
                return predict(str(path))
        legacy_ocr = getattr(engine, "ocr", None)
        if callable(legacy_ocr):
            return legacy_ocr(str(path), cls=False)
        raise RuntimeError("Installed PaddleOCR runtime exposes neither predict() nor ocr()")


def normalize_paddle_prediction(
    prediction: Any,
    *,
    page_no: int,
    model_id: str = KOREAN_PPOCRV5_MODEL,
    min_confidence: float = 0.35,
) -> OcrPageResult:
    candidates = _prediction_candidates(prediction)
    kept: list[OcrLine] = []
    dropped = 0
    for text, confidence, bbox in candidates:
        normalized_text = " ".join(str(text or "").split()).strip()
        normalized_confidence = max(0.0, min(float(confidence or 0.0), 1.0))
        if not normalized_text:
            continue
        if normalized_confidence < min_confidence:
            dropped += 1
            continue
        kept.append(
            OcrLine(
                text=normalized_text,
                confidence=round(normalized_confidence, 6),
                bbox=bbox,
            )
        )
    kept.sort(key=_line_sort_key)
    confidence = mean(line.confidence for line in kept) if kept else 0.0
    return OcrPageResult(
        page_no=page_no,
        text="\n".join(line.text for line in kept),
        lines=tuple(kept),
        mean_confidence=round(confidence, 6),
        dropped_low_confidence_lines=dropped,
        model_id=model_id,
    )


def _prediction_candidates(prediction: Any) -> list[tuple[str, float, tuple[float, float, float, float] | None]]:
    objects = prediction if isinstance(prediction, list) else [prediction]
    candidates: list[tuple[str, float, tuple[float, float, float, float] | None]] = []
    for item in objects:
        payload = _as_mapping(item)
        if payload:
            result = payload.get("res") if isinstance(payload.get("res"), dict) else payload
            texts = _mapping_sequence(result, "rec_texts")
            scores = _mapping_sequence(result, "rec_scores")
            boxes = _mapping_sequence(result, "rec_boxes", "rec_polys")
            if texts:
                for index, text in enumerate(texts):
                    score = scores[index] if index < len(scores) else 0.0
                    box = boxes[index] if index < len(boxes) else None
                    candidates.append((str(text), float(score or 0.0), _coerce_bbox(box)))
                continue
        candidates.extend(_legacy_candidates(item))
    return candidates


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raw = getattr(value, "json", None)
    if callable(raw):
        raw = raw()
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _legacy_candidates(value: Any) -> list[tuple[str, float, tuple[float, float, float, float] | None]]:
    candidates: list[tuple[str, float, tuple[float, float, float, float] | None]] = []

    def visit(node: Any) -> None:
        if not isinstance(node, (list, tuple)):
            return
        if len(node) >= 2:
            text_score = node[1]
            if (
                isinstance(text_score, (list, tuple))
                and len(text_score) >= 2
                and isinstance(text_score[0], str)
                and isinstance(text_score[1], (int, float))
            ):
                candidates.append(
                    (str(text_score[0]), float(text_score[1] or 0.0), _coerce_bbox(node[0]))
                )
                return
        for child in node:
            visit(child)

    visit(value)
    return candidates


def _coerce_bbox(value: Any) -> tuple[float, float, float, float] | None:
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) >= 4 and all(isinstance(item, (int, float)) for item in value[:4]):
        x0, y0, x1, y1 = (float(item) for item in value[:4])
        return (x0, y0, x1, y1)
    points = [point for point in value if isinstance(point, (list, tuple)) and len(point) >= 2]
    if not points:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _mapping_sequence(mapping: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        if isinstance(value, (str, bytes, dict)):
            return []
        try:
            return list(value)
        except TypeError:
            return []
    return []


def _line_sort_key(line: OcrLine) -> tuple[float, float]:
    if line.bbox is None:
        return (float("inf"), float("inf"))
    return (line.bbox[1], line.bbox[0])
