"""Run each Synapse-generated server as a real MCP stdio server and call its tool.

Synapse's own check does not import or run the generated code, so this test is
the only proof the file works.
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


def _call(server_file, tool, args):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(BACKEND / server_file)],
            cwd=str(BACKEND),
            env={**os.environ, "EMAILCHRONO_DATA_DIR": str(DATA_DIR)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                result = await session.call_tool(tool, args)
                text = "".join(part.text for part in result.content if getattr(part, "text", None))
                return tools, result, text

    return asyncio.run(run())


def test_duplicates_server_lists_one_tool_with_optional_plan_id(planted_case):
    tools, _, _ = _call("resolve_case_duplicates_server.py", "resolve_case_duplicates", {"case_id": planted_case["case_id"]})
    assert [t.name for t in tools] == ["resolve_case_duplicates"]
    schema = tools[0].inputSchema
    assert schema["required"] == ["case_id"]
    assert "plan_id" in schema["properties"]


def test_duplicates_server_plans_waits_then_applies(planted_case):
    case_id = planted_case["case_id"]
    _, result, text = _call("resolve_case_duplicates_server.py", "resolve_case_duplicates", {"case_id": case_id})
    assert not result.isError, text
    planned = json.loads(text)
    assert planned["ok"] is True and planned["plan_id"].startswith("rp_")

    _, _, text = _call(
        "resolve_case_duplicates_server.py", "resolve_case_duplicates", {"case_id": case_id, "plan_id": planned["plan_id"]}
    )
    assert json.loads(text)["status"] == "waiting_for_approval"

    plan = plans.get_plan(planned["plan_id"])
    approval.approve_plan(plan["id"], via="bench", items_hash=plan["items_hash"])
    _, _, text = _call(
        "resolve_case_duplicates_server.py", "resolve_case_duplicates", {"case_id": case_id, "plan_id": planned["plan_id"]}
    )
    applied = json.loads(text)
    assert applied["status"] == "applied" and applied["receipt"]["item_count"] == 6


def test_duplicates_server_reports_missing_case(planted_case):
    _, result, text = _call("resolve_case_duplicates_server.py", "resolve_case_duplicates", {"case_id": 999999})
    body = json.loads(text)
    assert body["ok"] is False and "not found" in body["reason"]
