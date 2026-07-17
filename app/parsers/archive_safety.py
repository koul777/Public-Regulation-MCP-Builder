from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
import zipfile

from app.parsers.base import ParserError


DEFAULT_ARCHIVE_MAX_ENTRIES = 4096
DEFAULT_ARCHIVE_MAX_FILE_BYTES = 128 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_ENTRY_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_COMPRESSION_RATIO = 200.0
DEFAULT_ARCHIVE_MAX_MEMBER_NAME_CHARS = 512
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SUPPORTED_COMPRESSION_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


@dataclass(frozen=True)
class OfficeArchiveLimits:
    max_entries: int = DEFAULT_ARCHIVE_MAX_ENTRIES
    max_archive_bytes: int = DEFAULT_ARCHIVE_MAX_FILE_BYTES
    max_total_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_TOTAL_UNCOMPRESSED_BYTES
    max_entry_uncompressed_bytes: int = DEFAULT_ARCHIVE_MAX_ENTRY_UNCOMPRESSED_BYTES
    max_compression_ratio: float = DEFAULT_ARCHIVE_MAX_COMPRESSION_RATIO
    max_member_name_chars: int = DEFAULT_ARCHIVE_MAX_MEMBER_NAME_CHARS

    def __post_init__(self) -> None:
        values = {
            "max_entries": self.max_entries,
            "max_archive_bytes": self.max_archive_bytes,
            "max_total_uncompressed_bytes": self.max_total_uncompressed_bytes,
            "max_entry_uncompressed_bytes": self.max_entry_uncompressed_bytes,
            "max_member_name_chars": self.max_member_name_chars,
        }
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values.values()):
            raise ValueError("Office archive integer safety limits must be positive.")
        ratio = float(self.max_compression_ratio)
        if isinstance(self.max_compression_ratio, bool) or not math.isfinite(ratio) or ratio <= 1.0:
            raise ValueError("Office archive maximum compression ratio must be greater than 1.0.")


def validate_office_archive_file_size(
    path: Path,
    *,
    format_name: str,
    limits: OfficeArchiveLimits,
) -> None:
    try:
        archive_bytes = path.stat().st_size
    except OSError as exc:
        raise ParserError(f"Failed to inspect {format_name} archive file size.") from exc
    if archive_bytes > limits.max_archive_bytes:
        raise ParserError(
            f"Unsafe {format_name} archive: file size {archive_bytes} exceeds limit {limits.max_archive_bytes}."
        )


def validate_office_archive(
    archive: zipfile.ZipFile,
    *,
    format_name: str,
    limits: OfficeArchiveLimits,
) -> list[zipfile.ZipInfo]:
    try:
        infos = archive.infolist()
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ParserError(f"Failed to inspect {format_name} archive safely.") from exc
    if len(infos) > limits.max_entries:
        raise ParserError(
            f"Unsafe {format_name} archive: entry count {len(infos)} exceeds limit {limits.max_entries}."
        )

    total_uncompressed = 0
    normalized_names: set[str] = set()
    for info in infos:
        _validate_member_path(info, format_name=format_name, limits=limits, normalized_names=normalized_names)
        if info.flag_bits & 0x1:
            raise ParserError(f"Unsafe {format_name} archive: encrypted entries are not supported.")
        if _is_symlink(info):
            raise ParserError(f"Unsafe {format_name} archive: symbolic-link entries are not supported.")
        if info.compress_type not in _SUPPORTED_COMPRESSION_METHODS:
            raise ParserError(f"Unsafe {format_name} archive: unsupported compression method.")
        if info.file_size < 0 or info.compress_size < 0:
            raise ParserError(f"Unsafe {format_name} archive: invalid negative entry size.")
        if info.file_size > limits.max_entry_uncompressed_bytes:
            raise ParserError(
                f"Unsafe {format_name} archive: entry size {info.file_size} exceeds limit "
                f"{limits.max_entry_uncompressed_bytes}."
            )
        total_uncompressed += info.file_size
        if total_uncompressed > limits.max_total_uncompressed_bytes:
            raise ParserError(
                f"Unsafe {format_name} archive: total uncompressed size {total_uncompressed} exceeds limit "
                f"{limits.max_total_uncompressed_bytes}."
            )
        if info.is_dir() or info.file_size == 0:
            continue
        if info.compress_size == 0:
            raise ParserError(f"Unsafe {format_name} archive: non-empty entry has zero compressed size.")
        compression_ratio = info.file_size / info.compress_size
        if compression_ratio > limits.max_compression_ratio:
            raise ParserError(
                f"Unsafe {format_name} archive: compression ratio {compression_ratio:.1f} exceeds limit "
                f"{limits.max_compression_ratio:.1f}."
            )
    return infos


def read_archive_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    format_name: str,
    max_bytes: int,
    chunk_bytes: int = 1024 * 1024,
) -> bytes:
    payload = bytearray()
    try:
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(min(chunk_bytes, max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise ParserError(
                        f"Unsafe {format_name} archive: decompressed entry exceeded runtime limit {max_bytes}."
                    )
    except ParserError:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise ParserError(f"Failed to decompress {format_name} archive entry safely.") from exc
    return bytes(payload)


def _validate_member_path(
    info: zipfile.ZipInfo,
    *,
    format_name: str,
    limits: OfficeArchiveLimits,
    normalized_names: set[str],
) -> None:
    name = str(info.filename or "")
    if not name or "\x00" in name or len(name) > limits.max_member_name_chars:
        raise ParserError(f"Unsafe {format_name} archive: invalid member name.")
    if "\\" in name:
        raise ParserError(f"Unsafe {format_name} archive: non-canonical member path separator.")
    if name.startswith(("/", "//")) or _WINDOWS_DRIVE_RE.match(name):
        raise ParserError(f"Unsafe {format_name} archive: absolute member path.")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ParserError(f"Unsafe {format_name} archive: member path traversal.")
    normalized = unicodedata.normalize("NFC", path.as_posix().rstrip("/")).casefold()
    if normalized in {"", ".", ".."} or normalized in normalized_names:
        raise ParserError(f"Unsafe {format_name} archive: duplicate or ambiguous member path.")
    normalized_names.add(normalized)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return bool(unix_mode and stat.S_ISLNK(unix_mode))
