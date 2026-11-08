from pathlib import Path

import pytest

pytest.importorskip("airflow")

from airflow.models import DagBag

DAGS = Path(__file__).resolve().parents[1] / "airflow" / "dags"


def test_dag_imports_and_has_retry_policy():
    bag = DagBag(dag_folder=str(DAGS), include_examples=False)
    assert not bag.import_errors
    dag = bag.dags["contract_check"]
    task = dag.get_task("validate_month")
    kw = task.partial_kwargs
    assert kw["retries"] == 3 and kw["retry_exponential_backoff"]
    assert kw["on_failure_callback"] is not None
    assert len(task.op_kwargs_expand_input.value["month"]) == 12
