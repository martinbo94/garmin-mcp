"""User profile: status, parsing, and init."""
from typing import Optional

import garmin_sync
from core import _USER_PROFILE_PATH, _client, mcp


# ─── First-time profile setup ──────────────────────────────────────────
_PROFILE_SETUP_QUESTIONS = [
    # Essential — asked in both Bakken and minimal mode.
    {
        "field": "max_hr",
        "required": True,
        "framework_only": False,
        "question": (
            "What's your max heart rate? If you've measured it (a maximum-effort 5k, "
            "hill repeats, or a lab test), use that value. '220 − age' is a rough "
            "estimate but typically underestimates well-trained athletes."
        ),
    },
    # zone_ceilings is auto-fetched from a recent Garmin activity in
    # init_user_profile — no need to ask the user for it.
    {
        "field": "race_prs",
        "required": False,
        "framework_only": False,
        "question": (
            "What are your current PRs for 5k, 10k, half marathon, marathon? "
            "Leave out any distance you haven't raced. Times like '23:08' or '1:45:30'."
        ),
    },
    # Framework-specific — only asked when user has chosen the Bakken method.
    {
        "field": "lt2_hr",
        "required": False,
        "framework_only": True,
        "question": (
            "Have you had a lactate / VO2max test? If yes, what was your LT2 HR "
            "(classical threshold, ~4 mmol)? The HR at the highest sustainable "
            "steady-state effort."
        ),
    },
    {
        "field": "lt1_hr",
        "required": False,
        "framework_only": True,
        "question": (
            "From the same test, what was your LT1 HR (aerobic threshold, ~2 mmol)? "
            "This becomes your hard cap on easy runs in the Bakken framework."
        ),
    },
    {
        "field": "vo2max",
        "required": False,
        "framework_only": True,
        "question": (
            "What's your VO2max (ml/min/kg) from the test? Useful for reasoning about "
            "whether VO2 work or threshold work is your bigger lever (Profile A vs B)."
        ),
    },
    {
        "field": "weight_kg",
        "required": False,
        "framework_only": True,
        "question": "Body weight in kg? Optional, for VO2max L/min context.",
    },
    {
        "field": "notes",
        "required": False,
        "framework_only": False,
        "question": (
            "Any context worth recording? Recent injuries, planned races, training "
            "history, current limitations, etc."
        ),
    },
]



@mcp.tool()
def user_profile_status() -> dict:
    """Check whether user_profile.md exists and is filled in.

    Returns existence flag, file path, whether the file still has placeholder
    values from the example template, AND structured question lists split by
    mode:
    - `essential_questions`: asked regardless of training framework.
    - `framework_questions`: only asked when the user has chosen the Bakken
      Norwegian threshold method (lactate test data, LT1/LT2, VO2max, etc.).

    Call this at the start of a fresh session or whenever you suspect the
    profile isn't set up.

    After collecting answers, call `init_user_profile()` with whatever the
    user provided.
    """
    essential = [q for q in _PROFILE_SETUP_QUESTIONS if not q["framework_only"]]
    framework = [q for q in _PROFILE_SETUP_QUESTIONS if q["framework_only"]]

    if not _USER_PROFILE_PATH.exists():
        return {
            "exists": False,
            "path": str(_USER_PROFILE_PATH),
            "essential_questions": essential,
            "framework_questions": framework,
            "next_step": (
                "Ask the user whether they want to use the Bakken Norwegian threshold "
                "framework or just track workouts and health. Then walk through "
                "essential_questions (both modes) and, if Bakken, also framework_questions. "
                "max_hr is the only required field. Call init_user_profile() with answers."
            ),
        }

    content = _USER_PROFILE_PATH.read_text(encoding="utf-8")
    placeholders = ["XXX bpm", "XX km/h", "X.X mmol", "XX ml/min/kg", "XX:XX"]
    found = [p for p in placeholders if p in content]

    result: dict = {
        "exists": True,
        "path": str(_USER_PROFILE_PATH),
        "size_bytes": len(content),
        "placeholders_found": found,
    }
    if found:
        result["essential_questions"] = essential
        result["framework_questions"] = framework
        result["next_step"] = (
            f"Profile exists but still has template placeholders: {found}. Walk through "
            "the questions to collect real values, then call init_user_profile(overwrite=True)."
        )
    else:
        result["next_step"] = "Profile looks filled in — no setup action needed."
    return result




def _split_markdown_sections(text: str) -> dict[str, str]:
    """Split a markdown doc by H2 headings into a {heading: body} dict."""
    out: dict[str, str] = {}
    current_heading: Optional[str] = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                out[current_heading] = "\n".join(buf)
            current_heading = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current_heading is not None:
        out[current_heading] = "\n".join(buf)
    return out




def _parse_athlete_profile() -> dict:
    """Parse coach_data/user_profile.md into a structured dict.

    Section-scoped: race PRs only parsed from the Race PRs section, pace
    estimates only from the Session pace estimates section, etc. Tolerant
    of missing fields — returns None for anything it can't extract, plus
    the raw markdown so the agent can fall back when needed.
    """
    import re as _re
    if not _USER_PROFILE_PATH.exists():
        return {
            "exists": False,
            "path": str(_USER_PROFILE_PATH),
            "next_step": "Run init_user_profile() to create the profile.",
        }

    text = _USER_PROFILE_PATH.read_text(encoding="utf-8")
    sections = _split_markdown_sections(text)

    def grep_section(section_key_substr: str, pattern: str, group: int = 1, cast=str):
        for key, body in sections.items():
            if section_key_substr.lower() in key.lower():
                m = _re.search(pattern, body, _re.IGNORECASE)
                if m:
                    try:
                        return cast(m.group(group))
                    except (ValueError, TypeError):
                        return None
        return None

    max_hr = grep_section("Max HR", r"\*\*(\d+)\s*bpm\*\*", cast=int)

    vo2_section = next((b for k, b in sections.items() if "VO2max" in k), "")
    vo2max = _re.search(r"VO2max\s*\|\s*\*?\*?(\d+(?:\.\d+)?)\s*ml/min/kg", vo2_section)
    weight = _re.search(r"Weight\s*\|\s*(\d+(?:\.\d+)?)\s*kg", vo2_section)
    lt2 = _re.search(r"\*\*LT2 HR\*\*[^|]*\|\s*\*?\*?(\d+)\s*bpm", vo2_section)
    lt1 = _re.search(r"\*\*LT1 HR\*\*[^|]*\|\s*\*?\*?(\d+)\s*bpm", vo2_section)
    util = _re.search(r"Utilization at LT2\s*\|\s*\*?\*?(\d+)%", vo2_section)

    # Sub-threshold training target band, e.g. "training target: 178-188".
    st = _re.search(
        r"sub-threshold[^|]*\|.*?training target:\s*(\d+)\s*-\s*(\d+)",
        vo2_section, _re.IGNORECASE,
    )
    if not st:
        st = _re.search(
            r"sub-threshold[^|]*\|\s*~?(\d+)\s*-\s*(\d+)\s*bpm",
            vo2_section, _re.IGNORECASE,
        )
    sub_threshold_band = (
        {"low": int(st.group(1)), "high": int(st.group(2))} if st else None
    )

    # Athlete profile A/B/C — looks like "**Profile A: ...**"
    profile_section = next((b for k, b in sections.items() if "Athlete profile" in k), "")
    profile_match = _re.search(r"\*\*Profile\s+([A-C])\s*:\s*([^*]+?)\*\*", profile_section)
    athlete_profile = (
        {
            "label": profile_match.group(1),
            "description": profile_match.group(2).strip().rstrip(",").strip(),
        }
        if profile_match
        else None
    )

    # HR zones — reuse the existing parser (reads the whole doc; zones table
    # is the only place its pattern matches).
    zones = []
    try:
        for low, high, name in garmin_sync._parse_zones():
            zones.append({"name": name, "low": low, "high": None if high >= 9999 else high})
    except Exception:
        pass

    # Race PRs — parse the Race PRs section, table rows only.
    race_prs = []
    race_section = next((b for k, b in sections.items() if "Race PRs" in k), "")
    for line in race_section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.replace("**", "").strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        dist, time_s, pace_s = cells[0], cells[1], cells[2]
        if not _re.match(r"^\d+\s*(?:k|km|HM|hm|Marathon|marathon)$", dist):
            continue
        if not _re.match(r"^\d+:\d+(?::\d+)?$", time_s):
            continue
        if not _re.match(r"^\d+:\d+/km$", pace_s):
            continue
        date_field = cells[3] if len(cells) > 3 else ""
        race_prs.append({
            "distance": dist,
            "time": time_s,
            "pace": pace_s,
            "date": date_field if date_field and date_field not in ("—", "-", "older", "") else None,
        })

    # Pace estimates — parse the Session pace estimates section.
    pace_estimates = {}
    pace_section = next(
        (b for k, b in sections.items() if "pace estimate" in k.lower()), ""
    )
    for line in pace_section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.replace("**", "").strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        effort, pace = cells[0], cells[1]
        if not _re.search(r"\d+:\d+", pace) or "km" not in pace.lower():
            continue
        if effort.lower() in {"effort", "outdoor pace"} or effort.startswith("-"):
            continue
        pace_estimates[effort] = pace

    def _f(m, idx=1, cast=float):
        if not m:
            return None
        try:
            return cast(m.group(idx))
        except (ValueError, TypeError):
            return None

    return {
        "exists": True,
        "max_hr_bpm": max_hr,
        "lt1_hr": _f(lt1, cast=int),
        "lt2_hr": _f(lt2, cast=int),
        "sub_threshold_band_bpm": sub_threshold_band,
        "vo2max_ml_min_kg": _f(vo2max),
        "weight_kg": _f(weight),
        "utilization_at_lt2_pct": _f(util, cast=int),
        "zones": zones,
        "athlete_profile": athlete_profile,
        "race_prs": race_prs,
        "pace_estimates": pace_estimates,
    }




@mcp.tool()
def get_athlete_profile() -> dict:
    """**Authoritative source for HR zones, paces, athlete profile, and race
    PRs.** Use this before any analytical task — race goal estimation,
    weekly review, session interpretation, plan drafting.

    Returns a structured dict parsed from `coach://user_profile`:
    - `max_hr_bpm`, `lt1_hr`, `lt2_hr` (lab-calibrated thresholds)
    - `zones`: [{name, low, high}] for Z1-Z5 (verbatim from Garmin Connect)
    - `sub_threshold_band_bpm`: Bakken Golden Zone training target
    - `vo2max_ml_min_kg`, `weight_kg`, `utilization_at_lt2_pct`
    - `athlete_profile`: {label: A/B/C, description} — drives race-goal
      bias (Profile A: bias conservative; Profile B: Riegel underestimates;
      Profile C: as-is)
    - `race_prs`: list of {distance, time, pace, date}
    - `pace_estimates`: {effort_name: pace_string} (easy, sub-threshold, etc.)

    NEVER substitute zones from third-party apps for these values — they
    may use different calibration methods or be based on a recent race HR
    rather than the user's true max. Always anchor zone
    reasoning to this tool's output.
    """
    return _parse_athlete_profile()




@mcp.tool()
def init_user_profile(
    max_hr: int,
    zone_ceilings: Optional[list[int]] = None,
    weight_kg: Optional[float] = None,
    lt1_hr: Optional[int] = None,
    lt2_hr: Optional[int] = None,
    vo2max: Optional[float] = None,
    race_prs: Optional[dict] = None,
    notes: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Generate and write coach_data/user_profile.md from structured params.

    Use this when setting up a new install. `max_hr` is the only required
    field — everything else is optional and the tool will compute sensible
    defaults using Olympiatoppen %-of-max-HR rules where needed. Derived
    HR target bands (sub-threshold, easy cap, VO2) are computed from
    whatever you provide (LT values preferred over % approximations).

    Args:
        max_hr: Estimated or measured max heart rate (bpm). Required.
        zone_ceilings: Four ints — Z1, Z2, Z3, Z4 ceilings (Z5 begins
            above Z4). If omitted, auto-fetched from the most recent
            Garmin activity with HR data (most accurate), falling back to
            72/82/87/92% of max_hr if no activities are cached yet.
        weight_kg: Body weight (optional, for context).
        lt1_hr: Aerobic threshold / LT1 HR from a lactate test (optional).
        lt2_hr: Classical threshold / LT2 HR (~4 mmol) from a test (optional).
        vo2max: VO2max in ml/min/kg (optional).
        race_prs: Dict like {"5k": "23:08", "10k": "52:00"} (optional).
        notes: Free-text notes for the bottom of the file (optional).
        overwrite: Refuse to clobber an existing user_profile.md unless True.

    Returns a one-line confirmation with the derived HR bands.
    """
    if _USER_PROFILE_PATH.exists() and not overwrite:
        return (
            f"user_profile.md already exists at {_USER_PROFILE_PATH}. "
            "Call with overwrite=True to replace it."
        )

    if zone_ceilings is None:
        # Try to read zone boundaries from a recent Garmin activity —
        # more accurate than %-of-max-HR defaults.
        try:
            import sqlite3 as _sqlite3
            from garmin_sync import DB_PATH as _DB_PATH
            with _sqlite3.connect(_DB_PATH) as _conn:
                _row = _conn.execute(
                    "SELECT id FROM activities WHERE sport_type='Run' "
                    "AND avg_hr IS NOT NULL ORDER BY start_date_local DESC LIMIT 1"
                ).fetchone()
            if _row:
                _zones = _client().get_activity_hr_in_timezones(_row[0])
                if _zones and len(_zones) >= 5:
                    zone_ceilings = [_zones[i]["zoneLowBoundary"] - 1 for i in range(1, 5)]
        except Exception:
            pass

    if zone_ceilings is None:
        zone_ceilings = [
            round(max_hr * 0.72),
            round(max_hr * 0.82),
            round(max_hr * 0.87),
            round(max_hr * 0.92),
        ]
    if len(zone_ceilings) != 4:
        raise ValueError("zone_ceilings must be exactly 4 ints (Z1, Z2, Z3, Z4 ceilings).")
    z1c, z2c, z3c, z4c = zone_ceilings
    z1_floor = round(max_hr * 0.55)

    # Derived bands
    easy_cap = lt1_hr or round(max_hr * 0.84)
    sub_thresh_floor = round(max_hr * 0.80)
    sub_thresh_cap = (lt2_hr - 3) if lt2_hr else round(max_hr * 0.87)
    hard_cap = (lt2_hr - 1) if lt2_hr else round(max_hr * 0.89)
    vo2_low = round(max_hr * 0.92)
    vo2_high = round(max_hr * 0.96)

    parts: list[str] = [
        "# User profile",
        "",
        "Current physiological reference values, zones, race PRs.",
        "Generated via `init_user_profile`. Edit freely to refine.",
        "",
        "## Maximum heart rate",
        "",
        f"**{max_hr} bpm**",
        "",
    ]
    if weight_kg:
        parts += ["## Weight", "", f"**{weight_kg} kg**", ""]

    if any(v is not None for v in (vo2max, lt1_hr, lt2_hr)):
        parts += ["## Lactate / VO2max test", "", "| Metric | Value |", "|---|---|"]
        if vo2max is not None:
            parts.append(f"| VO2max | {vo2max} ml/min/kg |")
        if lt2_hr is not None:
            parts.append(f"| LT2 HR (classical 4 mmol) | {lt2_hr} bpm |")
        if lt1_hr is not None:
            parts.append(f"| LT1 HR (aerobic threshold) | {lt1_hr} bpm |")
        parts.append("")

    parts += [
        "## HR zone system: Olympiatoppen 5-zone",
        "",
        "| Zone | bpm range | Description |",
        "|------|-----------|-------------|",
        f"| Z1 | {z1_floor} – {z1c} | Very easy / recovery |",
        f"| Z2 | {z1c + 1} – {z2c} | Easy / aerobic base |",
        f"| Z3 | {z2c + 1} – {z3c} | Moderate / tempo |",
        f"| Z4 | {z3c + 1} – {z4c} | Threshold |",
        f"| Z5 | ≥ {z4c + 1} | VO2max |",
        "",
        "Ranges are inclusive integer intervals.",
        "",
        "## Quality session HR targets",
        "",
        "### Easy / aerobic base",
        "- Aim for average HR in Z1 / low-mid Z2.",
        f"- **Hard cap: {easy_cap} bpm** "
        f"({'LT1 from test' if lt1_hr else '~84% max HR estimate'}).",
        "",
        "### Threshold reps (Bakken sub-threshold)",
        "",
        "| Session type | Target HR | Notes |",
        "|---|---|---|",
        f"| All sub-threshold work | **{sub_thresh_floor} – {sub_thresh_cap} bpm** | Same band for any rep length. |",
        f"| Hard cap | **{hard_cap} bpm** | Above this you're at-threshold. |",
        f"| VO2 / X element | **{vo2_low} – {vo2_high} bpm** | 0-1× per 7-10 days. |",
        "",
        "See `coach://training_philosophy` for the framework discussion.",
        "",
        "## Race PRs",
        "",
        "| Distance | Time |",
        "|---|---|",
    ]
    prs = race_prs or {}
    for dist in ["5k", "10k", "HM", "Marathon"]:
        parts.append(f"| {dist} | {prs.get(dist, '—')} |")
    parts.append("")

    if notes:
        parts += ["## Notes", "", notes, ""]

    _USER_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _USER_PROFILE_PATH.write_text("\n".join(parts), encoding="utf-8")

    return (
        f"Wrote {_USER_PROFILE_PATH}. "
        f"Sub-threshold band: {sub_thresh_floor}-{sub_thresh_cap} bpm. "
        f"Hard cap: {hard_cap}. Easy cap: {easy_cap}. VO2 band: {vo2_low}-{vo2_high}."
    )


