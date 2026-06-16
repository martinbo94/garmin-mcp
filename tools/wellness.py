"""Wellness history, illness risk, stress balance, morning check-in, sleep."""
from typing import Optional

import garmin_sync
from core import _client, mcp


@mcp.tool()
def get_wellness_history(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    force_refetch: bool = False,
    baseline_days: int = 90,
) -> dict:
    """Historical daily wellness metrics (HRV, RHR, sleep, stress, body battery)
    with rolling averages AND a long-baseline drift check.

    Defaults to the last 7 days when no dates are given. Reads the local
    cache (assumed current with full history).

    The `baseline_comparison` block is the important part for spotting
    SUSTAINED drift: a trailing 7-day average moves with the drift, so a
    metric that's been elevated for two weeks still looks "within the 7-day
    average." This compares the recent week against the MEDIAN over the last
    `baseline_days` (~90) — drift-resistant — and reports how many days the
    metric has sat on the bad side (consecutive + within the last 14). Use
    this, not just the rolling mean, to judge whether e.g. RHR is genuinely
    back to normal vs. still elevated against the real baseline.

    Rolling averages (trailing, for the daily view):
    - **RHR:** simple 7-day mean.
    - **HRV:** 7-day **geometric mean** (HRV is ~log-normal; per Altini /
      HRV4Training).

    Args:
        start_date: 'YYYY-MM-DD' inclusive. Default: 7 days before end_date.
        end_date:   'YYYY-MM-DD' inclusive. Default: today.
        force_refetch: re-pull the detail range from Garmin even if cached.
        baseline_days: window for the drift-resistant baseline (default 90).

    Returns: range, daily, rolling, summary (as before), plus
    `baseline_comparison` (recent-vs-90d-median per metric, with flag +
    days-off-baseline drift counts).
    """
    from datetime import date as _date, timedelta as _td
    end = end_date or _date.today().isoformat()
    start = start_date or (_date.fromisoformat(end) - _td(days=6)).isoformat()

    sync_result = garmin_sync.sync_wellness_range(
        _client(), start, end, force_refetch=force_refetch
    )
    data = garmin_sync.wellness_history(start, end)
    data["sync"] = sync_result
    data["baseline_comparison"] = garmin_sync.wellness_baseline_comparison(
        as_of_date=end, baseline_days=baseline_days
    )
    return data


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
    """Today's recovery snapshot — the single readiness tool. Flattened
    metrics, three complementary trend lenses, and Garmin's assessments.

    Returns:
    - `wellness.today`: flat HRV (overnight, weekly avg, baseline band,
      status), RHR, sleep (duration, score, deep/REM/light/awake),
      stress, body battery (high/low/at-wake), respiration, SpO2.
    - `wellness.trends`: prior 7-day mean + delta + stdev + deviation flag
      per metric (today >1σ outside the trailing mean in the bad direction).
    - `wellness.baseline_comparison`: recent vs 90-day-median drift —
      catches SUSTAINED multi-week drift the 7-day trend can't see (e.g.
      RHR elevated for two weeks). Includes days-off-baseline counts.
    - `wellness.illness_signals`: acute illness-onset check (today vs 7-day
      mean across HRV/RHR/sleep/short-sleep/stress) → risk_level + flags.
      Folded in from the former illness_risk_check tool.
    - `training_summary`: flat readiness score/level/feedback, ACWR, acute
      load, recovery_time_hours (Garmin's own recovery estimate), training
      status verbal, VO2max, weekly load.
    - `training_readiness_raw`, `training_status_raw`, `body_battery`: full
      Garmin payloads for anything not flattened above.

    Use to decide whether to do a planned quality session or shift it — but
    weight the numbers per the athlete's context (for a sleep-disrupted
    user, HRV/RHR are weak signals; body feel + performance lead, and the
    long-baseline drift matters more than a single day). For trends beyond
    7 days use `get_wellness_history`.
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


