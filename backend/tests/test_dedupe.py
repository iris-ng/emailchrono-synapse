"""Duplicate adapters, scored against the planted case answer key."""

import json

from app import db
from app.review import approval, dedupe, plans, problems


def _key(planted_case, email_id):
    return planted_case["cards"][email_id]["keys"]


def _pairs_and_decisions(planted_case):
    pairs = dedupe.list_pending_duplicate_pairs(planted_case["case_id"])
    return pairs, [dedupe.propose_duplicate_decision(p) for p in pairs]


def test_problem_cards_finds_undated_and_conflicting_cards(planted_case):
    found = {p["email_id"]: p["problems"] for p in problems.find_problem_cards(planted_case["case_id"])}
    assert "undated" in found[planted_case["card"]("01_drawings_reply.eml", 1)]
    assert "undated" in found[planted_case["card"]("10_access_cards_undated.eml")]
    assert "chain_date_conflict" in found[planted_case["card"]("08_acoustic_reply.eml", 1)]
    assert planted_case["card"]("14_costplan_v3.eml") not in found


def test_list_pairs_gives_ids_reasons_and_keeper(planted_case):
    pairs = dedupe.list_pending_duplicate_pairs(planted_case["case_id"])
    assert len(pairs) == 7
    pair = next(p for p in pairs if "same_message_id" in p["reasons"] and p["email_a"]["subject"].startswith("Re: Ceiling"))
    assert pair["suggested_keeper_id"] in (pair["email_a"]["id"], pair["email_b"]["id"])
    assert pair["email_a"]["from"] and pair["email_a"]["first_line"]
    assert pair["group_id"].startswith("group-")


def test_every_proposed_change_agrees_with_ground_truth(planted_case):
    pairs, decisions = _pairs_and_decisions(planted_case)
    for pair, decision in zip(pairs, decisions):
        same_message = _key(planted_case, pair["email_a"]["id"]) == _key(planted_case, pair["email_b"]["id"])
        if decision["decision"] == "duplicate":
            assert same_message, pair
        if decision["decision"] == "dissimilar":
            assert not same_message, pair
            assert decision["evidence"], decision


def test_strong_pairs_are_duplicates_and_weak_true_copy_is_left_for_the_user(planted_case):
    pairs, decisions = _pairs_and_decisions(planted_case)
    counts = {}
    for d in decisions:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    assert counts == {"duplicate": 4, "dissimilar": 2, "left_for_you": 1}
    left = next(d for d in decisions if d["decision"] == "left_for_you")
    left_pair = next(p for p in pairs if p["candidate_id"] == left["candidate_id"])
    assert left_pair["email_a"]["subject"] == "Cost plan v3"


def test_week_apart_minutes_are_marked_not_duplicate_with_date_evidence(planted_case):
    pairs, decisions = _pairs_and_decisions(planted_case)
    minutes = next(
        d for p, d in zip(pairs, decisions) if p["email_a"]["subject"] == "Site meeting minutes"
    )
    assert minutes["decision"] == "dissimilar"
    assert any("days apart" in e for e in minutes["evidence"])


def test_save_plan_stores_items_and_changes_no_candidates(planted_case):
    case_id = planted_case["case_id"]
    _, decisions = _pairs_and_decisions(planted_case)
    result = dedupe.save_duplicate_plan(case_id, decisions)
    assert result["ok"] and result["plan_id"].startswith("rp_")
    assert "waiting for approval" in result["summary"].lower()
    plan = plans.get_plan(result["plan_id"])
    assert len(plan["items"]) == 6 and len(plan["left_for_you"]) == 1
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM email_duplicate_candidates WHERE case_id = ? AND status != 'pending'", (case_id,)
        ).fetchone()[0] == 0


def test_save_rejects_keeper_outside_the_pair(planted_case):
    case_id = planted_case["case_id"]
    _, decisions = _pairs_and_decisions(planted_case)
    bad = next(d for d in decisions if d["decision"] == "duplicate")
    bad["keeper_id"] = planted_case["card"]("16_minutes_wk11.eml")
    result = dedupe.save_duplicate_plan(case_id, decisions)
    assert result["ok"] is False and "keeper" in result["error"]
    assert plans.list_plans(case_id) == []


def test_save_rejects_keeper_that_is_deleted(planted_case):
    case_id = planted_case["case_id"]
    _, decisions = _pairs_and_decisions(planted_case)
    dup = next(d for d in decisions if d["decision"] == "duplicate")
    with db.get_conn() as conn:
        conn.execute("UPDATE emails SET deleted_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (dup["keeper_id"],))
    result = dedupe.save_duplicate_plan(case_id, decisions)
    assert result["ok"] is False and "not a live email" in result["error"]


def test_save_refuses_while_a_chronology_plan_is_in_flight(planted_case):
    case_id = planted_case["case_id"]
    email_id = planted_case["card"]("10_access_cards_undated.eml")
    chrono = plans.create_plan(
        case_id, "chronology", items=[{"op": "noop"}], left_for_you=[], summary="chrono",
        touched={"emails": [email_id]},
    )
    ready = dedupe.check_case_ready_for_duplicate_review(case_id)
    assert ready["ok"] is False and chrono["id"] in ready["reason"]
    _, decisions = _pairs_and_decisions(planted_case)
    result = dedupe.save_duplicate_plan(case_id, decisions)
    assert result["ok"] is False and "chronology plan" in result["error"]


def test_ready_check_warns_about_problem_cards(planted_case):
    ready = dedupe.check_case_ready_for_duplicate_review(planted_case["case_id"])
    assert ready["ok"] is True
    assert any("chronology problems" in w for w in ready["warnings"])


def test_ready_check_for_missing_case():
    ready = dedupe.check_case_ready_for_duplicate_review(424242)
    assert ready["ok"] is False and "not found" in ready["reason"]


def test_full_flow_applies_every_decision_after_approval(planted_case):
    case_id = planted_case["case_id"]
    _, decisions = _pairs_and_decisions(planted_case)
    saved = dedupe.save_duplicate_plan(case_id, decisions)
    waiting = dedupe.apply_duplicate_plan(case_id, saved["plan_id"])
    assert waiting["status"] == "waiting_for_approval"

    plan = plans.get_plan(saved["plan_id"])
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    applied = dedupe.apply_duplicate_plan(case_id, saved["plan_id"])
    assert applied["status"] == "applied"
    assert len(applied["receipt"]["event_ids"]) == 6
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT status, canonical_email_id, duplicate_email_id, email_a_id, email_b_id FROM email_duplicate_candidates WHERE case_id = ?",
            (case_id,),
        ).fetchall()
        actions = [
            r["action"] for r in conn.execute(
                "SELECT action FROM audit_events WHERE id IN (%s)" % ",".join("?" * 6), applied["receipt"]["event_ids"]
            )
        ]
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["dissimilar", "dissimilar", "duplicate", "duplicate", "duplicate", "duplicate", "pending"]
    for r in rows:
        if r["status"] == "duplicate":
            assert {r["canonical_email_id"], r["duplicate_email_id"]} == {r["email_a_id"], r["email_b_id"]}
    assert set(actions) == {"email.duplicate_reviewed"}

    again = dedupe.list_pending_duplicate_pairs(case_id)
    assert len(again) == 1  # only the pair left for the user


def test_nothing_to_do_when_no_pairs_are_pending(planted_case):
    case_id = planted_case["case_id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE email_duplicate_candidates SET status = 'dissimilar' WHERE case_id = ?", (case_id,))
    result = dedupe.save_duplicate_plan(case_id, [])
    assert result["ok"] is True and result["plan_id"] is None
    assert "nothing to do" in result["summary"].lower()


def test_plan_goes_stale_if_a_pair_is_decided_in_the_ui_first(planted_case):
    case_id = planted_case["case_id"]
    _, decisions = _pairs_and_decisions(planted_case)
    saved = dedupe.save_duplicate_plan(case_id, decisions)
    plan = plans.get_plan(saved["plan_id"])
    approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])
    first_item = plan["items"][0]
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE email_duplicate_candidates SET status = 'dissimilar' WHERE id = ?", (first_item["candidate_id"],)
        )
    result = dedupe.apply_duplicate_plan(case_id, saved["plan_id"])
    assert result["code"] == "stale"
    with db.get_conn() as conn:
        decided = conn.execute(
            "SELECT COUNT(*) FROM email_duplicate_candidates WHERE case_id = ? AND status != 'pending'", (case_id,)
        ).fetchone()[0]
    assert decided == 1  # only the UI change


def test_decisions_are_plain_json(planted_case):
    _, decisions = _pairs_and_decisions(planted_case)
    json.dumps(decisions)


# --- review findings 2026-11-22 ------------------------------------------------------------


def _pair(reasons, senders=("a@x.example", "a@x.example"), days=0.0):
    labels = {
        "same_body_hash": "Same normalized body",
        "same_message_id": "Same Message-ID",
        "same_body_different_headers": "Same normalized body, different sender or date",
    }
    return {
        "candidate_id": 1,
        "email_a": {"id": 1},
        "email_b": {"id": 2},
        "reasons": list(reasons),
        "reason_labels": [labels.get(r, r) for r in reasons],
        "suggested_keeper_id": 1,
        "sender_addresses": list(senders),
        "days_apart": days,
        "own_text_similarity": 1.0,
    }


TEMPLATE = (
    "Please send the outstanding payment confirmation by close of business.\n"
    "This automated reminder is sent by the accounts team."
)


def test_scorer_does_not_call_a_template_body_from_another_sender_exact():
    """Upstream emailchrono-local PR #1 (Codex F14): same text, different sender or date."""
    from app.services.duplicates import duplicate_score

    left = {"from_addr": "Accounts <a@vendor-a.example>", "date_utc": "2024-03-01T09:00:00+00:00", "body_text": TEMPLATE}
    right = {"from_addr": "Billing <b@vendor-b.example>", "date_utc": "2024-04-01T09:00:00+00:00", "body_text": TEMPLATE}
    _, reasons = duplicate_score(left, right)
    assert [r["code"] for r in reasons] == ["same_body_different_headers"]


def test_same_text_from_different_senders_is_proposed_as_not_a_duplicate():
    """Different sender addresses or dates far apart: different emails, such as a
    repeated template. The plan says the text is the same, so the person sees why."""
    for pair in (
        _pair(["same_body_different_headers"], senders=("a@x.example", "b@y.example")),
        _pair(["same_body_different_headers"], days=30.0),
    ):
        decision = dedupe.propose_duplicate_decision(pair)
        assert decision["decision"] == "dissimilar"
        assert "Same normalized body, different sender or date" in decision["evidence"]
        assert "same text" in decision["why"].lower()


def test_same_text_a_few_hours_apart_is_left_for_the_person():
    """Same sender, time off by hours: may be one email read twice (a quoted copy
    whose time was read in the wrong time zone)."""
    decision = dedupe.propose_duplicate_decision(_pair(["same_body_different_headers"], days=0.15))
    assert decision["decision"] == "left_for_you"
    assert "same email" in decision["why"].lower()


def test_same_body_same_sender_same_day_is_still_a_duplicate():
    assert dedupe.propose_duplicate_decision(_pair(["same_body_hash"]))["decision"] == "duplicate"


def test_same_message_id_is_a_duplicate_even_if_headers_differ():
    decision = dedupe.propose_duplicate_decision(
        _pair(["same_message_id", "same_body_hash"], senders=("a@x.example", "b@y.example"))
    )
    assert decision["decision"] == "duplicate"
