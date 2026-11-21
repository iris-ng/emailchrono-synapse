"""Load the OpenAI API key without ever printing it.

Order: the OPENAI_API_KEY environment variable, then a line
OPENAI_API_KEY=... in the repo's gitignored secrets.local file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

SECRETS_FILE = Path(__file__).resolve().parents[3] / "secrets.local"
_LINE = re.compile(r"^\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*(.+?)\s*$")


def get_openai_api_key() -> Optional[str]:
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if value:
        return value
    try:
        lines = SECRETS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        match = _LINE.match(line)
        if match:
            return match.group(1).strip().strip("'\"") or None
    return None
