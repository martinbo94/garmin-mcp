"""Personal grade→pace response, fit from the athlete's own run streams.

Empirically derives how much THIS runner slows on hills at easy effort, so a
race-course pace plan can use a personal grade-adjustment curve instead of a
generic population one (per the research: population GAP systematically
mis-estimates anyone off the mean).

Method (grounded in Minetti 2002 + GAP literature; see the research notes):
  - per run, resample elevation to a fixed 5 m grid, Savitzky-Golay smooth
    (window 21, polyorder 2) and differentiate → grade %, clipped ±40%;
  - aggregate into 60 s steady-grade windows (drops the first 10 min and
    high grade-variance windows) to absorb the ~55 s HR lag;
  - fit HR ~ speed + g_up + g_dn + mins with run fixed effects (within-run
    demeaning) + cluster-robust SEs; mins absorbs cardiac drift;
  - HR-neutral slow-off per +1% grade = -(beta_grade / beta_speed),
    converted from speed to pace at the athlete's median easy speed.

The factor is derived at EASY effort but used as a personal GAP: the *shape*
(slow-off per %) transfers to race pacing; treat the absolute pace as
advisory. Well-supported only within the observed grade range (about ±6%);
steeper is extrapolation.

Fit is cached to coach_data/grade_model.json (personal; gitignored).
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import savgol_filter
import statsmodels.formula.api as smf

import garmin_sync

GRADE_MODEL_PATH = Path(__file__).parent / "coach_data" / "grade_model.json"

_WIN_S = 60            # window length
_MIN_START_S = 600     # drop first 10 min (HR not settled)
_GRADE_SD_MAX = 1.5    # keep steady-grade windows only
_EASY_HR_CEILING = 180  # exclude races/intervals/TT by effort (avg HR)
_RESAMPLE_M = 5.0
_SAVGOL_WIN = 21
_SUPPORT_Q = (0.02, 0.98)  # report grade support as central 96% of windows


def _is_steady_easy(name, sport_type, planned_type, avg_hr) -> bool:
    if avg_hr is not None and avg_hr > _EASY_HR_CEILING:
        return False  # race / interval / TT effort
    cls = garmin_sync.classify_activity(name, sport_type, planned_type)[0]
    return cls in ("easy", "long", "unknown")


def _windows_for_run(t, hr, sp, el, di):
    """Return list of steady 60 s window dicts for one run, or [] if unusable."""
    if len(t) < 300 or np.any(np.isnan(el)) or np.any(np.isnan(di)):
        return []
    dmax = float(di[-1])
    if dmax < 2000:
        return []
    grid = np.arange(0, dmax, _RESAMPLE_M)
    if len(grid) < 25:
        return []
    el_g = np.interp(grid, di, el)
    dedx = savgol_filter(el_g, _SAVGOL_WIN, 2, deriv=1, delta=_RESAMPLE_M)
    grade = np.interp(di, grid, np.clip(dedx * 100, -40, 40))

    out = []
    t0 = t[0]
    start = t0
    while start < t[-1]:
        m = (t >= start) & (t < start + _WIN_S)
        if m.sum() >= 15 and (start - t0) >= _MIN_START_S and sp[m].mean() > 1.5:
            g = grade[m]
            if g.std() <= _GRADE_SD_MAX:
                out.append({"hr": hr[m].mean(), "speed": sp[m].mean(),
                            "grade": g.mean(), "mins": (start - t0) / 60.0})
        start += _WIN_S
    return out


def compute_grade_response(recompute: bool = False) -> dict:
    """Fit (or load cached) the personal grade→pace response. See module docs."""
    if not recompute:
        cached = load_grade_response()
        if cached:
            return cached

    import sqlite3
    garmin_sync._init_db()
    con = sqlite3.connect(garmin_sync.DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT a.id, a.name, a.sport_type, a.planned_type, a.avg_hr,
               s.time_json, s.hr_json, s.speed_json, s.elevation_json,
               s.distance_json
        FROM activities a JOIN streams s ON s.activity_id = a.id
        WHERE a.sport_type='Run' AND s.elevation_json IS NOT NULL
          AND s.speed_json IS NOT NULL AND s.distance_json IS NOT NULL
          AND a.distance_m > 3000
        """
    ).fetchall()

    recs = []
    n_runs = 0
    for r in rows:
        if not _is_steady_easy(r["name"], r["sport_type"], r["planned_type"], r["avg_hr"]):
            continue
        t = np.array(json.loads(r["time_json"]), float)
        hr = np.array(json.loads(r["hr_json"]), float)
        sp = np.array(json.loads(r["speed_json"]), float)
        el = np.array(json.loads(r["elevation_json"]), float)
        di = np.array(json.loads(r["distance_json"]), float)
        w = _windows_for_run(t, hr, sp, el, di)
        if w:
            for d in w:
                d["run"] = r["id"]
            recs.extend(w)
            n_runs += 1

    if len(recs) < 200 or n_runs < 10:
        return {"error": (
            f"Not enough steady easy-run stream data to fit a personal grade "
            f"model (got {n_runs} runs / {len(recs)} windows; need ~10 / 200). "
            f"Sync more outdoor runs and run sync_activities(backfill_streams=True)."
        )}

    df = pd.DataFrame(recs)
    df["g_up"] = df.grade.clip(lower=0)
    df["g_dn"] = df.grade.clip(upper=0)
    mod = smf.ols("hr ~ speed + g_up + g_dn + mins + C(run)", data=df).fit(
        cov_type="cluster", cov_kwds={"groups": df["run"]})
    b = mod.params
    v0 = float(df.speed.median())
    p0 = 1000.0 / v0

    def dpace(g):  # s/km vs flat at the easy operating point (+ = slower)
        bg = b["g_up"] if g > 0 else b["g_dn"]
        dv = -(bg * g) / b["speed"]
        return float(-(1000.0 / v0 ** 2) * dv)

    lo = round(float(df.grade.quantile(_SUPPORT_Q[0])), 1)
    hi = round(float(df.grade.quantile(_SUPPORT_Q[1])), 1)
    curve = {str(g): round(dpace(g), 1) for g in (-8, -5, -3, -2, -1, 1, 2, 3, 5, 8)}

    model = {
        "fit_date": datetime.now(timezone.utc).isoformat(),
        "n_runs": n_runs,
        "n_windows": len(df),
        "operating_speed_ms": round(v0, 3),
        "operating_pace_s_per_km": round(p0, 1),
        "beta_speed_bpm_per_ms": round(float(b["speed"]), 3),
        "beta_g_up_bpm_per_pct": round(float(b["g_up"]), 3),
        "beta_g_dn_bpm_per_pct": round(float(b["g_dn"]), 3),
        "beta_mins": round(float(b["mins"]), 3),
        "r2": round(float(mod.rsquared), 3),
        "slowoff_s_per_km_per_pct_up": round(dpace(1), 1),
        "speedup_s_per_km_per_pct_down": round(dpace(-1), 1),
        "grade_support_pct": [lo, hi],
        "pace_adjustment_curve_s_per_km": curve,
        "caveats": (
            "Derived at easy effort (used as a personal GAP — shape transfers, "
            f"absolute pace advisory). Well-supported only within ~{lo}..{hi}% "
            "grade; steeper is extrapolation. Observational (you choose to slow "
            "on hills), mitigated by run fixed effects + steady-grade windows."
        ),
    }
    try:
        GRADE_MODEL_PATH.write_text(json.dumps(model, indent=1), encoding="utf-8")
    except OSError:
        pass
    return model


def load_grade_response() -> Optional[dict]:
    try:
        return json.loads(GRADE_MODEL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def personal_grade_factor(grade_pct: float, model: dict) -> float:
    """Pace multiplier at a gradient from a fitted model (1.0 = flat).

    Mirrors gpx_analysis.grade_factor so it's a drop-in for pacing. Uphill
    > 1 (slower), downhill < 1 (faster). Grade is clamped to the model's
    supported range so extrapolation can't produce absurd splits.
    """
    lo, hi = model["grade_support_pct"]
    g = max(lo, min(hi, grade_pct))
    bg = model["beta_g_up_bpm_per_pct"] if g > 0 else model["beta_g_dn_bpm_per_pct"]
    v0 = model["operating_speed_ms"]
    p0 = model["operating_pace_s_per_km"]
    dv = -(bg * g) / model["beta_speed_bpm_per_ms"]
    dpace = -(1000.0 / v0 ** 2) * dv
    return 1.0 + dpace / p0
