"""Command line: python -m datacontracts validate|diff ..."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .annotations import github_annotations
from .diff import bump_ok, diff_contracts, required_bump
from .spec import ContractError, load_contract
from .validate import validate


def _validate(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    now = datetime.fromisoformat(args.now) if args.now else None
    report = validate(contract, args.source, now=now)
    if args.json or args.format == "json":
        print(json.dumps(report.as_dict(), indent=2))
    else:
        if args.format == "github":
            text = Path(args.contract).read_text()
            for line in github_annotations(report, args.contract, text):
                print(line)
        print(f"{report.dataset} v{report.version}: {report.rows:,} rows in {report.seconds:.2f}s")
        for r in report.results:
            mark = "ok  " if r.passed else ("WARN" if r.severity == "warn" else "FAIL")
            print(
                f"  {mark} {r.rule}: {r.violations:,} violations ({r.rate:.4%}) {r.detail if not r.passed else ''}".rstrip()
            )
        print("PASS" if report.ok else "FAIL")
    return 0 if report.ok else 1


def _diff(args: argparse.Namespace) -> int:
    old, new = load_contract(args.old), load_contract(args.new)
    changes = diff_contracts(old, new)
    ok, why = bump_ok(old, new, changes)
    print(f"{old.dataset}: {old.version} -> {new.version}")
    for c in sorted(changes, key=lambda c: ("breaking", "review", "safe").index(c.level)):
        print(f"  {c.level.upper():8} {c.where}: {c.message}")
    print(f"needs a {required_bump(changes)} bump; {'version ok' if ok else 'VERSION TOO LOW: ' + why}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="datacontracts")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="check a parquet or csv file against a contract")
    v.add_argument("contract")
    v.add_argument("source")
    v.add_argument("--json", action="store_true", help="same as --format json")
    v.add_argument(
        "--format",
        choices=["text", "json", "github"],
        default="text",
        help="github prints ::error annotations per failed rule, then the text summary",
    )
    v.add_argument("--now", help="ISO time to measure freshness against")
    d = sub.add_parser("diff", help="classify changes between two contract versions")
    d.add_argument("old")
    d.add_argument("new")
    args = p.parse_args(argv)
    try:
        return _validate(args) if args.cmd == "validate" else _diff(args)
    except (ContractError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
