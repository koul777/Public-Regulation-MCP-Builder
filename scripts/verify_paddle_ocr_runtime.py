from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Allow this operational CLI to run directly from a source checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.parsers.paddle_ocr import KOREAN_PPOCRV5_MODEL, PaddleKoreanOcrAdapter


FIXTURE_LINES = (
    "직원복무규정",
    "제1조(목적) 이 규정은 직원의 복무에 관한 사항을 정함을 목적으로 한다.",
    "제2조(연차휴가) 직원은 승인된 절차에 따라 연차휴가를 신청한다.",
)
REQUIRED_TERMS = ("직원복무규정", "제1조", "목적", "제2조", "연차휴가", "승인")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify Korean PP-OCRv5 against a generated Korean page.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/image_pipeline_6hour/paddle_ocr_runtime_verification.json"),
    )
    parser.add_argument(
        "--font",
        type=Path,
        default=Path("C:/Windows/Fonts/malgun.ttf"),
    )
    return parser


def _normalized(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(text or ""))


def _render_fixture(path: Path, font_path: Path) -> None:
    if not font_path.is_file():
        raise RuntimeError("A local Korean font is required for the OCR verification fixture")
    image = Image.new("RGB", (2200, 900), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(font_path), 76)
    body_font = ImageFont.truetype(str(font_path), 54)
    draw.text((110, 90), FIXTURE_LINES[0], font=title_font, fill="black")
    draw.text((110, 260), FIXTURE_LINES[1], font=body_font, fill="black")
    draw.text((110, 430), FIXTURE_LINES[2], font=body_font, fill="black")
    image.save(path, format="PNG")


def verify(font_path: Path) -> dict[str, object]:
    expected = "\n".join(FIXTURE_LINES)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="reg_rag_ocr_verify_") as temporary_dir:
        fixture_path = Path(temporary_dir) / "korean_regulation_page.png"
        _render_fixture(fixture_path, font_path)
        result = PaddleKoreanOcrAdapter(
            min_confidence=0.25,
            cache_dir=Path("data/local_ai_runtime/paddlex"),
        ).recognize_pages(
            [fixture_path],
            page_numbers=[1],
        )[0]
    normalized_expected = _normalized(expected)
    normalized_actual = _normalized(result.text)
    similarity = SequenceMatcher(None, normalized_expected, normalized_actual).ratio()
    term_results = {
        term: _normalized(term) in normalized_actual
        for term in REQUIRED_TERMS
    }
    passed = (
        result.page_no == 1
        and bool(result.lines)
        and result.mean_confidence >= 0.5
        and similarity >= 0.80
        and all(term_results.values())
    )
    return {
        "schema_version": "reg-rag-paddle-ocr-runtime-verification-v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "model": KOREAN_PPOCRV5_MODEL,
        "language": "ko",
        "passed": passed,
        "page_count": 1,
        "recognized_line_count": len(result.lines),
        "mean_confidence": result.mean_confidence,
        "character_similarity": round(similarity, 6),
        "required_terms": term_results,
        "dropped_low_confidence_lines": result.dropped_low_confidence_lines,
        "recognized_text": result.text,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "runtime_scope": "local_only",
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = verify(args.font)
    except Exception as exc:
        report = {
            "schema_version": "reg-rag-paddle-ocr-runtime-verification-v1",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "model": KOREAN_PPOCRV5_MODEL,
            "passed": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
