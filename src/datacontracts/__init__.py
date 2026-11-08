"""Data contracts: a YAML spec, a DuckDB validator and a breaking-change detector."""

from .diff import Change, diff_contracts
from .spec import Column, Contract, load_contract
from .validate import RuleResult, validate

__all__ = [
    "Change",
    "Column",
    "Contract",
    "RuleResult",
    "diff_contracts",
    "load_contract",
    "validate",
]
