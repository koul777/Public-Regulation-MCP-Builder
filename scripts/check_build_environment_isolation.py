from __future__ import annotations

import argparse
import ast
import importlib.metadata
import importlib.util
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "build-environment-isolation-v1"
REQUIRED_MODULE_GROUPS = {
    "pymupdf_backend": ("pymupdf", "fitz"),
}


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _is_within(path: str | Path, root: str | Path) -> bool:
    normalized_path = _normalized_path(path)
    normalized_root = _normalized_path(root)
    try:
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:
        return False


def evaluate_build_environment_isolation(
    *,
    venv_root: str | Path,
    python_prefix: str | Path,
    distribution_roots: Iterable[str | Path],
    module_files: Mapping[str, str | Path | None],
) -> dict[str, Any]:
    """Evaluate build dependency provenance without returning filesystem paths."""

    roots = tuple(distribution_roots)
    prefix_matches = _normalized_path(python_prefix) == _normalized_path(venv_root)
    external_distribution_count = sum(
        not _is_within(root, venv_root) for root in roots
    )
    missing_modules = tuple(
        group_name
        for group_name, candidates in REQUIRED_MODULE_GROUPS.items()
        if all(module_files.get(candidate) is None for candidate in candidates)
    )
    external_module_count = 0
    for candidates in REQUIRED_MODULE_GROUPS.values():
        candidate_files = tuple(
            module_files.get(candidate)
            for candidate in candidates
            if module_files.get(candidate) is not None
        )
        if candidate_files and any(
            not _is_within(module_file, venv_root)
            for module_file in candidate_files
        ):
            external_module_count += 1

    if external_distribution_count or external_module_count:
        reason_code = "build_dependency_outside_venv"
    elif not prefix_matches:
        reason_code = "build_python_prefix_mismatch"
    elif missing_modules:
        reason_code = "build_dependency_missing"
    else:
        reason_code = "ok"

    return {
        "schema_version": SCHEMA_VERSION,
        "passed": reason_code == "ok",
        "reason_code": reason_code,
        "python_prefix_matches_venv": prefix_matches,
        "checked_distribution_count": len(roots),
        "external_distribution_count": external_distribution_count,
        "required_modules": list(REQUIRED_MODULE_GROUPS),
        "missing_module_count": len(missing_modules),
        "external_module_count": external_module_count,
    }


def inspect_current_environment(venv_root: str | Path) -> dict[str, Any]:
    distribution_roots = tuple(
        Path(distribution.locate_file(""))
        for distribution in importlib.metadata.distributions()
    )
    module_files: dict[str, str | Path | None] = {}
    module_names = tuple(
        dict.fromkeys(
            candidate
            for candidates in REQUIRED_MODULE_GROUPS.values()
            for candidate in candidates
        )
    )
    for name in module_names:
        try:
            spec = importlib.util.find_spec(name)
        except Exception:
            module_files[name] = None
            continue
        module_files[name] = Path(spec.origin) if spec and spec.origin else None
    return evaluate_build_environment_isolation(
        venv_root=venv_root,
        python_prefix=sys.prefix,
        distribution_roots=distribution_roots,
        module_files=module_files,
    )


def _iter_nested_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_nested_strings(item)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _iter_nested_strings(item)


def _iter_binary_sources(value: object) -> Iterable[str]:
    if (
        isinstance(value, (list, tuple))
        and len(value) >= 3
        and isinstance(value[1], str)
        and value[2] in {"BINARY", "EXTENSION"}
    ):
        yield value[1]
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_binary_sources(item)
    elif isinstance(value, Sequence) and not isinstance(value, str):
        for item in value:
            yield from _iter_binary_sources(item)


def evaluate_artifact_source_provenance(
    *,
    source_paths: Iterable[str | Path],
    allowed_roots: Iterable[str | Path],
    allowed_paths: Iterable[str | Path] = (),
) -> dict[str, int | bool]:
    """Count absolute PyInstaller inputs without returning private paths."""

    normalized_allowed_roots = tuple(allowed_roots)
    normalized_allowed_paths = {
        _normalized_path(path) for path in allowed_paths
    }
    absolute_sources = {
        _normalized_path(path)
        for path in source_paths
        if os.path.isabs(os.fspath(path))
    }
    external_source_count = sum(
        path not in normalized_allowed_paths
        and not any(_is_within(path, root) for root in normalized_allowed_roots)
        for path in absolute_sources
    )
    return {
        "artifact_source_check_performed": True,
        "checked_artifact_source_count": len(absolute_sources),
        "external_artifact_source_count": external_source_count,
        "artifact_sources_within_allowed_roots": external_source_count == 0,
    }


def inspect_pyinstaller_analysis_toc(
    analysis_toc: str | Path,
    *,
    allowed_roots: Iterable[str | Path],
    allowed_paths: Iterable[str | Path] = (),
) -> dict[str, int | bool]:
    """Inspect PyInstaller's literal analysis table without executing it."""

    payload = ast.literal_eval(Path(analysis_toc).read_text(encoding="utf-8"))
    return evaluate_artifact_source_provenance(
        source_paths=_iter_nested_strings(payload),
        allowed_roots=allowed_roots,
        allowed_paths=allowed_paths,
    )


def evaluate_binary_source_provenance(
    *, source_paths: Iterable[str | Path], allowed_roots: Iterable[str | Path]
) -> dict[str, int | bool]:
    """Require every collected binary source to be absolute and explicitly rooted."""

    roots = tuple(allowed_roots)
    raw_sources = {os.fspath(path) for path in source_paths}
    relative_source_count = sum(not os.path.isabs(path) for path in raw_sources)
    absolute_sources = {
        _normalized_path(path) for path in raw_sources if os.path.isabs(path)
    }
    external_source_count = sum(
        not any(_is_within(path, root) for root in roots)
        for path in absolute_sources
    )
    return {
        "binary_source_check_performed": True,
        "checked_binary_source_count": len(raw_sources),
        "external_binary_source_count": external_source_count,
        "relative_binary_source_count": relative_source_count,
        "binary_sources_within_allowed_roots": (
            external_source_count == 0 and relative_source_count == 0
        ),
    }


def inspect_pyinstaller_binary_tocs(
    toc_paths: Iterable[str | Path], *, allowed_roots: Iterable[str | Path]
) -> dict[str, int | bool]:
    sources: list[str] = []
    for toc_path in toc_paths:
        payload = ast.literal_eval(Path(toc_path).read_text(encoding="utf-8"))
        sources.extend(_iter_binary_sources(payload))
    return evaluate_binary_source_provenance(
        source_paths=sources,
        allowed_roots=allowed_roots,
    )


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ValueError("invalid_arguments")


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Verify that Windows portable build dependencies are venv-local."
    )
    parser.add_argument("--venv-root", required=True)
    parser.add_argument("--analysis-toc")
    parser.add_argument("--allowed-root", action="append", default=[])
    parser.add_argument("--allowed-path", action="append", default=[])
    parser.add_argument("--binary-toc", action="append", default=[])
    parser.add_argument("--binary-allowed-root", action="append", default=[])
    parser.add_argument("--fail-on-issue", action="store_true")
    return parser


def _failure_report(reason_code: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "passed": False,
        "reason_code": reason_code,
        "python_prefix_matches_venv": False,
        "checked_distribution_count": 0,
        "external_distribution_count": 0,
        "required_modules": list(REQUIRED_MODULE_GROUPS),
        "missing_module_count": len(REQUIRED_MODULE_GROUPS),
        "external_module_count": 0,
        "artifact_source_check_performed": False,
        "checked_artifact_source_count": 0,
        "external_artifact_source_count": 0,
        "artifact_sources_within_allowed_roots": False,
        "binary_source_check_performed": False,
        "checked_binary_source_count": 0,
        "external_binary_source_count": 0,
        "relative_binary_source_count": 0,
        "binary_sources_within_allowed_roots": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    fail_on_issue = "--fail-on-issue" in raw_argv
    try:
        args = _build_parser().parse_args(raw_argv)
        fail_on_issue = bool(args.fail_on_issue)
        report = inspect_current_environment(args.venv_root)
        report.update(
            {
                "artifact_source_check_performed": False,
                "checked_artifact_source_count": 0,
                "external_artifact_source_count": 0,
                "artifact_sources_within_allowed_roots": False,
                "binary_source_check_performed": False,
                "checked_binary_source_count": 0,
                "external_binary_source_count": 0,
                "relative_binary_source_count": 0,
                "binary_sources_within_allowed_roots": False,
            }
        )
        if args.analysis_toc:
            if not args.allowed_root:
                raise ValueError("allowed_roots_required")
            artifact_report = inspect_pyinstaller_analysis_toc(
                args.analysis_toc,
                allowed_roots=args.allowed_root,
                allowed_paths=args.allowed_path,
            )
            report.update(artifact_report)
            if report["external_artifact_source_count"]:
                report["passed"] = False
                report["reason_code"] = (
                    "build_artifact_source_outside_allowed_roots"
                )
        if args.binary_toc:
            if not args.binary_allowed_root:
                raise ValueError("binary_allowed_roots_required")
            binary_report = inspect_pyinstaller_binary_tocs(
                args.binary_toc,
                allowed_roots=args.binary_allowed_root,
            )
            report.update(binary_report)
            if report["external_binary_source_count"]:
                report["passed"] = False
                report["reason_code"] = (
                    "build_binary_source_outside_allowed_roots"
                )
            elif report["relative_binary_source_count"]:
                report["passed"] = False
                report["reason_code"] = "build_binary_source_not_absolute"
    except ValueError:
        report = _failure_report("invalid_arguments")
    except Exception:
        report = _failure_report("build_environment_isolation_check_failed")
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 1 if fail_on_issue and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "REQUIRED_MODULE_GROUPS",
    "SCHEMA_VERSION",
    "evaluate_build_environment_isolation",
    "evaluate_artifact_source_provenance",
    "evaluate_binary_source_provenance",
    "inspect_current_environment",
    "inspect_pyinstaller_analysis_toc",
    "inspect_pyinstaller_binary_tocs",
    "main",
]
