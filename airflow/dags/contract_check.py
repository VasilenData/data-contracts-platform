"""Airflow DAG: validate each monthly file against a data contract.

Set DATACONTRACTS_DATA_DIR to the folder holding yellow_tripdata_2023-MM.parquet.
Optional: DATACONTRACTS_ALERT_LOG (file) and DATACONTRACTS_WEBHOOK (Slack-style URL).

Retry policy: transient I/O errors retry with exponential backoff. A contract
violation raises AirflowFailException, which skips retries (the data will not
change by waiting) and goes straight to the failure alert.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException

from datacontracts.orchestrate import ContractFailed, alert_text, check, send_alert

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = str(ROOT / "contracts" / "yellow_taxi_v1.yaml")
DATA_DIR = os.environ.get("DATACONTRACTS_DATA_DIR", str(ROOT / "data"))
RETRY_DELAY = int(os.environ.get("DATACONTRACTS_RETRY_DELAY", "10"))  # seconds
MONTHS = [f"2023-{m:02d}" for m in range(1, 13)]


def on_failure(context):
    """Runs after the final attempt only, for failures that are not contract failures.

    Contract failures are alerted inside the task (with the rule detail) and raise
    AirflowFailException, so they are skipped here to avoid a duplicate alert.
    """
    exc = context.get("exception")
    if isinstance(exc, AirflowFailException):
        return
    ti = context["task_instance"]
    send_alert(f"Task {ti.task_id} (map index {ti.map_index}) failed after retries: {exc!r}")


@dag(
    dag_id="contract_check",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=RETRY_DELAY),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=5),
        "on_failure_callback": on_failure,
    },
    tags=["data-quality", "contracts"],
)
def contract_check():
    @task
    def validate_month(month: str) -> dict:
        source = f"{DATA_DIR}/yellow_tripdata_{month}.parquet"
        try:
            report = check(CONTRACT, source)
        except ContractFailed as e:
            # not retryable: alert with the rule detail, then fail without retries
            send_alert(alert_text(e.source, e.report))
            raise AirflowFailException(str(e)) from e
        return {"month": month, "rows": report["rows"], "ok": True}

    validate_month.expand(month=MONTHS)


contract_check()
