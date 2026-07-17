from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.institution_profiles import load_institution_profile_registry


def validate_institution_profiles(path: Path) -> dict[str, Any]:
    registry = load_institution_profile_registry(path)
    return {
        "valid": True,
        **registry.summary(),
        "profiles": {
            profile.profile_id: profile.summary()
            for profile in sorted(registry.profiles.values(), key=lambda item: item.profile_id)
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate an institution profile registry JSON file.")
    parser.add_argument("path", help="Path to institution_profiles.json.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = validate_institution_profiles(Path(args.path))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
