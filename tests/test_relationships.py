"""Cross-file relationships: known-answer counts, null handling, errors, CLI."""

import pandas as pd
import pytest

from datacontracts.__main__ import main
from datacontracts.spec import ContractError, parse_contract
from datacontracts.validate import validate


def contract(**rel):
    rule = {"name": "zone_exists", "column": "zone", "to": "zones", "to_column": "id"} | rel
    return parse_contract(
        {
            "dataset": "trips",
            "version": "1.0.0",
            "owner": "o",
            "columns": [{"name": "zone", "type": "int"}],
            "relationships": [rule],
        }
    )


@pytest.fixture
def files(tmp_path):
    child = tmp_path / "trips.csv"
    parent = tmp_path / "zones.csv"
    pd.DataFrame({"zone": pd.array([1, 2, 3, 3, 99, 98, None], dtype="Int64")}).to_csv(child, index=False)
    pd.DataFrame({"id": [1, 2, 3, 4]}).to_csv(parent, index=False)
    return str(child), str(parent)


def result(report):
    return next(r for r in report.results if r.rule == "relationship:zone_exists")


def test_counts_rows_without_a_match_and_ignores_null_child(files):
    child, parent = files
    r = result(validate(contract(), child, refs={"zones": parent}))
    assert r.violations == 2  # 99 and 98; the null child value is not a violation
    assert r.rows == 7
    assert not r.passed
    assert sorted(s["zone"] for s in r.samples) == ["98", "99"]


def test_duplicate_matching_parent_rows_do_not_multiply_counts(tmp_path, files):
    child, _ = files
    parent = tmp_path / "dup.csv"
    pd.DataFrame({"id": [1, 1, 1, 2, 2, 3]}).to_csv(parent, index=False)
    assert result(validate(contract(), child, refs={"zones": str(parent)})).violations == 2


def test_null_in_reference_column_does_not_hide_violations(tmp_path, files):
    child, _ = files
    parent = tmp_path / "nulls.csv"
    pd.DataFrame({"id": pd.array([1, 2, 3, None], dtype="Int64")}).to_csv(parent, index=False)
    assert result(validate(contract(), child, refs={"zones": str(parent)})).violations == 2


def test_all_values_present_passes(tmp_path):
    child, parent = tmp_path / "c.csv", tmp_path / "p.csv"
    pd.DataFrame({"zone": [1, 2]}).to_csv(child, index=False)
    pd.DataFrame({"id": [1, 2, 3]}).to_csv(parent, index=False)
    report = validate(contract(), str(child), refs={"zones": str(parent)})
    assert result(report).passed and report.ok


def test_tolerance_allows_a_small_rate(files):
    child, parent = files
    assert result(validate(contract(max_violation_rate=0.5), child, refs={"zones": parent})).passed


def test_missing_reference_name_is_a_contract_error(files):
    child, _ = files
    with pytest.raises(ContractError, match="--ref NAME=PATH for zones"):
        validate(contract(), child)


def test_missing_reference_column_is_a_failed_rule(files):
    child, parent = files
    r = result(validate(contract(to_column="nope"), child, refs={"zones": parent}))
    assert not r.passed and "nope" in r.detail


def test_spec_rejects_bad_relationships():
    base = {"dataset": "t", "version": "1.0.0", "owner": "o", "columns": [{"name": "a", "type": "int"}]}
    rule = {"name": "r", "column": "a", "to": "x", "to_column": "id"}
    with pytest.raises(ContractError, match="not in the contract"):
        parse_contract(base | {"relationships": [rule | {"column": "b"}]})
    with pytest.raises(ContractError, match="non-empty string to_column"):
        parse_contract(base | {"relationships": [{k: v for k, v in rule.items() if k != "to_column"}]})
    with pytest.raises(ContractError, match="duplicate relationship"):
        parse_contract(base | {"relationships": [rule, rule]})


def write_contract(path):
    path.write_text(
        "dataset: trips\nversion: 1.0.0\nowner: o\ncolumns:\n  - {name: zone, type: int}\n"
        "relationships:\n  - {name: zone_exists, column: zone, to: zones, to_column: id}\n"
    )
    return str(path)


def test_cli_ref_flag(tmp_path, files, capsys):
    child, parent = files
    c = write_contract(tmp_path / "c.yaml")
    assert main(["validate", c, child, "--ref", f"zones={parent}"]) == 1
    assert "FAIL relationship:zone_exists: 2 violations" in capsys.readouterr().out
    assert main(["validate", c, child]) == 2
    assert "--ref NAME=PATH" in capsys.readouterr().err
    assert main(["validate", c, child, "--ref", "zones"]) == 2
