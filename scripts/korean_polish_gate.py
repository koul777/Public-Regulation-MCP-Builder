from __future__ import annotations

"""Korean polish (윤문) gate for generated user-facing text.

Rules written only in prose get skipped, so the phrases that are almost always
AI boilerplate in Korean guides are checked here in code. Only prose lines that
contain Hangul are checked; fenced code blocks and inline code are skipped so
commands, paths and identifiers are never flagged.

Findings carry the file name, line number, rule code and the matched phrase
only. They never copy the surrounding sentence, so a report stays free of
institution text.
"""

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence


SCHEMA_VERSION = "korean-polish-gate-v1"
OPT_OUT_MARKER = "slop-gate: off"
DOC_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst"})
_HANGUL_RE = re.compile(r"[가-힣]")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")

# The "확실한 슬롭" list kept in the team replacement table, plus the symbols
# the team always replaces (줄표, 문장 속 화살표, 말줄임표).
POLISH_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("dash", "줄표", re.compile("—")),
    ("arrow", "문장 속 화살표", re.compile("→")),
    ("ellipsis", "말줄임표", re.compile(r"…|(?<=[가-힣])\.{3}")),
    ("double-passive", "이중 피동", re.compile("되어지")),
    ("chatbot-phrase", "챗봇 말투", re.compile("좋은 질문|살펴보겠습니다|다음과 같습니다")),
    (
        "hype-word",
        "과장 어휘",
        re.compile("획기적|혁신적|패러다임|시너지|인사이트|시사하는 바|주목할 만|본질적으로"),
    ),
    ("slogan-ending", "결산·슬로건", re.compile("결론적으로|할 때입니다|지금이야말로")),
    ("metaphor-verb", "비유 동사", re.compile("꽂|박아|박힌|녹여|겨냥|걷어")),
)


def prose_lines(text: str) -> list[tuple[int, str]]:
    """Return (line number, prose) pairs outside code fences and inline code."""
    lines: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(str(text or "").splitlines(), start=1):
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            lines.append((number, _INLINE_CODE_RE.sub("", line)))
    return lines


def check_text(text: str, *, source: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for number, line in prose_lines(text):
        if not _HANGUL_RE.search(line):
            continue
        for code, label, pattern in POLISH_RULES:
            matches = sorted(set(pattern.findall(line)))
            if matches:
                findings.append(
                    {
                        "source": source,
                        "line": number,
                        "rule": code,
                        "label": label,
                        "matches": matches,
                    }
                )
    return findings


def check_files(
    paths: Iterable[str | Path],
    *,
    honor_opt_out: bool = True,
) -> dict[str, object]:
    """Check document files and return a report with findings and skipped files.

    Generated bundle guides pass ``honor_opt_out=False`` so a template cannot
    switch the gate off for itself.
    """
    findings: list[dict[str, object]] = []
    checked: list[str] = []
    skipped: list[dict[str, str]] = []
    for raw_path in paths:
        path = Path(raw_path)
        name = path.name
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            skipped.append({"source": name, "reason": type(exc).__name__})
            continue
        if honor_opt_out and OPT_OUT_MARKER in text:
            skipped.append({"source": name, "reason": "opt-out-marker"})
            continue
        checked.append(name)
        findings.extend(check_text(text, source=name))
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_sources": checked,
        "skipped_sources": skipped,
        "issue_count": len(findings),
        "findings": findings,
        "passed": not findings and not any(item["reason"] != "opt-out-marker" for item in skipped),
    }


def _format_findings(findings: Sequence[dict[str, object]]) -> list[str]:
    return [
        f"{item['source']}:{item['line']} {item['label']} ({', '.join(str(m) for m in item['matches'])})"
        for item in findings
    ]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Korean guide files for phrases the team always rewrites.",
    )
    parser.add_argument("paths", nargs="+", help="Document files or directories to check.")
    parser.add_argument(
        "--ignore-opt-out",
        action="store_true",
        help=f"Check files even when they contain '{OPT_OUT_MARKER}'.",
    )
    parser.add_argument("--out-json", help="Write the JSON report to this path.")
    return parser.parse_args(argv)


def _expand_paths(values: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value)
        if path.is_dir():
            paths.extend(
                sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in DOC_SUFFIXES)
            )
        else:
            paths.append(path)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = check_files(_expand_paths(args.paths), honor_opt_out=not args.ignore_opt_out)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for line in _format_findings(report["findings"]):  # type: ignore[arg-type]
        print(line)
    for item in report["skipped_sources"]:  # type: ignore[union-attr]
        print(f"{item['source']}: 건너뜀 ({item['reason']})")
    print(f"윤문 게이트: {'통과' if report['passed'] else '실패'} · 확인 {len(report['checked_sources'])}개 · 걸린 곳 {report['issue_count']}곳")  # type: ignore[arg-type]
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
