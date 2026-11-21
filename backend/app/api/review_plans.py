"""Routes for AI review plans: the banner, the plan panel, Approve and Decline.

These routes are the main approval channel. No MCP server calls them: the
tools only read plan status from the database, so the model cannot approve.
POST routes need the X-EmailChrono-Local header (HostAllowlistMiddleware).
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..review import approval, plans

router = APIRouter(tags=["review-plans"])

_ERROR_STATUS = {"not_found": 404, "bad_approver": 400}


class ApproveRequest(BaseModel):
    items_hash: str


def _public(plan: Dict[str, Any], detail: bool = False) -> Dict[str, Any]:
    keys = [
        "id", "case_id", "kind", "status", "summary", "warnings", "created_at", "expires_at",
        "approved_at", "approved_via", "declined_at", "applied_at", "items_hash",
    ]
    out = {key: plan.get(key) for key in keys}
    out["item_count"] = len(plan["items"])
    out["left_for_you_count"] = len(plan["left_for_you"])
    if detail:
        out["items"] = plan["items"]
        out["left_for_you"] = plan["left_for_you"]
        out["receipt"] = plan["receipt"]
        out["stale_now"] = _stale_now(plan)
    return out


def _stale_now(plan: Dict[str, Any]) -> bool:
    if plan["status"] not in plans.OPEN_STATUSES:
        return False
    with db.get_conn() as conn:
        return plans.compute_fingerprint(conn, plan["touched"]) != plan["fingerprint"]


def _raise(err: approval.ApprovalError):
    raise HTTPException(
        status_code=_ERROR_STATUS.get(err.code, 409),
        detail={"code": err.code, "message": err.message},
    )


@router.get("/api/cases/{case_id}/review-plans")
def list_waiting_plans(case_id: int, status: Optional[str] = "planned"):
    statuses = [s for s in (status or "").split(",") if s] or None
    return [_public(plan) for plan in plans.list_plans(case_id, statuses)]


@router.get("/api/review-plans/{plan_id}")
def show_plan(plan_id: str):
    plan = plans.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Plan not found"})
    return _public(plan, detail=True)


@router.post("/api/review-plans/{plan_id}/approve")
def approve(plan_id: str, payload: ApproveRequest):
    try:
        return _public(approval.approve_plan(plan_id, via="web", items_hash=payload.items_hash), detail=True)
    except approval.ApprovalError as err:
        _raise(err)


@router.post("/api/review-plans/{plan_id}/decline")
def decline(plan_id: str):
    try:
        return _public(approval.decline_plan(plan_id, via="web"), detail=True)
    except approval.ApprovalError as err:
        _raise(err)
