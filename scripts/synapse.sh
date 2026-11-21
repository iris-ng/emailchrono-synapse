#!/usr/bin/env bash
# Run the pinned local Synapse CLI (0.1.9) with telemetry off.
#
# - SYNAPSE_TELEMETRY=0 is always set (user requirement).
# - The OpenAI key comes from $OPENAI_API_KEY, or from the gitignored file
#   secrets.local (a line OPENAI_API_KEY=...). The file is parsed, never sourced,
#   and the key is never printed (key-shaped text in Synapse output is masked).
# - The provider defaults to openai via SYNAPSE_LLM_PROVIDER, so no
#   `synapse model set` (which writes to ~/.synapse) is needed.
#
# Usage: scripts/synapse.sh <synapse args...>   e.g. scripts/synapse.sh --version
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYNAPSE_BIN="$REPO/tools/synapse/node_modules/.bin/synapse"
if [ ! -x "$SYNAPSE_BIN" ]; then
  echo "Synapse CLI is not installed. Run: (cd tools/synapse && npm ci)" >&2
  exit 1
fi

export SYNAPSE_TELEMETRY=0
# Node fetch ignores proxy settings unless asked (needed inside sandboxes).
export NODE_USE_ENV_PROXY="${NODE_USE_ENV_PROXY:-1}"

if [ -z "${OPENAI_API_KEY:-}" ] && [ -f "$REPO/secrets.local" ]; then
  line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?OPENAI_API_KEY=' "$REPO/secrets.local" | tail -n 1 || true)"
  if [ -n "$line" ]; then
    value="${line#*=}"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    value="$(printf '%s' "$value" | tr -d '[:space:]')"
    export OPENAI_API_KEY="$value"
  fi
fi

export SYNAPSE_LLM_PROVIDER="${SYNAPSE_LLM_PROVIDER:-openai}"

# `synapse model` prints part of the key, so mask anything key-shaped in the
# output. SYNAPSE_RAW=1 skips the filter (needed for interactive prompts).
if [ "${SYNAPSE_RAW:-0}" = "1" ]; then
  exec "$SYNAPSE_BIN" "$@"
fi
"$SYNAPSE_BIN" "$@" 2>&1 | sed -l -E 's/sk-[A-Za-z0-9_*-]+/sk-***/g'
