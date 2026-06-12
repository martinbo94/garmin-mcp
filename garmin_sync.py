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
    detail_fetched_at TEXT
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
}


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        for table, columns in (
            ("wellness_daily", _WELLNESS_MIGRATION_COLUMNS),
            ("activities", _ACTIVITY_MIGRATION_COLUMNS),
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
    """Fetch ~2s-resolution HR+elapsed stream from Garmin activity details.

    Returns {"time": [...elapsed_s...], "heartrate": [...bpm...]} or None.
    maxchart=6000 covers ~3.3 hours at 2s/sample.
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
    elapsed_list: list[float] = []
    hr_list: list[float] = []
    for m in metrics:
        vals = m.get("metrics", [])
        hr = vals[hr_idx] if hr_idx < len(vals) else None
        elapsed = vals[elapsed_idx] if elapsed_idx < len(vals) else None
        if hr is not None and elapsed is not None:
            elapsed_list.append(elapsed)
            hr_list.append(hr)
    return {"time": elapsed_list, "heartrate": hr_list} if hr_list else None


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
                detail_fetched_at = ?
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


def run_sync(
    garmin_client,
    force_full: bool = False,
    weeks_back: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> dict:
    """Pull new activities + streams + laps from Garmin into the local cache.

    Args:
        garmin_client: Authenticated garminconnect.Garmin instance.
        force_full: If True, re-pull the default 12-week backfill window.
        weeks_back: Optional explicit backfill window in weeks.
        since: Explicit start datetime (overrides weeks_back/force_full).
        until: Optional end datetime — only activities before this are synced.
            Used for month-by-month backfills.
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
                        "INSERT OR IGNORE INTO streams (activity_id, time_json, hr_json) VALUES (?, ?, ?)",
                        (act_id, json.dumps(stream["time"]), json.dumps(stream["heartrate"])),
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

    _set_last_sync(sync_start)

    return {
        "new_activities": new_count,
        "streams_fetched": streams_count,
        "laps_fetched": laps_count,
        "details_fetched": details_count,
        "errors": errors,
        "last_sync": sync_start.isoformat(),
        "since": after.isoformat(),
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

    return {
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
