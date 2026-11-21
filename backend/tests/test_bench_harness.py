"""bench/run.py and bench/report.py plumbing, with a fake agent (no API calls)."""

import json
import shutil
import sys
from pathlib import Path

from app.config import DATA_DIR

from conftest import GROUND_TRUTH, REPO

sys.path.insert(0, str(REPO / "bench"))
import report  # noqa: E402
import run as bench_run  # noqa: E402

FAKE_AGENT = [sys.executable, str(Path(__file__).with_name("fake_bench_agent.py"))]


def test_schedule_alternates_which_mode_goes_first():
    order = bench_run.schedule(["chronology"], ["composed", "small"], runs=2)
    assert order == [(1, "chronology", "composed"), (1, "chronology", "small"), (2, "chronology", "small"), (2, "chronology", "composed")]


def test_every_task_names_the_case():
    for text in bench_run.TASKS.values():
        assert "{case_id}" in text


def test_run_one_uses_a_fresh_copy_and_scores_it(planted_case, tmp_path):
    base = tmp_path / "base"
    shutil.copytree(DATA_DIR, base)
    cards = {str(k): v for k, v in planted_case["cards"].items()}
    result = bench_run.run_one(
        base, tmp_path / "run", task="chronology", mode="small", run_index=1,
        case_id=planted_case["case_id"], cards=cards, truth=GROUND_TRUTH,
        model="fake-model", agent_cmd=FAKE_AGENT, timeout=60,
    )
    printed = json.loads((tmp_path / "run" / "stdout.txt").read_text().strip().splitlines()[-1])
    assert printed["tools"] == "small" and printed["approver"] == "bench-approve"
    assert printed["env_data_dir"] == str(tmp_path / "run" / "data")
    assert "case {}".format(planted_case["case_id"]) in printed["task"]
    assert (tmp_path / "run" / "data" / "emailchrono.sqlite").exists()
    assert result["exit_code"] == 0 and result["timed_out"] is False
    assert result["log"]["model_calls"] == 2 and result["log"]["tools_offered"] == 11
    assert result["usage"]["tool_input_tokens"] == 50
    assert result["case"]["splits"]["expected"] == 3
    assert json.loads((tmp_path / "run" / "score.json").read_text())["mode"] == "small"


def _result(mode, task="chronology", **over):
    base = {
        "task": task, "mode": mode, "run": 1, "exit_code": 0, "timed_out": False,
        "case": {
            "splits": {"expected": 3, "correct": 3, "wrong": 0}, "merged_left": 0,
            "fields": {"correct": 100, "total": 108}, "ai_changes": {"total": 20, "wrong": 1, "unscored": 0},
            "duplicates": {"decided": 0, "correct": 0, "wrong": 0, "unscored": 0, "pending": 7, "true_duplicates_pending": 5, "left_for_you": 0},
            "plans": {"chronology": {"applied": 1}}, "chronology_left_for_you": 1,
        },
        "log": {"finished": True, "model_calls": 3, "tool_calls": 2, "agent_input_tokens": 1000, "agent_output_tokens": 100,
                "wall_s": 30.0, "tool_errors": 0, "unsafe_attempts": 0, "tools_offered": 2},
        "usage": {"tool_ai_calls": 12, "tool_ai_failures": 0, "tool_input_tokens": 8000, "tool_output_tokens": 4000},
    }
    for key, value in over.items():
        section, _, name = key.partition("__")
        base[section][name] = value
    return base


def test_report_shows_mean_and_spread_per_mode():
    results = [
        _result("composed", log__model_calls=3),
        _result("composed", log__model_calls=5),
        _result("small", log__model_calls=20, log__tools_offered=11),
    ]
    text = report.render(results, baseline={"fields": {"correct": 80, "total": 100}}, meta={"model": "m", "runs": 2})
    assert "## chronology" in text
    assert "| Agent model calls | 4 (3–5) | 20 |" in text
    assert "composed" in text and "small" in text


def test_report_counts_a_run_as_done_only_when_its_plan_applied():
    failed = _result("small", case__plans={"chronology": {"planned": 1}})
    done = report.task_done(_result("small"))
    assert done is True and report.task_done(failed) is False
    both = _result("small", task="both")
    assert report.task_done(both) is False  # needs a duplicate plan applied too
