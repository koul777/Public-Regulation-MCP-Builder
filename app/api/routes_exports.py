from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

from app.core.api_audit import audit_api_event
from app.core.config import get_settings
from app.core.security import API_READ_ROLES, AuthContext, coerce_auth_context, get_auth_context, require_api_role
from app.core.tenant_access import settings_for_tenant
from app.api.routes_documents import _chunks_visible_to_auth, _has_review_chunk_access, _require_document_access
from app.processors.exporter import Exporter
from app.storage.repository import JsonRepository


router = APIRouter(prefix="/api/documents", tags=["exports"])
CHUNK_EXPORT_EXTENSIONS = frozenset({"jsonl", "csv", "md", "tables.jsonl", "tables.csv"})


@router.get("/{document_id}/export")
def export_document(
    document_id: str,
    format: str,
    auth_context: AuthContext = Depends(get_auth_context),
):
    settings = get_settings()
    auth = coerce_auth_context(auth_context)
    request_settings = settings_for_tenant(settings, auth.tenant_id)
    extension = {
        "jsonl": "jsonl",
        "csv": "csv",
        "markdown": "md",
        "md": "md",
        "tables_jsonl": "tables.jsonl",
        "tables_csv": "tables.csv",
        "quality_json": "quality.json",
        "quality_md": "quality.md",
    }.get(format)
    if extension is None:
        audit_api_event(
            request_settings,
            auth,
            action="document.export",
            outcome="failure",
            status_code=400,
            resource_type="document",
            document_id=document_id,
            export_format=format,
            detail="unsupported export format",
        )
        raise HTTPException(
            status_code=400,
            detail="format must be jsonl, csv, markdown, tables_jsonl, tables_csv, quality_json, or quality_md",
        )

    repository = JsonRepository(request_settings)
    try:
        require_api_role(auth, API_READ_ROLES)
        _require_document_access(repository, document_id, auth)
    except HTTPException as exc:
        audit_api_event(
            request_settings,
            auth,
            action="document.export",
            outcome="failure",
            status_code=exc.status_code,
            resource_type="document",
            document_id=document_id,
            export_format=format,
            detail=str(exc.detail),
        )
        raise

    persisted = request_settings.exports_dir / f"{document_id}.{extension}"
    if persisted.exists() and (extension not in CHUNK_EXPORT_EXTENSIONS or _has_review_chunk_access(auth)):
        media_type = {
            "jsonl": "application/x-ndjson; charset=utf-8",
            "csv": "text/csv; charset=utf-8",
            "md": "text/markdown; charset=utf-8",
            "tables.jsonl": "application/x-ndjson; charset=utf-8",
            "tables.csv": "text/csv; charset=utf-8",
            "quality.json": "application/json; charset=utf-8",
            "quality.md": "text/markdown; charset=utf-8",
        }[extension]
        audit_api_event(
            request_settings,
            auth,
            action="document.export",
            outcome="success",
            status_code=200,
            resource_type="document",
            document_id=document_id,
            export_format=format,
            filename=persisted.name,
        )
        return FileResponse(path=persisted, media_type=media_type, filename=persisted.name)

    chunks = (
        _chunks_visible_to_auth(repository, document_id, repository.get_chunks(document_id), auth)
        if extension in CHUNK_EXPORT_EXTENSIONS
        else []
    )
    if extension in CHUNK_EXPORT_EXTENSIONS and not chunks:
        audit_api_event(
            request_settings,
            auth,
            action="document.export",
            outcome="failure",
            status_code=404,
            resource_type="document",
            document_id=document_id,
            export_format=format,
            detail="no approved chunks found" if not _has_review_chunk_access(auth) else "no chunks found",
        )
        detail = (
            f"No approved chunks found for document: {document_id}"
            if not _has_review_chunk_access(auth)
            else f"No chunks found for document: {document_id}"
        )
        raise HTTPException(status_code=404, detail=detail)

    exporter = Exporter()
    if format == "jsonl":
        content = exporter.to_jsonl(chunks)
        media_type = "application/x-ndjson; charset=utf-8"
        filename = f"{document_id}.jsonl"
    elif format == "csv":
        content = exporter.to_csv(chunks)
        media_type = "text/csv; charset=utf-8"
        filename = f"{document_id}.csv"
    elif format in {"markdown", "md"}:
        content = exporter.to_markdown(chunks)
        media_type = "text/markdown; charset=utf-8"
        filename = f"{document_id}.md"
    elif format == "tables_jsonl":
        content = exporter.to_tables_jsonl(chunks)
        media_type = "application/x-ndjson; charset=utf-8"
        filename = f"{document_id}.tables.jsonl"
    elif format == "tables_csv":
        content = exporter.to_tables_csv(chunks)
        media_type = "text/csv; charset=utf-8"
        filename = f"{document_id}.tables.csv"
    elif format in {"quality_json", "quality_md"}:
        report = repository.get_quality_report(document_id)
        if report is None:
            audit_api_event(
                request_settings,
                auth,
                action="document.export",
                outcome="failure",
                status_code=404,
                resource_type="document",
                document_id=document_id,
                export_format=format,
                detail="no quality report found",
            )
            raise HTTPException(status_code=404, detail=f"No quality report found for document: {document_id}")
        if format == "quality_json":
            content = report.model_dump_json(indent=2)
            media_type = "application/json; charset=utf-8"
            filename = f"{document_id}.quality.json"
        else:
            from app.processors.quality_gate import QualityGate

            content = QualityGate().to_markdown(report)
            media_type = "text/markdown; charset=utf-8"
            filename = f"{document_id}.quality.md"

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    audit_api_event(
        request_settings,
        auth,
        action="document.export",
        outcome="success",
        status_code=200,
        resource_type="document",
        document_id=document_id,
        export_format=format,
        filename=filename,
    )
    return Response(content=content, media_type=media_type, headers=headers)
