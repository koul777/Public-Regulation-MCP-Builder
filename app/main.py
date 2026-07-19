from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes_documents import router as documents_router
from app.api.routes_institutions import router as institutions_router
from app.api.routes_exports import router as exports_router
from app.api.routes_jobs import router as jobs_router
from app.api.routes_rag import router as rag_router
from app.core.api_audit import api_audit_path
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.request_body_limit import JsonRequestBodyLimitMiddleware
from app.core.security import api_auth_credentials_configured


configure_logging()


def _assert_protected_env_storage_posture(settings: Settings) -> None:
    protected_env = settings.app_env.lower() not in {"local", "dev", "development", "test"}
    if protected_env and not settings.tenant_storage_isolation:
        raise RuntimeError("Protected environment requires TENANT_STORAGE_ISOLATION=true.")


_assert_protected_env_storage_posture(Settings())

app = FastAPI(title="PR MCP Builder", version=__version__)
app.add_middleware(
    JsonRequestBodyLimitMiddleware,
    max_body_bytes=Settings().max_json_request_body_mb * 1024 * 1024,
)
app.include_router(documents_router)
app.include_router(institutions_router)
app.include_router(jobs_router)
app.include_router(exports_router)
app.include_router(rag_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    settings = get_settings()
    checks = readiness_checks(settings)
    status_code = 200 if all(check["passed"] for check in checks) else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if status_code == 200 else "not_ready",
            "checks": [_public_readiness_check(check) for check in checks],
        },
    )


def readiness_checks(settings: Settings) -> list[dict[str, object]]:
    protected_env = settings.app_env.lower() not in {"local", "dev", "development", "test"}
    checks = [
        {
            "name": "api_auth_required_for_protected_env",
            "passed": (not protected_env) or bool(settings.api_auth_required),
        },
        {
            "name": "api_auth_token_configured",
            "passed": (not settings.api_auth_required) or _api_auth_credentials_configured(settings),
        },
        {
            "name": "tenant_storage_isolation_enabled_for_protected_env",
            "passed": (not protected_env) or bool(settings.tenant_storage_isolation),
        },
        {
            "name": "api_audit_enabled_for_protected_env",
            "passed": (not protected_env) or bool(settings.api_audit_enabled),
        },
        {
            "name": "json_request_body_limit_positive",
            "passed": settings.max_json_request_body_mb > 0,
        },
        _required_directory_check("data_dir_exists_for_protected_env", settings.data_dir, required=protected_env),
        _writeable_directory_check("data_dir_writeable", settings.data_dir, create_missing=not protected_env),
        _writeable_directory_check("uploads_dir_writeable", settings.uploads_dir),
        _writeable_directory_check("exports_dir_writeable", settings.exports_dir),
    ]
    if settings.api_audit_enabled:
        checks.append(_writeable_directory_check("api_audit_dir_writeable", api_audit_path(settings).parent))
    return checks


def _required_directory_check(name: str, path: Path, *, required: bool) -> dict[str, object]:
    if not required:
        return {"name": name, "passed": True}
    return {"name": name, "passed": path.is_dir(), "path": str(path)}


def _writeable_directory_check(name: str, path: Path, *, create_missing: bool = True) -> dict[str, object]:
    try:
        if create_missing:
            path.mkdir(parents=True, exist_ok=True)
        elif not path.is_dir():
            return {"name": name, "passed": False, "path": str(path), "error": "directory does not exist"}
        probe = path / f".ready-{uuid4().hex}.tmp"
        probe.write_text("ready\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"name": name, "passed": True, "path": str(path)}
    except OSError as exc:
        return {"name": name, "passed": False, "path": str(path), "error": str(exc)}


def _api_auth_credentials_configured(settings: Settings) -> bool:
    try:
        return api_auth_credentials_configured(settings)
    except Exception:
        return False


def _public_readiness_check(check: dict[str, object]) -> dict[str, object]:
    return {
        "name": check.get("name", ""),
        "passed": bool(check.get("passed")),
    }
