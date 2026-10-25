"""Strength workouts: Garmin's exercise catalog, step building for
structured strength workouts, the reusable-template registry, and the
exercise sets Garmin records for a strength activity.

Schema facts verified against Garmin (2026-10-25, /workout-service/workout/types
and a create/read-back round trip): sport strength_training = 5, step types
interval = 3 / rest = 5 / repeat = 6 (warmup 1, cooldown 2), end conditions
reps = 10 / time = 2 / lap.button = 1 / iterations = 7. Exercise steps carry
`category` + `exerciseName` (exerciseName may be null: the watch then shows
the category) and optional `weightValue` + `weightUnit` (kilogram).
"""
import difflib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import garmin_sync
from core import StrengthBlock

CATALOG_URL = "https://connect.garmin.com/web-data/exercises/Exercises.json"
NAMES_URL = "https://connect.garmin.com/web-translations/exercise_types/exercise_types.properties"
CATALOG_MAX_AGE_DAYS = 30

SPORT = {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 3}
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_STEP_TYPES = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "main": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
}
_ST_REST = {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5}
_ST_REPEAT = {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}
_EC_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True}
_EC_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
_EC_LAP = {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}
_EC_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7,
                  "displayable": False}
_KILOGRAM = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}


# ─── Exercise catalog ─────────────────────────────────────────────────
def _catalog_age_days() -> Optional[float]:
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = 'exercise_catalog_at'").fetchone()
    if not row:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 86400


def _fetch_catalog() -> list[tuple]:
    import requests

    cats = requests.get(CATALOG_URL, timeout=30)
    cats.raise_for_status()
    names: dict[str, str] = {}
    try:
        r = requests.get(NAMES_URL, timeout=30)
        r.raise_for_status()
        for line in r.text.splitlines():
            key, sep, val = line.partition("=")
            if sep:
                names[key.strip()] = val.strip()
    except Exception:
        pass  # display names are a nicety; keys alone still work
    rows = []
    for category, body in (cats.json().get("categories") or {}).items():
        for ex, info in (body.get("exercises") or {}).items():
            display = names.get(f"{category}_{ex}") or ex.replace("_", " ").strip().title()
            rows.append((category, ex, display, ",".join(info.get("primaryMuscles") or [])))
    return rows


def load_catalog(force: bool = False) -> int:
    """Make sure the local catalog is filled and under a month old. Keeps
    the old copy if Garmin cannot be reached. Returns the exercise count."""
    garmin_sync._init_db()
    age = _catalog_age_days()
    if force or age is None or age > CATALOG_MAX_AGE_DAYS:
        try:
            rows = _fetch_catalog()
        except Exception:
            rows = []
            if age is None:
                raise
        if rows:
            with sqlite3.connect(garmin_sync.DB_PATH) as conn:
                conn.execute("DELETE FROM exercise_catalog")
                conn.executemany("INSERT INTO exercise_catalog VALUES (?, ?, ?, ?)", rows)
                conn.execute(
                    "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('exercise_catalog_at', ?)",
                    (datetime.now(timezone.utc).isoformat(),))
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM exercise_catalog").fetchone()[0]


def _norm(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def search_exercises(query: str, category: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Rank catalog exercises by how many query words they contain (key,
    display name or category), then by closeness of the display name."""
    load_catalog()
    words = _norm(query)
    sql = "SELECT category, exercise_name, display_name, primary_muscles FROM exercise_catalog"
    args: tuple = ()
    if category:
        sql += " WHERE category = ?"
        args = (category.upper(),)
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        rows = conn.execute(sql, args).fetchall()
    scored = []
    for cat, ex, display, muscles in rows:
        hay = set(_norm(f"{ex} {display} {cat}"))
        hits = sum(1 for w in words if w in hay or any(h.startswith(w) for h in hay))
        if words and not hits:
            continue
        closeness = difflib.SequenceMatcher(None, query.lower(), (display or ex).lower()).ratio()
        scored.append((-hits, -closeness, len(ex), cat, ex, display, muscles))
    scored.sort()
    return [{"category": c, "exercise_name": e, "display_name": d,
             "primary_muscles": m.split(",") if m else []}
            for _, _, _, c, e, d, m in scored[:limit]]


def _check_exercise(category: str, exercise_name: Optional[str]) -> None:
    load_catalog()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        if not conn.execute("SELECT 1 FROM exercise_catalog WHERE category = ? LIMIT 1",
                            (category,)).fetchone():
            cats = [r[0] for r in conn.execute("SELECT DISTINCT category FROM exercise_catalog")]
            close = difflib.get_close_matches(category, cats, n=5, cutoff=0.4)
            raise ValueError(f"Unknown exercise category {category!r}. Closest: {close}. "
                             "Use list_strength_exercises to find the right one.")
        if exercise_name is None:
            return
        if conn.execute("SELECT 1 FROM exercise_catalog WHERE category = ? AND exercise_name = ?",
                        (category, exercise_name)).fetchone():
            return
    same_cat = search_exercises(exercise_name.replace("_", " "), category=category, limit=5)
    anywhere = search_exercises(exercise_name.replace("_", " "), limit=5)
    raise ValueError(
        f"Unknown exercise {category}/{exercise_name}. In {category}: "
        f"{[s['exercise_name'] for s in same_cat]}; anywhere: "
        f"{[(s['category'], s['exercise_name']) for s in anywhere]}. "
        "Or omit exercise_name and put the real name in `note`.")


# ─── Building workout steps ───────────────────────────────────────────
def _reps_value(reps) -> tuple[float, Optional[str]]:
    """'6-10' → (10, '6-10 reps'); 8 → (8, None)."""
    if isinstance(reps, int):
        return float(reps), None
    m = re.fullmatch(r"\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*", str(reps))
    if not m:
        raise ValueError(f"reps must be a number or a range like '6-10', got {reps!r}")
    low, high = int(m.group(1)), m.group(2)
    if high is None:
        return float(low), None
    return float(high), f"{low}-{high} reps"


def build_steps(blocks: list[StrengthBlock], side_labels: tuple[str, str] = ("right", "left")) -> list:
    """Turn blocks into Garmin workout steps. A block with more than one
    round becomes a RepeatGroupDTO; per_side exercises become two steps."""
    for block in blocks:
        for ex in block.exercises:
            _check_exercise(ex.category, ex.exercise_name)

    steps: list = []
    order = 0
    group_id = 0

    def next_order() -> int:
        nonlocal order
        order += 1
        return order

    def rest_step(value, child: Optional[int]) -> dict:
        ec, val = (_EC_LAP, None) if value == "lap" else (_EC_TIME, float(value))
        s = {"type": "ExecutableStepDTO", "stepOrder": next_order(), "stepType": _ST_REST,
             "endCondition": ec, "endConditionValue": val, "targetType": _NO_TARGET}
        if child:
            s["childStepId"] = child
        return s

    for block in blocks:
        repeated = block.rounds > 1
        if repeated:
            group_id += 1
            group = {"type": "RepeatGroupDTO", "stepOrder": next_order(), "childStepId": group_id,
                     "stepType": _ST_REPEAT, "numberOfIterations": block.rounds,
                     "endCondition": _EC_ITERATIONS, "endConditionValue": float(block.rounds),
                     "smartRepeat": False, "skipLastRestStep": False, "workoutSteps": []}
            steps.append(group)
            target, child = group["workoutSteps"], group_id
        else:
            target, child = steps, None

        for i, ex in enumerate(block.exercises):
            if ex.reps is not None:
                ec, val = _EC_REPS, None
                val, range_note = _reps_value(ex.reps)
            elif ex.duration_s is not None:
                ec, val, range_note = _EC_TIME, float(ex.duration_s), None
            else:
                ec, val, range_note = _EC_LAP, None, None
            base_note = " · ".join(t for t in (ex.note, range_note) if t)
            for side in (side_labels if ex.per_side else (None,)):
                s = {"type": "ExecutableStepDTO", "stepOrder": next_order(),
                     "stepType": _STEP_TYPES[block.kind], "endCondition": ec,
                     "endConditionValue": val, "targetType": _NO_TARGET,
                     "category": ex.category, "exerciseName": ex.exercise_name}
                note = " · ".join(t for t in (base_note, side) if t)
                if note:
                    s["description"] = note
                if ex.weight_kg is not None:
                    s["weightValue"] = float(ex.weight_kg)
                    s["weightUnit"] = _KILOGRAM
                if child:
                    s["childStepId"] = child
                target.append(s)
            last = i == len(block.exercises) - 1
            if not last and block.rest_between_exercises_s:
                target.append(rest_step(block.rest_between_exercises_s, child))
        if block.rest_after_round_s:
            target.append(rest_step(block.rest_after_round_s, child))
    return steps


def workout_payload(name: str, steps: list, description: Optional[str]) -> dict:
    w = {"workoutName": name, "sportType": SPORT,
         "workoutSegments": [{"segmentOrder": 1, "sportType": SPORT, "workoutSteps": steps}]}
    if description:
        w["description"] = description
    return w


def summarize_steps(steps: list) -> list[str]:
    """One line per block, for tool results: what the watch will show."""
    def line(s) -> str:
        ec = s["endCondition"]["conditionTypeKey"]
        if s["stepType"]["stepTypeKey"] == "rest":
            return "rest " + ("until lap" if ec == "lap.button" else f"{int(s['endConditionValue'])} s")
        amount = {"reps": f"{int(s['endConditionValue'])} reps",
                  "time": f"{int(s['endConditionValue'])} s"}.get(ec, "until lap")
        what = s.get("exerciseName") or s.get("category")
        kg = f" @ {s['weightValue']:g} kg" if s.get("weightValue") else ""
        note = f" ({s['description']})" if s.get("description") else ""
        return f"{what} {amount}{kg}{note}"
    out = []
    for s in steps:
        if s["type"] == "RepeatGroupDTO":
            out.append(f"{s['numberOfIterations']}× [ " + " → ".join(line(c) for c in s["workoutSteps"]) + " ]")
        else:
            out.append(line(s))
    return out


# ─── Template registry ────────────────────────────────────────────────
def remember_template(name: str, workout_id: int, old_name: Optional[str] = None) -> None:
    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        if old_name and old_name != name:
            conn.execute("DELETE FROM strength_templates WHERE name = ?", (old_name,))
        conn.execute("DELETE FROM strength_templates WHERE workout_id = ?", (workout_id,))
        conn.execute("INSERT OR REPLACE INTO strength_templates VALUES (?, ?, ?)",
                     (name, workout_id, datetime.now(timezone.utc).isoformat()))


def template_id(name: str) -> Optional[int]:
    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        row = conn.execute("SELECT workout_id FROM strength_templates WHERE name = ?",
                           (name,)).fetchone()
    return row[0] if row else None


def list_templates() -> list[dict]:
    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        return [{"name": n, "workout_id": w, "updated_at": u}
                for n, w, u in conn.execute(
                    "SELECT name, workout_id, updated_at FROM strength_templates ORDER BY name")]


# ─── Recorded sets ────────────────────────────────────────────────────
def fetch_and_store_sets(garmin_client, activity_id: int) -> int:
    """Fetch Garmin's exercise sets for one strength activity into
    strength_sets (replacing earlier rows) and mark it fetched. Returns the
    number of sets stored."""
    garmin_sync._init_db()
    data = garmin_client.get_activity_exercise_sets(activity_id) or {}
    rows = []
    for i, st in enumerate(data.get("exerciseSets") or []):
        guesses = st.get("exercises") or []
        best = max(guesses, key=lambda e: e.get("probability") or 0) if guesses else {}
        weight = st.get("weight")
        rows.append((
            activity_id, i, (st.get("setType") or "").lower() or None,
            best.get("category"), best.get("name"),
            st.get("repetitionCount"),
            # The API serves set weight in grams.
            round(weight / 1000, 2) if weight is not None else None,
            st.get("duration"), st.get("startTime"), st.get("wktStepIndex"),
        ))
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        conn.execute("DELETE FROM strength_sets WHERE activity_id = ?", (activity_id,))
        conn.executemany("INSERT INTO strength_sets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("UPDATE activities SET strength_sets_fetched_at = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), activity_id))
    return len(rows)


def backfill_sets(garmin_client, max_activities: int = 100) -> dict:
    """Fetch sets for cached strength activities that never got them,
    newest first, one API call each."""
    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM activities WHERE sport_type = 'WeightTraining' "
            "AND strength_sets_fetched_at IS NULL ORDER BY start_date_local DESC LIMIT ?",
            (max_activities,))]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE sport_type = 'WeightTraining' "
            "AND strength_sets_fetched_at IS NULL").fetchone()[0] - len(ids)
    fetched = sets = 0
    errors: list[str] = []
    for act_id in ids:
        try:
            sets += fetch_and_store_sets(garmin_client, act_id)
            fetched += 1
        except Exception as e:
            errors.append(f"sets {act_id}: {type(e).__name__}: {e}")
    return {"activities_fetched": fetched, "sets_stored": sets,
            "remaining": max(0, remaining), "errors": errors}


def activity_sets(activity_id: int) -> Optional[dict]:
    """Cached sets for one activity grouped per exercise, or None if the
    activity's sets were never fetched."""
    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        act = conn.execute(
            "SELECT id, start_date_local, name, strength_sets_fetched_at FROM activities WHERE id = ?",
            (activity_id,)).fetchone()
        if not act or act["strength_sets_fetched_at"] is None:
            return None
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM strength_sets WHERE activity_id = ? ORDER BY set_index", (activity_id,))]
    active = [r for r in rows if r["set_type"] == "active"]
    per_ex: dict[tuple, dict] = {}
    for r in active:
        key = (r["category"], r["exercise_name"])
        e = per_ex.setdefault(key, {"category": r["category"], "exercise_name": r["exercise_name"],
                                    "sets": 0, "reps": [], "weights_kg": [], "durations_s": []})
        e["sets"] += 1
        e["reps"].append(r["reps"])
        e["weights_kg"].append(r["weight_kg"])
        e["durations_s"].append(round(r["duration_s"]) if r["duration_s"] is not None else None)
    out = {"activity_id": activity_id, "date": act["start_date_local"][:10], "name": act["name"],
           "active_sets": len(active), "rest_periods": len(rows) - len(active),
           "exercises": list(per_ex.values())}
    if active and all(r["category"] == "UNKNOWN" for r in active):
        out["note"] = ("Garmin recorded no exercise detail (one UNKNOWN set) — the session "
                       "was not run from a structured strength workout on the watch.")
    return out
