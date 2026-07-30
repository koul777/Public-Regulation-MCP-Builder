from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import json
import os
import stat as stat_module
from pathlib import Path
import re
import threading
from typing import Any

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

from starlette.exceptions import HTTPException

from app.core.security_primitives import (
    API_ROLE_ADMIN,
    ROLE_SECURITY_LEVELS,
    SECURITY_LEVEL_ORDER,
    AuthContext,
)
from app.core.tenant_access import tenant_storage_key
from app.retrieval.bm25_index import (
    Bm25Index,
    default_bm25_index_path,
    load_bm25_index,
)


BLOCKED_QUERY_PATTERNS = (
    re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:show|reveal|print|dump)\s+(?:the\s+)?(?:system\s+)?prompt",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:read|open|print)\s+(?:local\s+)?(?:file|path)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:execute|run)\s+(?:shell|cmd|powershell|command)",
        re.IGNORECASE,
    ),
)

_FileIdentitySignature = tuple[int, int, int, int]
_MissingFileIdentitySignature = tuple[str]
_MISSING_FILE_IDENTITY: _MissingFileIdentitySignature = ("missing",)
if os.name == "nt":
    _WINDOWS_EPOCH_FILETIME = 116_444_736_000_000_000
    _WINDOWS_FILE_LIST_DIRECTORY = 0x0001
    _WINDOWS_FILE_READ_ATTRIBUTES = 0x0080
    _WINDOWS_FILE_SHARE_ALL = 0x0001 | 0x0002 | 0x0004
    _WINDOWS_OPEN_EXISTING = 3
    _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _WINDOWS_FILE_BASIC_INFO_CLASS = 0
    _WINDOWS_FILE_ID_BOTH_DIRECTORY_INFO_CLASS = 0x0A
    _WINDOWS_ERROR_NO_MORE_FILES = 18
    _WINDOWS_DIRECTORY_BUFFER_SIZE = 64 * 1024


    class _WindowsFileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]


    class _WindowsFileIdBothDirectoryInfo(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", wintypes.DWORD),
            ("FileIndex", wintypes.DWORD),
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("AllocationSize", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
            ("FileNameLength", wintypes.DWORD),
            ("EaSize", wintypes.DWORD),
            ("ShortNameLength", ctypes.c_byte),
            ("ShortName", wintypes.WCHAR * 12),
            ("FileId", ctypes.c_longlong),
            ("FileName", wintypes.WCHAR * 1),
        ]


    @dataclass(frozen=True)
    class _WindowsDirectoryIdentity:
        name: str
        last_write_time_ns: int
        size: int
        change_time_ns: int
        attributes: int
        file_id: int


    _WINDOWS_DIRECTORY_FILE_NAME_OFFSET = (
        _WindowsFileIdBothDirectoryInfo.FileName.offset
    )
    _WINDOWS_DIRECTORY_LAYOUT_IS_VALID = (
        ctypes.sizeof(wintypes.WCHAR) == 2
        and ctypes.alignment(_WindowsFileIdBothDirectoryInfo) == 8
        and _WindowsFileIdBothDirectoryInfo.CreationTime.offset == 8
        and _WindowsFileIdBothDirectoryInfo.ChangeTime.offset == 32
        and _WindowsFileIdBothDirectoryInfo.EndOfFile.offset == 40
        and _WindowsFileIdBothDirectoryInfo.FileAttributes.offset == 56
        and _WindowsFileIdBothDirectoryInfo.FileNameLength.offset == 60
        and _WindowsFileIdBothDirectoryInfo.ShortNameLength.offset == 68
        and _WindowsFileIdBothDirectoryInfo.ShortName.offset == 70
        and _WindowsFileIdBothDirectoryInfo.FileId.offset == 96
        and _WINDOWS_DIRECTORY_FILE_NAME_OFFSET == 104
        and ctypes.sizeof(_WindowsFileIdBothDirectoryInfo) == 112
    )


    _WINDOWS_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _WINDOWS_CREATE_FILE = _WINDOWS_KERNEL32.CreateFileW
    _WINDOWS_CREATE_FILE.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _WINDOWS_CREATE_FILE.restype = wintypes.HANDLE
    _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE_EX = (
        _WINDOWS_KERNEL32.GetFileInformationByHandleEx
    )
    _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE_EX.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE_EX.restype = wintypes.BOOL
    _WINDOWS_CLOSE_HANDLE = _WINDOWS_KERNEL32.CloseHandle
    _WINDOWS_CLOSE_HANDLE.argtypes = [wintypes.HANDLE]
    _WINDOWS_CLOSE_HANDLE.restype = wintypes.BOOL
    _WINDOWS_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_RAG_VECTOR_CACHE_LOCK = threading.Lock()
_RAG_BM25_INDEX_CACHE: dict[
    Path,
    tuple[_FileIdentitySignature, Any],
] = {}
_RAG_VECTOR_RECORD_CACHE: dict[
    Path,
    tuple[_FileIdentitySignature, list[dict[str, Any]]],
] = {}
_RAG_VISIBLE_RECORDS_CACHE_LOCK = threading.Lock()
_RAG_VISIBLE_RECORDS_CACHE: OrderedDict[
    tuple[Any, ...],
    list[dict[str, Any]],
] = OrderedDict()
_RAG_VISIBLE_RECORDS_MAX_ENTRIES = 512
_RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE: dict[
    tuple[Path, tuple[str, ...]],
    tuple[tuple[Any, Any], str],
] = {}
_RAG_APPROVAL_JOURNAL_CACHE: OrderedDict[
    Path,
    tuple[
        _FileIdentitySignature | None,
        dict[str, tuple[dict[str, Any], ...]],
    ],
] = OrderedDict()
_RAG_APPROVAL_JOURNAL_CACHE_MAX_ENTRIES = 128
_RAG_APPROVAL_SNAPSHOT_CACHE: dict[
    tuple[Path, str, tuple[str, ...]],
    tuple[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]],
] = {}
_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE: OrderedDict[
    tuple[Path, str, tuple[str, ...]],
    tuple[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]],
] = OrderedDict()
_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE_MAX_ENTRIES = 128
_RUNTIME_CONTENT_SIGNATURE_LOCK = threading.Lock()
_RUNTIME_CONTENT_SIGNATURE_CACHE: dict[
    Path,
    tuple[_FileIdentitySignature, tuple[int, str]],
] = {}


@dataclass(frozen=True)
class RegulationQuery:
    query: str
    top_k: int = 5
    security_levels: list[str] | None = None
    department_ids: list[str] = field(default_factory=list)
    document_id: str | None = None
    profile_id: str | None = None
    as_of: date | datetime | str | None = None
    as_of_date: str | None = None


@dataclass(frozen=True)
class RepositoryPathDescriptor:
    """Read-only paths needed to validate an immutable RAG runtime bundle."""

    root: Path
    manifest_path: Path
    legacy_path: Path


def repository_path_descriptor(settings: Any) -> RepositoryPathDescriptor:
    data_dir = Path(settings.data_dir)
    root = data_dir / "repository"
    return RepositoryPathDescriptor(
        root=root,
        manifest_path=root / "manifest.json",
        legacy_path=data_dir / "repository.json",
    )


def normalize_department_id(value: Any) -> str:
    return re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        str(value or "").strip(),
    ).strip("._-").lower()


def normalize_department_ids(value: Any) -> tuple[str, ...]:
    raw_items = (
        value
        if isinstance(value, (list, tuple, set))
        else str(value or "").split(",")
    )
    departments = [
        cleaned
        for item in raw_items
        if (cleaned := normalize_department_id(item))
    ]
    return tuple(dict.fromkeys(departments))


def department_acl_set(value: Any) -> set[str]:
    if value is None:
        return set()
    return set(normalize_department_ids(value))


def requested_security_levels(
    request: Any,
    auth: AuthContext,
) -> frozenset[str]:
    allowed = ROLE_SECURITY_LEVELS.get(auth.role, frozenset())
    if not request.security_levels:
        return allowed
    return frozenset(
        str(level or "").strip().lower()
        for level in request.security_levels
        if str(level or "").strip()
    )


def requested_department_ids(
    request: Any,
    auth: AuthContext,
) -> frozenset[str]:
    requested = frozenset(department_acl_set(request.department_ids))
    if not requested:
        return frozenset()
    if auth.role == API_ROLE_ADMIN:
        return requested
    allowed = frozenset(str(item) for item in auth.department_ids)
    if not requested.issubset(allowed):
        raise HTTPException(
            status_code=403,
            detail="Requested department is not allowed for this API token.",
        )
    return requested


def validate_security_scope(request: Any, auth: AuthContext) -> None:
    requested = requested_security_levels(request, auth)
    allowed = ROLE_SECURITY_LEVELS.get(auth.role, frozenset())
    if not requested.issubset(allowed):
        raise HTTPException(
            status_code=403,
            detail="Requested security level is not allowed for this API role.",
        )


def validate_query_policy(query: str) -> None:
    normalized = " ".join(str(query or "").split())
    for pattern in BLOCKED_QUERY_PATTERNS:
        if pattern.search(normalized):
            raise HTTPException(
                status_code=400,
                detail="Query was blocked by the local RAG input policy.",
            )


def local_vector_path(settings: Any, auth: AuthContext) -> Path:
    return (
        settings.data_dir
        / "vector_db"
        / tenant_storage_key(auth.tenant_id)
        / "approved_vectors.jsonl"
    )


def bm25_index_path(settings: Any, auth: AuthContext) -> Path:
    return default_bm25_index_path(local_vector_path(settings, auth))


def path_signature(path: Path) -> _FileIdentitySignature | None:
    try:
        path_stat = path.stat()
    except OSError:
        return None
    # Keep the historical (mtime_ns, size) prefix. MCP warmup accounting reads
    # index 1 as the byte count; ctime/inode extend the invalidation identity.
    return _file_identity_signature(path, path_stat)


def _windows_file_change_time_ns(path: Path) -> int | None:
    """Return NTFS ChangeTime as Unix-epoch nanoseconds when on Windows."""

    if os.name != "nt":
        return None
    handle = _WINDOWS_CREATE_FILE(
        str(path),
        _WINDOWS_FILE_READ_ATTRIBUTES,
        _WINDOWS_FILE_SHARE_ALL,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == _WINDOWS_INVALID_HANDLE_VALUE:
        return None
    try:
        basic_info = _WindowsFileBasicInfo()
        if not _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE_EX(
            handle,
            _WINDOWS_FILE_BASIC_INFO_CLASS,
            ctypes.byref(basic_info),
            ctypes.sizeof(basic_info),
        ):
            return None
        if basic_info.ChangeTime <= 0:
            return None
        return int(
            (basic_info.ChangeTime - _WINDOWS_EPOCH_FILETIME) * 100
        )
    finally:
        _WINDOWS_CLOSE_HANDLE(handle)


if os.name == "nt":
    def _windows_filetime_to_unix_ns(value: int) -> int:
        return int((value - _WINDOWS_EPOCH_FILETIME) * 100)


    def _windows_parse_directory_identity_buffer(
        buffer: Any,
        buffer_size: int,
    ) -> list[_WindowsDirectoryIdentity] | None:
        """Parse one FILE_ID_BOTH_DIR_INFO chain without trusting offsets."""

        if (
            not _WINDOWS_DIRECTORY_LAYOUT_IS_VALID
            or buffer_size < ctypes.sizeof(
                _WindowsFileIdBothDirectoryInfo
            )
        ):
            return None
        entries: list[_WindowsDirectoryIdentity] = []
        offset = 0
        record_limit = (
            buffer_size // _WINDOWS_DIRECTORY_FILE_NAME_OFFSET
        ) + 1
        for _record_index in range(record_limit):
            if (
                offset % 8 != 0
                or offset + _WINDOWS_DIRECTORY_FILE_NAME_OFFSET
                > buffer_size
            ):
                return None
            try:
                record = _WindowsFileIdBothDirectoryInfo.from_buffer(
                    buffer,
                    offset,
                )
            except (TypeError, ValueError):
                return None
            name_length = int(record.FileNameLength)
            next_offset = int(record.NextEntryOffset)
            if name_length <= 0 or name_length % 2 != 0:
                return None
            name_start = offset + _WINDOWS_DIRECTORY_FILE_NAME_OFFSET
            name_end = name_start + name_length
            record_end = (
                offset + next_offset
                if next_offset
                else buffer_size
            )
            if name_end > record_end or record_end > buffer_size:
                return None
            if next_offset and (
                next_offset % 8 != 0
                or next_offset
                < _WINDOWS_DIRECTORY_FILE_NAME_OFFSET + name_length
                or offset + next_offset <= offset
                or offset
                + next_offset
                + _WINDOWS_DIRECTORY_FILE_NAME_OFFSET
                > buffer_size
            ):
                return None
            try:
                raw_name = ctypes.string_at(
                    ctypes.addressof(buffer) + name_start,
                    name_length,
                )
                name = raw_name.decode("utf-16-le", errors="strict")
            except (TypeError, ValueError, UnicodeDecodeError):
                return None
            if (
                not name
                or "\x00" in name
                or "/" in name
                or "\\" in name
            ):
                return None
            last_write_time = int(record.LastWriteTime)
            change_time = int(record.ChangeTime)
            size = int(record.EndOfFile)
            if (
                last_write_time <= 0
                or change_time <= 0
                or size < 0
            ):
                return None
            entries.append(
                _WindowsDirectoryIdentity(
                    name=name,
                    last_write_time_ns=_windows_filetime_to_unix_ns(
                        last_write_time
                    ),
                    size=size,
                    change_time_ns=_windows_filetime_to_unix_ns(
                        change_time
                    ),
                    attributes=int(record.FileAttributes),
                    file_id=(
                        int(record.FileId)
                        & ((1 << 64) - 1)
                    ),
                )
            )
            if next_offset == 0:
                return entries
            offset += next_offset
        return None


    def _windows_index_directory_identities(
        entries: Iterable[_WindowsDirectoryIdentity],
    ) -> dict[str, _WindowsDirectoryIdentity] | None:
        indexed: dict[str, _WindowsDirectoryIdentity] = {}
        folded_names: set[str] = set()
        for entry in entries:
            folded_name = entry.name.casefold()
            if (
                entry.name in indexed
                or folded_name in folded_names
            ):
                return None
            indexed[entry.name] = entry
            folded_names.add(folded_name)
        return indexed


    def _windows_enumerate_directory_identities(
        directory: Path,
    ) -> dict[str, _WindowsDirectoryIdentity] | None:
        """Enumerate one directory handle completely or return no data."""

        if not _WINDOWS_DIRECTORY_LAYOUT_IS_VALID:
            return None
        handle = _WINDOWS_CREATE_FILE(
            str(directory),
            _WINDOWS_FILE_LIST_DIRECTORY
            | _WINDOWS_FILE_READ_ATTRIBUTES,
            _WINDOWS_FILE_SHARE_ALL,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if (
            handle is None
            or handle == _WINDOWS_INVALID_HANDLE_VALUE
        ):
            return None

        buffer = ctypes.create_string_buffer(
            _WINDOWS_DIRECTORY_BUFFER_SIZE
        )
        entries: list[_WindowsDirectoryIdentity] = []
        enumeration_complete = False
        enumeration_failed = False
        try:
            while True:
                ctypes.memset(
                    buffer,
                    0,
                    _WINDOWS_DIRECTORY_BUFFER_SIZE,
                )
                ctypes.set_last_error(0)
                succeeded = bool(
                    _WINDOWS_GET_FILE_INFORMATION_BY_HANDLE_EX(
                        handle,
                        _WINDOWS_FILE_ID_BOTH_DIRECTORY_INFO_CLASS,
                        buffer,
                        _WINDOWS_DIRECTORY_BUFFER_SIZE,
                    )
                )
                if not succeeded:
                    if (
                        ctypes.get_last_error()
                        == _WINDOWS_ERROR_NO_MORE_FILES
                    ):
                        enumeration_complete = True
                    else:
                        enumeration_failed = True
                    break
                batch = _windows_parse_directory_identity_buffer(
                    buffer,
                    _WINDOWS_DIRECTORY_BUFFER_SIZE,
                )
                if not batch:
                    enumeration_failed = True
                    break
                entries.extend(batch)
        finally:
            close_succeeded = bool(_WINDOWS_CLOSE_HANDLE(handle))
        if (
            enumeration_failed
            or not enumeration_complete
            or not close_succeeded
        ):
            return None
        return _windows_index_directory_identities(entries)


def _file_identity_signature(
    path: Path,
    path_stat: Any,
) -> _FileIdentitySignature | None:
    change_time_ns = path_stat.st_ctime_ns
    if os.name == "nt":
        change_time_ns = _windows_file_change_time_ns(path)
        if change_time_ns is None:
            return None
    return (
        path_stat.st_mtime_ns,
        path_stat.st_size,
        change_time_ns,
        path_stat.st_ino,
    )


def load_cached_bm25_index(path: Path) -> Bm25Index | None:
    signature = path_signature(path)
    if signature is None:
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_BM25_INDEX_CACHE.pop(path, None)
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_BM25_INDEX_CACHE.get(path)
        if cached and cached[0] == signature:
            return cached[1]
    index = load_bm25_index(path)
    with _RAG_VECTOR_CACHE_LOCK:
        if index is None:
            _RAG_BM25_INDEX_CACHE.pop(path, None)
        else:
            _RAG_BM25_INDEX_CACHE[path] = (signature, index)
    return index


def store_cached_bm25_index(path: Path, index: Bm25Index) -> None:
    signature = path_signature(path)
    if signature is None:
        return
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_BM25_INDEX_CACHE[path] = (signature, index)


def runtime_approval_snapshot_path(repository: Any) -> Path:
    return repository.root / "approval_snapshot.json"


def _repository_chunk_path_lstat(
    path: Path,
    *,
    repository_directory: Path,
    repository_root: Path,
    allow_missing: bool,
) -> Any | _MissingFileIdentitySignature | None:
    """Return validated lstat metadata without following a chunk link."""

    # Normal candidates are direct children of the repository directory,
    # whose resolved containment was established once by the caller.
    # Resolve only an unusual parent (for example, an escaped document ID).
    if path.parent != repository_directory:
        try:
            resolved_parent = path.parent.resolve(strict=True)
        except OSError:
            return None
        if resolved_parent != repository_root:
            return None
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return _MISSING_FILE_IDENTITY
        return None
    except OSError:
        return None

    file_attributes = int(getattr(path_stat, "st_file_attributes", 0))
    reparse_point = int(
        getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if (
        stat_module.S_ISLNK(path_stat.st_mode)
        or file_attributes & reparse_point
        or not stat_module.S_ISREG(path_stat.st_mode)
    ):
        return None
    return path_stat


def _repository_chunk_path_identity(
    path: Path,
    *,
    repository_directory: Path,
    repository_root: Path,
    allow_missing: bool,
) -> tuple[
    Path,
    _FileIdentitySignature | _MissingFileIdentitySignature,
] | None:
    """Return one fail-closed lstat identity for a repository chunk path."""

    path_stat = _repository_chunk_path_lstat(
        path,
        repository_directory=repository_directory,
        repository_root=repository_root,
        allow_missing=allow_missing,
    )
    if path_stat is None:
        return None
    if path_stat is _MISSING_FILE_IDENTITY:
        return (path, _MISSING_FILE_IDENTITY)
    signature = _file_identity_signature(path, path_stat)
    if signature is None:
        return None
    return (path, signature)


if os.name == "nt":
    def _windows_directory_identity_signature(
        path_stat: Any,
        identity: _WindowsDirectoryIdentity,
    ) -> _FileIdentitySignature | None:
        """Match bulk metadata to the independently acquired lstat."""

        stat_attributes = getattr(
            path_stat,
            "st_file_attributes",
            None,
        )
        if stat_attributes is None:
            return None
        if (
            int(path_stat.st_mtime_ns)
            != identity.last_write_time_ns
            or int(path_stat.st_size) != identity.size
            or int(stat_attributes) != identity.attributes
            or int(path_stat.st_ino) != identity.file_id
            or identity.change_time_ns <= 0
        ):
            return None
        return (
            identity.last_write_time_ns,
            identity.size,
            identity.change_time_ns,
            identity.file_id,
        )


    def _windows_repository_chunk_entries(
        *,
        repository_directory: Path,
        repository_root: Path,
        chunk_paths: Iterable[Path],
    ) -> list[tuple[Path, _FileIdentitySignature]] | None:
        """Build all chunk identities or fail without returning a subset."""

        requested_paths = list(chunk_paths)
        requested_names: set[str] = set()
        requested_folded_names: set[str] = set()
        validated_stats: dict[str, Any] = {}
        paths_by_name: dict[str, Path] = {}
        for path in requested_paths:
            name = path.name
            folded_name = name.casefold()
            if (
                name in requested_names
                or folded_name in requested_folded_names
            ):
                return None
            path_stat = _repository_chunk_path_lstat(
                path,
                repository_directory=repository_directory,
                repository_root=repository_root,
                allow_missing=False,
            )
            if (
                path_stat is None
                or path_stat is _MISSING_FILE_IDENTITY
            ):
                return None
            requested_names.add(name)
            requested_folded_names.add(folded_name)
            validated_stats[name] = path_stat
            paths_by_name[name] = path

        enumerated = _windows_enumerate_directory_identities(
            repository_directory
        )
        if enumerated is None:
            return None
        enumerated_chunk_names = {
            name
            for name in enumerated
            if name.casefold().endswith("_chunks.json")
        }
        if enumerated_chunk_names != requested_names:
            return None

        chunk_entries: list[
            tuple[Path, _FileIdentitySignature]
        ] = []
        for name in sorted(requested_names):
            identity = enumerated.get(name)
            if identity is None:
                return None
            signature = _windows_directory_identity_signature(
                validated_stats[name],
                identity,
            )
            if signature is None:
                return None
            chunk_entries.append((paths_by_name[name], signature))
        if len(chunk_entries) != len(requested_paths):
            return None
        return chunk_entries


def _repository_chunk_path_is_safe(
    path: Path,
    *,
    repository_root: Path,
    allow_missing: bool,
) -> bool:
    """Reject links and paths that can escape the repository chunk directory."""

    return (
        _repository_chunk_path_identity(
            path,
            repository_directory=repository_root,
            repository_root=repository_root,
            allow_missing=allow_missing,
        )
        is not None
    )


def _runtime_approval_identity_chunk_entries(
    repository: Any,
    *,
    document_ids: Iterable[str] | None,
) -> list[
    tuple[Path, _FileIdentitySignature | _MissingFileIdentitySignature]
] | None:
    repository_directory = repository.root
    try:
        repository_root = repository_directory.resolve(strict=True)
    except OSError:
        return None
    if document_ids is None:
        try:
            chunk_paths = sorted(
                repository_directory.glob("*_chunks.json"),
                key=lambda candidate: candidate.name,
            )
        except OSError:
            return None
    else:
        chunk_paths = [
            repository_directory / f"{document_id}_chunks.json"
            for document_id in sorted(
                {
                    str(value or "").strip()
                    for value in document_ids
                    if str(value or "").strip()
                }
            )
        ]

    if document_ids is None and os.name == "nt":
        return _windows_repository_chunk_entries(
            repository_directory=repository_directory,
            repository_root=repository_root,
            chunk_paths=chunk_paths,
        )

    chunk_entries: list[
        tuple[Path, _FileIdentitySignature | _MissingFileIdentitySignature]
    ] = []
    for path in chunk_paths:
        entry = _repository_chunk_path_identity(
            path,
            repository_directory=repository_directory,
            repository_root=repository_root,
            allow_missing=document_ids is not None,
        )
        if entry is None:
            return None
        chunk_entries.append(entry)
    return chunk_entries


def runtime_approval_identity_chunk_paths(
    repository: Any,
    *,
    document_ids: Iterable[str] | None,
) -> list[Path] | None:
    chunk_entries = _runtime_approval_identity_chunk_entries(
        repository,
        document_ids=document_ids,
    )
    if chunk_entries is None:
        return None
    return [path for path, _signature in chunk_entries]


def runtime_approval_snapshot_identity(
    repository: Any,
    document_ids: Iterable[str] | None = None,
) -> tuple[Any, ...] | None:
    """Return a cheap identity for every authorization source in scope."""

    runtime_manifest_path = repository.root.parent / "mcp_runtime_manifest.json"
    sidecar_path = runtime_approval_snapshot_path(repository)
    runtime_manifest_signature = path_signature(runtime_manifest_path)
    sidecar_signature = path_signature(sidecar_path)
    if runtime_manifest_signature is None or sidecar_signature is None:
        return None
    chunk_entries = _runtime_approval_identity_chunk_entries(
        repository,
        document_ids=document_ids,
    )
    if chunk_entries is None:
        return None
    # A missing expected file remains explicit in a document-scoped identity
    # so an older verified superset cannot authorize the document vacuously.
    chunk_signatures = [
        (path.name, signature)
        for path, signature in chunk_entries
    ]
    return (
        runtime_manifest_signature,
        sidecar_signature,
        path_signature(repository.manifest_path),
        path_signature(repository.legacy_path),
        path_signature(repository.root / "journals" / "approvals.jsonl"),
        tuple(chunk_signatures),
    )


def runtime_approval_identity_covers_scope(
    cached_identity: tuple[Any, ...],
    scoped_identity: tuple[Any, ...],
) -> bool:
    if len(cached_identity) != 6 or len(scoped_identity) != 6:
        return cached_identity == scoped_identity
    if cached_identity[:5] != scoped_identity[:5]:
        return False
    try:
        cached_chunk_signatures = dict(cached_identity[5])
        scoped_chunk_signatures = dict(scoped_identity[5])
    except (TypeError, ValueError):
        return False
    return all(
        cached_chunk_signatures.get(name) == signature
        for name, signature in scoped_chunk_signatures.items()
    )


def store_runtime_approval_identity_cache(
    cache_key: tuple[Path, str, tuple[str, ...]],
    source_identity: tuple[Any, ...],
    snapshot: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """Store a verified snapshot while the shared cache lock is held."""

    _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE[cache_key] = (
        source_identity,
        snapshot,
    )
    _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.move_to_end(cache_key)
    while (
        len(_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE)
        > _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE_MAX_ENTRIES
    ):
        _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.popitem(last=False)


def runtime_approval_snapshot_signature(
    repository: Any,
    document_ids: list[str],
) -> tuple[Any, ...] | None:
    sidecar_path = runtime_approval_snapshot_path(repository)
    if not sidecar_path.is_file():
        return None
    runtime_manifest_path = repository.root.parent / "mcp_runtime_manifest.json"
    if not runtime_manifest_path.is_file():
        return None
    file_signatures = runtime_approval_snapshot_file_signatures(repository)
    if file_signatures["repository_chunk_files"] is None:
        return None
    return (
        "runtime_approval_snapshot_sidecar",
        tuple(document_ids),
        path_signature(runtime_manifest_path),
        path_signature(sidecar_path),
        file_signatures,
    )


def portable_file_signature(path: Path) -> tuple[int, str] | None:
    stat_signature = path_signature(path)
    if stat_signature is None:
        return None
    with _RUNTIME_CONTENT_SIGNATURE_LOCK:
        cached = _RUNTIME_CONTENT_SIGNATURE_CACHE.get(path)
        if cached and cached[0] == stat_signature:
            return cached[1]
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    digest.update(block)
        except OSError:
            return None
        signature = (int(stat_signature[1]), digest.hexdigest())
        _RUNTIME_CONTENT_SIGNATURE_CACHE[path] = (stat_signature, signature)
        return signature


def repository_chunk_files_signature(
    repository: Any,
) -> tuple[int, str] | None:
    chunk_paths = runtime_approval_identity_chunk_paths(
        repository,
        document_ids=None,
    )
    if chunk_paths is None:
        return None
    file_signatures: list[tuple[str, tuple[int, str]]] = []
    for path in chunk_paths:
        signature = portable_file_signature(path)
        if signature is None:
            return None
        file_signatures.append((path.name, signature))
    digest = hashlib.sha256(
        json.dumps(
            file_signatures,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    total_bytes = sum(
        int(signature[1][0])
        for signature in file_signatures
    )
    return (total_bytes, digest)


def runtime_approval_snapshot_file_signatures(
    repository: Any,
) -> dict[str, tuple[Any, ...] | None]:
    return {
        "repository_manifest": portable_file_signature(
            repository.manifest_path
        ),
        "legacy_repository": portable_file_signature(repository.legacy_path),
        "approval_journal": portable_file_signature(
            repository.root / "journals" / "approvals.jsonl"
        ),
        "repository_chunk_files": repository_chunk_files_signature(repository),
    }


def load_runtime_approval_snapshot_sidecar(
    repository: Any,
    document_ids: list[str],
    auth: AuthContext,
) -> dict[tuple[str, str], dict[str, Any]] | None:
    sidecar_path = runtime_approval_snapshot_path(repository)
    runtime_manifest_path = repository.root.parent / "mcp_runtime_manifest.json"
    try:
        runtime_manifest = json.loads(
            runtime_manifest_path.read_text(encoding="utf-8-sig")
        )
        payload = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(runtime_manifest, dict)
        or runtime_manifest.get("report_type") != "mcp_runtime_data_bundle"
    ):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("report_type") != "mcp_runtime_approval_snapshot"
        or payload.get("schema_version")
        != "mcp-runtime-approval-snapshot-v1"
    ):
        return None
    runtime_reuse = runtime_manifest.get("runtime_data_reuse")
    runtime_file_hashes = (
        runtime_reuse.get("file_sha256")
        if isinstance(runtime_reuse, dict)
        else None
    )
    if not isinstance(runtime_file_hashes, dict):
        return None
    try:
        sidecar_relative_path = sidecar_path.relative_to(
            repository.root.parent
        ).as_posix()
    except ValueError:
        return None
    expected_sidecar_hash = str(
        runtime_file_hashes.get(sidecar_relative_path) or ""
    ).strip().lower()
    sidecar_content_signature = portable_file_signature(sidecar_path)
    if (
        not re.fullmatch(r"[a-f0-9]{64}", expected_sidecar_hash)
        or sidecar_content_signature is None
        or sidecar_content_signature[1] != expected_sidecar_hash
    ):
        return None

    tenant_id = str(
        payload.get("tenant_id") or runtime_manifest.get("tenant_id") or ""
    )
    if tenant_id and tenant_id != auth.tenant_id:
        return None
    manifest_ids = {
        str(value or "")
        for value in (
            runtime_manifest.get("document_ids")
            or payload.get("document_ids")
            or []
        )
        if str(value or "").strip()
    }
    sidecar_ids = {
        str(value or "")
        for value in (payload.get("document_ids") or [])
        if str(value or "").strip()
    }
    requested_ids = {
        document_id for document_id in document_ids if document_id
    }
    if (
        not requested_ids.issubset(manifest_ids or sidecar_ids)
        or not requested_ids.issubset(sidecar_ids)
    ):
        return None
    payload_signatures = payload.get("file_signatures")
    if not isinstance(payload_signatures, dict):
        return None
    current_file_signatures = runtime_approval_snapshot_file_signatures(
        repository
    )
    if current_file_signatures["repository_chunk_files"] is None:
        return None
    for key, expected in current_file_signatures.items():
        actual = payload_signatures.get(key)
        if (list(expected) if expected is not None else None) != actual:
            return None

    entries = payload.get("entries")
    if (
        not isinstance(entries, list)
        or payload.get("record_count") != len(entries)
        or payload.get("snapshot_count") != len(entries)
    ):
        return None
    snapshot: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        document_id = str(entry.get("document_id") or "")
        chunk_id = str(entry.get("chunk_id") or "")
        if document_id not in requested_ids or not chunk_id:
            continue
        security_level = str(
            entry.get("security_level") or ""
        ).strip().lower()
        if security_level not in SECURITY_LEVEL_ORDER:
            continue
        snapshot[(document_id, chunk_id)] = {
            "approval_id": entry.get("approval_id"),
            "approved_content_hash": entry.get("approved_content_hash"),
            "security_level": security_level,
            "department_acl": department_acl_set(entry.get("department_acl")),
            "content_hash": str(entry.get("content_hash") or ""),
        }
    return snapshot


def load_cached_runtime_approval_snapshot(
    repository: Any,
    document_ids: list[str],
    auth: AuthContext,
    *,
    identity_loader: Callable[
        [Any, Iterable[str] | None],
        tuple[Any, ...] | None,
    ]
    | None = None,
    signature_loader: Callable[
        [Any, list[str]],
        tuple[Any, ...] | None,
    ]
    | None = None,
    sidecar_loader: Callable[
        [Any, list[str], AuthContext],
        dict[tuple[str, str], dict[str, Any]] | None,
    ]
    | None = None,
) -> dict[tuple[str, str], dict[str, Any]] | None:
    """Load and TOCTOU-revalidate a manifest-pinned approval sidecar."""

    identity_loader = identity_loader or runtime_approval_snapshot_identity
    signature_loader = signature_loader or runtime_approval_snapshot_signature
    sidecar_loader = sidecar_loader or load_runtime_approval_snapshot_sidecar
    normalized_document_ids = sorted(
        {
            str(document_id or "").strip()
            for document_id in document_ids
            if str(document_id or "").strip()
        }
    )
    cache_key = (
        repository.root,
        auth.tenant_id,
        tuple(normalized_document_ids),
    )
    source_identity = identity_loader(repository, normalized_document_ids)
    if source_identity is None:
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        identity_cached = _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.get(cache_key)
        if identity_cached and identity_cached[0] == source_identity:
            _RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.move_to_end(cache_key)
            cached_snapshot = identity_cached[1]
        else:
            cached_snapshot = None
    if cached_snapshot is not None:
        if (
            identity_loader(repository, normalized_document_ids)
            != source_identity
        ):
            return None
        return cached_snapshot

    requested_document_ids = frozenset(normalized_document_ids)
    derived_snapshot: dict[tuple[str, str], dict[str, Any]] | None = None
    with _RAG_VECTOR_CACHE_LOCK:
        for superset_key, (
            superset_identity,
            superset_snapshot,
        ) in reversed(list(_RAG_RUNTIME_APPROVAL_IDENTITY_CACHE.items())):
            if (
                superset_key[0] != repository.root
                or superset_key[1] != auth.tenant_id
                or not requested_document_ids.issubset(superset_key[2])
                or not runtime_approval_identity_covers_scope(
                    superset_identity,
                    source_identity,
                )
            ):
                continue
            derived_snapshot = {
                key: value
                for key, value in superset_snapshot.items()
                if key[0] in requested_document_ids
            }
            store_runtime_approval_identity_cache(
                cache_key,
                source_identity,
                derived_snapshot,
            )
            break
    if derived_snapshot is not None:
        if (
            identity_loader(repository, normalized_document_ids)
            != source_identity
        ):
            return None
        return derived_snapshot

    signature = signature_loader(repository, normalized_document_ids)
    if signature is None:
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_APPROVAL_SNAPSHOT_CACHE.get(cache_key)
        if cached and cached[0] == signature:
            store_runtime_approval_identity_cache(
                cache_key,
                source_identity,
                cached[1],
            )
            return cached[1]

    snapshot = sidecar_loader(repository, normalized_document_ids, auth)
    if snapshot is None:
        return None
    if identity_loader(repository, normalized_document_ids) != source_identity:
        return None
    if signature_loader(repository, normalized_document_ids) != signature:
        return None
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_APPROVAL_SNAPSHOT_CACHE[cache_key] = (signature, snapshot)
        store_runtime_approval_identity_cache(
            cache_key,
            source_identity,
            snapshot,
        )
    return snapshot


def chunk_path_identity_signature(
    path: Path,
) -> tuple[int, int, int, int] | None:
    signature = path_signature(path)
    if signature is None:
        return None
    return (
        signature[0],
        signature[2],
        signature[1],
        signature[3],
    )


def approval_journal_cache_path(repository: Any) -> Path | None:
    root = getattr(repository, "root", None)
    if root is None:
        return None
    return Path(root) / "journals" / "approvals.jsonl"


def approval_journal_records_by_document(
    repository: Any,
    document_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    selected_document_ids = set(document_ids)
    journal_path = approval_journal_cache_path(repository)
    journal_signature = (
        path_signature(journal_path) if journal_path is not None else None
    )
    if journal_path is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            cached = _RAG_APPROVAL_JOURNAL_CACHE.get(journal_path)
            if cached and cached[0] == journal_signature:
                _RAG_APPROVAL_JOURNAL_CACHE.move_to_end(journal_path)
                return {
                    document_id: list(cached[1].get(document_id, ()))
                    for document_id in document_ids
                }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in repository.list_approval_journal_records():
        if not isinstance(record, dict):
            continue
        document_id = str(record.get("document_id") or "")
        if document_id:
            grouped[document_id].append(record)

    if (
        journal_path is not None
        and path_signature(journal_path) == journal_signature
    ):
        immutable_grouped = {
            document_id: tuple(records)
            for document_id, records in grouped.items()
        }
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_APPROVAL_JOURNAL_CACHE[journal_path] = (
                journal_signature,
                immutable_grouped,
            )
            _RAG_APPROVAL_JOURNAL_CACHE.move_to_end(journal_path)
            while (
                len(_RAG_APPROVAL_JOURNAL_CACHE)
                > _RAG_APPROVAL_JOURNAL_CACHE_MAX_ENTRIES
            ):
                _RAG_APPROVAL_JOURNAL_CACHE.popitem(last=False)

    return {
        document_id: list(grouped.get(document_id, ()))
        for document_id in selected_document_ids
    }


def approval_journal_signature(
    repository: Any,
    document_ids: list[str],
) -> str:
    try:
        records_by_document = approval_journal_records_by_document(
            repository,
            document_ids,
        )
        records = [
            record
            for document_id in document_ids
            for record in records_by_document.get(document_id, ())
        ]
    except Exception:
        records = []
    payload = [
        {
            "approval_record_id": record.get("approval_record_id"),
            "approval_id": record.get("approval_id"),
            "document_id": record.get("document_id"),
            "chunk_ids": record.get("chunk_ids"),
            "approved_content_hashes": record.get(
                "approved_content_hashes"
            ),
            "worklist_evidence": record.get("worklist_evidence"),
            "tenant_id": record.get("tenant_id"),
            "approved_at": record.get("approved_at"),
        }
        for record in records
        if isinstance(record, dict)
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def repository_documents_signature(
    repository: Any,
    document_ids: list[str],
) -> str:
    cache_key = (repository.root, tuple(document_ids))
    source_signature = (
        path_signature(repository.manifest_path),
        path_signature(repository.legacy_path),
    )
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE.get(cache_key)
        if cached and cached[0] == source_signature:
            return cached[1]
    try:
        manifest = repository._read_manifest()
        legacy = repository._read_legacy()
    except Exception:
        payload = [[document_id, None] for document_id in document_ids]
    else:
        manifest_documents = (
            manifest.get("documents", {})
            if isinstance(manifest, dict)
            else {}
        )
        legacy_documents = (
            legacy.get("documents", {})
            if isinstance(legacy, dict)
            else {}
        )
        payload = [
            [
                document_id,
                manifest_documents.get(document_id)
                if document_id in manifest_documents
                else legacy_documents.get(document_id),
            ]
            for document_id in document_ids
        ]
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_REPOSITORY_DOCUMENT_SIGNATURE_CACHE[cache_key] = (
            source_signature,
            digest,
        )
    return digest


def approval_snapshot_signature(
    repository: Any,
    document_ids: list[str],
) -> tuple[Any, ...] | None:
    if (
        runtime_approval_identity_chunk_paths(
            repository,
            document_ids=document_ids,
        )
        is None
    ):
        return None
    chunk_signatures = tuple(
        (
            document_id,
            chunk_path_identity_signature(
                repository.root / f"{document_id}_chunks.json"
            ),
        )
        for document_id in document_ids
    )
    return (
        repository_documents_signature(repository, document_ids),
        path_signature(repository.legacy_path),
        chunk_signatures,
        approval_journal_signature(repository, document_ids),
    )


class RagRequestRepositoryCache:
    def __init__(self, repository: Any) -> None:
        self._repository = repository
        self._documents: dict[str, Any | None] = {}
        self._chunks_by_document: dict[str, dict[str, Any]] = {}

    def get_document(self, document_id: str) -> Any | None:
        if document_id not in self._documents:
            self._documents[document_id] = self._repository.get_document(
                document_id
            )
        return self._documents[document_id]

    def get_chunk(self, document_id: str, chunk_id: str) -> Any | None:
        if document_id not in self._chunks_by_document:
            self._chunks_by_document[document_id] = {
                str(chunk.chunk_id): chunk
                for chunk in self._repository.get_chunks(document_id)
            }
        return self._chunks_by_document[document_id].get(chunk_id)


def iter_local_vector_lines(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            yield line_no, line


def validated_local_vector_record(
    record: dict[str, Any],
) -> dict[str, Any] | None:
    from app.ingestion.vector_adapter import stable_content_hash
    from app.ingestion.vector_upsert import validate_vector_records

    try:
        validated_records = validate_vector_records([record])
    except ValueError:
        return None
    for validated_record in validated_records:
        metadata_value = validated_record.get("metadata")
        metadata = (
            metadata_value if isinstance(metadata_value, dict) else {}
        )
        if stable_content_hash(
            str(validated_record.get("text") or ""),
            metadata,
        ) != str(validated_record.get("content_hash") or ""):
            continue
        return validated_record
    return None


def local_vector_record_matches_chunk(
    record: dict[str, Any],
    *,
    document_id: str,
    chunk_id: str,
) -> bool:
    metadata_value = record.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    return (
        str(record.get("document_id") or metadata.get("document_id") or "")
        == document_id
        and str(record.get("chunk_id") or metadata.get("chunk_id") or "")
        == chunk_id
    )


def read_local_vector_records(
    path: Path,
    *,
    line_iterator: Callable[[Path], Iterable[tuple[int, str]]] | None = None,
) -> list[dict[str, Any]]:
    line_iterator = line_iterator or iter_local_vector_lines
    validated: list[dict[str, Any]] = []
    for line_no, line in line_iterator(path):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Invalid local vector store JSONL "
                    f"at line {line_no}."
                ),
            ) from exc
        if isinstance(record, dict):
            validated_record = validated_local_vector_record(record)
            if validated_record is not None:
                validated.append(validated_record)
    return validated


def load_local_vector_records(
    settings: Any,
    auth: AuthContext,
    *,
    record_reader: Callable[[Path], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    record_reader = record_reader or read_local_vector_records
    path = local_vector_path(settings, auth)
    if not path.is_file():
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_VECTOR_RECORD_CACHE.pop(path, None)
        return []
    signature = path_signature(path)
    if signature is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            cached = _RAG_VECTOR_RECORD_CACHE.get(path)
            if cached and cached[0] == signature:
                return list(cached[1])
            # Serialize the first read for one signature. The vector JSONL can
            # be large and concurrent cold callers must not stampede it.
            validated = record_reader(path)
            _RAG_VECTOR_RECORD_CACHE[path] = (
                signature,
                list(validated),
            )
            return list(validated)
    return record_reader(path)


def load_local_vector_record_by_chunk(
    settings: Any,
    auth: AuthContext,
    *,
    document_id: str,
    chunk_id: str,
    line_iterator: Callable[[Path], Iterable[tuple[int, str]]] | None = None,
) -> dict[str, Any] | None:
    line_iterator = line_iterator or iter_local_vector_lines
    path = local_vector_path(settings, auth)
    if not path.is_file():
        with _RAG_VECTOR_CACHE_LOCK:
            _RAG_VECTOR_RECORD_CACHE.pop(path, None)
        return None
    signature = path_signature(path)
    if signature is not None:
        with _RAG_VECTOR_CACHE_LOCK:
            cached = _RAG_VECTOR_RECORD_CACHE.get(path)
            if cached and cached[0] == signature:
                candidate = None
                for record in cached[1]:
                    if local_vector_record_matches_chunk(
                        record,
                        document_id=document_id,
                        chunk_id=chunk_id,
                    ):
                        candidate = record
                return candidate
    candidate = None
    for line_no, line in line_iterator(path):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Invalid local vector store JSONL "
                    f"at line {line_no}."
                ),
            ) from exc
        if (
            not isinstance(record, dict)
            or not local_vector_record_matches_chunk(
                record,
                document_id=document_id,
                chunk_id=chunk_id,
            )
        ):
            continue
        validated = validated_local_vector_record(record)
        if validated is not None:
            candidate = validated
    return candidate


def current_repository_chunk(
    repository: Any,
    document_id: str,
    chunk_id: str,
    *,
    repository_cache: RagRequestRepositoryCache | None = None,
) -> Any | None:
    if repository_cache is not None:
        return repository_cache.get_chunk(document_id, chunk_id)
    for chunk in repository.get_chunks(document_id):
        if chunk.chunk_id == chunk_id:
            return chunk
    return None


def expected_vector_record_for_chunk(
    chunk: Any,
    document: Any,
    auth: AuthContext,
) -> dict[str, Any] | None:
    from app.ingestion.vector_adapter import vector_record_from_chunk

    chunk_data = chunk.model_dump(mode="json")
    metadata = dict(chunk_data.get("metadata") or {})
    for key, value in {
        "institution_name": getattr(document, "institution_name", None),
        "apba_id": getattr(document, "apba_id", None),
        "source_system": getattr(document, "source_system", None),
        "source_url": getattr(document, "source_url", None),
        "source_record_id": getattr(document, "source_record_id", None),
        "source_file_id": getattr(document, "source_file_id", None),
        "source_disclosure_date": getattr(
            document,
            "source_disclosure_date",
            None,
        ),
        "source_posted_date": getattr(
            document,
            "source_posted_date",
            None,
        ),
        "profile_id": getattr(document, "profile_id", None),
    }.items():
        if value and not metadata.get(key):
            metadata[key] = value
    metadata["tenant_id"] = document.tenant_id or auth.tenant_id
    chunk_data["tenant_id"] = document.tenant_id or auth.tenant_id
    chunk_data["department_acl"] = sorted(
        department_acl_set(chunk.department_acl)
    )
    chunk_data["metadata"] = metadata
    return vector_record_from_chunk(chunk_data)


def approval_journal_match_key(
    *,
    chunk_id: str,
    document_id: str,
    tenant_id: str,
    approval_id: str,
    approved_content_hash: str,
    expected_metadata: dict[str, Any],
) -> tuple[Any, ...]:
    from app.services.review_decision_service import (
        APPROVAL_WORKLIST_METADATA_KEYS,
    )

    return (
        str(document_id),
        str(tenant_id),
        str(approval_id),
        str(chunk_id),
        str(approved_content_hash),
        tuple(
            (key, str(expected_metadata.get(key) or ""))
            for key in sorted(APPROVAL_WORKLIST_METADATA_KEYS)
        ),
    )


def approval_journal_match_index(
    records: Iterable[dict[str, Any]],
) -> set[tuple[Any, ...]]:
    from app.services.review_decision_service import (
        APPROVAL_WORKLIST_METADATA_KEYS,
        approval_worklist_metadata,
    )

    index: set[tuple[Any, ...]] = set()
    expected_worklist_keys = set(APPROVAL_WORKLIST_METADATA_KEYS)
    for record in records:
        if not isinstance(record, dict):
            continue
        document_id = str(record.get("document_id") or "")
        tenant_id = str(record.get("tenant_id") or "")
        approval_id = str(record.get("approval_id") or "")
        if not document_id or not tenant_id or not approval_id:
            continue
        worklist_evidence = record.get("worklist_evidence") or {}
        if not isinstance(worklist_evidence, dict):
            continue
        worklist_metadata = approval_worklist_metadata(worklist_evidence)
        if set(worklist_metadata) != expected_worklist_keys:
            continue
        chunk_ids = {
            str(value)
            for value in (record.get("chunk_ids") or [])
            if str(value or "")
        }
        approved_hashes = (
            {
                str(chunk_id): str(value)
                for chunk_id, value in (
                    record.get("approved_content_hashes") or {}
                ).items()
                if str(chunk_id or "") and str(value or "")
            }
            if isinstance(record.get("approved_content_hashes"), dict)
            else {}
        )
        for item in record.get("approved_chunks") or []:
            if not isinstance(item, dict):
                continue
            chunk_id = str(item.get("chunk_id") or "")
            approved_hash = str(item.get("approved_content_hash") or "")
            if chunk_id and approved_hash and chunk_id not in approved_hashes:
                approved_hashes[chunk_id] = approved_hash
        for chunk_id in chunk_ids:
            approved_content_hash = approved_hashes.get(chunk_id, "")
            if approved_content_hash:
                index.add(
                    approval_journal_match_key(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        tenant_id=tenant_id,
                        approval_id=approval_id,
                        approved_content_hash=approved_content_hash,
                        expected_metadata=worklist_metadata,
                    )
                )
    return index


def build_approval_snapshot(
    repository: Any,
    document_ids: list[str],
    auth: AuthContext,
) -> dict[tuple[str, str], dict[str, Any]]:
    from app.core.tenant_access import resource_visible_to_tenant

    snapshot: dict[tuple[str, str], dict[str, Any]] = {}
    records_by_document = approval_journal_records_by_document(
        repository,
        document_ids,
    )
    approval_match_index = approval_journal_match_index(
        record
        for document_id in document_ids
        for record in records_by_document.get(document_id, ())
    )
    for document_id in document_ids:
        document = repository.get_document(document_id)
        if (
            document is None
            or not resource_visible_to_tenant(document, auth.tenant_id)
        ):
            continue
        for chunk in repository.get_chunks(document_id):
            if chunk.approval_status != "approved" or not chunk.approval_id:
                continue
            expected_record = expected_vector_record_for_chunk(
                chunk,
                document,
                auth,
            )
            if expected_record is None:
                continue
            expected_metadata = expected_record.get("metadata")
            if not isinstance(expected_metadata, dict):
                continue
            chunk_id = str(
                expected_record.get("chunk_id")
                or expected_metadata.get("chunk_id")
                or ""
            )
            security_level = str(
                expected_metadata.get("security_level") or ""
            ).strip().lower()
            if not chunk_id or security_level not in SECURITY_LEVEL_ORDER:
                continue
            match_key = approval_journal_match_key(
                chunk_id=chunk_id,
                document_id=document_id,
                tenant_id=auth.tenant_id,
                approval_id=str(chunk.approval_id or ""),
                approved_content_hash=str(chunk.approved_content_hash or ""),
                expected_metadata=expected_metadata,
            )
            if match_key not in approval_match_index:
                continue
            snapshot[(document_id, chunk_id)] = {
                "approval_id": expected_metadata.get("approval_id"),
                "approved_content_hash": expected_metadata.get(
                    "approved_content_hash"
                ),
                "security_level": security_level,
                "department_acl": department_acl_set(
                    expected_metadata.get("department_acl")
                ),
                "content_hash": str(
                    expected_record.get("content_hash") or ""
                ),
            }
    return snapshot


def load_cached_approval_snapshot(
    repository: Any,
    records: list[dict[str, Any]],
    auth: AuthContext,
    *,
    runtime_snapshot_loader: Callable[
        [Any, list[str], AuthContext],
        dict[tuple[str, str], dict[str, Any]] | None,
    ]
    | None = None,
    signature_loader: Callable[
        [Any, list[str]],
        tuple[Any, ...] | None,
    ]
    | None = None,
    snapshot_builder: Callable[
        [Any, list[str], AuthContext],
        dict[tuple[str, str], dict[str, Any]],
    ]
    | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    runtime_snapshot_loader = (
        runtime_snapshot_loader or load_cached_runtime_approval_snapshot
    )
    signature_loader = signature_loader or approval_snapshot_signature
    snapshot_builder = snapshot_builder or build_approval_snapshot
    document_ids = sorted(
        {
            str(
                record.get("document_id")
                or (record.get("metadata") or {}).get("document_id")
                or ""
            )
            for record in records
            if str(
                record.get("document_id")
                or (record.get("metadata") or {}).get("document_id")
                or ""
            ).strip()
        }
    )
    sidecar_snapshot = runtime_snapshot_loader(
        repository,
        document_ids,
        auth,
    )
    if sidecar_snapshot is not None:
        return sidecar_snapshot
    cache_key = (repository.root, auth.tenant_id, tuple(document_ids))
    signature = signature_loader(repository, document_ids)
    if signature is None:
        return {}
    with _RAG_VECTOR_CACHE_LOCK:
        cached = _RAG_APPROVAL_SNAPSHOT_CACHE.get(cache_key)
        if cached and cached[0] == signature:
            return cached[1]
    snapshot = snapshot_builder(repository, document_ids, auth)
    with _RAG_VECTOR_CACHE_LOCK:
        _RAG_APPROVAL_SNAPSHOT_CACHE[cache_key] = (signature, snapshot)
    return snapshot


def record_visible_to_request(
    record: dict[str, Any],
    *,
    request: Any,
    auth: AuthContext,
    repository: Any,
    repository_cache: RagRequestRepositoryCache | None = None,
    approval_snapshot: dict[tuple[str, str], dict[str, Any]] | None = None,
    requested_department_ids: frozenset[str],
) -> bool:
    from app.core.tenant_access import resource_visible_to_tenant
    from app.ingestion.vector_adapter import stable_content_hash
    from app.ingestion.vector_integrity import embedded_vector_integrity_reason

    metadata_value = record.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    if (
        metadata.get("approval_status") != "approved"
        or not metadata.get("approval_id")
    ):
        return False
    document_id = str(
        record.get("document_id") or metadata.get("document_id") or ""
    )
    if request.document_id and document_id != request.document_id:
        return False
    record_profile_id = str(
        metadata.get("profile_id") or record.get("profile_id") or ""
    ).strip()
    if request.profile_id:
        requested_profile_id = str(request.profile_id).strip().casefold()
        if record_profile_id:
            if record_profile_id.casefold() != requested_profile_id:
                return False
        else:
            document = (
                repository_cache.get_document(document_id)
                if repository_cache is not None
                else repository.get_document(document_id)
            )
            document_profile_id = str(
                getattr(document, "profile_id", "") or ""
            ).strip().casefold()
            if (
                not document_profile_id
                or document_profile_id != requested_profile_id
            ):
                return False
    security_level = str(
        metadata.get("security_level") or ""
    ).strip().lower()
    if security_level not in requested_security_levels(request, auth):
        return False
    department_acl = department_acl_set(metadata.get("department_acl"))
    if (
        department_acl
        and requested_department_ids
        and not requested_department_ids.intersection(department_acl)
    ):
        return False
    if department_acl and auth.role != API_ROLE_ADMIN:
        if not set(auth.department_ids).intersection(department_acl):
            return False
    chunk_id = str(
        record.get("chunk_id") or metadata.get("chunk_id") or ""
    )
    if approval_snapshot is not None:
        current = approval_snapshot.get((document_id, chunk_id))
        if current is None:
            return False
        if (
            current.get("approval_id") != metadata.get("approval_id")
            or current.get("approved_content_hash")
            != metadata.get("approved_content_hash")
            or current.get("content_hash")
            != str(record.get("content_hash") or "")
            or security_level != current.get("security_level")
            or department_acl != current.get("department_acl")
        ):
            return False
        return True
    if stable_content_hash(
        str(record.get("text") or ""),
        metadata,
    ) != str(record.get("content_hash") or ""):
        return False
    if embedded_vector_integrity_reason(record):
        return False
    document = (
        repository_cache.get_document(document_id)
        if repository_cache is not None
        else repository.get_document(document_id)
    )
    if (
        document is None
        or not resource_visible_to_tenant(document, auth.tenant_id)
    ):
        return False
    chunk = current_repository_chunk(
        repository,
        document_id,
        chunk_id,
        repository_cache=repository_cache,
    )
    if chunk is None:
        return False
    if (
        chunk.approval_status != "approved"
        or chunk.approval_id != metadata.get("approval_id")
        or chunk.approved_content_hash
        != metadata.get("approved_content_hash")
        or security_level
        != str(chunk.security_level or "").strip().lower()
        or department_acl != department_acl_set(chunk.department_acl)
    ):
        return False
    expected_record = expected_vector_record_for_chunk(chunk, document, auth)
    return bool(
        expected_record is not None
        and str(expected_record.get("content_hash") or "")
        == str(record.get("content_hash") or "")
    )


def load_visible_records(
    *,
    request: Any,
    auth: AuthContext,
    settings: Any,
    repository: Any,
    repository_cache: RagRequestRepositoryCache,
    records: list[dict[str, Any]],
    approval_snapshot: dict[tuple[str, str], dict[str, Any]] | None,
    requested_department_ids_value: frozenset[str] | None = None,
    requested_department_ids: frozenset[str] | None = None,
    latest_only: bool = True,
    visibility_checker: Callable[..., bool] | None = None,
    lifecycle_filter: Callable[..., list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    from app.services.regulation_catalog_service import (
        filter_to_latest_active_versions,
    )

    requested_departments = (
        requested_department_ids
        if requested_department_ids is not None
        else requested_department_ids_value or frozenset()
    )
    visibility_checker = visibility_checker or record_visible_to_request
    lifecycle_filter = lifecycle_filter or filter_to_latest_active_versions
    cache_key = (
        path_signature(local_vector_path(settings, auth)),
        id(approval_snapshot) if approval_snapshot is not None else None,
        auth.tenant_id,
        auth.role,
        tuple(
            sorted(
                str(item)
                for item in auth.department_ids
                if str(item).strip()
            )
        ),
        tuple(sorted(requested_security_levels(request, auth))),
        request.document_id or "",
        request.profile_id or "",
        request.as_of_date or "",
        tuple(sorted(requested_departments)),
        latest_only,
    )
    with _RAG_VISIBLE_RECORDS_CACHE_LOCK:
        cached = _RAG_VISIBLE_RECORDS_CACHE.get(cache_key)
        if cached is not None:
            _RAG_VISIBLE_RECORDS_CACHE.move_to_end(cache_key)
            return list(cached)
    visible_records = [
        record
        for record in records
        if visibility_checker(
            record,
            request=request,
            auth=auth,
            repository=repository,
            repository_cache=repository_cache,
            approval_snapshot=approval_snapshot,
            requested_department_ids=requested_departments,
        )
    ]
    if latest_only:
        visible_records = lifecycle_filter(
            visible_records,
            as_of=request.as_of_date,
            include_legacy=True,
        )
    with _RAG_VISIBLE_RECORDS_CACHE_LOCK:
        _RAG_VISIBLE_RECORDS_CACHE[cache_key] = list(visible_records)
        _RAG_VISIBLE_RECORDS_CACHE.move_to_end(cache_key)
        entry_limit = max(1, int(_RAG_VISIBLE_RECORDS_MAX_ENTRIES))
        while len(_RAG_VISIBLE_RECORDS_CACHE) > entry_limit:
            _RAG_VISIBLE_RECORDS_CACHE.popitem(last=False)
    return visible_records


def public_search_result(
    record: dict[str, Any],
    score: float,
    *,
    related_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata_value = record.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    governing_article = governing_article_for_reference_chunk(
        record,
        related_records or [],
    )
    return {
        "score": score,
        "document_id": record.get("document_id")
        or metadata.get("document_id")
        or "",
        "chunk_id": record.get("chunk_id") or metadata.get("chunk_id") or "",
        "text": str(record.get("text") or ""),
        "document_name": metadata.get("document_name") or "",
        "institution_name": metadata.get("institution_name") or "",
        "apba_id": metadata.get("apba_id") or "",
        "source_system": metadata.get("source_system") or "",
        "source_url": metadata.get("source_url") or "",
        "source_record_id": metadata.get("source_record_id") or "",
        "source_file_id": metadata.get("source_file_id") or "",
        "profile_id": metadata.get("profile_id")
        or record.get("profile_id")
        or "",
        "regulation_id": metadata.get("regulation_id")
        or record.get("regulation_id")
        or "",
        "regulation_version": metadata.get("regulation_version")
        or record.get("regulation_version")
        or "",
        "regulation_status": metadata.get("regulation_status")
        or record.get("regulation_status")
        or "",
        "chunk_type": metadata.get("chunk_type") or "",
        "hierarchy_path": metadata.get("hierarchy_path") or "",
        "part_title": metadata.get("part_title") or "",
        "chapter_title": metadata.get("chapter_title") or "",
        "regulation_title": metadata.get("regulation_title") or "",
        "article_no": metadata.get("article_no") or "",
        "article_title": metadata.get("article_title") or "",
        "article_refs": metadata.get("article_refs") or [],
        "appendix_refs": metadata.get("appendix_refs") or [],
        "form_refs": metadata.get("form_refs") or [],
        "reference_edges": metadata.get("reference_edges") or [],
        "governing_article_no": governing_article.get("article_no", ""),
        "governing_article_title": governing_article.get(
            "article_title",
            "",
        ),
        "governing_article_chunk_id": governing_article.get("chunk_id", ""),
        "governing_article_match_ref": governing_article.get("match_ref", ""),
        "source_page_start": metadata.get("source_page_start"),
        "source_page_end": metadata.get("source_page_end"),
        "effective_date": metadata.get("effective_date") or "",
        "revision_date": metadata.get("revision_date") or "",
        "effective_from": metadata.get("effective_from"),
        "effective_to": metadata.get("effective_to"),
        "repealed_at": metadata.get("repealed_at"),
        "supersedes_document_id": metadata.get("supersedes_document_id") or "",
        "valid_from": metadata.get("valid_from") or "",
        "valid_to": metadata.get("valid_to") or "",
        "revision_history": metadata.get("revision_history") or [],
        "revision_history_spans": metadata.get("revision_history_spans") or [],
        "article_effective_overrides": metadata.get(
            "article_effective_overrides"
        )
        or [],
        "article_validity_windows": metadata.get("article_validity_windows")
        or [],
        "supplementary_identifier_date": metadata.get(
            "supplementary_identifier_date"
        )
        or "",
        "temporal_metadata_inherited": bool(
            metadata.get("temporal_metadata_inherited")
        ),
        "temporal_metadata_scope": metadata.get("temporal_metadata_scope")
        or "",
        "temporal_metadata_inherited_fields": metadata.get(
            "temporal_metadata_inherited_fields"
        )
        or [],
        "temporal_metadata_normalized_fields": metadata.get(
            "temporal_metadata_normalized_fields"
        )
        or [],
        "temporal_metadata_conflict_fields": metadata.get(
            "temporal_metadata_conflict_fields"
        )
        or [],
        "security_level": metadata.get("security_level") or "",
        "approval_status": metadata.get("approval_status") or "",
        "approval_id": metadata.get("approval_id") or "",
        "approval_worklist_report_sha256": metadata.get(
            "approval_worklist_report_sha256"
        )
        or "",
        "approval_review_batch_manifest_path": metadata.get(
            "approval_review_batch_manifest_path"
        )
        or "",
        "approval_review_batch_manifest_sha256": metadata.get(
            "approval_review_batch_manifest_sha256"
        )
        or "",
        "approval_review_batch_id": metadata.get("approval_review_batch_id")
        or "",
        "approval_review_batch_chunk_fingerprint": metadata.get(
            "approval_review_batch_chunk_fingerprint"
        )
        or "",
        "approval_review_strategy": metadata.get("approval_review_strategy")
        or "",
        "content_hash": str(record.get("content_hash") or ""),
        "approved_content_hash": str(
            metadata.get("approved_content_hash") or ""
        ),
        "answer_profile_version": metadata.get("answer_profile_version") or "",
        "answer_intents": metadata.get("answer_intents") or [],
        "answer_keywords": metadata.get("answer_keywords") or [],
        "answer_facts": metadata.get("answer_facts") or [],
        "answer_outline": metadata.get("answer_outline") or [],
        "source_hwpx_block_types": metadata.get("source_hwpx_block_types")
        or [],
        "source_xml_files": metadata.get("source_xml_files") or [],
        "source_xml_roles": metadata.get("source_xml_roles") or [],
        "source_hwpx_parser_review_flags": metadata.get(
            "source_hwpx_parser_review_flags"
        )
        or [],
        "source_hwpx_xml_block_indices": metadata.get(
            "source_hwpx_xml_block_indices"
        )
        or [],
        "source_hwpx_table_direct_captions": metadata.get(
            "source_hwpx_table_direct_captions"
        )
        or [],
        "source_hwpx_table_image_captions": metadata.get(
            "source_hwpx_table_image_captions"
        )
        or [],
        "source_hwpx_table_note_snippets": metadata.get(
            "source_hwpx_table_note_snippets"
        )
        or [],
        "source_hwpx_nested_table_text_snippets": metadata.get(
            "source_hwpx_nested_table_text_snippets"
        )
        or [],
        "source_hwp_extraction_modes": metadata.get(
            "source_hwp_extraction_modes"
        )
        or [],
        "source_hwp_streams": metadata.get("source_hwp_streams") or [],
        "source_hwp_section_indices": metadata.get(
            "source_hwp_section_indices"
        )
        or [],
        "source_hwp_native_table_geometry": metadata.get(
            "source_hwp_native_table_geometry"
        ),
        "pdf_embedded_image_pages": metadata.get("pdf_embedded_image_pages")
        or [],
        "table_source": metadata.get("table_source") or "",
        "table_geometry_source": metadata.get("table_geometry_source") or "",
        "primary_parser_table_source": metadata.get(
            "primary_parser_table_source"
        )
        or "",
        "kordoc_table_parser_status": metadata.get(
            "kordoc_table_parser_status"
        )
        or "",
        "kordoc_table_count": metadata.get("kordoc_table_count"),
        "kordoc_table_promoted": bool(metadata.get("kordoc_table_promoted")),
        "kordoc_table_promotion_review_required": bool(
            metadata.get("kordoc_table_promotion_review_required")
        ),
        "kordoc_table_unmatched_source": bool(
            metadata.get("kordoc_table_unmatched_source")
        ),
        "kordoc_table_match": metadata.get("kordoc_table_match") or {},
        "kordoc_table_match_review_required": bool(
            metadata.get("kordoc_table_match_review_required")
        ),
        "kordoc_table_match_provisional": bool(
            metadata.get("kordoc_table_match_provisional")
        ),
        "parser_uncertainty_source": metadata.get(
            "parser_uncertainty_source"
        )
        or "",
        "parser_uncertainty_risk_level": metadata.get(
            "parser_uncertainty_risk_level"
        )
        or "",
        "parser_uncertainty_confidence": metadata.get(
            "parser_uncertainty_confidence"
        ),
        "parser_uncertainty_flags": metadata.get("parser_uncertainty_flags")
        or [],
        "parser_uncertainty_recommendation": metadata.get(
            "parser_uncertainty_recommendation"
        )
        or "",
        "parser_uncertainty_remediation_hint": metadata.get(
            "parser_uncertainty_remediation_hint"
        )
        or "",
    }


def governing_article_for_reference_chunk(
    record: dict[str, Any],
    related_records: list[dict[str, Any]],
) -> dict[str, str]:
    metadata_value = record.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    if metadata.get("article_no") and metadata.get("article_title"):
        return {}
    reference_labels = normalized_reference_labels(
        [
            *(metadata.get("form_refs") or []),
            *(metadata.get("appendix_refs") or []),
        ]
    )
    if not reference_labels:
        return {}
    document_id = str(
        record.get("document_id") or metadata.get("document_id") or ""
    )
    chunk_id = str(record.get("chunk_id") or metadata.get("chunk_id") or "")
    matches: dict[str, dict[str, str]] = {}
    for candidate in related_records:
        candidate_metadata_value = candidate.get("metadata")
        candidate_metadata = (
            candidate_metadata_value
            if isinstance(candidate_metadata_value, dict)
            else {}
        )
        candidate_document_id = str(
            candidate.get("document_id")
            or candidate_metadata.get("document_id")
            or ""
        )
        candidate_chunk_id = str(
            candidate.get("chunk_id")
            or candidate_metadata.get("chunk_id")
            or ""
        )
        if (
            candidate_document_id != document_id
            or candidate_chunk_id == chunk_id
        ):
            continue
        article_no = str(candidate_metadata.get("article_no") or "").strip()
        article_title = str(
            candidate_metadata.get("article_title") or ""
        ).strip()
        if (
            not article_no
            or not article_title
            or not same_reference_context(metadata, candidate_metadata)
        ):
            continue
        matched_ref = candidate_references_any_label(
            candidate,
            reference_labels,
        )
        if not matched_ref:
            continue
        key = f"{article_no}\n{article_title}\n{candidate_chunk_id}"
        matches[key] = {
            "article_no": article_no,
            "article_title": article_title,
            "chunk_id": candidate_chunk_id,
            "match_ref": matched_ref,
        }
    if len(matches) != 1:
        return {}
    return next(iter(matches.values()))


def same_reference_context(
    source_metadata: dict[str, Any],
    candidate_metadata: dict[str, Any],
) -> bool:
    source_context = reference_context_values(source_metadata)
    candidate_context = reference_context_values(candidate_metadata)
    if not source_context and not candidate_context:
        return True
    return bool(source_context & candidate_context)


def reference_context_values(metadata: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in (
        "regulation_no",
        "regulation_title",
        "chapter_title",
        "section_title",
    ):
        normalized = normalize_reference_context(metadata.get(key))
        if normalized:
            values.add(normalized)
    return values


def normalize_reference_context(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def candidate_references_any_label(
    record: dict[str, Any],
    labels: set[str],
) -> str:
    metadata_value = record.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    candidate_refs = normalized_reference_labels(
        [
            *(metadata.get("form_refs") or []),
            *(metadata.get("appendix_refs") or []),
        ]
    )
    for label in sorted(labels):
        if label in candidate_refs:
            return label
    compact_text = normalize_reference_label(
        " ".join(
            str(value or "")
            for value in (record.get("text"), metadata.get("retrieval_text"))
        )
    )
    for label in sorted(labels):
        if label and re.search(re.escape(label) + r"(?!\d)", compact_text):
            return label
    return ""


def normalized_reference_labels(values: list[Any]) -> set[str]:
    return {
        normalized
        for value in values
        if (normalized := normalize_reference_label(str(value or "")))
    }


def normalize_reference_label(value: str) -> str:
    return re.sub(
        r"[^0-9A-Za-z\uac00-\ud7a3]",
        "",
        str(value or ""),
    ).lower()
