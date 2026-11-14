"""Quality models for result processing."""

from __future__ import annotations

from pydantic import BaseModel


class QualityServiceIssue(BaseModel):
    """Model for individual quality issues found by tools."""

    tool: str
    severity: str
    message: str
    line: int | None = None
    column: int | None = None
    rule_id: str | None = None
    fixable: bool = False
