"""garmin-mcp — MCP server for managing Garmin Connect workouts.

Run with:
    python server.py             # via stdio (how Claude Desktop/Code invokes it)
    mcp dev server.py            # interactive inspector for development
"""
import math
import os
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).parent / ".env")

import garmin_sync  # noqa: E402  (must load after dotenv so token refresh works)
import plan as plan_mod  # noqa: E402
import gpx_analysis  # noqa: E402

SERVER_INSTRUCTIONS = """
This server is a personal running coach MCP. It connects to Garmin
Connect for workouts, activity history, and wellness data. It hosts
`coach://` resources with training framework docs and the user's
calibrated HR zones and profile.

━━━ FIRST SESSION — NEW USER SETUP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

At the start of any session, call `user_profile_status` silently.

If the profile is missing or has placeholder values:
1. Tell the user the server is set up but needs a personal profile.
2. Ask: "Do you want to use the Bakken Norwegian threshold framework
   as your training philosophy, or would you prefer to use this mainly
   for creating/editing workouts and tracking health data?"
3. Based on their answer:
   - **Bakken framework**: walk through the full profile setup
     (`user_profile_status` → ask questions → `init_user_profile`),
     then offer to sync activities and explain the weekly review flow.
   - **Workouts + health only**: still run the minimal profile setup
     (max HR is needed for zone computation) but skip the framework
     discussion. Explain the core tools: create_continuous_run /
     create_interval_workout for building sessions, morning_check_in
     for daily readiness, activity_breakdown for reviewing a session.
4. After setup, offer to sync activities: `sync_activities()`. Mention
   that the default window is 12 weeks, and they can call
   `sync_activities(weeks_back=52)` for a full year of history.

If the profile exists and is filled in, proceed normally.

━━━ ROUTING RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. HR ZONES AND PACES come from `get_athlete_profile` (or
   `coach://user_profile`). Never use zones from third-party apps —
   they may use different calibration or be based on a race HR, not
   the user's true max.

2. ACTIVITY ANALYSIS: use `activity_breakdown(activity_id)` for any
   completed session. Returns lap classification, per-lap zone time,
   session category, and overall zone distribution in one call.

3. RECOVERY / READINESS: `morning_check_in` returns HRV, RHR, sleep,
   Garmin training_status, and 7-day trend deltas. Call it before
   deciding whether to push a quality session.

4. WEEKLY VOLUME / ZONE TIME: `weekly_summary`.

━━━ WHEN THE USER USES THE BAKKEN FRAMEWORK ━━━━━━━━━━━━━━━━━━━━━━━━

Pre-analysis protocol (race goal, weekly review, plan tweak):
  a. `get_athlete_profile` — lock in zones, paces, PRs, profile A/B/C.
  b. `morning_check_in` — current readiness.
  c. `weekly_summary` for the relevant window.
  d. `activity_breakdown` for specific reference sessions.
  e. THEN reason — not before.

Interpretation rules for interval sessions:
- Use drag laps' `avg_hr` from `activity_breakdown.laps`, NEVER the
  session-wide `avg_hr`. A 3×6 min sub-threshold session can show
  session avg 165 bpm while the reps were at 184 — concluding "not
  threshold" from session avg is the classic error.
- HR-lag on the first rep: low-Z2 avg with Z3+ max still counts as a
  working rep (the classifier rescues these via the pace co-signal).

Athlete profile and race-goal estimation:
- Profile A (VO2-strong, utilization-weak): Riegel/VDOT overestimate.
  Bias goals slightly conservative.
- Profile B (utilization-strong, VO2-weak): Riegel underestimates.
- Profile C (balanced): Riegel/VDOT as-is.

━━━ WHEN THE USER USES WORKOUTS + HEALTH ONLY ━━━━━━━━━━━━━━━━━━━━━━

Core tools:
- Build sessions: `create_continuous_run`, `create_interval_workout`
- Schedule: `schedule_workout`, `reschedule_workout`, `swap_scheduled_workouts`
- Review a session: `activity_breakdown`
- Daily readiness: `morning_check_in`
- Trends: `get_wellness_history`, `weekly_summary`

Skip the Bakken-specific analysis (session_category, profile A/B/C,
sub-threshold band) — just use HR zones and lap data directly.
""".strip()

mcp = FastMCP("garmin-mcp", instructions=SERVER_INSTRUCTIONS)
_COACH_DATA = Path(__file__).parent / "coach_data"
_USER_PROFILE_PATH = _COACH_DATA / "user_profile.md"


# ─── Auth ──────────────────────────────────────────────────────────────
_garmin: Optional[Garmin] = None
_GARMIN_TOKEN_STORE = str(Path.home() / ".garminconnect")


def _client() -> Garmin:
    """Lazy singleton. Prefers cached tokens; falls back to credentials."""
    global _garmin
    if _garmin is not None:
        return _garmin

    # Try cached tokens first (works after setup.sh has run once).
    if Path(_GARMIN_TOKEN_STORE).exists():
        try:
            g = Garmin()
            g.login(tokenstore=_GARMIN_TOKEN_STORE)
            _garmin = g
            return _garmin
        except Exception:
            pass  # Tokens expired or corrupt — fall through to credentials.

    # Fresh login with email/password.
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "No cached Garmin tokens found and GARMIN_EMAIL / GARMIN_PASSWORD "
            "are not set. Run 'bash setup.sh' to authenticate once."
        )

    g = Garmin(email, password, return_on_mfa=True)
    status, _ = g.login(tokenstore=_GARMIN_TOKEN_STORE)
    if status == "needs_mfa":
        raise RuntimeError(
            "Garmin account has MFA enabled. Run 'bash setup.sh' once to "
            "authenticate interactively — tokens are cached afterwards and "
            "MFA will not be required again until the refresh token expires."
        )
    _garmin = g
    return _garmin


# ─── Input schemas (these become the JSON Schema Claude sees) ─────────
class EndCondition(BaseModel):
    """How a workout step ends."""

    type: Literal["time", "distance"]
    value: float = Field(
        description="Seconds if type='time', meters if type='distance'"
    )


class Step(BaseModel):
    """A single workout step. Intensity is descriptive only — no watch alerts."""

    end_condition: EndCondition
    description: Optional[str] = Field(
        default=None,
        description="Free-text note shown on watch & in Connect (e.g. 'Z2, 4:50-5:10/km').",
    )


class IntervalSet(BaseModel):
    """A repeated group: e.g. 6 × (400m work + 90s recovery)."""

    repeats: int = Field(ge=1)
    work: Step
    recovery: Step

