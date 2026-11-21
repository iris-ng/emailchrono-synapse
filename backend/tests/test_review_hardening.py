"""Fixes from the code review of the approval gate: races, terminal states,
fingerprint coverage, plan snapshot consistency and old scorer reasons."""

import json
import sqlite3

import pytest

from app import db
from app.config import DB_PATH
from app.review import approval, dedupe, plans


def _plan(planted_case):
    pairs = dedupe.list_pending_duplicate_pairs(planted_case["case_id"])
    saved = dedupe.save_duplicate_plan(planted_case["case_id"], [dedupe.propose_duplicate_decision(p) for p in pairs])
    return plans.get_plan(saved["plan_id"])


def _other_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def test_terminal_status_cannot_change(planted_case):
    plan = _plan(planted_case)
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    assert dedupe.apply_duplicate_plan(planted_case["case_id"], plan["id"])["status"] == "applied"
    with db.get_conn() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_plans SET status = 'declined' WHERE id = ?", (plan["id"],))


def test_approval_fields_cannot_change_once_set(planted_case):
    plan = _plan(planted_case)
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    with db.get_conn() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE review_plans SET approved_via = 'terminal' WHERE id = ?", (plan["id"],))


def test_approve_refuses_if_plan_was_declined_meanwhile(planted_case, monkeypatch):
    plan = _plan(planted_case)
    real = plans.compute_fingerprint

    def decline_meanwhile(conn, touched):
        other = _other_conn()
        other.execute("UPDATE review_plans SET status = 'declined' WHERE id = ?", (plan["id"],))
        other.commit()
        other.close()
        return real(conn, touched)

    monkeypatch.setattr(plans, "compute_fingerprint", decline_meanwhile)
    with pytest.raises(approval.ApprovalError) as err:
        approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    assert err.value.code == "wrong_status"
    with db.get_conn() as conn:
        approved_events = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'review_plan.approved'"
        ).fetchone()[0]
    assert approved_events == 0
    assert plans.get_plan(plan["id"])["status"] == "declined"


def test_decline_refuses_if_plan_was_applied_meanwhile(planted_case, monkeypatch):
    plan = _plan(planted_case)
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    real = plans.expire_if_due

    def apply_meanwhile(conn, row):
        status = real(conn, row)
        other = _other_conn()
        other.execute("UPDATE review_plans SET status = 'applied' WHERE id = ?", (plan["id"],))
        other.commit()
        other.close()
        return status

    monkeypatch.setattr(plans, "expire_if_due", apply_meanwhile)
    with pytest.raises(approval.ApprovalError) as err:
        approval.decline_plan(plan["id"], via="web")
    assert err.value.code == "wrong_status"
    monkeypatch.undo()
    assert plans.get_plan(plan["id"])["status"] == "applied"


def test_expiry_does_not_overwrite_a_plan_applied_meanwhile(planted_case):
    plan = _plan(planted_case)
    with db.get_conn() as conn:
        conn.execute("UPDATE review_plans SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (plan["id"],))
    with db.get_conn() as conn:
        old_row = conn.execute("SELECT * FROM review_plans WHERE id = ?", (plan["id"],)).fetchone()
    other = _other_conn()
    other.execute("UPDATE review_plans SET status = 'applied' WHERE id = ?", (plan["id"],))
    other.commit()
    other.close()
    with db.get_conn() as conn:
        plans.expire_if_due(conn, old_row)
        status = conn.execute("SELECT status FROM review_plans WHERE id = ?", (plan["id"],)).fetchone()[0]
    assert status == "applied"


def test_apply_recomputes_the_items_hash(planted_case):
    case_id = planted_case["case_id"]
    email_id = planted_case["card"]("14_costplan_v3.eml")
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO review_plans (id, case_id, kind, status, items_json, touched_json, fingerprint,
              items_hash, created_at, expires_at, approved_at, approved_via, approved_items_hash)
            VALUES ('rp_forged', ?, 'duplicates', 'approved', '[{"op":"noop"}]', ?, ?, 'abc', ?, ?, ?, 'web', 'abc')
            """,
            (
                case_id,
                json.dumps({"emails": [email_id], "duplicate_candidates": []}),
                plans.compute_fingerprint(conn, {"emails": [email_id]}),
                "2026-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
    result = plans.apply_plan(
        case_id, "rp_forged", "duplicates", validate_item=lambda c, i: None, apply_item=lambda c, i: []
    )
    assert result["status"] == "refused" and result["code"] == "not_approved"


def test_message_id_change_makes_plan_stale(planted_case):
    plan = _plan(planted_case)
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    email_id = plan["items"][0]["email_ids"][0]
    with db.get_conn() as conn:
        conn.execute("UPDATE emails SET message_id = '<changed@example>' WHERE id = ?", (email_id,))
    assert dedupe.apply_duplicate_plan(planted_case["case_id"], plan["id"])["code"] == "stale"


def test_save_refuses_if_a_card_changes_while_planning(planted_case, monkeypatch):
    case_id = planted_case["case_id"]
    pairs = dedupe.list_pending_duplicate_pairs(case_id)
    decisions = [dedupe.propose_duplicate_decision(p) for p in pairs]
    target = next(d for d in decisions if d["decision"] == "duplicate")["keeper_id"]
    real = dedupe._summarize

    def edit_meanwhile(items, left, warnings):
        other = _other_conn()
        other.execute("UPDATE emails SET subject = 'Edited while planning' WHERE id = ?", (target,))
        other.commit()
        other.close()
        return real(items, left, warnings)

    monkeypatch.setattr(dedupe, "_summarize", edit_meanwhile)
    result = dedupe.save_duplicate_plan(case_id, decisions)
    assert result["ok"] is False and "changed while" in result["error"]
    assert plans.list_plans(case_id) == []


def test_old_shared_file_hash_reason_is_not_treated_as_exact(planted_case):
    case_id = planted_case["case_id"]
    parent = planted_case["card"]("06_costplan_v2_reply.eml", 0)
    child = planted_case["card"]("06_costplan_v2_reply.eml", 1)
    with db.get_conn() as conn:  # a pair saved by the old scorer, before the fix
        conn.execute(
            """
            INSERT INTO email_duplicate_candidates (case_id, email_a_id, email_b_id, score, reason_json, status, created_at, updated_at)
            VALUES (?, ?, ?, 1.0, ?, 'pending', '2026-01-01', '2026-01-01')
            """,
            (case_id, min(parent, child), max(parent, child),
             json.dumps([{"code": "same_source_sha256", "label": "Same source file hash"}])),
        )
    pair = next(
        p for p in dedupe.list_pending_duplicate_pairs(case_id)
        if {p["email_a"]["id"], p["email_b"]["id"]} == {parent, child}
    )
    assert pair["strength"] == "weak"
    assert dedupe.propose_duplicate_decision(pair)["decision"] != "duplicate"
