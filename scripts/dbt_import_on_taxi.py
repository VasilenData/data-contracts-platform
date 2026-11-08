"""Import the taxi project's real dbt `schema.yml`, then judge fct_trips for all 12 months of 2023.

The warehouse of the taxi project is not shipped, so fct_trips is rebuilt here in plain DuckDB from the
same SQL as the dbt models (stg_trips_classified -> stg_trips -> fct_trips, same rules and variables).
It is NOT produced by `dbt run`. The schema file is copied unchanged from nyc-taxi-data-platform.

Prediction written before the first run: the imported contract passes all 12 months, because stg_trips
only lets valid rows through; the two relationships tests are skipped; and injected defects are caught.

Usage: PYTHONPATH=src python scripts/dbt_import_on_taxi.py DIR ZONES_CSV
       (DIR holds yellow_tripdata_2023-MM.parquet, ZONES_CSV is taxi_zone_lookup.csv)
"""

import json
import sys
import tempfile
import time
from pathlib import Path

import duckdb

from datacontracts.dbt_import import dbt_contract_yaml, import_dbt_report
from datacontracts.spec import parse_contract
from datacontracts.validate import validate

data, zones = Path(sys.argv[1]), sys.argv[2]
root = Path(__file__).resolve().parents[1]
schema = root / "contracts" / "dbt_example" / "taxi_marts_schema.yml"

BUILD = """
COPY (
  WITH raw AS (
    SELECT *, '{month}' AS _source_month,
           md5(concat_ws('|', "VendorID", tpep_pickup_datetime, tpep_dropoff_datetime,
                         "PULocationID", "DOLocationID", trip_distance, fare_amount, total_amount)) AS trip_id
    FROM read_parquet('{src}')),
  ranked AS (SELECT *, row_number() OVER (PARTITION BY _source_month, trip_id) AS occurrence FROM raw),
  classified AS (
    SELECT trip_id, _source_month, tpep_pickup_datetime AS pickup_at, tpep_dropoff_datetime AS dropoff_at,
           passenger_count, trip_distance AS distance_miles, "PULocationID" AS pickup_zone_id,
           "DOLocationID" AS dropoff_zone_id, payment_type, fare_amount, tip_amount, total_amount,
      CASE
        WHEN tpep_pickup_datetime IS NULL OR tpep_dropoff_datetime IS NULL THEN 'missing_timestamp'
        WHEN tpep_dropoff_datetime <= tpep_pickup_datetime THEN 'non_positive_duration'
        WHEN date_diff('second', tpep_pickup_datetime, tpep_dropoff_datetime) >= 6 * 3600 THEN 'implausible_duration'
        WHEN strftime(tpep_pickup_datetime, '%Y-%m') <> _source_month THEN 'out_of_period'
        WHEN trip_distance IS NULL OR trip_distance <= 0 OR trip_distance > 200 THEN 'bad_distance'
        WHEN fare_amount IS NULL OR fare_amount < 0 OR total_amount IS NULL OR total_amount <= 0 THEN 'bad_fare'
        WHEN "PULocationID" IS NULL OR "PULocationID" NOT BETWEEN 1 AND 265
          OR "DOLocationID" IS NULL OR "DOLocationID" NOT BETWEEN 1 AND 265 THEN 'unknown_zone'
        WHEN occurrence > 1 THEN 'duplicate'
      END AS reject_reason
    FROM ranked),
  t AS (SELECT * EXCLUDE (reject_reason) FROM classified WHERE reject_reason IS NULL)
  SELECT
    t.trip_id::varchar AS trip_id, t._source_month::varchar AS source_month,
    t.pickup_at::timestamp AS pickup_at, t.dropoff_at::timestamp AS dropoff_at,
    date_trunc('day', t.pickup_at)::date AS pickup_date,
    extract('hour' FROM t.pickup_at)::integer AS pickup_hour, isodow(t.pickup_at)::integer AS pickup_dow,
    t.pickup_zone_id::integer AS pickup_zone_id, t.dropoff_zone_id::integer AS dropoff_zone_id,
    pz.Borough::varchar AS pickup_borough, pz.Zone::varchar AS pickup_zone, dz.Borough::varchar AS dropoff_borough,
    t.passenger_count::double AS passenger_count, t.distance_miles::double AS distance_miles,
    (date_diff('second', t.pickup_at, t.dropoff_at) / 60.0)::double AS duration_min,
    (t.distance_miles / (date_diff('second', t.pickup_at, t.dropoff_at) / 3600.0))::double AS avg_speed_mph,
    t.fare_amount::double AS fare_amount, t.tip_amount::double AS tip_amount, t.total_amount::double AS total_amount,
    CASE WHEN t.payment_type = 1 AND t.fare_amount > 0 THEN t.tip_amount / t.fare_amount END::double AS tip_pct
  FROM t
  LEFT JOIN read_csv('{zones}') pz ON pz.LocationID = t.pickup_zone_id
  LEFT JOIN read_csv('{zones}') dz ON dz.LocationID = t.dropoff_zone_id
) TO '{out}' (FORMAT PARQUET)
"""

report = import_dbt_report(str(schema), model="fct_trips", dataset="fct_trips")
contract = parse_contract(report.raw)
out = {
    "schema_file": "contracts/dbt_example/taxi_marts_schema.yml",
    "tests_found": report.found,
    "tests_imported": report.imported,
    "skipped_notes": report.notes,
    "contract_columns": len(contract.columns),
    "months": [],
}
(root / "measurements" / "dbt_import_taxi_fct_trips.yaml").write_text(
    dbt_contract_yaml(report.raw, "contracts/dbt_example/taxi_marts_schema.yml", "fct_trips", report.notes)
)
tmp = Path(tempfile.mkdtemp())
con = duckdb.connect()
for m in range(1, 13):
    month = f"2023-{m:02d}"
    path = tmp / f"fct_{month}.parquet"
    t0 = time.perf_counter()
    con.execute(BUILD.format(month=month, src=data / f"yellow_tripdata_{month}.parquet", zones=zones, out=path))
    built = time.perf_counter() - t0
    rep = validate(contract, str(path), con=con)
    out["months"].append(
        {
            "month": month,
            "rows": rep.rows,
            "rules": len(rep.results),
            "failed": sorted(r.rule for r in rep.failures),
            "build_seconds": round(built, 1),
            "validate_seconds": round(rep.seconds, 2),
        }
    )
    print(out["months"][-1], flush=True)

# Injected defects on January: the imported rules must notice each one.
jan = tmp / "fct_2023-01.parquet"
bad = tmp / "fct_bad.parquet"
con.execute(f"""COPY (
  SELECT * REPLACE (
    CASE WHEN rn <= 100 THEN NULL ELSE pickup_at END AS pickup_at,
    CASE WHEN rn BETWEEN 101 AND 150 THEN 25 ELSE pickup_hour END AS pickup_hour,
    CASE WHEN rn BETWEEN 151 AND 200 THEN (SELECT min(trip_id) FROM read_parquet('{jan}')) ELSE trip_id END AS trip_id,
    CASE WHEN rn BETWEEN 201 AND 250 THEN -1.0 ELSE distance_miles END AS distance_miles)
  FROM (SELECT *, row_number() OVER () AS rn FROM read_parquet('{jan}'))
) TO '{bad}' (FORMAT PARQUET)""")
rep = validate(contract, str(bad), con=con)
out["injected_defects"] = {
    "injected": {"null pickup_at": 100, "pickup_hour 25": 50, "duplicate trip_id": 50, "distance -1": 50},
    "rules_failed": {r.rule: r.violations for r in rep.failures},
}
(root / "measurements" / "dbt_import_on_taxi.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps({k: v for k, v in out.items() if k != "months"}, indent=2))
