from __future__ import annotations

"""Seal a private development/holdout query-spec pair before any holdout run.

The command checks that the two specs are labeled for their split and share no
query id, query text or target chunk, then appends one ``sealed`` record to a
private append-only ledger. The ledger holds SHA-256 values, query counts and a
timestamp. It never stores query text, answers, identifiers or paths.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import unicodedata
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_real_regulation_e2e import (  # noqa: E402
    EvidenceError,
    query_split,
    require_private_path,
    seal_holdout,
)


def _read_spec(path: Path, expected_split: str, label: str) -> tuple[str, dict[str, Any]]:
    """Hash and parse the same bytes so the sealed hash is the parsed content."""
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise EvidenceError("expected_json_object")
    if query_split(payload) != expected_split:
        raise EvidenceError(f"{label}_spec_split_mismatch")
    return hashlib.sha256(raw).hexdigest(), payload


def _strings(value: Any) -> set[str]:
    found: set[str] = set()
    for entry in value if isinstance(value, (list, tuple)) else [value]:
        if entry not in (None, ""):
            found.add(str(entry).strip())
    return found - {""}


def _chunk_ids(case: dict[str, Any]) -> set[str]:
    # expected_* are the aliases the MCP probe accepts for the same target identifiers.
    found = set()
    for key in ("target_chunk_id", "target_chunk_ids", "expected_chunk_id", "expected_chunk_ids"):
        found |= _strings(case.get(key))
    citations = case.get("allowed_citations")
    for source in citations if isinstance(citations, list) else []:
        if isinstance(source, dict):
            for key in ("chunk_id", "evidence_id", "evidence_ids"):
                found |= _strings(source.get(key))
    return found


def _texts(case: dict[str, Any]) -> set[str]:
    """Question text under both accepted keys, compared after NFKC and whitespace/case folding."""
    found = set()
    for key in ("query", "question"):
        value = case.get(key)
        if isinstance(value, str):
            found.add(" ".join(unicodedata.normalize("NFKC", value).split()).casefold())
    return found - {""}


def _identity(payload: dict[str, Any]) -> dict[str, set[str]]:
    cases = payload.get("queries")
    if not isinstance(cases, list) or not cases or any(not isinstance(case, dict) for case in cases):
        raise EvidenceError("seal_spec_has_no_cases")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise EvidenceError("seal_spec_query_ids_invalid")
    if any("split" in case for case in cases):
        raise EvidenceError("case_level_split_not_supported")
    return {"ids": set(ids), "texts": {text for case in cases for text in _texts(case)},
            "chunks": set().union(*(_chunk_ids(case) for case in cases))}


def seal_pair(development: Path, holdout: Path, ledger: Path) -> dict[str, Any]:
    development, holdout, ledger = (require_private_path(path) for path in (development, holdout, ledger))
    if development == holdout:
        raise EvidenceError("development_and_holdout_must_be_different_files")
    if ledger in {development, holdout}:
        raise EvidenceError("holdout_ledger_overlaps_spec")
    development_sha, development_payload = _read_spec(development, "development", "development")
    holdout_sha, holdout_payload = _read_spec(holdout, "holdout", "holdout")
    left, right = _identity(development_payload), _identity(holdout_payload)
    for key, reason in (("ids", "development_and_holdout_share_query_ids"),
                        ("texts", "development_and_holdout_share_query_text"),
                        ("chunks", "development_and_holdout_share_target_chunk_ids")):
        if left[key] & right[key]:
            raise EvidenceError(reason)
    return seal_holdout(ledger, development_sha256=development_sha, holdout_sha256=holdout_sha,
                        development_query_count=len(development_payload["queries"]),
                        holdout_query_count=len(holdout_payload["queries"]))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seal a private development/holdout query-spec pair.")
    parser.add_argument("--development", type=Path, required=True, help="Private query spec with split=development.")
    parser.add_argument("--holdout", type=Path, required=True, help="Private query spec with split=holdout.")
    parser.add_argument("--ledger", type=Path, required=True, help="Private append-only holdout ledger (JSONL).")
    args = parser.parse_args(argv)
    try:
        record = seal_pair(args.development, args.holdout, args.ledger)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        print(json.dumps({"sealed": False, "reason_code": reason}))
        return 2
    print(json.dumps({"sealed": True, "ledger_id": record["ledger_id"],
                      "holdout_spec_sha256": record["query_spec_sha256"],
                      "development_spec_sha256": record["development_spec_sha256"],
                      "holdout_query_count": record["query_count"],
                      "development_query_count": record["development_query_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
