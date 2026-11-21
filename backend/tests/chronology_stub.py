"""Scripted stand-in for the OpenAI model in the chronology AI functions.

Used by tests and by offline benchmark runs, never by the app itself. Enable it
with EMAILCHRONO_REVIEW_MODEL_STUB=tests.chronology_stub:respond (run from
backend/). It answers the planted case the way a careful model would, using
the answer key, and only quotes text that is really in the prompt.

Tests can bend it: OVERRIDES[task] = function(text) -> answer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

TRUTH = json.loads((Path(__file__).resolve().parents[2] / "demo_data" / "ground_truth.json").read_text(encoding="utf-8"))
MESSAGES = TRUTH["messages"]

OVERRIDES: Dict[str, Callable[[str], Dict[str, Any]]] = {}
CALLS: List[Dict[str, str]] = []
CURRENT_TEXT = [""]  # the whole prompt, for evidence lookups across parts

# A phrase from each hidden or quoted message's own text, and the date text
# shown above it in the planted files.
KNOWN = {
    "Can you confirm when the revised ceiling drawings": ("M1", "Tue, Mar 12, 2024 at 9:14 AM"),
    "Suite à notre appel": ("M3", "mar. 12 mars 2024 à 18:45"),
    "die zulässige Deckenlast": ("M6", "Mi., 13. März 2024 um 10:30 Uhr"),
    "Attached is cost plan v2": ("M9", "Thu, 14 Mar 2024 at 16:20"),
    "the landlord has approved the revised ceiling layout": ("M12", "Fri, Mar 15, 2024 at 11:02 AM"),
    "attaching the revised ceiling layout for landlord approval": ("M11", "Fri, Mar 15, 2024 at 9:47 AM"),
    "Final acoustic report attached": ("M14", "Mon, Mar 18, 2024 at 11:30 AM"),
}
SPLIT_LINES = [
    "Le mar. 12 mars 2024 à 18:45, Claire Martin <claire.martin@atelier-lumiere.example> a écrit :",
    "Am Mi., 13. März 2024 um 10:30 Uhr schrieb Hans Weber <hans.weber@weber-statik.example>:",
    "John Tan <john.tan@harbourview.example>\tFri, Mar 15, 2024 at 11:02 AM",
    "Mary Lim <mary.lim@limarchitects.example>\tFri, Mar 15, 2024 at 9:47 AM",
]


def respond(task: str, text: str) -> Dict[str, Any]:
    CALLS.append({"task": task, "text": text})
    CURRENT_TEXT[0] = text
    if task in OVERRIDES:
        return OVERRIDES[task](text)
    if task == "split":
        return {"split_lines": [line for line in SPLIT_LINES if line in text], "note": ""}
    if task == "fields":
        return {"parts": [_answer(index, block) for index, block in _parts(text)]}
    raise ValueError(f"unknown task {task}")


def _parts(text: str):
    for match in re.finditer(r"(?ms)^PART (\d+) OF \d+\n(.*?)(?=^PART \d+ OF \d+\n|\Z)", text):
        yield int(match.group(1)), match.group(2)


def _section(text: str, start: str) -> str:
    match = re.search(re.escape(start) + r"\n<<<\n(.*?)\n>>>", text, re.S)
    return match.group(1) if match else ""


def _line_with(text: str, value: str, block: str = "") -> Optional[str]:
    """Cite like a careful model: the part's own text first, then the header text, then anything."""
    for source in (_section(block, "Text:"), _section(text, "HEADER TEXT ABOVE THIS CARD IN ITS SOURCE"), text):
        for line in source.split("\n"):
            if value in line:
                return re.sub(r"^[ \t>]*", "", line).strip()
    return None


def _answer(index: int, block: str) -> Dict[str, Any]:
    empty = {"part": index, "sender": None, "recipients": None, "subject": None, "date": None, "note": ""}
    found = next(((key, quote) for phrase, (key, quote) in KNOWN.items() if phrase in block), None)
    if found is None or (index == 1 and found[0] in ("M11", "M12")):
        return {**empty, "note": "Nothing to fix, or no date in the text."}
    key, quote = found
    message = MESSAGES[key]
    whole = CURRENT_TEXT[0]
    answer = dict(empty)
    sender_evidence = _line_with(whole, message["from"], block)
    if sender_evidence:
        answer["sender"] = {"value": message["from"], "evidence": sender_evidence}
    if "Parsed recipients: (none)" in block:
        evidence = _line_with(whole, message["to"][0])
        if evidence:
            answer["recipients"] = {"value": message["to"], "evidence": evidence}
    evidence = _line_with(whole, f"Subject: Re: {message['subject']}") or _line_with(whole, f"Parsed subject: Re: {message['subject']}")
    if evidence and f"Parsed subject: {message['subject']}\n" not in block:
        answer["subject"] = {"value": message["subject"], "evidence": evidence}
    answer["date"] = {"quote": quote, "local_iso": message["date_local"][:16]}
    return answer

