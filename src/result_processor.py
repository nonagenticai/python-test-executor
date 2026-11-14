#!/usr/bin/env python3
"""Result processor module."""

from __future__ import annotations

import json
import logging
import re

from .quality_models import QualityServiceIssue

logger = logging.getLogger(__name__)


class ResultProcessor:
    """Processes and parses quality analysis tool outputs."""

    def __init__(self) -> None:
        """Initialize result processor."""
        pass

    async def parse_tool_output(
        self,
        tool_name: str,
        stdout: str,
        stderr: str,
        language: str,
        exit_code: int,
    ) -> list[QualityServiceIssue]:
        """Parse tool output to extract quality issues with updated error handling.

        Python tooling is astral: ``ruff`` (``ruff check --output-format json``),
        ``ruff-format`` (``ruff format --check``) and ``ty``
        (``ty check --output-format gitlab``), plus bandit / vulture / radon.

        ``exit_code`` is REQUIRED: ruff / ruff-format / ty exit 0 clean, 1 with
        findings, and 2 when the tool itself failed (bad config, unreadable path,
        panic). ty can exit 2 while still printing a valid JSON array, so stdout
        alone cannot tell a failed run from findings. A failed run is reported as a
        ``tool-failure`` error issue, never as a clean result.
        """
        issues: list[QualityServiceIssue] = []

        try:
            if tool_name == "ruff":
                issues = await self._parse_ruff_output(stdout, stderr, exit_code)
            elif tool_name == "ty":
                issues = await self._parse_ty_output(stdout, stderr, exit_code)
            elif tool_name == "bandit" and stdout:
                issues = await self._parse_bandit_output(stdout)
            elif tool_name == "eslint" and stdout:
                issues = await self._parse_eslint_output(stdout)
            elif tool_name in ["ruff-format", "prettier"] and (stdout or stderr or exit_code >= 2):
                issues = await self._parse_formatter_output(tool_name, stdout, stderr, exit_code)
            elif tool_name == "vulture":
                issues = await self._parse_vulture_output(stdout, stderr)
            elif tool_name == "radon" and stdout:
                issues = await self._parse_radon_output(stdout)

        except Exception as e:
            logger.warning(f"Error parsing {tool_name} output: {e}")

        return issues

    @staticmethod
    def _tool_failure(tool: str, reason: str, stderr: str) -> QualityServiceIssue:
        """An issue saying the tool did not complete - distinct from 'no findings'."""
        tail = stderr.strip()[-500:]
        message = f"{tool} failed to run: {reason}"
        if tail:
            message += f" - {tail}"
        return QualityServiceIssue(
            tool=tool,
            severity="error",
            message=message,
            rule_id="tool-failure",
            fixable=False,
        )

    @staticmethod
    def _json_list(stdout: str) -> list | None:
        """The tool's JSON array, or None when stdout is not one (empty included)."""
        try:
            data = json.loads(stdout.strip())
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, list) else None

    async def _parse_ruff_output(
        self, stdout: str, stderr: str, exit_code: int
    ) -> list[QualityServiceIssue]:
        """Parse ``ruff check --output-format json``.

        Shape: ``[{code, message, filename, location: {row, column}, fix, url, ...}]``.
        Clean prints ``[]`` (exit 0), so empty or non-JSON stdout means ruff did not
        complete, as does exit code >= 2.
        """
        issues: list[QualityServiceIssue] = []
        data = self._json_list(stdout)
        if exit_code >= 2:
            issues.append(self._tool_failure("ruff", f"exit code {exit_code}", stderr))
        elif data is None:
            reason = "no output" if not stdout.strip() else "output is not a JSON array"
            issues.append(self._tool_failure("ruff", reason, stderr))
            return issues

        for item in data or []:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            location = item.get("location") or {}
            # Syntax errors carry code "invalid-syntax" (measured, ruff 0.17.3); F
            # (pyflakes) and E9 are real defects; the rest is style/maintainability.
            is_error = code in (None, "invalid-syntax") or code.startswith(("F", "E9"))
            issues.append(
                QualityServiceIssue(
                    tool="ruff",
                    severity="error" if is_error else "warning",
                    message=item.get("message", ""),
                    line=location.get("row"),
                    column=location.get("column"),
                    rule_id=code or item.get("name"),
                    fixable=item.get("fix") is not None,
                )
            )
        return issues

    # ty emits GitLab code-quality severities; map them onto this model's scale.
    _TY_SEVERITY = {
        "blocker": "error",
        "critical": "error",
        "major": "error",
        "minor": "warning",
        "info": "info",
    }

    async def _parse_ty_output(
        self, stdout: str, stderr: str, exit_code: int
    ) -> list[QualityServiceIssue]:
        """Parse ``ty check --output-format gitlab``.

        Shape: ``[{check_name, description, severity, fingerprint,
        location: {path, positions: {begin: {line, column}}}}]``. Exit 0 clean
        (prints ``[]``), 1 diagnostics, 2 when ty itself failed - which can still
        print a JSON array (measured on ty 0.0.92: a missing path exits 2 with an
        ``io`` diagnostic), so the exit code is checked independently of the parse.

        Unresolved imports are expected when code is analysed in isolation, so they
        are counted into one summary issue rather than reported individually.
        """
        issues: list[QualityServiceIssue] = []
        data = self._json_list(stdout)
        if exit_code >= 2:
            issues.append(self._tool_failure("ty", f"exit code {exit_code}", stderr))
        elif data is None:
            reason = "no output" if not stdout.strip() else "output is not a JSON array"
            issues.append(self._tool_failure("ty", reason, stderr))
            return issues

        import_error_count = 0
        for item in data or []:
            if not isinstance(item, dict):
                continue
            check_name = item.get("check_name")
            if check_name == "unresolved-import":
                import_error_count += 1
                continue
            begin = ((item.get("location") or {}).get("positions") or {}).get("begin") or {}
            issues.append(
                QualityServiceIssue(
                    tool="ty",
                    severity=self._TY_SEVERITY.get(str(item.get("severity", "")).lower(), "error"),
                    message=item.get("description", ""),
                    line=begin.get("line"),
                    column=begin.get("column"),
                    rule_id=check_name,
                    fixable=False,
                )
            )

        if import_error_count > 0:
            issues.append(
                QualityServiceIssue(
                    tool="ty",
                    severity="info",
                    message=f"Skipped {import_error_count} import-related errors (expected in isolated analysis)",
                    fixable=False,
                )
            )
        return issues

    async def _parse_bandit_output(self, stdout: str) -> list[QualityServiceIssue]:
        """Parse bandit JSON output with robust error handling."""
        issues: list[QualityServiceIssue] = []

        try:
            stdout_clean = stdout.strip()
            bandit_data = json.loads(stdout_clean)
            for result in bandit_data.get("results", []):
                issues.append(
                    QualityServiceIssue(
                        tool="bandit",
                        severity=result.get("issue_severity", "medium").lower(),
                        message=result.get("issue_text", ""),
                        line=result.get("line_number"),
                        rule_id=result.get("test_id"),
                        fixable=False,
                    )
                )
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse bandit JSON output (length: {len(stdout)}): {e}")
            # Create error issue for failed parsing
            issues.append(
                QualityServiceIssue(
                    tool="bandit",
                    severity="error",
                    message=f"Bandit failed to produce valid JSON output. Parse error: {str(e)}",
                    fixable=False,
                )
            )

        return issues

    async def _parse_eslint_output(self, stdout: str) -> list[QualityServiceIssue]:
        """Parse ESLint JSON output."""
        issues: list[QualityServiceIssue] = []

        try:
            eslint_data = json.loads(stdout)
            for file_result in eslint_data:
                for message in file_result.get("messages", []):
                    issues.append(
                        QualityServiceIssue(
                            tool="eslint",
                            severity=message.get("severity") == 2 and "error" or "warning",
                            message=message.get("message", ""),
                            line=message.get("line"),
                            column=message.get("column"),
                            rule_id=message.get("ruleId"),
                            fixable=message.get("resolve") is not None,
                        )
                    )
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse eslint JSON output: {e}")

        return issues

    async def _parse_formatter_output(
        self, tool_name: str, stdout: str, stderr: str, exit_code: int
    ) -> list[QualityServiceIssue]:
        """Parse formatting tool output with specific details."""
        issues: list[QualityServiceIssue] = []
        output_text = stdout + "\n" + stderr

        if tool_name == "prettier":
            # Parse prettier-specific output
            if "SyntaxError: Unexpected token" in output_text:
                # Extract line number from syntax error if available
                line_match = re.search(r"line (\d+)", output_text)
                line_num = int(line_match.group(1)) if line_match else None

                issues.append(
                    QualityServiceIssue(
                        tool=tool_name,
                        severity="error",
                        message="Prettier syntax error - file contains invalid JavaScript/TypeScript syntax",
                        line=line_num,
                        fixable=False,
                    )
                )
            elif "[warn]" in output_text and "Code style issues found" in output_text:
                # Extract specific file warnings
                for line in output_text.split("\n"):
                    if line.startswith("[warn]") and "Code style issues found" not in line:
                        file_name = line.replace("[warn]", "").strip()
                        if file_name:
                            issues.append(
                                QualityServiceIssue(
                                    tool=tool_name,
                                    severity="info",
                                    message=f"File '{file_name}' has formatting issues - run 'prettier --write {file_name}' to resolve",
                                    fixable=True,
                                )
                            )
            elif output_text.strip():
                # Fallback for other prettier output
                issues.append(
                    QualityServiceIssue(
                        tool=tool_name,
                        severity="info",
                        message="Prettier found formatting issues. Run 'prettier --write <files>' to resolve.",
                        fixable=True,
                    )
                )
        else:
            # `ruff format --check`: exit 1 + "Would reformat" / "would be reformatted"
            # lines for unformatted files, exit 2 when it could not format at all
            # (e.g. a syntax error) - that is a failure, not a formatting nit.
            if exit_code >= 2:
                issues.append(self._tool_failure(tool_name, f"exit code {exit_code}", output_text))
            elif output_text.strip():
                lowered = output_text.lower()
                if "would reformat" in lowered or "would be reformatted" in lowered:
                    issues.append(
                        QualityServiceIssue(
                            tool=tool_name,
                            severity="info",
                            message="ruff format found formatting issues. Run 'ruff format <files>' to resolve.",
                            fixable=True,
                        )
                    )
                elif exit_code != 0 and "already formatted" not in lowered:
                    issues.append(
                        QualityServiceIssue(
                            tool=tool_name,
                            severity="info",
                            message=f"Code formatting issues detected by {tool_name}",
                            fixable=True,
                        )
                    )

        return issues

    async def _parse_vulture_output(self, stdout: str, stderr: str) -> list[QualityServiceIssue]:
        """Parse vulture output (dead code detection)."""
        issues: list[QualityServiceIssue] = []
        vulture_output = stdout + "\n" + stderr

        for line in vulture_output.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                # Vulture output format: filename:line: message
                if ":" in line:
                    parts = line.split(":")
                    if len(parts) >= 3:
                        try:
                            line_num = int(parts[1]) if parts[1].isdigit() else None
                            message = ":".join(parts[2:]).strip()
                            issues.append(
                                QualityServiceIssue(
                                    tool="vulture",
                                    severity="warning",
                                    message=f"Dead code: {message}",
                                    line=line_num,
                                    fixable=False,
                                )
                            )
                        except (ValueError, IndexError):  # fmt: skip
                            pass

        return issues

    async def _parse_radon_output(self, stdout: str) -> list[QualityServiceIssue]:
        """Parse radon complexity output."""
        issues: list[QualityServiceIssue] = []

        try:
            radon_data = json.loads(stdout)
            for file_path, functions in radon_data.items():
                for func in functions:
                    complexity = func.get("complexity", 0)
                    if complexity > 10:  # High complexity threshold
                        issues.append(
                            QualityServiceIssue(
                                tool="radon",
                                severity=("warning" if complexity > 15 else "info"),
                                message=f"High complexity: {func.get('name', 'unknown')} (complexity: {complexity})",
                                line=func.get("lineno"),
                                fixable=False,
                            )
                        )
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse radon JSON output: {e}")

        return issues


# Create global result processor instance
result_processor = ResultProcessor()
