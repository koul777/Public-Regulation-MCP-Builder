from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.core.config import Settings


WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".py")


class KordocTableParser:
    """Runs Kordoc as the preferred source for table structure signals."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def parse_file(self, path: Path) -> dict[str, Any]:
        started_at = time.perf_counter()
        input_extension = str(path.suffix or "").casefold()
        timeout_seconds = max(1, int(self.settings.kordoc_table_timeout_seconds))

        def finish(result: dict[str, Any]) -> dict[str, Any]:
            output = dict(result)
            output.setdefault("kordoc_elapsed_ms", round((time.perf_counter() - started_at) * 1000, 3))
            output.setdefault("kordoc_input_extension", input_extension)
            output.setdefault("kordoc_timeout_seconds", timeout_seconds)
            return output

        if not self.settings.enable_kordoc_table_parser:
            return finish({"status": "disabled", "parser": "kordoc", "table_count": 0, "tables": []})
        command = str(self.settings.kordoc_table_command or "").strip()
        if not command:
            return finish({"status": "not_available", "parser": "kordoc", "table_count": 0, "tables": []})
        parts = split_command(command)
        resolved = _resolve_command_executable(parts[0]) if parts else None
        if not parts or resolved is None:
            return finish({
                "status": "not_available",
                "parser": "kordoc",
                "table_count": 0,
                "tables": [],
                "command_label": parts[0] if parts else "",
            })
        run_path = path
        temp_dir: tempfile.TemporaryDirectory | None = None
        copied_to_ascii_path = False
        if _requires_ascii_safe_input_path(path):
            try:
                temp_dir, run_path = _copy_to_ascii_temporary_path(
                    path,
                    prefix="reg_rag_kordoc_",
                    preferred_parent=self.settings.data_dir / ".tmp" / "kordoc",
                )
                copied_to_ascii_path = True
            except OSError:
                if temp_dir is not None:
                    temp_dir.cleanup()
                return finish({
                    "status": "failed",
                    "parser": "kordoc",
                    "table_count": 0,
                    "tables": [],
                    # The exception may contain an absolute local path. Keep
                    # runtime exports and authenticated document responses
                    # free of host-specific filesystem details.
                    "error": "ascii_temp_unavailable",
                })
        argv = [resolved, *parts[1:], str(run_path), "--format", "json", "--silent"]
        # Windows npm shims need an explicit shell host when launched from
        # subprocess with argv-list form.
        if _is_windows() and resolved.lower().endswith(".ps1"):
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *argv]
        elif _is_windows() and resolved.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", *argv]
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            if temp_dir is not None:
                temp_dir.cleanup()
            return finish({"status": "timeout", "parser": "kordoc", "table_count": 0, "tables": []})
        except OSError:
            if temp_dir is not None:
                temp_dir.cleanup()
            return finish({"status": "failed", "parser": "kordoc", "table_count": 0, "tables": []})
        if temp_dir is not None:
            temp_dir.cleanup()
        if completed.returncode != 0:
            return finish({
                "status": "failed",
                "parser": "kordoc",
                "table_count": 0,
                "tables": [],
                "stderr_chars": len(completed.stderr or ""),
            })
        try:
            payload = _load_json_payload(completed.stdout or "")
        except json.JSONDecodeError:
            return finish({"status": "invalid_json", "parser": "kordoc", "table_count": 0, "tables": []})
        max_tables = max(1, int(getattr(self.settings, "kordoc_table_max_tables", 500)))
        result = extract_kordoc_table_inventory(payload, max_tables=max_tables)
        if copied_to_ascii_path:
            result["input_path_normalized_for_kordoc"] = True
        return finish(result)


def split_command(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=not _is_windows())
    except ValueError:
        return [command]
    if _is_windows():
        return _repair_windows_executable_path([part.strip("\"") for part in parts])
    return parts


def _is_windows() -> bool:
    """Keep platform branching mockable without mutating process-wide os.name."""
    return os.name == "nt"


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
        return parent
    return None


def _ascii_temp_parent_candidates(preferred_parent: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if preferred_parent is not None:
        candidates.append(preferred_parent)
    candidates.append(Path(tempfile.gettempdir()))
    if _is_windows():
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


def _resolve_command_executable(command_name: str) -> str | None:
    resolved = shutil.which(command_name)
    if resolved:
        return resolved
    if not _is_windows() or _looks_like_path(command_name):
        return None
    for directory in _windows_npm_global_dirs():
        for candidate in _windows_command_candidates(directory, command_name):
            if candidate.exists():
                return str(candidate)
    return None


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


def _load_json_payload(stdout: str) -> Any:
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


def extract_kordoc_table_inventory(payload: Any, *, max_tables: int = 50) -> dict[str, Any]:
    tables = _collect_tables(payload)
    normalized = [_normalize_table(table, index) for index, table in enumerate(tables[:max_tables], start=1)]
    normalized = [table for table in normalized if table["row_count"] > 0 or table["cell_count"] > 0]
    return {
        "status": "parsed",
        "parser": "kordoc",
        "table_count": len(tables),
        "stored_table_count": len(normalized),
        "tables_truncated": len(tables) > max_tables,
        "tables": normalized,
        "review_required": bool(normalized),
        "review_flags": ["kordoc_table_parser_used"] if normalized else [],
    }


def _collect_tables(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(node: Any, *, inside_table: bool = False) -> None:
        if isinstance(node, dict):
            looks_like_table = _looks_like_table(node)
            if looks_like_table and not inside_table:
                found.append(node)
            for child in node.values():
                visit(child, inside_table=inside_table or looks_like_table)
            return
        if isinstance(node, list):
            for child in node:
                visit(child, inside_table=inside_table)

    visit(value)
    return found


def _looks_like_table(node: dict[str, Any]) -> bool:
    type_text = str(node.get("type") or node.get("kind") or node.get("blockType") or "").lower()
    if "table" in type_text:
        return True
    if isinstance(node.get("table"), dict) and _looks_like_table_data(node["table"]):
        return True
    if _looks_like_table_data(node):
        return True
    return False


def _looks_like_table_data(node: dict[str, Any]) -> bool:
    rows = node.get("rows")
    if isinstance(rows, list) and rows and _row_like(rows[0]):
        return True
    cells = node.get("cells")
    if isinstance(cells, list) and cells and isinstance(cells[0], list):
        return True
    if isinstance(cells, list) and cells and isinstance(cells[0], dict):
        keys = set(cells[0])
        return bool(
            keys.intersection({"row", "rowIndex", "row_index", "r"})
            and keys.intersection({"col", "colIndex", "col_index", "column", "c"})
        )
    body = node.get("body")
    if isinstance(body, list) and body and isinstance(body[0], list):
        return True
    return False


def _row_like(value: Any) -> bool:
    if isinstance(value, list):
        return True
    if isinstance(value, dict):
        return isinstance(value.get("cells"), list) or isinstance(value.get("columns"), list)
    return False


def _normalize_table(table: dict[str, Any], index: int) -> dict[str, Any]:
    rows = _rows_from_table(table)
    column_count = max((len(row["cells"]) for row in rows), default=0)
    data = _table_data(table)
    merged_cell_count = _merged_cell_count(_table_cells(data))
    nested_count = _nested_table_count(data)
    grid = _dense_grid(data)
    explicit_title = _first_text(table, data, keys=("title", "caption", "name"))
    inferred_title = explicit_title or _infer_table_title(rows)
    return {
        "table_index": index,
        "title": inferred_title,
        "title_source": "kordoc" if explicit_title else ("first_row" if inferred_title else "missing"),
        "source_page": _first_value(table, data, keys=("page", "pageNo", "source_page", "page_number")),
        "row_count": len(rows),
        "column_count": column_count,
        "cell_count": sum(len(row["cells"]) for row in rows),
        "merged_cell_count": merged_cell_count,
        "nested_table_count": max(0, nested_count),
        # Position-stripped rows (backward compatible: empty cells removed).
        "cell_rows": rows,
        # Geometry-preserving rows: column positions retained and cell spans
        # expanded, so marks that depend on their column (e.g. approval "○"
        # under a specific approver) stay aligned. Empty spacer columns are
        # pruned for readability. Empty when the source is not a dense matrix.
        "grid_rows": grid["rows"],
        "grid_column_count": grid["column_count"],
        "grid_has_header": grid["has_header"],
        "markdown": grid["markdown"],
    }


def _infer_table_title(rows: list[dict[str, Any]]) -> str:
    """Use a short caption-like first row when Kordoc omitted table title."""
    for row in rows[:3]:
        cells = [str(cell or "").strip() for cell in row.get("cells") or []]
        text = " ".join(cell for cell in cells if cell).strip()
        if not text or len(text) > 120:
            continue
        compact = re.sub(r"\s+", "", text)
        if compact.startswith(("별표", "별지")) or any(
            token in compact
            for token in ("기준표", "명부", "심의서", "계정과목", "평가표", "환산율표")
        ):
            return text
    return ""


def _dense_grid(table: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct a rectangular text grid honoring colSpan/rowSpan.

    Kordoc emits ``cells`` as a matrix (list of rows) where merged regions are
    filled with span metadata. Expanding spans and keeping empty positions lets
    downstream rendering preserve which column a value sits under. Falls back to
    an empty grid for non-matrix shapes (flat cells / row-list only)."""
    empty = {"rows": [], "column_count": 0, "has_header": False, "markdown": ""}
    cells = table.get("cells")
    if not (isinstance(cells, list) and cells and isinstance(cells[0], list)):
        return empty
    occupied: dict[tuple[int, int], str] = {}
    header_rows: set[int] = set()
    max_col = 0
    for row_index, row in enumerate(cells):
        if not isinstance(row, list):
            continue
        col_cursor = 0
        for cell in row:
            while (row_index, col_cursor) in occupied:
                col_cursor += 1
            text = _cell_text(cell)
            col_span = _first_int(cell, ("cs", "colSpan", "colspan")) or 1 if isinstance(cell, dict) else 1
            row_span = _first_int(cell, ("rs", "rowSpan", "rowspan")) or 1 if isinstance(cell, dict) else 1
            col_span = max(1, col_span)
            row_span = max(1, row_span)
            if isinstance(cell, dict) and _is_truthy(cell.get("isHeader")):
                header_rows.add(row_index)
            for dr in range(row_span):
                for dc in range(col_span):
                    occupied[(row_index + dr, col_cursor + dc)] = text if dr == 0 and dc == 0 else ""
            col_cursor += col_span
            max_col = max(max_col, col_cursor)
    if not occupied:
        return empty
    row_total = max(r for r, _ in occupied) + 1
    matrix = [["" for _ in range(max_col)] for _ in range(row_total)]
    for (r, c), text in occupied.items():
        if c < max_col:
            matrix[r][c] = text
    keep_cols = [c for c in range(max_col) if any(matrix[r][c].strip() for r in range(row_total))]
    pruned = [[row[c] for c in keep_cols] for row in matrix]
    pruned = [row for row in pruned if any(cell.strip() for cell in row)]
    has_header = 0 in header_rows or bool(_is_truthy(table.get("hasHeader")))
    return {
        "rows": [{"row_index": i, "cells": row, "raw": " | ".join(row)} for i, row in enumerate(pruned)],
        "column_count": len(keep_cols),
        "has_header": has_header,
        "markdown": _grid_to_markdown(pruned, has_header),
    }


def _grid_to_markdown(rows: list[list[str]], has_header: bool) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]

    def render(cells: list[str]) -> str:
        return "| " + " | ".join(cell.replace("\n", " ").replace("|", "\\|").strip() for cell in cells) + " |"

    lines = [render(padded[0])]
    if has_header:
        lines.append("| " + " | ".join(["---"] * width) + " |")
    lines.extend(render(row) for row in padded[1:])
    return "\n".join(lines)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _rows_from_table(table: dict[str, Any]) -> list[dict[str, Any]]:
    table = _table_data(table)
    rows = table.get("rows")
    if isinstance(rows, list):
        result = [_normalize_row(row, index) for index, row in enumerate(rows)]
        return [row for row in result if row["cells"]]
    cells = table.get("cells")
    if isinstance(cells, list) and cells and isinstance(cells[0], dict):
        return _rows_from_flat_cells(cells)
    if isinstance(cells, list) and cells and isinstance(cells[0], list):
        return _rows_from_matrix_cells(cells)
    body = table.get("body")
    if isinstance(body, list) and body and isinstance(body[0], list):
        return _rows_from_matrix_cells(body)
    return []


def _normalize_row(row: Any, index: int) -> dict[str, Any]:
    if isinstance(row, list):
        cells = [_cell_text(cell) for cell in row]
    elif isinstance(row, dict):
        source_cells = row.get("cells") if isinstance(row.get("cells"), list) else row.get("columns")
        cells = [_cell_text(cell) for cell in source_cells] if isinstance(source_cells, list) else []
    else:
        cells = []
    cells = [cell for cell in cells if cell != ""]
    return {"row_index": index, "cells": cells, "raw": " | ".join(cells)}


def _rows_from_flat_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[int, dict[int, str]] = {}
    for cell in cells:
        row_index = _first_int(cell, ("row", "rowIndex", "row_index", "r"))
        col_index = _first_int(cell, ("col", "colIndex", "col_index", "column", "c"))
        if row_index is None or col_index is None:
            continue
        rows.setdefault(row_index, {})[col_index] = _cell_text(cell)
    result: list[dict[str, Any]] = []
    for row_index in sorted(rows):
        row_cells = [rows[row_index][col] for col in sorted(rows[row_index]) if rows[row_index][col] != ""]
        result.append({"row_index": row_index, "cells": row_cells, "raw": " | ".join(row_cells)})
    return result


def _rows_from_matrix_cells(rows: list[list[Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        cells = [_cell_text(cell) for cell in row]
        cells = [cell for cell in cells if cell != ""]
        if cells:
            result.append({"row_index": row_index, "cells": cells, "raw": " | ".join(cells)})
    return result


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        for key in ("text", "value", "content", "markdown"):
            if cell.get(key) is not None:
                return str(cell.get(key)).strip()
        if isinstance(cell.get("paragraphs"), list):
            return " ".join(str(item).strip() for item in cell["paragraphs"] if str(item).strip())
        blocks = cell.get("blocks") or cell.get("children") or cell.get("content")
        if isinstance(blocks, list):
            return " ".join(_cell_text(item) for item in blocks if _cell_text(item)).strip()
    return str(cell if cell is not None else "").strip()


def _first_int(row: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key not in row:
            continue
        try:
            return int(row[key])
        except (TypeError, ValueError):
            return None
    return None


def _table_data(table: dict[str, Any]) -> dict[str, Any]:
    nested = table.get("table")
    if isinstance(nested, dict):
        return nested
    return table


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


def _merged_cell_count(cells: list[Any]) -> int:
    total = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        row_span = _first_int(cell, ("rs", "rowSpan", "rowspan")) or 1
        col_span = _first_int(cell, ("cs", "colSpan", "colspan")) or 1
        if row_span > 1 or col_span > 1:
            total += 1
    return total


def _nested_table_count(table: dict[str, Any]) -> int:
    count = 0

    def visit(node: Any) -> None:
        nonlocal count
        if isinstance(node, dict):
            if _looks_like_table(node):
                count += 1
                return
            for child in node.values():
                visit(child)
            return
        if isinstance(node, list):
            for child in node:
                visit(child)

    for cell in _table_cells(table):
        if not isinstance(cell, dict):
            continue
        visit(cell.get("blocks") or cell.get("children") or cell.get("content") or [])
    return count


def _first_value(*items: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for item in items:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
    return None


def _first_text(*items: dict[str, Any], keys: tuple[str, ...]) -> str:
    value = _first_value(*items, keys=keys)
    return str(value or "").strip()
