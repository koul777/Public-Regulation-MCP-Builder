from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.processors.quality_gate import QualityGateProfile, load_quality_gate_profile_config


def validate_quality_profiles(path: Path) -> dict[str, Any]:
    config = load_quality_gate_profile_config(path)
    profiles = config.profiles or {}
    return {
        "path": str(path),
        "valid": True,
        "sha256": config.sha256,
        "default": profile_summary(config.default_profile),
        "profile_count": len(profiles),
        "profiles": {profile_id: profile_summary(profile) for profile_id, profile in sorted(profiles.items())},
    }


def profile_summary(profile: QualityGateProfile) -> dict[str, int | float | str]:
    return {
        "coverage_ratio_min": profile.coverage_ratio_min,
        "coverage_ratio_max": profile.coverage_ratio_max,
        "coverage_threshold_label": profile.coverage_threshold_label,
        "table_false_positive_attention_max_count": profile.table_false_positive_attention_max_count,
        "table_false_positive_attention_max_ratio": profile.table_false_positive_attention_max_ratio,
        "table_false_positive_threshold_label": profile.table_false_positive_threshold_label,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a quality profile JSON file.")
    parser.add_argument("path", help="Path to quality_profiles.json.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = validate_quality_profiles(Path(args.path))
    except Exception as exc:
        print(json.dumps({"path": args.path, "valid": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
