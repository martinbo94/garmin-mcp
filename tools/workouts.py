"""Workout templates: connection test, read, create, delete."""
from datetime import date
from typing import Optional

import garmin_sync
from core import EndCondition, IntervalSet, Step, _client, mcp


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


def _register_created_workout(workout_id: int, name: str) -> None:
    """Record an MCP-created workout id in the local registry.

    Garmin's API exposes nothing that distinguishes templates created
    programmatically from ones the user made by hand in the app, so we
    keep our own ledger. cleanup_workout_templates only ever deletes
    ids found here — the user's hand-made templates (e.g. generic
    "5x5") are structurally protected.
    """
    import sqlite3

    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS created_workouts ("
            "workout_id INTEGER PRIMARY KEY, name TEXT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO created_workouts (workout_id, name) VALUES (?, ?)",
            (workout_id, name),
        )


def _upload(workout: dict) -> int:
    result = _client().upload_workout(workout)
    workout_id = result.get("workoutId") if isinstance(result, dict) else None
    if not workout_id:
        raise RuntimeError(f"Upload returned no workout_id: {result!r}")
    _register_created_workout(int(workout_id), workout.get("workoutName") or "")
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




@mcp.tool()
def delete_workout_template(workout_id: int) -> str:
    """Delete a workout template entirely (also unschedules it from all dates)."""
    _client().delete_workout(workout_id)
    return f"Deleted workout {workout_id}."


@mcp.tool()
def cleanup_workout_templates(dry_run: bool = True) -> dict:
    """Delete Garmin Connect workout templates that no FUTURE scheduled
    workout references — completed sessions' templates, old blocks,
    one-offs.

    Why this matters: the watch only holds ~25 structured workouts. Stale
    templates crowd out upcoming sessions, so today's scheduled økt can
    silently fail to appear on the watch. Run this as part of the weekly
    review to keep the Connect list lean.

    ⚠ PROCESS: ALWAYS run with dry_run=True first, show the user the
    itemized `would_delete` list (names, grouped), and only re-run with
    dry_run=False after they have confirmed the actual items — approval
    of a count is not approval of the contents.

    Safety:
    - Anything scheduled from today onward is never touched.
    - Only templates in the local `created_workouts` registry (ids this
      MCP created itself) are ever deleted — Garmin's API cannot
      distinguish hand-made from programmatic templates, so the user's
      own templates (e.g. generic "5x5") come back as
      `unmanaged_skipped` and are left alone.
    - Completed *activities* are separate objects and are unaffected.
    Note: deletion in Connect does NOT clean the on-watch Treningsøkter
    list — orphaned entries on the device persist until a Garmin
    Express/USB sync or manual deletion on the watch.

    Args:
        dry_run: True (default) = report only; False = actually delete.

    Returns: total, keeping, `unmanaged_skipped`, and
    `would_delete`/`deleted` + `failed` lists of {workout_id, name,
    created}.
    """
    from datetime import date as _date, timedelta as _td

    from tools.scheduling import list_scheduled_workouts as _lsw

    g = _client()
    all_w: list[dict] = []
    start = 0
    while True:
        batch = g.get_workouts(start, 100) or []
        all_w.extend(batch)
        if len(batch) < 100:
            break
        start += 100

    today = _date.today()
    horizon = today + _td(days=730)
    future = _lsw(today.isoformat(), horizon.isoformat())
    fut_ids = {w["workout_id"] for w in future}

    # Belt and braces: also protect every template id plan.json holds for
    # today-or-future dates. If the calendar API under-reports (partial
    # month response), this keeps live plan templates out of the delete set.
    try:
        import plan as _plan_mod

        _p = _plan_mod.load_plan() or {}
        fut_ids |= {
            w["garmin_workout_id"] for w in _p.get("workouts", [])
            if w.get("garmin_workout_id") and w.get("date", "") >= today.isoformat()
        }
    except Exception:
        pass

    import sqlite3 as _sqlite3

    with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
        try:
            registry = {
                r[0] for r in conn.execute("SELECT workout_id FROM created_workouts")
            }
        except _sqlite3.OperationalError:
            registry = set()

    seen: set = set()
    candidates = [
        {
            "workout_id": w["workoutId"],
            "name": w.get("workoutName"),
            "created": (w.get("createdDate") or "")[:10],
        }
        for w in all_w
        if w["workoutId"] not in fut_ids
        and not (w["workoutId"] in seen or seen.add(w["workoutId"]))
    ]
    stale = [c for c in candidates if c["workout_id"] in registry]
    unmanaged = [c for c in candidates if c["workout_id"] not in registry]

    result: dict = {
        "total_templates": len(all_w),
        "keeping_future_scheduled": len(all_w) - len(candidates),
        "unmanaged_skipped": unmanaged,
        "dry_run": dry_run,
    }
    if dry_run:
        result["would_delete"] = stale
        result["next_step"] = (
            "Show the user this list grouped by `created`; re-run with "
            "dry_run=False only after they confirm the items."
        )
        return result

    deleted, failed = [], []
    for w in stale:
        try:
            g.delete_workout(w["workout_id"])
            deleted.append(w)
        except Exception as e:
            failed.append({**w, "error": f"{type(e).__name__}: {e}"})
    result["deleted"] = deleted
    result["failed"] = failed
    result["watch_note"] = (
        "Connect is clean, but the watch's own Treningsøkter list keeps "
        "orphaned entries until a Garmin Express/USB sync or manual "
        "on-device deletion."
    )
    return result

