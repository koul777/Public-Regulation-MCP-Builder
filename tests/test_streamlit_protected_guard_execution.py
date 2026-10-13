from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

import streamlit

from app.core.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[1]


class _StreamlitStop(RuntimeError):
    pass


class StreamlitProtectedGuardExecutionTests(unittest.TestCase):
    def test_protected_mode_stops_before_repository_or_processing_service_is_created(self):
        # Patch page effects while retaining the real package and components.
        # Replacing sys.modules['streamlit'] made this guard depend on other tests
        # having already imported frontend modules and on every UI API call.
        page_effects = {name: mock.Mock() for name in (
            "set_page_config", "set_option", "title", "error", "info",
        )}
        page_effects["stop"] = mock.Mock(side_effect=_StreamlitStop)
        protected_settings = Settings(api_auth_required=True, tenant_storage_isolation=True)
        module_path = REPO_ROOT / "frontend" / "streamlit_app.py"
        spec = importlib.util.spec_from_file_location("streamlit_app_guard_test", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)

        with mock.patch.multiple(streamlit, session_state={}, **page_effects):
            with mock.patch("app.core.config.get_settings", return_value=protected_settings):
                with mock.patch("app.storage.repository.JsonRepository") as repository_cls:
                    with mock.patch("app.services.processing_service.ProcessingService") as processing_cls:
                        with self.assertRaises(_StreamlitStop):
                            spec.loader.exec_module(module)

        page_effects["error"].assert_called_once()
        page_effects["info"].assert_called_once()
        page_effects["stop"].assert_called_once()
        repository_cls.assert_not_called()
        processing_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
