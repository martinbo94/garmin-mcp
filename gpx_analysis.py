"""GPX course parsing + grade-adjusted race pacing.

Pure functions, no network / Garmin / cache: parse a .gpx track, compute
course stats (distance, ascent/descent, per-km gradient), and build an
even-effort pacing plan for a goal time. The grade-adjustment model is the
same family as `elevation_impact` in server.py — a linear heuristic, not a
validated lab model — but asymmetric (uphill costs more pace than downhill
saves, matching established grade-adjusted-pace curves like Strava/Minetti).
"""
import math
import xml.etree.ElementTree as ET
from typing import Optional

# Pace multipliers per 1% grade. Uphill penalty > downhill benefit, and the
# downhill benefit bottoms out (steep descents stop helping — you brake).
_UPHILL_COEFF = 0.033       # +3.3% pace per +1% grade
_DOWNHILL_COEFF = 0.018     # -1.8% pace per -1% grade (downhill helps less)
_DOWNHILL_FACTOR_FLOOR = 0.90
# The linear model is only a reasonable approximation for moderate gradients.
# Beyond this the true cost is nonlinear (steep uphills become a hike; steep
# downhills slow again from braking/eccentric load), so we clamp the grade fed
# to the model and flag segments past it as "run by effort, not by the split."
MODEL_RELIABLE_MAX_GRADE = 10.0
_GRADE_CLAMP = 15.0
# Total-ascent smoothing: GPS elevation is noisy. Smooth over a fixed
# DISTANCE (~30 m) rather than a fixed point count, so dense (1 s) tracks get
# denoised without flattening the profile of sparse tracks.
_SMOOTH_DISTANCE_M = 30.0
_ASCENT_STEP_THRESHOLD_M = 0.5


def _local(tag: str) -> str:
    """Local tag name, dropping any XML namespace."""
    return tag.split("}")[-1]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_gpx(path: str) -> list[dict]:
    """Parse a GPX file into ordered track points [{lat, lon, ele}].

    Handles namespaced GPX (1.0/1.1) and multiple tracks/segments by
    concatenating all <trkpt> in document order. `ele` may be None.
    """
    root = ET.parse(path).getroot()
    points: list[dict] = []
    for el in root.iter():
        if _local(el.tag) != "trkpt":
            continue
        try:
            lat = float(el.attrib["lat"])
            lon = float(el.attrib["lon"])
        except (KeyError, ValueError):
            continue
        ele = None
        for child in el:
            if _local(child.tag) == "ele":
                try:
                    ele = float(child.text)
                except (TypeError, ValueError):
                    ele = None
                break
        points.append({"lat": lat, "lon": lon, "ele": ele})
    return points


def grade_factor(grade_pct: float) -> float:
    """Pace multiplier for a gradient (1.0 = flat-equivalent).

    Grade is clamped to ±_GRADE_CLAMP before applying the linear model, so
    a freak-steep segment can't produce an absurd split — but past
    MODEL_RELIABLE_MAX_GRADE the number is only a rough placeholder; callers
    should flag those segments as effort-paced.
    """
    g = max(-_GRADE_CLAMP, min(_GRADE_CLAMP, grade_pct))
    if g >= 0:
        return 1 + g * _UPHILL_COEFF
    return max(_DOWNHILL_FACTOR_FLOOR, 1 + g * _DOWNHILL_COEFF)


def _smoothed_elevations(eles: list[float], window: int) -> list[float]:
    if window <= 1:
        return list(eles)
    n = len(eles)
    out = []
    half = window // 2
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        chunk = eles[lo:hi]
        out.append(sum(chunk) / len(chunk))
    return out


def course_profile(points: list[dict]) -> dict:
    """Cumulative distance + (optional) elevation arrays and totals.

    Returns dict with: cum_dist_m (list), elevations (list or None),
    total_distance_m, total_ascent_m, total_descent_m, net_elevation_m,
    has_elevation (bool).
    """
    n = len(points)
    cum = [0.0]
    for i in range(1, n):
        d = _haversine_m(
            points[i - 1]["lat"], points[i - 1]["lon"],
            points[i]["lat"], points[i]["lon"],
        )
        cum.append(cum[-1] + d)

    eles_raw = [p["ele"] for p in points]
    has_ele = sum(1 for e in eles_raw if e is not None) >= max(2, n * 0.5)
    elevations = None
    ascent = descent = net = 0.0
    if has_ele:
        # forward-fill any sparse gaps.
        filled = []
        last = next((e for e in eles_raw if e is not None), 0.0)
        for e in eles_raw:
            if e is not None:
                last = e
            filled.append(last)
        # `elevations` (used for grades + climb detection) stays as raw filled
        # so sharp features aren't blunted. The ascent/descent TOTALS, which
        # sum many small deltas and so amplify noise, use a ~_SMOOTH_DISTANCE_M
        # moving average instead.
        elevations = filled
        diffs = [cum[i] - cum[i - 1] for i in range(1, n) if cum[i] > cum[i - 1]]
        median_spacing = sorted(diffs)[len(diffs) // 2] if diffs else 1.0
        window = max(1, min(25, round(_SMOOTH_DISTANCE_M / median_spacing)))
        smooth = _smoothed_elevations(filled, window)
        for i in range(1, n):
            delta = smooth[i] - smooth[i - 1]
            if delta >= _ASCENT_STEP_THRESHOLD_M:
                ascent += delta
            elif delta <= -_ASCENT_STEP_THRESHOLD_M:
                descent += -delta
        net = smooth[-1] - smooth[0]

    return {
        "cum_dist_m": cum,
        "elevations": elevations,
        "total_distance_m": cum[-1],
        "total_ascent_m": round(ascent, 1),
        "total_descent_m": round(descent, 1),
        "net_elevation_m": round(net, 1),
        "has_elevation": has_ele,
    }


def _ele_at(profile: dict, dist_m: float) -> Optional[float]:
    """Linear-interpolate elevation at a cumulative distance."""
    if not profile["has_elevation"]:
        return None
    cum = profile["cum_dist_m"]
    eles = profile["elevations"]
    if dist_m <= 0:
        return eles[0]
    if dist_m >= cum[-1]:
        return eles[-1]
    # binary-ish linear scan (courses are modest size)
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < dist_m:
            lo = mid + 1
        else:
            hi = mid
    i = max(1, lo)
    span = cum[i] - cum[i - 1]
    if span <= 0:
        return eles[i]
    frac = (dist_m - cum[i - 1]) / span
    return eles[i - 1] + frac * (eles[i] - eles[i - 1])


def per_km_segments(profile: dict) -> list[dict]:
    """Split the course into 1 km segments (last may be partial).

    Each: km (1-based label), distance_km, start/end distance, elevation
    change, and average grade (% over the segment).
    """
    total = profile["total_distance_m"]
    segments = []
    start = 0.0
    km = 1
    while start < total - 1e-6:
        end = min(start + 1000.0, total)
        seg_dist_m = end - start
        elev_change = None
        grade = 0.0
        if profile["has_elevation"]:
            e0 = _ele_at(profile, start)
            e1 = _ele_at(profile, end)
            elev_change = e1 - e0
            grade = (elev_change / seg_dist_m) * 100 if seg_dist_m > 0 else 0.0
        segments.append({
            "km": km,
            "distance_km": round(seg_dist_m / 1000.0, 3),
            "elev_change_m": round(elev_change, 1) if elev_change is not None else None,
            "avg_grade_pct": round(grade, 1),
        })
        start = end
        km += 1
    return segments


# ─── Notable climbs / descents (point-level, not per-km) ──────────────
# Per-km averages hide short steep hills (a 100 m @ 10% inside a flat km reads
# as ~1%). These detectors work on a fine resample so a sharp ramp is caught.
_DETECT_STEP_M = 20.0       # resample resolution
_RETRACE_TOL_M = 4.0        # vertical retracement that ends a climb/descent
_MIN_GAIN_M = 8.0           # ignore bumps smaller than this
_MIN_AVG_GRADE = 3.0        # ignore gentler-than-this features
_MAXGRADE_WIN_M = 40.0      # window for the steepest sub-stretch


def _grade_category(abs_grade: float) -> str:
    if abs_grade >= 10:
        return "very steep"
    if abs_grade >= 7:
        return "steep"
    if abs_grade >= 4:
        return "moderate"
    return "gentle"


def _resample(profile: dict, step_m: float):
    total = profile["total_distance_m"]
    dists = []
    d = 0.0
    while d < total:
        dists.append(d)
        d += step_m
    dists.append(total)
    eles = [_ele_at(profile, x) for x in dists]
    return dists, eles


def _max_grade_in(dists, eles, a, b, sign) -> float:
    """Steepest ~_MAXGRADE_WIN_M sub-stretch grade within [a, b] (signed)."""
    mg = 0.0
    for k in range(a, b):
        m = k + 1
        while m < b and dists[m] - dists[k] < _MAXGRADE_WIN_M:
            m += 1
        span = dists[m] - dists[k]
        if span <= 0:
            continue
        g = sign * (eles[m] - eles[k]) / span * 100
        if g > mg:
            mg = g
    return mg


def _detect(profile: dict, sign: int) -> list[dict]:
    """Sustained ascents (sign=+1) or descents (sign=-1) on the resampled
    profile, merged across small retracements, filtered to notable ones."""
    if not profile["has_elevation"]:
        return []
    dists, eles = _resample(profile, _DETECT_STEP_M)
    n = len(eles)
    feats = []
    i = 0
    while i < n - 1:
        if sign * (eles[i + 1] - eles[i]) <= 0:
            i += 1
            continue
        start = i
        ext = i + 1            # peak (climb) or trough (descent)
        j = i + 1
        while j < n:
            if sign * (eles[j] - eles[ext]) > 0:
                ext = j
            if sign * (eles[ext] - eles[j]) >= _RETRACE_TOL_M:
                break
            j += 1
        gain = sign * (eles[ext] - eles[start])      # always positive
        length = dists[ext] - dists[start]
        if length > 0 and gain >= _MIN_GAIN_M:
            avg_grade = gain / length * 100
            if avg_grade >= _MIN_AVG_GRADE:
                max_grade = _max_grade_in(dists, eles, start, ext, sign)
                start_km = dists[start] / 1000.0
                end_km = dists[ext] / 1000.0
                reliable = (avg_grade <= MODEL_RELIABLE_MAX_GRADE
                            and max_grade <= MODEL_RELIABLE_MAX_GRADE + 3)
                feats.append({
                    "kind": "climb" if sign > 0 else "descent",
                    "start_km": round(start_km, 1),
                    "end_km": round(end_km, 1),
                    "length_m": int(round(length / 10.0) * 10),
                    ("gain_m" if sign > 0 else "drop_m"): round(gain),
                    "avg_grade_pct": round(avg_grade, 1),
                    "max_grade_pct": round(max_grade, 1),
                    "category": _grade_category(avg_grade),
                    "pace_model_reliable": reliable,
                    "note": _feature_note(
                        sign, start_km, int(round(length / 10.0) * 10),
                        avg_grade, max_grade, gain, _grade_category(avg_grade),
                        reliable,
                    ),
                })
        i = max(ext, start + 1)
    return feats


def _feature_note(sign, start_km, length_m, avg_grade, max_grade, gain,
                  category, reliable) -> str:
    if sign > 0:
        note = (f"Climb around {start_km:.1f} km: ~{length_m} m at ~{avg_grade:.0f}% "
                f"(max ~{max_grade:.0f}%, +{gain:.0f} m) — {category}.")
        if not reliable:
            note += (" Pace target here is unreliable at this gradient — run by "
                     "effort and be ready to hike.")
        else:
            note += " Hold effort, let pace drift up; don't chase the split."
    else:
        note = (f"Descent around {start_km:.1f} km: ~{length_m} m at ~{avg_grade:.0f}% "
                f"(max ~{max_grade:.0f}%, −{gain:.0f} m) — {category}.")
        if not reliable:
            note += (" Steep — the time-gain estimate is optimistic; control the "
                     "quads, don't brake-and-blow.")
        else:
            note += " Free speed — stay relaxed and roll it."
    return note


def detect_features(profile: dict) -> dict:
    """Notable climbs and descents (point-level), each with a coaching note."""
    climbs = _detect(profile, +1)
    descents = _detect(profile, -1)
    return {"climbs": climbs, "descents": descents}


def fmt_pace(s_per_km: float) -> str:
    m = int(s_per_km // 60)
    s = int(round(s_per_km % 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}/km"


def fmt_time(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pacing_plan(segments: list[dict], goal_time_s: float) -> dict:
    """Even-EFFORT pacing for a goal time over a graded course.

    Holds grade-adjusted effort constant: each km's target pace = a single
    flat-equivalent 'effort pace' × that km's grade factor, scaled so the
    total predicted time equals the goal. Uphill km are slower, downhill
    faster, and the splits sum to the goal.
    """
    denom = sum(grade_factor(s["avg_grade_pct"]) * s["distance_km"] for s in segments)
    if denom <= 0:
        return {"error": "Course has no usable distance."}
    effort_pace_s = goal_time_s / denom  # flat-equivalent s/km

    plan = []
    cum_t = 0.0
    for s in segments:
        f = grade_factor(s["avg_grade_pct"])
        pace_s = effort_pace_s * f
        seg_t = pace_s * s["distance_km"]
        cum_t += seg_t
        plan.append({
            "km": s["km"],
            "distance_km": s["distance_km"],
            "avg_grade_pct": s["avg_grade_pct"],
            "elev_change_m": s["elev_change_m"],
            "target_pace": fmt_pace(pace_s),
            "target_pace_s_per_km": round(pace_s, 1),
            "split_time": fmt_time(seg_t),
            "cumulative_time": fmt_time(cum_t),
        })
    return {
        "effort_pace": fmt_pace(effort_pace_s),
        "effort_pace_s_per_km": round(effort_pace_s, 1),
        "predicted_finish_time": fmt_time(cum_t),
        "per_km": plan,
    }
