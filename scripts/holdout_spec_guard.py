from __future__ import annotations

"""Keep holdout query specs out of the stage CLIs.

Only the integrated runner (scripts/run_real_regulation_e2e.py) checks the holdout
seal and writes the once-only opened record. A stage CLI that loads a spec file
would run the holdout without either, so each of them calls refuse_holdout_spec()
on the parsed file before using any query. The integrated runner passes already
parsed queries in-process and never goes through these file loaders.

This module deliberately imports nothing from the project, so every script can use
it without import cycles.
"""

import unicodedata
from typing import Any


HOLDOUT_REFUSAL_CODE = "holdout_spec_requires_integrated_runner"
_WRAPPER_KEYS = ("queries", "items", "query_specs")


class HoldoutSpecRefused(ValueError):
    """A fixed, path-free reason code."""


def refuse_holdout_spec(payload: Any) -> None:
    """Raise unless the parsed spec is unlabeled or labeled development.

    The top-level split must be absent or exactly ``development``. Any spelling of
    ``holdout`` gets the integrated-runner code. Other values are invalid, and a
    per-query ``split`` key is refused because it could hide a holdout query inside
    a development file.
    """
    if isinstance(payload, dict):
        if "split" in payload:
            value = payload["split"]
            if isinstance(value, str) and unicodedata.normalize("NFKC", value).strip().casefold() == "holdout":
                raise HoldoutSpecRefused(HOLDOUT_REFUSAL_CODE)
            if value != "development":
                raise HoldoutSpecRefused("query_split_invalid")
        entries = [entry for key in _WRAPPER_KEYS if isinstance(payload.get(key), list) for entry in payload[key]]
    elif isinstance(payload, list):
        entries = payload
    else:
        return
    if any(isinstance(entry, dict) and "split" in entry for entry in entries):
        raise HoldoutSpecRefused("case_level_split_not_supported")
