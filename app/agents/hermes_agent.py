from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.base import AgentResult, BaseAgent
from scripts.run_release_harness import HarnessOptions, run_harness


VALID_HERMES_MODES = {"plan", "mcp-check", "internal-check", "public-check"}


class HermesAgent(BaseAgent):
    """Deterministic operator agent for release and MCP handoff orchestration."""

    def run(self, payload: dict) -> AgentResult:
        mode = str(payload.get("mode") or "plan").strip()
        if mode not in VALID_HERMES_MODES:
            raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_HERMES_MODES))}")

        project_root = Path(str(payload.get("project_root") or Path.cwd())).resolve()
        dry_run = bool(payload.get("dry_run", mode == "plan"))
        keep_going = bool(payload.get("keep_going", True))
        harness_mode = _harness_mode(mode)
        skip_build = bool(payload.get("skip_build", mode == "mcp-check"))
        include_wheel_in_bundle = bool(payload.get("include_wheel_in_bundle", mode == "mcp-check" and not skip_build))
        options = HarnessOptions(
            project_root=project_root,
            mode=harness_mode,
            host=str(payload.get("host") or "0.0.0.0"),
            port=int(payload.get("port") or 8000),
            public_url=str(payload.get("public_url") or "https://mcp.example.invalid/mcp"),
            server_name=str(payload.get("server_name") or "regulation_mcp"),
            tenant_id=str(payload.get("tenant_id") or "default"),
            tenant_storage_isolation=bool(payload.get("tenant_storage_isolation", False)),
            mcp_runtime_data_dir=(
                Path(str(payload.get("mcp_runtime_data_dir")))
                if payload.get("mcp_runtime_data_dir") not in (None, "")
                else None
            ),
            mcp_bundle_profile_id=(
                str(payload.get("mcp_bundle_profile_id"))
                if payload.get("mcp_bundle_profile_id") not in (None, "")
                else None
            ),
            mcp_bundle_document_id=(
                str(payload.get("mcp_bundle_document_id"))
                if payload.get("mcp_bundle_document_id") not in (None, "")
                else None
            ),
            mcp_min_visible_records=int(payload.get("mcp_min_visible_records") or 1),
            bundle_dir=Path(str(payload.get("bundle_dir") or "reports/mcp_connection_bundle_hermes")),
            bundle_zip=Path(str(payload.get("bundle_zip") or "reports/mcp_connection_bundle_hermes.zip")),
            bundle_json=Path(str(payload.get("bundle_json") or "reports/mcp_client_bundle_hermes.json")),
            console_json=Path(str(payload.get("console_json") or "reports/installed_console_scripts_hermes.json")),
            mcp_smoke_json=Path(str(payload.get("mcp_smoke_json") or "reports/mcp_smoke_hermes.json")),
            mcp_transport_smoke_json=Path(
                str(payload.get("mcp_transport_smoke_json") or "reports/mcp_transport_smoke_hermes.json")
            ),
            bundle_doctor_json=Path(
                str(payload.get("bundle_doctor_json") or "reports/mcp_connection_readiness_bundle_hermes.json")
            ),
            chatgpt_https_json=Path(
                str(payload.get("chatgpt_https_json") or "reports/mcp_connection_readiness_chatgpt_https_hermes.json")
            ),
            chatgpt_tunnel_json=Path(
                str(payload.get("chatgpt_tunnel_json") or "reports/mcp_connection_readiness_chatgpt_tunnel_hermes.json")
            ),
            public_audit_json=Path(str(payload.get("public_audit_json") or "reports/public_release_readiness_audit.hermes.json")),
            cleanup_json=Path(str(payload.get("cleanup_json") or "reports/public_release_cleanup_plan.hermes.json")),
            cleanup_md=Path(str(payload.get("cleanup_md") or "reports/public_release_cleanup_plan.hermes.md")),
            skip_tests=bool(payload.get("skip_tests", mode == "mcp-check")),
            skip_build=skip_build,
            skip_console_check=bool(payload.get("skip_console_check", False)),
            skip_mcp_smoke=bool(payload.get("skip_mcp_smoke", False)),
            skip_mcp_transport_smoke=bool(payload.get("skip_mcp_transport_smoke", False)),
            skip_public_audit=bool(payload.get("skip_public_audit", False)),
            require_tunnel_client=bool(payload.get("require_tunnel_client", False)),
            include_wheel_in_bundle=include_wheel_in_bundle,
            probe_public_url=bool(payload.get("probe_public_url", False)),
        )
        harness = run_harness(
            options,
            dry_run=dry_run,
            keep_going=keep_going,
            timeout_seconds=payload.get("timeout_seconds"),
        )
        probe_public_url = bool(payload.get("probe_public_url", False))
        report = AgentResult(
            {
                "report_type": "hermes_agent_run",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "mode": mode,
                "harness_mode": harness_mode,
                "dry_run": dry_run,
                "probe_public_url": probe_public_url,
                "project_root": str(project_root),
                "status": _status_for_harness(mode=mode, harness=harness),
                "summary": _summary_for_harness(harness),
                "attention_items": _attention_items_for_harness(
                    mode=mode,
                    harness=harness,
                    probe_public_url=probe_public_url,
                ),
                "next_actions": _next_actions_for_harness(
                    mode=mode,
                    harness=harness,
                    probe_public_url=probe_public_url,
                ),
                "harness": harness,
            }
        )
        return report


def render_hermes_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    harness = report.get("harness") if isinstance(report.get("harness"), dict) else {}
    actions = report.get("next_actions") if isinstance(report.get("next_actions"), list) else []
    attention_items = report.get("attention_items") if isinstance(report.get("attention_items"), list) else []
    lines = [
        "# Hermes Agent Report",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Harness mode: `{report.get('harness_mode')}`",
        f"- Dry run: `{str(report.get('dry_run')).lower()}`",
        f"- Probe public URL: `{str(report.get('probe_public_url')).lower()}`",
        f"- Step count: `{summary.get('step_count', 0)}`",
        f"- Executed steps: `{summary.get('executed_step_count', 0)}`",
        f"- Required failures: `{summary.get('failed_required_count', 0)}`",
        f"- Advisory failures: `{summary.get('failed_advisory_count', 0)}`",
        "",
        "## Attention Items",
        "",
    ]
    if attention_items:
        lines.extend(f"- {item}" for item in attention_items)
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Next Actions",
            "",
        ]
    )
    if actions:
        lines.extend(f"{idx}. {action}" for idx, action in enumerate(actions, start=1))
    else:
        lines.append("1. No action required.")
    lines.extend(["", "## Harness Steps", ""])
    steps = harness.get("steps") if isinstance(harness.get("steps"), list) else []
    if steps:
        for step in steps:
            if isinstance(step, dict):
                lines.append(_render_step_line(step, dry_run=bool(harness.get("dry_run"))))
    else:
        lines.append("- None.")
    artifacts = _evidence_artifacts(steps, project_root=report.get("project_root"))
    lines.extend(["", "## Evidence Outputs", ""])
    if artifacts:
        lines.extend(f"- `{artifact}`" for artifact in artifacts)
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _harness_mode(mode: str) -> str:
    if mode == "mcp-check":
        return "mcp"
    if mode == "public-check":
        return "public"
    return "internal"


def _summary_for_harness(harness: dict[str, Any]) -> dict[str, Any]:
    failed_required = list(harness.get("failed_required_step_names") or [])
    failed_advisory = list(harness.get("failed_advisory_step_names") or [])
    return {
        "passed": bool(harness.get("passed")),
        "step_count": int(harness.get("step_count") or 0),
        "executed_step_count": int(harness.get("executed_step_count") or 0),
        "failed_required_count": len(failed_required),
        "failed_required_step_names": failed_required,
        "failed_advisory_count": len(failed_advisory),
        "failed_advisory_step_names": failed_advisory,
    }


def _status_for_harness(*, mode: str, harness: dict[str, Any]) -> str:
    failed_required = list(harness.get("failed_required_step_names") or [])
    failed_advisory = list(harness.get("failed_advisory_step_names") or [])
    if failed_required:
        if mode == "public-check" and "public_release_audit" in failed_required:
            return "public_release_blocked"
        return "needs_attention"
    if failed_advisory:
        return "ready_with_advisories"
    if harness.get("dry_run"):
        return "plan_ready"
    return "ready"


def _attention_items_for_harness(*, mode: str, harness: dict[str, Any], probe_public_url: bool = False) -> list[str]:
    items: list[str] = []
    failed_required = list(harness.get("failed_required_step_names") or [])
    failed_advisory = list(harness.get("failed_advisory_step_names") or [])
    if harness.get("dry_run"):
        items.append("Dry-run report only; commands were planned but not executed.")
    if failed_required:
        items.append(f"Required harness failures: {', '.join(failed_required)}.")
    if failed_advisory:
        items.append(f"Advisory harness failures: {', '.join(failed_advisory)}.")
    if mode == "mcp-check" and not probe_public_url:
        items.append("HTTPS endpoint reachability is not proven; current remote doctor scope is configuration readiness.")
    if mode == "mcp-check":
        items.append(
            "MCP smoke validates a synthetic scratch chain only; approved institution runtime visibility still requires "
            "`reg-rag-mcp-index-visibility --forbid-smoke-docs` against the handoff data directory."
        )
    if mode == "public-check" and (harness.get("dry_run") or "public_release_audit" in failed_required):
        items.append("Public release readiness must be decided from a clean fresh clone and executed public release gate.")
    return items


def _next_actions_for_harness(*, mode: str, harness: dict[str, Any], probe_public_url: bool = False) -> list[str]:
    failed_required = list(harness.get("failed_required_step_names") or [])
    failed_advisory = list(harness.get("failed_advisory_step_names") or [])
    if harness.get("dry_run"):
        actions = ["Run Hermes without `--dry-run` when you are ready to execute the planned harness."]
        if mode == "public-check":
            actions.append(
                "After public-branch cleanup, run `reg-rag-fresh-clone-rehearsal --mode public --full` from a clean checkout."
            )
            actions.append(
                "Use `reg-rag-public-release-gate --include-untracked --execute-harness` for the final public gate."
            )
        if mode == "mcp-check" and not probe_public_url:
            actions.append(
                "For a real ChatGPT/Claude HTTPS handoff, rerun the relevant doctor or harness with `--probe-public-url` against the approved endpoint."
            )
        if mode == "mcp-check":
            actions.append(
                "Before connecting institution clients, run `reg-rag-mcp-index-visibility --forbid-smoke-docs --require-indexed --fail-on-issue` against the approved runtime data directory and tenant."
            )
        return actions
    actions: list[str] = []
    if failed_required:
        actions.append(f"Resolve required harness failures: {', '.join(failed_required)}.")
    if "public_release_audit" in failed_required or "public_release_audit" in failed_advisory:
        actions.append("Use `reports/public_release_cleanup_plan*.md` to remove or rewrite public-release blockers.")
        actions.append("Decide the project LICENSE before opening a public GitHub repository.")
    if "mcp_transport_smoke" in failed_required:
        actions.append("Check real MCP stdio server startup and `search`/`fetch` tool calls before client handoff.")
    if not actions and mode == "mcp-check":
        actions.append(
            "Run `reg-rag-mcp-index-visibility --forbid-smoke-docs --require-indexed --fail-on-issue` against the approved runtime data directory and tenant."
        )
        actions.append("Hand off the generated MCP bundle only after approved runtime visibility is clean.")
        if not probe_public_url:
            actions.append(
                "Remote HTTPS deploy readiness is not proven until `reg-rag-mcp-doctor --probe-public-url` passes for the approved endpoint."
            )
    if not actions and mode == "internal-check":
        actions.append("Internal file/private GitHub handoff can proceed after normal owner review.")
    if not actions and mode == "public-check":
        actions.append("Public release gate is clean; perform final fresh-clone verification before publishing.")
        actions.append("Run `reg-rag-fresh-clone-rehearsal --mode public --full` from a clean checkout before publishing.")
    return actions


def _render_step_line(step: dict[str, Any], *, dry_run: bool) -> str:
    name = str(step.get("name") or "unknown")
    required = "required" if step.get("required", True) else "advisory"
    if dry_run:
        status = "planned"
    else:
        status = "passed" if step.get("passed") else "failed"
    elapsed = step.get("elapsed_seconds")
    elapsed_text = f", {elapsed}s" if elapsed is not None else ""
    return f"- `{status}` `{name}` ({required}{elapsed_text})"


def _evidence_artifacts(steps: list[Any], *, project_root: Any = None) -> list[str]:
    artifacts: list[str] = []
    switches = {"--out-json", "--out-md", "--zip-out", "--out-dir"}
    for step in steps:
        if not isinstance(step, dict):
            continue
        command = step.get("command")
        if not isinstance(command, list):
            continue
        for idx, token in enumerate(command):
            if token in switches and idx + 1 < len(command):
                value = _display_path(str(command[idx + 1]), project_root=project_root)
                if value not in artifacts:
                    artifacts.append(value)
    return artifacts


def _display_path(value: str, *, project_root: Any = None) -> str:
    try:
        path = Path(value)
        root = Path(str(project_root)) if project_root else None
        if root and path.is_absolute():
            return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        pass
    return value.replace("\\", "/")
