"""Minimal ``.env`` loader and writer for the local operator console.

The README and the admin screen have told operators for a long time that
permanent AI connection values belong in ``.env``.  Nothing actually read that
file, so a setting that "survived a restart" only did so when the operator also
exported it as a real environment variable.  This module closes that gap
without adding a dependency: real environment variables always win, the file
is read once at import time by :mod:`app.core.config`, and the writer is used
only when an operator explicitly opts in from the sidebar.

Secrets are never logged here and the file lives outside the public source
tree (``.env`` is gitignored).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

DOTENV_PATH_ENV = "REG_RAG_DOTENV_PATH"
_LINE_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def project_root() -> Path:
    """Repository or portable-bundle root that owns the ``.env`` file."""

    return Path(__file__).resolve().parents[2]


def dotenv_path() -> Path:
    """Location of the operator ``.env`` file (override with ``REG_RAG_DOTENV_PATH``)."""

    override = str(os.getenv(DOTENV_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return project_root() / ".env"


def _unquote(value: str) -> str:
    text = value.strip()
    if text and text[0] in {'"', "'"}:
        # A quoted value ends at the matching quote; anything after it (such as
        # an inline comment) is ignored.
        closing = text.find(text[0], 1)
        return text[1:closing] if closing != -1 else text[1:]
    # Drop an inline comment only when it is separated by whitespace, so a key
    # such as ``abc#def`` survives intact.
    comment = re.search(r"\s+#", text)
    if comment:
        text = text[: comment.start()].rstrip()
    return text


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines; comments and blank lines are ignored."""

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_PATTERN.match(line)
        if not match:
            continue
        values[match.group(1)] = _unquote(match.group(2))
    return values


def load_dotenv_file(path: Path | None = None, *, override: bool = False) -> list[str]:
    """Load ``.env`` into ``os.environ`` and return the keys that were applied.

    Existing environment variables are kept unless ``override`` is true, so a
    value exported by the shell or a launcher script always beats the file.
    A missing or unreadable file is not an error: local operation must keep
    working without any configuration.
    """

    target = path or dotenv_path()
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return []
    applied: list[str] = []
    for key, value in parse_dotenv(text).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


def _quote_if_needed(value: str) -> str:
    text = str(value)
    if text == "" or re.search(r"[\s#\"']", text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def write_dotenv_values(values: Mapping[str, str], path: Path | None = None) -> Path:
    """Create or update ``.env`` so that each key has the given value.

    Existing lines for other keys, comments, and ordering are preserved. Keys
    that already exist are rewritten in place; new keys are appended. The
    caller decides which keys are written, so this function never reads
    secrets back out of the process environment on its own.
    """

    target = path or dotenv_path()
    try:
        existing = target.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, ValueError):
        existing = []
    pending = {str(key): _quote_if_needed(str(value)) for key, value in values.items()}
    rewritten: list[str] = []
    for line in existing:
        match = _LINE_PATTERN.match(line.strip()) if line.strip() and not line.strip().startswith("#") else None
        key = match.group(1) if match else None
        if key in pending:
            rewritten.append(f"{key}={pending.pop(key)}")
        else:
            rewritten.append(line)
    for key, value in pending.items():
        rewritten.append(f"{key}={value}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(rewritten).rstrip("\n") + "\n", encoding="utf-8")
    return target
