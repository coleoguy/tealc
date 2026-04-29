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
PYTHONPATH="$SCRIPT_DIR" chainlit run app.py --port 8000 -h
