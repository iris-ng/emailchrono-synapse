"""The backup terminal approver (scripts/approve_plan.py)."""

import os
import subprocess
import sys

from app.config import DATA_DIR
from app.review import dedupe, plans

from conftest import REPO

SCRIPT = REPO / "scripts" / "approve_plan.py"


def _run(*args, stdin=""):
    env = dict(os.environ, EMAILCHRONO_DATA_DIR=str(DATA_DIR))
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=env, timeout=60
    )


def _plan(planted_case):
    pairs = dedupe.list_pending_duplicate_pairs(planted_case["case_id"])
    saved = dedupe.save_duplicate_plan(planted_case["case_id"], [dedupe.propose_duplicate_decision(p) for p in pairs])
    return saved["plan_id"]


def test_list_shows_waiting_plans(planted_case):
    plan_id = _plan(planted_case)
    out = _run("list")
    assert out.returncode == 0, out.stderr
    assert plan_id in out.stdout and "duplicates" in out.stdout


def test_show_prints_items_and_left_for_you(planted_case):
    plan_id = _plan(planted_case)
    out = _run("show", plan_id)
    assert out.returncode == 0, out.stderr
    assert "Keep #" in out.stdout and "Left for you" in out.stdout


def test_approve_refuses_without_a_real_terminal(planted_case):
    plan_id = _plan(planted_case)
    out = _run("approve", plan_id, stdin="y\n")
    assert out.returncode != 0
    assert "terminal" in out.stderr.lower()
    assert plans.get_plan(plan_id)["status"] == "planned"


def test_decline_also_needs_a_real_terminal(planted_case):
    plan_id = _plan(planted_case)
    out = _run("decline", plan_id, stdin="y\n")
    assert out.returncode != 0
    assert plans.get_plan(plan_id)["status"] == "planned"
