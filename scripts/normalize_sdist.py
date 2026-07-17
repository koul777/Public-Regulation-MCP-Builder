from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile
from typing import Sequence, TextIO


MAX_GZIP_MTIME = (1 << 32) - 1
_ALLOWED_MEMBER_TYPES = {
    tarfile.REGTYPE,
    tarfile.AREGTYPE,
    tarfile.DIRTYPE,
    tarfile.SYMTYPE,
    tarfile.LNKTYPE,
}


def build_sdist_normalization_report(
    *,
    source_date_epoch: int,
    sdist_path: str | Path | None = None,
    sdist_dir: str | Path | None = None,
    output_path: str | Path | None = None,
    project_root: str | Path = ".",
) -> dict[str, object]:
    """Normalize one sdist archive and return machine-readable evidence.

    The default operation is an atomic in-place rewrite. The original archive is
    left untouched if validation or rewriting fails.
    """

    source: Path | None = None
    destination: Path | None = None
    try:
        epoch = validate_source_date_epoch(source_date_epoch)
        root = Path(project_root).resolve()
        source = _resolve_sdist(root, sdist_path=sdist_path, sdist_dir=sdist_dir)
        destination = _resolve_output(root, source, output_path)
        details = _normalize_archive(source, destination, epoch)
        return {
            "report_type": "deterministic_sdist_normalization",
            "passed": True,
            "source_date_epoch": epoch,
            "sdist_path": str(source),
            "output_path": str(destination),
            "in_place": source == destination,
            **details,
            "issues": [],
        }
    except Exception as exc:
        return {
            "report_type": "deterministic_sdist_normalization",
            "passed": False,
            "source_date_epoch": source_date_epoch,
            "sdist_path": str(source) if source is not None else None,
            "output_path": str(destination) if destination is not None else None,
            "in_place": source == destination if source is not None and destination is not None else None,
            "issues": [
                {
                    "code": "sdist-normalization-error",
                    "severity": "high",
                    "detail": str(exc),
                }
            ],
        }


def validate_source_date_epoch(value: int) -> int:
    try:
        epoch = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer.") from exc
    if epoch < 0 or epoch > MAX_GZIP_MTIME:
        raise ValueError(f"SOURCE_DATE_EPOCH must be between 0 and {MAX_GZIP_MTIME}.")
    return epoch


def _resolve_sdist(
    root: Path,
    *,
    sdist_path: str | Path | None,
    sdist_dir: str | Path | None,
) -> Path:
    if sdist_path is not None and sdist_dir is not None:
        raise ValueError("Use either --sdist or --sdist-dir, not both.")
    if sdist_path is not None:
        candidate = Path(sdist_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.is_file():
            raise FileNotFoundError(f"sdist does not exist: {candidate}")
        return candidate.resolve()

    directory = Path(sdist_dir) if sdist_dir is not None else Path("dist")
    if not directory.is_absolute():
        directory = root / directory
    candidates = sorted(directory.glob("reg_rag_preprocessor-*.tar.gz"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one reg_rag_preprocessor-*.tar.gz in {directory}, "
            f"found {len(candidates)}."
        )
    return candidates[0].resolve()


def _resolve_output(root: Path, source: Path, output_path: str | Path | None) -> Path:
    if output_path is None:
        return source
    destination = Path(output_path)
    if not destination.is_absolute():
        destination = root / destination
    return destination.resolve()


def _normalize_archive(source: Path, destination: Path, epoch: int) -> dict[str, object]:
    before_sha256 = _sha256_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    member_names: list[str] = []
    try:
        with tarfile.open(source, mode="r:gz") as input_archive:
            members = input_archive.getmembers()
            member_names = _validate_members(members)
            content_manifest_sha256 = _content_manifest_sha256(input_archive, members)
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=temporary_file,
                    compresslevel=9,
                    mtime=epoch,
                ) as gzip_stream:
                    with tarfile.open(
                        fileobj=gzip_stream,
                        mode="w|",
                        format=tarfile.PAX_FORMAT,
                    ) as output_archive:
                        for member in sorted(members, key=lambda item: item.name):
                            normalized = _normalized_member(member, epoch)
                            file_object = None
                            if member.isreg():
                                file_object = input_archive.extractfile(member)
                                if file_object is None:
                                    raise RuntimeError(f"Could not read sdist member: {member.name}")
                            try:
                                output_archive.addfile(normalized, fileobj=file_object)
                            finally:
                                if file_object is not None:
                                    file_object.close()

        _verify_normalized_archive(
            temporary_path,
            expected_names=sorted(member_names),
            expected_content_manifest_sha256=content_manifest_sha256,
            epoch=epoch,
        )
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return {
        "sha256_before": before_sha256,
        "sha256_after": _sha256_file(destination),
        "member_count": len(member_names),
        "member_name_sha256": hashlib.sha256(
            ("\n".join(sorted(member_names)) + "\n").encode("utf-8")
        ).hexdigest(),
        "content_manifest_sha256": content_manifest_sha256,
        "normalized_metadata": {
            "mtime": epoch,
            "uid": 0,
            "gid": 0,
            "uname": "",
            "gname": "",
            "member_order": "lexicographic-path",
            "gzip_filename": "",
            "gzip_compresslevel": 9,
        },
    }


def _validate_members(members: Sequence[tarfile.TarInfo]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for member in members:
        name = _validate_archive_path(member.name, label="member")
        if name in seen:
            raise RuntimeError(f"Duplicate sdist member path: {name}")
        seen.add(name)
        names.append(name)
        if member.type not in _ALLOWED_MEMBER_TYPES:
            raise RuntimeError(f"Unsupported sdist member type for {name}: {member.type!r}")
        if getattr(member, "sparse", None):
            raise RuntimeError(f"Sparse sdist members are not supported: {name}")
        if member.issym() or member.islnk():
            _validate_archive_path(member.linkname, label=f"link target for {name}")
    return names


def _validate_archive_path(raw_name: str, *, label: str) -> str:
    if not raw_name or "\x00" in raw_name:
        raise RuntimeError(f"Unsafe {label} path: {raw_name!r}")
    if "\\" in raw_name or raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
        raise RuntimeError(f"Unsafe {label} path: {raw_name}")
    path = PurePosixPath(raw_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw_name.split("/")):
        raise RuntimeError(f"Unsafe {label} path: {raw_name}")
    return path.as_posix()


def _normalized_member(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    normalized = copy.copy(member)
    normalized.uid = 0
    normalized.gid = 0
    normalized.uname = ""
    normalized.gname = ""
    normalized.mtime = epoch
    normalized.pax_headers = {}
    return normalized


def _verify_normalized_archive(
    path: Path,
    *,
    expected_names: Sequence[str],
    expected_content_manifest_sha256: str,
    epoch: int,
) -> None:
    with path.open("rb") as handle:
        header = handle.read(10)
    if len(header) != 10 or header[:3] != b"\x1f\x8b\x08":
        raise RuntimeError("Normalized sdist does not have a valid gzip header.")
    if header[3] & 0x08 or int.from_bytes(header[4:8], byteorder="little") != epoch:
        raise RuntimeError("Normalized sdist gzip filename or mtime verification failed.")
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if names != list(expected_names):
            raise RuntimeError("Normalized sdist member ordering or membership changed.")
        _validate_members(members)
        for member in members:
            if (
                member.mtime != epoch
                or member.uid != 0
                or member.gid != 0
                or member.uname != ""
                or member.gname != ""
            ):
                raise RuntimeError(f"Normalized metadata verification failed: {member.name}")
        if _content_manifest_sha256(archive, members) != expected_content_manifest_sha256:
            raise RuntimeError("Normalized sdist member content or install metadata changed.")


def _content_manifest_sha256(
    archive: tarfile.TarFile,
    members: Sequence[tarfile.TarInfo],
) -> str:
    digest = hashlib.sha256()
    for member in sorted(members, key=lambda item: item.name):
        payload_sha256 = None
        if member.isreg():
            file_object = archive.extractfile(member)
            if file_object is None:
                raise RuntimeError(f"Could not read sdist member: {member.name}")
            try:
                payload_digest = hashlib.sha256()
                for chunk in iter(lambda: file_object.read(1024 * 1024), b""):
                    payload_digest.update(chunk)
                payload_sha256 = payload_digest.hexdigest()
            finally:
                file_object.close()
        record = {
            "name": member.name,
            "type": member.type.hex(),
            "mode": member.mode,
            "size": member.size,
            "linkname": member.linkname,
            "payload_sha256": payload_sha256,
        }
        digest.update(
            (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_date_epoch_arg(value: str) -> int:
    try:
        return validate_source_date_epoch(int(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomically normalize one built sdist for deterministic release evidence."
    )
    parser.add_argument("--project-root", default=".")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sdist", default=None)
    source.add_argument("--sdist-dir", default=None)
    parser.add_argument("--output", default=None, help="Output archive; defaults to an in-place rewrite.")
    parser.add_argument("--source-date-epoch", type=_source_date_epoch_arg, required=True)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(argv)
    report = build_sdist_normalization_report(
        project_root=args.project_root,
        sdist_path=args.sdist,
        sdist_dir=args.sdist_dir,
        output_path=args.output,
        source_date_epoch=args.source_date_epoch,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        stdout.write(payload + "\n")
    elif report["passed"]:
        stdout.write("sdist normalization passed\n")
    else:
        stdout.write("sdist normalization failed\n")
    return 1 if args.fail_on_issue and not report["passed"] else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
