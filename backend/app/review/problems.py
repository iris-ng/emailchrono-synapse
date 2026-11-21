"""Find cards in a case that still need chronology review.

Problems found (deterministic, no AI):

- ``undated``: no parsed date.
- ``chain_date_conflict``: a quoted email dated later than the one above it.
- ``low_confidence``: parse_confidence is 'low' and no AI review has touched the card.
- ``bad_sender``: the sender starts with time text, such as "AM, John Tan" (a known
  quirk of the Gmail "On ... at 9:14 AM, Name <addr> wrote:" parser).
- ``merged``: the body still holds a reply marker or a sender-and-date line below its
  first line that emailchrono's own quote parser did not recognize, so the hidden
  email never got its own card. Markers the parser did recognize are left alone:
  they already produced a quoted card, or ingest dropped that card on purpose as a
  copy of a standalone email.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from .. import db
from ..repos.emails import chain_date_conflict_ids
from ..services.email_text import (
    CJK_HEADER_START_RE,
    CJK_WROTE_RE,
    ON_WROTE_RE,
    OUTLOOK_DELIMITER_RE,
    OUTLOOK_HEADER_DATE_RE,
    OUTLOOK_HEADER_START_RE,
)
from ..services.quotes import (
    find_cjk_header_blocks,
    find_on_wrote_blocks,
    find_outlook_delimited_blocks,
    find_outlook_header_blocks,
)

AI_REVIEWED_FLAG = "ai_reviewed"

# A reply marker is text that spans at most a few wrapped lines, never a blank line.
_SPAN = r"(?:[^\n]|\n(?![ \t>]*\n)){1,250}?"
_LEAD = r"(?im)^[ \t>]*"
LOCALIZED_MARKERS = (
    ("french", re.compile(_LEAD + r"Le\s" + _SPAN + r"\s+a\s+[ée]crit\s*:[ \t]*$")),
    ("german", re.compile(_LEAD + r"Am\s" + _SPAN + r"\s+schrieb\s" + _SPAN + r":[ \t]*$")),
    ("spanish", re.compile(_LEAD + r"El\s" + _SPAN + r"\s+escribi[óo]\s*:[ \t]*$")),
    ("dutch", re.compile(_LEAD + r"Op\s" + _SPAN + r"\s+schreef\s" + _SPAN + r":[ \t]*$")),
    ("italian", re.compile(_LEAD + r"Il\s" + _SPAN + r"\s+ha\s+scritto\s*:[ \t]*$")),
    ("portuguese", re.compile(_LEAD + r"Em\s" + _SPAN + r"\s+escreveu\s*:[ \t]*$")),
)
FORWARDED_RE = re.compile(r"(?im)^[ \t>]*(?:-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message:)[ \t]*$")
# Gmail "print" layout: "Name <addr>" then the date, on the same line or the next.
PRINT_STYLE_RE = re.compile(r"(?m)^[ \t>]*(?P<name>[^<>:\n]*\w[^<>:\n]*?)[ \t]*<[^<>\s@]+@[^<>\s]+>(?P<rest>[^\n]*)$")
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
TIME_RE = re.compile(r"(?<!\d)\d{1,2}[:.h]\d{2}(?!\d)")
CJK_CHAR_RE = re.compile(r"[㐀-鿿]")
TIME_TEXT_SENDER_RE =re.compile(r"^\s*(?:AM|PM|a\.m\.|p\.m\.)\s*,", re.I)

# An Outlook "From:" line counts only with a Sent/Date line close below it.
_OUTLOOK_DATE_WITHIN_LINES = 6
# Header lines that open a forward or "Original Message" block join that block.
_JOIN_WITHIN_LINES = 3


def _has_date(text: str) -> bool:
    return bool(YEAR_RE.search(text) and TIME_RE.search(text))


def _line_bounds(body: str, offset: int) -> tuple:
    start = body.rfind("\n", 0, offset) + 1
    end = body.find("\n", offset)
    return start, (len(body) if end == -1 else end)


def _next_lines(body: str, offset: int, count: int) -> List[str]:
    _, end = _line_bounds(body, offset)
    return body[end + 1 :].split("\n")[:count]


def _raw_markers(body: str) -> List[Dict[str, Any]]:
    found: Dict[int, Dict[str, Any]] = {}

    def add(kind: str, match_start: int) -> None:
        # Some shared regexes start with \s*, which can begin on the blank line above.
        text_start = match_start + (len(body[match_start:]) - len(body[match_start:].lstrip()))
        start, end = _line_bounds(body, text_start)
        line = re.sub(r"^[ \t>]*", "", body[start:end]).rstrip()
        found.setdefault(start, {"kind": kind, "offset": start, "line": line})

    for match in ON_WROTE_RE.finditer(body):
        add("on_wrote", match.start())
    for kind, pattern in LOCALIZED_MARKERS:
        for match in pattern.finditer(body):
            add(kind, match.start())
    for match in OUTLOOK_DELIMITER_RE.finditer(body):
        add("original_message", match.start())
    for match in FORWARDED_RE.finditer(body):
        add("forwarded", match.start())
    for match in OUTLOOK_HEADER_START_RE.finditer(body):
        if any(OUTLOOK_HEADER_DATE_RE.match(line) for line in _next_lines(body, match.start(), _OUTLOOK_DATE_WITHIN_LINES)):
            add("outlook_header", match.start())
    for pattern in (CJK_WROTE_RE, CJK_HEADER_START_RE):
        for match in pattern.finditer(body):
            if CJK_CHAR_RE.search(match.group(0)):  # the shared regex also matches English "Date:"
                add("cjk", match.start())
    for match in PRINT_STYLE_RE.finditer(body):
        rest = match.group("rest").strip()
        following = next((line for line in _next_lines(body, match.start(), 3) if line.strip()), "")
        if _has_date(rest) or (not rest and len(following) <= 80 and _has_date(following)):
            add("print_style", match.start())
    return [found[offset] for offset in sorted(found)]


def markers_in(text: Optional[str]) -> List[Dict[str, Any]]:
    """Every marker line in the text, the first line included: {kind, offset, line}, top to bottom."""
    return _raw_markers((text or "").replace("\r\n", "\n").replace("\r", "\n"))


def find_reply_markers(body: Optional[str]) -> List[Dict[str, Any]]:
    """Reply markers and sender-and-date lines below a card's first line.

    Returns one entry per hidden-email block: {kind, offset, line}. ``offset`` is
    the start of the marker line in the body with newlines normalized to \\n."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    first = re.search(r"\S", text)
    if not first:
        return []
    first_line_end = _line_bounds(text, first.start())[1]
    blocks: List[Dict[str, Any]] = []
    for marker in _raw_markers(text):
        if marker["offset"] < first_line_end:
            continue  # the card's own first line, not a hidden email
        if blocks and marker["kind"] == "outlook_header" and blocks[-1]["kind"] in ("forwarded", "original_message"):
            gap = text.count("\n", blocks[-1]["offset"], marker["offset"])
            if gap <= _JOIN_WITHIN_LINES:
                blocks[-1]["end"] = marker["offset"]
                continue
        blocks.append({**marker, "end": marker["offset"]})
    return blocks


def _parser_block_starts(body: str, default_tz: str) -> List[int]:
    starts: List[int] = []
    for finder in (find_outlook_delimited_blocks, find_outlook_header_blocks, find_on_wrote_blocks, find_cjk_header_blocks):
        starts.extend(offset for offset, _ in finder(body, default_tz))
    return starts


def find_missed_markers(body: Optional[str], default_tz: str = "UTC") -> List[Dict[str, Any]]:
    """Reply markers that emailchrono's quote parser did not turn into a quoted card."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    starts = _parser_block_starts(text, default_tz)
    missed = []
    for block in find_reply_markers(text):
        _, block_end = _line_bounds(text, block["end"])
        if not any(block["offset"] <= start <= block_end for start in starts):
            missed.append({key: block[key] for key in ("kind", "offset", "line")})
    return missed


def _flags_by_email(conn, ids: List[int]) -> Dict[int, Set[str]]:
    flags: Dict[int, Set[str]] = {}
    if not ids:
        return flags
    placeholders = ",".join("?" for _ in ids)
    for row in conn.execute(f"SELECT email_id, flag FROM email_flags WHERE email_id IN ({placeholders})", ids):
        flags.setdefault(row["email_id"], set()).add(row["flag"])
    return flags


def _issues(row, conflicts: Set[int], flags: Set[str], default_tz: str) -> tuple:
    issues = []
    markers = find_missed_markers(row["body_text"], default_tz)
    if markers:
        issues.append("merged")
    if not row["date_utc"]:
        issues.append("undated")
    if row["id"] in conflicts:
        issues.append("chain_date_conflict")
    if row["parse_confidence"] == "low" and AI_REVIEWED_FLAG not in flags:
        issues.append("low_confidence")
    if TIME_TEXT_SENDER_RE.match(row["from_addr"] or ""):
        issues.append("bad_sender")
    return issues, markers


def _live_rows(conn, where: str, params: tuple):
    return conn.execute(
        f"SELECT * FROM emails WHERE {where} AND deleted_at IS NULL AND superseded_at IS NULL ORDER BY id",
        params,
    ).fetchall()


def problems_for_card(conn, row) -> tuple:
    """(problem codes, missed markers) for one live card, on the caller's connection."""
    case = conn.execute("SELECT default_tz FROM cases WHERE id = ?", (row["case_id"],)).fetchone()
    default_tz = (case["default_tz"] if case else None) or "UTC"
    chain = (
        _live_rows(conn, "case_id = ? AND chain_source_id = ?", (row["case_id"], row["chain_source_id"]))
        if row["chain_source_id"] is not None
        else [row]
    )
    flags = _flags_by_email(conn, [row["id"]]).get(row["id"], set())
    return _issues(row, chain_date_conflict_ids(chain), flags, default_tz)


def find_problem_cards(case_id: int) -> List[Dict[str, Any]]:
    """List live cards with chronology problems: undated, chain date conflict, low confidence, bad sender or merged."""
    with db.get_conn() as conn:
        case = conn.execute("SELECT default_tz FROM cases WHERE id = ?", (case_id,)).fetchone()
        rows = _live_rows(conn, "case_id = ?", (case_id,))
        flags = _flags_by_email(conn, [row["id"] for row in rows])
    default_tz = (case["default_tz"] if case else None) or "UTC"
    conflicts = chain_date_conflict_ids(rows)
    found: List[Dict[str, Any]] = []
    for row in rows:
        issues, markers = _issues(row, conflicts, flags.get(row["id"], set()), default_tz)
        if issues:
            found.append(
                {
                    "email_id": row["id"],
                    "problems": issues,
                    "from": row["from_addr"],
                    "subject": row["subject"],
                    "date_utc": row["date_utc"],
                    "markers": markers,
                }
            )
    return found
