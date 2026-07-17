from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_ci_regression_gate import run_regression_gate


def _latest_report_file(root: Path, pattern: str) -> Path | None:
    candidates = sorted(root.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def run_nightly_smoke(
    *,
    project_root: Path | None = None,
    require_optional_artifacts: bool = True,
) -> dict[str, Any]:
    root = project_root or PROJECT_ROOT
    checks: list[dict[str, Any]] = []

    regression = run_regression_gate(project_root=root, include_release_hygiene=True)
    checks.append(
        {
            "name": "ci_regression_gate",
            "passed": bool(regression.get("passed")),
            "details": regression,
        }
    )

    ocr_alert_path = _latest_report_file(root / "reports", "batch_failure_alert_ocr_smoke_*.json")
    if ocr_alert_path and ocr_alert_path.is_file():
        ocr_alert = json.loads(ocr_alert_path.read_text(encoding="utf-8"))
        checks.append(
            {
                "name": "ocr_failure_alert_smoke",
                "passed": ocr_alert.get("severity") == "warning" and ocr_alert.get("api_call_count") == 0,
                "details": {
                    "artifact": str(ocr_alert_path.relative_to(root)).replace("\\", "/"),
                    "status": ocr_alert.get("status"),
                    "severity": ocr_alert.get("severity"),
                    "api_call_count": ocr_alert.get("api_call_count"),
                },
            }
        )
    else:
        checks.append(_missing_optional_check("ocr_failure_alert_smoke", require_optional_artifacts))

    qdrant_manifest = _latest_report_file(root / "reports", "qdrant_points_public_portal_smoke_*.manifest.json")
    if qdrant_manifest and qdrant_manifest.is_file():
        qdrant = json.loads(qdrant_manifest.read_text(encoding="utf-8"))
        checks.append(
            {
                "name": "qdrant_local_export_smoke",
                "passed": qdrant.get("api_call_count") == 0 and qdrant.get("local_path_leak_count") == 0,
                "details": {
                    "artifact": str(qdrant_manifest.relative_to(root)).replace("\\", "/"),
                    "target_type": qdrant.get("target_type"),
                    "input_record_count": qdrant.get("input_record_count"),
                    "api_call_count": qdrant.get("api_call_count"),
                },
            }
        )
    else:
        checks.append(_missing_optional_check("qdrant_local_export_smoke", require_optional_artifacts))

    pgvector_manifest = _latest_report_file(root / "reports", "pgvector_rows_public_portal_smoke_*.manifest.json")
    if pgvector_manifest and pgvector_manifest.is_file():
        pgvector = json.loads(pgvector_manifest.read_text(encoding="utf-8"))
        checks.append(
            {
                "name": "pgvector_local_export_smoke",
                "passed": pgvector.get("api_call_count") == 0 and pgvector.get("local_path_leak_count") == 0,
                "details": {
                    "artifact": str(pgvector_manifest.relative_to(root)).replace("\\", "/"),
                    "target_type": pgvector.get("target_type"),
                    "input_record_count": pgvector.get("input_record_count"),
                    "api_call_count": pgvector.get("api_call_count"),
                },
            }
        )
    else:
        checks.append(_missing_optional_check("pgvector_local_export_smoke", require_optional_artifacts))

    chroma_manifest = _latest_report_file(root / "reports", "chroma_rows_public_portal_smoke_*.manifest.json")
    if chroma_manifest and chroma_manifest.is_file():
        chroma = json.loads(chroma_manifest.read_text(encoding="utf-8"))
        checks.append(
            {
                "name": "chroma_local_export_smoke",
                "passed": chroma.get("api_call_count") == 0 and chroma.get("local_path_leak_count") == 0,
                "details": {
                    "artifact": str(chroma_manifest.relative_to(root)).replace("\\", "/"),
                    "target_type": chroma.get("target_type"),
                    "input_record_count": chroma.get("input_record_count"),
                    "api_call_count": chroma.get("api_call_count"),
                },
            }
        )
    else:
        checks.append(_missing_optional_check("chroma_local_export_smoke", require_optional_artifacts))

    law_ref_path = _latest_report_file(root / "reports", "law_reference_report_integrated_pdf_*.json")
    if law_ref_path and law_ref_path.is_file():
        law_ref = json.loads(law_ref_path.read_text(encoding="utf-8"))
        summary = law_ref.get("summary") or {}
        checks.append(
            {
                "name": "law_reference_report_smoke",
                "passed": (
                    law_ref.get("api_call_count") == 0
                    and int(summary.get("chunks_with_internal_regulation_refs") or 0) > 0
                    and int(summary.get("revision_history_span_count") or 0) > 0
                ),
                "details": {
                    "artifact": str(law_ref_path.relative_to(root)).replace("\\", "/"),
                    "chunks_with_internal_regulation_refs": summary.get("chunks_with_internal_regulation_refs"),
                    "chunks_with_external_law_refs": summary.get("chunks_with_external_law_refs"),
                    "revision_history_span_count": summary.get("revision_history_span_count"),
                },
            }
        )
    else:
        checks.append(_missing_optional_check("law_reference_report_smoke", require_optional_artifacts))

    public_portal_law_ref_path = _latest_report_file(root / "reports", "law_reference_report_public_portal_reprocess_*.json")
    if public_portal_law_ref_path and public_portal_law_ref_path.is_file():
        public_portal_law_ref = json.loads(public_portal_law_ref_path.read_text(encoding="utf-8"))
        public_portal_summary = public_portal_law_ref.get("summary") or {}
        checks.append(
            {
                "name": "public_portal_law_reference_report_smoke",
                "passed": (
                    public_portal_law_ref.get("api_call_count") == 0
                    and int(public_portal_summary.get("chunks_with_internal_regulation_refs") or 0) > 0
                    and int(public_portal_summary.get("chunks_with_external_law_refs") or 0) > 0
                    and int(public_portal_law_ref.get("document_count") or 0) == 83
                ),
                "details": {
                    "artifact": str(public_portal_law_ref_path.relative_to(root)).replace("\\", "/"),
                    "document_count": public_portal_law_ref.get("document_count"),
                    "chunks_with_internal_regulation_refs": public_portal_summary.get("chunks_with_internal_regulation_refs"),
                    "chunks_with_external_law_refs": public_portal_summary.get("chunks_with_external_law_refs"),
                },
            }
        )
    else:
        checks.append(_missing_optional_check("public_portal_law_reference_report_smoke", require_optional_artifacts))

    qdrant_rest_manifest = _latest_report_file(root / "reports", "qdrant_rest_manifest_public_portal_smoke_*.manifest.json")
    if qdrant_rest_manifest and qdrant_rest_manifest.is_file():
        qdrant_rest = json.loads(qdrant_rest_manifest.read_text(encoding="utf-8"))
        checks.append(
            {
                "name": "qdrant_rest_manifest_smoke",
                "passed": qdrant_rest.get("api_call_count") == 0 and qdrant_rest.get("live_network_blocked") is True,
                "details": {
                    "artifact": str(qdrant_rest_manifest.relative_to(root)).replace("\\", "/"),
                    "target_type": qdrant_rest.get("target_type"),
                    "planned_upsert_count": qdrant_rest.get("planned_upsert_count"),
                    "collection_name": qdrant_rest.get("collection_name"),
                },
            }
        )
    else:
        checks.append(_missing_optional_check("qdrant_rest_manifest_smoke", require_optional_artifacts))

    passed = all(item.get("passed") for item in checks)
    return {
        "report_type": "nightly_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "check_count": len(checks),
        "checks": checks,
        "api_call_count": 0,
        "mode": "local_smoke_only",
        "require_optional_artifacts": require_optional_artifacts,
    }


def _missing_optional_check(name: str, require_optional_artifacts: bool) -> dict[str, Any]:
    return {
        "name": name,
        "passed": not require_optional_artifacts,
        "details": {
            "reason": "missing_artifact" if require_optional_artifacts else "missing_optional_artifact",
            "required": require_optional_artifacts,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local nightly smoke checks without live PUBLIC_PORTAL fetch or provider calls.")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument(
        "--allow-missing-optional-artifacts",
        action="store_true",
        help="Do not fail when historical optional report artifacts are absent in a fresh clone.",
    )
    parser.add_argument("--fail-on-smoke-failure", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.project_root) if args.project_root else PROJECT_ROOT
    report = run_nightly_smoke(
        project_root=root,
        require_optional_artifacts=not args.allow_missing_optional_artifacts,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_json = Path(args.out_json) if args.out_json else root / "reports" / f"nightly_smoke_{timestamp}.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["out_json"] = str(out_json)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_smoke_failure and not report.get("passed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
