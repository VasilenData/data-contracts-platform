import json
from pathlib import Path

import duckdb

from datacontracts.__main__ import main

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def parquet(tmp_path, rows):
    path = tmp_path / "t.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t (a BIGINT, b VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?)", rows)
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    return str(path)


def write_contract(tmp_path, extra=""):
    p = tmp_path / "c.yaml"
    p.write_text(
        "dataset: t\nversion: 1.0.0\nowner: o\ncolumns:\n"
        "  - {name: a, type: int, nullable: false, min: 0}\n"
        "  - {name: b, type: string}\n" + extra
    )
    return str(p)


def test_validate_exit_codes(tmp_path, capsys):
    c = write_contract(tmp_path)
    assert main(["validate", c, parquet(tmp_path, [(1, "x"), (2, "y")])]) == 0
    assert "PASS" in capsys.readouterr().out
    assert main(["validate", c, parquet(tmp_path, [(1, "x"), (-2, "y")])]) == 1
    assert "FAIL min:a" in capsys.readouterr().out


def test_validate_json_output(tmp_path, capsys):
    c = write_contract(tmp_path)
    main(["validate", c, parquet(tmp_path, [(-1, "x")]), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["rows"] == 1
    assert any(r["rule"] == "min:a" and r["violations"] == 1 for r in out["results"])


def test_bad_contract_and_missing_file_exit_2(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("dataset: t\n")
    assert main(["validate", str(bad), "x.parquet"]) == 2
    assert "missing required key" in capsys.readouterr().err
    assert main(["validate", str(tmp_path / "nope.yaml"), "x.parquet"]) == 2


def test_diff_exit_codes_on_shipped_examples(capsys):
    v1, v2, v3 = (CONTRACTS / f"yellow_taxi_{n}.yaml" for n in ("v1", "v2", "v3_breaking"))
    assert main(["diff", str(v1), str(v2)]) == 0
    assert main(["diff", str(v1), str(v3)]) == 1
    out = capsys.readouterr().out
    assert "BREAKING column tip_amount: removed" in out and "VERSION TOO LOW" in out


def test_shipped_v1_contract_loads_and_diff_of_self_is_empty(capsys):
    v1 = str(CONTRACTS / "yellow_taxi_v1.yaml")
    assert main(["diff", v1, v1]) == 0
    assert "no changes" not in capsys.readouterr().err
