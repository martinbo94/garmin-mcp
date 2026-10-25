#!/bin/bash
# Setup for garmin-mcp on Mac (and Linux). Installs what is missing, then
# logs in to Garmin and registers the server with Claude.
#
# From a fresh Mac, in Terminal:
#   curl -fsSL https://raw.githubusercontent.com/martinbo94/garmin-mcp/main/setup.sh | bash
# Or from inside a clone:
#   bash setup.sh
# Safe to re-run: every step skips what is already done.

set -e

REPO_URL="https://github.com/martinbo94/garmin-mcp.git"
PYTHON_VERSION="3.12"

echo ""
echo "=== garmin-mcp setup ==="
echo ""

# 1. Git (Mac: the Command Line Tools). Only checked when we have to clone,
#    and on a fresh Mac always, since /usr/bin/git is a stub until they exist.
ensure_git() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if ! xcode-select -p >/dev/null 2>&1; then
            echo "Installing Apple's Command Line Tools (includes git)."
            echo "  A dialog opens: click 'Install' and wait for it to finish."
            xcode-select --install >/dev/null 2>&1 || true
            until xcode-select -p >/dev/null 2>&1; do sleep 5; done
            echo "  Done."
            echo ""
        fi
    elif ! command -v git >/dev/null 2>&1; then
        echo "git is missing. Install it with your package manager (e.g. 'sudo apt install git') and run this again."
        exit 1
    fi
}

# 2. Find the repo, or clone it when this script was piped in from curl
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)"
if [ -f "$SCRIPT_DIR/server.py" ]; then
    REPO="$SCRIPT_DIR"
else
    REPO="$HOME/garmin-mcp"
    ensure_git
    if [ -d "$REPO/.git" ]; then
        echo "Updating $REPO..."
        git -C "$REPO" pull --ff-only
    else
        echo "Downloading garmin-mcp to $REPO..."
        git clone "$REPO_URL" "$REPO"
    fi
    echo ""
    # Re-run from the clone, with the keyboard as input (stdin is this script under curl | bash)
    exec bash "$REPO/setup.sh" < /dev/tty
fi

# 3. uv, which downloads Python itself — no Homebrew or python.org installer needed
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
    UV="$HOME/.local/bin/uv"
    if [ ! -x "$UV" ]; then
        echo "Installing uv (Python installer)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
        echo "  Done."
        echo ""
    fi
fi

# 4. Python + dependencies in .venv
PYTHON="$REPO/.venv/bin/python"
echo "Installing Python $PYTHON_VERSION and dependencies..."
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    "$UV" venv --quiet --clear --python "$PYTHON_VERSION" "$REPO/.venv"
fi
"$UV" pip install --quiet --python "$PYTHON" -r "$REPO/requirements.txt"
echo "  Done."
echo ""

# 5. Garmin login, .env and Claude registration
"$PYTHON" "$REPO/setup_helper.py" < /dev/tty

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Quit Claude Desktop completely (Cmd+Q) and open it again"
echo "  2. Start a new conversation and say: 'Let's set up my profile'"
echo ""
