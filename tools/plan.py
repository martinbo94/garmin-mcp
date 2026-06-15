"""Training plan: load, validate, summarize, save, materialize, compare."""
from typing import Optional

import garmin_sync
import plan as plan_mod
from core import IntervalSet, Step, mcp
from tools.scheduling import schedule_workout
from tools.workouts import create_continuous_run, create_interval_workout


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


