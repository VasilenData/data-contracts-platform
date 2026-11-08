"""Fit contracts to January with `profile`, judge all 12 months, and compare with the hand-written v1.

Two headroom settings are reported: 1 (tolerance equals January's own rate) and 2 (the default).

Usage: PYTHONPATH=src python scripts/profile_vs_handwritten.py DIR   (DIR holds yellow_tripdata_2023-MM.parquet)
"""

import json
import sys
import time
from pathlib import Path

from datacontracts.profile import profile_contract
from datacontracts.spec import load_contract, parse_contract
from datacontracts.validate import validate

data = Path(sys.argv[1])
jan = str(data / "yellow_tripdata_2023-01.parquet")
hand = load_contract(Path(__file__).resolve().parents[1] / "contracts" / "yellow_taxi_v1.yaml")
months = [str(data / f"yellow_tripdata_2023-{m:02d}.parquet") for m in range(1, 13)]


def failed(contract, src):
    return sorted(r.rule for r in validate(contract, src).failures if r.severity != "warn")


hand_failed = [failed(hand, s) for s in months]
out = {"hand_written": {"rules": len(validate(hand, jan).results), "failed_per_month": [len(f) for f in hand_failed]}}
for headroom in (1.0, 2.0):
    t = time.perf_counter()
    raw, _ = profile_contract(jan, dataset="nyc_yellow_taxi_trips", headroom=headroom)
    seconds = time.perf_counter() - t
    auto = parse_contract(raw)
    auto_failed = [failed(auto, s) for s in months]
    both = [sorted(set(a) & set(h)) for a, h in zip(auto_failed, hand_failed, strict=True)]
    out[f"headroom_{headroom:g}"] = {
        "profile_seconds": round(seconds, 2),
        "rules": len(validate(auto, jan).results),
        "failed_per_month": [len(f) for f in auto_failed],
        "months_failing_after_january": sum(1 for f in auto_failed[1:] if f),
        "rules_failing_in_both_per_month": [len(b) for b in both],
        "rules_only_in_auto": sorted({r for f in auto_failed for r in f} - {r for f in hand_failed for r in f}),
        "rules_only_in_hand": sorted({r for f in hand_failed for r in f} - {r for f in auto_failed for r in f}),
        "january_passes": not auto_failed[0],
    }
print(json.dumps(out, indent=1))
