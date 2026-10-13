"""Restore packed approved files without changing their sealed names or bytes."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path, PurePosixPath

_LOCK = threading.Lock()
MAX_RUNTIME_BYTES = 400 * 1024 * 1024


def materialize_vercel_runtime(data_dir: Path, *, cache_root: Path | None = None) -> Path:
    """Unpack sealed gzip payloads to a verified, process-shared temporary cache."""
    manifest_path = data_dir / "mcp_runtime_manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    hashes = (manifest.get("runtime_data_reuse") or {}).get("file_sha256")
    if not isinstance(hashes, dict) or not hashes:
        return data_dir
    for name, digest in hashes.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or not path.parts or ".." in path.parts
                or "\\" in name or ":" in name or path.as_posix() != name
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("Invalid sealed runtime file entry.")
    if not any(not (data_dir / name).is_file() and Path(f"{data_dir / name}.gz").is_file()
               for name in hashes):
        return data_dir

    identity = hashlib.sha256(str(data_dir.resolve()).encode() + manifest_bytes).hexdigest()
    root = (cache_root or Path(tempfile.gettempdir()) / "pr-mcp-runtime").resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / identity
    # Vercel workers can share /tmp. Serialize extraction so they do not duplicate
    # hundreds of MB of data or observe a partially restored approval snapshot.
    with _LOCK, (root / f"{identity}.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ready = target / ".ready"
            if ready.is_file() and ready.read_text(encoding="ascii") == identity:
                return target
            target.mkdir(exist_ok=True)
            total_bytes = 0
            for name, expected_hash in hashes.items():
                source = data_dir / name
                compressed = not source.is_file()
                if compressed:
                    source = Path(f"{source}.gz")
                destination = target / name
                if not destination.resolve().is_relative_to(target.resolve()):
                    raise ValueError("Runtime cache path escapes its directory.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                opener = gzip.open if compressed else open
                with opener(source, "rb") as reader, destination.open("wb") as writer:
                    while block := reader.read(1024 * 1024):
                        total_bytes += len(block)
                        if total_bytes > MAX_RUNTIME_BYTES:
                            raise ValueError("Restored runtime exceeds the allowed size.")
                        digest.update(block)
                        writer.write(block)
                if digest.hexdigest() != expected_hash:
                    raise ValueError("Restored runtime failed its sealed file hash check.")
            (target / manifest_path.name).write_bytes(manifest_bytes)
            ready.write_text(identity, encoding="ascii")
            return target
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
