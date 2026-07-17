from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.batch_failure_alerting import build_failure_alert


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def emit_batch_failure_alert(
    batch_report_path: Path,
    *,
    readiness_report_path: Path | None = None,
    out_json: Path | None = None,
    alert_log: Path | None = None,
    webhook_url: str | None = None,
    include_local_paths: bool = False,
    max_items: int = 50,
    webhook_timeout_seconds: int = 30,
) -> dict[str, Any]:
    batch_report = load_json(batch_report_path)
    readiness_report = load_json(readiness_report_path) if readiness_report_path else None
    alert = build_failure_alert(
        batch_report,
        batch_report_file=batch_report_path.name,
        readiness_report=readiness_report,
        include_local_paths=include_local_paths,
        max_items=max_items,
    )
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(alert, ensure_ascii=False, indent=2), encoding="utf-8")
        alert["out_json"] = str(out_json)
    if alert_log:
        alert_log.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "alert": alert,
        }
        with alert_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        alert["alert_log"] = str(alert_log)
    if webhook_url:
        delivery = _post_webhook(webhook_url, alert, timeout_seconds=webhook_timeout_seconds)
        alert["webhook_delivery"] = delivery
    return alert


def _post_webhook(url: str, payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return {
                "delivered": True,
                "status_code": response.status,
            }
    except urllib.error.HTTPError as exc:
        return {
            "delivered": False,
            "status_code": exc.code,
            "error": str(exc),
        }
    except urllib.error.URLError as exc:
        return {
            "delivered": False,
            "error": str(exc.reason),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit a structured batch failure alert from a batch quality report."
    )
    parser.add_argument("--batch-report", required=True)
    parser.add_argument("--readiness-report", default=None)
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--alert-log", default=None, help="Append-only JSONL alert log path.")
    parser.add_argument("--webhook-url", default=None, help="Optional webhook URL for alert delivery.")
    parser.add_argument("--include-local-paths", action="store_true")
    parser.add_argument("--max-items", type=int, default=50)
    parser.add_argument("--webhook-timeout-seconds", type=int, default=30)
    parser.add_argument(
        "--fail-on-alert",
        action="store_true",
        help="Exit with code 1 when status is needs_attention.",
    )
    parser.add_argument(
        "--fail-on-webhook-error",
        action="store_true",
        help="Exit with code 2 when webhook delivery fails.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_json = Path(args.out_json) if args.out_json else Path("reports") / f"batch_failure_alert_{timestamp}.json"
    alert = emit_batch_failure_alert(
        Path(args.batch_report),
        readiness_report_path=Path(args.readiness_report) if args.readiness_report else None,
        out_json=out_json,
        alert_log=Path(args.alert_log) if args.alert_log else None,
        webhook_url=args.webhook_url,
        include_local_paths=args.include_local_paths,
        max_items=args.max_items,
        webhook_timeout_seconds=args.webhook_timeout_seconds,
    )
    print(json.dumps(alert, ensure_ascii=False, indent=2))
    if args.fail_on_webhook_error and alert.get("webhook_delivery", {}).get("delivered") is False:
        return 2
    if args.fail_on_alert and alert.get("status") == "needs_attention":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
