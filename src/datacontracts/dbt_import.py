"""Draft a contract from a dbt model's `schema.yml`.

Only dbt tests that mean the same thing as a contract rule are imported. Everything else is
skipped and listed, never guessed: a rule that quietly means something different is worse than
a missing one. The contract validator runs on one file, so joins (`relationships`) cannot be checked.

Imported: `not_null`, `unique`, `accepted_values`, `accepted_range` (dbt_utils), and the
`not_null`, `unique` and `primary_key` constraints of a dbt model contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import yaml

from .profile import _flow
from .spec import TYPES, ContractError
from .validate import kind_of, relation, source_exists

_INT = {"int", "integer", "bigint", "smallint", "tinyint", "hugeint", "int2", "int4", "int8", "int64", "long", "short"}
_FLOAT = {"double", "double precision", "float", "float4", "float8", "float32", "float64", "real", "numeric"}
_FLOAT |= {"decimal", "number"}
_STRING = {"varchar", "text", "string", "char", "character", "character varying", "nvarchar"}
_BOOL = {"boolean", "bool"}

# Keys that configure a test rather than parameterize it, when dbt's older flat style is used.
_CONFIG_KEYS = {"config", "severity", "where", "name", "tags", "meta", "limit", "fail_calc", "error_if", "warn_if"}
_CONFIG_KEYS |= {"store_failures", "store_failures_as", "enabled"}
_NULL_IGNORING = {"accepted_values", "accepted_range", "unique"}  # same result with or without `col is not null`


def map_type(data_type: str | None) -> str | None:
    """Map a dbt `data_type` to a contract type, or None when there is no honest equivalent."""
    if not isinstance(data_type, str):
        return None
    t = re.sub(r"\(.*?\)", "", data_type).strip().lower()
    if t in _INT:
        return "int"
    if t in _FLOAT:
        return "float"
    if t in _STRING:
        return "string"
    if t in _BOOL:
        return "bool"
    if t == "date":
        return "date"
    if t.startswith("timestamp") or t == "datetime":
        return "timestamp"
    return None


def _items(entry: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    for key in ("tests", "data_tests"):
        value = entry.get(key)
        if isinstance(value, list):
            out += value
    return out


def _split(item: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """(test name, arguments, config) from a string or a one-key mapping."""
    if isinstance(item, str):
        return item, {}, {}
    if isinstance(item, dict) and len(item) == 1:
        (name, body), *_ = item.items()
        body = body if isinstance(body, dict) else {}
        config = dict(body.get("config") or {})
        for key in ("severity", "where", "error_if", "warn_if", "limit", "fail_calc"):
            if key in body:
                config[key] = body[key]
        args = body["arguments"] if isinstance(body.get("arguments"), dict) else {}
        if not args:
            args = {k: v for k, v in body.items() if k not in _CONFIG_KEYS}
        return str(name), args, config
    return str(item), {}, {}


def _where_is_null_guard(where: str, column: str) -> bool:
    text = re.sub(r"\s+", " ", where.strip().lower()).replace('"', "").replace("`", "")
    return text == f"{column.lower()} is not null"


def _blocked(name: str, column: str, config: dict[str, Any]) -> str | None:
    """Why a test with this config cannot become a column rule, or None."""
    if str(config.get("severity", "error")).lower() == "warn":
        return "severity warn: contract columns have no warn level"
    if any(k in config for k in ("error_if", "warn_if", "limit", "fail_calc")):
        return "failure thresholds (error_if, warn_if, limit, fail_calc) have no contract equivalent"
    where = config.get("where")
    if where:
        if name in _NULL_IGNORING and isinstance(where, str) and _where_is_null_guard(where, column):
            return None
        return f"where filter {str(where)!r} cannot be expressed on a column"
    return None


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _apply_test(name: str, args: dict[str, Any], col: dict[str, Any], notes: list[str]) -> bool:
    """Apply one test to the column rule dict. True if imported."""
    cname = col["name"]
    if name == "not_null":
        col["nullable"] = False
        return True
    if name == "unique":
        col["unique"] = True
        return True
    if name == "accepted_values":
        values = args.get("values")
        if not isinstance(values, list) or not values:
            notes.append(f"skipped accepted_values on {cname}: no values list")
            return False
        if "allowed" in col:
            notes.append(f"skipped accepted_values on {cname}: the column already has an allowed list")
            return False
        col["allowed"] = list(values)
        return True
    if name == "accepted_range":
        lo, hi = args.get("min_value"), args.get("max_value")
        if col["type"] not in ("int", "float"):
            notes.append(f"skipped accepted_range on {cname}: column type is {col['type']}, not a number")
            return False
        if not (_number(lo) or _number(hi)) or any(v is not None and not _number(v) for v in (lo, hi)):
            notes.append(f"skipped accepted_range on {cname}: needs numeric min_value or max_value")
            return False
        if "min" in col or "max" in col:
            notes.append(f"skipped accepted_range on {cname}: the column already has a range")
            return False
        if lo is not None:
            col["min"] = lo
        if hi is not None:
            col["max"] = hi
        if args.get("inclusive") is False:
            notes.append(
                f"note: accepted_range on {cname} is strict in dbt; imported as inclusive, a slightly weaker rule"
            )
        return True
    if name == "relationships":
        notes.append(
            f"skipped relationships on {cname}: references {args.get('to')}.{args.get('field')}; "
            "the validator checks one file, not joins"
        )
        return False
    notes.append(f"skipped {name} on {cname}: no equivalent contract rule")
    return False


def _constraints(col_entry: dict[str, Any], col: dict[str, Any], notes: list[str]) -> tuple[int, int]:
    """(imported, total) for the dbt model-contract constraints on a column."""
    done = total = 0
    for c in col_entry.get("constraints") or []:
        kind = c.get("type") if isinstance(c, dict) else c
        total += 1
        if kind == "not_null":
            col["nullable"] = False
        elif kind == "unique":
            col["unique"] = True
        elif kind == "primary_key":
            col["nullable"], col["unique"] = False, True
        else:
            notes.append(f"skipped constraint {kind} on {col['name']}: no equivalent contract rule")
            continue
        done += 1
    return done, total


def _source_types(source: str | None) -> dict[str, str]:
    if source is None:
        return {}
    if not source_exists(source):
        raise FileNotFoundError(f"no such file: {source}")
    rows = duckdb.connect().execute(f"DESCRIBE SELECT * FROM {relation(source)}").fetchall()
    return {name: kind_of(duck) for name, duck, *_ in rows if kind_of(duck) in TYPES}


def _pick_model(parsed: Any, model: str | None) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        raise ContractError("schema file must be a YAML mapping")
    models = parsed.get("models")
    if not isinstance(models, list) or not models:
        raise ContractError("schema file has no models list")
    entries = [m for m in models if isinstance(m, dict)]
    if any("name" not in m for m in entries) or len(entries) != len(models):
        raise ContractError("every model needs a name")
    names = ", ".join(m["name"] for m in entries)
    if model is None:
        if len(entries) > 1:
            raise ContractError(f"the file has several models, pick one with --model: {names}")
        return entries[0]
    for m in entries:
        if m["name"] == model:
            return m
    raise ContractError(f"no model {model!r} in the file; models are: {names}")


@dataclass(frozen=True)
class Imported:
    raw: dict[str, Any]
    notes: list[str]
    imported: int  # tests and constraints that became contract rules
    found: int  # tests and constraints in the file for the chosen model
    model: str


def import_dbt_report(
    schema_path: str,
    *,
    model: str | None = None,
    dataset: str | None = None,
    owner: str | None = None,
    source: str | None = None,
) -> Imported:
    path = Path(schema_path)
    if not path.exists():
        raise FileNotFoundError(f"no such file: {schema_path}")
    entry = _pick_model(yaml.safe_load(path.read_text()), model)
    columns_in = entry.get("columns")
    if columns_in is not None and not isinstance(columns_in, list):
        raise ContractError(f"model {entry['name']}: columns must be a list")
    from_file = _source_types(source)
    notes: list[str] = []
    columns: list[dict[str, Any]] = []
    done = total = 0

    for c in columns_in or []:
        if not isinstance(c, dict) or "name" not in c:
            raise ContractError(f"model {entry['name']}: every column needs a name")
        name = c["name"]
        tests = _items(c)
        constraints = c.get("constraints") or []
        total += len(tests) + len(constraints)
        kind = map_type(c.get("data_type")) or from_file.get(name)
        if kind is None:
            why = f"not in {source}" if source and name not in from_file and not c.get("data_type") else None
            if c.get("data_type") and map_type(c["data_type"]) is None:
                why = f"data_type {c['data_type']!r} has no contract type"
            notes.append(
                f"skipped column {name}: "
                + (why or "no data_type; add one to the schema file or pass --source to read it from a file")
            )
            continue
        col: dict[str, Any] = {"name": name, "type": kind}
        d, _ = _constraints(c, col, notes)
        done += d
        for item in tests:
            tname, args, config = _split(item)
            tname = tname.removeprefix("dbt_utils.")
            why = _blocked(tname, name, config)
            if why:
                notes.append(f"skipped {tname} on {name}: {why}")
                continue
            done += _apply_test(tname, args, col, notes)
        columns.append(col)

    for item in _items(entry):
        total += 1
        tname, _, _ = _split(item)
        notes.append(
            f"skipped {tname} on model {entry['name']}: model-level tests span columns and have no contract rule"
        )
    for item in entry.get("constraints") or []:
        total += 1
        kind = item.get("type") if isinstance(item, dict) else item
        notes.append(f"skipped constraint {kind} on model {entry['name']}: model-level constraint")

    if not columns:
        raise ContractError(f"model {entry['name']} has no column with a usable type")
    meta = {**(entry.get("config") or {}).get("meta", {}), **(entry.get("meta") or {})}
    raw = {
        "dataset": dataset or entry["name"],
        "version": "0.1.0",
        "owner": owner or meta.get("owner") or "TODO-owner@example.com",
        "allow_extra_columns": True,
        "columns": columns,
    }
    return Imported(raw, notes, done, total, entry["name"])


def import_dbt(schema_path: str, **kwargs: Any) -> tuple[dict[str, Any], list[str]]:
    r = import_dbt_report(schema_path, **kwargs)
    return r.raw, r.notes


def dbt_contract_yaml(raw: dict[str, Any], schema_path: str, model: str, notes: list[str]) -> str:
    out = [
        "# Imported by `datacontracts dbt-import`. Review every rule before you rely on it.",
        f"# From {schema_path}, model {model}. dbt tests with no contract equivalent are listed below.",
    ]
    out += [f"# - {' '.join(n.split())}" for n in notes]
    out += [f"{k}: {_flow(raw[k])}" for k in ("dataset", "version", "owner", "allow_extra_columns")]
    out.append("columns:")
    for c in raw["columns"]:
        out.append("  - {" + ", ".join(f"{k}: {_flow(v)}" for k, v in c.items()) + "}")
    return "\n".join(out) + "\n"
