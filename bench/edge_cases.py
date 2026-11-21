#!/usr/bin/env python3
"""Live rehearsal of error and edge cases with the composed agent.

Each scenario runs in its own process on a fresh copy of the planted case, with
the real model and a scripted "person" that acts the way a person can in the
web app: approve, decline, edit a card, or do nothing. Approvals are recorded
with via="bench". The script prints what the agent said and checks the case
afterwards. It is a rehearsal for the demo, not a benchmark.

Usage (from the repo root; needs the OpenAI key in secrets.local):
    .venv/bin/python bench/edge_cases.py
    .venv/bin/python bench/edge_cases.py --only stale expired
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
DEFAULT_MODEL = os.environ.get("EMAILCHRONO_AGENT_MODEL", "gpt-5.6-luna")
DUPES = "Clean up the duplicate emails in case {case}."

# name: (turns, person actions per approval wait, extra environment, what should happen)
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "decline": {
        "turns": [DUPES],
        "person": ["decline"],
        "expect": "The person declines. Nothing changes and the agent does not try to apply that plan again.",
    },
    "stale": {
        "turns": [DUPES],
        "person": ["edit_only", "approve"],
        "expect": "The person edits a card in the plan, so Approve is disabled. The agent learns the plan is stale, builds a new one once, and that one is approved and applied.",
    },
    "expired": {
        "turns": [DUPES],
        "person": ["let_expire", "approve"],
        "expect": "The plan expires before anyone acts. The agent builds a new plan once; it is approved and applied.",
    },
    "repeat": {
        "turns": [DUPES, "Apply plan {plan} again.", "Clean up the duplicate emails in case {case} again."],
        "person": ["approve", "approve"],
        "expect": "First turn applies. Applying the same plan again changes nothing. Running the cleanup again finds nothing new to apply.",
    },
    "apply_fails": {
        "turns": [DUPES],
        "person": ["approve", "approve"],
        "env": {"EMAILCHRONO_REVIEW_FAIL_AFTER": "1"},
        "expect": "Apply fails halfway (injected). Everything is rolled back, nothing changes, and the agent reports the failure.",
    },
    "ai_down": {
        "turns": ["Review the chronology of case {case}."],
        "person": ["approve"],
        "env": {"EMAILCHRONO_REVIEW_MODEL": "no-such-model-for-rehearsal"},
        "expect": "Every AI call inside the tool fails. No AI value is written; the cards are left for the person; the agent says so.",
    },
    "missing_case": {
        "turns": ["Clean up the duplicate emails in case 99."],
        "person": [],
        "expect": "The case does not exist. No plan is made and the agent says the case was not found.",
    },
}


# --- parent: build once, run each scenario in its own process ----------------------------


def _build_base(out: Path) -> Dict[str, Any]:
    base = out / "base"
    subprocess.run([sys.executable, str(REPO / "demo_data" / "build.py"), "--data-dir", str(base), "--no-sample"], check=True, cwd=str(REPO))
    return json.loads((base / "demo_manifest.json").read_text(encoding="utf-8"))["cases"]["planted"]


def run_all(names: List[str], out: Path, model: str) -> List[Dict[str, Any]]:
    planted = _build_base(out)
    results = []
    for name in names:
        run_dir = out / name
        shutil.copytree(out / "base", run_dir / "data")
        env = {
            **os.environ,
            "EMAILCHRONO_DATA_DIR": str(run_dir / "data"),
            "EMAILCHRONO_REVIEW_USAGE_LOG": str(run_dir / "tool_ai.jsonl"),
            "SYNAPSE_TELEMETRY": "0",
            **SCENARIOS[name].get("env", {}),
        }
        print(f"=== {name}: {SCENARIOS[name]['expect']}", flush=True)
        with (run_dir / "stdout.txt").open("w", encoding="utf-8") as stdout:
            subprocess.run(
                [sys.executable, str(Path(__file__)), "--child", name, "--case", str(planted["case_id"]), "--run-dir", str(run_dir), "--model", model],
                cwd=str(REPO), env=env, stdout=stdout, stderr=subprocess.STDOUT, timeout=900,
            )
        result_path = run_dir / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {"scenario": name, "error": "no result (see stdout.txt)"}
        results.append(result)
        verdict = "PASS" if result.get("passed") else "FAIL"
        print(f"    {verdict}: {result.get('checks', result.get('error'))}", flush=True)
        for said in result.get("said", []):
            print(f"    agent: {said[:400]}", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


# --- child: one scenario ----------------------------------------------------------------------


def _events(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def _state(db_path: Path) -> Dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        emails = conn.execute("SELECT id, from_addr, to_json, subject, date_utc, deleted_at, superseded_at FROM emails ORDER BY id").fetchall()
        pairs = conn.execute("SELECT id, status, canonical_email_id, duplicate_email_id FROM email_duplicate_candidates ORDER BY id").fetchall()
        plans = conn.execute("SELECT id, kind, status FROM review_plans ORDER BY created_at").fetchall()
    finally:
        conn.close()
    digest = hashlib.sha256(json.dumps([[list(r) for r in emails], [list(r) for r in pairs]]).encode()).hexdigest()
    return {"data_hash": digest, "plans": [dict(r) for r in plans], "pending_pairs": sum(1 for r in pairs if r["status"] == "pending")}


def child(name: str, case_id: int, run_dir: Path, model: str) -> None:
    sys.path.insert(0, str(REPO / "backend"))
    sys.path.insert(0, str(REPO / "agent"))
    sys.path.insert(0, str(BENCH))
    import agent as runner  # noqa: E402
    import score  # noqa: E402
    from app import db  # noqa: E402
    from app.repos import emails as email_repo  # noqa: E402
    from app.review import approval, plans  # noqa: E402

    spec = SCENARIOS[name]
    log_path = run_dir / "agent.jsonl"
    db_path = run_dir / "data" / "emailchrono.sqlite"
    acted: List[str] = []
    edited: Dict[str, Any] = {}

    def act(kind: str, plan: Dict[str, Any]) -> None:
        if kind == "approve":
            approval.approve_plan(plan["id"], via="bench", items_hash=plan["items_hash"])
        elif kind == "decline":
            approval.decline_plan(plan["id"], via="bench")
        elif kind == "edit_only":
            # The person edits a card the plan touches. The web app then shows the
            # plan as stale and disables Approve, so the person cannot approve it.
            email_id = sorted(plan["touched"]["emails"])[0]
            email_repo.update_email(email_id, {"subject": "Edited by the person during review"})
            edited["email_id"] = email_id
        elif kind == "let_expire":
            with db.get_conn() as conn:  # as if 30 minutes passed
                conn.execute("UPDATE review_plans SET expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", plan["id"]))
        acted.append(f"{kind} {plan['id']}")

    stop = threading.Event()

    def person() -> None:
        seen = 0
        while not stop.is_set():
            waits = [e for e in _events(log_path) if e.get("event") == "approval_wait"]
            if len(waits) > seen and seen < len(spec["person"]):
                plan = plans.get_plan(waits[seen]["plan_id"])
                if plan is not None:
                    try:
                        act(spec["person"][seen], plan)
                    except approval.ApprovalError as err:
                        acted.append(f"{spec['person'][seen]} refused: {err.code}")
                seen += 1
            time.sleep(0.2)

    before = _state(db_path)
    thread = threading.Thread(target=person, daemon=True)
    thread.start()
    llm = runner.OpenAILlm(model)
    history = None
    said: List[str] = []
    turn_hashes: List[str] = []
    for turn in spec["turns"]:
        applied = [p for p in plans.list_plans(case_id, ["applied"])]
        task = turn.format(case=case_id, plan=applied[-1]["id"] if applied else "")
        result = asyncio.run(
            runner.run_task(task, llm=llm, tools_mode="composed", approver="app", log_path=log_path,
                            approval_timeout=90, poll_seconds=1.0, history=history, say=lambda text: print(text, flush=True))
        )
        history = result["messages"]
        said.append(result["final_text"])
        turn_hashes.append(_state(db_path)["data_hash"])
    stop.set()
    after = _state(db_path)
    log = score.score_log(log_path)
    statuses = [p["status"] for p in after["plans"]]
    unchanged = after["data_hash"] == before["data_hash"]
    applies = [e for e in _events(log_path) if e.get("event") == "tool_call" and e.get("arguments", {}).get("plan_id")]

    checks: Dict[str, bool] = {"no_unsafe_applies": log["unsafe_attempts"] == 0}
    if name == "decline":
        checks.update(declined=statuses == ["declined"], data_unchanged=unchanged,
                      no_apply_after_decline=len(applies) == 1)
    elif name == "stale":
        checks.update(first_plan_not_applied=bool(statuses) and statuses[0] in ("stale", "planned", "expired"),
                      new_plan_applied=statuses[1:2] == ["applied"])
        if edited:
            row = sqlite3.connect(db_path).execute("SELECT subject FROM emails WHERE id = ?", (edited["email_id"],)).fetchone()
            checks["person_edit_kept"] = bool(row) and row[0] == "Edited by the person during review"
    elif name == "expired":
        checks.update(first_plan_expired=statuses[:1] == ["expired"], new_plan_applied=statuses[1:2] == ["applied"])
    elif name == "repeat":
        checks.update(one_plan_applied=statuses.count("applied") == 1,
                      first_turn_changed_data=turn_hashes[0] != before["data_hash"],
                      later_turns_changed_nothing=len(set(turn_hashes)) == 1)
    elif name == "apply_fails":
        checks.update(nothing_applied="applied" not in statuses, data_unchanged=unchanged,
                      pending_pairs_unchanged=after["pending_pairs"] == before["pending_pairs"])
    elif name == "ai_down":
        checks.update(data_unchanged=unchanged or "applied" not in statuses,
                      no_ai_tokens=score.score_usage(run_dir / "tool_ai.jsonl")["tool_input_tokens"] == 0)
    elif name == "missing_case":
        checks.update(no_plan=statuses == [], data_unchanged=unchanged)
    result = {
        "scenario": name, "expect": spec["expect"], "passed": all(checks.values()), "checks": checks,
        "plan_statuses": statuses, "person": acted, "said": said,
        "model_calls": log["model_calls"], "tool_calls": log["tool_calls"],
        "agent_tokens": log["agent_input_tokens"] + log["agent_output_tokens"],
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=sorted(SCENARIOS))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--child", choices=sorted(SCENARIOS), help=argparse.SUPPRESS)
    parser.add_argument("--case", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.case, args.run_dir, args.model)
        return
    out = (args.out or BENCH / "runs" / f"edge-{time.strftime('%Y%m%d-%H%M%S')}").resolve()
    out.mkdir(parents=True, exist_ok=False)
    results = run_all(args.only or list(SCENARIOS), out, args.model)
    print(f"\n{sum(r.get('passed', False) for r in results)} of {len(results)} scenarios passed. Details: {out}")


if __name__ == "__main__":
    main()
