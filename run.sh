#!/bin/bash
# Start the lab agent chat interface.
VENV="$HOME/.lab-agent-venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -d "$VENV" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo ".env not found. Run ./setup.sh first."
    exit 1
fi

cd "$SCRIPT_DIR"
source "$VENV/bin/activate"

echo "Starting Lab Agent at http://localhost:8000"
echo "Press Ctrl+C to stop."
echo ""

#   -h  headless — don't auto-open a browser tab (your shortcut opens one)
# NOTE: -w (watch mode) is intentionally OFF.  The repo lives inside Google
# Drive, and Drive's background sync touches file mtimes even when content
# doesn't change — that would cause chainlit to reload every minute or two,
# clearing your active chat session.  Re-enable -w only during local
# development, and only when the repo is outside Drive.
#
# Output goes to BOTH this Terminal (so status is visible live) AND an
# append-only log file under AppSupport (so debugging doesn't depend on
# the Terminal window still being open). `tee -a` appends — clear the
# file manually when it gets too big.
CHAINLIT_LOG="$HOME/Library/Application Support/tealc/chainlit.log"
mkdir -p "$(dirname "$CHAINLIT_LOG")"
echo "Chainlit logs → $CHAINLIT_LOG"
echo "  tail -f \"$CHAINLIT_LOG\"   from another shell to follow"
echo ""

# This repo lives on Google Drive (CloudStorage), which serves files over the
# network — a cold-cache read can fail with ETIMEDOUT (Errno 60).  Chainlit (and
# app.py) read .env at *import* time with no retry, so one timeout crashes the
# server before it binds to :8000, leaving the browser tab dead.  Pre-warm the
# Drive-backed files we're about to import so those reads hit the local cache,
# retrying if Drive is slow to wake up.
echo "Warming Google Drive cache…"
for i in 1 2 3 4 5; do
    if cat .env app.py >/dev/null 2>&1 && find agent -name '*.py' -exec cat {} + >/dev/null 2>&1; then
        break
    fi
    echo "  Google Drive not ready (attempt $i/5); retrying in 2s…"
    sleep 2
done

PYTHONPATH="$SCRIPT_DIR" chainlit run app.py --port 8000 -h 2>&1 | tee -a "$CHAINLIT_LOG"
