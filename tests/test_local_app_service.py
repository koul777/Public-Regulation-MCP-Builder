from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.services.local_app_service import start_local_qwen_chat


class LocalAppServiceTests(unittest.TestCase):
    def test_source_and_portable_launch_are_headless_and_not_ready_claims(self) -> None:
        for executable in ("", "synthetic-app.exe"):
            with self.subTest(executable=executable):
                selector = Mock(return_value=8555)
                process = SimpleNamespace(pid=123)
                factory = Mock(return_value=process)
                env = {"APP_ENV": "local", "PYTHONUTF8": "1"}
                state = start_local_qwen_chat(
                    project_root=Path("."), environment=env, packaged_executable=executable,
                    port_selector=selector, process_factory=factory,
                )
                selector.assert_called_once_with(8502, host="127.0.0.1", search_count=100)
                command = factory.call_args.args[0]
                self.assertIn("--headless", command)
                self.assertIn("--qwen-chat" if executable else "scripts.run_qwen_chat", command)
                self.assertEqual("http://127.0.0.1:8555", state["url"])
                self.assertNotIn("ready", state)
                self.assertIs(process, state["_process"])
                self.assertIsNot(env, factory.call_args.kwargs["env"])
                self.assertFalse(factory.call_args.kwargs.get("shell", False))
                if sys.platform == "win32":
                    self.assertEqual(subprocess.CREATE_NO_WINDOW, factory.call_args.kwargs["creationflags"])

    def test_invalid_port_never_launches_process(self) -> None:
        factory = Mock()
        for port in (0, 65536, True, "8502"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                start_local_qwen_chat(project_root=Path("."), environment={},
                                      port_selector=lambda *args, **kwargs: port, process_factory=factory)
        factory.assert_not_called()
