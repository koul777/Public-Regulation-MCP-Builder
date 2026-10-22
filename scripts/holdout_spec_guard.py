from __future__ import annotations

"""Derive a query spec's split and keep holdout specs out of the stage CLIs.

Only the integrated runner (scripts/run_real_regulation_e2e.py) checks the holdout
seal and writes the once-only opened record. A stage CLI that loads a spec file
would run the holdout without either, so each of them calls refuse_holdout_spec()
on the parsed file before using any query. The integrated runner passes already
parsed queries in-process and never goes through these file loaders.

The split of a spec is one function, effective_query_split(), shared by the
runner, the seal command, the stage CLIs and the KPI checker. A label may sit at
the top level, on every query, or both. This module deliberately imports nothing
from the project, so every script can use it without import cycles.
"""

import unicodedata
from typing import Any


HOLDOUT_REFUSAL_CODE = "holdout_spec_requires_integrated_runner"
SPLIT_INVALID, SPLIT_MIXED, SPLIT_CONFLICT = "query_split_invalid", "query_split_mixed", "query_split_conflict"
_WRAPPER_KEYS = ("queries", "items", "query_specs")


class HoldoutSpecRefused(ValueError):
    """A fixed, path-free reason code."""


def normalize_split_label(value: Any) -> str:
    """Return ``development`` or ``holdout`` for a label, or refuse it.

    The label is compared after NFKC, whitespace stripping and case folding, so
    every spelling of holdout (full-width letters, capitals, padding) is holdout.
    Anything else, including ``unspecified`` written out, is invalid.
    """
    if isinstance(value, str):
        folded = unicodedata.normalize("NFKC", value).strip().casefold()
        if folded in ("development", "holdout"):
            return folded
    raise HoldoutSpecRefused(SPLIT_INVALID)


def _entries(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        return [entry for key in _WRAPPER_KEYS if isinstance(payload.get(key), list) for entry in payload[key]]
    return list(payload) if isinstance(payload, list) else []


def effective_query_split(payload: Any) -> str:
    """The split of a parsed spec: ``development``, ``holdout`` or ``unspecified``.

    Accepted forms: a top-level ``split``, a ``split`` on every query, or both with
    the same value. If any query has a label, every query needs one and they must
    all agree (``query_split_mixed``), and a top-level label must equal them
    (``query_split_conflict``). Unknown values give ``query_split_invalid``.
    """
    top = normalize_split_label(payload["split"]) if isinstance(payload, dict) and "split" in payload else None
    entries = _entries(payload)
    labeled = [entry["split"] for entry in entries if isinstance(entry, dict) and "split" in entry]
    if labeled:
        if len(labeled) != len(entries):
            raise HoldoutSpecRefused(SPLIT_MIXED)
        values = {normalize_split_label(label) for label in labeled}
        if len(values) != 1:
            raise HoldoutSpecRefused(SPLIT_MIXED)
        (case_split,) = values
        if top is not None and top != case_split:
            raise HoldoutSpecRefused(SPLIT_CONFLICT)
        return case_split
    return top if top is not None else "unspecified"


def refuse_holdout_spec(payload: Any) -> None:
    """Raise unless the parsed spec is unlabeled or labeled development.

    Holdout in either form gets the integrated-runner code. Invalid, mixed and
    conflicting labels are refused with their own codes.
    """
    if effective_query_split(payload) == "holdout":
        raise HoldoutSpecRefused(HOLDOUT_REFUSAL_CODE)
