from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.processors.exporter import Exporter
from app.storage.repository import JsonRepository


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def refresh_table_exports(batch_report: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    repository = JsonRepository(settings)
    exporter = Exporter()
    refreshed: list[dict[str, Any]] = []
    missing_chunks: list[dict[str, Any]] = []

    for row in batch_report.get("rows", []) or []:
        document_id = row.get("document_id")
        if not document_id:
            continue
        chunks = repository.get_chunks(document_id)
        if not chunks:
            missing_chunks.append({"document_id": document_id, "filename": row.get("filename")})
            continue
        tables_jsonl = resolve_output_path(row.get("tables_jsonl") or "", settings.exports_dir, document_id, "tables.jsonl")
        tables_csv = resolve_output_path(row.get("tables_csv") or "", settings.exports_dir, document_id, "tables.csv")
        tables_jsonl.parent.mkdir(parents=True, exist_ok=True)
        tables_csv.parent.mkdir(parents=True, exist_ok=True)
        tables_jsonl.write_text(exporter.to_tables_jsonl(chunks), encoding="utf-8")
        tables_csv.write_text(exporter.to_tables_csv(chunks), encoding="utf-8")
        refreshed.append(
            {
                "document_id": document_id,
                "filename": row.get("filename"),
                "tables_jsonl": str(tables_jsonl),
                "tables_csv": str(tables_csv),
                "table_row_count": len(exporter.table_rows(chunks)),
            }
        )

    return {
        "generated_from": batch_report.get("generated_at"),
        "input_count": int(batch_report.get("input_count") or 0),
        "refreshed_count": len(refreshed),
        "missing_chunk_count": len(missing_chunks),
        "table_row_count": sum(int(item["table_row_count"]) for item in refreshed),
        "missing_chunks": missing_chunks,
        "refreshed": refreshed,
    }


def resolve_output_path(raw_path: str, exports_dir: Path, document_id: str, extension: str) -> Path:
    if raw_path:
        path = Path(raw_path)
        return path if path.is_absolute() else path
    return exports_dir / f"{document_id}.{extension}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh table-only export artifacts from stored chunks.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--out-json", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = refresh_table_exports(load_json(Path(args.batch_report)), settings=Settings(data_dir=Path(args.data_dir)))
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["missing_chunk_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
