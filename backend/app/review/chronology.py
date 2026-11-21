"""Chronology review adapters.

Small, documented steps that Synapse composes into the review_chronology tool:

    check_case_ready_for_chronology_review -> list_chronology_problem_cards
    -> propose_splits (merged cards) -> propose_fields (per card)
    -> save_chronology_plan
    ... a person approves the plan in emailchrono ...
    -> apply_chronology_plan

The AI functions send one card's text to an OpenAI model with structured
output. The model never returns offsets or free dates: it returns exact lines
and exact quotes, and code checks every one against the stored card text
(design section 7). Anything that fails a check is listed "left for you" and
never applied. Nothing here approves a plan.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_tz
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import db
from ..repos.duplicates import recompute_duplicate_candidates
from ..repos.emails import update_email_fields
from ..repos.threads import recompute_case_threads
from ..services.dates import TZ_ABBREVIATIONS, coerce_datetime, resolve_zone
from ..services.email_text import strip_quote_prefixes
from ..services.snip import (
    SnipError,
    build_part,
    draft_from_part,
    load_source,
    normalize_newlines,
    snip_email_conn,
    split_body,
    validate_offsets,
)
from . import plans
from . import problems as problem_finder

ADDRESS_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
TIME_TEXT_PREFIX_RE = re.compile(r"^\s*(?:AM|PM|a\.m\.|p\.m\.)\s*,\s*", re.I)


# --- text helpers -------------------------------------------------------------


def _squash(text: str) -> str:
    """Collapse all whitespace so wrapped lines compare equal to one line."""
    return " ".join((text or "").split())


def _strip_quote_marks(text: str) -> str:
    return "\n".join(re.sub(r"^[ \t>]*", "", line) for line in normalize_newlines(text or "").split("\n"))


def _fold(text: str) -> str:
    """Lowercase without accents: 'März' -> 'marz', 'août' -> 'aout'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _address(value: Optional[str]) -> Optional[str]:
    match = ADDRESS_RE.search(value or "")
    return match.group(0).lower() if match else None


def _display_name(value: Optional[str]) -> str:
    text = TIME_TEXT_PREFIX_RE.sub("", value or "")
    text = re.sub(r"<[^>]*>", "", text)
    return _squash(text.replace('"', "")).strip(", ")


# --- split lines -> offsets -----------------------------------------------------


def split_offsets_for_lines(body: str, lines: List[str]) -> Tuple[List[int], List[str]]:
    """Turn the model's exact first lines of hidden emails into split offsets.

    Each line must equal a whole line of the body (quote marks and spacing aside),
    below the card's first line and in body order. Returns (offsets, errors);
    offsets is empty when anything is wrong."""
    text = normalize_newlines(body or "")
    spans = []
    position = 0
    for raw in text.split("\n"):
        spans.append((position, position + len(raw)))
        position += len(raw) + 1
    first_start = next((start for start, end in spans if text[start:end].strip()), 0)

    offsets: List[int] = []
    errors: List[str] = []
    cursor = 0
    for wanted in lines or []:
        target = _squash(re.sub(r"^[ \t>]*", "", wanted or ""))
        if not target:
            errors.append("The AI returned an empty split line.")
            continue
        found = next(
            (
                start
                for start, end in spans
                if start >= cursor and _squash(re.sub(r"^[ \t>]*", "", text[start:end])) == target
            ),
            None,
        )
        if found is None:
            errors.append(f'Split line not found as a whole line in the card text: "{target[:100]}"')
            continue
        if found <= first_start:
            errors.append(f'Split line "{target[:100]}" is the card\'s first line, not a hidden email.')
            continue
        offsets.append(found)
        cursor = found + 1
    if errors:
        return [], errors
    try:
        validate_offsets(text, offsets)
    except SnipError as err:
        return [], [str(err)]
    return offsets, []


# --- dates (design section 7, steps 1 to 4) -----------------------------------------


MONTH_NAMES: Dict[str, int] = {}
for _number, _names in {
    1: "january jan janvier janv januar janner jaenner enero ene gennaio gen januari janeiro",
    2: "february feb fevrier fevr fev februar febrero febbraio februari fevereiro",
    3: "march mar mars marz maerz mrz marzo maart marco",
    4: "april apr avril avr abril aprile",
    5: "may mai mayo maggio mag mei maio",
    6: "june jun juin juni junio giugno giu junho",
    7: "july jul juillet juil juli julio luglio lug julho",
    8: "august aug aout agosto ago augustus",
    9: "september sep sept septembre septiembre setiembre settembre set setembro",
    10: "october oct octobre oktober okt octubre ottobre ott outubro",
    11: "november nov novembre noviembre novembro",
    12: "december dec decembre dezember dez diciembre dic dicembre dezembro",
}.items():
    for _name in _names.split():
        MONTH_NAMES[_name] = _number

TIME_IN_TEXT_RE = re.compile(
    r"(?P<pre>上午|下午)?\s*(?<![\d.])(?P<h>\d{1,2})\s*[:.h]\s*(?P<m>\d{2})(?![\d.]|\.\d)\s*(?P<ampm>[ap]\.?\s?m\.?)?",
    re.I,
)
ZONE_IN_TEXT_RE = re.compile(r"(?:\b(?:GMT|UTC)\s*(?P<sign1>[+-])(?P<h1>\d{1,2})(?::?(?P<m1>\d{2}))?)|(?:(?<![\d\w-])(?P<sign2>[+-])(?P<h2>\d{2}):?(?P<m2>\d{2})\b)")


def _times_in(text: str) -> List[Tuple[int, int, int, int]]:
    """(hour24, minute, start, end) for every clock time written in the text."""
    found = []
    for match in TIME_IN_TEXT_RE.finditer(text):
        hour, minute = int(match.group("h")), int(match.group("m"))
        ampm = (match.group("ampm") or "").replace(".", "").replace(" ", "").lower()
        if ampm == "pm" or match.group("pre") == "下午":
            hour = hour + 12 if hour < 12 else hour
        elif (ampm == "am" or match.group("pre") == "上午") and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            found.append((hour, minute, match.start("h"), match.end("m")))
    return found


def _zone_in_quote(quote: str) -> Optional[tzinfo]:
    match = ZONE_IN_TEXT_RE.search(quote)
    if match:
        sign = match.group("sign1") or match.group("sign2")
        hours = int(match.group("h1") or match.group("h2"))
        minutes = int(match.group("m1") or match.group("m2") or 0)
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if sign == "-" else delta)
    for word in re.findall(r"\b[A-Z]{2,5}\b", quote):
        if word in TZ_ABBREVIATIONS:
            return TZ_ABBREVIATIONS[word]
        if word in ("GMT", "UTC"):
            return timezone.utc
    return None


def iso_matches_quote(local_iso: str, quote: str) -> Optional[str]:
    """Step 3: None if the ISO date-time agrees with the quote, else the reason it does not.

    Day, year, hour and minute must be written in the quote. The month must match
    a month name in the quote (English, French, German and a few more), or, when
    the quote has no month name, appear as a number."""
    try:
        value = datetime.fromisoformat((local_iso or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return f'The AI date "{local_iso}" is not an ISO 8601 date-time.'
    folded = _fold(quote)
    times = _times_in(folded)
    if (value.hour, value.minute) not in {(h, m) for h, m, _, _ in times}:
        return f"The time {value:%H:%M} is not written in the quote."
    in_time = [(start, end) for _, _, start, end in times]
    numbers = [
        match.group(0)
        for match in re.finditer(r"\d+", folded)
        if not any(start <= match.start() < end for start, end in in_time)
    ]
    years = [int(n) for n in numbers if len(n) == 4]
    small = [int(n) for n in numbers if len(n) <= 2]
    if years:
        if value.year not in years:
            return f"The year {value.year} is not written in the quote."
    elif value.year % 100 not in small:
        return f"The year {value.year} is not written in the quote."
    if value.day not in small:
        return f"The day {value.day} is not written in the quote."
    named = [MONTH_NAMES[word] for word in re.findall(r"[a-z]+", folded) if word in MONTH_NAMES]
    if len(set(named)) > 1 and "mar" in re.findall(r"[a-z]+", folded):
        # "mar." is also Tuesday in French and Spanish; trust the other month name.
        named = [MONTH_NAMES[word] for word in re.findall(r"[a-z]+", folded) if word in MONTH_NAMES and word != "mar"]
    if named:
        if value.month not in named:
            return f"The month {value.month} does not match the month name in the quote."
    elif value.month not in small:
        return f"The month {value.month} is not written in the quote."
    return None


def read_date(quote: str, local_iso: str, text: str, zone: tzinfo) -> Dict[str, Any]:
    """Read a date the AI quoted from the card (steps 1 to 3), in ``zone`` unless the quote names its own.

    Returns {ok, date_utc, method ('parser' or 'ai_checked'), zone_in_quote, reason}."""
    quote = (quote or "").strip()
    if not quote or _squash(quote) not in _squash(_strip_quote_marks(text)):
        return {"ok": False, "reason": f'The date text "{quote}" is not in the card text.'}
    explicit = _zone_in_quote(quote)
    parsed = coerce_datetime(quote)
    if parsed is not None:
        zone_in_quote = parsed.tzinfo is not None or explicit is not None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=explicit or zone)
        return {
            "ok": True,
            "date_utc": parsed.astimezone(timezone.utc).isoformat(),
            "method": "parser",
            "zone_in_quote": zone_in_quote,
            "reason": "",
        }
    problem = iso_matches_quote(local_iso, quote)
    if problem:
        return {"ok": False, "reason": problem}
    local = datetime.fromisoformat(local_iso.strip().replace("Z", "+00:00")).replace(tzinfo=None)
    aware = local.replace(tzinfo=explicit or zone)
    return {
        "ok": True,
        "date_utc": aware.astimezone(timezone.utc).isoformat(),
        "method": "ai_checked",
        "zone_in_quote": explicit is not None,
        "reason": "",
    }


def _explicit_offset(date_raw: Optional[str]) -> Optional[int]:
    """Minutes east of UTC when the raw date names a numeric zone, else None."""
    if not date_raw or not re.search(r"[+-]\d{4}\b", date_raw):
        return None
    parts = parsedate_tz(date_raw)
    if not parts or parts[9] is None:
        return None
    return int(parts[9]) // 60


def sender_zone(
    conn: sqlite3.Connection,
    case_id: int,
    sender: Optional[str],
    near: Optional[datetime],
    exclude_ids: Iterable[int],
) -> Optional[Dict[str, Any]]:
    """Step 4: the time zone of the same sender's other dated emails in the case.

    Matches by address, or by display name when the sender has no address. Uses
    the dated email nearest in time to ``near``. Returns None if there is none."""
    address = _address(sender)
    name = _display_name(sender).lower()
    if not address and not name:
        return None
    excluded = {int(i) for i in exclude_ids}
    best = None
    for row in conn.execute(
        """
        SELECT id, from_addr, subject, date_raw, date_utc FROM emails
        WHERE case_id = ? AND deleted_at IS NULL AND superseded_at IS NULL AND date_raw IS NOT NULL
        ORDER BY id
        """,
        (case_id,),
    ):
        if row["id"] in excluded:
            continue
        same = _address(row["from_addr"]) == address if address else _display_name(row["from_addr"]).lower() == name
        offset = _explicit_offset(row["date_raw"]) if same else None
        if offset is None:
            continue
        distance = 0.0
        if near is not None and row["date_utc"]:
            distance = abs((datetime.fromisoformat(row["date_utc"]) - near).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, row, offset)
    if best is None:
        return None
    _, row, offset = best
    return {
        "offset_minutes": offset,
        "zone": timezone(timedelta(minutes=offset)),
        "email_id": row["id"],
        "evidence": f'Same sender\'s email #{row["id"]} ("{row["subject"]}") is dated "{row["date_raw"]}"',
    }


# --- the model call ---------------------------------------------------------------

MODEL_ENV = "EMAILCHRONO_REVIEW_MODEL"
DEFAULT_MODEL = "gpt-5.6-luna"
# Tests and offline benchmarks only: "module:function" called as function(task, text)
# instead of OpenAI. Set in the environment of the process that plans.
MODEL_STUB_ENV = "EMAILCHRONO_REVIEW_MODEL_STUB"
MODEL_TIMEOUT_SECONDS = 90
MAX_PART_CHARS = 12000

SPLIT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["split_lines", "note"],
    "properties": {
        "split_lines": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
}
_TEXT_WITH_EVIDENCE = {
    "anyOf": [
        {"type": "null"},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["value", "evidence"],
            "properties": {"value": {"type": "string"}, "evidence": {"type": "string"}},
        },
    ]
}
FIELDS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parts"],
    "properties": {
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["part", "sender", "recipients", "subject", "date", "note"],
                "properties": {
                    "part": {"type": "integer"},
                    "sender": _TEXT_WITH_EVIDENCE,
                    "recipients": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["value", "evidence"],
                                "properties": {
                                    "value": {"type": "array", "items": {"type": "string"}},
                                    "evidence": {"type": "string"},
                                },
                            },
                        ]
                    },
                    "subject": _TEXT_WITH_EVIDENCE,
                    "date": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["quote", "local_iso"],
                                "properties": {"quote": {"type": "string"}, "local_iso": {"type": "string"}},
                            },
                        ]
                    },
                    "note": {"type": "string"},
                },
            },
        }
    },
}

SPLIT_INSTRUCTIONS = """You check one card from a legal email chronology. The card's text may hold more than one email: the card's own email at the top, plus older emails pasted or quoted below it that never got their own card.

Return split_lines: for each hidden email, the first line of its header block, copied exactly as it appears in the text (a reply marker such as "On ... wrote:", "Le ... a écrit :", "Am ... schrieb ...:", a sender-and-date line such as "Name <address>  date", or a "From:" line). List them top to bottom. Do not include the card's own first line. Return an empty list if there are no hidden emails.

The rule-based hints may be incomplete or wrong. Never invent or reword a line. Put any doubt in note."""

FIELDS_INSTRUCTIONS = """You review the header fields of one card (or each part of a card being split) in a legal email chronology. Each part is one email. Fix only fields that are missing or clearly wrong in "Parsed ...". Return one entry per part, in order, using its part number.

- sender: who wrote this part, as "Name <address>" when the address is known. Only if the parsed sender is missing or wrong (for example it starts with "AM," or "PM,", or has no address).
- recipients: who this part was sent to, only if parsed recipients are "(none)". Copy each one as "Name <address>" from the material; never use a name from a greeting alone. For an email quoted inside a reply, the sender of the email above it is usually the recipient.
- subject: only if missing or carried over from the email above. For an email quoted inside a reply, it is usually the subject of the reply without "Re:".
- date: only if this part's date is missing or may be in the wrong time zone. quote = the date and time exactly as written in this part's text or in the header text above the card, copied character for character. local_iso = the same date and time as ISO 8601 local time without a zone, like 2024-03-12T18:45.
- evidence: a short snippet copied exactly from the material that contains the value.

Read "Problems found by code" first: it says what to look at. Use null for anything you are not sure of, and say why in note. Never guess a date that is not written in the material. Never reword quoted text."""


_OPENAI_CLIENT = None


def _openai_client():
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        from openai import OpenAI

        from .openai_key import get_openai_api_key

        key = get_openai_api_key()
        if not key:
            raise RuntimeError("no OpenAI API key (set OPENAI_API_KEY or add it to secrets.local)")
        _OPENAI_CLIENT = OpenAI(api_key=key, timeout=MODEL_TIMEOUT_SECONDS, max_retries=1)
    return _OPENAI_CLIENT


def _ask_model(task: str, instructions: str, text: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    """One structured-output call. Returns {ok, data, usage} or {ok: False, error}; never raises."""
    started = time.monotonic()
    usage: Dict[str, Any] = {"model": "", "input_tokens": 0, "output_tokens": 0}
    try:
        stub = os.environ.get(MODEL_STUB_ENV, "").strip()
        if stub:
            module_name, _, function = stub.partition(":")
            data = getattr(importlib.import_module(module_name), function)(task, text)
            usage["model"] = f"stub:{stub}"
        else:
            model = os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
            response = _openai_client().responses.create(
                model=model,
                instructions=instructions,
                input=text,
                text={"format": {"type": "json_schema", "name": f"chronology_{task}", "schema": schema, "strict": True}},
                store=False,
            )
            data = json.loads(response.output_text)
            usage["model"] = response.model
            if response.usage:
                usage["input_tokens"] = response.usage.input_tokens
                usage["output_tokens"] = response.usage.output_tokens
    except Exception as exc:  # API error, timeout, bad JSON: the card goes to "left for you"
        message = " ".join(f"{type(exc).__name__}: {exc}".split())
        return {"ok": False, "error": message[:300], "usage": {**usage, "seconds": round(time.monotonic() - started, 2)}}
    if not isinstance(data, dict):
        return {"ok": False, "error": "the AI answer was not a JSON object", "usage": usage}
    usage["seconds"] = round(time.monotonic() - started, 2)
    return {"ok": True, "data": data, "usage": usage}


# --- cards and parts ----------------------------------------------------------------


def _first_line(body: Optional[str]) -> str:
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def _json_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        loaded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(v) for v in loaded] if isinstance(loaded, list) else []


def card_snapshot(row: sqlite3.Row) -> Dict[str, Any]:
    """What the person sees of a card in the plan; also used to spot changes before apply."""
    return {
        "id": row["id"],
        "from": row["from_addr"],
        "to": _json_list(row["to_json"]),
        "subject": row["subject"],
        "date_utc": row["date_utc"],
        "date_raw": row["date_raw"],
        "first_line": _first_line(row["body_text"]),
        "source_kind": row["source_kind"],
    }


def _load_card(conn: sqlite3.Connection, email_id: int) -> Optional[sqlite3.Row]:
    return load_source(conn, int(email_id))


def _header_text(conn: sqlite3.Connection, row: sqlite3.Row, above: sqlite3.Row) -> str:
    """The marker or header lines above a quoted card in its parent's text."""
    parent = normalize_newlines(above["body_text"] or "")
    own_first = _squash(_first_line(row["body_text"]))
    mapping = conn.execute(
        """
        SELECT start_offset FROM email_text_mappings
        WHERE source_email_id = ? AND target_email_id = ? ORDER BY id LIMIT 1
        """,
        (above["id"], row["id"]),
    ).fetchone()
    starts = []
    if mapping is not None and 0 <= int(mapping["start_offset"]) < len(parent):
        starts.append(int(mapping["start_offset"]))
    if row["date_raw"] and row["date_raw"] in parent:
        starts.append(parent.rfind("\n", 0, parent.index(row["date_raw"])) + 1)
    for start in starts:
        start = parent.rfind("\n", 0, start) + 1
        lines = []
        for line in parent[start:].split("\n")[:8]:
            if own_first and _squash(re.sub(r"^[ \t>]*", "", line)) == own_first:
                break
            lines.append(line)
        header = "\n".join(lines).strip()
        if header and (not row["date_raw"] or _squash(row["date_raw"]) in _squash(_strip_quote_marks(header))):
            return header
    return ""


def _cut_marker(segment: str) -> str:
    """A split part's own text: drop its marker line and quote marks."""
    end = None
    for _, pattern in problem_finder.LOCALIZED_MARKERS:
        match = pattern.match(segment)
        if match:
            end = match.end()
            break
    if end is None:
        newline = segment.find("\n")
        end = len(segment) if newline == -1 else newline
    return strip_quote_prefixes(segment[end:]).strip()


def _split_parts(row: sqlite3.Row, offsets: List[int]) -> List[Dict[str, Any]]:
    """Preview each part with emailchrono's own Snip logic (connection-free)."""
    body = normalize_newlines(row["body_text"] or "")
    segments = split_body(body, offsets)
    default_tz = row["default_tz"] or "UTC"
    parts = []
    for index, segment in enumerate(segments, start=1):
        parsed = build_part(row, segment, index, len(segments), default_tz, offsets)
        draft = draft_from_part(parsed, segment, index)
        body_text = draft["body_text"]
        if index > 1 and _squash(body_text) == _squash(segment):
            # The Snip parser did not recognize this header, so it copied the top
            # email's recipients. They are not this email's: start blank.
            body_text = _cut_marker(segment)
            draft["to"], draft["cc"] = [], []
        parts.append(
            {
                "part_index": index,
                "text": segment,
                "from_addr": draft["from_addr"] or "",
                "to": list(draft["to"] or []),
                "cc": list(draft["cc"] or []),
                "subject": draft["subject"] or "",
                "date_raw": draft["date_raw"],
                "date_utc": parsed.date_utc,
                "body_text": body_text,
                "notes": draft["notes"],
                "important": draft["important"],
                "is_new": index > 1,
            }
        )
    return parts


def _single_part(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "part_index": 1,
        "text": normalize_newlines(row["body_text"] or ""),
        "from_addr": row["from_addr"] or "",
        "to": _json_list(row["to_json"]),
        "cc": _json_list(row["cc_json"]),
        "subject": row["subject"] or "",
        "date_raw": row["date_raw"],
        "date_utc": row["date_utc"],
        "body_text": row["body_text"] or "",
        "is_new": False,
    }


PROBLEM_TEXT = {
    "merged": "the card seems to hold more than one email",
    "undated": "no date was parsed",
    "chain_date_conflict": "this card is dated later than the email above it, so its time zone may be wrong; give the date quote even though a date is parsed",
    "low_confidence": "the parser was unsure about this card",
    "bad_sender": "the parsed sender starts with time text such as 'AM,' or 'PM,'",
}


def _material(row: sqlite3.Row, above: Optional[sqlite3.Row], header: str, parts: List[Dict[str, Any]], issues: List[str]) -> str:
    lines = [f"CARD #{row['id']}", f"Case time zone: {row['default_tz'] or 'UTC'}"]
    shown = [PROBLEM_TEXT[code] for code in issues if code in PROBLEM_TEXT and not (code == "merged" and len(parts) > 1)]
    if shown:
        lines.append("Problems found by code: " + "; ".join(shown) + ".")
    lines.append("")
    if above is not None:
        lines += [
            "EMAIL ABOVE THIS CARD (the reply this card was quoted in)",
            f"From: {above['from_addr'] or '(none)'}",
            f"To: {', '.join(_json_list(above['to_json'])) or '(none)'}",
            f"Subject: {above['subject'] or '(none)'}",
            "",
        ]
    if header:
        lines += ["HEADER TEXT ABOVE THIS CARD IN ITS SOURCE", "<<<", header, ">>>", ""]
    for part in parts:
        text = part["text"]
        if len(text) > MAX_PART_CHARS:
            text = text[:MAX_PART_CHARS] + "\n[... text cut for length ...]"
        lines += [
            f"PART {part['part_index']} OF {len(parts)}",
            f"Parsed sender: {part['from_addr'] or '(none)'}",
            f"Parsed recipients: {', '.join(part['to']) or '(none)'}",
            f"Parsed subject: {part['subject'] or '(none)'}",
            f"Parsed date text: {part['date_raw'] or '(none)'}",
            "Text:",
            "<<<",
            text,
            ">>>",
            "",
        ]
    return "\n".join(lines)


def _prepare(conn: sqlite3.Connection, row: sqlite3.Row, split_lines: List[str]) -> Dict[str, Any]:
    """Everything code knows about a card before the model answers. Deterministic."""
    issues, _ = problem_finder.problems_for_card(conn, row)
    above = None
    header = ""
    if row["source_kind"] == "quoted" and row["parent_email_id"]:
        above = conn.execute("SELECT * FROM emails WHERE id = ?", (row["parent_email_id"],)).fetchone()
        if above is not None:
            header = _header_text(conn, row, above)
    offsets, split_errors = split_offsets_for_lines(row["body_text"] or "", split_lines) if split_lines else ([], [])
    parts = _split_parts(row, offsets) if offsets else [_single_part(row)]
    body = normalize_newlines(row["body_text"] or "")
    for part in parts:
        part["own_text"] = part["text"] + ("\n" + header if header and part["part_index"] == 1 else "")
        if part["part_index"] == 1:
            part["above_subject"] = above["subject"] if above is not None else None
        else:
            part["above_subject"] = parts[part["part_index"] - 2]["subject"]
    return {
        "row": row,
        "issues": issues,
        "above": above,
        "header": header,
        "offsets": offsets,
        "split_lines": [_squash(_strip_quote_marks(body[o : body.find("\n", o) if body.find("\n", o) != -1 else len(body)])) for o in offsets],
        "split_errors": split_errors,
        "parts": parts,
        "material": _material(row, above, header, parts, issues),
        "context_ids": [row["id"]] + ([above["id"]] if above is not None else []),
    }


def _needs_model(prepared: Dict[str, Any]) -> bool:
    issues = prepared["issues"]
    if prepared["offsets"]:
        return True
    if any(code in issues for code in ("undated", "bad_sender", "low_confidence")):
        return True
    return "chain_date_conflict" in issues and _explicit_offset(prepared["row"]["date_raw"]) is None


# --- checking the model's answer --------------------------------------------------------


def _line_holding(value: str, *texts: str) -> Optional[str]:
    for text in texts:
        for line in _strip_quote_marks(text).split("\n"):
            if value in _squash(line):
                return line.strip()
    return None


def _is_header(evidence: str, label: str) -> bool:
    """True when every evidence line is a header line such as 'To: ...' (not body text)."""
    lines = [line for line in evidence.split(" | ") if line.strip()]
    return bool(lines) and all(re.match(rf"^\s*\*?{label}\s*\*?\s*:", line, re.I) for line in lines)


def _check_text(value: str, evidence: str, corpus: str, own: str) -> Tuple[bool, str, bool, str]:
    """(ok, why, inferred, evidence). The value must be written in the card material.
    If the model's evidence does not hold it, code finds the line that does (own
    text first). The value is inferred unless that evidence is in the part's own text."""
    wanted = _squash(value)
    seen = _squash(_strip_quote_marks(evidence))
    if not wanted:
        return False, "The AI returned an empty value.", False, ""
    if not (seen and wanted in seen and seen in _squash(_strip_quote_marks(corpus))):
        found = _line_holding(wanted, own, corpus)
        if found is None:
            return False, f'The value "{value[:100]}" is not in the card material.', False, ""
        seen, evidence = _squash(found), found
    return True, "", seen not in _squash(_strip_quote_marks(own)), evidence.strip()


def _sender_check(current: str, value: str) -> Tuple[bool, str]:
    """(ok, why). ok with an empty why means 'accept'; ok with a why means 'no change needed'."""
    current_address, new_address = _address(current), _address(value)
    if not new_address and not _display_name(value):
        return False, "The AI sender has no name or address."
    if not current.strip():
        return True, ""
    if current_address:
        if new_address != current_address:
            return False, f"The AI would replace the parsed sender address {current_address}; a person should check the sender."
        if TIME_TEXT_PREFIX_RE.match(current):
            return True, ""
        return True, "same sender"
    if _display_name(current).lower() not in _display_name(value).lower():
        return False, f'The AI sender "{value}" does not match the parsed sender name "{current}".'
    return (True, "") if new_address or TIME_TEXT_PREFIX_RE.match(current) else (True, "same sender")


def _change(field: str, old: Any, new: Any, evidence: str, inferred: List[str], **extra: Any) -> Dict[str, Any]:
    return {"field": field, "old": old, "new": new, "evidence": evidence, "inferred": inferred, **extra}


def _resolve_date(conn: sqlite3.Connection, prepared: Dict[str, Any], part: Dict[str, Any], answer: Dict[str, Any], sender: str, conflict: bool) -> Dict[str, Any]:
    """Steps 1 to 5 for one part. Returns {ok, date_utc, date_raw, evidence, inferred, zone_email_id} or {ok: False, why}."""
    row = prepared["row"]
    case_tz = row["default_tz"] or "UTC"
    case_zone = resolve_zone(case_tz)
    quote, iso = str(answer.get("quote") or ""), str(answer.get("local_iso") or "")
    first = read_date(quote, iso, part["own_text"], case_zone)
    if not first["ok"]:
        return {"ok": False, "why": first["reason"]}
    zone_email_id = None
    if first["zone_in_quote"]:
        final, zone_source, zone_evidence = first, "quote", ""
    else:
        near = datetime.fromisoformat(first["date_utc"])
        history = sender_zone(conn, row["case_id"], sender, near, exclude_ids=[row["id"]])
        if history:
            zone, zone_source, zone_evidence, zone_email_id = history["zone"], "sender", history["evidence"], history["email_id"]
        else:
            zone, zone_source = case_zone, "case"
            zone_evidence = f"No dated email from this sender in the case, so the case time zone ({case_tz}) was used"
        final = read_date(quote, iso, part["own_text"], zone)
    if conflict:
        local = datetime.fromisoformat(final["date_utc"]).astimezone(case_zone)
        if zone_source == "case" or (
            zone_source == "sender"
            and datetime.fromisoformat(final["date_utc"]).astimezone(zone).utcoffset() == local.utcoffset()
        ):
            return {
                "ok": False,
                "why": f"The only time zone found for this sender is the case default ({case_tz}), so the date conflict is left for you.",
            }
    inferred = []
    evidence = f'"{quote}" in the card text'
    if final["method"] == "ai_checked":
        inferred.append("inferred_date")
        evidence += " (read by the AI; day, month, year and time checked against the text)"
    if not final["zone_in_quote"]:
        inferred.append("inferred_timezone")
        evidence += f". Time zone: {zone_evidence}"
    return {
        "ok": True,
        "date_utc": final["date_utc"],
        "date_raw": quote,
        "evidence": evidence,
        "inferred": inferred,
        "zone_email_id": zone_email_id,
    }


def _clean_answers(data: Any) -> Dict[int, Dict[str, Any]]:
    answers: Dict[int, Dict[str, Any]] = {}
    for entry in (data or {}).get("parts", []) if isinstance(data, dict) else []:
        if isinstance(entry, dict) and isinstance(entry.get("part"), int):
            answers.setdefault(entry["part"], entry)
    return answers


def _review_part(conn: sqlite3.Connection, prepared: Dict[str, Any], part: Dict[str, Any], answer: Dict[str, Any]) -> Dict[str, Any]:
    issues = prepared["issues"] if not prepared["offsets"] else []
    corpus = prepared["material"]
    changes: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    left: List[Dict[str, Any]] = []
    after = {key: part[key] for key in ("from_addr", "to", "cc", "subject", "date_utc", "date_raw", "body_text")}

    sender = answer.get("sender") if isinstance(answer.get("sender"), dict) else None
    if sender:
        value = str(sender.get("value") or "").strip()
        ok, why, inferred, evidence = _check_text(value, str(sender.get("evidence") or ""), corpus, part["own_text"])
        if ok:
            ok, why = _sender_check(part["from_addr"], value)
        if ok and not why and value != part["from_addr"]:
            changes.append(_change("from_addr", part["from_addr"], value, evidence, ["inferred_from"] if inferred else []))
            after["from_addr"] = value
        elif not ok:
            rejected.append({"field": "from_addr", "value": value, "why": why})

    recipients = answer.get("recipients") if isinstance(answer.get("recipients"), dict) else None
    if recipients:
        values = [str(v).strip() for v in recipients.get("value") or [] if str(v).strip()]
        evidence = str(recipients.get("evidence") or "")
        if part["to"]:
            rejected.append({"field": "to", "value": values, "why": "The card already has recipients."})
        elif not values:
            rejected.append({"field": "to", "value": values, "why": "The AI returned no recipients."})
        elif not all(_address(value) for value in values):
            rejected.append({"field": "to", "value": values, "why": "Each recipient needs an email address; a name alone (for example from a greeting) is not enough."})
        else:
            checks = [_check_text(value, evidence, corpus, part["own_text"]) for value in values]
            failed = next((why for ok, why, _, _ in checks if not ok), None)
            if failed:
                rejected.append({"field": "to", "value": values, "why": failed})
            else:
                used = " | ".join(dict.fromkeys(line for _, _, _, line in checks))
                inferred = any(flag for _, _, flag, _ in checks) or not _is_header(used, "to")
                changes.append(_change("to", part["to"], values, used, ["inferred_to"] if inferred else []))
                after["to"] = values

    subject = answer.get("subject") if isinstance(answer.get("subject"), dict) else None
    if subject:
        value = str(subject.get("value") or "").strip()
        current = part["subject"] or ""
        carried = current in ("", "(no subject)") or (part["above_subject"] is not None and current == part["above_subject"])
        ok, why, inferred, evidence = _check_text(value, str(subject.get("evidence") or ""), corpus, part["own_text"])
        inferred = inferred or not _is_header(evidence, "subject")
        if not carried:
            rejected.append({"field": "subject", "value": value, "why": "The card already has its own subject."})
        elif not ok:
            rejected.append({"field": "subject", "value": value, "why": why})
        elif value != current:
            changes.append(_change("subject", current, value, evidence, ["inferred_subject"] if inferred else []))
            after["subject"] = value

    conflict = "chain_date_conflict" in issues and _explicit_offset(part["date_raw"]) is None
    date_needed = part["is_new"] or not part["date_utc"] or conflict
    problem = "chain_date_conflict" if conflict and part["date_utc"] else "undated"
    date = answer.get("date") if isinstance(answer.get("date"), dict) else None
    if date_needed:
        if date:
            result = _resolve_date(conn, prepared, part, date, after["from_addr"], conflict and bool(part["date_utc"]))
            if not result["ok"]:
                left.append({"part_index": part["part_index"], "problem": problem, "why": result["why"]})
            elif result["date_utc"] != part["date_utc"]:
                changes.append(
                    _change("date_utc", part["date_utc"], result["date_utc"], result["evidence"], result["inferred"], quote=result["date_raw"])
                )
                after["date_utc"], after["date_raw"] = result["date_utc"], result["date_raw"]
                if result["zone_email_id"]:
                    prepared["context_ids"].append(result["zone_email_id"])
            elif conflict:
                left.append({"part_index": part["part_index"], "problem": problem, "why": "The date in the text gives the same time as now, so the conflict needs a person."})
        elif not part["date_utc"] or conflict:
            note = str(answer.get("note") or "").strip()
            left.append({"part_index": part["part_index"], "problem": problem, "why": "The AI found no date in the text." + (f" {note}" if note else "")})

    if "bad_sender" in issues and not any(c["field"] == "from_addr" for c in changes):
        why = next((r["why"] for r in rejected if r["field"] == "from_addr"), "The AI did not propose a corrected sender.")
        left.append({"part_index": part["part_index"], "problem": "bad_sender", "why": why})
    if "low_confidence" in issues and not changes and not left:
        left.append({"part_index": part["part_index"], "problem": "low_confidence", "why": "Parsed with low confidence and the AI found nothing to change. Check it by hand."})

    after["first_line"] = _first_line(after["body_text"])
    before = {key: part[key] for key in ("from_addr", "to", "subject", "date_utc", "date_raw")}
    before["first_line"] = _first_line(part["body_text"])
    return {"part_index": part["part_index"], "before": before, "after": after, "changes": changes, "rejected": rejected, "left": left}


def _review_card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    split_lines: List[str],
    model_output: Any,
    *,
    model_error: str = "",
    split_reason: str = "",
    called_model: bool = True,
) -> Dict[str, Any]:
    """Check the model's answer for one card against the database. Deterministic,
    so save_chronology_plan can run it again on the stored answer."""
    prepared = _prepare(conn, row, split_lines)
    answers = _clean_answers(model_output) if called_model and not model_error else {}
    reviewed = [_review_part(conn, prepared, part, answers.get(part["part_index"], {})) for part in prepared["parts"]]
    left = []
    if model_error:
        left.append({"part_index": None, "problem": "ai_error", "why": f"AI review failed: {model_error}"})
    else:
        for part in reviewed:
            left.extend(part.pop("left"))
    if "merged" in prepared["issues"] and not prepared["offsets"]:
        why = prepared["split_errors"][0] if prepared["split_errors"] else (split_reason or "No split was proposed.")
        left.insert(0, {"part_index": None, "problem": "merged", "why": f"Looks like more than one email, but no split is proposed: {why}"})
    for part in reviewed:
        part.pop("left", None)
    return {
        "email_id": row["id"],
        "ok": not model_error,
        "problems": prepared["issues"],
        "card": card_snapshot(row),
        "split_lines": prepared["split_lines"],
        "offsets": prepared["offsets"],
        "parts": reviewed,
        "left_for_you": [{"email_id": row["id"], **entry} for entry in left],
        "context_email_ids": sorted(set(prepared["context_ids"])),
    }


# --- adapters: the AI proposals -------------------------------------------------------------


def propose_splits(email_id: int) -> Dict[str, Any]:
    """AI: find the exact first line of each email hidden in one merged card; code finds the offsets. Returns {ok, split_lines, offsets, parts, reason}. No writes."""
    with db.get_conn() as conn:
        row = _load_card(conn, email_id)
        if row is None:
            return {"ok": False, "email_id": email_id, "split_lines": [], "offsets": [], "parts": [], "reason": f"Card {email_id} not found or no longer live."}
        markers = problem_finder.find_missed_markers(row["body_text"], row["default_tz"] or "UTC")
    body = normalize_newlines(row["body_text"] or "")
    hints = "\n".join(f"- {m['line']}" for m in markers) or "- (none found)"
    text = f"CARD #{row['id']}\n\nLikely header lines found by a rule-based check (may be incomplete or wrong):\n{hints}\n\nText:\n<<<\n{body[:MAX_PART_CHARS * 2]}\n>>>"
    asked = _ask_model("split", SPLIT_INSTRUCTIONS, text, SPLIT_SCHEMA)
    base = {"email_id": row["id"], "split_lines": [], "offsets": [], "parts": [], "model": asked.get("usage", {})}
    if not asked["ok"]:
        return {**base, "ok": False, "reason": f"AI call failed: {asked['error']}"}
    lines = [str(line) for line in asked["data"].get("split_lines") or [] if str(line).strip()]
    note = str(asked["data"].get("note") or "").strip()
    if not lines:
        return {**base, "ok": False, "reason": "The AI found no hidden emails in this card." + (f" {note}" if note else "")}
    offsets, errors = split_offsets_for_lines(body, lines)
    if errors:
        return {**base, "ok": False, "split_lines": lines, "reason": errors[0], "rejected": errors}
    parts = _split_parts(row, offsets)
    return {
        **base,
        "ok": True,
        "split_lines": [_squash(_strip_quote_marks(body[o : (body.find("\n", o) + 1 or len(body) + 1) - 1])) for o in offsets],
        "offsets": offsets,
        "parts": [
            {"part_index": p["part_index"], "from": p["from_addr"], "date_raw": p["date_raw"], "first_line": _first_line(p["body_text"])}
            for p in parts
        ],
        "reason": note,
    }


def propose_fields(email_id: int, split: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """AI: propose sender, date (quote + ISO), inferred To and Subject with evidence for one card, or for each part of a split from propose_splits. Code checks each value. No writes."""
    with db.get_conn() as conn:
        row = _load_card(conn, email_id)
        if row is None:
            return {"ok": False, "email_id": email_id, "error": f"Card {email_id} not found or no longer live.", "parts": [], "left_for_you": []}
        split_lines = list(split.get("split_lines") or []) if isinstance(split, dict) and split.get("ok") else []
        split_reason = str(split.get("reason") or "") if isinstance(split, dict) and not split.get("ok") else ""
        prepared = _prepare(conn, row, split_lines)
        asked: Dict[str, Any] = {"ok": True, "data": {"parts": []}, "usage": {}}
        called = _needs_model(prepared)
        if called:
            asked = _ask_model("fields", FIELDS_INSTRUCTIONS, prepared["material"], FIELDS_SCHEMA)
        model_output = asked.get("data") if asked["ok"] else None
        proposal = _review_card(
            conn, row, split_lines, model_output,
            model_error="" if asked["ok"] else asked["error"],
            split_reason=split_reason,
            called_model=called,
        )
    proposal["model_output"] = model_output
    proposal["model_error"] = "" if asked["ok"] else asked["error"]
    proposal["split_reason"] = split_reason
    proposal["model"] = {**asked.get("usage", {}), "called": called}
    return proposal


# --- adapters: readiness, plan, apply ---------------------------------------------------

MAX_CARDS_PER_PLAN = 30
AI_REVIEWED_FLAG = problem_finder.AI_REVIEWED_FLAG


def _case_exists(case_id: int) -> bool:
    with db.get_conn() as conn:
        return conn.execute("SELECT 1 FROM cases WHERE id = ? AND deleted_at IS NULL", (case_id,)).fetchone() is not None


def check_case_ready_for_chronology_review(case_id: int) -> Dict[str, Any]:
    """Check a case can get a chronology plan. Returns {ok, reason, warnings}. ok is False only if the case is missing; warns when a duplicate plan will go stale."""
    if not _case_exists(case_id):
        return {"ok": False, "case_id": case_id, "reason": f"Case {case_id} not found.", "warnings": []}
    warnings = []
    duplicates = plans.plan_in_flight(case_id, "duplicates")
    if duplicates:
        warnings.append(
            f"A duplicate plan ({duplicates['id']}, {duplicates['status']}) is waiting. Applying this chronology "
            "plan changes cards, so that duplicate plan will go stale and must be built again."
        )
    total = len(problem_finder.find_problem_cards(case_id))
    if total > MAX_CARDS_PER_PLAN:
        warnings.append(f"{total} cards need review; this plan covers the first {MAX_CARDS_PER_PLAN}. Run the review again afterwards.")
    return {"ok": True, "case_id": case_id, "reason": "", "warnings": warnings}


def list_chronology_problem_cards(case_id: int) -> List[Dict[str, Any]]:
    """List up to 30 cards needing chronology review: email_id, problems (merged, undated, chain_date_conflict, low_confidence, bad_sender), from, subject. Read only."""
    return problem_finder.find_problem_cards(case_id)[:MAX_CARDS_PER_PLAN]


def _item_from_card(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if card["offsets"]:
        return {
            "op": "split",
            "email_id": card["email_id"],
            "card": card["card"],
            "problems": card["problems"],
            "offsets": card["offsets"],
            "split_lines": card["split_lines"],
            "parts": card["parts"],
        }
    [part] = card["parts"]
    if not part["changes"]:
        return None
    update: Dict[str, Any] = {}
    for change in part["changes"]:
        update[change["field"]] = change["new"]
        if change["field"] == "date_utc":
            update["date_raw"] = change["quote"]
    return {
        "op": "set_fields",
        "email_id": card["email_id"],
        "card": card["card"],
        "problems": card["problems"],
        "update": update,
        "changes": part["changes"],
        "rejected": part["rejected"],
        "after": part["after"],
    }


def _summarize(items: List[Dict[str, Any]], left: List[Dict[str, Any]], warnings: List[str]) -> str:
    splits = sum(1 for i in items if i["op"] == "split")
    new_cards = sum(len(i["parts"]) for i in items if i["op"] == "split")
    fixes = sum(1 for i in items if i["op"] == "set_fields")
    inferred = sum(
        1
        for i in items
        for change in (i["changes"] if i["op"] == "set_fields" else [c for p in i["parts"] for c in p["changes"]])
        if change["inferred"]
    )
    text = (
        f"Chronology plan: split {splits} merged card(s) into {new_cards}, fix fields on {fixes} card(s) "
        f"({inferred} value(s) marked inferred), {len(left)} item(s) left for you. "
        "Waiting for approval in emailchrono; nothing has changed yet."
    )
    if warnings:
        text += " Warnings: " + " ".join(warnings)
    return text


def save_chronology_plan(case_id: int, proposals: List[Dict[str, Any]], warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """Store proposals from propose_fields as a plan waiting for human approval in emailchrono. Rechecks every value against the database. Returns plan_id and summary. No case data changes."""
    if not _case_exists(case_id):
        return {"ok": False, "error": f"Case {case_id} not found."}
    warnings = list(warnings or [])
    items: List[Dict[str, Any]] = []
    left: List[Dict[str, Any]] = []
    invalid: List[str] = []
    touched: set = set()
    seen: set = set()
    with db.get_conn() as conn:
        for proposal in proposals or []:
            email_id = proposal.get("email_id") if isinstance(proposal, dict) else None
            row = _load_card(conn, email_id) if isinstance(email_id, int) else None
            if row is None or int(row["case_id"]) != int(case_id):
                invalid.append(f"card {email_id} is not a live card in case {case_id}")
                continue
            if email_id in seen:
                invalid.append(f"card {email_id} appears more than once")
                continue
            seen.add(email_id)
            card = _review_card(
                conn,
                row,
                [str(line) for line in proposal.get("split_lines") or []],
                proposal.get("model_output"),
                model_error=str(proposal.get("model_error") or ""),
                split_reason=str(proposal.get("split_reason") or ""),
                called_model=proposal.get("model_output") is not None,
            )
            item = _item_from_card(card)
            if item:
                items.append(item)
                touched.update(card["context_email_ids"])
            snapshot = card_snapshot(row)
            left.extend({**entry, "email": snapshot} for entry in card["left_for_you"])
    if invalid:
        return {"ok": False, "error": "Plan not saved: " + "; ".join(invalid), "invalid": invalid}
    if not items:
        summary = (
            f"Nothing to change automatically: {len(left)} item(s) need a person in emailchrono."
            if left
            else f"Nothing to do: case {case_id} has no chronology problems the review can fix."
        )
        return {"ok": True, "plan_id": None, "status": "nothing_to_do", "summary": summary, "left_for_you": left}

    summary = _summarize(items, left, warnings)
    try:
        plan = plans.create_plan(
            case_id,
            "chronology",
            items=items,
            left_for_you=left,
            summary=summary,
            warnings=warnings,
            touched={"emails": sorted(touched)},
            created_by="review_chronology",
            check=_snapshot_check(case_id, items),
        )
    except plans.PlanError as err:
        return {"ok": False, "error": f"Plan not saved: {err}"}
    return {
        "ok": True,
        "plan_id": plan["id"],
        "status": "planned",
        "summary": f"Plan {plan['id']}. {summary}",
        "counts": {
            "split": sum(1 for i in items if i["op"] == "split"),
            "set_fields": sum(1 for i in items if i["op"] == "set_fields"),
            "left_for_you": len(left),
        },
        "expires_at": plan["expires_at"],
        "warnings": warnings,
        "next_step": "Ask the user to review and approve the plan in emailchrono, then call again with this plan_id.",
    }


def _item_problem(conn: sqlite3.Connection, case_id: int, item: Dict[str, Any]) -> Optional[str]:
    row = _load_card(conn, item["email_id"])
    if row is None or int(row["case_id"]) != int(case_id):
        return f"card {item['email_id']} was deleted or split"
    if item["op"] == "split":
        offsets, errors = split_offsets_for_lines(row["body_text"] or "", item["split_lines"])
        if errors or offsets != item["offsets"]:
            return f"card {item['email_id']} text changed, so the split lines no longer match"
    elif item["op"] != "set_fields":
        return f"unknown item {item['op']!r}"
    return None


def _snapshot_check(case_id: int, items: List[Dict[str, Any]]):
    """Inside plan creation: refuse if a card changed while the plan was being built."""

    def check(conn: sqlite3.Connection) -> Optional[str]:
        for item in items:
            problem = _item_problem(conn, case_id, item)
            if problem:
                return f"the case changed while the plan was being built ({problem}). Build it again."
            if card_snapshot(_load_card(conn, item["email_id"])) != item["card"]:
                return f"card #{item['email_id']} changed while the plan was being built. Build it again."
        return None

    return check


def _write_flags(conn: sqlite3.Connection, email_id: int, changes: List[Dict[str, Any]], plan_id: str) -> None:
    now = db.utc_now()
    rows = [
        (flag, {"evidence": change["evidence"], "value": change["new"], "plan_id": plan_id})
        for change in changes
        for flag in change["inferred"]
    ]
    rows.append((AI_REVIEWED_FLAG, {"plan_id": plan_id}))
    for flag, detail in rows:
        conn.execute("DELETE FROM email_flags WHERE email_id = ? AND flag = ?", (email_id, flag))
        conn.execute(
            "INSERT INTO email_flags (email_id, flag, detail_json, created_at) VALUES (?, ?, ?, ?)",
            (email_id, flag, json.dumps(detail, ensure_ascii=False), now),
        )


def _audit_ids_after(conn: sqlite3.Connection, last_id: int) -> List[int]:
    return [int(r[0]) for r in conn.execute("SELECT id FROM audit_events WHERE id > ? ORDER BY id", (last_id,))]


def _apply_item(plan_id: str):
    actor = f"ai_review (plan {plan_id})"

    def apply(conn: sqlite3.Connection, item: Dict[str, Any]) -> List[int]:
        last = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events").fetchone()[0])
        evidence = {c["field"]: c["evidence"] for c in item.get("changes", [])}
        if item["op"] == "set_fields":
            row = update_email_fields(
                conn, item["email_id"], item["update"], actor=actor,
                audit_metadata={"plan_id": plan_id, "evidence": evidence},
            )
            if row is None:
                raise RuntimeError(f"card {item['email_id']} is missing")
            _write_flags(conn, item["email_id"], item["changes"], plan_id)
        else:
            drafts = [
                {
                    "part_index": part["part_index"],
                    "from_addr": part["after"]["from_addr"],
                    "to": part["after"]["to"],
                    "cc": part["after"]["cc"],
                    "subject": part["after"]["subject"],
                    "date_raw": part["after"]["date_raw"],
                    "body_text": part["after"]["body_text"],
                    "notes": "",
                    "important": False,
                    "approved": True,
                }
                for part in item["parts"]
            ]
            snipped = snip_email_conn(
                conn,
                item["email_id"],
                item["offsets"],
                drafts,
                date_utc_overrides={part["part_index"]: part["after"]["date_utc"] for part in item["parts"]},
                extra_flags=["ai_split"],
                evidence_note=f"Split by AI review plan {plan_id}",
                audit_metadata={
                    "plan_id": plan_id,
                    "split_lines": item["split_lines"],
                    "evidence": [{c["field"]: c["evidence"] for c in part["changes"]} for part in item["parts"]],
                },
                actor=actor,
            )
            if snipped is None:
                raise RuntimeError(f"card {item['email_id']} is missing")
            for part, new_id in zip(item["parts"], snipped["created_ids"]):
                _write_flags(conn, new_id, part["changes"], plan_id)
        return _audit_ids_after(conn, last)

    return apply


def _refresh_derived(case_id: int, plan: Dict[str, Any]) -> None:
    with db.get_conn() as conn:
        recompute_case_threads(conn, case_id)
    recompute_duplicate_candidates(case_id)


def apply_chronology_plan(case_id: int, plan_id: str) -> Dict[str, Any]:
    """Apply an approved chronology plan in one transaction, then refresh threads and duplicate pairs. Returns status applied, waiting_for_approval or refused with a reason. Never approves."""
    return plans.apply_plan(
        case_id,
        plan_id,
        "chronology",
        validate_item=lambda conn, item: _item_problem(conn, case_id, item),
        apply_item=_apply_item(plan_id),
        after_commit=_refresh_derived,
    )
