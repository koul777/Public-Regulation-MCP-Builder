from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.core.api_audit import audit_api_event
from app.core.config import Settings, get_settings
from app.core.security import AuthContext, authenticate_request
from app.core.tenant_access import settings_for_tenant, tenant_directory_key


AUTHORING_API_PREFIX = "/api/authoring"
_SAFE_ERROR_TYPE_RE = re.compile(r"^[a-z0-9_.-]{1,100}$")
_SAFE_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})


def install_authoring_request_handlers(
    api: FastAPI,
    *,
    settings_provider: Callable[[], Settings] = get_settings,
) -> None:
    """Install a content-free validation response for the authoring API only."""

    async def handler(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, RequestValidationError):  # pragma: no cover
            raise exc
        if not is_authoring_api_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        settings = settings_provider()
        _record_rejection(
            settings,
            request.headers,
            status_code=422,
            action="authoring.request.validation",
            detail=f"authoring request validation rejected; errors={len(exc.errors())}",
        )
        return JSONResponse(
            status_code=422,
            content={"detail": _public_validation_errors(exc)},
        )

    api.add_exception_handler(RequestValidationError, handler)


def make_authoring_body_limit_observer(
    *,
    settings_provider: Callable[[], Settings] = get_settings,
):
    """Return an ASGI observer that audits authoring 413s without request data."""

    async def observe(scope: dict[str, Any], status_code: int) -> None:
        path = str(scope.get("path") or "")
        if not is_authoring_api_path(path):
            return
        _record_rejection(
            settings_provider(),
            _scope_headers(scope),
            status_code=status_code,
            action="authoring.request.body_limit",
            detail="authoring request rejected by byte limit",
        )

    return observe


def is_authoring_api_path(path: object) -> bool:
    candidate = str(path or "")
    return candidate == AUTHORING_API_PREFIX or candidate.startswith(
        AUTHORING_API_PREFIX + "/"
    )


def _public_validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    public: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for error in exc.errors():
        raw_type = str(error.get("type") or "validation_error").lower()
        error_type = (
            raw_type if _SAFE_ERROR_TYPE_RE.fullmatch(raw_type) else "validation_error"
        )
        location = "request"
        raw_location = error.get("loc")
        if isinstance(raw_location, (tuple, list)) and raw_location:
            candidate = str(raw_location[0]).lower()
            if candidate in _SAFE_LOCATIONS:
                location = candidate
        key = (location, error_type)
        if key not in seen:
            public.append({"location": location, "error_type": error_type})
            seen.add(key)
    return public or [{"location": "request", "error_type": "validation_error"}]


def _record_rejection(
    settings: Settings,
    headers: Mapping[str, str],
    *,
    status_code: int,
    action: str,
    detail: str,
) -> None:
    try:
        auth = _safe_auth_context(settings, headers)
        scoped_settings = settings_for_tenant(settings, auth.tenant_id)
        audit_api_event(
            scoped_settings,
            auth,
            action=action,
            outcome="failure",
            status_code=status_code,
            resource_type="authoring_request",
            detail=detail,
        )
    except Exception:
        # Rejection must remain fail-closed even when audit storage is degraded.
        return


def _safe_auth_context(
    settings: Settings,
    headers: Mapping[str, str],
) -> AuthContext:
    try:
        return authenticate_request(
            settings,
            authorization=headers.get("authorization"),
            api_key=headers.get("x-api-key"),
            actor=headers.get("x-actor"),
            tenant_id=headers.get("x-tenant-id"),
        )
    except Exception:
        try:
            tenant_id = tenant_directory_key(settings.api_default_tenant_id)
        except ValueError:
            tenant_id = "default"
        return AuthContext(
            actor="unverified-request",
            tenant_id=tenant_id,
            auth_mode="prevalidation",
            role="viewer",
        )


def _scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers") or []:
        try:
            name = bytes(raw_name).decode("latin-1").lower()
            value = bytes(raw_value).decode("latin-1")
        except (TypeError, UnicodeDecodeError):
            continue
        headers[name] = value
    return headers
