import csv
import random

import duckdb
import pytest
import yaml

from datacontracts.__main__ import main
from datacontracts.profile import _flow, ceil_sig, contract_yaml, profile_contract, widen
from datacontracts.spec import ContractError, load_contract, parse_contract
from datacontracts.validate import validate


def write(tmp_path, ddl, rows, name="t.parquet"):
    path = tmp_path / name
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({ddl})")
    if rows and any(isinstance(v, list) for v in rows[0]):
        con.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in rows[0])})", rows)
    elif rows:  # bulk load: row-by-row inserts of 10,000 rows are slow
        csv_path = tmp_path / (name + ".csv")
        with open(csv_path, "w", newline="") as f:
            csv.writer(f).writerows([["" if v is None else v for v in r] for r in rows])
        con.execute(f"COPY t FROM '{csv_path}' (FORMAT CSV, HEADER false, NULL '')")
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    return str(path)


def cols(raw):
    return {c["name"]: c for c in raw["columns"]}


def test_ceil_sig_rounds_up_to_two_significant_digits():
    assert ceil_sig(0.0) == 0.0
    assert ceil_sig(0.0001) == 0.0001
    assert ceil_sig(0.00012345) == 0.00013
    assert ceil_sig(0.0234) == 0.024
    assert ceil_sig(0.5) == 0.5


def test_widen_goes_outward_on_a_tidy_step():
    assert widen(9.999, 9989.001, is_int=True) == (0, 9990)
    lo, hi = widen(0.0003, 0.97, is_int=False)
    assert lo <= 0.0003 and hi >= 0.97


def test_types_and_nullable(tmp_path):
    p = write(
        tmp_path,
        "a BIGINT, b VARCHAR, c DOUBLE, d BOOLEAN, e TIMESTAMP",
        [(1, "x", 1.5, True, "2024-01-01")] * 3 + [(2, None, 2.5, False, "2024-01-02")],
    )
    raw, _ = profile_contract(p)
    c = cols(raw)
    assert [c[k]["type"] for k in "abcde"] == ["int", "string", "float", "bool", "timestamp"]
    assert c["a"]["nullable"] is False and "nullable" not in c["b"]
    assert "min" not in c["d"] and "min" not in c["e"]


def test_unique_needs_100_rows_and_no_duplicates(tmp_path):
    ids = [(i, "k") for i in range(100)]
    assert cols(profile_contract(write(tmp_path, "a BIGINT, b VARCHAR", ids))[0])["a"]["unique"] is True
    few = [(i, "k") for i in range(99)]
    assert "unique" not in cols(profile_contract(write(tmp_path, "a BIGINT, b VARCHAR", few, "f.parquet"))[0])["a"]
    dup = [*ids[:-1], (0, "k")]
    assert "unique" not in cols(profile_contract(write(tmp_path, "a BIGINT, b VARCHAR", dup, "d.parquet"))[0])["a"]


def test_allowed_values_exclude_rare_ones_and_set_the_tolerance(tmp_path):
    rows = [("x",)] * 5000 + [("y",)] * 4999 + [("z",)]
    raw, _ = profile_contract(write(tmp_path, "s VARCHAR", rows), headroom=1.0)
    s = cols(raw)["s"]
    assert s["allowed"] == ["x", "y"]
    assert s["max_violation_rate"] == 0.0001


def test_common_values_are_all_kept(tmp_path):
    rows = [("x",)] * 500 + [("y",)] * 499 + [("z",)] * 1
    raw, _ = profile_contract(write(tmp_path, "s VARCHAR", rows))
    # z is 0.1% of rows, exactly at the threshold, so it is kept
    assert cols(raw)["s"]["allowed"] == ["x", "y", "z"]
    assert "max_violation_rate" not in cols(raw)["s"]


def test_high_cardinality_strings_get_no_allowed_list(tmp_path):
    rows = [(f"v{i % 13}",) for i in range(2000)]
    assert "allowed" not in cols(profile_contract(write(tmp_path, "s VARCHAR", rows))[0])["s"]
    rows = [(f"v{i % 12}",) for i in range(2000)]
    assert len(cols(profile_contract(write(tmp_path, "s VARCHAR", rows, "u.parquet"))[0])["s"]["allowed"]) == 12


def test_integer_valued_float_with_few_values_becomes_an_int_list(tmp_path):
    rows = [(1.0,), (2.0,), (None,)] * 400
    a = cols(profile_contract(write(tmp_path, "f DOUBLE", rows))[0])["f"]["allowed"]
    assert a == [1, 2] and all(type(v) is int for v in a)
    rows = [(1.5,), (2.0,)] * 400
    assert "allowed" not in cols(profile_contract(write(tmp_path, "f DOUBLE", rows, "n.parquet"))[0])["f"]


def test_numeric_bounds_come_from_percentiles_with_exact_tolerance(tmp_path):
    rows = [(i,) for i in range(10000)]
    raw, _ = profile_contract(write(tmp_path, "a BIGINT", rows), headroom=1.0)
    a = cols(raw)["a"]
    assert a["min"] == 0 and a["max"] == 9990
    assert a["max_violation_rate"] == 0.0009  # 9 values above 9990 out of 10000


def test_min_rows_is_half_the_profiled_rows(tmp_path):
    raw, _ = profile_contract(write(tmp_path, "a BIGINT", [(i,) for i in range(101)]))
    assert raw["min_rows"] == 50


def test_all_null_column_gets_type_and_nothing_else(tmp_path):
    raw, _ = profile_contract(write(tmp_path, "a BIGINT, b BIGINT", [(1, None)] * 10))
    assert cols(raw)["b"] == {"name": "b", "type": "int"}


def test_unsupported_types_are_skipped_with_a_note(tmp_path):
    raw, notes = profile_contract(write(tmp_path, "a BIGINT, n INTEGER[]", [(1, [1, 2])] * 5))
    assert [c["name"] for c in raw["columns"]] == ["a"]
    assert any("n" in x and "skipped" in x for x in notes)


def test_empty_file_is_rejected(tmp_path):
    with pytest.raises(ContractError, match="empty"):
        profile_contract(write(tmp_path, "a BIGINT", []))


def test_dataset_name_comes_from_the_file_name(tmp_path):
    p = write(tmp_path, "a BIGINT", [(1,)], "Yellow Trips-2023.01.parquet")
    assert profile_contract(p)[0]["dataset"] == "yellow_trips_2023_01"
    assert profile_contract(p, dataset="orders")[0]["dataset"] == "orders"


def test_yaml_text_round_trips_awkward_column_names(tmp_path):
    p = write(tmp_path, '"we ird: name, x" BIGINT, "q\'uote" VARCHAR', [(i, "a") for i in range(200)])
    raw, notes = profile_contract(p)
    text = contract_yaml(raw, p, 200, notes)
    assert yaml.safe_load(text) == raw
    assert text.startswith("# Proposed by `datacontracts profile`")
    parse_contract(raw)


@pytest.mark.parametrize("seed", range(5))
def test_every_proposed_contract_passes_on_its_own_file(tmp_path, seed):
    rnd = random.Random(seed)
    rows = [
        (
            i,
            rnd.choice(["a", "b", "c", "d"]) if rnd.random() > 0.002 else "rare",
            rnd.gauss(50, 20),
            None if rnd.random() < 0.1 else rnd.randint(1, 6) * 1.0,
            rnd.random() < 0.5,
        )
        for i in range(3000)
    ]
    p = write(tmp_path, "id BIGINT, s VARCHAR, x DOUBLE, k DOUBLE, flag BOOLEAN", rows)
    raw, _ = profile_contract(p)
    report = validate(parse_contract(raw), p)
    assert report.ok and not report.failures, [r.rule for r in report.failures]


def test_a_contract_from_one_file_flags_a_second_file_that_drifted(tmp_path):
    base = [(i % 100, "a") for i in range(5000)]
    p1 = write(tmp_path, "x BIGINT, s VARCHAR", base)
    raw, _ = profile_contract(p1)
    drifted = [(i % 100, "a") for i in range(4900)] + [(100000, "a")] * 100
    p2 = write(tmp_path, "x BIGINT, s VARCHAR", drifted, "d.parquet")
    report = validate(parse_contract(raw), p2)
    bad = {r.rule: r.violations for r in report.failures}
    assert bad == {"max:x": 100}


def test_cli_profile_prints_yaml_and_the_result_validates(tmp_path, capsys):
    p = write(tmp_path, "a BIGINT, b VARCHAR", [(i, "x") for i in range(200)])
    assert main(["profile", p]) == 0
    cap = capsys.readouterr()
    raw = yaml.safe_load(cap.out)
    assert raw["version"] == "0.1.0" and raw["owner"].startswith("TODO")
    assert "self-check passed" in cap.err
    out = tmp_path / "c.yaml"
    assert main(["profile", p, "--out", str(out), "--dataset", "orders", "--owner", "me@x.io"]) == 0
    c = load_contract(out)
    assert c.dataset == "orders" and c.owner == "me@x.io"
    assert main(["validate", str(out), p]) == 0


def test_cli_profile_on_missing_file_exits_2(tmp_path, capsys):
    assert main(["profile", str(tmp_path / "nope.parquet")]) == 2


def test_values_exactly_on_the_lower_bound_are_not_violations(tmp_path):
    rows = [(100 + i % 100,) for i in range(9997)] + [(50,)] * 3
    a = cols(profile_contract(write(tmp_path, "a BIGINT", rows), headroom=1.0)[0])["a"]
    assert (a["min"], a["max"]) == (100, 199)
    assert a["max_violation_rate"] == 0.0003  # only the three 50s, not the ~100 rows equal to 100


def test_small_floats_are_written_without_an_exponent():
    # PyYAML reads `2e-06` (no dot) as a string, so the writer must not produce it
    for v in (2e-06, 6.6e-06, 0.0001, 0.00054, 0.5, 3.0, 1e-15):
        assert yaml.safe_load(f"a: {_flow(v)}")["a"] == v
    assert _flow(3.0) == "3.0" and _flow(2e-06) == "0.000002" and _flow(7) == "7" and _flow(True) == "true"


def test_default_headroom_doubles_the_tolerance_and_still_passes(tmp_path):
    rows = [(i,) for i in range(10000)]
    p = write(tmp_path, "a BIGINT", rows)
    raw, notes = profile_contract(p)
    assert cols(raw)["a"]["max_violation_rate"] == 0.0018  # 0.0009 x 2
    assert any("headroom x2" in x for x in notes)
    assert validate(parse_contract(raw), p).ok


def test_headroom_does_not_loosen_a_rule_with_no_violations(tmp_path):
    raw, _ = profile_contract(write(tmp_path, "s VARCHAR", [("x",), ("y",)] * 500), headroom=5.0)
    assert "max_violation_rate" not in cols(raw)["s"]


def test_headroom_below_one_is_rejected(tmp_path):
    with pytest.raises(ContractError, match="headroom"):
        profile_contract(write(tmp_path, "a BIGINT", [(1,)]), headroom=0.5)


def test_cli_headroom_flag(tmp_path, capsys):
    p = write(tmp_path, "a BIGINT", [(i,) for i in range(10000)])
    assert main(["profile", p, "--headroom", "3"]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["columns"][0]["max_violation_rate"] == 0.0027


def test_a_column_with_an_allowed_list_gets_no_redundant_bounds(tmp_path):
    rows = [(1,), (2,), (3,)] * 400
    c = cols(profile_contract(write(tmp_path, "k BIGINT", rows))[0])["k"]
    assert c["allowed"] == [1, 2, 3] and "min" not in c and "max" not in c
    many = [(i % 50,) for i in range(2000)]  # too many distinct values for a list, so bounds stay
    c = cols(profile_contract(write(tmp_path, "k BIGINT", many, "m.parquet"))[0])["k"]
    assert "allowed" not in c and c["min"] == 0 and c["max"] == 49
