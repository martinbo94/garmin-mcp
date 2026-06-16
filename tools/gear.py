"""Gear / equipment tools (Garmin = source of truth)."""
from core import _client, mcp


# ─── Gear / equipment (Garmin = source of truth) ──────────────────────
@mcp.tool()
def list_gear(active_only: bool = True, with_stats: bool = True) -> list[dict]:
    """List your Garmin gear (shoes etc.) with status and mileage.

    Args:
        active_only: If True (default), only return active (non-retired) gear.
        with_stats: If True (default), include total_distance_km and
            total_activities per item. Adds one API call per gear item —
            fast for typical libraries, but can be slow for very large
            histories.

    Returns list of dicts: uuid, name, make_model, type, status,
    in_use_since, retired_at, and (when with_stats=True)
    total_distance_km and total_activities.
    """
    g = _client()
    profile_id = str(g.get_user_profile()["id"])
    items = g.get_gear(profile_id) or []

    out: list[dict] = []
    for it in items:
        if active_only and it.get("gearStatusName") != "active":
            continue
        rec = {
            "uuid": it.get("uuid"),
            "name": it.get("displayName") or it.get("customMakeModel"),
            "make_model": it.get("customMakeModel"),
            "type": it.get("gearTypeName"),
            "status": it.get("gearStatusName"),
            "in_use_since": (it.get("dateBegin") or "")[:10] or None,
            "retired_at": (it.get("dateEnd") or "")[:10] or None,
        }
        if with_stats and rec["uuid"]:
            try:
                stats = g.get_gear_stats(rec["uuid"])
                rec["total_distance_km"] = round(stats.get("totalDistance", 0) / 1000, 1)
                rec["total_activities"] = stats.get("totalActivities", 0)
            except Exception as e:
                rec["stats_error"] = f"{type(e).__name__}: {e}"
        out.append(rec)
    return out


@mcp.tool()
def shoe_wear_check(
    warning_km: int = 500,
    critical_km: int = 700,
) -> dict:
    """Check shoe mileage against wear thresholds and flag shoes nearing retirement.

    Typical running shoe lifespan is 500-800 km depending on the model,
    surface, and runner weight. Racing shoes and carbon-plated supershoes
    wear faster (~300-500 km).

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
    g = _client()
    profile_id = str(g.get_user_profile()["id"])
    items = g.get_gear(profile_id) or []

    shoes = []
    for it in items:
        if it.get("gearStatusName") != "active":
            continue
        if it.get("gearTypeName") not in ("shoes", "running_shoes", None):
            # Include all active gear — Garmin uses varying type names
            pass
        uuid = it.get("uuid")
        name = it.get("displayName") or it.get("customMakeModel") or "Unknown shoe"
        km = 0.0
        activities = 0
        if uuid:
            try:
                stats = g.get_gear_stats(uuid)
                km = round(stats.get("totalDistance", 0) / 1000, 1)
                activities = stats.get("totalActivities", 0)
            except Exception:
                pass

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
            "in_use_since": (it.get("dateBegin") or "")[:10] or None,
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

    Useful for reasoning about shoe wear or rotation patterns ("which
    shoes were on yesterday's run?", "which shoe has the most threshold
    work on it?").

    Takes a Garmin activity_id — find it in the Garmin Connect URL
    (`connect.garmin.com/modern/activity/<id>`) or via
    `sync_activities` + `weekly_summary`.
    """
    return _client().get_activity_gear(activity_id)


