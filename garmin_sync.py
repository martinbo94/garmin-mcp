"""Garmin activity sync + local cache.

On server startup (and via explicit `sync_activities` tool call) pulls new
activities from Garmin Connect since the last sync, including HR streams and
lap data for runs. Data lives in `coach_data/cache.db` so weekly summaries
and activity breakdowns don't hit the API on every query.
"""
import collections
import json
import os
import re
import sqlite3
import statistics
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).parent
DB_PATH = ROOT / "coach_data" / "cache.db"
USER_PROFILE_PATH = ROOT / "coach_data" / "user_profile.md"
INITIAL_BACKFILL_WEEKS = 12

# Sport types that should get HR streams (cardio, not just running).
_CARDIO_TYPES = {
    "Run", "Rowing", "NordicSki", "RollerSki", "Ride", "Swim",
    "indoor_cardio", "Workout",
    # Raw typeKeys that might slip through the type map
    "indoor_rowing", "track_running", "virtual_run",
}

# Garmin typeKey → sport_type label used in the cache and name_hint.
_GARMIN_TYPE_MAP: dict[str, str] = {
    "running": "Run",
    "indoor_running": "Run",
    "treadmill_running": "Run",
    "trail_running": "Run",
    "strength_training": "WeightTraining",
    "indoor_cycling": "Ride",
    "cycling": "Ride",
    "mountain_biking": "Ride",
    "hiking": "Hike",
    "walking": "Walk",
    "elliptical": "Workout",
    "yoga": "Workout",
    "swimming": "Swim",
    "open_water_swimming": "Swim",
    "skate_skiing_ws": "NordicSki",
    "cross_country_skiing_ws": "NordicSki",
    "resort_skiing_snowboarding_ws": "AlpineSki",
    "rowing": "Rowing",
    "indoor_rowing": "Rowing",
    "track_running": "Run",
    "virtual_run": "Run",
}

# Garmin intensityType → our lap_type tag.
_INTENSITY_TYPE_MAP: dict[str, str] = {
    "WARMUP": "wu",
    "ACTIVE": "drag",
    "INTERVAL": "drag",
    "REST": "pause",
    "RECOVERY": "pause",
    "COOLDOWN": "cd",
}


# ─── SQLite cache ─────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    start_date_local TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    type TEXT,
    sport_type TEXT,
    distance_m REAL,
    moving_time_s INTEGER,
    elapsed_time_s INTEGER,
    avg_hr REAL,
    max_hr REAL,
    total_elevation_gain REAL,
    synced_at TEXT NOT NULL,
    associated_workout_id INTEGER,
    planned_type TEXT,
    training_effect_label TEXT,
    workout_rpe INTEGER,
    workout_feel INTEGER,
    workout_compliance INTEGER,
    detail_fetched_at TEXT,
    start_lat REAL,
    start_lon REAL,
    gear_uuid TEXT,
    gear_name TEXT,
    gear_fetched_at TEXT,
    -- Running dynamics + power + physiology, all straight off the activity
    -- LIST payload (no extra API call). NULL for activities synced before
    -- these columns existed and for sports/devices that don't report them.
    avg_cadence_spm REAL,
    max_cadence_spm REAL,
    avg_ground_contact_ms REAL,
    avg_gct_balance_pct REAL,
    avg_vertical_osc_cm REAL,
    avg_vertical_ratio_pct REAL,
    avg_stride_length_cm REAL,
    avg_power_w REAL,
    max_power_w REAL,
    norm_power_w REAL,
    avg_respiration_rate REAL,
    avg_grade_adj_speed REAL,
    steps INTEGER,
    activity_training_load REAL,
    aerobic_training_effect REAL,
    anaerobic_training_effect REAL,
    vo2max_at_activity REAL,
    -- Performance condition, derived from the stream at store time.
    perf_cond_first REAL,
    perf_cond_min REAL,
    perf_cond_max REAL,
    perf_cond_avg REAL,
    perf_cond_last REAL,
    metrics_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(start_date_local);

-- Gear library (shoes etc.), Garmin = source of truth, cached so the gear
-- tools read locally instead of hitting Garmin. Refreshed on every sync.
CREATE TABLE IF NOT EXISTS gear (
    uuid TEXT PRIMARY KEY,
    name TEXT,
    make_model TEXT,
    type TEXT,
    status TEXT,
    in_use_since TEXT,
    retired_at TEXT,
    total_distance_km REAL,
    total_activities INTEGER,
    synced_at TEXT NOT NULL
);

-- Durable garmin_workout_id → planned type mapping. Written at materialize
-- time and refreshed from plan.json on every sync, so completed activities
-- can be classified even after plan.json is replaced by the next block.
CREATE TABLE IF NOT EXISTS workout_type_map (
    garmin_workout_id INTEGER PRIMARY KEY,
    planned_type TEXT NOT NULL,
    workout_name TEXT,
    plan_name TEXT,
    planned_date TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
    activity_id INTEGER PRIMARY KEY,
    time_json TEXT NOT NULL,
    hr_json TEXT NOT NULL,
    elevation_json TEXT,
    speed_json TEXT,
    distance_json TEXT,
    cadence_json TEXT,
    perf_condition_json TEXT,
    grade_adj_speed_json TEXT,
    extras_fetched_at TEXT,
    -- Separate marker for perf_condition_json / grade_adj_speed_json.
    -- extras_fetched_at can't do double duty: rows backfilled for
    -- elevation/speed/distance/cadence already have it set, so reusing it
    -- would silently skip them here.
    perf_fetched_at TEXT,
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

-- Conditions at the activity. PRIMARY SOURCE = Open-Meteo ERA5 reanalysis
-- (hourly, ~9 km grid) at the activity's own start coordinates, start hour;
-- precip_mm sums the hours the activity spanned, precip_prev_6h_mm the six
-- hours before. Garmin's own per-activity record (nearest METAR station)
-- is kept in the garmin_* / station_* columns for audit and used only as a
-- fallback: the station Garmin picks can be far from the route, and
-- compared against ERA5 some stations disagree by several degrees and
-- report rain on dry evenings. A row exists for every activity checked,
-- with NULL temp_c for indoor / no-GPS ones, so the backfill terminates.
CREATE TABLE IF NOT EXISTS activity_weather (
    activity_id INTEGER PRIMARY KEY,
    temp_c REAL,
    apparent_temp_c REAL,
    dew_point_c REAL,
    relative_humidity REAL,
    wind_speed_kmh REAL,
    wind_gust_kmh REAL,
    wind_direction_deg REAL,
    wind_compass TEXT,
    conditions TEXT,
    station_id TEXT,
    station_name TEXT,
    station_lat REAL,
    station_lon REAL,
    observed_at TEXT,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

CREATE TABLE IF NOT EXISTS laps (
    activity_id INTEGER PRIMARY KEY,
    laps_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    -- Set once the per-lap running-dynamics fields are present in laps_json.
    -- Rows cached before they existed have it NULL — backfill_laps refetches.
    dynamics_fetched_at TEXT,
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Garmin's own VO2max estimate, one row per day it was recomputed. Sparse
-- by design: Garmin only writes a value on days with a qualifying activity,
-- so gaps mean "no new estimate", not "missing data". vo2max is the precise
-- one-decimal value; vo2max_rounded is the integer the watch displays.
CREATE TABLE IF NOT EXISTS vo2max_daily (
    date TEXT PRIMARY KEY,
    vo2max REAL,
    vo2max_rounded REAL,
    fitness_age REAL,
    synced_at TEXT NOT NULL
);

-- Garmin's running lactate-threshold estimate (HR, speed) and running FTP,
-- one row per day the model rewrote a value. Sparse like vo2max_daily.
-- lt_speed_m_s is the API value ×10 (Garmin serves 0.3417 for 3.417 m/s —
-- verified against the threshold pace the watch displays). Model output:
-- the LT2 in the user profile stays the anchor; this is trend material.
CREATE TABLE IF NOT EXISTS lactate_threshold_daily (
    date TEXT PRIMARY KEY,
    lt_hr INTEGER,
    lt_speed_m_s REAL,
    lt_pace_s_per_km REAL,
    ftp_w INTEGER,
    synced_at TEXT NOT NULL
);

-- Garmin's race-time predictions, dense (one row per day, seconds).
CREATE TABLE IF NOT EXISTS race_predictions_daily (
    date TEXT PRIMARY KEY,
    time_5k_s INTEGER,
    time_10k_s INTEGER,
    time_half_s INTEGER,
    time_marathon_s INTEGER,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wellness_daily (
    date TEXT PRIMARY KEY,
    resting_hr INTEGER,
    hrv_overnight_avg INTEGER,
    hrv_weekly_avg INTEGER,
    hrv_status TEXT,
    hrv_baseline_low INTEGER,
    hrv_baseline_upper INTEGER,
    sleep_seconds INTEGER,
    sleep_score INTEGER,
    sleep_deep_s INTEGER,
    sleep_rem_s INTEGER,
    sleep_light_s INTEGER,
    sleep_awake_s INTEGER,
    avg_stress INTEGER,
    body_battery_high INTEGER,
    body_battery_low INTEGER,
    body_battery_at_wake INTEGER,
    respiration_avg INTEGER,
    spo2_avg INTEGER,
    recovery_time_hours INTEGER,
    synced_at TEXT NOT NULL
);
"""

# Columns added after initial schema — applied via ALTER TABLE on existing DBs.
_WELLNESS_MIGRATION_COLUMNS = {
    "sleep_score": "INTEGER",
    "sleep_deep_s": "INTEGER",
    "sleep_rem_s": "INTEGER",
    "sleep_light_s": "INTEGER",
    "sleep_awake_s": "INTEGER",
    "respiration_avg": "INTEGER",
    "spo2_avg": "INTEGER",
    "recovery_time_hours": "INTEGER",
    # Garmin training status as of that day (get_training_status is
    # date-addressable, so history backfills). Load figures are Garmin's:
    # acute = 7-day, chronic = 28-day, acwr_percent = acute/chronic position
    # inside Garmin's optimal tunnel (not the ratio itself — that is
    # load_acwr). load_4w_* are the monthly focus buckets behind the
    # "anaerobic shortage"-style balance phrase.
    "training_status_code": "INTEGER",
    "training_status": "TEXT",
    "acwr_percent": "INTEGER",
    "acwr_status": "TEXT",
    "load_acwr": "REAL",
    "load_acute": "INTEGER",
    "load_chronic": "INTEGER",
    "load_chronic_min": "REAL",
    "load_chronic_max": "REAL",
    "load_4w_aerobic_low": "REAL",
    "load_4w_aerobic_high": "REAL",
    "load_4w_anaerobic": "REAL",
    "load_balance": "TEXT",
    # Morning training-readiness score/level (first reading of the day).
    "training_readiness_score": "INTEGER",
    "training_readiness_level": "TEXT",
    # Marker so backfill_training_status terminates on days Garmin has nothing.
    "training_status_fetched_at": "TEXT",
}

_ACTIVITY_MIGRATION_COLUMNS = {
    "associated_workout_id": "INTEGER",
    "planned_type": "TEXT",
    "training_effect_label": "TEXT",
    "workout_rpe": "INTEGER",
    "workout_feel": "INTEGER",
    "workout_compliance": "INTEGER",
    "detail_fetched_at": "TEXT",
    "start_lat": "REAL",
    "start_lon": "REAL",
    "gear_uuid": "TEXT",
    "gear_name": "TEXT",
    # Marks an activity as processed for gear — set even when no gear is
    # assigned, so the backfill terminates instead of re-fetching gearless
    # activities forever.
    "gear_fetched_at": "TEXT",
    # Running dynamics / power / physiology off the activity list payload.
    "avg_cadence_spm": "REAL",
    "max_cadence_spm": "REAL",
    "avg_ground_contact_ms": "REAL",
    "avg_gct_balance_pct": "REAL",
    "avg_vertical_osc_cm": "REAL",
    "avg_vertical_ratio_pct": "REAL",
    "avg_stride_length_cm": "REAL",
    "avg_power_w": "REAL",
    "max_power_w": "REAL",
    "norm_power_w": "REAL",
    "avg_respiration_rate": "REAL",
    "avg_grade_adj_speed": "REAL",
    "steps": "INTEGER",
    "activity_training_load": "REAL",
    "aerobic_training_effect": "REAL",
    "anaerobic_training_effect": "REAL",
    "vo2max_at_activity": "REAL",
    "perf_cond_first": "REAL",
    "perf_cond_min": "REAL",
    "perf_cond_max": "REAL",
    "perf_cond_avg": "REAL",
    "perf_cond_last": "REAL",
    # Marks an activity as processed for the metric columns above — set even
    # when the sport reports none of them, so the backfill terminates.
    "metrics_fetched_at": "TEXT",
    # Set whenever the detail fetch ran with the start_lat/lon fields present,
    # so backfill_coordinates skips indoor rows instead of re-asking forever.
    "coords_checked_at": "TEXT",
}

_STREAMS_MIGRATION_COLUMNS = {
    "elevation_json": "TEXT",
    "speed_json": "TEXT",
    "distance_json": "TEXT",
    "cadence_json": "TEXT",
    "perf_condition_json": "TEXT",
    "grade_adj_speed_json": "TEXT",
    # Markers: a stream is "processed" once fetched, even when a metric is
    # legitimately absent (treadmill = no elevation/GPS, walk = no
    # performance condition), so the backfills terminate instead of
    # re-fetching those rows forever.
    "extras_fetched_at": "TEXT",
    "perf_fetched_at": "TEXT",
}


_LAPS_MIGRATION_COLUMNS = {
    "dynamics_fetched_at": "TEXT",
}

_WEATHER_MIGRATION_COLUMNS = {
    "precip_mm": "REAL",
    "precip_prev_6h_mm": "REAL",
    "cloud_cover_pct": "REAL",
    "weather_code": "INTEGER",
    "source": "TEXT",             # 'open-meteo' | 'garmin-station'
    "location_source": "TEXT",    # 'activity_gps' | 'garmin_station'
    "lat": "REAL",
    "lon": "REAL",
    "garmin_temp_c": "REAL",
    "garmin_conditions": "TEXT",
}


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        for table, columns in (
            ("wellness_daily", _WELLNESS_MIGRATION_COLUMNS),
            ("activities", _ACTIVITY_MIGRATION_COLUMNS),
            ("streams", _STREAMS_MIGRATION_COLUMNS),
            ("laps", _LAPS_MIGRATION_COLUMNS),
            ("activity_weather", _WEATHER_MIGRATION_COLUMNS),
        ):
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, col_type in columns.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        # Created after the gear_uuid column is guaranteed to exist (above).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activities_gear ON activities(gear_uuid)"
        )


def _get_last_sync() -> Optional[datetime]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key='last_sync_at'"
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None


def _set_last_sync(when: datetime) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync_at', ?)",
            (when.isoformat(),),
        )


def _activity_exists(act_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT 1 FROM activities WHERE id = ?", (act_id,)
        ).fetchone() is not None


# ─── Garmin activity API ──────────────────────────────────────────────

def _garmin_list_activities(
    garmin_client,
    since: datetime,
    until: Optional[datetime] = None,
) -> list[dict]:
    """Return activities in (since, until] window, newest-first from Garmin."""
    all_acts: list[dict] = []
    since_ts = since.timestamp()
    until_ts = until.timestamp() if until else None
    start = 0
    batch_size = 100
    while True:
        batch = garmin_client.get_activities(start, batch_size)
        if not batch:
            break
        done = False
        for act in batch:
            gmt = act.get("startTimeGMT", "")
            try:
                ts = datetime.strptime(gmt, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                continue
            if ts < since_ts:
                done = True
                break
            if until_ts is None or ts <= until_ts:
                all_acts.append(act)
        if done or len(batch) < batch_size:
            break
        start += batch_size
    return all_acts


# Activity-summary metric columns ← keys on the Garmin activity LIST record.
# Everything here rides along on a call sync already makes, so populating
# these costs zero extra requests.
_ACTIVITY_METRIC_KEYS = {
    "avg_cadence_spm": "averageRunningCadenceInStepsPerMinute",
    "max_cadence_spm": "maxRunningCadenceInStepsPerMinute",
    "avg_ground_contact_ms": "avgGroundContactTime",
    "avg_gct_balance_pct": "avgGroundContactBalance",
    "avg_vertical_osc_cm": "avgVerticalOscillation",
    "avg_vertical_ratio_pct": "avgVerticalRatio",
    "avg_stride_length_cm": "avgStrideLength",
    "avg_power_w": "avgPower",
    "max_power_w": "maxPower",
    "norm_power_w": "normPower",
    "avg_respiration_rate": "avgRespirationRate",
    "avg_grade_adj_speed": "avgGradeAdjustedSpeed",
    "steps": "steps",
    "activity_training_load": "activityTrainingLoad",
    "aerobic_training_effect": "aerobicTrainingEffect",
    "anaerobic_training_effect": "anaerobicTrainingEffect",
    # Garmin's VO2max as of this activity. Integer-resolution here; the
    # one-decimal value lives in vo2max_daily, which is what trend maths
    # should use. Kept per-activity so a session can be attributed even on
    # days Garmin wrote no daily row.
    "vo2max_at_activity": "vO2MaxValue",
}


def _activity_metric_values(act: dict) -> list:
    """Metric column values, in _ACTIVITY_METRIC_KEYS order, from a list record."""
    return [act.get(src) for src in _ACTIVITY_METRIC_KEYS.values()]


def _store_activity_metrics(act_id: int, act: dict) -> None:
    """UPDATE the metric columns for an already-cached activity."""
    sets = ", ".join(f"{col} = ?" for col in _ACTIVITY_METRIC_KEYS)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE activities SET {sets}, metrics_fetched_at = ? WHERE id = ?",
            (*_activity_metric_values(act),
             datetime.now(timezone.utc).isoformat(), act_id),
        )


def _garmin_get_stream(garmin_client, activity_id: int) -> Optional[dict]:
    """Fetch ~2s-resolution HR + elapsed stream (plus elevation, pace, distance,
    cadence, performance condition, grade-adjusted speed) from Garmin
    activity details.

    Returns parallel arrays, all the same length, keyed:
      time (elapsed s), heartrate (bpm), elevation (m), speed (m/s),
      distance (cumulative m), cadence (spm), perf_condition
      (dimensionless, Garmin "ytelseskondisjon"), grade_adj_speed (m/s).
    Samples are kept where HR and elapsed are both present; the extra
    metrics are aligned to those kept samples (None when a metric's
    descriptor is absent or its value is missing for a sample). maxchart=6000
    covers ~3.3 hours at 2s/sample. Returns None if there's no HR stream.

    Garmin only starts emitting performance condition 6-20 minutes into a
    run, so the leading samples of `perf_condition` are legitimately None —
    that gap is the metric's warm-up, not missing data.

    The other running-dynamics streams Garmin exposes here (vertical
    oscillation / ratio, ground contact time and balance, stride length,
    power) are deliberately NOT stored per-sample: the coaching signal lives
    at lap and session level, which the laps rows and the activity summary
    columns already carry at a fraction of the size.
    """
    try:
        details = garmin_client.get_activity_details(str(activity_id), maxchart=6000)
    except Exception:
        return None
    descriptors = {
        d["key"]: d["metricsIndex"]
        for d in (details.get("metricDescriptors") or [])
    }
    metrics = details.get("activityDetailMetrics") or []
    hr_idx = descriptors.get("directHeartRate")
    elapsed_idx = descriptors.get("sumElapsedDuration")
    if hr_idx is None or elapsed_idx is None or not metrics:
        return None

    # Optional extra metrics — aligned to the kept HR samples.
    extra_idx = {
        "elevation": descriptors.get("directElevation"),
        "speed": descriptors.get("directSpeed"),
        "distance": descriptors.get("sumDistance"),
        "cadence": descriptors.get("directRunCadence"),
        "perf_condition": descriptors.get("directPerformanceCondition"),
        "grade_adj_speed": descriptors.get("directGradeAdjustedSpeed"),
    }

    elapsed_list: list[float] = []
    hr_list: list[float] = []
    extra: dict[str, list] = {k: [] for k in extra_idx}
    for m in metrics:
        vals = m.get("metrics", [])
        hr = vals[hr_idx] if hr_idx < len(vals) else None
        elapsed = vals[elapsed_idx] if elapsed_idx < len(vals) else None
        if hr is None or elapsed is None:
            continue
        elapsed_list.append(elapsed)
        hr_list.append(hr)
        for key, idx in extra_idx.items():
            v = vals[idx] if (idx is not None and idx < len(vals)) else None
            extra[key].append(v)

    if not hr_list:
        return None
    return {"time": elapsed_list, "heartrate": hr_list, **extra}


_STREAM_EXTRA_KEYS = (
    "elevation", "speed", "distance", "cadence",
    "perf_condition", "grade_adj_speed",
)


def _stream_extra_json(stream: dict) -> tuple:
    """Serialize the optional stream arrays to JSON in column order. Stores
    NULL when a whole array is missing or entirely None (e.g. an indoor run
    with no elevation, or a walk with no performance condition)."""
    out = []
    for key in _STREAM_EXTRA_KEYS:
        arr = stream.get(key)
        if arr and any(v is not None for v in arr):
            out.append(json.dumps(arr))
        else:
            out.append(None)
    return tuple(out)


def _perf_condition_summary(stream: Optional[dict]) -> dict:
    """Condense a performance-condition array into the five stored columns.

    `first` is the first value Garmin emitted — the number that pops up on
    the watch 6-20 min in — and `last` the final one, so `last - first` is
    the within-session drift (a durability / decoupling proxy: holding or
    rising through a long run is good, sagging is fatigue). Returns all-None
    when the activity has no performance-condition samples.
    """
    vals = [v for v in ((stream or {}).get("perf_condition") or [])
            if v is not None]
    if not vals:
        return {k: None for k in
                ("first", "min", "max", "avg", "last")}
    return {
        "first": float(vals[0]),
        "min": float(min(vals)),
        "max": float(max(vals)),
        "avg": round(sum(vals) / len(vals), 2),
        "last": float(vals[-1]),
    }


def _store_stream(activity_id: int, stream: dict) -> None:
    """Upsert a stream row and the derived performance-condition columns.

    Both writes happen here so the stored array and the summary columns on
    `activities` can never drift apart.
    """
    now = datetime.now(timezone.utc).isoformat()
    pc = _perf_condition_summary(stream)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO streams (activity_id, time_json, hr_json, "
            "elevation_json, speed_json, distance_json, cadence_json, "
            "perf_condition_json, grade_adj_speed_json, extras_fetched_at, "
            "perf_fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (activity_id, json.dumps(stream["time"]),
             json.dumps(stream["heartrate"]),
             *_stream_extra_json(stream), now, now),
        )
        conn.execute(
            "UPDATE activities SET perf_cond_first=?, perf_cond_min=?, "
            "perf_cond_max=?, perf_cond_avg=?, perf_cond_last=? WHERE id=?",
            (pc["first"], pc["min"], pc["max"], pc["avg"], pc["last"],
             activity_id),
        )


def _garmin_get_laps(garmin_client, activity_id: int) -> list[dict]:
    """Fetch lapDTOs from Garmin and normalise to our internal field names.

    Normalised lap dict:
      lap_index, average_heartrate, max_heartrate, distance, elapsed_time,
      moving_time, average_speed, start_date_local, intensityType,
      plus the per-lap running dynamics Garmin reports on the same payload:
      avg_cadence_spm, ground_contact_ms, gct_balance_pct, vertical_osc_cm,
      vertical_ratio_pct, stride_length_cm, avg_power_w, norm_power_w,
      avg_grade_adj_speed, avg_respiration_rate (None where unreported).

    The dynamics come free with the lap fetch — no extra API call — and are
    what makes per-rep form decay across an interval session visible.
    """
    try:
        splits = garmin_client.get_activity_splits(str(activity_id))
        raw_laps = splits.get("lapDTOs") or []
    except Exception:
        return []
    out = []
    for lap in raw_laps:
        speed = lap.get("averageMovingSpeed") or lap.get("averageSpeed") or 0
        out.append({
            "lap_index": lap.get("lapIndex"),
            "average_heartrate": lap.get("averageHR"),
            "max_heartrate": lap.get("maxHR"),
            "distance": lap.get("distance"),
            "elapsed_time": lap.get("elapsedDuration"),
            "moving_time": lap.get("movingDuration") or lap.get("elapsedDuration"),
            "average_speed": speed,
            "start_date_local": (lap.get("startTimeGMT") or "").replace(".0", ""),
            "intensityType": lap.get("intensityType"),
            "avg_cadence_spm": lap.get("averageRunCadence"),
            "ground_contact_ms": lap.get("groundContactTime"),
            "gct_balance_pct": lap.get("groundContactBalanceLeft"),
            "vertical_osc_cm": lap.get("verticalOscillation"),
            "vertical_ratio_pct": lap.get("verticalRatio"),
            "stride_length_cm": lap.get("strideLength"),
            "avg_power_w": lap.get("averagePower"),
            "norm_power_w": lap.get("normalizedPower"),
            "avg_grade_adj_speed": lap.get("avgGradeAdjustedSpeed"),
            "avg_respiration_rate": lap.get("avgRespirationRate"),
        })
    return out


def _cached_laps(activity_id: int) -> Optional[list[dict]]:
    """Read laps from cache. None if never fetched, [] if fetched-but-no-laps."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT laps_json FROM laps WHERE activity_id = ?", (activity_id,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _store_laps(activity_id: int, laps: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO laps (activity_id, laps_json, fetched_at, "
            "dynamics_fetched_at) VALUES (?, ?, ?, ?)",
            (activity_id, json.dumps(laps), now, now),
        )


# ─── Sync ─────────────────────────────────────────────────────────────
def clear_activity_cache() -> None:
    """Drop and recreate activities, streams, and laps tables.

    Wellness data is preserved. Call before a full Garmin re-sync to remove
    stale or duplicate entries from previous syncs.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM laps")
        conn.execute("DELETE FROM streams")
        conn.execute("DELETE FROM activities")
        conn.execute("DELETE FROM sync_state WHERE key='last_sync_at'")


# ─── Workout linkage: planned type ↔ completed activity ──────────────
PLAN_PATH = ROOT / "coach_data" / "plan.json"


def record_workout_types(entries: list[dict], plan_name: Optional[str] = None) -> int:
    """Upsert garmin_workout_id → planned_type rows into workout_type_map.

    Each entry needs `garmin_workout_id` and `type`; `name` and `date` are
    optional. Called from materialize_plan when workouts are created, and
    from sync as a refresh of whatever plan.json currently holds.
    """
    _init_db()
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with sqlite3.connect(DB_PATH) as conn:
        for e in entries:
            wid = e.get("garmin_workout_id")
            wtype = e.get("type")
            if not wid or not wtype or wtype in ("rest", "strength"):
                continue
            conn.execute(
                """
                INSERT INTO workout_type_map
                    (garmin_workout_id, planned_type, workout_name, plan_name,
                     planned_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(garmin_workout_id) DO UPDATE SET
                    planned_type=excluded.planned_type,
                    workout_name=excluded.workout_name,
                    plan_name=excluded.plan_name,
                    planned_date=excluded.planned_date,
                    updated_at=excluded.updated_at
                """,
                (wid, wtype, e.get("name"), plan_name, e.get("date"), now),
            )
            count += 1
    return count


def _refresh_workout_type_map() -> None:
    """Best-effort refresh of workout_type_map from the current plan.json."""
    try:
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        record_workout_types(plan.get("workouts", []), plan.get("block_name"))
    except (OSError, json.JSONDecodeError):
        pass


def _planned_type_for(workout_id: Optional[int]) -> Optional[str]:
    if not workout_id:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT planned_type FROM workout_type_map WHERE garmin_workout_id = ?",
            (workout_id,),
        ).fetchone()
    return row[0] if row else None


def _fetch_detail_fields(garmin_client, act_id: int) -> dict:
    """Per-activity detail fields the list API doesn't carry.

    associatedWorkoutId links the activity to the workout template it
    executed; RPE/feel are the watch's post-workout self-evaluation
    prompts; compliance is Garmin's how-closely-you-followed-it score.
    """
    detail = garmin_client.get_activity(act_id) or {}
    meta = detail.get("metadataDTO") or {}
    summ = detail.get("summaryDTO") or {}
    return {
        "associated_workout_id": meta.get("associatedWorkoutId"),
        "workout_rpe": summ.get("directWorkoutRpe"),
        "workout_feel": summ.get("directWorkoutFeel"),
        "workout_compliance": summ.get("directWorkoutComplianceScore"),
        "training_effect_label": summ.get("trainingEffectLabel"),
        "start_lat": summ.get("startLatitude"),
        "start_lon": summ.get("startLongitude"),
    }


def _store_detail_fields(act_id: int, fields: dict) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE activities SET
                associated_workout_id = ?,
                planned_type = ?,
                workout_rpe = ?,
                workout_feel = ?,
                workout_compliance = ?,
                training_effect_label = COALESCE(?, training_effect_label),
                detail_fetched_at = ?,
                start_lat = COALESCE(?, start_lat),
                start_lon = COALESCE(?, start_lon),
                coords_checked_at = ?
            WHERE id = ?
            """,
            (
                fields.get("associated_workout_id"),
                _planned_type_for(fields.get("associated_workout_id")),
                fields.get("workout_rpe"),
                fields.get("workout_feel"),
                fields.get("workout_compliance"),
                fields.get("training_effect_label"),
                datetime.now(timezone.utc).isoformat(),
                fields.get("start_lat"),
                fields.get("start_lon"),
                datetime.now(timezone.utc).isoformat(),
                act_id,
            ),
        )


# ─── Weather at the activity ──────────────────────────────────────────
# Primary source: Open-Meteo ERA5 archive at the activity's start coordinates
# (forecast API with past_days for the last week, where the archive lags).
# Garmin's per-activity station record is fetched too — it is the only
# indoor/outdoor signal we have, gives a fallback location, and is kept for
# audit — but its temperatures are NOT trusted (see the schema note).
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_OM_HOURLY = ("temperature_2m,apparent_temperature,dew_point_2m,relative_humidity_2m,"
              "precipitation,wind_speed_10m,wind_gusts_10m,cloud_cover,weather_code")
_ARCHIVE_LAG_DAYS = 7
_WET_WORDS = ("rain", "shower", "drizzle", "snow", "sleet", "thunder", "hail")
_WMO = {0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
        56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
        65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow",
        73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light showers",
        81: "Showers", 82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers",
        95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail"}


def _f_to_c(f: Optional[float]) -> Optional[float]:
    return round((f - 32) / 1.8, 1) if f is not None else None


def _fetch_garmin_weather(garmin_client, act_id: int) -> dict:
    """Garmin's weather record for one activity (nearest METAR station,
    served in °F). Indoor / no-GPS activities return all-null fields."""
    w = garmin_client.get_activity_weather(act_id) or {}
    station = w.get("weatherStationDTO") or {}
    wtype = w.get("weatherTypeDTO") or {}
    has_data = w.get("temp") is not None
    return {
        "garmin_temp_c": _f_to_c(w.get("temp")),
        "garmin_conditions": wtype.get("desc"),
        "station_id": station.get("id"),
        "station_name": station.get("name"),
        "station_lat": w.get("latitude") if has_data else None,
        "station_lon": w.get("longitude") if has_data else None,
        "observed_at": w.get("issueDate"),
        # Fallback values, used only when Open-Meteo has nothing.
        "_fallback": {
            "temp_c": _f_to_c(w.get("temp")),
            "apparent_temp_c": _f_to_c(w.get("apparentTemp")),
            "dew_point_c": _f_to_c(w.get("dewPoint")),
            "relative_humidity": w.get("relativeHumidity"),
            "wind_speed_kmh": w.get("windSpeed"),
            "wind_gust_kmh": w.get("windGust"),
            "wind_direction_deg": w.get("windDirection"),
            "wind_compass": w.get("windDirectionCompassPoint"),
            "conditions": wtype.get("desc"),
        } if has_data else None,
    }


def _om_hourly(lat: float, lon: float, d0: date, d1: date) -> dict:
    """Hourly Open-Meteo series for [d0, d1] (local time keys 'YYYY-MM-DDTHH:00').
    Archive for dates older than the lag, forecast API (past_days) for recent
    ones; chunks the archive into ≤365-day requests."""
    import requests
    out: dict[str, dict] = {}
    cutoff = date.today() - timedelta(days=_ARCHIVE_LAG_DAYS)

    def _pull(url, a, b):
        r = requests.get(url, params=dict(
            latitude=lat, longitude=lon, start_date=a.isoformat(), end_date=b.isoformat(),
            hourly=_OM_HOURLY, wind_speed_unit="kmh", timezone="auto"), timeout=60)
        r.raise_for_status()
        h = r.json().get("hourly") or {}
        keys = [k for k in h if k != "time"]
        for i, t in enumerate(h.get("time") or []):
            out[t] = {k: h[k][i] for k in keys}

    a = d0
    while a <= min(d1, cutoff - timedelta(days=1)):
        b = min(a + timedelta(days=364), d1, cutoff - timedelta(days=1))
        _pull(OPEN_METEO_ARCHIVE_URL, a, b)
        a = b + timedelta(days=1)
    if d1 >= cutoff:
        _pull(OPEN_METEO_FORECAST_URL, max(d0, cutoff), d1)
    return out


def _om_at(series: dict, start_local: str, duration_s: Optional[float]) -> Optional[dict]:
    """Conditions for one activity from an hourly series: start-hour values,
    precipitation summed over the hours the activity spanned and over the
    six hours before it."""
    key = start_local[:13] + ":00"
    h0 = series.get(key)
    if not h0 or h0.get("temperature_2m") is None:
        return None
    start = datetime.fromisoformat(start_local[:19])
    hours = max(1, int((start.minute * 60 + (duration_s or 0)) // 3600) + 1)
    precip = 0.0
    for i in range(hours):
        v = series.get((start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00"), {}).get("precipitation")
        precip += v or 0.0
    prev = 0.0
    for i in range(1, 7):
        v = series.get((start - timedelta(hours=i)).strftime("%Y-%m-%dT%H:00"), {}).get("precipitation")
        prev += v or 0.0
    code = h0.get("weather_code")
    return {
        "temp_c": h0.get("temperature_2m"),
        "apparent_temp_c": h0.get("apparent_temperature"),
        "dew_point_c": h0.get("dew_point_2m"),
        "relative_humidity": h0.get("relative_humidity_2m"),
        "wind_speed_kmh": h0.get("wind_speed_10m"),
        "wind_gust_kmh": h0.get("wind_gusts_10m"),
        "wind_direction_deg": None,
        "wind_compass": None,
        "cloud_cover_pct": h0.get("cloud_cover"),
        "weather_code": code,
        "conditions": _WMO.get(int(code)) if code is not None else None,
        "precip_mm": round(precip, 1),
        "precip_prev_6h_mm": round(prev, 1),
    }


def _store_activity_weather(act_id: int, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if not k.startswith("_")}
    cols = list(fields.keys()) + ["fetched_at"]
    vals = list(fields.values()) + [datetime.now(timezone.utc).isoformat()]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO activity_weather (activity_id, {', '.join(cols)}) "
            f"VALUES (?{', ?' * len(cols)})",
            [act_id, *vals],
        )


def _weather_row(garmin: dict, om: Optional[dict], lat, lon, loc_source) -> dict:
    row = {k: v for k, v in garmin.items() if k != "_fallback"}
    if om:
        row.update(om)
        row["source"] = "open-meteo"
    elif garmin.get("_fallback"):
        row.update(garmin["_fallback"])
        row["source"] = "garmin-station"
    else:
        row["source"] = None
    row["lat"], row["lon"], row["location_source"] = lat, lon, loc_source
    return row


def _activity_location(row) -> tuple:
    """(lat, lon, source) from the activity's own GPS start, else
    (None, None, None). Callers fall back to Garmin's station coordinates."""
    if row["start_lat"] is not None and row["start_lon"] is not None:
        return row["start_lat"], row["start_lon"], "activity_gps"
    return None, None, None


def fetch_and_store_weather(garmin_client, act_id: int) -> dict:
    """Weather for one activity: Garmin record for indoor detection + audit,
    Open-Meteo at the activity's coordinates for the values. Used by run_sync
    for each new cardio activity."""
    garmin = _fetch_garmin_weather(garmin_client, act_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        a = conn.execute(
            "SELECT start_date_local, moving_time_s, start_lat, start_lon "
            "FROM activities WHERE id = ?", (act_id,)).fetchone()
    lat, lon, src = _activity_location(a)
    if lat is None and garmin.get("station_lat"):
        lat, lon, src = garmin["station_lat"], garmin["station_lon"], "garmin_station"
    om = None
    indoor = garmin.get("_fallback") is None and a["start_lat"] is None
    if lat is not None and not indoor:
        try:
            d = date.fromisoformat(a["start_date_local"][:10])
            om = _om_at(_om_hourly(lat, lon, d, d), a["start_date_local"], a["moving_time_s"])
        except Exception:
            om = None
    row = _weather_row(garmin, om, lat, lon, src)
    _store_activity_weather(act_id, row)
    return row


def backfill_coordinates(garmin_client, max_activities: int = 400) -> dict:
    """Re-fetch detail fields for cached cardio activities whose row predates
    the start_lat/lon columns (one API call each, newest first). Indoor rows
    get coords_checked_at set and are not asked again."""
    _init_db()
    cardio = ", ".join("?" * len(_CARDIO_TYPES))
    with sqlite3.connect(DB_PATH) as conn:
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM activities WHERE start_lat IS NULL AND coords_checked_at IS NULL "
            f"AND sport_type IN ({cardio}) ORDER BY start_date_local DESC LIMIT ?",
            (*_CARDIO_TYPES, max_activities))]
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM activities WHERE start_lat IS NULL AND coords_checked_at IS NULL "
            f"AND sport_type IN ({cardio})", tuple(_CARDIO_TYPES)).fetchone()[0] - len(ids)
    fetched = with_coords = 0
    errors: list[str] = []
    for act_id in ids:
        try:
            f = _fetch_detail_fields(garmin_client, act_id)
            _store_detail_fields(act_id, f)
            fetched += 1
            if f.get("start_lat") is not None:
                with_coords += 1
        except Exception as e:
            errors.append(f"detail {act_id}: {type(e).__name__}: {e}")
    return {"details_fetched": fetched, "with_coords": with_coords,
            "remaining": remaining, "errors": errors}


def backfill_weather(garmin_client, max_activities: int = 500,
                     refresh_open_meteo: bool = False) -> dict:
    """Fill activity_weather for cached cardio activities. Garmin's record is
    fetched per activity (one call each) for indoor detection and audit;
    Open-Meteo values are fetched in bulk — activities are clustered by
    location (~5 km) and each cluster's whole date span is pulled in ≤365-day
    hourly requests, so a two-year history costs ~20 HTTP requests.

    refresh_open_meteo=True also re-derives rows whose source is not yet
    'open-meteo' (e.g. rows stored from the Garmin station only).
    """
    _init_db()
    cardio = ", ".join("?" * len(_CARDIO_TYPES))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        todo = conn.execute(
            f"""
            SELECT a.id, a.start_date_local, a.moving_time_s, a.start_lat, a.start_lon,
                   w.activity_id AS has_row, w.source, w.station_lat, w.station_lon,
                   w.garmin_temp_c, w.garmin_conditions, w.station_id, w.station_name,
                   w.observed_at, w.temp_c AS old_temp_c
            FROM activities a LEFT JOIN activity_weather w ON w.activity_id = a.id
            WHERE a.sport_type IN ({cardio})
              AND (w.activity_id IS NULL
                   OR (? AND COALESCE(w.source, '') != 'open-meteo'
                       AND (w.temp_c IS NOT NULL OR a.start_lat IS NOT NULL)))
            ORDER BY a.start_date_local DESC LIMIT ?
            """, (*_CARDIO_TYPES, 1 if refresh_open_meteo else 0, max_activities)).fetchall()

    errors: list[str] = []
    garmin_calls = 0
    per_act: dict[int, dict] = {}
    for a in todo:
        if a["has_row"] is None:
            try:
                g = _fetch_garmin_weather(garmin_client, a["id"])
                garmin_calls += 1
            except Exception as e:
                errors.append(f"garmin weather {a['id']}: {type(e).__name__}: {e}")
                continue
        else:
            g = {"garmin_temp_c": a["garmin_temp_c"], "garmin_conditions": a["garmin_conditions"],
                 "station_id": a["station_id"], "station_name": a["station_name"],
                 "station_lat": a["station_lat"], "station_lon": a["station_lon"],
                 "observed_at": a["observed_at"], "_fallback": None}
        lat, lon, src = _activity_location(a)
        if lat is None and g.get("station_lat"):
            lat, lon, src = g["station_lat"], g["station_lon"], "garmin_station"
        indoor = g.get("_fallback") is None and a["start_lat"] is None and a["old_temp_c"] is None
        per_act[a["id"]] = {"row": a, "garmin": g, "lat": lat, "lon": lon, "src": src,
                            "indoor": indoor or lat is None}

    # Cluster outdoor activities by location and pull each cluster's span once.
    clusters: dict[tuple, list[int]] = {}
    for act_id, info in per_act.items():
        if info["indoor"]:
            continue
        clusters.setdefault((round(info["lat"] / 0.05) * 0.05, round(info["lon"] / 0.05) * 0.05), []).append(act_id)

    om_requests = 0
    om_by_act: dict[int, Optional[dict]] = {}
    for (clat, clon), ids in clusters.items():
        days = [date.fromisoformat(per_act[i]["row"]["start_date_local"][:10]) for i in ids]
        try:
            series = _om_hourly(round(clat, 3), round(clon, 3), min(days), max(days))
            om_requests += 1 + (max(days) - min(days)).days // 365
        except Exception as e:
            errors.append(f"open-meteo cluster {clat:.2f},{clon:.2f}: {type(e).__name__}: {e}")
            continue
        for i in ids:
            r = per_act[i]["row"]
            om_by_act[i] = _om_at(series, r["start_date_local"], r["moving_time_s"])

    stored = with_om = 0
    for act_id, info in per_act.items():
        om = om_by_act.get(act_id)
        if not info["indoor"] and om is None and act_id not in om_by_act:
            continue  # cluster fetch failed — leave for next round
        row = _weather_row(info["garmin"], om, info["lat"], info["lon"], info["src"])
        _store_activity_weather(act_id, row)
        stored += 1
        if row.get("source") == "open-meteo":
            with_om += 1

    with sqlite3.connect(DB_PATH) as conn:
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM activities a LEFT JOIN activity_weather w ON w.activity_id = a.id "
            f"WHERE w.activity_id IS NULL AND a.sport_type IN ({cardio})", tuple(_CARDIO_TYPES)).fetchone()[0]
    return {"weather_stored": stored, "with_open_meteo": with_om,
            "garmin_calls": garmin_calls, "open_meteo_requests": om_requests,
            "remaining_without_weather": remaining, "errors": errors}


def cached_weather(activity_id: int) -> Optional[dict]:
    """The stored weather row for one activity as a dict, or None if the
    activity has not been weather-checked. A checked indoor activity
    returns a dict whose `temp_c` is None."""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM activity_weather WHERE activity_id = ?", (activity_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d.pop("activity_id", None)
    d["wet"] = _is_wet(d)
    return d


def _is_wet(w: dict) -> Optional[bool]:
    """Wet = measurable precipitation during the activity or in the six hours
    before it (Open-Meteo), else Garmin's condition text as a fallback."""
    if w.get("temp_c") is None:
        return None
    if w.get("source") == "open-meteo":
        return (w.get("precip_mm") or 0) >= 0.2 or (w.get("precip_prev_6h_mm") or 0) >= 1.0
    c = (w.get("conditions") or "").lower()
    return any(word in c for word in _WET_WORDS) if c else None


# ─── Gear (shoes etc.) ────────────────────────────────────────────────

def _gear_display_name(item: dict) -> Optional[str]:
    """Human-readable gear name from a Garmin gear record."""
    name = (
        item.get("customMakeModel")
        or item.get("displayName")
        or " ".join(
            p for p in (item.get("gearMakeName"), item.get("gearModelName")) if p
        ).strip()
    )
    return name or None


def _fetch_activity_gear(garmin_client, act_id: int) -> tuple[Optional[str], Optional[str]]:
    """Return (gear_uuid, gear_name) for an activity, preferring the shoe.

    Garmin returns a list (an activity can have multiple gear items, e.g.
    shoes + HR strap). For a running coach the shoe is what matters, so
    pick the Shoes-type item; fall back to the first item otherwise.
    Returns (None, None) when no gear is assigned.
    """
    items = garmin_client.get_activity_gear(act_id) or []
    if not items:
        return None, None
    shoe = next(
        (it for it in items if (it.get("gearTypeName") or "").lower() == "shoes"),
        items[0],
    )
    return shoe.get("uuid"), _gear_display_name(shoe)


def _store_activity_gear(act_id: int, gear_uuid: Optional[str], gear_name: Optional[str]) -> None:
    """Write gear_uuid/gear_name and stamp gear_fetched_at (always, so a
    gearless activity is not retried on every backfill)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE activities SET gear_uuid=?, gear_name=?, gear_fetched_at=? "
            "WHERE id=?",
            (gear_uuid, gear_name, datetime.now(timezone.utc).isoformat(), act_id),
        )


def sync_gear(garmin_client) -> dict:
    """Refresh the local `gear` table from Garmin (the source of truth).

    Pulls the gear library plus per-item mileage/activity-count stats so the
    gear tools can read everything locally. Upserts by uuid; one stats call
    per gear item.
    """
    _init_db()
    profile_id = str(garmin_client.get_user_profile()["id"])
    items = garmin_client.get_gear(profile_id) or []
    now = datetime.now(timezone.utc).isoformat()
    synced = 0
    errors: list[str] = []
    for it in items:
        uuid = it.get("uuid")
        if not uuid:
            continue
        total_km = None
        total_acts = None
        try:
            stats = garmin_client.get_gear_stats(uuid) or {}
            total_km = round(stats.get("totalDistance", 0) / 1000, 1)
            total_acts = stats.get("totalActivities", 0)
        except Exception as e:
            errors.append(f"gear stats {uuid}: {type(e).__name__}: {e}")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO gear (
                    uuid, name, make_model, type, status,
                    in_use_since, retired_at, total_distance_km,
                    total_activities, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uuid) DO UPDATE SET
                    name=excluded.name,
                    make_model=excluded.make_model,
                    type=excluded.type,
                    status=excluded.status,
                    in_use_since=excluded.in_use_since,
                    retired_at=excluded.retired_at,
                    total_distance_km=excluded.total_distance_km,
                    total_activities=excluded.total_activities,
                    synced_at=excluded.synced_at
                """,
                (
                    uuid,
                    _gear_display_name(it),
                    it.get("customMakeModel"),
                    it.get("gearTypeName"),
                    it.get("gearStatusName"),
                    (it.get("dateBegin") or "")[:10] or None,
                    (it.get("dateEnd") or "")[:10] or None,
                    total_km,
                    total_acts,
                    now,
                ),
            )
        synced += 1
    return {"gear_synced": synced, "errors": errors}


def backfill_gear(garmin_client, max_activities: int = 100) -> dict:
    """Fetch gear for cached activities that never got it (gear_fetched_at
    IS NULL), newest first — one API call each. Activities with no gear are
    still marked processed so they aren't retried forever.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        ids = [r[0] for r in conn.execute(
            """
            SELECT id FROM activities
            WHERE gear_fetched_at IS NULL
            ORDER BY start_date_local DESC LIMIT ?
            """,
            (max_activities,),
        )]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE gear_fetched_at IS NULL"
        ).fetchone()[0] - len(ids)

    updated = 0
    errors: list[str] = []
    for act_id in ids:
        try:
            uuid, name = _fetch_activity_gear(garmin_client, act_id)
            _store_activity_gear(act_id, uuid, name)
            if uuid:
                updated += 1
        except Exception as e:
            errors.append(f"gear {act_id}: {type(e).__name__}: {e}")

    return {
        "gear_fetched": updated,
        "remaining_without_gear": remaining,
        "errors": errors,
    }


def refresh_activity_gear(garmin_client, days: int = 14, max_activities: int = 50) -> dict:
    """Re-fetch gear for cardio activities in the trailing `days`, whether or
    not they already have a value — one API call each.

    `backfill_gear` only fills rows that were never fetched, so a shoe that
    was corrected in Garmin Connect AFTER the activity was synced (Garmin
    auto-assigns the default shoe; the user fixes it later) would otherwise
    stay wrong in the cache forever. Newest first. Returns the rows whose
    gear actually changed so the caller can see what was corrected.
    """
    _init_db()
    if not days or days <= 0:
        return {"gear_checked": 0, "gear_changed": 0, "changes": [], "errors": []}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(_CARDIO_TYPES))
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"""
            SELECT id, start_date_local, name, gear_uuid, gear_name
            FROM activities
            WHERE date(start_date_local) >= ? AND sport_type IN ({placeholders})
            ORDER BY start_date_local DESC LIMIT ?
            """,
            (cutoff, *sorted(_CARDIO_TYPES), max_activities),
        ).fetchall()

    checked = 0
    changes: list[dict] = []
    errors: list[str] = []
    for act_id, start, name, old_uuid, old_name in rows:
        try:
            uuid, gear_name = _fetch_activity_gear(garmin_client, act_id)
        except Exception as e:
            errors.append(f"gear {act_id}: {type(e).__name__}: {e}")
            continue
        checked += 1
        _store_activity_gear(act_id, uuid, gear_name)
        if uuid != old_uuid:
            changes.append({
                "activity_id": act_id,
                "date": start[:10],
                "name": name,
                "old_gear": old_name,
                "new_gear": gear_name,
            })
    return {
        "gear_checked": checked,
        "gear_changed": len(changes),
        "changes": changes,
        "errors": errors,
    }


def backfill_workout_links(garmin_client, max_activities: int = 100) -> dict:
    """Fetch detail fields for cached activities that never got them.

    Targets rows where detail_fetched_at IS NULL (one API call each),
    newest first. Also re-resolves planned_type for already-fetched rows
    where the workout_type_map has since gained the mapping.
    """
    _init_db()
    _refresh_workout_type_map()
    with sqlite3.connect(DB_PATH) as conn:
        ids = [r[0] for r in conn.execute(
            """
            SELECT id FROM activities
            WHERE detail_fetched_at IS NULL
            ORDER BY start_date_local DESC LIMIT ?
            """,
            (max_activities,),
        )]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE detail_fetched_at IS NULL"
        ).fetchone()[0] - len(ids)

    fetched = 0
    errors: list[str] = []
    for act_id in ids:
        try:
            _store_detail_fields(act_id, _fetch_detail_fields(garmin_client, act_id))
            fetched += 1
        except Exception as e:
            errors.append(f"detail {act_id}: {type(e).__name__}: {e}")

    # Pick up mappings that arrived after the detail fetch (e.g. a plan
    # materialized after the activity was synced — shouldn't happen, but cheap).
    with sqlite3.connect(DB_PATH) as conn:
        relinked = conn.execute(
            """
            UPDATE activities SET planned_type = (
                SELECT m.planned_type FROM workout_type_map m
                WHERE m.garmin_workout_id = activities.associated_workout_id
            )
            WHERE planned_type IS NULL AND associated_workout_id IS NOT NULL
              AND associated_workout_id IN
                  (SELECT garmin_workout_id FROM workout_type_map)
            """
        ).rowcount

    return {
        "details_fetched": fetched,
        "relinked": relinked,
        "remaining_without_detail": remaining,
        "errors": errors,
    }


def backfill_streams(garmin_client, max_activities: int = 100) -> dict:
    """Populate the extra stream columns (elevation / speed / distance /
    cadence / performance condition / grade-adjusted speed) for cached
    activities whose streams row predates them (extras_fetched_at IS NULL).

    Re-fetches the activity stream (one API call each), newest first, and
    rewrites the row plus the derived perf_cond_* columns on `activities`.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        ids = [r[0] for r in conn.execute(
            """
            SELECT a.id
            FROM activities a LEFT JOIN streams s ON s.activity_id = a.id
            WHERE (s.activity_id IS NOT NULL
                   AND (s.extras_fetched_at IS NULL OR s.perf_fetched_at IS NULL))
               -- Recent cardio activity that never got a stream at all
               -- (typically synced seconds after upload, before Garmin had
               -- processed it). Bounded to 30 days so an activity that
               -- genuinely has no stream isn't retried forever.
               OR (s.activity_id IS NULL AND a.avg_hr IS NOT NULL
                   AND a.sport_type IN ({cardio})
                   AND a.start_date_local >= date('now', '-30 days'))
            ORDER BY a.start_date_local DESC LIMIT ?
            """.format(cardio=", ".join("?" * len(_CARDIO_TYPES))),
            (*_CARDIO_TYPES, max_activities),
        )]
        remaining = max(0, conn.execute(
            "SELECT COUNT(*) FROM streams "
            "WHERE extras_fetched_at IS NULL OR perf_fetched_at IS NULL"
        ).fetchone()[0] - len(ids))

    updated = 0
    errors: list[str] = []
    for act_id in ids:
        try:
            stream = _garmin_get_stream(garmin_client, act_id)
        except Exception as e:
            errors.append(f"stream {act_id}: {type(e).__name__}: {e}")
            continue
        if not stream:
            # No stream available — mark processed so it isn't retried forever
            # (no-op for activities without a streams row; those age out).
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE streams SET extras_fetched_at=?, perf_fetched_at=? "
                    "WHERE activity_id=?",
                    (datetime.now(timezone.utc).isoformat(),
                     datetime.now(timezone.utc).isoformat(), act_id),
                )
            continue
        _store_stream(act_id, stream)
        updated += 1

    return {
        "streams_updated": updated,
        "remaining_without_streams": remaining,
        "errors": errors,
    }


def backfill_activity_metrics(
    garmin_client, weeks_back: int = 52, max_activities: int = 500
) -> dict:
    """Populate the running-dynamics / power / VO2max columns on cached
    activities synced before those columns existed (metrics_fetched_at NULL).

    Cheap: the values live on the activity LIST payload, so this re-lists the
    trailing `weeks_back` window in batches of 100 rather than making a call
    per activity. Rows outside that window keep metrics_fetched_at NULL —
    widen `weeks_back` to reach them.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        pending = {r[0] for r in conn.execute(
            "SELECT id FROM activities WHERE metrics_fetched_at IS NULL"
        )}
    if not pending:
        return {"metrics_updated": 0, "remaining_without_metrics": 0,
                "errors": []}

    after = datetime.now(timezone.utc) - timedelta(weeks=weeks_back)
    try:
        listed = _garmin_list_activities(garmin_client, after)
    except Exception as e:
        return {"metrics_updated": 0,
                "remaining_without_metrics": len(pending),
                "errors": [f"list: {type(e).__name__}: {e}"]}

    updated = 0
    errors: list[str] = []
    for act in listed:
        act_id = act.get("activityId")
        if act_id not in pending or updated >= max_activities:
            continue
        try:
            _store_activity_metrics(act_id, act)
            updated += 1
        except Exception as e:
            errors.append(f"metrics {act_id}: {type(e).__name__}: {e}")

    with sqlite3.connect(DB_PATH) as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE metrics_fetched_at IS NULL"
        ).fetchone()[0]

    return {
        "metrics_updated": updated,
        "remaining_without_metrics": remaining,
        "errors": errors,
    }


def backfill_laps(garmin_client, max_activities: int = 100) -> dict:
    """Re-fetch laps for cached activities whose laps_json predates the
    per-lap running-dynamics fields (dynamics_fetched_at IS NULL).

    One API call each, newest first. HR/pace lap fields are unchanged by
    this — it only adds cadence / GCT / vertical / power / respiration.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        ids = [r[0] for r in conn.execute(
            """
            SELECT l.activity_id
            FROM laps l JOIN activities a ON a.id = l.activity_id
            WHERE l.dynamics_fetched_at IS NULL
            ORDER BY a.start_date_local DESC LIMIT ?
            """,
            (max_activities,),
        )]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM laps WHERE dynamics_fetched_at IS NULL"
        ).fetchone()[0] - len(ids)

    updated = 0
    errors: list[str] = []
    for act_id in ids:
        try:
            laps = _garmin_get_laps(garmin_client, act_id)
        except Exception as e:
            errors.append(f"laps {act_id}: {type(e).__name__}: {e}")
            continue
        if not laps:
            # Nothing to re-store — mark processed so it isn't retried forever.
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE laps SET dynamics_fetched_at=? WHERE activity_id=?",
                    (datetime.now(timezone.utc).isoformat(), act_id),
                )
            continue
        _store_laps(act_id, laps)
        updated += 1

    return {
        "laps_updated": updated,
        "remaining_without_lap_dynamics": remaining,
        "errors": errors,
    }


# ─── VO2max (Garmin max-metrics) ──────────────────────────────────────
_MAXMET_DAILY_PATH = "/metrics-service/metrics/maxmet/daily/{start}/{end}"


def sync_vo2max_range(garmin_client, start_date: str, end_date: str) -> dict:
    """Pull Garmin's daily VO2max estimates for a date range into vo2max_daily.

    One API call for the whole range (the endpoint takes start/end), so this
    is cheap enough to run on every sync and to backfill a full year in one
    go. The series is sparse: Garmin writes a value only on days with a
    qualifying activity, so absent dates mean "no new estimate that day".
    """
    _init_db()
    try:
        raw = garmin_client.connectapi(
            _MAXMET_DAILY_PATH.format(start=start_date, end=end_date)
        ) or []
    except Exception as e:
        return {"vo2max_days": 0, "errors": [f"maxmet: {type(e).__name__}: {e}"]}

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for entry in raw:
        gen = (entry or {}).get("generic") or {}
        day = gen.get("calendarDate")
        if not day:
            continue
        rows.append((
            day,
            gen.get("vo2MaxPreciseValue"),
            gen.get("vo2MaxValue"),
            gen.get("fitnessAge"),
            now,
        ))
    if rows:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO vo2max_daily "
                "(date, vo2max, vo2max_rounded, fitness_age, synced_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
    return {"vo2max_days": len(rows), "errors": []}


# ─── Lactate threshold + race predictions (Garmin estimates) ──────────
def sync_lactate_threshold_range(garmin_client, start_date: str, end_date: str) -> dict:
    """Pull Garmin's running lactate-threshold estimate (HR + speed) and
    running FTP for a date range into lactate_threshold_daily. One API call;
    the series is sparse (a row only on days the model rewrote a value). The
    three series arrive separately and are merged per date with COALESCE so a
    speed-only day does not null out an HR-only day.
    """
    _init_db()
    try:
        raw = garmin_client.get_lactate_threshold(
            latest=False, start_date=start_date, end_date=end_date, aggregation="daily"
        ) or {}
    except Exception as e:
        return {"lt_days": 0, "errors": [f"lactate_threshold: {type(e).__name__}: {e}"]}

    per_day: dict[str, dict] = {}
    for key, col in (("heart_rate", "lt_hr"), ("speed", "lt_speed_m_s"), ("power", "ftp_w")):
        for e in raw.get(key) or []:
            day = (e or {}).get("from") or (e or {}).get("updatedDate")
            val = (e or {}).get("value")
            if not day or val is None:
                continue
            if col == "lt_speed_m_s":
                val = round(val * 10, 4)  # API unit is 10 m/s
            per_day.setdefault(day, {})[col] = val

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        for day, f in per_day.items():
            spd = f.get("lt_speed_m_s")
            pace = round(1000 / spd, 1) if spd else None
            conn.execute(
                """
                INSERT INTO lactate_threshold_daily
                    (date, lt_hr, lt_speed_m_s, lt_pace_s_per_km, ftp_w, synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    lt_hr = COALESCE(excluded.lt_hr, lt_hr),
                    lt_speed_m_s = COALESCE(excluded.lt_speed_m_s, lt_speed_m_s),
                    lt_pace_s_per_km = COALESCE(excluded.lt_pace_s_per_km, lt_pace_s_per_km),
                    ftp_w = COALESCE(excluded.ftp_w, ftp_w),
                    synced_at = excluded.synced_at
                """,
                (day, f.get("lt_hr"), spd, pace, f.get("ftp_w"), now),
            )
    return {"lt_days": len(per_day), "errors": []}


def sync_race_predictions_range(garmin_client, start_date: str, end_date: str) -> dict:
    """Pull Garmin's daily 5k/10k/half/marathon predictions (seconds) for a
    date range into race_predictions_daily. One API call, dense series."""
    _init_db()
    try:
        raw = garmin_client.get_race_predictions(
            startdate=start_date, enddate=end_date, _type="daily"
        ) or []
    except Exception as e:
        return {"race_prediction_days": 0,
                "errors": [f"race_predictions: {type(e).__name__}: {e}"]}
    if isinstance(raw, dict):
        raw = [raw]
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (e["calendarDate"], e.get("time5K"), e.get("time10K"),
         e.get("timeHalfMarathon"), e.get("timeMarathon"), now)
        for e in raw if isinstance(e, dict) and e.get("calendarDate")
    ]
    if rows:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO race_predictions_daily "
                "(date, time_5k_s, time_10k_s, time_half_s, time_marathon_s, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
    return {"race_prediction_days": len(rows), "errors": []}


def _fmt_time(secs: Optional[float]) -> Optional[str]:
    if secs is None:
        return None
    secs = int(round(secs))
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_pace(sec_per_km: Optional[float]) -> Optional[str]:
    if sec_per_km is None:
        return None
    return f"{int(sec_per_km // 60)}:{int(round(sec_per_km % 60)):02d}"


def lactate_threshold_trend(weeks_back: int = 52) -> dict:
    """Garmin's lactate-threshold estimate (HR, pace), running FTP and race
    predictions over time, next to the LT2 from the user profile.
    Descriptive: the series and their changes, no re-derivation of zones.
    """
    _init_db()
    since = (datetime.now(timezone.utc) - timedelta(weeks=weeks_back)).date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        lt_rows = conn.execute(
            "SELECT * FROM lactate_threshold_daily WHERE date >= ? ORDER BY date", (since,)
        ).fetchall()
        rp_rows = conn.execute(
            "SELECT * FROM race_predictions_daily WHERE date >= ? ORDER BY date", (since,)
        ).fetchall()
        extent = conn.execute(
            "SELECT MIN(date) o, MAX(date) n FROM lactate_threshold_daily").fetchone()

    if not lt_rows and not rp_rows:
        return {
            "error": "No Garmin threshold / race-prediction data cached for that window.",
            "next_steps": ["sync_activities() keeps the trailing 30 days current; "
                           "sync_vo2max(start_date, end_date) backfills all three "
                           "estimate series for a range."],
        }

    try:
        anchors = _parse_intensity_anchors(_parse_zones())
        profile_lt2 = anchors.get("lt2_hr")
        profile_source = anchors.get("source")
    except Exception:
        profile_lt2, profile_source = None, None

    series = []
    prev_hr = prev_pace = None
    for r in lt_rows:
        series.append({
            "date": r["date"],
            "lt_hr": r["lt_hr"],
            "lt_hr_delta": (r["lt_hr"] - prev_hr) if r["lt_hr"] is not None and prev_hr is not None else None,
            "lt_pace": _fmt_pace(r["lt_pace_s_per_km"]),
            "lt_pace_s_per_km": r["lt_pace_s_per_km"],
            "lt_pace_delta_s": round(r["lt_pace_s_per_km"] - prev_pace, 1)
                if r["lt_pace_s_per_km"] is not None and prev_pace is not None else None,
            "ftp_w": r["ftp_w"],
        })
        if r["lt_hr"] is not None:
            prev_hr = r["lt_hr"]
        if r["lt_pace_s_per_km"] is not None:
            prev_pace = r["lt_pace_s_per_km"]

    hr_pts = [(r["date"], r["lt_hr"]) for r in lt_rows if r["lt_hr"] is not None]
    pace_pts = [(r["date"], r["lt_pace_s_per_km"]) for r in lt_rows if r["lt_pace_s_per_km"] is not None]
    ftp_pts = [(r["date"], r["ftp_w"]) for r in lt_rows if r["ftp_w"] is not None]

    def _summ(pts, fmt=lambda v: v):
        if not pts:
            return None
        vals = [v for _, v in pts]
        return {"first": {"date": pts[0][0], "value": fmt(pts[0][1])},
                "latest": {"date": pts[-1][0], "value": fmt(pts[-1][1])},
                "min": fmt(min(vals)), "max": fmt(max(vals)),
                "n_updates": len(pts)}

    lt_summary = {
        "lt_hr": _summ(hr_pts),
        "lt_pace": _summ(pace_pts, _fmt_pace),
        "ftp_w": _summ(ftp_pts),
        "profile_lt2_hr": profile_lt2,
        "profile_lt2_source": profile_source,
        "garmin_minus_profile_bpm": (hr_pts[-1][1] - profile_lt2) if hr_pts and profile_lt2 else None,
    }

    def _at_or_before(rows, target):
        best = None
        for r in rows:
            if r["date"] <= target:
                best = r
        return best

    rp = None
    if rp_rows:
        latest = rp_rows[-1]
        today = date.fromisoformat(latest["date"])
        deltas = {}
        for label, days in (("4w", 28), ("12w", 84), ("52w", 364)):
            ref = _at_or_before(rp_rows, (today - timedelta(days=days)).isoformat())
            if ref:
                deltas[label] = {
                    "from_date": ref["date"],
                    "d5k_s": latest["time_5k_s"] - ref["time_5k_s"] if latest["time_5k_s"] and ref["time_5k_s"] else None,
                    "d10k_s": latest["time_10k_s"] - ref["time_10k_s"] if latest["time_10k_s"] and ref["time_10k_s"] else None,
                    "dhalf_s": latest["time_half_s"] - ref["time_half_s"] if latest["time_half_s"] and ref["time_half_s"] else None,
                }
        # Sampled every 28 days (plus the latest) to keep the payload small;
        # the daily rows are in race_predictions_daily for SQL.
        step = 28
        sampled = [{"date": r["date"], "5k": _fmt_time(r["time_5k_s"]),
                    "10k": _fmt_time(r["time_10k_s"]), "half": _fmt_time(r["time_half_s"]),
                    "marathon": _fmt_time(r["time_marathon_s"])}
                   for i, r in enumerate(rp_rows)
                   if (i % step == (len(rp_rows) - 1) % step)
                   and r["time_10k_s"] is not None]
        rp = {
            "latest": {"date": latest["date"], "5k": _fmt_time(latest["time_5k_s"]),
                       "10k": _fmt_time(latest["time_10k_s"]),
                       "half": _fmt_time(latest["time_half_s"]),
                       "marathon": _fmt_time(latest["time_marathon_s"])},
            "change": deltas,
            "every_4_weeks": sampled,
        }

    return {
        "window": {"since": since, "weeks_back": weeks_back,
                   "lt_cache_oldest": extent["o"], "lt_cache_newest": extent["n"]},
        "lactate_threshold": {"summary": lt_summary, "updates": series},
        "race_predictions": rp,
        "caveats": [
            "Garmin's LT is a model estimate from the pace/HR relationship in "
            "hard running; the LT2 in the profile (a lab test when the athlete "
            "has one, else an estimate from their zones — see "
            "profile_lt2_source) stays the anchor for zones and the "
            "sub-threshold band. Read the "
            "MOVEMENT here (up 4 bpm over 3 weeks), not the level.",
            "Updates land only after qualifying hard efforts (races, long "
            "threshold reps), so a flat series usually means 'no qualifying "
            "session', not 'no change'.",
            "Cool, rainy days flatter the model exactly like they flatter "
            "performance condition (same pace/HR input) — check weather on the "
            "update day before reading a jump as fitness.",
            "Race predictions are Garmin's VO2max-driven extrapolation; chip "
            "times outrank them. Use them as a trend, and remember they shift "
            "day to day with the VO2max estimate.",
        ],
    }


# ─── Location + weather (Open-Meteo) ──────────────────────────────────
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def latest_location() -> Optional[dict]:
    """Coordinates of the most recent cached activity that has GPS, if any.

    Coordinates are populated on the per-activity detail fetch during sync;
    indoor/treadmill activities have none, so this skips them.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT start_lat, start_lon, name, date(start_date_local) AS d
            FROM activities
            WHERE start_lat IS NOT NULL AND start_lon IS NOT NULL
            ORDER BY start_date_local DESC LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    return {
        "lat": row["start_lat"], "lon": row["start_lon"],
        "from_activity": row["name"], "as_of": row["d"],
    }


def fetch_weather(lat: float, lon: float, date_str: str, hour: int) -> dict:
    """One local date+hour of conditions from Open-Meteo (free, no API key).

    Read-only public API. Covers roughly the past 92 days through 16 days
    ahead. Returns the requested hour's temperature / humidity / dew point
    (the exact inputs heat_pace_adjustment wants) plus wind, precipitation,
    and the day's temperature range.
    """
    import requests
    from datetime import date as _date

    try:
        target = _date.fromisoformat(date_str)
    except ValueError:
        return {"error": f"Invalid date '{date_str}', expected YYYY-MM-DD."}
    delta = (target - _date.today()).days
    if delta < -92 or delta > 16:
        return {
            "error": (
                f"{date_str} is outside Open-Meteo's window (about the past 92 "
                "days through 16 days ahead)."
            )
        }

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": (
            "temperature_2m,relative_humidity_2m,dew_point_2m,"
            "precipitation,wind_speed_10m"
        ),
        "timezone": "auto",
        "start_date": date_str,
        "end_date": date_str,
    }
    try:
        resp = requests.get(OPEN_METEO_FORECAST_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": f"Weather fetch failed: {type(e).__name__}: {e}"}

    h = data.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return {"error": "No hourly data returned for that date/location."}

    hour = max(0, min(23, int(hour)))
    idx = next(
        (i for i, t in enumerate(times) if t.endswith(f"T{hour:02d}:00")),
        min(hour, len(times) - 1),
    )

    def at(key):
        arr = h.get(key) or []
        return arr[idx] if idx < len(arr) else None

    temps = [t for t in (h.get("temperature_2m") or []) if t is not None]
    return {
        "resolved_local_time": times[idx],
        "temp_c": at("temperature_2m"),
        "dew_point_c": at("dew_point_2m"),
        "relative_humidity": at("relative_humidity_2m"),
        "precipitation_mm": at("precipitation"),
        "wind_speed_kmh": at("wind_speed_10m"),
        "day_temp_max_c": max(temps) if temps else None,
        "day_temp_min_c": min(temps) if temps else None,
        "source": "open-meteo",
    }


def run_sync(
    garmin_client,
    force_full: bool = False,
    weeks_back: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    wellness_days: int = 10,
    gear_refresh_days: int = 14,
) -> dict:
    """Pull new activities + streams + laps + recent wellness into the cache.

    Args:
        garmin_client: Authenticated garminconnect.Garmin instance.
        force_full: If True, re-pull the default 12-week backfill window.
        weeks_back: Optional explicit backfill window in weeks.
        since: Explicit start datetime (overrides weeks_back/force_full).
        until: Optional end datetime — only activities before this are synced.
            Used for month-by-month backfills.
        wellness_days: Also refresh the trailing N days of wellness (HRV,
            resting HR, sleep, body battery) ending today, so the recovery /
            readiness tools don't see stale "no data". Default 10; set 0 to
            skip. Deeper historical wellness backfill is via
            get_wellness_history(start, end), not here.
        gear_refresh_days: Re-check the shoe on cardio activities from the
            trailing N days (one call each) so gear corrected in Connect
            after the activity was first synced lands in the cache.
            Default 14; set 0 to skip.
    """
    _init_db()

    if since is not None:
        after = since
    elif weeks_back is not None:
        after = datetime.now(timezone.utc) - timedelta(weeks=weeks_back)
    elif force_full or _get_last_sync() is None:
        after = datetime.now(timezone.utc) - timedelta(weeks=INITIAL_BACKFILL_WEEKS)
    else:
        after = _get_last_sync()  # type: ignore[assignment]

    sync_start = datetime.now(timezone.utc)
    _refresh_workout_type_map()

    try:
        activities = _garmin_list_activities(garmin_client, after, until=until)
    except Exception as e:
        return {"error": f"Failed to fetch activity list: {type(e).__name__}: {e}"}

    new_count = 0
    streams_count = 0
    laps_count = 0
    details_count = 0
    gear_count = 0
    weather_count = 0
    errors: list[str] = []

    for act in activities:
        act_id = act["activityId"]
        if _activity_exists(act_id):
            continue

        type_key = (act.get("activityType") or {}).get("typeKey", "")
        sport_type = _GARMIN_TYPE_MAP.get(type_key, type_key)
        # startTimeLocal: "2026-06-02 07:32:21" → store as ISO with T
        local_str = (act.get("startTimeLocal") or "").replace(" ", "T")

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO activities (
                    id, start_date_local, name, description, type, sport_type,
                    distance_m, moving_time_s, elapsed_time_s, avg_hr, max_hr,
                    total_elevation_gain, synced_at, training_effect_label,
                    metrics_fetched_at, {metric_cols}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{metric_qs})
                """.format(
                    metric_cols=", ".join(_ACTIVITY_METRIC_KEYS),
                    metric_qs=", ?" * len(_ACTIVITY_METRIC_KEYS),
                ),
                (
                    act_id, local_str,
                    act.get("activityName") or "",
                    None,  # Garmin list API has no description; acceptable
                    sport_type, sport_type,
                    act.get("distance"),
                    act.get("movingDuration"),
                    act.get("duration"),
                    act.get("averageHR"),
                    act.get("maxHR"),
                    act.get("elevationGain"),
                    sync_start.isoformat(),
                    act.get("trainingEffectLabel"),
                    sync_start.isoformat(),
                    *_activity_metric_values(act),
                ),
            )
        new_count += 1

        try:
            _store_detail_fields(act_id, _fetch_detail_fields(garmin_client, act_id))
            details_count += 1
        except Exception as e:
            errors.append(f"detail {act_id}: {type(e).__name__}: {e}")

        is_cardio = sport_type in _CARDIO_TYPES
        has_hr = bool(act.get("averageHR"))

        if is_cardio and has_hr:
            try:
                stream = _garmin_get_stream(garmin_client, act_id)
            except Exception as e:
                errors.append(f"stream {act_id}: {e}")
                stream = None
            if stream:
                _store_stream(act_id, stream)
                streams_count += 1

        if is_cardio and act.get("lapCount", 0) > 1:
            try:
                laps = _garmin_get_laps(garmin_client, act_id)
            except Exception as e:
                errors.append(f"laps {act_id}: {e}")
                laps = []
            if laps:
                _store_laps(act_id, laps)
                laps_count += 1

        if is_cardio:
            try:
                uuid, name = _fetch_activity_gear(garmin_client, act_id)
                _store_activity_gear(act_id, uuid, name)
                if uuid:
                    gear_count += 1
            except Exception as e:
                errors.append(f"gear {act_id}: {e}")

        if is_cardio:
            try:
                fetch_and_store_weather(garmin_client, act_id)
                weather_count += 1
            except Exception as e:
                errors.append(f"weather {act_id}: {e}")

    # Refresh recent wellness so HRV/RHR/sleep stay current alongside
    # activities — activity sync alone leaves these stale, which makes the
    # recovery/readiness tools report phantom "no data". Failures here never
    # break activity sync.
    wellness_fetched = 0
    wellness_cached = 0
    if wellness_days and wellness_days > 0:
        try:
            today = datetime.now(timezone.utc).date()
            w_start = (today - timedelta(days=wellness_days - 1)).isoformat()
            w = sync_wellness_range(garmin_client, w_start, today.isoformat())
            wellness_fetched = w.get("fetched", 0)
            wellness_cached = w.get("cached", 0)
            errors.extend(w.get("errors", []))
        except Exception as e:
            errors.append(f"wellness: {type(e).__name__}: {e}")

    # Garmin's VO2max estimate for the same trailing window — one call for
    # the whole range, so this is nearly free. Failures never break sync.
    vo2max_days = 0
    if wellness_days and wellness_days > 0:
        try:
            today = datetime.now(timezone.utc).date()
            v_start = (today - timedelta(days=max(wellness_days, 30) - 1)).isoformat()
            v = sync_vo2max_range(garmin_client, v_start, today.isoformat())
            vo2max_days = v.get("vo2max_days", 0)
            errors.extend(v.get("errors", []))
        except Exception as e:
            errors.append(f"vo2max: {type(e).__name__}: {e}")

    # Same trailing window for Garmin's lactate-threshold estimate and race
    # predictions — one call each, so nearly free. Failures never break sync.
    lt_days = 0
    race_prediction_days = 0
    if wellness_days and wellness_days > 0:
        try:
            today = datetime.now(timezone.utc).date()
            e_start = (today - timedelta(days=max(wellness_days, 30) - 1)).isoformat()
            lt = sync_lactate_threshold_range(garmin_client, e_start, today.isoformat())
            lt_days = lt.get("lt_days", 0)
            errors.extend(lt.get("errors", []))
            rp = sync_race_predictions_range(garmin_client, e_start, today.isoformat())
            race_prediction_days = rp.get("race_prediction_days", 0)
            errors.extend(rp.get("errors", []))
        except Exception as e:
            errors.append(f"garmin estimates: {type(e).__name__}: {e}")

    # Refresh the gear library (status + mileage) so the gear tools read
    # locally. Cheap (one list + one stats call per gear item) and failures
    # never break activity sync.
    gear_synced = 0
    try:
        gear_synced = sync_gear(garmin_client).get("gear_synced", 0)
    except Exception as e:
        errors.append(f"gear sync: {type(e).__name__}: {e}")

    # Re-check the shoe on recent activities. Garmin auto-assigns the default
    # shoe and the user often corrects it in Connect after the sync has run,
    # so a fetch-once cache would keep the wrong shoe.
    gear_refresh: dict = {}
    try:
        gear_refresh = refresh_activity_gear(garmin_client, days=gear_refresh_days)
        errors.extend(gear_refresh.pop("errors", []))
    except Exception as e:
        errors.append(f"gear refresh: {type(e).__name__}: {e}")

    _set_last_sync(sync_start)

    # Report the newest cached dates so callers can tell "0 new" (already
    # up to date) from "stale" without a second lookup — `new_activities: 0`
    # is normal and does NOT mean the cache is behind.
    with sqlite3.connect(DB_PATH) as conn:
        newest_activity = conn.execute(
            "SELECT MAX(date(start_date_local)) FROM activities"
        ).fetchone()[0]
        newest_wellness = conn.execute(
            "SELECT MAX(date) FROM wellness_daily"
        ).fetchone()[0]

    return {
        "new_activities": new_count,
        "streams_fetched": streams_count,
        "laps_fetched": laps_count,
        "details_fetched": details_count,
        "activity_gear_fetched": gear_count,
        "weather_fetched": weather_count,
        "gear_synced": gear_synced,
        "gear_refresh": gear_refresh,
        "wellness_fetched": wellness_fetched,
        "wellness_cached": wellness_cached,
        "vo2max_days": vo2max_days,
        "lactate_threshold_days": lt_days,
        "race_prediction_days": race_prediction_days,
        "errors": errors,
        "last_sync": sync_start.isoformat(),
        "since": after.isoformat(),
        "cache_newest_activity": newest_activity,
        "cache_newest_wellness": newest_wellness,
    }


# ─── Name-based classification hint ───────────────────────────────────
_NAME_PATTERNS = [
    # Each pattern covers common English terms + Norwegian equivalents.
    # Add your own naming conventions in coach_data/workout_classification.md and
    # extend these regexes if you use a different language or convention.
    ("prog-long", re.compile(
        r"progressiv langtur|progressive long|prog.?long", re.I)),
    ("long", re.compile(
        r"langtur|long run|long easy", re.I)),
    ("threshold", re.compile(
        r"terskel|subterskel|threshold|sub.?threshold|tempo.?run", re.I)),
    ("tempo", re.compile(
        r"\btempo\b", re.I)),
    ("intervals", re.compile(
        r"intervall|interval|pyramide|pyramid|vo2|speed.?work|track", re.I)),
    ("race", re.compile(
        # (?!\s?pace) keeps workout names like "3x800m race pace" out.
        r"stafett|etappe|race(?!\s?pace)|parkrun|\bfun run\b|\b5k\b|\b10k\b|\bhalf marathon\b"
        # Norwegian race-name suffixes: Fornebuløpet, Bislettmila, Oslo Maraton.
        # \b after løpet/mila keeps "løpetur"/"milaXX" from matching.
        r"|\bmarathon\b|løpet\b|mila\b|maraton", re.I)),
    # Keep last so the more specific patterns above win (e.g. "Long easy").
    ("easy", re.compile(
        r"easy run|rolig tur|recovery run", re.I)),
]
_DEFAULT_RUN_NAMES = re.compile(
    r"^(morning|afternoon|evening|lunch|easy|recovery|slow|base|aerobic|zone ?2)\s*(run|jog|løp)?$",
    re.I,
)


def name_hint(name: str, sport_type: Optional[str]) -> str:
    """Deterministic name-based classification hint (90% case).

    Returns one of: prog-long, long, threshold, tempo, intervals, race,
    strength, hike, ride, easy, unknown. Claude refines via
    coach://classification for ambiguous cases.
    """
    n = (name or "").strip()
    if sport_type in ("WeightTraining", "Workout"):
        return "strength"
    if sport_type in ("Hike", "Walk"):
        return "hike"
    if sport_type in ("Ride", "VirtualRide", "EBikeRide"):
        return "ride"
    for label, pat in _NAME_PATTERNS:
        if pat.search(n):
            return label
    if sport_type == "Run" and _DEFAULT_RUN_NAMES.match(n):
        return "easy"
    return "unknown"


def classify_activity(
    name: str, sport_type: Optional[str], planned_type: Optional[str] = None
) -> tuple[str, str]:
    """Resolve an activity's classification and where it came from.

    planned_type (the plan's own label, linked via the executed Garmin
    workout) is ground truth when present; the name-based hint is the
    fallback for free runs and pre-linkage history.

    Returns (classification, source) where source is 'plan' or 'name'.
    """
    if planned_type:
        return planned_type, "plan"
    return name_hint(name, sport_type), "name"


# ─── Zone parsing ─────────────────────────────────────────────────────
def _parse_zones() -> list[tuple[int, int, str]]:
    """Parse HR zone bpm ranges from coach_data/user_profile.md."""
    text = USER_PROFILE_PATH.read_text(encoding="utf-8")
    zones: list[tuple[int, int, str]] = []
    pat = re.compile(r"\|\s*(Z\d)\s*\|\s*(?:≥\s*)?(\d+)\s*(?:[–\-]\s*(\d+))?\s*\|")
    for line in text.splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        zname = m.group(1)
        n1 = int(m.group(2))
        n2 = m.group(3)
        zones.append((n1, int(n2) if n2 else 9999, zname))
    return zones


def _parse_intensity_anchors(
    zones: Optional[list[tuple[int, int, str]]] = None,
) -> dict:
    """Bpm anchors for intensity judgment from coach_data/user_profile.md.

    Returns {"sub_band_low", "sub_band_high", "hard_cap", "lt2_hr",
    "source"}. Profile values win; zone-derived estimates fill any gaps
    (hard cap ≈ Z4 low + 2, LT2 ≈ Z4 low + 6, band ≈ Z3 low → Z4 low) so
    callers always get usable numbers.

    These anchors deliberately do NOT line up with the Z1-Z5 boundaries:
    Z4 typically straddles the hard cap (e.g. Z4 = 188-198 vs cap 190),
    so "time in Z4" mixes at-cap sub-threshold work with genuinely
    over-threshold work. Intensity verdicts must use these bpm anchors,
    not zone shares.
    """
    if zones is None:
        try:
            zones = _parse_zones()
        except OSError:
            zones = []
    try:
        text = USER_PROFILE_PATH.read_text(encoding="utf-8")
    except OSError:
        text = ""

    lt2 = hard_cap = band_low = band_high = None
    for line in text.splitlines():
        if lt2 is None:
            m = re.search(r"LT2 HR[^|]*\|\s*\**(\d+)\s*bpm", line)
            if m:
                lt2 = int(m.group(1))
        if hard_cap is None:
            # [^:|] excludes the easy-run "Hard cap: LT1 = ..." wording.
            m = re.search(r"Hard cap[^:|]*\|\s*\**(\d+)\s*bpm", line)
            if m:
                hard_cap = int(m.group(1))
        if band_low is None:
            m = re.search(
                r"training target:\s*(\d+)\s*[–\-]\s*(\d+)", line, re.IGNORECASE
            ) or re.search(
                r"sub-threshold[^|]*\|\s*\**~?(\d+)\s*[–\-]\s*(\d+)\s*bpm",
                line,
                re.IGNORECASE,
            )
            if m:
                band_low, band_high = int(m.group(1)), int(m.group(2))

    z3_low = zones[2][0] if len(zones) >= 3 else 178
    z4_low = zones[3][0] if len(zones) >= 4 else 188
    source = "profile" if any(v is not None for v in (lt2, hard_cap, band_low)) else "zones"
    if band_low is None:
        band_low, band_high = z3_low, z4_low
    if hard_cap is None:
        hard_cap = (lt2 - 4) if lt2 is not None else z4_low + 2
    if lt2 is None:
        lt2 = z4_low + 6
    return {
        "sub_band_low": band_low,
        "sub_band_high": band_high,
        "hard_cap": hard_cap,
        "lt2_hr": lt2,
        "source": source,
    }


# ─── Weekly summary query ─────────────────────────────────────────────
def weekly_summary(start_date: str, end_date: str) -> dict:
    """Per-week aggregates from the local cache.

    Weeks are Mon-Sun. Zone time uses current bpm boundaries from
    coach://user_profile at query time, so retests automatically apply.

    Returns a dict with:
    - `volume_note`: reminder that run_* and cross_* are separate volumes.
    - `weeks`: list of per-week aggregates (only weeks with activities).
      Volume is split by sport: run_distance_m/run_moving_time_s/
      run_session_count (running only) vs cross_distance_m/
      cross_moving_time_s/cross_session_count (all other sports).
      zone_secs and the activities list span ALL sports.
    - `coverage`: cache extent metadata (oldest/newest activity dates,
      requested range, and `gap_warning` True if the request extends
      before the cache's oldest record). Use this to distinguish "no
      runs that week" from "we don't have data that far back."
    """
    _init_db()
    zones = _parse_zones()
    zone_names = [z[2] for z in zones]

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT a.*, s.time_json, s.hr_json
            FROM activities a
            LEFT JOIN streams s ON s.activity_id = a.id
            WHERE date(a.start_date_local) BETWEEN ? AND ?
            ORDER BY a.start_date_local
            """,
            (start_date, end_date),
        ).fetchall()

        extent = conn.execute(
            "SELECT MIN(date(start_date_local)) AS oldest, "
            "MAX(date(start_date_local)) AS newest FROM activities"
        ).fetchone()

    weeks: dict[str, dict] = {}
    for r in rows:
        d = date.fromisoformat(r["start_date_local"][:10])
        wk = (d - timedelta(days=d.weekday())).isoformat()
        bucket = weeks.setdefault(wk, {
            "week_start": wk,
            "week_end": (date.fromisoformat(wk) + timedelta(days=6)).isoformat(),
            "activities": [],
            "zone_secs": {z: 0 for z in zone_names},
            "below_z1_secs": 0,
            # Running and cross-training are SEPARATE volumes — never sum
            # them into one "total". zone_secs/activities include all sports.
            "run_distance_m": 0.0,
            "run_moving_time_s": 0,
            "cross_distance_m": 0.0,
            "cross_moving_time_s": 0,
            "session_count": 0,
            "run_session_count": 0,
            "cross_session_count": 0,
        })

        act_zones = {z: 0 for z in zone_names}
        if r["time_json"] and r["hr_json"]:
            times = json.loads(r["time_json"])
            hrs = json.loads(r["hr_json"])
            for i in range(len(times) - 1):
                dt = times[i + 1] - times[i]
                hr = hrs[i]
                placed = False
                for low, high, zname in zones:
                    if low <= hr <= high:
                        act_zones[zname] += dt
                        bucket["zone_secs"][zname] += dt
                        placed = True
                        break
                if not placed:
                    bucket["below_z1_secs"] += dt

        bucket["activities"].append({
            "id": r["id"],
            "date": r["start_date_local"][:10],
            "name": r["name"],
            "description": r["description"],
            "type": r["type"],
            "sport_type": r["sport_type"],
            "distance_m": r["distance_m"],
            "moving_time_s": r["moving_time_s"],
            "avg_hr": r["avg_hr"],
            "max_hr": r["max_hr"],
            "classification_hint": classify_activity(
                r["name"], r["sport_type"], r["planned_type"]
            )[0],
            "zone_secs": act_zones if r["time_json"] else None,
        })
        if r["type"] == "Run":
            bucket["run_session_count"] += 1
            bucket["run_distance_m"] += r["distance_m"] or 0
            bucket["run_moving_time_s"] += r["moving_time_s"] or 0
        else:
            bucket["cross_session_count"] += 1
            bucket["cross_distance_m"] += r["distance_m"] or 0
            bucket["cross_moving_time_s"] += r["moving_time_s"] or 0
        bucket["session_count"] += 1

    oldest = extent["oldest"] if extent else None
    gap_warning = bool(oldest and start_date < oldest)
    return {
        "volume_note": (
            "run_* fields are RUNNING ONLY; cross_* fields are everything "
            "else (ski, bike, ...). zone_secs and the activities list span "
            "ALL sports. When reporting weekly volume, state running and "
            "cross-training separately — never one combined total."
        ),
        "weeks": list(weeks.values()),
        "coverage": {
            "cache_oldest_activity": oldest,
            "cache_newest_activity": extent["newest"] if extent else None,
            "requested_start": start_date,
            "requested_end": end_date,
            "gap_warning": gap_warning,
            "gap_hint": (
                f"Requested range starts {start_date} but cache only goes back to "
                f"{oldest}. Call sync_activities(weeks_back=N) for deeper history."
            ) if gap_warning else None,
        },
    }


# ─── Flat activity list with filters ──────────────────────────────────
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
    """Flat list of cached activities with per-activity metadata.

    Unlike weekly_summary this returns one lightweight row per activity
    (no streams, no zone computation) so it scales to the whole cache.
    """
    _init_db()
    limit = max(1, min(limit, 1000))

    where = ["1=1"]
    params: list[Any] = []
    if start_date:
        where.append("date(start_date_local) >= ?")
        params.append(start_date)
    if end_date:
        where.append("date(start_date_local) <= ?")
        params.append(end_date)
    if sport_type:
        where.append("sport_type = ?")
        params.append(sport_type)
    if started_before:
        where.append("substr(start_date_local, 12, 5) < ?")
        params.append(started_before)
    if started_after:
        where.append("substr(start_date_local, 12, 5) >= ?")
        params.append(started_after)
    if name_contains:
        where.append("name LIKE ?")
        params.append(f"%{name_contains}%")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, start_date_local, name, type, sport_type, distance_m,
                   moving_time_s, avg_hr, max_hr, total_elevation_gain,
                   planned_type, training_effect_label, workout_rpe,
                   workout_feel, workout_compliance, gear_name
            FROM activities
            WHERE {' AND '.join(where)}
            ORDER BY start_date_local
            """,
            params,
        ).fetchall()
        extent = conn.execute(
            "SELECT MIN(date(start_date_local)) AS oldest, "
            "MAX(date(start_date_local)) AS newest FROM activities"
        ).fetchone()

    activities = []
    for r in rows:
        hint, hint_source = classify_activity(
            r["name"], r["sport_type"], r["planned_type"]
        )
        if classification and hint != classification:
            continue
        pace = None
        if r["distance_m"] and r["moving_time_s"]:
            sec_per_km = r["moving_time_s"] / (r["distance_m"] / 1000)
            pace = f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"
        activities.append({
            "id": r["id"],
            "date": r["start_date_local"][:10],
            "start_time": r["start_date_local"][11:16],
            "name": r["name"],
            "sport_type": r["sport_type"],
            "distance_km": round((r["distance_m"] or 0) / 1000, 2),
            "moving_time_s": r["moving_time_s"],
            "avg_hr": r["avg_hr"],
            "max_hr": r["max_hr"],
            "elevation_gain_m": r["total_elevation_gain"],
            "pace_per_km": pace,
            "classification_hint": hint,
            "classification_source": hint_source,
            "training_effect_label": r["training_effect_label"],
            "workout_rpe": r["workout_rpe"],
            "workout_feel": r["workout_feel"],
            "workout_compliance": r["workout_compliance"],
            "gear_name": r["gear_name"],
        })

    matched = len(activities)
    oldest = extent["oldest"] if extent else None
    gap_warning = bool(oldest and start_date and start_date < oldest)
    return {
        "activities": activities[:limit],
        "matched_count": matched,
        "returned_count": min(matched, limit),
        "coverage": {
            "cache_oldest_activity": oldest,
            "cache_newest_activity": extent["newest"] if extent else None,
            "gap_warning": gap_warning,
            "gap_hint": (
                f"Requested range starts {start_date} but cache only goes back "
                f"to {oldest}. Call sync_activities(weeks_back=N) for deeper "
                f"history."
            ) if gap_warning else None,
        },
    }


# ─── Read-only SQL access to the cache ────────────────────────────────
_WRITE_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|pragma|attach"
    r"|detach|vacuum|reindex)\b",
    re.IGNORECASE,
)


def query_cache(
    sql: str,
    params: Optional[list] = None,
    limit: int = 200,
    max_cell_chars: int = 500,
) -> dict:
    """Run a read-only SELECT against cache.db.

    The connection is opened with mode=ro (writes fail at the SQLite
    level); the keyword check just gives a clearer error message.
    """
    _init_db()
    stripped = sql.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        return {"error": "Only SELECT (or WITH ... SELECT) statements are allowed."}
    if _WRITE_SQL.search(stripped):
        return {"error": "Statement contains a write/DDL keyword; the cache is read-only."}
    limit = max(1, min(limit, 1000))

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cur = conn.execute(stripped, params or [])
        columns = [d[0] for d in cur.description] if cur.description else []
        raw = cur.fetchmany(limit + 1)
    except sqlite3.Error as e:
        return {"error": f"SQLite error: {e}"}
    finally:
        conn.close()

    truncated_rows = len(raw) > limit
    truncated_cells = 0
    rows = []
    for r in raw[:limit]:
        out = []
        for cell in r:
            if isinstance(cell, str) and len(cell) > max_cell_chars:
                cell = cell[:max_cell_chars] + f"… [truncated, {len(cell)} chars total]"
                truncated_cells += 1
            out.append(cell)
        rows.append(out)

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated_rows": truncated_rows,
        "truncated_cells": truncated_cells,
    }


# ─── Wellness history (HRV, RHR, sleep, stress, body battery) ────────
import math as _math


def _fetch_wellness_day(garmin_client, date_str: str) -> dict:
    """Pull HRV + daily-stats + sleep wellness metrics for one date.

    Tolerates missing fields — Garmin returns nulls for days without
    watch wear / sync, and individual endpoints may be unavailable
    depending on device model.
    """
    out: dict = {"date": date_str}

    try:
        h = garmin_client.get_hrv_data(date_str)
        if h and isinstance(h, dict) and h.get("hrvSummary"):
            s = h["hrvSummary"]
            out["hrv_overnight_avg"] = s.get("lastNightAvg")
            out["hrv_weekly_avg"] = s.get("weeklyAvg")
            out["hrv_status"] = s.get("status")
            base = s.get("baseline") or {}
            out["hrv_baseline_low"] = base.get("balancedLow")
            out["hrv_baseline_upper"] = base.get("balancedUpper")
    except Exception:
        pass

    try:
        s = garmin_client.get_stats(date_str)
        if s and isinstance(s, dict):
            out["resting_hr"] = s.get("restingHeartRate")
            out["avg_stress"] = s.get("averageStressLevel")
            out["body_battery_high"] = s.get("bodyBatteryHighestValue")
            out["body_battery_low"] = s.get("bodyBatteryLowestValue")
            out["body_battery_at_wake"] = s.get("bodyBatteryAtWakeTime")
            # Respiration + SpO2 are device-dependent — graceful None if absent.
            out["respiration_avg"] = (
                s.get("avgWakingRespirationValue")
                or s.get("averageRespirationValue")
            )
            out["spo2_avg"] = (
                s.get("averageSpo2")
                or s.get("averageSpO2HR")
                or s.get("avgSleepSpO2")
            )
    except Exception:
        pass

    # Sleep duration + score + stage breakdown ALL come from get_sleep_data,
    # not get_stats. get_stats has a `sleepingSeconds` field but it's a
    # different daily aggregate keyed to the calendar date and does NOT match
    # the watch's reported sleep duration — use sleepTimeSeconds here so
    # duration, score, and stages are one consistent record (sleepTimeSeconds
    # == deep + rem + light, i.e. excludes awake time, matching the watch).
    # Sleep data is keyed to the date the sleep STARTED, i.e. typically the
    # date BEFORE the caller's "today" — caller decides which date to pass.
    try:
        sd = garmin_client.get_sleep_data(date_str)
        if sd and isinstance(sd, dict):
            dto = sd.get("dailySleepDTO") or {}
            scores = dto.get("sleepScores") or {}
            overall = scores.get("overall") or {}
            out["sleep_seconds"] = dto.get("sleepTimeSeconds")
            out["sleep_score"] = overall.get("value")
            out["sleep_deep_s"] = dto.get("deepSleepSeconds")
            out["sleep_rem_s"] = dto.get("remSleepSeconds")
            out["sleep_light_s"] = dto.get("lightSleepSeconds")
            out["sleep_awake_s"] = dto.get("awakeSleepSeconds")
    except Exception:
        pass

    # Recovery time from training readiness. Two extraction nuances:
    #
    # 1. The endpoint returns a list of readings throughout the day,
    #    including any late-evening reading from the previous day. For
    #    a historical training-stress signal we want the PEAK recovery
    #    time on the calendar day — that's the estimate right after the
    #    day's hardest workout, before overnight decay normalizes it.
    #    A morning snapshot would lose race-day stress (Garmin
    #    overnight-resets aggressively after big efforts).
    # 2. Garmin's `recoveryTime` field is in MINUTES, not hours, despite
    #    being displayed on the watch as hours. Convert before storing.
    try:
        tr = garmin_client.get_training_readiness(date_str)
        if tr and isinstance(tr, list):
            same_day = [
                e for e in tr
                if isinstance(e.get("timestamp"), str)
                and e["timestamp"].startswith(date_str)
            ]
            if same_day:
                peak = max(same_day, key=lambda e: e.get("recoveryTime") or 0)
                raw_minutes = peak.get("recoveryTime")
                if raw_minutes is not None:
                    out["recovery_time_hours"] = round(raw_minutes / 60)
                first = min(same_day, key=lambda e: e.get("timestamp") or "")
                out["training_readiness_score"] = first.get("score")
                out["training_readiness_level"] = first.get("level")
    except Exception:
        pass

    out.update(_fetch_training_status_day(garmin_client, date_str))
    return out


_TRAINING_STATUS_KEYS = (
    "training_status_code", "training_status", "acwr_percent", "acwr_status",
    "load_acwr", "load_acute", "load_chronic", "load_chronic_min",
    "load_chronic_max", "load_4w_aerobic_low", "load_4w_aerobic_high",
    "load_4w_anaerobic", "load_balance",
)


def _fetch_training_status_day(garmin_client, date_str: str) -> dict:
    """Garmin training status / acute-chronic load / 4-week load balance as
    of one date. The endpoint is date-addressable (a past date returns that
    day's figures), so this backfills history. Always sets
    training_status_fetched_at so the backfill terminates."""
    out: dict = {"training_status_fetched_at": datetime.now(timezone.utc).isoformat()}
    try:
        ts = garmin_client.get_training_status(date_str) or {}
    except Exception:
        return out
    latest = (ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
    dev = next(iter(latest.values()), {}) if isinstance(latest, dict) else {}
    if dev and dev.get("calendarDate") == date_str:
        out["training_status_code"] = dev.get("trainingStatus")
        out["training_status"] = dev.get("trainingStatusFeedbackPhrase")
        acute = dev.get("acuteTrainingLoadDTO") or {}
        out["acwr_percent"] = acute.get("acwrPercent")
        out["acwr_status"] = acute.get("acwrStatus")
        out["load_acwr"] = acute.get("dailyAcuteChronicWorkloadRatio")
        out["load_acute"] = acute.get("dailyTrainingLoadAcute")
        out["load_chronic"] = acute.get("dailyTrainingLoadChronic")
        out["load_chronic_min"] = acute.get("minTrainingLoadChronic")
        out["load_chronic_max"] = acute.get("maxTrainingLoadChronic")
    bal_map = (ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
    bal = next(iter(bal_map.values()), {}) if isinstance(bal_map, dict) else {}
    if bal and bal.get("calendarDate") == date_str:
        out["load_4w_aerobic_low"] = bal.get("monthlyLoadAerobicLow")
        out["load_4w_aerobic_high"] = bal.get("monthlyLoadAerobicHigh")
        out["load_4w_anaerobic"] = bal.get("monthlyLoadAnaerobic")
        out["load_balance"] = bal.get("trainingBalanceFeedbackPhrase")
    return out


def backfill_training_status(garmin_client, max_days: int = 200) -> dict:
    """Fill the training-status / load / readiness columns on wellness_daily
    rows cached before they existed (two API calls per day, newest first)."""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        days = [r[0] for r in conn.execute(
            "SELECT date FROM wellness_daily WHERE training_status_fetched_at IS NULL "
            "ORDER BY date DESC LIMIT ?", (max_days,))]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM wellness_daily WHERE training_status_fetched_at IS NULL"
        ).fetchone()[0] - len(days)
    updated = 0
    with_status = 0
    errors: list[str] = []
    for ds in days:
        try:
            fields = _fetch_training_status_day(garmin_client, ds)
            tr = garmin_client.get_training_readiness(ds)
            same_day = [e for e in (tr or []) if isinstance(e, dict)
                        and str(e.get("timestamp", "")).startswith(ds)]
            if same_day:
                first = min(same_day, key=lambda e: e.get("timestamp") or "")
                fields["training_readiness_score"] = first.get("score")
                fields["training_readiness_level"] = first.get("level")
            time.sleep(0.1)
        except Exception as e:
            errors.append(f"{ds}: {type(e).__name__}: {e}")
            continue
        sets = ", ".join(f"{k} = ?" for k in fields)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(f"UPDATE wellness_daily SET {sets} WHERE date = ?",
                         [*fields.values(), ds])
        updated += 1
        if fields.get("training_status") is not None:
            with_status += 1
    return {"days_updated": updated, "days_with_status": with_status,
            "remaining": remaining, "errors": errors}


def _wellness_day_cached(date_str: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM wellness_daily WHERE date = ?", (date_str,)
        ).fetchone()
        return dict(row) if row else None


def _save_wellness_day(row: dict) -> None:
    cols = [
        "date", "resting_hr", "hrv_overnight_avg", "hrv_weekly_avg",
        "hrv_status", "hrv_baseline_low", "hrv_baseline_upper",
        "sleep_seconds", "sleep_score",
        "sleep_deep_s", "sleep_rem_s", "sleep_light_s", "sleep_awake_s",
        "avg_stress",
        "body_battery_high", "body_battery_low", "body_battery_at_wake",
        "respiration_avg", "spo2_avg",
        "recovery_time_hours",
        *_TRAINING_STATUS_KEYS,
        "training_readiness_score", "training_readiness_level",
        "training_status_fetched_at",
        "synced_at",
    ]
    vals = [row.get(c) for c in cols[:-1]] + [datetime.now(timezone.utc).isoformat()]
    placeholders = ", ".join(["?"] * len(cols))
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO wellness_daily ({', '.join(cols)}) "
            f"VALUES ({placeholders})",
            vals,
        )


def sync_wellness_range(
    garmin_client,
    start_date: str,
    end_date: str,
    force_refetch: bool = False,
) -> dict:
    """Pull wellness for each date in range. Returns counts of cached/fetched/errors."""
    _init_db()
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date")

    cached = 0
    fetched = 0
    errors: list[str] = []
    d = start
    while d <= end:
        ds = d.isoformat()
        if not force_refetch and _wellness_day_cached(ds):
            cached += 1
        else:
            try:
                row = _fetch_wellness_day(garmin_client, ds)
                _save_wellness_day(row)
                fetched += 1
                # Be polite to Garmin's rate limiter
                time.sleep(0.2)
            except Exception as e:
                errors.append(f"{ds}: {type(e).__name__}: {e}")
        d += timedelta(days=1)
    return {"cached": cached, "fetched": fetched, "errors": errors}


def _compute_morning_trends(today_metrics: dict, history: list[dict]) -> dict:
    """Compare today's wellness metrics against the prior-7-day window.

    For each tracked metric: 7-day mean, today's value, delta, stdev,
    and a deviation flag when today's reading is >1 stdev outside the
    trailing mean in the "bad" direction (HRV ↓, RHR ↑, sleep ↓,
    stress ↑). Direction-good encoded per metric so the flag is
    semantically meaningful.
    """
    def mean(vals):
        return round(sum(vals) / len(vals), 1) if vals else None

    def stdev(vals, m):
        if not vals or len(vals) < 2 or m is None:
            return None
        var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
        return _math.sqrt(var)

    def trend(field: str, higher_is_better: bool) -> Optional[dict]:
        hist_vals = [d[field] for d in history if d.get(field) is not None]
        today_v = today_metrics.get(field)
        m = mean(hist_vals)
        s = stdev(hist_vals, m)
        if m is None and today_v is None:
            return None
        delta = round(today_v - m, 1) if (today_v is not None and m is not None) else None
        flag = None
        if delta is not None and s is not None and abs(delta) > s:
            bad_direction = "below" if higher_is_better else "above"
            flag = f"{bad_direction}_normal" if (
                (delta < 0 and higher_is_better) or (delta > 0 and not higher_is_better)
            ) else "outside_normal_favorable"
        return {
            "today": today_v,
            "mean_7d": m,
            "delta_vs_7d": delta,
            "stdev_7d": round(s, 1) if s is not None else None,
            "samples_7d": len(hist_vals),
            "flag": flag,
        }

    return {
        "hrv_overnight_avg": trend("hrv_overnight_avg", higher_is_better=True),
        "resting_hr": trend("resting_hr", higher_is_better=False),
        "sleep_seconds": trend("sleep_seconds", higher_is_better=True),
        "sleep_score": trend("sleep_score", higher_is_better=True),
        "avg_stress": trend("avg_stress", higher_is_better=False),
        "respiration_avg": trend("respiration_avg", higher_is_better=False),
        "spo2_avg": trend("spo2_avg", higher_is_better=True),
    }


def _illness_signals(today: dict, history: list[dict]) -> dict:
    """Acute illness-onset check: today vs the trailing 7-day mean.

    Illness is a SUDDEN shift, so this deliberately uses the short window
    (sustained drift is handled by wellness_baseline_comparison). Five
    independent flags; 3+ = high, 2 = moderate. Computed from the data
    morning_check_in already has — no extra fetch.
    """
    def mean(field):
        vals = [d[field] for d in history if d.get(field) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    t_hrv, m_hrv = today.get("hrv_overnight_avg"), mean("hrv_overnight_avg")
    t_rhr, m_rhr = today.get("resting_hr"), mean("resting_hr")
    t_ss, m_ss = today.get("sleep_score"), mean("sleep_score")
    t_sleep = today.get("sleep_seconds")
    t_stress = today.get("avg_stress")

    flagged: list[str] = []
    hrv_drop = rhr_rise = ss_drop = None
    if t_hrv is not None and m_hrv and m_hrv > 0:
        hrv_drop = round((m_hrv - t_hrv) / m_hrv * 100, 1)
        if hrv_drop > 15:
            flagged.append("hrv_low")
    if t_rhr is not None and m_rhr is not None:
        rhr_rise = round(t_rhr - m_rhr, 1)
        if rhr_rise > 5:
            flagged.append("rhr_elevated")
    if t_ss is not None and m_ss is not None:
        ss_drop = round(m_ss - t_ss, 1)
        if ss_drop > 10:
            flagged.append("sleep_score_low")
    if t_sleep is not None and t_sleep < 21_600:
        flagged.append("sleep_short")
    if t_stress is not None and t_stress > 60:
        flagged.append("stress_high")

    n = len(flagged)
    risk = "high" if n >= 3 else "moderate" if n == 2 else "low"
    note = {
        "high": "Multiple acute illness signals — rest or very easy, reassess tomorrow.",
        "moderate": "Two acute signals — consider easing intensity, weighed against how you feel.",
        "low": "No meaningful acute illness signal.",
    }[risk]
    return {
        "risk_level": risk,
        "flag_count": n,
        "flagged_signals": flagged,
        "note": note,
        "raw": {
            "hrv_today": t_hrv, "hrv_7d_mean": m_hrv, "hrv_drop_pct": hrv_drop,
            "rhr_today": t_rhr, "rhr_7d_mean": m_rhr, "rhr_rise_bpm": rhr_rise,
            "sleep_score_today": t_ss, "sleep_score_7d_mean": m_ss,
            "sleep_seconds_today": t_sleep, "avg_stress_today": t_stress,
        },
        "thresholds": {
            "hrv_drop_pct": 15, "rhr_rise_bpm": 5, "sleep_score_drop": 10,
            "sleep_hours": 6, "stress": 60,
        },
    }


def morning_check_in_data(
    garmin_client,
    today_str: str,
    history_start: str,
    history_end: str,
) -> dict:
    """Build today's wellness snapshot + 7-day trend block.

    Pulled fresh from Garmin for today (so the snapshot reflects current
    sync state, not stale cache), backed by the cached wellness_daily
    history for the prior week.
    """
    # get_sleep_data(D) returns the sleep that ENDED the morning of date D
    # (i.e. last night), so the sleep fields already on `today` ARE last
    # night's sleep. No yesterday re-fetch needed — doing so would pull the
    # night before last and report the wrong duration/score.
    today = _fetch_wellness_day(garmin_client, today_str)

    # Make sure the 7-day window is in the cache before we trend on it.
    sync_wellness_range(garmin_client, history_start, history_end)
    history = _read_wellness_range(history_start, history_end)
    return {
        "today": today,
        "trends": _compute_morning_trends(today, history),
        # Long-baseline drift check: the 7-day trend above moves with a
        # sustained drift, so it can't see a multi-week elevation. This does.
        "baseline_comparison": wellness_baseline_comparison(as_of_date=today_str),
        # Acute illness-onset flags (today vs 7-day mean) — folded in from the
        # former standalone illness_risk_check tool.
        "illness_signals": _illness_signals(today, history),
        "history_window": {"start": history_start, "end": history_end, "days": len(history)},
    }


def _read_wellness_range(start_date: str, end_date: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM wellness_daily
            WHERE date BETWEEN ? AND ?
            ORDER BY date
            """,
            (start_date, end_date),
        ).fetchall()
    return [dict(r) for r in rows]


def _rolling_averages(daily: list[dict], window: int = 7, min_data: int = 4) -> list[dict]:
    """Compute rolling means.

    - RHR: simple arithmetic 7-day mean.
    - HRV: 7-day geometric mean (mean of ln(HRV), exp back). HRV is roughly
      log-normally distributed so this is the right shape per HRV4Training /
      Altini's research.
    """
    out: list[dict] = []
    for i in range(len(daily)):
        win_start = max(0, i - window + 1)
        win = daily[win_start : i + 1]

        rhr_vals = [w["resting_hr"] for w in win if w.get("resting_hr")]
        rhr_mean = (
            round(sum(rhr_vals) / len(rhr_vals), 1) if len(rhr_vals) >= min_data else None
        )

        hrv_vals = [w["hrv_overnight_avg"] for w in win if w.get("hrv_overnight_avg")]
        if len(hrv_vals) >= min_data:
            hrv_ln_mean = sum(_math.log(v) for v in hrv_vals) / len(hrv_vals)
            hrv_geo_mean = round(_math.exp(hrv_ln_mean), 1)
        else:
            hrv_geo_mean = None

        out.append({
            "date": daily[i]["date"],
            "rhr_7d_mean": rhr_mean,
            "hrv_7d_geomean": hrv_geo_mean,
        })
    return out


def wellness_history(start_date: str, end_date: str) -> dict:
    """Read wellness range from cache, compute rolling averages, return shaped result."""
    daily = _read_wellness_range(start_date, end_date)
    rolling = _rolling_averages(daily)

    # Summary stats
    rhr_vals = [d["resting_hr"] for d in daily if d.get("resting_hr")]
    hrv_vals = [d["hrv_overnight_avg"] for d in daily if d.get("hrv_overnight_avg")]
    baseline_low = next(
        (d["hrv_baseline_low"] for d in reversed(daily) if d.get("hrv_baseline_low")), None
    )
    baseline_upper = next(
        (d["hrv_baseline_upper"] for d in reversed(daily) if d.get("hrv_baseline_upper")), None
    )

    return {
        "range": {"start": start_date, "end": end_date, "days": len(daily)},
        "daily": daily,
        "rolling": rolling,
        "summary": {
            "rhr_days_with_data": len(rhr_vals),
            "rhr_min": min(rhr_vals) if rhr_vals else None,
            "rhr_max": max(rhr_vals) if rhr_vals else None,
            "rhr_mean": round(sum(rhr_vals) / len(rhr_vals), 1) if rhr_vals else None,
            "hrv_days_with_data": len(hrv_vals),
            "hrv_min": min(hrv_vals) if hrv_vals else None,
            "hrv_max": max(hrv_vals) if hrv_vals else None,
            "hrv_mean": round(sum(hrv_vals) / len(hrv_vals), 1) if hrv_vals else None,
            "hrv_baseline_band": [baseline_low, baseline_upper] if baseline_low else None,
        },
    }


# ─── Recent-vs-long-baseline drift detection ──────────────────────────
# A trailing 7-day average MOVES WITH a sustained drift: if RHR has been
# elevated for two weeks, the 7-day mean is elevated too, so "today is near
# the 7-day average" reads as fine while you're two weeks into a rise. This
# compares the recent window against a long, drift-resistant baseline (the
# MEDIAN over ~90 days — a two-week excursion is a minority of the window, so
# it barely moves the median, unlike a mean) and reports HOW LONG the metric
# has sat on the bad side. Reads the cache and assumes it holds full history.

# Per-metric "off by a meaningful amount" floor (in the metric's own units);
# the effective threshold is max(floor, 0.5 × baseline spread).
_BASELINE_METRICS = [
    # (field, label, higher_is_better, min_threshold)
    ("resting_hr", "resting_hr", False, 2.0),       # bpm
    ("hrv_overnight_avg", "hrv", True, 3.0),         # ms
    ("avg_stress", "avg_stress", False, 4.0),
    ("body_battery_at_wake", "body_battery_at_wake", True, 5.0),
]


def _compare_to_baseline(series, recent_days, higher_is_better, min_threshold):
    """series: list of (date, value) oldest→newest, non-null. Compare the
    recent_days mean to the median over the whole series, and count how long
    the metric has sat on the unfavourable side."""
    if len(series) < recent_days + 5:
        return {"status": "insufficient_data", "n_baseline": len(series)}
    vals = [v for _, v in series]
    recent = vals[-recent_days:]
    recent_mean = sum(recent) / len(recent)
    baseline_median = statistics.median(vals)
    spread = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    threshold = max(min_threshold, 0.5 * spread)
    delta = recent_mean - baseline_median  # +ve = above baseline

    # "off" = on the unfavourable side of baseline by more than the threshold.
    def off_day(v):
        return (baseline_median - v) >= threshold if higher_is_better \
            else (v - baseline_median) >= threshold

    recent_off = (delta <= -threshold) if higher_is_better else (delta >= threshold)
    flag = "normal"
    if recent_off:
        flag = "suppressed" if higher_is_better else "elevated"

    # consecutive days (from most recent) on the unfavourable side
    consec = 0
    for _, v in reversed(series):
        if off_day(v):
            consec += 1
        else:
            break
    last14 = series[-14:]
    days_off_14 = sum(1 for _, v in last14 if off_day(v))

    return {
        "recent_mean": round(recent_mean, 1),
        "baseline_median": round(baseline_median, 1),
        "baseline_spread": round(spread, 1),
        "delta_vs_baseline": round(delta, 1),
        "threshold": round(threshold, 1),
        "flag": flag,                              # normal | elevated | suppressed
        "consecutive_days_off_baseline": consec,
        "days_off_baseline_last_14": days_off_14,
        "n_baseline": len(vals),
    }


def wellness_baseline_comparison(
    as_of_date: Optional[str] = None,
    recent_days: int = 7,
    baseline_days: int = 90,
) -> dict:
    """Recent wellness vs a long, drift-resistant baseline.

    For each tracked metric (RHR, HRV, stress, body-battery-at-wake): the
    recent_days mean, the baseline median over baseline_days, the delta, a
    flag (elevated/suppressed/normal vs baseline), and — crucially — how many
    days the metric has sat on the unfavourable side (consecutive, and within
    the last 14). This catches multi-week drift that a trailing 7-day average
    masks. Cache-only; assumes full history is present.
    """
    _init_db()
    as_of = date.fromisoformat(as_of_date) if as_of_date else date.today()
    start = (as_of - timedelta(days=baseline_days - 1)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, resting_hr, hrv_overnight_avg, avg_stress, "
            "body_battery_at_wake FROM wellness_daily "
            "WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, as_of.isoformat()),
        ).fetchall()

    metrics = {}
    for field, label, higher_better, floor in _BASELINE_METRICS:
        series = [(r["date"], r[field]) for r in rows if r[field] is not None]
        metrics[label] = _compare_to_baseline(series, recent_days, higher_better, floor)

    return {
        "as_of": as_of.isoformat(),
        "recent_days": recent_days,
        "baseline_days": baseline_days,
        "metrics": metrics,
    }


def _zone_index(hr: Optional[float], zones: list[tuple[int, int, str]]) -> Optional[int]:
    """Return 0-based zone index (0=Z1...4=Z5) for an HR value, or None."""
    if hr is None:
        return None
    for i, (low, high, _) in enumerate(zones):
        if low <= hr <= high:
            return i
    return None


def _classify_laps(
    laps: list[dict], zones: list[tuple[int, int, str]]
) -> list[dict]:
    """Tag each lap with type: drag, pause, wu, cd, or easy.

    Garmin laps: uses intensityType directly (WARMUP→wu, ACTIVE/INTERVAL→drag,
    REST/RECOVERY→pause, COOLDOWN→cd). Falls back to heuristic when absent.

    Heuristic (two-pass):
    - Primary: a lap is a "drag" if avg_hr is in Z3+ AND moving_time >= 30s.
    - HR-lag rescue: if a lap has max_hr in Z3+ AND its pace is no more
      than 30 sec/km SLOWER than the median pace of primary drags, it's
      reclassified as a drag. This catches the common case where the
      first rep's avg HR sits just below Z3 because HR hadn't caught up
      yet — pace + max both confirm it was actually a working rep. Faster
      laps are always eligible: on hilly terrain a downhill rep can be
      much faster than the median without being a recovery (recoveries
      are slower than the work, never faster).
    - If no drags exist → all laps are "easy" (continuous easy run).
    - Otherwise: laps before first drag = "wu", after last drag = "cd",
      between drags = "pause".
    """
    # Fast path: Garmin structured laps with intensityType.
    # Only use when the workout has structural diversity (at least one
    # non-ACTIVE lap), which confirms it's a programmed session, not a
    # continuous easy run where every lap is just ACTIVE/INTERVAL.
    non_active = {"WARMUP", "COOLDOWN", "REST", "RECOVERY"}
    if laps and all(lap.get("intensityType") for lap in laps) and any(
        lap["intensityType"] in non_active for lap in laps
    ):
        out = []
        for lap in laps:
            t = _INTENSITY_TYPE_MAP.get(lap["intensityType"], "easy")
            out.append({**lap, "lap_type": t})
        return out
    if not laps:
        return []

    is_drag: list[bool] = []
    for lap in laps:
        zi = _zone_index(lap.get("average_heartrate"), zones)
        moving = lap.get("moving_time") or 0
        is_drag.append(zi is not None and zi >= 2 and moving >= 30)

    drag_paces = [
        1000.0 / lap["average_speed"]
        for lap, d in zip(laps, is_drag)
        if d and (lap.get("average_speed") or 0) > 0
    ]
    if drag_paces:
        sorted_paces = sorted(drag_paces)
        median_pace = sorted_paces[len(sorted_paces) // 2]
        for i, lap in enumerate(laps):
            if is_drag[i]:
                continue
            mx_zi = _zone_index(lap.get("max_heartrate"), zones)
            speed = lap.get("average_speed") or 0
            moving = lap.get("moving_time") or 0
            if moving < 30 or speed <= 0 or mx_zi is None or mx_zi < 2:
                continue
            if 1000.0 / speed - median_pace <= 30:
                is_drag[i] = True

    out: list[dict] = []
    if not any(is_drag):
        for lap in laps:
            out.append({**lap, "lap_type": "easy"})
        return out

    first = is_drag.index(True)
    last = len(is_drag) - 1 - list(reversed(is_drag)).index(True)
    for i, lap in enumerate(laps):
        if is_drag[i]:
            t = "drag"
        elif i < first:
            t = "wu"
        elif i > last:
            t = "cd"
        else:
            t = "pause"
        out.append({**lap, "lap_type": t})
    return out


def _lap_zone_secs(
    lap: dict,
    activity_start_s: float,
    times: list[int],
    hrs: list[int],
    zones: list[tuple[int, int, str]],
) -> Optional[dict]:
    """Compute per-zone seconds for one lap using elapsed-time windows.

    Uses the lap's start_date + elapsed_time to find which stream samples
    fall within the lap. Resolution-independent — works whether the stream
    is downsampled (100 pts) or full per-second.
    """
    from datetime import datetime, timezone as tz
    lap_start_str = lap.get("start_date_local") or lap.get("start_date")
    elapsed = lap.get("elapsed_time")
    if not lap_start_str or elapsed is None or not times or not hrs:
        return None
    try:
        lap_start_utc = datetime.strptime(
            lap_start_str.replace("+00:00", "Z"), "%Y-%m-%dT%H:%M:%SZ"
        )
        lap_offset_start = lap_start_utc.replace(tzinfo=tz.utc).timestamp() - activity_start_s
    except Exception:
        return None
    lap_offset_end = lap_offset_start + elapsed

    secs = {z[2]: 0 for z in zones}
    for i in range(len(times) - 1):
        t = times[i]
        if t < lap_offset_start:
            continue
        if t >= lap_offset_end:
            break
        dt = times[i + 1] - t
        hr = hrs[i]
        for low, high, zname in zones:
            if low <= hr <= high:
                secs[zname] += dt
                break
    return secs


# Per-lap running dynamics, kept compact because a session can have 20+ laps.
# Rounded on the way out; absent keys mean Garmin reported nothing for them.
_LAP_FORM_FIELDS = (
    ("avg_cadence_spm", "cadence_spm", 0),
    ("stride_length_cm", "stride_cm", 0),
    ("ground_contact_ms", "gct_ms", 0),
    ("vertical_osc_cm", "vert_osc_cm", 1),
    ("vertical_ratio_pct", "vert_ratio_pct", 1),
    ("avg_power_w", "power_w", 0),
    ("avg_respiration_rate", "resp_rate", 1),
)


def _lap_form(lap: dict) -> dict:
    """Compact running-dynamics block for one lap, empty when unreported."""
    out = {}
    for src, key, digits in _LAP_FORM_FIELDS:
        v = lap.get(src)
        if v is not None:
            out[key] = round(v, digits) if digits else round(v)
    return out


def _summarize_laps(
    laps: list[dict],
    activity_start_s: Optional[float] = None,
    times: Optional[list] = None,
    hrs: Optional[list] = None,
    zones: Optional[list[tuple[int, int, str]]] = None,
) -> list[dict]:
    """Compact lap summary for the report (only fields a coach needs)."""
    # Precompute cumulative elapsed offsets for zone-window slicing.
    # Garmin streams use elapsed-from-zero, so we can slice by lap duration
    # without relying on timestamps. Both Garmin and Strava elapsed arrays start at t=0.
    cumulative = 0.0
    lap_offsets: list[tuple[float, float]] = []
    for lap in laps:
        start = cumulative
        dur = lap.get("elapsed_time") or 0
        cumulative += dur
        lap_offsets.append((start, start + dur))

    summary = []
    for lap, (offset_start, offset_end) in zip(laps, lap_offsets):
        moving = lap.get("moving_time") or 0
        dist = lap.get("distance") or 0
        avg_speed = lap.get("average_speed") or 0
        pace_s_per_km = round(1000 / avg_speed, 1) if avg_speed > 0 else None
        entry: dict = {
            "lap_index": lap.get("lap_index"),
            "type": lap.get("lap_type"),
            "distance_m": round(dist),
            "moving_time_s": moving,
            "pace_s_per_km": pace_s_per_km,
            "avg_hr": lap.get("average_heartrate"),
            "max_hr": lap.get("max_heartrate"),
        }
        form = _lap_form(lap)
        if form:
            entry["form"] = form
        if times is not None and hrs is not None and zones is not None:
            entry["zone_secs"] = _lap_zone_secs_by_offset(
                offset_start, offset_end, times, hrs, zones
            )
        summary.append(entry)
    return summary


def _lap_zone_secs_by_offset(
    offset_start: float,
    offset_end: float,
    times: list,
    hrs: list,
    zones: list[tuple[int, int, str]],
) -> dict:
    """Slice per-zone seconds from a stream using elapsed-time offsets."""
    secs = {z[2]: 0 for z in zones}
    for i in range(len(times) - 1):
        t = times[i]
        if t < offset_start:
            continue
        if t >= offset_end:
            break
        dt = times[i + 1] - t
        hr = hrs[i]
        for low, high, zname in zones:
            if low <= hr <= high:
                secs[zname] += dt
                break
    return secs


def _session_category(
    zone_pcts: dict,
    drag_laps: list[dict],
    zones: list[tuple[int, int, str]],
    anchors: Optional[dict] = None,
    work_summary: Optional[dict] = None,
    session_band: Optional[dict] = None,
    session_total: float = 0,
) -> str:
    """Heuristic session classification anchored to the Bakken framework.

    Returns 'easy' | 'sub-threshold' | 'at-threshold' | 'vo2'.

    Intensity verdicts are anchored to the profile's bpm anchors
    (sub-threshold band / hard cap / LT2 via _parse_intensity_anchors),
    NOT zone boundaries — Z4 straddles the hard cap, so zone shares
    cannot separate at-cap sub-threshold work from over-threshold work.
    When stream-sliced work time is available it outranks momentary
    peaks: a couple of HR samples brushing LT2 in a hill do not make a
    threshold session — sustained SECONDS at/above LT2 do.

    Decision order:
    1. Z5 share >= 5% → 'vo2' (sustained top-end work).
    2. Drag count >= 3 and >= 50% of drags peak in Z5 → 'vo2' (short-rep
       VO2 style where peaks are brief but cover most reps).
    3. SHORT reps only (median drag < 180 s, where HR lag drags both the
       averages and time-in-band down): drag count >= 3 and >= 50% of
       drags peak >= LT2 → 'at-threshold'. On long reps a momentary peak
       at LT2 is normal HR behavior (hills, rep endings) and does NOT
       reclassify — the stream-time rule below judges those.
    4. Stream-sliced work time (when work_summary has band_secs and
       work >= 300 s — a lone stride must not classify a session): time
       at/above LT2 >= max(120 s, 10% of work) OR time above the hard
       cap >= 40% of work → 'at-threshold'. Minutes at LT2 = threshold
       work; isolated peak-seconds stay sub-threshold.
    5. Drags exist and median drag avg HR > hard cap → 'at-threshold'.
    6. Drags exist and median drag avg >= sub-band low (≈ Z3 low) →
       'sub-threshold'.
    7. No drag signal, stream band data available: LT2 time >= 15% or
       over-cap time >= 25% → 'at-threshold'; in-band time >= 10% or Z3
       share >= 10% → 'sub-threshold'; else 'easy'.
    8. No drag signal, no stream: Z4 share >= 25% → 'at-threshold';
       Z3 share >= 10% → 'sub-threshold'.
    9. Else → 'easy'.

    The 50% drag-max thresholds require >= 3 drags to be meaningful;
    sessions with only 1-2 drags fall through to the time/avg rules.
    """
    z3 = zone_pcts.get("Z3", 0)
    z4 = zone_pcts.get("Z4", 0)
    z5 = zone_pcts.get("Z5", 0)

    if z5 >= 5:
        return "vo2"

    z3_low = zones[2][0] if len(zones) >= 3 else 178
    z5_low = zones[4][0] if len(zones) >= 5 else 198
    anchors = anchors or _parse_intensity_anchors(zones)
    hard_cap = anchors["hard_cap"]
    lt2 = anchors["lt2_hr"]
    band_low = anchors["sub_band_low"]

    avgs = [lap["average_heartrate"] for lap in drag_laps if lap.get("average_heartrate") is not None]
    maxes = [lap["max_heartrate"] for lap in drag_laps if lap.get("max_heartrate") is not None]
    durations = [lap.get("elapsed_time") or 0 for lap in drag_laps]
    median_dur = sorted(durations)[len(durations) // 2] if durations else 0

    # Within-rep peak signals (need >= 3 drags for the share to be meaningful).
    if len(maxes) >= 3:
        peaks_in_z5 = sum(1 for m in maxes if m >= z5_low)
        if peaks_in_z5 / len(maxes) >= 0.5:
            return "vo2"
        if median_dur < 180:
            peaks_at_lt2 = sum(1 for m in maxes if m >= lt2)
            if peaks_at_lt2 / len(maxes) >= 0.5:
                return "at-threshold"

    work_band = (work_summary or {}).get("band_secs") or {}
    work_total = (work_summary or {}).get("work_total_secs") or 0
    # Share-based rules need a real work volume — a single sub-minute
    # surge (a stride) must not classify the whole session.
    if work_total >= 300:
        if work_band.get("at_or_above_lt2", 0) >= max(120, 0.10 * work_total):
            return "at-threshold"
        if work_band.get("over_cap", 0) >= 0.40 * work_total:
            return "at-threshold"

    if avgs:
        sorted_avgs = sorted(avgs)
        median_avg = sorted_avgs[len(sorted_avgs) // 2]
        if median_avg > hard_cap:
            return "at-threshold"
        if median_avg >= min(band_low, z3_low):
            return "sub-threshold"
        # Median drag avg is in Z2 — drags are likely noisy false-positives.
        # Fall through to aggregate fallback below.

    if session_band is not None and session_total > 0:
        if (
            session_band.get("at_or_above_lt2", 0) >= 0.15 * session_total
            or session_band.get("over_cap", 0) >= 0.25 * session_total
        ):
            return "at-threshold"
        if session_band.get("in_band", 0) >= 0.10 * session_total or z3 >= 10:
            return "sub-threshold"
        return "easy"

    if z4 >= 25:
        return "at-threshold"
    if z3 >= 10:
        return "sub-threshold"
    return "easy"


# ─── Per-rep interval analysis (timestamp-sliced) ─────────────────────
# Minimum lap duration (s) for a "drag" lap to count as a real work rep in
# the interval analysis. Garmin sometimes emits sub-2s ACTIVE auto-lap
# fragments at the end of a session (e.g. a 0.6 s, 1 m "lap"); these pass
# the HR validation trivially (single sample == Garmin avg) but are not
# reps. This filter is local to the per-rep analysis ONLY — it does NOT
# change _classify_laps or the existing `laps` output, which still report
# every drag lap as before.
_MIN_REP_SECONDS = 30

# Tolerance (bpm) between the plain stream-sliced mean of a rep and Garmin's
# authoritative lap average_heartrate. A larger gap means the timestamp slice
# is misaligned, so reconstructed sample stats (trimmed mean, drift) are not
# trustworthy and are suppressed (Task 3 validation guard).
_REP_SLICE_TOLERANCE_BPM = 3

# HR-lag onset window (s) trimmed from the start of a rep when computing
# trimmed_avg_hr — HR has not yet caught up to the effort in the first
# seconds of a work rep.
_HR_LAG_ONSET_SECONDS = 15
# Within-rep drift is only computed when the post-onset (settled) portion of a
# rep is at least this long — on shorter reps HR is still climbing throughout,
# so a within-rep "drift" number would just be HR kinetics, not a signal.
_MIN_DRIFT_SETTLED_SECONDS = 180


def _lap_start_offset(lap: dict, activity_start_s: float) -> Optional[float]:
    """Elapsed-seconds offset of a lap's start within the activity stream.

    Lap start timestamps come from Garmin's startTimeGMT (stored in the
    `start_date_local` field) and are UTC. `activity_start_s` is the UTC
    epoch of the activity start (the earliest lap's start). Returns the lap
    start as elapsed seconds from t=0, matching the stream's `time` array.
    """
    lap_start_str = lap.get("start_date_local") or lap.get("start_date")
    if not lap_start_str:
        return None
    s = lap_start_str.replace("+00:00", "Z")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp() - activity_start_s
        except ValueError:
            continue
    return None


def _activity_start_epoch(laps: list[dict]) -> Optional[float]:
    """UTC epoch of the activity start, anchored to the earliest lap start.

    The stream's elapsed-time array starts at 0 at the activity start, which
    is the start of the first lap. Using the earliest lap timestamp as the
    anchor lets every other lap be located in the stream by true timestamp.
    """
    epochs = []
    for lap in laps:
        lap_start_str = lap.get("start_date_local") or lap.get("start_date")
        if not lap_start_str:
            continue
        s = lap_start_str.replace("+00:00", "Z")
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
            try:
                epochs.append(
                    datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
                )
                break
            except ValueError:
                continue
    return min(epochs) if epochs else None


def _rep_hr_samples(
    lap: dict,
    activity_start_s: float,
    times: list,
    hrs: list,
) -> list[tuple[float, float]]:
    """(elapsed_offset_within_rep, hr) samples inside one lap's window."""
    offset_start = _lap_start_offset(lap, activity_start_s)
    elapsed = lap.get("elapsed_time")
    if offset_start is None or elapsed is None or not times or not hrs:
        return []
    offset_end = offset_start + elapsed
    out: list[tuple[float, float]] = []
    n = min(len(times), len(hrs))
    for i in range(n):
        t = times[i]
        if t < offset_start:
            continue
        if t >= offset_end:
            break
        out.append((t - offset_start, hrs[i]))
    return out


def _interval_analysis(
    classified: list[dict],
    activity_start_s: Optional[float],
    times: Optional[list],
    hrs: Optional[list],
    zones: list[tuple[int, int, str]],
    anchors: Optional[dict] = None,
) -> Optional[dict]:
    """Per-rep + work-summary analysis for sessions with work (drag) reps.

    Returns None when there are no real work reps. Mean/peak HR come from
    Garmin's authoritative per-lap fields; trimmed mean and drift are
    reconstructed from the timestamp-sliced stream and are ONLY emitted when
    the slice validates against Garmin's lap average (within tolerance).
    """
    drag_laps = [
        lap for lap in classified
        if lap.get("lap_type") == "drag"
        and (lap.get("elapsed_time") or 0) >= _MIN_REP_SECONDS
    ]
    if not drag_laps:
        return None

    have_stream = (
        activity_start_s is not None and times is not None and hrs is not None
    )
    anchors = anchors or _parse_intensity_anchors(zones)
    band_low = anchors["sub_band_low"]
    band_high = anchors["sub_band_high"]
    hard_cap = anchors["hard_cap"]
    lt2 = anchors["lt2_hr"]

    work_reps: list[dict] = []
    work_zone_secs = {z[2]: 0 for z in zones}
    work_band_secs = {"in_band": 0.0, "over_cap": 0.0, "at_or_above_lt2": 0.0}

    for idx, lap in enumerate(drag_laps, start=1):
        avg_speed = lap.get("average_speed") or 0
        pace = round(1000 / avg_speed, 1) if avg_speed > 0 else None
        lap_avg = lap.get("average_heartrate")
        rep: dict = {
            "rep_index": idx,
            "pace_s_per_km": pace,
            "avg_hr": lap_avg,
            "peak_hr": lap.get("max_heartrate"),
            "trimmed_avg_hr": None,
            "drift_bpm": None,
            "zone_secs": None,
            "band_secs": None,
            "samples_validated": False,
        }

        if have_stream:
            samples = _rep_hr_samples(lap, activity_start_s, times, hrs)
            hr_vals = [hr for _, hr in samples]
            if hr_vals and lap_avg is not None:
                sliced_mean = statistics.mean(hr_vals)
                validated = abs(sliced_mean - lap_avg) <= _REP_SLICE_TOLERANCE_BPM
                rep["samples_validated"] = validated
                if validated:
                    settled = [
                        (off, hr) for off, hr in samples
                        if off >= _HR_LAG_ONSET_SECONDS
                    ]
                    if settled:
                        rep["trimmed_avg_hr"] = round(
                            statistics.mean(hr for _, hr in settled), 1
                        )
                    # Within-rep drift is only meaningful on LONG reps, where HR
                    # plateaus in the Golden Zone and a late climb signals going
                    # out too hard. On short reps (e.g. 400 m / ~2 min) HR is
                    # still rising the whole rep, so "drift" is just kinetics —
                    # report null there and let across_rep_drift carry the signal.
                    settled_span = settled[-1][0] - settled[0][0] if settled else 0
                    third = len(settled) // 3
                    if settled_span >= _MIN_DRIFT_SETTLED_SECONDS and third >= 1:
                        svals = [hr for _, hr in settled]
                        rep["drift_bpm"] = round(
                            statistics.mean(svals[-third:])
                            - statistics.mean(svals[:third]), 1
                        )

            # Real time-in-zone over the drag lap, integrated from the same
            # timestamp-sliced samples (sample spacing from the offset deltas,
            # not an assumed 1 Hz) so it stays aligned with the validated slice.
            # Accumulated both per-rep (so the over-the-band story is visible
            # rep-by-rep, not just in aggregate) and into the work totals.
            # band_secs uses the profile's bpm anchors instead of zone
            # boundaries — Z4 straddles the hard cap, so zone_secs alone
            # can't separate at-cap from over-threshold time. The three
            # counters are independent flags, NOT a partition: over_cap and
            # at_or_above_lt2 overlap (LT2 > cap ⇒ LT2 time is also over
            # cap), and the sliver between band top and cap (e.g. 189-190)
            # counts in none of them — they don't sum to rep/work time.
            rep_zone_secs = {z[2]: 0 for z in zones}
            rep_band_secs = {"in_band": 0.0, "over_cap": 0.0, "at_or_above_lt2": 0.0}
            for i in range(len(samples) - 1):
                dt = samples[i + 1][0] - samples[i][0]
                if dt <= 0:
                    continue
                hr = samples[i][1]
                for low, high, zname in zones:
                    if low <= hr <= high:
                        rep_zone_secs[zname] += dt
                        work_zone_secs[zname] += dt
                        break
                if band_high is not None and band_low <= hr <= band_high:
                    rep_band_secs["in_band"] += dt
                if hr > hard_cap:
                    rep_band_secs["over_cap"] += dt
                if hr >= lt2:
                    rep_band_secs["at_or_above_lt2"] += dt
            if any(rep_zone_secs.values()):
                rep["zone_secs"] = {z: round(s) for z, s in rep_zone_secs.items()}
                rep["band_secs"] = {k: round(s) for k, s in rep_band_secs.items()}
                for k, s in rep_band_secs.items():
                    work_band_secs[k] += s

        work_reps.append(rep)

    rep_avgs = [r["avg_hr"] for r in work_reps if r["avg_hr"] is not None]
    rep_peaks = [r["peak_hr"] for r in work_reps if r["peak_hr"] is not None]
    work_total = sum(work_zone_secs.values())
    work_zone_pcts = {
        z: round(100 * s / work_total, 1) if work_total else 0
        for z, s in work_zone_secs.items()
    }

    return {
        "note": (
            "Whole-session zone_secs / zone_pcts / avg_hr MIX IN warmup, "
            "recoveries, and cooldown and are NOT the primary read for an "
            "interval session — use work_reps and work_summary (drag laps only)."
        ),
        "how_to_present": {
            "lead_with": (
                "A per-rep table: pace, avg_hr, peak_hr, and band_secs (time "
                "in the sub-threshold band / over the hard cap / at-or-above "
                "LT2). Then work_summary.band_secs for the work-only totals. "
                "Do NOT headline avg_hr / trimmed_avg_hr alone — on its own "
                "it hides what the reps actually cost."
            ),
            "band_vs_zones": (
                "Intensity verdicts come from band_secs (bpm-anchored to the "
                "user's sub-threshold band, hard cap, and LT2 — see "
                "band_anchors_bpm), NOT from zone_secs: Z4 straddles the hard "
                "cap, so 'time in Z4' mixes at-cap sub-threshold work with "
                "over-threshold work and overstates how hard the session was. "
                "The three band_secs counters are independent flags, not a "
                "partition — at_or_above_lt2 is a subset of over_cap, time "
                "between the band top and the cap counts in neither, and "
                "they do NOT sum to total time. For finer bpm slicing call "
                "hr_time_in_buckets(scope='work', edges=[...])."
            ),
            "hr_lag_caveat": (
                "HR lags effort by ~45-60s, so on SHORT reps (<~3 min, e.g. "
                "400m) HR spends the first half climbing through Z1/Z2 and "
                "often peaks AFTER the rep ends. avg_hr and trimmed_avg_hr are "
                "therefore dragged DOWN by the climb and understate the "
                "stimulus — judge short reps by peak_hr plus band_secs. On "
                "LONG reps (>~3 min) HR has settled, so avg and band_secs are "
                "the honest read there."
            ),
            "read_intensity_vs_intent": (
                "Judge over-band work by SECONDS, not peaks: "
                "band_secs.at_or_above_lt2 is threshold time, "
                "band_secs.over_cap is drift beyond the sub-threshold "
                "ceiling. A long rep whose peak briefly brushes the cap or "
                "LT2 (a hill, the rep ending) is normal HR behavior — it "
                "does NOT make the rep 'effectively threshold' unless "
                "meaningful time (minutes, not seconds) sits at/above LT2. "
                "Before reclassifying a sub-threshold session as threshold, "
                "check work_summary.band_secs and say how many seconds were "
                "actually over."
            ),
            "fade": (
                "Use across_rep_drift_bpm (last vs first rep avg) and the "
                "trend in peak_hr to judge fade/control across the session."
            ),
        },
        "work_reps": work_reps,
        "work_summary": {
            "rep_count": len(work_reps),
            "avg_rep_hr": round(statistics.mean(rep_avgs), 1) if rep_avgs else None,
            "avg_rep_peak_hr": (
                round(statistics.mean(rep_peaks), 1) if rep_peaks else None
            ),
            "across_rep_drift_bpm": (
                round(rep_avgs[-1] - rep_avgs[0], 1) if len(rep_avgs) >= 2 else None
            ),
            "work_zone_secs": work_zone_secs,
            "work_zone_pcts": work_zone_pcts,
            "work_total_secs": round(work_total, 1),
            "band_anchors_bpm": {
                "sub_band": (
                    f"{band_low}-{band_high}" if band_high is not None else f"{band_low}+"
                ),
                "hard_cap": hard_cap,
                "lt2": lt2,
                "source": anchors["source"],
            },
            "band_secs": {k: round(s) for k, s in work_band_secs.items()},
            "band_pcts": {
                k: round(100 * s / work_total, 1) if work_total else 0
                for k, s in work_band_secs.items()
            },
        },
    }


def hr_time_in_buckets(
    activity_id: int,
    edges: Optional[list[int]] = None,
    scope: str = "session",
) -> dict:
    """Bin HR-stream time into bpm buckets (data fn behind the MCP tool).

    See the `hr_time_in_buckets` @mcp.tool wrapper for the full contract.
    """
    _init_db()
    zones = _parse_zones()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT a.id, a.start_date_local, s.time_json, s.hr_json
            FROM activities a
            LEFT JOIN streams s ON s.activity_id = a.id
            WHERE a.id = ?
            """,
            (activity_id,),
        ).fetchone()

    if not row:
        return {
            "error": f"Activity {activity_id} not in local cache.",
            "next_steps": ["Run sync_activities() to pull recent activities."],
        }
    if not row["time_json"] or not row["hr_json"]:
        return {
            "error": f"Activity {activity_id} has no HR stream cached.",
            "next_steps": ["Run sync_activities() to (re)fetch the HR stream."],
        }

    times = json.loads(row["time_json"])
    hrs = json.loads(row["hr_json"])

    scope = scope if scope in ("session", "work") else "session"

    # Build the [start, end) elapsed-offset windows that count toward the bins.
    windows: Optional[list[tuple[float, float]]] = None
    if scope == "work":
        laps_raw = _cached_laps(activity_id) or []
        classified = _classify_laps(laps_raw, zones)
        activity_start_s = _activity_start_epoch(laps_raw)
        windows = []
        if activity_start_s is not None:
            for lap in classified:
                if lap.get("lap_type") != "drag":
                    continue
                if (lap.get("elapsed_time") or 0) < _MIN_REP_SECONDS:
                    continue
                offset_start = _lap_start_offset(lap, activity_start_s)
                elapsed = lap.get("elapsed_time")
                if offset_start is None or elapsed is None:
                    continue
                windows.append((offset_start, offset_start + elapsed))

    # Resolve edges → ascending cut points. Default = inner HR-zone boundaries.
    if edges:
        cut_points = sorted(int(e) for e in edges)
    else:
        # Zone lower bounds excluding the first (so buckets are <Z2low,
        # Z2..Z3low, ...). Each zone's low edge after the first.
        cut_points = [z[0] for z in zones[1:]]

    # Bucket labels: <c0, c0-(c1-1), ..., >=clast.
    n_buckets = len(cut_points) + 1
    bucket_secs = [0.0] * n_buckets

    def _bucket_index(hr: float) -> int:
        for i, c in enumerate(cut_points):
            if hr < c:
                return i
        return n_buckets - 1

    def _in_window(t: float) -> bool:
        if windows is None:
            return True
        return any(s <= t < e for s, e in windows)

    total_secs = 0.0
    n = min(len(times), len(hrs))
    for i in range(n - 1):
        dt = times[i + 1] - times[i]
        if dt <= 0:
            continue
        t = times[i]
        if not _in_window(t):
            continue
        bucket_secs[_bucket_index(hrs[i])] += dt
        total_secs += dt

    def _label(i: int) -> str:
        if i == 0:
            return f"<{cut_points[0]}" if cut_points else "all"
        if i == n_buckets - 1:
            return f"{cut_points[-1]}+"
        return f"{cut_points[i - 1]}-{cut_points[i] - 1}"

    buckets = [
        {
            "label": _label(i),
            "seconds": round(bucket_secs[i], 1),
            "percent": round(100 * bucket_secs[i] / total_secs, 1) if total_secs else 0,
        }
        for i in range(n_buckets)
    ]

    return {
        "id": row["id"],
        "scope": scope,
        "edges": cut_points,
        "edges_source": "custom" if edges else "hr_zones",
        "buckets": buckets,
        "total_seconds": round(total_secs, 1),
    }


def activity_breakdown(activity_id: int) -> dict:
    """Lap-level breakdown + zone distribution for one cached activity.

    Returns metadata, per-lap classification (drag/pause/wu/cd/easy),
    HR-zone time/percent, and a heuristic session_category.

    Laps are fetched from Garmin at sync time and cached locally.
    """
    _init_db()
    zones = _parse_zones()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT a.*, s.time_json, s.hr_json
            FROM activities a
            LEFT JOIN streams s ON s.activity_id = a.id
            WHERE a.id = ?
            """,
            (activity_id,),
        ).fetchone()

    if not row:
        return {
            "error": f"Activity {activity_id} not in local cache.",
            "next_steps": [
                "Run sync_activities() to pull recent activities.",
                "If the activity is older than 12 weeks, call "
                "sync_activities(weeks_back=N) with N covering the activity date.",
            ],
        }

    anchors = _parse_intensity_anchors(zones)
    zone_secs = {z[2]: 0 for z in zones}
    below_z1 = 0
    session_band: Optional[dict] = None
    stream_times: Optional[list[int]] = None
    stream_hrs: Optional[list[int]] = None
    if row["time_json"] and row["hr_json"]:
        stream_times = json.loads(row["time_json"])
        stream_hrs = json.loads(row["hr_json"])
        session_band = {"in_band": 0.0, "over_cap": 0.0, "at_or_above_lt2": 0.0}
        band_low = anchors["sub_band_low"]
        band_high = anchors["sub_band_high"]
        for i in range(len(stream_times) - 1):
            dt = stream_times[i + 1] - stream_times[i]
            hr = stream_hrs[i]
            placed = False
            for low, high, zname in zones:
                if low <= hr <= high:
                    zone_secs[zname] += dt
                    placed = True
                    break
            if not placed:
                below_z1 += dt
            if band_high is not None and band_low <= hr <= band_high:
                session_band["in_band"] += dt
            if hr > anchors["hard_cap"]:
                session_band["over_cap"] += dt
            if hr >= anchors["lt2_hr"]:
                session_band["at_or_above_lt2"] += dt

    total = sum(zone_secs.values()) + below_z1
    zone_pcts = {z: round(100 * s / total, 1) if total else 0 for z, s in zone_secs.items()}

    laps_raw = _cached_laps(activity_id)
    if laps_raw is None:
        laps_raw = []
        lap_fetch_error = "Laps not in cache — run sync_activities() to populate."
    else:
        lap_fetch_error = None

    classified = _classify_laps(laps_raw, zones)
    lap_summary = _summarize_laps(classified, None, stream_times, stream_hrs, zones)
    drag_laps = [lap for lap in classified if lap.get("lap_type") == "drag"]

    # Interval-aware per-rep analysis (timestamp-sliced) — added only when the
    # session has real work reps. Runs before classification so the category
    # can be judged on stream-sliced work seconds, not momentary lap peaks.
    activity_start_s = _activity_start_epoch(laps_raw) if laps_raw else None
    interval_analysis = _interval_analysis(
        classified, activity_start_s, stream_times, stream_hrs, zones, anchors
    )
    session_category = _session_category(
        zone_pcts,
        drag_laps,
        zones,
        anchors=anchors,
        work_summary=interval_analysis["work_summary"] if interval_analysis else None,
        session_band=session_band,
        session_total=total,
    )

    hint = name_hint(row["name"], row["sport_type"])
    result = {
        "id": row["id"],
        "date": row["start_date_local"][:10],
        "name": row["name"],
        "description": row["description"],
        "type": row["type"],
        "sport_type": row["sport_type"],
        "distance_m": row["distance_m"],
        "moving_time_s": row["moving_time_s"],
        "avg_hr": row["avg_hr"],
        "max_hr": row["max_hr"],
        "classification_hint": hint,
        "session_category": session_category,
        "zone_secs": zone_secs,
        "zone_pcts": zone_pcts,
        "band_anchors_bpm": {
            "sub_band": (
                f"{anchors['sub_band_low']}-{anchors['sub_band_high']}"
                if anchors["sub_band_high"] is not None
                else f"{anchors['sub_band_low']}+"
            ),
            "hard_cap": anchors["hard_cap"],
            "lt2": anchors["lt2_hr"],
            "source": anchors["source"],
        },
        "band_secs": (
            {k: round(s) for k, s in session_band.items()}
            if session_band is not None
            else None
        ),
        "below_z1_secs": below_z1,
        "laps": lap_summary,
        "lap_count": len(lap_summary),
        "has_stream_data": row["time_json"] is not None,
        "lap_fetch_error": lap_fetch_error,
        "performance_condition": _perf_cond_block(row),
        "weather": cached_weather(activity_id),
    }
    # Race detection: name patterns rarely help (Garmin default-names races
    # like any other run, e.g. "<City> Løping"), so also flag on effort —
    # >=15 min at/above LT2 does not occur in any Bakken-framework training
    # session, only in races/TTs.
    race_effort = (
        session_band is not None
        and session_band.get("at_or_above_lt2", 0) >= 900
    )
    if hint == "race" or race_effort:
        result["race_note"] = (
            "This is a RACE (or race-level effort: sustained time at/above "
            "LT2 that no planned session in this framework contains). "
            "session_category describes physiological load — a race reading "
            "'vo2' or 'at-threshold' is expected and correct, NOT an "
            "unplanned hard training session or a zone violation. Races "
            "<~60 min run at/above LT2 by design; do not use race avg HR to "
            "recalibrate the user's zones or LT2."
        )
    if interval_analysis is not None:
        result["zone_note"] = (
            "This is an interval session — whole-session zone_secs / zone_pcts "
            "/ avg_hr mix in warmup, recoveries, and cooldown. See "
            "interval_analysis.work_summary for the work-only read."
        )
        result["interval_analysis"] = interval_analysis
    return result


# ─── Performance condition ("ytelseskondisjon") ───────────────────────
def _perf_cond_block(row) -> Optional[dict]:
    """Session-level performance-condition summary from the perf_cond_*
    columns: the first value the watch showed (day-form read, 6-20 min
    in) and the last (where the session left it). None when Garmin never
    emitted the metric (indoor, walk, or stream not yet cached)."""
    first = row["perf_cond_first"]
    if first is None:
        return None
    last = row["perf_cond_last"]
    return {
        "first": first,
        "last": last,
        "change": round(last - first, 1) if last is not None else None,
        "min": row["perf_cond_min"],
        "max": row["perf_cond_max"],
        "avg": row["perf_cond_avg"],
        "how_to_read": (
            "first = today's pace/HR vs Garmin's RECENT baseline once the "
            "estimate settles (a day-form read, 0 = as expected); last = "
            "where the session left it; change = what the session cost. "
            "Intervals sag more than easy runs by design — compare like "
            "with like. Descriptive only, never a gate."
        ),
    }


def performance_condition(activity_id: int) -> dict:
    """Performance-condition time course for one cached activity.

    Garmin's performance condition is a real-time -20..+20 read of how the
    current effort's pace/HR relationship compares to the athlete's recent
    baseline. It is NOT a fitness score: 0 means "running as your recent
    baseline predicts", so a good session can sit at 0 all the way through.
    Garmin needs 6-20 minutes of running before it emits a first value.

    Returns:
      - first_value / first_at_min: the number that pops up on the watch,
        and how far into the activity it arrived.
      - min / max / avg / last, plus `drift` (last - first).
      - by_quarter: mean value per quarter of the sampled window — the shape
        of the curve, which is what the Connect chart draws.
      - per_lap: mean value per lap (with the lap's drag/pause/wu/cd tag),
        present when laps are cached. This is where an interval session's
        rep-to-rep decay shows up.
      - interpretation: how to read the numbers, including the baseline
        caveat below.

    Baseline caveat, worth stating whenever this is reported: the reference
    is Garmin's own recent-performance model, so a negative value can mean
    "slower than lately" OR "the baseline drifted up after a good block".
    Treat it as a within-session shape signal (does it hold or sag?) rather
    than an absolute verdict on the day.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT a.id, a.start_date_local, a.name, a.sport_type,
                   a.distance_m, a.moving_time_s, a.avg_hr, a.max_hr,
                   a.planned_type, a.vo2max_at_activity,
                   s.time_json, s.perf_condition_json, s.perf_fetched_at
            FROM activities a
            LEFT JOIN streams s ON s.activity_id = a.id
            WHERE a.id = ?
            """,
            (activity_id,),
        ).fetchone()

    if not row:
        return {
            "error": f"Activity {activity_id} not in local cache.",
            "next_steps": ["Run sync_activities() to pull recent activities."],
        }

    if not row["perf_condition_json"]:
        stale = row["perf_fetched_at"] is None
        return {
            "activity_id": activity_id,
            "date": row["start_date_local"][:10],
            "name": row["name"],
            "error": "No performance-condition samples cached for this activity.",
            "reason": (
                "This stream was cached before performance condition was "
                "stored — run sync_activities(backfill_streams=True)."
                if stale else
                "Garmin recorded none. Expected for activities shorter than "
                "~6-20 min, non-running sports, and sessions the watch could "
                "not baseline."
            ),
        }

    times = json.loads(row["time_json"] or "[]")
    pcs = json.loads(row["perf_condition_json"])
    samples = [(t, v) for t, v in zip(times, pcs) if v is not None]
    if not samples:
        return {
            "activity_id": activity_id,
            "error": "Performance-condition array is present but all-null.",
        }

    vals = [v for _, v in samples]
    first_t, first_v = samples[0]
    last_v = samples[-1][1]

    # Quarters of the SAMPLED window (not the whole activity) — the metric
    # only exists after its warm-up, so quartering the activity would put an
    # empty first bucket in the report.
    span_start, span_end = samples[0][0], samples[-1][0]
    span = span_end - span_start
    by_quarter = []
    for q in range(4):
        lo = span_start + span * q / 4
        hi = span_start + span * (q + 1) / 4
        bucket = [v for t, v in samples if (lo <= t < hi or (q == 3 and t == hi))]
        by_quarter.append({
            "quarter": q + 1,
            "from_min": round(lo / 60, 1),
            "to_min": round(hi / 60, 1),
            "avg": round(sum(bucket) / len(bucket), 1) if bucket else None,
            "samples": len(bucket),
        })

    per_lap = []
    laps_raw = _cached_laps(activity_id) or []
    if laps_raw:
        classified = _classify_laps(laps_raw, _parse_zones())
        cumulative = 0.0
        for lap in classified:
            start = cumulative
            cumulative += lap.get("elapsed_time") or 0
            bucket = [v for t, v in samples if start <= t < cumulative]
            per_lap.append({
                "lap_index": lap.get("lap_index"),
                "type": lap.get("lap_type"),
                "from_min": round(start / 60, 1),
                "avg": round(sum(bucket) / len(bucket), 1) if bucket else None,
                "min": min(bucket) if bucket else None,
                "max": max(bucket) if bucket else None,
            })

    drift = round(last_v - first_v, 1)
    if drift >= 1:
        drift_read = "rose through the session — effort felt easier as it went"
    elif drift <= -2:
        drift_read = (
            "sagged through the session — the pace/HR relationship "
            "deteriorated, the usual signature of fatigue or heat"
        )
    else:
        drift_read = "essentially flat — the effort held its pace/HR relationship"

    return {
        "activity_id": activity_id,
        "date": row["start_date_local"][:10],
        "name": row["name"],
        "sport_type": row["sport_type"],
        "planned_type": row["planned_type"],
        "distance_km": round((row["distance_m"] or 0) / 1000, 2),
        "avg_hr": row["avg_hr"],
        "vo2max_at_activity": row["vo2max_at_activity"],
        "first_value": first_v,
        "first_at_min": round(first_t / 60, 1),
        "min": min(vals),
        "max": max(vals),
        "avg": round(sum(vals) / len(vals), 1),
        "last": last_v,
        "drift": drift,
        "sample_count": len(vals),
        "by_quarter": by_quarter,
        "per_lap": per_lap,
        "interpretation": {
            "drift": drift_read,
            "scale": (
                "-20..+20, where 0 = matching Garmin's recent baseline for "
                "this athlete. Not a fitness score; sustained 0 is normal."
            ),
            "baseline_caveat": (
                "The reference is Garmin's own recent-performance model, so a "
                "negative run can mean 'slower than lately' OR 'the baseline "
                "moved up'. Read the within-session shape, not the absolute "
                "level, and never recalibrate zones or paces off it."
            ),
        },
    }


# ─── Running form (dynamics) from the local cache ─────────────────────
_FORM_FIELDS = (
    "avg_cadence_spm", "avg_ground_contact_ms", "avg_gct_balance_pct",
    "avg_vertical_osc_cm", "avg_vertical_ratio_pct", "avg_stride_length_cm",
    "avg_power_w", "norm_power_w", "avg_respiration_rate",
)


# Only these four have a direction in which "better" is meaningful, so only
# these get rated and given an improving/declining verdict. Stride length,
# power and respiration rate have no good side — a longer stride can be
# efficiency or overstriding, and respiration tracks effort — and
# ground-contact balance is best at 50 in either direction, so all four are
# reported as raw deltas instead.
_FORM_BANDS = {
    # field: ((elite, good, needs_work) thresholds, higher_is_better)
    "avg_cadence_spm": ((180, 170, 165), True),
    "avg_ground_contact_ms": ((200, 240, 260), False),
    "avg_vertical_osc_cm": ((6, 8, 10), False),
    "avg_vertical_ratio_pct": ((6, 8, 10), False),
}

# Smallest half-to-half change worth calling a trend. Below this a metric is
# "stable" — Garmin's wrist/accelerometer estimates carry more run-to-run
# noise than a bare percentage test allows for, and a 0.1 cm move in vertical
# oscillation is not a form change.
_FORM_NOISE_FLOOR = {
    "avg_cadence_spm": 1.5,
    "avg_ground_contact_ms": 5.0,
    "avg_vertical_osc_cm": 0.3,
    "avg_vertical_ratio_pct": 0.3,
}


# ─── Weather vs performance (descriptive) ─────────────────────────────
_TEMP_BINS = [(-99, 0, "<0"), (0, 5, "0-5"), (5, 10, "5-10"), (10, 15, "10-15"),
              (15, 20, "15-20"), (20, 99, ">=20")]
_WEATHER_OUTCOMES = ("perf_cond_first", "perf_cond_last", "perf_cond_change",
                     "workout_feel", "workout_rpe")
_WEATHER_INPUTS = ("temp_c", "dew_point_c", "relative_humidity", "wind_speed_kmh")


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 5 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return round(cov / (vx * vy), 2) if vx and vy else None


def _mean(vals: list) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _outcome_means(rows: list[dict]) -> dict:
    out = {"n": len(rows)}
    for key in _WEATHER_OUTCOMES:
        out[key] = _mean([r[key] for r in rows])
    out["avg_hr"] = _mean([r["avg_hr"] for r in rows])
    return out


def weather_performance(
    weeks_back: int = 52,
    classification: Optional[str] = None,
    sport_type: str = "Run",
    include_sessions: bool = False,
) -> dict:
    """Descriptive read of how conditions at the start of a session line up
    with performance condition (first / last / change), post-run feel and
    RPE. Groups by fixed temperature bins and wet vs dry, and reports
    Spearman rank correlations. It does not fit thresholds or rules.
    """
    _init_db()
    since = (datetime.now(timezone.utc) - timedelta(weeks=weeks_back)).date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        raw = conn.execute(
            """
            SELECT a.id, a.start_date_local, a.name, a.sport_type, a.planned_type,
                   a.avg_hr, a.distance_m, a.moving_time_s,
                   a.workout_rpe, a.workout_feel,
                   a.perf_cond_first, a.perf_cond_last,
                   w.temp_c, w.dew_point_c, w.relative_humidity,
                   w.wind_speed_kmh, w.conditions, w.precip_mm,
                   w.precip_prev_6h_mm, w.source
            FROM activities a JOIN activity_weather w ON w.activity_id = a.id
            WHERE a.sport_type = ? AND w.temp_c IS NOT NULL
              AND a.start_date_local >= ?
            ORDER BY a.start_date_local
            """,
            (sport_type, since),
        ).fetchall()
        unchecked = conn.execute(
            """
            SELECT COUNT(*) FROM activities a
            LEFT JOIN activity_weather w ON w.activity_id = a.id
            WHERE a.sport_type = ? AND a.start_date_local >= ?
              AND w.activity_id IS NULL
            """,
            (sport_type, since),
        ).fetchone()[0]

    rows: list[dict] = []
    for r in raw:
        cls, _src = classify_activity(r["name"], r["sport_type"], r["planned_type"])
        if classification and cls != classification:
            continue
        d = dict(r)
        d["classification"] = cls
        d["perf_cond_change"] = (
            round(r["perf_cond_last"] - r["perf_cond_first"], 1)
            if r["perf_cond_first"] is not None and r["perf_cond_last"] is not None
            else None
        )
        d["wet"] = _is_wet(d)
        d["month"] = r["start_date_local"][:7]
        rows.append(d)

    if not rows:
        return {
            "error": "No sessions with weather in that window.",
            "unchecked_activities": unchecked,
            "next_steps": ["sync_activities(backfill_coordinates=True) then "
                           "sync_activities(backfill_weather=True) to fetch "
                           "weather for cached history."],
        }

    by_temp = []
    for low, high, label in _TEMP_BINS:
        sub = [r for r in rows if low <= r["temp_c"] < high]
        if sub:
            by_temp.append({"temp_bin_c": label, **_outcome_means(sub)})

    wet = [r for r in rows if r["wet"]]
    dry = [r for r in rows if r["wet"] is False]
    by_wet = {"wet": _outcome_means(wet) if wet else None,
              "dry": _outcome_means(dry) if dry else None,
              "wet_definition": "≥0.2 mm precipitation during the activity's "
                                "hours or ≥1.0 mm in the six hours before "
                                "(Open-Meteo ERA5 at the start coordinates)."}

    correlations = {}
    for inp in _WEATHER_INPUTS:
        correlations[inp] = {}
        for out in _WEATHER_OUTCOMES:
            pairs = [(r[inp], r[out]) for r in rows
                     if r[inp] is not None and r[out] is not None]
            rho = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
            correlations[inp][out] = {"spearman": rho, "n": len(pairs)}

    by_class: dict[str, list] = {}
    for r in rows:
        by_class.setdefault(r["classification"], []).append(r)
    by_classification = {
        cls: {
            "n": len(sub),
            "temp_vs_perf_cond_first": _spearman(
                [r["temp_c"] for r in sub if r["perf_cond_first"] is not None],
                [r["perf_cond_first"] for r in sub if r["perf_cond_first"] is not None]),
            "temp_vs_perf_cond_change": _spearman(
                [r["temp_c"] for r in sub if r["perf_cond_change"] is not None],
                [r["perf_cond_change"] for r in sub if r["perf_cond_change"] is not None]),
            "temp_vs_feel": _spearman(
                [r["temp_c"] for r in sub if r["workout_feel"] is not None],
                [r["workout_feel"] for r in sub if r["workout_feel"] is not None]),
        }
        for cls, sub in sorted(by_class.items()) if len(sub) >= 5
    }

    months: dict[str, list] = {}
    for r in rows:
        months.setdefault(r["month"], []).append(r)
    monthly = [{"month": m, "n": len(sub), "temp_c": _mean([r["temp_c"] for r in sub]),
                "perf_cond_first": _mean([r["perf_cond_first"] for r in sub]),
                "workout_feel": _mean([r["workout_feel"] for r in sub])}
               for m, sub in sorted(months.items())]

    return {
        "window": {"since": since, "weeks_back": weeks_back, "sport_type": sport_type,
                   "classification": classification},
        "n_sessions": len(rows),
        "n_with_perf_cond": sum(1 for r in rows if r["perf_cond_first"] is not None),
        "weather_source_counts": dict(collections.Counter(r["source"] for r in rows)),
        "unchecked_activities": unchecked,
        "by_temperature": by_temp,
        "by_wet_dry": by_wet,
        "correlations": correlations,
        "by_classification": by_classification,
        "monthly": monthly,
        # Per-session rows only on request — 137 sessions × 16 fields is
        # ~50k characters, which blows the tool-output budget. The same rows
        # are one SQL join away in query_activity_cache.
        "sessions": (
            [
                {"id": r["id"], "date": r["start_date_local"][:10],
                 "cls": r["classification"], "temp_c": r["temp_c"], "wet": r["wet"],
                 "pc_first": r["perf_cond_first"], "pc_last": r["perf_cond_last"],
                 "feel": r["workout_feel"], "rpe": r["workout_rpe"]}
                for r in rows
            ]
            if include_sessions
            else f"{len(rows)} sessions omitted — pass include_sessions=True, or "
                 "join activities ⨝ activity_weather in query_activity_cache."
        ),
        "caveats": [
            "Temperature tracks the calendar, and so does fitness: a warm-month "
            "vs cold-month contrast is partly a training-block contrast. Use "
            "`monthly` to see how much of the spread is seasonal.",
            "Performance condition is measured against Garmin's RECENT baseline, "
            "so a hot spell that lasts weeks gets absorbed into the baseline and "
            "stops showing as negative.",
            "Session type dominates perf_cond_change (intervals sag, easy runs "
            "hold) — read `by_classification`, or pass `classification`.",
            "Conditions are ERA5 reanalysis (~9 km grid) at the start hour and "
            "coordinates — good for temperature, dew point and rain yes/no, "
            "coarse for local showers and wind. Feel is Garmin's 5-step "
            "post-run prompt (0/25/50/75/100), RPE 10-100.",
            "Descriptive: bins and correlations, no fitted cut-points.",
        ],
    }


def _rate_form(field: str, v: Optional[float]) -> Optional[str]:
    """Garmin's published benchmark bands for a running-dynamics average."""
    if v is None or field not in _FORM_BANDS:
        return None
    (elite, good, poor), higher_better = _FORM_BANDS[field]
    if higher_better:
        if v >= elite:
            return "elite"
        return "good" if v >= good else ("fair" if v >= poor else "needs_work")
    if v < elite:
        return "elite"
    return "good" if v <= good else ("fair" if v <= poor else "needs_work")


def running_form_trends(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    sport_type: str = "Run",
    classification: Optional[str] = None,
    limit: int = 200,
) -> dict:
    """Running-dynamics trends over the local cache (no Garmin calls).

    Form metrics are strongly pace-dependent — cadence and stride length rise
    with speed, ground contact falls — so a single average across easy runs
    and intervals is close to meaningless. Every row therefore carries its
    pace and classification, and the response breaks the averages down
    `by_classification`; compare like with like.
    """
    _init_db()
    limit = max(1, min(limit, 1000))

    where = ["1=1"]
    params: list[Any] = []
    if sport_type:
        where.append("sport_type = ?")
        params.append(sport_type)
    if start_date:
        where.append("date(start_date_local) >= ?")
        params.append(start_date)
    if end_date:
        where.append("date(start_date_local) <= ?")
        params.append(end_date)

    cols = ", ".join(_FORM_FIELDS)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, start_date_local, name, sport_type, planned_type,
                   distance_m, moving_time_s, avg_hr, {cols}
            FROM activities
            WHERE {' AND '.join(where)}
            ORDER BY start_date_local
            """,
            params,
        ).fetchall()
        missing_metrics = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE metrics_fetched_at IS NULL"
        ).fetchone()[0]

    per_activity = []
    for r in rows:
        if all(r[f] is None for f in _FORM_FIELDS):
            continue
        hint, _ = classify_activity(r["name"], r["sport_type"], r["planned_type"])
        if classification and hint != classification:
            continue
        pace = None
        if r["distance_m"] and r["moving_time_s"]:
            pace = round(r["moving_time_s"] / (r["distance_m"] / 1000), 1)
        entry = {
            "id": r["id"],
            "date": r["start_date_local"][:10],
            "classification": hint,
            "distance_km": round((r["distance_m"] or 0) / 1000, 2),
            "pace_s_per_km": pace,
            "avg_hr": r["avg_hr"],
        }
        for f in _FORM_FIELDS:
            entry[f] = round(r[f], 1) if r[f] is not None else None
        per_activity.append(entry)

    if not per_activity:
        return {
            "error": "No cached activities with running-dynamics data in range.",
            "activities_without_metrics": missing_metrics,
            "next_steps": [
                "Run sync_activities(backfill_activity_metrics=True) to "
                "populate dynamics for history synced before they were stored.",
            ],
        }

    def mean(items, field):
        vals = [i[field] for i in items if i.get(field) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    averages = {f: mean(per_activity, f) for f in _FORM_FIELDS}

    half = len(per_activity) // 2
    first_half, second_half = per_activity[:half], per_activity[half:]
    trends = {}
    for f in _FORM_FIELDS:
        a, b = mean(first_half, f), mean(second_half, f)
        if a is None or b is None:
            trends[f] = {"direction": "insufficient_data",
                         "first_half_avg": a, "second_half_avg": b}
            continue
        delta = b - a
        if f not in _FORM_BANDS:
            # No "better" direction — report the move, don't judge it.
            direction = "not_rated"
        elif abs(delta) < max(abs(a) * 0.02, _FORM_NOISE_FLOOR[f]):
            direction = "stable"
        else:
            higher_better = _FORM_BANDS[f][1]
            better = delta > 0 if higher_better else delta < 0
            direction = "improving" if better else "declining"
        trends[f] = {
            "direction": direction,
            "first_half_avg": a,
            "second_half_avg": b,
            "delta": round(delta, 1),
        }

    by_class: dict[str, dict] = {}
    for entry in per_activity:
        by_class.setdefault(entry["classification"], []).append(entry)
    by_classification = {
        cls: {
            "n": len(items),
            "avg_pace_s_per_km": mean(items, "pace_s_per_km"),
            **{f: mean(items, f) for f in _FORM_FIELDS},
        }
        for cls, items in sorted(by_class.items())
    }

    insights = []
    cad = averages["avg_cadence_spm"]
    if cad is not None and cad < 170:
        insights.append(
            f"Cadence averages {cad} spm across the window. Read it against "
            "pace before acting — the same runner turns over faster at "
            "threshold than on an easy run, so check the by_classification "
            "split rather than this pooled number."
        )
    bal = averages["avg_gct_balance_pct"]
    if bal is not None and abs(bal - 50) >= 2:
        side = "left" if bal > 50 else "right"
        insights.append(
            f"Ground-contact balance averages {bal}% left, i.e. {side}-biased "
            f"by {abs(round(bal - 50, 1))} points. Garmin's own guidance is "
            "that anything inside 50±2 is noise; outside it, look for a "
            "one-sided niggle."
        )
    vr = averages["avg_vertical_ratio_pct"]
    if vr is not None and vr > 10:
        insights.append(
            f"Vertical ratio averages {vr}% (bounce relative to stride "
            "length). The productive fix is usually a longer stride at the "
            "same cadence, not consciously trying to bounce less."
        )
    declining = [f for f, t in trends.items() if t["direction"] == "declining"]
    if declining:
        insights.append(
            "Declining across the window: " + ", ".join(declining) +
            ". Confirm the two halves contain a comparable mix of session "
            "types before reading this as real form decay — a block that "
            "shifted from intervals to easy volume will show exactly this."
        )

    return {
        "window": {
            "start_date": per_activity[0]["date"],
            "end_date": per_activity[-1]["date"],
            "activities": len(per_activity),
            "sport_type": sport_type,
            "classification_filter": classification,
        },
        "per_activity": per_activity[-limit:],
        "averages": averages,
        "by_classification": by_classification,
        "trends": trends,
        "ratings": {f: _rate_form(f, averages[f]) for f in _FORM_BANDS
                    if _rate_form(f, averages[f]) is not None},
        "insights": insights,
        "activities_without_metrics": missing_metrics,
        "notes": [
            "Benchmarks (Garmin): cadence >=180 elite / 170-179 good; ground "
            "contact <200 ms elite / 200-240 good; vertical oscillation "
            "<6 cm elite / 6-8 good; vertical ratio <6% elite / 6-8% good. "
            "They are population bands, not targets — a tall runner is "
            "structurally penalised on oscillation.",
            "Power here is the watch's wrist-based estimate, not a footpod or "
            "power meter. Useful for within-session comparison, not for "
            "absolute training prescription.",
            "Only cadence, ground contact, vertical oscillation and vertical "
            "ratio get an improving/declining verdict. Stride length, power "
            "and respiration rate have no inherently good direction, and "
            "ground-contact balance is best near 50 either way, so those are "
            "reported as raw deltas with direction 'not_rated'.",
        ],
    }


# ─── VO2max trend + per-session attribution ───────────────────────────
def vo2max_trend(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Garmin's VO2max day by day, plus which sessions preceded each move.

    Returns:
      - daily: [{date, vo2max, delta}] — the sparse series as Garmin wrote it
        (a value only lands on days with a qualifying activity).
      - summary: first / last / min / max / net change, and the peak's date.
      - by_session: each day the estimate moved, joined to that day's
        activities (name, classification, distance, avg_hr, training load),
        sorted by the size of the move — the closest thing available to
        "which sessions pushed it".
      - by_classification: mean move per session type, with counts.
      - caveats: read them before presenting any of this as causal.
    """
    _init_db()
    where = ["1=1"]
    params: list[Any] = []
    if start_date:
        where.append("date >= ?")
        params.append(start_date)
    if end_date:
        where.append("date <= ?")
        params.append(end_date)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT date, vo2max, vo2max_rounded, fitness_age FROM vo2max_daily "
            f"WHERE {' AND '.join(where)} ORDER BY date",
            params,
        ).fetchall()
        extent = conn.execute(
            "SELECT MIN(date) AS oldest, MAX(date) AS newest FROM vo2max_daily"
        ).fetchone()

    if not rows:
        return {
            "error": "No VO2max data cached for that range.",
            "coverage": {"cache_oldest": extent["oldest"] if extent else None,
                         "cache_newest": extent["newest"] if extent else None},
            "next_steps": [
                "Run sync_activities() to pull the trailing window, or "
                "sync_vo2max(start_date, end_date) for deeper history.",
            ],
        }

    daily = []
    prev = None
    for r in rows:
        v = r["vo2max"]
        delta = round(v - prev, 1) if (v is not None and prev is not None) else None
        daily.append({
            "date": r["date"],
            "vo2max": v,
            "vo2max_rounded": r["vo2max_rounded"],
            "delta": delta,
        })
        if v is not None:
            prev = v

    vals = [d["vo2max"] for d in daily if d["vo2max"] is not None]
    peak = max(daily, key=lambda d: d["vo2max"] if d["vo2max"] is not None else -1)

    # Join the days that moved to the activities recorded on them.
    moved = [d for d in daily if d["delta"]]
    by_session = []
    if moved:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            for d in moved:
                acts = conn.execute(
                    """
                    SELECT id, name, sport_type, planned_type, distance_m,
                           moving_time_s, avg_hr, activity_training_load,
                           aerobic_training_effect, anaerobic_training_effect
                    FROM activities WHERE date(start_date_local) = ?
                    ORDER BY start_date_local
                    """,
                    (d["date"],),
                ).fetchall()
                sessions = []
                for a in acts:
                    hint, _ = classify_activity(
                        a["name"], a["sport_type"], a["planned_type"]
                    )
                    sessions.append({
                        "id": a["id"],
                        "name": a["name"],
                        "classification": hint,
                        "sport_type": a["sport_type"],
                        "distance_km": round((a["distance_m"] or 0) / 1000, 2),
                        "avg_hr": a["avg_hr"],
                        "training_load": a["activity_training_load"],
                        "aerobic_te": a["aerobic_training_effect"],
                        "anaerobic_te": a["anaerobic_training_effect"],
                    })
                by_session.append({
                    "date": d["date"],
                    "vo2max": d["vo2max"],
                    "delta": d["delta"],
                    "sessions": sessions,
                })
        by_session.sort(key=lambda x: abs(x["delta"]), reverse=True)

    # Mean move per session type — only for days with exactly one activity,
    # so the attribution isn't split across a double day.
    agg: dict[str, list[float]] = {}
    for item in by_session:
        if len(item["sessions"]) != 1:
            continue
        agg.setdefault(item["sessions"][0]["classification"], []).append(item["delta"])
    by_classification = {
        cls: {
            "n": len(ds),
            "mean_delta": round(sum(ds) / len(ds), 2),
            "total_delta": round(sum(ds), 1),
        }
        for cls, ds in sorted(agg.items(), key=lambda kv: -abs(sum(kv[1])))
    }

    return {
        "window": {"start_date": daily[0]["date"], "end_date": daily[-1]["date"],
                   "days_with_value": len(vals)},
        "daily": daily,
        "summary": {
            "first": vals[0] if vals else None,
            "last": vals[-1] if vals else None,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "peak_date": peak["date"] if vals else None,
            "net_change": round(vals[-1] - vals[0], 1) if len(vals) > 1 else None,
        },
        "by_session": by_session,
        "by_classification": by_classification,
        "coverage": {
            "cache_oldest": extent["oldest"] if extent else None,
            "cache_newest": extent["newest"] if extent else None,
        },
        "caveats": [
            "The series is sparse by design — Garmin writes a value only on "
            "days with a qualifying (outdoor, HR-stable) run, so gaps mean "
            "'no new estimate', never 'fitness unmeasured'.",
            "Garmin's estimate is smoothed over recent history, so a day's "
            "delta is NOT that session's effect in isolation. `by_session` "
            "shows what preceded each move; it is association, not cause.",
            "The estimate is derived from the pace/HR relationship, so it is "
            "depressed by heat, hills, fatigue and wrist-HR error, and can "
            "drift down through a heavy block in which fitness is rising. "
            "Race results outrank it as evidence.",
            "by_classification counts only single-activity days, so a double "
            "day contributes to `by_session` but not to the per-type means.",
        ],
    }
