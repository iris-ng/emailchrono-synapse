"""No MCP server may reach the approval code or the web app.

Synapse scans all of backend/, so it can see app.review.approval. This test
reads every server file (generated and hand-written) and fails if one imports
or calls approval code, imports the web app, or makes HTTP calls.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SERVER_FILES = sorted(BACKEND.glob("*_server*.py"))
EXPECTED_TOOLS = {
    "resolve_case_duplicates_server.py": {"resolve_case_duplicates"},
    "review_chronology_server.py": {"review_chronology"},
}

FORBIDDEN_MODULES = ("app.review.approval", "app.api", "app.main", "httpx", "requests", "urllib", "http.client", "aiohttp")
FORBIDDEN_NAMES = ("approve_plan", "decline_plan", "approval")


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, alias.name.split(".")[-1]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                yield f"{module}.{alias.name}", alias.name


def test_there_is_at_least_one_server_file():
    assert SERVER_FILES, "no *_server.py files found in backend/"


def test_servers_do_not_import_approval_web_or_http_code():
    for path in SERVER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for full, short in _imports(tree):
            assert not any(full == m or full.startswith(m + ".") for m in FORBIDDEN_MODULES), f"{path.name} imports {full}"
            assert short not in FORBIDDEN_NAMES, f"{path.name} imports {full}"


def test_servers_never_mention_approving():
    for path in SERVER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                raise AssertionError(f"{path.name} uses {node.id}")
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                raise AssertionError(f"{path.name} uses .{node.attr}")


def test_generated_servers_expose_only_their_one_tool():
    for path in SERVER_FILES:
        if path.name not in EXPECTED_TOOLS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tools = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for deco in node.decorator_list:
                    if isinstance(deco, ast.Call) and getattr(deco.func, "attr", "") == "tool":
                        name = next((k.value.value for k in deco.keywords if k.arg == "name"), node.name)
                        tools.add(name)
        assert tools == EXPECTED_TOOLS[path.name], f"{path.name} exposes {tools}"
