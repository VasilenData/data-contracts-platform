"""Validate a dataset against a contract. All rules run in one DuckDB scan."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from .spec import Column, Contract

_INT = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
}
_FLOAT = {"FLOAT", "DOUBLE", "REAL"}


def kind_of(duck_type: str) -> str:
    """Map a DuckDB type name to a contract type."""
    t = duck_type.upper()
    if t in _INT:
        return "int"
    if t in _FLOAT or t.startswith("DECIMAL"):
        return "float"
    if t.startswith("TIMESTAMP"):
        return "timestamp"
    return {"VARCHAR": "string", "BOOLEAN": "bool", "DATE": "date"}.get(t, t.lower())


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return repr(value)


def relation(source: str) -> str:
    """A DuckDB relation expression for a parquet or csv path (globs allowed)."""
    if source.endswith(".parquet") or "*.parquet" in source:
        return f"read_parquet({lit(source)})"
    return f"read_csv_auto({lit(source)})"


@dataclass
class RuleResult:
    rule: str
    severity: str
    violations: int
    rows: int
    allowed_rate: float = 0.0
    detail: str = ""
    samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.violations / self.rows if self.rows else 0.0

    @property
    def passed(self) -> bool:
        return self.rate <= self.allowed_rate and (self.violations == 0 or self.allowed_rate > 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "violations": self.violations,
            "rate": round(self.rate, 6),
            "allowed_rate": self.allowed_rate,
            "passed": self.passed,
            "detail": self.detail,
            "samples": self.samples,
        }


@dataclass
class Report:
    dataset: str
    version: str
    rows: int
    seconds: float
    results: list[RuleResult]

    @property
    def ok(self) -> bool:
        return all(r.passed or r.severity == "warn" for r in self.results)

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "version": self.version,
            "rows": self.rows,
            "seconds": round(self.seconds, 3),
            "ok": self.ok,
            "results": [r.as_dict() for r in self.results],
        }


def _column_predicates(c: Column) -> list[tuple[str, str, str]]:
    """(rule id, SQL predicate that is TRUE for a violating row, detail)."""
    col = q(c.name)
    out: list[tuple[str, str, str]] = []
    if not c.nullable:
        out.append((f"not_null:{c.name}", f"{col} IS NULL", "null in a non-nullable column"))
    if c.min is not None:
        out.append((f"min:{c.name}", f"{col} < {lit(c.min)}", f"value below {c.min}"))
    if c.max is not None:
        out.append((f"max:{c.name}", f"{col} > {lit(c.max)}", f"value above {c.max}"))
    if c.allowed is not None:
        values = ", ".join(lit(v) for v in c.allowed)
        predicate = f"{col} IS NOT NULL" + (f" AND {col} NOT IN ({values})" if c.allowed else "")
        out.append((f"allowed:{c.name}", predicate, "value not allowed"))
    if c.regex is not None:
        out.append(
            (
                f"regex:{c.name}",
                f"{col} IS NOT NULL AND NOT regexp_matches(CAST({col} AS VARCHAR), {lit(c.regex)})",
                f"does not match {c.regex}",
            )
        )
    return out


def validate(
    contract: Contract,
    source: str,
    *,
    now: datetime | None = None,
    sample_size: int = 3,
    con: duckdb.DuckDBPyConnection | None = None,
) -> Report:
    if con is None:
        with duckdb.connect() as owned:
            return validate(contract, source, now=now, sample_size=sample_size, con=owned)
    start = time.perf_counter()
    rel = relation(source)
    described = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()}
    results: list[RuleResult] = []
    present: list[Column] = []

    for c in contract.columns:
        if c.name not in described:
            results.append(RuleResult(f"schema:{c.name}", "error", 1, 1, 0.0, "column is missing"))
            continue
        actual = kind_of(described[c.name])
        if actual != c.type:
            results.append(
                RuleResult(
                    f"schema:{c.name}",
                    "error",
                    1,
                    1,
                    0.0,
                    f"expected {c.type}, found {described[c.name]}",
                )
            )
            continue
        present.append(c)
    if not contract.allow_extra_columns:
        known = {c.name for c in contract.columns}
        extra = sorted(set(described) - known)
        if extra:
            results.append(RuleResult("extra_columns", "error", len(extra), len(described), 0.0, ", ".join(extra)))

    # One scan computes the row count and every violation count.
    selects = ["count(*) AS n"]
    plan: list[tuple[str, str, str, float, str]] = []  # rule, alias, detail, allowed, predicate
    for c in present:
        for rule, pred, detail in _column_predicates(c):
            plan.append((rule, f"v{len(plan)}", detail, c.max_violation_rate, pred))
        if c.unique:
            alias = f"v{len(plan)}"
            selects.append(f"count({q(c.name)}) - count(DISTINCT {q(c.name)}) AS {alias}")
            plan.append((f"unique:{c.name}", alias, "duplicate values", c.max_violation_rate, ""))
    for chk in contract.checks:
        plan.append((f"check:{chk.name}", f"v{len(plan)}", chk.expr, chk.max_violation_rate, f"({chk.expr}) IS FALSE"))
    for _, alias, _, _, pred in plan:
        if pred:
            selects.append(f"coalesce(sum(CASE WHEN {pred} THEN 1 ELSE 0 END), 0) AS {alias}")
    freshness_ready = contract.freshness and any(c.name == contract.freshness.column for c in present)
    if freshness_ready:
        selects.append(f"max({q(contract.freshness.column)}) AS newest")
    cur = con.execute(f"SELECT {', '.join(selects)} FROM {rel}")
    row = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
    n = int(row["n"])

    sev = {f"check:{c.name}": c.severity for c in contract.checks}
    for rule, alias, detail, allowed, pred in plan:
        res = RuleResult(rule, sev.get(rule, "error"), int(row[alias]), n, allowed, detail)
        if res.violations and pred and sample_size:
            res.samples = _samples(con, rel, pred, sample_size)
        results.append(res)

    if contract.min_rows:
        short = max(0, contract.min_rows - n)
        results.append(
            RuleResult(
                "min_rows", "error", short, max(n, 1), 0.0, f"expected at least {contract.min_rows} rows, found {n}"
            )
        )
    if freshness_ready:
        newest = row["newest"]
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc).replace(tzinfo=None)
        if newest is None:
            results.append(RuleResult("freshness", "error", 1, 1, 0.0, "no timestamps to measure"))
        else:
            if isinstance(newest, date) and not isinstance(newest, datetime):
                newest = datetime.combine(newest, datetime.min.time())
            if getattr(newest, "tzinfo", None) is not None:
                newest = newest.astimezone(timezone.utc).replace(tzinfo=None)
            age = (now - newest).total_seconds() / 3600
            stale = age > contract.freshness.max_age_hours
            results.append(
                RuleResult(
                    "freshness",
                    "error",
                    int(stale),
                    1,
                    0.0,
                    f"newest {contract.freshness.column} is {age:.1f} h old, limit {contract.freshness.max_age_hours} h",
                )
            )
    return Report(contract.dataset, contract.version, n, time.perf_counter() - start, results)


def _samples(con: duckdb.DuckDBPyConnection, rel: str, pred: str, k: int) -> list[dict[str, Any]]:
    cur = con.execute(f"SELECT * FROM {rel} WHERE {pred} LIMIT {int(k)}")
    cols = [d[0] for d in cur.description]
    return [{c: str(v) for c, v in zip(cols, r, strict=True)} for r in cur.fetchall()]


def source_exists(source: str) -> bool:
    return Path(source).exists() or "*" in source
