from __future__ import annotations

import socket
import unittest

from scripts.find_available_ui_port import port_is_available, select_available_port


class FindAvailableUiPortTests(unittest.TestCase):
    def test_selects_next_port_when_preferred_port_is_in_use(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            occupied_port = int(listener.getsockname()[1])

            selected_port = select_available_port(occupied_port, search_count=10)

        self.assertGreater(selected_port, occupied_port)
        self.assertTrue(port_is_available(selected_port))

    def test_rejects_invalid_preferred_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            select_available_port(0)


if __name__ == "__main__":
    unittest.main()
