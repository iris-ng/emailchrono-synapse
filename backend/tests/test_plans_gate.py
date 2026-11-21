"""The plan, approve, apply contract shared by both review tools (spec section 6)."""

import sqlite3

import pytest

from app import db
from app.review import approval, plans


def _touch_case_name_plan(case_id, email_id):
    """A tiny plan whose single item renames nothing but is tracked on one email."""
    return plans.create_plan(
        case_id,
        "duplicates",
        items=[{"op": "noop", "email_id": email_id}],
        left_for_you=[{"why": "example"}],
        summary="test plan",
        touched={"emails": [email_id], "duplicate_candidates": []},
    )


def _apply_noop(case_id, plan_id, **kwargs):
    def apply_item(conn, item):
        conn.execute("UPDATE emails SET notes = notes || '+' WHERE id = ?", (item["email_id"],))
        return []

    return plans.apply_plan(
        case_id,
        plan_id,
        "duplicates",
        validate_item=lambda conn, item: None,
        apply_item=apply_item,
        **kwargs,
    )


def _notes(email_id):
    with db.get_conn() as conn:
        return conn.execute("SELECT notes FROM emails WHERE id = ?", (email_id,)).fetchone()["notes"]


def test_new_plan_is_stored_as_planned_with_hash_and_expiry(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    stored = plans.get_plan(plan["id"])
    assert stored["status"] == "planned"
    assert stored["items"] == [{"op": "noop", "email_id": email_id}]
    assert stored["left_for_you"] == [{"why": "example"}]
    assert len(stored["items_hash"]) == 64
    assert stored["expires_at"] > stored["created_at"]


def test_apply_before_approval_waits_and_changes_nothing(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    result = _apply_noop(planted_case["case_id"], plan["id"])
    assert result["status"] == "waiting_for_approval"
    assert _notes(email_id) == ""


def test_approve_needs_the_hash_the_user_saw(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    with pytest.raises(approval.ApprovalError) as err:
        approval.approve_plan(plan["id"], via="web", items_hash="0" * 64)
    assert err.value.code == "hash_mismatch"
    assert plans.get_plan(plan["id"])["status"] == "planned"


def test_approved_plan_applies_once_and_retry_returns_same_receipt(planted_case):
    case_id = planted_case["case_id"]
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(case_id, email_id)
    approved = approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    assert approved["status"] == "approved" and approved["approved_via"] == "web"

    first = _apply_noop(case_id, plan["id"])
    assert first["status"] == "applied"
    assert _notes(email_id) == "+"
    second = _apply_noop(case_id, plan["id"])
    assert second["status"] == "applied" and second["already_applied"] is True
    assert second["receipt"] == first["receipt"]
    assert _notes(email_id) == "+"


def test_apply_writes_audit_event_and_bumps_case(planted_case):
    case_id = planted_case["case_id"]
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(case_id, email_id)
    approval.approve_plan(plan["id"], via="terminal", items_hash=plan["items_hash"])
    with db.get_conn() as conn:
        before = conn.execute("SELECT updated_at FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
    result = _apply_noop(case_id, plan["id"])
    with db.get_conn() as conn:
        after = conn.execute("SELECT updated_at FROM cases WHERE id = ?", (case_id,)).fetchone()[0]
        event = conn.execute(
            "SELECT action FROM audit_events WHERE id = ?", (result["receipt"]["plan_event_id"],)
        ).fetchone()
    assert after > before
    assert event["action"] == "review_plan.applied"


def test_declined_plan_is_refused(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    approval.decline_plan(plan["id"], via="web")
    result = _apply_noop(planted_case["case_id"], plan["id"])
    assert result["status"] == "refused" and result["code"] == "declined"
    assert _notes(email_id) == ""


def test_expired_plan_is_refused_and_cannot_be_approved(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    with db.get_conn() as conn:
        conn.execute("UPDATE review_plans SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (plan["id"],))
    with pytest.raises(approval.ApprovalError) as err:
        approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    assert err.value.code == "expired"
    assert plans.get_plan(plan["id"])["status"] == "expired"


def test_approved_plan_past_expiry_is_refused(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    with db.get_conn() as conn:
        conn.execute("UPDATE review_plans SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (plan["id"],))
    result = _apply_noop(planted_case["case_id"], plan["id"])
    assert result["code"] == "expired"
    assert _notes(email_id) == ""


def test_plan_goes_stale_when_a_touched_row_changes(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    with db.get_conn() as conn:  # the user edits the card in the UI
        conn.execute("UPDATE emails SET subject = 'Cost plan v3 (edited)' WHERE id = ?", (email_id,))
    result = _apply_noop(planted_case["case_id"], plan["id"])
    assert result["status"] == "refused" and result["code"] == "stale"
    assert plans.get_plan(plan["id"])["status"] == "stale"
    assert _notes(email_id) == ""


def test_plan_for_another_case_is_refused(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    result = _apply_noop(planted_case["case_id"] + 99, plan["id"])
    assert result["code"] == "wrong_case"


def test_unknown_plan_is_refused():
    result = _apply_noop(1, "rp_doesnotexist")
    assert result["status"] == "refused" and result["code"] == "not_found"


def test_invalid_item_refuses_whole_plan(planted_case):
    case_id = planted_case["case_id"]
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(case_id, email_id)
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    result = plans.apply_plan(
        case_id, plan["id"], "duplicates",
        validate_item=lambda conn, item: "card was deleted",
        apply_item=lambda conn, item: [],
    )
    assert result["code"] == "invalid_item"
    assert "card was deleted" in result["reason"]


def test_failure_halfway_through_apply_changes_nothing(planted_case, monkeypatch):
    case_id = planted_case["case_id"]
    a = planted_case["card"]("14_costplan_v3.eml")
    b = planted_case["card"]("15_costplan_v3_received.eml")
    plan = plans.create_plan(
        case_id, "duplicates",
        items=[{"op": "noop", "email_id": a}, {"op": "noop", "email_id": b}],
        left_for_you=[], summary="two items",
        touched={"emails": [a, b], "duplicate_candidates": []},
    )
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    monkeypatch.setenv("EMAILCHRONO_REVIEW_FAIL_AFTER", "1")
    with db.get_conn() as conn:
        events_before = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    result = _apply_noop(case_id, plan["id"])
    assert result["status"] == "refused" and result["code"] == "apply_failed"
    assert _notes(a) == "" and _notes(b) == ""
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == events_before
    assert plans.get_plan(plan["id"])["status"] == "approved"


def test_plans_are_never_edited_after_creation(planted_case):
    email_id = planted_case["card"]("14_costplan_v3.eml")
    plan = _touch_case_name_plan(planted_case["case_id"], email_id)
    with db.get_conn() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_plans SET items_json = '[]' WHERE id = ?", (plan["id"],))


def test_plan_in_flight_reports_planned_or_approved_plans(planted_case):
    case_id = planted_case["case_id"]
    email_id = planted_case["card"]("14_costplan_v3.eml")
    assert plans.plan_in_flight(case_id, "duplicates") is None
    plan = _touch_case_name_plan(case_id, email_id)
    assert plans.plan_in_flight(case_id, "duplicates")["id"] == plan["id"]
    approval.decline_plan(plan["id"], via="web")
    assert plans.plan_in_flight(case_id, "duplicates") is None
