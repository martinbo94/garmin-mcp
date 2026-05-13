# Training Philosophy

The framework the coach reasons within. Drawn from Marius Bakken's
Norwegian threshold method, adapted for amateur volume (the Norwegian
Singles variant — single sub-threshold sessions every other day rather
than the elite double-threshold day). This file is **strategic** — how
to think about training. It changes rarely, only when the methodology
itself shifts.

This doc is intentionally generic — no personal HR numbers, paces, or
test data live here. For your specific bpm bands, easy run cap, target
paces, and current calibrations, see **`coach://user_profile`**. For *where
you are right now* (goal race, block phase, weekly schedule), see your
`plan.json`.

---

## The core principle

> "Intensity must be precise enough to be tolerated and repeated. You do
> not improve by training as hard as possible. You improve by training
> hard enough, often enough, for long enough."
> — Bakken

The whole framework follows from this. Most coaching mistakes come from
crossing the line where training stops building you up and starts
breaking you down. The signature Bakken move is to **stay just under that
line, repeatedly**, week after week. Repeatability is the goal; any
session that costs too much undoes the next one.

This implies an inversion of conventional advice:
- Threshold work is the **engine**, not a sprinkle on top of easy mileage.
- Threshold intensity is **lower** than most people run it.
- Easy is **truly easy** — no productive middle ground between easy and
  threshold.
- VO2max work is **supplemental**, not central to distance development.

---

## "Threshold" vs "threshold work" — terminology trap

These get conflated all the time, so be explicit:

- **Threshold (LT2 / MLSS / OBLA) ≈ 4.0 mmol/L lactate.** Both
  conventional sports science and Bakken agree on this. It's a fixed
  physiological landmark — the highest steady-state effort before lactate
  accumulates uncontrollably.
- **"Threshold work" — the training session — means different things to
  different coaches.** This is where the confusion lives.
  - Conventional plans: "threshold work" = sessions *at* ~4 mmol (one
    hard session per week, costs a lot of recovery).
  - Bakken: "threshold work" = sessions *below* threshold, at ~2.3-3.0
    mmol (the "Golden Zone"). Lower per-session stimulus but much higher
    repeatable weekly volume.

Bakken doesn't redefine threshold — he just deliberately trains under it.
The trade is net-positive over weeks if you stay disciplined: at-threshold
once a week vs sub-threshold 2-3× a week is a much higher weekly stimulus,
with less recovery cost per session.

**We're training under Bakken's framework**, so when this doc or the
plan.json says "threshold session" or "terskelintervaller," that means
**sub-threshold at 2.3-3.0 mmol**, not at-threshold at 4 mmol.

### What this maps to for *this athlete*

Interpolating from the 2026-05-01 test lactate profile (see
`coach://user_profile` for the table):

| Lactate | km/h | HR | What it is |
|---|---|---|---|
| 2.2 mmol | 9.0 | 170 | just below Bakken's floor |
| **2.3 mmol** | ~9.1 | **~172** | sub-threshold floor |
| 2.6 mmol | 10.0 | 185 | comfortably sub-threshold |
| **3.0 mmol** | ~10.3 | **~188** | sub-threshold ceiling |
| 4.0 mmol | 11.0 | 193 | classical LT2 (lab number) |

### Deriving your sub-threshold band

Two rules, which should converge:

1. **From a lactate test** (most precise): interpolate where 2.3 and 3.0
   mmol fall on your HR curve. Those bpm values are your sub-threshold
   band's lower and upper bounds.
2. **Without a test** (approximation): **80 – 87% of max HR**.

Add a hard cap a few bpm below your classical LT2 (≈ 91-93% of max HR or
the 4 mmol point on a test) to leave buffer for HR lag. Above that cap
you've slipped into at-threshold territory.

Your specific bpm band lives in **`coach://user_profile`** under "Quality
session HR targets." Look it up before any threshold workout.

### Same HR target regardless of rep length

The whole point of Bakken's short reps (45/15, 30/15) is to hit a *higher
pace* at the *same sub-threshold HR/lactate*, not to push HR higher. The
short rests let you maintain lactate control while running faster. Rep
length is a pace lever, not an intensity lever.

| Session type | Target HR | What changes vs long reps |
|---|---|---|
| **Long reps** (5×6 min, 4×8 min, 5×1k) | sub-threshold band (see user_profile) | Default. 2× per week. |
| **Short reps** (45/15, 30/15, 400-1000m) | same sub-threshold band | Pace faster because of short rests. |
| **VO2 / X element** (1× per 7-10 days, when fresh) | ~92-96% of max HR | Separate stimulus, only when rested. |

**Subjective feel** for sub-threshold work: "controlled, sustainable, I
could keep doing this much longer than I am." Talk test = short
sentences, not gasping. If a 5×6 min session feels like the absolute max
you could do, you're at-threshold not sub-threshold — slow down.

If HR drifts above the sub-threshold band's upper bound on later reps:
cut pace, not the session. The target is repeatability across weeks, not
making each session as hard as possible.

The point is *repeatability*, not heroics. A session that wrecks the next
one is a net loss.

---

## Session formats

Bakken's three threshold session types, all done as **intervals with
recovery** (not continuous):

| Type | Rep length | Recovery | Lactate target | Notes |
|---|---|---|---|---|
| **Long reps (sustained)** | 6 – 10 min | 60 – 90 s | At or just below 3.0 | Most common; e.g. 5×6 min, 4×8 min, 5×1000 m |
| **Short reps (over/under)** | 45 s – 1 min | 15 – 30 s | At or slightly above threshold | 45/15, 30/15, etc. Higher turnover at same lactate band |
| **Float / progression** | 6 – 10 min | minimal | Build from 2.0 → 3.0 across the set | Slightly faster each rep |

Why intervals beat continuous: "Threshold speed will be higher doing
intervals and it is easier to have a progression of speed throughout the
session." The short rests let muscle tone reset, keeping lactate
controllable.

### Example sessions the user has used or might use

- 5 × 6 min @ LT2 bpm, 1 min jog rest — classic Bakken long-rep
- 4 × 8 min @ LT2 bpm, 90 s rest — slightly longer reps
- 10 × 1 km @ ~5:00-5:10/km (LT2 pace), 60-90 s jog — high-turnover
- 15 × 3 min @ LT2 bpm, 30-60 s rest — Bakken's example for "increase
  total dragtid" when threshold feels too easy
- 45/15 × 20-30 min — short on/off, controlled hard

**Progression rule** (from HK Lab + Bakken): when threshold feels too
easy, do NOT make it faster. Instead:
- Increase total threshold time (work up to 30-50 min cumulative)
- Lengthen individual reps (4 → 6 → 8 min)
- Shorten recovery (60s → 45s → 30s)

Faster pace at the same HR is what improvement *looks* like. It happens
on its own; don't chase it.

---

## VO2max work ("X element")

Bakken treats VO2max as one weekly "X element," not as a primary stimulus:

> "The mechanical benefit you get from running faster on sessions cannot
> be compared to the increase in performance you'll get from optimizing
> the anaerobic threshold."

Where VO2 work fits depends on what's actually limiting performance:

- **If raw VO2max is the limiter** (low VO2max for the volume you train),
  occasional VO2 sessions may matter more in the mix.
- **If utilization rate is the limiter** (you've got plenty of aerobic
  ceiling but can't sustain a high % of it at threshold), more threshold
  work closes that gap better than more VO2 work.
- **If both are reasonable already**, the question is what you've
  *responded* to historically — runners who've done a lot of interval
  work may have less to gain from more of it, vs. runners new to
  high-intensity who might still get fast gains from VO2.

See your test report (if you have one) or your race time vs aerobic
ceiling to judge which camp you're in. Default in this framework: keep
VO2 work modest unless there's a clear reason it's the bottleneck.

**X-element guidelines:**
- 0 – 1 session per week, on a rested day.
- HR target: roughly 92 – 96% of max HR (top of Z5). See
  `coach://user_profile` for your specific band.
- Formats: 4 × 4 min steady, 5 × 3 min, 4 × 6 min 30/15 in hills, or
  shorter (1 – 2 min) high-effort reps.
- Skip it in weeks where two threshold sessions already feel taxing —
  threshold takes priority.

---

## Easy runs

The discipline is non-negotiable: **easy is truly easy**.

> "I would entirely stay away from the zone in between very easy running
> and the threshold."  — Bakken

Bakken's elite athletes run easy below 70% of max HR — but that's
because elites have excellent running economy. At a given pace, an
amateur's HR is meaningfully higher. Trying to enforce 70% on an
amateur often means walking. Use a more realistic interpretation:

**Easy run targets (amateur-realistic):**

- **Aim for average HR in Z1 or low-mid Z2.** HR drift upward on a long
  run is normal — start low, end a bit higher, that's fine.
- **Hard cap: your LT1** (or roughly 84% of max HR if untested). Going
  above LT1 on an "easy" run means you're in aerobic-moderate territory
  — the "gray zone" Bakken warns about. Brief excursions for hills are
  fine; routine drift above LT1 is a problem.
- **Purpose:** aerobic base, running economy, recovery — *not* a moderate
  workout.

See `coach://user_profile` for your specific easy-cap bpm.

The signal isn't a strict number — it's that easy should feel easy the
next morning too. If you're not fully recovered by the next day, the run
was too hard regardless of HR.

If a planned easy run climbs into mid-Z2 / Z3 territory without good
reason (hills, heat, fatigue), it's drifting toward the gray zone —
too hard to recover from, too easy to drive adaptation. The worst place
to live.

---

## Long runs

In this framework, long runs are aerobic base — **not quality**.

- Run them at easy pace (Z1 / low Z2, same discipline as a regular easy
  run).
- No progression, no surges, no marathon-pace finishes (in this block).
- Duration: typically 90 – 120 min, less if weekly volume is low.
- Frequency: weekly, but can be skipped during heavy threshold blocks.

The deliberate choice not to make long runs "progressive" or "marathon
pace" in this block keeps total quality stress concentrated in the actual
threshold sessions. Long runs build durability without spending the
recovery budget that threshold work needs.

---

## Weekly structure: Norwegian Singles adaptation

The full Bakken method runs **double-threshold days** (two threshold
sessions in one day, ~6-8 h apart) on Tuesdays and Thursdays/Saturdays.
That's elite practice — 4 threshold sessions per week, 150-220 km total.

For amateurs (the Norwegian Singles adaptation that emerged on forums),
the structure preserves the framework but reduces frequency:

- **2-3 threshold sessions per week**, single sessions (not doubles).
- Every other day = a quality day; alternate days = easy.
- Most other runs are truly easy.
- 1 long run weekly (easy pace).
- 0-1 X-element session weekly (VO2max).

### Sample week template (amateur, ~45-55 km)

| Day | Session | Notes |
|---|---|---|
| Mon | Easy 6-8 km | Z1-Z2, recovery from Sunday long |
| Tue | **Threshold** | 5×6 min or similar, 8-11 km total |
| Wed | Easy 6-8 km | Hard cap at LT1 |
| Thu | **Threshold** | Different format (e.g. 10×1k if Tue was 5×6) |
| Fri | Easy 5-6 km, or rest | Listen to body |
| Sat | Easy or X-element (VO2) | VO2 only if fresh; otherwise easy |
| Sun | **Long run** | Easy, 90-120 min |

Two single threshold sessions (Tue, Thu) is the standard. A third quality
session (Sat VO2 or extra threshold) only goes in on weeks where
recovery is clearly intact.

**Strength training** fits as a separate workout on a non-threshold day
(typically Mon or Wed/Fri), aimed at general durability rather than
running-specific power.

---

## Recovery and the "traffic light" check

Bakken used a traffic-light system to decide whether a planned hard
session should go ahead. Adapted for this setup, before each threshold
or VO2 day:

- **🟢 Green:** Low morning resting HR, low warm-up HR, lactate climbs
  easily at fast paces, legs feel responsive → run the session as
  planned, maybe push the upper end of the band.
- **🟡 Yellow:** Normal warm-up HR, normal feel → standard session.
- **🔴 Red:** Elevated resting/warm-up HR, can't drive lactate up despite
  effort, legs heavy → **scale back or skip**. Cut volume by 30-50%,
  lower target HR by 2-3 bpm, OR convert to easy. The next session
  matters more than this one.

Concrete signals to watch (via `morning_check_in` tool):
- **Training readiness** (Garmin) score → low score = consider yellow/red
- **HRV** trending below baseline → yellow/red
- **Resting HR** elevated by 5+ bpm above your typical → yellow/red
- **Sleep** under 6 hrs or poor quality → yellow at minimum
- **Body battery** low at wake → adjust

If two consecutive yellow/red days, skip the planned quality session;
the framework only works if you can repeat sessions.

---

## How to react to a missed or off session

- **Missed entirely** (skipped because of recovery / life): don't try to
  cram it in later in the week. Roll on. Compliance is about the *trend*,
  not single-week perfection.
- **Did but felt off** (HR too high for the pace, couldn't hold target):
  log it, take an extra easy day, drop intensity on the next threshold
  slightly. Don't compensate by making the next session harder.
- **Did and felt great** (under HR target, easy effort): note it, but
  resist the urge to push pace faster. Progress in this method comes from
  *more time at the same controlled effort*, not from squeezing each
  session.

---

## What this framework is NOT

To prevent drift toward conventional advice:

- **Not "polarized 80/20."** Polarized treats the 20% as hard intervals at
  VO2max effort. This framework's "hard" portion is sub-threshold, not
  VO2max. Closer to 70-30 with the 30% being controlled threshold.
- **Not "more volume = better."** Bakken's elite block was 180 km/week
  *because* of double-threshold loading. At amateur volume, adding easy
  km doesn't substitute for threshold quality.
- **Not "long runs as quality."** Pure aerobic base. No fast finishes.
- **Not "marathon-style pyramid."** No huge volume blocks; the structure
  is consistent week to week with periodic deloads.
- **Not "harder is better."** Slightly under target HR is fine, often
  better. Significantly over target HR is a problem.

---

## When this file gets out of date

Edit this doc when:
- The user's training methodology shifts (e.g., away from Bakken toward
  something else).
- New evidence from your own training challenges a tenet here (e.g., if
  you find that one threshold per week with a true long run works better
  than two thresholds with a flat-pace long run, update accordingly).
- Race results suggest a different focus (e.g., shifting from 10k to
  marathon-specific work).

Do NOT edit it for one-off week-level adjustments — those go in
`plan.json`. The philosophy doc is the framework; the plan is the
execution.
