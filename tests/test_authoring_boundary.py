from __future__ import annotations

import ast
from pathlib import Path
import unittest

from app.schemas.authoring import AuthoringProject


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTHORING_MODULES = (
    PROJECT_ROOT / "app" / "schemas" / "authoring.py",
    PROJECT_ROOT / "app" / "storage" / "authoring_repository.py",
    PROJECT_ROOT / "app" / "services" / "authoring_lint_service.py",
    PROJECT_ROOT / "app" / "services" / "authoring_safety_service.py",
    PROJECT_ROOT / "app" / "services" / "authoring_template_service.py",
    PROJECT_ROOT / "app" / "services" / "authoring_service.py",
    PROJECT_ROOT / "app" / "api" / "routes_authoring.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "app.api.routes_documents",
    "app.ingestion",
    "app.mcp",
    "app.retrieval",
    "app.schemas.chunk",
    "app.schemas.document",
    "app.services.review_workflow_service",
    "app.storage.repository",
)


class AuthoringBoundaryTests(unittest.TestCase):
    def test_authoring_modules_do_not_import_official_workflow_or_indexing(self) -> None:
        violations: list[str] = []
        for path in AUTHORING_MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [node.module or ""]
                for module_name in imported:
                    if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                        violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{module_name}")

        self.assertEqual([], violations)

    def test_authoring_project_has_no_official_approval_fields(self) -> None:
        field_names = set(AuthoringProject.model_fields)

        self.assertTrue(
            {"approval_id", "approval_status", "approved_content_hash"}.isdisjoint(field_names)
        )

    def test_p0_routes_do_not_offer_import_index_or_official_approval(self) -> None:
        source = (PROJECT_ROOT / "app" / "api" / "routes_authoring.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        route_paths = {
            argument.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "post", "put", "patch", "delete"}
            for argument in node.args[:1]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        }

        self.assertFalse(
            any(
                forbidden in path.lower()
                for path in route_paths
                for forbidden in ("import", "index", "official-approval", "approve")
            ),
            route_paths,
        )


if __name__ == "__main__":
    unittest.main()
