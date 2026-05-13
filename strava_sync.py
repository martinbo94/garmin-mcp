"""Strava sync + local cache.

On server startup (and via explicit `sync_activities` tool call) pulls new
activities from Strava since the last sync, including HR streams for runs.
Data lives in `coach_data/cache.db` so weekly summaries don't hit Strava
on every query.
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

ROOT = Path(__file__).parent
TOKEN_FILE = Path.home() / ".config" / "strava-mcp" / "config.json"
DB_PATH = ROOT / "coach_data" / "cache.db"
USER_PROFILE_PATH = ROOT / "coach_data" / "user_profile.md"
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_OAUTH = "https://www.strava.com/oauth/token"
INITIAL_BACKFILL_WEEKS = 12


# ─── Token management ─────────────────────────────────────────────────
def _load_tokens() -> dict:
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"Strava token file not found at {TOKEN_FILE}. "
            "Run 'connect-strava' via the Strava MCP first."
        )
    return json.loads(TOKEN_FILE.read_text())


def _save_tokens(tokens: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))


def _refresh_if_needed(tokens: dict) -> dict:
    if tokens.get("expiresAt", 0) > time.time() + 60:
        return tokens
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env for token refresh."
        )
    resp = httpx.post(STRAVA_OAUTH, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": tokens["refreshToken"],
    }, timeout=30)
    resp.raise_for_status()
    new = resp.json()
    tokens.update({
        "accessToken": new["access_token"],
        "refreshToken": new["refresh_token"],
        "expiresAt": new["expires_at"],
    })
    _save_tokens(tokens)
    return tokens


def _access_token() -> str:
    return _refresh_if_needed(_load_tokens())["accessToken"]


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
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(start_date_local);

CREATE TABLE IF NOT EXISTS streams (
    activity_id INTEGER PRIMARY KEY,
    time_json TEXT NOT NULL,
    hr_json TEXT NOT NULL,
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


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


# ─── Strava API ───────────────────────────────────────────────────────
def _strava_get(path: str, params: Optional[dict] = None, timeout: float = 30) -> Any:
    resp = httpx.get(
        f"{STRAVA_API}{path}",
        headers={"Authorization": f"Bearer {_access_token()}"},
        params=params or {},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _list_activities_since(after: datetime) -> list[dict]:
    """Paginate through activities with start_date > after."""
    activities: list[dict] = []
    page = 1
    while True:
        batch = _strava_get("/athlete/activities", {
            "after": int(after.timestamp()),
            "page": page,
            "per_page": 100,
        })
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return activities


def _get_activity_detail(activity_id: int) -> dict:
    return _strava_get(f"/activities/{activity_id}")


def _get_activity_streams(activity_id: int) -> Optional[dict]:
    """Time + heartrate at low resolution. None if no HR stream available."""
    try:
        data = _strava_get(
            f"/activities/{activity_id}/streams",
            {
                "keys": "time,heartrate",
                "key_by_type": "true",
                "resolution": "low",
                "series_type": "distance",
            },
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    if "heartrate" not in data:
        return None
    return {
        "time": data["time"]["data"],
        "heartrate": data["heartrate"]["data"],
    }


# ─── Sync ─────────────────────────────────────────────────────────────
def run_sync(force_full: bool = False) -> dict:
    """Pull new activities + streams. Incremental unless force_full=True."""
    _init_db()

    if force_full or _get_last_sync() is None:
        after = datetime.now(timezone.utc) - timedelta(weeks=INITIAL_BACKFILL_WEEKS)
    else:
        after = _get_last_sync()  # type: ignore[assignment]

    sync_start = datetime.now(timezone.utc)

    try:
        activities = _list_activities_since(after)
    except Exception as e:
        return {"error": f"Failed to fetch activity list: {type(e).__name__}: {e}"}

    new_count = 0
    streams_count = 0
    errors: list[str] = []

    for act in activities:
        if _activity_exists(act["id"]):
            continue
        try:
            detail = _get_activity_detail(act["id"])
        except Exception as e:
            errors.append(f"detail {act['id']}: {e}")
            continue

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO activities (
                    id, start_date_local, name, description, type, sport_type,
                    distance_m, moving_time_s, elapsed_time_s, avg_hr, max_hr,
                    total_elevation_gain, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    act["id"], act["start_date_local"], act["name"],
                    detail.get("description"),
                    act.get("type"), act.get("sport_type"),
                    act.get("distance"), act.get("moving_time"),
                    act.get("elapsed_time"),
                    act.get("average_heartrate"), act.get("max_heartrate"),
                    act.get("total_elevation_gain"),
                    sync_start.isoformat(),
                ),
            )
        new_count += 1

        if act.get("type") == "Run" and act.get("has_heartrate"):
            try:
                streams = _get_activity_streams(act["id"])
            except Exception as e:
                errors.append(f"stream {act['id']}: {e}")
                continue
            if streams:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "INSERT INTO streams (activity_id, time_json, hr_json) VALUES (?, ?, ?)",
                        (act["id"], json.dumps(streams["time"]), json.dumps(streams["heartrate"])),
                    )
                streams_count += 1

    _set_last_sync(sync_start)

    return {
        "new_activities": new_count,
        "streams_fetched": streams_count,
        "errors": errors,
        "last_sync": sync_start.isoformat(),
        "since": after.isoformat(),
    }


# ─── Name-based classification hint ───────────────────────────────────
_NAME_PATTERNS = [
    ("prog-long", re.compile(r"progressiv langtur|progressive long", re.I)),
    ("long", re.compile(r"langtur|long run", re.I)),
    ("threshold", re.compile(r"terskel|subterskel|threshold", re.I)),
    ("tempo", re.compile(r"tempo", re.I)),
    ("intervals", re.compile(r"intervall|pyramide|vo2", re.I)),
    ("race", re.compile(r"stafett|etappe", re.I)),
]
_DEFAULT_RUN_NAMES = re.compile(r"^(morning|afternoon|evening|lunch)\s+run", re.I)


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
def weekly_summary(start_date: str, end_date: str) -> list[dict]:
    """Per-week aggregates from the local cache.

    Weeks are Mon-Sun. Zone time uses current bpm boundaries from
    coach://user_profile at query time, so retests automatically apply.
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
            "classification_hint": name_hint(r["name"], r["sport_type"]),
            "zone_secs": act_zones if r["time_json"] else None,
        })
        if r["type"] == "Run":
            bucket["run_session_count"] += 1
            bucket["total_distance_m"] += r["distance_m"] or 0
            bucket["total_moving_time_s"] += r["moving_time_s"] or 0
        bucket["session_count"] += 1

    return list(weeks.values())


def activity_breakdown(activity_id: int) -> dict:
    """Single activity with zone time breakdown computed from cached streams."""
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
        return {"error": f"Activity {activity_id} not in cache. Try sync_activities first."}

    zone_secs = {z[2]: 0 for z in zones}
    below_z1 = 0
    if row["time_json"] and row["hr_json"]:
        times = json.loads(row["time_json"])
        hrs = json.loads(row["hr_json"])
        for i in range(len(times) - 1):
            dt = times[i + 1] - times[i]
            hr = hrs[i]
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
        "zone_secs": zone_secs,
        "zone_pcts": zone_pcts,
        "below_z1_secs": below_z1,
        "has_stream_data": row["time_json"] is not None,
    }
