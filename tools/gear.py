"""Gear / equipment tools.

All reads come from the local cache (the `gear` and `activities` tables),
populated by `sync_activities` / `run_sync`. These tools never call Garmin
directly — run a sync to refresh gear status and mileage.
"""
import sqlite3

import garmin_sync
from core import mcp


def _gear_rows(active_only: bool = True) -> list[dict]:
    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM gear"
        if active_only:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY total_distance_km DESC"
        return [dict(r) for r in conn.execute(sql)]


# ─── Gear / equipment (read from local cache) ─────────────────────────
@mcp.tool()
def list_gear(active_only: bool = True, with_stats: bool = True) -> list[dict]:
    """List your Garmin gear (shoes etc.) with status and mileage.

    Reads the local `gear` cache (refreshed on every sync) — does not call
    Garmin. If gear looks stale or missing, run `sync_activities`.

    Args:
        active_only: If True (default), only return active (non-retired) gear.
        with_stats: If True (default), include total_distance_km and
            total_activities per item.

    Returns list of dicts: uuid, name, make_model, type, status,
    in_use_since, retired_at, and (when with_stats=True)
    total_distance_km and total_activities.
    """
    out: list[dict] = []
    for r in _gear_rows(active_only=active_only):
        rec = {
            "uuid": r["uuid"],
            "name": r["name"],
            "make_model": r["make_model"],
            "type": r["type"],
            "status": r["status"],
            "in_use_since": r["in_use_since"],
            "retired_at": r["retired_at"],
        }
        if with_stats:
            rec["total_distance_km"] = r["total_distance_km"]
            rec["total_activities"] = r["total_activities"]
        out.append(rec)
    return out


@mcp.tool()
def shoe_wear_check(
    warning_km: int = 500,
    critical_km: int = 700,
) -> dict:
    """Check shoe mileage against wear thresholds and flag shoes nearing retirement.

    Reads the local `gear` cache (refreshed on every sync) — does not call
    Garmin. Typical running shoe lifespan is 500-800 km depending on the
    model, surface, and runner weight. Racing shoes and carbon-plated
    supershoes wear faster (~300-500 km).

    Args:
        warning_km: Flag as WARNING above this distance. Default 500 km.
        critical_km: Flag as CRITICAL (overdue for retirement) above this.
            Default 700 km.

    Returns:
        - `summary`: one-line human-readable status
        - `shoes`: list of all active shoes with status (ok/warning/critical),
          total_distance_km, km_remaining (to warning threshold), and
          estimated sessions remaining (based on recent avg session distance)
        - `action_needed`: True if any shoe is warning or critical
    """
    shoes = []
    for r in _gear_rows(active_only=True):
        km = r["total_distance_km"] or 0.0
        activities = r["total_activities"] or 0
        name = r["name"] or "Unknown shoe"

        if km >= critical_km:
            status = "critical"
        elif km >= warning_km:
            status = "warning"
        else:
            status = "ok"

        avg_session_km = round(km / activities, 1) if activities > 0 else None
        km_to_warning = max(0, warning_km - km)
        sessions_to_warning = (
            round(km_to_warning / avg_session_km) if avg_session_km else None
        )

        shoes.append({
            "name": name,
            "status": status,
            "total_distance_km": km,
            "total_activities": activities,
            "avg_session_km": avg_session_km,
            "km_to_warning": round(km_to_warning, 1),
            "sessions_to_warning": sessions_to_warning,
            "in_use_since": r["in_use_since"],
        })

    shoes.sort(key=lambda s: s["total_distance_km"], reverse=True)

    critical = [s for s in shoes if s["status"] == "critical"]
    warning = [s for s in shoes if s["status"] == "warning"]
    action_needed = bool(critical or warning)

    if critical:
        names = ", ".join(s["name"] for s in critical)
        summary = f"CRITICAL: {names} past {critical_km} km — replace soon."
    elif warning:
        names = ", ".join(s["name"] for s in warning)
        summary = f"WARNING: {names} past {warning_km} km — monitor closely."
    else:
        summary = f"All shoes under {warning_km} km — no action needed."

    return {
        "summary": summary,
        "action_needed": action_needed,
        "shoes": shoes,
        "thresholds": {"warning_km": warning_km, "critical_km": critical_km},
    }


@mcp.tool()
def get_gear_for_activity(activity_id: int) -> dict:
    """Return the gear (e.g. shoe) used for a specific activity.

    Reads the local cache — the gear captured for the activity during sync,
    joined to the gear library for status/mileage. Does not call Garmin.
    Useful for reasoning about shoe wear or rotation patterns ("which shoes
    were on yesterday's run?", "which shoe has the most threshold work?").

    Returns a dict with the activity's gear_uuid / gear_name and, when the
    gear is in the local library, its make_model, type, status,
    total_distance_km and total_activities. `gear_fetched` is False when the
    activity hasn't been gear-synced yet (run sync_activities(backfill_gear=
    True)); gear_uuid is None when the activity simply has no gear assigned.
    """
    garmin_sync._init_db()
    with sqlite3.connect(garmin_sync.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, name, gear_uuid, gear_name, gear_fetched_at "
            "FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        if row is None:
            return {"error": f"Activity {activity_id} not in cache — run sync_activities."}
        result = {
            "activity_id": row["id"],
            "activity_name": row["name"],
            "gear_uuid": row["gear_uuid"],
            "gear_name": row["gear_name"],
            "gear_fetched": row["gear_fetched_at"] is not None,
        }
        if row["gear_uuid"]:
            g = conn.execute(
                "SELECT make_model, type, status, total_distance_km, "
                "total_activities FROM gear WHERE uuid = ?",
                (row["gear_uuid"],),
            ).fetchone()
            if g:
                result.update({
                    "make_model": g["make_model"],
                    "type": g["type"],
                    "status": g["status"],
                    "total_distance_km": g["total_distance_km"],
                    "total_activities": g["total_activities"],
                })
        return result
