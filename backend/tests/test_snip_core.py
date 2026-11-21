"""The manual Snip path still works after its core moved to snip_email_conn."""

from app import db
from app.services import snip


def test_manual_snip_creates_parts_and_supersedes_the_source(planted_case):
    email_id = planted_case["card"]("02_lighting_fr.eml")
    with db.get_conn() as conn:
        body = conn.execute("SELECT body_text FROM emails WHERE id = ?", (email_id,)).fetchone()["body_text"]
    offset = body.replace("\r\n", "\n").index("Le mar. 12 mars")
    cards = snip.snip_email(email_id, [offset])
    assert [c["source_kind"] for c in cards] == ["snipped", "snipped"]
    assert cards[1]["body_text"].startswith("Le mar. 12 mars")
    assert "inferred" in cards[0]
    with db.get_conn() as conn:
        source = conn.execute("SELECT superseded_at FROM emails WHERE id = ?", (email_id,)).fetchone()
        flags = {r["flag"] for r in conn.execute("SELECT flag FROM email_flags WHERE email_id = ?", (cards[1]["id"],))}
    assert source["superseded_at"] is not None
    assert "manual_snip" in flags and "ai_split" not in flags


def test_snip_core_takes_a_checked_date_and_extra_flags(planted_case):
    email_id = planted_case["card"]("02_lighting_fr.eml")
    with db.get_conn() as conn:
        body = conn.execute("SELECT body_text FROM emails WHERE id = ?", (email_id,)).fetchone()["body_text"]
        offset = body.replace("\r\n", "\n").index("Le mar. 12 mars")
        drafts = [
            {"part_index": i, "from_addr": "A <a@x.example>", "to": [], "cc": [], "subject": "S",
             "date_raw": "mar. 12 mars 2024 à 18:45", "body_text": "Some text", "approved": True}
            for i in (1, 2)
        ]
        result = snip.snip_email_conn(
            conn, email_id, [offset], drafts,
            date_utc_overrides={2: "2024-03-12T17:45:00+00:00"}, extra_flags=["ai_split"],
        )
        part = conn.execute("SELECT date_utc FROM emails WHERE id = ?", (result["created_ids"][1],)).fetchone()
        flags = {r["flag"] for r in conn.execute("SELECT flag FROM email_flags WHERE email_id = ?", (result["created_ids"][1],))}
    assert part["date_utc"] == "2024-03-12T17:45:00+00:00"
    assert "ai_split" in flags
