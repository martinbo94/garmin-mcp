# Plan Design — Block Structure and Progression

The reference for designing a multi-week training block (typically 8-16
weeks). Read this when drafting or revising `plan.json`. Sourced mainly
from Marius Bakken's *Løping!* chapter 7 ("Mosjonisten som vil mer"),
which is the chapter aimed at runners on 4-6 hours of weekly volume.

This doc is **plan-design layer** — separate from:
- `coach://training_philosophy` — the framework (why sub-threshold,
  what's a session, recovery cues)
- `coach://user_profile` — the athlete's numbers (max HR, zones, paces, PRs)
- `coach://classification` — how to interpret completed activities

## Choosing the block structure (Bakken figur 7.2)

Three structural archetypes:

### Flat structure
- Same load every week, no scheduled variation.
- Predictable, low cognitive overhead, hard to overcook.
- Best for: returning to training after a break, injury-prone runners,
  or when stability is the priority. Suitable for whole-year background
  training between race blocks.
- Risk: stagnation without progression. Works less well for
  goal-oriented build cycles.

### Block periodization (3-4 weeks build + 1 week deload)
- The "trappetrinn" / staircase model. Standard race-prep structure.
- 3-4 weeks of progressive load (more volume, longer reps, or harder
  X-økt), then a deload week (~70-80% of normal volume, intensity
  preserved or also down).
- Most dynamic. Suits runners who respond well to varied stimulus.
- Risk: too much variation can mask the actual fitness signal week to
  week; harder to track whether you're progressing.

### Progressive X-økt
- Same weekly structure throughout, but the **X-økt slot progresses
  systematically** each week (intensity / volume / specificity).
- Intermediate option: stability of flat with progression of blocks.
- Good when the other 4 sessions in the week are stable and you want a
  single dial to turn.

**How to choose:** Match the structure to the goal and the user's
history. Race-focused 12-week blocks → block periodization. Year-round
base with no specific race → flat. First Bakken-style block where you
want stability + a sense of progression → progressive X-økt.

## Periodization horizon

| Horizon | When |
|---|---|
| Short (2-3 week blocks) | Experienced runners who respond to frequent variation; not for first Bakken block |
| Medium (4-6 week blocks) | Standard goal-oriented training; the default |
| Long (multi-month macroblocks) | Specific race goals (HM, marathon) where weeks 1-4 / 5-8 / 9-12 each have a distinct character |

## Adjusted intensity distribution (4-6 hours/week)

The conventional 80/20 split is too easy-heavy at this volume. Bakken's
chapter 7 prescription:

| Intensity | % of weekly run time | At ~5 h/week |
|---|---|---|
| Z1-Z2 (easy / aerobic base) | 60-65% | ~3-3:15 h |
| Z3-Z4 (Golden Zone) | 20-30% | ~1:00-1:30 h |
| Z5 (high intensity) | 5-10% | ~20-30 min |

These are the target bands when planning a week and when reviewing
compliance after.

## Reference 5-hour week (Bakken's chapter 7 layout)

| Day | Session | Notes |
|---|---|---|
| Mon | **Rest** | Full recovery from weekend |
| Tue | **Threshold "støtte"** | 6×6 min, or 10-12×3 min with 1 min jog. Long-rep sub-threshold. |
| Wed | Easy 40-60 min | *"Så slakk at den nesten føles feil."* Hard cap at LT1. |
| Thu | **Threshold "hoved"** | 3-4×10 min, or another long-rep variation. The most balanced session of the week. |
| Fri | **Rest** or short easy | Strategic rest. Skip if 4 rest days/week feels excessive. |
| Sat | **X-økt (flex slot)** | Varies — see below. NOT a fixed third threshold. |
| Sun | **Long easy** | 60-120 min, distance-specific to goal race. |

Two threshold + one flex + one long + two rest + one easy.

Strength training is a separate add-on, ideally Mon/Wed/Fri or other
non-threshold day.

## Pause conventions

Pause length is a stimulus lever, not a fixed property of a rep design.
Bakken's defaults:

- **15-30 sec** (45/15, 30/30 etc.) — standing or very slow jog both fine.
- **45-90 sec** — slow jog. Default for 200 m – 1 km repeats and 4-6 min
  threshold reps.
- **2-3 min** — walk or very easy jog ("lett gå pause"). For longer reps
  (8-10 min) or when reps are at the harder end of the band.
- **3-5 min between BLOCKS** of intervals — walking + breathing reset.

Shorter pauses at the same rep design = more sustained accumulation
(tempo-like). Longer pauses = more "drypp" effect (sharper individual
reps, less cumulative fatigue). Use shorter when building accumulation
tolerance; longer when you want to push pace within the rep without HR
runaway.

## The X-økt — rotating Saturday variation

The X-økt is the **single most important variation lever** in this
structure. Rotate the content across weeks to keep adaptation moving
without escalating the other days:

- **45/15 short-rep work** — 15-30 reps at sub-threshold pace
- **Hills** — 8-12 × 200-400 m hill repeats, controlled effort
- **Smart Strides** — 5-6 × 100 m strides slightly above threshold after
  a short main set
- **Race-specific work** — closer to race day. 6×1k at goal pace, mile
  reps, race-pace progressions
- **Long marathon-pace block** — for HM/marathon goals, 10-15 km steady
  at goal pace (one of the few cases where continuous tempo beats intervals)
- **Easy or skipped** — deload weeks, or when recovery is marginal

A pattern that works for a 12-week block: rotate through 45/15, hills,
race-specific, deload-easy → repeats.

## Four alternatives for increasing weekly load

When the current week's load feels too easy, options (in roughly
ascending complexity):

1. **Ren volumøkning** — increase easy mileage. Add 10-15 min to easy
   runs or extend long run. Cheapest progression, least disruptive.
2. **Lengre terskeløkter** — extend threshold session length. Progression:
   4×6 → 5×6 → 6×6 → 4×8 → 5×8 → 6×8. Same HR target, more total
   work-time per session.
3. **Spesialvarianter (X-økt)** — make the X-økt harder (longer 45/15,
   race-specific pace work, hill volume).
4. **Forsiktig dobbel terskel** — light double-threshold day. ONLY if
   the gatekeeping criteria in `coach://training_philosophy` are met.
   Not default.

Mix-and-match across weeks; don't escalate all four at once.

## Race-prep 12-week template (generic)

For a goal race at week 12, the generic 4-phase shape (any distance):

| Weeks | Phase | Focus |
|---|---|---|
| 1-4 | Base / grunntrening | Volume build, threshold rhythm established, no race-specific work yet. X-økt rotates 45/15, easy hills, strides. |
| 5-8 | Intensity / spesifikk forberedelse | Longer threshold reps (6×6 → 5×8), introduce race-pace work in X-økt. Volume stabilizes. |
| 9-11 | Race-specific / konkurransesperiode | X-økt becomes race-pace specific. Threshold sessions shorter but sharper. Volume starts coming down in week 11. |
| 12 | Taper | Volume down 25-40%, threshold intensity preserved (1-2 short sharp sessions). Race day at end. |

## Distance-specific X-økt menu (chapter 8)

The Saturday X-økt slot adapts to goal distance. Bakken's level-8
prescriptions (scale rep counts down for amateur volume):

| Goal distance | Primary X-økt content | Notes |
|---|---|---|
| **5 km** | Hill repeats (8-10 × 60-75 sec) or 30/30 blocks | Speed/strength bias; the fast end matters |
| **10 km** | Threshold variation (2-3 × progressive 10 min) + 6-8 × 30/30 | Mixed: sustained threshold + short overspeed |
| **Half marathon** | 3 × 12-15 min sub-threshold + 120 min long easy | Endurance-biased threshold accumulation |
| **Marathon** | "Hovedøkter": 4 × 10 min sub-threshold + 120 min long easy | Long sub-threshold work, no overspeed |

The pattern: shorter races bias toward overspeed and hills; longer
races bias toward longer sub-threshold reps and aerobic capacity. The
sub-threshold Tue/Thu sessions stay constant — the X-økt is what
distance-tunes the block.

## 5 km race-prep X-økt progression (7-week build)

Bakken's specific Saturday progression for the 7 weeks leading into a
5 km race. Scale rep counts down for amateur volume; intensities and
intervals as written:

| Weeks before race | X-økt |
|---|---|
| 7 weeks out | 8-12 × 200-300 m @ 3-5 km pace, 45-60 sec rest |
| 6 weeks out | 6-10 × 400 m @ 3-5 km pace, ~90 sec rest |
| 5 weeks out | 4-6 × 800 m @ 3-5 km pace, 2-3 min rest |
| 2-4 weeks out | **4-8 × 1000 m @ 3-5 km pace, 2-3 min rest** (the keystone session) — optionally finish with 3-5 × 300 m controlled overspeed |
| 1 week out | Reduce: shorter intervals 200-300 m at race pace, 5-7 reps |
| Race week | Light shake-out + race day |

For 10 km, halve the weekly progression intensity and keep more
sustained-threshold structure (longer reps, fewer overspeed elements).
For HM and marathon, see the 5-phase macro below.

## Marathon-specific 5-phase macro (Bakken's *100 Day Marathon Plan*)

For marathons (and adaptable to HM with shorter phases). Macro structure
across ~14 weeks:

| Phase | Weeks | Focus |
|---|---|---|
| **1. 5/10 km focus** | 1-3 | Sharpen the fast end first. Short reps, hills, threshold. |
| **2. HM focus** | 4-7 | Shift toward sustained threshold (5×8 min, 3-4×10 min) + longer continuous tempo. Build long run. |
| **3. Marathon-specific** | 8-11 | 4-10 min sub-threshold reps, **long runs include marathon-pace blocks**, weekly long stretches toward 30+ km. |
| **4. Taper** | 12-13 | Volume drops 25-40%. Intensity preserved with short sharp sessions (e.g. 20 × 1 min). |
| **5. Recovery (post-race)** | After race | 2-3 weeks pure easy / no quality. Hard reset. |

Note: chapter 8 details this for high-volume runners (6-8+ h/week). At
amateur volume, compress the phases and skip the late-cycle marathon-pace
long-run blocks until weekly km supports them (~70+ km/week).

## Tailoring to athlete physiology — train the bottleneck, not the strength

Bakken's chapters describe race-prep templates as if the athlete is
roughly balanced — speed, VO2max, threshold all reasonable for their
level. Real athletes are rarely balanced. Before applying a
race-specific template verbatim, identify what the limiter actually is
and bias the plan toward fixing that, not toward developing what's
already strong.

The signal-rich numbers (when available) are:

- **VO2max** — sets the aerobic ceiling
- **LT2 / threshold HR** — where lactate accumulates
- **Utilization rate** (VO2 at LT2 / VO2max) — what % of aerobic ceiling
  you can sustain
- **Race PRs** — actual performance at given distances
- **Subjective "fast vs durable"** — Type II (sprinter-leaning) vs Type I
  (diesel-engine) tendency

### Three common profiles and how to bias the plan

**Profile A: High VO2max, low utilization (VO2-strong, threshold-weak).**
Symptoms: natural speed, fast 5k for the volume trained, but feels like
"hits a wall" at sustained efforts, can't hold threshold pace long.
*Implication:* the bottleneck is utilization. Sub-threshold work is the
right lever; speed and overspeed are not. Even for short-distance prep
(5k / 10k), **bias X-økt toward sub-threshold variation** (45/15 at
sub-threshold, longer threshold reps) rather than hills, 200-300 m
repeats, or 30/30 at VO2max effort. Standard distance-specific X-økt
menu is wrong here.

**Profile B: Moderate VO2max, high utilization (well-rounded, "good for
their VO2").** Symptoms: strong threshold but feels relative slow at
short distances. *Implication:* VO2max work moves the ceiling and
indirectly helps everything. Standard race-prep templates apply. The
distance-specific X-økt menu in this doc is built for this profile.

**Profile C: Low VO2max, low utilization (early in training history or
returning).** *Implication:* both ends need work but threshold work is
still the engine. Stay close to the Norwegian Singles base for months
before adding any race-specific overspeed.

### Rule of thumb

If you don't know your profile, follow the standard template. If you do
know your profile (from lab tests, race performance patterns, training
history), bias the X-økt and supplemental work accordingly. The Tue/Thu
threshold backbone stays the same regardless — only the X-økt and
specific-prep elements change.

The athlete's profile and bias notes are in `coach://user_profile`.
Always check before applying race-prep templates verbatim.

## Top-up mileage weeks (volume-build option)

A pure volume progression for 6-8 weeks before a competition period when
you want to lift the weekly km baseline. Example from a 70 km/week
baseline:

| Week | Target volume | % of baseline |
|---|---|---|
| 1 | 77 km | +10% |
| 2 | 84 km | +20% |
| 3 | 91 km | +30% |
| 4 | 98 km | +40% |
| 5 | 105 km | +50% (peak) |
| 6 | 105-110 km | hold or slight increase |
| 7 | 70-75 km | back to baseline (recovery) |
| 8-10 | 70 km | normal load (consolidate) |
| 11+ | — | race season / specific prep |

Key rules:
- **Only easy mileage scales up.** Quality sessions stay at the same
  volume/intensity throughout this block.
- **The intensive sessions stay at the same level** — both rep length and
  recovery — both on threshold days and weekends.
- **Build over 4-6 weeks max**; longer adds injury risk faster than
  fitness.

Most amateurs at 40-60 km/week shouldn't do this — the base is too low
for the additional load to be productive. Worth considering when weekly
volume is already sustained at 60+ km and you want to break through.

## Periodization staircase (multi-block career arc)

Beyond a single 12-week block, Bakken describes a multi-block
progression for runners who stick with the method over months/years:

1. **First 3-4 months:** Flat structure with sub-threshold work.
   Normalize the musculature to the Golden Zone, build base.
2. **Next phase:** Introduce 45/15 and 5-7 min threshold progressions
   alongside the standard long-rep work.
3. **Then:** Systematic block periodization with full sub-threshold
   variation across rep designs.
4. **Eventually:** Add double-threshold experiments if criteria met.

This applies once you've done several singles-method blocks
successfully. For a first Bakken-style block, **stay in step 1**.

## When periodization fails

Bakken's personal anecdote (VM 2002): over-did threshold across a block,
arrived at race week tired with stale HR and lactate response. Recovery
took **5 full days of pure rolig running** (no quality) before signs
returned to normal.

Signals that a block has overshot:
- Resting HR elevated 5+ bpm above baseline for multiple days
- Pace at same HR slower than 2 weeks prior (despite no apparent reason)
- Persistent leg heaviness that doesn't resolve in a single rest day
- Lactate-equivalent feel: threshold session HR climbs unusually fast
- Sleep quality degraded

Recovery move: **drop all quality for 4-7 days**, keep only easy
mileage. This isn't a "lighter week" — it's a reset. Resume at a
slightly lower starting point than where the block was when it broke.

## Drafting the plan — practical checklist

### Step 0 — Ground the draft in actual data (do this FIRST)

Before proposing any structure, weekly volume, or starting point, load
the user's real recent training. Numbers self-reported in chat ("I run
about 40 km/wk") are anchoring guesses — verify against the cache.
Skipping this step is how plans get prescribed that don't match the
runner.

Mandatory pre-draft calls (in order):

1. **`sync_activities()`** — ensure the local cache is current. The
   cache holds the last ~12 weeks by default. For year-long trajectory
   analysis call `sync_activities(weeks_back=52)` to extend it.
2. **`weekly_summary(start, end)`** for the last 12 weeks — returns
   `{"weeks": [...], "coverage": {...}}`. Inspect `coverage.gap_warning`
   first: if True, the requested range is older than the cache holds —
   call `sync_activities(weeks_back=N)` and retry. Then look at:
   - Weekly total km (median, trend, max).
   - Longest run per week (this anchors where the long run should
     start).
   - Number of quality sessions per week (the runner's current Bakken
     compliance, if any).
   - Zone distribution (is the easy/threshold/VO2 mix already in the
     60-65 / 20-30 / 5-10 band, or skewed gray-zone?).
3. **`get_wellness_history(days=30)`** — HRV trend, RHR baseline, sleep
   consistency, training readiness pattern. A draft that ignores a
   declining HRV trend will pile load onto an under-recovering athlete.
4. **`read_coach_doc('user_profile')`** — current HR zones, paces, race
   PRs, athlete profile (A/B/C). The plan's quality-session HR bands
   come from here.
5. **`read_coach_doc('training_philosophy')`** — confirm framework
   constraints (easy cap at LT1, sub-threshold band, X-økt rules).

Only after these calls should you propose a structure. When you do,
**state the data the structure is anchored to** ("starting at 45 km
because that's the median of the last 8 weeks; long run starts at 14 km
because that's what you've already been doing on Sundays") — don't pull
starting numbers from the user's chat estimate alone.

Re-plan trigger: at each phase boundary (or every 4 weeks in a flat
block), re-run Step 0 before adjusting the next phase. Reality drifts;
plans get re-anchored.

### Step 1 — Structure

1. **Pick the structural archetype** (flat / block / progressive X-økt).
2. **Set the horizon** (weeks count, deload cadence if blocks).
3. **Define the X-økt rotation** across the block.
4. **Lay out the standard week** (Mon/Tue/Wed/Thu/Fri/Sat/Sun roles).
5. **Specify threshold session progression** week to week (rep count,
   rep length, total work-time).
6. **Account for the goal race date** — taper in final week, race-specific
   work in weeks before.
7. **Verify intensity distribution** roughly matches 60-65 / 20-30 / 5-10.
8. **Write the draft to `coach_data/plan.draft.json`** via the Write
   tool (multi-week plans serialize to 20-40 KB of JSON — inlining that
   into MCP tool calls is expensive and error-prone). Then run
   `summarize_plan()` + `validate_plan()` with no arguments — they read
   the draft file by default. Only call `save_plan()` once both pass.

When this doc gets out of date: revise as the user's training history
accumulates and patterns emerge that the chapter 7 template doesn't
cover.
