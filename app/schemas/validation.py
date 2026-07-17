from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ValidationIssue(BaseModel):
    issue_id: str
    document_id: str
    target_id: str | None = None
    severity: Literal["info", "warning", "error"]
    issue_type: str
    message: str
    suggested_action: str | None = None
    auto_fixable: bool = False

