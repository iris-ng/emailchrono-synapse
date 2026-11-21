#!/usr/bin/env python3
"""Comparison harness: the same jobs done with the two Synapse-composed tools
("composed") and with one tool per adapter ("small").

For every run it copies a freshly built planted-case data folder, runs
agent/agent.py in its own process with the scripted bench approver, then
scores the run's database and logs against the answer key (bench/score.py).
Runs alternate which mode goes first. Same model, settings, system prompt and
approval rule in both modes; only the tool list differs.

Usage (from the repo root; needs the OpenAI key in secrets.local):
    .venv/bin/python bench/run.py --runs 5
    .venv/bin/python bench/run.py --runs 1 --tasks chronology --modes composed small
    .venv/bin/python bench/run.py --report-only bench/runs/20261121-200000

Output goes to bench/runs/<timestamp>/ (gitignored): one folder per run with
the data copy, agent log, AI-call log, stdout and score.json, plus
results.jsonl and report.md for the whole batch.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
sys.path.insert(0, str(BENCH))

import report  # noqa: E402
import score  # noqa: E402

TASKS = {
    "duplicates": "Clean up the duplicate emails in case {case_id}.",
    "chronology": "Review the chronology of case {case_id}: fix merged, undated and misdated cards.",
    "both": "Review the chronology of case {case_id}, then clean up its duplicate emails.",
}
MODES = ("composed", "small")
DEFAULT_MODEL = os.environ.get("EMAILCHRONO_AGENT_MODEL", "gpt-5.6-luna")
AGENT_CMD = [sys.executable, str(REPO / "agent" / "agent.py")]
RUN_TIMEOUT_S = 20 * 60


def schedule(tasks: Sequence[str], modes: Sequence[str], runs: int) -> List[Tuple[int, str, str]]:
    """(run, task, mode) in order. Odd runs go in the given mode order, even runs
    reversed, so neither mode always goes first."""
    order = []
    for run_index in range(1, runs + 1):
        run_modes = list(modes) if run_index % 2 else list(reversed(modes))
        for task in tasks:
            order.extend((run_index, task, mode) for mode in run_modes)
    return order


def code_version() -> Dict[str, Any]:
    """The code a batch measured: commit, uncommitted files and a hash of their diff."""
    import hashlib

    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return ""

    changed = [line[3:] for line in git("status", "--porcelain").splitlines() if line.strip()]
    diff = git("diff", "HEAD")
    return {
        "commit": git("rev-parse", "HEAD").strip(),
        "uncommitted": changed,
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16] if diff else "",
    }


def build_base(out: Path) -> Tuple[Path, Dict[str, Any]]:
    """Build the planted case once (no sample mailbox) through emailchrono's own ingest."""
    base = out / "base"
    subprocess.run(
        [sys.executable, str(REPO / "demo_data" / "build.py"), "--data-dir", str(base), "--no-sample"],
        check=True, cwd=str(REPO),
    )
    manifest = json.loads((base / "demo_manifest.json").read_text(encoding="utf-8"))
    return base, manifest["cases"]["planted"]


def score_run(run_dir: Path, case_id: int, cards: Dict[str, Any], truth: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "case": score.score_case(run_dir / "data", case_id, cards, truth),
        "log": score.score_log(run_dir / "agent.jsonl"),
        "usage": score.score_usage(run_dir / "tool_ai.jsonl"),
    }


def run_one(
    base: Path,
    run_dir: Path,
    *,
    task: str,
    mode: str,
    run_index: int,
    case_id: int,
    cards: Dict[str, Any],
    truth: Dict[str, Any],
    model: str,
    agent_cmd: Sequence[str] = AGENT_CMD,
    timeout: float = RUN_TIMEOUT_S,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    data = run_dir / "data"
    shutil.copytree(base, data)
    env = {
        **os.environ,
        "EMAILCHRONO_DATA_DIR": str(data),
        "EMAILCHRONO_REVIEW_USAGE_LOG": str(run_dir / "tool_ai.jsonl"),
        "SYNAPSE_TELEMETRY": "0",
        **(extra_env or {}),
    }
    command = [
        *agent_cmd,
        "--tools", mode,
        "--approver", "bench-approve",
        "--data-dir", str(data),
        "--log", str(run_dir / "agent.jsonl"),
        "--model", model,
        "--approval-timeout", "60",
        TASKS[task].format(case_id=case_id),
    ]
    started = time.monotonic()
    timed_out = False
    with (run_dir / "stdout.txt").open("w", encoding="utf-8") as out:
        try:
            exit_code = subprocess.run(command, cwd=str(REPO), env=env, stdout=out, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            exit_code, timed_out = None, True
    result = {
        "task": task,
        "mode": mode,
        "run": run_index,
        "model": model,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "harness_s": round(time.monotonic() - started, 1),
        **score_run(run_dir, case_id, cards, truth),
    }
    (run_dir / "score.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _load_results(out: Path) -> List[Dict[str, Any]]:
    path = out / "results.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def write_report(out: Path, meta: Dict[str, Any]) -> Path:
    baseline = json.loads((out / "baseline.json").read_text(encoding="utf-8")) if (out / "baseline.json").exists() else {}
    path = out / "report.md"
    path.write_text(report.render(_load_results(out), baseline=baseline, meta=meta), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--tasks", nargs="+", choices=sorted(TASKS), default=["duplicates", "chronology", "both"])
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--report-only", type=Path, default=None, help="rebuild report.md for an existing batch")
    args = parser.parse_args()

    if args.report_only:
        meta = json.loads((args.report_only / "meta.json").read_text(encoding="utf-8"))
        print(write_report(args.report_only, meta))
        return

    out = (args.out or BENCH / "runs" / time.strftime("%Y%m%d-%H%M%S")).resolve()
    out.mkdir(parents=True, exist_ok=False)
    truth = json.loads((REPO / "demo_data" / "ground_truth.json").read_text(encoding="utf-8"))
    base, planted = build_base(out)
    case_id, cards = planted["case_id"], planted["cards"]
    (out / "baseline.json").write_text(json.dumps(score.score_case(base, case_id, cards, truth), indent=2) + "\n", encoding="utf-8")
    meta = {
        "model": args.model,
        "runs": args.runs,
        "tasks": args.tasks,
        "modes": args.modes,
        "started": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "code": code_version(),
        "tasks_text": {task: TASKS[task].format(case_id=case_id) for task in args.tasks},
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    plan = schedule(args.tasks, args.modes, args.runs)
    for number, (run_index, task, mode) in enumerate(plan, start=1):
        print(f"[{number}/{len(plan)}] run {run_index} {task} {mode} ...", flush=True)
        result = run_one(
            base, out / task / mode / f"run-{run_index}",
            task=task, mode=mode, run_index=run_index, case_id=case_id, cards=cards, truth=truth, model=args.model,
        )
        with (out / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result) + "\n")
        case, log = result["case"], result["log"]
        print(
            f"    done={report.task_done(result)} splits={case['splits']['correct']}/{case['splits']['expected']} "
            f"ai_wrong={case['ai_changes']['wrong']}/{case['ai_changes']['total']} "
            f"dups_wrong={case['duplicates']['wrong']}/{case['duplicates']['decided']} "
            f"model_calls={log['model_calls']} tokens={log['agent_input_tokens']}+{log['agent_output_tokens']} "
            f"wall={log['wall_s']}s exit={result['exit_code']}",
            flush=True,
        )
        write_report(out, meta)
    print(f"Report: {write_report(out, meta)}")


if __name__ == "__main__":
    main()
