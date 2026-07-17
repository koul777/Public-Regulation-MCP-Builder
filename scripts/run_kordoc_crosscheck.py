from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.parsers.factory import get_parser
from app.processors.chunker import Chunker
from app.processors.normalizer import TextNormalizer
from app.processors.structure_detector import StructureDetector
from scripts.analyze_regulation_corpus import summarize_pipeline_counts


DEFAULT_KORDOC_COMMAND = os.getenv("KORDOC_COMMAND", "kordoc")
WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".py")
LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?:\b[a-z]:\\[^\s'\"<>|]+|\\\\[^\s'\"<>|]+|/(?:users|home|tmp|var|etc)/[^\s'\"<>|]+)"
)


def split_command(command: str) -> list[str]:
    if not command.strip():
        return []
    parts = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        return _repair_windows_executable_path([part.strip("\"") for part in parts])
    return parts


def redact_local_paths(text: str) -> str:
    return LOCAL_PATH_PATTERN.sub("<local-path-redacted>", str(text or ""))


def run_kordoc_parse(path: Path, command: str = DEFAULT_KORDOC_COMMAND, timeout_seconds: int = 120) -> dict[str, Any]:
    parts = split_command(command)
    command_label = _command_label(parts)
    if not parts:
        return {
            "status": "not_available",
            "command_label": "",
            "error": "empty_kordoc_command",
        }
    resolved = _resolve_command_executable(parts[0])
    if resolved is None:
        return {
            "status": "not_available",
            "command_label": command_label,
            "error": f"executable_not_found:{command_label}",
        }

    run_path = path
    temp_dir: tempfile.TemporaryDirectory | None = None
    normalized_input_path = False
    if _requires_ascii_safe_input_path(path):
        try:
            temp_dir, run_path = _copy_to_ascii_temporary_path(
                path,
                prefix="reg_rag_kordoc_crosscheck_",
                preferred_parent=Path.cwd() / ".tmp" / "kordoc_crosscheck",
            )
            normalized_input_path = True
        except OSError as exc:
            if temp_dir is not None:
                temp_dir.cleanup()
            return {
                "status": "failed",
                "command_label": command_label,
                "error": redact_local_paths(str(exc)),
            }

    cmd = [resolved, *parts[1:], str(run_path), "--format", "json", "--silent"]
    if os.name == "nt" and resolved.lower().endswith(".ps1"):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *cmd]
    elif os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        if temp_dir is not None:
            temp_dir.cleanup()
        return {
            "status": "timeout",
            "command_label": command_label,
            "timeout_seconds": timeout_seconds,
        }
    except OSError as exc:
        if temp_dir is not None:
            temp_dir.cleanup()
        return {
            "status": "failed",
            "command_label": command_label,
            "error": redact_local_paths(str(exc)),
        }
    if temp_dir is not None:
        temp_dir.cleanup()

    if completed.returncode != 0:
        return {
            "status": "failed",
            "command_label": command_label,
            "returncode": completed.returncode,
            "stderr_chars": len(completed.stderr or ""),
            "stdout_chars": len(completed.stdout or ""),
        }

    try:
        payload = load_json_payload(completed.stdout or "")
    except json.JSONDecodeError as exc:
        return {
            "status": "failed",
            "command_label": command_label,
            "error": f"invalid_json:{exc}",
            "stdout_chars": len(completed.stdout or ""),
        }

    metrics = extract_kordoc_metrics(payload)
    metrics.update(
        {
            "status": "parsed",
            "command_label": command_label,
        }
    )
    if normalized_input_path:
        metrics["input_path_normalized_for_kordoc"] = True
    return metrics


def extract_kordoc_metrics(payload: Any) -> dict[str, Any]:
    document = _document_payload(payload)
    blocks = _as_list(document.get("blocks") or document.get("children") or document.get("content"))
    table_stats = _collect_table_stats(blocks)
    warnings = _as_list(document.get("warnings") or document.get("issues") or [])
    markdown = document.get("markdown") or document.get("text") or ""
    outline = _as_list(document.get("outline") or document.get("headings") or [])
    return {
        "file_type": str(document.get("fileType") or document.get("format") or document.get("type") or ""),
        "page_count": _safe_int(document.get("pageCount") or document.get("pages")),
        "markdown_chars": len(str(markdown)),
        "block_count": table_stats["block_count"],
        "table_count": table_stats["table_count"],
        "nested_table_count": table_stats["nested_table_count"],
        "table_cell_count": table_stats["table_cell_count"],
        "merged_cell_count": table_stats["merged_cell_count"],
        "max_table_rows": table_stats["max_table_rows"],
        "max_table_cols": table_stats["max_table_cols"],
        "warning_count": len(warnings),
        "outline_count": len(outline),
    }


def load_json_payload(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped[0] not in "[{":
            continue
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            continue
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            return json.loads(text[index:])
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("No JSON payload found in Kordoc stdout.", text, 0)


def extract_local_metrics(path: Path, data_dir: Path | None = None) -> dict[str, Any]:
    document_id = f"kordoc_crosscheck_{uuid.uuid4().hex[:12]}"
    settings = Settings(data_dir=data_dir or Path("./data"))
    try:
        parser = get_parser(path, settings=settings)
        parsed = parser.parse(path, document_id=document_id)
        normalized = TextNormalizer().normalize_document(parsed)
        nodes = StructureDetector().detect(normalized)
        chunks = Chunker().build_chunks(nodes, normalized)
    except Exception as exc:
        return {
            "status": "failed",
            "error": redact_local_paths(f"{type(exc).__name__}: {exc}"),
        }

    chunk_dicts = [chunk.model_dump() for chunk in chunks]
    counts = summarize_pipeline_counts(chunk_dicts)
    inventory = normalized.metadata.get("document_inventory") if isinstance(normalized.metadata, dict) else None
    return {
        "status": "parsed",
        "parser": type(parser).__name__,
        "file_type": normalized.file_type,
        "page_count": normalized.page_count,
        "raw_text_chars": len(normalized.raw_text or ""),
        "node_count": len(nodes),
        "chunk_count": len(chunks),
        "pipeline_counts": counts,
        "document_inventory": inventory if isinstance(inventory, dict) else {},
    }


def compare_metrics(local: dict[str, Any], kordoc: dict[str, Any]) -> dict[str, Any]:
    content_flags: list[str] = []
    operational_flags: list[str] = []
    deltas: dict[str, int] = {}

    if local.get("status") != "parsed":
        operational_flags.append("local_parse_failed")
    if kordoc.get("status") != "parsed":
        operational_flags.append(f"kordoc_{kordoc.get('status') or 'unavailable'}")
        return _comparison_result(content_flags, operational_flags, deltas)

    local_counts = local.get("pipeline_counts") if isinstance(local.get("pipeline_counts"), dict) else {}
    local_table_count = _safe_int(local_counts.get("table_like_chunk_count"))
    local_nested_count = _safe_int(local_counts.get("nested_table_candidate_count"))
    kordoc_table_count = _safe_int(kordoc.get("table_count"))
    kordoc_nested_count = _safe_int(kordoc.get("nested_table_count"))

    deltas["table_count_delta"] = kordoc_table_count - local_table_count
    deltas["nested_table_count_delta"] = kordoc_nested_count - local_nested_count

    if deltas["table_count_delta"] != 0:
        content_flags.append("table_count_disagreement")
    if kordoc_table_count > 0 and local_table_count == 0:
        content_flags.append("kordoc_table_signal_without_local_table")
    if deltas["nested_table_count_delta"] > 0:
        content_flags.append("nested_table_disagreement")
    if _safe_int(kordoc.get("warning_count")) > 0:
        content_flags.append("kordoc_warnings_present")
    return _comparison_result(content_flags, operational_flags, deltas)


def review_required_from_comparison(comparison: dict[str, Any]) -> bool:
    return bool(comparison.get("content_flags"))


def build_crosscheck_report(
    inputs: list[Path],
    kordoc_command: str = DEFAULT_KORDOC_COMMAND,
    data_dir: Path | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in inputs:
        local = extract_local_metrics(path, data_dir=data_dir)
        kordoc = run_kordoc_parse(path, command=kordoc_command, timeout_seconds=timeout_seconds)
        comparison = compare_metrics(local, kordoc)
        rows.append(
            {
                "input_path": str(path),
                "filename": path.name,
                "local": local,
                "kordoc": kordoc,
                "comparison": comparison,
                "review_required": review_required_from_comparison(comparison),
                "operational_issue": bool(comparison.get("operational_flags")),
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract": "kordoc_sidecar_crosscheck_v1",
        "scope": "diagnostic_only_not_indexing_input",
        "kordoc_command_label": _command_label(split_command(kordoc_command)),
        "counts": {
            "documents": len(rows),
            "review_required": sum(1 for row in rows if row["review_required"]),
            "operational_issue": sum(1 for row in rows if row["operational_issue"]),
            "kordoc_parsed": sum(1 for row in rows if row["kordoc"].get("status") == "parsed"),
            "local_parsed": sum(1 for row in rows if row["local"].get("status") == "parsed"),
        },
        "rows": rows,
    }


def markdown_report(report: dict[str, Any]) -> str:
    counts = report.get("counts", {})
    lines = [
        "# Kordoc Sidecar Crosscheck",
        "",
        f"- Contract: `{report.get('contract')}`",
        f"- Scope: `{report.get('scope')}`",
        f"- Documents: {counts.get('documents', 0)}",
        f"- Review required: {counts.get('review_required', 0)}",
        f"- Operational issues: {counts.get('operational_issue', 0)}",
        f"- Local parsed: {counts.get('local_parsed', 0)}",
        f"- Kordoc parsed: {counts.get('kordoc_parsed', 0)}",
        "",
        "## Findings",
        "",
    ]
    rows = _as_list(report.get("rows"))
    if not rows:
        lines.append("- None")
    for row in rows:
        comparison = row.get("comparison") if isinstance(row.get("comparison"), dict) else {}
        flags = comparison.get("flags") or []
        deltas = comparison.get("deltas") or {}
        local_counts = row.get("local", {}).get("pipeline_counts", {}) if isinstance(row.get("local"), dict) else {}
        kordoc = row.get("kordoc") if isinstance(row.get("kordoc"), dict) else {}
        lines.append(
            "- "
            f"`{row.get('filename')}` "
            f"flags={','.join(flags) if flags else 'none'} "
            f"basis={comparison.get('basis', 'unknown')} "
            f"local_tables={_safe_int(local_counts.get('table_like_chunk_count'))} "
            f"kordoc_tables={_safe_int(kordoc.get('table_count'))} "
            f"deltas={json.dumps(deltas, ensure_ascii=False, sort_keys=True)}"
        )
    lines.extend(
        [
            "",
            "## Use",
            "",
            "- Treat this as an internal QA signal only.",
            "- Do not index Kordoc output directly.",
            "- Convert disagreements into AI review draft and human review tasks before approval.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare the local regulation parser with Kordoc as a diagnostic sidecar."
    )
    parser.add_argument("inputs", nargs="+", help="PDF/HWP/HWPX/DOCX files to compare.")
    parser.add_argument("--kordoc-command", default=DEFAULT_KORDOC_COMMAND)
    parser.add_argument("--data-dir", default="./data/kordoc_crosscheck")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs = [Path(item) for item in args.inputs]
    report = build_crosscheck_report(
        inputs,
        kordoc_command=args.kordoc_command,
        data_dir=Path(args.data_dir),
        timeout_seconds=args.timeout_seconds,
    )
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md = Path(args.out_md) if args.out_md else out_json.with_suffix(".md")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(markdown_report(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(out_json),
                "markdown": str(out_md),
                "counts": report["counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _document_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("document", "result", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _comparison_result(
    content_flags: list[str],
    operational_flags: list[str],
    deltas: dict[str, int],
) -> dict[str, Any]:
    return {
        "flags": [*content_flags, *operational_flags],
        "content_flags": content_flags,
        "operational_flags": operational_flags,
        "deltas": deltas,
        "basis": "heuristic_table_signal_not_entity_equivalence",
    }


def _command_label(parts: list[str]) -> str:
    if not parts:
        return ""
    return Path(parts[0].replace("\\", "/")).name or parts[0]


def _resolve_command_executable(command_name: str) -> str | None:
    resolved = shutil.which(command_name)
    if resolved:
        return resolved
    if os.name != "nt" or _looks_like_path(command_name):
        return None
    for directory in _windows_npm_global_dirs():
        for candidate in _windows_command_candidates(directory, command_name):
            if candidate.exists():
                return str(candidate)
    return None


def _requires_ascii_safe_input_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return True
    return False


def _copy_to_ascii_temporary_path(
    source: Path,
    *,
    prefix: str,
    preferred_parent: Path | None = None,
) -> tuple[tempfile.TemporaryDirectory, Path]:
    parent = _ascii_safe_temp_parent(preferred_parent)
    if parent is None:
        raise OSError("no ASCII-safe temporary directory is available")
    temp_dir = tempfile.TemporaryDirectory(prefix=prefix, dir=str(parent))
    try:
        suffix = source.suffix.lower() or ".bin"
        target = Path(temp_dir.name) / f"input{suffix}"
        if _requires_ascii_safe_input_path(target):
            raise OSError("generated temporary input path is not ASCII-safe")
        shutil.copy2(source, target)
    except OSError:
        temp_dir.cleanup()
        raise
    return temp_dir, target


def _ascii_safe_temp_parent(preferred_parent: Path | None = None) -> Path | None:
    for candidate in _ascii_temp_parent_candidates(preferred_parent):
        parent = _resolve_path(candidate)
        if _requires_ascii_safe_input_path(parent):
            continue
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if not _can_create_child_directory(parent):
            continue
        return parent
    return None


def _can_create_child_directory(parent: Path) -> bool:
    probe = parent / f".reg_rag_kordoc_write_probe_{uuid.uuid4().hex}"
    created = False
    try:
        probe.mkdir(mode=0o700, exist_ok=False)
        created = True
        probe.rmdir()
        created = False
    except OSError:
        return False
    finally:
        if created:
            try:
                probe.rmdir()
            except OSError:
                pass
    return True


def _ascii_temp_parent_candidates(preferred_parent: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if preferred_parent is not None:
        candidates.append(preferred_parent)
    candidates.append(Path(tempfile.gettempdir()))
    if os.name == "nt":
        program_data = os.environ.get("PROGRAMDATA")
        if program_data:
            candidates.append(Path(program_data) / "reg-rag" / "kordoc_tmp")
        system_drive = os.environ.get("SYSTEMDRIVE") or "C:"
        candidates.append(Path(f"{system_drive}\\Temp\\reg-rag-kordoc"))
        candidates.append(Path("C:\\Temp\\reg-rag-kordoc"))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _resolve_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _looks_like_path(value: str) -> bool:
    return "\\" in value or "/" in value or bool(re.match(r"^[A-Za-z]:", value))


def _windows_npm_global_dirs() -> list[Path]:
    directories: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        directories.append(Path(appdata) / "npm")
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        directories.append(Path(userprofile) / "AppData" / "Roaming" / "npm")
    directories.append(Path.home() / "AppData" / "Roaming" / "npm")
    unique: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        key = str(directory).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(directory)
    return unique


def _windows_command_candidates(directory: Path, command_name: str) -> list[Path]:
    if _has_executable_suffix(command_name):
        return [directory / command_name]
    return [directory / command_name, *(directory / f"{command_name}{suffix}" for suffix in (".cmd", ".bat", ".exe"))]


def _repair_windows_executable_path(parts: list[str]) -> list[str]:
    if not parts or not re.match(r"^[A-Za-z]:\\", parts[0]) or _has_executable_suffix(parts[0]):
        return parts
    joined: list[str] = []
    for index, part in enumerate(parts):
        joined.append(part)
        candidate = " ".join(joined)
        if _has_executable_suffix(candidate):
            return [candidate, *parts[index + 1 :]]
    return parts


def _has_executable_suffix(value: str) -> bool:
    lower = value.lower()
    return any(lower.endswith(suffix) for suffix in WINDOWS_EXECUTABLE_SUFFIXES)


def _collect_table_stats(blocks: list[Any], depth: int = 0) -> dict[str, int]:
    stats = {
        "block_count": 0,
        "table_count": 0,
        "nested_table_count": 0,
        "table_cell_count": 0,
        "merged_cell_count": 0,
        "max_table_rows": 0,
        "max_table_cols": 0,
    }
    for block in blocks:
        if not isinstance(block, dict):
            continue
        stats["block_count"] += 1
        block_type = str(block.get("type") or block.get("kind") or "").lower()
        if block_type == "table" or isinstance(block.get("table"), dict):
            stats["table_count"] += 1
            if depth > 0:
                stats["nested_table_count"] += 1
            table = block.get("table") if isinstance(block.get("table"), dict) else block
            table_shape = _table_shape(table)
            stats["max_table_rows"] = max(stats["max_table_rows"], table_shape["rows"])
            stats["max_table_cols"] = max(stats["max_table_cols"], table_shape["cols"])
            stats["table_cell_count"] += table_shape["cells"]
            stats["merged_cell_count"] += table_shape["merged_cells"]
            nested = _collect_table_stats(_cell_blocks(table), depth=depth + 1)
            _merge_stats(stats, nested)
            continue
        nested_blocks = _as_list(block.get("blocks") or block.get("children") or block.get("content"))
        if nested_blocks:
            nested = _collect_table_stats(nested_blocks, depth=depth)
            _merge_stats(stats, nested)
    return stats


def _table_shape(table: dict[str, Any]) -> dict[str, int]:
    cells = _table_cells(table)
    row_values = [
        _safe_int(cell.get("r") or cell.get("row") or cell.get("rowAddr") or cell.get("rowIndex"))
        for cell in cells
        if isinstance(cell, dict)
    ]
    col_values = [
        _safe_int(cell.get("c") or cell.get("col") or cell.get("colAddr") or cell.get("colIndex"))
        for cell in cells
        if isinstance(cell, dict)
    ]
    explicit_rows = _safe_int(table.get("rows") or table.get("rowCount"))
    explicit_cols = _safe_int(table.get("cols") or table.get("colCount") or table.get("columns"))
    rows = max(explicit_rows, max(row_values, default=-1) + 1)
    cols = max(explicit_cols, max(col_values, default=-1) + 1)
    merged_cells = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        row_span = _safe_int(cell.get("rs") or cell.get("rowSpan") or cell.get("rowspan"), default=1)
        col_span = _safe_int(cell.get("cs") or cell.get("colSpan") or cell.get("colspan"), default=1)
        if row_span > 1 or col_span > 1:
            merged_cells += 1
    return {
        "rows": rows,
        "cols": cols,
        "cells": len(cells),
        "merged_cells": merged_cells,
    }


def _cell_blocks(table: dict[str, Any]) -> list[Any]:
    nested: list[Any] = []
    for cell in _table_cells(table):
        if isinstance(cell, dict):
            nested.extend(_as_list(cell.get("blocks") or cell.get("children") or cell.get("content")))
    return nested


def _table_cells(table: dict[str, Any]) -> list[Any]:
    raw_cells = table.get("cells") or table.get("body") or []
    if not isinstance(raw_cells, list):
        return []
    cells: list[Any] = []
    for item in raw_cells:
        if isinstance(item, list):
            cells.extend(item)
        else:
            cells.append(item)
    return cells


def _merge_stats(target: dict[str, int], source: dict[str, int]) -> None:
    for key in ("block_count", "table_count", "nested_table_count", "table_cell_count", "merged_cell_count"):
        target[key] += source.get(key, 0)
    target["max_table_rows"] = max(target["max_table_rows"], source.get("max_table_rows", 0))
    target["max_table_cols"] = max(target["max_table_cols"], source.get("max_table_cols", 0))


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
