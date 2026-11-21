"""Shared fixtures: one planted-case database built per test session, restored
fresh before every test.

EMAILCHRONO_DATA_DIR must be set before anything imports ``app`` (app.config
reads it at import time), so it is set at module import here.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="ec-review-tests-"))
DATA_DIR = _SESSION_DIR / "data"
os.environ["EMAILCHRONO_DATA_DIR"] = str(DATA_DIR)
os.environ.pop("EMAILCHRONO_REVIEW_FAIL_AFTER", None)
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "demo_data"))

import planted  # noqa: E402

from app import db  # noqa: E402
from app.config import DB_PATH  # noqa: E402

GROUND_TRUTH = json.loads(planted.GROUND_TRUTH.read_text(encoding="utf-8"))


def _build_snapshot() -> dict:
    from app.repos import cases as case_repo
    from app.services.ingest import ingest_folder

    db.init_db()
    case = case_repo.create_case(planted.CASE_NAME, planted.CASE_TZ)
    job = ingest_folder(case["id"], str(planted.OUT_DIR), True)
    assert job["status"] == "completed", job
    cards = {}
    with db.get_conn() as conn:
        for row in conn.execute(
            "SELECT id, source_file_display, chain_position FROM emails WHERE case_id = ? AND deleted_at IS NULL",
            (case["id"],),
        ):
            name = Path(row["source_file_display"].split(" quoted #")[0]).name
            keys = GROUND_TRUTH["files"][name]["cards_at_ingest"][str(row["chain_position"])]
            cards[row["id"]] = {"file": name, "chain_position": row["chain_position"], "keys": keys}
    shutil.copy(DB_PATH, _SESSION_DIR / "snapshot.sqlite")
    return {"case_id": case["id"], "cards": cards}


_SNAPSHOT_INFO = None


@pytest.fixture()
def planted_case():
    """Fresh copy of the planted case. Returns case_id, card map and helpers."""
    global _SNAPSHOT_INFO
    if _SNAPSHOT_INFO is None:
        _SNAPSHOT_INFO = _build_snapshot()
    shutil.copy(_SESSION_DIR / "snapshot.sqlite", DB_PATH)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    info = dict(_SNAPSHOT_INFO)
    info["card"] = lambda file, pos=0: next(
        cid for cid, c in info["cards"].items() if c["file"] == file and c["chain_position"] == pos
    )
    return info
