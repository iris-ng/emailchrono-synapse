"""Chronology plans: build, save, approve, apply. Scored against the planted case answer key."""

import io
import json

import pytest
from openpyxl import load_workbook

from app import db
from app.repos import emails as email_repo
from app.review import approval, chronology, plans, problems
from app.services import tags as tag_service
from app.services import transfer
from tests import chronology_stub as stub

GT = stub.TRUTH
JOHN = "John Tan <john.tan@harbourview.example>"
MARY = "Mary Lim <mary.lim@limarchitects.example>"


@pytest.fixture()
def model(monkeypatch):
    monkeypatch.setenv(chronology.MODEL_STUB_ENV, "tests.chronology_stub:respond")
    stub.OVERRIDES.clear()
    stub.CALLS.clear()
    yield stub
    stub.OVERRIDES.clear()


def _proposals(case_id):
    proposals = []
    for card in chronology.list_chronology_problem_cards(case_id):
        split = chronology.propose_splits(card["email_id"]) if "merged" in card["problems"] else None
        proposals.append(chronology.propose_fields(card["email_id"], split))
    return proposals


def _build(case_id):
    ready = chronology.check_case_ready_for_chronology_review(case_id)
    assert ready["ok"], ready
    return chronology.save_chronology_plan(case_id, _proposals(case_id), ready["warnings"])


def _where(planted_case, email_id):
    card = planted_case["cards"][email_id]
    return card["file"], card["chain_position"]


def _approve(plan_id):
    plan = plans.get_plan(plan_id)
    approval.approve_plan(plan_id, via="web", items_hash=plan["items_hash"])


def _row(email_id):
    with db.get_conn() as conn:
        return conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()


def _flags(email_id):
    with db.get_conn() as conn:
        return {
            r["flag"]: json.loads(r["detail_json"])
            for r in conn.execute("SELECT flag, detail_json FROM email_flags WHERE email_id = ?", (email_id,))
        }


def _parts_of(source_id):
    with db.get_conn() as conn:
        return conn.execute(
            """
            SELECT e.* FROM email_snip_parts p JOIN email_snips s ON s.id = p.snip_id
            JOIN emails e ON e.id = p.email_id
            WHERE s.source_email_id = ? ORDER BY p.part_index
            """,
            (source_id,),
        ).fetchall()


# --- plan ------------------------------------------------------------------------------


def test_plan_matches_the_answer_key(planted_case, model):
    result = _build(planted_case["case_id"])
    assert result["ok"] and result["plan_id"].startswith("rp_"), result
    plan = plans.get_plan(result["plan_id"])
    splits = {_where(planted_case, i["email_id"])[0]: i for i in plan["items"] if i["op"] == "split"}
    fields = {_where(planted_case, i["email_id"]): i for i in plan["items"] if i["op"] == "set_fields"}
    assert set(splits) == {"02_lighting_fr.eml", "04_structure_de.eml", "07_landlord_print.eml"}
    assert set(fields) == {
        ("01_drawings_reply.eml", 1), ("06_costplan_v2_reply.eml", 1), ("08_acoustic_reply.eml", 1),
        ("11_drawings_reply_copy.eml", 1), ("13_fwd_acoustic.eml", 1),
    }
    for file, item in splits.items():
        keys = GT["files"][file]["order"]
        assert len(item["parts"]) == len(keys)
        for part, key in zip(item["parts"], keys):
            truth = GT["messages"][key]
            assert part["after"]["from_addr"] == truth["from"], (file, key)
            assert part["after"]["date_utc"] == truth["date_utc"], (file, key)
            assert part["after"]["first_line"] == truth["first_line"], (file, key)
            assert part["after"]["to"] == truth["to"], (file, key)
    for where, item in fields.items():
        truth = GT["messages"][planted_case["cards"][item["email_id"]]["keys"][0]]
        assert item["after"]["from_addr"] == truth["from"], where
        assert item["after"]["date_utc"] == truth["date_utc"], where
    assert [_where(planted_case, e["email_id"]) for e in plan["left_for_you"]] == [("10_access_cards_undated.eml", 0)]


def test_plan_summary_says_it_waits_for_approval(planted_case, model):
    result = _build(planted_case["case_id"])
    assert "waiting for approval" in result["summary"].lower()
    assert result["counts"] == {"split": 3, "set_fields": 5, "left_for_you": 1}


def test_saving_a_plan_changes_no_case_data(planted_case, model):
    with db.get_conn() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM emails ORDER BY id")]
        flags_before = conn.execute("SELECT COUNT(*) FROM email_flags").fetchone()[0]
    _build(planted_case["case_id"])
    with db.get_conn() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM emails ORDER BY id")] == before
        assert conn.execute("SELECT COUNT(*) FROM email_flags").fetchone()[0] == flags_before


def test_save_rebuilds_items_from_the_database_not_from_the_caller(planted_case, model):
    case_id = planted_case["case_id"]
    proposals = _proposals(case_id)
    target = planted_case["card"]("01_drawings_reply.eml", 1)
    tampered = next(p for p in proposals if p["email_id"] == target)
    for change in tampered["parts"][0]["changes"]:
        change["new"] = "tampered"
    tampered["parts"][0]["after"]["date_utc"] = "1999-01-01T00:00:00+00:00"
    result = chronology.save_chronology_plan(case_id, proposals, [])
    item = next(i for i in plans.get_plan(result["plan_id"])["items"] if i["email_id"] == target)
    assert item["after"]["date_utc"] == "2024-03-12T01:14:00+00:00"
    assert item["after"]["from_addr"] == JOHN


def test_save_rechecks_a_tampered_model_answer(planted_case, model):
    case_id = planted_case["case_id"]
    proposals = _proposals(case_id)
    target = planted_case["card"]("01_drawings_reply.eml", 1)
    tampered = next(p for p in proposals if p["email_id"] == target)
    tampered["model_output"]["parts"][0]["date"] = {"quote": "Tue, Mar 12, 2024 at 11:59 PM", "local_iso": "2024-03-12T23:59"}
    result = chronology.save_chronology_plan(case_id, proposals, [])
    plan = plans.get_plan(result["plan_id"])
    item = next(i for i in plan["items"] if i["email_id"] == target)
    assert "date_utc" not in {c["field"] for c in item["changes"]}
    assert any(e["email_id"] == target and "not in the card text" in e["why"] for e in plan["left_for_you"])


def test_save_rejects_a_card_from_another_case(planted_case, model):
    result = chronology.save_chronology_plan(planted_case["case_id"], [{"email_id": 999999}], [])
    assert result["ok"] is False and "999999" in result["error"]
    assert plans.list_plans(planted_case["case_id"]) == []


def test_nothing_to_do_without_proposals(planted_case, model):
    result = chronology.save_chronology_plan(planted_case["case_id"], [], [])
    assert result["ok"] is True and result["plan_id"] is None
    assert "nothing to do" in result["summary"].lower()


def test_ready_check_warns_that_a_waiting_duplicate_plan_will_go_stale(planted_case, model):
    case_id = planted_case["case_id"]
    dup = plans.create_plan(case_id, "duplicates", items=[{"op": "noop"}], left_for_you=[], summary="dup", touched={})
    ready = chronology.check_case_ready_for_chronology_review(case_id)
    assert ready["ok"] is True
    assert any(dup["id"] in warning for warning in ready["warnings"])


def test_ready_check_for_a_missing_case():
    ready = chronology.check_case_ready_for_chronology_review(424242)
    assert ready["ok"] is False and "not found" in ready["reason"]


# --- apply -------------------------------------------------------------------------------


def test_apply_waits_for_approval(planted_case, model):
    case_id = planted_case["case_id"]
    result = _build(case_id)
    waiting = chronology.apply_chronology_plan(case_id, result["plan_id"])
    assert waiting["status"] == "waiting_for_approval" and waiting["changed"] is False


def test_apply_splits_sets_fields_and_marks_inferred_values(planted_case, model):
    case_id = planted_case["case_id"]
    result = _build(case_id)
    _approve(result["plan_id"])
    applied = chronology.apply_chronology_plan(case_id, result["plan_id"])
    assert applied["status"] == "applied", applied
    assert applied["receipt"]["item_count"] == 8 and applied["receipt"]["event_ids"]

    # Merged cards are superseded by their parts, which carry the checked fields.
    french = planted_case["card"]("02_lighting_fr.eml")
    assert _row(french)["superseded_at"] is not None
    top, hidden = _parts_of(french)
    assert top["from_addr"] == MARY and top["date_utc"] == "2024-03-13T01:30:00+00:00"
    assert hidden["from_addr"] == "Claire Martin <claire.martin@atelier-lumiere.example>"
    assert hidden["date_utc"] == "2024-03-12T17:45:00+00:00"
    assert json.loads(hidden["to_json"]) == [MARY]
    hidden_flags = _flags(hidden["id"])
    assert {"inferred_date", "inferred_timezone", "inferred_to", "inferred_subject", "ai_reviewed"} <= set(hidden_flags)
    assert hidden_flags["inferred_to"]["evidence"]

    # Field fixes on an undated quoted card.
    quoted = planted_case["card"]("01_drawings_reply.eml", 1)
    row = _row(quoted)
    assert row["from_addr"] == JOHN and row["date_utc"] == "2024-03-12T01:14:00+00:00"
    assert row["date_raw"] == "Tue, Mar 12, 2024 at 9:14 AM"
    assert json.loads(row["to_json"]) == [MARY] and row["subject"] == "Revised drawings"
    flags = _flags(quoted)
    assert {"inferred_to", "inferred_subject", "inferred_timezone", "ai_reviewed"} <= set(flags)
    assert "inferred_from" not in flags and "inferred_date" not in flags
    with db.get_conn() as conn:
        tag_names = [t["name"] for t in tag_service.tags_for_emails(conn, [quoted]).get(quoted, [])]
    assert "Undated" not in tag_names

    # The Sydney-time quote now sits before the reply above it.
    assert _row(planted_case["card"]("08_acoustic_reply.eml", 1))["date_utc"] == "2024-03-18T00:30:00+00:00"


def test_after_apply_only_the_undated_access_card_is_left(planted_case, model):
    case_id = planted_case["case_id"]
    result = _build(case_id)
    _approve(result["plan_id"])
    chronology.apply_chronology_plan(case_id, result["plan_id"])
    left = [p["email_id"] for p in problems.find_problem_cards(case_id)]
    assert left == [planted_case["card"]("10_access_cards_undated.eml")]


def test_apply_recomputes_duplicates_for_new_parts(planted_case, model):
    case_id = planted_case["case_id"]
    result = _build(case_id)
    _approve(result["plan_id"])
    chronology.apply_chronology_plan(case_id, result["plan_id"])
    landlord_parts = _parts_of(planted_case["card"]("07_landlord_print.eml"))
    approval_part = next(p for p in landlord_parts if p["date_utc"] == "2024-03-15T03:02:00+00:00")
    standalone = {planted_case["card"]("12a_landlord_approval.eml"), planted_case["card"]("12b_landlord_approval_resaved.eml")}
    with db.get_conn() as conn:
        linked = {
            r["email_a_id"] if r["email_b_id"] == approval_part["id"] else r["email_b_id"]
            for r in conn.execute(
                "SELECT * FROM email_duplicate_candidates WHERE status = 'pending' AND ? IN (email_a_id, email_b_id)",
                (approval_part["id"],),
            )
        }
    assert linked & standalone


def test_apply_twice_changes_nothing_more(planted_case, model):
    case_id = planted_case["case_id"]
    result = _build(case_id)
    _approve(result["plan_id"])
    chronology.apply_chronology_plan(case_id, result["plan_id"])
    again = chronology.apply_chronology_plan(case_id, result["plan_id"])
    assert again["already_applied"] is True and again["changed"] is False


def test_injected_failure_rolls_everything_back(planted_case, model, monkeypatch):
    case_id = planted_case["case_id"]
    result = _build(case_id)
    _approve(result["plan_id"])
    with db.get_conn() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM emails ORDER BY id")]
        flags_before = conn.execute("SELECT COUNT(*) FROM email_flags").fetchone()[0]
    monkeypatch.setenv(plans.FAIL_AFTER_ENV, "4")
    failed = chronology.apply_chronology_plan(case_id, result["plan_id"])
    assert failed["code"] == "apply_failed" and failed["changed"] is False
    with db.get_conn() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM emails ORDER BY id")] == before
        assert conn.execute("SELECT COUNT(*) FROM email_flags").fetchone()[0] == flags_before


def test_card_edited_after_planning_makes_the_plan_stale(planted_case, model):
    case_id = planted_case["case_id"]
    result = _build(case_id)
    _approve(result["plan_id"])
    email_repo.update_email(planted_case["card"]("01_drawings_reply.eml", 1), {"subject": "Edited by hand"})
    stale = chronology.apply_chronology_plan(case_id, result["plan_id"])
    assert stale["code"] == "stale"
    assert _parts_of(planted_case["card"]("02_lighting_fr.eml")) == []


# --- inferred marks after apply -----------------------------------------------------------


@pytest.fixture()
def applied_case(planted_case, model):
    result = _build(planted_case["case_id"])
    _approve(result["plan_id"])
    assert chronology.apply_chronology_plan(planted_case["case_id"], result["plan_id"])["status"] == "applied"
    return planted_case


def test_editing_a_field_by_hand_clears_only_its_inferred_mark(applied_case):
    quoted = applied_case["card"]("01_drawings_reply.eml", 1)
    email_repo.update_email(quoted, {"to": [MARY, "Ann Other <ann@x.example>"]})
    flags = _flags(quoted)
    assert "inferred_to" not in flags
    assert "inferred_subject" in flags and "inferred_timezone" in flags


def test_editing_the_date_by_hand_clears_both_date_marks(applied_case):
    hidden = _parts_of(applied_case["card"]("02_lighting_fr.eml"))[1]
    email_repo.update_email(hidden["id"], {"date_utc": "2024-03-12T17:46:00+00:00"})
    flags = _flags(hidden["id"])
    assert "inferred_date" not in flags and "inferred_timezone" not in flags
    assert "inferred_to" in flags


def test_cards_from_the_api_list_inferred_fields_with_evidence(applied_case):
    quoted = applied_case["card"]("01_drawings_reply.eml", 1)
    card = next(e for e in email_repo.list_emails(applied_case["case_id"]) if e["id"] == quoted)
    assert set(card["inferred"]) == {"to", "subject", "timezone"}
    assert "Mary Lim" in card["inferred"]["to"]


def test_case_export_round_trip_keeps_inferred_marks(applied_case):
    quoted = applied_case["card"]("01_drawings_reply.eml", 1)
    data = transfer.export_case(applied_case["case_id"])
    imported = transfer.import_bundle(data)
    with db.get_conn() as conn:
        copy = conn.execute(
            "SELECT id FROM emails WHERE case_id = ? AND date_utc = ? AND from_addr = ? AND source_kind = 'quoted'",
            (imported["id"], "2024-03-12T01:14:00+00:00", JOHN),
        ).fetchall()
    assert copy
    for row in copy:
        assert {"inferred_to", "inferred_subject", "inferred_timezone"} <= set(_flags(row["id"]))
    assert _flags(quoted)  # the original keeps its marks too


def test_excel_export_has_an_inferred_fields_column(applied_case):
    import zipfile

    data = transfer.export_case(applied_case["case_id"])
    sheet = load_workbook(io.BytesIO(zipfile.ZipFile(io.BytesIO(data)).read("chronology.xlsx"))).active
    header = [cell.value for cell in sheet[1]]
    assert "Inferred fields" in header
    column = header.index("Inferred fields")
    subjects = header.index("Subject")
    values = {row[subjects]: row[column] for row in sheet.iter_rows(min_row=2, values_only=True)}
    assert "to" in values["Revised drawings"] and "time zone" in values["Revised drawings"]
    assert not values["Cost plan v3"]


# --- review findings 2026-11-22 ------------------------------------------------------------


def test_split_keeps_the_source_cards_notes_and_star_on_part_one(planted_case, model):
    """Codex F03: a split used to blank notes and importance on every part. Manual Snip
    keeps them on part 1, and so must an AI split, including a note added after planning."""
    case_id = planted_case["case_id"]
    french = planted_case["card"]("02_lighting_fr.eml")
    result = _build(case_id)
    email_repo.update_email(french, {"notes": "Check the lux figures", "important": True})
    _approve(result["plan_id"])
    applied = chronology.apply_chronology_plan(case_id, result["plan_id"])
    assert applied["status"] == "applied", applied
    top, hidden = _parts_of(french)
    assert top["notes"] == "Check the lux figures" and top["important"] == 1
    assert hidden["notes"] == "" and hidden["important"] == 0


def test_changing_the_case_time_zone_makes_the_plan_stale(planted_case, model):
    """Codex F07: undated cards get dates worked out in the case time zone."""
    from app.repos import cases as case_repo

    case_id = planted_case["case_id"]
    result = _build(case_id)
    _approve(result["plan_id"])
    case_repo.update_case(case_id, default_tz="America/New_York")
    stale = chronology.apply_chronology_plan(case_id, result["plan_id"])
    assert stale["code"] == "stale", stale
    assert _parts_of(planted_case["card"]("02_lighting_fr.eml")) == []


def test_trashing_the_case_stops_an_approved_plan(planted_case, model):
    """Codex F07: an approved plan must not change a case that was moved to the trash."""
    from app.repos import cases as case_repo

    case_id = planted_case["case_id"]
    result = _build(case_id)
    _approve(result["plan_id"])
    assert case_repo.soft_delete_case(case_id)
    refused = chronology.apply_chronology_plan(case_id, result["plan_id"])
    assert refused.get("status") != "applied", refused
    assert _parts_of(planted_case["card"]("02_lighting_fr.eml")) == []
