"""Scripted approver for benchmark runs only.

Stands in for the person so benchmark runs can finish unattended. It calls the
same approval functions as scripts/approve_plan.py, with via="bench", and logs
every decision. It is used only when the runner is started with
--approver bench-approve or bench-decline; the model cannot reach it.
"""

from __future__ import annotations

from typing import Callable

from app.review import approval, plans


def scripted_decision(plan_id: str, policy: str, log: Callable[..., None]) -> str:
    plan = plans.get_plan(plan_id)
    if plan is None:
        log("approval_decision", plan_id=plan_id, decision="missing", via="bench")
        return "missing"
    try:
        if policy == "approve":
            approval.approve_plan(plan_id, via="bench", items_hash=plan["items_hash"])
        elif policy == "decline":
            approval.decline_plan(plan_id, via="bench")
        else:
            raise ValueError(f"unknown policy {policy!r}")
        decision = policy
    except approval.ApprovalError as err:
        decision = f"refused:{err.code}"
    log("approval_decision", plan_id=plan_id, decision=decision, via="bench",
        kind=plan["kind"], items=len(plan["items"]), left_for_you=len(plan["left_for_you"]))
    return decision
