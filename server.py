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
def elevation_impact(
    actual_pace_min_per_km: Optional[str] = None,
    elevation_gain_m: Optional[float] = None,
    distance_km: Optional[float] = None,
    activity_id: Optional[int] = None,
) -> dict:
    """Estimate how elevation gain affects running effort (grade-adjusted pace).

    Uses a simple linear heuristic (NOT Strava's nonlinear GAP model —
    treat the output as a rough estimate; it ignores the descent rebate,
    so rolling/out-and-back routes are overestimated):
      GAP_factor = 1 + (avg_grade_pct * 0.033)
      flat_equivalent_pace = actual_pace / GAP_factor

    Call modes:
    - Manual: provide actual_pace_min_per_km + elevation_gain_m + distance_km.
    - Cache lookup: provide activity_id — distance and elevation are read from
      the local cache (actual_pace_min_per_km is still required).
    - Both: activity_id + actual_pace_min_per_km (cache supplies distance/elevation).

    Args:
        actual_pace_min_per_km: Pace string like "5:30" or "5:30/km".
        elevation_gain_m: Total elevation gain in meters.
        distance_km: Distance in kilometers.
        activity_id: Optional Garmin activity ID to look up distance and elevation
            from the local cache instead of passing them manually.

    Returns:
        - flat_equivalent_pace: What this run equals on flat terrain (e.g. "5:05/km").
        - elevation_cost_seconds: How many seconds the climbing added to the total time.
        - elevation_cost_formatted: Human-readable string like "3:20 added".
        - avg_grade_pct: Average grade as a percentage.
        - effort_level: "easy" / "moderate" / "significant" / "hilly" based on
          m/km ratio (< 10 / 10-20 / 20-40 / > 40).
        - note: Plain-English summary sentence.
    """
    try:
        def _parse_pace(s: str) -> float:
            """Return pace in seconds/km from 'M:SS' or 'M:SS/km'."""
            s = s.replace("/km", "").strip()
            parts = s.split(":")
            if len(parts) != 2:
                raise ValueError(f"expected M:SS, got {s!r}")
            return int(parts[0]) * 60 + int(parts[1])

        def _fmt_pace(s_per_km: float) -> str:
            m = int(s_per_km // 60)
            s = int(round(s_per_km % 60))
            return f"{m}:{s:02d}/km"

        def _fmt_duration(seconds: float) -> str:
            m = int(abs(seconds) // 60)
            s = int(round(abs(seconds) % 60))
            return f"{m}:{s:02d}"

        # --- Optional cache lookup ---
        if activity_id is not None:
            import sqlite3 as _sqlite3
            from garmin_sync import DB_PATH as _DB_PATH
            try:
                with _sqlite3.connect(_DB_PATH) as _conn:
                    _row = _conn.execute(
                        "SELECT distance_m, total_elevation_gain FROM activities WHERE id = ?",
                        (activity_id,),
                    ).fetchone()
                if _row is None:
                    return {
                        "error": (
                            f"Activity {activity_id} not found in local cache. "
                            "Call sync_activities() first."
                        )
                    }
                if distance_km is None and _row[0] is not None:
                    distance_km = _row[0] / 1000.0
                if elevation_gain_m is None:
                    if _row[1] is None:
                        return {
                            "error": (
                                f"Activity {activity_id} has no elevation data in the "
                                "cache (likely a treadmill/indoor run) — elevation "
                                "analysis is not applicable."
                            )
                        }
                    elevation_gain_m = float(_row[1])
            except Exception as e:
                return {"error": f"Cache lookup failed: {type(e).__name__}: {e}"}

        # --- Validate inputs ---
        if actual_pace_min_per_km is None:
            return {"error": "actual_pace_min_per_km is required."}
        if elevation_gain_m is None:
            return {"error": "elevation_gain_m is required (or pass activity_id to read from cache)."}
        if elevation_gain_m < 0:
            return {"error": "elevation_gain_m must be >= 0 (total ascent, not net elevation change)."}
        if distance_km is None:
            return {"error": "distance_km is required (or pass activity_id to read from cache)."}
        if distance_km <= 0:
            return {"error": "distance_km must be greater than 0."}

        try:
            actual_pace_s = _parse_pace(actual_pace_min_per_km)
        except Exception:
            return {"error": f"Could not parse pace '{actual_pace_min_per_km}'. Use format like '5:30' or '5:30/km'."}

        # --- GAP calculation ---
        avg_grade_pct = (elevation_gain_m / (distance_km * 1000)) * 100
        gap_factor = 1 + (avg_grade_pct * 0.033)
        flat_pace_s = actual_pace_s / gap_factor

        # Total time on actual course vs equivalent flat time
        actual_total_s = actual_pace_s * distance_km
        flat_total_s = flat_pace_s * distance_km
        elevation_cost_s = actual_total_s - flat_total_s

        # --- Effort level ---
        m_per_km = elevation_gain_m / distance_km
        if m_per_km < 10:
            effort_level = "easy"
        elif m_per_km < 20:
            effort_level = "moderate"
        elif m_per_km <= 40:
            effort_level = "significant"
        else:
            effort_level = "hilly"

        flat_pace_str = _fmt_pace(flat_pace_s)
        cost_str = _fmt_duration(elevation_cost_s)
        note = (
            f"Your {actual_pace_min_per_km.replace('/km', '')}/km on this route "
            f"= {flat_pace_str} on flat terrain"
        )

        return {
            "flat_equivalent_pace": flat_pace_str,
            "elevation_cost_seconds": round(elevation_cost_s),
            "elevation_cost_formatted": f"{cost_str} added",
            "avg_grade_pct": round(avg_grade_pct, 2),
            "effort_level": effort_level,
            "m_per_km": round(m_per_km, 1),
            "gap_factor": round(gap_factor, 4),
            "inputs": {
                "actual_pace": actual_pace_min_per_km.replace("/km", ""),
                "elevation_gain_m": elevation_gain_m,
                "distance_km": distance_km,
                "activity_id": activity_id,
            },
            "note": note,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


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
def sync_activities(
    force_full: bool = False,
    weeks_back: Optional[int] = None,
    backfill_links: bool = False,
    backfill_max: int = 100,
) -> dict:
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
        backfill_links: If True, also fetch workout-linkage detail
            (associated_workout_id, planned_type, RPE/feel/compliance,
            training_effect_label) for cached activities synced before
            those fields existed. One extra API call per activity —
            new activities get this automatically; this is only for
            history.
        backfill_max: Max activities to backfill per call (default 100).
            `remaining_without_detail` in the response tells you whether
            another round is needed.

    Returns dict with new_activities count, streams_fetched count,
    laps_fetched count, details_fetched count, last_sync timestamp, and
    any per-activity errors. With backfill_links also details_fetched /
    relinked / remaining_without_detail for the backfill pass.
    """
    result = garmin_sync.run_sync(_client(), force_full=force_full, weeks_back=weeks_back)
    if backfill_links and "error" not in result:
        result["backfill"] = garmin_sync.backfill_workout_links(
            _client(), max_activities=backfill_max
        )
    return result


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


@mcp.tool()
def list_activities(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    sport_type: Optional[str] = None,
    started_before: Optional[str] = None,
    started_after: Optional[str] = None,
    name_contains: Optional[str] = None,
    classification: Optional[str] = None,
    limit: int = 200,
) -> dict:
    """Flat, filterable list of cached activities — one lightweight row each.

    Use this for cross-activity analysis over many sessions (e.g. "all runs
    before 09:00", "every threshold session this year", "easy runs over
    10 km"). Unlike `weekly_summary` it returns per-activity metadata
    including the local start TIME, and scales to the full cache in one
    call. For per-lap detail on a single session use `activity_breakdown`;
    for arbitrary aggregations use `query_activity_cache`.

    All filters are optional and combine with AND:
        start_date / end_date: 'YYYY-MM-DD' (inclusive) activity date range.
        sport_type: exact match, e.g. 'Run', 'Rowing', 'NordicSki',
            'WeightTraining', 'Ride'.
        started_before / started_after: 'HH:MM' local start-of-day time —
            e.g. started_before='09:00' for morning sessions,
            started_after='16:00' for evening sessions.
        name_contains: case-insensitive substring match on activity name.
        classification: filter on classification_hint — one of easy/
            threshold/tempo/intervals/long/prog-long/race/strength/hike/
            ride/unknown.
        limit: max rows returned (default 200, cap 1000). `matched_count`
            in the response tells you if more matched than were returned.

    Each activity row: id, date, start_time ('HH:MM'), name, sport_type,
    distance_km, moving_time_s, avg_hr, max_hr, elevation_gain_m,
    pace_per_km ('M:SS'), classification_hint, classification_source,
    training_effect_label, workout_rpe, workout_feel, workout_compliance.

    classification_hint is the planned type from the training plan when
    the activity was run from a materialized workout
    (classification_source='plan' — ground truth), falling back to
    name-pattern matching (classification_source='name'). RPE/feel are
    the watch's post-workout self-evaluation (0-100), compliance is
    Garmin's execution score, and training_effect_label is Garmin's
    physiological auto-label (TEMPO/AEROBIC_BASE/...) — a response
    signal, NOT session intent. These are null for activities synced
    before linkage existed; run sync_activities(backfill_links=True) to
    populate history.

    The response also carries the same `coverage` / `gap_warning`
    metadata as weekly_summary — check it to distinguish "no matches"
    from "cache doesn't go back that far" (default cache depth is 12
    weeks; extend with sync_activities(weeks_back=N)).
    """
    return garmin_sync.list_activities(
        start_date=start_date, end_date=end_date, sport_type=sport_type,
        started_before=started_before, started_after=started_after,
        name_contains=name_contains, classification=classification,
        limit=limit,
    )


@mcp.tool()
def query_activity_cache(
    sql: str,
    params: Optional[list] = None,
    limit: int = 200,
    max_cell_chars: int = 500,
) -> dict:
    """Run a read-only SQL SELECT against the local activity cache.

    Escape hatch for analyses the dedicated tools don't cover: arbitrary
    grouping, joins, time-of-day buckets, HR distributions, trends. The
    connection is opened read-only at the SQLite level — only a single
    SELECT (or WITH ... SELECT) statement is accepted. Prefer
    `list_activities` / `weekly_summary` / `get_wellness_history` when
    they fit; reach for SQL when they don't.

    Schema (all timestamps are local time, ISO 'YYYY-MM-DDTHH:MM:SS'):
      activities(id, start_date_local, name, description, type,
                 sport_type, distance_m, moving_time_s, elapsed_time_s,
                 avg_hr, max_hr, total_elevation_gain, synced_at,
                 associated_workout_id, planned_type,
                 training_effect_label, workout_rpe, workout_feel,
                 workout_compliance, detail_fetched_at)
          associated_workout_id: Garmin workout template the activity
          executed (null for free runs). planned_type: the plan's label
          for that workout (threshold/easy/long/...) — ground truth for
          classification when present. workout_rpe/workout_feel: watch
          post-workout self-evaluation (0-100). workout_compliance:
          Garmin execution score. training_effect_label: Garmin's
          physiological auto-label — response signal, not intent.
      workout_type_map(garmin_workout_id, planned_type, workout_name,
                 plan_name, planned_date, updated_at)
          Durable workout_id → planned type mapping, written at
          materialize time; survives plan.json being replaced.
      laps(activity_id, laps_json, fetched_at)
          laps_json: JSON array of laps, each with lap_index, lap_type
          ('wu'/'drag'/'pause'/'cd'/'lap'), distance_m, moving_time_s,
          avg_hr, max_hr, avg_speed_m_s.
      streams(activity_id, time_json, hr_json)
          Parallel JSON arrays of elapsed seconds and HR samples. These
          are LARGE (thousands of points) — never SELECT them raw; use
          json_each() to aggregate in SQL, e.g.:
          SELECT avg(value) FROM streams, json_each(hr_json)
          WHERE activity_id = 123.
      wellness_daily(date, resting_hr, hrv_overnight_avg, hrv_weekly_avg,
                 hrv_status, hrv_baseline_low, hrv_baseline_upper,
                 sleep_seconds, sleep_score, sleep_deep_s, sleep_rem_s,
                 sleep_light_s, sleep_awake_s, avg_stress,
                 body_battery_high, body_battery_low,
                 body_battery_at_wake, respiration_avg, spo2_avg,
                 recovery_time_hours, synced_at)
      sync_state(key, value)

    Useful idioms: substr(start_date_local, 12, 5) gives 'HH:MM' start
    time; date(start_date_local) gives the date; SQLite JSON1 functions
    (json_each, json_extract, json_array_length) are available for the
    laps/streams JSON columns.

    Args:
        sql: a single SELECT or WITH ... SELECT statement. Use ? placeholders
            with `params` for values.
        params: positional parameters for ? placeholders.
        limit: max rows returned (default 200, cap 1000); `truncated_rows`
            is True when the query matched more.
        max_cell_chars: long text cells are cut at this length (default 500)
            and marked — raise it deliberately if you truly need a big blob.

    Returns {columns, rows, row_count, truncated_rows, truncated_cells}
    or {error} on invalid/non-SELECT SQL.
    """
    return garmin_sync.query_cache(
        sql, params=params, limit=limit, max_cell_chars=max_cell_chars
    )


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
            # Durable workout_id → type mapping; survives plan.json turnover
            garmin_sync.record_workout_types([w], p.get("block_name"))

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
def running_form_trends(activities_to_analyze: int = 20) -> dict:
    """Track running dynamics (form) over recent runs using Garmin data.

    Fetches the last 30 activities from Garmin, filters to runs, and
    extracts running dynamics: cadence, ground contact time, vertical
    oscillation, stride length, and vertical ratio.

    Args:
        activities_to_analyze: How many recent runs to include in the
            analysis. Default 20. Must be between 1 and 30.

    Returns:
        - per_activity: list of {date, distance_km, cadence, ground_contact_ms,
          vertical_osc_cm, stride_length_m, vertical_ratio_pct}
        - averages: mean of each metric across all analyzed runs
        - trends: comparison of first half vs second half of analyzed window
          (improving/stable/declining per metric, and raw values)
        - ratings: dict of metric → 'elite'/'good'/'needs_work' based on
          Garmin benchmarks
        - insights: list of human-readable coaching observations

    Garmin benchmarks used for ratings:
        - Cadence (spm): ≥180 elite, 170-179 good, <165 needs work
        - Ground contact (ms): <200 elite, 200-240 good, >260 needs work
        - Vertical oscillation (cm): <6 elite, 6-8 good, >10 needs work
        - Vertical ratio (%): <6 elite, 6-8 good, >10 needs work
    """
    try:
        activities_to_analyze = max(1, min(30, activities_to_analyze))
        raw = _client().get_activities(0, 30)
        if not raw:
            return {"error": "No activities returned from Garmin API."}

        runs = [
            a for a in raw
            if (a.get("activityType") or {}).get("typeKey", "").endswith("running")
            or (a.get("activityType") or {}).get("typeKey", "") in (
                "running", "indoor_running", "treadmill_running",
                "trail_running", "track_running", "virtual_run",
            )
        ]

        if not runs:
            return {"error": "No running activities found in the last 30 activities."}

        runs = runs[:activities_to_analyze]

        per_activity = []
        for act in runs:
            date_str = (act.get("startTimeLocal") or "")[:10]
            dist_m = act.get("distance") or 0
            dist_km = round(dist_m / 1000, 2) if dist_m else None

            cadence = act.get("averageRunningCadenceInStepsPerMinute")
            gct = act.get("avgGroundContactTime")       # milliseconds
            vo = act.get("avgVerticalOscillation")       # centimeters
            stride = act.get("avgStrideLength")          # centimeters from the API
            vr = act.get("avgVerticalRatio")             # percent
            if stride is not None:
                stride = stride / 100.0                  # → meters

            # Skip activities with no running dynamics at all
            if all(v is None for v in (cadence, gct, vo, stride, vr)):
                continue

            per_activity.append({
                "date": date_str,
                "distance_km": dist_km,
                "cadence": round(cadence, 1) if cadence is not None else None,
                "ground_contact_ms": round(gct, 1) if gct is not None else None,
                "vertical_osc_cm": round(vo, 1) if vo is not None else None,
                "stride_length_m": round(stride, 2) if stride is not None else None,
                "vertical_ratio_pct": round(vr, 1) if vr is not None else None,
            })

        if not per_activity:
            return {
                "error": (
                    "No running dynamics data found. Your Garmin device may not "
                    "record running form metrics, or no runs have been synced yet."
                ),
                "activities_checked": len(runs),
            }

        def _avg(field: str) -> Optional[float]:
            vals = [a[field] for a in per_activity if a.get(field) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        averages = {
            "cadence": _avg("cadence"),
            "ground_contact_ms": _avg("ground_contact_ms"),
            "vertical_osc_cm": _avg("vertical_osc_cm"),
            "stride_length_m": _avg("stride_length_m"),
            "vertical_ratio_pct": _avg("vertical_ratio_pct"),
            "runs_with_data": len(per_activity),
        }

        # Trends: first half vs second half (chronological order — oldest first)
        # per_activity is newest-first (Garmin API order), so reverse for trend calc
        ordered = list(reversed(per_activity))
        mid = len(ordered) // 2
        first_half = ordered[:mid] if mid > 0 else []
        second_half = ordered[mid:] if mid > 0 else ordered

        def _half_avg(half: list[dict], field: str) -> Optional[float]:
            vals = [a[field] for a in half if a.get(field) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        def _trend_direction(field: str, higher_is_better: bool) -> str:
            """Return 'improving', 'stable', or 'declining'."""
            a = _half_avg(first_half, field)
            b = _half_avg(second_half, field)
            if a is None or b is None:
                return "insufficient_data"
            delta = b - a
            threshold = abs(a) * 0.02  # 2% change threshold for "stable"
            if abs(delta) <= threshold:
                return "stable"
            if (delta > 0) == higher_is_better:
                return "improving"
            return "declining"

        trends = {
            "cadence": {
                "direction": _trend_direction("cadence", higher_is_better=True),
                "first_half_avg": _half_avg(first_half, "cadence"),
                "second_half_avg": _half_avg(second_half, "cadence"),
            },
            "ground_contact_ms": {
                "direction": _trend_direction("ground_contact_ms", higher_is_better=False),
                "first_half_avg": _half_avg(first_half, "ground_contact_ms"),
                "second_half_avg": _half_avg(second_half, "ground_contact_ms"),
            },
            "vertical_osc_cm": {
                "direction": _trend_direction("vertical_osc_cm", higher_is_better=False),
                "first_half_avg": _half_avg(first_half, "vertical_osc_cm"),
                "second_half_avg": _half_avg(second_half, "vertical_osc_cm"),
            },
            "stride_length_m": {
                "direction": _trend_direction("stride_length_m", higher_is_better=True),
                "first_half_avg": _half_avg(first_half, "stride_length_m"),
                "second_half_avg": _half_avg(second_half, "stride_length_m"),
            },
            "vertical_ratio_pct": {
                "direction": _trend_direction("vertical_ratio_pct", higher_is_better=False),
                "first_half_avg": _half_avg(first_half, "vertical_ratio_pct"),
                "second_half_avg": _half_avg(second_half, "vertical_ratio_pct"),
            },
        }

        # Ratings based on Garmin benchmarks
        def _rate_cadence(v: Optional[float]) -> Optional[str]:
            if v is None:
                return None
            if v >= 180:
                return "elite"
            if v >= 170:
                return "good"
            return "needs_work"

        def _rate_gct(v: Optional[float]) -> Optional[str]:
            if v is None:
                return None
            if v < 200:
                return "elite"
            if v <= 240:
                return "good"
            return "needs_work"

        def _rate_vo(v: Optional[float]) -> Optional[str]:
            if v is None:
                return None
            if v < 6:
                return "elite"
            if v <= 8:
                return "good"
            return "needs_work"

        def _rate_vr(v: Optional[float]) -> Optional[str]:
            if v is None:
                return None
            if v < 6:
                return "elite"
            if v <= 8:
                return "good"
            return "needs_work"

        ratings = {
            "cadence": _rate_cadence(averages["cadence"]),
            "ground_contact_ms": _rate_gct(averages["ground_contact_ms"]),
            "vertical_osc_cm": _rate_vo(averages["vertical_osc_cm"]),
            "vertical_ratio_pct": _rate_vr(averages["vertical_ratio_pct"]),
        }

        # Insights
        insights: list[str] = []

        cad = averages.get("cadence")
        if cad is not None:
            if cad < 165:
                insights.append(
                    f"Cadence is low at {cad} spm — focus on quick, light steps. "
                    "A metronome or cadence cue during easy runs can help."
                )
            elif cad < 170:
                insights.append(
                    f"Cadence ({cad} spm) is below the 170 spm threshold — "
                    "some improvement possible."
                )
            elif cad >= 180:
                insights.append(f"Cadence is elite at {cad} spm.")

        gct = averages.get("ground_contact_ms")
        if gct is not None:
            if gct > 260:
                insights.append(
                    f"Ground contact time is high at {gct} ms — indicates heavy "
                    "footstrike or overstriding. Focus on landing under your hips."
                )
            elif gct < 200:
                insights.append(f"Ground contact time is excellent at {gct} ms.")

        vo = averages.get("vertical_osc_cm")
        if vo is not None:
            if vo > 10:
                insights.append(
                    f"Vertical oscillation is high at {vo} cm — you may be "
                    "bouncing more than needed. Aim for forward propulsion."
                )
            elif vo < 6:
                insights.append(f"Vertical oscillation is excellent at {vo} cm.")

        vr = averages.get("vertical_ratio_pct")
        if vr is not None:
            if vr > 10:
                insights.append(
                    f"Vertical ratio is high at {vr}% — "
                    "energy is being spent going up rather than forward."
                )
            elif vr < 6:
                insights.append(f"Vertical ratio is excellent at {vr}%.")

        # Trend-based insights
        for metric, label in [
            ("cadence", "Cadence"),
            ("ground_contact_ms", "Ground contact time"),
            ("vertical_osc_cm", "Vertical oscillation"),
            ("vertical_ratio_pct", "Vertical ratio"),
        ]:
            t = trends.get(metric, {})
            if t.get("direction") == "improving":
                insights.append(f"{label} is trending in the right direction.")
            elif t.get("direction") == "declining":
                insights.append(
                    f"{label} has been trending in the wrong direction recently — "
                    "worth monitoring."
                )

        if not insights:
            insights.append("Running form looks consistent — no major issues detected.")

        return {
            "per_activity": per_activity,
            "averages": averages,
            "trends": trends,
            "ratings": ratings,
            "insights": insights,
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def detect_personal_records(
    activity_id: Optional[int] = None,
    recent_n: int = 20,
) -> dict:
    """Scan cached run activities for personal bests at common distances.

    Checks whether any run sets a new PR at: 1 km, 1 mile (1609 m), 5 km,
    10 km, half marathon (21097 m), marathon (42195 m), and longest run ever.

    PR estimation uses average pace × target distance (no split columns in the
    schema). For the distance PR, the run's total distance_m is compared
    against all other cached runs.

    Args:
        activity_id: If given, check only this activity against historical
            bests from all other cached runs. Returns a `broken_in_activity`
            field with matching PR labels.
        recent_n: When activity_id is not given, scan the most recent N run
            activities. Default 20.

    Returns:
        any_pr (bool), records list [{distance_label, time_formatted,
        pace_per_km, date, activity_name, activity_id}],
        broken_in_activity (only when activity_id given, list of distance labels).
    """
    import sqlite3 as _sqlite3

    _DISTANCES = [
        ("1 km",         1_000.0),
        ("1 mile",       1_609.0),
        ("5 km",         5_000.0),
        ("10 km",       10_000.0),
        ("Half marathon", 21_097.0),
        ("Marathon",     42_195.0),
    ]
    # Minimum run distance to be eligible for a split-distance PR estimate.
    # A run must be at least 110% of the target distance to use avg-pace estimation.
    _COVERAGE_FACTOR = 1.10

    def _fmt_time(seconds: float) -> str:
        total = round(seconds)
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _fmt_pace(s_per_km: float) -> str:
        total = round(s_per_km)
        return f"{total // 60}:{total % 60:02d}/km"

    try:
        garmin_sync._init_db()
        with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row

            if activity_id is not None:
                # Fetch the specific activity plus all other cached runs for comparison.
                target_row = conn.execute(
                    "SELECT id, start_date_local, name, distance_m, moving_time_s "
                    "FROM activities WHERE id = ? AND sport_type = 'Run'",
                    (activity_id,),
                ).fetchone()
                if not target_row:
                    return {
                        "error": f"Activity {activity_id} not found in cache or is not a Run.",
                        "any_pr": False,
                        "records": [],
                        "broken_in_activity": [],
                    }
                candidate_rows = [target_row]
                # Historical rows are all OTHER runs (exclude the candidate itself).
                history_rows = conn.execute(
                    "SELECT id, start_date_local, name, distance_m, moving_time_s "
                    "FROM activities WHERE sport_type = 'Run' AND id != ? "
                    "ORDER BY start_date_local",
                    (activity_id,),
                ).fetchall()
                all_rows = list(history_rows) + list(candidate_rows)
            else:
                # Scan the most recent N runs.
                all_rows = conn.execute(
                    "SELECT id, start_date_local, name, distance_m, moving_time_s "
                    "FROM activities WHERE sport_type = 'Run' "
                    "ORDER BY start_date_local DESC LIMIT ?",
                    (recent_n,),
                ).fetchall()

        if not all_rows:
            return {"any_pr": False, "records": [], "note": "No cached run activities found."}

        # Build PR table: for each distance, track the all-time best (min time_s).
        # {distance_label: {"time_s": float, "date": str, "name": str, "id": int}}
        pr_table: dict[str, dict] = {}

        def _estimate_split_time(row, target_m: float) -> Optional[float]:
            dist = row["distance_m"] or 0
            moving = row["moving_time_s"] or 0
            if dist <= 0 or moving <= 0:
                return None
            if dist < target_m * _COVERAGE_FACTOR:
                return None
            s_per_m = moving / dist
            return s_per_m * target_m

        # Scan all rows to compute all-time bests.
        for row in all_rows:
            dist = row["distance_m"] or 0
            moving = row["moving_time_s"] or 0
            act_date = (row["start_date_local"] or "")[:10]
            act_name = row["name"] or ""
            act_id_val = row["id"]

            # Split-distance PRs.
            for label, target_m in _DISTANCES:
                est = _estimate_split_time(row, target_m)
                if est is None:
                    continue
                existing = pr_table.get(label)
                if existing is None or est < existing["time_s"]:
                    pr_table[label] = {
                        "time_s": est,
                        "date": act_date,
                        "activity_name": act_name,
                        "activity_id": act_id_val,
                    }

            # Longest run PR.
            if dist > 0:
                existing_dist = pr_table.get("Longest run")
                if existing_dist is None or dist > existing_dist["distance_m"]:
                    pr_table["Longest run"] = {
                        "distance_m": dist,
                        "time_s": moving if moving > 0 else None,
                        "date": act_date,
                        "activity_name": act_name,
                        "activity_id": act_id_val,
                    }

        # Build records list.
        records: list[dict] = []
        for label, target_m in _DISTANCES:
            best = pr_table.get(label)
            if best is None:
                continue
            t = best["time_s"]
            pace = t / (target_m / 1000)
            records.append({
                "distance_label": label,
                "time_formatted": _fmt_time(t),
                "pace_per_km": _fmt_pace(pace),
                "date": best["date"],
                "activity_name": best["activity_name"],
                "activity_id": best["activity_id"],
            })

        longest = pr_table.get("Longest run")
        if longest:
            dist_km = round((longest["distance_m"] or 0) / 1000, 2)
            t = longest.get("time_s")
            pace_str = _fmt_pace(t / (longest["distance_m"] / 1000)) if t and longest["distance_m"] else None
            records.append({
                "distance_label": "Longest run",
                "distance_km": dist_km,
                "time_formatted": _fmt_time(t) if t else None,
                "pace_per_km": pace_str,
                "date": longest["date"],
                "activity_name": longest["activity_name"],
                "activity_id": longest["activity_id"],
            })

        result: dict = {
            "any_pr": len(records) > 0,
            "records": records,
        }

        if activity_id is not None:
            # Determine which PRs were set by the target activity.
            broken: list[str] = []
            for rec in records:
                if rec.get("activity_id") == activity_id:
                    broken.append(rec["distance_label"])
            result["broken_in_activity"] = broken
            result["any_pr"] = len(broken) > 0

        return result

    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "any_pr": False,
            "records": [],
        }


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


@mcp.tool()
def illness_risk_check() -> dict:
    """Scan today's wellness signals for early illness onset.

    Combines five independent signals — HRV drop, RHR spike, low sleep
    score, short sleep, and high stress — into a single risk rating.

    Returns:
    - `risk_level`: 'low' (0-1 flags), 'moderate' (2 flags), or
      'high' (3+ flags — multiple illness signals present).
    - `flagged_signals`: list of signal names that crossed the threshold.
    - `recommendation`: plain-language action string.
    - `raw_values`: dict with today's values, 7-day means, and thresholds
      used. Nulls indicate the device wasn't worn or the field isn't
      available for the current device.

    Flag thresholds (all evidence-based heuristics):
    - HRV: today >15% below 7-day mean.
    - RHR: today >5 bpm above 7-day mean.
    - Sleep score: today >10 points below 7-day mean.
    - Sleep duration: <6 hours (<21 600 s).
    - Avg stress: today >60 (moderate-high on Garmin's 0-100 scale).

    Wellness data is read from the local cache (wellness_daily table) after
    syncing the last 7 days; a short Garmin API call is made for today's
    data if it isn't cached.
    """
    import math as _math
    from datetime import date as _date, timedelta as _td

    try:
        today = _date.today()
        seven_days_ago = today - _td(days=7)

        # Sync the last 7 days into cache (fast — most days will be cached).
        garmin_sync.sync_wellness_range(
            _client(), seven_days_ago.isoformat(), today.isoformat()
        )

        # Read history (7 prior days, excluding today) for baseline means.
        history_start = seven_days_ago.isoformat()
        history_end = (today - _td(days=1)).isoformat()
        hist_data = garmin_sync.wellness_history(history_start, history_end)
        history_daily = hist_data.get("daily", [])

        # Today's metrics — read from cache after the sync above.
        today_row = garmin_sync._wellness_day_cached(today.isoformat())
        if today_row is None:
            # Fallback: fetch live if cache miss (shouldn't happen after sync).
            today_row = garmin_sync._fetch_wellness_day(_client(), today.isoformat())

        # ── Helper: arithmetic mean over a field across history rows ──────
        def _mean(field: str) -> Optional[float]:
            vals = [r[field] for r in history_daily if r.get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        # ── Collect today's values and 7-day means ─────────────────────────
        today_hrv = today_row.get("hrv_overnight_avg") if today_row else None
        today_rhr = today_row.get("resting_hr") if today_row else None
        today_sleep_score = today_row.get("sleep_score") if today_row else None
        today_sleep_s = today_row.get("sleep_seconds") if today_row else None
        today_stress = today_row.get("avg_stress") if today_row else None

        mean_hrv = _mean("hrv_overnight_avg")
        mean_rhr = _mean("resting_hr")
        mean_sleep_score = _mean("sleep_score")

        # ── Check each signal ──────────────────────────────────────────────
        flagged: list[str] = []

        # HRV: flag if today is >15% below 7-day mean
        hrv_flag = None
        if today_hrv is not None and mean_hrv is not None and mean_hrv > 0:
            hrv_drop_pct = (mean_hrv - today_hrv) / mean_hrv * 100
            if hrv_drop_pct > 15:
                flagged.append("hrv_low")
            hrv_flag = round(hrv_drop_pct, 1)

        # RHR: flag if today is >5 bpm above 7-day mean
        rhr_flag = None
        if today_rhr is not None and mean_rhr is not None:
            rhr_rise = today_rhr - mean_rhr
            if rhr_rise > 5:
                flagged.append("rhr_elevated")
            rhr_flag = round(rhr_rise, 1)

        # Sleep score: flag if >10 points below 7-day mean
        sleep_score_flag = None
        if today_sleep_score is not None and mean_sleep_score is not None:
            sleep_score_drop = mean_sleep_score - today_sleep_score
            if sleep_score_drop > 10:
                flagged.append("sleep_score_low")
            sleep_score_flag = round(sleep_score_drop, 1)

        # Sleep duration: flag if <6 hours (21600 seconds)
        if today_sleep_s is not None and today_sleep_s < 21_600:
            flagged.append("sleep_short")

        # Stress: flag if avg_stress > 60
        if today_stress is not None and today_stress > 60:
            flagged.append("stress_high")

        # ── Determine risk level ───────────────────────────────────────────
        n = len(flagged)
        if n >= 3:
            risk_level = "high"
            recommendation = "Rest day recommended — multiple illness signals present"
        elif n == 2:
            risk_level = "moderate"
            recommendation = "Consider reducing intensity"
        else:
            risk_level = "low"
            recommendation = "Train as planned"

        return {
            "date": today.isoformat(),
            "risk_level": risk_level,
            "flagged_signals": flagged,
            "flag_count": n,
            "recommendation": recommendation,
            "raw_values": {
                "hrv_today": today_hrv,
                "hrv_7d_mean": mean_hrv,
                "hrv_drop_pct": hrv_flag,
                "hrv_threshold_pct": 15,
                "rhr_today": today_rhr,
                "rhr_7d_mean": mean_rhr,
                "rhr_rise_bpm": rhr_flag,
                "rhr_threshold_bpm": 5,
                "sleep_score_today": today_sleep_score,
                "sleep_score_7d_mean": mean_sleep_score,
                "sleep_score_drop": sleep_score_flag,
                "sleep_score_threshold": 10,
                "sleep_seconds_today": today_sleep_s,
                "sleep_hours_today": round(today_sleep_s / 3600, 1) if today_sleep_s is not None else None,
                "sleep_short_threshold_hours": 6,
                "avg_stress_today": today_stress,
                "avg_stress_threshold": 60,
                "history_days": len(history_daily),
            },
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


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


# ─── Taper planner ────────────────────────────────────────────────────
@mcp.tool()
def taper_plan(
    race_date: str,
    race_distance_km: float,
    current_weekly_km: Optional[float] = None,
) -> dict:
    """Generate a race taper schedule from today to race day.

    Applies standard percentage-based taper reductions:
    - Marathon (>35 km):        3-week taper — Week -3: 80%, Week -2: 60%, Week -1: 40%
    - Half/10k (10–35 km):      2-week taper — Week -2: 70%, Week -1: 40%
    - 5k and shorter (<10 km):  1-week taper — Week -1: 60%

    Args:
        race_date: Race day in 'YYYY-MM-DD' format.
        race_distance_km: Race distance in km (e.g. 10, 21.1, 42.2).
        current_weekly_km: Your current weekly volume baseline. If omitted,
            estimated from the last 4 weeks in the activity cache (run km
            only). Pass explicitly when you want to override the estimate.

    Returns:
        - `taper_weeks`: ordered list of weekly targets from taper start
          to race week. Each entry has `week_start`, `week_end`,
          `target_km`, `sessions`, `key_sessions`, and `notes`.
        - `race_week`: {date, distance_km, advice} for race day itself.
        - `base_weekly_km`: the baseline volume used for calculations.
        - `base_source`: 'provided' | 'cache_estimate' | 'default'.
        - `warning`: set when the race is fewer than 7 days away (full
          taper is not possible).
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta as _td

    try:
        today = _date.today()
        race = _date.fromisoformat(race_date)
    except ValueError as e:
        return {"error": f"Invalid race_date: {e}"}

    days_until_race = (race - today).days
    warning: Optional[str] = None
    if days_until_race < 7:
        warning = (
            f"Race is only {days_until_race} day(s) away — a full taper is not possible. "
            "Focus on rest, short easy runs at most, and race-day logistics."
        )

    # ── Determine base volume ─────────────────────────────────────────
    base_source: str
    if current_weekly_km is not None:
        base_km = float(current_weekly_km)
        base_source = "provided"
    else:
        # Estimate from last 4 weeks of run activities in cache.
        try:
            four_weeks_ago = (today - _td(weeks=4)).isoformat()
            with _sqlite3.connect(garmin_sync.DB_PATH) as _conn:
                rows = _conn.execute(
                    """
                    SELECT start_date_local, distance_m
                    FROM activities
                    WHERE start_date_local >= ?
                      AND sport_type = 'Run'
                      AND distance_m IS NOT NULL
                    ORDER BY start_date_local
                    """,
                    (four_weeks_ago,),
                ).fetchall()

            if rows:
                total_m = sum(r[1] for r in rows)
                base_km = round(total_m / 4000, 1)  # 4 weeks, m → km
                base_source = "cache_estimate"
            else:
                base_km = 40.0  # conservative default
                base_source = "default"
        except Exception:
            base_km = 40.0
            base_source = "default"

    # ── Choose taper schedule ─────────────────────────────────────────
    # Each entry: (week_offset_before_race, pct, sessions, key_session_note, notes)
    if race_distance_km > 35:
        # Marathon: 3-week taper
        schedule = [
            (3, 0.80, 5, "One sub-threshold session (shorter reps), one medium long run (13-15 km)",
             "Reduce long run to ~13-15 km. Keep one quality sub-threshold session at normal pace but fewer reps. Eliminate second quality session."),
            (2, 0.60, 4, "One short quality session (20-25 min of reps), long run ≤12 km",
             "Drop long run to 12 km or less. Quality session: short reps only (e.g. 4-5 × 4 min). No tempo runs."),
            (1, 0.40, 3, "2-3 short easy runs + 1 × 10-15 min strides session",
             "Race week — all easy. Optional strides mid-week to keep legs sharp. No quality work after Thursday."),
        ]
    elif race_distance_km >= 10:
        # Half marathon / 10k: 2-week taper
        schedule = [
            (2, 0.70, 4, "One quality session (sub-threshold, 60-70% of normal rep volume)",
             "Reduce overall volume but maintain one quality session. Long run shortened by ~30%."),
            (1, 0.40, 3, "2 easy runs + optional strides mid-week",
             "Race week — mostly easy. Short strides 2 days before race if feeling flat. No hard efforts after Wednesday."),
        ]
    else:
        # 5k and shorter: 1-week taper
        schedule = [
            (1, 0.60, 3, "1-2 easy runs + short strides 2 days before race",
             "Race week — keep legs fresh. A single short quality session early in the week is optional. Race-pace strides are enough stimulus."),
        ]

    # ── Build week entries ────────────────────────────────────────────
    def _monday(d: _date) -> _date:
        return d - _td(days=d.weekday())

    race_monday = _monday(race)
    taper_weeks = []

    for week_offset, pct, sessions, key_sessions, notes in schedule:
        week_start = race_monday - _td(weeks=week_offset)
        week_end = week_start + _td(days=6)
        target_km = round(base_km * pct, 1)
        taper_weeks.append({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "target_km": target_km,
            "volume_pct": int(pct * 100),
            "sessions": sessions,
            "key_sessions": key_sessions,
            "notes": notes,
        })

    race_advice = (
        "Rest and hydrate. Short 10-15 min shakeout run the day before is optional. "
        "Trust the taper — fitness is locked in. Focus on pacing strategy and race-day logistics."
    )

    result: dict = {
        "race_date": race_date,
        "race_distance_km": race_distance_km,
        "days_until_race": days_until_race,
        "base_weekly_km": base_km,
        "base_source": base_source,
        "taper_weeks": taper_weeks,
        "race_week": {
            "date": race_date,
            "distance_km": race_distance_km,
            "advice": race_advice,
        },
    }
    if warning:
        result["warning"] = warning
    return result


@mcp.tool()
def return_from_break() -> dict:
    """Detect a running inactivity gap and generate a conservative ramp-back plan.

    Queries the local activity cache to find the most recent run and computes
    how many days have elapsed since it. If fewer than 7 days have passed,
    returns an "active" status. Otherwise generates a week-by-week return plan
    scaled to the break length.

    Break length → plan:
    - 1-2 weeks off: 2-week ramp, start at 60% of pre-break volume, +20%/week
    - 2-4 weeks off: 3-week ramp, start at 50%, +20%/week
    - 4-8 weeks off: 4-week ramp, start at 40%, +15%/week
    - 8+ weeks off: 5-week ramp, start at 30%, +15%/week (includes clearance note)

    Pre-break volume is the average weekly km over the 4 weeks immediately
    before the break.

    Each week in the return plan includes:
    - target_km: total weekly volume target
    - max_single_run_km: longest single run allowed (40% of weekly)
    - session_count: recommended number of sessions (3-4)
    - notes: coaching notes including quality-session restrictions

    Returns:
    - status: 'active' | 'break_detected'
    - break_days: days since last run
    - pre_break_weekly_km: estimated pre-break weekly volume
    - return_weeks: list of week plans
    - first_quality_session_week: earliest week number where quality is allowed
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, datetime as _datetime

    try:
        from garmin_sync import DB_PATH as _DB_PATH
        today = _date.today()
        with _sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                """
                SELECT start_date_local, distance_m
                FROM activities
                WHERE sport_type = 'Run' AND distance_m IS NOT NULL
                ORDER BY start_date_local DESC
                LIMIT 1
                """
            ).fetchone()

            if not row:
                return {
                    "status": "no_data",
                    "message": "No runs found in the local cache. Run sync_activities() first.",
                }

            last_run_date_str = row[0][:10]
            last_run_date = _date.fromisoformat(last_run_date_str)
            break_days = (today - last_run_date).days

            if break_days < 7:
                return {
                    "status": "active",
                    "message": f"No significant break detected. Last run was {break_days} day(s) ago on {last_run_date_str}.",
                    "break_days": break_days,
                    "last_run_date": last_run_date_str,
                }

            # Pre-break volume: 4 weeks before the last run
            pre_break_end = last_run_date_str
            from datetime import timedelta as _td
            pre_break_start = (last_run_date - _td(weeks=4)).isoformat()

            weekly_rows = conn.execute(
                """
                SELECT
                    strftime('%Y-%W', start_date_local) AS iso_week,
                    SUM(distance_m) / 1000.0 AS weekly_km
                FROM activities
                WHERE sport_type = 'Run'
                    AND distance_m IS NOT NULL
                    AND date(start_date_local) BETWEEN ? AND ?
                GROUP BY iso_week
                ORDER BY iso_week
                """,
                (pre_break_start, pre_break_end),
            ).fetchall()

        if weekly_rows:
            total_km = sum(r[1] for r in weekly_rows)
            pre_break_weekly_km = round(total_km / len(weekly_rows), 1)
        else:
            # Fallback: use the last known run and estimate 20 km/week
            pre_break_weekly_km = 20.0

        # Determine plan parameters based on break length
        break_weeks = break_days / 7
        if break_weeks < 2:
            plan_weeks = 2
            start_pct = 0.60
            step_pct = 0.20
            medical_note = None
        elif break_weeks < 4:
            plan_weeks = 3
            start_pct = 0.50
            step_pct = 0.20
            medical_note = None
        elif break_weeks < 8:
            plan_weeks = 4
            start_pct = 0.40
            step_pct = 0.15
            medical_note = None
        else:
            plan_weeks = 5
            start_pct = 0.30
            step_pct = 0.15
            medical_note = "Consider medical clearance before resuming training after an 8+ week break."

        # Build the weekly return plan
        return_weeks = []
        for week_num in range(1, plan_weeks + 1):
            pct = start_pct + step_pct * (week_num - 1)
            target_km = round(pre_break_weekly_km * pct, 1)
            max_single_km = round(target_km * 0.40, 1)

            # Session count: 3 in week 1, 4 from week 2 onward
            session_count = 3 if week_num == 1 else 4

            # Quality session policy
            if week_num == 1:
                notes = (
                    "Easy running only — no quality sessions. "
                    "All runs at conversational/Z1-Z2 effort. "
                    "Stop if any pain or unusual fatigue."
                )
            elif week_num == 2:
                notes = (
                    "Continue building aerobic base. "
                    "Optional: one gentle fartlek with 4-6 short pickups (30s each) "
                    "only if week 1 felt effortless."
                )
            else:
                notes = (
                    f"Week {week_num}: quality sessions allowed. "
                    "Introduce one sub-threshold session (e.g. 3×8 min at sub-threshold pace). "
                    "Keep remaining sessions easy."
                )

            week_entry: dict = {
                "week": week_num,
                "target_km": target_km,
                "volume_pct_of_prebreak": round(pct * 100),
                "max_single_run_km": max_single_km,
                "session_count": session_count,
                "notes": notes,
            }
            if medical_note and week_num == 1:
                week_entry["medical_note"] = medical_note
            return_weeks.append(week_entry)

        result: dict = {
            "status": "break_detected",
            "break_days": break_days,
            "last_run_date": last_run_date_str,
            "pre_break_weekly_km": pre_break_weekly_km,
            "return_weeks": return_weeks,
            "first_quality_session_week": 3,
            "plan_note": (
                f"{break_days}-day break ({break_days // 7} week(s)). "
                f"Pre-break base: {pre_break_weekly_km} km/week. "
                f"{plan_weeks}-week return ramp starting at "
                f"{round(start_pct * 100)}% of base volume."
            ),
        }
        if medical_note:
            result["medical_note"] = medical_note

        return result

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


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
