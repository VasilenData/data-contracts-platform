"""GitHub Actions workflow commands for failed contract rules.

`::error file=...,line=...,title=...::message` makes GitHub show the failure on the
contract file in the run summary and, for pull requests, in the Files changed tab.
"""

from __future__ import annotations

import re

from .validate import Report

COLUMN_RULES = {"not_null", "min", "max", "allowed", "regex", "unique", "schema"}
DATASET_RULES = {"min_rows", "freshness", "extra_columns"}
DATASET_KEYS = {"extra_columns": "allow_extra_columns"}


def escape_data(text: str) -> str:
    """Escape the message part of a workflow command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    """Escape a property value (file, line, title), which also may not contain ':' or ','."""
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def _line_of(text: str, pattern: str) -> int | None:
    rx = re.compile(pattern)
    for number, line in enumerate(text.splitlines(), start=1):
        if rx.search(line):
            return number
    return None


def locate(contract_text: str, rule: str) -> int | None:
    """1-based line in the contract YAML that defines this rule, or None if it cannot be found."""
    kind, _, name = rule.partition(":")
    if name and kind in COLUMN_RULES:
        return _line_of(
            contract_text, rf"name:\s*[\"']?{re.escape(name)}[\"']?\s*[,}}]|name:\s*[\"']?{re.escape(name)}[\"']?\s*$"
        )
    if kind == "check" and name:
        return _line_of(contract_text, rf"name:\s*[\"']?{re.escape(name)}[\"']?\s*[,}}]")
    if rule in DATASET_RULES:
        return _line_of(contract_text, rf"^\s*{DATASET_KEYS.get(rule, rule)}\s*:")
    return None


def github_annotations(report: Report, contract_path: str, contract_text: str) -> list[str]:
    """One workflow command per failed rule, in report order. Sample rows are left out on purpose."""
    lines = []
    for r in report.failures:
        level = "warning" if r.severity == "warn" else "error"
        props = [f"file={escape_property(contract_path)}"]
        where = locate(contract_text, r.rule)
        if where is not None:
            props.append(f"line={where}")
        props.append(f"title={escape_property(r.rule)}")
        message = (
            f"{r.violations:,} violations of {r.rows:,} rows ({r.rate:.4%}), allowed {r.allowed_rate:.4%}. {r.detail}"
        ).strip()
        lines.append(f"::{level} {','.join(props)}::{escape_data(message)}")
    return lines
