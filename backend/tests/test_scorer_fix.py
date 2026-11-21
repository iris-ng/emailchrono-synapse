"""Scorer fix in the copy: a shared file hash only counts as an exact match
when both rows hold the same place in their file (same chain position and
same kind of row). Two uploads of the same file must still match."""

import json

from app import db
from app.services.duplicates import duplicate_score


def _pair_reasons(case_id, a, b):
    a, b = sorted((a, b))
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT reason_json FROM email_duplicate_candidates WHERE case_id=? AND email_a_id=? AND email_b_id=?",
            (case_id, a, b),
        ).fetchone()
    return None if row is None else [r["code"] for r in json.loads(row["reason_json"])]


def test_parent_and_its_own_quoted_child_are_not_an_exact_match(planted_case):
    parent = planted_case["card"]("01_drawings_reply.eml", 0)
    child = planted_case["card"]("01_drawings_reply.eml", 1)
    reasons = _pair_reasons(planted_case["case_id"], parent, child)
    assert reasons is None or "same_source_sha256" not in reasons


def test_two_uploads_of_the_same_file_still_match_exactly(planted_case):
    first = planted_case["card"]("01_drawings_reply.eml", 0)
    copy = planted_case["card"]("11_drawings_reply_copy.eml", 0)
    assert "same_source_sha256" in _pair_reasons(planted_case["case_id"], first, copy)
    first_q = planted_case["card"]("01_drawings_reply.eml", 1)
    copy_q = planted_case["card"]("11_drawings_reply_copy.eml", 1)
    assert "same_source_sha256" in _pair_reasons(planted_case["case_id"], first_q, copy_q)


def test_parent_of_one_upload_is_not_exact_with_quoted_child_of_the_copy(planted_case):
    parent = planted_case["card"]("01_drawings_reply.eml", 0)
    copy_q = planted_case["card"]("11_drawings_reply_copy.eml", 1)
    reasons = _pair_reasons(planted_case["case_id"], parent, copy_q)
    assert reasons is None or "same_source_sha256" not in reasons


def test_snip_part_and_quoted_child_at_same_position_are_not_exact():
    base = {"source_sha256": "abc", "chain_position": 1, "body_text": "x"}
    left = dict(base, id=1, source_kind="snipped", body_text="Mary's own text, long enough")
    right = dict(base, id=2, source_kind="quoted", body_text="John's quoted text, different words")
    scored = duplicate_score(left, right)
    assert scored is None or "same_source_sha256" not in [r["code"] for r in scored[1]]
