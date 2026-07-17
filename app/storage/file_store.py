from __future__ import annotations

import hashlib
import shutil
import unicodedata
from pathlib import Path
from typing import BinaryIO, Callable

from app.core.config import Settings


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".hwpx", ".hwp"}
MAX_SIGNATURE_BYTES = 8


class FileStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.settings.exports_dir.mkdir(parents=True, exist_ok=True)

    def validate_upload(self, filename: str, content: bytes) -> None:
        self.validate_upload_filename(filename)
        max_bytes = self.max_upload_bytes()
        if len(content) > max_bytes:
            raise ValueError(f"Upload exceeds {self.settings.max_upload_mb}MB limit.")
        self.validate_upload_content(filename, content[:MAX_SIGNATURE_BYTES], len(content))

    def validate_upload_filename(self, filename: str) -> None:
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file extension: {suffix}")

    def validate_upload_content(self, filename: str, leading_bytes: bytes, total_size: int) -> None:
        if total_size <= 0:
            raise ValueError("Upload file is empty.")
        suffix = Path(filename).suffix.lower()
        if suffix == ".pdf" and not leading_bytes.startswith(b"%PDF-"):
            raise ValueError("Upload content does not match .pdf signature.")
        if suffix in {".docx", ".hwpx"} and not leading_bytes.startswith(b"PK"):
            raise ValueError(f"Upload content does not match {suffix} signature.")
        if suffix == ".hwp" and not leading_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ValueError("Upload content does not match .hwp signature.")

    def max_upload_bytes(self) -> int:
        return self.settings.max_upload_mb * 1024 * 1024

    def save_upload(
        self,
        document_id: str,
        filename: str,
        content: bytes,
        *,
        regulation_id: str | None = None,
        profile_id: str | None = None,
        regulation_version: str | None = None,
    ) -> Path:
        self.validate_upload(filename, content)
        destination = self._upload_destination(
            document_id,
            filename,
            regulation_id=regulation_id,
            profile_id=profile_id,
            regulation_version=regulation_version,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def save_upload_stream(
        self,
        document_id: str,
        filename: str,
        source: BinaryIO,
        *,
        chunk_size: int = 1024 * 1024,
        expected_size: int | None = None,
        progress_callback: Callable[[int, int | None], None] | None = None,
        regulation_id: str | None = None,
        profile_id: str | None = None,
        regulation_version: str | None = None,
    ) -> tuple[Path, str, int]:
        self.validate_upload_filename(filename)
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive.")
        destination = self._upload_destination(
            document_id,
            filename,
            regulation_id=regulation_id,
            profile_id=profile_id,
            regulation_version=regulation_version,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        digest = hashlib.sha256()
        total_size = 0
        max_bytes = self.max_upload_bytes()
        leading = bytearray()
        if progress_callback is not None:
            progress_callback(0, expected_size)
        try:
            with temporary.open("wb") as handle:
                while True:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    total_size += len(chunk)
                    if total_size > max_bytes:
                        raise ValueError(f"Upload exceeds {self.settings.max_upload_mb}MB limit.")
                    if len(leading) < MAX_SIGNATURE_BYTES:
                        leading.extend(chunk[: MAX_SIGNATURE_BYTES - len(leading)])
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress_callback is not None:
                        progress_callback(total_size, expected_size)
            self.validate_upload_content(filename, bytes(leading), total_size)
            temporary.replace(destination)
            return destination, digest.hexdigest(), total_size
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def upload_path(
        self,
        document_id: str,
        filename: str,
        *,
        regulation_id: str | None = None,
        profile_id: str | None = None,
        regulation_version: str | None = None,
    ) -> Path:
        grouped = self._upload_destination(
            document_id,
            filename,
            regulation_id=regulation_id,
            profile_id=profile_id,
            regulation_version=regulation_version,
        )
        candidates = [grouped]
        if regulation_id:
            candidates.append(
                self._upload_destination(
                    document_id,
                    filename,
                    regulation_id=regulation_id,
                    profile_id=profile_id,
                    regulation_version=None,
                )
            )
            candidates.append(
                self._upload_destination(
                    document_id,
                    filename,
                    regulation_id=regulation_id,
                    profile_id=None,
                    regulation_version=None,
                )
            )
        candidates.append(
            self._upload_destination(
                document_id,
                filename,
                regulation_id=None,
                profile_id=None,
                regulation_version=None,
            )
        )
        for candidate in dict.fromkeys(candidates):
            if candidate.is_file():
                return candidate
        return grouped

    def relocate_upload(
        self,
        document_id: str,
        filename: str,
        *,
        old_regulation_id: str | None,
        new_regulation_id: str | None,
        profile_id: str | None = None,
        old_regulation_version: str | None = None,
        new_regulation_version: str | None = None,
    ) -> Path:
        source = self.upload_path(
            document_id,
            filename,
            regulation_id=old_regulation_id,
            profile_id=profile_id,
            regulation_version=old_regulation_version,
        )
        destination = self._upload_destination(
            document_id,
            filename,
            regulation_id=new_regulation_id,
            profile_id=profile_id,
            regulation_version=new_regulation_version,
        )
        if source == destination:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        parent = source.parent
        while parent != self.settings.uploads_dir and self.settings.uploads_dir in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return destination

    def _upload_destination(
        self,
        document_id: str,
        filename: str,
        *,
        regulation_id: str | None,
        profile_id: str | None,
        regulation_version: str | None,
    ) -> Path:
        suffix = Path(filename).suffix.lower()
        if not regulation_id:
            return self.settings.uploads_dir / f"{document_id}{suffix}"
        regulation_directory = _safe_directory_name(regulation_id, fallback="regulation")
        if profile_id:
            profile_directory = _safe_directory_name(profile_id, fallback="institution")
            root = self.settings.uploads_dir / "institutions" / profile_directory / "regulations" / regulation_directory
        else:
            root = self.settings.uploads_dir / "regulations" / regulation_directory
        if regulation_version:
            version_directory = _safe_directory_name(regulation_version, fallback="version")
            root = root / "versions" / version_directory
        return root / f"{document_id}{suffix}"

    def export_path(self, document_id: str, extension: str) -> Path:
        clean_extension = extension.lstrip(".")
        return self.settings.exports_dir / f"{document_id}.{clean_extension}"

    def copy_export(self, source: Path, document_id: str, extension: str) -> Path:
        destination = self.export_path(document_id, extension)
        shutil.copyfile(source, destination)
        return destination


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_directory_name(raw_value: str, *, fallback: str) -> str:
    value = unicodedata.normalize("NFKC", str(raw_value or "")).strip()
    value = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in value)
    value = value.rstrip(" .")
    if not value:
        value = hashlib.sha256(str(raw_value or fallback).encode("utf-8")).hexdigest()[:16]
    if len(value) > 80:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        value = f"{value[:69].rstrip(' ._')}-{digest}"
    return value
