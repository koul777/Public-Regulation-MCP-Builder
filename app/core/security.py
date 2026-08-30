from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import re
from typing import Any

from fastapi import Depends, Header, HTTPException

from app.core.api_audit import append_api_audit_record, redact_sensitive_paths
from app.core.config import Settings, get_settings
from app.core.security_primitives import (
    API_READ_ROLES as API_READ_ROLES,
    API_ROLE_ADMIN,
    API_ROLE_OPERATOR as API_ROLE_OPERATOR,
    API_ROLES,
    API_ROLE_VIEWER,
    API_WRITE_ROLES as API_WRITE_ROLES,
    ROLE_SECURITY_LEVELS as ROLE_SECURITY_LEVELS,
    SECURITY_LEVEL_ORDER as SECURITY_LEVEL_ORDER,
    AuthContext,
)
from app.core.tenant_access import CANONICAL_TENANT_ID_PATTERN


MAX_ACTOR_HEADER_CHARS = 200
MAX_TENANT_HEADER_CHARS = 128
LOCAL_APP_ENVS = frozenset({"local", "dev", "development", "test"})

# Security-level clearance policy — the single source of truth shared by every
# endpoint that returns approved chunk content (RAG search and the documents
# chunks route).


@dataclass(frozen=True)
class _TokenIdentity:
    role: str
    actor: str = ""
    actor_is_bound: bool = False
    auth_mode: str = "api_token"
    department_ids: tuple[str, ...] = ()
    tenant_ids: tuple[str, ...] = ()


def authenticate_request(
    settings: Settings,
    *,
    authorization: str | None = None,
    api_key: str | None = None,
    actor: str | None = None,
    tenant_id: str | None = None,
) -> AuthContext:
    actor_value = _validated_identity_value(
        actor,
        label="X-Actor",
        max_chars=MAX_ACTOR_HEADER_CHARS,
        status_code=400,
    )
    tenant_header_value = ""
    if tenant_id is not None:
        tenant_header_value = _validated_tenant_id(
            tenant_id,
            label="X-Tenant-Id",
            status_code=400,
        )
    default_tenant_id = _validated_tenant_id(
        settings.api_default_tenant_id,
        label="API_DEFAULT_TENANT_ID",
        status_code=500,
    )
    tenant_value = tenant_header_value or default_tenant_id
    if not settings.api_auth_required:
        if _is_protected_env(settings):
            raise HTTPException(
                status_code=500,
                detail="API authentication must be enabled in a protected environment.",
            )
        return AuthContext(
            actor=actor_value or "local-anonymous",
            tenant_id=tenant_value,
            auth_mode="local",
            role=API_ROLE_ADMIN,
        )

    configured_tokens, legacy_token = _configured_auth_credentials(
        settings,
        default_tenant_id=default_tenant_id,
    )
    if not legacy_token and not configured_tokens:
        raise HTTPException(
            status_code=500,
            detail="API authentication is required but neither API_AUTH_TOKEN nor API_AUTH_TOKENS is set.",
        )

    supplied = _bearer_token(authorization) or _clean_header(api_key)
    identity = _resolve_api_token(
        supplied,
        configured_tokens=configured_tokens,
        legacy_token=legacy_token,
        default_tenant_id=default_tenant_id,
    )
    if not supplied or identity is None:
        raise HTTPException(status_code=401, detail="Missing or invalid API credentials.")
    if identity.actor:
        if identity.actor_is_bound and actor_value and actor_value != identity.actor:
            raise HTTPException(status_code=403, detail="X-Actor header does not match the authenticated token actor.")
        actor_value = _validated_identity_value(
            identity.actor,
            label="API_AUTH_TOKENS actor",
            max_chars=MAX_ACTOR_HEADER_CHARS,
            status_code=500,
        )
    if not actor_value:
        raise HTTPException(status_code=400, detail="X-Actor header is required when API authentication is enabled.")
    if settings.tenant_storage_isolation and not tenant_header_value:
        raise HTTPException(
            status_code=400,
            detail="X-Tenant-Id header is required when tenant storage isolation is enabled.",
        )
    if tenant_value not in identity.tenant_ids:
        raise HTTPException(
            status_code=403,
            detail="X-Tenant-Id is not allowed for the authenticated token.",
        )
    return AuthContext(
        actor=actor_value,
        tenant_id=tenant_value,
        auth_mode=identity.auth_mode,
        role=identity.role,
        department_ids=identity.department_ids,
    )


def get_auth_context(
    authorization: str | None = Header(default=None),
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    actor: str | None = Header(default=None, alias="X-Actor"),
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    try:
        return authenticate_request(
            settings,
            authorization=authorization,
            api_key=api_key,
            actor=actor,
            tenant_id=tenant_id,
        )
    except HTTPException as exc:
        audit_auth_denial(settings, exc, actor=actor, tenant_id=tenant_id)
        raise


def audit_auth_denial(
    settings: Settings,
    exc: HTTPException,
    *,
    actor: str | None = None,
    tenant_id: str | None = None,
) -> None:
    safe_default_tenant = _safe_audit_header_value(settings.api_default_tenant_id, fallback="default")
    claimed_tenant_value = _safe_audit_header_value(tenant_id, fallback=safe_default_tenant)
    actor_value = _safe_audit_header_value(actor, fallback="unknown")
    record = {
        "actor": actor_value,
        "tenant_id": safe_default_tenant,
        "auth_mode": "denied",
        "action": "auth.denied",
        "outcome": "denied",
        "status_code": exc.status_code,
        "detail": redact_sensitive_paths(str(exc.detail)),
        "claimed_tenant_id": claimed_tenant_value,
    }
    try:
        append_api_audit_record(settings, record)
    except Exception:
        try:
            append_api_audit_record(
                settings,
                {
                    **record,
                    "actor": "unknown",
                    "claimed_tenant_id": "[untrusted-header-redacted]",
                    "detail": "Authentication denied; untrusted header values were redacted before audit fallback.",
                },
            )
        except Exception:
            return


def coerce_auth_context(value: object) -> AuthContext:
    if isinstance(value, AuthContext):
        return value
    return AuthContext(
        actor="local-direct",
        tenant_id="default",
        auth_mode="direct",
        role=API_ROLE_VIEWER,
    )


def require_api_role(auth_context: AuthContext, allowed_roles: set[str] | frozenset[str]) -> AuthContext:
    auth = coerce_auth_context(auth_context)
    role = _normalize_role(auth.role)
    allowed = frozenset(_normalize_role(candidate) for candidate in allowed_roles)
    if role not in allowed:
        allowed_label = ", ".join(sorted(allowed))
        raise HTTPException(status_code=403, detail=f"API role '{role}' is not allowed. Required role: {allowed_label}.")
    return auth


def api_auth_credentials_configured(settings: Settings) -> bool:
    default_tenant_id = _validated_tenant_id(
        settings.api_default_tenant_id,
        label="API_DEFAULT_TENANT_ID",
        status_code=500,
    )
    configured_tokens, legacy_token = _configured_auth_credentials(
        settings,
        default_tenant_id=default_tenant_id,
    )
    return bool(legacy_token or configured_tokens)


def representative_api_auth_credentials(settings: Settings) -> tuple[str, str]:
    default_tenant_id = _validated_tenant_id(
        settings.api_default_tenant_id,
        label="API_DEFAULT_TENANT_ID",
        status_code=500,
    )
    configured_tokens, legacy_token = _configured_auth_credentials(
        settings,
        default_tenant_id=default_tenant_id,
    )
    if legacy_token:
        return legacy_token, "private-release-readiness"
    for token, identity in configured_tokens.items():
        return token, identity.actor or "private-release-readiness"
    return "", "private-release-readiness"


def _bearer_token(authorization: str | None) -> str:
    value = _clean_header(authorization)
    if not value:
        return ""
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _clean_header(value: str | None) -> str:
    return str(value or "").strip()


def _validated_identity_value(
    value: str | None,
    *,
    label: str,
    max_chars: int,
    status_code: int,
) -> str:
    raw = str(value or "")
    cleaned = raw.strip()
    if len(cleaned) > max_chars:
        raise HTTPException(status_code=status_code, detail=f"{label} exceeds {max_chars} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise HTTPException(status_code=status_code, detail=f"{label} contains control characters.")
    return cleaned


def _resolve_api_token(
    supplied: str,
    *,
    configured_tokens: dict[str, _TokenIdentity],
    legacy_token: str,
    default_tenant_id: str,
) -> _TokenIdentity | None:
    if not supplied:
        return None
    for token, identity in configured_tokens.items():
        if hmac.compare_digest(supplied, token):
            return identity
    if legacy_token and hmac.compare_digest(supplied, legacy_token):
        return _TokenIdentity(
            role=API_ROLE_ADMIN,
            actor="legacy-api-token",
            auth_mode="api_token",
            tenant_ids=(default_tenant_id,),
        )
    return None


def _configured_auth_credentials(
    settings: Settings,
    *,
    default_tenant_id: str,
) -> tuple[dict[str, _TokenIdentity], str]:
    protected_env = _is_protected_env(settings)
    legacy_token = _clean_header(settings.api_auth_token)
    if protected_env and legacy_token:
        raise HTTPException(
            status_code=500,
            detail="API_AUTH_TOKEN is not allowed in a protected environment; use structured API_AUTH_TOKENS identities.",
        )
    configured_tokens = _configured_api_tokens(
        settings.api_auth_tokens,
        default_tenant_id=default_tenant_id,
        protected_env=protected_env,
    )
    return configured_tokens, legacy_token


def _configured_api_tokens(
    raw_value: str,
    *,
    default_tenant_id: str,
    protected_env: bool,
) -> dict[str, _TokenIdentity]:
    raw = str(raw_value or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="API_AUTH_TOKENS must be a JSON object.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="API_AUTH_TOKENS must be a JSON object.")

    identities: dict[str, _TokenIdentity] = {}
    for token, spec in payload.items():
        token_value = str(token or "").strip()
        if not token_value:
            raise HTTPException(status_code=500, detail="API_AUTH_TOKENS contains an empty token key.")
        identity = _parse_token_identity(
            spec,
            default_tenant_id=default_tenant_id,
            protected_env=protected_env,
        )
        identities[token_value] = identity
    return identities


def _parse_token_identity(
    spec: Any,
    *,
    default_tenant_id: str,
    protected_env: bool,
) -> _TokenIdentity:
    if isinstance(spec, str):
        if protected_env:
            raise HTTPException(
                status_code=500,
                detail="Protected environments require structured API_AUTH_TOKENS identities with actor and tenant_id(s).",
            )
        return _TokenIdentity(
            role=_normalize_role(spec),
            actor="configured-api-token",
            auth_mode="api_token_rbac",
            tenant_ids=(default_tenant_id,),
        )
    if isinstance(spec, dict):
        role = _normalize_role(str(spec.get("role", "")).strip())
        actor_value = spec.get("actor")
        if actor_value is not None and not isinstance(actor_value, str):
            raise HTTPException(status_code=500, detail="API_AUTH_TOKENS actor must be a string.")
        actor = _validated_identity_value(
            actor_value,
            label="API_AUTH_TOKENS actor",
            max_chars=MAX_ACTOR_HEADER_CHARS,
            status_code=500,
        )
        if protected_env and not actor:
            raise HTTPException(
                status_code=500,
                detail="Protected API_AUTH_TOKENS identities require a nonempty actor.",
            )
        actor_is_bound = bool(actor)
        if not actor:
            actor = "configured-api-token"
        tenant_ids = _parse_token_tenant_ids(
            spec,
            default_tenant_id=default_tenant_id,
            protected_env=protected_env,
        )
        department_ids = _parse_department_ids(spec.get("department_ids") or spec.get("departments"))
        return _TokenIdentity(
            role=role,
            actor=actor,
            actor_is_bound=actor_is_bound,
            auth_mode="api_token_rbac",
            department_ids=department_ids,
            tenant_ids=tenant_ids,
        )
    raise HTTPException(
        status_code=500,
        detail="API_AUTH_TOKENS values must be role strings or objects with role/actor fields.",
    )


def _parse_token_tenant_ids(
    spec: dict[str, Any],
    *,
    default_tenant_id: str,
    protected_env: bool,
) -> tuple[str, ...]:
    has_tenant_id = "tenant_id" in spec
    has_tenant_ids = "tenant_ids" in spec
    if has_tenant_id and has_tenant_ids:
        raise HTTPException(
            status_code=500,
            detail="API_AUTH_TOKENS identity must not set both tenant_id and tenant_ids.",
        )
    if has_tenant_id:
        tenant_id = spec.get("tenant_id")
        if not isinstance(tenant_id, str):
            raise HTTPException(status_code=500, detail="API_AUTH_TOKENS tenant_id must be a string.")
        return (
            _validated_tenant_id(
                tenant_id,
                label="API_AUTH_TOKENS tenant_id",
                status_code=500,
            ),
        )
    if has_tenant_ids:
        tenant_ids = spec.get("tenant_ids")
        if not isinstance(tenant_ids, list):
            raise HTTPException(status_code=500, detail="API_AUTH_TOKENS tenant_ids must be a JSON array.")
        if not tenant_ids:
            raise HTTPException(status_code=500, detail="API_AUTH_TOKENS tenant_ids must not be empty.")
        parsed = []
        for tenant_id in tenant_ids:
            if not isinstance(tenant_id, str):
                raise HTTPException(
                    status_code=500,
                    detail="API_AUTH_TOKENS tenant_ids entries must be strings.",
                )
            parsed.append(
                _validated_tenant_id(
                    tenant_id,
                    label="API_AUTH_TOKENS tenant_ids entry",
                    status_code=500,
                )
            )
        return tuple(dict.fromkeys(parsed))
    if protected_env:
        raise HTTPException(
            status_code=500,
            detail="Protected API_AUTH_TOKENS identities require tenant_id or tenant_ids.",
        )
    return (default_tenant_id,)


def _validated_tenant_id(value: str | None, *, label: str, status_code: int) -> str:
    raw = str(value or "")
    cleaned = _validated_identity_value(
        value,
        label=label,
        max_chars=MAX_TENANT_HEADER_CHARS,
        status_code=status_code,
    )
    if not cleaned:
        raise HTTPException(status_code=status_code, detail=f"{label} must not be empty.")
    if raw != cleaned or not CANONICAL_TENANT_ID_PATTERN.fullmatch(cleaned):
        raise HTTPException(
            status_code=status_code,
            detail=f"{label} must be a canonical lowercase tenant ID.",
        )
    return cleaned


def _is_protected_env(settings: Settings) -> bool:
    return str(settings.app_env or "").strip().lower() not in LOCAL_APP_ENVS


def _normalize_role(role: str) -> str:
    normalized = str(role or "").strip().lower()
    if normalized not in API_ROLES:
        allowed = ", ".join(sorted(API_ROLES))
        raise HTTPException(status_code=500, detail=f"API role must be one of: {allowed}.")
    return normalized


def _parse_department_ids(value: Any) -> tuple[str, ...]:
    return tuple(normalize_department_ids(value))


def normalize_department_ids(value: Any) -> tuple[str, ...]:
    departments = []
    raw_items = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    for item in raw_items:
        cleaned = normalize_department_id(item)
        if cleaned:
            departments.append(cleaned)
    return tuple(dict.fromkeys(departments))


def normalize_department_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-").lower()


def _safe_audit_header_value(value: str | None, *, fallback: str) -> str:
    cleaned = _clean_header(value)
    if not cleaned:
        return fallback
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        return "[untrusted-header-redacted]"
    redacted = redact_sensitive_paths(cleaned)
    if redacted != cleaned or _looks_like_local_path_header(cleaned):
        return "[local-path-redacted]"
    return redacted[:200]


def _looks_like_local_path_header(value: str) -> bool:
    normalized = value.strip()
    lowered = normalized.lower()
    return bool(
        re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]", normalized)
        or normalized.startswith("\\\\")
        or lowered.startswith(("/users/", "/home/", "/var/", "/tmp/", "/mnt/", "/workspace/", "/data/", "/app/"))
    )
