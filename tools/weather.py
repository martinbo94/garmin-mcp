"""Weather at past sessions vs how they went (Garmin per-activity weather)."""
from typing import Optional

import garmin_sync
from core import mcp


@mcp.tool()
def weather_performance(
    weeks_back: int = 52,
    classification: Optional[str] = None,
    sport_type: str = "Run",
    include_sessions: bool = False,
) -> dict:
    """How conditions at the start of a session line up with how it went:
    performance condition (first / last / change), Garmin post-run feel
    and RPE, grouped by temperature bin and wet vs dry, plus Spearman
    correlations. Answers "do I run better when it's colder / wetter?".

    Weather is Open-Meteo ERA5 reanalysis at the activity's own GPS start
    and start hour (temperature, dew point, humidity, wind, precipitation
    during the run and the six hours before) — cached on sync; run
    sync_activities(backfill_weather=True) first if `unchecked_activities`
    is large. Garmin's own station record is NOT used for values: the
    nearest station can be far from the route and is often wrong.

    DESCRIPTIVE ONLY. Fixed 5-degree bins and rank correlations; it fits
    no thresholds and derives no rules. Before quoting a temperature
    effect, read `monthly` (temperature and fitness both follow the
    calendar) and `by_classification` (intervals sag in performance
    condition by design, easy runs hold), and repeat the caveats to the
    user. A correlation of |rho| < 0.2 on n < 30 is noise — say so.

    Args:
        weeks_back: window (default 52).
        classification: restrict to one name/plan classification
            (easy, sub-threshold, threshold, vo2, long, race, ...).
        sport_type: default 'Run'.
        include_sessions: also return one compact row per session (default
            False — the aggregates answer the question; the rows are large).

    Returns n_sessions, by_temperature (n + mean outcomes per bin),
    by_wet_dry, correlations[input][outcome] = {spearman, n},
    by_classification, monthly (temp + perf_cond_first + feel per month),
    and caveats.
    """
    return garmin_sync.weather_performance(
        weeks_back=weeks_back, classification=classification,
        sport_type=sport_type, include_sessions=include_sessions,
    )
