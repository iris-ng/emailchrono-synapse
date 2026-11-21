"""When may a value come from another email? (docs: inference-rules design)

A value is borrowed only through a relation code has established: the email is
quoted in a reply, or forwarded. With no such relation the field is left as it
is and the plan says why. Code computes the borrowed value; the model's proposal
must agree with it.
"""

import pytest

from app.review import chronology
from tests import chronology_stub as stub  # same module object the stub hook imports

MARY = "Mary Lim <mary.lim@limarchitects.example>"
JOHN = "John Tan <john.tan@harbourview.example>"
WEI = "Wei Ling Koh <weiling.koh@harbourview.example>"
DAVID = "David Brooks <david.brooks@brooks-acoustics.example>"


# --- the relation ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, relation",
    [
        ("On Tue, Mar 12, 2024 at 9:14 AM, John Tan <john.tan@harbourview.example> wrote:", "reply"),
        ("Le mar. 12 mars 2024 à 18:45, Claire Martin <claire@x.example> a écrit :", "reply"),
        ("Am Mi., 13. März 2024 um 10:30 Uhr schrieb Hans Weber <hans@x.example>:", "reply"),
        ("-----Original Message-----\nFrom: A <a@x.example>\nSent: Monday, March 18, 2024 10:00 AM", "reply"),
        ("---------- Forwarded message ---------\nFrom: A <a@x.example>\nDate: Mon, 18 Mar 2024", "forward"),
        ("Begin forwarded message:\n\nFrom: A <a@x.example>", "forward"),
        ("Mary Lim <mary.lim@limarchitects.example>\tFri, Mar 15, 2024 at 9:47 AM", "unknown"),
        ("From: A <a@x.example>\nSent: Monday, March 18, 2024 10:00 AM\nTo: B <b@x.example>", "unknown"),
        ("Hello, see below.", "unknown"),
        ("", "unknown"),
    ],
)
def test_relation_comes_from_the_marker_kind(text, relation):
    found = chronology.relation_of(text)
    assert found["relation"] == relation
    if relation != "unknown":
        assert found["marker"] and found["marker"] in text


# --- subject ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "relation, above, position, expected",
    [
        ("reply", "Re: Cost plan v2", "first", "Cost plan v2"),
        ("reply", "AW: Deckenlast / ceiling load", "first", "Deckenlast / ceiling load"),
        ("reply", "RE: Re: Cost plan v2", "first", "Re: Cost plan v2"),  # strips one only
        ("reply", "Re: Cost plan v2", "middle", "Re: Cost plan v2"),
        ("forward", "Fwd: Acoustic report", "first", "Acoustic report"),
        ("forward", "FW: Acoustic report", "middle", "Acoustic report"),
    ],
)
def test_borrowed_subject_follows_the_relation(relation, above, position, expected):
    value, why = chronology.borrowed_subject(relation, above, position)
    assert value == expected and why == ""


@pytest.mark.parametrize(
    "relation, above, position, needle",
    [
        ("unknown", "Landlord approval thread", "first", "attached"),
        (None, "Re: Anything", "first", "not quoted"),
        ("reply", "Cost plan v2", "first", "Re:"),  # reply subject without a reply prefix
        ("reply", "Re: Cost plan v2", "unknown", "thread"),
        ("reply", "", "first", "no subject"),
        ("forward", "Acoustic report", "first", "Fwd:"),
    ],
)
def test_borrowed_subject_is_refused_without_a_sound_relation(relation, above, position, needle):
    value, why = chronology.borrowed_subject(relation, above, position)
    assert value is None and needle in why


# --- recipients ------------------------------------------------------------------------


def test_reply_and_forward_give_the_sender_above():
    assert chronology.borrowed_recipient("reply", WEI, DAVID) == (WEI, "")
    assert chronology.borrowed_recipient("forward", WEI, DAVID) == (WEI, "")


@pytest.mark.parametrize(
    "relation, above, own, needle",
    [
        ("unknown", MARY, JOHN, "attached"),
        (None, MARY, JOHN, "not quoted"),
        ("forward", WEI, "Wei Ling <WEILING.KOH@harbourview.example>", "own sender"),  # forwarding her own email
        ("reply", "Mary Lim", JOHN, "address"),
    ],
)
def test_borrowed_recipient_is_refused_without_a_sound_relation(relation, above, own, needle):
    value, why = chronology.borrowed_recipient(relation, above, own)
    assert value is None and needle in why


# --- time zone -------------------------------------------------------------------------


def _c(email_id, date_utc, offset):
    return {"email_id": email_id, "subject": f"s{email_id}", "date_raw": f"d{email_id}", "date_utc": date_utc, "offset": offset}


def test_one_offset_for_the_sender_is_used_even_far_away():
    from datetime import datetime, timezone

    near = datetime(2024, 9, 1, tzinfo=timezone.utc)
    picked = chronology.pick_sender_offset([_c(1, "2024-02-01T00:00:00+00:00", 60), _c(2, "2024-03-01T00:00:00+00:00", 60)], near)
    assert picked["candidate"]["email_id"] == 2


def test_disagreeing_offsets_near_the_email_leave_the_zone_open():
    from datetime import datetime, timezone

    near = datetime(2024, 3, 25, tzinfo=timezone.utc)
    picked = chronology.pick_sender_offset([_c(1, "2024-03-20T00:00:00+00:00", 60), _c(2, "2024-04-05T00:00:00+00:00", 120)], near)
    assert picked["ambiguous"] is True and "+0100" in picked["why"] and "+0200" in picked["why"]


def test_agreeing_offsets_near_the_email_win_over_a_different_one_far_away():
    from datetime import datetime, timezone

    near = datetime(2024, 7, 1, tzinfo=timezone.utc)
    picked = chronology.pick_sender_offset(
        [_c(1, "2024-01-10T00:00:00+00:00", 60), _c(2, "2024-06-20T00:00:00+00:00", 120), _c(3, "2024-07-15T00:00:00+00:00", 120)], near
    )
    assert picked["candidate"]["offset"] == 120 and picked["candidate"]["email_id"] == 2


def test_different_offsets_all_far_away_leave_the_zone_open():
    from datetime import datetime, timezone

    near = datetime(2024, 9, 1, tzinfo=timezone.utc)
    picked = chronology.pick_sender_offset([_c(1, "2024-01-10T00:00:00+00:00", 60), _c(2, "2024-05-20T00:00:00+00:00", 120)], near)
    assert picked["ambiguous"] is True


def test_no_dated_email_from_the_sender_gives_none():
    assert chronology.pick_sender_offset([], None) is None


# --- on the planted case, with what the live model actually proposed ---------------------


@pytest.fixture()
def model(monkeypatch):
    monkeypatch.setenv(chronology.MODEL_STUB_ENV, "tests.chronology_stub:respond")
    stub.OVERRIDES.clear()
    stub.CALLS.clear()
    yield stub
    stub.OVERRIDES.clear()


def _default_answer(text):
    return {"parts": [stub._answer(index, block) for index, block in stub._parts(text)]}


def _rejected(part, field):
    return [r for r in part["rejected"] if r["field"] == field]


def test_pasted_print_blocks_get_no_borrowed_subject(planted_case, model):
    """Seen live (2 runs): the outer subject was copied onto both pasted emails.
    (Their recipients are read from the pasted header lines by emailchrono's own
    split parser, so no recipient needs borrowing here.)"""
    card = planted_case["card"]("07_landlord_print.eml")

    def misled(text):
        answer = _default_answer(text)
        for part in answer["parts"][1:]:
            part["subject"] = {"value": "Landlord approval thread", "evidence": "Parsed subject: Landlord approval thread"}
        return answer

    model.OVERRIDES["fields"] = misled
    proposal = chronology.propose_fields(card, chronology.propose_splits(card))
    for part in proposal["parts"][1:]:
        assert "subject" not in {c["field"] for c in part["changes"]}
        assert part["after"]["subject"] in ("", "(no subject)")
        assert any("attached" in r["why"] for r in _rejected(part, "subject"))
    assert "Attached to the email above as: unknown" in model.CALLS[-1]["text"]


def test_a_quoted_email_never_gets_its_own_sender_as_recipient(planted_case, model):
    """Seen live (1 run): David Brooks's quoted email was given David as its recipient."""
    card = planted_case["card"]("08_acoustic_reply.eml", 1)

    def misled(text):
        answer = _default_answer(text)
        answer["parts"][0]["recipients"] = {"value": [DAVID], "evidence": f"Parsed sender: {DAVID}"}
        return answer

    model.OVERRIDES["fields"] = misled
    [part] = chronology.propose_fields(card, None)["parts"]
    assert "to" not in {c["field"] for c in part["changes"]}
    [why] = [r["why"] for r in _rejected(part, "to")]
    assert "Wei Ling Koh" in why  # the reply rule's answer is shown for the person to choose


def test_reply_quote_still_gets_the_replier_and_the_subject_with_the_rule_as_evidence(planted_case, model):
    [part] = chronology.propose_fields(planted_case["card"]("01_drawings_reply.eml", 1), None)["parts"]
    changes = {c["field"]: c for c in part["changes"]}
    assert changes["to"]["new"] == [MARY] and changes["to"]["inferred"] == ["inferred_to"]
    assert "reply" in changes["to"]["evidence"] and "wrote:" in changes["to"]["evidence"]
    assert changes["subject"]["new"] == "Revised drawings" and changes["subject"]["inferred"] == ["inferred_subject"]
    assert "first email of its thread" in changes["subject"]["evidence"]
