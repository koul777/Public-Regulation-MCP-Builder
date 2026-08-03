from __future__ import annotations

import argparse
import os
import stat
from collections.abc import Sequence
from pathlib import Path


def is_safe_recreate_target(path: Path) -> bool:
    try:
        if not path.exists():
            return True
        stat_result = os.lstat(path)
    except OSError:
        return False

    if not stat.S_ISDIR(stat_result.st_mode):
        return False

    reparse_point_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    file_attributes = getattr(stat_result, "st_file_attributes", 0)
    return (file_attributes & reparse_point_flag) == 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail closed unless the requested Windows venv target is a normal directory."
    )
    parser.add_argument("--path", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return 0 if is_safe_recreate_target(Path(args.path)) else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["is_safe_recreate_target", "main"]
