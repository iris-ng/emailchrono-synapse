"""Duplicate review adapters.

Small, documented steps that Synapse composes into the resolve_case_duplicates
tool, and that small_tools_server.py exposes one by one. The flow is:

    check_case_ready_for_duplicate_review -> list_pending_duplicate_pairs
    -> propose_duplicate_decision (per pair) -> save_duplicate_plan
    ... a person approves the plan in emailchrono ...
    -> apply_duplicate_plan

Decisions are made per pair, never per group. Pairs with an exact reason are
proposed as duplicates. Pairs with only weak reasons are proposed as "not a
duplicate" when there is evidence they differ, and otherwise left for the user.
Nothing here approves a plan.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from .. import db
from ..repos import duplicates as duplicate_repo
from ..repos.duplicates import _write_candidate_decision
from ..services.duplicates import EXACT_REASON_CODES, same_place_in_file, suggest_canonical
from ..services.email_text import find_next_quoted_boundary
from ..services.text_norm import canonical_body, similarity_ratio
from . import plans, problems

ADDRESS_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
FORWARD_RE = re.compile(r"(?im)^-{3,}\s*Forwarded message\s*-{3,}\s*$")
DIFFERENT_TEXT_BELOW = 0.5  # own-text similarity under this counts as different text
DAYS_APART_ABOVE = 1.0


# --- helpers ---------------------------------------------------------------


def _sender_address(value: Optional[str]) -> Optional[str]:
    match = ADDRESS_RE.search(value or "")
    return match.group(0).lower() if match else None


def _first_line(body: Optional[str]) -> str:
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def _own_text(body: Optional[str]) -> str:
    """The email's own words: text above the first quote or forward marker, minus '>' lines."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    cut = find_next_quoted_boundary(text, 0)
    forward = FORWARD_RE.search(text)
    if forward:
        cut = min(cut, forward.start())
    lines = [line for line in text[:cut].split("\n") if not line.lstrip().startswith(">")]
    return canonical_body("\n".join(lines))


def _days_apart(left: Optional[str], right: Optional[str]) -> Optional[float]:
    if not left or not right:
        return None
    try:
        return abs((datetime.fromisoformat(left) - datetime.fromisoformat(right)).total_seconds()) / 86400
    except ValueError:
        return None


def _summary(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "from": row["from_addr"],
        "date_utc": row["date_utc"],
        "subject": row["subject"],
        "first_line": _first_line(row["body_text"]),
        "source_kind": row["source_kind"],
    }


def _email_rows(conn: sqlite3.Connection, ids: Iterable[int]) -> Dict[int, sqlite3.Row]:
    ids = sorted(set(ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    return {
        row["id"]: row
        for row in conn.execute(f"SELECT * FROM emails WHERE id IN ({placeholders})", ids)
    }


def _is_live(row: Optional[sqlite3.Row], case_id: int) -> bool:
    return bool(
        row is not None
        and int(row["case_id"]) == int(case_id)
        and row["deleted_at"] is None
        and row["superseded_at"] is None
    )


def _case_exists(case_id: int) -> bool:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM cases WHERE id = ? AND deleted_at IS NULL", (case_id,)
        ).fetchone() is not None


# --- adapters --------------------------------------------------------------


def check_case_ready_for_duplicate_review(case_id: int) -> Dict[str, Any]:
    """Check a case can get a duplicate plan. Returns {ok, reason, warnings}. ok is False if the case is missing or a chronology plan is still waiting or approved."""
    if not _case_exists(case_id):
        return {"ok": False, "case_id": case_id, "reason": f"Case {case_id} not found.", "warnings": []}
    chrono = plans.plan_in_flight(case_id, "chronology")
    if chrono:
        return {
            "ok": False,
            "case_id": case_id,
            "reason": (
                f"A chronology plan ({chrono['id']}, {chrono['status']}) for case {case_id} is not applied yet. "
                "Apply or decline it first, because splitting cards changes the duplicate pairs."
            ),
            "warnings": [],
        }
    warnings = []
    problem_cards = problems.find_problem_cards(case_id)
    if problem_cards:
        warnings.append(
            f"{len(problem_cards)} card(s) still have chronology problems (merged, undated, date conflicts or unclear senders). "
            "A chronology review may create new duplicate pairs later."
        )
    return {"ok": True, "case_id": case_id, "reason": "", "warnings": warnings}


def list_pending_duplicate_pairs(case_id: int) -> List[Dict[str, Any]]:
    """List pending duplicate pairs of a case: candidate_id, email_a, email_b, reasons, group_id, suggested_keeper_id and difference evidence. Read only."""
    clusters = duplicate_repo.list_duplicate_clusters(case_id)
    group_of: Dict[int, Dict[str, Any]] = {}
    for cluster in clusters:
        for candidate_id in cluster["candidate_ids"]:
            group_of[candidate_id] = cluster
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM email_duplicate_candidates
            WHERE case_id = ? AND status = 'pending'
            ORDER BY score DESC, id ASC
            """,
            (case_id,),
        ).fetchall()
        emails = _email_rows(conn, [r["email_a_id"] for r in rows] + [r["email_b_id"] for r in rows])
    pairs = []
    for row in rows:
        a, b = emails.get(row["email_a_id"]), emails.get(row["email_b_id"])
        if a is None or b is None:
            continue
        reasons = json.loads(row["reason_json"] or "[]")
        ignored = []
        if not same_place_in_file(dict(a), dict(b)):
            # Pairs saved before the scorer fix can still carry this reason for a
            # parent and its own quoted reply. Re-check it instead of trusting it.
            ignored = [r for r in reasons if r.get("code") == "same_source_sha256"]
            reasons = [r for r in reasons if r.get("code") != "same_source_sha256"]
        cluster = group_of.get(row["id"])
        own_a, own_b = _own_text(a["body_text"]), _own_text(b["body_text"])
        pairs.append(
            {
                "candidate_id": row["id"],
                "case_id": case_id,
                "score": row["score"],
                "reasons": [r.get("code") for r in reasons],
                "reason_labels": [r.get("label") for r in reasons],
                "ignored_reasons": [
                    f"{r.get('code')} (the rows sit at different places in one file)" for r in ignored
                ],
                "strength": "strong" if any(r.get("code") in EXACT_REASON_CODES for r in reasons) else "weak",
                "group_id": f"group-{min(cluster['email_ids'])}" if cluster else f"group-{a['id']}",
                "group_email_ids": cluster["email_ids"] if cluster else [a["id"], b["id"]],
                "group_keeper_id": cluster["suggested_canonical_id"] if cluster else None,
                "suggested_keeper_id": suggest_canonical([dict(a), dict(b)]),
                "email_a": _summary(a),
                "email_b": _summary(b),
                "sender_addresses": [_sender_address(a["from_addr"]), _sender_address(b["from_addr"])],
                "days_apart": _days_apart(a["date_utc"], b["date_utc"]),
                "own_text_similarity": (
                    round(similarity_ratio(own_a, own_b, 0.0), 3) if own_a and own_b else None
                ),
            }
        )
    return pairs


def propose_duplicate_decision(pair: Dict[str, Any]) -> Dict[str, Any]:
    """Propose a decision for one pair from list_pending_duplicate_pairs: duplicate (exact reason), dissimilar (evidence they differ) or left_for_you. Pure, no writes."""
    a_id, b_id = pair["email_a"]["id"], pair["email_b"]["id"]
    labels = dict(zip(pair["reasons"], pair["reason_labels"]))
    base = {
        "candidate_id": pair["candidate_id"],
        "email_ids": [a_id, b_id],
        "reasons": pair["reasons"],
        "keeper_id": None,
        "duplicate_id": None,
    }
    exact = [code for code in pair["reasons"] if code in EXACT_REASON_CODES]
    if exact:
        keeper = pair["suggested_keeper_id"]
        return {
            **base,
            "decision": "duplicate",
            "keeper_id": keeper,
            "duplicate_id": b_id if keeper == a_id else a_id,
            "evidence": [labels[code] for code in exact],
            "why": f"Exact match ({', '.join(exact)}). Keep #{keeper}, the better-parsed copy.",
        }

    evidence = []
    left_addr, right_addr = pair.get("sender_addresses") or [None, None]
    if left_addr and right_addr and left_addr != right_addr:
        evidence.append(f"Different senders: {left_addr} and {right_addr}")
    days = pair.get("days_apart")
    if days is not None and days > DAYS_APART_ABOVE:
        evidence.append(f"Dates {days:.1f} days apart")
    similarity = pair.get("own_text_similarity")
    if similarity is not None and similarity < DIFFERENT_TEXT_BELOW:
        evidence.append(f"Different own text once quoted text is removed ({round(similarity * 100)}% similar)")
    if evidence:
        return {**base, "decision": "dissimilar", "evidence": evidence, "why": "Weak match only, and the evidence shows different emails."}
    return {
        **base,
        "decision": "left_for_you",
        "evidence": [],
        "why": f"Only weak reasons ({', '.join(pair['reasons'])}) and no evidence they differ. A person should decide.",
    }


def _summarize(items: List[Dict[str, Any]], left: List[Dict[str, Any]], warnings: List[str]) -> str:
    dup = sum(1 for i in items if i["status"] == "duplicate")
    diss = sum(1 for i in items if i["status"] == "dissimilar")
    parts = [f"{dup} pair(s) marked duplicate", f"{diss} marked not duplicate", f"{len(left)} left for you"]
    text = "Duplicate plan: " + ", ".join(parts) + ". Waiting for approval in emailchrono; nothing has changed yet."
    if warnings:
        text += " Warnings: " + " ".join(warnings)
    return text


def save_duplicate_plan(case_id: int, decisions: List[Dict[str, Any]], warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """Store decisions from propose_duplicate_decision as a plan waiting for human approval in emailchrono. Checks keepers. Returns plan_id and summary. Changes no case data."""
    if not _case_exists(case_id):
        return {"ok": False, "error": f"Case {case_id} not found."}
    chrono = plans.plan_in_flight(case_id, "chronology")
    if chrono:
        return {
            "ok": False,
            "error": f"A chronology plan ({chrono['id']}) for case {case_id} is not applied yet. Apply or decline it first.",
        }
    if warnings is None:
        warnings = check_case_ready_for_duplicate_review(case_id).get("warnings", [])

    pairs = {p["candidate_id"]: p for p in list_pending_duplicate_pairs(case_id)}
    with db.get_conn() as conn:
        emails = _email_rows(conn, [i for p in pairs.values() for i in (p["email_a"]["id"], p["email_b"]["id"])])

    invalid: List[str] = []
    items: List[Dict[str, Any]] = []
    left: List[Dict[str, Any]] = []
    seen = set()
    for decision in decisions or []:
        candidate_id = decision.get("candidate_id")
        pair = pairs.get(candidate_id)
        if pair is None:
            invalid.append(f"candidate {candidate_id} is not a pending pair in case {case_id}")
            continue
        if candidate_id in seen:
            invalid.append(f"candidate {candidate_id} appears more than once")
            continue
        seen.add(candidate_id)
        a_id, b_id = pair["email_a"]["id"], pair["email_b"]["id"]
        rule = propose_duplicate_decision(pair)
        choice = decision.get("decision")
        if choice == "left_for_you":
            left.append(
                {"candidate_id": candidate_id, "email_ids": [a_id, b_id], "reasons": pair["reasons"],
                 "why": decision.get("why") or rule["why"], "email_a": pair["email_a"], "email_b": pair["email_b"]}
            )
            continue
        if choice not in ("duplicate", "dissimilar"):
            invalid.append(f"candidate {candidate_id}: unknown decision {choice!r}")
            continue
        if not (_is_live(emails.get(a_id), case_id) and _is_live(emails.get(b_id), case_id)):
            invalid.append(f"candidate {candidate_id}: one of its emails is not a live email in case {case_id}")
            continue
        keeper_id = duplicate_id = None
        if choice == "duplicate":
            keeper_id, duplicate_id = decision.get("keeper_id"), decision.get("duplicate_id")
            if keeper_id not in (a_id, b_id) or duplicate_id not in (a_id, b_id) or keeper_id == duplicate_id:
                invalid.append(
                    f"candidate {candidate_id}: keeper {keeper_id} must be one of the pair ({a_id}, {b_id}) "
                    "and differ from the duplicate"
                )
                continue
        items.append(
            {
                "op": "decide_duplicate_pair",
                "candidate_id": candidate_id,
                "status": choice,
                "keeper_id": keeper_id,
                "duplicate_id": duplicate_id,
                "email_ids": [a_id, b_id],
                "reasons": pair["reasons"],
                "reason_labels": pair["reason_labels"],
                # Evidence is always recomputed here from the database, never copied from the caller.
                "evidence": rule["evidence"] if rule["decision"] == choice else [],
                "agent_override": rule["decision"] != choice,
                "rule_decision": rule["decision"],
                "email_a": pair["email_a"],
                "email_b": pair["email_b"],
            }
        )

    if invalid:
        return {"ok": False, "error": "Plan not saved: " + "; ".join(invalid), "invalid": invalid}
    if not items:
        if left:
            summary = (
                f"Nothing to do automatically: {len(left)} pair(s) need a person's decision in emailchrono."
            )
        else:
            summary = f"Nothing to do: case {case_id} has no pending duplicate pairs to decide."
        return {"ok": True, "plan_id": None, "status": "nothing_to_do", "summary": summary, "left_for_you": left}

    summary = _summarize(items, left, warnings)
    try:
        plan = plans.create_plan(
            case_id,
            "duplicates",
            items=items,
            left_for_you=left,
            summary=summary,
            warnings=warnings,
            touched={
                "emails": [i for item in items for i in item["email_ids"]],
                "duplicate_candidates": [item["candidate_id"] for item in items],
            },
            created_by="resolve_case_duplicates",
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
            "duplicate": sum(1 for i in items if i["status"] == "duplicate"),
            "dissimilar": sum(1 for i in items if i["status"] == "dissimilar"),
            "left_for_you": len(left),
        },
        "expires_at": plan["expires_at"],
        "warnings": warnings,
        "next_step": "Ask the user to review and approve the plan in emailchrono, then call again with this plan_id.",
    }


def _snapshot_check(case_id: int, items: List[Dict[str, Any]]):
    """Inside plan creation: refuse if any pair or card changed since the items were built."""

    def check(conn: sqlite3.Connection) -> Optional[str]:
        validate = _validate_item(case_id)
        for item in items:
            problem = validate(conn, item)
            if problem:
                return f"the case changed while the plan was being built ({problem}). Build it again."
            emails = _email_rows(conn, item["email_ids"])
            for shown in (item["email_a"], item["email_b"]):
                if _summary(emails[shown["id"]]) != shown:
                    return f"card #{shown['id']} changed while the plan was being built. Build it again."
        return None

    return check


def _validate_item(case_id: int):
    def validate(conn: sqlite3.Connection, item: Dict[str, Any]) -> Optional[str]:
        row = conn.execute(
            "SELECT * FROM email_duplicate_candidates WHERE id = ?", (item["candidate_id"],)
        ).fetchone()
        if row is None or int(row["case_id"]) != int(case_id):
            return f"pair {item['candidate_id']} no longer exists"
        if row["status"] != "pending":
            return f"pair {item['candidate_id']} was already decided ({row['status']})"
        pair_ids = {row["email_a_id"], row["email_b_id"]}
        if set(item["email_ids"]) != pair_ids:
            return f"pair {item['candidate_id']} now links different emails"
        emails = _email_rows(conn, pair_ids)
        if not all(_is_live(emails.get(i), case_id) for i in pair_ids):
            return f"an email in pair {item['candidate_id']} was deleted or split"
        if item["status"] == "duplicate" and item["keeper_id"] not in pair_ids:
            return f"keeper {item['keeper_id']} is not in pair {item['candidate_id']}"
        return None

    return validate


def _apply_item(conn: sqlite3.Connection, item: Dict[str, Any]) -> List[int]:
    row = conn.execute(
        "SELECT * FROM email_duplicate_candidates WHERE id = ?", (item["candidate_id"],)
    ).fetchone()
    _write_candidate_decision(
        conn,
        row,
        status=item["status"],
        canonical_email_id=item["keeper_id"] if item["status"] == "duplicate" else None,
        duplicate_email_id=item["duplicate_id"] if item["status"] == "duplicate" else None,
    )
    return [int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])]


def apply_duplicate_plan(case_id: int, plan_id: str) -> Dict[str, Any]:
    """Apply an approved duplicate plan in one transaction. Returns status applied, waiting_for_approval or refused with a reason. Never approves a plan."""
    return plans.apply_plan(
        case_id,
        plan_id,
        "duplicates",
        validate_item=_validate_item(case_id),
        apply_item=_apply_item,
    )
