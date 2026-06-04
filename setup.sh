#!/bin/bash
# One-time setup for garmin-coach-mcp.
# Run once from the repo root: bash setup.sh

set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
PYTHON=".venv/bin/python"
CONFIG_DIR="$HOME/Library/Application Support/Claude"
CONFIG_FILE="$CONFIG_DIR/claude_desktop_config.json"

echo ""
echo "=== garmin-coach-mcp setup ==="
echo ""

# 1. Create virtual environment and install dependencies
echo "Installing Python dependencies..."
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "  Done."
echo ""

# 2. Create .env if it doesn't exist
if [ ! -f "$REPO/.env" ]; then
    cp "$REPO/.env.example" "$REPO/.env"
    echo "Created .env — open it and fill in your Garmin credentials:"
    echo "  $REPO/.env"
    echo ""
else
    echo ".env already exists, skipping."
    echo ""
fi

# 3. Register in Claude Desktop config
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    # No config yet — create it from scratch
    cat > "$CONFIG_FILE" <<EOF
{
  "mcpServers": {
    "garmin-coach": {
      "command": "$REPO/$PYTHON",
      "args": ["$REPO/server.py"]
    }
  }
}
EOF
    echo "Created Claude Desktop config."
else
    # Config exists — check if garmin-coach is already in it
    if grep -q "garmin-coach" "$CONFIG_FILE"; then
        echo "garmin-coach already in Claude Desktop config, skipping."
    else
        # Insert the new server entry using Python (avoids jq dependency)
        python3 - <<PYEOF
import json, pathlib

path = pathlib.Path("$CONFIG_FILE")
cfg = json.loads(path.read_text())
cfg.setdefault("mcpServers", {})["garmin-coach"] = {
    "command": "$REPO/$PYTHON",
    "args": ["$REPO/server.py"]
}
path.write_text(json.dumps(cfg, indent=2))
print("  Added garmin-coach to existing Claude Desktop config.")
PYEOF
    fi
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Open $REPO/.env and fill in your Garmin email and password"
echo "  2. Restart Claude Desktop"
echo "  3. Start a new conversation and say: 'Let's set up my profile'"
echo ""
