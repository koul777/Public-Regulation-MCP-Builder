from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from mcp.server.fastmcp import FastMCP

from app.core.input_limits import (
    McpArticleNo,
    McpDepartmentIds,
    McpIdentifier,
    McpOptionalIdentifier,
    McpQuery,
    McpResultId,
    McpSecurityLevels,
    McpTopK,
)
from app.mcp_server.regulation_tools import (
    compare_versions as compare_versions_result,
    fetch_regulation,
    get_article as get_article_result,
    get_document as get_document_result,
    get_citation as get_citation_result,
    get_index_status as get_index_status_result,
    get_regulation_article as get_regulation_article_result,
    get_regulation_history as get_regulation_history_result,
    get_regulation_toc as get_regulation_toc_result,
    get_table as get_table_result,
    list_documents as list_document_results,
    list_regulations as list_regulation_results,
    lookup_regulation,
    mcp_auth_context,
    search_regulations,
    settings_for_mcp_project,
    start_background_tokenizer_warmup,
    warm_mcp_runtime,
)


SERVER_INSTRUCTIONS = """Local public-institution regulation MCP server.

Terminology:
- MCP is the client/server protocol and tool contract.
- This process is the MCP server; it owns the approved-runtime boundary and exposes tools.
- Claude, ChatGPT, or an institution AI is the MCP client; it must not receive raw files or bypass these tools.

Use these tools for questions about an institution's approved regulations, internal rules,
articles, appendices, forms, tables, revision evidence, or citation metadata.

Trust boundary:
- Use approved local regulation records only.
- Do not read raw uploaded files or unapproved preprocessing output.
- Do not infer an answer when returned evidence is missing or ambiguous.
- Cite returned document, article, page, approval id, approved content hash, and profile metadata.

Tool flow:
- Use search first for natural-language regulation questions. Search automatically narrows the institution catalog,
  regulation table of contents, and body evidence instead of scanning every chunk as one flat corpus.
- Use list_regulations to inspect institution-local regulation families and their current/revision versions.
- Use get_regulation_toc before broad exploration of a known regulation.
- Use get_regulation_article for an exact article when regulation_unit_id is known.
- Use lookup when a document ID is known: it performs exact approved document/article lookup first and falls back to RAG only on a miss.
- Use fetch with a returned search id before drafting a final evidence-backed answer.
- Use get_document when the user explicitly asks to load the whole approved document.
- Use get_article for exact document/article lookups.
- Use get_table for table, appendix, or form evidence.
- Use get_regulation_history when the user asks for regulation versions, effective dates, or repeal history.
- Use get_citation to validate citation metadata for a returned result id.
- Use compare_versions only when the user asks about revision or version differences.
- Use get_index_status when the user asks whether the MCP-visible index is ready or complete.

Verbatim evidence rule:
- Search and fetch responses include `verbatim_text` and a `verbatim` evidence block.
- When showing legal or regulation evidence, quote or display `verbatim.text` together with its document,
  chunk, version, effective-date, page, and content-hash metadata; do not present a generated summary as a quote.
- Tables also include a `verbatim` block and structured `rows`; do not fill missing cells from prior knowledge.

If the evidence does not support an answer, state that the approved regulation index does not
contain enough evidence instead of guessing."""

READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
VALID_TOOL_PROFILES = {"full", "chatgpt-data"}


class StaticBearerTokenVerifier:
    def __init__(self, token: str, *, client_id: str = "govreg-mcp-client") -> None:
        self._token = token
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self._token:
            return None
        return AccessToken(token=token, client_id=self._client_id, scopes=["mcp:read"])


def create_regulation_mcp_server(
    *,
    data_dir: str | Path = "data",
    tenant_id: str = "default",
    profile_id: str | None = None,
    actor: str = "mcp-regulation-server",
    role: str = "operator",
    department_ids: list[str] | None = None,
    tenant_storage_isolation: bool | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    http_bearer_token: str | None = None,
    auth_issuer_url: str | None = None,
    allowed_http_hosts: list[str] | None = None,
    allowed_http_origins: list[str] | None = None,
    tool_profile: str = "full",
    warm_cache: bool = True,
) -> FastMCP:
    normalized_tool_profile = tool_profile.strip().lower()
    if normalized_tool_profile not in VALID_TOOL_PROFILES:
        raise ValueError("tool_profile must be full or chatgpt-data.")
    settings = settings_for_mcp_project(
        data_dir=data_dir,
        tenant_id=tenant_id,
        tenant_storage_isolation=tenant_storage_isolation,
    )
    auth = mcp_auth_context(
        tenant_id=tenant_id,
        actor=actor,
        role=role,
        department_ids=department_ids,
    )
    default_profile_id = str(profile_id or "").strip() or None
    auth_settings = None
    token_verifier = None
    if http_bearer_token:
        issuer_url = auth_issuer_url or _default_auth_issuer_url(host=host, port=port)
        auth_settings = AuthSettings(
            issuer_url=issuer_url,
            required_scopes=["mcp:read"],
            resource_server_url=None,
        )
        token_verifier = StaticBearerTokenVerifier(http_bearer_token)

    scope_instruction = (
        f"Runtime institution scope: profile_id={default_profile_id}. "
        "Keep profile_id bound to this institution for every regulation tool call."
        if default_profile_id
        else "Runtime institution scope is unbound. Clients must provide profile_id explicitly when the tenant contains multiple institutions."
    )
    server = FastMCP(
        "Public Institution Regulation MCP",
        instructions=f"{SERVER_INSTRUCTIONS}\n\n{scope_instruction}",
        host=host,
        port=port,
        auth=auth_settings,
        token_verifier=token_verifier,
        transport_security=_http_transport_security_settings(
            host=host,
            port=port,
            auth_issuer_url=auth_issuer_url,
            allowed_hosts=allowed_http_hosts,
            allowed_origins=allowed_http_origins,
        ),
    )
    server._reg_rag_scope = {
        "protocol": "MCP",
        "server_component": "regulation_mcp_server",
        "client_component": "external_ai_or_institution_client",
        "tenant_id": tenant_id,
        "profile_id": default_profile_id,
        "profile_bound": bool(default_profile_id),
    }
    if warm_cache:
        server._reg_rag_warmup_status = warm_mcp_runtime(settings=settings, auth=auth)
    else:
        server._reg_rag_warmup_status = {
            "warmed": False,
            "skipped": True,
            "background_tokenizer_warmup": start_background_tokenizer_warmup(delay_seconds=5.0),
        }

    @server.tool(
        name="search",
        title="Search approved regulations",
        description=(
            "Search approved local regulation chunks. Use this first for regulation questions, "
            "then call fetch with a returned id for full evidence. The response includes verbatim_text "
            "and a verbatim evidence block; show that block when the user asks for the original wording. "
            "Answer only from returned text: "
            "Pass profile_id when a tenant contains multiple institutions so results stay within "
            "the selected institution. "
            "this institution's terminology can differ from general Korean public-sector usage, so "
            "never fill in definitions, categories, or day counts from prior knowledge. If a result "
            "enumerates items without defining them, search or fetch the defining articles before "
            "describing each item, and cite the document and article for every statement."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def search_tool(
        query: McpQuery,
        top_k: McpTopK = 5,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        document_id: McpOptionalIdentifier | None = None,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return search_regulations(
            settings=settings,
            auth=auth,
            query=query,
            top_k=top_k,
            security_levels=security_levels,
            department_ids=department_ids,
            document_id=document_id,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
            metadata_profile=normalized_tool_profile,
        )

    lookup_decorator = (
        server.tool(
            name="lookup",
            title="Direct regulation lookup with RAG fallback",
            description=(
                "Look up an approved regulation document or article directly when document_id is known. "
                "If the exact lookup has no result, fall back to approved-local RAG search. The response "
                "marks retrieval_mode as direct_lookup or rag_fallback and includes verbatim evidence."
            ),
            annotations=READ_ONLY_TOOL_ANNOTATIONS,
            structured_output=True,
        )
        if normalized_tool_profile != "chatgpt-data"
        else (lambda function: function)
    )
    @lookup_decorator
    def lookup_tool(
        query: McpQuery,
        document_id: McpOptionalIdentifier | None = None,
        article_no: McpArticleNo | None = None,
        top_k: McpTopK = 5,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return lookup_regulation(
            settings=settings,
            auth=auth,
            query=query,
            document_id=document_id,
            article_no=article_no,
            top_k=top_k,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
            metadata_profile=normalized_tool_profile,
        )

    @server.tool(
        name="fetch",
        title="Fetch approved regulation evidence",
        description=(
            "Fetch full approved regulation evidence for an id returned by search. "
            "Use verbatim_text/verbatim.text and citation metadata as answer evidence; do not add "
            "definitions or conditions from prior knowledge. If the fetched text does not answer "
            "the question, fetch the defining article instead of guessing. If search used "
            "as_of_date, pass the same as_of_date to fetch so historical visibility is preserved."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def fetch_tool(
        id: McpResultId,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return fetch_regulation(
            settings=settings,
            auth=auth,
            result_id=id,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
            metadata_profile=normalized_tool_profile,
        )

    if normalized_tool_profile == "chatgpt-data":
        return server

    @server.tool(
        name="list_documents",
        title="List approved regulation documents",
        description="List approved MCP-visible regulation documents, optionally restricted to one institution profile.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def list_documents_tool(
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return list_document_results(
            settings=settings,
            auth=auth,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
        )

    @server.tool(
        name="list_regulations",
        title="List institution regulation catalog",
        description=(
            "List individual regulations detected inside the selected institution, even when many regulations "
            "came from one combined HWP/PDF file. Optionally search regulation titles and TOC terms or include revisions."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def list_regulations_tool(
        query: McpOptionalIdentifier | None = None,
        include_history: bool = False,
        profile_id: McpOptionalIdentifier | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return list_regulation_results(
            settings=settings,
            auth=auth,
            profile_id=effective_profile_id,
            query=query,
            include_history=include_history,
        )

    @server.tool(
        name="get_regulation_toc",
        title="Get regulation table of contents",
        description=(
            "Return the chapter, section, article, appendix, and form hierarchy for one regulation_unit_id. "
            "Pass as_of_date to inspect the version effective on a historical date."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def get_regulation_toc_tool(
        regulation_unit_id: McpIdentifier,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return get_regulation_toc_result(
            settings=settings,
            auth=auth,
            regulation_unit_id=regulation_unit_id,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
        )

    @server.tool(
        name="get_regulation_article",
        title="Get exact article from a regulation",
        description=(
            "Return an exact approved article using regulation_unit_id plus article number without scanning other "
            "institution regulations. Pass as_of_date for a historical revision."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def get_regulation_article_tool(
        regulation_unit_id: McpIdentifier,
        article_no: McpArticleNo,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return get_regulation_article_result(
            settings=settings,
            auth=auth,
            regulation_unit_id=regulation_unit_id,
            article_no=article_no,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
        )

    @server.tool(
        name="get_regulation_history",
        title="Get regulation version history",
        description=(
            "Return version, lifecycle status, effective dates, and predecessor metadata for one "
            "tenant-scoped regulation family. This tool does not return raw regulation text."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def get_regulation_history_tool(
        regulation_id: McpIdentifier,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return get_regulation_history_result(
            settings=settings,
            auth=auth,
            regulation_id=regulation_id,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
        )

    @server.tool(
        name="get_document",
        title="Get full approved document",
        description=(
            "Return the full approved MCP-visible text for a document ID. Each chunk includes verbatim_text "
            "and a verbatim evidence block; pass as_of_date for historical effectiveness."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def get_document_tool(
        document_id: McpIdentifier,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return get_document_result(
            settings=settings,
            auth=auth,
            document_id=document_id,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
        )

    @server.tool(
        name="get_article",
        title="Get regulation article",
        description=(
            "Return approved regulation evidence for a specific document ID and article number, including "
            "verbatim_text/verbatim evidence; pass as_of_date for historical effectiveness."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def get_article_tool(
        document_id: McpIdentifier,
        article_no: McpArticleNo,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return get_article_result(
            settings=settings,
            auth=auth,
            document_id=document_id,
            article_no=article_no,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
        )

    @server.tool(
        name="get_table",
        title="Get regulation table",
        description=(
            "Return an approved table or appendix by table ID or chunk ID, including verbatim table evidence "
            "and structured rows; pass as_of_date for historical effectiveness."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def get_table_tool(
        table_id: McpIdentifier,
        document_id: McpOptionalIdentifier | None = None,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return get_table_result(
            settings=settings,
            auth=auth,
            table_id=table_id,
            document_id=document_id,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
            as_of_date=as_of_date,
        )

    @server.tool(
        name="compare_versions",
        title="Compare regulation versions",
        description="Compare approved regulation evidence between two document IDs.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def compare_versions_tool(
        base_document_id: McpIdentifier,
        target_document_id: McpIdentifier,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return compare_versions_result(
            settings=settings,
            auth=auth,
            base_document_id=base_document_id,
            target_document_id=target_document_id,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
        )

    @server.tool(
        name="get_citation",
        title="Get regulation citation",
        description="Return citation metadata for a result ID returned by search.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def get_citation_tool(
        id: McpResultId,
        profile_id: McpOptionalIdentifier | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return get_citation_result(
            settings=settings,
            auth=auth,
            result_id=id,
            profile_id=effective_profile_id,
        )

    @server.tool(
        name="get_index_status",
        title="Get MCP index status",
        description="Return document-level approved-vector and indexing status for MCP-visible regulation data.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        structured_output=True,
    )
    def get_index_status_tool(
        document_id: McpOptionalIdentifier | None = None,
        security_levels: McpSecurityLevels | None = None,
        department_ids: McpDepartmentIds | None = None,
        profile_id: McpOptionalIdentifier | None = None,
    ) -> dict[str, Any]:
        effective_profile_id = _resolve_profile_scope(profile_id, default_profile_id)
        return get_index_status_result(
            settings=settings,
            auth=auth,
            document_id=document_id,
            security_levels=security_levels,
            department_ids=department_ids,
            profile_id=effective_profile_id,
        )

    return server


def _default_auth_issuer_url(*, host: str, port: int) -> str:
    issuer_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    bracketed_host = f"[{issuer_host}]" if ":" in issuer_host and not issuer_host.startswith("[") else issuer_host
    return f"http://{bracketed_host}:{int(port)}"


def _http_transport_security_settings(
    *,
    host: str,
    port: int,
    auth_issuer_url: str | None,
    allowed_hosts: list[str] | None,
    allowed_origins: list[str] | None,
) -> TransportSecuritySettings:
    hosts = {str(value).strip() for value in (allowed_hosts or []) if str(value).strip()}
    origins = {str(value).strip().rstrip("/") for value in (allowed_origins or []) if str(value).strip()}
    if host in {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0", "::"}:
        hosts.update({"127.0.0.1:*", "localhost:*", "[::1]:*"})
        origins.update({"http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"})
    else:
        hosts.update({host, f"{host}:*"})

    issuer = urlparse(str(auth_issuer_url or "").strip())
    if issuer.scheme in {"http", "https"} and issuer.hostname:
        hosts.add(issuer.netloc)
        hosts.add(f"{issuer.hostname}:*")
        origins.add(f"{issuer.scheme}://{issuer.netloc}")

    if not hosts:
        hosts.add(f"127.0.0.1:{int(port)}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def _resolve_profile_scope(requested_profile_id: str | None, default_profile_id: str | None) -> str | None:
    requested = str(requested_profile_id or "").strip() or None
    default = str(default_profile_id or "").strip() or None
    if default and requested and default.casefold() != requested.casefold():
        raise ValueError("The MCP server is bound to a different institution profile.")
    return default or requested
