import copy

import pytest

from datacontracts.diff import BREAKING, REVIEW, SAFE, bump_ok, diff_contracts, required_bump
from datacontracts.spec import parse_contract

BASE = {
    "dataset": "orders",
    "version": "1.0.0",
    "owner": "team-a",
    "min_rows": 100,
    "freshness": {"column": "ts", "max_age_hours": 24},
    "columns": [
        {"name": "id", "type": "int", "nullable": False, "unique": True},
        {"name": "amount", "type": "float", "min": 0, "max": 1000},
        {"name": "status", "type": "string", "allowed": ["paid", "refunded"]},
        {"name": "ts", "type": "timestamp"},
    ],
    "checks": [{"name": "pos", "expr": "amount >= 0", "max_violation_rate": 0.01}],
}


def make(mutate=None, version="1.0.0"):
    raw = copy.deepcopy(BASE)
    raw["version"] = version
    if mutate:
        mutate(raw)
    return parse_contract(raw)


def levels(old, new):
    return {(c.level, c.where) for c in diff_contracts(old, new)}


def col(raw, name):
    return next(c for c in raw["columns"] if c["name"] == name)


def test_identical_contracts_have_no_changes():
    assert diff_contracts(make(), make()) == []


@pytest.mark.parametrize(
    "mutate,where",
    [
        (lambda r: r["columns"].pop(1), "column amount"),
        (lambda r: col(r, "amount").update(type="int"), "column amount"),
        (lambda r: col(r, "id").update(nullable=True), "column id"),
        (lambda r: col(r, "id").update(unique=False), "column id"),
        (lambda r: col(r, "amount").update(min=-5), "column amount"),
        (lambda r: col(r, "amount").update(max=5000), "column amount"),
        (lambda r: col(r, "amount").pop("max"), "column amount"),
        (lambda r: col(r, "status").update(allowed=["paid", "refunded", "lost"]), "column status"),
        (lambda r: col(r, "status").pop("allowed"), "column status"),
        (lambda r: col(r, "amount").update(max_violation_rate=0.05), "column amount"),
        (lambda r: r.update(min_rows=10), "min_rows"),
        (lambda r: r["freshness"].update(max_age_hours=48), "freshness"),
        (lambda r: r.pop("freshness"), "freshness"),
        (lambda r: r["checks"].clear(), "check pos"),
        (lambda r: r["checks"][0].update(max_violation_rate=0.5), "check pos"),
        (lambda r: r["checks"][0].update(severity="warn"), "check pos"),
    ],
)
def test_weakening_changes_are_breaking(mutate, where):
    assert (BREAKING, where) in levels(make(), make(mutate))


@pytest.mark.parametrize(
    "mutate,where",
    [
        (lambda r: r["columns"].append({"name": "note", "type": "string"}), "column note"),
        (lambda r: col(r, "amount").update(min=1), "column amount"),
        (lambda r: col(r, "amount").update(max=500), "column amount"),
        (lambda r: col(r, "status").update(allowed=["paid"]), "column status"),
        (lambda r: col(r, "ts").update(nullable=False), "column ts"),
        (lambda r: r.update(min_rows=500), "min_rows"),
        (lambda r: r["freshness"].update(max_age_hours=6), "freshness"),
        (lambda r: r["checks"].append({"name": "x", "expr": "id > 0"}), "check x"),
    ],
)
def test_strengthening_changes_are_safe(mutate, where):
    changes = levels(make(), make(mutate))
    assert (SAFE, where) in changes
    assert not any(level == BREAKING for level, _ in changes)


def test_review_items():
    new = make(lambda r: (r.update(owner="team-b"), col(r, "status").update(regex="^p")))
    got = levels(make(), new)
    assert (REVIEW, "owner") in got and (REVIEW, "column status") in got


def test_a_swap_of_allowed_values_reports_both_directions():
    new = make(lambda r: col(r, "status").update(allowed=["paid", "voided"]))
    msgs = {c.level: c.message for c in diff_contracts(make(), new)}
    assert "voided" in msgs[BREAKING] and "refunded" in msgs[SAFE]


def test_required_bump_levels():
    safe = diff_contracts(make(), make(lambda r: r["columns"].append({"name": "n", "type": "int"})))
    brk = diff_contracts(make(), make(lambda r: r["columns"].pop(1)))
    assert required_bump(safe) == "minor" and required_bump(brk) == "major"
    assert required_bump([]) == "none"


def test_semver_enforcement():
    mutate = lambda r: r["columns"].pop(1)  # noqa: E731
    old = make()
    for version, ok in [("1.0.0", False), ("1.1.0", False), ("1.0.1", False), ("2.0.0", True)]:
        new = make(mutate, version)
        assert bump_ok(old, new, diff_contracts(old, new))[0] is ok
    add = lambda r: r["columns"].append({"name": "n", "type": "int"})  # noqa: E731
    for version, ok in [("1.0.0", False), ("1.0.1", False), ("1.1.0", True), ("2.0.0", True)]:
        new = make(add, version)
        assert bump_ok(old, new, diff_contracts(old, new))[0] is ok


@pytest.mark.parametrize("version,accepted", [("1.0.0", False), ("1.1.0", False), ("2.0.0", True)])
def test_closed_schema_cannot_be_opened_without_major_bump(version, accepted):
    old = make(lambda r: r.update(allow_extra_columns=False))
    new = make(lambda r: r.update(allow_extra_columns=True), version=version)
    changes = diff_contracts(old, new)
    assert (BREAKING, "allow_extra_columns") in {(c.level, c.where) for c in changes}
    assert required_bump(changes) == "major"
    assert bump_ok(old, new, changes)[0] is accepted


@pytest.mark.parametrize("change", ["close_schema", "lower_check_tolerance", "promote_warning"])
def test_previously_omitted_strengthening_changes_need_minor_bump(change):
    old = make(lambda r: r["checks"][0].update(severity="warn"))

    def mutate(raw):
        raw["checks"][0]["severity"] = "warn"
        if change == "close_schema":
            raw["allow_extra_columns"] = False
        elif change == "lower_check_tolerance":
            raw["checks"][0]["max_violation_rate"] = 0
        else:
            raw["checks"][0]["severity"] = "error"

    for version, accepted in [("1.0.0", False), ("1.0.1", False), ("1.1.0", True)]:
        new = make(mutate, version=version)
        changes = diff_contracts(old, new)
        assert len(changes) == 1 and changes[0].level == SAFE
        assert required_bump(changes) == "minor"
        assert bump_ok(old, new, changes)[0] is accepted
        reverse = diff_contracts(new, old)
        assert len(reverse) == 1 and reverse[0].level == BREAKING


def test_new_extra_column_policy_agrees_with_validator_and_cli(tmp_path, capsys):
    import duckdb
    import yaml

    from datacontracts.__main__ import main
    from datacontracts.validate import validate

    raw = {
        "dataset": "orders",
        "owner": "test",
        "version": "1.0.0",
        "columns": [{"name": "id", "type": "int"}],
        "allow_extra_columns": False,
    }
    old = tmp_path / "old.yaml"
    new = tmp_path / "new.yaml"
    old.write_text(yaml.safe_dump(raw))
    new.write_text(yaml.safe_dump({**raw, "allow_extra_columns": True}))
    source = tmp_path / "orders.parquet"
    with duckdb.connect() as con:
        con.execute("copy (select 1 as id, 'new payload' as surprise) to ? (format parquet)", [str(source)])
    assert not validate(parse_contract(raw), str(source)).ok
    assert validate(parse_contract({**raw, "allow_extra_columns": True}), str(source)).ok
    assert main(["diff", str(old), str(new)]) == 1
    output = capsys.readouterr().out
    assert "BREAKING allow_extra_columns" in output and "VERSION TOO LOW" in output
