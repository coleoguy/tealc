#!/bin/bash
# Run once on any new machine to get the agent running.
set -e

VENV="$HOME/.lab-agent-venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Lab Agent Setup ==="

# Find Python 3.10+
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(sys.version_info.minor)")
        if [ "$VER" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10+ not found. Run: brew install python@3.12"
    exit 1
fi

echo "Using $PYTHON ($($PYTHON --version))"

# Create venv outside Google Drive (platform-specific binaries)
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment at $VENV ..."
    "$PYTHON" -m venv "$VENV"
fi

echo "Installing dependencies..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" --quiet

# Create .env from template if missing
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo ""
    echo "ACTION NEEDED: Add your Anthropic API key to:"
    echo "  $SCRIPT_DIR/.env"
    echo ""
    echo "Get a key at: https://console.anthropic.com"
else
    echo ".env already exists — skipping."
fi

echo ""
echo "=== Setup complete ==="
echo "Run the agent with:  ./run.sh"
