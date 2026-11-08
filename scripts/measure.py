"""Validate real NYC TLC months against contract v1 and record what happens.

Usage: python scripts/measure.py DIR_WITH_PARQUET [--out measurements/real_months.json]
Files are yellow_tripdata_2023-MM.parquet from the public TLC trip record data.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from pathlib import Path

import duckdb

from datacontracts.spec import load_contract
from datacontracts.validate import _column_predicates, relation, validate

ROOT = Path(__file__).resolve().parents[1]


def best_of(fn, runs=3):
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t)
    return out, min(times), statistics.median(times)


def naive_run(contract, src):
    """One query per rule, the obvious way to write it."""
    con = duckdb.connect()
    rel = relation(src)
    preds = [p for c in contract.columns for _, p, _ in _column_predicates(c)]
    preds += [f"({k.expr}) IS FALSE" for k in contract.checks]
    for p in preds:
        con.execute(f"SELECT count(*) FROM {rel} WHERE {p}").fetchone()
    return len(preds)


def compare(contract, src, runs=5):
    """Interleave the two designs so cache and noise hit both equally."""
    single, naive = [], []
    for _ in range(runs):
        t = time.perf_counter()
        validate(contract, src, sample_size=0)
        single.append(time.perf_counter() - t)
        t = time.perf_counter()
        n_rules = naive_run(contract, src)
        naive.append(time.perf_counter() - t)
    return {
        "runs": runs,
        "single_scan_median_s": round(statistics.median(single), 3),
        "one_query_per_rule_median_s": round(statistics.median(naive), 3),
        "rules": n_rules,
        "speedup_of_single_scan": round(statistics.median(naive) / statistics.median(single), 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("--out", default=str(ROOT / "measurements" / "real_months.json"))
    a = ap.parse_args()
    contract = load_contract(ROOT / "contracts" / "yellow_taxi_v1.yaml")
    months = []
    for f in sorted(Path(a.data_dir).glob("yellow_tripdata_2023-*.parquet")):
        rep, best, med = best_of(lambda f=f: validate(contract, str(f), sample_size=0))
        months.append(
            {
                "month": f.stem[-7:],
                "rows": rep.rows,
                "best_seconds": round(best, 3),
                "median_seconds": round(med, 3),
                "rules_evaluated": len(rep.results),
                "rows_per_second": round(rep.rows / best),
                "ok": rep.ok,
                "failures": [r.as_dict() for r in rep.failures],
                "rates": {r.rule: round(r.rate, 6) for r in rep.results if r.violations},
            }
        )
        print(
            months[-1]["month"], rep.rows, f"{best:.2f}s", "PASS" if rep.ok else "FAIL", [r.rule for r in rep.failures]
        )
    jan = str(next(Path(a.data_dir).glob("yellow_tripdata_2023-01.parquet")))
    csv = "/tmp/jan_500k_sample.csv"
    duckdb.connect().execute(f"COPY (SELECT * FROM read_parquet('{jan}') USING SAMPLE 500000 ROWS) TO '{csv}' (HEADER)")
    designs = {"parquet_jan_3.07M_rows": compare(contract, jan), "csv_500k_row_sample_of_jan": compare(contract, csv)}
    out = {
        "contract": "contracts/yellow_taxi_v1.yaml",
        "contract_fitted_on": "2023-01",
        "machine": f"{platform.processor() or platform.machine()}, Python {platform.python_version()}, DuckDB {duckdb.__version__}",
        "months": months,
        "design_comparison": designs,
    }
    Path(a.out).write_text(json.dumps(out, indent=2))
    print(json.dumps(designs, indent=2))


if __name__ == "__main__":
    main()
