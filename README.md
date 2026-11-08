# Data contracts platform

[![ci](https://github.com/VasilenData/data-contracts-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/VasilenData/data-contracts-platform/actions/workflows/ci.yml)

A small tool for the promise a data producer makes to its consumers. You write the promise as a YAML contract, check a file against it, and compare two versions of the contract to see whether a change would break a consumer.

- **Validate**: schema, nulls, uniqueness, ranges, allowed values, regex, row count, freshness and cross-column SQL checks, all counted in one DuckDB scan, with tolerances and sample bad rows.
- **Diff**: classifies every change between two contract versions as breaking, review or safe, and fails if the version number was not bumped enough (semver).
- **CLI**: `validate` and `diff` with exit codes (0 pass, 1 violations or breaking, 2 bad input) so it can gate a pipeline.

## Result on real data

I wrote `contracts/yellow_taxi_v1.yaml` by profiling **January 2023** of the public NYC TLC yellow taxi files (3,066,766 rows), then ran it unchanged on all 12 months of 2023 (the January file is the only one the contract was fitted on, so the other 11 are out-of-sample).

![Violation rates by month against the contract tolerance](reports/drift.png)

| Month | Rows | Result | passenger_count, RatecodeID type | VendorID not 1 or 2 | Negative fare | Dropoff before pickup |
|---|---|---|---|---|---|---|
| 2023-01 | 3,066,766 | pass | ok | 0 | 0.817% | 0.0001% |
| 2023-02 | 2,913,955 | FAIL | double to int | 0.041% | 0.850% | 0.0095% |
| 2023-03 | 3,403,766 | FAIL | double to int | 0.057% | 0.872% | 0.0105% |
| 2023-04 | 3,288,250 | FAIL | double to int | 0.013% | 0.912% | 0.0028% |
| 2023-05 | 3,513,649 | FAIL | double to int | 0.012% | 0.914% | 0.0019% |
| 2023-06 | 3,307,234 | FAIL | double to int | 0.034% | 0.951% | 0.0057% |
| 2023-07 | 2,907,108 | FAIL | double to int | 0.025% | 1.072% | 0.0056% |
| 2023-08 | 2,824,209 | FAIL | double to int | 0.022% | 1.109% | 0.0052% |
| 2023-09 | 2,846,722 | FAIL | double to int | 0.030% | 1.039% | 0.0051% |
| 2023-10 | 3,522,285 | FAIL | double to int | 0.014% | 1.053% | 0.0024% |
| 2023-11 | 3,339,715 | FAIL | double to int | 0.014% | 1.104% | 0.0265% |
| 2023-12 | 3,376,567 | FAIL | double to int | 0.012% | 1.263% | 0.0020% |

What the real files showed:

- **Schema drift from February.** `passenger_count` and `RatecodeID` are stored as doubles in January and as integers in every later file. The contract pinned them as `float`, so the schema rule fails, which is the right call for a consumer that reads those columns as doubles. When a column fails its schema rule the validator skips that column's value rules, because comparing values of the wrong type would be noise.
- **A new vendor code.** `VendorID` 6 first appears in February (1,201 rows) and keeps appearing every month. The contract allows only 1 and 2 with no tolerance, so it fails every month after January.
- **Negative fares crept over the tolerance.** January has 0.817% negative fares and I set a 1% tolerance. From July the rate is above 1% in every month (1.04% to 1.26%).
- **Dropoff before pickup** is 3 rows in January (about 0.0001%) and 67 to 885 rows later, over the 0.001% tolerance every month.

The row counts for each rule match an independent hand profile of January (88 trips over 100 miles, 20 with more than 6 passengers, 25,049 negative fares, 13,106 `RatecodeID` values outside 1 to 6, 225 negative tips, 3 dropoffs before pickup). Raw results: `measurements/real_months.json`.

### Contract versions

`datacontracts diff` between the shipped examples:

```
$ datacontracts diff contracts/yellow_taxi_v1.yaml contracts/yellow_taxi_v2.yaml
  SAFE     column congestion_surcharge: added (float)
  SAFE     column airport_fee: added (float)
  SAFE     column fare_amount: max tightened: 500 -> 400
needs a minor bump; version ok

$ datacontracts diff contracts/yellow_taxi_v1.yaml contracts/yellow_taxi_v3_breaking.yaml
  BREAKING column tip_amount: removed
  BREAKING column passenger_count: type changed: float -> int
  BREAKING column passenger_count: max widened: 6 -> 9
  BREAKING column VendorID: now nullable (was NOT NULL)
  BREAKING column VendorID: new allowed values consumers may not handle: [6]
  BREAKING min_rows: lowered: 1000000 -> 500000
  BREAKING check total_covers_fare: removed
  BREAKING check pickup_in_2023: removed
needs a major bump; VERSION TOO LOW: breaking changes need a major version bump
```

CI runs both: v1 to v2 must pass and v1 to v3 must be rejected.

## Quick start

```bash
pip install .            # or: pip install -r requirements.txt and run with PYTHONPATH=src
datacontracts validate contracts/yellow_taxi_v1.yaml yellow_tripdata_2023-01.parquet
datacontracts validate contracts/yellow_taxi_v1.yaml data.csv --json
datacontracts diff old_contract.yaml new_contract.yaml
```

Get the real files (about 50 MB each, not stored in this repo):
`https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet` (change the month). To reproduce the table and chart: `python scripts/measure.py DIR` then `python scripts/plot_drift.py`.

A contract looks like this:

```yaml
dataset: orders
version: 1.2.0
owner: data-platform@example.com
min_rows: 1000
freshness: {column: created_at, max_age_hours: 24}
columns:
  - {name: order_id, type: int, nullable: false, unique: true}
  - {name: amount, type: float, min: 0, max: 100000, max_violation_rate: 0.001}
  - {name: status, type: string, allowed: [paid, refunded]}
checks:
  - {name: refund_not_above_amount, expr: "refund <= amount", severity: warn}
```

## How it works

- `spec.py` loads and validates the YAML (unknown types, duplicate columns, bad semver and a freshness column that is not in the contract are rejected).
- `validate.py` turns every rule into a SQL predicate that is TRUE for a violating row, and computes the row count and every violation count in a single `SELECT`. Nulls never trip a range, allowed-values or regex rule (use `nullable: false` for that), and a cross-column check that evaluates to NULL is not a violation.
- `diff.py` treats the contract as a promise. Weakening it is breaking (a removed column, a wider range, a new allowed value, a nullable column that was NOT NULL, a looser tolerance, a removed check). Strengthening it is safe. Regex and owner changes are flagged for review.

## Run it under Airflow

`airflow/dags/contract_check.py` validates the 12 monthly files, one mapped task per month, so a bad month fails alone and the other months still run. `src/datacontracts/orchestrate.py` holds the logic without any Airflow import, so it is unit tested directly.

- **Two kinds of failure, handled differently.** A missing file or I/O error is transient, so the task retries 3 times with exponential backoff. A contract violation means the data arrived and is wrong, so waiting cannot fix it: the task sends the alert and fails at once through `AirflowFailException`, with no retries.
- **The alert says what broke.** One message per failed file, listing each blocking rule with its violation count, rate and allowed rate. It is always appended to a log file (`DATACONTRACTS_ALERT_LOG`) and also posted to a Slack-style webhook if `DATACONTRACTS_WEBHOOK` is set. A broken webhook is reported, not raised, so it cannot hide the original failure.

Measured with `airflow dags test` on Airflow 2.10.5 (`measurements/airflow_runs.json`, `measurements/airflow_alerts_real_data.log`):

| Run | Result |
|---|---|
| Real 2023 files, contract v1 | January passed, the other 11 months failed, 11 alerts, 0 retries |
| Data folder missing (retry delay set to 1 s) | 36 retry attempts (12 tasks x 3), 12 tasks failed after retries, 12 alerts |

To run it: `pip install apache-airflow==2.10.5` with the official constraints file, then `PYTHONPATH=src DATACONTRACTS_DATA_DIR=<folder> airflow dags test contract_check 2026-11-08`. This is a local run for a demonstration. It is not deployed, the DAG has `schedule=None`, and I did not run a long-lived scheduler or webserver.

## Tests

77 tests (64 for the validator, diff and CLI, 12 for the orchestration helpers, and 1 DAG import and retry-policy test that runs in its own CI job with Airflow installed), run on every push with ruff. The validator tests inject an exact number of each violation into synthetic data and assert that exact count (for example 7 values below the minimum, 4 disallowed statuses, 2 duplicate ids). Boundary tests confirm that a value equal to a min or max, and a table exactly at the freshness limit, pass. I also broke the code on purpose to check that the tests notice: flipping `<` to `<=` on a bound, `>` to `>=` on the freshness limit, inverting the allowed-values test, dropping the duplicate count and zeroing the row-count shortfall each made at least one test fail. Two of those survived my first draft of the tests, and I added the boundary tests to kill them. The orchestration helpers got the same treatment with 7 mutants (inverting the warning filter, the truncation limit, the missing-file check, the pass check, the log write and the webhook error handling), and one survived until I added a test for exactly five failures.

## Decisions and trade-offs

- **DuckDB, not a Python row loop.** The rules are SQL predicates, so a few million rows validate in under a second and the engine reads parquet and CSV directly.
- **One scan for all rules, and where it helps.** On the 3.07M-row January parquet file, one query per rule was faster than my single scan (0.566 s against 0.78 s, median of 5 interleaved runs, a speedup of 0.73x, so a slowdown). On a 500,000-row CSV sample of the same file (median of 5 interleaved runs) it reversed: 0.275 s against 3.374 s, about 12x faster, because each separate query has to parse the CSV again. I kept the single scan because it gives consistent counts from one pass and it wins by a wide margin on CSV, and I did not claim it is faster in general.
- **Tolerances per rule.** Real data is never clean. A strict zero-violation default plus an explicit `max_violation_rate` makes the accepted mess visible in the contract instead of hidden in code.
- **Schema failure skips value rules for that column.** It avoids a flood of follow-on failures, at the cost that a drifted column is not value-checked until the contract or the source is fixed.
- **Contract diff is rule-based, not data-based.** It only needs the two YAML files, so it can run in a pull request without access to the data.

## Limits

- The contract was fitted on one month and judged on later months, so the failures show where January was not representative of the year, not that the TLC data is bad. The thresholds are my choices.
- Throughput is reported only as the comparison above. Per-month timings in `real_months.json` ran on a shared one-core sandbox and varied between about 0.2 s and 0.8 s for similar files, and months where a column fails its schema rule evaluate fewer rules, so they are not comparable with each other.
- Checks are SQL expressions, which are trusted input. Do not load contracts from untrusted sources.
- `diff` compares contract files only. It does not know which consumers read which column, so "breaking" means "could break a consumer", not "will".
- Types are coarse (string, int, float, bool, date, timestamp). There is no decimal precision or nested type support.
- Local files only. The Airflow DAG runs locally through `airflow dags test`. It is not deployed, not scheduled, and not wired to a warehouse or a catalog.

## Next steps

- Read a contract from a dbt model's columns and tests.
- Emit a GitHub Actions annotation per failed rule.
- Add a `profile` command that proposes a first contract and tolerances from a file.

MIT licensed. Data: NYC Taxi and Limousine Commission trip records (public).
