#!/usr/bin/env bash
# Run the emailchrono web app (copy) on the demo data folder.
# Usage: scripts/run_app.sh [data-dir]   (default: data/demo)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${1:-${EMAILCHRONO_DATA_DIR:-$REPO/data/demo}}"
case "$(cd "$DATA_DIR" 2>/dev/null && pwd)" in
  "$HOME/projs/emailchrono"*) echo "Refusing to use the original emailchrono data folder." >&2; exit 1 ;;
esac
if [ ! -f "$DATA_DIR/emailchrono.sqlite" ]; then
  echo "No demo database in $DATA_DIR. Run: .venv/bin/python demo_data/build.py" >&2
  exit 1
fi
export EMAILCHRONO_DATA_DIR="$DATA_DIR"
cd "$REPO/backend"
exec "$REPO/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8765}"
