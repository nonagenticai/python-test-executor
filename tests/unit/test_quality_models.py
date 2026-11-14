"""Tests for quality_models.py — Pydantic model validation, defaults, edge cases."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.quality_models import QualityServiceIssue


class TestQualityServiceIssue:
    """Tests for QualityServiceIssue model."""

    def test_minimal_required_fields(self):
        issue = QualityServiceIssue(tool="ruff", severity="error", message="bad code")
        assert issue.tool == "ruff"
        assert issue.severity == "error"
        assert issue.message == "bad code"
        assert issue.line is None
        assert issue.column is None
        assert issue.rule_id is None
        assert issue.fixable is False

    def test_all_fields_provided(self):
        issue = QualityServiceIssue(
            tool="eslint",
            severity="warning",
            message="unused variable",
            line=42,
            column=10,
            rule_id="no-unused-vars",
            fixable=True,
        )
        assert issue.tool == "eslint"
        assert issue.severity == "warning"
        assert issue.message == "unused variable"
        assert issue.line == 42
        assert issue.column == 10
        assert issue.rule_id == "no-unused-vars"
        assert issue.fixable is True

    def test_defaults_line_none(self):
        issue = QualityServiceIssue(tool="x", severity="info", message="m")
        assert issue.line is None

    def test_defaults_column_none(self):
        issue = QualityServiceIssue(tool="x", severity="info", message="m")
        assert issue.column is None

    def test_defaults_rule_id_none(self):
        issue = QualityServiceIssue(tool="x", severity="info", message="m")
        assert issue.rule_id is None

    def test_defaults_fixable_false(self):
        issue = QualityServiceIssue(tool="x", severity="info", message="m")
        assert issue.fixable is False

    def test_missing_tool_raises(self):
        with pytest.raises(ValidationError):
            QualityServiceIssue.model_validate({"severity": "error", "message": "bad"})

    def test_missing_severity_raises(self):
        with pytest.raises(ValidationError):
            QualityServiceIssue.model_validate({"tool": "ruff", "message": "bad"})

    def test_missing_message_raises(self):
        with pytest.raises(ValidationError):
            QualityServiceIssue.model_validate({"tool": "ruff", "severity": "error"})

    def test_empty_string_fields(self):
        issue = QualityServiceIssue(tool="", severity="", message="")
        assert issue.tool == ""
        assert issue.severity == ""
        assert issue.message == ""

    def test_serialization_roundtrip(self):
        original = QualityServiceIssue(
            tool="bandit",
            severity="high",
            message="SQL injection",
            line=10,
            column=5,
            rule_id="B608",
            fixable=False,
        )
        data = original.model_dump()
        restored = QualityServiceIssue(**data)
        assert restored == original

    def test_json_serialization(self):
        issue = QualityServiceIssue(tool="ty", severity="error", message="type mismatch")
        json_str = issue.model_dump_json()
        assert "ty" in json_str
        assert "type mismatch" in json_str

    def test_fixable_true(self):
        issue = QualityServiceIssue(
            tool="ruff-format", severity="info", message="formatting", fixable=True
        )
        assert issue.fixable is True

    def test_large_line_number(self):
        issue = QualityServiceIssue(tool="ruff", severity="warning", message="msg", line=999999)
        assert issue.line == 999999

    def test_zero_line_number(self):
        issue = QualityServiceIssue(tool="ruff", severity="warning", message="msg", line=0)
        assert issue.line == 0

    def test_negative_line_number(self):
        """Pydantic allows negative int since no validator restricts it."""
        issue = QualityServiceIssue(tool="ruff", severity="warning", message="msg", line=-1)
        assert issue.line == -1

    def test_unicode_message(self):
        issue = QualityServiceIssue(
            tool="ruff", severity="error", message="Variable 'x' is not defined"
        )
        assert "not defined" in issue.message

    def test_long_message(self):
        long_msg = "x" * 10000
        issue = QualityServiceIssue(tool="t", severity="s", message=long_msg)
        assert len(issue.message) == 10000
