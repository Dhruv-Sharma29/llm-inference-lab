#!/usr/bin/env bash
# Start mock upstream (:8000) + gateway (:9000) on loopback for development without a GPU.
# Usage: scripts/dev_stack.sh   (Ctrl-C stops both). Override gateway limits via GATEWAY_* env vars.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
$PY -m mock_upstream.server --port "${MOCK_PORT:-8000}" &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null' EXIT
export GATEWAY_UPSTREAM_URL="${GATEWAY_UPSTREAM_URL:-http://127.0.0.1:${MOCK_PORT:-8000}}"
$PY -m uvicorn --factory gateway.app:create_app_from_env --host 127.0.0.1 --port "${GATEWAY_PORT:-9000}"
