from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any


MCP_PERFORMANCE_SOURCE_SCOPE = "mcp-performance-python-source-v1"
_SOURCE_STATE_AVAILABLE = "available"
_SOURCE_STATE_UNAVAILABLE = "unavailable"
_SOURCE_STATE_CHANGED = "changed_during_run"
_LENGTH_PREFIX_BYTES = 8


class _SourceStateUnavailable(RuntimeError):
    pass


class _SourceStateChanged(RuntimeError):
    pass


def current_repo_commit(repo_root: Path | None = None) -> str | None:
    root = repo_root or Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        # Installed wheels and handoff bundles must remain usable on hosts that
        # do not have Git on PATH.  The commit is optional report metadata, not
        # a runtime prerequisite.
        return None
    if completed.returncode != 0:
        return None
    commit = completed.stdout.decode("utf-8", "replace").strip()
    return commit if len(commit) == 40 else None


def capture_mcp_performance_source_state(
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Fingerprint the Python source scope without consulting Git metadata."""

    try:
        root = (repo_root or Path(__file__).resolve().parents[1]).resolve(
            strict=True
        )
        paths_before = _enumerate_mcp_performance_source_files(root)
        relative_paths_before = [
            _repo_relative_source_path(root, path) for path in paths_before
        ]
        digest = hashlib.sha256()
        digest.update(MCP_PERFORMANCE_SOURCE_SCOPE.encode("utf-8"))
        byte_count = 0
        scanned_files: list[
            tuple[Path, Path, tuple[int, int, int, int, int], bytes]
        ] = []
        for path, relative_path in zip(
            paths_before,
            relative_paths_before,
            strict=True,
        ):
            resolved_before = _resolved_source_file(root, path)
            signature_before = _source_file_signature(path)
            raw = _read_source_file_bytes(path)
            signature_after = _source_file_signature(path)
            resolved_after = _resolved_source_file(root, path)
            if (
                resolved_before != resolved_after
                or signature_before != signature_after
            ):
                raise _SourceStateChanged
            _update_length_prefixed(digest, relative_path.encode("utf-8"))
            _update_length_prefixed(digest, raw)
            byte_count += len(raw)
            scanned_files.append(
                (
                    path,
                    resolved_after,
                    signature_after,
                    hashlib.sha256(raw).digest(),
                )
            )

        paths_after = _enumerate_mcp_performance_source_files(root)
        relative_paths_after = [
            _repo_relative_source_path(root, path) for path in paths_after
        ]
        if relative_paths_before != relative_paths_after:
            raise _SourceStateChanged
        for path, resolved, signature, raw_digest in scanned_files:
            final_signature_before = _source_file_signature(path)
            final_raw = _read_source_file_bytes(path)
            final_signature_after = _source_file_signature(path)
            if (
                _resolved_source_file(root, path) != resolved
                or final_signature_before != final_signature_after
                or final_signature_after != signature
                or hashlib.sha256(final_raw).digest() != raw_digest
            ):
                raise _SourceStateChanged
        paths_final = _enumerate_mcp_performance_source_files(root)
        if relative_paths_before != [
            _repo_relative_source_path(root, path) for path in paths_final
        ]:
            raise _SourceStateChanged
        return {
            "scope": MCP_PERFORMANCE_SOURCE_SCOPE,
            "status": _SOURCE_STATE_AVAILABLE,
            "sha256": digest.hexdigest(),
            "file_count": len(paths_before),
            "byte_count": byte_count,
            "stable": True,
        }
    except _SourceStateChanged:
        return _unavailable_source_state(_SOURCE_STATE_CHANGED)
    except (OSError, RuntimeError, ValueError):
        return _unavailable_source_state(_SOURCE_STATE_UNAVAILABLE)


def finalize_mcp_performance_source_state(
    started: Mapping[str, Any],
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Compare start/end snapshots and suppress a digest when source drifted."""

    finished = capture_mcp_performance_source_state(repo_root)
    started_status = str(started.get("status") or "")
    finished_status = str(finished.get("status") or "")
    if _SOURCE_STATE_CHANGED in {started_status, finished_status}:
        return _unavailable_source_state(_SOURCE_STATE_CHANGED)
    if (
        started_status != _SOURCE_STATE_AVAILABLE
        or finished_status != _SOURCE_STATE_AVAILABLE
    ):
        return _unavailable_source_state(_SOURCE_STATE_UNAVAILABLE)
    comparable_fields = ("scope", "sha256", "file_count", "byte_count")
    if any(started.get(name) != finished.get(name) for name in comparable_fields):
        return _unavailable_source_state(_SOURCE_STATE_CHANGED)
    return dict(finished)


def _enumerate_mcp_performance_source_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory_name in ("app", "scripts"):
        directory = root / directory_name
        if not directory.is_dir() or directory.is_symlink():
            raise _SourceStateUnavailable

        def raise_walk_error(error: OSError) -> None:
            raise error

        for directory_path, directory_names, file_names in os.walk(
            directory,
            topdown=True,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            current = Path(directory_path)
            for child_name in directory_names:
                child = current / child_name
                if child.is_symlink():
                    raise _SourceStateUnavailable
            for file_name in file_names:
                if file_name.endswith(".py"):
                    paths.append(current / file_name)
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file() and not pyproject_path.is_symlink():
        raise _SourceStateUnavailable
    paths.append(pyproject_path)
    return sorted(
        paths,
        key=lambda path: _repo_relative_source_path(root, path).encode("utf-8"),
    )


def _repo_relative_source_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise _SourceStateUnavailable from exc


def _resolved_source_file(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _SourceStateUnavailable from exc
    if not resolved.is_file():
        raise _SourceStateUnavailable
    return resolved


def _source_file_signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _read_source_file_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _update_length_prefixed(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(_LENGTH_PREFIX_BYTES, "big"))
    digest.update(value)


def _unavailable_source_state(status: str) -> dict[str, Any]:
    return {
        "scope": MCP_PERFORMANCE_SOURCE_SCOPE,
        "status": status,
        "sha256": None,
        "file_count": None,
        "byte_count": None,
        "stable": False,
    }
