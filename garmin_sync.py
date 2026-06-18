"""Garmin activity sync + local cache.

On server startup (and via explicit `sync_activities` tool call) pulls new
activities from Garmin Connect since the last sync, including HR streams and
lap data for runs. Data lives in `coach_data/cache.db` so weekly summaries
and activity breakdowns don't hit the API on every query.
"""
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
    start_lon REAL
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(start_date_local);

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
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

CREATE TABLE IF NOT EXISTS laps (
    activity_id INTEGER PRIMARY KEY,
    laps_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
}

_STREAMS_MIGRATION_COLUMNS = {
    "elevation_json": "TEXT",
    "speed_json": "TEXT",
    "distance_json": "TEXT",
    "cadence_json": "TEXT",
}


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        for table, columns in (
            ("wellness_daily", _WELLNESS_MIGRATION_COLUMNS),
            ("activities", _ACTIVITY_MIGRATION_COLUMNS),
            ("streams", _STREAMS_MIGRATION_COLUMNS),
        ):
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, col_type in columns.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


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


def _garmin_get_stream(garmin_client, activity_id: int) -> Optional[dict]:
    """Fetch ~2s-resolution HR + elapsed stream (plus elevation, pace, distance,
    cadence) from Garmin activity details.

    Returns parallel arrays, all the same length, keyed:
      time (elapsed s), heartrate (bpm), elevation (m), speed (m/s),
      distance (cumulative m), cadence (spm).
    Samples are kept where HR and elapsed are both present; the extra
    metrics are aligned to those kept samples (None when a metric's
    descriptor is absent or its value is missing for a sample). maxchart=6000
    covers ~3.3 hours at 2s/sample. Returns None if there's no HR stream.
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


def _stream_extra_json(stream: dict) -> tuple:
    """Serialize the optional stream arrays (elevation, speed, distance,
    cadence) to JSON in column order. Stores NULL when a whole array is
    missing or entirely None (e.g. an indoor run with no elevation)."""
    out = []
    for key in ("elevation", "speed", "distance", "cadence"):
        arr = stream.get(key)
        if arr and any(v is not None for v in arr):
            out.append(json.dumps(arr))
        else:
            out.append(None)
    return tuple(out)


def _garmin_get_laps(garmin_client, activity_id: int) -> list[dict]:
    """Fetch lapDTOs from Garmin and normalise to our internal field names.

    Normalised lap dict:
      lap_index, average_heartrate, max_heartrate, distance, elapsed_time,
      moving_time, average_speed, start_date_local, intensityType.
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
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO laps (activity_id, laps_json, fetched_at) VALUES (?, ?, ?)",
            (activity_id, json.dumps(laps), datetime.now(timezone.utc).isoformat()),
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
                start_lon = COALESCE(?, start_lon)
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
                act_id,
            ),
        )


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
    """Populate the new stream columns (elevation/speed/distance/cadence) for
    cached activities whose streams row predates them (elevation_json IS NULL).

    Re-fetches the activity stream (one API call each), newest first, and
    UPDATEs the four new columns. Existing time/hr arrays are left untouched.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        ids = [r[0] for r in conn.execute(
            """
            SELECT s.activity_id
            FROM streams s JOIN activities a ON a.id = s.activity_id
            WHERE s.elevation_json IS NULL
            ORDER BY a.start_date_local DESC LIMIT ?
            """,
            (max_activities,),
        )]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM streams WHERE elevation_json IS NULL"
        ).fetchone()[0] - len(ids)

    updated = 0
    errors: list[str] = []
    for act_id in ids:
        try:
            stream = _garmin_get_stream(garmin_client, act_id)
        except Exception as e:
            errors.append(f"stream {act_id}: {type(e).__name__}: {e}")
            continue
        if not stream:
            continue
        elev, speed, dist, cad = _stream_extra_json(stream)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE streams SET elevation_json=?, speed_json=?, "
                "distance_json=?, cadence_json=? WHERE activity_id=?",
                (elev, speed, dist, cad, act_id),
            )
        updated += 1

    return {
        "streams_updated": updated,
        "remaining_without_streams": remaining,
        "errors": errors,
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
                    total_elevation_gain, synced_at, training_effect_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO streams (activity_id, time_json, "
                        "hr_json, elevation_json, speed_json, distance_json, "
                        "cadence_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (act_id, json.dumps(stream["time"]),
                         json.dumps(stream["heartrate"]),
                         *_stream_extra_json(stream)),
                    )
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
        "wellness_fetched": wellness_fetched,
        "wellness_cached": wellness_cached,
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
        r"stafett|etappe|race|parkrun|\bfun run\b|\b5k\b|\b10k\b|\bhalf marathon\b|\bmarathon\b", re.I)),
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


# ─── Weekly summary query ─────────────────────────────────────────────
def weekly_summary(start_date: str, end_date: str) -> dict:
    """Per-week aggregates from the local cache.

    Weeks are Mon-Sun. Zone time uses current bpm boundaries from
    coach://user_profile at query time, so retests automatically apply.

    Returns a dict with:
    - `weeks`: list of per-week aggregates (only weeks with activities).
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
            "total_distance_m": 0.0,
            "total_moving_time_s": 0,
            "session_count": 0,
            "run_session_count": 0,
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
            bucket["total_distance_m"] += r["distance_m"] or 0
            bucket["total_moving_time_s"] += r["moving_time_s"] or 0
        bucket["session_count"] += 1

    oldest = extent["oldest"] if extent else None
    gap_warning = bool(oldest and start_date < oldest)
    return {
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
                   workout_feel, workout_compliance
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
            out["sleep_seconds"] = s.get("sleepingSeconds")
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

    # Sleep score + stage breakdown live under get_sleep_data, not get_stats.
    # Sleep data is keyed to the date the sleep STARTED, i.e. typically the
    # date BEFORE the caller's "today" — caller decides which date to pass.
    try:
        sd = garmin_client.get_sleep_data(date_str)
        if sd and isinstance(sd, dict):
            dto = sd.get("dailySleepDTO") or {}
            scores = dto.get("sleepScores") or {}
            overall = scores.get("overall") or {}
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
    except Exception:
        pass

    return out


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
    yesterday_str: str,
    history_start: str,
    history_end: str,
) -> dict:
    """Build today's wellness snapshot + 7-day trend block.

    Pulled fresh from Garmin for today (so the snapshot reflects current
    sync state, not stale cache), backed by the cached wellness_daily
    history for the prior week.
    """
    today = _fetch_wellness_day(garmin_client, today_str)
    # Sleep data is keyed to the date the sleep STARTED — for "last night",
    # that's yesterday. Re-fetch and overwrite sleep_* from yesterday.
    sleep_last_night = _fetch_wellness_day(garmin_client, yesterday_str)
    for k in (
        "sleep_seconds", "sleep_score",
        "sleep_deep_s", "sleep_rem_s", "sleep_light_s", "sleep_awake_s",
    ):
        if sleep_last_night.get(k) is not None:
            today[k] = sleep_last_night[k]

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
    - HR-lag rescue: if a lap has max_hr in Z3+ AND its pace is within
      30 sec/km of the median pace of primary drags, it's reclassified
      as a drag. This catches the common case where the first rep's avg
      HR sits just below Z3 because HR hadn't caught up yet — pace + max
      both confirm it was actually a working rep.
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
            if abs(1000.0 / speed - median_pace) <= 30:
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
) -> str:
    """Heuristic session classification anchored to the Bakken framework.

    Returns 'easy' | 'sub-threshold' | 'at-threshold' | 'vo2'.

    Uses both drag AVG (Bakken-discipline signal) AND drag MAX (true
    within-rep intensity). Drag avg alone hides VO2-style sessions where
    each rep spikes briefly into Z5 — the time at Z5 is short, but the
    stimulus IS top-end. Drag max captures that.

    Decision order:
    1. Z5 share >= 5% → 'vo2' (sustained top-end work).
    2. Drag count >= 3 and >= 50% of drags peak in Z5 → 'vo2' (short-rep
       VO2 style where peaks are brief but cover most reps).
    3. Drag count >= 3 and >= 50% of drags peak >= LT2 → 'at-threshold'
       (reps consistently broke into Z4 by the end — beyond Bakken
       sub-threshold discipline).
    4. Drags exist and median drag avg HR > hard_cap (≈ LT2-4) →
       'at-threshold' (drag avg itself crossed Bakken's hard cap).
    5. Drags exist and median drag avg in Z3+ → 'sub-threshold'.
    6. No drag signal, but Z4 share >= 25% → 'at-threshold' (continuous
       tempo-style session without distinct rep structure).
    7. No drag signal, Z3 share >= 10% → 'sub-threshold'.
    8. Else → 'easy'.

    The 50% drag-max thresholds require >= 3 drags to be meaningful;
    sessions with only 1-2 drags fall through to the avg-based rules.
    """
    z3 = zone_pcts.get("Z3", 0)
    z4 = zone_pcts.get("Z4", 0)
    z5 = zone_pcts.get("Z5", 0)

    if z5 >= 5:
        return "vo2"

    z3_low = zones[2][0] if len(zones) >= 3 else 178
    z4_low = zones[3][0] if len(zones) >= 4 else 188
    z5_low = zones[4][0] if len(zones) >= 5 else 198
    hard_cap = z4_low + 2  # Bakken's documented hard cap ≈ LT2 - 4
    lt2_est = z4_low + 6   # LT2 ≈ Z4 midpoint for typical user zones

    avgs = [lap["average_heartrate"] for lap in drag_laps if lap.get("average_heartrate") is not None]
    maxes = [lap["max_heartrate"] for lap in drag_laps if lap.get("max_heartrate") is not None]

    # Within-rep peak signals (need >= 3 drags for the share to be meaningful).
    if len(maxes) >= 3:
        peaks_in_z5 = sum(1 for m in maxes if m >= z5_low)
        if peaks_in_z5 / len(maxes) >= 0.5:
            return "vo2"
        peaks_at_lt2 = sum(1 for m in maxes if m >= lt2_est)
        if peaks_at_lt2 / len(maxes) >= 0.5:
            return "at-threshold"

    if avgs:
        sorted_avgs = sorted(avgs)
        median_avg = sorted_avgs[len(sorted_avgs) // 2]
        if median_avg > hard_cap:
            return "at-threshold"
        if median_avg >= z3_low:
            return "sub-threshold"
        # Median drag avg is in Z2 — drags are likely noisy false-positives.
        # Fall through to aggregate-zone fallback below.

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

    work_reps: list[dict] = []
    work_zone_secs = {z[2]: 0 for z in zones}

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
            for i in range(len(samples) - 1):
                dt = samples[i + 1][0] - samples[i][0]
                if dt <= 0:
                    continue
                hr = samples[i][1]
                for low, high, zname in zones:
                    if low <= hr <= high:
                        work_zone_secs[zname] += dt
                        break

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

    zone_secs = {z[2]: 0 for z in zones}
    below_z1 = 0
    stream_times: Optional[list[int]] = None
    stream_hrs: Optional[list[int]] = None
    if row["time_json"] and row["hr_json"]:
        stream_times = json.loads(row["time_json"])
        stream_hrs = json.loads(row["hr_json"])
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
    session_category = _session_category(zone_pcts, drag_laps, zones)

    # Interval-aware per-rep analysis (timestamp-sliced) — added only when the
    # session has real work reps. Existing fields above are unchanged.
    activity_start_s = _activity_start_epoch(laps_raw) if laps_raw else None
    interval_analysis = _interval_analysis(
        classified, activity_start_s, stream_times, stream_hrs, zones
    )

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
        "classification_hint": name_hint(row["name"], row["sport_type"]),
        "session_category": session_category,
        "zone_secs": zone_secs,
        "zone_pcts": zone_pcts,
        "below_z1_secs": below_z1,
        "laps": lap_summary,
        "lap_count": len(lap_summary),
        "has_stream_data": row["time_json"] is not None,
        "lap_fetch_error": lap_fetch_error,
    }
    if interval_analysis is not None:
        result["zone_note"] = (
            "This is an interval session — whole-session zone_secs / zone_pcts "
            "/ avg_hr mix in warmup, recoveries, and cooldown. See "
            "interval_analysis.work_summary for the work-only read."
        )
        result["interval_analysis"] = interval_analysis
    return result
