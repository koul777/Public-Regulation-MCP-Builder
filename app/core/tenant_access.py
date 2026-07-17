from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.core.config import Settings


def tenant_scoped_value_matches(resource_tenant_id: str | None, requester_tenant_id: str | None) -> bool:
    if resource_tenant_id in (None, ""):
        return False
    return resource_tenant_id == requester_tenant_id


def resource_visible_to_tenant(resource: Any, requester_tenant_id: str | None) -> bool:
    return tenant_scoped_value_matches(getattr(resource, "tenant_id", None), requester_tenant_id)


def tenant_storage_key(tenant_id: str | None) -> str:
    raw = str(tenant_id or "default").strip() or "default"
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")
    return key or "default"


def settings_for_tenant(settings: Settings, tenant_id: str | None) -> Settings:
    if not settings.tenant_storage_isolation:
        return settings
    return replace(settings, data_dir=settings.data_dir / "tenants" / tenant_storage_key(tenant_id))
