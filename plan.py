"""Training plan storage + compliance tracking.

The plan is a single JSON file at `coach_data/plan.json` describing a
training block (e.g. 12 weeks) as a list of workouts keyed by date. Each
workout entry has a `type` (used for compliance matching), a name +
description, and optionally either a `continuous` block (for easy runs,
long runs, tempos) or an `interval` block (for structured interval
workouts) that maps 1:1 to the `create_continuous_run` /
`create_interval_workout` tool inputs.

Materialization (pushing the plan to Garmin) lives in server.py since it
needs the Garmin client + workout builders. This module handles plan I/O
and plan-vs-actual comparison against the Garmin cache.
"""
import json
import sqlite3
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
PLAN_PATH = ROOT / "coach_data" / "plan.json"
DB_PATH = ROOT / "coach_data" / "cache.db"

# How a planned type maps to acceptable actual classification_hint values.
# "Medium" strictness: tempo and threshold are interchangeable (both Z3-Z4
# sustained work); race counts if user did the race or related fragments.
PLAN_TYPE_MATCHES = {
    "easy": {"easy"},
    "threshold": {"threshold", "tempo"},
    "tempo": {"tempo", "threshold"},
    "intervals": {"intervals"},
    "long": {"long", "easy"},  # 14 km "Afternoon Run" still counts as long
    "prog-long": {"prog-long"},
    "race": {"race"},
    "strength": {"strength"},
    "rest": set(),
}

# Compliance distance tolerance: actual within ±15% of planned/estimated.
DISTANCE_TOLERANCE = 0.15


VALID_PLAN_TYPES = {
    "easy", "threshold", "tempo", "intervals", "long", "prog-long",
    "race", "strength", "rest",
}


def validate_plan(plan_data: dict) -> dict:
    """Check a plan dict for structural issues before saving."""
    from datetime import date as _date

    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(plan_data, dict):
        return {"ok": False, "errors": ["plan_data must be a dict"], "warnings": []}

    if not plan_data.get("block_name"):
        warnings.append("block_name is empty")

    start_date = plan_data.get("start_date")
    if not start_date:
        warnings.append("start_date is missing")
    else:
        try:
            _date.fromisoformat(start_date)
        except ValueError:
            errors.append(f"start_date '{start_date}' is not ISO YYYY-MM-DD")

    workouts = plan_data.get("workouts")
    if not isinstance(workouts, list):
        return {"ok": False, "errors": ["workouts must be a list"], "warnings": warnings}
    if not workouts:
        warnings.append("workouts list is empty")

    seen_dates: dict[str, int] = {}
    for i, w in enumerate(workouts):
        prefix = f"workouts[{i}]"
        if not isinstance(w, dict):
            errors.append(f"{prefix}: must be a dict")
            continue

        d = w.get("date")
        if not d:
            errors.append(f"{prefix}: missing 'date'")
        else:
            try:
                _date.fromisoformat(d)
            except ValueError:
                errors.append(f"{prefix}: invalid date '{d}' (expected YYYY-MM-DD)")
            if d in seen_dates:
                warnings.append(
                    f"{prefix} ({d}): duplicate date — also at workouts[{seen_dates[d]}]"
                )
            else:
                seen_dates[d] = i

        t = w.get("type")
        if not t:
            errors.append(f"{prefix} ({d}): missing 'type'")
        elif t not in VALID_PLAN_TYPES:
            errors.append(
                f"{prefix} ({d}): unknown type '{t}'; expected one of {sorted(VALID_PLAN_TYPES)}"
            )

        if not w.get("name"):
            warnings.append(f"{prefix} ({d}): missing 'name' — materialize_plan will use a default")

        has_cont = bool(w.get("continuous"))
        has_int = bool(w.get("interval"))

        if t in ("rest", "strength"):
            if has_cont or has_int:
                warnings.append(
                    f"{prefix} ({d}): {t} workout has continuous/interval block — ignored on materialize"
                )
        elif t and t in VALID_PLAN_TYPES:
            if not (has_cont or has_int):
                errors.append(
                    f"{prefix} ({d}): {t} workout needs either 'continuous' or 'interval' block"
                )
            elif has_cont and has_int:
                errors.append(
                    f"{prefix} ({d}): has both 'continuous' and 'interval' — pick one"
                )
            elif has_cont:
                c = w["continuous"]
                has_d = c.get("distance_m") is not None
                has_t_field = c.get("duration_s") is not None
                if has_d == has_t_field:
                    errors.append(
                        f"{prefix} ({d}): 'continuous' needs exactly one of distance_m or duration_s"
                    )
            elif has_int:
                iv = w["interval"]
                for req in ("warmup", "sets", "cooldown"):
                    if not iv.get(req):
                        errors.append(f"{prefix} ({d}): interval missing '{req}'")
                sets = iv.get("sets")
                if sets is not None and not isinstance(sets, list):
                    errors.append(f"{prefix} ({d}): interval.sets must be a list")
                elif isinstance(sets, list):
                    for j, s in enumerate(sets):
                        if not isinstance(s, dict):
                            errors.append(f"{prefix} ({d}): sets[{j}] must be a dict")
                            continue
                        for req in ("repeats", "work", "recovery"):
                            if req not in s:
                                errors.append(f"{prefix} ({d}): sets[{j}] missing '{req}'")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "workout_count": len(workouts),
    }


def summarize_plan(plan_data: dict) -> dict:
    """Per-week and block-level summary of a plan dict."""
    from datetime import date as _date, timedelta as _td

    workouts = plan_data.get("workouts", [])
    if not workouts:
        return {
            "block_name": plan_data.get("block_name"),
            "start_date": plan_data.get("start_date"),
            "weeks": [],
            "totals": {},
        }

    weeks_dict: dict[str, dict] = {}
    for w in workouts:
        try:
            d = _date.fromisoformat(w["date"])
        except (KeyError, ValueError, TypeError):
            continue
        wk_start = (d - _td(days=d.weekday())).isoformat()
        bucket = weeks_dict.setdefault(wk_start, {
            "week_start": wk_start,
            "week_end": (_date.fromisoformat(wk_start) + _td(days=6)).isoformat(),
            "sessions": 0,
            "quality": 0,
            "easy": 0,
            "long": 0,
            "strength": 0,
            "rest": 0,
            "total_km": 0.0,
            "workouts": [],
        })
        bucket["sessions"] += 1
        t = w.get("type", "unknown")
        if t in ("threshold", "tempo", "intervals", "prog-long", "race"):
            bucket["quality"] += 1
        elif t == "easy":
            bucket["easy"] += 1
        elif t == "long":
            bucket["long"] += 1
        elif t == "strength":
            bucket["strength"] += 1
        elif t == "rest":
            bucket["rest"] += 1

        est = w.get("estimated_distance_m")
        if est is None:
            cont = w.get("continuous") or {}
            est = cont.get("distance_m")
        km = (est or 0) / 1000
        bucket["total_km"] = round(bucket["total_km"] + km, 1)
        bucket["workouts"].append({
            "date": w["date"],
            "type": t,
            "name": w.get("name"),
            "km": round(km, 1) if km else None,
        })

    weeks = sorted(weeks_dict.values(), key=lambda w: w["week_start"])

    return {
        "block_name": plan_data.get("block_name"),
        "start_date": plan_data.get("start_date"),
        "weeks": weeks,
        "totals": {
            "total_workouts": len(workouts),
            "total_weeks": len(weeks),
            "total_km": round(sum(w["total_km"] for w in weeks), 1),
            "total_quality": sum(w["quality"] for w in weeks),
            "total_easy": sum(w["easy"] for w in weeks),
            "total_long": sum(w["long"] for w in weeks),
            "total_strength": sum(w["strength"] for w in weeks),
            "total_rest": sum(w["rest"] for w in weeks),
        },
    }


def load_plan() -> Optional[dict]:
    """Load the current plan from disk. Returns None if no plan exists."""
    if not PLAN_PATH.exists():
        return None
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def save_plan(plan: dict) -> None:
    """Write the plan back to disk."""
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


def _estimated_km(workout: dict) -> Optional[float]:
    """Pull the planned distance in km, preferring explicit estimated_distance_m."""
    est = workout.get("estimated_distance_m")
    if est is not None:
        return est / 1000
    cont = workout.get("continuous") or {}
    if cont.get("distance_m"):
        return cont["distance_m"] / 1000
    return None


def _get_actuals_in_range(start_date: str, end_date: str) -> list[dict]:
    """Pull cached activities (with classification hint) in a date range."""
    # Imported here to avoid a top-level circular import with server.py.
    from garmin_sync import name_hint

    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, start_date_local, name, description, type, sport_type,
                   distance_m, moving_time_s, avg_hr, max_hr
            FROM activities
            WHERE date(start_date_local) BETWEEN ? AND ?
            ORDER BY start_date_local
            """,
            (start_date, end_date),
        ).fetchall()
    return [
        {
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
            "hint": name_hint(r["name"], r["sport_type"]),
        }
        for r in rows
    ]


def _classify_compliance(planned: dict, actual: Optional[dict]) -> str:
    """Return a compliance status string for a planned/actual pair."""
    ptype = planned["type"]

    # Rest day: violated only if a run-type actual exists
    if ptype == "rest":
        if actual is None or actual["hint"] in ("strength", "hike", "ride"):
            return "compliant"
        return "rest-violated"

    if actual is None:
        return "missed"

    type_ok = actual["hint"] in PLAN_TYPE_MATCHES.get(ptype, set())

    planned_km = _estimated_km(planned)
    actual_km = (actual.get("distance_m") or 0) / 1000
    if planned_km and actual_km:
        delta = abs(actual_km - planned_km) / planned_km
        distance_ok = delta <= DISTANCE_TOLERANCE
    else:
        distance_ok = True  # no distance to compare (strength session, etc)

    if type_ok and distance_ok:
        return "compliant"
    if type_ok and not distance_ok:
        return "off-distance"
    if not type_ok and distance_ok:
        return "off-type"
    return "off"


def compare_plan_vs_actual(start_date: str, end_date: str) -> dict:
    """Compare planned workouts vs actual cached activities in a date range.

    Each planned workout is matched against the actual activity on the same
    date. Returns a summary plus per-workout detail.
    """
    plan = load_plan()
    if plan is None:
        return {"error": "No plan found. Create coach_data/plan.json first."}

    actuals = _get_actuals_in_range(start_date, end_date)
    actuals_by_date: dict[str, list[dict]] = {}
    for a in actuals:
        actuals_by_date.setdefault(a["date"], []).append(a)

    planned_in_range = [
        w for w in plan["workouts"]
        if start_date <= w["date"] <= end_date
    ]

    details = []
    matched_actual_ids: set[int] = set()
    counts = {
        "compliant": 0, "off-distance": 0, "off-type": 0, "off": 0,
        "missed": 0, "rest-violated": 0,
    }

    for w in sorted(planned_in_range, key=lambda x: x["date"]):
        day_actuals = actuals_by_date.get(w["date"], [])
        # Pick the run-type actual on this date that matches best,
        # preferring same-hint match. If none, fall back to first run.
        candidates = [a for a in day_actuals if a["hint"] not in ("strength", "hike", "ride")]
        if not candidates and w["type"] == "strength":
            candidates = [a for a in day_actuals if a["hint"] == "strength"]

        best = None
        if candidates:
            wanted = PLAN_TYPE_MATCHES.get(w["type"], set())
            best = next((a for a in candidates if a["hint"] in wanted), candidates[0])
            matched_actual_ids.add(best["id"])

        status = _classify_compliance(w, best)
        counts[status] = counts.get(status, 0) + 1

        details.append({
            "date": w["date"],
            "planned_type": w["type"],
            "planned_name": w.get("name"),
            "planned_km": _estimated_km(w),
            "status": status,
            "actual": {
                "name": best["name"],
                "hint": best["hint"],
                "km": round((best["distance_m"] or 0) / 1000, 1),
                "moving_time_min": round((best["moving_time_s"] or 0) / 60),
                "avg_hr": best["avg_hr"],
            } if best else None,
        })

    # Any actuals not matched to a planned workout are "extra"
    extras = [
        {
            "date": a["date"],
            "name": a["name"],
            "hint": a["hint"],
            "km": round((a["distance_m"] or 0) / 1000, 1),
        }
        for a in actuals
        if a["id"] not in matched_actual_ids
        and a["hint"] not in ("strength", "hike", "ride")
    ]

    return {
        "block_name": plan.get("block_name"),
        "range": {"start": start_date, "end": end_date},
        "summary": {
            "planned_count": len(planned_in_range),
            **counts,
            "extras_count": len(extras),
        },
        "details": details,
        "extras": extras,
    }
