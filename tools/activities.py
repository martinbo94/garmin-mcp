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
    backfill_streams: bool = False,
    backfill_gear: bool = False,
    backfill_laps: bool = False,
    backfill_activity_metrics: bool = False,
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
        backfill_streams: If True, populate the extra stream columns
            (elevation / pace / distance / cadence / performance condition /
            grade-adjusted speed) for activities whose cached stream predates
            them (one API call each). New activities get these automatically;
            this is only for history. Also fills the perf_cond_* summary
            columns, so run it before trusting a performance-condition trend
            over older sessions.
        backfill_laps: If True, re-fetch laps for cached activities whose
            laps_json predates the per-lap running-dynamics fields (cadence,
            ground contact, vertical oscillation/ratio, stride, power,
            respiration) — one API call each. HR/pace lap data is unaffected.
        backfill_activity_metrics: If True, populate the activity-level
            running-dynamics / power / training-load / VO2max columns for
            history synced before they existed. Cheap — these ride on the
            activity list payload, so it re-lists the trailing year in
            batches rather than calling per activity.
        backfill_gear: If True, fetch the gear (shoe) used for cached
            activities that never got it (one API call each). New activities
            get this automatically; this is only for history.
        backfill_max: Max activities to backfill per call (default 100),
            shared by the backfills. The `remaining_*` field in each
            backfill result tells you whether another round is needed.
        wellness_days: Trailing days of wellness to refresh (default 10;
            0 to skip). Historical wellness backfill is via
            get_wellness_history.

    Returns dict with new_activities, streams_fetched, laps_fetched,
    details_fetched, wellness_fetched/wellness_cached, vo2max_days,
    last_sync, per-item errors, and — for freshness checks —
    `cache_newest_activity` and `cache_newest_wellness` (the newest cached
    dates). Each requested backfill adds its own block: `backfill`,
    `stream_backfill`, `gear_backfill`, `lap_backfill`, `metrics_backfill`.
    """
    result = garmin_sync.run_sync(
        _client(), force_full=force_full, weeks_back=weeks_back,
        wellness_days=wellness_days,
    )
    if backfill_links and "error" not in result:
        result["backfill"] = garmin_sync.backfill_workout_links(
            _client(), max_activities=backfill_max
        )
    if backfill_streams and "error" not in result:
        result["stream_backfill"] = garmin_sync.backfill_streams(
            _client(), max_activities=backfill_max
        )
    if backfill_gear and "error" not in result:
        result["gear_backfill"] = garmin_sync.backfill_gear(
            _client(), max_activities=backfill_max
        )
    if backfill_laps and "error" not in result:
        result["lap_backfill"] = garmin_sync.backfill_laps(
            _client(), max_activities=backfill_max
        )
    if backfill_activity_metrics and "error" not in result:
        result["metrics_backfill"] = garmin_sync.backfill_activity_metrics(
            _client()
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
    training_effect_label, workout_rpe, workout_feel, workout_compliance,
    gear_name (the shoe used; null if unassigned or not yet gear-synced).

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
                 workout_compliance, detail_fetched_at,
                 gear_uuid, gear_name,
                 avg_cadence_spm, max_cadence_spm, avg_ground_contact_ms,
                 avg_gct_balance_pct, avg_vertical_osc_cm,
                 avg_vertical_ratio_pct, avg_stride_length_cm,
                 avg_power_w, max_power_w, norm_power_w,
                 avg_respiration_rate, avg_grade_adj_speed, steps,
                 activity_training_load, aerobic_training_effect,
                 anaerobic_training_effect, vo2max_at_activity,
                 perf_cond_first, perf_cond_min, perf_cond_max,
                 perf_cond_avg, perf_cond_last, metrics_fetched_at)
          associated_workout_id: Garmin workout template the activity
          executed (null for free runs). planned_type: the plan's label
          for that workout (threshold/easy/long/...) — ground truth for
          classification when present. workout_rpe/workout_feel: watch
          post-workout self-evaluation (0-100). workout_compliance:
          Garmin execution score. training_effect_label: Garmin's
          physiological auto-label — response signal, not intent.
          gear_uuid/gear_name: the shoe used (joins gear.uuid); null when
          no gear was assigned or the activity isn't gear-synced yet
          (sync_activities(backfill_gear=True) populates history).
          Running dynamics (avg_cadence_spm .. avg_grade_adj_speed) come
          off the Garmin activity list payload: cadence in steps/min (NOT
          the half-step stream units), stride length in CENTIMETRES,
          avg_gct_balance_pct = left-foot share (50 = even), power in watts
          from the wrist-based estimate. They are strongly pace-dependent —
          always group by session type before comparing.
          activity_training_load / aerobic_training_effect /
          anaerobic_training_effect are Garmin's own per-session load and
          TE scores. vo2max_at_activity is Garmin's VO2max as of that
          activity at integer resolution — use vo2max_daily.vo2max for
          trend maths, it carries the decimal.
          perf_cond_* summarize the performance-condition stream (see
          streams.perf_condition_json); NULL where the metric was never
          emitted. metrics_fetched_at NULL = row predates these columns,
          fix with sync_activities(backfill_activity_metrics=True).
      gear(uuid, name, make_model, type, status, in_use_since,
                 retired_at, total_distance_km, total_activities, synced_at)
          Gear library (shoes etc.), status='active'/'retired'. Join
          activities.gear_uuid = gear.uuid to attribute mileage by
          session type, e.g. threshold km per shoe.
      workout_type_map(garmin_workout_id, planned_type, workout_name,
                 plan_name, planned_date, updated_at)
          Durable workout_id → planned type mapping, written at
          materialize time; survives plan.json being replaced.
      laps(activity_id, laps_json, fetched_at, dynamics_fetched_at)
          laps_json: JSON array of laps, each with lap_index,
          average_heartrate, max_heartrate, distance (m), elapsed_time (s),
          moving_time (s), average_speed (m/s), start_date_local,
          intensityType, plus per-lap running dynamics: avg_cadence_spm,
          ground_contact_ms, gct_balance_pct, vertical_osc_cm,
          vertical_ratio_pct, stride_length_cm, avg_power_w, norm_power_w,
          avg_grade_adj_speed, avg_respiration_rate. Note the lap_type tag
          ('wu'/'drag'/'pause'/'cd'/'easy') is computed at read time by
          activity_breakdown and is NOT stored in laps_json.
          dynamics_fetched_at NULL = row predates the dynamics fields; fix
          with sync_activities(backfill_laps=True).
      streams(activity_id, time_json, hr_json, elevation_json, speed_json,
                 distance_json, cadence_json, perf_condition_json,
                 grade_adj_speed_json, extras_fetched_at, perf_fetched_at)
          Parallel JSON arrays, all the same length and index-aligned to
          time_json (elapsed seconds): hr (bpm), elevation (m), speed (m/s),
          distance (cumulative m), cadence (Garmin directRunCadence =
          strides/min, i.e. ~half steps-per-minute — double for spm).
          elevation/speed/distance/
          perf_condition (Garmin "ytelseskondisjon", -20..+20; leading
          samples are legitimately NULL because Garmin needs 6-20 min of
          running before its first estimate), grade_adj_speed (m/s, pace
          normalized for gradient — use this, not speed, when comparing
          effort across hilly and flat sessions).
          The extra arrays are NULL for streams cached before they were added
          (extras_fetched_at covers elevation/speed/distance/cadence,
          perf_fetched_at covers perf_condition/grade_adj_speed) —
          sync_activities(backfill_streams=True) populates them; they're also
          NULL per-sample for activities lacking a metric (e.g. indoor =
          no elevation/GPS). These are LARGE (thousands of points) — never
          SELECT them raw; use json_each() to aggregate, e.g. align HR and
          elevation by position:
          SELECT he.value AS hr, ee.value AS elev
          FROM streams,
               json_each(hr_json) he, json_each(elevation_json) ee
          WHERE activity_id = 123 AND he.key = ee.key.
      vo2max_daily(date, vo2max, vo2max_rounded, fitness_age, synced_at)
          Garmin's own VO2max estimate. SPARSE BY DESIGN — a row exists only
          for days Garmin recomputed it (i.e. days with a qualifying
          outdoor run), so missing dates mean "no new estimate", not "no
          data". vo2max carries one decimal; vo2max_rounded is the integer
          the watch shows. Prefer the vo2max_trend tool, which also joins
          each move to the sessions around it and carries the caveats.
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
      pace_s_per_km, avg_hr, max_hr, form}. `form` (present when Garmin
      reported dynamics) carries cadence_spm, stride_cm, gct_ms,
      vert_osc_cm, vert_ratio_pct, power_w, resp_rate for that lap — read
      ACROSS the drag laps to see fatigue accumulate: a shortening stride
      at held cadence, with power falling and HR climbing, is the standard
      signature of reps biting. Pause-lap dynamics are meaningless (walking
      or standing skews every one of them) — ignore them. Laps cached
      before these fields existed have no `form`; fix with
      sync_activities(backfill_laps=True). `type` is auto-classified as
      "drag" (work rep, Z3+ avg HR ≥30s), "pause" (recovery between
      drags), "wu" (warmup before first drag), "cd" (cooldown after
      last drag), or "easy" (continuous easy run, no drags found).
    - `zone_secs` + `zone_pcts`: time in each HR zone (Z1-Z5).
    - `band_anchors_bpm` + `band_secs`: session time measured against the
      user's bpm anchors — inside the sub-threshold band, above the hard
      cap, and at/above LT2. **These, not Z1-Z5 shares, are the intensity
      verdict**: Z4 straddles the hard cap, so "time in Z4" mixes at-cap
      sub-threshold work with over-threshold work. Note the three
      counters are independent flags, not a partition: `at_or_above_lt2`
      ⊂ `over_cap`, time between the band top and the cap counts in
      neither, and they don't sum to the total.
    - `session_category`: heuristic "easy" | "sub-threshold" |
      "at-threshold" | "vo2" — judged on stream-sliced SECONDS at/above
      LT2 and over the cap, not momentary peaks. Useful for compliance
      scoring; refine ambiguous edges via coach://classification.
    - `classification_hint`: name-pattern hint (deterministic 90% case).
    - `race_note` (present when the name or the effort — >=15 min at/above
      LT2 — marks a race): read it and follow it. A race categorized "vo2"
      / "at-threshold" is expected load, not a training-zone violation,
      and race HR must not be used to recalibrate zones or LT2.
    - `interval_analysis` (present only for sessions with work reps):
      `work_reps` (per rep: pace, avg_hr, peak_hr, trimmed_avg_hr,
      drift_bpm, `zone_secs`, and **`band_secs`** = seconds in-band /
      over-cap / at-or-above-LT2), a `work_summary` (work-only zone +
      band distribution, across-rep drift), and a `how_to_present` block.

    HOW TO ANALYZE AN INTERVAL SESSION (read `how_to_present` in the
    payload and follow it): lead with a per-rep table of pace / avg_hr /
    peak_hr / band_secs, then work_summary.band_secs. Judge intensity by
    SECONDS at/above LT2 and over the cap — a long rep whose peak briefly
    brushes LT2 (a hill, the rep ending) is normal HR behavior, NOT a
    threshold rep. Only short reps (<~3 min) are judged by peak_hr, since
    HR lag (~45-60s) drags their averages and time-in-band down. Before
    calling a sub-threshold session "at threshold", state how many
    seconds actually sat at/above LT2; for finer bpm slicing use
    `hr_time_in_buckets(scope="work", edges=[...])`.

    The activity must be in the local cache. If `error` is
    returned with `next_steps`, call `sync_activities()` (or
    `sync_activities(weeks_back=N)` for older activities) and retry.
    Laps are cached from Garmin at sync time.

    Garmin activity_id.
    """
    return garmin_sync.activity_breakdown(activity_id)


@mcp.tool()
def hr_time_in_buckets(
    activity_id: int,
    edges: Optional[list[int]] = None,
    scope: str = "session",
) -> dict:
    """Time spent in custom HR (bpm) ranges for one cached activity.

    Complements `activity_breakdown`'s zone time when you need bands that
    don't line up with the Z1-Z5 boundaries — e.g. "how long over the
    sub-threshold cap?" or "minutes above 190 bpm?". Time is integrated
    from the raw HR stream using the true sample spacing (the stream's
    elapsed-time deltas, NOT an assumed 1 Hz), so down-sampled streams are
    handled correctly.

    Args:
        activity_id: Garmin activity id (must be in the local cache with an
            HR stream — run sync_activities() if missing).
        edges: Ascending bpm cut points. E.g. [175, 181, 186, 191] yields
            buckets <175, 175-180, 181-185, 186-190, 191+. When omitted,
            the user's HR-zone boundaries from coach://user_profile are
            used (one bucket per zone).
        scope: 'session' (default) bins the whole stream; 'work' bins only
            the time inside work-rep (drag) lap windows — warmup, recovery,
            and cooldown excluded — using the timestamp-sliced rep windows.

    Returns: id, scope, edges (the resolved cut points), edges_source
    ('custom' or 'hr_zones'), total_seconds, and `buckets` — a list of
    {label, seconds, percent}. Compare the upper buckets against the
    sub-threshold band / hard cap in coach://user_profile to judge how
    much time was spent above the intended ceiling.
    """
    return garmin_sync.hr_time_in_buckets(activity_id, edges=edges, scope=scope)


@mcp.tool()
def update_activity(
    activity_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Edit a completed activity's title and/or description on Garmin Connect.

    This is the write path back onto a finished activity. Use it to pin
    session context where every later reader will find it — treadmill +
    heat ("varmt/fuktig, lav band-% delvis termisk"), illness, terrain,
    why a session was cut short, RPE notes. `weekly_retrospective` and
    `activity_breakdown` read the cached description, so a note saved
    here survives across sessions without relying on chat memory.

    Both fields optional; pass at least one. The change is written to
    Garmin Connect AND to the local cache row in the same call — no
    resync needed, later tool calls see it immediately.

    ⚠ `description` REPLACES the existing text. To append, take the
    current text from the returned `old` value (or activity_breakdown)
    and include it in the new string.

    Args:
        activity_id: Garmin activity id (must be in the local cache —
            run sync_activities() first if missing).
        name: New activity title, or None to leave unchanged.
        description: New description/notes, or None to leave unchanged.

    Returns: id plus per-field {old, new} for what changed.
    """
    import sqlite3

    if name is None and description is None:
        return {"error": "Pass at least one of `name` or `description`."}

    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, name, description FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
    if not row:
        return {
            "error": f"Activity {activity_id} not in local cache.",
            "next_steps": ["Run sync_activities() to pull recent activities."],
        }

    # Garmin's activity endpoint accepts a partial PUT — same pattern the
    # garminconnect library uses for set_activity_name.
    payload: dict = {"activityId": activity_id}
    if name is not None:
        payload["activityName"] = name
    if description is not None:
        payload["description"] = description
    g = _client()
    url = f"{g.garmin_connect_activity}/{activity_id}"
    g.client.put("connectapi", url, json=payload, api=True)

    sets, params = [], []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    params.append(activity_id)
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        conn.execute(f"UPDATE activities SET {', '.join(sets)} WHERE id = ?", params)

    result: dict = {"id": activity_id, "updated": {}}
    if name is not None:
        result["updated"]["name"] = {"old": row["name"], "new": name}
    if description is not None:
        result["updated"]["description"] = {"old": row["description"], "new": description}
    return result


@mcp.tool()
def running_form_trends(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    sport_type: str = "Run",
    classification: Optional[str] = None,
    limit: int = 200,
) -> dict:
    """Running-dynamics (form) trends over the local cache — no Garmin calls.

    Covers cadence, ground contact time, ground-contact balance, vertical
    oscillation, vertical ratio, stride length, running power and respiration
    rate. Reads the cache, so the window is the whole synced history rather
    than Garmin's last 30 activities.

    **Form metrics are pace-dependent, and that dominates everything else
    here.** Cadence and stride length rise with speed; ground contact time
    falls. Pooling easy runs with intervals produces an average that
    describes the session mix, not the runner. So:
      - every `per_activity` row carries `pace_s_per_km` and its
        `classification`;
      - `by_classification` gives the averages split by session type — **this
        is the block to compare across time**, not the pooled `averages`;
      - a `trends` direction can flip purely because the session mix changed
        between the two halves of the window. Check that before reporting
        form decay.

    Args:
        start_date / end_date: 'YYYY-MM-DD' inclusive bounds. Default: the
            whole cache.
        sport_type: exact match, default 'Run'. Dynamics only exist for
            running.
        classification: restrict to one session type (easy / threshold /
            intervals / long / prog-long / race / tempo) — the cleanest way
            to get a like-for-like trend.
        limit: max `per_activity` rows returned (most recent kept, cap 1000).
            Averages and trends are computed over the full match set.

    Returns window, per_activity, averages, by_classification, trends,
    ratings (Garmin benchmark bands), insights, notes, and
    `activities_without_metrics` — a non-zero count there means part of the
    cache predates these columns; run
    sync_activities(backfill_activity_metrics=True) to fill it in.

    Ratings use Garmin's published population bands (cadence >=180 elite,
    ground contact <200 ms elite, vertical oscillation <6 cm elite, vertical
    ratio <6% elite). They are not targets — height and leg length move
    oscillation independently of running economy, so treat a "needs_work"
    on a tall runner as context, not a fault.
    """
    return garmin_sync.running_form_trends(
        start_date=start_date, end_date=end_date, sport_type=sport_type,
        classification=classification, limit=limit,
    )


@mcp.tool()
def performance_condition(activity_id: int) -> dict:
    """Garmin's performance condition ("ytelseskondisjon") for one activity.

    The number that appears on the watch 6-20 minutes into a run, and its
    whole time course afterwards — the same data the Connect app draws as
    "ytelseskondisjon over tid".

    What it measures: a -20..+20 comparison of the current effort's pace/HR
    relationship against Garmin's model of the athlete's recent performance.
    **0 means "exactly as your recent baseline predicts"**, so a strong
    session can legitimately read 0 the whole way. It is not a fitness score
    and not a readiness score.

    Returns first_value + first_at_min (the pop-up number), min / max / avg /
    last, `drift` (last - first), `by_quarter` (the curve's shape),
    `per_lap` (mean per lap with its drag/pause/wu/cd tag — where interval
    rep-to-rep decay shows), and an `interpretation` block.

    HOW TO READ IT: the within-session shape is the signal. Holding flat or
    rising through a long run is durability; a steady sag is fatigue, heat or
    a too-fast start. The absolute level is far weaker evidence, because the
    baseline it compares against moves as fitness moves — after a good block
    the same run scores lower. Never recalibrate zones, paces or LT2 from it,
    and never let it override the athlete's own read of the session.

    Requires a cached stream. Activities synced before performance condition
    was stored return a `reason` pointing at
    sync_activities(backfill_streams=True).
    """
    return garmin_sync.performance_condition(activity_id)


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

    Each week entry covers one Monday-Sunday week. **Volume fields are
    split by sport and must be reported separately**: `run_distance_m` /
    `run_moving_time_s` / `run_session_count` cover RUNNING ONLY, while
    `cross_distance_m` / `cross_moving_time_s` / `cross_session_count`
    cover everything else (ski, bike, ...). Never add them into one
    combined "total km". `zone_secs` and the `activities` list span ALL
    sports (zone time computed from raw streams using current bpm
    boundaries from `get_athlete_profile` / coach://user_profile — NOT
    the local cache zones). Activities carry names, descriptions,
    distance, HR, and a `classification_hint` from naming patterns.

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


