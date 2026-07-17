from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.compare_batch_quality_reports import row_identity


DEFAULT_TOLERANCES = {
    "chunk_to_source_char_ratio": 0.005,
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def check_expectations(batch_report: dict[str, Any], expectations: dict[str, Any]) -> dict[str, Any]:
    rows = {row_identity(row): row for row in batch_report.get("rows", []) or []}
    failures: list[dict[str, Any]] = []
    checked: list[str] = []
    for fixture in expectations.get("fixtures", []) or []:
        identity = fixture["identity"]
        expected_metrics = fixture.get("metrics", {}) or {}
        tolerances = {**DEFAULT_TOLERANCES, **(fixture.get("tolerances", {}) or {})}
        row = rows.get(identity)
        if row is None:
            failures.append({"identity": identity, "reason": "missing_fixture_row"})
            continue
        checked.append(identity)
        for metric, expected_value in expected_metrics.items():
            actual_value = metric_value(row.get(metric))
            tolerance = float(tolerances.get(metric, 0))
            if abs(actual_value - metric_value(expected_value)) > tolerance:
                failures.append(
                    {
                        "identity": identity,
                        "reason": "metric_mismatch",
                        "metric": metric,
                        "expected": expected_value,
                        "actual": actual_value,
                        "tolerance": tolerance,
                    }
                )
    return {
        "passed": not failures,
        "checked_count": len(checked),
        "failure_count": len(failures),
        "failures": failures,
    }


def metric_value(value: Any) -> float:
    if isinstance(value, bool) or value in ("", None):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check batch report metrics against regression fixture expectations.")
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--expectations", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = check_expectations(load_json(Path(args.batch_report)), load_json(Path(args.expectations)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
