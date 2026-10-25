"""Strength workouts: exercise catalog search, create/update structured
strength workouts, and the sets Garmin recorded for a strength activity."""
from typing import Optional

import strength
from core import StrengthBlock, _client, mcp


@mcp.tool()
def list_strength_exercises(query: str, category: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Search Garmin's strength exercise catalog (~1,500 exercises in ~47
    categories, English names) for the keys a strength workout step needs.

    Search with English words: "bulgarian split squat", "side plank",
    "face pull", "calf raise". Results are ranked by how many of the words
    appear in the key, display name or category (prefixes count, so
    "pull" finds "pull up"), then by how close the display name is. Pass
    `category` (e.g. "PLANK") to stay inside one category.
    The catalog is cached locally and refreshed monthly.

    Returns [{category, exercise_name, display_name, primary_muscles}],
    best match first. Use category + exercise_name in
    create_strength_workout. Nothing close? Use the nearest category, leave
    exercise_name out and name the real exercise in the step's note.
    """
    return strength.search_exercises(query, category=category, limit=limit)


@mcp.tool()
def create_strength_workout(
    name: str,
    blocks: list[StrengthBlock],
    description: Optional[str] = None,
    schedule_date: Optional[str] = None,
    side_labels: tuple[str, str] = ("right", "left"),
) -> dict:
    """Create a structured strength workout in Garmin Connect that the
    watch can run step by step (exercise, reps or time, rest, rounds).

    blocks run in order. Each block is a `single` exercise, a `superset`
    (two exercises back to back) or a `circuit` (three or more), done for
    `rounds` rounds:
    - each exercise: category + exercise_name (from
      list_strength_exercises; unknown keys are refused with suggestions),
      and exactly one of `reps` (a number, or a range like "6-10": the
      watch counts to the top and the step note shows the range),
      `duration_s`, or `until_lap`. Optional `weight_kg` and `note`.
    - `per_side: true` makes two steps per round, labelled with
      side_labels (default right/left), each with the full reps/duration.
    - `rest_between_exercises_s` inside a round, `rest_after_round_s` after
      each round (seconds, or "lap" for an open rest you end with the lap
      key). `kind: "warmup"` labels a block as warm-up on the watch.

    The workout is remembered by name (strength_templates) so a plan can
    reuse it every week: put {"type": "strength", "strength_template":
    "<name>"} in plan.json and materialize_plan schedules this workout_id
    instead of creating a new one. These templates are not touched by
    cleanup_workout_templates. A name already in use is refused — use
    update_strength_workout to change that workout instead.

    Args:
        name: Workout name (shown on the watch), unique among strength templates.
        blocks: The workout, in order.
        description: Workout-level note.
        schedule_date: Optional 'YYYY-MM-DD' to put it on the calendar.
        side_labels: Labels for per_side steps.

    Returns {workout_id, name, steps (one line per block, as built),
    scheduled}.
    """
    existing = strength.template_id(name)
    g = _client()
    if existing:
        try:
            g.get_workout_by_id(existing)
            raise ValueError(f"A strength template named {name!r} already exists (workout_id "
                             f"{existing}). Use update_strength_workout to change it.")
        except ValueError:
            raise
        except Exception:
            pass  # deleted in Connect — the name is free again
    steps = strength.build_steps(blocks, side_labels)
    created = g.upload_workout(strength.workout_payload(name, steps, description))
    wid = created.get("workoutId") if isinstance(created, dict) else None
    if not wid:
        raise RuntimeError(f"Garmin did not return a workoutId: {created!r}")
    strength.remember_template(name, int(wid))
    result = {"workout_id": int(wid), "name": name, "steps": strength.summarize_steps(steps),
              "scheduled": None}
    if schedule_date:
        from tools.scheduling import schedule_workout

        result["scheduled"] = schedule_workout(int(wid), schedule_date)
    return result


@mcp.tool()
def update_strength_workout(
    workout_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    blocks: Optional[list[StrengthBlock]] = None,
    side_labels: tuple[str, str] = ("right", "left"),
) -> dict:
    """Change a strength workout IN PLACE — same workout_id, so scheduled
    dates and plan references keep pointing at it.

    Pass `blocks` (same shape as create_strength_workout) to replace all
    steps; omit it to keep the steps and change only name/description.
    Renaming also renames the strength template a plan refers to — update
    plan.json's strength_template to match.

    Returns {workout_id, name, steps_replaced, steps, scheduled_dates}.
    The watch picks up the change on its next Connect sync.
    """
    g = _client()
    existing = g.get_workout_by_id(workout_id)
    if not isinstance(existing, dict) or not existing.get("workoutId"):
        raise RuntimeError(f"Workout {workout_id} not found: {existing!r}")
    if (existing.get("sportType") or {}).get("sportTypeKey") != "strength_training":
        raise ValueError(f"Workout {workout_id} is not a strength workout — use update_workout.")

    payload = dict(existing)
    if name is not None:
        payload["workoutName"] = name
    if description is not None:
        payload["description"] = description
    if blocks is not None:
        steps = strength.build_steps(blocks, side_labels)
        payload["workoutSegments"] = [{"segmentOrder": 1, "sportType": strength.SPORT,
                                       "workoutSteps": steps}]
    g.client.put("connectapi", f"/workout-service/workout/{workout_id}", json=payload, api=True)
    strength.remember_template(payload["workoutName"], int(workout_id),
                               old_name=existing.get("workoutName"))

    from datetime import date, timedelta

    from tools.scheduling import list_scheduled_workouts

    today = date.today()
    scheduled = [w["date"] for w in list_scheduled_workouts(
        today.isoformat(), (today + timedelta(days=365)).isoformat())
        if w.get("workout_id") == int(workout_id)]
    segs = payload.get("workoutSegments") or [{}]
    return {"workout_id": int(workout_id), "name": payload["workoutName"],
            "steps_replaced": blocks is not None,
            "steps": strength.summarize_steps(segs[0].get("workoutSteps") or []),
            "scheduled_dates": scheduled}


@mcp.tool()
def get_strength_activity_sets(activity_id: int, refresh: bool = False) -> dict:
    """The sets Garmin recorded for a completed strength activity, grouped
    per exercise: number of sets, reps, weight and duration per set.

    Reads the local cache (sync_activities fills it for new strength
    activities; backfill_strength_sets=True for history) and fetches from
    Garmin when the activity was never fetched or refresh=True (e.g. after
    the sets were edited in Connect).

    Exercise names are Garmin's: taken from the workout step when the
    session was run from a structured workout, otherwise Garmin's own
    detection, which often says UNKNOWN. A session done without a workout
    usually comes back as one long UNKNOWN set — the result says so.
    Raw rows are in strength_sets (query_activity_cache).
    """
    cached = None if refresh else strength.activity_sets(activity_id)
    if cached is not None:
        return cached
    import sqlite3

    import garmin_sync

    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        known = conn.execute("SELECT 1 FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if not known:
        return {"error": f"Activity {activity_id} not in cache — run sync_activities first."}
    strength.fetch_and_store_sets(_client(), activity_id)
    return strength.activity_sets(activity_id)
