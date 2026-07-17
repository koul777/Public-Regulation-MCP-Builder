from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.hermes_agent import HermesAgent, render_hermes_markdown
from scripts.build_release_evidence_index import build_release_evidence_index
from scripts.verify_release_evidence_bundle import verify_release_evidence_index


DEFAULT_HERMES_REPORT_JSON = Path("reports/hermes_mcp_check_current.json")
DEFAULT_HERMES_REPORT_MD = Path("reports/hermes_mcp_check_current.md")
DEFAULT_EVIDENCE_INDEX_JSON = Path("reports/hermes_release_evidence_index_current.json")
DEFAULT_EVIDENCE_VERIFICATION_JSON = Path("reports/hermes_release_evidence_verification_current.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Hermes deterministic operator orchestration.")
    parser.add_argument("--mode", choices=["plan", "mcp-check", "internal-check", "public-check"], default="plan")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--public-url", default="https://mcp.example.invalid/mcp")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--server-name", default="regulation_mcp")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--tenant-storage-isolation", action="store_true")
    parser.add_argument("--mcp-runtime-data-dir", default=None)
    parser.add_argument("--mcp-bundle-profile-id", default=None)
    parser.add_argument("--mcp-bundle-document-id", default=None)
    parser.add_argument("--mcp-min-visible-records", type=int, default=1)
    parser.add_argument("--bundle-dir", default="reports/mcp_connection_bundle_hermes")
    parser.add_argument("--bundle-zip", default="reports/mcp_connection_bundle_hermes.zip")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Execute even when mode is plan.")
    parser.add_argument("--no-keep-going", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--include-tests", action="store_true", help="Include unit tests in mcp-check mode.")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--include-build", action="store_true", help="Include package build in mcp-check mode.")
    parser.add_argument("--include-wheel-in-bundle", action="store_true", help="Include the built wheel in the MCP setup bundle zip.")
    parser.add_argument("--skip-mcp-transport-smoke", action="store_true")
    parser.add_argument("--require-tunnel-client", action="store_true")
    parser.add_argument("--probe-public-url", action="store_true", help="Forward live HTTPS /mcp probing to remote MCP doctors.")
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument(
        "--include-evidence",
        action="store_true",
        help="After writing the MCP check report, build and verify the hermes-mcp evidence index.",
    )
    parser.add_argument("--evidence-index-json", default=str(DEFAULT_EVIDENCE_INDEX_JSON))
    parser.add_argument("--evidence-verification-json", default=str(DEFAULT_EVIDENCE_VERIFICATION_JSON))
    parser.add_argument("--fail-on-attention", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    dry_run = args.dry_run or (args.mode == "plan" and not args.execute)
    payload = {
        "mode": args.mode,
        "project_root": args.project_root or Path.cwd(),
        "public_url": args.public_url,
        "host": args.host,
        "port": args.port,
        "server_name": args.server_name,
        "tenant_id": args.tenant_id,
        "tenant_storage_isolation": args.tenant_storage_isolation,
        "mcp_runtime_data_dir": args.mcp_runtime_data_dir,
        "mcp_bundle_profile_id": args.mcp_bundle_profile_id,
        "mcp_bundle_document_id": args.mcp_bundle_document_id,
        "mcp_min_visible_records": args.mcp_min_visible_records,
        "bundle_dir": args.bundle_dir,
        "bundle_zip": args.bundle_zip,
        "dry_run": dry_run,
        "keep_going": not args.no_keep_going,
        "skip_tests": args.skip_tests or (args.mode == "mcp-check" and not args.include_tests),
        "skip_build": args.skip_build or (args.mode == "mcp-check" and not args.include_build),
        "include_wheel_in_bundle": args.include_wheel_in_bundle
        or (args.mode == "mcp-check" and args.include_build and not args.skip_build),
        "skip_mcp_transport_smoke": args.skip_mcp_transport_smoke,
        "require_tunnel_client": args.require_tunnel_client,
        "probe_public_url": args.probe_public_url,
        "timeout_seconds": args.timeout_seconds,
    }
    report = HermesAgent().run(payload)
    output_report = report
    evidence_verification: dict | None = None
    if args.include_evidence and args.mode != "mcp-check":
        raise ValueError("--include-evidence is supported only with --mode mcp-check")
    _write_hermes_report_outputs(
        report,
        out_json=Path(args.out_json) if args.out_json else None,
        out_md=Path(args.out_md) if args.out_md else None,
        include_evidence=args.include_evidence and not dry_run,
    )
    if args.include_evidence and not dry_run:
        evidence_verification = _write_hermes_evidence_outputs(
            project_root=Path(payload["project_root"]).resolve(),
            index_json=Path(args.evidence_index_json),
            verification_json=Path(args.evidence_verification_json),
        )
        output_report = {
            "report_type": "hermes_agent_evidence_run",
            "hermes_report": report,
            "evidence_verification": evidence_verification,
        }
    elif args.include_evidence and dry_run:
        output_report = {
            "report_type": "hermes_agent_evidence_plan",
            "hermes_report": report,
            "evidence_plan": {
                "profile": "hermes-mcp",
                "index_json": str(Path(args.evidence_index_json)),
                "verification_json": str(Path(args.evidence_verification_json)),
                "required_report_json": str(DEFAULT_HERMES_REPORT_JSON),
                "required_report_md": str(DEFAULT_HERMES_REPORT_MD),
            },
        }
    stdout.write(json.dumps(output_report, ensure_ascii=False, indent=2) + "\n")
    if args.fail_on_attention and report.get("status") not in {"ready", "ready_with_advisories", "plan_ready"}:
        return 2
    if evidence_verification is not None and not evidence_verification.get("passed"):
        return 1
    return 0


def _write_hermes_report_outputs(
    report: dict,
    *,
    out_json: Path | None,
    out_md: Path | None,
    include_evidence: bool,
) -> None:
    json_targets: list[Path] = []
    md_targets: list[Path] = []
    if out_json is not None:
        json_targets.append(out_json)
    if out_md is not None:
        md_targets.append(out_md)
    if include_evidence:
        json_targets.append(DEFAULT_HERMES_REPORT_JSON)
        md_targets.append(DEFAULT_HERMES_REPORT_MD)
    payload_text = json.dumps(report, ensure_ascii=False, indent=2)
    markdown_text = render_hermes_markdown(report)
    for target in _unique_paths(json_targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload_text + "\n", encoding="utf-8")
    for target in _unique_paths(md_targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown_text, encoding="utf-8")


def _write_hermes_evidence_outputs(
    *,
    project_root: Path,
    index_json: Path,
    verification_json: Path,
) -> dict:
    index = build_release_evidence_index(project_root, evidence_profile="hermes-mcp")
    index_json = index_json if index_json.is_absolute() else project_root / index_json
    verification_json = verification_json if verification_json.is_absolute() else project_root / verification_json
    index_json.parent.mkdir(parents=True, exist_ok=True)
    index_json.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verification = verify_release_evidence_index(index, repo_root=project_root)
    verification_json.parent.mkdir(parents=True, exist_ok=True)
    verification_json.write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return verification


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()) if path.is_absolute() else path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
