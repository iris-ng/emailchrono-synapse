"""Build a fresh emailchrono demo data folder with two cases.

1. Planted case: the generated .eml files in demo_data/planted (fake, with ground truth).
2. Sample case: the Gmail .eml files from a local folder (default ~/Desktop/sampleemails).
   These are read in place and never copied into the repo.

Both are imported through emailchrono's own ingest code. The script also writes
``<data-dir>/demo_manifest.json``, which maps each planted-case card id to the
ground-truth message key(s) it holds, so benchmarks can score results.

Usage:
    .venv/bin/python demo_data/build.py                 # builds data/demo
    .venv/bin/python demo_data/build.py --force         # rebuild (deletes the old demo folder)
    .venv/bin/python demo_data/build.py --data-dir X --no-sample
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO / "data" / "demo"
DEFAULT_SAMPLE_DIR = Path.home() / "Desktop" / "sampleemails"
MARKER = ".emailchrono-demo"
FORBIDDEN_ROOT = Path.home() / "projs" / "emailchrono"  # the original app: never touch its data


def _guard_data_dir(data_dir: Path) -> None:
    resolved = data_dir.resolve()
    if resolved == FORBIDDEN_ROOT or FORBIDDEN_ROOT in resolved.parents:
        sys.exit(f"Refusing to use {resolved}: that is the original emailchrono folder.")


def _prepare_data_dir(data_dir: Path, force: bool) -> None:
    _guard_data_dir(data_dir)
    if data_dir.exists() and any(data_dir.iterdir()):
        if not force:
            sys.exit(f"{data_dir} already exists. Use --force to rebuild it.")
        if not (data_dir / MARKER).exists():
            sys.exit(f"{data_dir} has no {MARKER} marker, so it was not made by this script. Not deleting it.")
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / MARKER).write_text("Built by demo_data/build.py. Safe to delete.\n", encoding="utf-8")


def _card_key_map(conn, case_id: int, truth: dict) -> dict:
    """Map each planted card id to the ground-truth message keys it holds."""
    rows = conn.execute(
        """
        SELECT id, source_file_display, chain_position, source_kind
        FROM emails WHERE case_id = ? AND deleted_at IS NULL
        ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    mapping = {}
    unmatched = []
    for row in rows:
        display = Path(row["source_file_display"].split(" quoted #")[0]).name
        file_truth = truth["files"].get(display)
        keys = file_truth and file_truth["cards_at_ingest"].get(str(row["chain_position"]))
        if not keys:
            unmatched.append(row["id"])
            continue
        mapping[str(row["id"])] = {"file": display, "chain_position": row["chain_position"], "keys": keys}
    expected = sum(len(f["cards_at_ingest"]) for f in truth["files"].values())
    if unmatched or len(mapping) != expected:
        sys.exit(
            f"Planted import did not match ground truth: {len(mapping)} cards mapped, "
            f"{expected} expected, unmatched ids {unmatched}. Check demo_data/planted.py."
        )
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--sample-dir", type=Path, default=DEFAULT_SAMPLE_DIR)
    parser.add_argument("--no-sample", action="store_true", help="skip the sample mailbox case")
    parser.add_argument("--force", action="store_true", help="delete and rebuild an existing demo folder")
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    _prepare_data_dir(data_dir, args.force)

    # emailchrono reads its data folder from the environment at import time.
    os.environ["EMAILCHRONO_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(REPO / "backend"))
    sys.path.insert(0, str(REPO / "demo_data"))

    import planted  # noqa: E402

    from app import db  # noqa: E402
    from app.repos import cases as case_repo  # noqa: E402
    from app.services.ingest import ingest_folder  # noqa: E402

    planted.write_all()
    truth = json.loads(planted.GROUND_TRUTH.read_text(encoding="utf-8"))

    db.init_db()
    manifest = {"data_dir": str(data_dir), "cases": {}}

    case = case_repo.create_case(planted.CASE_NAME, planted.CASE_TZ)
    job = ingest_folder(case["id"], str(planted.OUT_DIR), True)
    if job["status"] != "completed":
        sys.exit(f"Planted ingest failed: {job}")
    with db.get_conn() as conn:
        cards = _card_key_map(conn, case["id"], truth)
    manifest["cases"]["planted"] = {"case_id": case["id"], "name": planted.CASE_NAME, "cards": cards}
    print(f"Planted case #{case['id']}: {len(cards)} cards from {job['total_files']} files")

    if args.no_sample:
        print("Skipped sample case (--no-sample)")
    elif not args.sample_dir.is_dir():
        print(f"Skipped sample case: {args.sample_dir} not found")
    else:
        sample = case_repo.create_case("Sample mailbox 2015", "Asia/Singapore")
        job = ingest_folder(sample["id"], str(args.sample_dir), True)
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE case_id = ? AND deleted_at IS NULL", (sample["id"],)
            ).fetchone()[0]
        manifest["cases"]["sample"] = {"case_id": sample["id"], "name": "Sample mailbox 2015"}
        print(f"Sample case #{sample['id']}: {count} cards from {job['total_files']} files ({job['status']})")

    (data_dir / "demo_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Demo data ready in {data_dir}")


if __name__ == "__main__":
    main()
