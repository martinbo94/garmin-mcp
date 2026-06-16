"""Activity sync, summaries, cache queries, breakdowns, PRs, form trends."""
from typing import Literal, Optional

import garmin_sync
import plan as plan_mod
from core import _client, mcp


# ─── Activity sync + weekly summary (reads local cache) ────────────────
@mcp.tool()
def sync_activities(
    force_full: bool = False,
    weeks_back: Optional[int] = None,
    backfill_links: bool = False,
    backfill_max: int = 100,
    wellness_days: int = 10,
) -> dict:
    """Pull new activities + HR streams + laps + recent wellness into the cache.

    Runs incrementally since the last sync, and also refreshes the trailing
    `wellness_days` of wellness (HRV, resting HR, sleep) so the recovery /
    readiness tools stay current.

    IMPORTANT — `new_activities: 0` does NOT mean the cache is stale or that a
    recent activity is missing. It almost always means the activity was
    already synced (e.g. by a prior sync or the startup sync) and there is
    simply nothing new since then. To judge freshness, do NOT re-read the
    `new_activities` count — instead check the `cache_newest_activity` and
    `cache_newest_wellness` dates returned by this call (they are the actual
    newest cached dates), or call `list_activities` and look at the top row.
    Only escalate to `force_full=True` if those dates confirm a genuinely
    missing recent activity. Re-syncing because the count was 0 is a common
    mistake — verify the cached dates first.

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
        wellness_days: Trailing days of wellness to refresh (default 10;
            0 to skip). Historical wellness backfill is via
            get_wellness_history.

    Returns dict with new_activities, streams_fetched, laps_fetched,
    details_fetched, wellness_fetched/wellness_cached, last_sync, per-item
    errors, and — for freshness checks — `cache_newest_activity` and
    `cache_newest_wellness` (the newest cached dates). With backfill_links
    also details_fetched / relinked / remaining_without_detail.
    """
    result = garmin_sync.run_sync(
        _client(), force_full=force_full, weeks_back=weeks_back,
        wellness_days=wellness_days,
    )
    if backfill_links and "error" not in result:
        result["backfill"] = garmin_sync.backfill_workout_links(
            _client(), max_activities=backfill_max
        )
    return result


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
def weekly_retrospective(
    start_date: str,
    end_date: Optional[str] = None,
    with_compliance: bool = True,
) -> dict:
    """Per-week training summary, optionally with plan compliance.

    The single weekly tool. It returns per-week volume, HR-zone time, and
    the session list from the local Garmin cache (via the same engine as
    the per-week summary), and can layer on plan compliance.

    Two modes, selected by whether `end_date` is given:
    - **Single week** (`end_date` omitted): `start_date` is treated as a
      week_start and the Monday-Sunday week beginning on it is summarized.
      The response carries `week_start`, `week_end`, and a `summary` block
      for that one week. Use as a Sunday-evening reflection input — one
      call covers both "what did I do" and "how close to plan was I".
    - **Arbitrary range** (`end_date` given): summarizes every week in the
      inclusive `start_date`..`end_date` range. The response carries
      `weeks` (a list of per-week entries) over that span.

    Each week entry covers one Monday-Sunday week and contains total
    distance, run count, time in each HR zone (computed from raw streams
    using current bpm boundaries from `get_athlete_profile` /
    coach://user_profile — NOT the local cache zones), and the list of
    activities with names, descriptions, distance, HR, and a
    `classification_hint` derived from naming patterns.

    The `coverage` field reports cache extent and a `gap_warning` flag
    when the requested range extends before the oldest cached activity —
    use it to distinguish "no runs that week" from "we don't have data
    that far back" (the local cache holds 12 weeks by default; call
    `sync_activities(weeks_back=N)` to extend it).

    Args:
        start_date: 'YYYY-MM-DD' (inclusive). When `end_date` is omitted,
            this is the week_start (typically the Monday of the week).
        end_date: 'YYYY-MM-DD' (inclusive). Omit for single-week mode.
        with_compliance: When True (default), add a `plan_compliance` block
            (`compare_plan_vs_actual` against plan.json) over the same
            span. Set False for just the summary.
    """
    from datetime import date as _date, timedelta as _td
    start = _date.fromisoformat(start_date)
    if end_date is None:
        end = start + _td(days=6)
        result = garmin_sync.weekly_summary(start.isoformat(), end.isoformat())
        weeks = result["weeks"]
        out: dict = {
            "week_start": start_date,
            "week_end": end.isoformat(),
            "summary": weeks[0] if weeks else None,
            "coverage": result["coverage"],
        }
        if with_compliance:
            out["plan_compliance"] = plan_mod.compare_plan_vs_actual(
                start.isoformat(), end.isoformat()
            )
        return out

    end = _date.fromisoformat(end_date)
    result = garmin_sync.weekly_summary(start.isoformat(), end.isoformat())
    out = {
        "weeks": result["weeks"],
        "coverage": result["coverage"],
    }
    if with_compliance:
        out["plan_compliance"] = plan_mod.compare_plan_vs_actual(
            start.isoformat(), end.isoformat()
        )
    return out




# ─── Progress report ──────────────────────────────────────────────────
@mcp.tool()
def progress_report(
    session_type: Literal["threshold", "intervals", "long", "easy", "tempo"],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Compare the same session type over time to track fitness progress.

    Sessions are classified via the plan link first (the workout's own
    planned_type is ground truth) and fall back to the name pattern for
    free runs and pre-linkage history — so plan-driven blocks (e.g.
    threshold) classify correctly even when the activity name is generic.

    For interval/threshold sessions, per-session metrics are derived from
    work-rep (drag) laps only — warmup, cooldown, and rest laps are
    excluded so the comparison isn't diluted by session structure.

    For continuous sessions (easy, long, tempo) the whole-session avg HR
    and pace are used since there are no meaningful lap divisions.

    Trend is HR-based for every session type. Pace is *not* used to assess
    the trend: for intervals/threshold pace varies with rep length (a pace
    lever, not an intensity lever), and for easy/long pace is HR-capped by
    this framework. Pace is still reported per session for context, and
    pace deltas are included as informational-only fields.

    Args:
        session_type: One of 'threshold', 'intervals', 'long', 'easy', 'tempo'.
        start_date: 'YYYY-MM-DD' (inclusive). Defaults to 90 days ago.
        end_date:   'YYYY-MM-DD' (inclusive). Defaults to today.

    Returns:
    - `sessions`: matching sessions in chronological order (oldest first).
      For intervals/threshold: `avg_hr` and `pace_s_per_km` are drag-lap
      averages; `drag_count` shows how many reps were found; sessions with
      no lap data are flagged with data_source 'session_avg_fallback'.
      For easy/long/tempo: whole-session values. Each session also carries
      `classification_source` ('plan' or 'name').
    - `trend`: first_half vs second_half comparison with `assessment`
      ('improving' / 'stable' / 'declining'), based on HR. For interval
      types only drag-lap sessions feed the trend (fallback sessions are
      excluded so warmup/rest HR doesn't pollute the halves).
    - `data_source`: 'drag_laps' or 'session_avg' — the per-session signal.
    - `note`: human-readable summary, including the pace caveat.
    """
    import sqlite3 as _sqlite3
    import json as _json
    from datetime import date as _date, datetime as _datetime, timedelta as _td

    _INTERVAL_TYPES = {"threshold", "intervals"}
    # classify_activity may return richer labels (e.g. prog-long); map the
    # ones that belong to a requested bucket.
    _type_aliases = {
        "threshold": {"threshold"},
        "intervals": {"intervals"},
        "long": {"long", "prog-long"},
        "easy": {"easy"},
        "tempo": {"tempo"},
    }

    def _valid_date(s: str) -> bool:
        try:
            _datetime.strptime(s, "%Y-%m-%d")
            return True
        except (ValueError, TypeError):
            return False

    try:
        today = _date.today()
        if start_date is not None and not _valid_date(start_date):
            return {"error": f"Invalid start_date '{start_date}'. Expected 'YYYY-MM-DD'."}
        if end_date is not None and not _valid_date(end_date):
            return {"error": f"Invalid end_date '{end_date}'. Expected 'YYYY-MM-DD'."}

        effective_end = end_date or today.isoformat()
        effective_start = start_date or (today - _td(days=90)).isoformat()
        if effective_start > effective_end:
            return {"error": f"start_date ({effective_start}) is after end_date ({effective_end})."}

        target_hints = _type_aliases.get(session_type, {session_type})
        use_drag_laps = session_type in _INTERVAL_TYPES

        with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """
                SELECT a.id, a.start_date_local, a.name, a.sport_type,
                       a.distance_m, a.moving_time_s, a.avg_hr,
                       a.planned_type, l.laps_json
                FROM activities a
                LEFT JOIN laps l ON l.activity_id = a.id
                WHERE date(a.start_date_local) BETWEEN ? AND ?
                  AND a.sport_type = 'Run'
                ORDER BY a.start_date_local
                """,
                (effective_start, effective_end),
            ).fetchall()

        zones = garmin_sync._parse_zones()
        sessions = []

        for r in rows:
            classification, cls_source = garmin_sync.classify_activity(
                r["name"], r["sport_type"], r["planned_type"]
            )
            if classification not in target_hints:
                continue

            dist_m = r["distance_m"] or 0
            time_s = r["moving_time_s"]

            if use_drag_laps and r["laps_json"]:
                # Use drag-lap averages — excludes wu/cd/pause
                raw_laps = _json.loads(r["laps_json"])
                classified = garmin_sync._classify_laps(raw_laps, zones)
                drag_laps = [l for l in classified if l.get("lap_type") == "drag"]

                if not drag_laps:
                    # No drags found — fall back to session avg with a flag.
                    # Flagged sessions are excluded from the drag-lap trend.
                    avg_hr = r["avg_hr"]
                    pace = round(time_s / (dist_m / 1000)) if (time_s and dist_m) else None
                    drag_count = 0
                    source = "session_avg_fallback"
                else:
                    hrs = [l["average_heartrate"] for l in drag_laps if l.get("average_heartrate")]
                    paces = [
                        round(1000 / l["average_speed"])
                        for l in drag_laps
                        if (l.get("average_speed") or 0) > 0
                    ]
                    avg_hr = round(sum(hrs) / len(hrs), 1) if hrs else None
                    pace = round(sum(paces) / len(paces)) if paces else None
                    drag_count = len(drag_laps)
                    source = "drag_laps"
            else:
                # Continuous session or no lap data
                avg_hr = r["avg_hr"]
                pace = round(time_s / (dist_m / 1000)) if (time_s and dist_m) else None
                drag_count = None
                source = "session_avg"

            entry: dict = {
                "date": r["start_date_local"][:10],
                "name": r["name"],
                "classification_source": cls_source,
                "distance_km": round(dist_m / 1000, 2) if dist_m else None,
                "avg_hr": avg_hr,
                "moving_time_s": round(time_s) if time_s is not None else None,
                "pace_s_per_km": pace,
                "data_source": source,
            }
            if drag_count is not None:
                entry["drag_count"] = drag_count
            sessions.append(entry)

        # Rows are already chronological (oldest first) from the query.
        sessions_chrono = sessions

        def _halves_stats(items):
            hrs = [s["avg_hr"] for s in items if s.get("avg_hr") is not None]
            paces = [s["pace_s_per_km"] for s in items if s.get("pace_s_per_km") is not None]
            return {
                "avg_hr": round(sum(hrs) / len(hrs), 1) if hrs else None,
                "avg_pace_s_per_km": round(sum(paces) / len(paces)) if paces else None,
                "count": len(items),
            }

        # Trend is HR-based. For interval types, exclude fallback sessions
        # (whole-session HR would mix warmup/rest into the comparison).
        if use_drag_laps:
            trend_items = [s for s in sessions_chrono if s.get("data_source") == "drag_laps"]
        else:
            trend_items = sessions_chrono

        trend: dict = {}
        if len(trend_items) >= 2:
            mid = len(trend_items) // 2
            fh = _halves_stats(trend_items[:mid])
            sh = _halves_stats(trend_items[mid:])

            assessment = "stable"
            if fh["avg_hr"] is not None and sh["avg_hr"] is not None:
                hr_d = sh["avg_hr"] - fh["avg_hr"]
                # Lower HR for the same kind of work = improving fitness.
                if hr_d < -2:
                    assessment = "improving"
                elif hr_d > 3:
                    assessment = "declining"
            else:
                assessment = "insufficient_data"

            pace_delta = (
                sh["avg_pace_s_per_km"] - fh["avg_pace_s_per_km"]
                if (fh["avg_pace_s_per_km"] and sh["avg_pace_s_per_km"]) else None
            )
            trend = {
                "based_on": "hr",
                "first_half": fh,
                "second_half": sh,
                "hr_delta_bpm": round(sh["avg_hr"] - fh["avg_hr"], 1)
                    if (fh["avg_hr"] is not None and sh["avg_hr"] is not None) else None,
                "pace_delta_s_per_km_informational": pace_delta,
                "assessment": assessment,
                "trend_session_count": len(trend_items),
            }
        elif trend_items:
            trend = {"based_on": "hr", "assessment": "insufficient_data",
                     "trend_session_count": len(trend_items)}
        else:
            trend = {"based_on": "hr", "assessment": "no_data", "trend_session_count": 0}

        total = len(sessions)
        data_source = "drag_laps" if use_drag_laps else "session_avg"
        note = f"Found {total} {session_type} session(s) between {effective_start} and {effective_end}."
        if total == 0:
            note += (f" No sessions classified as '{session_type}'. "
                     "Try sync_activities() or check session names/plan links.")
        elif use_drag_laps:
            no_laps = sum(1 for s in sessions if s.get("data_source") == "session_avg_fallback")
            note += " HR/pace from work reps (drag laps) only — warmup, rest, and cooldown excluded."
            if no_laps:
                note += (f" {no_laps} session(s) had no lap data; shown with a "
                         "'session_avg_fallback' flag and excluded from the HR trend.")
            note += " Trend is HR-based; pace varies with rep length so it is informational only."
        else:
            note += " Trend is HR-based; pace is HR-capped in this framework and shown for context only."

        return {
            "session_type": session_type,
            "data_source": data_source,
            "date_range": {"start": effective_start, "end": effective_end},
            "sessions": sessions_chrono,
            "trend": trend,
            "note": note,
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


