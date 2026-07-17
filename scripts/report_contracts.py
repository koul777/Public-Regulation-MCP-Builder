from __future__ import annotations

from pathlib import Path


REPORT_TYPE_MCP_PRODUCT_READINESS = "mcp_product_readiness"
REPORT_TYPE_MCP_DEMO_ANSWERS = "mcp_demo_answers"
REPORT_TYPE_MCP_TRANSPORT_SMOKE = "mcp_transport_smoke"
REPORT_TYPE_MCP_INDEX_VISIBILITY_AUDIT = "mcp_index_visibility_audit"
REPORT_TYPE_MCP_CONNECTION_READINESS = "mcp_connection_readiness"
REPORT_TYPE_MCP_READINESS_AUTHORITY = "mcp_readiness_authority"
REPORT_TYPE_MCP_HANDOFF_REPORT = "mcp_handoff_report"

AUTHORITY_ROLE_PRODUCT_READINESS = "product_readiness"
AUTHORITY_ROLE_MCP_DEMO_ANSWERS = "mcp_demo_answers"
AUTHORITY_ROLE_MCP_TRANSPORT_SMOKE = "mcp_transport_smoke"
AUTHORITY_ROLE_MCP_INDEX_VISIBILITY = "mcp_index_visibility"
AUTHORITY_ROLE_MCP_CONNECTION_READINESS = "mcp_connection_readiness"

EXPECTED_AUTHORITY_ARTIFACTS = {
    AUTHORITY_ROLE_PRODUCT_READINESS: {
        "path": "reports/mcp_product_readiness_current.json",
        "report_type": REPORT_TYPE_MCP_PRODUCT_READINESS,
    },
    AUTHORITY_ROLE_MCP_DEMO_ANSWERS: {
        "path": "reports/mcp_demo_answers_current.json",
        "report_type": REPORT_TYPE_MCP_DEMO_ANSWERS,
    },
    AUTHORITY_ROLE_MCP_TRANSPORT_SMOKE: {
        "path": "reports/mcp_transport_smoke_current.json",
        "report_type": REPORT_TYPE_MCP_TRANSPORT_SMOKE,
    },
    AUTHORITY_ROLE_MCP_INDEX_VISIBILITY: {
        "path": "reports/mcp_index_visibility_current.json",
        "report_type": REPORT_TYPE_MCP_INDEX_VISIBILITY_AUDIT,
    },
    AUTHORITY_ROLE_MCP_CONNECTION_READINESS: {
        "path": "reports/mcp_connection_readiness_current.json",
        "report_type": REPORT_TYPE_MCP_CONNECTION_READINESS,
    },
}

REQUIRED_AUTHORITY_ROLES = frozenset(EXPECTED_AUTHORITY_ARTIFACTS)

MCP_CORE_REPORT_PATHS = {
    REPORT_TYPE_MCP_READINESS_AUTHORITY: Path("reports/mcp_readiness_authority_current.json"),
    REPORT_TYPE_MCP_PRODUCT_READINESS: Path("reports/mcp_product_readiness_current.json"),
    REPORT_TYPE_MCP_DEMO_ANSWERS: Path("reports/mcp_demo_answers_current.json"),
    REPORT_TYPE_MCP_TRANSPORT_SMOKE: Path("reports/mcp_transport_smoke_current.json"),
    REPORT_TYPE_MCP_INDEX_VISIBILITY_AUDIT: Path("reports/mcp_index_visibility_current.json"),
    REPORT_TYPE_MCP_CONNECTION_READINESS: Path("reports/mcp_connection_readiness_current.json"),
}
