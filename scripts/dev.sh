#!/usr/bin/env bash
# Run the StoryBored backend (auto-reload) and the Vite dev server together.
# Usage: ./scripts/dev.sh   (Ctrl-C stops both)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${STORYBORED_PORT:-8600}"
VENV="$ROOT/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "No virtualenv at $VENV — create it first:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -e backend[dev]"
  exit 1
fi

if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "Installing frontend dependencies…"
  npm --prefix "$ROOT/frontend" install
fi

pids=()
cleanup() {
  trap - INT TERM EXIT
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "▸ backend  → http://localhost:$PORT"
(cd "$ROOT/backend" && "$VENV/bin/python" -m uvicorn storybored.main:create_app --factory --reload --port "$PORT") &
pids+=($!)

echo "▸ frontend → http://localhost:5173 (proxies /api to :$PORT)"
npm --prefix "$ROOT/frontend" run dev &
pids+=($!)

wait -n
