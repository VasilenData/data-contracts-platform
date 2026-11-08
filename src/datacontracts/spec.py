"""Contract model and YAML loader."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TYPES = {"string", "int", "float", "bool", "date", "timestamp"}
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ContractError(ValueError):
    """The contract file itself is invalid."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True
    unique: bool = False
    min: float | None = None
    max: float | None = None
    allowed: tuple[Any, ...] | None = None
    regex: str | None = None
    max_violation_rate: float = 0.0


@dataclass(frozen=True)
class Check:
    name: str
    expr: str
    severity: str = "error"
    max_violation_rate: float = 0.0


@dataclass(frozen=True)
class Freshness:
    column: str
    max_age_hours: float


@dataclass(frozen=True)
class Contract:
    dataset: str
    version: str
    owner: str
    columns: tuple[Column, ...]
    min_rows: int = 0
    freshness: Freshness | None = None
    checks: tuple[Check, ...] = field(default_factory=tuple)
    allow_extra_columns: bool = True

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def version_tuple(self) -> tuple[int, int, int]:
        m = SEMVER.match(self.version)
        return int(m[1]), int(m[2]), int(m[3])  # type: ignore[index]


def _column(raw: dict[str, Any]) -> Column:
    if "name" not in raw or "type" not in raw:
        raise ContractError(f"column needs name and type: {raw}")
    if raw["type"] not in TYPES:
        raise ContractError(f"column {raw['name']}: unknown type {raw['type']!r}")
    allowed = raw.get("allowed")
    return Column(
        name=raw["name"],
        type=raw["type"],
        nullable=_boolean(raw.get("nullable", True), "nullable"),
        unique=_boolean(raw.get("unique", False), "unique"),
        min=raw.get("min"),
        max=raw.get("max"),
        allowed=tuple(allowed) if allowed is not None else None,
        regex=raw.get("regex"),
        max_violation_rate=_number(raw.get("max_violation_rate", 0.0), "max_violation_rate", maximum=1),
    )


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be a YAML boolean, not {value!r}")
    return value


def _number(value: Any, name: str, maximum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name} must be a finite non-negative number") from exc
    if isinstance(value, bool) or not math.isfinite(result) or result < 0 or (maximum is not None and result > maximum):
        raise ContractError(f"{name} must be finite and between 0 and {maximum if maximum is not None else 'infinity'}")
    return result


def parse_contract(raw: dict[str, Any]) -> Contract:
    for key in ("dataset", "version", "owner", "columns"):
        if key not in raw:
            raise ContractError(f"missing required key: {key}")
    if not SEMVER.fullmatch(str(raw["version"])):
        raise ContractError(f"version must be MAJOR.MINOR.PATCH, got {raw['version']!r}")
    columns = tuple(_column(c) for c in raw["columns"])
    names = [c.name for c in columns]
    if len(set(names)) != len(names):
        raise ContractError("duplicate column names")
    fresh = raw.get("freshness")
    freshness = None
    if fresh:
        if fresh["column"] not in names:
            raise ContractError(f"freshness column {fresh['column']!r} is not in the contract")
        if next(c for c in columns if c.name == fresh["column"]).type not in {"date", "timestamp"}:
            raise ContractError("freshness column must have type date or timestamp")
        freshness = Freshness(fresh["column"], _number(fresh["max_age_hours"], "max_age_hours"))
    checks = []
    for c in raw.get("checks", []):
        severity = c.get("severity", "error")
        if severity not in {"error", "warn"}:
            raise ContractError(f"check {c.get('name')}: severity must be error or warn")
        checks.append(Check(c["name"], c["expr"], severity,
                            _number(c.get("max_violation_rate", 0.0), "max_violation_rate", maximum=1)))
    min_rows = raw.get("min_rows", 0)
    if isinstance(min_rows, bool) or not isinstance(min_rows, int) or min_rows < 0:
        raise ContractError("min_rows must be a non-negative integer")
    return Contract(
        dataset=raw["dataset"],
        version=str(raw["version"]),
        owner=raw["owner"],
        columns=columns,
        min_rows=min_rows,
        freshness=freshness,
        checks=tuple(checks),
        allow_extra_columns=_boolean(raw.get("allow_extra_columns", True), "allow_extra_columns"),
    )


def load_contract(path: str | Path) -> Contract:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ContractError("contract file must be a YAML mapping")
    return parse_contract(raw)
