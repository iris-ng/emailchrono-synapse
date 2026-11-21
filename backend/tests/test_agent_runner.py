"""Agent runner logic with a scripted fake model (no API calls).

Checks the approval wait: the runner pauses on "waiting_for_approval", retries
the same call once the person approves, and tells the model when the person
declines. Tools run as real MCP stdio servers.
"""

import asyncio
import json
import sys
import threading
import time

from app.review import approval, plans

from conftest import REPO

sys.path.insert(0, str(REPO / "agent"))
import agent as runner  # noqa: E402


class FakeLlm:
    """Replays a script. Each step is a list of (tool, args) calls or a final text.
    A callable step gets the messages so far (to read a plan_id)."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(list(messages))
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            return runner.LlmReply(text=step, tool_calls=[], usage={"input": 1, "output": 1}, model="fake")
        calls = [runner.ToolCall(id=f"c{i}", name=name, arguments=args) for i, (name, args) in enumerate(step)]
        return runner.LlmReply(text="", tool_calls=calls, usage={"input": 1, "output": 1}, model="fake")


def _last_plan_id(messages):
    for message in reversed(messages):
        if message.get("role") == "tool":
            body = json.loads(message["content"])
            if body.get("plan_id"):
                return body["plan_id"]
    raise AssertionError("no plan id in tool results")


def _script(case_id):
    return [
        [("resolve_case_duplicates", {"case_id": case_id})],
        lambda messages: [("resolve_case_duplicates", {"case_id": case_id, "plan_id": _last_plan_id(messages)})],
        "done",
    ]


def _run(llm, tmp_path, approver="app", timeout=20):
    return asyncio.run(
        runner.run_task(
            "Clean up duplicates in case 1",
            llm=llm,
            tools_mode="composed",
            approver=approver,
            log_path=tmp_path / "log.jsonl",
            approval_timeout=timeout,
            poll_seconds=0.2,
        )
    )


def _approve_in_background(decide, log_path):
    """Act like the person: decide only once the runner has started waiting."""

    def worker():
        for _ in range(200):
            waiting = log_path.exists() and '"approval_wait"' in log_path.read_text()
            planned = plans.list_plans(1, ["planned"])
            if waiting and planned:
                decide(planned[0])
                return
            time.sleep(0.05)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def test_runner_waits_for_web_approval_then_applies(planted_case, tmp_path):
    llm = FakeLlm(_script(planted_case["case_id"]))
    # The person approves while the runner waits (as if they clicked Approve).
    def approve_when_waiting(plan):
        approval.approve_plan(plan["id"], via="web", items_hash=plan["items_hash"])

    thread = _approve_in_background(approve_when_waiting, tmp_path / "log.jsonl")
    result = _run(llm, tmp_path)
    thread.join(timeout=5)
    assert result["final_text"] == "done"
    tool_messages = [m for m in llm.seen[-1] if m.get("role") == "tool"]
    assert json.loads(tool_messages[-1]["content"])["status"] == "applied"
    events = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert "approval_wait" in kinds and kinds.count("tool_call") >= 3
    assert any(e["event"] == "model_call" and "latency_s" in e for e in events)


def test_runner_tells_model_when_user_declines(planted_case, tmp_path):
    llm = FakeLlm(_script(planted_case["case_id"]))
    thread = _approve_in_background(lambda plan: approval.decline_plan(plan["id"], via="web"), tmp_path / "log.jsonl")
    _run(llm, tmp_path)
    thread.join(timeout=5)
    last_tool = [m for m in llm.seen[-1] if m.get("role") == "tool"][-1]
    assert "declined" in last_tool["content"].lower()


def test_runner_bench_approver_approves_and_logs(planted_case, tmp_path):
    llm = FakeLlm(_script(planted_case["case_id"]))
    _run(llm, tmp_path, approver="bench-approve")
    events = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["event"] == "approval_decision"]
    assert decisions and decisions[0]["decision"] == "approve" and decisions[0]["via"] == "bench"
    plan_id = decisions[0]["plan_id"]
    assert plans.get_plan(plan_id)["status"] == "applied"


def _last_tool_body(messages):
    return json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])


def test_runner_small_tools_mode_waits_on_apply_then_applies(planted_case, tmp_path):
    """Many-tools mode: the model chains the adapters itself; the runner pauses on
    the apply tool exactly as it does on the composed tool."""
    case_id = planted_case["case_id"]
    decisions = []

    def propose(messages):
        pairs = _last_tool_body(messages)["pairs"]
        return [("propose_duplicate_decision", {"pair": pair}) for pair in pairs]

    def save(messages):
        tool_bodies = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        decisions.extend(body for body in tool_bodies if "decision" in body)
        return [("save_duplicate_plan", {"case_id": case_id, "decisions": decisions})]

    script = [
        [("list_pending_duplicate_pairs", {"case_id": case_id})],
        propose,
        save,
        lambda messages: [("apply_duplicate_plan", {"case_id": case_id, "plan_id": _last_plan_id(messages)})],
        "done",
    ]
    llm = FakeLlm(script)
    result = asyncio.run(
        runner.run_task(
            "Clean up duplicates in case 1",
            llm=llm,
            tools_mode="small",
            approver="bench-approve",
            log_path=tmp_path / "log.jsonl",
            approval_timeout=20,
            poll_seconds=0.2,
        )
    )
    assert result["final_text"] == "done"
    assert _last_tool_body(llm.seen[-1])["status"] == "applied"
    events = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    tools_event = next(e for e in events if e["event"] == "tools")
    assert len(tools_event["names"]) == 11


def test_small_mode_gets_the_same_chaining_guide_synapse_got(planted_case, tmp_path):
    """Fairness: Synapse composed each tool from an intent that spelled out the
    steps. In small mode the agent gets that same text; in composed mode it does not
    (the composed tools already contain it)."""
    prompts = {}
    for mode in ("composed", "small"):
        llm = FakeLlm(["done"])
        asyncio.run(runner.run_task("hi", llm=llm, tools_mode=mode, log_path=tmp_path / f"{mode}.jsonl"))
        prompts[mode] = llm.seen[0][0]["content"]
    assert runner.SMALL_TOOLS_GUIDE not in prompts["composed"]
    assert prompts["small"] == prompts["composed"] + "\n\n" + runner.SMALL_TOOLS_GUIDE
    guide = runner.SMALL_TOOLS_GUIDE
    for step in ("check_case_ready_for_duplicate_review", "propose_duplicate_decision", "save_duplicate_plan",
                 "apply_duplicate_plan", "propose_splits(card['email_id'])", "propose_fields(card['email_id'], split)",
                 "save_chronology_plan(case_id, proposals", "apply_chronology_plan(case_id, plan_id)"):
        assert step in guide


def test_system_prompt_fits_both_tool_modes():
    prompt = runner.SYSTEM_PROMPT
    assert "without plan_id" not in prompt  # composed-only wording
    assert "plan_id" in prompt and "applies" in prompt


def test_refresh_status_marks_a_waiting_plan_stale_when_a_card_it_touches_changes(planted_case):
    from app.repos import emails as email_repo
    from app.review import dedupe

    case_id = planted_case["case_id"]
    decisions = [dedupe.propose_duplicate_decision(p) for p in dedupe.list_pending_duplicate_pairs(case_id)]
    plan_id = dedupe.save_duplicate_plan(case_id, decisions)["plan_id"]
    assert plans.refresh_status(plan_id)["status"] == "planned"
    email_repo.update_email(sorted(plans.get_plan(plan_id)["touched"]["emails"])[0], {"subject": "Edited during review"})
    assert plans.refresh_status(plan_id)["status"] == "stale"
    assert plans.get_plan(plan_id)["status"] == "stale"
    assert plans.refresh_status("rp_missing") is None


def test_runner_stops_waiting_when_the_case_changes_under_the_plan(planted_case, tmp_path):
    """Seen in the live rehearsal: a person edits a card the plan touches. The web
    app then disables Approve, so waiting for an approval is pointless. The runner
    must notice at once and tell the model to build a new plan."""
    from app.repos import emails as email_repo

    llm = FakeLlm(_script(planted_case["case_id"]))

    def edit_a_touched_card(plan):
        email_repo.update_email(sorted(plan["touched"]["emails"])[0], {"subject": "Edited during review"})

    thread = _approve_in_background(edit_a_touched_card, tmp_path / "log.jsonl")
    started = time.monotonic()
    _run(llm, tmp_path, timeout=20)
    thread.join(timeout=5)
    assert time.monotonic() - started < 10  # did not sit out the 20 s timeout
    last_tool = _last_tool_body(llm.seen[-1])
    assert last_tool["status"] == "stale" and last_tool["changed"] is False
    assert "build a new plan" in last_tool["message"].lower()


def test_runner_gives_up_after_timeout(planted_case, tmp_path):
    llm = FakeLlm(_script(planted_case["case_id"]))
    _run(llm, tmp_path, timeout=0.5)
    last_tool = [m for m in llm.seen[-1] if m.get("role") == "tool"][-1]
    assert "did not approve" in last_tool["content"].lower()
