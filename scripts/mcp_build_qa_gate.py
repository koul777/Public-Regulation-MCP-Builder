from __future__ import annotations

"""Final-build QA gate for generated MCP bundles.

Rules that live only in documents get skipped or rewritten by agents, so the
MCP design rules are enforced here in code:

- tool surface: tool count, estimated tool-description tokens, parameter
  counts, read-only annotations and bounded inputs for every shipped profile;
- tool chaining: every required ID parameter names the tool that produces it,
  every tool appears in the server instructions, and the built runtime really
  answers the catalog, table-of-contents, article, search and fetch chain;
- public output: tool responses carry no local paths or secret-like values;
- Korean polish (윤문): shipped guides contain none of the phrases the team
  always rewrites;
- optional small-model check: a local Ollama model must chain the tools
  in-process (see ``evaluate_mcp_tool_chaining``).

Blockers fail the build. Values over the design targets are reported as
``target`` findings and block only with ``require_targets``. The report holds
counts, tool names and finding codes only: no paths, identifiers, queries or
regulation text, so it can be shown in the builder UI.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.korean_polish_gate import check_files as check_polish_files
from scripts.korean_polish_gate import check_text as check_polish_text
from scripts.mcp_bundle_contract import SETUP_BUNDLE_FILES


SCHEMA_VERSION = "mcp-build-qa-gate-v1"
TOOL_PROFILES = ("full", "chatgpt-data")

# Design targets from the tool-chaining review: an e4b-class local model must
# still pick and chain the tools, so the surface has to stay small.
TARGETS: Mapping[str, int] = {
    "max_tools": 12,
    "max_surface_tokens": 4_000,
    "max_params_per_tool": 6,
    "max_description_chars": 400,
    "max_response_tokens": 3_000,
}
# Ratchet ceilings for the surface shipped today. A build may not grow past
# them. Lower them when tools are consolidated; never raise them to pass.
# The token ceilings leave about 70 tokens for the institution scope line,
# whose length follows the bound profile_id.
RATCHET: Mapping[str, Mapping[str, int]] = {
    "full": {"max_tools": 16, "max_surface_tokens": 4_650},
    "chatgpt-data": {"max_tools": 7, "max_surface_tokens": 1_300},
}
MIN_DESCRIPTION_CHARS = 40
MAX_DESCRIPTION_CHARS = 1_200
MAX_REQUIRED_PARAMS = 3
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FOREIGN_PROFILE_PROBE = "qa-gate-foreign-profile"
DEFAULT_CHAIN_SAMPLE_SIZE = 3

_LOCAL_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("windows-drive-path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]")),
    ("unc-path", re.compile(r"\\\\[A-Za-z0-9._$-]+\\")),
    ("posix-home-path", re.compile(r"(?:^|[\s\"'(=])/(?:home|Users|root)/[^\s]")),
    ("file-uri", re.compile(r"(?i)\bfile://")),
)
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
# The chatgpt-data profile is the externally shared contract: local-only
# govreg:// locators must not reach it.
_EXTERNAL_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("local-govreg-uri", re.compile(r"(?i)\bgovreg://")),
)

ToolCaller = Callable[[str, dict[str, Any]], Any]


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate: ASCII at 4 chars/token, other chars 1 each.

    Real counts depend on the client tokenizer; the live model check records
    Ollama's prompt_eval_count instead.
    """
    value = str(text or "")
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    return math.ceil(ascii_chars / 4 + (len(value) - ascii_chars))


def _finding(code: str, severity: str, *, profile: str | None = None, tool: str | None = None, **detail: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {"code": code, "severity": severity}
    if profile is not None:
        finding["profile"] = profile
    if tool is not None:
        finding["tool"] = tool
    finding.update(detail)
    return finding


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


# ---------------------------------------------------------------------------
# Tool surface


def collect_tool_surface(server: Any) -> dict[str, Any]:
    """Read what an MCP client sends to its model: names, descriptions, input schemas."""
    tools = asyncio.run(server.list_tools())
    payload: list[dict[str, Any]] = []
    for tool in tools:
        dumped = tool.model_dump(mode="json", exclude_none=True)
        payload.append(
            {
                "name": dumped.get("name", ""),
                "description": dumped.get("description", "") or "",
                "inputSchema": dumped.get("inputSchema") or {},
                "annotations": dumped.get("annotations") or {},
            }
        )
    return {"tools": payload, "instructions": str(getattr(server, "instructions", "") or "")}


def _model_visible_tools(surface: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": tool["name"], "description": tool["description"], "inputSchema": tool["inputSchema"]}
        for tool in surface["tools"]
    ]


def _branches(schema: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        return [item for item in variants if isinstance(item, Mapping) and item.get("type") != "null"]
    return [schema]


def _has_nested_object(schema: Mapping[str, Any]) -> bool:
    for branch in _branches(schema):
        if branch.get("type") == "object":
            return True
        items = branch.get("items")
        if branch.get("type") == "array" and isinstance(items, Mapping) and _has_nested_object(items):
            return True
    return False


def _unbounded_reasons(schema: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for branch in _branches(schema):
        if "enum" in branch or "const" in branch:
            continue
        kind = branch.get("type")
        if kind == "string" and "maxLength" not in branch:
            reasons.append("string-without-maxLength")
        elif kind in {"integer", "number"} and "maximum" not in branch and "exclusiveMaximum" not in branch:
            reasons.append("number-without-maximum")
        elif kind == "array":
            if "maxItems" not in branch:
                reasons.append("array-without-maxItems")
            items = branch.get("items")
            if isinstance(items, Mapping):
                reasons.extend(_unbounded_reasons(items))
        elif kind is None and "$ref" not in branch:
            reasons.append("untyped-input")
    return reasons


def _is_identifier_param(name: str) -> bool:
    return name == "id" or name.endswith("_id")


def _mentions(text: str, tool_name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(tool_name)}(?![A-Za-z0-9_])", text) is not None


def check_tool_surface(profile: str, surface: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tools = list(surface["tools"])
    instructions = str(surface.get("instructions") or "")
    names = [str(tool["name"]) for tool in tools]
    findings: list[dict[str, Any]] = []
    visible_json = _compact_json(_model_visible_tools(surface))
    tool_tokens = estimate_tokens(visible_json)
    instruction_tokens = estimate_tokens(instructions)
    surface_tokens = tool_tokens + instruction_tokens
    ratchet = RATCHET.get(profile, {})

    if len(tools) > ratchet.get("max_tools", TARGETS["max_tools"]):
        findings.append(
            _finding("tool-count-above-ratchet", "blocker", profile=profile, value=len(tools), limit=ratchet.get("max_tools"))
        )
    if len(tools) > TARGETS["max_tools"]:
        findings.append(
            _finding("tool-count-above-target", "target", profile=profile, value=len(tools), limit=TARGETS["max_tools"])
        )
    if surface_tokens > ratchet.get("max_surface_tokens", TARGETS["max_surface_tokens"]):
        findings.append(
            _finding(
                "tool-surface-tokens-above-ratchet",
                "blocker",
                profile=profile,
                value=surface_tokens,
                limit=ratchet.get("max_surface_tokens"),
            )
        )
    if surface_tokens > TARGETS["max_surface_tokens"]:
        findings.append(
            _finding(
                "tool-surface-tokens-above-target",
                "target",
                profile=profile,
                value=surface_tokens,
                limit=TARGETS["max_surface_tokens"],
            )
        )

    entry_points: list[str] = []
    tool_rows: list[dict[str, Any]] = []
    for tool in tools:
        name = str(tool["name"])
        description = str(tool["description"] or "")
        schema = tool["inputSchema"] if isinstance(tool["inputSchema"], Mapping) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        required = [str(item) for item in schema.get("required") or []]
        required_ids = [param for param in required if _is_identifier_param(param)]
        if not required_ids:
            entry_points.append(name)
        tool_rows.append(
            {
                "name": name,
                "description_chars": len(description),
                "param_count": len(properties),
                "required_count": len(required),
            }
        )

        if not TOOL_NAME_RE.match(name):
            findings.append(_finding("tool-name-not-portable", "blocker", profile=profile, tool=name))
        if len(description) < MIN_DESCRIPTION_CHARS:
            findings.append(
                _finding(
                    "tool-description-too-thin",
                    "blocker",
                    profile=profile,
                    tool=name,
                    value=len(description),
                    limit=MIN_DESCRIPTION_CHARS,
                )
            )
        if len(description) > MAX_DESCRIPTION_CHARS:
            findings.append(
                _finding(
                    "tool-description-too-long",
                    "blocker",
                    profile=profile,
                    tool=name,
                    value=len(description),
                    limit=MAX_DESCRIPTION_CHARS,
                )
            )
        elif len(description) > TARGETS["max_description_chars"]:
            findings.append(
                _finding(
                    "tool-description-above-target",
                    "target",
                    profile=profile,
                    tool=name,
                    value=len(description),
                    limit=TARGETS["max_description_chars"],
                )
            )
        if len(required) > MAX_REQUIRED_PARAMS:
            findings.append(
                _finding(
                    "tool-required-params-excessive",
                    "blocker",
                    profile=profile,
                    tool=name,
                    value=len(required),
                    limit=MAX_REQUIRED_PARAMS,
                )
            )
        if len(properties) > TARGETS["max_params_per_tool"]:
            findings.append(
                _finding(
                    "tool-params-above-target",
                    "target",
                    profile=profile,
                    tool=name,
                    value=len(properties),
                    limit=TARGETS["max_params_per_tool"],
                )
            )
        if required_ids and not any(_mentions(description, other) for other in names if other != name):
            findings.append(
                _finding(
                    "tool-chain-producer-missing",
                    "blocker",
                    profile=profile,
                    tool=name,
                    params=required_ids,
                )
            )
        if not _mentions(instructions, name):
            findings.append(_finding("tool-missing-from-instructions", "blocker", profile=profile, tool=name))
        annotations = tool.get("annotations") or {}
        if annotations.get("readOnlyHint") is not True or annotations.get("destructiveHint") is not False:
            findings.append(_finding("tool-not-read-only", "blocker", profile=profile, tool=name))
        # Small models and strict function-calling APIs handle flat scalar and
        # list inputs; references and nested objects break argument filling.
        schema_text = _compact_json(schema)
        if (
            "$ref" in schema_text
            or "$defs" in schema_text
            or any(isinstance(item, Mapping) and _has_nested_object(item) for item in properties.values())
        ):
            findings.append(_finding("tool-input-schema-not-flat", "blocker", profile=profile, tool=name))
        for param, param_schema in properties.items():
            if not isinstance(param_schema, Mapping):
                continue
            reasons = sorted(set(_unbounded_reasons(param_schema)))
            if reasons:
                findings.append(
                    _finding(
                        "tool-input-unbounded",
                        "blocker",
                        profile=profile,
                        tool=name,
                        param=str(param),
                        reasons=reasons,
                    )
                )
    if not entry_points:
        findings.append(_finding("tool-entry-point-missing", "blocker", profile=profile))

    summary = {
        "tool_count": len(tools),
        "tool_tokens_estimate": tool_tokens,
        "instructions_tokens_estimate": instruction_tokens,
        "surface_tokens_estimate": surface_tokens,
        "max_param_count": max((row["param_count"] for row in tool_rows), default=0),
        "entry_points": sorted(entry_points),
        "tools": tool_rows,
    }
    return summary, findings


# ---------------------------------------------------------------------------
# Runtime chain smoke


def server_tool_caller(server: Any) -> ToolCaller:
    """Call registered tools in-process with FastMCP argument validation."""
    manager = server._tool_manager

    def call(name: str, arguments: dict[str, Any]) -> Any:
        return _plain(asyncio.run(manager.call_tool(name, dict(arguments), convert_result=False)))

    return call


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_values(item)


def check_public_output(profile: str, tool: str, response: Any) -> list[dict[str, Any]]:
    patterns = (*_LOCAL_PATH_PATTERNS, *_SECRET_PATTERNS)
    if profile == "chatgpt-data":
        patterns = (*patterns, *_EXTERNAL_ONLY_PATTERNS)
    hits: dict[str, int] = {}
    for text in _string_values(response):
        for code, pattern in patterns:
            if pattern.search(text):
                hits[code] = hits.get(code, 0) + 1
    return [
        _finding("public-output-unsafe-value", "blocker", profile=profile, tool=tool, pattern=code, count=count)
        for code, count in sorted(hits.items())
    ]


def _sample_evenly(items: Sequence[Any], count: int) -> list[Any]:
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(index * step)] for index in range(count)]


def _locator_variants(node: Mapping[str, Any]) -> list[tuple[str, str]]:
    number = str(node.get("number") or "").strip()
    label = str(node.get("label") or "").strip()
    title = str(node.get("title") or "").strip()
    variants: list[tuple[str, str]] = []
    if number:
        variants.append(("number", number))
    if label and label != number:
        variants.append(("toc-label", label))
    if number and title:
        variants.append(("citation", f"{number}({title})"))
    digits = re.sub(r"\D", "", number)
    if digits and "의" not in number:
        variants.append(("bare-digits", digits))
    return variants


def run_chain_smoke(
    call: ToolCaller,
    *,
    profile: str,
    profile_bound: bool,
    sample_size: int = DEFAULT_CHAIN_SAMPLE_SIZE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Walk catalog, TOC, article, search and fetch on the built runtime."""
    findings: list[dict[str, Any]] = []
    response_tokens: dict[str, int] = {}
    call_count = 0

    def invoke(name: str, arguments: dict[str, Any]) -> Any:
        nonlocal call_count
        call_count += 1
        try:
            response = call(name, arguments)
        except Exception as exc:  # noqa: BLE001 - a failing tool is a gate finding
            findings.append(
                _finding("chain-call-error", "blocker", profile=profile, tool=name, error_type=type(exc).__name__)
            )
            return None
        tokens = estimate_tokens(_compact_json(response))
        response_tokens[name] = max(response_tokens.get(name, 0), tokens)
        findings.extend(check_public_output(profile, name, response))
        return response

    catalog = invoke("list_regulations", {"page": 1, "page_size": 50}) or {}
    regulations = [item for item in catalog.get("regulations") or [] if isinstance(item, Mapping)]
    samples = _sample_evenly(regulations, sample_size)
    if catalog and not regulations:
        findings.append(_finding("chain-catalog-empty", "blocker", profile=profile))

    articles_checked = 0
    searches_checked = 0
    for regulation in samples:
        unit_id = str(regulation.get("regulation_unit_id") or "")
        title = str(regulation.get("regulation_title") or "").strip()
        if not unit_id:
            findings.append(_finding("chain-catalog-unit-id-missing", "blocker", profile=profile))
            continue
        toc = invoke("get_regulation_toc", {"regulation_unit_id": unit_id}) or {}
        article_nodes = [
            node for node in toc.get("nodes") or [] if isinstance(node, Mapping) and node.get("node_type") == "article"
        ]
        if article_nodes:
            articles_checked += 1
            for variant, article_no in _locator_variants(article_nodes[0]):
                response = invoke("get_regulation_article", {"regulation_unit_id": unit_id, "article_no": article_no})
                if response is not None and not (response.get("articles") or []):
                    findings.append(
                        _finding(
                            "chain-article-empty",
                            "blocker",
                            profile=profile,
                            tool="get_regulation_article",
                            locator_form=variant,
                        )
                    )
        if title:
            searches_checked += 1
            search = invoke("search", {"query": title}) or {}
            results = [item for item in search.get("results") or [] if isinstance(item, Mapping)]
            if search and not results:
                findings.append(_finding("chain-search-empty", "blocker", profile=profile, tool="search"))
            elif results:
                fetched = invoke("fetch", {"id": str(results[0].get("id") or "")}) or {}
                if fetched and not str(fetched.get("text") or fetched.get("verbatim_text") or "").strip():
                    findings.append(_finding("chain-fetch-empty", "blocker", profile=profile, tool="fetch"))
    if samples and not articles_checked:
        findings.append(_finding("chain-toc-without-articles", "blocker", profile=profile))

    profile_binding = "not-applicable"
    if profile == "full" and profile_bound and searches_checked:
        try:
            call("search", {"query": "규정", "profile_id": FOREIGN_PROFILE_PROBE})
        except Exception:  # noqa: BLE001 - refusal is the expected outcome
            profile_binding = "enforced"
        else:
            profile_binding = "not-enforced"
            findings.append(_finding("chain-profile-binding-not-enforced", "blocker", profile=profile, tool="search"))

    for tool, tokens in sorted(response_tokens.items()):
        if tokens > TARGETS["max_response_tokens"]:
            findings.append(
                _finding(
                    "tool-response-tokens-above-target",
                    "target",
                    profile=profile,
                    tool=tool,
                    value=tokens,
                    limit=TARGETS["max_response_tokens"],
                )
            )
    summary = {
        "skipped": False,
        "regulation_count": int(catalog.get("total_count") or len(regulations)) if catalog else 0,
        "sampled_regulations": len(samples),
        "articles_checked": articles_checked,
        "searches_checked": searches_checked,
        "call_count": call_count,
        "max_response_tokens_estimate": dict(sorted(response_tokens.items())),
        "profile_binding": profile_binding,
    }
    return summary, findings


# ---------------------------------------------------------------------------
# Korean polish


def bundle_guide_paths(bundle_dir: str | Path) -> list[Path]:
    root = Path(bundle_dir)
    names = {
        SETUP_BUNDLE_FILES["readme"],
        SETUP_BUNDLE_FILES["readme_ko"],
        SETUP_BUNDLE_FILES["usage_guide"],
    }
    names.update(
        item.name for item in root.glob("*") if item.is_file() and item.suffix.lower() in {".md", ".txt"}
    )
    return [root / name for name in sorted(names)]


def check_polish(bundle_dir: str | Path | None, surfaces: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    checked: list[str] = []
    missing: list[str] = []
    if bundle_dir is not None:
        paths = bundle_guide_paths(bundle_dir)
        existing = [path for path in paths if path.is_file()]
        missing = sorted(path.name for path in paths if not path.is_file())
        report = check_polish_files(existing, honor_opt_out=False)
        issues.extend(report["findings"])  # type: ignore[arg-type]
        checked.extend(report["checked_sources"])  # type: ignore[arg-type]
    for profile, surface in surfaces.items():
        source = f"mcp:{profile}:instructions"
        issues.extend(check_polish_text(str(surface.get("instructions") or ""), source=source))
        checked.append(source)
        for tool in surface["tools"]:
            source = f"mcp:{profile}:{tool['name']}"
            issues.extend(check_polish_text(str(tool.get("description") or ""), source=source))
    findings = [
        _finding(
            "polish-phrase",
            "blocker",
            source=str(issue["source"]),
            line=int(issue["line"]),
            rule=str(issue["rule"]),
            label=str(issue["label"]),
            matches=list(issue["matches"]),
        )
        for issue in issues
    ]
    findings.extend(_finding("polish-guide-missing", "blocker", source=name) for name in missing)
    return {"checked_sources": checked, "issue_count": len(issues), "missing_guides": missing}, findings


# ---------------------------------------------------------------------------
# Gate


def _default_server_factory(**kwargs: Any) -> Any:
    from app.mcp_server.regulation_server import create_regulation_mcp_server

    return create_regulation_mcp_server(**kwargs)


def _read_runtime_scope(data_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((data_dir / "mcp_runtime_manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def run_mcp_build_qa_gate(
    *,
    data_dir: str | Path | None,
    tenant_id: str = "default",
    profile_id: str | None = None,
    bundle_dir: str | Path | None = None,
    tool_profiles: Sequence[str] = TOOL_PROFILES,
    tenant_storage_isolation: bool | None = None,
    chain_sample_size: int = DEFAULT_CHAIN_SAMPLE_SIZE,
    require_targets: bool = False,
    model_eval: Mapping[str, Any] | None = None,
    server_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the final-build QA gate and return an identifier-free report.

    ``data_dir`` is the built runtime (``<bundle>/data``). Without it only the
    source-level checks run, against servers built on an empty temporary dir.
    ``model_eval`` holds ``evaluate_mcp_tool_chaining.evaluate_tool_chaining``
    keyword options; when given, the small-model check must pass too.
    """
    factory = server_factory or _default_server_factory
    findings: list[dict[str, Any]] = []
    profiles: dict[str, Any] = {}
    surfaces: dict[str, Mapping[str, Any]] = {}
    model_report: dict[str, Any] | None = None
    unknown = [profile for profile in tool_profiles if profile not in TOOL_PROFILES]
    if unknown:
        raise ValueError(f"Unknown tool profile: {', '.join(unknown)}")

    with tempfile.TemporaryDirectory(prefix="mcp-qa-gate-") as scratch:
        runtime_dir = Path(data_dir) if data_dir is not None else Path(scratch) / "data"
        for profile in tool_profiles:
            server = factory(
                data_dir=runtime_dir,
                tenant_id=tenant_id,
                profile_id=profile_id,
                tool_profile=profile,
                tenant_storage_isolation=tenant_storage_isolation,
                warm_cache=False,
                background_tokenizer_warmup=False,
                api_audit_enabled=False,
                rag_trace_enabled=False,
            )
            surface = collect_tool_surface(server)
            surfaces[profile] = surface
            surface_summary, surface_findings = check_tool_surface(profile, surface)
            findings.extend(surface_findings)
            if data_dir is not None:
                chain_summary, chain_findings = run_chain_smoke(
                    server_tool_caller(server),
                    profile=profile,
                    profile_bound=bool(str(profile_id or "").strip()),
                    sample_size=chain_sample_size,
                )
                findings.extend(chain_findings)
            else:
                chain_summary = {"skipped": True, "reason": "no-runtime-data"}
            profiles[profile] = {**surface_summary, "chain_smoke": chain_summary}
            if model_eval is not None and profile == str(model_eval.get("tool_profile") or "full"):
                from scripts.evaluate_mcp_tool_chaining import evaluate_tool_chaining

                options = {key: value for key, value in model_eval.items() if key != "tool_profile"}
                model_report = evaluate_tool_chaining(
                    call=server_tool_caller(server),
                    tools=_model_visible_tools(surface),
                    instructions=str(surface.get("instructions") or ""),
                    **options,
                )
                findings.extend(_model_eval_findings(model_report, profile=profile))

    polish_summary, polish_findings = check_polish(bundle_dir, surfaces)
    findings.extend(polish_findings)
    if model_eval is not None and model_report is None:
        findings.append(_finding("model-eval-profile-not-built", "blocker", profile=str(model_eval.get("tool_profile"))))

    target_gaps = [item for item in findings if item["severity"] == "target"]
    if require_targets:
        findings = [{**item, "severity": "blocker"} if item["severity"] == "target" else item for item in findings]
    blockers = [item for item in findings if item["severity"] == "blocker"]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "runtime" if data_dir is not None else "source-only",
        "passed": not blockers,
        "targets_met": not target_gaps,
        "require_targets": require_targets,
        "blocker_count": len(blockers),
        "target_gap_count": len(target_gaps),
        "profiles": profiles,
        "polish": polish_summary,
        "model_eval": model_report,
        "policy": {
            "targets": dict(TARGETS),
            "ratchet": {key: dict(value) for key, value in RATCHET.items()},
            "min_description_chars": MIN_DESCRIPTION_CHARS,
            "max_description_chars": MAX_DESCRIPTION_CHARS,
            "max_required_params": MAX_REQUIRED_PARAMS,
            "token_estimate": "ascii/4 + non-ascii chars",
        },
        "findings": findings,
    }


def _model_eval_findings(report: Mapping[str, Any], *, profile: str) -> list[dict[str, Any]]:
    if not report.get("available"):
        return [_finding("model-eval-unavailable", "blocker", profile=profile, error_type=report.get("error_type"))]
    findings: list[dict[str, Any]] = []
    summary = report.get("summary") or {}
    thresholds = report.get("thresholds") or {}
    checks = (
        ("model-eval-chain-success-below-threshold", "chain_success_rate", "min_chain_success_rate", "min"),
        ("model-eval-false-abstention-above-threshold", "false_abstention_rate", "max_false_abstention_rate", "max"),
        ("model-eval-invalid-calls-above-threshold", "invalid_call_rate", "max_invalid_call_rate", "max"),
        ("model-eval-no-evidence-not-abstained", "no_evidence_abstention_rate", "min_no_evidence_abstention_rate", "min"),
        ("model-eval-prompt-tokens-above-threshold", "max_prompt_tokens", "max_prompt_tokens", "max"),
    )
    for code, metric, threshold_key, direction in checks:
        limit = thresholds.get(threshold_key)
        value = summary.get(metric)
        if limit is None or value is None:
            continue
        if (direction == "min" and value < limit) or (direction == "max" and value > limit):
            findings.append(_finding(code, "blocker", profile=profile, value=value, limit=limit))
    return findings


def describe_findings_ko(report: Mapping[str, Any], *, limit: int = 8) -> list[str]:
    """Short Korean lines for the builder UI; codes stay visible for lookup."""
    lines: list[str] = []
    for item in report.get("findings") or []:
        if item.get("severity") != "blocker":
            continue
        where = " · ".join(str(item[key]) for key in ("profile", "tool", "source") if item.get(key))
        value = f" ({item['value']}/{item['limit']})" if "value" in item and "limit" in item else ""
        lines.append(f"{item['code']}{value}{' · ' + where if where else ''}")
        if len(lines) >= limit:
            break
    return lines


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the MCP final-build QA gate.")
    parser.add_argument("--bundle-dir", help="Generated MCP bundle folder (uses <bundle>/data as runtime).")
    parser.add_argument("--data-dir", help="Runtime data folder when it is not <bundle>/data.")
    parser.add_argument("--tenant-id", help="Tenant ID; defaults to the runtime manifest value.")
    parser.add_argument("--profile-id", help="Institution profile ID; defaults to the runtime manifest value.")
    parser.add_argument("--source-only", action="store_true", help="Check the tool surface without runtime data.")
    parser.add_argument("--tool-profile", action="append", choices=TOOL_PROFILES, help="Profile to check (repeatable).")
    parser.add_argument("--chain-sample-size", type=int, default=DEFAULT_CHAIN_SAMPLE_SIZE)
    parser.add_argument("--require-targets", action="store_true", help="Treat design-target gaps as blockers.")
    parser.add_argument("--model", help="Ollama model for the small-model tool-chaining check, e.g. an e4b-class model.")
    parser.add_argument("--model-tool-profile", choices=TOOL_PROFILES, default="full")
    parser.add_argument("--tool-call-mode", choices=("native", "json"), default="native")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--scenario-json", help="Private scenario spec (questions stay outside the checkout).")
    parser.add_argument("--auto-scenarios", type=int, default=6, help="Questions generated from the built catalog.")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--no-think", action="store_true", help="Send think=false (thinking models such as qwen3).")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--min-chain-success-rate", type=float, default=0.8)
    parser.add_argument("--max-false-abstention-rate", type=float, default=0.1)
    parser.add_argument("--max-invalid-call-rate", type=float, default=0.2)
    parser.add_argument("--min-no-evidence-abstention-rate", type=float, default=1.0)
    parser.add_argument("--max-prompt-tokens", type=int)
    parser.add_argument("--out-json", help="Write the report here (keep it outside the public checkout).")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    bundle_dir = Path(args.bundle_dir) if args.bundle_dir else None
    data_dir: Path | None = None
    if not args.source_only:
        if args.data_dir:
            data_dir = Path(args.data_dir)
        elif bundle_dir is not None:
            data_dir = bundle_dir / "data"
        else:
            print("--bundle-dir, --data-dir 또는 --source-only 중 하나가 필요합니다.", file=sys.stderr)
            return 2
        if not (data_dir / "mcp_runtime_manifest.json").is_file():
            print("런타임 데이터 폴더에 mcp_runtime_manifest.json이 없습니다.", file=sys.stderr)
            return 2
    scope = _read_runtime_scope(data_dir) if data_dir is not None else {}
    model_eval: dict[str, Any] | None = None
    if args.model:
        if data_dir is None:
            print("--model 평가에는 런타임 데이터가 필요합니다.", file=sys.stderr)
            return 2
        model_eval = {
            "tool_profile": args.model_tool_profile,
            "model": args.model,
            "ollama_url": args.ollama_url,
            "mode": args.tool_call_mode,
            "scenario_path": args.scenario_json,
            "auto_scenarios": args.auto_scenarios,
            "max_steps": args.max_steps,
            "num_ctx": args.num_ctx,
            "think": False if args.no_think else None,
            "timeout_seconds": args.timeout_seconds,
            "thresholds": {
                "min_chain_success_rate": args.min_chain_success_rate,
                "max_false_abstention_rate": args.max_false_abstention_rate,
                "max_invalid_call_rate": args.max_invalid_call_rate,
                "min_no_evidence_abstention_rate": args.min_no_evidence_abstention_rate,
                "max_prompt_tokens": args.max_prompt_tokens,
            },
        }
    report = run_mcp_build_qa_gate(
        data_dir=data_dir,
        tenant_id=args.tenant_id or str(scope.get("tenant_id") or "default"),
        profile_id=args.profile_id or (str(scope.get("profile_id") or "") or None),
        bundle_dir=bundle_dir,
        tool_profiles=tuple(args.tool_profile or TOOL_PROFILES),
        chain_sample_size=args.chain_sample_size,
        require_targets=args.require_targets,
        model_eval=model_eval,
    )
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for profile, summary in report["profiles"].items():
        print(
            f"{profile}: 도구 {summary['tool_count']}개 · 도구 설명 약 {summary['surface_tokens_estimate']:,}토큰"
            f" · 체인 확인 {'건너뜀' if summary['chain_smoke'].get('skipped') else str(summary['chain_smoke']['call_count']) + '회 호출'}"
        )
    for item in report["findings"]:
        print(f"[{item['severity']}] {_compact_json(item)}")
    print(
        f"최종 빌드 QA 게이트: {'통과' if report['passed'] else '실패'}"
        f" · 차단 {report['blocker_count']}건 · 목표 미달 {report['target_gap_count']}건"
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
