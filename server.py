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

import strava_sync  # noqa: E402  (must load after dotenv so token refresh works)
import plan as plan_mod  # noqa: E402

mcp = FastMCP("garmin-coach")
_COACH_DATA = Path(__file__).parent / "coach_data"


# ─── Resources (markdown context for the coaching agent) ───────────────
@mcp.resource("coach://classification")
def classification_rules() -> str:
    """How to classify Strava activities (easy / threshold / VO2 / long / race).

    Read this before summarizing a week of training or analyzing a session.
    """
    return (_COACH_DATA / "classification.md").read_text(encoding="utf-8")


_USER_PROFILE_PATH = _COACH_DATA / "user_profile.md"
_USER_PROFILE_EXAMPLE_PATH = _COACH_DATA / "examples" / "user_profile.example.md"


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
        "classification": _COACH_DATA / "classification.md",
        "plan_design": _COACH_DATA / "plan_design.md",
    }
    return paths[name].read_text(encoding="utf-8")


# ─── First-time profile setup ──────────────────────────────────────────
_PROFILE_SETUP_QUESTIONS = [
    {
        "field": "max_hr",
        "required": True,
        "question": (
            "What's your max heart rate? If you've measured it (a maximum-effort 5k, "
            "hill repeats, or a lab test), use that value. '220 − age' is a starting "
            "estimate but typically underestimates well-trained runners."
        ),
    },
    {
        "field": "zone_ceilings",
        "required": False,
        "question": (
            "I'll use the Olympiatoppen 5-zone system by default — Z1-Z4 ceilings "
            "computed from 72/82/87/92% of your max HR. (Note: Garmin's own defaults "
            "are different; if you've set custom Olympiatoppen zones in Garmin "
            "Connect, give me those bpm values verbatim so the in-watch zones match "
            "what this MCP uses. Otherwise I'll compute defaults and you can set "
            "Garmin to match later.)"
        ),
    },
    {
        "field": "lt2_hr",
        "required": False,
        "question": (
            "Have you had a lactate / VO2max test? If yes, what was your LT2 HR "
            "(classical threshold, ~4 mmol)? The HR at the highest sustainable "
            "steady-state effort."
        ),
    },
    {
        "field": "lt1_hr",
        "required": False,
        "question": (
            "From the same test, what was your LT1 HR (aerobic threshold, ~2 mmol)? "
            "The boundary between truly easy and aerobic-moderate. Important because "
            "it becomes your hard cap on easy runs in the Bakken framework."
        ),
    },
    {
        "field": "vo2max",
        "required": False,
        "question": (
            "What's your VO2max (ml/min/kg) from the test? Optional context — useful "
            "for reasoning about whether VO2 work or threshold work is your bigger "
            "lever."
        ),
    },
    {
        "field": "weight_kg",
        "required": False,
        "question": "Body weight in kg? Optional, for VO2max L/min context.",
    },
    {
        "field": "race_prs",
        "required": False,
        "question": (
            "What are your current PRs for 5k, 10k, half marathon, marathon? "
            "Anything you haven't raced, leave out. Times like '23:08' or '1:45:30'. "
            "PRs are a better fitness anchor than treadmill test pace numbers."
        ),
    },
    {
        "field": "notes",
        "required": False,
        "question": (
            "Any context worth recording at the bottom of the profile? Recent injuries, "
            "planned races, training history, current limitations, etc."
        ),
    },
]


@mcp.tool()
def user_profile_status() -> dict:
    """Check whether user_profile.md exists and is filled in.

    Returns existence flag, file path, whether the file still has placeholder
    values from the example template, AND a structured list of
    `suggested_questions` to walk the user through if setup is needed.

    Call this at the start of a fresh session or whenever you suspect the
    profile isn't set up. Each `suggested_questions` entry has a `field`
    (matching an `init_user_profile` parameter), `required` flag, and
    `question` text to ask the user.

    After collecting answers, call `init_user_profile()` with whatever the
    user provided.
    """
    if not _USER_PROFILE_PATH.exists():
        return {
            "exists": False,
            "path": str(_USER_PROFILE_PATH),
            "suggested_questions": _PROFILE_SETUP_QUESTIONS,
            "next_step": (
                "Walk the user through the questions in suggested_questions in order. "
                "max_hr is the only required field; for the rest, accept whatever the "
                "user provides or knows. Then call init_user_profile() with the answers."
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
        result["suggested_questions"] = _PROFILE_SETUP_QUESTIONS
        result["next_step"] = (
            f"Profile exists but still has template placeholders: {found}. Walk through "
            "suggested_questions to collect real values, then call "
            "init_user_profile(overwrite=True)."
        )
    else:
        result["next_step"] = "Profile looks filled in — no setup action needed."
    return result


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
            above Z4). Copy from Garmin Connect → Settings → HR Zones if
            you can. If omitted, computed from 72/82/87/92% of max_hr.
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
    these values override whatever Strava has cached.
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


def _client() -> Garmin:
    """Lazy singleton — log in on first tool call, reuse across calls."""
    global _garmin
    if _garmin is None:
        email = os.environ.get("GARMIN_EMAIL")
        password = os.environ.get("GARMIN_PASSWORD")
        if not email or not password:
            raise RuntimeError("Set GARMIN_EMAIL and GARMIN_PASSWORD in .env")
        g = Garmin(email, password)
        g.login()
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


# ─── Strava sync + weekly summary (reads local cache) ──────────────────
@mcp.tool()
def sync_activities(force_full: bool = False, weeks_back: Optional[int] = None) -> dict:
    """Pull new activities + HR streams from Strava into the local cache.

    Runs incrementally since the last sync. First-time sync backfills the
    last 12 weeks of activities; subsequent runs are cheap and just fetch
    what's new.

    Args:
        force_full: If True, re-pull the default 12-week backfill window.
            Default False — just pick up new activities since last sync.
        weeks_back: Optional explicit backfill window (e.g. 26 or 52) to
            pull deeper history than the 12-week default. Use when the
            agent needs year-long trajectory data (`weekly_summary` will
            return `gap_warning=True` when the requested range is older
            than what's cached).

    Returns dict with new_activities count, streams_fetched count,
    last_sync timestamp, and any per-activity errors encountered.
    """
    return strava_sync.run_sync(force_full=force_full, weeks_back=weeks_back)


@mcp.tool()
def weekly_summary(start_date: str, end_date: str) -> dict:
    """Per-week training summary from the local Strava cache.

    Returns `{"weeks": [...], "coverage": {...}}`. Each week entry covers
    one Monday-Sunday week and contains total distance, run count, time
    in each HR zone (computed from raw streams using current bpm
    boundaries from coach://user_profile), and the list of activities
    with names, descriptions, distance, HR, and a `classification_hint`
    derived from naming patterns.

    The `coverage` field reports cache extent and a `gap_warning` flag
    when the requested range extends before the oldest cached activity —
    use it to distinguish "no runs that week" from "we don't have data
    that far back" (the local cache holds 12 weeks by default; call
    `sync_activities(weeks_back=N)` to extend it).

    Args:
        start_date: 'YYYY-MM-DD' (inclusive)
        end_date:   'YYYY-MM-DD' (inclusive)
    """
    return strava_sync.weekly_summary(start_date, end_date)


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
    Strava (via the local cache). Medium strictness: type must match
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
def get_gear_for_activity(activity_id: int) -> dict:
    """Return the gear (e.g. shoe) used for a specific activity.

    Useful for reasoning about shoe wear or rotation patterns ("which
    shoes were on yesterday's run?", "which shoe has the most threshold
    work on it?").

    NOTE: takes a **Garmin** activity_id, not the Strava ID used elsewhere
    in this MCP. Find it in the Garmin Connect URL
    (`connect.garmin.com/modern/activity/<id>`) or via python-garminconnect's
    `get_activities()`. Passing a Strava ID will 403.
    """
    return _client().get_activity_gear(activity_id)


# ─── Drill-in / recovery / retrospective ──────────────────────────────
@mcp.tool()
def activity_breakdown(activity_id: int) -> dict:
    """Detailed breakdown of a single cached activity.

    Returns name, description, distance, moving time, avg/max HR,
    classification hint, and time in each HR zone (Z1-Z5) as both seconds
    and percentages. Useful for drilling into a specific session ("how did
    Tuesday's threshold go?") without pulling the full weekly summary.

    If the activity isn't in the cache, run `sync_activities` first.
    """
    return strava_sync.activity_breakdown(activity_id)


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
        hrv_baseline_low/upper, sleep_seconds, avg_stress, body_battery_*}
      - rolling: list of {date, rhr_7d_mean, hrv_7d_geomean}
      - summary: min/max/mean for RHR and HRV across the range, plus the
        most recent Garmin "balanced HRV" baseline band for context
    """
    sync_result = strava_sync.sync_wellness_range(
        _client(), start_date, end_date, force_refetch=force_refetch
    )
    data = strava_sync.wellness_history(start_date, end_date)
    data["sync"] = sync_result
    return data


@mcp.tool()
def morning_check_in() -> dict:
    """Today's recovery / readiness metrics from Garmin.

    Pulls training readiness, sleep (last night), HRV, body battery,
    training status, and resting HR. Each endpoint is fetched
    independently — partial results are returned with per-field errors if
    individual calls fail.

    Use to decide whether to do a planned quality session today or shift
    it (e.g., low training readiness → defer threshold to tomorrow).
    """
    from datetime import date as _date, timedelta as _td
    g = _client()
    today = _date.today().isoformat()
    yesterday = (_date.today() - _td(days=1)).isoformat()

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    return {
        "date": today,
        "training_readiness": safe(g.get_training_readiness, today),
        "morning_readiness": safe(g.get_morning_training_readiness, today),
        "training_status": safe(g.get_training_status, today),
        "sleep_last_night": safe(g.get_sleep_data, yesterday),
        "hrv": safe(g.get_hrv_data, today),
        "body_battery": safe(g.get_body_battery, today),
        "resting_hr": safe(g.get_rhr_day, today),
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
    result = strava_sync.weekly_summary(start.isoformat(), end.isoformat())
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
        result = strava_sync.run_sync()
        if result.get("new_activities") or result.get("errors"):
            print(
                f"[startup-sync] {result.get('new_activities', 0)} new, "
                f"{result.get('streams_fetched', 0)} streams, "
                f"{len(result.get('errors', []))} errors",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[startup-sync] failed: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    threading.Thread(target=_startup_sync, daemon=True).start()
    mcp.run()
