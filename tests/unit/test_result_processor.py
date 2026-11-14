"""Tests for result_processor.py — all parse methods and dispatch logic."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.result_processor import ResultProcessor


@pytest.fixture
def processor():
    return ResultProcessor()


# ---------------------------------------------------------------------------
# parse_tool_output dispatch
# ---------------------------------------------------------------------------
class TestParseToolOutput:
    """Tests for the top-level dispatch method."""

    async def test_exit_code_is_required(self, processor):
        with pytest.raises(TypeError):
            await processor.parse_tool_output("ty", TY_0092_EXIT2_IO, "", "python")  # type: ignore[call-arg]

    async def test_unknown_tool_returns_empty(self, processor):
        issues = await processor.parse_tool_output("unknown_tool", "output", "", "python", 0)
        assert issues == []

    async def test_empty_stdout_returns_empty_for_bandit(self, processor):
        issues = await processor.parse_tool_output("bandit", "", "", "python", 0)
        assert issues == []

    async def test_empty_stdout_returns_empty_for_eslint(self, processor):
        issues = await processor.parse_tool_output("eslint", "", "", "javascript", 0)
        assert issues == []

    async def test_empty_stdout_returns_empty_for_radon(self, processor):
        issues = await processor.parse_tool_output("radon", "", "", "python", 0)
        assert issues == []

    async def test_replaced_tools_are_not_parsed(self, processor):
        # Forward-only: mypy/pylint/black/isort output is no longer recognised.
        for tool in ("mypy", "pylint", "black", "isort", "flake8"):
            assert (
                await processor.parse_tool_output(tool, "x.py:1: error: e", "", "python", 1) == []
            )

    async def test_vulture_called_with_empty_strings(self, processor):
        issues = await processor.parse_tool_output("vulture", "", "", "python", 0)
        assert issues == []

    async def test_formatter_with_stdout(self, processor):
        issues = await processor.parse_tool_output(
            "ruff-format", "Would reformat: foo.py\n1 file would be reformatted", "", "python", 1
        )
        assert len(issues) == 1
        assert issues[0].tool == "ruff-format"

    async def test_formatter_with_stderr_only(self, processor):
        issues = await processor.parse_tool_output("ruff-format", "", "some stderr", "python", 1)
        assert len(issues) >= 1


# ---------------------------------------------------------------------------
# _parse_ruff_output — `ruff check --output-format json` (shapes measured on ruff 0.17.3)
# ---------------------------------------------------------------------------
RUFF_F401 = {
    "cell": None,
    "code": "F401",
    "end_location": {"column": 10, "row": 1},
    "filename": "/app/test/bad.py",
    "fix": {"applicability": "safe", "edits": [], "message": "Remove unused import: `os`"},
    "location": {"column": 8, "row": 1},
    "message": "`os` imported but unused",
    "name": "unused-import",
    "noqa_row": 1,
    "severity": "error",
    "url": "https://docs.astral.sh/ruff/rules/unused-import",
}
RUFF_I001 = {
    **RUFF_F401,
    "code": "I001",
    "message": "Import block is un-sorted or un-formatted",
    "name": "unsorted-imports",
}
RUFF_E501_NOFIX = {
    **RUFF_F401,
    "code": "E501",
    "fix": None,
    "location": {"column": 101, "row": 7},
    "message": "Line too long (120 > 100)",
}


# Captured verbatim: `uvx ruff@0.17.3 check --output-format json syn.py` on
# `def (:\n` (exit 1; filename shortened). Syntax errors carry
# code "invalid-syntax", not None.
RUFF_0173_SYNTAX_ERROR = """[
  {
    "cell": null,
    "code": "invalid-syntax",
    "end_location": {"column": 6, "row": 1},
    "filename": "/tmp/syn.py",
    "fix": null,
    "location": {"column": 5, "row": 1},
    "message": "Expected an identifier",
    "name": "invalid-syntax",
    "noqa_row": null,
    "severity": "error",
    "url": null
  },
  {
    "cell": null,
    "code": "invalid-syntax",
    "end_location": {"column": 1, "row": 2},
    "filename": "/tmp/syn.py",
    "fix": null,
    "location": {"column": 1, "row": 2},
    "message": "unexpected EOF while parsing",
    "name": "invalid-syntax",
    "noqa_row": null,
    "severity": "error",
    "url": null
  }
]"""

# Captured verbatim: `uvx ty@0.0.92 check --output-format gitlab nonexist.py`
# -> exit 2 WITH a valid JSON array on stdout (path shortened).
TY_0092_EXIT2_IO = """[
  {
    "check_name": "io",
    "description": "io: `/tmp/nonexist.py`: No such file or directory (os error 2)",
    "severity": "major",
    "fingerprint": "482aa4d4dbea8467",
    "location": {
      "path": "",
      "positions": {
        "begin": {"line": 1, "column": 1},
        "end": {"line": 1, "column": 1}
      }
    }
  }
]"""


class TestParseRuffOutput:
    async def test_diagnostics_are_mapped(self, processor):
        stdout = json.dumps([RUFF_F401, RUFF_I001, RUFF_E501_NOFIX])
        issues = await processor.parse_tool_output("ruff", stdout, "", "python", exit_code=1)
        assert [i.rule_id for i in issues] == ["F401", "I001", "E501"]
        f401, i001, e501 = issues
        assert f401.tool == "ruff"
        assert (f401.line, f401.column) == (1, 8)
        assert f401.severity == "error"  # pyflakes = real defect
        assert f401.fixable is True
        assert i001.severity == "warning"  # isort `I` = style
        assert e501.fixable is False
        assert (e501.line, e501.column) == (7, 101)

    async def test_real_syntax_error_output_is_error(self, processor):
        issues = await processor.parse_tool_output(
            "ruff", RUFF_0173_SYNTAX_ERROR, "", "python", exit_code=1
        )
        assert [i.rule_id for i in issues] == ["invalid-syntax", "invalid-syntax"]
        assert all(i.severity == "error" for i in issues)
        assert (issues[0].line, issues[0].column) == (1, 5)

    async def test_clean_is_empty(self, processor):
        assert await processor.parse_tool_output("ruff", "[]", "", "python", exit_code=0) == []

    async def test_exit_2_is_tool_failure_not_clean(self, processor):
        stderr = "error: invalid value 'bogus=1' for '--config <CONFIG_OPTION>'"
        issues = await processor.parse_tool_output("ruff", "", stderr, "python", exit_code=2)
        assert len(issues) == 1
        assert issues[0].rule_id == "tool-failure"
        assert issues[0].severity == "error"
        assert "exit code 2" in issues[0].message
        assert "bogus" in issues[0].message

    async def test_empty_stdout_at_exit_0_is_tool_failure(self, processor):
        # Clean ruff prints `[]`; nothing at all means it never completed.
        issues = await processor.parse_tool_output("ruff", "", "", "python", exit_code=0)
        assert [i.rule_id for i in issues] == ["tool-failure"]
        assert "no output" in issues[0].message

    async def test_non_json_stdout_is_tool_failure(self, processor):
        issues = await processor._parse_ruff_output("panic: something", "", 1)
        assert [i.rule_id for i in issues] == ["tool-failure"]
        assert "not a JSON array" in issues[0].message

    async def test_non_dict_items_skipped(self, processor):
        issues = await processor._parse_ruff_output(json.dumps(["x", RUFF_F401]), "", 1)
        assert [i.rule_id for i in issues] == ["F401"]


# ---------------------------------------------------------------------------
# _parse_ty_output — `ty check --output-format gitlab` (shapes measured on ty 0.0.92)
# ---------------------------------------------------------------------------
def _ty_item(check_name, description, line, column, severity="major"):
    return {
        "check_name": check_name,
        "description": f"{check_name}: {description}",
        "severity": severity,
        "fingerprint": "966195585497f407",  # pragma: allowlist secret (ty fingerprint, not a secret)
        "location": {
            "path": "bad.py",
            "positions": {
                "begin": {"line": line, "column": column},
                "end": {"line": line, "column": column + 3},
            },
        },
    }


class TestParseTyOutput:
    async def test_diagnostics_are_mapped(self, processor):
        stdout = json.dumps(
            [
                _ty_item("invalid-assignment", "Object of type ... not assignable", 3, 10),
                _ty_item("possibly-unresolved-reference", "Name may be undefined", 5, 12, "minor"),
            ]
        )
        issues = await processor.parse_tool_output("ty", stdout, "", "python", exit_code=1)
        assert len(issues) == 2
        first, second = issues
        assert first.tool == "ty"
        assert first.rule_id == "invalid-assignment"
        assert first.severity == "error"
        assert (first.line, first.column) == (3, 10)
        assert first.message.startswith("invalid-assignment:")
        assert second.severity == "warning"

    async def test_clean_is_empty(self, processor):
        assert await processor.parse_tool_output("ty", "[]", "", "python", exit_code=0) == []

    async def test_unresolved_imports_are_summarised(self, processor):
        stdout = json.dumps(
            [
                _ty_item("unresolved-import", "Cannot resolve `foo`", 1, 8),
                _ty_item("unresolved-import", "Cannot resolve `bar`", 2, 8),
                _ty_item("invalid-return-type", "bad return", 9, 5),
            ]
        )
        issues = await processor._parse_ty_output(stdout, "", 1)
        assert [i.rule_id for i in issues] == ["invalid-return-type", None]
        assert "Skipped 2 import-related errors" in issues[1].message
        assert issues[1].severity == "info"

    async def test_crash_with_no_stdout_is_tool_failure(self, processor):
        # Measured: `ty check --python /nonexistent` -> exit 2, stdout empty,
        # stderr "ty failed\n  Cause: Invalid `--python` argument ...".
        stderr = "ty failed\n  Cause: Invalid `--python` argument `/nonexistent`"
        issues = await processor.parse_tool_output("ty", "", stderr, "python", exit_code=2)
        assert len(issues) == 1
        assert issues[0].rule_id == "tool-failure"
        assert issues[0].severity == "error"
        assert "ty failed" in issues[0].message

    async def test_exit_2_with_json_is_still_tool_failure(self, processor):
        # Real ty 0.0.92 output: a missing path exits 2 yet prints valid JSON.
        # The exit code must win over the "parsed fine" reading.
        issues = await processor.parse_tool_output(
            "ty", TY_0092_EXIT2_IO, "", "python", exit_code=2
        )
        assert issues[0].rule_id == "tool-failure"
        assert [i.rule_id for i in issues[1:]] == ["io"]

    async def test_empty_stdout_at_exit_0_is_tool_failure(self, processor):
        issues = await processor.parse_tool_output("ty", "", "", "python", exit_code=0)
        assert [i.rule_id for i in issues] == ["tool-failure"]

    async def test_non_json_stdout_is_tool_failure(self, processor):
        issues = await processor._parse_ty_output("thread 'main' panicked", "", 1)
        assert [i.rule_id for i in issues] == ["tool-failure"]

    async def test_unknown_severity_defaults_to_error(self, processor):
        stdout = json.dumps([_ty_item("x", "y", 1, 1, severity="weird")])
        issues = await processor._parse_ty_output(stdout, "", 1)
        assert issues[0].severity == "error"

    async def test_missing_location_tolerated(self, processor):
        stdout = json.dumps([{"check_name": "x", "description": "y", "severity": "major"}, 3])
        issues = await processor._parse_ty_output(stdout, "", 1)
        assert len(issues) == 1
        assert issues[0].line is None


# ---------------------------------------------------------------------------
# _parse_bandit_output
# ---------------------------------------------------------------------------
class TestParseBanditOutput:
    """Tests for bandit output parsing."""

    async def test_valid_bandit_json(self, processor):
        data = {
            "results": [
                {
                    "issue_severity": "HIGH",
                    "issue_text": "SQL injection possible",
                    "line_number": 42,
                    "test_id": "B608",
                }
            ]
        }
        issues = await processor._parse_bandit_output(json.dumps(data))
        assert len(issues) == 1
        assert issues[0].tool == "bandit"
        assert issues[0].severity == "high"
        assert issues[0].message == "SQL injection possible"
        assert issues[0].line == 42
        assert issues[0].rule_id == "B608"

    async def test_empty_results(self, processor):
        issues = await processor._parse_bandit_output(json.dumps({"results": []}))
        assert issues == []

    async def test_no_results_key(self, processor):
        issues = await processor._parse_bandit_output(json.dumps({"errors": []}))
        assert issues == []

    async def test_invalid_json(self, processor):
        issues = await processor._parse_bandit_output("not json")
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "parse error" in issues[0].message.lower() or "json" in issues[0].message.lower()

    async def test_multiple_results(self, processor):
        data = {
            "results": [
                {
                    "issue_severity": "MEDIUM",
                    "issue_text": "hardcoded password",
                    "line_number": 10,
                    "test_id": "B105",
                },
                {
                    "issue_severity": "LOW",
                    "issue_text": "assert used",
                    "line_number": 20,
                    "test_id": "B101",
                },
            ]
        }
        issues = await processor._parse_bandit_output(json.dumps(data))
        assert len(issues) == 2
        assert issues[0].severity == "medium"
        assert issues[1].severity == "low"


# ---------------------------------------------------------------------------
# _parse_eslint_output
# ---------------------------------------------------------------------------
class TestParseEslintOutput:
    """Tests for ESLint output parsing."""

    async def test_valid_eslint_json(self, processor):
        data = [
            {
                "messages": [
                    {
                        "severity": 2,
                        "message": "Unexpected var",
                        "line": 1,
                        "column": 1,
                        "ruleId": "no-var",
                        "resolve": None,
                    },
                    {
                        "severity": 1,
                        "message": "Missing semicolon",
                        "line": 5,
                        "column": 10,
                        "ruleId": "semi",
                        "resolve": {"text": ";"},
                    },
                ]
            }
        ]
        issues = await processor._parse_eslint_output(json.dumps(data))
        assert len(issues) == 2
        assert issues[0].severity == "error"  # severity == 2 -> "error"
        assert issues[0].fixable is False  # resolve is None
        assert issues[1].severity == "warning"  # severity == 1 -> "warning"
        assert issues[1].fixable is True  # resolve is not None

    async def test_empty_messages(self, processor):
        data = [{"messages": []}]
        issues = await processor._parse_eslint_output(json.dumps(data))
        assert issues == []

    async def test_invalid_json(self, processor):
        issues = await processor._parse_eslint_output("not json")
        assert issues == []

    async def test_multiple_files(self, processor):
        data = [
            {
                "messages": [
                    {"severity": 2, "message": "err1", "line": 1, "column": 1, "ruleId": "r1"}
                ]
            },
            {
                "messages": [
                    {"severity": 1, "message": "warn1", "line": 2, "column": 2, "ruleId": "r2"}
                ]
            },
        ]
        issues = await processor._parse_eslint_output(json.dumps(data))
        assert len(issues) == 2


# ---------------------------------------------------------------------------
# _parse_formatter_output
# ---------------------------------------------------------------------------
class TestParseFormatterOutput:
    """Tests for formatter (ruff format --check, prettier) parsing."""

    async def test_prettier_syntax_error(self, processor):
        output = "SyntaxError: Unexpected token at line 42"
        issues = await processor._parse_formatter_output("prettier", output, "", 1)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].line == 42

    async def test_prettier_syntax_error_no_line(self, processor):
        output = "SyntaxError: Unexpected token"
        issues = await processor._parse_formatter_output("prettier", output, "", 1)
        assert len(issues) == 1
        assert issues[0].line is None

    async def test_prettier_warn_issues(self, processor):
        output = "[warn] src/foo.js\n[warn] src/bar.js\n[warn] Code style issues found in the above files."
        issues = await processor._parse_formatter_output("prettier", output, "", 1)
        assert len(issues) == 2
        assert all(i.fixable for i in issues)
        assert "foo.js" in issues[0].message

    async def test_prettier_other_output(self, processor):
        output = "some other prettier output"
        issues = await processor._parse_formatter_output("prettier", output, "", 1)
        assert len(issues) == 1
        assert issues[0].fixable is True

    async def test_prettier_empty_output(self, processor):
        issues = await processor._parse_formatter_output("prettier", "", "", 0)
        assert issues == []

    async def test_ruff_format_would_reformat(self, processor):
        # ruff 0.17.3 `format --check` on an unformatted file, exit 1.
        output = (
            "unformatted: File would be reformatted\n --> bad.py:3:1\n1 file would be reformatted"
        )
        issues = await processor._parse_formatter_output("ruff-format", output, "", 1)
        assert len(issues) == 1
        assert "formatting issues" in issues[0].message.lower()
        assert "ruff format" in issues[0].message
        assert issues[0].fixable is True

    async def test_ruff_format_legacy_would_reformat_wording(self, processor):
        issues = await processor._parse_formatter_output(
            "ruff-format", "Would reformat: a.py", "", 1
        )
        assert len(issues) == 1

    async def test_ruff_format_already_formatted_is_clean(self, processor):
        issues = await processor._parse_formatter_output(
            "ruff-format", "1 file already formatted", "", 0
        )
        assert issues == []

    async def test_ruff_format_other_output(self, processor):
        issues = await processor._parse_formatter_output("ruff-format", "something odd", "", 1)
        assert len(issues) == 1
        assert issues[0].tool == "ruff-format"

    async def test_ruff_format_exit_2_is_tool_failure(self, processor):
        # Measured: `ruff format --check` on a syntax error exits 2 ("invalid-syntax").
        out = "invalid-syntax: Expected an identifier\n --> syn.py:1:5"
        issues = await processor.parse_tool_output("ruff-format", out, "", "python", exit_code=2)
        assert len(issues) == 1
        assert issues[0].rule_id == "tool-failure"
        assert issues[0].severity == "error"

    async def test_ruff_format_exit_2_with_no_output_is_tool_failure(self, processor):
        issues = await processor.parse_tool_output("ruff-format", "", "", "python", exit_code=2)
        assert [i.rule_id for i in issues] == ["tool-failure"]

    async def test_formatter_empty_output(self, processor):
        issues = await processor._parse_formatter_output("ruff-format", "", "", 0)
        assert issues == []


# ---------------------------------------------------------------------------
# _parse_vulture_output
# ---------------------------------------------------------------------------
class TestParseVultureOutput:
    """Tests for vulture (dead code) output parsing."""

    async def test_standard_output(self, processor):
        stdout = "foo.py:10: unused function 'bar' (60% confidence)"
        issues = await processor._parse_vulture_output(stdout, "")
        assert len(issues) == 1
        assert issues[0].tool == "vulture"
        assert issues[0].severity == "warning"
        assert issues[0].line == 10
        assert "Dead code" in issues[0].message

    async def test_multiple_lines(self, processor):
        stdout = "foo.py:10: unused function 'bar'\nbaz.py:20: unused variable 'x'"
        issues = await processor._parse_vulture_output(stdout, "")
        assert len(issues) == 2

    async def test_empty_output(self, processor):
        issues = await processor._parse_vulture_output("", "")
        assert issues == []

    async def test_comment_lines_skipped(self, processor):
        stdout = "# some comment\nfoo.py:1: unused import 'os'"
        issues = await processor._parse_vulture_output(stdout, "")
        assert len(issues) == 1

    async def test_stderr_also_parsed(self, processor):
        stderr = "foo.py:5: unused class 'MyClass'"
        issues = await processor._parse_vulture_output("", stderr)
        assert len(issues) == 1

    async def test_non_numeric_line(self, processor):
        stdout = "foo.py:abc: some message"
        issues = await processor._parse_vulture_output(stdout, "")
        assert len(issues) == 1
        assert issues[0].line is None


# ---------------------------------------------------------------------------
# _parse_radon_output
# ---------------------------------------------------------------------------
class TestParseRadonOutput:
    """Tests for radon complexity output parsing."""

    async def test_high_complexity(self, processor):
        data = {
            "foo.py": [
                {"name": "complex_func", "complexity": 20, "lineno": 5},
            ]
        }
        issues = await processor._parse_radon_output(json.dumps(data))
        assert len(issues) == 1
        assert issues[0].severity == "warning"  # > 15
        assert "complex_func" in issues[0].message
        assert issues[0].line == 5

    async def test_medium_complexity(self, processor):
        data = {
            "foo.py": [
                {"name": "medium_func", "complexity": 12, "lineno": 10},
            ]
        }
        issues = await processor._parse_radon_output(json.dumps(data))
        assert len(issues) == 1
        assert issues[0].severity == "info"  # > 10 but <= 15

    async def test_low_complexity_ignored(self, processor):
        data = {
            "foo.py": [
                {"name": "simple_func", "complexity": 5, "lineno": 1},
            ]
        }
        issues = await processor._parse_radon_output(json.dumps(data))
        assert issues == []

    async def test_boundary_complexity_10(self, processor):
        data = {
            "foo.py": [
                {"name": "edge_func", "complexity": 10, "lineno": 1},
            ]
        }
        issues = await processor._parse_radon_output(json.dumps(data))
        assert issues == []  # threshold is > 10, not >=

    async def test_boundary_complexity_11(self, processor):
        data = {
            "foo.py": [
                {"name": "edge_func", "complexity": 11, "lineno": 1},
            ]
        }
        issues = await processor._parse_radon_output(json.dumps(data))
        assert len(issues) == 1

    async def test_multiple_files_and_functions(self, processor):
        data = {
            "foo.py": [
                {"name": "a", "complexity": 15, "lineno": 1},
                {"name": "b", "complexity": 5, "lineno": 20},
            ],
            "bar.py": [
                {"name": "c", "complexity": 25, "lineno": 10},
            ],
        }
        issues = await processor._parse_radon_output(json.dumps(data))
        assert len(issues) == 2  # a (15 > 10) and c (25 > 10), b is 5

    async def test_invalid_json(self, processor):
        issues = await processor._parse_radon_output("not json")
        assert issues == []

    async def test_empty_json_object(self, processor):
        issues = await processor._parse_radon_output("{}")
        assert issues == []

    async def test_missing_name_key(self, processor):
        data = {"foo.py": [{"complexity": 20, "lineno": 1}]}
        issues = await processor._parse_radon_output(json.dumps(data))
        assert len(issues) == 1
        assert "unknown" in issues[0].message


# ---------------------------------------------------------------------------
# parse_tool_output dispatch coverage — going through the top-level method
# ---------------------------------------------------------------------------
class TestParseToolOutputDispatch:
    """Cover the dispatch branches inside parse_tool_output."""

    async def test_dispatch_ruff(self, processor):
        issues = await processor.parse_tool_output("ruff", json.dumps([RUFF_F401]), "", "python", 1)
        assert len(issues) == 1
        assert issues[0].tool == "ruff"

    async def test_dispatch_ty(self, processor):
        stdout = json.dumps([_ty_item("invalid-assignment", "x", 1, 1)])
        issues = await processor.parse_tool_output("ty", stdout, "", "python", 1)
        assert len(issues) == 1
        assert issues[0].tool == "ty"

    async def test_dispatch_bandit(self, processor):
        data = {
            "results": [
                {
                    "issue_severity": "HIGH",
                    "issue_text": "SQL injection",
                    "line_number": 5,
                    "test_id": "B608",
                }
            ]
        }
        issues = await processor.parse_tool_output("bandit", json.dumps(data), "", "python", 1)
        assert len(issues) == 1
        assert issues[0].tool == "bandit"

    async def test_dispatch_eslint(self, processor):
        data = [
            {
                "messages": [
                    {"severity": 2, "message": "err", "line": 1, "column": 1, "ruleId": "r1"}
                ]
            }
        ]
        issues = await processor.parse_tool_output("eslint", json.dumps(data), "", "javascript", 1)
        assert len(issues) == 1
        assert issues[0].tool == "eslint"

    async def test_dispatch_radon(self, processor):
        data = {"foo.py": [{"name": "f", "complexity": 20, "lineno": 1}]}
        issues = await processor.parse_tool_output("radon", json.dumps(data), "", "python", 1)
        assert len(issues) == 1
        assert issues[0].tool == "radon"

    async def test_dispatch_exception_handler(self, processor):
        """Cover the outer except block (lines 44-45) in parse_tool_output."""
        with patch.object(
            processor,
            "_parse_bandit_output",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            issues = await processor.parse_tool_output("bandit", "some output", "", "python", 1)
        assert issues == []


# ---------------------------------------------------------------------------
# Additional vulture edge cases
# ---------------------------------------------------------------------------
class TestVultureEdgeCases:
    """Cover the ValueError/IndexError fallback in _parse_vulture_output (lines 373-374)."""

    async def test_value_error_in_vulture_parsing(self, processor):
        """Force ValueError fallback — the except just passes."""
        with patch("builtins.int", side_effect=ValueError("bad")):
            issues = await processor._parse_vulture_output("foo.py:10: unused function 'bar'", "")
        # ValueError is caught and pass'd, so no issues should be created
        assert issues == []
