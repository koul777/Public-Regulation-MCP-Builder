from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import re
from typing import Any

from fastapi import Depends, Header, HTTPException

from app.core.api_audit import append_api_audit_record, redact_sensitive_paths
from app.core.config import Settings, get_settings


API_ROLE_ADMIN = "admin"
API_ROLE_OPERATOR = "operator"
API_ROLE_VIEWER = "viewer"
API_ROLES = {API_ROLE_ADMIN, API_ROLE_OPERATOR, API_ROLE_VIEWER}
API_READ_ROLES = frozenset(API_ROLES)
API_WRITE_ROLES = frozenset({API_ROLE_ADMIN, API_ROLE_OPERATOR})
MAX_ACTOR_HEADER_CHARS = 200
MAX_TENANT_HEADER_CHARS = 128


@dataclass(frozen=True)
class AuthContext:
    actor: str
    tenant_id: str
    auth_mode: str
    role: str = API_ROLE_ADMIN
    department_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _TokenIdentity:
    role: str
    actor: str = ""
    auth_mode: str = "api_token"
    department_ids: tuple[str, ...] = ()


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
    tenant_header_value = _validated_identity_value(
        tenant_id,
        label="X-Tenant-Id",
        max_chars=MAX_TENANT_HEADER_CHARS,
        status_code=400,
    )
    tenant_value = tenant_header_value or _validated_identity_value(
        settings.api_default_tenant_id,
        label="API_DEFAULT_TENANT_ID",
        max_chars=MAX_TENANT_HEADER_CHARS,
        status_code=500,
    )
    if not settings.api_auth_required:
        return AuthContext(
            actor=actor_value or "local-anonymous",
            tenant_id=tenant_value,
            auth_mode="local",
            role=API_ROLE_ADMIN,
        )

    if not settings.api_auth_token and not _clean_header(settings.api_auth_tokens):
        raise HTTPException(
            status_code=500,
            detail="API authentication is required but neither API_AUTH_TOKEN nor API_AUTH_TOKENS is set.",
        )

    supplied = _bearer_token(authorization) or _clean_header(api_key)
    identity = _resolve_api_token(settings, supplied)
    if not supplied or identity is None:
        raise HTTPException(status_code=401, detail="Missing or invalid API credentials.")
    if identity.actor:
        if actor_value and actor_value != identity.actor:
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
    return AuthContext(actor="local-direct", tenant_id="default", auth_mode="direct", role=API_ROLE_ADMIN)


def require_api_role(auth_context: AuthContext, allowed_roles: set[str] | frozenset[str]) -> AuthContext:
    auth = coerce_auth_context(auth_context)
    role = _normalize_role(auth.role)
    allowed = frozenset(_normalize_role(candidate) for candidate in allowed_roles)
    if role not in allowed:
        allowed_label = ", ".join(sorted(allowed))
        raise HTTPException(status_code=403, detail=f"API role '{role}' is not allowed. Required role: {allowed_label}.")
    return auth


def api_auth_credentials_configured(settings: Settings) -> bool:
    configured_tokens = _configured_api_tokens(settings.api_auth_tokens)
    return bool(_clean_header(settings.api_auth_token) or configured_tokens)


def representative_api_auth_credentials(settings: Settings) -> tuple[str, str]:
    configured_tokens = _configured_api_tokens(settings.api_auth_tokens)
    legacy_token = _clean_header(settings.api_auth_token)
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
    cleaned = _clean_header(value)
    if len(cleaned) > max_chars:
        raise HTTPException(status_code=status_code, detail=f"{label} exceeds {max_chars} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise HTTPException(status_code=status_code, detail=f"{label} contains control characters.")
    return cleaned


def _resolve_api_token(settings: Settings, supplied: str) -> _TokenIdentity | None:
    if not supplied:
        return None
    for token, identity in _configured_api_tokens(settings.api_auth_tokens).items():
        if hmac.compare_digest(supplied, token):
            return identity
    expected = settings.api_auth_token
    if expected and hmac.compare_digest(supplied, expected):
        return _TokenIdentity(role=API_ROLE_ADMIN, auth_mode="api_token")
    return None


def _configured_api_tokens(raw_value: str) -> dict[str, _TokenIdentity]:
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
        identity = _parse_token_identity(spec)
        identities[token_value] = identity
    return identities


def _parse_token_identity(spec: Any) -> _TokenIdentity:
    if isinstance(spec, str):
        return _TokenIdentity(role=_normalize_role(spec), auth_mode="api_token_rbac")
    if isinstance(spec, dict):
        role = _normalize_role(str(spec.get("role", "")).strip())
        actor = _clean_header(spec.get("actor"))
        department_ids = _parse_department_ids(spec.get("department_ids") or spec.get("departments"))
        return _TokenIdentity(
            role=role,
            actor=actor,
            auth_mode="api_token_rbac",
            department_ids=department_ids,
        )
    raise HTTPException(
        status_code=500,
        detail="API_AUTH_TOKENS values must be role strings or objects with role/actor fields.",
    )


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
