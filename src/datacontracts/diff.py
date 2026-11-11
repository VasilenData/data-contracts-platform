"""Classify the difference between two contract versions.

A contract is a promise from a producer to its consumers. A change that
weakens the promise can break a consumer (BREAKING). A change that
strengthens it cannot (SAFE). Some changes need a human look (REVIEW).
"""

from __future__ import annotations

from dataclasses import dataclass

from .spec import Column, Contract

BREAKING, REVIEW, SAFE = "breaking", "review", "safe"


@dataclass(frozen=True)
class Change:
    level: str
    where: str
    message: str


def _num_bound(where: str, name: str, old, new, lower: bool) -> Change | None:
    """A lower bound (min) is weakened when it goes down or disappears."""
    if old == new:
        return None
    if old is None:  # a bound was added: stronger promise
        return Change(SAFE, where, f"{name} added: {new}")
    if new is None:
        return Change(BREAKING, where, f"{name} removed (was {old})")
    weaker = new < old if lower else new > old
    verb = "widened" if weaker else "tightened"
    return Change(BREAKING if weaker else SAFE, where, f"{name} {verb}: {old} -> {new}")


def _diff_column(old: Column, new: Column) -> list[Change]:
    w = f"column {old.name}"
    out: list[Change] = []
    if old.type != new.type:
        out.append(Change(BREAKING, w, f"type changed: {old.type} -> {new.type}"))
    if old.nullable != new.nullable:
        if new.nullable:
            out.append(Change(BREAKING, w, "now nullable (was NOT NULL)"))
        else:
            out.append(Change(SAFE, w, "now NOT NULL (was nullable)"))
    if old.unique != new.unique:
        if old.unique:
            out.append(Change(BREAKING, w, "no longer unique"))
        else:
            out.append(Change(SAFE, w, "now unique"))
    for c in (_num_bound(w, "min", old.min, new.min, True), _num_bound(w, "max", old.max, new.max, False)):
        if c:
            out.append(c)
    if old.allowed != new.allowed:
        if old.allowed is None:
            out.append(Change(SAFE, w, f"allowed values added: {list(new.allowed or [])}"))
        elif new.allowed is None:
            out.append(Change(BREAKING, w, "allowed-values list removed"))
        else:
            added = sorted(set(new.allowed) - set(old.allowed), key=str)
            removed = sorted(set(old.allowed) - set(new.allowed), key=str)
            if added:
                out.append(Change(BREAKING, w, f"new allowed values consumers may not handle: {added}"))
            if removed:
                out.append(Change(SAFE, w, f"allowed values removed: {removed}"))
    if old.regex != new.regex:
        out.append(Change(REVIEW, w, f"regex changed: {old.regex!r} -> {new.regex!r}"))
    if new.max_violation_rate > old.max_violation_rate:
        out.append(Change(BREAKING, w, f"tolerance raised: {old.max_violation_rate} -> {new.max_violation_rate}"))
    elif new.max_violation_rate < old.max_violation_rate:
        out.append(Change(SAFE, w, f"tolerance lowered: {old.max_violation_rate} -> {new.max_violation_rate}"))
    return out


def diff_contracts(old: Contract, new: Contract) -> list[Change]:
    out: list[Change] = []
    if old.dataset != new.dataset:
        out.append(Change(REVIEW, "dataset", f"renamed: {old.dataset} -> {new.dataset}"))
    if old.owner != new.owner:
        out.append(Change(REVIEW, "owner", f"owner changed: {old.owner} -> {new.owner}"))
    if old.allow_extra_columns != new.allow_extra_columns:
        out.append(
            Change(
                BREAKING if new.allow_extra_columns else SAFE,
                "allow_extra_columns",
                "undeclared columns now allowed" if new.allow_extra_columns else "undeclared columns now rejected",
            )
        )
    old_cols = {c.name: c for c in old.columns}
    new_cols = {c.name: c for c in new.columns}
    for name in old_cols.keys() - new_cols.keys():
        out.append(Change(BREAKING, f"column {name}", "removed"))
    for name in new_cols.keys() - old_cols.keys():
        col = new_cols[name]
        out.append(Change(SAFE, f"column {name}", f"added ({col.type}{'' if col.nullable else ', NOT NULL'})"))
    for name in old_cols.keys() & new_cols.keys():
        out.extend(_diff_column(old_cols[name], new_cols[name]))
    if new.min_rows < old.min_rows:
        out.append(Change(BREAKING, "min_rows", f"lowered: {old.min_rows} -> {new.min_rows}"))
    elif new.min_rows > old.min_rows:
        out.append(Change(SAFE, "min_rows", f"raised: {old.min_rows} -> {new.min_rows}"))
    if old.freshness and not new.freshness:
        out.append(Change(BREAKING, "freshness", "freshness promise removed"))
    elif new.freshness and not old.freshness:
        out.append(Change(SAFE, "freshness", "freshness promise added"))
    elif old.freshness and new.freshness:
        if new.freshness.column != old.freshness.column:
            out.append(Change(REVIEW, "freshness", f"column changed: {old.freshness.column} -> {new.freshness.column}"))
        if new.freshness.max_age_hours > old.freshness.max_age_hours:
            out.append(
                Change(
                    BREAKING,
                    "freshness",
                    f"limit loosened: {old.freshness.max_age_hours} h -> {new.freshness.max_age_hours} h",
                )
            )
        elif new.freshness.max_age_hours < old.freshness.max_age_hours:
            out.append(
                Change(
                    SAFE,
                    "freshness",
                    f"limit tightened: {old.freshness.max_age_hours} h -> {new.freshness.max_age_hours} h",
                )
            )
    old_checks = {c.name: c for c in old.checks}
    new_checks = {c.name: c for c in new.checks}
    for name in old_checks.keys() - new_checks.keys():
        out.append(Change(BREAKING, f"check {name}", "removed"))
    for name in new_checks.keys() - old_checks.keys():
        out.append(Change(SAFE, f"check {name}", "added"))
    for name in old_checks.keys() & new_checks.keys():
        a, b = old_checks[name], new_checks[name]
        if a.expr != b.expr:
            out.append(Change(REVIEW, f"check {name}", "expression changed"))
        if b.max_violation_rate > a.max_violation_rate:
            out.append(
                Change(BREAKING, f"check {name}", f"tolerance raised: {a.max_violation_rate} -> {b.max_violation_rate}")
            )
        elif b.max_violation_rate < a.max_violation_rate:
            out.append(
                Change(SAFE, f"check {name}", f"tolerance lowered: {a.max_violation_rate} -> {b.max_violation_rate}")
            )
        if a.severity == "error" and b.severity == "warn":
            out.append(Change(BREAKING, f"check {name}", "downgraded from error to warn"))
        elif a.severity == "warn" and b.severity == "error":
            out.append(Change(SAFE, f"check {name}", "promoted from warn to error"))
    if not out and old.version != new.version:
        out.append(Change(SAFE, "version", f"bumped with no schema change: {old.version} -> {new.version}"))
    return out


def required_bump(changes: list[Change]) -> str:
    """The smallest semver bump the changes call for."""
    if any(c.level == BREAKING for c in changes):
        return "major"
    if any(c.level == SAFE and c.where != "version" for c in changes):
        return "minor"
    if changes:
        return "patch"
    return "none"


def bump_ok(old: Contract, new: Contract, changes: list[Change]) -> tuple[bool, str]:
    need = required_bump(changes)
    o, n = old.version_tuple(), new.version_tuple()
    if need == "none":
        return True, "no changes"
    if need == "major":
        return n[0] > o[0], "breaking changes need a major version bump"
    if need == "minor":
        return n[:2] > o[:2] and n[0] >= o[0], "additive changes need at least a minor bump"
    return n > o, "changes need a version bump"
