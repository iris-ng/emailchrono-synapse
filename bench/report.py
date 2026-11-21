"""Turn bench results (results.jsonl rows) into a Markdown report: one table per
task, one column per tool mode, each cell the mean and (min–max) over runs."""

from __future__ import annotations

from statistics import mean
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

MODE_ORDER = ("composed", "small")
MODE_TEXT = {
    "composed": "composed (2 Synapse tools)",
    "small": "small (11 tools, one per adapter)",
}

Row = Tuple[str, Callable[[Dict[str, Any]], Optional[float]], Sequence[str]]

CHRONO = ("chronology", "both")
DUPES = ("duplicates", "both")
ALL = ("chronology", "duplicates", "both")


def _applied(result: Dict[str, Any], kind: str) -> bool:
    return result["case"]["plans"].get(kind, {}).get("applied", 0) > 0


def task_done(result: Dict[str, Any]) -> bool:
    """Done means the job's plan(s) were approved and applied."""
    needs = {"chronology": ["chronology"], "duplicates": ["duplicates"], "both": ["chronology", "duplicates"]}[result["task"]]
    return all(_applied(result, kind) for kind in needs)


def _pct(part: float, whole: float) -> Optional[float]:
    return round(100 * part / whole, 1) if whole else None


ROWS: List[Row] = [
    ("Merged cards that need a split", lambda r: r["case"]["splits"]["expected"], CHRONO),
    ("Splits right", lambda r: r["case"]["splits"]["correct"], CHRONO),
    ("Wrong splits", lambda r: r["case"]["splits"]["wrong"], CHRONO),
    ("AI values written", lambda r: r["case"]["ai_changes"]["total"], CHRONO),
    ("AI values wrong", lambda r: r["case"]["ai_changes"]["wrong"], CHRONO),
    ("Card fields right (%)", lambda r: _pct(r["case"]["fields"]["correct"], r["case"]["fields"]["total"]), CHRONO),
    ("Chronology items left for you", lambda r: r["case"].get("chronology_left_for_you", 0), CHRONO),
    ("Duplicate decisions applied", lambda r: r["case"]["duplicates"]["decided"], DUPES),
    ("Wrong duplicate decisions", lambda r: r["case"]["duplicates"]["wrong"], DUPES),
    ("True duplicates still pending", lambda r: r["case"]["duplicates"]["true_duplicates_pending"], DUPES),
    ("Unsafe apply attempts", lambda r: r["log"]["unsafe_attempts"], ALL),
    ("Tool errors", lambda r: r["log"]["tool_errors"], ALL),
    ("Agent model calls", lambda r: r["log"]["model_calls"], ALL),
    ("Tool calls", lambda r: r["log"]["tool_calls"], ALL),
    ("Agent tokens in", lambda r: r["log"]["agent_input_tokens"], ALL),
    ("Agent tokens out", lambda r: r["log"]["agent_output_tokens"], ALL),
    ("AI calls inside tools", lambda r: r["usage"]["tool_ai_calls"], CHRONO),
    ("AI tokens inside tools (in + out)", lambda r: r["usage"]["tool_input_tokens"] + r["usage"]["tool_output_tokens"], CHRONO),
    (
        "All tokens (agent + inside tools)",
        lambda r: r["log"]["agent_input_tokens"] + r["log"]["agent_output_tokens"]
        + r["usage"]["tool_input_tokens"] + r["usage"]["tool_output_tokens"],
        ALL,
    ),
    ("Wall time (s)", lambda r: r["log"]["wall_s"], ALL),
]


def _number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"


def cell(values: List[float]) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "–"
    if min(values) == max(values):
        return _number(values[0])
    return f"{_number(round(mean(values), 1))} ({_number(min(values))}–{_number(max(values))})"


def _safe(extract: Callable[[Dict[str, Any]], Optional[float]], result: Dict[str, Any]) -> Optional[float]:
    try:
        return extract(result)
    except (KeyError, TypeError):
        return None


def render(results: List[Dict[str, Any]], baseline: Dict[str, Any], meta: Dict[str, Any]) -> str:
    modes = [m for m in MODE_ORDER if any(r["mode"] == m for r in results)]
    tasks = [t for t in ("chronology", "duplicates", "both") if any(r["task"] == t for r in results)]
    lines = [
        "# Comparison: composed tools vs one tool per adapter",
        "",
        f"Test case: {meta.get('set', 'planted')}. "
        f"Model: `{meta.get('model', '?')}`. Planned runs per mode per task: {meta.get('runs', '?')}"
        " (the \"Runs done\" row shows how many finished)."
        + (f" Started {meta['started']}." if meta.get("started") else ""),
        "Each run starts from a fresh copy of the planted case; a scripted approver approves every plan.",
        "Cells show the mean and (min–max) over runs; a single number means every run gave the same value.",
    ]
    fields = baseline.get("fields") or {}
    if fields.get("total"):
        lines.append(
            f"Before any change, {fields['correct']} of {fields['total']} card fields are right "
            f"({_pct(fields['correct'], fields['total'])}%)."
        )
    for task in tasks:
        task_results = [r for r in results if r["task"] == task]
        lines += ["", f"## {task}", ""]
        text = (meta.get("tasks_text") or {}).get(task)
        if text:
            lines += [f"Task: \"{text}\"", ""]
        lines.append("| Metric | " + " | ".join(MODE_TEXT.get(m, m) for m in modes) + " |")
        lines.append("|---|" + "---|" * len(modes))
        done = []
        for mode in modes:
            runs = [r for r in task_results if r["mode"] == mode]
            done.append(f"{sum(task_done(r) for r in runs)}/{len(runs)}")
        lines.append("| Runs done (plan applied) | " + " | ".join(done) + " |")
        for label, extract, applies_to in ROWS:
            if task not in applies_to:
                continue
            cells = [cell([_safe(extract, r) for r in task_results if r["mode"] == mode]) for mode in modes]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## What the numbers mean",
        "",
        "- **AI values written / wrong**: every value an applied chronology plan wrote, checked against the answer key.",
        "- **Card fields right**: from, to, subject and date of every live card that holds one message, after the run.",
        "- **Unsafe apply attempts**: an apply on a plan already known to be declined, stale or expired, or one without a valid approval.",
        "- **AI calls inside tools**: the split and field proposals the tools ask the model for. The composed tools hide these from the agent, so they are counted from their own log.",
        "",
    ]
    return "\n".join(lines)
