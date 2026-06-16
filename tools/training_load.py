"""Training load: deload, recovery prediction, ACWR, taper, return, double-day."""
from datetime import date
from typing import Optional

import garmin_sync
import plan as plan_mod
from core import mcp


@mcp.tool()
def deload_check(as_of_date: Optional[str] = None) -> dict:
    """Detect whether the current week is a recovery (deload) week.

    Queries the local activity cache for the last 5 weeks of runs and
    buckets them into Mon-Sun weeks. The current week is almost always
    *partial*, so comparing its raw km against a full-week average would
    fire a false positive every Monday/Tuesday/Wednesday. Instead the
    current week's km is compared against a **prorated** reference: the
    3-week rolling average scaled by how much of the week has elapsed
    (`avg_3week_km * days_elapsed / 7`). A deload is only flagged when
    the *projected* full-week volume (current_km / fraction_elapsed)
    falls to <= 60% of the 3-week average — the standard threshold for
    an intentional deload in most periodized plans.

    Planned-vs-unplanned is read from the plan, not guessed: if the
    scheduled workouts for the assessed week (coach_data/plan.json)
    include any session marked "(deload)" the deload is classified
    `planned`; otherwise `unplanned`.

    A lightweight `recovery_context` is surfaced from wellness_daily —
    recent HRV vs its baseline band and resting HR vs its trailing
    mean — purely as informational color alongside the volume verdict.

    Args:
        as_of_date: 'YYYY-MM-DD' to assess as of a specific day (default
            today). Useful for retrospectives and testing.

    Returns:
    - current_week_km: running distance so far this week (km)
    - projected_week_km: current_km extrapolated to a full week
    - avg_3week_km: mean of the 3 complete weeks before this one
    - prorated_reference_km: avg_3week_km scaled to the elapsed fraction
    - deload_ratio: projected_week_km / avg_3week_km (or null if no history)
    - is_deload: True when deload_ratio <= 0.60
    - deload_type: 'planned' | 'unplanned' | 'none'
    - recovery_context: HRV/RHR readout vs baseline (informational)
    - recommendation: plain-language coaching note
    - weekly_history: list of last 4 complete weeks + current
      (each entry: week_start, total_km, total_minutes, run_count)
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta as _td

    try:
        if as_of_date:
            today = _date.fromisoformat(as_of_date)
        else:
            today = _date.today()
        # Monday of current week
        current_week_start = today - _td(days=today.weekday())
        # Go back 5 weeks (4 complete + current)
        history_start = (current_week_start - _td(weeks=4)).isoformat()
        history_end = today.isoformat()

        with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                """
                SELECT start_date_local, distance_m, moving_time_s
                FROM activities
                WHERE sport_type = 'Run'
                  AND date(start_date_local) BETWEEN ? AND ?
                ORDER BY start_date_local
                """,
                (history_start, history_end),
            ).fetchall()

        # Bucket activities into Mon-Sun weeks
        week_buckets: dict[str, dict] = {}
        for r in rows:
            d = _date.fromisoformat(r["start_date_local"][:10])
            wk_monday = (d - _td(days=d.weekday())).isoformat()
            b = week_buckets.setdefault(wk_monday, {
                "week_start": wk_monday,
                "total_km": 0.0,
                "total_minutes": 0.0,
                "run_count": 0,
            })
            b["total_km"] += (r["distance_m"] or 0.0) / 1000.0
            b["total_minutes"] += (r["moving_time_s"] or 0) / 60.0
            b["run_count"] += 1

        # Ensure every complete week in the lookback window is present as a
        # zero-km bucket, so a week with no runs counts as 0 km in the
        # reference average instead of silently vanishing.
        for i in range(1, 5):
            wk = (current_week_start - _td(weeks=i)).isoformat()
            week_buckets.setdefault(wk, {
                "week_start": wk,
                "total_km": 0.0,
                "total_minutes": 0.0,
                "run_count": 0,
            })

        # Round km/minutes for readability
        for b in week_buckets.values():
            b["total_km"] = round(b["total_km"], 1)
            b["total_minutes"] = round(b["total_minutes"], 0)

        # Ordered list of all buckets (oldest first)
        all_weeks = sorted(week_buckets.values(), key=lambda w: w["week_start"])

        current_week_key = current_week_start.isoformat()
        current_week = week_buckets.get(current_week_key, {
            "week_start": current_week_key,
            "total_km": 0.0,
            "total_minutes": 0.0,
            "run_count": 0,
        })

        complete_weeks = [w for w in all_weeks if w["week_start"] != current_week_key]

        # Take up to 3 complete weeks immediately before the current week
        reference_weeks = complete_weeks[-3:]

        current_km = current_week["total_km"]

        # Fraction of the current week that has elapsed (Mon=1/7 … Sun=7/7).
        days_into_week = today.weekday()  # Mon=0 … Sun=6
        days_elapsed = days_into_week + 1
        fraction_elapsed = days_elapsed / 7.0
        # Project the partial week to a full-week equivalent.
        projected_week_km = round(current_km / fraction_elapsed, 1)

        if reference_weeks:
            avg_3week_km = round(
                sum(w["total_km"] for w in reference_weeks) / len(reference_weeks), 1
            )
        else:
            avg_3week_km = None

        prorated_reference_km = (
            round(avg_3week_km * fraction_elapsed, 1) if avg_3week_km is not None else None
        )

        if avg_3week_km is not None and avg_3week_km > 0:
            # Ratio is projected-full-week vs full-week average, so a young
            # week can no longer flag a deload purely for being incomplete.
            deload_ratio = round(projected_week_km / avg_3week_km, 2)
            is_deload = deload_ratio <= 0.60
        else:
            deload_ratio = None
            is_deload = False

        # ── Planned vs unplanned: read from the plan, do not guess ─────────
        # A week is a *planned* deload when its scheduled workouts include
        # any session whose name/description is marked "(deload)".
        deload_type = "none"
        planned_deload_workouts: list[str] = []
        if is_deload:
            plan = plan_mod.load_plan()
            if plan:
                week_end_key = (current_week_start + _td(days=6)).isoformat()
                for w in plan.get("workouts", []):
                    wdate = w.get("date", "")
                    if not (current_week_key <= wdate <= week_end_key):
                        continue
                    text = f"{w.get('name', '')} {w.get('description', '')}".lower()
                    if "(deload)" in text or "deload" in text:
                        planned_deload_workouts.append(w.get("name", wdate))
            deload_type = "planned" if planned_deload_workouts else "unplanned"

        # ── Recovery context from wellness_daily (informational) ───────────
        recovery_context = _deload_recovery_context(today)

        # Recommendation
        if deload_type == "unplanned":
            recommendation = (
                "Volume is well below the 3-week average and no deload is scheduled "
                "in the plan for this week — this looks like an unplanned drop "
                "(illness/injury/life) rather than a deliberate recovery week. "
                "Keep efforts genuinely easy and address the root cause before "
                "resuming normal load."
            )
        elif deload_type == "planned":
            recommendation = (
                "This is a scheduled recovery week (plan has deload-marked sessions) — "
                "keep easy efforts genuinely easy, avoid the temptation to add extra "
                "sessions, and trust the process. Sleep and nutrition quality matter "
                "most right now."
            )
        else:
            # Not a deload
            if avg_3week_km and projected_week_km > avg_3week_km * 1.15:
                recommendation = (
                    f"Volume is tracking above the 3-week average "
                    f"(projected {projected_week_km} km vs {avg_3week_km} km avg). "
                    "Monitor fatigue closely — consider whether this is an intentional "
                    "build week or an inadvertent overreach."
                )
            else:
                recommendation = (
                    f"Normal training week — {current_km} km so far "
                    f"(projecting ~{projected_week_km} km) vs "
                    f"{avg_3week_km} km 3-week average. No deload detected."
                ) if avg_3week_km else (
                    "Not enough history to assess deload status — sync more activities."
                )

        # Build weekly_history: last 4 complete weeks + current
        weekly_history = complete_weeks[-4:] + [current_week]

        return {
            "as_of_date": today.isoformat(),
            "current_week_start": current_week_key,
            "days_into_week": days_into_week,
            "current_week_km": current_km,
            "projected_week_km": projected_week_km,
            "avg_3week_km": avg_3week_km,
            "prorated_reference_km": prorated_reference_km,
            "deload_ratio": deload_ratio,
            "is_deload": is_deload,
            "deload_type": deload_type,
            "planned_deload_workouts": planned_deload_workouts,
            "recovery_context": recovery_context,
            "recommendation": recommendation,
            "weekly_history": weekly_history,
        }

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _deload_recovery_context(as_of: "date") -> dict:
    """Read HRV/RHR from the wellness cache and summarise vs baseline.

    Read-only and informational — never blocks or overrides the volume
    verdict. Compares the most recent few days of HRV against the Garmin
    baseline band (and resting HR against its trailing mean) and returns
    a short status string. Returns a 'no wellness data' status when the
    cache has nothing for the window.
    """
    from datetime import timedelta as _td

    try:
        window_start = (as_of - _td(days=10)).isoformat()
        window_end = as_of.isoformat()
        hist = garmin_sync.wellness_history(window_start, window_end)
        daily = hist.get("daily", [])
        if not daily:
            return {"status": "no wellness data", "detail": "No wellness rows cached for the window."}

        # Recent HRV (last up to 3 days with data) vs baseline band.
        hrv_recent = [d["hrv_overnight_avg"] for d in daily
                      if d.get("hrv_overnight_avg") is not None][-3:]
        rhr_recent = [d["resting_hr"] for d in daily
                      if d.get("resting_hr") is not None][-3:]
        baseline_band = hist.get("summary", {}).get("hrv_baseline_band")
        rhr_mean = hist.get("summary", {}).get("rhr_mean")

        # Rows existed but carried no usable HRV/RHR (older device, etc.).
        if not hrv_recent and not rhr_recent:
            return {
                "status": "no wellness data",
                "detail": "Wellness rows cached but no HRV/RHR values in the window.",
            }

        flags: list[str] = []
        hrv_recent_avg = round(sum(hrv_recent) / len(hrv_recent), 1) if hrv_recent else None
        rhr_recent_avg = round(sum(rhr_recent) / len(rhr_recent), 1) if rhr_recent else None

        if hrv_recent_avg is not None and baseline_band and baseline_band[0]:
            if hrv_recent_avg < baseline_band[0]:
                flags.append("HRV suppressed")
        if rhr_recent_avg is not None and rhr_mean is not None:
            # >3 bpm above the window mean reads as elevated.
            if rhr_recent_avg > rhr_mean + 3:
                flags.append("RHR elevated")

        status = ", ".join(flags) if flags else "wellness normal"
        return {
            "status": status,
            "hrv_recent_avg": hrv_recent_avg,
            "hrv_baseline_band": baseline_band,
            "rhr_recent_avg": rhr_recent_avg,
            "rhr_window_mean": rhr_mean,
        }
    except Exception as e:
        return {"status": "wellness unavailable", "detail": f"{type(e).__name__}: {e}"}


@mcp.tool()
def recovery_prediction(lookback_sessions: int = 8) -> dict:
    """Predict how many days it typically takes to return to baseline HRV
    after a quality session, based on your personal historical pattern.

    For each quality session in the last `lookback_sessions` quality runs,
    finds how many days until HRV returned to within the normal baseline
    band. Averages the measured ones to build a personal recovery profile,
    then applies it to the most recent quality session to estimate when
    you're likely fully recovered.

    A "quality session" is detected from the plan's `planned_type`
    (threshold / intervals / vo2 / race), the activity name, OR — as a
    fallback for default-named hard runs and races — Garmin's
    `training_effect_label` (VO2MAX / LACTATE_THRESHOLD /
    ANAEROBIC_CAPACITY). This catches max-effort races logged under a
    generic name (e.g. a 10k race named "Bærum Løping").

    The "recovered" verdict is gated on today's *actual* HRV being back in
    the baseline band — the predicted date merely passing is not treated
    as recovered. This stays consistent with morning_check_in's
    traffic-light HRV model. recovery_time_hours (Garmin's own estimate)
    is surfaced for cross-checking.

    Args:
        lookback_sessions: Number of past quality sessions to analyse.
            Default 8 — enough for a stable average without going too far
            back in time. At least 2 measured sessions are needed for a
            prediction.

    This predicts recovery from your most recent QUALITY session (see
    last_quality_session) — it is NOT a readiness check for right now and
    does not look at easy runs. For "am I ready to train today" use
    morning_check_in.

    Returns:
        - typical_recovery_days: your personal average (float)
        - confidence: 'low' / 'medium' / 'high' based on sample size
        - last_quality_session: date and type of the most recent hard session
        - predicted_recovered_by: estimated date of full HRV recovery
        - recovery_status: one of
            'recovered'        — today's HRV is confirmed in the baseline band
            'not_recovered'    — today's HRV is confirmed below baseline
            'likely_recovered' — predicted date has passed but there's no HRV
                                 reading today to confirm (NOT a negative signal)
            'recovering'       — still within the predicted window, unconfirmed
        - already_recovered: legacy boolean; True ONLY for 'recovered'. Prefer
            recovery_status — a False here can mean 'likely_recovered' (just
            no data), not actual non-recovery.
        - today_hrv_status: today's HRV vs baseline (or 'no_data')
        - garmin_recovery_time_hours: Garmin's own recovery estimate, if any
        - sample: list of past sessions with their measured recovery days
        - censored_sessions: count whose HRV had not returned within 7 days
        - no_depression_sessions: count where HRV never dropped below baseline
        - note: human-readable summary
    """
    import sqlite3 as _sq
    from datetime import datetime as _dt, timedelta as _td

    try:
        _db = garmin_sync.DB_PATH

        # ── 1. Find quality sessions ──────────────────────────────────
        # Detect via plan label (planned_type), name hint, OR Garmin's
        # training-effect label as a fallback for default-named hard runs.
        _QUALITY_PLANNED = {"threshold", "intervals", "vo2", "race"}
        _QUALITY_NAME = {"threshold", "intervals", "tempo", "race"}
        _QUALITY_TE = {"VO2MAX", "LACTATE_THRESHOLD", "ANAEROBIC_CAPACITY"}

        with _sq.connect(_db) as conn:
            conn.row_factory = _sq.Row
            acts = conn.execute("""
                SELECT id, start_date_local, name, sport_type, avg_hr,
                       planned_type, training_effect_label
                FROM activities
                WHERE sport_type = 'Run'
                ORDER BY start_date_local DESC
                LIMIT 120
            """).fetchall()

        def _quality_type(a) -> Optional[str]:
            """Return the quality label for a session, or None if not hard."""
            pt = (a["planned_type"] or "").lower()
            if pt in _QUALITY_PLANNED:
                return pt
            hint = garmin_sync.name_hint(a["name"], a["sport_type"])
            if hint in _QUALITY_NAME:
                return hint
            te = (a["training_effect_label"] or "").upper()
            if te in _QUALITY_TE:
                return te.lower()
            return None

        quality = []
        for a in acts:
            qt = _quality_type(a)
            if qt is not None:
                d = dict(a)
                d["quality_type"] = qt
                quality.append(d)
            if len(quality) >= lookback_sessions + 2:
                break

        if not quality:
            return {
                "error": "No quality sessions found in recent history.",
                "note": "Record some threshold, interval, or race sessions first.",
            }

        # ── 2. For each quality session, find days until HRV recovered ─
        with _sq.connect(_db) as conn:
            conn.row_factory = _sq.Row
            wellness = conn.execute("""
                SELECT date, hrv_overnight_avg, hrv_baseline_low, hrv_baseline_upper,
                       hrv_status, recovery_time_hours
                FROM wellness_daily
                ORDER BY date ASC
            """).fetchall()

        wellness_by_date = {r["date"]: dict(r) for r in wellness}

        def _hrv_in_baseline(w: Optional[dict]) -> Optional[bool]:
            """True/False if HRV state is known for the day, None if unknown.

            Unknown (missing day / no HRV reading) returns None so callers
            can skip it rather than treating absence as 'not recovered'.
            """
            if not w:
                return None
            hrv = w.get("hrv_overnight_avg")
            if hrv is None:
                return None
            lo = w.get("hrv_baseline_low")
            hi = w.get("hrv_baseline_upper")
            if lo and hi:
                return lo <= hrv <= hi
            # Fallback: use Garmin's status string
            status = (w.get("hrv_status") or "").lower()
            return status in ("balanced", "good")

        sample = []
        censored = 0          # HRV had not returned within the 7-day window
        no_depression = 0     # HRV was never below baseline (no measurable dip)
        for sess in quality[:lookback_sessions]:
            sess_date = sess["start_date_local"][:10]

            # Did this session actually depress HRV? If the day after is
            # already in-baseline (and we have data for it), there is no
            # measurable recovery to time — flag rather than score "1 day".
            day1 = (_dt.strptime(sess_date, "%Y-%m-%d") + _td(days=1)).strftime("%Y-%m-%d")
            day1_state = _hrv_in_baseline(wellness_by_date.get(day1))

            recovery_days = None
            saw_suppressed = False
            for d in range(1, 8):
                check_date = (
                    _dt.strptime(sess_date, "%Y-%m-%d") + _td(days=d)
                ).strftime("%Y-%m-%d")
                state = _hrv_in_baseline(wellness_by_date.get(check_date))
                if state is None:
                    continue  # missing/unknown day — skip, don't penalise
                if state is False:
                    saw_suppressed = True
                elif state is True:
                    if saw_suppressed:
                        recovery_days = d
                    break

            if recovery_days is not None:
                sample.append({
                    "date": sess_date,
                    "session_type": sess["quality_type"],
                    "name": sess["name"],
                    "recovery_days": recovery_days,
                })
            elif not saw_suppressed and day1_state is True:
                # HRV never dipped after this session — don't average a
                # phantom "1 day" recovery; count it separately.
                no_depression += 1
            elif saw_suppressed:
                # HRV was suppressed but had not returned within 7 days —
                # a slow recovery. Counting it (not silently dropping) keeps
                # the mean from being biased downward.
                censored += 1

        if len(sample) < 2:
            note = (
                "Not enough measured recoveries to build a pattern yet "
                f"({len(sample)} of {len(quality)} quality sessions had a "
                "suppression-then-return signal in the HRV record)."
            )
            if no_depression:
                note += f" {no_depression} session(s) never depressed HRV."
            if censored:
                note += f" {censored} session(s) had not recovered within 7 days."
            return {
                "error": "Not enough HRV data to build a recovery pattern.",
                "sessions_found": len(quality),
                "sessions_with_measured_recovery": len(sample),
                "no_depression_sessions": no_depression,
                "censored_sessions": censored,
                "note": note,
            }

        avg_recovery = round(sum(s["recovery_days"] for s in sample) / len(sample), 1)
        n = len(sample)
        confidence = "high" if n >= 6 else "medium" if n >= 3 else "low"

        # ── 3. Apply to most recent quality session ────────────────────
        last_sess = quality[0]
        last_date = last_sess["start_date_local"][:10]
        predicted_date = (
            _dt.strptime(last_date, "%Y-%m-%d") + _td(days=round(avg_recovery))
        ).strftime("%Y-%m-%d")

        # "Recovered" is gated on today's ACTUAL HRV being in baseline — a
        # passed predicted date is NOT sufficient. Use local date (UTC can
        # roll to the wrong day 00:00-02:00 Norway time).
        today_str = _dt.now().strftime("%Y-%m-%d")
        today_wellness = wellness_by_date.get(today_str)
        today_state = _hrv_in_baseline(today_wellness)
        if today_state is True:
            today_hrv_status = "in_baseline"
        elif today_state is False:
            today_hrv_status = "below_baseline"
        else:
            today_hrv_status = "no_data"

        garmin_recovery_hours = (
            today_wellness.get("recovery_time_hours") if today_wellness else None
        )

        # Tri-state recovery_status disambiguates the three distinct cases a
        # bare boolean conflates — most importantly "no HRV today" must NOT
        # read as "not recovered". Note this is recovery from the last
        # QUALITY session (see last_quality_session), not readiness right now
        # (use morning_check_in for that).
        date_passed = predicted_date <= today_str
        if today_state is True:
            recovery_status = "recovered"            # HRV confirms in baseline
        elif today_state is False:
            recovery_status = "not_recovered"        # HRV confirms below baseline
        elif date_passed:
            recovery_status = "likely_recovered"     # date passed, no HRV to confirm
        else:
            recovery_status = "recovering"           # within predicted window, unconfirmed
        already = today_state is True

        note = (
            f"Based on {n} measured session(s), your HRV typically recovers in "
            f"{avg_recovery} days after a quality session. "
        )
        if already:
            note += "Today's HRV is back in the baseline band — you're recovered."
        elif today_state is False:
            if predicted_date <= today_str:
                note += (
                    f"The predicted recovery date ({predicted_date}) has passed, "
                    "but today's HRV is still below baseline — not yet recovered."
                )
            else:
                note += (
                    f"After your last quality session ({last_date}), expect full "
                    f"recovery around {predicted_date}. Today's HRV is still below baseline."
                )
        else:
            # No HRV reading for today — can't confirm recovery either way.
            if predicted_date <= today_str:
                note += (
                    f"The predicted recovery date ({predicted_date}) has passed, but "
                    "there's no HRV reading for today to confirm recovery — wear your "
                    "watch overnight to verify."
                )
            else:
                note += (
                    f"After your last quality session ({last_date}), expect full "
                    f"recovery around {predicted_date}."
                )
        if garmin_recovery_hours:
            note += (
                f" (Garmin's own recovery estimate today: {garmin_recovery_hours}h "
                "remaining.)"
            )
        if censored:
            note += (
                f" Note: {censored} recent session(s) had not recovered within 7 days "
                "and are excluded from the average (true recovery may be slightly longer)."
            )
        if no_depression:
            note += (
                f" {no_depression} session(s) never depressed HRV and are excluded."
            )

        return {
            "typical_recovery_days": avg_recovery,
            "confidence": confidence,
            "sample_size": n,
            "last_quality_session": {
                "date": last_date,
                "name": last_sess["name"],
                "type": last_sess["quality_type"],
            },
            "predicted_recovered_by": predicted_date,
            "recovery_status": recovery_status,
            "already_recovered": already,
            "today_hrv_status": today_hrv_status,
            "garmin_recovery_time_hours": garmin_recovery_hours,
            "censored_sessions": censored,
            "no_depression_sessions": no_depression,
            "sample": sample,
            "note": note,
        }

    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}




# ─── Training load balance (ACWR) ─────────────────────────────────────
@mcp.tool()
def training_load_balance(as_of_date: Optional[str] = None) -> dict:
    """Estimate the Acute:Chronic Workload Ratio (ACWR) from cached run history.

    Load metric is **run duration in minutes** (duration-only — there is no
    intensity / HR / TRIMP weighting, even though avg_hr is cached, so a hard
    interval session and an easy run of equal length count the same). Both
    windows are *rolling* windows ending on the as-of date — NOT calendar
    weeks — so the figure is stable on any weekday (including Monday morning).

    - acute load   = total run minutes over the rolling LAST 7 DAYS
                     (as_of - 6 days .. as_of, inclusive).
    - chronic load = total run minutes over the rolling LAST 28 DAYS
                     (as_of - 27 days .. as_of, inclusive), divided by 4 to
                     express it as a weekly-equivalent that matches the 7-day
                     acute window. Note the windows are *coupled*: the acute
                     7 days sit inside the chronic 28 days.
    - acwr = acute / chronic_weekly. If chronic is 0 (not enough history),
      acwr is None and a "not enough history" message is returned.

    The 0.8 / 1.3 / 1.5 ACWR thresholds below are a commonly-used heuristic
    (Gabbett et al.) — the underlying injury model is contested in the
    literature and has NOT been validated for this athlete. This repo's
    framework is Bakken threshold-based, not ACWR-based; treat the zone as a
    rough volume-trend sanity check, not a hard injury predictor.

    Args:
        as_of_date: Optional 'YYYY-MM-DD' anchor date for retrospectives.
            Defaults to today (local date). Both windows end on this date.

    Returns:
        as_of_date             : the anchor date used
        acute_load_min         : total run minutes over the last 7 days
        chronic_load_min       : weekly-equivalent run minutes (last 28 days / 4)
        acwr                   : rounded to 2 dp (None if chronic is 0)
        zone                   : 'undertraining' | 'optimal' | 'caution'
                                 | 'high_risk' (None if chronic is 0)
        recommendation         : coaching string
        weekly_breakdown       : list of {week_start, week_end, run_minutes} for
                                 the 4 rolling 7-day blocks ending on as_of
                                 (oldest first; last block == acute window)
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta as _td

    try:
        if as_of_date is not None:
            try:
                as_of = _date.fromisoformat(as_of_date)
            except ValueError as e:
                return {"error": f"Invalid as_of_date: {e}"}
        else:
            as_of = _date.today()

        acute_start = as_of - _td(days=6)    # rolling last 7 days, inclusive
        chronic_start = as_of - _td(days=27)  # rolling last 28 days, inclusive

        with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT date(start_date_local) AS run_date,
                       COALESCE(NULLIF(moving_time_s, 0), elapsed_time_s, 0)
                           AS load_s
                FROM activities
                WHERE sport_type = 'Run'
                  AND date(start_date_local) BETWEEN ? AND ?
                ORDER BY run_date
                """,
                (chronic_start.isoformat(), as_of.isoformat()),
            ).fetchall()

        # Build 4 rolling 7-day blocks ending on as_of (oldest first).
        weeks: list[dict] = []
        for block_offset in range(3, -1, -1):
            wk_end = as_of - _td(days=7 * block_offset)
            wk_start = wk_end - _td(days=6)
            weeks.append({
                "week_start": wk_start.isoformat(),
                "week_end": wk_end.isoformat(),
                "run_minutes": 0.0,
            })

        acute_load_min = 0.0
        chronic_total_min = 0.0
        for run_date_str, load_s in rows:
            minutes = (load_s or 0) / 60.0
            chronic_total_min += minutes
            if acute_start.isoformat() <= run_date_str <= as_of.isoformat():
                acute_load_min += minutes
            for bucket in weeks:
                if bucket["week_start"] <= run_date_str <= bucket["week_end"]:
                    bucket["run_minutes"] += minutes
                    break

        acute_load_min = round(acute_load_min, 1)
        # Chronic expressed as a weekly equivalent (28 days / 4).
        chronic_load_min = round(chronic_total_min / 4.0, 1)
        for bucket in weeks:
            bucket["run_minutes"] = round(bucket["run_minutes"], 1)

        if chronic_load_min == 0:
            return {
                "as_of_date": as_of.isoformat(),
                "acute_load_min": acute_load_min,
                "chronic_load_min": chronic_load_min,
                "acwr": None,
                "zone": None,
                "recommendation": (
                    "Not enough history — no run load found in the last 28 days, "
                    "so a chronic baseline can't be computed. Sync activities "
                    "(or pick a later as_of_date with cache coverage), then "
                    "rebuild volume gently."
                ),
                "weekly_breakdown": weeks,
            }

        acwr = round(acute_load_min / chronic_load_min, 2)
        if acwr < 0.8:
            zone = "undertraining"
            recommendation = (
                f"ACWR {acwr} — below the 0.8 floor. Recent volume is low "
                "relative to your 28-day baseline. A moderate increase in "
                "easy volume is reasonable. (Heuristic only, not validated "
                "for you, and duration-only — it ignores intensity.)"
            )
        elif acwr <= 1.3:
            zone = "optimal"
            recommendation = (
                f"ACWR {acwr} — in the commonly-cited 0.8–1.3 'optimal' band. "
                "Recent load is well-matched to your 28-day base. (Heuristic "
                "only, not validated for you, and duration-only.)"
            )
        elif acwr <= 1.5:
            zone = "caution"
            recommendation = (
                f"ACWR {acwr} — in the 1.3–1.5 'caution' band. Acute load is "
                "running ahead of your chronic base; consider holding volume "
                "steady. (Heuristic only, not validated for you, and "
                "duration-only.)"
            )
        else:
            zone = "high_risk"
            recommendation = (
                f"ACWR {acwr} — above 1.5. Acute load is well ahead of your "
                "chronic base; an easier day could let the base catch up. "
                "(Heuristic only, not validated for you, and duration-only.)"
            )

        return {
            "as_of_date": as_of.isoformat(),
            "acute_load_min": acute_load_min,
            "chronic_load_min": chronic_load_min,
            "acwr": acwr,
            "zone": zone,
            "recommendation": recommendation,
            "weekly_breakdown": weeks,
        }

    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}



# ─── Taper planner ────────────────────────────────────────────────────
@mcp.tool()
def taper_plan(
    race_date: str,
    race_distance_km: float,
    current_weekly_km: Optional[float] = None,
) -> dict:
    """Generate a race taper schedule from today to race day.

    Applies standard percentage-based taper reductions:
    - Marathon (>35 km):        3-week taper — Week -3: 80%, Week -2: 60%, Week -1: 40%
    - Half/10k (10–35 km):      2-week taper — Week -2: 70%, Week -1: 40%
    - 5k and shorter (<10 km):  1-week taper — Week -1: 60%

    Args:
        race_date: Race day in 'YYYY-MM-DD' format.
        race_distance_km: Race distance in km (e.g. 10, 21.1, 42.2).
        current_weekly_km: Your current weekly volume baseline. If omitted,
            estimated from the last 4 weeks in the activity cache (run km
            only). Pass explicitly when you want to override the estimate.

    Returns:
        - `taper_weeks`: ordered list of weekly targets from taper start
          to race week. Each entry has `week_start`, `week_end`,
          `target_km`, `sessions`, `key_sessions`, and `notes`.
        - `race_week`: {date, distance_km, advice} for race day itself.
        - `base_weekly_km`: the baseline volume used for calculations.
        - `base_source`: 'provided' | 'cache_estimate' | 'default'.
        - `warning`: set when the race is fewer than 7 days away (full
          taper is not possible).
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta as _td

    try:
        today = _date.today()
        race = _date.fromisoformat(race_date)
    except ValueError as e:
        return {"error": f"Invalid race_date: {e}"}

    days_until_race = (race - today).days
    warning: Optional[str] = None
    if days_until_race < 7:
        warning = (
            f"Race is only {days_until_race} day(s) away — a full taper is not possible. "
            "Focus on rest, short easy runs at most, and race-day logistics."
        )

    # ── Determine base volume ─────────────────────────────────────────
    base_source: str
    if current_weekly_km is not None:
        base_km = float(current_weekly_km)
        base_source = "provided"
    else:
        # Estimate from last 4 weeks of run activities in cache.
        try:
            four_weeks_ago = (today - _td(weeks=4)).isoformat()
            with _sqlite3.connect(garmin_sync.DB_PATH) as _conn:
                rows = _conn.execute(
                    """
                    SELECT start_date_local, distance_m
                    FROM activities
                    WHERE start_date_local >= ?
                      AND sport_type = 'Run'
                      AND distance_m IS NOT NULL
                    ORDER BY start_date_local
                    """,
                    (four_weeks_ago,),
                ).fetchall()

            if rows:
                total_m = sum(r[1] for r in rows)
                base_km = round(total_m / 4000, 1)  # 4 weeks, m → km
                base_source = "cache_estimate"
            else:
                base_km = 40.0  # conservative default
                base_source = "default"
        except Exception:
            base_km = 40.0
            base_source = "default"

    # ── Choose taper schedule ─────────────────────────────────────────
    # Each entry: (week_offset_before_race, pct, sessions, key_session_note, notes)
    if race_distance_km > 35:
        # Marathon: 3-week taper
        schedule = [
            (3, 0.80, 5, "One sub-threshold session (shorter reps), one medium long run (13-15 km)",
             "Reduce long run to ~13-15 km. Keep one quality sub-threshold session at normal pace but fewer reps. Eliminate second quality session."),
            (2, 0.60, 4, "One short quality session (20-25 min of reps), long run ≤12 km",
             "Drop long run to 12 km or less. Quality session: short reps only (e.g. 4-5 × 4 min). No tempo runs."),
            (1, 0.40, 3, "2-3 short easy runs + 1 × 10-15 min strides session",
             "Race week — all easy. Optional strides mid-week to keep legs sharp. No quality work after Thursday."),
        ]
    elif race_distance_km >= 10:
        # Half marathon / 10k: 2-week taper
        schedule = [
            (2, 0.70, 4, "One quality session (sub-threshold, 60-70% of normal rep volume)",
             "Reduce overall volume but maintain one quality session. Long run shortened by ~30%."),
            (1, 0.40, 3, "2 easy runs + optional strides mid-week",
             "Race week — mostly easy. Short strides 2 days before race if feeling flat. No hard efforts after Wednesday."),
        ]
    else:
        # 5k and shorter: 1-week taper
        schedule = [
            (1, 0.60, 3, "1-2 easy runs + short strides 2 days before race",
             "Race week — keep legs fresh. A single short quality session early in the week is optional. Race-pace strides are enough stimulus."),
        ]

    # ── Build week entries ────────────────────────────────────────────
    def _monday(d: _date) -> _date:
        return d - _td(days=d.weekday())

    race_monday = _monday(race)
    taper_weeks = []

    for week_offset, pct, sessions, key_sessions, notes in schedule:
        week_start = race_monday - _td(weeks=week_offset)
        week_end = week_start + _td(days=6)
        target_km = round(base_km * pct, 1)
        taper_weeks.append({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "target_km": target_km,
            "volume_pct": int(pct * 100),
            "sessions": sessions,
            "key_sessions": key_sessions,
            "notes": notes,
        })

    race_advice = (
        "Rest and hydrate. Short 10-15 min shakeout run the day before is optional. "
        "Trust the taper — fitness is locked in. Focus on pacing strategy and race-day logistics."
    )

    result: dict = {
        "race_date": race_date,
        "race_distance_km": race_distance_km,
        "days_until_race": days_until_race,
        "base_weekly_km": base_km,
        "base_source": base_source,
        "taper_weeks": taper_weeks,
        "race_week": {
            "date": race_date,
            "distance_km": race_distance_km,
            "advice": race_advice,
        },
    }
    if warning:
        result["warning"] = warning
    return result


@mcp.tool()
def return_from_break() -> dict:
    """Detect a running inactivity gap and generate a conservative ramp-back plan.

    Queries the local activity cache to find the most recent run and computes
    how many days have elapsed since it. If fewer than 7 days have passed,
    returns an "active" status. Otherwise generates a week-by-week return plan
    scaled to the break length.

    Break length → plan:
    - 1-2 weeks off: 2-week ramp, start at 60% of pre-break volume, +20%/week
    - 2-4 weeks off: 3-week ramp, start at 50%, +20%/week
    - 4-8 weeks off: 4-week ramp, start at 40%, +15%/week
    - 8+ weeks off: 5-week ramp, start at 30%, +15%/week (includes clearance note)

    Pre-break volume is the average weekly km over the 4 weeks immediately
    before the break.

    Each week in the return plan includes:
    - target_km: total weekly volume target
    - max_single_run_km: longest single run allowed (40% of weekly)
    - session_count: recommended number of sessions (3-4)
    - notes: coaching notes including quality-session restrictions

    Returns:
    - status: 'active' | 'break_detected'
    - break_days: days since last run
    - pre_break_weekly_km: estimated pre-break weekly volume
    - return_weeks: list of week plans
    - first_quality_session_week: earliest week number where quality is allowed
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date

    try:
        from garmin_sync import DB_PATH as _DB_PATH
        today = _date.today()
        with _sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                """
                SELECT start_date_local, distance_m
                FROM activities
                WHERE sport_type = 'Run' AND distance_m IS NOT NULL
                ORDER BY start_date_local DESC
                LIMIT 1
                """
            ).fetchone()

            if not row:
                return {
                    "status": "no_data",
                    "message": "No runs found in the local cache. Run sync_activities() first.",
                }

            last_run_date_str = row[0][:10]
            last_run_date = _date.fromisoformat(last_run_date_str)
            break_days = (today - last_run_date).days

            if break_days < 7:
                return {
                    "status": "active",
                    "message": f"No significant break detected. Last run was {break_days} day(s) ago on {last_run_date_str}.",
                    "break_days": break_days,
                    "last_run_date": last_run_date_str,
                }

            # Pre-break volume: 4 weeks before the last run
            pre_break_end = last_run_date_str
            from datetime import timedelta as _td
            pre_break_start = (last_run_date - _td(weeks=4)).isoformat()

            weekly_rows = conn.execute(
                """
                SELECT
                    strftime('%Y-%W', start_date_local) AS iso_week,
                    SUM(distance_m) / 1000.0 AS weekly_km
                FROM activities
                WHERE sport_type = 'Run'
                    AND distance_m IS NOT NULL
                    AND date(start_date_local) BETWEEN ? AND ?
                GROUP BY iso_week
                ORDER BY iso_week
                """,
                (pre_break_start, pre_break_end),
            ).fetchall()

        if weekly_rows:
            total_km = sum(r[1] for r in weekly_rows)
            pre_break_weekly_km = round(total_km / len(weekly_rows), 1)
        else:
            # Fallback: use the last known run and estimate 20 km/week
            pre_break_weekly_km = 20.0

        # Determine plan parameters based on break length
        break_weeks = break_days / 7
        if break_weeks < 2:
            plan_weeks = 2
            start_pct = 0.60
            step_pct = 0.20
            medical_note = None
        elif break_weeks < 4:
            plan_weeks = 3
            start_pct = 0.50
            step_pct = 0.20
            medical_note = None
        elif break_weeks < 8:
            plan_weeks = 4
            start_pct = 0.40
            step_pct = 0.15
            medical_note = None
        else:
            plan_weeks = 5
            start_pct = 0.30
            step_pct = 0.15
            medical_note = "Consider medical clearance before resuming training after an 8+ week break."

        # Build the weekly return plan
        return_weeks = []
        for week_num in range(1, plan_weeks + 1):
            pct = start_pct + step_pct * (week_num - 1)
            target_km = round(pre_break_weekly_km * pct, 1)
            max_single_km = round(target_km * 0.40, 1)

            # Session count: 3 in week 1, 4 from week 2 onward
            session_count = 3 if week_num == 1 else 4

            # Quality session policy
            if week_num == 1:
                notes = (
                    "Easy running only — no quality sessions. "
                    "All runs at conversational/Z1-Z2 effort. "
                    "Stop if any pain or unusual fatigue."
                )
            elif week_num == 2:
                notes = (
                    "Continue building aerobic base. "
                    "Optional: one gentle fartlek with 4-6 short pickups (30s each) "
                    "only if week 1 felt effortless."
                )
            else:
                notes = (
                    f"Week {week_num}: quality sessions allowed. "
                    "Introduce one sub-threshold session (e.g. 3×8 min at sub-threshold pace). "
                    "Keep remaining sessions easy."
                )

            week_entry: dict = {
                "week": week_num,
                "target_km": target_km,
                "volume_pct_of_prebreak": round(pct * 100),
                "max_single_run_km": max_single_km,
                "session_count": session_count,
                "notes": notes,
            }
            if medical_note and week_num == 1:
                week_entry["medical_note"] = medical_note
            return_weeks.append(week_entry)

        result: dict = {
            "status": "break_detected",
            "break_days": break_days,
            "last_run_date": last_run_date_str,
            "pre_break_weekly_km": pre_break_weekly_km,
            "return_weeks": return_weeks,
            "first_quality_session_week": 3,
            "plan_note": (
                f"{break_days}-day break ({break_days // 7} week(s)). "
                f"Pre-break base: {pre_break_weekly_km} km/week. "
                f"{plan_weeks}-week return ramp starting at "
                f"{round(start_pct * 100)}% of base volume."
            ),
        }
        if medical_note:
            result["medical_note"] = medical_note

        return result

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def double_day_advisor(
    target_weekly_km: Optional[float] = None,
    user_confirms_ready: bool = False,
) -> dict:
    """Advise whether a runner is ready to adopt double-THRESHOLD days.

    Double-threshold days are the gatekept *advanced* Bakken variant: two
    SUB-threshold sessions in one day, 6-8 h apart, both in the Golden Zone
    (neither at-threshold). They compound weekly threshold volume well beyond
    the default Norwegian Singles framework — but only when a deep aerobic
    base is already in place. The default for everyone is Singles, NOT doubles.

    See coach_data/training_philosophy.md → "Advanced variant:
    double-threshold days". Preconditions (per the doc):
    - Sustained ~70+ km/week (ideally 100+).
    - >= 8-12 weeks of consistent sub-threshold singles.
    - Goal race >= 10k (less benefit for a pure 5k focus).
    - The USER explicitly wants to try it (informed opt-in).

    ELIGIBILITY — base OR informed opt-in (not both required):
        `eligible: true` is returned when EITHER the objective base is in place
        (volume / consistency / recovery checks pass) OR the user has explicitly
        opted in via `user_confirms_ready=True`. Meeting the base is not a hard
        gate — an athlete who understands the trade-off can choose to proceed
        without it; the tool then sets `override: true` and a prominent caution
        listing exactly which preconditions are unmet and the added risk. The
        ONLY not-eligible case is: base not met AND no explicit opt-in — there
        the tool explains the gap and offers the opt-in path rather than
        refusing. Only pass `user_confirms_ready=True` after the USER has
        actually said they want to proceed — never decide that for them.

    WHEN NOT TO USE:
    - The user has not asked about double days and is on the Singles default
      (don't volunteer this — it's an advanced variant most runners shouldn't
      adopt). Surface it only when the user raises double days.
    - There is a race or hard quality session scheduled in the next 3 days
      (checked against plan.json), or wellness flags poor recovery today.

    Args:
        target_weekly_km: Target weekly km for context. If None, estimated
            from the trailing full-week average. Pass explicitly to override.
        user_confirms_ready: Set True ONLY after the user has explicitly said
            they want to try double days. Grants eligibility as an informed
            override even when the objective base isn't met.

    Returns:
        - eligible (bool): True when the base is met OR the user opted in.
        - override (bool): True when eligible only via opt-in (base not met).
        - awaiting_user_confirmation (bool): True only when base not met and
          no opt-in yet — the tool is offering the informed-consent path.
        - reason (str): why eligible / not / awaiting confirmation.
        - preconditions / failed_preconditions: status against each check.
        - suggested_structure: AM + PM sub-threshold plan when eligible.
        - weekly_context: trailing volume + today's wellness.
        - caution (str): safety reminder; stronger when override is True.
    """
    import sqlite3 as _sqlite3
    from datetime import date as _date, timedelta as _td

    # Precondition thresholds (from training_philosophy.md).
    MIN_WEEKLY_KM = 70.0          # sustained 70+ km/week (ideally 100+)
    MIN_CONSISTENT_WEEKS = 8      # >= 8-12 weeks of consistency
    CONSISTENT_WEEK_FLOOR_KM = 40.0  # a week "counts" only if it has real volume

    try:
        today = _date.today()
        # Current week: Mon to today.
        week_start = today - _td(days=today.weekday())
        # Monday-aligned window: the Monday MIN_CONSISTENT_WEEKS *full* weeks
        # before the current week. The current (incomplete) week is excluded
        # from the baseline; each prior week is a complete Mon-Sun bucket.
        window_start = week_start - _td(weeks=MIN_CONSISTENT_WEEKS)

        with _sqlite3.connect(garmin_sync.DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row

            # ── Runs across the full-week baseline window ─────────────────
            run_rows = conn.execute(
                """
                SELECT start_date_local, moving_time_s, distance_m
                FROM activities
                WHERE sport_type = 'Run'
                  AND date(start_date_local) >= ?
                  AND date(start_date_local) < ?
                ORDER BY start_date_local
                """,
                (window_start.isoformat(), week_start.isoformat()),
            ).fetchall()

            # ── Current week's runs ───────────────────────────────────────
            current_week_rows = conn.execute(
                """
                SELECT moving_time_s, distance_m
                FROM activities
                WHERE sport_type = 'Run'
                  AND date(start_date_local) >= ?
                  AND date(start_date_local) <= ?
                """,
                (week_start.isoformat(), today.isoformat()),
            ).fetchall()

            # ── Wellness for today ────────────────────────────────────────
            wellness_row = conn.execute(
                "SELECT body_battery_at_wake, hrv_status, recovery_time_hours "
                "FROM wellness_daily WHERE date = ?",
                (today.isoformat(),),
            ).fetchone()

        # ── Build one complete Mon-Sun bucket per week in the window ───────
        # Pre-seed every week with 0.0 so weeks with NO runs count as 0 (an
        # inconsistent runner must not pass by averaging only the weeks run).
        weekly_km: dict[str, float] = {}
        weekly_min: dict[str, float] = {}
        for i in range(MIN_CONSISTENT_WEEKS):
            wk = (window_start + _td(weeks=i)).isoformat()
            weekly_km[wk] = 0.0
            weekly_min[wk] = 0.0
        for r in run_rows:
            d = _date.fromisoformat(r["start_date_local"][:10])
            wk = (d - _td(days=d.weekday())).isoformat()
            if wk in weekly_km:  # ignore stragglers outside the seeded weeks
                weekly_km[wk] += (r["distance_m"] or 0) / 1000
                weekly_min[wk] += (r["moving_time_s"] or 0) / 60

        baseline_km = list(weekly_km.values())
        n_weeks = len(baseline_km)
        avg_weekly_km = sum(baseline_km) / n_weeks if n_weeks else 0.0
        consistent_weeks = sum(1 for v in baseline_km if v >= CONSISTENT_WEEK_FLOOR_KM)

        if target_weekly_km is None:
            target_weekly_km = round(avg_weekly_km * 1.05, 1) if avg_weekly_km > 0 else MIN_WEEKLY_KM

        # Current week so far.
        current_week_km = sum((r["distance_m"] or 0) for r in current_week_rows) / 1000
        current_week_min = sum((r["moving_time_s"] or 0) for r in current_week_rows) / 60
        remaining_km = max(0.0, target_weekly_km - current_week_km)

        # ── Objective precondition checks ─────────────────────────────────
        reasons_fail: list[str] = []
        preconditions: list[dict] = []

        # 1. Sustained weekly volume (~70+ km/week).
        volume_ok = avg_weekly_km >= MIN_WEEKLY_KM
        preconditions.append({
            "name": "sustained_weekly_volume",
            "requirement": f">= {MIN_WEEKLY_KM:.0f} km/week (ideally 100+)",
            "your_status": f"{avg_weekly_km:.1f} km/week over the last {n_weeks} full weeks",
            "met": volume_ok,
        })
        if not volume_ok:
            reasons_fail.append(
                f"Weekly volume too low ({avg_weekly_km:.1f} km/week) — "
                f"double-threshold needs sustained {MIN_WEEKLY_KM:.0f}+ km/week"
            )

        # 2. Consistency (>= 8-12 weeks of real volume).
        consistency_ok = consistent_weeks >= MIN_CONSISTENT_WEEKS
        preconditions.append({
            "name": "consistency",
            "requirement": f">= {MIN_CONSISTENT_WEEKS} consecutive weeks of consistent volume",
            "your_status": (
                f"{consistent_weeks} of the last {n_weeks} weeks had "
                f">= {CONSISTENT_WEEK_FLOOR_KM:.0f} km"
            ),
            "met": consistency_ok,
        })
        if not consistency_ok:
            reasons_fail.append(
                f"Not enough consistent weeks ({consistent_weeks}/{MIN_CONSISTENT_WEEKS}) — "
                f"need {MIN_CONSISTENT_WEEKS}-12 weeks of consistent singles first"
            )

        # 3. Recovery / readiness today (wellness).
        body_battery = wellness_row["body_battery_at_wake"] if wellness_row else None
        hrv_status_raw = wellness_row["hrv_status"] if wellness_row else None
        hrv_status = (hrv_status_raw or "").lower()
        recovery_hours = wellness_row["recovery_time_hours"] if wellness_row else None
        battery_ok = body_battery is not None and body_battery >= 70
        hrv_ok = hrv_status not in ("poor", "low", "unbalanced")
        recovery_ok = recovery_hours is None or recovery_hours <= 36
        wellness_ok = battery_ok and hrv_ok and recovery_ok
        preconditions.append({
            "name": "recovery_today",
            "requirement": "body battery >= 70, HRV not poor/unbalanced, recovery time <= 36 h",
            "your_status": (
                f"body battery {body_battery}, HRV '{hrv_status_raw}', "
                f"recovery {recovery_hours}h"
            ),
            "met": wellness_ok,
        })
        if not battery_ok:
            if body_battery is None:
                reasons_fail.append("Body battery at wake not available (sync wellness data)")
            else:
                reasons_fail.append(f"Body battery at wake too low ({body_battery}) — need >= 70")
        if not hrv_ok:
            reasons_fail.append(
                f"HRV status is '{hrv_status_raw}' — double day inadvisable when HRV is suppressed"
            )
        if not recovery_ok:
            reasons_fail.append(
                f"Recovery time elevated ({recovery_hours}h) — likely post-race/hard effort. "
                "Skip a hard double until recovered"
            )

        # 4. No race / hard quality session scheduled in the next 3 days
        #    (checked against plan.json, not inferred from recovery time).
        upcoming_conflict: Optional[dict] = None
        try:
            plan = plan_mod.load_plan()
        except Exception:
            plan = None
        if plan and isinstance(plan.get("workouts"), list):
            horizon = today + _td(days=3)
            HARD_TYPES = {"race", "threshold", "vo2", "interval"}
            for w in plan["workouts"]:
                w_date_s = w.get("date")
                if not w_date_s:
                    continue
                try:
                    w_date = _date.fromisoformat(w_date_s[:10])
                except ValueError:
                    continue
                if today <= w_date <= horizon and (w.get("type") or "").lower() in HARD_TYPES:
                    upcoming_conflict = {
                        "date": w_date.isoformat(),
                        "type": w.get("type"),
                        "name": w.get("name"),
                    }
                    break
        calendar_ok = upcoming_conflict is None
        preconditions.append({
            "name": "calendar_clear",
            "requirement": "no race or hard quality session scheduled in the next 3 days",
            "your_status": (
                "clear" if calendar_ok
                else f"{upcoming_conflict['type']} on {upcoming_conflict['date']} "
                     f"({upcoming_conflict['name']})"
            ),
            "met": calendar_ok,
        })
        if not calendar_ok:
            reasons_fail.append(
                f"Hard session scheduled within 3 days "
                f"({upcoming_conflict['type']} on {upcoming_conflict['date']}) — "
                "don't stack a double day right before it"
            )

        objective_ok = len(reasons_fail) == 0

        # ── Consent gate: never eligible without explicit user opt-in ─────
        weekly_context = {
            "current_week_km": round(current_week_km, 1),
            "current_week_min": round(current_week_min, 0),
            "target_weekly_km": target_weekly_km,
            "remaining_km": round(remaining_km, 1),
            "avg_weekly_km": round(avg_weekly_km, 1),
            "weeks_assessed": n_weeks,
            "consistent_weeks": consistent_weeks,
            "body_battery_at_wake": body_battery,
            "hrv_status": hrv_status_raw,
            "recovery_time_hours": recovery_hours,
        }
        base_caution = (
            "Double-threshold days are the advanced variant — at most 2 per "
            "week with full easy days between, and the default remains "
            "Norwegian Singles. Both sessions stay sub-threshold (Golden Zone); "
            "ramp in gradually (start with easy+threshold on the same day, run "
            "the first true doubles 10-15 sec/km slower than normal)."
        )

        # Eligible if EITHER the objective base is in place OR the user has
        # explicitly opted in (informed consent). Not meeting the base is not
        # a hard block — the user can override; we just explain the risk. Only
        # block when neither holds: base not met AND no explicit opt-in.
        eligible = objective_ok or user_confirms_ready
        override = user_confirms_ready and not objective_ok

        if not eligible:
            # Base not met and the user hasn't opted in — explain and offer
            # the informed-consent path rather than refusing outright.
            return {
                "eligible": False,
                "awaiting_user_confirmation": True,
                "reason": (
                    "You don't currently meet the base for double-threshold days ("
                    + " | ".join(reasons_fail) + "). That's not a hard block — if you "
                    "still want to try them it's your call. Review the preconditions "
                    "and your standing below; to proceed anyway, re-run with "
                    "user_confirms_ready=True (informed-consent override)."
                ),
                "preconditions": preconditions,
                "objective_checks_pass": objective_ok,
                "failed_preconditions": reasons_fail,
                "confirmation_prompt": (
                    "You don't yet meet the usual base (70+ km/week, 8-12 consistent "
                    "weeks). Double-threshold days raise injury/overreach risk without "
                    "it. Do you still want to proceed? It's your choice — confirm only "
                    "if you understand the trade-off."
                ),
                "suggested_structure": None,
                "weekly_context": weekly_context,
                "caution": base_caution,
            }

        suggested_structure = {
            "am_session": {
                "type": "sub-threshold",
                "description": (
                    "Long reps in the Golden Zone — e.g. 5×6 min or 4×8 min at "
                    "sub-threshold HR (2.3-3.0 mmol / 80-87% max HR), short jog "
                    "rests. Controlled and sustainable, NOT at-threshold."
                ),
            },
            "pm_session": {
                "type": "sub-threshold",
                "description": (
                    "Short reps in the same Golden Zone — e.g. 10×1k or 45/15 "
                    "for 20-30 min. Same HR target as AM; pace is faster only "
                    "because rests are shorter. Still sub-threshold."
                ),
                "rest_between_sessions_h": "6-8",
            },
            "timing_note": (
                "Space the two sessions 6-8 h apart so muscle tone recovers and "
                "the PM reps land on relatively fresh legs. AM in the morning, "
                "PM by early evening to protect sleep."
            ),
            "rationale": (
                "Both sessions are sub-threshold (Golden Zone) — the goal is to "
                "compound recoverable threshold volume across the day, not to "
                "train hard twice. Partial glycogen depletion by the PM session "
                "is a side effect to MANAGE (fuel between sessions, keep both in "
                "the band), not the adaptive mechanism."
            ),
        }

        if override:
            reason = (
                "Preconditions NOT met (" + " | ".join(reasons_fail) + "), but you've "
                "explicitly opted in — proceeding is your informed choice."
            )
            caution = (
                "⚠ INFORMED-CONSENT OVERRIDE: you do not meet the base ("
                + " | ".join(reasons_fail) + "), so injury/overreach risk is higher. "
                + base_caution + " Start with the most conservative ramp (easy+threshold "
                "on the same day before any true double; first doubles 10-15 sec/km "
                "slower), cap at 1/week, and stop at the first sign recovery markers slip."
            )
        elif user_confirms_ready:
            reason = (
                "User confirmed and all preconditions met — double-threshold day is "
                "appropriate. Keep both sessions sub-threshold."
            )
            caution = base_caution + (
                " Skip or convert the PM session to easy if fatigue accumulates "
                "during the AM session."
            )
        else:
            # Objective base met; user hasn't explicitly opted in this call.
            reason = (
                "All preconditions met — double-threshold days are appropriate when "
                "you want them. Both sessions stay sub-threshold."
            )
            caution = base_caution + (
                " Skip or convert the PM session to easy if fatigue accumulates "
                "during the AM session."
            )

        return {
            "eligible": True,
            "awaiting_user_confirmation": False,
            "override": override,
            "reason": reason,
            "preconditions": preconditions,
            "objective_checks_pass": objective_ok,
            "failed_preconditions": reasons_fail,
            "suggested_structure": suggested_structure,
            "weekly_context": weekly_context,
            "caution": caution,
        }

    except Exception as exc:
        return {
            "eligible": False,
            "awaiting_user_confirmation": False,
            "reason": f"Error assessing eligibility: {type(exc).__name__}: {exc}",
            "preconditions": [],
            "suggested_structure": None,
            "weekly_context": {},
            "caution": (
                "Double-threshold days are the gatekept advanced variant; the "
                "default is Norwegian Singles. Stay on Singles unless a deep base "
                "is in place and the user has explicitly opted in."
            ),
        }

