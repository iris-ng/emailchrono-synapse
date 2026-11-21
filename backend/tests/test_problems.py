"""Problem-card detection, scored against the planted case answer key."""

from app.review import problems


def _found(planted_case):
    return {p["email_id"]: p for p in problems.find_problem_cards(planted_case["case_id"])}


def _files_with(planted_case, found, code):
    return {
        (planted_case["cards"][email_id]["file"], planted_case["cards"][email_id]["chain_position"])
        for email_id, problem in found.items()
        if code in problem["problems"]
    }


def test_merged_cards_match_the_answer_key(planted_case):
    found = _found(planted_case)
    expected = {
        (card["file"], card["chain_position"])
        for card in planted_case["cards"].values()
        if len(card["keys"]) > 1
    }
    assert expected == {("02_lighting_fr.eml", 0), ("04_structure_de.eml", 0), ("07_landlord_print.eml", 0)}
    assert _files_with(planted_case, found, "merged") == expected


def test_merged_cards_list_each_hidden_header_line(planted_case):
    found = _found(planted_case)
    lines = lambda file: [m["line"] for m in found[planted_case["card"](file)]["markers"]]  # noqa: E731
    assert lines("07_landlord_print.eml") == [
        "John Tan <john.tan@harbourview.example>\tFri, Mar 15, 2024 at 11:02 AM",
        "Mary Lim <mary.lim@limarchitects.example>\tFri, Mar 15, 2024 at 9:47 AM",
    ]
    assert lines("02_lighting_fr.eml")[0].startswith("Le mar. 12 mars 2024 à 18:45, Claire Martin")
    assert lines("04_structure_de.eml")[0].startswith("Am Mi., 13. März 2024 um 10:30 Uhr schrieb Hans Weber")


def test_parents_whose_quotes_already_have_cards_are_not_merged(planted_case):
    found = _found(planted_case)
    for file in (
        "01_drawings_reply.eml", "06_costplan_v2_reply.eml", "08_acoustic_reply.eml",
        "11_drawings_reply_copy.eml", "13_fwd_acoustic.eml",
    ):
        problem = found.get(planted_case["card"](file))
        assert problem is None or "merged" not in problem["problems"], (file, problem)


def test_undated_and_low_confidence_cards(planted_case):
    found = _found(planted_case)
    expected = {
        ("01_drawings_reply.eml", 1), ("06_costplan_v2_reply.eml", 1),
        ("11_drawings_reply_copy.eml", 1), ("10_access_cards_undated.eml", 0),
    }
    assert _files_with(planted_case, found, "undated") == expected
    assert _files_with(planted_case, found, "low_confidence") == expected


def test_sydney_time_conflicts(planted_case):
    found = _found(planted_case)
    conflicts = _files_with(planted_case, found, "chain_date_conflict")
    assert {("08_acoustic_reply.eml", 1), ("13_fwd_acoustic.eml", 1)} <= conflicts
    assert {file for file, _ in conflicts} == {"08_acoustic_reply.eml", "13_fwd_acoustic.eml"}


def test_sender_with_time_text_is_flagged(planted_case):
    found = _found(planted_case)
    assert _files_with(planted_case, found, "bad_sender") == {
        ("01_drawings_reply.eml", 1), ("11_drawings_reply_copy.eml", 1),
    }


def test_clean_cards_are_not_listed(planted_case):
    found = _found(planted_case)
    files = {planted_case["cards"][email_id]["file"] for email_id in found}
    for clean in ("03_claire_spec.eml", "05_hans_calc.eml", "09_acoustic_scope.eml", "12a_landlord_approval.eml",
                  "14_costplan_v3.eml", "15_costplan_v3_received.eml", "16_minutes_wk11.eml"):
        assert clean not in files


# --- the marker finder on its own -------------------------------------------------


BODY_TOP = "Hi Ann,\n\nSee below.\n\nBen\n\n"


def test_marker_kinds():
    cases = {
        "on_wrote": "On Tue, 12 Mar 2024 at 09:14, John Tan <john@x.example> wrote:",
        "french": "Le mar. 12 mars 2024 à 18:45, Claire <c@x.example> a écrit :",
        "german": "Am Mi., 13. März 2024 um 10:30 Uhr schrieb Hans <h@x.example>:",
        "spanish": "El mar, 12 mar 2024 a las 18:45, Ana <a@x.example> escribió:",
        "dutch": "Op di 12 mrt 2024 om 18:45 schreef Piet <p@x.example>:",
        "italian": "Il giorno mar 12 mar 2024 alle ore 18:45 Gia <g@x.example> ha scritto:",
        "portuguese": "Em ter., 12 de mar. de 2024 às 18:45, Rui <r@x.example> escreveu:",
        "cjk": "2024年3月12日 18:45，张三 <z@x.example> 写道：",
        "print_style": "John Tan <john@x.example>\tFri, Mar 15, 2024 at 11:02 AM",
        "outlook_header": "From: John Tan <john@x.example>",
        "forwarded": "---------- Forwarded message ---------",
        "original_message": "-----Original Message-----",
    }
    for kind, line in cases.items():
        markers = problems.find_reply_markers(BODY_TOP + line + "\nSent: Tue, 12 Mar 2024 09:14\n> quoted text\n")
        assert [m["kind"] for m in markers] == [kind], (kind, markers)
        assert markers[0]["line"] == line
        assert markers[0]["offset"] == len(BODY_TOP)


def test_print_style_with_date_on_the_next_line():
    body = BODY_TOP + "John Tan <john@x.example>\nFri, Mar 15, 2024 at 11:02 AM\nTo: Ann\n\nText\n"
    markers = problems.find_reply_markers(body)
    assert [m["kind"] for m in markers] == ["print_style"]
    assert markers[0]["line"] == "John Tan <john@x.example>"


def test_outlook_from_line_without_a_date_line_is_not_a_marker():
    body = BODY_TOP + "From: the site team <site@x.example>\nPlease reply by Friday.\n"
    assert problems.find_reply_markers(body) == []


def test_english_date_label_is_not_a_cjk_marker():
    body = BODY_TOP + "Date: Mon, Mar 18, 2024\nText\n"
    assert problems.find_reply_markers(body) == []


def test_name_and_address_without_a_date_is_not_a_marker():
    body = BODY_TOP + "Please copy Jane Lee <jane@x.example> on the next email.\nThanks\n"
    assert problems.find_reply_markers(body) == []


def test_marker_in_the_first_lines_does_not_count():
    body = "On Tue, 12 Mar 2024 at 09:14, John Tan <john@x.example> wrote:\nHi Ann,\nText\n"
    assert problems.find_reply_markers(body) == []


def test_forward_delimiter_and_its_header_count_as_one_block():
    body = BODY_TOP + "---------- Forwarded message ---------\nFrom: D <d@x.example>\nDate: Mon, Mar 18, 2024\n\nText\n"
    assert [m["kind"] for m in problems.find_reply_markers(body)] == ["forwarded"]
