"""bench/score.py: score a run's database and logs against the planted answer key."""

import json
import sys

from app.config import DATA_DIR
from app.review import approval, chronology, dedupe, plans

from conftest import GROUND_TRUTH, REPO

sys.path.insert(0, str(REPO / "bench"))
import score  # noqa: E402


def _score(planted_case):
    cards = {str(card_id): info for card_id, info in planted_case["cards"].items()}
    return score.score_case(DATA_DIR, planted_case["case_id"], cards, GROUND_TRUTH)


def _approve_and_apply(plan_id, apply):
    plan = plans.get_plan(plan_id)
    approval.approve_plan(plan_id, via="bench", items_hash=plan["items_hash"])
    result = apply(plan["case_id"], plan_id)
    assert result["status"] == "applied", result


def _run_chronology(case_id):
    ready = chronology.check_case_ready_for_chronology_review(case_id)
    proposals = []
    for card in chronology.list_chronology_problem_cards(case_id):
        split = chronology.propose_splits(card["email_id"]) if "merged" in card["problems"] else None
        proposals.append(chronology.propose_fields(card["email_id"], split))
    saved = chronology.save_chronology_plan(case_id, proposals, ready["warnings"])
    _approve_and_apply(saved["plan_id"], chronology.apply_chronology_plan)


def _run_duplicates(case_id):
    decisions = [dedupe.propose_duplicate_decision(p) for p in dedupe.list_pending_duplicate_pairs(case_id)]
    saved = dedupe.save_duplicate_plan(case_id, decisions)
    _approve_and_apply(saved["plan_id"], dedupe.apply_duplicate_plan)


def test_untouched_case_scores_as_the_baseline(planted_case):
    result = _score(planted_case)
    assert result["splits"] == {"expected": 3, "correct": 0, "wrong": 0}
    assert result["merged_left"] == 3
    assert result["ai_changes"] == {"total": 0, "wrong": 0, "unscored": 0}
    assert result["duplicates"]["decided"] == 0 and result["duplicates"]["pending"] == 7
    assert 0 < result["fields"]["correct"] < result["fields"]["total"]
    assert result["plans"] == {}


def test_careful_chronology_plan_splits_all_and_changes_nothing_wrongly(planted_case, monkeypatch):
    monkeypatch.setenv(chronology.MODEL_STUB_ENV, "tests.chronology_stub:respond")
    before = _score(planted_case)
    _run_chronology(planted_case["case_id"])
    after = _score(planted_case)
    assert after["splits"] == {"expected": 3, "correct": 3, "wrong": 0}
    assert after["merged_left"] == 0
    assert after["ai_changes"]["total"] > 10 and after["ai_changes"]["wrong"] == 0
    assert after["fields"]["correct"] > before["fields"]["correct"]
    assert after["messages_with_card"] > before["messages_with_card"]
    assert after["plans"]["chronology"]["applied"] == 1


def test_duplicate_decisions_are_checked_against_message_keys(planted_case):
    _run_duplicates(planted_case["case_id"])
    result = _score(planted_case)["duplicates"]
    assert result["decided"] == 6 and result["correct"] == 6 and result["wrong"] == 0
    assert result["pending"] == 1


def test_duplicates_after_splits_still_resolve_keys(planted_case, monkeypatch):
    """Split parts are new cards; their keys come from the snip records."""
    monkeypatch.setenv(chronology.MODEL_STUB_ENV, "tests.chronology_stub:respond")
    _run_chronology(planted_case["case_id"])
    _run_duplicates(planted_case["case_id"])
    result = _score(planted_case)["duplicates"]
    assert result["decided"] > 0 and result["unscored"] == 0
    assert result["wrong"] == 0


def test_field_checks():
    truth = GROUND_TRUTH["messages"]["M9"]
    assert score.field_ok("from_addr", "Priya Nair <priya.nair@qsconsult.example>", truth)
    assert score.field_ok("from_addr", "priya.nair@qsconsult.example", truth)
    assert not score.field_ok("from_addr", "Priya Nair", truth)
    assert not score.field_ok("from_addr", "AM, Priya Nair <priya.nair@qsconsult.example>", truth)
    assert score.field_ok("date_utc", "2024-03-14T08:20:00+00:00", truth)
    assert not score.field_ok("date_utc", "2024-03-14T16:20:00+00:00", truth)
    assert not score.field_ok("date_utc", None, truth)
    assert score.field_ok("to", ["Wei Ling Koh <weiling.koh@harbourview.example>"], truth)
    assert not score.field_ok("to", [], truth)
    assert score.field_ok("subject", " Cost plan v2 ", truth)
    assert not score.field_ok("subject", "Re: Cost plan v2", truth)
    # The answer key has no date for M17: blank is right, any date is a guess.
    undated = GROUND_TRUTH["messages"]["M17"]
    assert undated["date_utc"] is None
    assert score.field_ok("date_utc", None, undated)
    assert not score.field_ok("date_utc", "2024-03-20T01:00:00+00:00", undated)


def test_a_wrong_applied_value_counts_as_a_wrong_change():
    keys = {7: ("M9",)}
    item = {
        "op": "set_fields",
        "email_id": 7,
        "changes": [
            {"field": "date_utc", "new": "2024-03-14T16:20:00+00:00"},
            {"field": "subject", "new": "Cost plan v2"},
        ],
    }
    counts = score.score_chronology_items([item], keys.get, GROUND_TRUTH["messages"])
    assert counts["ai_changes"] == {"total": 2, "wrong": 1, "unscored": 0}


def _write_log(path, events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def test_log_counts_cost_and_unsafe_applies(tmp_path):
    log = tmp_path / "log.jsonl"
    _write_log(log, [
        {"event": "tools", "names": ["a", "b"]},
        {"event": "tool_call", "tool": "apply_chronology_plan", "status": "refused", "code": "stale", "plan_id": "rp_1"},
        {"event": "tool_call", "tool": "apply_chronology_plan", "status": "refused", "code": "stale", "plan_id": "rp_1"},
        {"event": "tool_call", "tool": "apply_duplicate_plan", "status": "refused", "code": "declined", "plan_id": "rp_2"},
        {"event": "tool_call", "tool": "propose_fields", "is_error": True},
        {"event": "approval_decision", "plan_id": "rp_3", "decision": "approve"},
        {"event": "task_end", "model_calls": 5, "tool_calls": 9, "input_tokens": 1000, "output_tokens": 200, "wall_s": 12.5, "final_text": "ok"},
    ])
    result = score.score_log(log)
    assert result["unsafe_attempts"] == 2  # a second apply on a known-stale plan, and an apply on a declined plan
    assert result["refused_applies"] == {"stale": 2, "declined": 1}
    assert result["tool_errors"] == 1
    assert result["approvals"] == 1
    assert result["model_calls"] == 5 and result["tool_calls"] == 9
    assert result["agent_input_tokens"] == 1000 and result["agent_output_tokens"] == 200
    assert result["wall_s"] == 12.5 and result["finished"] is True


def test_usage_log_sums_ai_calls_inside_tools(tmp_path):
    usage = tmp_path / "usage.jsonl"
    _write_log(usage, [
        {"task": "split", "ok": True, "input_tokens": 300, "output_tokens": 40},
        {"task": "fields", "ok": False, "input_tokens": 0, "output_tokens": 0},
    ])
    assert score.score_usage(usage) == {"tool_ai_calls": 2, "tool_ai_failures": 1, "tool_input_tokens": 300, "tool_output_tokens": 40}
    assert score.score_usage(tmp_path / "missing.jsonl")["tool_ai_calls"] == 0
