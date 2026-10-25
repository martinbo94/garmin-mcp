"""Second half of setup.sh / setup.ps1, run with the project's .venv Python.

Asks for the Garmin login and writes .env, logs in once so the tokens are
cached in ~/.garminconnect, and registers the server in Claude Desktop (and
Claude Code when its CLI is installed). Safe to re-run: every step skips what
is already done.
"""
import getpass
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent
ENV_FILE = REPO / ".env"
TOKEN_STORE = Path.home() / ".garminconnect"
PLACEHOLDER_EMAIL = "your-garmin-email@example.com"
SERVER_NAME = "garmin-mcp"


def _quote(value: str) -> str:
    # python-dotenv reads single-quoted values verbatim but cannot hold a ' in
    # them; double quotes can (escaped). core.py loads with interpolate=False,
    # so ${VAR} inside either form stays literal.
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ensure_env() -> tuple[str, str]:
    values = dotenv_values(ENV_FILE, interpolate=False) if ENV_FILE.exists() else {}
    email = (values.get("GARMIN_EMAIL") or "").strip()
    password = values.get("GARMIN_PASSWORD") or ""
    if email and password and email != PLACEHOLDER_EMAIL:
        return email, password

    print(f"Enter your Garmin Connect login. It is saved to {ENV_FILE} (readable only by you).")
    email = input("  Garmin email: ").strip()
    password = getpass.getpass("  Garmin password (hidden): ")
    if not email or not password:
        sys.exit("  Both email and password are needed. Run the setup again.")

    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"GARMIN_EMAIL={email}\nGARMIN_PASSWORD={_quote(password)}\n")
    print("  Saved .env\n")
    return email, password


def garmin_login(email: str, password: str) -> None:
    from garminconnect import Garmin

    print("Authenticating with Garmin Connect...")
    if TOKEN_STORE.exists():
        try:
            Garmin().login(tokenstore=str(TOKEN_STORE))
            print(f"  Existing tokens still valid — logged in as {email}\n")
            return
        except Exception:
            print("  Cached tokens expired, re-authenticating...")

    def prompt_mfa() -> str:
        print("  MFA required. Open your authenticator app or email.")
        code = input("  MFA code: ").strip()
        if not code:
            sys.exit("  No MFA code entered.")
        return code

    Garmin(email, password, prompt_mfa=prompt_mfa).login(tokenstore=str(TOKEN_STORE))
    print(f"  Authenticated as {email}")
    print(f"  Tokens cached to {TOKEN_STORE}")
    print("  (Future logins are automatic — no password or MFA code needed)\n")


def _claude_desktop_configs() -> list[Path]:
    if sys.platform == "darwin":
        return [Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"]
    if os.name == "nt":
        configs = [Path(os.environ["APPDATA"]) / "Claude/claude_desktop_config.json"]
        # The Microsoft Store build of Claude Desktop keeps its config inside its package folder
        local = os.environ.get("LOCALAPPDATA", "")
        for package in glob.glob(os.path.join(local, "Packages", "Claude_*")):
            configs.append(Path(package) / "LocalCache/Roaming/Claude/claude_desktop_config.json")
        return configs
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [base / "Claude/claude_desktop_config.json"]


def register_claude_desktop() -> None:
    entry = {"command": sys.executable, "args": [str(REPO / "server.py")]}
    for path in _claude_desktop_configs():
        cfg = json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
        servers = cfg.setdefault("mcpServers", {})
        if servers.get(SERVER_NAME) == entry:
            print(f"{SERVER_NAME} already in Claude Desktop config, skipping.")
            continue
        servers[SERVER_NAME] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"Registered {SERVER_NAME} in Claude Desktop ({path}).")


def register_claude_code() -> None:
    claude = shutil.which("claude")
    if not claude:
        return
    listed = subprocess.run([claude, "mcp", "get", SERVER_NAME], capture_output=True, text=True)
    if listed.returncode == 0:
        print(f"{SERVER_NAME} already in Claude Code, skipping.")
        return
    added = subprocess.run(
        [claude, "mcp", "add", "-s", "user", SERVER_NAME, "--", sys.executable, str(REPO / "server.py")],
        capture_output=True,
        text=True,
    )
    if added.returncode == 0:
        print(f"Registered {SERVER_NAME} in Claude Code.")
    else:
        print(f"  Could not register in Claude Code: {added.stderr.strip()}")


def main() -> None:
    email, password = ensure_env()
    garmin_login(email, password)
    register_claude_desktop()
    register_claude_code()


if __name__ == "__main__":
    main()
