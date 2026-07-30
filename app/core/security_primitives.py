from __future__ import annotations

from dataclasses import dataclass


API_ROLE_ADMIN = "admin"
API_ROLE_OPERATOR = "operator"
API_ROLE_VIEWER = "viewer"
API_ROLES = {API_ROLE_ADMIN, API_ROLE_OPERATOR, API_ROLE_VIEWER}
API_READ_ROLES = frozenset(API_ROLES)
API_WRITE_ROLES = frozenset({API_ROLE_ADMIN, API_ROLE_OPERATOR})

# Security-level clearance policy is the shared source of truth for approved
# chunk access decisions across API and MCP surfaces.
SECURITY_LEVEL_ORDER = ("public", "internal", "sensitive", "confidential")
ROLE_SECURITY_LEVELS = {
    API_ROLE_ADMIN: frozenset(SECURITY_LEVEL_ORDER),
    API_ROLE_OPERATOR: frozenset({"public", "internal", "sensitive"}),
    API_ROLE_VIEWER: frozenset({"public"}),
}


@dataclass(frozen=True)
class AuthContext:
    actor: str
    tenant_id: str
    auth_mode: str
    role: str = API_ROLE_ADMIN
    department_ids: tuple[str, ...] = ()
