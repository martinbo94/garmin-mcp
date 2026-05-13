# User profile (template)

Copy this file to `coach_data/user_profile.md` and fill in your own
values. The agent reads it as the `coach://user_profile` resource.

The bpm targets that the framework references (sub-threshold band, easy
run cap, VO2 band) are all derived from the values you fill in below.

## Maximum heart rate

**XXX bpm**

If you don't know your max HR, the standard `220 − age` estimate is a
rough start but typically underestimates well-trained runners. A flat-out
5k or hill repeats will get you closer.

## VO2max / lactate test results (optional)

If you've had a treadmill lactate profile, record key results. The Bakken
sub-threshold band is most precisely derived from your lactate test
points (interpolating where 2.3 and 3.0 mmol fall on your HR curve).
Without a test, fall back to the 80-87% max HR rule.

| Metric | Value |
|---|---|
| VO2max | XX ml/min/kg |
| Weight | XX kg |
| Max HR (test) | XXX |
| Max lactate | X.X mmol |
| Utilization at LT2 | XX% |
| LT2 HR (classical 4 mmol) | XXX bpm |
| LT1 HR | XXX bpm, X.X mmol |
| Bakken sub-threshold zone (2.3 – 3.0 mmol) | ~XXX – XXX bpm |

### Test caveats

Note conditions that might bias the result (treadmill vs outdoor,
fatigue, sleep, recent training load). The HR thresholds are intrinsic
and reliable; pace numbers are more conditional.

## HR zone system

Copy bpm ranges from Garmin Connect (or equivalent) verbatim. Don't
recompute from a formula — let the device's own rounding stand.

| Zone | bpm range | Description |
|------|-----------|-------------|
| Z1 | XXX – XXX | Very easy / recovery |
| Z2 | XXX – XXX | Easy / aerobic base |
| Z3 | XXX – XXX | Moderate / tempo |
| Z4 | XXX – XXX | Threshold |
| Z5 | ≥ XXX | VO2max |

Ranges are inclusive integer intervals (HR at the upper bound belongs to
the lower zone).

### Updating

When max HR changes or zones recalibrate:
1. Open Garmin Connect → Settings → User Settings → Heart Rate Zones.
2. Copy the bpm ranges into the table above verbatim.
3. Update the max HR value at the top.

## Quality session HR targets (primary intensity signal)

Drive sessions from HR, not pace. These are derived from your test (or
estimated from max HR). See `coach://training_philosophy` for the rules
behind them.

### Easy / aerobic base
- **Average HR in Z1 / low-mid Z2.**
- **Hard cap: your LT1** (or ~84% of max HR if untested).
- HR drift on long runs is normal; brief excursions over LT1 for hills
  are fine. Routine drift above LT1 is gray-zone territory.

### Threshold reps (Bakken sub-threshold)

| Session type | Target HR | Notes |
|---|---|---|
| All sub-threshold work | **XXX – XXX bpm** (your band) | Same band for any rep length — long reps and short reps share the same HR target. |
| Hard cap | **XXX bpm** (3-bpm buffer below LT2) | Above this you're at-threshold, not sub-threshold. |
| VO2 / X element | **~92 – 96% of max HR** | 0-1× per 7-10 days, only when fresh. |

The sub-threshold band is typically meaningfully lower than conventional
"threshold work" guidance (which targets ~4 mmol / 91-93% max HR). See
`coach://training_philosophy` for why.

## Race PRs

Real-world fitness anchors. Add as you set them.

| Distance | Time | Pace | Notes |
|---|---|---|---|
| 5k | — | — | |
| 10k | — | — | |
| HM | — | — | |
| Marathon | — | — | |

## Pace ↔ HR mapping (optional, from lactate test)

If you have a treadmill lactate profile, record the data points. Used for
estimating distance when planning time-based workouts.

| Speed | Pace | HR | Lactate | Zone |
|---|---|---|---|---|
| X.X km/h | X:XX/km | XXX | X.X mmol | ZX |

Note that treadmill pace may underestimate outdoor pace at the same HR.
Cross-check against your actual outdoor runs as data accumulates.

## Distance estimation rules of thumb

For planning weekly mileage. Calibrate these once you've logged a few
weeks of real outdoor data.

- **Easy run:** ~6:00 – 7:00 /km outdoor, depending on fitness and terrain.
- **Threshold reps (sub-threshold band):** roughly 10-15 seconds per km
  slower than 5k pace, or 2-4% slower than threshold pace.
- **VO2 reps:** roughly 5k pace ± a few seconds, depending on rep length.
- **Long run:** all easy.

## Recent outdoor data points

Track real outdoor runs that anchor what your true bands look like in
field conditions. Useful when treadmill test data and outdoor reality
diverge.

| Date | Distance | Avg HR | Avg pace | Notes |
|---|---|---|---|---|
