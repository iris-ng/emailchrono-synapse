#!/usr/bin/env python3
"""Backup approver for AI review plans, for when the web app is not available.

The main approval channel is the Approve button in the emailchrono web app.
This script is the backup. It shows the plan from the database and needs a
person to type "y" at a real terminal: piped input is refused, so no program
(and no model) can approve through it. It is never exposed as an MCP tool.

Usage (from the repo root):
    .venv/bin/python scripts/approve_plan.py list [--case ID] [--all]
    .venv/bin/python scripts/approve_plan.py show PLAN_ID
    .venv/bin/python scripts/approve_plan.py approve PLAN_ID
    .venv/bin/python scripts/approve_plan.py decline PLAN_ID

The data folder is $EMAILCHRONO_DATA_DIR, else --data-dir, else data/demo.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _setup(data_dir: Path | None) -> None:
    if "EMAILCHRONO_DATA_DIR" not in os.environ:
        os.environ["EMAILCHRONO_DATA_DIR"] = str((data_dir or REPO / "data" / "demo").resolve())
    sys.path.insert(0, str(REPO / "backend"))


def _ask(question: str) -> bool:
    if not sys.stdin.isatty():
        print("Refusing: approval needs a person at a real terminal (stdin is not a TTY).", file=sys.stderr)
        sys.exit(2)
    return input(question).strip().lower() == "y"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    list_cmd = sub.add_parser("list", help="list plans waiting for approval")
    list_cmd.add_argument("--case", type=int)
    list_cmd.add_argument("--all", action="store_true", help="include applied, declined, stale and expired plans")
    for name in ("show", "approve", "decline"):
        sub.add_parser(name).add_argument("plan_id")
    args = parser.parse_args()
    _setup(args.data_dir)

    from app import db
    from app.review import approval, plans
    from app.review.render import render_plan_text

    if args.command == "list":
        with db.get_conn() as conn:
            case_ids = [args.case] if args.case else [r["id"] for r in conn.execute("SELECT id FROM cases ORDER BY id")]
        statuses = None if args.all else ["planned", "approved"]
        rows = [plan for case_id in case_ids for plan in plans.list_plans(case_id, statuses)]
        if not rows:
            print("No plans waiting." if not args.all else "No plans.")
        for plan in rows:
            print(f"{plan['id']}  case {plan['case_id']}  {plan['kind']:<11} {plan['status']:<9} "
                  f"{len(plan['items'])} change(s), {len(plan['left_for_you'])} left for you, expires {plan['expires_at']}")
        return 0

    plan = plans.get_plan(args.plan_id)
    if plan is None:
        print(f"No plan {args.plan_id}", file=sys.stderr)
        return 1
    print(render_plan_text(plan))
    if args.command == "show":
        return 0

    try:
        if args.command == "approve":
            if not _ask(f"\nApprove plan {plan['id']} exactly as shown above? [y/N] "):
                print("Not approved. Nothing changed.")
                return 1
            approval.approve_plan(plan["id"], via="terminal", items_hash=plan["items_hash"])
            print(f"Approved {plan['id']}. The agent can now apply it.")
        else:
            if not _ask(f"\nDecline plan {plan['id']}? [y/N] "):
                print("Not declined.")
                return 1
            approval.decline_plan(plan["id"], via="terminal")
            print(f"Declined {plan['id']}.")
    except approval.ApprovalError as err:
        print(f"Refused ({err.code}): {err.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
