# User profile (template)

Copy this file to `coach_data/user_profile.md` and fill in your own
values. The agent reads it as the `coach://user_profile` resource.

The bpm targets that the framework references (sub-threshold band, easy
run cap, VO2 band) are all derived from the values you fill in below.

## Max HR

**XXX bpm**

If you don't know your max HR, `220 − age` is a rough start but typically
underestimates well-trained runners. A flat-out 5k or hill repeats will
get you closer.

## VO2max test (optional)

If you've had a treadmill lactate profile, record key results. The Bakken
sub-threshold band is most precisely derived from a lactate test
(interpolating where 2.3 and 3.0 mmol fall on your HR curve). Without a
test, fall back to the 80-87% max HR rule.

| Metric | Value |
|---|---|
| VO2max | XX ml/min/kg |
| Weight | XX kg |
| Max lactate | X.X mmol |
| **LT2 HR** (~4 mmol) | **XXX bpm** |
| **LT1 HR** (~2.3 mmol) | **XXX bpm** |
| Utilization at LT2 | XX% |
| Bakken sub-threshold zone (2.3-3.0 mmol) | ~XXX-XXX bpm |

**Test day caveat:** note conditions that might bias the result
(treadmill vs outdoor, fatigue, sleep, recent training load). HR
thresholds are intrinsic and reliable; pace numbers are more conditional.

## HR zones (from Garmin Connect)

Copy bpm ranges verbatim. Don't recompute from a formula — let the
device's own rounding stand.

| Zone | bpm | Description |
|---|---|---|
| Z1 | XXX–XXX | Very easy / recovery |
| Z2 | XXX–XXX | Easy / aerobic base |
| Z3 | XXX–XXX | Moderate / tempo |
| Z4 | XXX–XXX | Threshold |
| Z5 | ≥ XXX | VO2max |

To update: Garmin Connect → Settings → User Settings → Heart Rate Zones,
copy verbatim.

## Quality session HR targets

Drive sessions from HR, not pace.

| Session type | Target HR | Notes |
|---|---|---|
| Easy / aerobic base | avg in Z1 / low-mid Z2 | **Hard cap: LT1.** Drifting above LT1 = gray zone. |
| **Sub-threshold** (any rep length) | **XXX-XXX bpm** | Bakken Golden Zone. Same band whether long reps or short reps — short reps just allow faster *pace* at same HR. |
| Hard cap above sub-threshold | XXX bpm | 3-bpm buffer below LT2 for HR lag. |
| VO2 / X-element | ~92-96% max HR | 0-1× per 7-10 days, fresh days only. |

## Race PRs

Real-world fitness anchors. Add as you set them.

| Distance | Time | Pace | Date | Notes |
|---|---|---|---|---|
| 5k | — | — | — | |
| 10k | — | — | — | |
| HM | — | — | — | |
| Marathon | — | — | — | |

## Athlete profile

State your current limiter as a revisable observation, not a fixed type
(see `coach://plan_design` → "Reading the limiter"). The common reads:

- **Limiter looks like utilization/endurance** — high VO2max relative to
  race times, low measured utilization at LT2 (< ~80%). Bias the X-økt
  toward sub-threshold variation, not VO2. (Race time alone can't tell
  utilization from economy — lean on a lab utilization number.)
- **Limiter looks like the VO2 ceiling** — already using most of your
  VO2max at threshold (utilization > ~85%). VO2 work matters more.
- **No clear limiter** — standard mix.

## Session pace estimates (conservative, training-day)

Concrete pace targets per effort type. Used to estimate distance for
time-based workouts so weekly mileage stays accurate.

| Effort | Outdoor pace | Used for |
|---|---|---|
| Easy / WU / CD / Long | X:XX/km | All Z1-Z2 work |
| **Sub-threshold** | **X:XX-X:XX/km** | Bakken threshold reps (cornerstone) |
| At-threshold (rare) | X:XX/km | ~1 hr sustainable. Outside Bakken default. |
| VO2 / X-element | X:XX/km | Around 5k race pace. Fresh days only. |

Bands assume normal training conditions. Race-day pace runs faster.

If untested, rough rules of thumb:
- **Sub-threshold:** ~10-15 sec/km slower than 5k pace.
- **VO2 reps:** ~5k pace.
- **Easy:** conversational, well below sub-threshold.

## Pace ↔ HR mapping (treadmill test, low confidence)

If you have a treadmill lactate profile, record the data points. Note
that **outdoor paces at the same HR are typically faster** than
treadmill — cross-check against real outdoor runs as data accumulates.

| Speed | Pace | HR | Lactate |
|---|---|---|---|
| X.X km/h | X:XX/km | XXX | X.X mmol |

### Recent outdoor data points

Real outdoor runs that anchor what your true bands look like in field
conditions. Useful when treadmill test data and outdoor reality diverge.

| Date | Distance | Avg HR | Pace | Notes |
|---|---|---|---|---|
