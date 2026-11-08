"""Command line: python -m datacontracts validate|diff|profile|dbt-import ..."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .annotations import github_annotations
from .dbt_import import dbt_contract_yaml, import_dbt_report
from .diff import bump_ok, diff_contracts, required_bump
from .profile import contract_yaml, profile_contract
from .spec import ContractError, load_contract, parse_contract
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


def _profile(args: argparse.Namespace) -> int:
    raw, notes = profile_contract(args.source, dataset=args.dataset, owner=args.owner, headroom=args.headroom)
    report = validate(parse_contract(raw), args.source)
    if not report.ok:  # a proposal that fails on its own file is a bug, not a result
        raise ContractError("self-check failed: " + ", ".join(r.rule for r in report.failures))
    text = contract_yaml(raw, args.source, report.rows, notes)
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text, end="")
    print(f"self-check passed: the proposal validates against {args.source} ({report.rows:,} rows)", file=sys.stderr)
    return 0


def _dbt_import(args: argparse.Namespace) -> int:
    r = import_dbt_report(args.schema, model=args.model, dataset=args.dataset, owner=args.owner, source=args.source)
    text = dbt_contract_yaml(r.raw, args.schema, r.model, r.notes)
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text, end="")
    print(
        f"imported {r.imported} of {r.found} tests, skipped {r.found - r.imported}; see the comments at the top",
        file=sys.stderr,
    )
    return 0


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
    pr = sub.add_parser("profile", help="propose a first contract from a parquet or csv file")
    pr.add_argument("source")
    pr.add_argument("--dataset", help="dataset name (default: from the file name)")
    pr.add_argument("--owner", default="TODO-owner@example.com")
    pr.add_argument(
        "--headroom", type=float, default=2.0, help="multiply each tolerance by this (default 2, minimum 1)"
    )
    pr.add_argument("--out", help="write the YAML here instead of stdout")
    db = sub.add_parser("dbt-import", help="draft a contract from a dbt model's schema.yml")
    db.add_argument("schema", help="path to a dbt schema.yml")
    db.add_argument("--model", help="model name (needed when the file has several)")
    db.add_argument("--dataset", help="dataset name (default: the model name)")
    db.add_argument("--owner", help="owner (default: the model's meta.owner, else a TODO placeholder)")
    db.add_argument("--source", help="parquet or csv file to read types from for columns without a data_type")
    db.add_argument("--out", help="write the YAML here instead of stdout")
    args = p.parse_args(argv)
    try:
        return {"validate": _validate, "diff": _diff, "profile": _profile, "dbt-import": _dbt_import}[args.cmd](args)
    except (ContractError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
