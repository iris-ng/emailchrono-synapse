#!/usr/bin/env python3
"""Agent runner for the emailchrono AI review tools.

A chat loop: an OpenAI model with tool calling, connected over MCP stdio to
either the two Synapse-composed tools (--tools composed) or the hand-written
one-tool-per-adapter server (--tools small).

Approval: tools only build plans and apply approved ones. When a tool says a
plan is waiting for approval, the runner pauses, polls the plan status (10
minute limit) while the person reviews it in the emailchrono web app, then
retries the same call once it is approved, or tells the model it was
declined. The model has no tool that approves. For benchmark runs only,
--approver bench-approve / bench-decline uses the scripted approver in
bench/approver.py instead of a person, and logs every decision.

Every model call and tool call is logged to JSONL.

Usage (from the repo root):
    .venv/bin/python agent/agent.py                          # chat
    .venv/bin/python agent/agent.py "Clean up duplicates in the planted case"
    .venv/bin/python agent/agent.py --tools small --approver bench-approve "..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
DEFAULT_DATA_DIR = REPO / "data" / "demo"
DEFAULT_MODEL = os.environ.get("EMAILCHRONO_AGENT_MODEL", "gpt-5.6-luna")
APP_URL = "http://127.0.0.1:8765"

SERVERS = {
    "composed": ["resolve_case_duplicates_server.py", "review_chronology_server.py"],
    "small": ["small_tools_server.py"],
}

SYSTEM_PROMPT = """You help a person review an email chronology in emailchrono, a local app for legal email chronologies.

Rules:
- Tools never change case data on their own. A tool first builds a plan, the person reviews it in the emailchrono web app and approves or declines it, and only an approved plan can be applied.
- You cannot approve or decline plans, and must never claim a plan was approved unless a tool result says so.
- To do a job: call the tool without plan_id to build a plan. If it returns a plan_id, briefly tell the person the plan id and summary, and in the same turn call the tool again with that plan_id. Do not stop to ask first: the runner pauses that call until the person approves or declines in emailchrono, then gives you the result.
- If a plan was declined, stop and report that nothing changed. If a plan is stale or expired, build a new plan once and try again.
- Report what changed using only the tool results. Mention items left for the person to decide.
- Keep replies short and plain."""


# --- model interface -------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LlmReply:
    text: str
    tool_calls: List[ToolCall]
    usage: Dict[str, int] = field(default_factory=dict)
    model: str = ""


class OpenAILlm:
    def __init__(self, model: str = DEFAULT_MODEL):
        from openai import OpenAI

        sys.path.insert(0, str(BACKEND))
        from app.review.openai_key import get_openai_api_key

        key = get_openai_api_key()
        if not key:
            raise SystemExit("No OpenAI key. Set OPENAI_API_KEY or add it to secrets.local.")
        self.client = OpenAI(api_key=key, timeout=120)
        self.model = model

    def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> LlmReply:
        """One model turn via the Responses API (needed for tools on reasoning
        models such as gpt-5.6-luna). The history stays in chat format here and
        is converted on each call; nothing is stored server-side."""
        instructions, items = _to_responses_input(messages)
        response = self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=items,
            tools=[_to_responses_tool(tool) for tool in tools] or None,
            store=False,
        )
        calls = []
        for item in response.output:
            if getattr(item, "type", "") != "function_call":
                continue
            try:
                arguments = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_unparsed": item.arguments}
            calls.append(ToolCall(id=item.call_id, name=item.name, arguments=arguments))
        usage = response.usage
        return LlmReply(
            text=response.output_text or "",
            tool_calls=calls,
            usage={"input": usage.input_tokens, "output": usage.output_tokens} if usage else {},
            model=response.model,
        )


def _to_responses_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    function = tool["function"]
    return {
        "type": "function",
        "name": function["name"],
        "description": function.get("description", ""),
        "parameters": function.get("parameters") or {"type": "object", "properties": {}},
    }


def _to_responses_input(messages: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
    """Convert chat-format history to Responses API instructions plus input items."""
    instructions: List[str] = []
    items: List[Dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            instructions.append(message["content"])
        elif role == "user":
            items.append({"role": "user", "content": message["content"]})
        elif role == "assistant":
            if message.get("content"):
                items.append({"role": "assistant", "content": message["content"]})
            for call in message.get("tool_calls") or []:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }
                )
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
    return "\n\n".join(instructions), items


# --- logging -----------------------------------------------------------------


class JsonlLog:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: str, **fields: Any) -> None:
        record = {"ts": time.time(), "run_id": self.run_id, "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# --- MCP tools -------------------------------------------------------------


class ToolHub:
    """Connects to MCP stdio servers and routes tool calls by name."""

    def __init__(self, stack: AsyncExitStack, errlog):
        self.stack = stack
        self.errlog = errlog  # server stderr goes here, not to the terminal
        self.sessions: Dict[str, Any] = {}
        self.openai_tools: List[Dict[str, Any]] = []

    async def connect(self, server_files: List[str], env: Dict[str, str]) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        for name in server_files:
            path = BACKEND / name
            if not path.exists():
                continue
            params = StdioServerParameters(command=sys.executable, args=[str(path)], cwd=str(BACKEND), env=env)
            read, write = await self.stack.enter_async_context(stdio_client(params, errlog=self.errlog))
            session = await self.stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            for tool in (await session.list_tools()).tools:
                self.sessions[tool.name] = session
                self.openai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        },
                    }
                )
        if not self.sessions:
            raise SystemExit(f"No tool servers found among {server_files} in {BACKEND}")

    async def call(self, name: str, arguments: Dict[str, Any]) -> tuple[str, bool]:
        session = self.sessions.get(name)
        if session is None:
            return json.dumps({"status": "refused", "reason": f"Unknown tool {name}"}), True
        result = await session.call_tool(name, arguments)
        text = "".join(part.text for part in result.content if getattr(part, "text", None))
        return text, bool(result.isError)


def _parse(text: str) -> Dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# --- approval wait -----------------------------------------------------------


async def wait_for_decision(
    plan_id: str,
    case_id: Optional[int],
    *,
    approver: str,
    timeout: float,
    poll_seconds: float,
    log: Callable[..., None],
    say: Callable[[str], None],
) -> str:
    """Return the plan status once it leaves 'planned', or 'timeout'."""
    from app.review import plans

    if approver.startswith("bench-"):
        sys.path.insert(0, str(REPO / "bench"))
        from approver import scripted_decision

        scripted_decision(plan_id, approver.removeprefix("bench-"), log)
    else:
        where = f"{APP_URL}/cases/{case_id}" if case_id else APP_URL
        say(f"\n[waiting] Plan {plan_id} needs your approval. Review it in emailchrono: {where}")
    start = time.monotonic()
    log("approval_wait", plan_id=plan_id, approver=approver)
    while True:
        plan = plans.get_plan(plan_id)
        status = plan["status"] if plan else "missing"
        if status != "planned":
            log("approval_result", plan_id=plan_id, status=status, waited_s=round(time.monotonic() - start, 2))
            return status
        if time.monotonic() - start >= timeout:
            log("approval_result", plan_id=plan_id, status="timeout", waited_s=round(time.monotonic() - start, 2))
            return "timeout"
        await asyncio.sleep(poll_seconds)


# --- the loop ----------------------------------------------------------------


def _case_list() -> str:
    from app import db

    with db.get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM cases WHERE deleted_at IS NULL ORDER BY id").fetchall()
    return "\n".join(f"- case {row['id']}: {row['name']}" for row in rows) or "- (no cases)"


async def run_task(
    task: str,
    *,
    llm: Any,
    tools_mode: str = "composed",
    approver: str = "app",
    log_path: Path,
    approval_timeout: float = 600,
    poll_seconds: float = 2.0,
    max_turns: int = 16,
    history: Optional[List[Dict[str, Any]]] = None,
    say: Callable[[str], None] = lambda text: None,
) -> Dict[str, Any]:
    """Run one user request to completion. Returns final text, messages and totals."""
    log = JsonlLog(log_path, uuid.uuid4().hex[:12])
    env = {key: value for key, value in os.environ.items()}
    messages = history if history is not None else [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\nCases in emailchrono:\n" + _case_list()}
    ]
    messages.append({"role": "user", "content": task})
    totals = {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}
    log("task_start", task=task, tools_mode=tools_mode, approver=approver)
    started = time.monotonic()
    final_text = ""
    async with AsyncExitStack() as stack:
        errlog = stack.enter_context(open(log_path.with_suffix(".servers.log"), "a", encoding="utf-8"))
        hub = ToolHub(stack, errlog)
        await hub.connect(SERVERS[tools_mode], env)
        log("tools", names=sorted(hub.sessions))
        for _ in range(max_turns):
            t0 = time.monotonic()
            reply = llm.complete(messages, hub.openai_tools)
            totals["model_calls"] += 1
            totals["input_tokens"] += reply.usage.get("input", 0)
            totals["output_tokens"] += reply.usage.get("output", 0)
            log("model_call", model=reply.model, latency_s=round(time.monotonic() - t0, 3),
                usage=reply.usage, tool_calls=[c.name for c in reply.tool_calls])
            if not reply.tool_calls:
                final_text = reply.text
                messages.append({"role": "assistant", "content": reply.text})
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.text or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in reply.tool_calls
                    ],
                }
            )
            if reply.text:
                say(reply.text)
            for call in reply.tool_calls:
                say(f"  -> {call.name}({json.dumps(call.arguments)})")
                text = await _call_tool(hub, call, log, totals)
                body = _parse(text)
                if body.get("status") == "waiting_for_approval" and body.get("plan_id"):
                    decision = await wait_for_decision(
                        body["plan_id"], call.arguments.get("case_id"), approver=approver,
                        timeout=approval_timeout, poll_seconds=poll_seconds, log=log, say=say,
                    )
                    if decision == "approved":
                        say(f"[approved] Plan {body['plan_id']} approved. Applying.")
                        text = await _call_tool(hub, call, log, totals, retry=True)
                    elif decision == "declined":
                        text = json.dumps({**body, "status": "declined", "changed": False,
                                           "message": f"The user declined plan {body['plan_id']} in emailchrono. Nothing was changed. Do not apply it."})
                    elif decision == "timeout":
                        text = json.dumps({**body, "changed": False,
                                           "message": f"The user did not approve plan {body['plan_id']} within {int(approval_timeout)} seconds. Nothing was changed. Ask them to approve it, then try again."})
                    else:
                        text = json.dumps({**body, "status": decision, "changed": False,
                                           "message": f"Plan {body['plan_id']} is now {decision}. Nothing was changed."})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
        else:
            final_text = "(stopped: turn limit reached)"
    totals["wall_s"] = round(time.monotonic() - started, 2)
    log("task_end", final_text=final_text, **totals)
    return {"final_text": final_text, "messages": messages, "totals": totals}


async def _call_tool(hub: ToolHub, call: ToolCall, log, totals, retry: bool = False) -> str:
    t0 = time.monotonic()
    try:
        text, is_error = await hub.call(call.name, call.arguments)
    except Exception as exc:  # a crashed server must not crash the runner
        text, is_error = json.dumps({"status": "refused", "reason": f"Tool call failed: {exc}"}), True
    totals["tool_calls"] += 1
    body = _parse(text)
    log("tool_call", tool=call.name, arguments=call.arguments, retry=retry, is_error=is_error,
        latency_s=round(time.monotonic() - t0, 3), result_chars=len(text),
        status=body.get("status"), code=body.get("code"), plan_id=body.get("plan_id"))
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", nargs="?", help="one request to run; omit for a chat")
    parser.add_argument("--tools", choices=sorted(SERVERS), default="composed")
    parser.add_argument("--approver", choices=["app", "bench-approve", "bench-decline"], default="app")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None, help="JSONL log path")
    parser.add_argument("--approval-timeout", type=float, default=600)
    args = parser.parse_args()

    data_dir = (args.data_dir or Path(os.environ.get("EMAILCHRONO_DATA_DIR", DEFAULT_DATA_DIR))).resolve()
    if not (data_dir / "emailchrono.sqlite").exists():
        raise SystemExit(f"No emailchrono database in {data_dir}. Run demo_data/build.py first.")
    os.environ["EMAILCHRONO_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(BACKEND))
    log_path = args.log or REPO / "agent" / "logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.tools}.jsonl"
    llm = OpenAILlm(args.model)
    say = lambda text: print(text, flush=True)  # noqa: E731

    if args.task:
        result = asyncio.run(run_task(args.task, llm=llm, tools_mode=args.tools, approver=args.approver,
                                      log_path=log_path, approval_timeout=args.approval_timeout, say=say))
        print(result["final_text"])
        return
    history = None
    print(f"emailchrono review agent ({args.tools} tools, {args.model}). Type 'exit' to quit. Log: {log_path}")
    while True:
        try:
            task = input("\nyou> ").strip()
        except EOFError:
            break
        if task.lower() in {"exit", "quit"}:
            break
        if not task:
            continue
        result = asyncio.run(run_task(task, llm=llm, tools_mode=args.tools, approver=args.approver,
                                      log_path=log_path, approval_timeout=args.approval_timeout,
                                      history=history, say=say))
        history = result["messages"]
        print(f"\nagent> {result['final_text']}")


if __name__ == "__main__":
    main()
