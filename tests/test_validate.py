"""Known-answer tests: inject an exact number of each violation, expect that count."""

from datetime import datetime

import duckdb
import pandas as pd
import pytest

from datacontracts.spec import parse_contract
from datacontracts.validate import kind_of, validate

N = 1000


@pytest.fixture
def frame():
    df = pd.DataFrame(
        {
            "id": range(N),
            "amount": [10.0] * N,
            "status": ["paid"] * N,
            "email": ["a@b.com"] * N,
            "ts": pd.to_datetime(["2026-01-01 12:00:00"] * N),
            "ts_end": pd.to_datetime(["2026-01-01 13:00:00"] * N),
        }
    )
    return df


def contract(**over):
    raw = {
        "dataset": "t",
        "version": "1.0.0",
        "owner": "o",
        "columns": [
            {"name": "id", "type": "int", "nullable": False, "unique": True},
            {"name": "amount", "type": "float", "min": 0, "max": 100},
            {"name": "status", "type": "string", "allowed": ["paid", "refunded"]},
            {"name": "email", "type": "string", "regex": "^[^@]+@[^@]+$"},
            {"name": "ts", "type": "timestamp"},
            {"name": "ts_end", "type": "timestamp"},
        ],
    }
    raw.update(over)
    return parse_contract(raw)


def run(df, tmp_path, c=None, **kw):
    path = tmp_path / "t.parquet"
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY df TO '{path}' (FORMAT PARQUET)")
    return validate(c or contract(), str(path), **kw)


def by_rule(report):
    return {r.rule: r for r in report.results}


def test_clean_data_passes(frame, tmp_path):
    r = run(frame, tmp_path)
    assert r.ok and r.rows == N and not r.failures


def test_exact_violation_counts(frame, tmp_path):
    frame.loc[0:6, "amount"] = -1.0  # 7 below min
    frame.loc[10:12, "amount"] = 500.0  # 3 above max
    frame.loc[20:23, "status"] = "lost"  # 4 not allowed
    frame.loc[30:35, "email"] = "nope"  # 6 bad format
    frame.loc[40:41, "id"] = 99  # id 99 now appears 3 times: 2 extra duplicates
    r = by_rule(run(frame, tmp_path))
    assert r["min:amount"].violations == 7
    assert r["max:amount"].violations == 3
    assert r["allowed:status"].violations == 4
    assert r["regex:email"].violations == 6
    assert r["unique:id"].violations == 2
    assert not r["min:amount"].passed


def test_nulls_do_not_trip_range_or_allowed_rules(frame, tmp_path):
    frame.loc[0:4, "amount"] = None
    frame.loc[5:9, "status"] = None
    r = by_rule(run(frame, tmp_path))
    assert r["min:amount"].violations == 0
    assert r["allowed:status"].violations == 0


def test_not_null_counts_nulls(frame, tmp_path):
    frame["id"] = frame["id"].astype("Int64")
    frame.loc[0:2, "id"] = pd.NA
    r = by_rule(run(frame, tmp_path))
    assert r["not_null:id"].violations == 3


def test_tolerance_allows_small_rates_but_not_large(frame, tmp_path):
    frame.loc[0:4, "amount"] = -1.0  # 0.5%
    loose = contract()
    loose = parse_contract(
        {
            "dataset": "t",
            "version": "1.0.0",
            "owner": "o",
            "columns": [{"name": "amount", "type": "float", "min": 0, "max_violation_rate": 0.01}],
        }
    )
    strict = parse_contract(
        {
            "dataset": "t",
            "version": "1.0.0",
            "owner": "o",
            "columns": [{"name": "amount", "type": "float", "min": 0, "max_violation_rate": 0.001}],
        }
    )
    assert run(frame, tmp_path, loose).ok
    assert not run(frame, tmp_path, strict).ok


def test_schema_missing_column_and_wrong_type(frame, tmp_path):
    r = by_rule(run(frame.drop(columns=["email"]), tmp_path))
    assert "column is missing" in r["schema:email"].detail
    frame["amount"] = frame["amount"].astype(int)
    r = by_rule(run(frame, tmp_path))
    assert "expected float" in r["schema:amount"].detail
    assert not run(frame, tmp_path).ok


def test_extra_columns_policy(frame, tmp_path):
    frame["surprise"] = 1
    assert run(frame, tmp_path).ok
    closed = contract(allow_extra_columns=False)
    r = by_rule(run(frame, tmp_path, closed))
    assert r["extra_columns"].detail == "surprise"


def test_cross_column_check_and_severity(frame, tmp_path):
    frame.loc[0:8, "ts_end"] = pd.Timestamp("2026-01-01 11:00:00")  # 9 end before start
    c = contract(
        checks=[
            {"name": "order", "expr": "ts_end >= ts"},
            {"name": "soft", "expr": "ts_end >= ts", "severity": "warn"},
        ]
    )
    rep = run(frame, tmp_path, c)
    r = by_rule(rep)
    assert r["check:order"].violations == 9 and r["check:soft"].violations == 9
    assert not rep.ok
    only_warn = contract(checks=[{"name": "soft", "expr": "ts_end >= ts", "severity": "warn"}])
    assert run(frame, tmp_path, only_warn).ok  # warnings do not fail the run


def test_check_with_null_inputs_is_not_a_violation(frame, tmp_path):
    frame.loc[0:4, "ts_end"] = pd.NaT
    c = contract(checks=[{"name": "order", "expr": "ts_end >= ts"}])
    assert by_rule(run(frame, tmp_path, c))["check:order"].violations == 0


def test_min_rows(frame, tmp_path):
    r = by_rule(run(frame, tmp_path, contract(min_rows=N + 5)))
    assert r["min_rows"].violations == 5


def test_freshness_uses_supplied_clock(frame, tmp_path):
    c = contract(freshness={"column": "ts", "max_age_hours": 24})
    fresh = by_rule(run(frame, tmp_path, c, now=datetime(2026, 1, 1, 20, 0)))
    stale = by_rule(run(frame, tmp_path, c, now=datetime(2026, 1, 3, 12, 0)))
    assert fresh["freshness"].passed
    assert not stale["freshness"].passed and "48.0 h" in stale["freshness"].detail


def test_samples_are_returned_for_failures(frame, tmp_path):
    frame.loc[0:9, "amount"] = -5.0
    res = by_rule(run(frame, tmp_path, sample_size=2))["min:amount"]
    assert len(res.samples) == 2 and res.samples[0]["amount"] == "-5.0"


def test_csv_source_and_quoting_of_odd_column_names(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text('"my col",x\n1,a\n-2,b\n')
    c = parse_contract(
        {
            "dataset": "t",
            "version": "1.0.0",
            "owner": "o",
            "columns": [{"name": "my col", "type": "int", "min": 0}],
        }
    )
    rep = validate(c, str(p))
    assert by_rule(rep)["min:my col"].violations == 1


def test_apostrophes_in_allowed_values(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("s\nit's\nno\n")
    c = parse_contract(
        {
            "dataset": "t",
            "version": "1.0.0",
            "owner": "o",
            "columns": [{"name": "s", "type": "string", "allowed": ["it's"]}],
        }
    )
    assert by_rule(validate(c, str(p)))["allowed:s"].violations == 1


@pytest.mark.parametrize(
    "duck,expected",
    [
        ("BIGINT", "int"),
        ("DOUBLE", "float"),
        ("DECIMAL(10,2)", "float"),
        ("VARCHAR", "string"),
        ("TIMESTAMP WITH TIME ZONE", "timestamp"),
        ("DATE", "date"),
        ("BOOLEAN", "bool"),
    ],
)
def test_kind_of(duck, expected):
    assert kind_of(duck) == expected


def test_bounds_are_inclusive(frame, tmp_path):
    frame.loc[0:4, "amount"] = 0.0  # equal to min
    frame.loc[5:9, "amount"] = 100.0  # equal to max
    r = by_rule(run(frame, tmp_path))
    assert r["min:amount"].violations == 0 and r["max:amount"].violations == 0


def test_freshness_limit_is_inclusive(frame, tmp_path):
    c = contract(freshness={"column": "ts", "max_age_hours": 24})
    exactly = by_rule(run(frame, tmp_path, c, now=datetime(2026, 1, 2, 12, 0)))
    assert exactly["freshness"].passed  # 24.0 h old with a 24 h limit
