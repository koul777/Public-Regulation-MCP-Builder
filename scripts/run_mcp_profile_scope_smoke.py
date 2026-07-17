"""Exercise MCP profile isolation with two institution profiles in one tenant."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.api import routes_documents
from app.core.config import Settings
from app.core.security import AuthContext
from app.core.tenant_access import settings_for_tenant
from app.mcp_server.regulation_tools import (
    get_regulation_history,
    list_documents,
    mcp_auth_context,
    search_regulations,
)
from app.storage.repository import JsonRepository
from scripts.run_mcp_smoke import (
    _has_non_smoke_runtime_data,
    _save_smoke_document,
    _write_smoke_approval_evidence,
)


def _prepare_profile(
    *,
    base_settings: Settings,
    runtime_settings: Settings,
    tenant_id: str,
    profile_id: str,
    suffix: str,
) -> dict[str, str]:
    regulation_id = f"reg_scope_{suffix}"
    v1 = f"doc_scope_{suffix}_v1"
    v2 = f"doc_scope_{suffix}_v2"
    _save_smoke_document(
        runtime_settings,
        document_id=v1,
        tenant_id=tenant_id,
        profile_id=profile_id,
        regulation_id=regulation_id,
        regulation_version="1.0",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
        article_text=f"Article 1: {profile_id} process version one.",
        table_text=f"Profile | {profile_id}\nVersion | 1.0",
    )
    _save_smoke_document(
        runtime_settings,
        document_id=v2,
        tenant_id=tenant_id,
        profile_id=profile_id,
        regulation_id=regulation_id,
        regulation_version="2.0",
        effective_from="2026-01-01",
        supersedes_document_id=v1,
        article_text=f"Article 2: {profile_id} process version two.",
        table_text=f"Profile | {profile_id}\nVersion | 2.0",
    )
    auth = AuthContext(actor="mcp-profile-scope-smoke", tenant_id=tenant_id, auth_mode="api_token", role="admin")
    with patch.object(routes_documents, "get_settings", return_value=base_settings):
        for document_id, approval_id in ((v1, f"approval-{v1}"), (v2, f"approval-{v2}")):
            chunks = JsonRepository(runtime_settings).get_chunks(document_id)
            evidence = _write_smoke_approval_evidence(
                base_settings,
                runtime_settings=runtime_settings,
                tenant_id=tenant_id,
                document_id=document_id,
                chunks=chunks,
            )
            routes_documents.approve_review_chunks(
                document_id,
                routes_documents.ApprovalRequest(
                    chunk_ids=[f"{document_id}-article", f"{document_id}-table"],
                    approval_id=approval_id,
                    security_level="internal",
                    **evidence,
                ),
                auth,
            )
            routes_documents.index_document(
                document_id,
                routes_documents.IndexRequest(target_type="local-jsonl", embedding_dimensions=8),
                auth,
            )
    return {
        "profile_id": profile_id,
        "regulation_id": regulation_id,
        "current_document_id": v2,
        "document_prefix": f"doc_scope_{suffix}_",
    }


def run_profile_scope_smoke(
    *,
    data_dir: Path,
    tenant_id: str,
    profile_ids: tuple[str, str],
    allow_persistent_smoke_data: bool = False,
    allow_existing_data: bool = False,
) -> dict[str, Any]:
    if not allow_persistent_smoke_data:
        raise ValueError(
            "Refusing to write synthetic profile-scope documents into an explicit data directory. "
            "Pass --allow-persistent-smoke-data only for a disposable runtime."
        )
    base_settings = Settings(data_dir=data_dir, artifact_root=data_dir.parent, tenant_storage_isolation=True)
    runtime_settings = settings_for_tenant(base_settings, tenant_id)
    if not allow_existing_data and _has_non_smoke_runtime_data(runtime_settings):
        raise ValueError(
            "Refusing to write synthetic profile-scope documents into an existing non-smoke runtime. "
            "Use a disposable runtime or pass --allow-existing-data explicitly."
        )
    prepared = [
        _prepare_profile(
            base_settings=base_settings,
            runtime_settings=runtime_settings,
            tenant_id=tenant_id,
            profile_id=profile_id,
            suffix=str(index),
        )
        for index, profile_id in enumerate(profile_ids, start=1)
    ]
    auth_by_profile = {profile_id: mcp_auth_context(tenant_id=tenant_id) for profile_id in profile_ids}
    checks: list[dict[str, Any]] = []
    for profile_id, expected in zip(profile_ids, prepared):
        auth = auth_by_profile[profile_id]
        search = search_regulations(
            settings=runtime_settings,
            auth=auth,
            query="Article",
            profile_id=profile_id,
            security_levels=["internal"],
        )
        result_profiles = sorted(
            {
                str((result.get("metadata") or {}).get("profile_id") or "")
                for result in search.get("results", [])
            }
        )
        result_documents = sorted(
            {
                str((result.get("metadata") or {}).get("document_id") or "")
                for result in search.get("results", [])
            }
        )
        documents = list_documents(
            settings=runtime_settings,
            auth=auth,
            profile_id=profile_id,
            security_levels=["internal"],
        )
        document_ids = sorted(str(item.get("document_id") or "") for item in documents.get("documents", []))
        history = get_regulation_history(
            settings=runtime_settings,
            auth=auth,
            regulation_id=expected["regulation_id"],
            profile_id=profile_id,
        )
        history_profiles = sorted(
            {
                str(item.get("profile_id") or "")
                for item in history.get("versions", [])
                if isinstance(item, dict)
            }
        )
        checks.append(
            {
                "profile_id": profile_id,
                "search_result_count": len(search.get("results", [])),
                "search_profiles": result_profiles,
                "search_document_ids": result_documents,
                "list_document_ids": document_ids,
                "history_profiles": history_profiles,
                "history_current_document_id": history.get("current_document_id"),
                "passed": (
                    bool(search.get("results"))
                    and result_profiles == [profile_id]
                    and all(document_id.startswith(expected["document_prefix"]) for document_id in result_documents)
                    and all(document_id.startswith(expected["document_prefix"]) for document_id in document_ids)
                    and history_profiles == [profile_id]
                    and history.get("current_document_id") == expected["current_document_id"]
                ),
            }
        )
    report = {
        "report_type": "mcp_profile_scope_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "profile_ids": list(profile_ids),
        "tenant_storage_isolation": True,
        "checks": checks,
        "passed": all(bool(check.get("passed")) for check in checks),
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a two-profile MCP isolation smoke in one tenant.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--profile-id", action="append", required=True)
    parser.add_argument("--allow-persistent-smoke-data", action="store_true")
    parser.add_argument("--allow-existing-data", action="store_true")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if len(args.profile_id) != 2 or len({str(value).strip() for value in args.profile_id}) != 2:
        raise SystemExit("exactly two distinct --profile-id values are required")
    report = run_profile_scope_smoke(
        data_dir=Path(args.data_dir),
        tenant_id=args.tenant_id,
        profile_ids=(str(args.profile_id[0]).strip(), str(args.profile_id[1]).strip()),
        allow_persistent_smoke_data=args.allow_persistent_smoke_data,
        allow_existing_data=args.allow_existing_data,
    )
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if args.fail_on_issue and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
