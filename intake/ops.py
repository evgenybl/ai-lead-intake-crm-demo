"""The allow-list: the only four operations a plan may hold (section 3.5).

The write gate refuses a plan with any other operation before anything leaves, and `desk.py doctor`
checks every stored plan and every ledger row against this same list.
"""
from __future__ import annotations

ALLOWED_OPS = ("create_contact", "fill_empty_fields", "add_note", "add_task")


def operations_outside(plan: dict, allowed=ALLOWED_OPS) -> list:
    """The names of the plan's operations that are not allowed, in plan order."""
    return [op.get("op") for op in plan.get("operations", []) if op.get("op") not in allowed]


def repeated_operations(plan: dict) -> list:
    """An operation kind may appear once per plan: the ledger records one result per kind."""
    seen, repeated = set(), []
    for op in plan.get("operations", []):
        name = op.get("op")
        if name in seen and name not in repeated:
            repeated.append(name)
        seen.add(name)
    return repeated
