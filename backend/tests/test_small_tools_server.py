"""The hand-written many-tools baseline (small_tools_server.py): every adapter
as its own MCP tool, run as a real MCP stdio server.

The agent must chain the tools itself, passing each result back in, so this is
the "many small tools" side of the comparison with the two Synapse-composed tools.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import DATA_DIR
from app.review import approval, plans

BACKEND = Path(__file__).resolve().parents[1]
SERVER = "small_tools_server.py"

EXPECTED_TOOLS = {
    "check_case_ready_for_duplicate_review",
    "list_pending_duplicate_pairs",
    "propose_duplicate_decision",
    "save_duplicate_plan",
    "apply_duplicate_plan",
    "check_case_ready_for_chronology_review",
    "list_chronology_problem_cards",
    "propose_splits",
    "propose_fields",
    "save_chronology_plan",
    "apply_chronology_plan",
}


def _with_session(steps):
    """Open one stdio session and run `steps(call)`; call(tool, args) -> parsed JSON."""

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(BACKEND / SERVER)],
            cwd=str(BACKEND),
            env={**os.environ, "EMAILCHRONO_DATA_DIR": str(DATA_DIR)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools

                async def call(tool, args):
                    result = await session.call_tool(tool, args)
                    text = "".join(part.text for part in result.content if getattr(part, "text", None))
                    assert not result.isError, text
                    return json.loads(text)

                return tools, await steps(call)

    return asyncio.run(run())


@pytest.fixture()
def stub_model(monkeypatch):
    from app.review import chronology

    monkeypatch.setenv(chronology.MODEL_STUB_ENV, "tests.chronology_stub:respond")


def test_lists_one_tool_per_adapter():
    async def nothing(call):
        return None

    tools, _ = _with_session(nothing)
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == EXPECTED_TOOLS
    assert by_name["propose_fields"].inputSchema["required"] == ["email_id"]
    assert set(by_name["save_chronology_plan"].inputSchema["required"]) == {"case_id", "proposals"}
    assert set(by_name["apply_duplicate_plan"].inputSchema["required"]) == {"case_id", "plan_id"}
    assert all(tool.description for tool in tools)


def test_duplicate_flow_chained_by_hand(planted_case):
    case_id = planted_case["case_id"]

    async def steps(call):
        ready = await call("check_case_ready_for_duplicate_review", {"case_id": case_id})
        assert ready["ok"] is True
        listed = await call("list_pending_duplicate_pairs", {"case_id": case_id})
        assert len(listed["pairs"]) == 7
        decisions = [await call("propose_duplicate_decision", {"pair": pair}) for pair in listed["pairs"]]
        saved = await call("save_duplicate_plan", {"case_id": case_id, "decisions": decisions, "warnings": ready["warnings"]})
        assert saved["ok"] is True, saved
        waiting = await call("apply_duplicate_plan", {"case_id": case_id, "plan_id": saved["plan_id"]})
        assert waiting["status"] == "waiting_for_approval"
        plan = plans.get_plan(saved["plan_id"])
        approval.approve_plan(plan["id"], via="bench", items_hash=plan["items_hash"])
        return await call("apply_duplicate_plan", {"case_id": case_id, "plan_id": saved["plan_id"]})

    _, applied = _with_session(steps)
    assert applied["status"] == "applied" and applied["receipt"]["item_count"] == 6


def test_chronology_flow_chained_by_hand(planted_case, stub_model):
    case_id = planted_case["case_id"]

    async def steps(call):
        ready = await call("check_case_ready_for_chronology_review", {"case_id": case_id})
        listed = await call("list_chronology_problem_cards", {"case_id": case_id})
        proposals = []
        for card in listed["cards"]:
            split = None
            if "merged" in card["problems"]:
                split = await call("propose_splits", {"email_id": card["email_id"]})
            proposals.append(await call("propose_fields", {"email_id": card["email_id"], "split": split}))
        saved = await call("save_chronology_plan", {"case_id": case_id, "proposals": proposals, "warnings": ready["warnings"]})
        assert saved["ok"] is True, saved
        assert saved["counts"] == {"split": 3, "set_fields": 5, "left_for_you": 1}
        waiting = await call("apply_chronology_plan", {"case_id": case_id, "plan_id": saved["plan_id"]})
        assert waiting["status"] == "waiting_for_approval"
        plan = plans.get_plan(saved["plan_id"])
        approval.approve_plan(plan["id"], via="bench", items_hash=plan["items_hash"])
        return await call("apply_chronology_plan", {"case_id": case_id, "plan_id": saved["plan_id"]})

    _, applied = _with_session(steps)
    assert applied["status"] == "applied" and applied["receipt"]["item_count"] == 8


def test_save_rechecks_values_that_passed_through_the_agent(planted_case, stub_model):
    """In this mode the agent copies proposals into save_chronology_plan, so it
    could change them. save rebuilds every item from the database and the stored
    model answer, and drops a value the card text does not support."""
    case_id = planted_case["case_id"]
    card_id = planted_case["card"]("06_costplan_v2_reply.eml", 1)

    async def steps(call):
        proposal = await call("propose_fields", {"email_id": card_id})
        for part in proposal["model_output"]["parts"]:
            part["sender"] = {"value": "Mallory <mallory@evil.example>", "evidence": "From: Mallory <mallory@evil.example>"}
        return await call("save_chronology_plan", {"case_id": case_id, "proposals": [proposal]})

    _, saved = _with_session(steps)
    assert saved["ok"] is True, saved
    items = plans.get_plan(saved["plan_id"])["items"]
    senders = [c["new"] for item in items for c in item.get("changes", []) if c["field"] == "from_addr"]
    assert "Mallory <mallory@evil.example>" not in senders
