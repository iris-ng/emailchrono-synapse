"""AI chronology proposals (propose_splits, propose_fields), with a scripted model."""

import pytest

from app import db
from app.review import chronology
from tests import chronology_stub as stub

FR_LINE = "Le mar. 12 mars 2024 à 18:45, Claire Martin <claire.martin@atelier-lumiere.example> a écrit :"
JOHN = "John Tan <john.tan@harbourview.example>"
MARY = "Mary Lim <mary.lim@limarchitects.example>"


@pytest.fixture()
def model(monkeypatch):
    monkeypatch.setenv(chronology.MODEL_STUB_ENV, "tests.chronology_stub:respond")
    stub.OVERRIDES.clear()
    stub.CALLS.clear()
    yield stub
    stub.OVERRIDES.clear()


def _changes(part):
    return {change["field"]: change for change in part["changes"]}


def _body(email_id):
    with db.get_conn() as conn:
        return conn.execute("SELECT body_text FROM emails WHERE id = ?", (email_id,)).fetchone()["body_text"]


# --- propose_splits --------------------------------------------------------------


def test_split_offsets_come_from_the_models_exact_line(planted_case, model):
    email_id = planted_case["card"]("02_lighting_fr.eml")
    split = chronology.propose_splits(email_id)
    assert split["ok"] is True, split
    assert split["split_lines"] == [FR_LINE]
    assert split["offsets"] == [_body(email_id).replace("\r\n", "\n").index(FR_LINE)]
    assert [p["first_line"] for p in split["parts"]] == ["Dear Claire,", "Bonjour Mary,"]


def test_split_hints_list_the_markers_the_parser_missed(planted_case, model):
    chronology.propose_splits(planted_case["card"]("07_landlord_print.eml"))
    prompt = model.CALLS[-1]["text"]
    assert "Fri, Mar 15, 2024 at 11:02 AM" in prompt.split("Likely header lines")[1]


def test_split_line_not_in_the_card_is_rejected(planted_case, model):
    model.OVERRIDES["split"] = lambda text: {"split_lines": ["Le 12 mars, Claire a écrit :"], "note": ""}
    split = chronology.propose_splits(planted_case["card"]("02_lighting_fr.eml"))
    assert split["ok"] is False and split["offsets"] == []
    assert "not found" in split["reason"]


def test_split_reports_a_model_error_without_raising(planted_case, model):
    def boom(text):
        raise RuntimeError("API timeout")

    model.OVERRIDES["split"] = boom
    split = chronology.propose_splits(planted_case["card"]("02_lighting_fr.eml"))
    assert split["ok"] is False and "API timeout" in split["reason"]


def test_split_with_no_hidden_emails(planted_case, model):
    split = chronology.propose_splits(planted_case["card"]("14_costplan_v3.eml"))
    assert split["ok"] is False and split["split_lines"] == []


# --- propose_fields on one card --------------------------------------------------


def test_undated_gmail_quote_gets_sender_date_and_inferred_fields(planted_case, model):
    email_id = planted_case["card"]("01_drawings_reply.eml", 1)
    proposal = chronology.propose_fields(email_id, None)
    assert proposal["ok"] is True, proposal
    [part] = proposal["parts"]
    changes = _changes(part)
    assert changes["from_addr"]["new"] == JOHN and changes["from_addr"]["inferred"] == []
    assert changes["to"]["new"] == [MARY] and changes["to"]["inferred"] == ["inferred_to"]
    assert changes["subject"]["new"] == "Revised drawings" and changes["subject"]["inferred"] == ["inferred_subject"]
    date = changes["date_utc"]
    assert date["new"] == "2024-03-12T01:14:00+00:00"
    assert date["inferred"] == ["inferred_timezone"]
    assert date["quote"] == "Tue, Mar 12, 2024 at 9:14 AM"
    john_dated = planted_case["card"]("04_structure_de.eml")
    assert f"#{john_dated}" in date["evidence"] or "+0800" in date["evidence"]
    assert proposal["left_for_you"] == []


def test_ai_read_date_is_marked_inferred_date(planted_case, model):
    proposal = chronology.propose_fields(planted_case["card"]("06_costplan_v2_reply.eml", 1), None)
    changes = _changes(proposal["parts"][0])
    assert changes["date_utc"]["new"] == "2024-03-14T08:20:00+00:00"
    assert changes["date_utc"]["inferred"] == ["inferred_date", "inferred_timezone"]
    assert changes["from_addr"]["new"] == "Priya Nair <priya.nair@qsconsult.example>"
    assert changes["from_addr"]["inferred"] == ["inferred_from"]


def test_sydney_conflict_is_fixed_with_the_senders_zone(planted_case, model):
    proposal = chronology.propose_fields(planted_case["card"]("08_acoustic_reply.eml", 1), None)
    date = _changes(proposal["parts"][0])["date_utc"]
    assert date["old"] == "2024-03-18T03:30:00+00:00"
    assert date["new"] == "2024-03-18T00:30:00+00:00"
    assert "+1100" in date["evidence"]
    assert proposal["left_for_you"] == []


def test_conflict_is_left_for_you_when_the_zone_is_the_case_default(planted_case, model):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE emails SET date_raw = 'Fri, 08 Mar 2024 11:00:00 +0800' WHERE id = ?",
            (planted_case["card"]("09_acoustic_scope.eml"),),
        )
    proposal = chronology.propose_fields(planted_case["card"]("08_acoustic_reply.eml", 1), None)
    assert "date_utc" not in _changes(proposal["parts"][0])
    assert len(proposal["left_for_you"]) == 1
    assert "time zone" in proposal["left_for_you"][0]["why"]


def test_card_with_no_date_anywhere_is_left_for_you(planted_case, model):
    proposal = chronology.propose_fields(planted_case["card"]("10_access_cards_undated.eml"), None)
    assert proposal["parts"][0]["changes"] == []
    assert [entry["problem"] for entry in proposal["left_for_you"]] == ["undated"]


def test_header_dated_card_in_a_conflict_needs_no_model_call(planted_case, model):
    proposal = chronology.propose_fields(planted_case["card"]("08_acoustic_reply.eml"), None)
    assert model.CALLS == []
    assert proposal["parts"][0]["changes"] == [] and proposal["left_for_you"] == []


def test_date_quote_not_in_the_text_is_rejected(planted_case, model):
    def wrong_date(text):
        return {"parts": [{"part": 1, "sender": None, "recipients": None, "subject": None,
                           "date": {"quote": "Tue, Mar 12, 2024 at 10:00 AM", "local_iso": "2024-03-12T10:00"}, "note": ""}]}

    model.OVERRIDES["fields"] = wrong_date
    proposal = chronology.propose_fields(planted_case["card"]("01_drawings_reply.eml", 1), None)
    assert "date_utc" not in _changes(proposal["parts"][0])
    assert "not in the card text" in proposal["left_for_you"][0]["why"]


def test_ai_cannot_replace_a_parsed_sender_address(planted_case, model):
    model.OVERRIDES["fields"] = lambda text: {"parts": [{
        "part": 1, "recipients": None, "subject": None, "date": None, "note": "",
        "sender": {"value": MARY, "evidence": f"From: {MARY}"},  # real text in the card material
    }]}
    proposal = chronology.propose_fields(planted_case["card"]("01_drawings_reply.eml", 1), None)
    assert "from_addr" not in _changes(proposal["parts"][0])
    assert any("sender" in r["why"] for r in proposal["parts"][0]["rejected"])


def test_recipient_not_in_the_material_is_rejected(planted_case, model):
    model.OVERRIDES["fields"] = lambda text: {"parts": [{
        "part": 1, "sender": None, "subject": None, "date": None, "note": "",
        "recipients": {"value": ["Boss <boss@x.example>"], "evidence": "To: Boss <boss@x.example>"},
    }]}
    proposal = chronology.propose_fields(planted_case["card"]("01_drawings_reply.eml", 1), None)
    assert "to" not in _changes(proposal["parts"][0])
    assert any("not in the card" in r["why"] for r in proposal["parts"][0]["rejected"])


def test_recipient_without_an_address_is_rejected(planted_case, model):
    # Seen live: the model took "Mary" from the salutation "Hi Mary,".
    model.OVERRIDES["fields"] = lambda text: {"parts": [{
        "part": 1, "sender": None, "subject": None, "date": None, "note": "",
        "recipients": {"value": ["Mary"], "evidence": "Hi Mary,"},
    }]}
    proposal = chronology.propose_fields(planted_case["card"]("01_drawings_reply.eml", 1), None)
    assert "to" not in _changes(proposal["parts"][0])
    assert any("address" in r["why"] for r in proposal["parts"][0]["rejected"])


def test_value_in_the_material_with_the_wrong_evidence_gets_the_real_line(planted_case, model):
    # Seen live: right recipient, but the model cited "Dear Claire," as evidence.
    email_id = planted_case["card"]("02_lighting_fr.eml")

    def wrong_evidence(text):
        return {"parts": [
            {"part": 1, "sender": None, "recipients": None, "subject": None, "date": None, "note": ""},
            {"part": 2, "sender": None, "subject": None, "date": None, "note": "",
             "recipients": {"value": [MARY], "evidence": "Dear Claire,"}},
        ]}

    model.OVERRIDES["fields"] = wrong_evidence
    proposal = chronology.propose_fields(email_id, chronology.propose_splits(email_id))
    change = _changes(proposal["parts"][1])["to"]
    assert change["new"] == [MARY]
    # A borrowed value's evidence is the rule that gives it (reply marker and replier),
    # never the model's citation.
    assert MARY in change["evidence"] and FR_LINE in change["evidence"]
    assert change["inferred"] == ["inferred_to"]


def test_subject_taken_from_body_text_is_not_used(planted_case, model):
    # A subject must come from a Subject: line in the email or from its reply or
    # forward relation. A phrase from the body is a guess, so it is not used.
    model.OVERRIDES["fields"] = lambda text: {"parts": [{
        "part": 1, "sender": None, "recipients": None, "date": None, "note": "",
        "subject": {"value": "revised ceiling drawings",
                    "evidence": "Can you confirm when the revised ceiling drawings will be ready? The"},
    }]}
    proposal = chronology.propose_fields(planted_case["card"]("01_drawings_reply.eml", 1), None)
    assert "subject" not in _changes(proposal["parts"][0])
    [rejected] = [r for r in proposal["parts"][0]["rejected"] if r["field"] == "subject"]
    assert "Revised drawings" in rejected["why"]  # the reply rule's answer, for the person to choose


def test_the_model_is_told_about_a_date_conflict(planted_case, model):
    # Seen live: without this the model said the parsed date looked fine.
    chronology.propose_fields(planted_case["card"]("13_fwd_acoustic.eml", 1), None)
    prompt = model.CALLS[-1]["text"]
    assert "Problems found by code:" in prompt
    assert "dated later than the email above it" in prompt


def test_model_error_leaves_the_card_for_you(planted_case, model):
    def boom(text):
        raise RuntimeError("rate limited")

    model.OVERRIDES["fields"] = boom
    proposal = chronology.propose_fields(planted_case["card"]("01_drawings_reply.eml", 1), None)
    assert proposal["ok"] is False
    assert "rate limited" in proposal["left_for_you"][0]["why"]


# --- propose_fields on a split card ------------------------------------------------


def test_split_parts_get_their_own_sender_date_and_inferred_fields(planted_case, model):
    email_id = planted_case["card"]("02_lighting_fr.eml")
    proposal = chronology.propose_fields(email_id, chronology.propose_splits(email_id))
    assert proposal["split_lines"] == [FR_LINE]
    top, hidden = proposal["parts"]
    assert top["after"]["from_addr"] == MARY and top["after"]["date_utc"] == "2024-03-13T01:30:00+00:00"
    after = hidden["after"]
    assert after["from_addr"] == "Claire Martin <claire.martin@atelier-lumiere.example>"
    assert after["date_utc"] == "2024-03-12T17:45:00+00:00"
    assert after["to"] == [MARY]
    assert after["subject"] == "Luminaires - hall d'entrée"
    assert after["first_line"] == "Bonjour Mary,"
    assert not after["body_text"].startswith("Le mar.") and ">" not in after["body_text"]
    assert _changes(hidden)["date_utc"]["inferred"] == ["inferred_date", "inferred_timezone"]


def test_failed_split_leaves_a_merged_card_for_you_without_a_field_call(planted_case, model):
    email_id = planted_case["card"]("02_lighting_fr.eml")
    proposal = chronology.propose_fields(email_id, {"ok": False, "split_lines": [], "reason": "no lines"})
    assert model.CALLS == []
    assert proposal["split_lines"] == []
    assert proposal["left_for_you"][0]["problem"] == "merged"
