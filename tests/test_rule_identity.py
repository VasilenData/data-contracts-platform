"""Adding or reordering warnings must never downgrade a blocking failure."""

import itertools
import random

import duckdb

from datacontracts.spec import Check, Column, Contract
from datacontracts.validate import validate


def test_direct_contract_rule_severity_is_independent_of_names_and_order(tmp_path):
    source = tmp_path / "values.parquet"
    values = [random.Random(seed).randint(-20, -1) for seed in range(12)]
    with duckdb.connect() as con:
        con.execute("create table data (value bigint)")
        con.executemany("insert into data values (?)", [(value,) for value in values])
        con.execute("copy data to ? (format parquet)", [str(source)])
    rules = [
        Check("same-name", "value > 0", "error"),
        Check("same-name", "true", "warn"),
        Check("same-name", "value > 0", "warn"),
    ]
    # Python callers can construct Contract directly, bypassing YAML validation.
    for ordering in itertools.permutations(rules):
        contract = Contract("d", "1.0.0", "owner", (Column("value", "int"),), checks=ordering)
        report = validate(contract, str(source))
        assert not report.ok
        assert [(r.violations, r.severity) for r in report.results if r.severity == "error"] == [(len(values), "error")]
