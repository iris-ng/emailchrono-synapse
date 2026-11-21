"""Date checks for AI-read dates (design section 7, steps 1 to 4) and split-line offsets."""

from datetime import timedelta, timezone

from app import db
from app.review import chronology

PLUS_1 = timezone(timedelta(hours=1))
PLUS_8 = timezone(timedelta(hours=8))
PLUS_11 = timezone(timedelta(hours=11))


def _read(quote, iso, text=None, zone=PLUS_8):
    return chronology.read_date(quote, iso, text if text is not None else f"Header: {quote}\n", zone)


# --- step 1: the quote must be in the card text ------------------------------------


def test_quote_missing_from_the_text_is_rejected():
    result = chronology.read_date("Tue, Mar 12, 2024 at 9:14 AM", "2024-03-12T09:14", "On Monday, John wrote:", PLUS_8)
    assert result["ok"] is False and "not in the card text" in result["reason"]


def test_quote_match_ignores_line_wrapping_and_quote_marks():
    text = "On Tue, Mar 12, 2024 at\n> 9:14 AM, John wrote:"
    assert chronology.read_date("Tue, Mar 12, 2024 at 9:14 AM", "2024-03-12T09:14", text, PLUS_8)["ok"] is True


# --- step 2: the existing parser wins when it can read the quote -------------------


def test_parser_result_wins_over_the_model():
    result = _read("Tue, Mar 12, 2024 at 9:14 AM", "2024-03-12T10:14")
    assert result["ok"] is True
    assert result["method"] == "parser"
    assert result["date_utc"] == "2024-03-12T01:14:00+00:00"


def test_zone_written_in_the_quote_beats_the_given_zone():
    result = _read("Tue, 12 Mar 2024 09:14:00 +0100", "2024-03-12T09:14", zone=PLUS_8)
    assert result["date_utc"] == "2024-03-12T08:14:00+00:00"
    assert result["zone_in_quote"] is True


# --- step 3: the model's ISO date must match the quote -----------------------------


def test_french_date_read_by_the_model_is_accepted():
    result = _read("mar. 12 mars 2024 à 18:45", "2024-03-12T18:45", zone=PLUS_1)
    assert result["ok"] is True and result["method"] == "ai_checked"
    assert result["date_utc"] == "2024-03-12T17:45:00+00:00"
    assert result["zone_in_quote"] is False


def test_german_date_read_by_the_model_is_accepted():
    result = _read("Mi., 13. März 2024 um 10:30 Uhr", "2024-03-13T10:30", zone=PLUS_1)
    assert result["ok"] is True and result["date_utc"] == "2024-03-13T09:30:00+00:00"


def test_twelve_hour_time_matches_a_24_hour_iso_value():
    result = _read("Thursday 9th April 2015, 4:07 pm", "2015-04-09T16:07")
    assert result["ok"] is True and result["date_utc"] == "2015-04-09T08:07:00+00:00"


def test_wrong_month_is_rejected():
    result = _read("mar. 12 mars 2024 à 18:45", "2024-04-12T18:45", zone=PLUS_1)
    assert result["ok"] is False and "month" in result["reason"]


def test_wrong_hour_day_or_year_is_rejected():
    for iso, word in (("2024-03-12T19:45", "time"), ("2024-03-13T18:45", "day"), ("2023-03-12T18:45", "year")):
        result = _read("mar. 12 mars 2024 à 18:45", iso, zone=PLUS_1)
        assert result["ok"] is False and word in result["reason"], (iso, result)


def test_unreadable_iso_value_is_rejected():
    result = _read("mar. 12 mars 2024 à 18:45", "next Tuesday", zone=PLUS_1)
    assert result["ok"] is False and "ISO" in result["reason"]


def test_numeric_month_is_accepted_when_the_quote_has_no_month_name():
    result = _read("12.03.2024 um 18.45", "2024-03-12T18:45", zone=PLUS_1)
    assert result["ok"] is True


def test_month_name_must_win_over_a_matching_number():
    # "12" is the day here; the model must not get away with December.
    result = _read("mar. 12 mars 2024 à 18:45", "2024-12-12T18:45", zone=PLUS_1)
    assert result["ok"] is False and "month" in result["reason"]


# --- split lines -> offsets ---------------------------------------------------------


BODY = "Dear Claire,\n\nThanks.\n\nMary\n\nLe mar. 12 mars 2024 à 18:45, Claire <c@x.example> a écrit :\n> Bonjour Mary,\n> Texte\n"


def test_split_line_gives_the_offset_of_its_line():
    offsets, errors = chronology.split_offsets_for_lines(BODY, ["Le mar. 12 mars 2024 à 18:45, Claire <c@x.example> a écrit :"])
    assert errors == []
    assert offsets == [BODY.index("Le mar.")]


def test_split_line_not_in_the_body_is_rejected():
    offsets, errors = chronology.split_offsets_for_lines(BODY, ["Le 12 mars, Claire a écrit :"])
    assert offsets == [] and "not found" in errors[0]


def test_split_line_must_be_a_whole_line():
    offsets, errors = chronology.split_offsets_for_lines(BODY, ["Claire <c@x.example> a écrit :"])
    assert offsets == [] and errors


def test_split_at_the_first_line_is_rejected():
    offsets, errors = chronology.split_offsets_for_lines(BODY, ["Dear Claire,"])
    assert offsets == [] and "first line" in errors[0]


def test_split_line_whitespace_differences_are_tolerated():
    body = "Top\n\nJohn Tan <j@x.example>\tFri, Mar 15, 2024 at 11:02 AM\nTo: Mary\n\nText here\n"
    offsets, errors = chronology.split_offsets_for_lines(body, ["John Tan <j@x.example> Fri, Mar 15, 2024 at 11:02 AM"])
    assert errors == [] and offsets == [body.index("John Tan")]


# --- step 4: the same sender's time zone --------------------------------------------


def test_sender_zone_comes_from_the_same_senders_dated_email(planted_case):
    with db.get_conn() as conn:
        zone = chronology.sender_zone(
            conn, planted_case["case_id"], "David Brooks <david.brooks@brooks-acoustics.example>", near=None, exclude_ids=[]
        )
    assert zone["offset_minutes"] == 11 * 60
    assert zone["email_id"] == planted_case["card"]("09_acoustic_scope.eml")
    assert "+1100" in zone["evidence"]


def test_sender_zone_matches_a_name_without_an_address(planted_case):
    with db.get_conn() as conn:
        zone = chronology.sender_zone(conn, planted_case["case_id"], "Priya Nair", near=None, exclude_ids=[])
    assert zone["offset_minutes"] == 8 * 60


def test_sender_zone_is_none_for_an_unknown_sender(planted_case):
    with db.get_conn() as conn:
        assert chronology.sender_zone(conn, planted_case["case_id"], "Nobody <no@x.example>", near=None, exclude_ids=[]) is None
