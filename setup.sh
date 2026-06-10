#!/bin/bash
# One-time setup for garmin-coach-mcp.
# Run once from the repo root: bash setup.sh

set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$REPO/.venv/bin/python"
CONFIG_DIR="$HOME/Library/Application Support/Claude"
CONFIG_FILE="$CONFIG_DIR/claude_desktop_config.json"

echo ""
echo "=== garmin-coach-mcp setup ==="
echo ""

# 1. Create virtual environment and install dependencies
echo "Installing Python dependencies..."
python3 -m venv "$REPO/.venv"
"$PYTHON" -m pip install -q -r "$REPO/requirements.txt"
echo "  Done."
echo ""

# 2. Create .env if it doesn't exist
if [ ! -f "$REPO/.env" ]; then
    cp "$REPO/.env.example" "$REPO/.env"
    echo "Created .env — fill in your Garmin email and password, then re-run:"
    echo "  $REPO/.env"
    echo ""
    echo "=== Stopped: fill in .env first, then run 'bash setup.sh' again ==="
    exit 0
fi

# 3. Authenticate with Garmin and cache tokens
echo "Authenticating with Garmin Connect..."
REPO="$REPO" "$PYTHON" <<'PYEOF'
import os, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(os.environ["REPO"]) / ".env")

email    = os.environ.get("GARMIN_EMAIL", "").strip()
password = os.environ.get("GARMIN_PASSWORD", "").strip()

if not email or not password:
    print("  Fill in GARMIN_EMAIL and GARMIN_PASSWORD in .env first.")
    sys.exit(1)

token_store = str(Path.home() / ".garminconnect")

# Re-use existing tokens if they are still valid
if Path(token_store).exists():
    try:
        from garminconnect import Garmin
        g = Garmin()
        g.login(tokenstore=token_store)
        print(f"  Existing tokens still valid — logged in as {email}")
        sys.exit(0)
    except Exception:
        print("  Cached tokens expired, re-authenticating...")

from garminconnect import Garmin

def prompt_mfa():
    print("  MFA required. Open your authenticator app.")
    return input("  MFA code: ").strip()

g = Garmin(email, password, prompt_mfa=prompt_mfa)
g.login(tokenstore=token_store)
print(f"  Authenticated as {email}")
print(f"  Tokens cached to ~/.garminconnect")
print("  (Future logins are automatic — no password or MFA code needed)")
PYEOF
echo ""

# 4. Register in Claude Desktop config
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<EOF
{
  "mcpServers": {
    "garmin-coach": {
      "command": "$PYTHON",
      "args": ["$REPO/server.py"]
    }
  }
}
EOF
    echo "Created Claude Desktop config."
else
    if grep -q "garmin-coach" "$CONFIG_FILE"; then
        echo "garmin-coach already in Claude Desktop config, skipping."
    else
        REPO="$REPO" PYTHON="$PYTHON" python3 <<PYEOF
import json, pathlib, os
path = pathlib.Path(os.environ["HOME"] + "/Library/Application Support/Claude/claude_desktop_config.json")
cfg = json.loads(path.read_text())
cfg.setdefault("mcpServers", {})["garmin-coach"] = {
    "command": os.environ["PYTHON"],
    "args": [os.environ["REPO"] + "/server.py"]
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
echo "  1. Restart Claude Desktop"
echo "  2. Start a new conversation and say: 'Let's set up my profile'"
echo ""
