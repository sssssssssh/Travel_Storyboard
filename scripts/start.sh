#!/usr/bin/env bash
# Travel Storyboard v2.0 — one-click launcher (macOS / Linux)
#
#   ./scripts/start.sh                  # install if needed, build UI, then run
#   ./scripts/start.sh --skip-frontend  # backend only
#   PORT=8800 ./scripts/start.sh
#
# Safe to re-run: every step is idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
FRONTEND="$REPO_ROOT/frontend"
VENV_PY="$BACKEND/.venv/bin/python"
PORT="${PORT:-8737}"
BIND_HOST="${BIND_HOST:-127.0.0.1}"
SKIP_INSTALL=0
SKIP_FRONTEND=0

for arg in "$@"; do
  case "$arg" in
    --skip-install)  SKIP_INSTALL=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    -h|--help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$1"; }
ok()   { printf '    %s\n' "$1"; }
warn() { printf '    %s\n' "$1" >&2; }

echo "Travel Storyboard v2.0"
echo "Repository: $REPO_ROOT"

# ------------------------------------------------------------- prerequisites
step "Checking prerequisites"

PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    [ -z "$version" ] && continue
    major="${version%%.*}"; minor="${version##*.}"
    if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then PY="$candidate"; break; fi
    warn "$candidate is Python $version (need 3.11+)"
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ not found. Install it from https://www.python.org/downloads/" >&2
  exit 1
fi
ok "Python $("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))') ($PY)"

if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg found on PATH: $(command -v ffmpeg)"
elif [ -f "$BACKEND/.env" ] && grep -qE '^\s*TRAVELSB_FFMPEG_PATH\s*=' "$BACKEND/.env"; then
  ok "ffmpeg configured through backend/.env"
else
  warn "ffmpeg not found on PATH and not set in backend/.env"
  warn "Scanning/detection need it (brew install ffmpeg / apt install ffmpeg)."
fi

# ---------------------------------------------------------------- venv + deps
if [ "$SKIP_INSTALL" -eq 0 ]; then
  step "Preparing Python environment"
  if [ ! -x "$VENV_PY" ]; then
    echo "    creating backend/.venv"
    "$PY" -m venv "$BACKEND/.venv"
  fi
  echo "    installing requirements (first run can take a few minutes)"
  "$VENV_PY" -m pip install --upgrade pip --quiet
  "$VENV_PY" -m pip install -r "$BACKEND/requirements.txt" --quiet
  ok "Python dependencies ready"
else
  [ -x "$VENV_PY" ] || { echo "--skip-install given but backend/.venv is missing." >&2; exit 1; }
  ok "Skipping dependency install"
fi

# ------------------------------------------------------------- frontend build
if [ "$SKIP_FRONTEND" -eq 0 ]; then
  step "Building the web UI"
  pm=""
  if command -v pnpm >/dev/null 2>&1; then pm="pnpm"
  elif command -v npm >/dev/null 2>&1; then pm="npm"; fi

  if [ -z "$pm" ]; then
    warn "Neither pnpm nor npm found — skipping the UI build."
    [ -f "$FRONTEND/dist/index.html" ] || { echo "No prebuilt UI and no package manager." >&2; exit 1; }
  else
    if [ ! -d "$FRONTEND/node_modules" ]; then
      echo "    installing frontend dependencies with $pm"
      (cd "$FRONTEND" && "$pm" install)
    else
      echo "    frontend dependencies already installed"
    fi
    echo "    building"
    (cd "$FRONTEND" && "$pm" run build)
    ok "UI built at frontend/dist"
  fi
else
  ok "Skipping UI build"
fi

# ------------------------------------------------------------------------ run
step "Starting the server"

DATA_DIR="$BACKEND/data"
echo ""
echo "    UI ............ http://$BIND_HOST:$PORT/"
echo "    API docs ...... http://$BIND_HOST:$PORT/docs"
echo "    data dir ...... $DATA_DIR"
echo "    token file .... $DATA_DIR/token.txt"
echo ""
echo "    The startup log prints a ready-to-open URL including the access token."
echo "    Press Ctrl+C to stop."
echo ""

export TRAVELSB_BIND_HOST="$BIND_HOST"
export TRAVELSB_BIND_PORT="$PORT"

cd "$BACKEND"
exec "$VENV_PY" -m uvicorn app.main:app --host "$BIND_HOST" --port "$PORT"
