"""Contract model and YAML loader."""

from __future__ import annotations

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
        nullable=bool(raw.get("nullable", True)),
        unique=bool(raw.get("unique", False)),
        min=raw.get("min"),
        max=raw.get("max"),
        allowed=tuple(allowed) if allowed is not None else None,
        regex=raw.get("regex"),
        max_violation_rate=float(raw.get("max_violation_rate", 0.0)),
    )


def parse_contract(raw: dict[str, Any]) -> Contract:
    for key in ("dataset", "version", "owner", "columns"):
        if key not in raw:
            raise ContractError(f"missing required key: {key}")
    if not SEMVER.match(str(raw["version"])):
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
        freshness = Freshness(fresh["column"], float(fresh["max_age_hours"]))
    checks = []
    for c in raw.get("checks", []):
        severity = c.get("severity", "error")
        if severity not in {"error", "warn"}:
            raise ContractError(f"check {c.get('name')}: severity must be error or warn")
        checks.append(Check(c["name"], c["expr"], severity, float(c.get("max_violation_rate", 0.0))))
    return Contract(
        dataset=raw["dataset"],
        version=str(raw["version"]),
        owner=raw["owner"],
        columns=columns,
        min_rows=int(raw.get("min_rows", 0)),
        freshness=freshness,
        checks=tuple(checks),
        allow_extra_columns=bool(raw.get("allow_extra_columns", True)),
    )


def load_contract(path: str | Path) -> Contract:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ContractError("contract file must be a YAML mapping")
    return parse_contract(raw)
