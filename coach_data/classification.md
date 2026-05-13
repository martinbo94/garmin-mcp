# Workout Classification Rules

How to read a Strava activity and decide what kind of session it was. The
workout **name** is the primary signal — HR, pace, and lap structure are
fallback hints when the name is ambiguous.

## Naming conventions (source of truth)

The agent assumes Strava activities are named according to a personal
convention: default Strava titles for easy runs, intentional custom names
for quality sessions. The exact wording below is one user's setup (in
Norwegian) — **replace the labels with whatever convention you use** in
your own copy of this file.

Example convention:

| Category | Naming pattern |
|---|---|
| Easy run | Default Strava names: "Morning Run", "Afternoon Run", "Lunch Run", "Evening Run" (often with 🏃‍♂️ / 🏃 emoji). Not renamed. |
| Long run | "Langtur" / "Long Run". Aerobic base — **NOT quality**. |
| Progressive long run | "Progressiv langtur". **Counts as quality.** |
| Threshold intervals | "Terskelintervaller" / "Terskel" / "Subterskel". **Cornerstone of the plan — Bakken method.** |
| Tempo | "Tempo" or "Xkm tempo" (e.g. "4km tempo"). Sustained sub-threshold effort with no rests. **Counts as threshold work.** |
| VO2max intervals | "Intervaller" / "Intervals" / "VO2" / "VO2max" (without a "terskel" qualifier). |
| Strength | Strava activity type = WeightTraining, or name contains "styrke" / "weight training" / "strength". |
| Other | Hike, Walk, Ride, etc. Log but exclude from running volume. |

**If the name is default** ("Afternoon Run"), classify as **easy
regardless of distance**. The athlete labels intentionally — a quality
session would have been renamed.

The patterns above also match the `classification_hint` regexes in
`strava_sync.py:name_hint()`. If you change naming conventions, update
both this file and the regex patterns there.

## Quality vs. easy (Marius Bakken framework)

**Quality** = threshold intervals + tempo + VO2max intervals + progressive long runs.

**NOT quality** (despite being long or hard):
- Regular long runs are aerobic base.
- Easy runs of any distance.

Example: a week with 1 threshold + 1 progressive long run + 1 regular long run
= **2 quality sessions, not 3**.

## Threshold vs. VO2max

**Distinguished by intensity, not by rep length.** Both can use any duration.

The Marius Bakken method explicitly does short-rep work (e.g. 45/15 — 45s on,
15s off) at **threshold** intensity. What a generic coach would call a VO2max
session by rep length is threshold here because the *intensity* is controlled
(roughly sub-2.5 mmol lactate, "controlled hard" not all-out).

When the name alone is ambiguous:
- **Threshold:** avg HR in upper Z3 / lower Z4 across work portions. Feels
  "controlled, sustainable, conversational becomes hard."
- **VO2max:** avg HR pushes Z5. Feels "all-out, can't hold pace much longer."

**Trust the name when it's explicit.** If the name says "terskel"/"threshold"
even with short reps and high HR, classify as threshold — the user is
intentionally training that zone the Bakken way.

## Weekly summary format

When asked "how was last week" or similar, default to Monday-Sunday and
return:

- **Volume:** total run distance in km
- **Sessions:** total count of run activities
- **Quality count:** N (threshold + tempo + VO2max + progressive long runs)
- **Easy count:** N
- **Long run:** Y/N — if yes, name + distance, and note whether progressive
- **Strength count:** N (separate from running)
- **Time in HR zones:** show **each zone individually** (Z1, Z2, Z3, Z4, Z5)
  as a percentage of total weekly running time. Do NOT pre-group zones in
  the display — the Z1 vs Z2 split distinguishes recovery from aerobic base,
  and the Z3 vs Z4 split distinguishes tempo from threshold work. The band
  check (next section) is done by summing the displayed zones internally,
  not by collapsing them in the report.

### Heart rate zones — how to compute time in zone

The user uses the **Olympiatoppen 5-zone system** (% of max HR). Always read
`coach://user_profile` for the current max HR and bpm boundaries — do NOT use
whatever Strava has cached for zones (the user is on the free tier and can't
edit Strava's stored zones, so they may be stale).

To compute time in zone for an activity:
1. Pull HR stream via `get-activity-streams` with
   `types=["time","heartrate"]` and `resolution="low"` (≈100 points is plenty
   for aggregates and keeps payloads small for multi-activity summaries).
2. Apply the zone bpm boundaries from `coach://user_profile`.
3. For each consecutive sample pair (t[i], hr[i]) → (t[i+1], hr[i+1]), add
   (t[i+1] − t[i]) seconds to the zone hr[i] falls in.

## Target weekly zone distribution (rough guidance)

The canonical 80/20 polarized rule (80% easy / 20% hard) is too
conservative for an amateur on moderate weekly volume — there's not enough
total time for that little quality to deliver an aerobic stimulus. Working
targets, to refine once enough data has accumulated:

| Zone band | Target % of weekly run time |
|---|---|
| Z1 + Z2 | 60 – 80% |
| Z3 + Z4 | 20 – 35% |
| Z5 | 0 – 5% |

The displayed report keeps zones separate. To check the bands, sum Z1+Z2
and Z3+Z4 internally and compare against the targets — don't show the
collapsed numbers in the table.

When summarizing a week, compare actual zone totals against these bands and
flag noticeable deviation (in either direction):

- **Z1–Z2 over 80%:** plenty of base but light on quality — might be a
  deliberate recovery week, or might be missing a threshold session.
- **Z3–Z4 over 35%:** heavy quality load. Sustainable for a week or two,
  but if it persists multiple weeks check for accumulated fatigue.
- **Z5 over 5%:** drift toward over-hard intervals — reconsider whether
  the Bakken "controlled hard" intent is being honored.

These bands are placeholders. Update them here when the user refines.

## Ambiguity handling

If anything doesn't fit cleanly (generic name + interval-like lap structure,
unusual distance, missing HR data), **flag it** instead of guessing:

> 5/9 "Afternoon Run" 8.1 km — lap structure shows 4×1km with sharp pace
> drops between; possibly threshold but not labeled. Confirm?

## When this file gets out of date

The user is iterating on naming and on the training plan itself. If you
notice the rules don't fit what the data shows (e.g., a new naming pattern),
say so and ask whether to update this file rather than silently
re-classifying.
