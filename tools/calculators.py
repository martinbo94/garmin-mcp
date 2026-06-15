"""Pure calculators: interval planning, pace, elevation, heat, forecast, race course."""
import math
from typing import Optional

import garmin_sync
import gpx_analysis
from core import _client, mcp
from tools.profile import _parse_athlete_profile


# ─── Schedule tools ────────────────────────────────────────────────────
@mcp.tool()
def plan_interval_session(
    total_minutes: Optional[float] = None,
    work_minutes: Optional[float] = None,
    work_meters: Optional[float] = None,
    rest_minutes: Optional[float] = None,
    rest_meters: Optional[float] = None,
    warmup_minutes: float = 10.0,
    cooldown_minutes: float = 10.0,
    reps: Optional[int] = None,
) -> dict:
    """Calculate interval session structure and estimate distances from user profile paces.

    Solves for the missing variable given the others:
    - Provide `total_minutes` + work/rest → calculates how many reps fit.
    - Provide `reps` + work/rest → calculates total duration.
    - Works with time-based (minutes) or distance-based (meters) intervals.

    All durations are in minutes. Distances in meters.

    Examples:
      plan_interval_session(total_minutes=45, work_minutes=4, rest_minutes=2)
      → "5×4 min with 2 min rest fits in 45 min (10 wu + 10 cd)"

      plan_interval_session(reps=6, work_meters=1000, rest_minutes=90)
      → total time estimate based on your sub-threshold pace

    Returns a structured breakdown plus a `create_hint` with the exact
    `create_interval_workout` call to use if you want to push it to Garmin.
    """
    # Load paces from user profile for distance→time conversion
    pace_map: dict = {}
    try:
        profile = _parse_athlete_profile()
        paces = profile.get("pace_estimates", {})
        def _pace_to_s_per_m(pace_str: str) -> Optional[float]:
            if not pace_str or "/" not in pace_str:
                return None
            try:
                mins, secs = pace_str.replace("/km", "").strip().split(":")
                return (int(mins) * 60 + int(secs)) / 1000
            except Exception:
                return None
        pace_map = {k: _pace_to_s_per_m(v) for k, v in paces.items()}
    except Exception:
        pass

    sub_thresh_s_per_m = pace_map.get("sub_threshold") or pace_map.get("sub-threshold")
    easy_s_per_m = pace_map.get("easy")

    def _work_duration_s() -> Optional[float]:
        if work_minutes:
            return work_minutes * 60
        if work_meters and sub_thresh_s_per_m:
            return work_meters * sub_thresh_s_per_m
        if work_meters and not sub_thresh_s_per_m:
            return None  # handled below with clear error
        return None

    def _rest_duration_s() -> Optional[float]:
        if rest_minutes:
            return rest_minutes * 60
        if rest_meters and easy_s_per_m:
            return rest_meters * easy_s_per_m
        if rest_meters:
            return rest_meters * (sub_thresh_s_per_m or 0.33)  # ~5 min/km fallback
        return None

    work_s = _work_duration_s()
    rest_s = _rest_duration_s()
    wu_s = warmup_minutes * 60
    cd_s = cooldown_minutes * 60

    if work_s is None:
        if work_meters:
            return {"error": "work_meters requires a sub-threshold pace in your profile. "
                    "Set up your profile first, or use work_minutes instead."}
        return {"error": "Provide work_minutes or work_meters."}
    if rest_s is None:
        if rest_meters:
            return {"error": "rest_meters requires an easy pace in your profile. "
                    "Set up your profile first, or use rest_minutes instead."}
        return {"error": "Provide rest_minutes or rest_meters."}

    if reps is None and total_minutes is not None:
        available_s = total_minutes * 60 - wu_s - cd_s
        if available_s <= 0:
            return {"error": "total_minutes is too short for the warmup + cooldown alone."}
        reps = max(1, int(available_s / (work_s + rest_s)))
    elif reps is None:
        return {"error": "Provide either total_minutes or reps."}

    interval_block_s = reps * (work_s + rest_s) - rest_s  # last rep has no trailing rest
    total_s = wu_s + interval_block_s + cd_s
    total_min = round(total_s / 60, 1)

    # Distance estimates
    def _dist(duration_s: float, s_per_m: Optional[float]) -> Optional[float]:
        return round(duration_s / s_per_m / 1000, 2) if s_per_m else None

    work_km = (work_meters / 1000) if work_meters else _dist(work_s, sub_thresh_s_per_m)
    rest_km = (rest_meters / 1000) if rest_meters else _dist(rest_s, easy_s_per_m)
    wu_km = _dist(wu_s, easy_s_per_m)
    cd_km = _dist(cd_s, easy_s_per_m)
    total_km = round(sum(x for x in [
        wu_km, reps * (work_km or 0), (reps - 1) * (rest_km or 0), cd_km
    ] if x), 2) if work_km else None

    # Build create_interval_workout hint
    work_ec = ({"type": "distance", "value": work_meters}
               if work_meters else {"type": "time", "value": int(work_s)})
    rest_ec = ({"type": "distance", "value": rest_meters}
               if rest_meters else {"type": "time", "value": int(rest_s)})
    wu_ec = {"type": "time", "value": int(wu_s)}
    cd_ec = {"type": "time", "value": int(cd_s)}

    work_label = f"{work_meters:.0f}m" if work_meters else f"{work_minutes:.0f} min"
    rest_label = f"{rest_meters:.0f}m" if rest_meters else f"{rest_minutes:.0f} min"
    summary = (f"{reps}×{work_label} / {rest_label} rest — "
               f"{warmup_minutes:.0f} min wu + {cooldown_minutes:.0f} min cd = "
               f"~{total_min} min total")
    if total_km:
        summary += f" (~{total_km} km)"

    return {
        "summary": summary,
        "reps": reps,
        "work": {"label": work_label, "duration_s": round(work_s), "distance_km": work_km},
        "rest": {"label": rest_label, "duration_s": round(rest_s), "distance_km": rest_km},
        "warmup": {"duration_min": warmup_minutes, "distance_km": wu_km},
        "cooldown": {"duration_min": cooldown_minutes, "distance_km": cd_km},
        "total_minutes": total_min,
        "total_km": total_km,
        "create_hint": {
            "tool": "create_interval_workout",
            "warmup": wu_ec,
            "sets": [{"repeats": reps, "work": work_ec, "recovery": rest_ec}],
            "cooldown": cd_ec,
        },
    }


@mcp.tool()
def pace_calculator(
    pace_min_per_km: Optional[str] = None,
    speed_km_per_h: Optional[float] = None,
    distance_km: Optional[float] = None,
    duration_seconds: Optional[float] = None,
    duration_hms: Optional[str] = None,
) -> dict:
    """Convert between pace, speed, distance, and duration. Always use this
    tool for running math — never compute pace/speed conversions mentally.

    Provide any two of the four variables and the tool solves for the rest:
      pace_min_per_km  (string like "4:30" or "4:30/km")
      speed_km_per_h   (float, e.g. 13.3)
      distance_km      (float, e.g. 10.0)
      duration_seconds (float) OR duration_hms (string like "45:00" or "1:02:30")

    Examples:
      pace_calculator(pace_min_per_km="4:30", distance_km=10)
        → duration = 45:00, speed = 13.33 km/h
      pace_calculator(speed_km_per_h=12, duration_hms="1:00:00")
        → distance = 12.0 km, pace = 5:00/km
      pace_calculator(distance_km=21.1, duration_hms="1:45:00")
        → pace = 4:58/km, speed = 12.06 km/h
    """
    def _parse_pace(s: str) -> float:
        s = s.replace("/km", "").strip()
        parts = s.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    def _parse_hms(s: str) -> float:
        parts = s.strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return int(parts[0]) * 60 + float(parts[1])

    def _fmt_pace(s_per_km: float) -> str:
        m = int(s_per_km // 60)
        s = int(s_per_km % 60)
        return f"{m}:{s:02d}/km"

    def _fmt_duration(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # Parse inputs
    pace_s: Optional[float] = None
    dur_s: Optional[float] = None

    if pace_min_per_km:
        pace_s = _parse_pace(pace_min_per_km)
    if speed_km_per_h is not None:
        pace_s = 3600 / speed_km_per_h
    if duration_hms:
        dur_s = _parse_hms(duration_hms)
    if duration_seconds is not None:
        dur_s = duration_seconds

    # Check for conflicting pace/speed inputs
    if pace_min_per_km and speed_km_per_h is not None:
        return {"error": "Provide pace OR speed, not both."}
    if duration_hms and duration_seconds is not None:
        return {"error": "Provide duration_hms OR duration_seconds, not both."}

    known = sum(x is not None for x in [pace_s, distance_km, dur_s])
    # Allow single pace/speed input for simple unit conversion
    if known == 1 and pace_s is not None and distance_km is None and dur_s is None:
        speed = round(3600 / pace_s, 2)
        return {"pace": _fmt_pace(pace_s), "speed_km_h": speed,
                "distance_km": None, "duration": None, "duration_seconds": None}
    if known < 2:
        return {"error": "Provide at least two of: pace/speed, distance, duration."}

    # Solve for the missing variable
    if pace_s and distance_km and dur_s is None:
        dur_s = pace_s * distance_km
    elif pace_s and dur_s is not None and distance_km is None:
        distance_km = dur_s / pace_s
    elif distance_km and dur_s is not None and pace_s is None:
        pace_s = dur_s / distance_km
    elif known == 3:
        pass  # all three given — just convert/validate

    speed = round(3600 / pace_s, 2) if pace_s else None

    return {
        "pace": _fmt_pace(pace_s) if pace_s else None,
        "speed_km_h": speed,
        "distance_km": round(distance_km, 3) if distance_km else None,
        "duration": _fmt_duration(dur_s) if dur_s else None,
        "duration_seconds": round(dur_s) if dur_s else None,
    }


@mcp.tool()
def elevation_impact(
    actual_pace_min_per_km: Optional[str] = None,
    elevation_gain_m: Optional[float] = None,
    distance_km: Optional[float] = None,
    activity_id: Optional[int] = None,
) -> dict:
    """Estimate how elevation gain affects running effort (grade-adjusted pace).

    Uses a simple linear heuristic (NOT Strava's nonlinear GAP model —
    treat the output as a rough estimate; it ignores the descent rebate,
    so rolling/out-and-back routes are overestimated):
      GAP_factor = 1 + (avg_grade_pct * 0.033)
      flat_equivalent_pace = actual_pace / GAP_factor

    Call modes:
    - Manual: provide actual_pace_min_per_km + elevation_gain_m + distance_km.
    - Cache lookup: provide activity_id — distance and elevation are read from
      the local cache (actual_pace_min_per_km is still required).
    - Both: activity_id + actual_pace_min_per_km (cache supplies distance/elevation).

    Args:
        actual_pace_min_per_km: Pace string like "5:30" or "5:30/km".
        elevation_gain_m: Total elevation gain in meters.
        distance_km: Distance in kilometers.
        activity_id: Optional Garmin activity ID to look up distance and elevation
            from the local cache instead of passing them manually.

    Returns:
        - flat_equivalent_pace: What this run equals on flat terrain (e.g. "5:05/km").
        - elevation_cost_seconds: How many seconds the climbing added to the total time.
        - elevation_cost_formatted: Human-readable string like "3:20 added".
        - avg_grade_pct: Average grade as a percentage.
        - effort_level: "easy" / "moderate" / "significant" / "hilly" based on
          m/km ratio (< 10 / 10-20 / 20-40 / > 40).
        - note: Plain-English summary sentence.
    """
    try:
        def _parse_pace(s: str) -> float:
            """Return pace in seconds/km from 'M:SS' or 'M:SS/km'."""
            s = s.replace("/km", "").strip()
            parts = s.split(":")
            if len(parts) != 2:
                raise ValueError(f"expected M:SS, got {s!r}")
            return int(parts[0]) * 60 + int(parts[1])

        def _fmt_pace(s_per_km: float) -> str:
            m = int(s_per_km // 60)
            s = int(round(s_per_km % 60))
            return f"{m}:{s:02d}/km"

        def _fmt_duration(seconds: float) -> str:
            m = int(abs(seconds) // 60)
            s = int(round(abs(seconds) % 60))
            return f"{m}:{s:02d}"

        # --- Optional cache lookup ---
        if activity_id is not None:
            import sqlite3 as _sqlite3
            from garmin_sync import DB_PATH as _DB_PATH
            try:
                with _sqlite3.connect(_DB_PATH) as _conn:
                    _row = _conn.execute(
                        "SELECT distance_m, total_elevation_gain FROM activities WHERE id = ?",
                        (activity_id,),
                    ).fetchone()
                if _row is None:
                    return {
                        "error": (
                            f"Activity {activity_id} not found in local cache. "
                            "Call sync_activities() first."
                        )
                    }
                if distance_km is None and _row[0] is not None:
                    distance_km = _row[0] / 1000.0
                if elevation_gain_m is None:
                    if _row[1] is None:
                        return {
                            "error": (
                                f"Activity {activity_id} has no elevation data in the "
                                "cache (likely a treadmill/indoor run) — elevation "
                                "analysis is not applicable."
                            )
                        }
                    elevation_gain_m = float(_row[1])
            except Exception as e:
                return {"error": f"Cache lookup failed: {type(e).__name__}: {e}"}

        # --- Validate inputs ---
        if actual_pace_min_per_km is None:
            return {"error": "actual_pace_min_per_km is required."}
        if elevation_gain_m is None:
            return {"error": "elevation_gain_m is required (or pass activity_id to read from cache)."}
        if elevation_gain_m < 0:
            return {"error": "elevation_gain_m must be >= 0 (total ascent, not net elevation change)."}
        if distance_km is None:
            return {"error": "distance_km is required (or pass activity_id to read from cache)."}
        if distance_km <= 0:
            return {"error": "distance_km must be greater than 0."}

        try:
            actual_pace_s = _parse_pace(actual_pace_min_per_km)
        except Exception:
            return {"error": f"Could not parse pace '{actual_pace_min_per_km}'. Use format like '5:30' or '5:30/km'."}

        # --- GAP calculation ---
        avg_grade_pct = (elevation_gain_m / (distance_km * 1000)) * 100
        gap_factor = 1 + (avg_grade_pct * 0.033)
        flat_pace_s = actual_pace_s / gap_factor

        # Total time on actual course vs equivalent flat time
        actual_total_s = actual_pace_s * distance_km
        flat_total_s = flat_pace_s * distance_km
        elevation_cost_s = actual_total_s - flat_total_s

        # --- Effort level ---
        m_per_km = elevation_gain_m / distance_km
        if m_per_km < 10:
            effort_level = "easy"
        elif m_per_km < 20:
            effort_level = "moderate"
        elif m_per_km <= 40:
            effort_level = "significant"
        else:
            effort_level = "hilly"

        flat_pace_str = _fmt_pace(flat_pace_s)
        cost_str = _fmt_duration(elevation_cost_s)
        note = (
            f"Your {actual_pace_min_per_km.replace('/km', '')}/km on this route "
            f"= {flat_pace_str} on flat terrain"
        )

        return {
            "flat_equivalent_pace": flat_pace_str,
            "elevation_cost_seconds": round(elevation_cost_s),
            "elevation_cost_formatted": f"{cost_str} added",
            "avg_grade_pct": round(avg_grade_pct, 2),
            "effort_level": effort_level,
            "m_per_km": round(m_per_km, 1),
            "gap_factor": round(gap_factor, 4),
            "inputs": {
                "actual_pace": actual_pace_min_per_km.replace("/km", ""),
                "elevation_gain_m": elevation_gain_m,
                "distance_km": distance_km,
                "activity_id": activity_id,
            },
            "note": note,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}




# ─── Heat / dew-point pace adjustment (pure calculator) ────────────────
#
# Local pace parse/format helpers. The repo's pace math lives *inside*
# individual tools (pace_calculator, elevation tools) as nested closures,
# so there is no shared module-level helper to import. Keeping local
# copies here avoids reaching into another tool's scope; they mirror the
# pace_calculator style ("M:SS" / "M:SS/km" in, "M:SS/km" out).
def _heat_parse_pace_s(pace: str) -> float:
    """Parse 'M:SS' or 'M:SS/km' into seconds per km. Raises ValueError."""
    if pace is None:
        raise ValueError("pace is required")
    s = str(pace).replace("/km", "").strip()
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"expected 'M:SS' (optionally '/km'), got {pace!r}")
    minutes_str, seconds_str = parts[0].strip(), parts[1].strip()
    try:
        minutes = int(minutes_str)
        seconds = int(seconds_str)
    except ValueError:
        raise ValueError(f"non-numeric pace component in {pace!r}")
    if minutes < 0 or seconds < 0 or seconds >= 60:
        raise ValueError(f"seconds out of range in {pace!r}")
    return minutes * 60 + seconds


def _heat_fmt_pace(seconds_per_km: float) -> str:
    """Format seconds per km as 'M:SS/km'."""
    total = round(seconds_per_km)
    minutes, seconds = divmod(int(total), 60)
    return f"{minutes}:{seconds:02d}/km"


def _heat_dew_point_c(temp_c: float, rh_pct: float) -> float:
    """Dew point (°C) from temperature (°C) and relative humidity (%),
    via the Magnus-Tetens approximation."""
    alpha = (17.27 * temp_c) / (237.7 + temp_c) + math.log(rh_pct / 100.0)
    return (237.7 * alpha) / (17.27 - alpha)


# (sum_upper_F_inclusive, pct_low, pct_high)
# Bands keyed on temp_F + dewpoint_F per the Hadley / Maximum Performance
# Running dew-point chart. Each band covers (prev_upper, upper]; a sum is
# placed in the first band whose upper bound it does not exceed. Bounds are
# contiguous (no gaps), so fractional sums like 130.5 land cleanly and the
# mapping stays monotonic. Bucket midpoint = point estimate; (low, high)
# is the range. The >180 case is handled separately.
_HEAT_BANDS = [
    (100, 0.0, 0.0),
    (110, 0.0, 0.5),
    (120, 0.5, 1.0),
    (130, 1.0, 2.0),
    (140, 2.0, 3.0),
    (150, 3.0, 4.5),
    (160, 4.5, 6.0),
    (170, 6.0, 8.0),
    (180, 8.0, 10.0),
]


@mcp.tool()
def heat_pace_adjustment(
    base_pace_min_per_km: str,
    temp_c: float,
    dew_point_c: Optional[float] = None,
    relative_humidity: Optional[float] = None,
) -> dict:
    """Adjust target running pace for heat + humidity using the dew-point method.

    Model: the dew-point sum method (Hadley / Maximum Performance Running
    chart), the de-facto running-community standard. It sums the air
    temperature and the dew point (both in °F) and maps that sum onto a
    pace-slowdown band. Corroborated by RunnersConnect's heat chart and
    consistent with published marathon-vs-temperature data (Ely et al.,
    2007; Mantzios et al., 2022). This is a coaching heuristic validated
    against race results, NOT primary physiological research.

    Why dew point (not raw humidity): the body cools by evaporating sweat,
    and evaporation is governed by the moisture gradient between skin and
    air — which dew point captures directly. A cold, humid morning carries
    little absolute moisture (dew point stays low), so it costs little;
    raw relative-humidity penalties wrongly punish those conditions. Here
    humidity only ever enters *through* the dew point, so a cold humid day
    correctly yields ~0% adjustment.

    The adjustment is athlete-dependent: fitter, heat-adapted runners slow
    less than the band's high end, less-adapted runners more — hence each
    band returns a low/high range around the midpoint point-estimate.

    This athlete trains HR/effort-primary. Once conditions push into the
    ~5%+ buckets (sum ≳ 155°F), treat the pace number as advisory only and
    switch to running by effort / HR — heat elevates HR at any given pace,
    so HR caps protect you better than a pace target.

    Inputs are actual or forecast weather. This tool does NOT fetch
    weather — supply temp plus either dew point or relative humidity.

    Args:
        base_pace_min_per_km: Cool-conditions target pace, "M:SS" or
            "M:SS/km" (e.g. "5:30" or "5:30/km").
        temp_c: Air (or forecast) temperature in °C.
        dew_point_c: Dew point in °C, if known. Takes priority over
            relative_humidity. Must be ≤ temp_c (physically).
        relative_humidity: Relative humidity 0–100 (%). Used to derive
            dew point via Magnus-Tetens when dew_point_c is not given.

    Returns dict with: adjusted_pace ("M:SS/km"), adjustment_pct (midpoint),
    adjustment_pct_range [low, high], temp_f, dew_point_c, dew_point_f,
    heat_sum_f, base_pace, effort_based_recommended (bool), note, and
    hr_note. On bad input returns {"error": ...}.
    """
    # ── Parse / validate pace ──────────────────────────────────────────
    try:
        base_pace_s = _heat_parse_pace_s(base_pace_min_per_km)
    except ValueError as exc:
        return {"error": f"Invalid base_pace_min_per_km: {exc}"}

    # ── Resolve dew point ──────────────────────────────────────────────
    if dew_point_c is None and relative_humidity is None:
        return {
            "error": "Provide either dew_point_c or relative_humidity "
            "(plus temp_c)."
        }

    if dew_point_c is None:
        rh = relative_humidity
        if not (0 <= rh <= 100):
            return {
                "error": f"relative_humidity must be 0–100, got {rh}"
            }
        if rh <= 0:
            return {
                "error": "relative_humidity must be > 0 to compute dew point."
            }
        dew_c = _heat_dew_point_c(temp_c, rh)
    else:
        dew_c = dew_point_c

    # Dew point cannot exceed air temperature (allow a tiny float epsilon).
    if dew_c > temp_c + 1e-6:
        return {
            "error": f"dew_point_c ({dew_c:.1f}°C) cannot exceed temp_c "
            f"({temp_c:.1f}°C) — physically impossible."
        }

    # ── Convert to °F and sum ──────────────────────────────────────────
    temp_f = temp_c * 9 / 5 + 32
    dew_f = dew_c * 9 / 5 + 32
    heat_sum_f = temp_f + dew_f

    # ── Map sum → band ─────────────────────────────────────────────────
    effort_based = False
    if heat_sum_f > 180:
        pct_low, pct_high = 10.0, 10.0
        pct_mid = 10.0
        effort_based = True
        band_note = (
            "Sum exceeds 180°F — hard running is not recommended. Go "
            "effort/HR-based, cap intensity, and treat any pace as a ceiling."
        )
    else:
        pct_low = pct_high = pct_mid = 0.0
        for upper, p_lo, p_hi in _HEAT_BANDS:
            if heat_sum_f <= upper:
                pct_low, pct_high = p_lo, p_hi
                pct_mid = (p_lo + p_hi) / 2.0
                break
        band_note = None

    # 5%+ buckets → recommend effort/HR-based running for this athlete.
    if pct_mid >= 5.0:
        effort_based = True

    adjusted_pace_s = base_pace_s * (1.0 + pct_mid / 100.0)

    base_clean = base_pace_min_per_km.replace("/km", "").strip()
    if pct_mid == 0.0:
        note = (
            f"No adjustment — conditions are cool/dry enough (heat sum "
            f"{heat_sum_f:.0f}°F ≤ 100°F). Hold {base_clean}/km."
        )
    elif band_note:
        note = band_note
    elif effort_based:
        note = (
            f"Heat sum {heat_sum_f:.0f}°F → slow ~{pct_mid:.1f}% "
            f"({pct_low:.1f}–{pct_high:.1f}%). At this level treat the pace "
            "as advisory and run by effort/HR — heat inflates HR at any pace."
        )
    else:
        note = (
            f"Heat sum {heat_sum_f:.0f}°F → slow ~{pct_mid:.1f}% "
            f"({pct_low:.1f}–{pct_high:.1f}%). Target "
            f"{_heat_fmt_pace(adjusted_pace_s)} instead of {base_clean}/km; "
            "the range reflects how heat-adapted you are."
        )

    return {
        "adjusted_pace": _heat_fmt_pace(adjusted_pace_s),
        "adjustment_pct": round(pct_mid, 2),
        "adjustment_pct_range": [round(pct_low, 2), round(pct_high, 2)],
        "base_pace": f"{base_clean}/km",
        "temp_c": round(temp_c, 1),
        "temp_f": round(temp_f, 1),
        "dew_point_c": round(dew_c, 1),
        "dew_point_f": round(dew_f, 1),
        "heat_sum_f": round(heat_sum_f, 1),
        "effort_based_recommended": effort_based,
        "note": note,
        "hr_note": (
            "Heat elevates HR at any given pace. Run by HR/effort and let "
            "pace drift; your normal zone boundaries still apply."
        ),
    }


@mcp.tool()
def forecast_conditions(
    date: Optional[str] = None,
    hour: int = 17,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    base_pace_min_per_km: Optional[str] = None,
) -> dict:
    """Fetch temp / humidity / dew point for a date+hour, and optionally the
    heat-adjusted pace — the weather source for `heat_pace_adjustment`.

    ASK THE USER WHAT TIME THEY PLAN TO RUN and pass it as `hour` before
    quoting any heat adjustment. Conditions are read for that single hour, not
    a daily average, and temperature swings a lot across a day (a morning run
    and an evening run can land in different adjustment buckets). The `hour`
    default (17:00) is only a placeholder for when the time genuinely doesn't
    matter — do not rely on it for a real pace recommendation. There is no
    scheduled time on the plan to infer from, so the run time has to come from
    the user. The returned `day_temp_range_c` shows how much the time-of-day
    choice matters that day.

    Conditions come from Open-Meteo (free, public, no API key). Location
    defaults to your most recent outdoor activity's GPS coordinates (cached
    from sync; falls back to a one-off Garmin lookup if the cache has none
    yet), so you normally don't pass lat/lon at all — it follows you (e.g.
    Bærum vs. Spain). When `base_pace_min_per_km` is given it feeds the
    fetched conditions straight into `heat_pace_adjustment`.

    Args:
        date: 'YYYY-MM-DD' (default today). Open-Meteo covers roughly the past
            92 days through 16 days ahead — good for "tomorrow's session".
        hour: local hour 0-23 the run will happen. ASK THE USER — don't guess.
            Defaults to 17 only as a fallback; pass the real planned hour so
            the conditions match when they'll actually run.
        lat, lon: explicit coordinates; default = latest activity location.
        base_pace_min_per_km: if given (e.g. '5:30'), the result also includes
            `heat_adjustment` from `heat_pace_adjustment` for these conditions.

    Returns location (+ how it was resolved), the resolved local time, current
    `conditions` (temp_c, dew_point_c, relative_humidity, wind, precip), the
    day's temp range, and — when a base pace is given — `heat_adjustment`.
    """
    from datetime import date as _date

    loc_source = "explicit"
    if lat is None or lon is None:
        loc = garmin_sync.latest_location()
        if loc:
            lat, lon = loc["lat"], loc["lon"]
            loc_source = f"latest activity ({loc['from_activity']}, {loc['as_of']})"
        else:
            # Cache has no coordinates yet (e.g. before any post-migration
            # sync) — one-off Garmin lookup of the newest activity.
            try:
                acts = _client().get_activities(0, 1)
                if acts:
                    f = garmin_sync._fetch_detail_fields(
                        _client(), acts[0]["activityId"]
                    )
                    lat, lon = f.get("start_lat"), f.get("start_lon")
                    loc_source = "garmin latest activity"
            except Exception as e:
                return {
                    "error": (
                        f"No cached location and Garmin lookup failed: "
                        f"{type(e).__name__}: {e}. Pass lat/lon explicitly."
                    )
                }
    if lat is None or lon is None:
        return {
            "error": (
                "No location available (no outdoor activity with GPS in the "
                "cache). Pass lat/lon, or sync an outdoor run first."
            )
        }

    date = date or _date.today().isoformat()
    w = garmin_sync.fetch_weather(lat, lon, date, hour)
    if "error" in w:
        return w

    result = {
        "location": {"lat": round(lat, 4), "lon": round(lon, 4), "source": loc_source},
        "date": date,
        "conditions": {
            "resolved_local_time": w["resolved_local_time"],
            "temp_c": w["temp_c"],
            "dew_point_c": w["dew_point_c"],
            "relative_humidity": w["relative_humidity"],
            "wind_speed_kmh": w["wind_speed_kmh"],
            "precipitation_mm": w["precipitation_mm"],
        },
        "day_temp_range_c": {"min": w["day_temp_min_c"], "max": w["day_temp_max_c"]},
        "source": w["source"],
    }
    if base_pace_min_per_km and w.get("temp_c") is not None:
        kw = {"base_pace_min_per_km": base_pace_min_per_km, "temp_c": w["temp_c"]}
        if w.get("dew_point_c") is not None:
            kw["dew_point_c"] = w["dew_point_c"]
        elif w.get("relative_humidity") is not None:
            kw["relative_humidity"] = w["relative_humidity"]
        result["heat_adjustment"] = heat_pace_adjustment(**kw)
    return result


@mcp.tool()
def analyze_race_course(
    gpx_path: str,
    goal_time: Optional[str] = None,
    goal_pace_min_per_km: Optional[str] = None,
    negative_split_pct: float = 0.0,
) -> dict:
    """Analyze a race course from a GPX file and build a per-km pace plan.

    Reads a .gpx track, profiles the course (distance, ascent/descent,
    per-km gradient, hardest climb), and — given a goal — produces an
    EVEN-EFFORT pacing plan: each km's target pace is a single flat-equivalent
    'effort pace' scaled by that km's gradient, so uphill km are slower,
    downhill faster, and the splits sum to the goal. This paces by effort, not
    by the clock, which is how you actually run a hilly course evenly.

    Provide ONE of goal_time or goal_pace_min_per_km (or neither — then you get
    course analysis only, no plan):
        goal_time: target finish, 'H:MM:SS' or 'MM:SS' (e.g. '1:45:00', '45:00').
        goal_pace_min_per_km: target AVERAGE pace 'M:SS' (e.g. '5:30'); the goal
            time is this pace × the measured course distance.

    The grade adjustment is the same linear heuristic family as
    `elevation_impact` (asymmetric: uphill costs more pace than downhill saves).
    It is only reliable for MODERATE gradients (roughly within ±10%); beyond
    that the real cost is nonlinear (steep climbs become a hike, steep descents
    slow again from braking), so such segments are flagged and should be run by
    effort, not by the split. Since you train HR/effort-primary, run the whole
    plan by effort and let pace drift — the targets just show where the course
    pushes HR up.

    Elevation features are detected at the POINT level, not from per-km
    averages, in two complementary ways:
    - `course.steep_pitches`: short sharp ramps/drops (the "walls" — e.g. a
      ~100 m overpass at 12%) that BOTH the per-km table and the sustained
      detector average away. These spike HR but barely move the split — run
      them by effort.
    - `course.notable_climbs` / `notable_descents`: sustained features (a long
      gentle drag, a steep descent) that matter for HR management over
      distance.
    `course.warnings` is the quick-read list: the steep pitches first, then the
    sustained climbs. Each feature carries a plain-language `note`.

    Pacing is even-EFFORT by default, and even pacing IS the evidence-optimal
    strategy for a flat race (Foster 1993; Abbiss & Laursen 2008; distance
    world records are run ~even, not negative). So treat `negative_split_pct`
    as a conservative-START HEDGE against going out too fast — the dominant
    way amateurs blow up — NOT as a "faster back half" target. Guidance:
      - 5k / 10k: leave it 0. Even (with a quick settle + end-spurt) is
        optimal; a deliberate negative split is sub-optimal at these
        distances.
      - Half / marathon: 0 is fine; use a SMALL value (~1-2%) only when the
        goal is ambitious relative to fitness or the runner tends to fade
        late. Bigger is not better — large negative splits usually just mean
        the first half was left too slow.
    The bias is applied to effort and renormalised to keep the goal time
    exact (see the returned `half_split`: the negative split shows in the
    effort paces; the clock paces are reshaped by terrain).

    Args:
        gpx_path: path to a .gpx file on disk.
        goal_time / goal_pace_min_per_km: see above.
        negative_split_pct: conservative-start effort bias as a percent
            (default 0 = even effort, the recommended default). ~1-2% is the
            sensible ceiling for a half/marathon; 0 for 5k/10k.

    Returns `course` (distance, ascent/descent/net, per-km gradient table,
    steepest km, steep_pitches, notable_climbs, notable_descents, warnings) and,
    when a goal is given, `pacing` (effort pace, predicted finish, strategy,
    per-km targets + cumulative splits; plus half_split + a note when a negative
    split is used). Each climb/descent carries start/end km, length, gain/drop,
    avg and max grade, a difficulty category, and a `pace_model_reliable` flag.
    """
    import os as _os
    if not _os.path.isfile(gpx_path):
        return {"error": f"GPX file not found: {gpx_path}"}
    try:
        points = gpx_analysis.parse_gpx(gpx_path)
    except Exception as e:
        return {"error": f"Could not parse GPX: {type(e).__name__}: {e}"}
    if len(points) < 2:
        return {"error": "GPX has fewer than 2 track points — nothing to analyze."}

    prof = gpx_analysis.course_profile(points)
    segments = gpx_analysis.per_km_segments(prof)
    dist_km = prof["total_distance_m"] / 1000.0

    steepest = max(segments, key=lambda s: s["avg_grade_pct"]) if segments else None
    features = gpx_analysis.detect_features(prof)
    steep_pitches = gpx_analysis.detect_steep_pitches(prof)
    course = {
        "total_distance_km": round(dist_km, 2),
        "total_ascent_m": prof["total_ascent_m"],
        "total_descent_m": prof["total_descent_m"],
        "net_elevation_m": prof["net_elevation_m"],
        "has_elevation": prof["has_elevation"],
        "steepest_km": steepest,
        "steep_pitches": steep_pitches,
        "notable_climbs": features["climbs"],
        "notable_descents": features["descents"],
        "per_km_grade": [
            {"km": s["km"], "distance_km": s["distance_km"],
             "avg_grade_pct": s["avg_grade_pct"], "elev_change_m": s["elev_change_m"]}
            for s in segments
        ],
    }
    # Warnings, most actionable first: short steep pitches (the walls the per-km
    # table AND the sustained-climb merge both hide), then sustained climbs
    # (HR-creep drags). Sustained descents stay in notable_descents but are kept
    # out of the headline list — the steep-pitch entries already cover the sharp
    # drops, and gentle descents need no warning.
    course["warnings"] = (
        [p["note"] for p in steep_pitches]
        + [c["note"] for c in features["climbs"]]
    )
    if not prof["has_elevation"]:
        course["elevation_note"] = (
            "GPX has no usable elevation data — treated as flat. Pacing (if "
            "requested) assumes no gradient."
        )

    result = {"course": course}

    # Resolve the goal into seconds, if any.
    goal_time_s = None
    if goal_time and goal_pace_min_per_km:
        return {"error": "Provide only one of goal_time or goal_pace_min_per_km."}
    if goal_time:
        parts = goal_time.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return {"error": f"Could not parse goal_time '{goal_time}'. Use H:MM:SS or MM:SS."}
        if len(nums) == 3:
            goal_time_s = nums[0] * 3600 + nums[1] * 60 + nums[2]
        elif len(nums) == 2:
            goal_time_s = nums[0] * 60 + nums[1]
        else:
            return {"error": f"Could not parse goal_time '{goal_time}'. Use H:MM:SS or MM:SS."}
    elif goal_pace_min_per_km:
        p = goal_pace_min_per_km.replace("/km", "").strip().split(":")
        if len(p) != 2:
            return {"error": f"Could not parse goal_pace_min_per_km '{goal_pace_min_per_km}'. Use M:SS."}
        try:
            pace_s = int(p[0]) * 60 + int(p[1])
        except ValueError:
            return {"error": f"Could not parse goal_pace_min_per_km '{goal_pace_min_per_km}'. Use M:SS."}
        goal_time_s = pace_s * dist_km

    if goal_time_s is not None:
        plan = gpx_analysis.pacing_plan(segments, goal_time_s, negative_split_pct)
        if "error" in plan:
            return plan
        plan["goal"] = {
            "goal_time": gpx_analysis.fmt_time(goal_time_s),
            "implied_avg_pace": gpx_analysis.fmt_pace(goal_time_s / dist_km),
            "course_distance_km": round(dist_km, 2),
        }
        result["pacing"] = plan
        result["hr_note"] = (
            "Run this by effort/HR, not the watch — the splits show where the "
            "course steepens so you hold effort instead of pace on the climbs."
        )
    else:
        result["note"] = (
            "No goal given — course analysis only. Pass goal_time or "
            "goal_pace_min_per_km for a per-km pace plan."
        )
    return result


