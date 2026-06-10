"""garmin-coach-mcp — MCP server for managing Garmin Connect workouts.

Run with:
    python server.py             # via stdio (how Claude Desktop/Code invokes it)
    mcp dev server.py            # interactive inspector for development
"""
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

mcp = FastMCP("garmin-coach", instructions=SERVER_INSTRUCTIONS)
_COACH_DATA = Path(__file__).parent / "coach_data"


# ─── Resources (markdown context for the coaching agent) ───────────────
@mcp.resource("coach://classification")
def classification_rules() -> str:
    """How to classify activities (easy / threshold / VO2 / long / race).

    Read this before summarizing a week of training or analyzing a session.
    """
    return (_COACH_DATA / "workout_classification.md").read_text(encoding="utf-8")


_USER_PROFILE_PATH = _COACH_DATA / "user_profile.md"


# ─── Resource-equivalent tools (for clients that don't auto-load resources)
@mcp.tool()
def read_coach_doc(name: Literal["user_profile", "training_philosophy", "classification", "plan_design"]) -> str:
    """Read one of the coach:// markdown docs as a tool call.

    Functionally equivalent to reading the corresponding `coach://` MCP
    resource, but works in clients that don't autonomously read resources
    (Claude Desktop, most API integrations).

    Read this whenever you need:
    - 'user_profile' — the athlete's max HR, zone boundaries, race PRs,
      lactate test data, derived HR target bands.
    - 'training_philosophy' — the Bakken Norwegian threshold framework,
      session formats, weekly structure, recovery cues.
    - 'classification' — workout type rules, naming conventions, target
      zone distribution bands for weekly summaries.
    - 'plan_design' — structural reference for designing a multi-week
      training block. Read before drafting `plan.json`: block archetypes,
      X-økt rotation, weekly templates, race-prep 12-week structure.

    A good pattern at the start of a coaching conversation is to read
    'user_profile' + 'training_philosophy' before answering anything that
    depends on the athlete's thresholds or framework. Read 'plan_design'
    additionally when drafting or revising a training block.
    """
    paths = {
        "user_profile": _USER_PROFILE_PATH,
        "training_philosophy": _COACH_DATA / "training_philosophy.md",
        "classification": _COACH_DATA / "workout_classification.md",
        "plan_design": _COACH_DATA / "plan_design.md",
    }
    return paths[name].read_text(encoding="utf-8")


# ─── First-time profile setup ──────────────────────────────────────────
_PROFILE_SETUP_QUESTIONS = [
    # Essential — asked in both Bakken and minimal mode.
    {
        "field": "max_hr",
        "required": True,
        "framework_only": False,
        "question": (
            "What's your max heart rate? If you've measured it (a maximum-effort 5k, "
            "hill repeats, or a lab test), use that value. '220 − age' is a rough "
            "estimate but typically underestimates well-trained athletes."
        ),
    },
    # zone_ceilings is auto-fetched from a recent Garmin activity in
    # init_user_profile — no need to ask the user for it.
    {
        "field": "race_prs",
        "required": False,
        "framework_only": False,
        "question": (
            "What are your current PRs for 5k, 10k, half marathon, marathon? "
            "Leave out any distance you haven't raced. Times like '23:08' or '1:45:30'."
        ),
    },
    # Framework-specific — only asked when user has chosen the Bakken method.
    {
        "field": "lt2_hr",
        "required": False,
        "framework_only": True,
        "question": (
            "Have you had a lactate / VO2max test? If yes, what was your LT2 HR "
            "(classical threshold, ~4 mmol)? The HR at the highest sustainable "
            "steady-state effort."
        ),
    },
    {
        "field": "lt1_hr",
        "required": False,
        "framework_only": True,
        "question": (
            "From the same test, what was your LT1 HR (aerobic threshold, ~2 mmol)? "
            "This becomes your hard cap on easy runs in the Bakken framework."
        ),
    },
    {
        "field": "vo2max",
        "required": False,
        "framework_only": True,
        "question": (
            "What's your VO2max (ml/min/kg) from the test? Useful for reasoning about "
            "whether VO2 work or threshold work is your bigger lever (Profile A vs B)."
        ),
    },
    {
        "field": "weight_kg",
        "required": False,
        "framework_only": True,
        "question": "Body weight in kg? Optional, for VO2max L/min context.",
    },
    {
        "field": "notes",
        "required": False,
        "framework_only": False,
        "question": (
            "Any context worth recording? Recent injuries, planned races, training "
            "history, current limitations, etc."
        ),
    },
]


@mcp.tool()
def user_profile_status() -> dict:
    """Check whether user_profile.md exists and is filled in.

    Returns existence flag, file path, whether the file still has placeholder
    values from the example template, AND structured question lists split by
    mode:
    - `essential_questions`: asked regardless of training framework.
    - `framework_questions`: only asked when the user has chosen the Bakken
      Norwegian threshold method (lactate test data, LT1/LT2, VO2max, etc.).

    Call this at the start of a fresh session or whenever you suspect the
    profile isn't set up.

    After collecting answers, call `init_user_profile()` with whatever the
    user provided.
    """
    essential = [q for q in _PROFILE_SETUP_QUESTIONS if not q["framework_only"]]
    framework = [q for q in _PROFILE_SETUP_QUESTIONS if q["framework_only"]]

    if not _USER_PROFILE_PATH.exists():
        return {
            "exists": False,
            "path": str(_USER_PROFILE_PATH),
            "essential_questions": essential,
            "framework_questions": framework,
            "next_step": (
                "Ask the user whether they want to use the Bakken Norwegian threshold "
                "framework or just track workouts and health. Then walk through "
                "essential_questions (both modes) and, if Bakken, also framework_questions. "
                "max_hr is the only required field. Call init_user_profile() with answers."
            ),
        }

    content = _USER_PROFILE_PATH.read_text(encoding="utf-8")
    placeholders = ["XXX bpm", "XX km/h", "X.X mmol", "XX ml/min/kg", "XX:XX"]
    found = [p for p in placeholders if p in content]

    result: dict = {
        "exists": True,
        "path": str(_USER_PROFILE_PATH),
        "size_bytes": len(content),
        "placeholders_found": found,
    }
    if found:
        result["essential_questions"] = essential
        result["framework_questions"] = framework
        result["next_step"] = (
            f"Profile exists but still has template placeholders: {found}. Walk through "
            "the questions to collect real values, then call init_user_profile(overwrite=True)."
        )
    else:
        result["next_step"] = "Profile looks filled in — no setup action needed."
    return result


def _split_markdown_sections(text: str) -> dict[str, str]:
    """Split a markdown doc by H2 headings into a {heading: body} dict."""
    out: dict[str, str] = {}
    current_heading: Optional[str] = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                out[current_heading] = "\n".join(buf)
            current_heading = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current_heading is not None:
        out[current_heading] = "\n".join(buf)
    return out


def _parse_athlete_profile() -> dict:
    """Parse coach_data/user_profile.md into a structured dict.

    Section-scoped: race PRs only parsed from the Race PRs section, pace
    estimates only from the Session pace estimates section, etc. Tolerant
    of missing fields — returns None for anything it can't extract, plus
    the raw markdown so the agent can fall back when needed.
    """
    import re as _re
    if not _USER_PROFILE_PATH.exists():
        return {
            "exists": False,
            "path": str(_USER_PROFILE_PATH),
            "next_step": "Run init_user_profile() to create the profile.",
        }

    text = _USER_PROFILE_PATH.read_text(encoding="utf-8")
    sections = _split_markdown_sections(text)

    def grep_section(section_key_substr: str, pattern: str, group: int = 1, cast=str):
        for key, body in sections.items():
            if section_key_substr.lower() in key.lower():
                m = _re.search(pattern, body, _re.IGNORECASE)
                if m:
                    try:
                        return cast(m.group(group))
                    except (ValueError, TypeError):
                        return None
        return None

    max_hr = grep_section("Max HR", r"\*\*(\d+)\s*bpm\*\*", cast=int)

    vo2_section = next((b for k, b in sections.items() if "VO2max" in k), "")
    vo2max = _re.search(r"VO2max\s*\|\s*\*?\*?(\d+(?:\.\d+)?)\s*ml/min/kg", vo2_section)
    weight = _re.search(r"Weight\s*\|\s*(\d+(?:\.\d+)?)\s*kg", vo2_section)
    lt2 = _re.search(r"\*\*LT2 HR\*\*[^|]*\|\s*\*?\*?(\d+)\s*bpm", vo2_section)
    lt1 = _re.search(r"\*\*LT1 HR\*\*[^|]*\|\s*\*?\*?(\d+)\s*bpm", vo2_section)
    util = _re.search(r"Utilization at LT2\s*\|\s*\*?\*?(\d+)%", vo2_section)

    # Sub-threshold training target band, e.g. "training target: 178-188".
    st = _re.search(
        r"sub-threshold[^|]*\|.*?training target:\s*(\d+)\s*-\s*(\d+)",
        vo2_section, _re.IGNORECASE,
    )
    if not st:
        st = _re.search(
            r"sub-threshold[^|]*\|\s*~?(\d+)\s*-\s*(\d+)\s*bpm",
            vo2_section, _re.IGNORECASE,
        )
    sub_threshold_band = (
        {"low": int(st.group(1)), "high": int(st.group(2))} if st else None
    )

    # Athlete profile A/B/C — looks like "**Profile A: ...**"
    profile_section = next((b for k, b in sections.items() if "Athlete profile" in k), "")
    profile_match = _re.search(r"\*\*Profile\s+([A-C])\s*:\s*([^*]+?)\*\*", profile_section)
    athlete_profile = (
        {
            "label": profile_match.group(1),
            "description": profile_match.group(2).strip().rstrip(",").strip(),
        }
        if profile_match
        else None
    )

    # HR zones — reuse the existing parser (reads the whole doc; zones table
    # is the only place its pattern matches).
    zones = []
    try:
        for low, high, name in garmin_sync._parse_zones():
            zones.append({"name": name, "low": low, "high": None if high >= 9999 else high})
    except Exception:
        pass

    # Race PRs — parse the Race PRs section, table rows only.
    race_prs = []
    race_section = next((b for k, b in sections.items() if "Race PRs" in k), "")
    for line in race_section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.replace("**", "").strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        dist, time_s, pace_s = cells[0], cells[1], cells[2]
        if not _re.match(r"^\d+\s*(?:k|km|HM|hm|Marathon|marathon)$", dist):
            continue
        if not _re.match(r"^\d+:\d+(?::\d+)?$", time_s):
            continue
        if not _re.match(r"^\d+:\d+/km$", pace_s):
            continue
        date_field = cells[3] if len(cells) > 3 else ""
        race_prs.append({
            "distance": dist,
            "time": time_s,
            "pace": pace_s,
            "date": date_field if date_field and date_field not in ("—", "-", "older", "") else None,
        })

    # Pace estimates — parse the Session pace estimates section.
    pace_estimates = {}
    pace_section = next(
        (b for k, b in sections.items() if "pace estimate" in k.lower()), ""
    )
    for line in pace_section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.replace("**", "").strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        effort, pace = cells[0], cells[1]
        if not _re.search(r"\d+:\d+", pace) or "km" not in pace.lower():
            continue
        if effort.lower() in {"effort", "outdoor pace"} or effort.startswith("-"):
            continue
        pace_estimates[effort] = pace

    def _f(m, idx=1, cast=float):
        if not m:
            return None
        try:
            return cast(m.group(idx))
        except (ValueError, TypeError):
            return None

    return {
        "exists": True,
        "max_hr_bpm": max_hr,
        "lt1_hr": _f(lt1, cast=int),
        "lt2_hr": _f(lt2, cast=int),
        "sub_threshold_band_bpm": sub_threshold_band,
        "vo2max_ml_min_kg": _f(vo2max),
        "weight_kg": _f(weight),
        "utilization_at_lt2_pct": _f(util, cast=int),
        "zones": zones,
        "athlete_profile": athlete_profile,
        "race_prs": race_prs,
        "pace_estimates": pace_estimates,
    }


@mcp.tool()
def get_athlete_profile() -> dict:
    """**Authoritative source for HR zones, paces, athlete profile, and race
    PRs.** Use this before any analytical task — race goal estimation,
    weekly review, session interpretation, plan drafting.

    Returns a structured dict parsed from `coach://user_profile`:
    - `max_hr_bpm`, `lt1_hr`, `lt2_hr` (lab-calibrated thresholds)
    - `zones`: [{name, low, high}] for Z1-Z5 (verbatim from Garmin Connect)
    - `sub_threshold_band_bpm`: Bakken Golden Zone training target
    - `vo2max_ml_min_kg`, `weight_kg`, `utilization_at_lt2_pct`
    - `athlete_profile`: {label: A/B/C, description} — drives race-goal
      bias (Profile A: bias conservative; Profile B: Riegel underestimates;
      Profile C: as-is)
    - `race_prs`: list of {distance, time, pace, date}
    - `pace_estimates`: {effort_name: pace_string} (easy, sub-threshold, etc.)

    NEVER substitute zones from third-party apps for these values — they
    may use different calibration methods or be based on a recent race HR
    rather than the user's true max. Always anchor zone
    reasoning to this tool's output.
    """
    return _parse_athlete_profile()


@mcp.tool()
def init_user_profile(
    max_hr: int,
    zone_ceilings: Optional[list[int]] = None,
    weight_kg: Optional[float] = None,
    lt1_hr: Optional[int] = None,
    lt2_hr: Optional[int] = None,
    vo2max: Optional[float] = None,
    race_prs: Optional[dict] = None,
    notes: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Generate and write coach_data/user_profile.md from structured params.

    Use this when setting up a new install. `max_hr` is the only required
    field — everything else is optional and the tool will compute sensible
    defaults using Olympiatoppen %-of-max-HR rules where needed. Derived
    HR target bands (sub-threshold, easy cap, VO2) are computed from
    whatever you provide (LT values preferred over % approximations).

    Args:
        max_hr: Estimated or measured max heart rate (bpm). Required.
        zone_ceilings: Four ints — Z1, Z2, Z3, Z4 ceilings (Z5 begins
            above Z4). If omitted, auto-fetched from the most recent
            Garmin activity with HR data (most accurate), falling back to
            72/82/87/92% of max_hr if no activities are cached yet.
        weight_kg: Body weight (optional, for context).
        lt1_hr: Aerobic threshold / LT1 HR from a lactate test (optional).
        lt2_hr: Classical threshold / LT2 HR (~4 mmol) from a test (optional).
        vo2max: VO2max in ml/min/kg (optional).
        race_prs: Dict like {"5k": "23:08", "10k": "52:00"} (optional).
        notes: Free-text notes for the bottom of the file (optional).
        overwrite: Refuse to clobber an existing user_profile.md unless True.

    Returns a one-line confirmation with the derived HR bands.
    """
    if _USER_PROFILE_PATH.exists() and not overwrite:
        return (
            f"user_profile.md already exists at {_USER_PROFILE_PATH}. "
            "Call with overwrite=True to replace it."
        )

    if zone_ceilings is None:
        # Try to read zone boundaries from a recent Garmin activity —
        # more accurate than %-of-max-HR defaults.
        try:
            import sqlite3 as _sqlite3
            from garmin_sync import DB_PATH as _DB_PATH
            with _sqlite3.connect(_DB_PATH) as _conn:
                _row = _conn.execute(
                    "SELECT id FROM activities WHERE sport_type='Run' "
                    "AND avg_hr IS NOT NULL ORDER BY start_date_local DESC LIMIT 1"
                ).fetchone()
            if _row:
                _zones = _client().get_activity_hr_in_timezones(_row[0])
                if _zones and len(_zones) >= 5:
                    zone_ceilings = [_zones[i]["zoneLowBoundary"] - 1 for i in range(1, 5)]
        except Exception:
            pass

    if zone_ceilings is None:
        zone_ceilings = [
            round(max_hr * 0.72),
            round(max_hr * 0.82),
            round(max_hr * 0.87),
            round(max_hr * 0.92),
        ]
    if len(zone_ceilings) != 4:
        raise ValueError("zone_ceilings must be exactly 4 ints (Z1, Z2, Z3, Z4 ceilings).")
    z1c, z2c, z3c, z4c = zone_ceilings
    z1_floor = round(max_hr * 0.55)

    # Derived bands
    easy_cap = lt1_hr or round(max_hr * 0.84)
    sub_thresh_floor = round(max_hr * 0.80)
    sub_thresh_cap = (lt2_hr - 3) if lt2_hr else round(max_hr * 0.87)
    hard_cap = (lt2_hr - 1) if lt2_hr else round(max_hr * 0.89)
    vo2_low = round(max_hr * 0.92)
    vo2_high = round(max_hr * 0.96)

    parts: list[str] = [
        "# User profile",
        "",
        "Current physiological reference values, zones, race PRs.",
        "Generated via `init_user_profile`. Edit freely to refine.",
        "",
        "## Maximum heart rate",
        "",
        f"**{max_hr} bpm**",
        "",
    ]
    if weight_kg:
        parts += ["## Weight", "", f"**{weight_kg} kg**", ""]

    if any(v is not None for v in (vo2max, lt1_hr, lt2_hr)):
        parts += ["## Lactate / VO2max test", "", "| Metric | Value |", "|---|---|"]
        if vo2max is not None:
            parts.append(f"| VO2max | {vo2max} ml/min/kg |")
        if lt2_hr is not None:
            parts.append(f"| LT2 HR (classical 4 mmol) | {lt2_hr} bpm |")
        if lt1_hr is not None:
            parts.append(f"| LT1 HR (aerobic threshold) | {lt1_hr} bpm |")
        parts.append("")

    parts += [
        "## HR zone system: Olympiatoppen 5-zone",
        "",
        "| Zone | bpm range | Description |",
        "|------|-----------|-------------|",
        f"| Z1 | {z1_floor} – {z1c} | Very easy / recovery |",
        f"| Z2 | {z1c + 1} – {z2c} | Easy / aerobic base |",
        f"| Z3 | {z2c + 1} – {z3c} | Moderate / tempo |",
        f"| Z4 | {z3c + 1} – {z4c} | Threshold |",
        f"| Z5 | ≥ {z4c + 1} | VO2max |",
        "",
        "Ranges are inclusive integer intervals.",
        "",
        "## Quality session HR targets",
        "",
        "### Easy / aerobic base",
        "- Aim for average HR in Z1 / low-mid Z2.",
        f"- **Hard cap: {easy_cap} bpm** "
        f"({'LT1 from test' if lt1_hr else '~84% max HR estimate'}).",
        "",
        "### Threshold reps (Bakken sub-threshold)",
        "",
        "| Session type | Target HR | Notes |",
        "|---|---|---|",
        f"| All sub-threshold work | **{sub_thresh_floor} – {sub_thresh_cap} bpm** | Same band for any rep length. |",
        f"| Hard cap | **{hard_cap} bpm** | Above this you're at-threshold. |",
        f"| VO2 / X element | **{vo2_low} – {vo2_high} bpm** | 0-1× per 7-10 days. |",
        "",
        "See `coach://training_philosophy` for the framework discussion.",
        "",
        "## Race PRs",
        "",
        "| Distance | Time |",
        "|---|---|",
    ]
    prs = race_prs or {}
    for dist in ["5k", "10k", "HM", "Marathon"]:
        parts.append(f"| {dist} | {prs.get(dist, '—')} |")
    parts.append("")

    if notes:
        parts += ["## Notes", "", notes, ""]

    _USER_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _USER_PROFILE_PATH.write_text("\n".join(parts), encoding="utf-8")

    return (
        f"Wrote {_USER_PROFILE_PATH}. "
        f"Sub-threshold band: {sub_thresh_floor}-{sub_thresh_cap} bpm. "
        f"Hard cap: {hard_cap}. Easy cap: {easy_cap}. VO2 band: {vo2_low}-{vo2_high}."
    )


@mcp.resource("coach://user_profile")
def user_profile() -> str:
    """The athlete's profile: max HR, Olympiatoppen zone boundaries, lactate
    test data, race PRs, derived HR target bands, and pace ↔ HR mappings.

    Read this before any HR zone analysis or workout-prescription work —
    these values are the source of truth for HR zones.
    """
    return (_COACH_DATA / "user_profile.md").read_text(encoding="utf-8")


@mcp.resource("coach://training_philosophy")
def training_philosophy() -> str:
    """The strategic framework — Bakken-style Norwegian threshold method.

    Read this when planning workouts, designing a training block, or
    reasoning about how to react to fatigue, missed sessions, or
    off-target HR/effort patterns. The framework that lives above
    individual workouts and individual weeks.
    """
    return (_COACH_DATA / "training_philosophy.md").read_text(encoding="utf-8")


@mcp.resource("coach://plan_design")
def plan_design() -> str:
    """Structural reference for designing a multi-week training block.

    Read this before drafting plan.json. Covers block archetypes (flat /
    block periodization / progressive X-økt), the X-økt rotation menu,
    Bakken's reference 5-hour weekly template, intensity distribution
    targets for 4-6 h/week amateurs, four load-increase options, a
    race-prep 12-week template, and the multi-block periodization
    staircase. Separate concern from training_philosophy.md (the
    framework) and user_profile.md (the athlete's numbers).
    """
    return (_COACH_DATA / "plan_design.md").read_text(encoding="utf-8")


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


# ─── Read tools ────────────────────────────────────────────────────────
@mcp.tool()
def test_garmin_connection() -> str:
    """Verify Garmin Connect login works by doing a real read."""
    try:
        workouts = _client().get_workouts() or []
        return f"Connected. Found {len(workouts)} workout template(s) in library."
    except Exception as e:
        return f"Failed: {type(e).__name__}: {e}"


@mcp.tool()
def list_workout_templates(limit: int = 50) -> list[dict]:
    """List the user's saved workout templates in Garmin Connect.

    Returns id, name, and sport type for each template.
    """
    workouts = _client().get_workouts() or []
    return [
        {
            "workout_id": w.get("workoutId"),
            "name": w.get("workoutName"),
            "sport": w.get("sportType", {}).get("sportTypeKey"),
        }
        for w in workouts[:limit]
    ]


@mcp.tool()
def get_workout_template(workout_id: int) -> dict:
    """Fetch the full structure (segments, steps, targets) of a saved template."""
    return _client().get_workout_by_id(workout_id)


@mcp.tool()
def list_scheduled_workouts(start_date: str, end_date: str) -> list[dict]:
    """List workouts scheduled on the Garmin calendar between two ISO dates.

    Args:
        start_date: 'YYYY-MM-DD' (inclusive)
        end_date:   'YYYY-MM-DD' (inclusive)

    Returns each item with schedule_id (use for unschedule/reschedule), workout_id,
    date, and name. Non-workout calendar items (weigh-ins, etc.) are filtered out.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date")

    client = _client()
    # get_scheduled_workouts(year, month) returns a whole month — including spillover
    # items from adjacent months — so we dedup by id and filter by date.
    seen: dict[int, dict] = {}
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        resp = client.get_scheduled_workouts(y, m) or {}
        for item in resp.get("calendarItems", []):
            if item.get("itemType") != "workout":
                continue
            try:
                item_date = date.fromisoformat(item["date"])
            except (KeyError, ValueError):
                continue
            if start <= item_date <= end:
                seen[item["id"]] = item
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    return [
        {
            "schedule_id": item["id"],
            "workout_id": item.get("workoutId"),
            "date": item["date"],
            "name": item.get("title"),
        }
        for item in sorted(seen.values(), key=lambda i: i["date"])
    ]


# ─── Write tools ───────────────────────────────────────────────────────
_RUNNING_SPORT = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
_NO_TARGET = {
    "workoutTargetTypeId": 1,
    "workoutTargetTypeKey": "no.target",
    "displayOrder": 1,
}
# Garmin step type enum — IDs and keys must agree or the API rejects it
_ST_WARMUP = {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1}
_ST_COOLDOWN = {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2}
_ST_INTERVAL = {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}
_ST_RECOVERY = {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4}
_ST_REPEAT = {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}


def _end_condition_block(ec: EndCondition):
    """Convert an EndCondition to (Garmin endCondition dict, endConditionValue)."""
    if ec.type == "distance":
        return (
            {"conditionTypeId": 3, "conditionTypeKey": "distance",
             "displayOrder": 3, "displayable": True},
            float(ec.value),
        )
    return (
        {"conditionTypeId": 2, "conditionTypeKey": "time",
         "displayOrder": 2, "displayable": True},
        float(ec.value),
    )


def _executable_step(step_order: int, step_type: dict, ec: EndCondition,
                     description: Optional[str], child_step_id: Optional[int] = None) -> dict:
    ec_dict, ec_val = _end_condition_block(ec)
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": step_type,
        "endCondition": ec_dict,
        "endConditionValue": ec_val,
        "targetType": _NO_TARGET,
    }
    if description:
        step["description"] = description
    if child_step_id is not None:
        step["childStepId"] = child_step_id
    return step


def _wrap_workout(name: str, all_steps: list, description: Optional[str]) -> dict:
    workout = {
        "workoutName": name,
        "sportType": _RUNNING_SPORT,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": _RUNNING_SPORT,
            "workoutSteps": all_steps,
        }],
    }
    if description:
        workout["description"] = description
    return workout


def _upload(workout: dict) -> int:
    result = _client().upload_workout(workout)
    workout_id = result.get("workoutId") if isinstance(result, dict) else None
    if not workout_id:
        raise RuntimeError(f"Upload returned no workout_id: {result!r}")
    return int(workout_id)


@mcp.tool()
def create_continuous_run(
    name: str,
    distance_meters: Optional[float] = None,
    duration_seconds: Optional[float] = None,
    description: Optional[str] = None,
) -> int:
    """Create a single-step running workout template (easy, long, recovery, tempo).

    Provide exactly one of `distance_meters` or `duration_seconds` — distance is
    the more natural unit for most runs.

    Args:
        name: Template name in Garmin Connect (e.g. 'Easy 8k', 'Long 18k Z2').
        distance_meters: End after this many meters (e.g. 8000 for 8 km).
        duration_seconds: End after this much time (e.g. 2700 for 45 min).
        description: Free-text note shown on the watch & in Connect — use this for
            pace/HR guidance like 'Z2, 4:50-5:10/km, easy effort'. The watch will
            NOT alert if you drift outside it (deliberate).

    Returns the new workout_id — pass it to schedule_workout to put it on a date.
    """
    if (distance_meters is None) == (duration_seconds is None):
        raise ValueError("Provide exactly one of distance_meters or duration_seconds.")
    ec = EndCondition(
        type="distance" if distance_meters is not None else "time",
        value=distance_meters if distance_meters is not None else duration_seconds,
    )
    step = _executable_step(1, _ST_INTERVAL, ec, description)
    return _upload(_wrap_workout(name, [step], description))


@mcp.tool()
def create_interval_workout(
    name: str,
    warmup: Step,
    sets: list[IntervalSet],
    cooldown: Step,
    description: Optional[str] = None,
) -> int:
    """Create a structured interval running workout template.

    `sets` is an ordered list of repeat groups. Example for 6×400m + 90s easy
    after 10 min warmup, 10 min cooldown:

        warmup={"end_condition": {"type":"time","value":600},
                "description": "easy 10 min"}
        sets=[{
            "repeats": 6,
            "work":     {"end_condition": {"type":"distance","value":400},
                         "description": "5k pace"},
            "recovery": {"end_condition": {"type":"time","value":90},
                         "description": "easy jog"}
        }]
        cooldown={"end_condition": {"type":"time","value":600},
                  "description": "easy 10 min"}

    Multiple entries in `sets` produce back-to-back repeat groups — useful for
    e.g. pyramid workouts (3×400 + 3×800 + 3×400) or broken miles (4×(1600+200)).

    Returns the new workout_id.
    """
    all_steps: list = [_executable_step(1, _ST_WARMUP, warmup.end_condition, warmup.description)]
    step_order = 2

    for child_id, iset in enumerate(sets, start=1):
        repeat_group = {
            "type": "RepeatGroupDTO",
            "stepOrder": step_order,
            "childStepId": child_id,
            "stepType": _ST_REPEAT,
            "numberOfIterations": iset.repeats,
            "smartRepeat": False,
            "workoutSteps": [
                _executable_step(step_order + 1, _ST_INTERVAL, iset.work.end_condition,
                                 iset.work.description, child_step_id=child_id),
                _executable_step(step_order + 2, _ST_RECOVERY, iset.recovery.end_condition,
                                 iset.recovery.description, child_step_id=child_id),
            ],
        }
        all_steps.append(repeat_group)
        step_order += 3

    all_steps.append(_executable_step(step_order, _ST_COOLDOWN, cooldown.end_condition,
                                       cooldown.description))
    return _upload(_wrap_workout(name, all_steps, description))


# ─── Schedule tools ────────────────────────────────────────────────────
@mcp.tool()
def plan_interval_session(
    total_minutes: Optional[float] = None,
    work_minutes: Optional[float] = None,
    work_meters: Optional[float] = None,
    rest_minutes: Optional[float] = None,
    rest_meters: Optional[float] = None,
    warmup_minutes: float = 10.0,
    cooldown_minutes: float = 10.0,
    reps: Optional[int] = None,
) -> dict:
    """Calculate interval session structure and estimate distances from user profile paces.

    Solves for the missing variable given the others:
    - Provide `total_minutes` + work/rest → calculates how many reps fit.
    - Provide `reps` + work/rest → calculates total duration.
    - Works with time-based (minutes) or distance-based (meters) intervals.

    All durations are in minutes. Distances in meters.

    Examples:
      plan_interval_session(total_minutes=45, work_minutes=4, rest_minutes=2)
      → "5×4 min with 2 min rest fits in 45 min (10 wu + 10 cd)"

      plan_interval_session(reps=6, work_meters=1000, rest_minutes=90)
      → total time estimate based on your sub-threshold pace

    Returns a structured breakdown plus a `create_hint` with the exact
    `create_interval_workout` call to use if you want to push it to Garmin.
    """
    # Load paces from user profile for distance→time conversion
    pace_map: dict = {}
    try:
        profile = _parse_athlete_profile()
        paces = profile.get("pace_estimates", {})
        def _pace_to_s_per_m(pace_str: str) -> Optional[float]:
            if not pace_str or "/" not in pace_str:
                return None
            try:
                mins, secs = pace_str.replace("/km", "").strip().split(":")
                return (int(mins) * 60 + int(secs)) / 1000
            except Exception:
                return None
        pace_map = {k: _pace_to_s_per_m(v) for k, v in paces.items()}
    except Exception:
        pass

    sub_thresh_s_per_m = pace_map.get("sub_threshold") or pace_map.get("sub-threshold")
    easy_s_per_m = pace_map.get("easy")

    def _work_duration_s() -> Optional[float]:
        if work_minutes:
            return work_minutes * 60
        if work_meters and sub_thresh_s_per_m:
            return work_meters * sub_thresh_s_per_m
        if work_meters and not sub_thresh_s_per_m:
            return None  # handled below with clear error
        return None

    def _rest_duration_s() -> Optional[float]:
        if rest_minutes:
            return rest_minutes * 60
        if rest_meters and easy_s_per_m:
            return rest_meters * easy_s_per_m
        if rest_meters:
            return rest_meters * (sub_thresh_s_per_m or 0.33)  # ~5 min/km fallback
        return None

    work_s = _work_duration_s()
    rest_s = _rest_duration_s()
    wu_s = warmup_minutes * 60
    cd_s = cooldown_minutes * 60

    if work_s is None:
        if work_meters:
            return {"error": "work_meters requires a sub-threshold pace in your profile. "
                    "Set up your profile first, or use work_minutes instead."}
        return {"error": "Provide work_minutes or work_meters."}
    if rest_s is None:
        if rest_meters:
            return {"error": "rest_meters requires an easy pace in your profile. "
                    "Set up your profile first, or use rest_minutes instead."}
        return {"error": "Provide rest_minutes or rest_meters."}

    if reps is None and total_minutes is not None:
        available_s = total_minutes * 60 - wu_s - cd_s
        if available_s <= 0:
            return {"error": "total_minutes is too short for the warmup + cooldown alone."}
        reps = max(1, int(available_s / (work_s + rest_s)))
    elif reps is None:
        return {"error": "Provide either total_minutes or reps."}

    interval_block_s = reps * (work_s + rest_s) - rest_s  # last rep has no trailing rest
    total_s = wu_s + interval_block_s + cd_s
    total_min = round(total_s / 60, 1)

    # Distance estimates
    def _dist(duration_s: float, s_per_m: Optional[float]) -> Optional[float]:
        return round(duration_s / s_per_m / 1000, 2) if s_per_m else None

    work_km = (work_meters / 1000) if work_meters else _dist(work_s, sub_thresh_s_per_m)
    rest_km = (rest_meters / 1000) if rest_meters else _dist(rest_s, easy_s_per_m)
    wu_km = _dist(wu_s, easy_s_per_m)
    cd_km = _dist(cd_s, easy_s_per_m)
    total_km = round(sum(x for x in [
        wu_km, reps * (work_km or 0), (reps - 1) * (rest_km or 0), cd_km
    ] if x), 2) if work_km else None

    # Build create_interval_workout hint
    work_ec = ({"type": "distance", "value": work_meters}
               if work_meters else {"type": "time", "value": int(work_s)})
    rest_ec = ({"type": "distance", "value": rest_meters}
               if rest_meters else {"type": "time", "value": int(rest_s)})
    wu_ec = {"type": "time", "value": int(wu_s)}
    cd_ec = {"type": "time", "value": int(cd_s)}

    work_label = f"{work_meters:.0f}m" if work_meters else f"{work_minutes:.0f} min"
    rest_label = f"{rest_meters:.0f}m" if rest_meters else f"{rest_minutes:.0f} min"
    summary = (f"{reps}×{work_label} / {rest_label} rest — "
               f"{warmup_minutes:.0f} min wu + {cooldown_minutes:.0f} min cd = "
               f"~{total_min} min total")
    if total_km:
        summary += f" (~{total_km} km)"

    return {
        "summary": summary,
        "reps": reps,
        "work": {"label": work_label, "duration_s": round(work_s), "distance_km": work_km},
        "rest": {"label": rest_label, "duration_s": round(rest_s), "distance_km": rest_km},
        "warmup": {"duration_min": warmup_minutes, "distance_km": wu_km},
        "cooldown": {"duration_min": cooldown_minutes, "distance_km": cd_km},
        "total_minutes": total_min,
        "total_km": total_km,
        "create_hint": {
            "tool": "create_interval_workout",
            "warmup": wu_ec,
            "sets": [{"repeats": reps, "work": work_ec, "recovery": rest_ec}],
            "cooldown": cd_ec,
        },
    }


@mcp.tool()
def pace_calculator(
    pace_min_per_km: Optional[str] = None,
    speed_km_per_h: Optional[float] = None,
    distance_km: Optional[float] = None,
    duration_seconds: Optional[float] = None,
    duration_hms: Optional[str] = None,
) -> dict:
    """Convert between pace, speed, distance, and duration. Always use this
    tool for running math — never compute pace/speed conversions mentally.

    Provide any two of the four variables and the tool solves for the rest:
      pace_min_per_km  (string like "4:30" or "4:30/km")
      speed_km_per_h   (float, e.g. 13.3)
      distance_km      (float, e.g. 10.0)
      duration_seconds (float) OR duration_hms (string like "45:00" or "1:02:30")

    Examples:
      pace_calculator(pace_min_per_km="4:30", distance_km=10)
        → duration = 45:00, speed = 13.33 km/h
      pace_calculator(speed_km_per_h=12, duration_hms="1:00:00")
        → distance = 12.0 km, pace = 5:00/km
      pace_calculator(distance_km=21.1, duration_hms="1:45:00")
        → pace = 4:58/km, speed = 12.06 km/h
    """
    def _parse_pace(s: str) -> float:
        s = s.replace("/km", "").strip()
        parts = s.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    def _parse_hms(s: str) -> float:
        parts = s.strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return int(parts[0]) * 60 + float(parts[1])

    def _fmt_pace(s_per_km: float) -> str:
        m = int(s_per_km // 60)
        s = int(s_per_km % 60)
        return f"{m}:{s:02d}/km"

    def _fmt_duration(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # Parse inputs
    pace_s: Optional[float] = None
    dur_s: Optional[float] = None

    if pace_min_per_km:
        pace_s = _parse_pace(pace_min_per_km)
    if speed_km_per_h is not None:
        pace_s = 3600 / speed_km_per_h
    if duration_hms:
        dur_s = _parse_hms(duration_hms)
    if duration_seconds is not None:
        dur_s = duration_seconds

    # Check for conflicting pace/speed inputs
    if pace_min_per_km and speed_km_per_h is not None:
        return {"error": "Provide pace OR speed, not both."}
    if duration_hms and duration_seconds is not None:
        return {"error": "Provide duration_hms OR duration_seconds, not both."}

    known = sum(x is not None for x in [pace_s, distance_km, dur_s])
    # Allow single pace/speed input for simple unit conversion
    if known == 1 and pace_s is not None and distance_km is None and dur_s is None:
        speed = round(3600 / pace_s, 2)
        return {"pace": _fmt_pace(pace_s), "speed_km_h": speed,
                "distance_km": None, "duration": None, "duration_seconds": None}
    if known < 2:
        return {"error": "Provide at least two of: pace/speed, distance, duration."}

    # Solve for the missing variable
    if pace_s and distance_km and dur_s is None:
        dur_s = pace_s * distance_km
    elif pace_s and dur_s is not None and distance_km is None:
        distance_km = dur_s / pace_s
    elif distance_km and dur_s is not None and pace_s is None:
        pace_s = dur_s / distance_km
    elif known == 3:
        pass  # all three given — just convert/validate

    speed = round(3600 / pace_s, 2) if pace_s else None

    return {
        "pace": _fmt_pace(pace_s) if pace_s else None,
        "speed_km_h": speed,
        "distance_km": round(distance_km, 3) if distance_km else None,
        "duration": _fmt_duration(dur_s) if dur_s else None,
        "duration_seconds": round(dur_s) if dur_s else None,
    }


@mcp.tool()
def schedule_workout(workout_id: int, on_date: str) -> dict:
    """Schedule an existing workout template on a date ('YYYY-MM-DD').

    Returns the schedule_id of the new calendar entry — use it with
    unschedule_workout or reschedule_workout to move/remove it.
    """
    result = _client().schedule_workout(workout_id, on_date)
    return {
        "workout_id": workout_id,
        "date": on_date,
        "schedule_id": result.get("workoutScheduleId") if isinstance(result, dict) else None,
    }


@mcp.tool()
def unschedule_workout(schedule_id: int) -> str:
    """Remove a scheduled instance from the calendar (template stays in library)."""
    _client().unschedule_workout(schedule_id)
    return f"Unscheduled {schedule_id}."


@mcp.tool()
def reschedule_workout(schedule_id: int, new_date: str) -> dict:
    """Move a scheduled workout to a new date (unschedule + reschedule)."""
    client = _client()
    item = client.get_scheduled_workout_by_id(schedule_id) or {}
    workout_id = (item.get("workout") or {}).get("workoutId")
    if not workout_id:
        raise RuntimeError(f"No workoutId found on scheduled item {schedule_id}")
    client.unschedule_workout(schedule_id)
    result = client.schedule_workout(workout_id, new_date)
    return {
        "workout_id": workout_id,
        "new_date": new_date,
        "new_schedule_id": result.get("workoutScheduleId") if isinstance(result, dict) else None,
    }


@mcp.tool()
def swap_scheduled_workouts(date_a: str, date_b: str) -> dict:
    """Swap whatever workouts are scheduled on two dates.

    If both dates have multiple workouts, all of date_a's move to date_b and
    vice versa. If only one date has anything, this is equivalent to moving.
    """
    items_a = list_scheduled_workouts(date_a, date_a)
    items_b = list_scheduled_workouts(date_b, date_b)
    if not items_a and not items_b:
        return {"moved_to_b": [], "moved_to_a": [], "note": "nothing scheduled on either date"}

    moved_to_b = [reschedule_workout(i["schedule_id"], date_b)["workout_id"] for i in items_a]
    moved_to_a = [reschedule_workout(i["schedule_id"], date_a)["workout_id"] for i in items_b]
    return {"moved_to_b": moved_to_b, "moved_to_a": moved_to_a}


@mcp.tool()
def delete_workout_template(workout_id: int) -> str:
    """Delete a workout template entirely (also unschedules it from all dates)."""
    _client().delete_workout(workout_id)
    return f"Deleted workout {workout_id}."


# ─── Activity sync + weekly summary (reads local cache) ────────────────
@mcp.tool()
def sync_activities(force_full: bool = False, weeks_back: Optional[int] = None) -> dict:
    """Pull new activities + HR streams + laps from Garmin into the local cache.

    Runs incrementally since the last sync. An incremental sync runs
    automatically on every server startup, so `new_activities: 0` is normal
    and just means nothing has been recorded since the last run — it does NOT
    mean a recent activity is missing.

    If a recent activity appears to be missing after sync returns 0:
    1. Use `weekly_summary` for the relevant week to check the cache.
       The activity is almost certainly already there under its Garmin ID.
    2. Only call sync again (with force_full=True) if weekly_summary confirms
       the activity is genuinely absent.

    Args:
        force_full: If True, re-pull the default 12-week backfill window.
            Default False — just pick up new activities since last sync.
        weeks_back: Optional explicit backfill window (e.g. 26 or 52) to
            pull deeper history than the 12-week default. Use when the
            agent needs year-long trajectory data (`weekly_summary` will
            return `gap_warning=True` when the requested range is older
            than what's cached).

    Returns dict with new_activities count, streams_fetched count,
    laps_fetched count, last_sync timestamp, and any per-activity errors.
    """
    return garmin_sync.run_sync(_client(), force_full=force_full, weeks_back=weeks_back)


@mcp.tool()
def weekly_summary(start_date: str, end_date: str) -> dict:
    """Per-week training summary from the local Garmin cache.

    Returns `{"weeks": [...], "coverage": {...}}`. Each week entry covers
    one Monday-Sunday week and contains total distance, run count, time
    in each HR zone (computed from raw streams using current bpm
    boundaries from `get_athlete_profile` / coach://user_profile — NOT
    the local cache zones), and the list of activities with names,
    descriptions, distance, HR, and a `classification_hint` derived
    from naming patterns.

    The `coverage` field reports cache extent and a `gap_warning` flag
    when the requested range extends before the oldest cached activity —
    use it to distinguish "no runs that week" from "we don't have data
    that far back" (the local cache holds 12 weeks by default; call
    `sync_activities(weeks_back=N)` to extend it).

    Args:
        start_date: 'YYYY-MM-DD' (inclusive)
        end_date:   'YYYY-MM-DD' (inclusive)
    """
    return garmin_sync.weekly_summary(start_date, end_date)


# ─── Training plan: load, save, materialize, compare ──────────────────
@mcp.tool()
def get_plan() -> dict:
    """Return the current training plan from coach_data/plan.json.

    The plan describes a training block as an ordered list of workouts by
    date. Each workout has a type (easy/threshold/tempo/intervals/long/
    prog-long/race/strength/rest), a name + optional description, and
    either a `continuous` block (mapping to create_continuous_run inputs)
    or an `interval` block (mapping to create_interval_workout inputs).
    Materialized workouts also carry garmin_workout_id and
    garmin_schedule_id.
    """
    p = plan_mod.load_plan()
    if not p:
        return {"error": "No plan found at coach_data/plan.json. Use save_plan to create one."}
    return p


@mcp.tool()
def _resolve_plan_input(
    plan_data: Optional[dict], draft_path: Optional[str]
) -> dict:
    """Return the plan dict from either an inline argument or a JSON file.

    Resolution order:
    1. `plan_data` if explicitly provided
    2. `draft_path` if explicitly provided (read JSON from disk)
    3. Default draft path `coach_data/plan.draft.json` if it exists

    Multi-week plans serialize to 20-40 KB of JSON, which is expensive to
    inline into a tool call. The recommended workflow is to Write the
    draft JSON to disk first, then call these tools with `draft_path` (or
    rely on the default `plan.draft.json` location).
    """
    import json as _json
    from pathlib import Path as _Path

    if plan_data is not None:
        return plan_data

    if draft_path is not None:
        p = _Path(draft_path)
    else:
        p = _Path(__file__).parent / "coach_data" / "plan.draft.json"

    if not p.exists():
        raise FileNotFoundError(
            f"No plan provided and {p} does not exist. Either pass "
            f"plan_data directly, write the draft JSON to {p} first, or "
            f"pass an explicit draft_path."
        )
    return _json.loads(p.read_text(encoding="utf-8"))


def validate_plan(
    plan_data: Optional[dict] = None, draft_path: Optional[str] = None
) -> dict:
    """Check a draft plan for structural issues before save_plan.

    Validates required fields, ISO date format, type enum values, and the
    shape of continuous / interval blocks. Returns {ok, errors, warnings,
    workout_count}.

    Pass either `plan_data` (an in-flight dict, best for short plans) or
    `draft_path` (a path to a JSON file on disk, best for multi-week
    plans where inlining 20-40 KB of JSON into a tool call is expensive).
    With neither argument, reads `coach_data/plan.draft.json` by default.

    Use this BEFORE save_plan to catch issues that would otherwise only
    surface at materialize_plan time.
    """
    return plan_mod.validate_plan(_resolve_plan_input(plan_data, draft_path))


@mcp.tool()
def summarize_plan(
    plan_data: Optional[dict] = None, draft_path: Optional[str] = None
) -> dict:
    """Preview the weekly structure of a draft plan before saving.

    Groups workouts by Mon-Sun week. Per week: session count, distribution
    (quality / easy / long / strength / rest), total estimated km. Plus
    block-level totals.

    Pass either `plan_data` (in-flight dict) or `draft_path` (JSON file on
    disk). With neither argument, reads `coach_data/plan.draft.json`.

    Use this BEFORE save_plan as a sanity check on what you've drafted.
    """
    return plan_mod.summarize_plan(_resolve_plan_input(plan_data, draft_path))


@mcp.tool()
def save_plan(
    plan_data: Optional[dict] = None, draft_path: Optional[str] = None
) -> str:
    """Save a training plan to coach_data/plan.json (overwrites any existing).

    Pass either `plan_data` (in-flight dict, best for short plans) or
    `draft_path` (a path to a JSON file on disk, best for multi-week
    plans). With neither argument, reads `coach_data/plan.draft.json`.

    Expected shape:
        {
          "block_name": "Base 1 — return to running",
          "start_date": "2026-05-28",
          "weeks": 12,
          "workouts": [
            {"date": "2026-05-28", "type": "easy", "name": "Easy 6km",
             "continuous": {"distance_m": 6000}, "description": "Z2 easy"},
            ...
          ]
        }
    """
    plan = _resolve_plan_input(plan_data, draft_path)
    plan_mod.save_plan(plan)
    return f"Saved: {plan.get('block_name', '(unnamed)')} with {len(plan.get('workouts', []))} workouts."


@mcp.tool()
def materialize_plan(from_date: Optional[str] = None) -> dict:
    """Push planned workouts from plan.json to Garmin Connect.

    For each workout in the plan that has not yet been materialized (no
    `garmin_workout_id` set), creates the workout template via
    create_continuous_run / create_interval_workout, schedules it on the
    planned date, and writes the Garmin IDs back to plan.json so subsequent
    calls skip it.

    Skips rest/strength workouts (no Garmin template needed).

    Args:
        from_date: Optional 'YYYY-MM-DD' — only materialize workouts on or
            after this date. Useful for "push just the next week" rather
            than the whole block.
    """
    p = plan_mod.load_plan()
    if not p:
        return {"error": "No plan at coach_data/plan.json. Use save_plan first."}

    created, scheduled, skipped = 0, 0, 0
    errors: list[str] = []

    for w in p["workouts"]:
        if from_date and w["date"] < from_date:
            continue
        if w.get("type") in ("rest", "strength"):
            continue
        if w.get("garmin_workout_id"):
            skipped += 1
            continue

        try:
            if w.get("continuous"):
                c = w["continuous"]
                wid = create_continuous_run(
                    name=w.get("name") or f"{w['type']} {w['date']}",
                    distance_meters=c.get("distance_m"),
                    duration_seconds=c.get("duration_s"),
                    description=w.get("description"),
                )
            elif w.get("interval"):
                iv = w["interval"]
                warmup_step = Step.model_validate(iv["warmup"])
                cooldown_step = Step.model_validate(iv["cooldown"])
                sets = [IntervalSet.model_validate(s) for s in iv["sets"]]
                wid = create_interval_workout(
                    name=w.get("name") or f"{w['type']} {w['date']}",
                    warmup=warmup_step,
                    sets=sets,
                    cooldown=cooldown_step,
                    description=w.get("description"),
                )
            else:
                errors.append(f"{w['date']}: no continuous or interval block")
                continue

            w["garmin_workout_id"] = wid
            created += 1
            # Persist immediately so an exception below doesn't lose the workout_id
            plan_mod.save_plan(p)

            sched = schedule_workout(wid, w["date"])
            sid = sched.get("schedule_id") if isinstance(sched, dict) else None
            if sid:
                w["garmin_schedule_id"] = sid
                scheduled += 1
                plan_mod.save_plan(p)
        except Exception as e:
            errors.append(f"{w['date']}: {type(e).__name__}: {e}")

    return {
        "created": created,
        "scheduled": scheduled,
        "skipped": skipped,
        "errors": errors,
    }


@mcp.tool()
def compare_plan_vs_actual(start_date: str, end_date: str) -> dict:
    """Compare planned workouts against actual cached activities.

    Matches each planned workout on its date with the actual activity from
    Garmin (via the local cache). Medium strictness: type must match
    (via classification_hint), distance within ±15%. Returns per-workout
    status plus a summary count.

    Statuses: compliant, off-distance, off-type, off, missed, rest-violated.
    Activities with no matching planned workout appear in `extras`.
    """
    return plan_mod.compare_plan_vs_actual(start_date, end_date)


# ─── Gear / equipment (Garmin = source of truth) ──────────────────────
@mcp.tool()
def list_gear(active_only: bool = True, with_stats: bool = True) -> list[dict]:
    """List your Garmin gear (shoes etc.) with status and mileage.

    Args:
        active_only: If True (default), only return active (non-retired) gear.
        with_stats: If True (default), include total_distance_km and
            total_activities per item. Adds one API call per gear item —
            fast for typical libraries, but can be slow for very large
            histories.

    Returns list of dicts: uuid, name, make_model, type, status,
    in_use_since, retired_at, and (when with_stats=True)
    total_distance_km and total_activities.
    """
    g = _client()
    profile_id = str(g.get_user_profile()["id"])
    items = g.get_gear(profile_id) or []

    out: list[dict] = []
    for it in items:
        if active_only and it.get("gearStatusName") != "active":
            continue
        rec = {
            "uuid": it.get("uuid"),
            "name": it.get("displayName") or it.get("customMakeModel"),
            "make_model": it.get("customMakeModel"),
            "type": it.get("gearTypeName"),
            "status": it.get("gearStatusName"),
            "in_use_since": (it.get("dateBegin") or "")[:10] or None,
            "retired_at": (it.get("dateEnd") or "")[:10] or None,
        }
        if with_stats and rec["uuid"]:
            try:
                stats = g.get_gear_stats(rec["uuid"])
                rec["total_distance_km"] = round(stats.get("totalDistance", 0) / 1000, 1)
                rec["total_activities"] = stats.get("totalActivities", 0)
            except Exception as e:
                rec["stats_error"] = f"{type(e).__name__}: {e}"
        out.append(rec)
    return out


@mcp.tool()
def shoe_wear_check(
    warning_km: int = 500,
    critical_km: int = 700,
) -> dict:
    """Check shoe mileage against wear thresholds and flag shoes nearing retirement.

    Typical running shoe lifespan is 500-800 km depending on the model,
    surface, and runner weight. Racing shoes and carbon-plated supershoes
    wear faster (~300-500 km).

    Args:
        warning_km: Flag as WARNING above this distance. Default 500 km.
        critical_km: Flag as CRITICAL (overdue for retirement) above this.
            Default 700 km.

    Returns:
        - `summary`: one-line human-readable status
        - `shoes`: list of all active shoes with status (ok/warning/critical),
          total_distance_km, km_remaining (to warning threshold), and
          estimated sessions remaining (based on recent avg session distance)
        - `action_needed`: True if any shoe is warning or critical
    """
    g = _client()
    profile_id = str(g.get_user_profile()["id"])
    items = g.get_gear(profile_id) or []

    shoes = []
    for it in items:
        if it.get("gearStatusName") != "active":
            continue
        if it.get("gearTypeName") not in ("shoes", "running_shoes", None):
            # Include all active gear — Garmin uses varying type names
            pass
        uuid = it.get("uuid")
        name = it.get("displayName") or it.get("customMakeModel") or "Unknown shoe"
        km = 0.0
        activities = 0
        if uuid:
            try:
                stats = g.get_gear_stats(uuid)
                km = round(stats.get("totalDistance", 0) / 1000, 1)
                activities = stats.get("totalActivities", 0)
            except Exception:
                pass

        if km >= critical_km:
            status = "critical"
        elif km >= warning_km:
            status = "warning"
        else:
            status = "ok"

        avg_session_km = round(km / activities, 1) if activities > 0 else None
        km_to_warning = max(0, warning_km - km)
        sessions_to_warning = (
            round(km_to_warning / avg_session_km) if avg_session_km else None
        )

        shoes.append({
            "name": name,
            "status": status,
            "total_distance_km": km,
            "total_activities": activities,
            "avg_session_km": avg_session_km,
            "km_to_warning": round(km_to_warning, 1),
            "sessions_to_warning": sessions_to_warning,
            "in_use_since": (it.get("dateBegin") or "")[:10] or None,
        })

    shoes.sort(key=lambda s: s["total_distance_km"], reverse=True)

    critical = [s for s in shoes if s["status"] == "critical"]
    warning = [s for s in shoes if s["status"] == "warning"]
    action_needed = bool(critical or warning)

    if critical:
        names = ", ".join(s["name"] for s in critical)
        summary = f"CRITICAL: {names} past {critical_km} km — replace soon."
    elif warning:
        names = ", ".join(s["name"] for s in warning)
        summary = f"WARNING: {names} past {warning_km} km — monitor closely."
    else:
        summary = f"All shoes under {warning_km} km — no action needed."

    return {
        "summary": summary,
        "action_needed": action_needed,
        "shoes": shoes,
        "thresholds": {"warning_km": warning_km, "critical_km": critical_km},
    }


@mcp.tool()
def get_gear_for_activity(activity_id: int) -> dict:
    """Return the gear (e.g. shoe) used for a specific activity.

    Useful for reasoning about shoe wear or rotation patterns ("which
    shoes were on yesterday's run?", "which shoe has the most threshold
    work on it?").

    Takes a Garmin activity_id — find it in the Garmin Connect URL
    (`connect.garmin.com/modern/activity/<id>`) or via
    `sync_activities` + `weekly_summary`.
    """
    return _client().get_activity_gear(activity_id)


# ─── Drill-in / recovery / retrospective ──────────────────────────────
@mcp.tool()
def activity_breakdown(activity_id: int) -> dict:
    """**First-line tool for analyzing a single completed activity.** Use
    this before reaching for raw activity data — it returns the lap
    structure, HR-zone distribution, and a heuristic session category in
    one call, all anchored to the user's current HR zones from
    `get_athlete_profile` / coach://user_profile.

    Returns:
    - Metadata: id, date, name, description, distance_m, moving_time_s,
      avg_hr, max_hr, sport_type
    - `laps`: list of {lap_index, type, distance_m, moving_time_s,
      pace_s_per_km, avg_hr, max_hr}. `type` is auto-classified as
      "drag" (work rep, Z3+ avg HR ≥30s), "pause" (recovery between
      drags), "wu" (warmup before first drag), "cd" (cooldown after
      last drag), or "easy" (continuous easy run, no drags found).
    - `zone_secs` + `zone_pcts`: time in each HR zone (Z1-Z5).
    - `session_category`: heuristic "easy" | "sub-threshold" |
      "at-threshold" | "vo2" — useful for compliance scoring against
      the plan. Refine ambiguous edges via coach://classification.
    - `classification_hint`: name-pattern hint (deterministic 90% case).

    The activity must be in the local cache. If `error` is
    returned with `next_steps`, call `sync_activities()` (or
    `sync_activities(weeks_back=N)` for older activities) and retry.
    Laps are cached from Garmin at sync time.

    Garmin activity_id.
    """
    return garmin_sync.activity_breakdown(activity_id)


@mcp.tool()
def get_wellness_history(
    start_date: str,
    end_date: str,
    force_refetch: bool = False,
) -> dict:
    """Historical daily wellness metrics (HRV, RHR, sleep, stress, body battery)
    with rolling averages.

    On first call for a date, the daily metrics are pulled from Garmin and
    cached in coach_data/cache.db. Subsequent calls in the same range read
    from the cache and are fast. A first 90-day backfill takes ~30-60s.

    Rolling averages:
    - **RHR:** simple 7-day mean (it's a low-noise signal).
    - **HRV:** 7-day **geometric mean** (mean of ln(HRV), exp back).
      HRV is roughly log-normally distributed; this is the right shape
      per Altini's research and what HRV4Training uses.

    Args:
        start_date: 'YYYY-MM-DD' (inclusive).
        end_date:   'YYYY-MM-DD' (inclusive).
        force_refetch: If True, re-pull all days from Garmin even if cached.

    Returns dict with:
      - range: start/end/days
      - daily: list of {date, resting_hr, hrv_overnight_avg, hrv_status,
        hrv_baseline_low/upper, sleep_seconds, sleep_score, sleep stage
        durations, avg_stress, body_battery_*, respiration_avg, spo2_avg,
        recovery_time_hours}
      - rolling: list of {date, rhr_7d_mean, hrv_7d_geomean}
      - summary: min/max/mean for RHR and HRV across the range, plus the
        most recent Garmin "balanced HRV" baseline band for context

    Note: rows cached before a field was added to the schema will return
    null for that field. Call with `force_refetch=True` for the relevant
    range to backfill.
    """
    sync_result = garmin_sync.sync_wellness_range(
        _client(), start_date, end_date, force_refetch=force_refetch
    )
    data = garmin_sync.wellness_history(start_date, end_date)
    data["sync"] = sync_result
    return data


def _extract_training_summary(readiness, status) -> dict:
    """Flatten the key fields out of Garmin's verbose readiness + status payloads.

    Returns a single-level dict of the things a coach actually checks:
    readiness score/level/feedback, recovery time, ACWR, acute load,
    training status verbal label (PRODUCTIVE / MAINTAINING / etc.) and
    current VO2max estimate. Missing fields are silently None; the raw
    payloads are still returned alongside for anything not extracted.
    """
    out: dict = {}

    r = readiness[0] if isinstance(readiness, list) and readiness else (
        readiness if isinstance(readiness, dict) else {}
    )
    if r and "error" not in r:
        out["readiness_score"] = r.get("score")
        out["readiness_level"] = r.get("level")
        out["readiness_feedback"] = r.get("feedbackLong") or r.get("feedbackShort")
        # Garmin's recoveryTime is in MINUTES, not hours, despite the
        # watch display showing hours. Convert before reporting.
        raw_rt_min = r.get("recoveryTime")
        out["recovery_time_hours"] = (
            round(raw_rt_min / 60) if raw_rt_min is not None else None
        )
        out["acute_load"] = r.get("acuteLoad")
        # Garmin doesn't expose a raw ACWR number — only the factor (0-100)
        # and a verbal feedback like "VERY_GOOD" / "POOR".
        out["acwr_factor_percent"] = r.get("acwrFactorPercent")
        out["acwr_factor_feedback"] = r.get("acwrFactorFeedback")
        out["hrv_factor_percent"] = r.get("hrvFactorPercent")
        out["hrv_factor_feedback"] = r.get("hrvFactorFeedback")
        out["sleep_score_factor_percent"] = r.get("sleepScoreFactorPercent")
        out["sleep_score_factor_feedback"] = r.get("sleepScoreFactorFeedback")
        out["sleep_history_factor_percent"] = r.get("sleepHistoryFactorPercent")
        out["sleep_history_factor_feedback"] = r.get("sleepHistoryFactorFeedback")
        out["stress_history_factor_percent"] = r.get("stressHistoryFactorPercent")
        out["stress_history_factor_feedback"] = r.get("stressHistoryFactorFeedback")
        out["recovery_time_factor_percent"] = r.get("recoveryTimeFactorPercent")
        out["recovery_time_factor_feedback"] = r.get("recoveryTimeFactorFeedback")

    if isinstance(status, dict) and "error" not in status:
        # VO2max lives under mostRecentVO2Max, not the training status block.
        vo2 = (status.get("mostRecentVO2Max") or {}).get("generic") or {}
        out["vo2max"] = vo2.get("vo2MaxValue")
        out["vo2max_precise"] = vo2.get("vo2MaxPreciseValue")
        out["fitness_age"] = vo2.get("fitnessAge")

        ts = status.get("mostRecentTrainingStatus") or {}
        latest = ts.get("latestTrainingStatusData") or {}
        # latestTrainingStatusData is keyed by deviceId — grab the first device.
        device_data = next(iter(latest.values()), {}) if isinstance(latest, dict) else {}
        if device_data:
            out["training_status_code"] = device_data.get("trainingStatus")
            out["training_status"] = device_data.get("trainingStatusFeedbackPhrase")
            out["weekly_training_load"] = device_data.get("weeklyTrainingLoad")
            out["load_tunnel_min"] = device_data.get("loadTunnelMin")
            out["load_tunnel_max"] = device_data.get("loadTunnelMax")
            out["fitness_trend"] = device_data.get("fitnessTrend")
            out["load_level_trend"] = device_data.get("loadLevelTrend")

    return out


@mcp.tool()
def morning_check_in() -> dict:
    """Today's recovery snapshot — flattened metrics, 7-day trend deltas,
    and Garmin's readiness/status assessments. All in one call.

    Returns:
    - `wellness.today`: flat HRV (overnight, weekly avg, baseline band,
      status), RHR, sleep (duration, score, deep/REM/light/awake),
      stress, body battery (high/low/at-wake), respiration, SpO2.
    - `wellness.trends`: prior 7-day mean + delta + stdev + deviation
      flag for each metric. Flag fires when today is >1σ outside the
      trailing mean in the "bad" direction (HRV ↓, RHR ↑, sleep ↓,
      stress ↑).
    - `training_summary`: flat readiness score/level/feedback, ACWR,
      acute load, recovery time, training status verbal (PRODUCTIVE /
      MAINTAINING / etc.), VO2max, weekly load.
    - `training_readiness_raw`, `training_status_raw`, `body_battery`:
      the full Garmin payloads for anything not flattened above.

    Use to decide whether to do a planned quality session today or shift
    it (e.g., HRV deviation_low + elevated RHR + low readiness → defer
    threshold). For multi-day trends beyond 7 days, use
    `get_wellness_history`.
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz
    g = _client()
    today = _date.today()
    yesterday = today - _td(days=1)
    history_start = (today - _td(days=8)).isoformat()
    history_end = yesterday.isoformat()

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    wellness = garmin_sync.morning_check_in_data(
        g, today.isoformat(), yesterday.isoformat(), history_start, history_end,
    )

    training_readiness_raw = safe(g.get_training_readiness, today.isoformat())
    training_status_raw = safe(g.get_training_status, today.isoformat())
    body_battery = safe(g.get_body_battery, today.isoformat())
    training_summary = _extract_training_summary(
        training_readiness_raw, training_status_raw
    )

    return {
        "date": today.isoformat(),
        "captured_at": _dt.now(_tz.utc).isoformat(),
        "wellness": wellness,
        "training_summary": training_summary,
        "training_readiness_raw": training_readiness_raw,
        "training_status_raw": training_status_raw,
        "body_battery": body_battery,
    }


@mcp.tool()
def sleep_performance_correlation(
    days_back: int = 60,
    min_distance_km: float = 3.0,
) -> dict:
    """Find the relationship between sleep quality and running performance.

    Joins run activities with wellness data for the same date. Splits
    sessions into 'good sleep' (sleep_score >= 70 OR sleep >= 7 hours)
    vs 'poor sleep' and compares average HR and pace between groups.
    Also surfaces the 5 best performances (lowest pace) and their
    associated sleep metrics.

    Args:
        days_back: How many days of history to analyse. Default 60.
        min_distance_km: Minimum run distance to include. Default 3.0.

    Returns:
        good_sleep_avg, poor_sleep_avg, best_performances, insight string,
        and per-run data rows used for the analysis.
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta as _td

    try:
        cutoff = (_date.today() - _td(days=days_back)).isoformat()
        min_dist_m = min_distance_km * 1000

        with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    a.id,
                    date(a.start_date_local) AS run_date,
                    a.name,
                    a.avg_hr,
                    a.moving_time_s,
                    a.distance_m,
                    w.sleep_seconds,
                    w.sleep_score,
                    w.hrv_overnight_avg,
                    w.resting_hr AS wellness_rhr
                FROM activities a
                LEFT JOIN wellness_daily w ON date(a.start_date_local) = w.date
                WHERE a.sport_type = 'Run'
                  AND date(a.start_date_local) >= ?
                  AND a.distance_m >= ?
                  AND a.moving_time_s IS NOT NULL
                  AND a.moving_time_s > 0
                ORDER BY a.start_date_local
                """,
                (cutoff, min_dist_m),
            ).fetchall()

        runs = [dict(r) for r in rows]

        # Compute pace for each run
        for r in runs:
            if r["distance_m"] and r["moving_time_s"]:
                r["pace_s_per_km"] = r["moving_time_s"] / (r["distance_m"] / 1000)
            else:
                r["pace_s_per_km"] = None

        def _is_good_sleep(r: dict) -> bool:
            ss = r.get("sleep_score")
            secs = r.get("sleep_seconds")
            if ss is not None and ss >= 70:
                return True
            if secs is not None and secs >= 25200:  # 7 hours
                return True
            return False

        def _has_sleep_data(r: dict) -> bool:
            return r.get("sleep_score") is not None or r.get("sleep_seconds") is not None

        # Split into groups — only include runs that have sleep data
        runs_with_sleep = [r for r in runs if _has_sleep_data(r)]
        good_sleep = [r for r in runs_with_sleep if _is_good_sleep(r)]
        poor_sleep = [r for r in runs_with_sleep if not _is_good_sleep(r)]

        def _avg(values: list) -> Optional[float]:
            vals = [v for v in values if v is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        def _group_stats(group: list) -> dict:
            return {
                "n": len(group),
                "avg_hr": _avg([r["avg_hr"] for r in group]),
                "avg_pace_s_per_km": _avg([r["pace_s_per_km"] for r in group]),
                "avg_sleep_score": _avg([r["sleep_score"] for r in group]),
                "avg_sleep_hours": _avg(
                    [round(r["sleep_seconds"] / 3600, 2) for r in group
                     if r.get("sleep_seconds") is not None]
                ),
                "avg_hrv": _avg([r["hrv_overnight_avg"] for r in group]),
            }

        good_stats = _group_stats(good_sleep)
        poor_stats = _group_stats(poor_sleep)

        # Best 5 performances = lowest pace (fastest), runs that have pace data
        runs_with_pace = [r for r in runs if r.get("pace_s_per_km") is not None]
        best_5 = sorted(runs_with_pace, key=lambda r: r["pace_s_per_km"])[:5]
        best_performances = [
            {
                "date": r["run_date"],
                "name": r["name"],
                "distance_km": round(r["distance_m"] / 1000, 2),
                "pace_s_per_km": round(r["pace_s_per_km"], 1),
                "avg_hr": r["avg_hr"],
                "sleep_score": r["sleep_score"],
                "sleep_hours": (
                    round(r["sleep_seconds"] / 3600, 2)
                    if r.get("sleep_seconds") is not None else None
                ),
                "hrv_overnight_avg": r["hrv_overnight_avg"],
                "sleep_quality": "good" if _is_good_sleep(r) else (
                    "poor" if _has_sleep_data(r) else "no_data"
                ),
            }
            for r in best_5
        ]

        # Build insight string
        def _fmt_pace(s: Optional[float]) -> str:
            if s is None:
                return "N/A"
            m = int(s // 60)
            sec = int(s % 60)
            return f"{m}:{sec:02d}/km"

        insight_parts: list[str] = []
        g_n, p_n = good_stats["n"], poor_stats["n"]

        if g_n == 0 and p_n == 0:
            insight_parts.append(
                f"No runs with sleep data found in the last {days_back} days "
                f"(>= {min_distance_km} km). Sync wellness data first."
            )
        elif g_n == 0:
            insight_parts.append(
                f"Only poor-sleep runs found ({p_n} sessions). "
                "Need good-sleep sessions to compare."
            )
        elif p_n == 0:
            insight_parts.append(
                f"Only good-sleep runs found ({g_n} sessions). "
                "Need poor-sleep sessions to compare."
            )
        else:
            pace_good = good_stats["avg_pace_s_per_km"]
            pace_poor = poor_stats["avg_pace_s_per_km"]
            hr_good = good_stats["avg_hr"]
            hr_poor = poor_stats["avg_hr"]
            insight_parts.append(
                f"Analysed {g_n + p_n} runs ({g_n} good-sleep, {p_n} poor-sleep) "
                f"over the last {days_back} days."
            )
            if pace_good is not None and pace_poor is not None:
                diff = round(pace_poor - pace_good, 1)
                direction = "faster" if diff > 0 else "slower"
                insight_parts.append(
                    f"Good-sleep pace: {_fmt_pace(pace_good)} vs "
                    f"poor-sleep: {_fmt_pace(pace_poor)} "
                    f"({abs(diff):.1f}s/km {direction} on good sleep)."
                )
            if hr_good is not None and hr_poor is not None:
                hr_diff = round(hr_poor - hr_good, 1)
                insight_parts.append(
                    f"Good-sleep avg HR: {hr_good} bpm vs poor-sleep: {hr_poor} bpm "
                    f"(delta {hr_diff:+.1f} bpm)."
                )

        best_sleep_quality_counts = {
            "good": sum(1 for b in best_performances if b["sleep_quality"] == "good"),
            "poor": sum(1 for b in best_performances if b["sleep_quality"] == "poor"),
            "no_data": sum(1 for b in best_performances if b["sleep_quality"] == "no_data"),
        }
        if best_performances:
            insight_parts.append(
                f"Of the 5 best performances: "
                f"{best_sleep_quality_counts['good']} on good sleep, "
                f"{best_sleep_quality_counts['poor']} on poor sleep, "
                f"{best_sleep_quality_counts['no_data']} with no sleep data."
            )

        return {
            "days_back": days_back,
            "min_distance_km": min_distance_km,
            "total_runs_in_window": len(runs),
            "runs_with_sleep_data": len(runs_with_sleep),
            "good_sleep_avg": good_stats,
            "poor_sleep_avg": poor_stats,
            "best_performances": best_performances,
            "insight": " ".join(insight_parts),
        }

    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def weekly_retrospective(week_start: str) -> dict:
    """Combined weekly summary + plan compliance for one Mon-Sun week.

    Bundles `weekly_summary` (volume, zone time, sessions) with
    `compare_plan_vs_actual` (compliance against plan.json) for a single
    week. Use as a Sunday-evening reflection input — one tool call covers
    both "what did I do" and "how close to plan was I".

    Args:
        week_start: 'YYYY-MM-DD' (typically the Monday of the week).
    """
    from datetime import date as _date, timedelta as _td
    start = _date.fromisoformat(week_start)
    end = start + _td(days=6)
    result = garmin_sync.weekly_summary(start.isoformat(), end.isoformat())
    weeks = result["weeks"]
    return {
        "week_start": week_start,
        "week_end": end.isoformat(),
        "summary": weeks[0] if weeks else None,
        "coverage": result["coverage"],
        "plan_compliance": plan_mod.compare_plan_vs_actual(
            start.isoformat(), end.isoformat()
        ),
    }


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
