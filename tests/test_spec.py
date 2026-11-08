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
