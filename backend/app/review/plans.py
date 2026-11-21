"""Review plans: storage, fingerprints and the all-or-nothing apply gate.

Shared by both AI review tools (duplicates and chronology). A tool builds a
plan, a person approves or declines it in the emailchrono web app (or with the
backup script scripts/approve_plan.py), and only then can the plan be applied.
Nothing in this module approves a plan; see approval.py.

Apply checks, in order: the plan exists and belongs to the case, it is
approved (an applied plan returns its stored receipt), it has not expired, the
rows it touches have not changed since it was built, and every item is still
valid. All items then run in one SQLite transaction: all succeed or none do.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from .. import db
from ..services.audit import record_audit_event

PLAN_TTL = timedelta(minutes=30)
KINDS = ("chronology", "duplicates")
OPEN_STATUSES = ("planned", "approved")

# Test and benchmark hook: fail on purpose after this many items have been
# applied, to prove the rollback. Set only in the environment of the process
# that applies; the model cannot set it.
FAIL_AFTER_ENV = "EMAILCHRONO_REVIEW_FAIL_AFTER"

EMAIL_FINGERPRINT_COLUMNS = (
    "id", "case_id", "deleted_at", "superseded_at", "source_kind", "chain_position",
    "message_id", "source_sha256",
    "from_addr", "to_json", "cc_json", "subject", "date_utc", "date_raw", "body_text",
)
CANDIDATE_FINGERPRINT_COLUMNS = (
    "id", "case_id", "email_a_id", "email_b_id", "status",
    "canonical_email_id", "duplicate_email_id", "reason_json",
)
_FINGERPRINT_TABLES = {
    "emails": ("emails", EMAIL_FINGERPRINT_COLUMNS),
    "duplicate_candidates": ("email_duplicate_candidates", CANDIDATE_FINGERPRINT_COLUMNS),
}

ApplyItem = Callable[[sqlite3.Connection, Dict[str, Any]], Optional[Iterable[int]]]
ValidateItem = Callable[[sqlite3.Connection, Dict[str, Any]], Optional[str]]


class PlanError(ValueError):
    """A plan could not be built (bad kind, missing case)."""


class InjectedFailure(RuntimeError):
    """Raised on purpose by the FAIL_AFTER_ENV hook."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_fingerprint(conn: sqlite3.Connection, touched: Dict[str, Iterable[int]]) -> str:
    """Hash the current state of every row a plan touches (missing rows count too)."""
    state: Dict[str, List[Any]] = {}
    for key, (table, columns) in _FINGERPRINT_TABLES.items():
        ids = sorted({int(value) for value in touched.get(key, [])})
        rows: Dict[int, List[Any]] = {}
        if ids:
            placeholders = ",".join("?" for _ in ids)
            for row in conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE id IN ({placeholders})", ids
            ):
                rows[int(row["id"])] = [row[column] for column in columns]
        state[key] = [[row_id, rows.get(row_id)] for row_id in ids]
    return _sha256(_canonical_json(state))


def _row_to_plan(row: sqlite3.Row) -> Dict[str, Any]:
    plan = dict(row)
    plan["items"] = json.loads(plan.pop("items_json"))
    plan["left_for_you"] = json.loads(plan.pop("left_for_you_json"))
    plan["warnings"] = json.loads(plan.pop("warnings_json"))
    plan["touched"] = json.loads(plan.pop("touched_json"))
    receipt = plan.pop("receipt_json")
    plan["receipt"] = json.loads(receipt) if receipt else None
    return plan


def _is_due(row: sqlite3.Row) -> bool:
    return row["status"] in OPEN_STATUSES and row["expires_at"] <= _now().isoformat()


def expire_if_due(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Mark an open plan past its expiry as expired. Returns the current status.

    The update is guarded on status, so a plan applied since ``row`` was read stays applied."""
    if _is_due(row):
        conn.execute(
            "UPDATE review_plans SET status = 'expired' WHERE id = ? AND status IN ('planned', 'approved')",
            (row["id"],),
        )
        current = conn.execute("SELECT status FROM review_plans WHERE id = ?", (row["id"],)).fetchone()
        return current["status"] if current else "missing"
    return row["status"]


def create_plan(
    case_id: int,
    kind: str,
    *,
    items: List[Dict[str, Any]],
    left_for_you: List[Dict[str, Any]],
    summary: str,
    touched: Dict[str, Iterable[int]],
    warnings: Iterable[str] = (),
    created_by: str = "",
    check: Optional[Callable[[sqlite3.Connection], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Store a new plan in status 'planned'. Changes no case data.

    ``check`` runs inside the same write transaction that computes the
    fingerprint, so the items the person sees and the fingerprint describe the
    same state. It returns an error string to refuse (raised as PlanError)."""
    if kind not in KINDS:
        raise PlanError(f"Unknown plan kind {kind!r}")
    touched_clean = {key: sorted({int(v) for v in touched.get(key, [])}) for key in _FINGERPRINT_TABLES}
    items_json = _canonical_json(items)
    now = _now()
    plan_id = "rp_" + secrets.token_hex(6)
    conn = db.connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        case = conn.execute(
            "SELECT id FROM cases WHERE id = ? AND deleted_at IS NULL", (case_id,)
        ).fetchone()
        if not case:
            raise PlanError(f"Case {case_id} not found")
        problem = check(conn) if check is not None else None
        if problem:
            raise PlanError(problem)
        conn.execute(
            """
            INSERT INTO review_plans
              (id, case_id, kind, status, summary, items_json, left_for_you_json, warnings_json,
               touched_json, fingerprint, items_hash, created_by, created_at, expires_at)
            VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id, case_id, kind, summary, items_json,
                _canonical_json(left_for_you), _canonical_json(list(warnings)),
                _canonical_json(touched_clean), compute_fingerprint(conn, touched_clean),
                _sha256(items_json), created_by, now.isoformat(), (now + PLAN_TTL).isoformat(),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    plan = get_plan(plan_id)
    assert plan is not None
    return plan


def get_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM review_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            return None
        if expire_if_due(conn, row) != row["status"]:
            row = conn.execute("SELECT * FROM review_plans WHERE id = ?", (plan_id,)).fetchone()
        return _row_to_plan(row)


def refresh_status(plan_id: str) -> Optional[Dict[str, Any]]:
    """The plan with its status brought up to date: expired if past its expiry, or
    stale if a row it touches changed while it waited for approval (the web app
    will not let anyone approve it then). Changes no case data. None if missing."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM review_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            return None
        expire_if_due(conn, row)
        row = conn.execute("SELECT * FROM review_plans WHERE id = ?", (plan_id,)).fetchone()
        if row["status"] == "planned" and compute_fingerprint(conn, json.loads(row["touched_json"])) != row["fingerprint"]:
            conn.execute("UPDATE review_plans SET status = 'stale' WHERE id = ? AND status = 'planned'", (plan_id,))
            row = conn.execute("SELECT * FROM review_plans WHERE id = ?", (plan_id,)).fetchone()
        return _row_to_plan(row)


def list_plans(case_id: int, statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    with db.get_conn() as conn:
        for row in conn.execute("SELECT * FROM review_plans WHERE case_id = ?", (case_id,)).fetchall():
            expire_if_due(conn, row)
        rows = conn.execute(
            "SELECT * FROM review_plans WHERE case_id = ? ORDER BY created_at DESC", (case_id,)
        ).fetchall()
    wanted = set(statuses) if statuses else None
    return [_row_to_plan(row) for row in rows if wanted is None or row["status"] in wanted]


def plan_in_flight(case_id: int, kind: str) -> Optional[Dict[str, Any]]:
    """The newest plan of this kind that is still planned or approved, if any."""
    open_plans = [plan for plan in list_plans(case_id, OPEN_STATUSES) if plan["kind"] == kind]
    return open_plans[0] if open_plans else None


def _refusal(code: str, reason: str, plan_id: Optional[str] = None) -> Dict[str, Any]:
    return {"status": "refused", "code": code, "plan_id": plan_id, "reason": reason, "changed": False}


def _fail_after() -> Optional[int]:
    value = os.environ.get(FAIL_AFTER_ENV, "").strip()
    return int(value) if value.isdigit() else None


def apply_plan(
    case_id: int,
    plan_id: str,
    kind: str,
    *,
    validate_item: ValidateItem,
    apply_item: ApplyItem,
    after_commit: Optional[Callable[[int, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Apply an approved plan in one transaction, or refuse and change nothing.

    ``validate_item`` returns an error string for an item that is no longer valid.
    ``apply_item`` makes one change on the given connection and returns the audit
    event ids it wrote. Neither may commit.
    """
    conn = db.connect()
    conn.isolation_level = None  # manual transactions
    receipt: Dict[str, Any]
    try:
        conn.execute("BEGIN IMMEDIATE")  # take the write lock before checking
        row = conn.execute("SELECT * FROM review_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return _refusal("not_found", f"No review plan with id {plan_id}. Build a new plan.", plan_id)
        if int(row["case_id"]) != int(case_id):
            conn.execute("ROLLBACK")
            return _refusal("wrong_case", f"Plan {plan_id} belongs to case {row['case_id']}, not case {case_id}.", plan_id)
        if row["kind"] != kind:
            conn.execute("ROLLBACK")
            return _refusal("wrong_kind", f"Plan {plan_id} is a {row['kind']} plan, not a {kind} plan.", plan_id)

        status = row["status"]
        if status == "applied":
            conn.execute("ROLLBACK")
            return {
                "status": "applied",
                "already_applied": True,
                "plan_id": plan_id,
                "receipt": json.loads(row["receipt_json"]),
                "changed": False,
                "message": f"Plan {plan_id} was already applied. Nothing more was changed.",
            }
        if _is_due(row):
            conn.execute("UPDATE review_plans SET status = 'expired' WHERE id = ?", (plan_id,))
            conn.execute("COMMIT")
            return _refusal("expired", f"Plan {plan_id} expired before it was applied. Build a new plan.", plan_id)
        if status == "planned":
            conn.execute("ROLLBACK")
            return {
                "status": "waiting_for_approval",
                "plan_id": plan_id,
                "changed": False,
                "message": (
                    f"Plan {plan_id} is waiting for approval in emailchrono. Nothing was changed. "
                    "The user must open the case in emailchrono and approve or decline it."
                ),
            }
        if status == "declined":
            conn.execute("ROLLBACK")
            return _refusal("declined", f"The user declined plan {plan_id}. Do not apply it.", plan_id)
        if status in ("stale", "expired"):
            conn.execute("ROLLBACK")
            return _refusal(status, f"Plan {plan_id} is {status}. Build a new plan.", plan_id)
        if (
            status != "approved"
            or row["approved_items_hash"] != row["items_hash"]
            or _sha256(row["items_json"]) != row["approved_items_hash"]
        ):
            conn.execute("ROLLBACK")
            return _refusal("not_approved", f"Plan {plan_id} has no valid approval.", plan_id)

        touched = json.loads(row["touched_json"])
        if compute_fingerprint(conn, touched) != row["fingerprint"]:
            conn.execute("UPDATE review_plans SET status = 'stale' WHERE id = ?", (plan_id,))
            conn.execute("COMMIT")
            return _refusal(
                "stale",
                f"The case changed after plan {plan_id} was built, so it was not applied. Build a new plan.",
                plan_id,
            )

        items = json.loads(row["items_json"])
        for index, item in enumerate(items):
            problem = validate_item(conn, item)
            if problem:
                conn.execute("UPDATE review_plans SET status = 'stale' WHERE id = ?", (plan_id,))
                conn.execute("COMMIT")
                return _refusal(
                    "invalid_item",
                    f"Item {index + 1} of plan {plan_id} is no longer valid ({problem}). Nothing was changed. Build a new plan.",
                    plan_id,
                )

        fail_after = _fail_after()
        event_ids: List[int] = []
        for index, item in enumerate(items):
            if fail_after is not None and index >= fail_after:
                raise InjectedFailure(f"injected failure before item {index + 1}")
            event_ids.extend(int(e) for e in (apply_item(conn, item) or []))

        now = _now().isoformat()
        conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
        record_audit_event(
            conn,
            case_id=case_id,
            action="review_plan.applied",
            entity_type="review_plan",
            metadata={
                "plan_id": plan_id,
                "kind": kind,
                "item_count": len(items),
                "approved_via": row["approved_via"],
                "approved_at": row["approved_at"],
                "event_ids": event_ids,
            },
            actor=f"ai_review (approved via {row['approved_via']})",
        )
        plan_event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        receipt = {
            "plan_id": plan_id,
            "applied_at": now,
            "item_count": len(items),
            "event_ids": event_ids,
            "plan_event_id": plan_event_id,
        }
        conn.execute(
            "UPDATE review_plans SET status = 'applied', applied_at = ?, receipt_json = ? WHERE id = ?",
            (now, _canonical_json(receipt), plan_id),
        )
        conn.execute("COMMIT")
    except Exception as exc:  # roll back everything, whatever failed
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        return _refusal(
            "apply_failed",
            f"Applying plan {plan_id} failed and was rolled back, so nothing changed: {exc}",
            plan_id,
        )
    finally:
        conn.close()

    result: Dict[str, Any] = {
        "status": "applied",
        "already_applied": False,
        "plan_id": plan_id,
        "receipt": receipt,
        "changed": True,
        "message": f"Applied plan {plan_id}: {receipt['item_count']} change(s), each recorded in the audit trail.",
    }
    if after_commit is not None:
        try:
            after_commit(case_id, get_plan(plan_id) or {})
        except Exception as exc:  # derived data only; the applied changes stand
            result["warning"] = f"Applied, but refreshing derived data failed: {exc}"
    return result
