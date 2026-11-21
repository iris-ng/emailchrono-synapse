"""Human approval of review plans.

Only two callers may use this module: the web app routes in
app/api/review_plans.py (the Approve and Decline buttons) and the backup
terminal script scripts/approve_plan.py. It must never be imported by an MCP
server or exposed as a tool, so the model has no way to approve its own plan.
tests/test_no_self_approval.py checks every generated and hand-written server.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .. import db
from ..services.audit import record_audit_event
from . import plans

APPROVERS = ("web", "terminal", "bench")


class ApprovalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _load_open(conn, plan_id: str):
    row = conn.execute("SELECT * FROM review_plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise ApprovalError("not_found", f"No review plan with id {plan_id}")
    if plans.expire_if_due(conn, row) == "expired":
        conn.commit()  # keep the expiry even though we refuse
        raise ApprovalError("expired", f"Plan {plan_id} has expired. Ask the agent for a new plan.")
    return row


def approve_plan(plan_id: str, *, via: str, items_hash: str) -> Dict[str, Any]:
    """Approve a waiting plan. ``items_hash`` must match the plan the person saw."""
    if via not in APPROVERS:
        raise ApprovalError("bad_approver", f"Unknown approver {via!r}")
    with db.get_conn() as conn:
        row = _load_open(conn, plan_id)
        if row["status"] != "planned":
            raise ApprovalError("wrong_status", f"Plan {plan_id} is {row['status']}, not waiting for approval.")
        if items_hash != row["items_hash"]:
            raise ApprovalError(
                "hash_mismatch", "The plan shown is not the stored plan. Reload it and review again."
            )
        if plans.compute_fingerprint(conn, json.loads(row["touched_json"])) != row["fingerprint"]:
            conn.execute("UPDATE review_plans SET status = 'stale' WHERE id = ? AND status = 'planned'", (plan_id,))
            conn.commit()
            raise ApprovalError(
                "stale", "The case changed after this plan was built. Ask the agent for a new plan."
            )
        now = db.utc_now()
        updated = conn.execute(
            """
            UPDATE review_plans
            SET status = 'approved', approved_at = ?, approved_via = ?, approved_items_hash = ?
            WHERE id = ? AND status = 'planned'
            """,
            (now, via, items_hash, plan_id),
        )
        if updated.rowcount != 1:  # someone else decided first
            raise ApprovalError("wrong_status", f"Plan {plan_id} is no longer waiting for approval.")
        record_audit_event(
            conn,
            case_id=row["case_id"],
            action="review_plan.approved",
            entity_type="review_plan",
            metadata={"plan_id": plan_id, "kind": row["kind"], "via": via, "items_hash": items_hash},
            actor=f"person ({via})",
        )
    plan = plans.get_plan(plan_id)
    assert plan is not None
    return plan


def decline_plan(plan_id: str, *, via: str) -> Dict[str, Any]:
    """Decline a plan that is waiting or approved but not yet applied."""
    if via not in APPROVERS:
        raise ApprovalError("bad_approver", f"Unknown approver {via!r}")
    with db.get_conn() as conn:
        row = _load_open(conn, plan_id)
        if row["status"] not in plans.OPEN_STATUSES:
            raise ApprovalError("wrong_status", f"Plan {plan_id} is {row['status']} and cannot be declined.")
        updated = conn.execute(
            """
            UPDATE review_plans SET status = 'declined', declined_at = ?, declined_via = ?
            WHERE id = ? AND status IN ('planned', 'approved')
            """,
            (db.utc_now(), via, plan_id),
        )
        if updated.rowcount != 1:  # applied (or otherwise finished) in the meantime
            raise ApprovalError("wrong_status", f"Plan {plan_id} was already finished and cannot be declined.")
        record_audit_event(
            conn,
            case_id=row["case_id"],
            action="review_plan.declined",
            entity_type="review_plan",
            metadata={"plan_id": plan_id, "kind": row["kind"], "via": via},
            actor=f"person ({via})",
        )
    plan = plans.get_plan(plan_id)
    assert plan is not None
    return plan
