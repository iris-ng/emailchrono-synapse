"""Stand-in for agent/agent.py in harness tests: no model, no tools.

Takes the same arguments, writes a minimal JSONL log, prints its arguments and
the data folder it was given, and exits 0. Lets tests check the harness
plumbing (fresh data copy, environment, scoring, report) without API calls.
"""

import argparse
import json
import os
import time

parser = argparse.ArgumentParser()
parser.add_argument("task")
parser.add_argument("--tools")
parser.add_argument("--approver")
parser.add_argument("--data-dir")
parser.add_argument("--log")
parser.add_argument("--model")
parser.add_argument("--approval-timeout")
args = parser.parse_args()

with open(args.log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"event": "tools", "names": ["x"] * (11 if args.tools == "small" else 2)}) + "\n")
    handle.write(json.dumps({"event": "task_end", "final_text": "fake", "model_calls": 2, "tool_calls": 3,
                             "input_tokens": 100, "output_tokens": 10, "wall_s": 0.1}) + "\n")
usage = os.environ.get("EMAILCHRONO_REVIEW_USAGE_LOG")
if usage:
    with open(usage, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), "task": "fields", "ok": True, "input_tokens": 50, "output_tokens": 5}) + "\n")
print(json.dumps({"task": args.task, "tools": args.tools, "approver": args.approver,
                  "data_dir": args.data_dir, "env_data_dir": os.environ.get("EMAILCHRONO_DATA_DIR")}))
