"""Wellness history, illness risk, stress balance, morning check-in, sleep."""
from typing import Optional

import garmin_sync
from core import _client, mcp


@mcp.tool()
def get_wellness_history(
    start_date: str,
    end_date: str,
    force_refetch: bool = False,
) -> dict:
    """Historical daily wellness metrics (HRV, RHR, sleep, stress, body battery)
    with rolling averages.

    On first call for a date, the daily metrics are pulled from Garmin and
    cached in coach_data/cache.db. Subsequent calls in the same range read
    from the cache and are fast. A first 90-day backfill takes ~30-60s.

    Rolling averages:
    - **RHR:** simple 7-day mean (it's a low-noise signal).
    - **HRV:** 7-day **geometric mean** (mean of ln(HRV), exp back).
      HRV is roughly log-normally distributed; this is the right shape
      per Altini's research and what HRV4Training uses.

    Args:
        start_date: 'YYYY-MM-DD' (inclusive).
        end_date:   'YYYY-MM-DD' (inclusive).
        force_refetch: If True, re-pull all days from Garmin even if cached.

    Returns dict with:
      - range: start/end/days
      - daily: list of {date, resting_hr, hrv_overnight_avg, hrv_status,
        hrv_baseline_low/upper, sleep_seconds, sleep_score, sleep stage
        durations, avg_stress, body_battery_*, respiration_avg, spo2_avg,
        recovery_time_hours}
      - rolling: list of {date, rhr_7d_mean, hrv_7d_geomean}
      - summary: min/max/mean for RHR and HRV across the range, plus the
        most recent Garmin "balanced HRV" baseline band for context

    Note: rows cached before a field was added to the schema will return
    null for that field. Call with `force_refetch=True` for the relevant
    range to backfill.
    """
    sync_result = garmin_sync.sync_wellness_range(
        _client(), start_date, end_date, force_refetch=force_refetch
    )
    data = garmin_sync.wellness_history(start_date, end_date)
    data["sync"] = sync_result
    return data


@mcp.tool()
def illness_risk_check() -> dict:
    """Scan today's wellness signals for early illness onset.

    Combines five independent signals — HRV drop, RHR spike, low sleep
    score, short sleep, and high stress — into a single risk rating.

    Returns:
    - `risk_level`: 'low' (0-1 flags), 'moderate' (2 flags), or
      'high' (3+ flags — multiple illness signals present).
    - `flagged_signals`: list of signal names that crossed the threshold.
    - `recommendation`: plain-language action string.
    - `raw_values`: dict with today's values, 7-day means, and thresholds
      used. Nulls indicate the device wasn't worn or the field isn't
      available for the current device.

    Flag thresholds (all evidence-based heuristics):
    - HRV: today >15% below 7-day mean.
    - RHR: today >5 bpm above 7-day mean.
    - Sleep score: today >10 points below 7-day mean.
    - Sleep duration: <6 hours (<21 600 s).
    - Avg stress: today >60 (moderate-high on Garmin's 0-100 scale).

    Wellness data is read from the local cache (wellness_daily table) after
    syncing the last 7 days; a short Garmin API call is made for today's
    data if it isn't cached.
    """
    import math as _math
    from datetime import date as _date, timedelta as _td

    try:
        today = _date.today()
        seven_days_ago = today - _td(days=7)

        # Sync the last 7 days into cache (fast — most days will be cached).
        garmin_sync.sync_wellness_range(
            _client(), seven_days_ago.isoformat(), today.isoformat()
        )

        # Read history (7 prior days, excluding today) for baseline means.
        history_start = seven_days_ago.isoformat()
        history_end = (today - _td(days=1)).isoformat()
        hist_data = garmin_sync.wellness_history(history_start, history_end)
        history_daily = hist_data.get("daily", [])

        # Today's metrics — read from cache after the sync above.
        today_row = garmin_sync._wellness_day_cached(today.isoformat())
        if today_row is None:
            # Fallback: fetch live if cache miss (shouldn't happen after sync).
            today_row = garmin_sync._fetch_wellness_day(_client(), today.isoformat())

        # ── Helper: arithmetic mean over a field across history rows ──────
        def _mean(field: str) -> Optional[float]:
            vals = [r[field] for r in history_daily if r.get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        # ── Collect today's values and 7-day means ─────────────────────────
        today_hrv = today_row.get("hrv_overnight_avg") if today_row else None
        today_rhr = today_row.get("resting_hr") if today_row else None
        today_sleep_score = today_row.get("sleep_score") if today_row else None
        today_sleep_s = today_row.get("sleep_seconds") if today_row else None
        today_stress = today_row.get("avg_stress") if today_row else None

        mean_hrv = _mean("hrv_overnight_avg")
        mean_rhr = _mean("resting_hr")
        mean_sleep_score = _mean("sleep_score")

        # ── Check each signal ──────────────────────────────────────────────
        flagged: list[str] = []

        # HRV: flag if today is >15% below 7-day mean
        hrv_flag = None
        if today_hrv is not None and mean_hrv is not None and mean_hrv > 0:
            hrv_drop_pct = (mean_hrv - today_hrv) / mean_hrv * 100
            if hrv_drop_pct > 15:
                flagged.append("hrv_low")
            hrv_flag = round(hrv_drop_pct, 1)

        # RHR: flag if today is >5 bpm above 7-day mean
        rhr_flag = None
        if today_rhr is not None and mean_rhr is not None:
            rhr_rise = today_rhr - mean_rhr
            if rhr_rise > 5:
                flagged.append("rhr_elevated")
            rhr_flag = round(rhr_rise, 1)

        # Sleep score: flag if >10 points below 7-day mean
        sleep_score_flag = None
        if today_sleep_score is not None and mean_sleep_score is not None:
            sleep_score_drop = mean_sleep_score - today_sleep_score
            if sleep_score_drop > 10:
                flagged.append("sleep_score_low")
            sleep_score_flag = round(sleep_score_drop, 1)

        # Sleep duration: flag if <6 hours (21600 seconds)
        if today_sleep_s is not None and today_sleep_s < 21_600:
            flagged.append("sleep_short")

        # Stress: flag if avg_stress > 60
        if today_stress is not None and today_stress > 60:
            flagged.append("stress_high")

        # ── Determine risk level ───────────────────────────────────────────
        n = len(flagged)
        if n >= 3:
            risk_level = "high"
            recommendation = "Rest day recommended — multiple illness signals present"
        elif n == 2:
            risk_level = "moderate"
            recommendation = "Consider reducing intensity"
        else:
            risk_level = "low"
            recommendation = "Train as planned"

        return {
            "date": today.isoformat(),
            "risk_level": risk_level,
            "flagged_signals": flagged,
            "flag_count": n,
            "recommendation": recommendation,
            "raw_values": {
                "hrv_today": today_hrv,
                "hrv_7d_mean": mean_hrv,
                "hrv_drop_pct": hrv_flag,
                "hrv_threshold_pct": 15,
                "rhr_today": today_rhr,
                "rhr_7d_mean": mean_rhr,
                "rhr_rise_bpm": rhr_flag,
                "rhr_threshold_bpm": 5,
                "sleep_score_today": today_sleep_score,
                "sleep_score_7d_mean": mean_sleep_score,
                "sleep_score_drop": sleep_score_flag,
                "sleep_score_threshold": 10,
                "sleep_seconds_today": today_sleep_s,
                "sleep_hours_today": round(today_sleep_s / 3600, 1) if today_sleep_s is not None else None,
                "sleep_short_threshold_hours": 6,
                "avg_stress_today": today_stress,
                "avg_stress_threshold": 60,
                "history_days": len(history_daily),
            },
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def stress_training_balance(days_back: int = 14) -> dict:
    """Combined training load + life stress analysis from Garmin wellness data.

    Queries the local cache for the last `days_back` days of wellness metrics
    and activity data. Combines training minutes with daily stress score to
    produce a holistic load picture — useful for spotting weeks where hard
    training coincides with high life stress.

    Stress conversion: each 10-point stress score counts as 1 "equivalent
    minute" of training load (avg_stress / 10), giving a combined_load per day.

    Returns:
    - `days`: list of {date, training_min, avg_stress, body_battery_at_wake,
      combined_load} for each day in the window.
    - `rolling_7d`: list of {date, combined_load_7d_avg, training_min_7d_avg}
      (7-day rolling averages, requires at least 4 data points).
    - `high_stress_training_days`: count of days where avg_stress > 60 AND
      training_minutes > 30 (hard training while stressed).
    - `avg_daily_stress`, `avg_training_min`: simple means across the window.
    - `best_recovery_day`: date with lowest avg_stress + highest body_battery_at_wake.
    - `recommendation`: plain-language guidance based on the analysis.

    Args:
        days_back: How many calendar days to look back (default 14).
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta as _td

    try:
        if days_back < 1:
            return {"error": "days_back must be >= 1."}
        today = _date.today()
        start = today - _td(days=days_back - 1)
        start_str = start.isoformat()
        end_str = today.isoformat()

        # Query wellness data
        with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row
            wellness_rows = conn.execute(
                """
                SELECT date, avg_stress, body_battery_low,
                       body_battery_at_wake, recovery_time_hours
                FROM wellness_daily
                WHERE date BETWEEN ? AND ?
                ORDER BY date
                """,
                (start_str, end_str),
            ).fetchall()

            # Query activity training load per day. Rowing/indoor sessions
            # often have moving_time_s = 0 in Garmin's payload — fall back
            # to elapsed_time_s so that load still counts.
            activity_rows = conn.execute(
                """
                SELECT date(start_date_local) AS day,
                       SUM(COALESCE(NULLIF(moving_time_s, 0), elapsed_time_s, 0))
                           AS total_moving_s
                FROM activities
                WHERE date(start_date_local) BETWEEN ? AND ?
                GROUP BY date(start_date_local)
                """,
                (start_str, end_str),
            ).fetchall()

        # Build activity lookup: date -> training_minutes
        activity_by_date: dict[str, float] = {}
        for row in activity_rows:
            if row["total_moving_s"]:
                activity_by_date[row["day"]] = round(row["total_moving_s"] / 60, 1)

        # Build per-day records
        days_list: list[dict] = []
        for row in wellness_rows:
            d = row["date"]
            training_min = activity_by_date.get(d, 0.0)
            avg_stress = row["avg_stress"]
            stress_equiv = round((avg_stress / 10) if avg_stress is not None else 0.0, 1)
            combined_load = round(training_min + stress_equiv, 1)
            days_list.append({
                "date": d,
                "training_min": training_min,
                "avg_stress": avg_stress,
                "body_battery_at_wake": row["body_battery_at_wake"],
                "combined_load": combined_load,
            })

        # Also fill in days that have activity data but no wellness row
        wellness_dates = {r["date"] for r in wellness_rows}
        for act_date, training_min in activity_by_date.items():
            if act_date not in wellness_dates:
                days_list.append({
                    "date": act_date,
                    "training_min": training_min,
                    "avg_stress": None,
                    "body_battery_at_wake": None,
                    "combined_load": round(training_min, 1),
                })

        days_list.sort(key=lambda x: x["date"])

        # Flag high-stress training days
        high_stress_days = [
            d for d in days_list
            if (d["avg_stress"] or 0) > 60 and d["training_min"] > 30
        ]

        # Summary stats
        stress_vals = [d["avg_stress"] for d in days_list if d["avg_stress"] is not None]
        avg_daily_stress = round(sum(stress_vals) / len(stress_vals), 1) if stress_vals else None
        training_vals = [d["training_min"] for d in days_list]
        avg_training_min = round(sum(training_vals) / len(training_vals), 1) if training_vals else 0.0

        # Best recovery day: score = body_battery_at_wake - avg_stress (higher = better)
        best_recovery_day = None
        best_score: float = float("-inf")
        for d in days_list:
            bb = d["body_battery_at_wake"]
            stress = d["avg_stress"]
            if bb is not None and stress is not None:
                score = bb - stress
                if score > best_score:
                    best_score = score
                    best_recovery_day = d["date"]

        # 7-day rolling averages of combined_load and training_min
        rolling_7d: list[dict] = []
        min_data = 4
        for i in range(len(days_list)):
            win_start = max(0, i - 6)
            window = days_list[win_start: i + 1]
            cl_vals = [w["combined_load"] for w in window]
            tr_vals = [w["training_min"] for w in window]
            cl_avg = round(sum(cl_vals) / len(cl_vals), 1) if len(cl_vals) >= min_data else None
            tr_avg = round(sum(tr_vals) / len(tr_vals), 1) if len(tr_vals) >= min_data else None
            rolling_7d.append({
                "date": days_list[i]["date"],
                "combined_load_7d_avg": cl_avg,
                "training_min_7d_avg": tr_avg,
            })

        # Recommendation
        n_high = len(high_stress_days)
        if n_high >= 3:
            recommendation = (
                f"{n_high} days this period combined hard training with high stress "
                f"(avg_stress > 60 and > 30 min training) — consider lighter sessions "
                f"on stressful days to avoid compounding fatigue."
            )
        elif n_high > 0:
            recommendation = (
                f"{n_high} day(s) combined hard training with high stress "
                f"this period — watch for accumulated fatigue if this pattern persists."
            )
        elif avg_daily_stress is not None and avg_daily_stress > 55:
            recommendation = (
                f"Life stress is elevated (avg {avg_daily_stress}/100) — "
                f"treat training load conservatively even on low-stress training days."
            )
        else:
            recommendation = (
                "Stress and training load look balanced over this window — "
                "no pattern of compounding hard sessions with high life stress."
            )

        return {
            "window": {"start": start_str, "end": end_str, "days_back": days_back},
            "days": days_list,
            "rolling_7d": rolling_7d,
            "high_stress_training_days": n_high,
            "avg_daily_stress": avg_daily_stress,
            "avg_training_min": avg_training_min,
            "best_recovery_day": best_recovery_day,
            "recommendation": recommendation,
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _extract_training_summary(readiness, status) -> dict:
    """Flatten the key fields out of Garmin's verbose readiness + status payloads.

    Returns a single-level dict of the things a coach actually checks:
    readiness score/level/feedback, recovery time, ACWR, acute load,
    training status verbal label (PRODUCTIVE / MAINTAINING / etc.) and
    current VO2max estimate. Missing fields are silently None; the raw
    payloads are still returned alongside for anything not extracted.
    """
    out: dict = {}

    r = readiness[0] if isinstance(readiness, list) and readiness else (
        readiness if isinstance(readiness, dict) else {}
    )
    if r and "error" not in r:
        out["readiness_score"] = r.get("score")
        out["readiness_level"] = r.get("level")
        out["readiness_feedback"] = r.get("feedbackLong") or r.get("feedbackShort")
        # Garmin's recoveryTime is in MINUTES, not hours, despite the
        # watch display showing hours. Convert before reporting.
        raw_rt_min = r.get("recoveryTime")
        out["recovery_time_hours"] = (
            round(raw_rt_min / 60) if raw_rt_min is not None else None
        )
        out["acute_load"] = r.get("acuteLoad")
        # Garmin doesn't expose a raw ACWR number — only the factor (0-100)
        # and a verbal feedback like "VERY_GOOD" / "POOR".
        out["acwr_factor_percent"] = r.get("acwrFactorPercent")
        out["acwr_factor_feedback"] = r.get("acwrFactorFeedback")
        out["hrv_factor_percent"] = r.get("hrvFactorPercent")
        out["hrv_factor_feedback"] = r.get("hrvFactorFeedback")
        out["sleep_score_factor_percent"] = r.get("sleepScoreFactorPercent")
        out["sleep_score_factor_feedback"] = r.get("sleepScoreFactorFeedback")
        out["sleep_history_factor_percent"] = r.get("sleepHistoryFactorPercent")
        out["sleep_history_factor_feedback"] = r.get("sleepHistoryFactorFeedback")
        out["stress_history_factor_percent"] = r.get("stressHistoryFactorPercent")
        out["stress_history_factor_feedback"] = r.get("stressHistoryFactorFeedback")
        out["recovery_time_factor_percent"] = r.get("recoveryTimeFactorPercent")
        out["recovery_time_factor_feedback"] = r.get("recoveryTimeFactorFeedback")

    if isinstance(status, dict) and "error" not in status:
        # VO2max lives under mostRecentVO2Max, not the training status block.
        vo2 = (status.get("mostRecentVO2Max") or {}).get("generic") or {}
        out["vo2max"] = vo2.get("vo2MaxValue")
        out["vo2max_precise"] = vo2.get("vo2MaxPreciseValue")
        out["fitness_age"] = vo2.get("fitnessAge")

        ts = status.get("mostRecentTrainingStatus") or {}
        latest = ts.get("latestTrainingStatusData") or {}
        # latestTrainingStatusData is keyed by deviceId — grab the first device.
        device_data = next(iter(latest.values()), {}) if isinstance(latest, dict) else {}
        if device_data:
            out["training_status_code"] = device_data.get("trainingStatus")
            out["training_status"] = device_data.get("trainingStatusFeedbackPhrase")
            out["weekly_training_load"] = device_data.get("weeklyTrainingLoad")
            out["load_tunnel_min"] = device_data.get("loadTunnelMin")
            out["load_tunnel_max"] = device_data.get("loadTunnelMax")
            out["fitness_trend"] = device_data.get("fitnessTrend")
            out["load_level_trend"] = device_data.get("loadLevelTrend")

    return out


@mcp.tool()
def morning_check_in() -> dict:
    """Today's recovery snapshot — flattened metrics, 7-day trend deltas,
    and Garmin's readiness/status assessments. All in one call.

    Returns:
    - `wellness.today`: flat HRV (overnight, weekly avg, baseline band,
      status), RHR, sleep (duration, score, deep/REM/light/awake),
      stress, body battery (high/low/at-wake), respiration, SpO2.
    - `wellness.trends`: prior 7-day mean + delta + stdev + deviation
      flag for each metric. Flag fires when today is >1σ outside the
      trailing mean in the "bad" direction (HRV ↓, RHR ↑, sleep ↓,
      stress ↑).
    - `training_summary`: flat readiness score/level/feedback, ACWR,
      acute load, recovery time, training status verbal (PRODUCTIVE /
      MAINTAINING / etc.), VO2max, weekly load.
    - `training_readiness_raw`, `training_status_raw`, `body_battery`:
      the full Garmin payloads for anything not flattened above.

    Use to decide whether to do a planned quality session today or shift
    it (e.g., HRV deviation_low + elevated RHR + low readiness → defer
    threshold). For multi-day trends beyond 7 days, use
    `get_wellness_history`.
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz
    g = _client()
    today = _date.today()
    yesterday = today - _td(days=1)
    history_start = (today - _td(days=8)).isoformat()
    history_end = yesterday.isoformat()

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    wellness = garmin_sync.morning_check_in_data(
        g, today.isoformat(), yesterday.isoformat(), history_start, history_end,
    )

    training_readiness_raw = safe(g.get_training_readiness, today.isoformat())
    training_status_raw = safe(g.get_training_status, today.isoformat())
    body_battery = safe(g.get_body_battery, today.isoformat())
    training_summary = _extract_training_summary(
        training_readiness_raw, training_status_raw
    )

    return {
        "date": today.isoformat(),
        "captured_at": _dt.now(_tz.utc).isoformat(),
        "wellness": wellness,
        "training_summary": training_summary,
        "training_readiness_raw": training_readiness_raw,
        "training_status_raw": training_status_raw,
        "body_battery": body_battery,
    }


@mcp.tool()
def sleep_performance_correlation(
    days_back: int = 60,
    min_distance_km: float = 3.0,
    session_type: str = "easy",
) -> dict:
    """Relate pre-run-night sleep to running performance, within one session class.

    Restricting to a single session class (default 'easy') is deliberate:
    a raw all-runs comparison is dominated by session-type mix (good-sleep
    days holding more easy runs makes that group look "slower" for reasons
    that have nothing to do with sleep). Sessions are classified via
    `classify_activity` (planned_type when linked, else the name hint).

    Sleep is keyed to the night BEFORE the run. In this repo sleep_* fields
    in wellness_daily are stored on the date the sleep STARTED, so the night
    before a run on date D lives in the row dated D-1. `hrv_overnight_avg`
    follows the opposite convention (it is "last night's" HRV as of the
    morning of its row date), so the morning-of-run HRV lives in the row
    dated D. The two are joined from their respective rows.

    Within the chosen class this computes Pearson correlations of
    sleep_score (and sleep hours) against avg_hr and pace, plus a
    good-sleep vs poor-sleep mean comparison. Correlations and group means
    are only reported when n is large enough to be defensible.

    Args:
        days_back: How many days of history to analyse. Default 60.
        min_distance_km: Minimum run distance to include. Default 3.0.
        session_type: Session class to restrict to (e.g. 'easy',
            'threshold', 'long', 'intervals'). Default 'easy'.

    Returns:
        good_sleep_avg, poor_sleep_avg, correlations, best_performances,
        insight string, and the per-run rows used for the analysis.
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta as _td

    # Below this, group means / correlations are not presented as findings.
    MIN_GROUP_N = 3
    MIN_CORR_N = 5

    try:
        cutoff = (_date.today() - _td(days=days_back)).isoformat()
        min_dist_m = min_distance_km * 1000

        with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    a.id,
                    date(a.start_date_local) AS run_date,
                    a.name,
                    a.sport_type,
                    a.planned_type,
                    a.avg_hr,
                    a.moving_time_s,
                    a.distance_m,
                    ws.sleep_seconds,
                    ws.sleep_score,
                    ws.sleep_deep_s,
                    ws.sleep_rem_s,
                    ws.sleep_light_s,
                    wh.hrv_overnight_avg,
                    wh.resting_hr AS wellness_rhr
                FROM activities a
                -- sleep_* are stored on the date sleep started: the night
                -- before the run is the row dated run_date - 1 day.
                LEFT JOIN wellness_daily ws
                       ON ws.date = date(a.start_date_local, '-1 day')
                -- hrv_overnight_avg is "last night" as of its row's morning,
                -- so the morning-of-run HRV is the row dated run_date.
                LEFT JOIN wellness_daily wh
                       ON wh.date = date(a.start_date_local)
                WHERE a.sport_type = 'Run'
                  AND date(a.start_date_local) >= ?
                  AND a.distance_m >= ?
                  AND a.moving_time_s IS NOT NULL
                  AND a.moving_time_s > 0
                ORDER BY a.start_date_local
                """,
                (cutoff, min_dist_m),
            ).fetchall()

        all_runs = [dict(r) for r in rows]

        # Restrict to one session class so the comparison is controlled.
        runs: list[dict] = []
        for r in all_runs:
            cls, src = garmin_sync.classify_activity(
                r["name"], r["sport_type"], r["planned_type"]
            )
            r["classification"] = cls
            r["classification_source"] = src
            if cls == session_type:
                runs.append(r)

        # Compute pace for each run
        for r in runs:
            if r["distance_m"] and r["moving_time_s"]:
                r["pace_s_per_km"] = r["moving_time_s"] / (r["distance_m"] / 1000)
            else:
                r["pace_s_per_km"] = None

        def _is_good_sleep(r: dict) -> bool:
            ss = r.get("sleep_score")
            secs = r.get("sleep_seconds")
            if ss is not None and ss >= 70:
                return True
            if secs is not None and secs >= 25200:  # 7 hours
                return True
            return False

        def _has_sleep_data(r: dict) -> bool:
            return r.get("sleep_score") is not None or r.get("sleep_seconds") is not None

        # Split into groups — only include runs that have sleep data
        runs_with_sleep = [r for r in runs if _has_sleep_data(r)]
        good_sleep = [r for r in runs_with_sleep if _is_good_sleep(r)]
        poor_sleep = [r for r in runs_with_sleep if not _is_good_sleep(r)]

        def _avg(values: list) -> Optional[float]:
            vals = [v for v in values if v is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        def _group_stats(group: list) -> dict:
            return {
                "n": len(group),
                "avg_hr": _avg([r["avg_hr"] for r in group]),
                "avg_pace_s_per_km": _avg([r["pace_s_per_km"] for r in group]),
                "avg_sleep_score": _avg([r["sleep_score"] for r in group]),
                "avg_sleep_hours": _avg(
                    [round(r["sleep_seconds"] / 3600, 2) for r in group
                     if r.get("sleep_seconds") is not None]
                ),
                "avg_hrv": _avg([r["hrv_overnight_avg"] for r in group]),
            }

        good_stats = _group_stats(good_sleep)
        poor_stats = _group_stats(poor_sleep)

        # Pearson correlations within the class. Only meaningful with enough
        # paired observations and non-zero variance on both axes.
        def _pearson(pairs: list[tuple[float, float]]) -> Optional[float]:
            xs = [p[0] for p in pairs if p[0] is not None and p[1] is not None]
            ys = [p[1] for p in pairs if p[0] is not None and p[1] is not None]
            n = len(xs)
            if n < MIN_CORR_N:
                return None
            mx = sum(xs) / n
            my = sum(ys) / n
            sxx = sum((x - mx) ** 2 for x in xs)
            syy = sum((y - my) ** 2 for y in ys)
            if sxx == 0 or syy == 0:
                return None
            sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            return round(sxy / (sxx ** 0.5 * syy ** 0.5), 3)

        correlations = {
            "n_with_sleep_score": sum(
                1 for r in runs_with_sleep if r.get("sleep_score") is not None
            ),
            "sleep_score_vs_avg_hr": _pearson(
                [(r["sleep_score"], r["avg_hr"]) for r in runs_with_sleep]
            ),
            "sleep_score_vs_pace": _pearson(
                [(r["sleep_score"], r["pace_s_per_km"]) for r in runs_with_sleep]
            ),
            "sleep_hours_vs_avg_hr": _pearson(
                [((r["sleep_seconds"] / 3600) if r.get("sleep_seconds") else None,
                  r["avg_hr"]) for r in runs_with_sleep]
            ),
        }

        # Best 5 performances within the class = lowest pace (fastest).
        runs_with_pace = [r for r in runs if r.get("pace_s_per_km") is not None]
        best_5 = sorted(runs_with_pace, key=lambda r: r["pace_s_per_km"])[:5]

        def _run_row(r: dict) -> dict:
            return {
                "id": r["id"],
                "date": r["run_date"],
                "name": r["name"],
                "classification": r["classification"],
                "classification_source": r["classification_source"],
                "distance_km": round(r["distance_m"] / 1000, 2),
                "pace_s_per_km": (
                    round(r["pace_s_per_km"], 1)
                    if r.get("pace_s_per_km") is not None else None
                ),
                "avg_hr": r["avg_hr"],
                "sleep_score": r["sleep_score"],
                "sleep_hours": (
                    round(r["sleep_seconds"] / 3600, 2)
                    if r.get("sleep_seconds") is not None else None
                ),
                "hrv_overnight_avg": r["hrv_overnight_avg"],
                "sleep_quality": "good" if _is_good_sleep(r) else (
                    "poor" if _has_sleep_data(r) else "no_data"
                ),
            }

        best_performances = [_run_row(r) for r in best_5]
        per_run = [_run_row(r) for r in runs]

        # Build insight string
        def _fmt_pace(s: Optional[float]) -> str:
            if s is None:
                return "N/A"
            m = int(s // 60)
            sec = int(s % 60)
            return f"{m}:{sec:02d}/km"

        insight_parts: list[str] = []
        g_n, p_n = good_stats["n"], poor_stats["n"]
        n_class = len(runs)
        n_sleep = len(runs_with_sleep)

        if n_class == 0:
            insight_parts.append(
                f"No '{session_type}' runs found in the last {days_back} days "
                f"(>= {min_distance_km} km). Try a different session_type or "
                "widen the window."
            )
        elif n_sleep == 0:
            insight_parts.append(
                f"Found {n_class} '{session_type}' runs but none have wellness "
                "data for the night before. Sync wellness data first."
            )
        else:
            insight_parts.append(
                f"Analysed {n_sleep} '{session_type}' runs with pre-run-night "
                f"sleep data over the last {days_back} days "
                f"({g_n} good-sleep, {p_n} poor-sleep)."
            )
            if g_n >= MIN_GROUP_N and p_n >= MIN_GROUP_N:
                pace_good = good_stats["avg_pace_s_per_km"]
                pace_poor = poor_stats["avg_pace_s_per_km"]
                hr_good = good_stats["avg_hr"]
                hr_poor = poor_stats["avg_hr"]
                if pace_good is not None and pace_poor is not None:
                    diff = round(pace_poor - pace_good, 1)
                    direction = "faster" if diff > 0 else "slower"
                    insight_parts.append(
                        f"Good-sleep pace: {_fmt_pace(pace_good)} vs "
                        f"poor-sleep: {_fmt_pace(pace_poor)} "
                        f"({abs(diff):.1f}s/km {direction} on good sleep)."
                    )
                if hr_good is not None and hr_poor is not None:
                    hr_diff = round(hr_poor - hr_good, 1)
                    insight_parts.append(
                        f"Good-sleep avg HR: {hr_good} bpm vs poor-sleep: "
                        f"{hr_poor} bpm (delta {hr_diff:+.1f} bpm)."
                    )
            else:
                insight_parts.append(
                    f"Too few sessions per group to compare means "
                    f"(good={g_n}, poor={p_n}, need >= {MIN_GROUP_N} each)."
                )
            corr_hr = correlations["sleep_score_vs_avg_hr"]
            if corr_hr is not None:
                insight_parts.append(
                    f"Sleep-score vs avg-HR correlation r={corr_hr:+.2f} "
                    f"(n={correlations['n_with_sleep_score']})."
                )
            else:
                insight_parts.append(
                    f"Not enough paired sleep-score observations for a "
                    f"correlation (need >= {MIN_CORR_N})."
                )

        return {
            "days_back": days_back,
            "min_distance_km": min_distance_km,
            "session_type": session_type,
            "total_runs_in_window": len(all_runs),
            "runs_in_class": n_class,
            "runs_with_sleep_data": n_sleep,
            "good_sleep_avg": good_stats,
            "poor_sleep_avg": poor_stats,
            "correlations": correlations,
            "best_performances": best_performances,
            "per_run": per_run,
            "insight": " ".join(insight_parts),
        }

    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


