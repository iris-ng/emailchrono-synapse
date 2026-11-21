"""Find cards in a case that still need chronology review."""

from __future__ import annotations

from typing import Any, Dict, List

from .. import db
from ..repos.emails import chain_date_conflict_ids


def find_problem_cards(case_id: int) -> List[Dict[str, Any]]:
    """List live cards with chronology problems: undated or in a chain date conflict."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM emails
            WHERE case_id = ? AND deleted_at IS NULL AND superseded_at IS NULL
            ORDER BY id
            """,
            (case_id,),
        ).fetchall()
    conflicts = chain_date_conflict_ids(rows)
    found: List[Dict[str, Any]] = []
    for row in rows:
        issues = []
        if not row["date_utc"]:
            issues.append("undated")
        if row["id"] in conflicts:
            issues.append("chain_date_conflict")
        if issues:
            found.append(
                {
                    "email_id": row["id"],
                    "problems": issues,
                    "from": row["from_addr"],
                    "subject": row["subject"],
                    "date_utc": row["date_utc"],
                }
            )
    return found
