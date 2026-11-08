import pytest

from datacontracts.spec import ContractError, parse_contract


def base():
    return {
        "dataset": "d",
        "version": "1.0.0",
        "owner": "o",
        "columns": [{"name": "a", "type": "int"}],
    }


def test_minimal_contract_parses():
    c = parse_contract(base())
    assert c.column("a").nullable is True
    assert c.version_tuple() == (1, 0, 0)


@pytest.mark.parametrize("key", ["dataset", "version", "owner", "columns"])
def test_missing_required_key(key):
    raw = base()
    del raw[key]
    with pytest.raises(ContractError, match=key):
        parse_contract(raw)


def test_bad_version_unknown_type_and_duplicates():
    raw = base()
    raw["version"] = "1.0"
    with pytest.raises(ContractError, match="MAJOR"):
        parse_contract(raw)
    raw = base()
    raw["columns"] = [{"name": "a", "type": "money"}]
    with pytest.raises(ContractError, match="unknown type"):
        parse_contract(raw)
    raw = base()
    raw["columns"] = [{"name": "a", "type": "int"}, {"name": "a", "type": "int"}]
    with pytest.raises(ContractError, match="duplicate"):
        parse_contract(raw)


def test_freshness_column_must_exist_and_severity_checked():
    raw = base()
    raw["freshness"] = {"column": "zzz", "max_age_hours": 1}
    with pytest.raises(ContractError, match="freshness column"):
        parse_contract(raw)
    raw = base()
    raw["checks"] = [{"name": "c", "expr": "a > 0", "severity": "fatal"}]
    with pytest.raises(ContractError, match="severity"):
        parse_contract(raw)


@pytest.mark.parametrize("value", [-0.1, 1.01, float("nan"), float("inf"), True, "invalid"])
@pytest.mark.parametrize("target", ["column", "check"])
def test_invalid_tolerances_cannot_disable_quality_gates(value, target):
    raw = base()
    if target == "column":
        raw["columns"][0]["max_violation_rate"] = value
    else:
        raw["checks"] = [{"name": "positive", "expr": "a >= 0", "max_violation_rate": value}]
    with pytest.raises(ContractError, match="max_violation_rate"):
        parse_contract(raw)


@pytest.mark.parametrize("name", ["nullable", "unique"])
def test_quoted_false_is_not_silently_true(name):
    raw = base()
    raw["columns"][0][name] = "false"
    with pytest.raises(ContractError, match=name):
        parse_contract(raw)


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_invalid_row_floors_rejected(value):
    raw = base()
    raw["min_rows"] = value
    with pytest.raises(ContractError, match="min_rows"):
        parse_contract(raw)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_invalid_freshness_limits_rejected(value):
    raw = base()
    raw["columns"][0]["type"] = "timestamp"
    raw["freshness"] = {"column": "a", "max_age_hours": value}
    with pytest.raises(ContractError, match="max_age_hours"):
        parse_contract(raw)
