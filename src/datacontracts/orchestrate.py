"""Glue for running contract checks from a scheduler.

Kept free of Airflow imports so it can be unit tested anywhere. Two kinds of
failure are kept apart on purpose:

* ``FileNotFoundError`` / ``OSError``: transient (a late upload, a flaky mount).
  A scheduler should retry these.
* ``ContractFailed``: the data arrived and breaks the contract. Retrying cannot
  fix it, so a scheduler should fail the task at once and alert.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .spec import load_contract
from .validate import validate


class ContractFailed(Exception):
    """The data was read fine but one or more blocking rules failed."""

    def __init__(self, source: str, report: dict[str, Any]):
        self.source = source
        self.report = report
        super().__init__(alert_text(source, report))


def check(contract_path: str, source: str, now: datetime | None = None) -> dict[str, Any]:
    """Validate one file. Raises FileNotFoundError (retryable) or ContractFailed (not)."""
    if not Path(source).exists():
        raise FileNotFoundError(source)
    report = validate(load_contract(contract_path), source, now=now).as_dict()
    if not report["ok"]:
        raise ContractFailed(source, report)
    return report


def failed_rules(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Blocking failures only (warn-level rules are not listed)."""
    return [r for r in report["results"] if not r["passed"] and r["severity"] != "warn"]


def alert_text(source: str, report: dict[str, Any], limit: int = 5) -> str:
    bad = failed_rules(report)
    head = f"Contract {report['dataset']} v{report['version']} FAILED on {Path(source).name}: "
    head += f"{len(bad)} rule(s), {report['rows']:,} rows."
    lines = [
        f"- {r['rule']}: {r['violations']:,} violations ({r['rate']:.4%}, allowed {r['allowed_rate']:.4%})"
        for r in bad[:limit]
    ]
    if len(bad) > limit:
        lines.append(f"- and {len(bad) - limit} more")
    return "\n".join([head, *lines])


def send_alert(text: str, log_path: str | None = None, webhook: str | None = None) -> list[str]:
    """Always append to a log file; also POST to a Slack-style webhook if one is set.

    Returns the channels used. A webhook failure is reported, not raised, so a
    broken alert channel never hides the original failure.
    """
    used = []
    log_path = log_path or os.environ.get("DATACONTRACTS_ALERT_LOG")
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\n{text}\n\n")
        used.append("log")
    webhook = webhook or os.environ.get("DATACONTRACTS_WEBHOOK")
    if webhook:
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10).read()
            used.append("webhook")
        except OSError as e:
            used.append(f"webhook-failed:{type(e).__name__}")
    return used
