"""Decide when a quoted card is a copy of a standalone email in the same case.

When a reply and its original are both uploaded, the quoted copy inside the
reply is redundant and the standalone email should win. Body similarity alone
is not enough: separate messages can share most of their text. A quoted card
counts as a copy only when body, sender, date and subject all agree.
"""

from datetime import datetime
from typing import Any, Mapping, Optional

from .duplicates import sender_agreement
from .quotes import bodies_similar
from .text_norm import canonical_body, normalize_subject

# Marks a quoted card that ingest moved to trash once, so it is never trashed
# again after the user restores it.
QUOTED_COPY_FLAG = "quoted_copy_trashed"

# Quoted header dates are read in the case's zone and lose their seconds, so a
# true copy can be off by a whole time-zone offset (a multiple of 15 minutes,
# at most 14 hours) plus up to a minute.
MAX_ZONE_SHIFT_SECONDS = 14 * 3600
ZONE_STEP_SECONDS = 15 * 60
SLACK_SECONDS = 60

# Short bodies ("Thanks, will do.") recur across separate messages, so they
# count as a copy only with a matching sender and a time that agrees to the
# minute, not merely up to a time-zone shift.
SHORT_BODY_CHARS = 40

_NO_SUBJECT = {"", "(no subject)"}


def date_gap(quoted: Optional[str], standalone: Optional[str]) -> Optional[float]:
    """Seconds between two ISO dates, or None when either is missing or unreadable."""
    if not quoted or not standalone:
        return None
    try:
        return abs((datetime.fromisoformat(quoted) - datetime.fromisoformat(standalone)).total_seconds())
    except (TypeError, ValueError):
        return None


def date_agreement(quoted: Optional[str], standalone: Optional[str]) -> Optional[bool]:
    """True or False when both dates are readable, None when either is not."""
    gap = date_gap(quoted, standalone)
    if gap is None:
        return None
    if gap > MAX_ZONE_SHIFT_SECONDS + SLACK_SECONDS:
        return False
    off_step = gap % ZONE_STEP_SECONDS
    return off_step <= SLACK_SECONDS or off_step >= ZONE_STEP_SECONDS - SLACK_SECONDS


def subjects_agree(quoted: Optional[str], standalone: Optional[str]) -> bool:
    left, right = normalize_subject(quoted), normalize_subject(standalone)
    if left in _NO_SUBJECT or right in _NO_SUBJECT:
        return True
    return left == right


def is_quoted_copy(
    quoted: Mapping[str, Any],
    standalone: Mapping[str, Any],
    quoted_norm: Optional[str] = None,
    standalone_norm: Optional[str] = None,
) -> bool:
    """True when ``quoted`` is a copy of ``standalone``.

    Sender, date and subject must not disagree. When the sender or date can't
    be compared, the normalized bodies must be equal; otherwise they must be
    near-identical (the same threshold ingest has always used).
    """
    q_body = quoted_norm if quoted_norm is not None else canonical_body(quoted.get("body_text"))
    s_body = standalone_norm if standalone_norm is not None else canonical_body(standalone.get("body_text"))
    if not q_body or not s_body:
        return False
    sender = sender_agreement(quoted.get("from_addr"), standalone.get("from_addr"))
    date = date_agreement(quoted.get("date_utc"), standalone.get("date_utc"))
    if sender is False or date is False or not subjects_agree(quoted.get("subject"), standalone.get("subject")):
        return False
    if min(len(q_body), len(s_body)) < SHORT_BODY_CHARS:
        gap = date_gap(quoted.get("date_utc"), standalone.get("date_utc"))
        return sender is True and gap is not None and gap <= SLACK_SECONDS and q_body == s_body
    if sender is None or date is None:
        return q_body == s_body
    return bodies_similar(q_body, s_body)
