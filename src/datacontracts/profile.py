"""Propose a first contract from a file.

The proposal is a starting point to review, not a finished contract. Bounds come from the
0.1st and 99.9th percentiles, and each column's tolerance is the exact share of profiled rows
outside the proposed rules, rounded up. So the proposal passes on the file it was made from.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import duckdb

from .spec import TYPES, ContractError
from .validate import kind_of, q, relation, source_exists

MAX_ALLOWED_VALUES = 12  # a column with more distinct values gets no allowed list
RARE_SHARE = 0.001  # values below this share of rows are left out of an allowed list
LOW_QUANTILE, HIGH_QUANTILE = 0.001, 0.999
DEFAULT_HEADROOM = (
    2.0  # tolerance = in-sample rate x this. 1.0 left no margin and flagged ordinary month-to-month noise
)
UNIQUE_MIN_ROWS = 100  # a file smaller than this proves nothing about uniqueness


def ceil_sig(x: float, digits: int = 2) -> float:
    """Round up to a number of significant digits, so a tolerance never undershoots the real rate."""
    if x <= 0:
        return 0.0
    step = 10.0 ** (math.floor(math.log10(x)) - (digits - 1))
    out = round(math.ceil(x / step - 1e-9) * step, 15)
    return out if out >= x else math.nextafter(out, math.inf)


def _tidy(v: float) -> float | int:
    v = round(v, 12)
    return int(v) if float(v).is_integer() else v


def widen(lo: float, hi: float, *, is_int: bool) -> tuple[float | int, float | int]:
    """Move the bounds outward onto a step that is a hundredth of the larger magnitude."""
    scale = max(abs(lo), abs(hi), 1e-9)
    step = 10.0 ** (math.floor(math.log10(scale)) - 2)
    if is_int:
        step = max(step, 1.0)
    return _tidy(math.floor(lo / step) * step), _tidy(math.ceil(hi / step) * step)


def _dataset_name(source: str) -> str:
    stem = Path(source.replace("*", "")).stem or "dataset"
    name = re.sub(r"[^0-9a-zA-Z]+", "_", stem).strip("_").lower()
    return name or "dataset"


def profile_contract(
    source: str,
    *,
    dataset: str | None = None,
    owner: str = "TODO-owner@example.com",
    headroom: float = DEFAULT_HEADROOM,
    con: duckdb.DuckDBPyConnection | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return (contract as a plain dict that parse_contract accepts, notes for the reader)."""
    if headroom < 1:
        raise ContractError("headroom must be at least 1, or the proposal would fail on its own file")
    if not source_exists(source):
        raise FileNotFoundError(f"no such file: {source}")
    con = con or duckdb.connect()
    rel = relation(source)
    described = con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
    notes: list[str] = []
    usable: list[tuple[str, str, str]] = []  # name, kind, duckdb type
    for name, duck, *_ in described:
        kind = kind_of(duck)
        if kind in TYPES:
            usable.append((name, kind, duck))
        else:
            notes.append(f"column {name} ({duck}) skipped: nested or unsupported type")

    selects = ["count(*) AS n"]
    for i, (name, kind, _) in enumerate(usable):
        selects += [f"count({q(name)}) AS nn{i}", f"count(DISTINCT {q(name)}) AS d{i}"]
        if kind in ("int", "float"):
            selects += [
                f"quantile_cont({q(name)}, {LOW_QUANTILE}) AS lo{i}",
                f"quantile_cont({q(name)}, {HIGH_QUANTILE}) AS hi{i}",
            ]
    cur = con.execute(f"SELECT {', '.join(selects)} FROM {rel}")
    st = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
    n = int(st["n"])
    if n == 0:
        raise ContractError("cannot profile an empty file")

    columns: list[dict[str, Any]] = []
    bound_cols: list[tuple[int, str, float | int, float | int]] = []
    for i, (name, kind, _) in enumerate(usable):
        nn, distinct = int(st[f"nn{i}"]), int(st[f"d{i}"])
        col: dict[str, Any] = {"name": name, "type": kind}
        if nn == n:
            col["nullable"] = False
        if kind in ("int", "string") and distinct == n and n >= UNIQUE_MIN_ROWS:
            col["unique"] = True
        rates: list[float] = []
        has_bounds = False
        if kind in ("int", "float") and nn > 0 and st[f"lo{i}"] is not None:
            lo, hi = widen(float(st[f"lo{i}"]), float(st[f"hi{i}"]), is_int=kind == "int")
            col["min"], col["max"] = lo, hi
            has_bounds = True
        if kind in ("int", "float", "string") and 0 < distinct <= MAX_ALLOWED_VALUES and not col.get("unique"):
            values = con.execute(
                f"SELECT {q(name)}, count(*) FROM {rel} WHERE {q(name)} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1"
            ).fetchall()
            if kind == "float" and not all(float(v).is_integer() for v, _ in values):
                values = []
            if values:
                keep = [v for v, c in values if c / n >= RARE_SHARE]
                dropped = sum(c for v, c in values if v not in keep)
                if keep:
                    col["allowed"] = sorted((int(v) if kind != "string" else v) for v in keep)
                    rates.append(dropped / n)
                    if (
                        has_bounds
                    ):  # an allowed list already implies the range, and two rules would report one problem twice
                        del col["min"], col["max"]
                        has_bounds = False
        if has_bounds:
            bound_cols.append((i, name, col["min"], col["max"]))
        col["_rates"] = rates
        columns.append(col)

    if bound_cols:
        parts = []
        for i, name, lo, hi in bound_cols:
            parts.append(f"coalesce(sum(CASE WHEN {q(name)} < {lo} THEN 1 ELSE 0 END), 0) AS below{i}")
            parts.append(f"coalesce(sum(CASE WHEN {q(name)} > {hi} THEN 1 ELSE 0 END), 0) AS above{i}")
        cur = con.execute(f"SELECT {', '.join(parts)} FROM {rel}")
        vio = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
        by_name = {c["name"]: c for c in columns}
        for i, name, _, _ in bound_cols:
            by_name[name]["_rates"] += [int(vio[f"below{i}"]) / n, int(vio[f"above{i}"]) / n]

    for col in columns:
        rate = ceil_sig(max(col.pop("_rates"), default=0.0) * headroom)
        if rate > 0:
            col["max_violation_rate"] = rate
            notes.append(
                f"column {col['name']}: tolerates {rate:.4%} of rows outside the proposed rules (headroom x{headroom:g})"
            )

    notes.append("freshness, regex and cross-column checks are not proposed: they depend on how the data is produced")
    raw = {
        "dataset": dataset or _dataset_name(source),
        "version": "0.1.0",
        "owner": owner,
        "min_rows": n // 2,
        "allow_extra_columns": True,
        "columns": columns,
    }
    return raw, notes


def _flow(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.15f}".rstrip("0")  # no exponent: YAML 1.1 reads 2e-06 as a string
        return text + "0" if text.endswith(".") else text
    if isinstance(value, list):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)  # JSON strings are valid YAML


def contract_yaml(raw: dict[str, Any], source: str, rows: int, notes: list[str]) -> str:
    """Render the proposal in the same flow style as the hand-written contracts."""
    out = [
        "# Proposed by `datacontracts profile`. Review every rule before you rely on it.",
        f"# Profiled {rows:,} rows from {source}. Bounds are the 0.1st and 99.9th percentiles, widened outward.",
    ]
    out += [f"# - {x}" for x in notes]
    out += [f"{k}: {_flow(raw[k])}" for k in ("dataset", "version", "owner", "min_rows", "allow_extra_columns")]
    out.append("columns:")
    for c in raw["columns"]:
        out.append("  - {" + ", ".join(f"{k}: {_flow(v)}" for k, v in c.items()) + "}")
    return "\n".join(out) + "\n"
