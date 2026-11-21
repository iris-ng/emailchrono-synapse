"""Planted demo case: fake .eml files with a known right answer.

Every person, company and domain here is made up (``.example`` is a reserved
top-level domain). Each file is built to hit one known gap in emailchrono's
parser or duplicate scorer, and ``ground_truth.json`` records what a careful
human reviewer would end up with.

Run ``python demo_data/planted.py`` to (re)write ``demo_data/planted/*.eml`` and
``demo_data/ground_truth.json``. Output is byte-for-byte deterministic.

Ground truth model
------------------
* ``messages``: every real email in the case, keyed M1..Mn, with the sender,
  recipients, subject, true date and the first line of its own text.
* ``files``: for each .eml file, the message keys in body order (top first).
  ``cards_at_ingest`` maps emailchrono's chain_position to the message key(s)
  that card holds after a plain import. A list with more than one key means the
  parser merged hidden emails into one card (a split is needed).
* Two cards are true duplicates if and only if they hold the same message key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import format_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "planted"
GROUND_TRUTH = HERE / "ground_truth.json"

CASE_NAME = "Harbour View fit-out (planted)"
CASE_TZ = "Asia/Singapore"

SGT = timezone(timedelta(hours=8), "SGT")
CET = timezone(timedelta(hours=1), "CET")  # Paris and Berlin before 31 Mar 2024
AEDT = timezone(timedelta(hours=11), "AEDT")  # Sydney in March 2024

JOHN = "John Tan <john.tan@harbourview.example>"
WEILING = "Wei Ling Koh <weiling.koh@harbourview.example>"
MARY = "Mary Lim <mary.lim@limarchitects.example>"
CLAIRE = "Claire Martin <claire.martin@atelier-lumiere.example>"
HANS = "Hans Weber <hans.weber@weber-statik.example>"
PRIYA = "Priya Nair <priya.nair@qsconsult.example>"
DAVID = "David Brooks <david.brooks@brooks-acoustics.example>"
AHMAD = "Ahmad Rahman <ahmad.rahman@buildco.example>"


@dataclass
class Message:
    key: str
    sender: str
    to: List[str]
    subject: str
    date: Optional[datetime]
    text: str  # the message's own text (no quoted history)
    note: str = ""
    # How a reviewer can know the time zone when the visible text has none.
    tz_basis: str = "header"

    @property
    def first_line(self) -> str:
        return next(line.strip() for line in self.text.splitlines() if line.strip())

    def ground_truth(self) -> dict:
        return {
            "from": self.sender,
            "to": self.to,
            "subject": self.subject,
            "date_utc": self.date.astimezone(timezone.utc).isoformat() if self.date else None,
            "date_local": self.date.isoformat() if self.date else None,
            "tz_basis": self.tz_basis if self.date else None,
            "first_line": self.first_line,
            "note": self.note,
        }


def quote(text: str) -> str:
    return "\n".join(("> " + line) if line else ">" for line in text.strip("\n").split("\n"))


MESSAGES: Dict[str, Message] = {}


def msg(*args, **kwargs) -> Message:
    m = Message(*args, **kwargs)
    MESSAGES[m.key] = m
    return m


# --- Real messages ---------------------------------------------------------

M1 = msg("M1", JOHN, [MARY], "Revised drawings", datetime(2024, 3, 12, 9, 14, tzinfo=SGT),
         "Hi Mary,\n\nCan you confirm when the revised ceiling drawings will be ready? The\n"
         "contractor needs them before the site meeting.\n\nThanks,\nJohn",
         note="Only seen as a Gmail quote. Parser keeps 'AM,' in the sender and cannot read the date.",
         tz_basis="sender_history")
M2 = msg("M2", MARY, [JOHN], "Re: Revised drawings", datetime(2024, 3, 12, 10, 2, tzinfo=SGT),
         "Hi John,\n\nNoted. I will send the revised drawings by Friday 15 March.\n\nRegards,\nMary")
M3 = msg("M3", CLAIRE, [MARY], "Luminaires - hall d'entrée", datetime(2024, 3, 12, 18, 45, tzinfo=CET),
         "Bonjour Mary,\n\nSuite à notre appel, je vous propose deux options pour les suspensions\n"
         "du hall : option A (laiton) ou option B (verre opalin).\n\nBien cordialement,\nClaire",
         note="Hidden behind a French Gmail reply marker the parser does not know.",
         tz_basis="sender_history")
M4 = msg("M4", MARY, [CLAIRE], "Re: Luminaires - hall d'entrée", datetime(2024, 3, 13, 9, 30, tzinfo=SGT),
         "Dear Claire,\n\nThank you. We accept option B for the lobby pendants. Please send the\n"
         "updated specification sheet.\n\nBest regards,\nMary")
M5 = msg("M5", CLAIRE, [MARY], "Fiche technique option B", datetime(2024, 3, 14, 11, 20, tzinfo=CET),
         "Bonjour Mary,\n\nPlease find the specification sheet for option B (opal glass pendants)\n"
         "below. Lead time is six weeks from order.\n\nBien cordialement,\nClaire")
M6 = msg("M6", HANS, [JOHN], "Deckenlast / ceiling load", datetime(2024, 3, 13, 10, 30, tzinfo=CET),
         "Hallo John,\n\ndie zulässige Deckenlast beträgt 2,5 kN/m². The proposed steel frame is\n"
         "within this limit.\n\nViele Grüße\nHans Weber",
         note="Hidden behind a German Gmail reply marker the parser does not know.",
         tz_basis="sender_history")
M7 = msg("M7", JOHN, [HANS], "Re: Deckenlast / ceiling load", datetime(2024, 3, 14, 16, 5, tzinfo=SGT),
         "Hans,\n\nThanks for confirming the load. We will proceed with the steel frame.\n\nJohn")
M8 = msg("M8", HANS, [JOHN], "Statische Berechnung / calculation", datetime(2024, 3, 15, 8, 15, tzinfo=CET),
         "Hallo John,\n\nattached is the signed structural calculation for the ceiling frame.\n\n"
         "Viele Grüße\nHans Weber")
M9 = msg("M9", PRIYA, [WEILING], "Cost plan v2", datetime(2024, 3, 14, 16, 20, tzinfo=SGT),
         "Hi Wei Ling,\nAttached is cost plan v2. The lighting option B adds SGD 4,200.\n"
         "Regards,\nPriya",
         note="Gmail quote with no email address and a 24-hour time the date parser cannot read.",
         tz_basis="sender_history")
M10 = msg("M10", WEILING, [PRIYA], "Re: Cost plan v2", datetime(2024, 3, 15, 14, 0, tzinfo=SGT),
          "Priya,\n\nApproved. Please issue cost plan v3 with the lighting change included.\n\nWei Ling")
M11 = msg("M11", MARY, [JOHN], "Ceiling layout for landlord approval", datetime(2024, 3, 15, 9, 47, tzinfo=SGT),
          "John, attaching the revised ceiling layout for landlord approval.\n\nMary",
          note="Gmail print-style block (name and date line, no From: label) pasted into another email.",
          tz_basis="sender_history")
M12 = msg("M12", JOHN, [MARY], "Re: Ceiling layout for landlord approval", datetime(2024, 3, 15, 11, 2, tzinfo=SGT),
          "Mary, the landlord has approved the revised ceiling layout. Please proceed.\n\nJohn",
          note="Also present as two standalone files (12a, 12b), so a split creates a new duplicate.",
          tz_basis="sender_history")
M13 = msg("M13", MARY, [PRIYA], "Landlord approval thread", datetime(2024, 3, 18, 10, 0, tzinfo=SGT),
          "Priya,\n\nFor your records, the landlord approval exchange is pasted below.\n\nMary")
M14 = msg("M14", DAVID, [WEILING], "Acoustic report", datetime(2024, 3, 18, 11, 30, tzinfo=AEDT),
          "Wei Ling,\n\nFinal acoustic report attached. Partition type P3 meets the STC 50 target.\n\nDavid",
          note="Sydney time quoted without a zone. Read as Singapore time it lands after the reply above it.",
          tz_basis="sender_history")
M15 = msg("M15", WEILING, [DAVID], "Re: Acoustic report", datetime(2024, 3, 18, 9, 15, tzinfo=SGT),
          "David,\n\nThanks. We will share the report with the contractor today.\n\nWei Ling")
M16 = msg("M16", DAVID, [WEILING], "Acoustic scope", datetime(2024, 3, 8, 14, 0, tzinfo=AEDT),
          "Wei Ling,\n\nConfirming our scope: partition testing and a final report by 18 March.\n\nDavid")
M17 = msg("M17", JOHN, [MARY], "Site access cards", None,
          "Mary, the access cards for your team are ready for collection at the\n"
          "management office.\n\nJohn",
          note="No Date header and no date in the text. Must be left for the user.",
          tz_basis="")
M18 = msg("M18", WEILING, [AHMAD], "Fwd: Acoustic report", datetime(2024, 3, 18, 9, 40, tzinfo=SGT),
          "Ahmad,\n\nPlease see David's report below. Partition type P3 is confirmed.\n\nWei Ling",
          note="A forward is its own email, not a duplicate of the email it forwards.")
COST_PLAN_V3 = (
    "Hi Wei Ling,\n\nCost plan v3 is ready. It includes lighting option B (SGD 4,200) and the\n"
    "steel ceiling frame (SGD 18,600). The revised total is SGD 412,800, which is\n"
    "within the approved budget of SGD 420,000. Contingency stays at 5 percent.\n\n"
    "Regards,\nPriya"
)
M19 = msg("M19", PRIYA, [WEILING], "Cost plan v3", datetime(2024, 3, 19, 10, 0, tzinfo=SGT), COST_PLAN_V3,
          note="Two copies: one with a different Message-ID and a mail-gateway footer (weak match only).")


def minutes(week: int, items: List[str]) -> str:
    lines = [
        f"Site meeting minutes, week {week}",
        "",
        "Attendees: John Tan, Mary Lim, Wei Ling Koh, Ahmad Rahman (BuildCo).",
        "",
        "1. Safety: no incidents reported. Hoarding inspection passed. All workers on",
        "   site must wear helmets and safety boots at all times, including visitors.",
        "2. Programme: the fit-out remains on track for handover on 30 April. BuildCo",
        "   confirmed manpower of twelve workers per day for the next two weeks.",
        "3. Drawings: the latest ceiling and lighting drawings are on the shared drive.",
        "   Please do not work from printed copies older than the current revision.",
        "4. Access: site access cards are collected at the management office between",
        "   9 am and 5 pm. Lost cards must be reported to John Tan the same day.",
        "5. Noise: drilling and hacking are allowed only between 10 am and 4 pm on",
        "   weekdays, as required by the building management.",
        "6. Actions this week:",
    ]
    lines += [f"   - {item}" for item in items]
    lines += [
        "",
        "Next meeting at the same time next week in the site office.",
        "",
        "Wei Ling",
    ]
    return "\n".join(lines)


M20 = msg("M20", WEILING, [JOHN, MARY, AHMAD], "Site meeting minutes", datetime(2024, 3, 14, 17, 0, tzinfo=SGT),
          minutes(11, ["BuildCo to start ceiling frame install.", "Mary to issue lighting layout."]),
          note="Same template as M21 a week later. Not a duplicate.")
M21 = msg("M21", WEILING, [JOHN, MARY, AHMAD], "Site meeting minutes", datetime(2024, 3, 21, 17, 0, tzinfo=SGT),
          minutes(12, ["BuildCo to finish ceiling frame install.", "Priya to issue cost plan v4."]),
          note="Same template as M20 a week earlier. Not a duplicate.")


# --- File builders ---------------------------------------------------------


@dataclass
class PlantedFile:
    name: str
    message: Message  # the top (visible) message
    body: str
    message_id: Optional[str]
    order: List[str]  # message keys in body order, top first
    cards_at_ingest: Dict[int, List[str]]  # chain_position -> message keys in that card
    linesep: str = "\n"
    extra_headers: List[Tuple[str, str]] = field(default_factory=list)
    header_order: str = "normal"  # or "resaved"
    copy_of: Optional[str] = None  # byte-identical copy of another file
    purpose: str = ""


def gmail_marker(m: Message, *, comma_before_name: bool = True, with_address: bool = True) -> str:
    # English Gmail: "On Tue, Mar 12, 2024 at 9:14 AM, John Tan <john@x> wrote:"
    local = m.date
    stamp = f"{local:%a}, {local:%b} {local.day}, {local.year} at {local.strftime('%I:%M %p').lstrip('0')}"
    sender = m.sender if with_address else m.sender.split(" <")[0]
    sep = ", " if comma_before_name else " "
    return f"On {stamp}{sep}{sender} wrote:"


FILES: List[PlantedFile] = []


def planted(*args, **kwargs) -> PlantedFile:
    f = PlantedFile(*args, **kwargs)
    FILES.append(f)
    return f


planted(
    "01_drawings_reply.eml", M2,
    f"{M2.text}\n\n{gmail_marker(M1)}\n{quote(M1.text)}\n",
    "<p01.20240312@limarchitects.example>", ["M2", "M1"], {0: ["M2"], 1: ["M1"]},
    purpose="Gmail quote: parser makes the card but sender is 'AM, John Tan' and the date is missing.",
)
planted(
    "02_lighting_fr.eml", M4,
    f"{M4.text}\n\nLe mar. 12 mars 2024 à 18:45, {CLAIRE} a écrit :\n{quote(M3.text)}\n",
    "<p02.20240313@limarchitects.example>", ["M4", "M3"], {0: ["M4", "M3"]},
    purpose="French Gmail marker: the parser misses it, so two emails share one card.",
)
planted(
    "03_claire_spec.eml", M5, M5.text + "\n",
    "<p03.20240314@atelier-lumiere.example>", ["M5"], {0: ["M5"]},
    purpose="Gives Claire a dated email so her time zone can be inferred.",
)
planted(
    "04_structure_de.eml", M7,
    f"{M7.text}\n\nAm Mi., 13. März 2024 um 10:30 Uhr schrieb {HANS}:\n{quote(M6.text)}\n",
    "<p04.20240314@harbourview.example>", ["M7", "M6"], {0: ["M7", "M6"]},
    purpose="German Gmail marker: the parser misses it, so two emails share one card.",
)
planted(
    "05_hans_calc.eml", M8, M8.text + "\n",
    "<p05.20240315@weber-statik.example>", ["M8"], {0: ["M8"]},
    purpose="Gives Hans a dated email so his time zone can be inferred.",
)
planted(
    "06_costplan_v2_reply.eml", M10,
    f"{M10.text}\n\nOn Thu, 14 Mar 2024 at 16:20, Priya Nair wrote:\n{quote(M9.text)}\n",
    "<p06.20240315@harbourview.example>", ["M10", "M9"], {0: ["M10"], 1: ["M9"]},
    purpose="Gmail quote with no address and a 24-hour time: quoted card is undated.",
)
planted(
    "07_landlord_print.eml", M13,
    (
        f"{M13.text}\n\n"
        f"{JOHN}\tFri, Mar 15, 2024 at 11:02 AM\nTo: {MARY}\n\n{M12.text}\n\n"
        f"{MARY}\tFri, Mar 15, 2024 at 9:47 AM\nTo: {JOHN}\n\n{M11.text}\n"
    ),
    "<p07.20240318@limarchitects.example>", ["M13", "M12", "M11"], {0: ["M13", "M12", "M11"]},
    purpose="Gmail print-style blocks pasted in: no From: label, so three emails share one card.",
)
planted(
    "08_acoustic_reply.eml", M15,
    f"{M15.text}\n\n{gmail_marker(M14, comma_before_name=False)}\n{quote(M14.text)}\n",
    "<p08.20240318@harbourview.example>", ["M15", "M14"], {0: ["M15"], 1: ["M14"]},
    purpose="Sydney time read as Singapore time: chain date conflict.",
)
planted(
    "09_acoustic_scope.eml", M16, M16.text + "\n",
    "<p09.20240308@brooks-acoustics.example>", ["M16"], {0: ["M16"]},
    purpose="Gives David a dated email so his time zone can be inferred.",
)
planted(
    "10_access_cards_undated.eml", M17, M17.text + "\n",
    "<p10@harbourview.example>", ["M17"], {0: ["M17"]},
    purpose="No date anywhere: must stay 'left for you'.",
)
planted(
    "11_drawings_reply_copy.eml", M2, "", None, ["M2", "M1"], {0: ["M2"], 1: ["M1"]},
    copy_of="01_drawings_reply.eml",
    purpose="Byte-identical copy of file 01: exact duplicates.",
)
planted(
    "12a_landlord_approval.eml", M12, M12.text + "\n",
    "<p12.20240315@harbourview.example>", ["M12"], {0: ["M12"]},
    purpose="Standalone copy of M12 (also hidden inside file 07).",
)
planted(
    "12b_landlord_approval_resaved.eml", M12, M12.text + "\n",
    "<p12.20240315@harbourview.example>", ["M12"], {0: ["M12"]},
    linesep="\r\n", header_order="resaved",
    extra_headers=[("X-Saved-By", "Outlook for Mac 16.83")],
    purpose="Same Message-ID and body as 12a, different bytes: strong duplicate.",
)
planted(
    "13_fwd_acoustic.eml", M18,
    (
        f"{M18.text}\n\n---------- Forwarded message ---------\n"
        f"From: {DAVID}\nDate: Mon, Mar 18, 2024 at 11:30 AM\nSubject: {M14.subject}\n"
        f"To: {WEILING}\n\n{M14.text}\n"
    ),
    "<p13.20240318@harbourview.example>", ["M18", "M14"], {0: ["M18"], 1: ["M14"]},
    purpose="Forward of M14: quoted card duplicates file 08's quoted card; the forward itself is not a duplicate.",
)
planted(
    "14_costplan_v3.eml", M19, M19.text + "\n",
    "<p14.20240319@qsconsult.example>", ["M19"], {0: ["M19"]},
    purpose="Original of M19.",
)
planted(
    "15_costplan_v3_received.eml", M19,
    M19.text + "\n\n[External] Scanned by MailGuard for Harbour View. Report suspicious mail to it-help@harbourview.example.\n",
    "<p15.mailguard.20240319@harbourview.example>", ["M19"], {0: ["M19"]},
    purpose="Received copy of M19: new Message-ID and a gateway footer, so only weak reasons match.",
)
planted(
    "16_minutes_wk11.eml", M20, M20.text + "\n",
    "<p16.20240314@harbourview.example>", ["M20"], {0: ["M20"]},
    purpose="Weekly minutes, same template as file 17. Not a duplicate.",
)
planted(
    "17_minutes_wk12.eml", M21, M21.text + "\n",
    "<p17.20240321@harbourview.example>", ["M21"], {0: ["M21"]},
    purpose="Weekly minutes, same template as file 16. Not a duplicate.",
)


def build_eml(f: PlantedFile) -> bytes:
    m = f.message
    policy = default_policy.clone(linesep=f.linesep)
    em = EmailMessage(policy=policy)
    headers: List[Tuple[str, str]] = [
        ("From", m.sender),
        ("To", ", ".join(m.to)),
        ("Subject", m.subject),
    ]
    if m.date is not None:
        headers.append(("Date", format_datetime(m.date)))
    if f.message_id:
        headers.append(("Message-ID", f.message_id))
    headers.extend(f.extra_headers)
    if f.header_order == "resaved":
        headers = list(reversed(headers))
    for name, value in headers:
        em[name] = value
    em.set_content(f.body, charset="utf-8")
    return em.as_bytes(policy=policy)


def write_all(out_dir: Path = OUT_DIR, ground_truth_path: Path = GROUND_TRUTH) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.eml"):
        old.unlink()
    written: Dict[str, bytes] = {}
    for f in FILES:
        data = written[f.copy_of] if f.copy_of else build_eml(f)
        written[f.name] = data
        (out_dir / f.name).write_bytes(data)

    truth = {
        "case": CASE_NAME,
        "timezone": CASE_TZ,
        "rules": {
            "duplicate": "Two cards are true duplicates if and only if they hold the same message key.",
            "split": "A card whose cards_at_ingest entry lists more than one key needs a split, one part per key, in that order.",
            "date": "date_utc is the true time. tz_basis 'sender_history' means the visible text has no zone and the zone comes from the same sender's dated emails.",
        },
        "messages": {key: m.ground_truth() for key, m in MESSAGES.items()},
        "files": {
            f.name: {
                "order": f.order,
                "cards_at_ingest": {str(pos): keys for pos, keys in f.cards_at_ingest.items()},
                "copy_of": f.copy_of,
                "purpose": f.purpose,
            }
            for f in FILES
        },
    }
    ground_truth_path.write_text(json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {name: out_dir / name for name in written}


if __name__ == "__main__":
    paths = write_all()
    print(f"Wrote {len(paths)} .eml files to {OUT_DIR} and {GROUND_TRUTH}")
