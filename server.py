"""garmin-mcp — MCP server for managing Garmin Connect workouts.

Run with:
    python server.py             # via stdio (how Claude Desktop/Code invokes it)
    mcp dev server.py            # interactive inspector for development

This module is a thin bootstrap. The FastMCP instance, shared helpers and
schemas live in `core`; the tools and resources live in the `tools` package.
Importing the tools modules registers their @mcp.tool()/@mcp.resource()
decorators against the shared `core.mcp` instance.
"""
import sys
import threading

import garmin_sync
import core  # noqa: F401  (defines mcp + shared helpers)
from core import _client, mcp
from tools import (  # noqa: F401  (imported for decorator registration)
    activities,
    calculators,
    gear,
    plan,
    profile,
    resources,
    scheduling,
    training_load,
    wellness,
    workouts,
)


# ─── Background startup sync ───────────────────────────────────────────
def _startup_sync():
    try:
        result = garmin_sync.run_sync(_client())
        if result.get("new_activities") or result.get("errors"):
            print(
                f"[startup-sync] {result.get('new_activities', 0)} new, "
                f"{result.get('streams_fetched', 0)} streams, "
                f"{result.get('laps_fetched', 0)} laps, "
                f"{len(result.get('errors', []))} errors",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[startup-sync] failed: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    threading.Thread(target=_startup_sync, daemon=True).start()
    mcp.run()
