"""Web app routes for the review banner and plan panel (the approval channel)."""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.review import dedupe, plans

LOCAL = {"x-emailchrono-local": "1"}


@pytest.fixture()
def client(planted_case):
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture()
def dup_plan(planted_case):
    pairs = dedupe.list_pending_duplicate_pairs(planted_case["case_id"])
    saved = dedupe.save_duplicate_plan(planted_case["case_id"], [dedupe.propose_duplicate_decision(p) for p in pairs])
    return plans.get_plan(saved["plan_id"])


def test_waiting_plans_are_listed_for_the_case(client, planted_case, dup_plan):
    res = client.get(f"/api/cases/{planted_case['case_id']}/review-plans")
    assert res.status_code == 200
    body = res.json()
    assert [p["id"] for p in body] == [dup_plan["id"]]
    assert body[0]["status"] == "planned" and body[0]["kind"] == "duplicates"


def test_plan_detail_renders_from_the_database(client, dup_plan):
    res = client.get(f"/api/review-plans/{dup_plan['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["items_hash"] == dup_plan["items_hash"]
    assert len(body["items"]) == 6 and len(body["left_for_you"]) == 1
    assert body["stale_now"] is False
    assert body["items"][0]["email_a"]["first_line"]


def test_approve_requires_the_local_header(client, dup_plan):
    res = client.post(f"/api/review-plans/{dup_plan['id']}/approve", json={"items_hash": dup_plan["items_hash"]})
    assert res.status_code == 403
    assert plans.get_plan(dup_plan["id"])["status"] == "planned"


def test_approve_from_web_records_via_and_hash(client, dup_plan):
    res = client.post(
        f"/api/review-plans/{dup_plan['id']}/approve", json={"items_hash": dup_plan["items_hash"]}, headers=LOCAL
    )
    assert res.status_code == 200
    stored = plans.get_plan(dup_plan["id"])
    assert stored["status"] == "approved" and stored["approved_via"] == "web"
    assert stored["approved_items_hash"] == dup_plan["items_hash"]


def test_approve_with_wrong_hash_is_a_conflict(client, dup_plan):
    res = client.post(
        f"/api/review-plans/{dup_plan['id']}/approve", json={"items_hash": "f" * 64}, headers=LOCAL
    )
    assert res.status_code == 409 and res.json()["detail"]["code"] == "hash_mismatch"


def test_decline_from_web(client, dup_plan):
    res = client.post(f"/api/review-plans/{dup_plan['id']}/decline", headers=LOCAL)
    assert res.status_code == 200 and res.json()["status"] == "declined"
    assert client.get(f"/api/cases/{dup_plan['case_id']}/review-plans").json() == []


def test_unknown_plan_is_404(client, planted_case):
    assert client.get("/api/review-plans/rp_nope").status_code == 404
    res = client.post("/api/review-plans/rp_nope/approve", json={"items_hash": "x"}, headers=LOCAL)
    assert res.status_code == 404


def test_detail_warns_and_approve_refuses_when_plan_went_stale(client, dup_plan):
    email_id = dup_plan["items"][0]["email_ids"][0]
    with db.get_conn() as conn:
        conn.execute("UPDATE emails SET subject = subject || ' (edited)' WHERE id = ?", (email_id,))
    assert client.get(f"/api/review-plans/{dup_plan['id']}").json()["stale_now"] is True
    res = client.post(
        f"/api/review-plans/{dup_plan['id']}/approve", json={"items_hash": dup_plan["items_hash"]}, headers=LOCAL
    )
    assert res.status_code == 409 and res.json()["detail"]["code"] == "stale"
    assert plans.get_plan(dup_plan["id"])["status"] == "stale"
