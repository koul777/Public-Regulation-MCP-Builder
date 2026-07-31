from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class McpInputSchemaBoundaryTests(unittest.TestCase):
    def test_dependency_light_modules_do_not_cold_import_pydantic(self) -> None:
        for module_name in (
            "app.core.input_limits",
            "app.mcp_server.regulation_tools",
        ):
            with self.subTest(module_name=module_name):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import importlib,json,sys; "
                            f"module=importlib.import_module({module_name!r}); "
                            "print(json.dumps({"
                            "'pydantic_loaded':'pydantic' in sys.modules,"
                            "'schema_module_loaded':'app.core.mcp_input_schemas' in sys.modules,"
                            "'legacy_alias_present':hasattr(module,'McpQuery')"
                            "}))"
                        ),
                    ],
                    cwd=REPOSITORY_ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                state = json.loads(completed.stdout.strip())

                self.assertFalse(state["pydantic_loaded"])
                self.assertFalse(state["schema_module_loaded"])
                self.assertFalse(state["legacy_alias_present"])


if __name__ == "__main__":
    unittest.main()
