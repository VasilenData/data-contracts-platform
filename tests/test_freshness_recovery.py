from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from datacontracts.spec import parse_contract
from datacontracts.validate import validate


def contract(kind="timestamp"):
    return parse_contract(
        {
            "dataset": "events",
            "version": "1.0.0",
            "owner": "test",
            "columns": [{"name": "ts", "type": kind}],
            "freshness": {"column": "ts", "max_age_hours": 24},
        }
    )


@pytest.mark.parametrize("kind,sql", [("timestamp", "timestamp '2026-01-01 12:00:00'"), ("date", "date '2026-01-01'")])
def test_timezone_aware_clock_and_date_source(tmp_path, kind, sql):
    source = tmp_path / "data.parquet"
    with duckdb.connect() as con:
        con.execute(f"copy (select {sql} as ts) to ? (format parquet)", [str(source)])
    # 13:00 UTC+1 is noon UTC; DATE values are interpreted at midnight UTC.
    now = datetime(2026, 1, 2, 13 if kind == "timestamp" else 1, tzinfo=timezone(timedelta(hours=1)))
    assert validate(contract(kind), str(source), now=now).ok
    assert not validate(contract(kind), str(source), now=now + timedelta(seconds=1)).ok


def test_missing_freshness_column_reports_schema_failure_instead_of_query_crash(tmp_path):
    source = tmp_path / "data.parquet"
    with duckdb.connect() as con:
        con.execute("copy (select 1 as other) to ? (format parquet)", [str(source)])
        report = validate(contract(), str(source), con=con)
        assert not report.ok and report.failures[0].rule == "schema:ts"
        assert con.execute("select 1").fetchone() == (1,)


def test_empty_allowed_values_rejects_all_nonnull_values(tmp_path):
    source = tmp_path / "data.parquet"
    with duckdb.connect() as con:
        con.execute("copy (select * from (values ('x'), (null)) t(value)) to ? (format parquet)", [str(source)])
    spec = parse_contract(
        {
            "dataset": "empty-domain",
            "version": "1.0.0",
            "owner": "test",
            "columns": [{"name": "value", "type": "string", "allowed": []}],
        }
    )
    report = validate(spec, str(source))
    assert not report.ok and report.failures[0].violations == 1
