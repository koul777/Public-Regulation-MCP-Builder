"""Subprocess options that keep Windows console windows from flashing."""

from __future__ import annotations

import os
import subprocess
from typing import Any

# Documented Win32 CREATE_NO_WINDOW value. Used only when the running
# interpreter does not expose ``subprocess.CREATE_NO_WINDOW`` itself.
_CREATE_NO_WINDOW = 0x08000000


def _is_windows() -> bool:
    """Keep platform branching mockable without mutating process-wide os.name."""
    return os.name == "nt"


def hidden_window_options() -> dict[str, Any]:
    """Return ``subprocess`` keyword arguments that suppress a console window.

    Merge the result into the kwargs of ``subprocess.run``/``Popen``. The flag
    only hides the console host; windows a child creates itself (for example a
    native folder-picker dialog) are unaffected. Non-Windows platforms get an
    empty dict because ``creationflags`` is rejected there.
    """
    if not _is_windows():
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)}
