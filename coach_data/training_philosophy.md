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

> *"Du blir sliten av volumet, ikke intensiteten."*
> — Bakken, kap. 1 (s. 21)

Intensity must be precise enough to be tolerated and repeated. You do not
improve by training as hard as possible; you improve by training hard
enough, often enough, for long enough. The whole framework follows from
this. Most coaching mistakes come from
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

These get conflated all the time, so be explicit. Bakken himself flags
this directly in kap. 1, "Den gylne sonen" (s. 19):

> *"Når jeg bruker ordet terskeltrening her, mener jeg i praksis trening
> opp til, men ikke for mye rett på eller over terskelen."*
> — Bakken
> (When I use the word "threshold training" here, I mean in practice
> training up to, but not too much directly at or over the threshold.)

- **Threshold (LT2 / MLSS / OBLA) ≈ 4.0 mmol/L lactate.** Both
  conventional sports science and Bakken agree on this. A fixed
  physiological landmark — the highest steady-state effort before lactate
  accumulates uncontrollably. The 4.0 number itself is a pedagogical
  simplification standardized since the 1970s.
- **"Threshold work" — the session — means different things to different
  coaches.** This is where the confusion lives.
  - Conventional plans: "threshold work" = sessions *at* ~4 mmol (one
    hard session per week, costs a lot of recovery).
  - Bakken: "threshold work" = sessions *below* threshold, at ~2.3-3.0
    mmol with individual variation 2.0-3.5 mmol ("Den gylne sonen" — the
    Golden Zone). Lower per-session stimulus but much higher repeatable
    weekly volume.

Bakken doesn't redefine threshold — he just deliberately trains under it.
The trade is net-positive over weeks: at-threshold once a week vs
sub-threshold 2-3× a week is much higher weekly stimulus with less
recovery cost per session.

**We're training under Bakken's framework**, so when this doc or the
plan.json says "threshold session" or "terskelintervaller," that means
**sub-threshold in the Golden Zone (2.3-3.0 mmol / 80-87% max HR)**, not
at-threshold at 4 mmol.

### The Golden Zone vs other zones — Bakken's figur 1.2

Direct comparison from the book:

| | Rolig | Grå sone | **Den gylne sonen** | Høyintensitet |
|---|---|---|---|---|
| **Talk test** | Can speak freely | Whole sentences | **3-5 words per breath** | Single words |
| **% max HR** | < 70% | 70 – 80% | **80 – 87%** | > 90% |
| **Lactate** | Low | Moderate | **2-3 mmol/L** | Accumulates |
| **Training effect** | Low | Limited | **High ✓** | High |
| **Muscular load** | Low ✓ | Moderate | **Moderate ✓** | High |
| **Recovery time** | Short ✓ | Medium | **Medium ✓** | Long |
| **OK for frequent training** | Yes ✓ | No | **Yes ✓** | No |

The point of the table: the gray zone (70-80% max HR, where many runners
default) has limited training effect AND moderate cost — worst of both
worlds. The Golden Zone has high training effect at a recoverable cost.
That's why Bakken says **"Du øker via volumet, ikke intensiteten"** —
you progress by accumulating more time in this zone, not by going harder.

### Deriving your sub-threshold band

Two rules, which should converge:

1. **From a lactate test** (most precise): interpolate where 2.3 and 3.0
   mmol fall on your HR curve. Those bpm values are your sub-threshold
   band's lower and upper bounds.
2. **Without a test** (approximation): **80 – 87% of max HR** *for a
   well-trained runner*. The book scales this by training level (s. 30):
   **77-84%** for a newer runner, **80-87%** godt trent, **82-89%**
   topptrent. Pick the band by training history, not by ambition.

**If you take the band from a watch's own auto-threshold**: Bakken says
subtract a **3-5% safety margin** on threshold sessions (s. 30) — the
algorithms validate well against lab tests but land lactate a bit higher
than intended.

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
could keep doing this much longer than I am." Bakken's specific talk
test: **you should be able to get 3-5 words out per breath**. If you can
speak in whole sentences without strain, you're too slow. If you can only
manage single words, you're at-threshold or above — slow down.

If HR drifts above the sub-threshold band's upper bound on later reps:
cut pace, not the session. The target is repeatability across weeks, not
making each session as hard as possible.

The point is *repeatability*, not heroics. A session that wrecks the next
one is a net loss.

---

## Rep-length pace adjustment (within the same HR target)

Same Golden Zone HR target, different paces depending on rep design.
Bakken's calibration has two parts:

1. **Anchor:** calculated golden-zone pace ≈ standard T-pace (from race
   calculators) **+ 8-12 sec/km** — the conservative shift that moves
   textbook T-tempo down into the 2.3-3.0 mmol zone.
2. **Tempo guide by rep length**, relative to that calculated
   golden-zone pace (the book's own table):

| Rep length | Pace vs calculated golden-zone pace |
|---|---|
| 45 s – 1 min | ~5-7 sec/km **faster** |
| 4 – 5 min | equal — the baseline |
| 6 – 8 min | ~5-7 sec/km slower |
| 10 – 15 min | ~7-10 sec/km slower |

The book's warning: follow a race calculator slavishly on 10-min reps
and you run 7-10 sec/km too fast — well over threshold, and over time
that is overload. The athlete's actual numbers are in
`coach://user_profile` under "Session pace estimates."

## Session formats

Bakken's three threshold session types, all done as **intervals with
recovery** (not continuous):

| Type | Rep length | Recovery | Lactate target | Notes |
|---|---|---|---|---|
| **Long reps (sustained)** | 6 – 10 min | 1 – 1:30 (10+ min: 1:30 – 2) | At or just below 3.0 | Most common; e.g. 5×6 min, 4×8 min, 5×1000 m |
| **Short reps (over/under)** | 45 s – 1 min | 15 – 30 s | At or slightly above threshold | 45/15, 30/15, etc. Higher turnover at same lactate band |
| **Float / progression** | 6 – 10 min | minimal | Build from 2.0 → 3.0 across the set | Slightly faster each rep |

Bakken's pause table is keyed to **rep length**, not to where you sit in
the band (s. 43): 1-2 min reps → 30 s-1 min · 3-6 min reps → 1-1:30 ·
10+ min reps → 1:30-2 min. These are starting points, not rules —
temperature, form and training phase all shift them.

**Composite sessions** the book names (s. 42) and this doc otherwise
misses:
- **Blandet:** 3 × 10 min (90 s) then, after a few min jog, 10 × 45/15 —
  long reps for base load, short reps to loosen the musculature.
- **Pyramide:** 3–6–9–6–3 min (90 s), optionally closing with 5-10 × 20/20.
- **Nedtrapping:** 10–8–6–4–2–1 min, increasing speed — put the 10 min at
  the bottom of the band and the 1 min at the top.

Why intervals beat continuous: short rests let muscle tone reset, keeping
lactate controllable. Bakken's stronger claim (kap. 3, s. 38-39):
**continuous threshold runs are inferior to interval threshold work** at
the same intensity because (a) intervals let you accumulate more total
time at the target HR before hitting the wall, and (b) the brief rests
prevent the slow drift into supra-threshold that ruins a continuous
tempo. The exception is HM **and** marathon prep — Bakken explicitly
allows 20-40 min continuous threshold runs for half marathon and
marathon ("metabolsk utholdenhet"), and lists 40-60 min continuous
threshold **in varying tempo** as a half-marathon X-økt option. Even
then the continuous work varies pace around threshold (under → at →
just over, with running "pauses" below); it is never a steady
goal-pace grind. For 5-10 km goals, intervals remain the clear
recommendation.

### Variation within sub-threshold work

Bakken's third "frame" is to vary *inside* the Golden Zone over a block:

- **Rep length variation:** rotate short (1-3 min), medium (4-8 min),
  long (8-12 min) reps across weeks so the body sees different stimuli at
  the same HR target.
- **Intensity micro-variation:** progress sessions across a block from
  the lower end of the sub-threshold band toward the upper end.
- **Recovery variation:** pause length is set by rep length (see the table
  above), and varying it is its own lever — shorter pauses at the same rep
  design accumulate more, longer pauses sharpen each rep. The book also
  uses *long* running rests (2-4 min) deliberately on intensive work, to
  reach near race pace at far lower total cost (s. 138).
- **Intensity variation:** the book's own span here is wider than
  sub-threshold alone — "fra under terskel (maratontempo) til rett på
  terskel (halvmaratontempo) til litt over terskel (15 km-tempo)" (s. 37).
- **Terrain variation:** track, road, light trail, hill — same HR target,
  different load profiles.

The point: monotony at sub-threshold intensity is what causes both
plateaus and burnout. Variation keeps adaptation moving without escalating
intensity.

### Smart Strides (session-end recovery protocol)

Bakken's named protocol, and its purpose is **recovery, not speed work**:
lowering muscle tone faster after hard work — he calls it one of the few
shortcuts to quicker recovery between sessions. Prescribed after **all
hard sessions** as routine, not kept rare: 2-3 min easy jog after the
main set, then **5-8 × ~100 m** even, controlled strides slightly above
threshold pace. Not sprints; walk or jog between; the focus is
normalizing the musculature, not speed. Treat it as the default way to
*end* a hard session.

### Example sessions the user has used or might use

- 5 × 6 min @ sub-threshold HR, 1 min jog rest — classic Bakken long-rep
- 4 × 8 min @ sub-threshold HR, 90 s rest — slightly longer reps
- 10 × 1 km @ sub-threshold pace, 60-90 s jog — high-turnover
- 30 × 1 min @ sub-threshold HR — Bakken's own example (s. 41) of how to
  bank 30 min of threshold work with every rep feeling manageable
- 45/15 × 20-30 min — short on/off at sub-threshold HR, not all-out

### 45/15 deserves a special note

Bakken treats 45/15 as the **single most versatile threshold format**.
Time-efficient (~30 min for a full session), low cognitive load (you don't
have to think about pacing 6-min reps), and unusually easy to recover from
because each work bout is short.

Specific protocol variants from the book:
- **Standard:** 15-30 reps, controlled — volume progresses toward 30
  (or split as 2 × 15) over a block.
- **Pyramid:** vary the WORK duration, not rep count — 3 × 30 s /
  3 × 45 s / 3 × 60 s / 3 × 45 s / 3 × 30 s, all with 15 s rest.
- **Block:** 3 × (10 × 45/15) with ~3 min rest between blocks, pace
  escalating per block (10k → 5k → 3k effort — this variant runs above
  threshold and belongs in the X-økt slot).

**Dual-use:** 45/15 spans two roles. As a *threshold session* it runs at
sub-threshold HR (lactate oscillates around threshold; the 15 s pauses
clear it). As the *X-økt* it runs progressive and over threshold —
start at 10 km effort, build toward 5 km effort, up to ~95% of max HR.
Same format, different intent; be explicit about which one is planned.

**Where 45/15 specifically shines:** weeks with limited time, return from
illness or injury (you can do half the reps and still get useful stimulus),
and as a variation lever inside a longer training block to break monotony
on weeks where longer reps feel stale.

**Progression rule** (from HK Lab + Bakken): when threshold feels too
easy, do NOT make it faster. Instead:
- Increase total threshold time. The book's reference points: **30-50 min
  per WEEK** is already enough for good progress at low volume (s. 45),
  while **30-40 min within a single session** is the length elite runners
  and Bakken himself used (s. 94). Don't confuse the two.
- Lengthen individual reps (4 → 6 → 8 min)
- Shorten recovery (60s → 45s → 30s) — a house lever; the book's own pause
  guidance is by rep length, not a progression axis.

Faster pace at the same HR is what improvement *looks* like. It happens
on its own; don't chase it.

**Within-session progression** (Bakken, "konservativ tilnærming"):
**make every session progressive** — start the first rep at the slow end
of your sub-threshold band and build through it. Feeling strong at the
end? **Add an extra rep / extra volume — do not push intensity higher**
(book: *"legg heller inn et ekstra volum mot slutten hvis du føler deg
sterk"*). Volume is the reward for a good day; pace/HR is not.

**Myk start, myk landing:** the first and last interval of a session are
never intensive. Composite sessions end with the *shortest* reps, run
below threshold — that lowers muscle tone into the recovery day
("nullstilling av kroppen").
> *"Det er bedre å avslutte med følelsen av at du kunne gjort mer, enn å
> ha presset deg for hardt i starten, for så å måtte redusere
> intensiteten senere."*
> — Bakken
>
> *(Better to finish feeling you could have done more than to push too hard
> early and need to scale back later.)*

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
- HR target: roughly 92 – 96% of max HR (top of Z5) — our
  operationalization; the book says "well over threshold" / >90%, with
  progressive 45/15 reaching ~95%. See `coach://user_profile`.
- Formats (the book's menu is SHORT efforts, never stacked sustained
  intervals): hill repeats 8-15 × ~200 m well over threshold;
  progressive 45/15 (start at 10k effort, build to 5k); short 20-30 s
  bursts; fast 400s with long (2-4 min) rests; near race day, 800-1000 m
  reps around 5k pace.
- Skip it in weeks where two threshold sessions already feel taxing —
  threshold takes priority.

---

## Easy runs

The discipline is non-negotiable: **easy is truly easy**.

> "I would entirely stay away from the zone in between very easy running
> and the threshold."  — Bakken

Bakken's rule is explicit and **universal**, and it is stated as **heart
rate, not pace**: easy runs are run *in their entirety* below **70% of
max HR** (s. 35). The pace that produces is individual and downstream —
the book only illustrates it: *"Min erfaring er at dette prinsippet
gjelder uansett nivå. For mange mosjonister betyr det 5:00-6:30 min/km,
kanskje saktere — og det er helt riktig."*

Treat that range as an illustration, never a target. **Whatever pace it
takes to hold the HR ceiling is the correct pace**, including well slower
than 6:30/km — the book's "kanskje saktere" is the operative clause, and
running faster to stay inside a quoted pace range inverts the rule. The
gray zone (70-80%) is a bad trade: the effect barely rises, the load does.

- **Aim: the whole run below ~70% of max HR** (Z1 / bottom of Z2 — see
  `coach://user_profile` for the bpm). Upward drift on long runs
  happens; rein it in rather than budgeting for it.
- **Absolute ceiling: LT1.** Anything above LT1 is unambiguously gray
  zone. (Using lab-measured LT1 as the hard ceiling is this framework's
  lab-anchored adaptation; the book's own stated rule is the stricter
  70%.)
- **Purpose:** aerobic base, economy, recovery — not a moderate workout.

See `coach://user_profile` for your easy-cap bpm. The real signal isn't
the number; it's whether you feel recovered the next morning.

---

## Long runs

In this framework, long runs are aerobic base — **not quality**.

- Run them at easy pace (Z1 / low Z2, same discipline as a regular easy
  run).
- No progression, no surges, no marathon-pace finishes (in this block).
- Duration: for goals under 10 km the book says **60-90 min is more than
  enough** (s. 107). Longer only for HM/marathon prep, where it also
  becomes progressive.
- Frequency: weekly, but can be skipped during heavy threshold blocks.

The deliberate choice not to make long runs "progressive" or "marathon
pace" in this block keeps total quality stress concentrated in the actual
threshold sessions. Long runs build durability without spending the
recovery budget that threshold work needs.

---

## Weekly structure: Norwegian Singles adaptation

The full Bakken method runs **double-threshold days** (two threshold
sessions in one day, preferably 6-8 h apart — down to 2-3 h works for
many) on Tuesdays and Thursdays/Saturdays. That's elite practice —
4 threshold sessions per week at ~180 km/week in the book's example.

For amateurs, the structure preserves the framework but reduces
frequency. The book names this variant and its lineage (s. 115-116):
Kristoffer Ingebrigtsen trialled it to stay fit around a busy schedule,
and James Copeland developed it further — what the forums later called
"Norwegian Singles." Bakken's own layout for it is **three threshold
sessions a week** (reps of ~10 min, ~6 min and ~3 min), three easy runs,
one rest day, and a test race every 4-8 weeks as the only harder load.
He puts its lactate range at **2.5-3.5 mmol** — slightly above the
2.3-3.0 golden zone — and recommends it especially for the injury-prone:

- **2-3 threshold sessions per week**, single sessions (not doubles).
- Every other day = a quality day; alternate days = easy.
- Most other runs are truly easy.
- 1 long run weekly (easy pace).
- 0-1 X-element session weekly (VO2max).

### Adjusted intensity distribution for 4-6 hour weekly volume

Bakken's chapter 7 ("Mosjonisten som vil mer") gives concrete targets
specifically for amateurs at this volume — the conventional 80/20 split
is too easy-heavy for this dose. The adjusted distribution:

| Intensity | % of weekly run time | At 5 h/week |
|---|---|---|
| Z1-Z2 (easy / aerobic base) | **60-65%** | ~3-3:15 hours |
| Z3-Z4 (sub-threshold, the Golden Zone) | **20-30%** | ~1:00-1:30 hours |
| Z5 (true high intensity) | **5-10%** | ~20-30 min |

(Z3-Z4 in this table is a weekly-time *accounting* bucket; the session HR
target itself is the profile's sub-threshold band — the part of Z4 above
the hard cap is not part of the target.)

These are the bands Claude should check against when reviewing weekly
summaries. They override the looser "Z1-2: 60-80% / Z3-4: 20-35% / Z5:
0-5%" target in `coach://classification` for athletes at this volume —
the sub-threshold share is treated as a firm *floor* (≥20%, not a range
to drift below) and the Z5 allowance is *higher* (5-10% vs 0-5%) because
amateurs need a slightly larger quality fraction to drive adaptation
than elites do.

### Sample week template (Bakken, 5-hour reference week)

This is the book's concrete chapter 7 layout, lightly adapted:

| Day | Session | Notes |
|---|---|---|
| Mon | **Rest** | Full recovery from weekend training. |
| Tue | **Threshold "støtte" session** | E.g. 6×6 min sub-threshold, or 10×3 min with 1 min jog. Variations in rep length OK; same HR target. |
| Wed | Easy 40-60 min | Truly easy — *"så sakte at det nesten føles feil"*. You should be able to talk effortlessly. |
| Thu | **Threshold "hoved" session** | E.g. 3-4×10 min, or a long varying-tempo session. Different format than Tuesday. **One of the most taxing sessions of the week** ("en av de mest belastende øktene") — long reps, more muscular wear; max once per week. |
| Fri | **Rest** or short easy | Strategic rest before the weekend. The book's caveat: some people respond badly to a fully free day before a Saturday quality session — if that's you, shuffle so Friday isn't completely off. |
| Sat | **X-økt (flex slot)** | See below — varies week to week. |
| Sun | **Long easy** | 60-90 min for sub-10k goals; progressive week-on-week only for HM/marathon. |

Two threshold sessions (Tue / Thu) + one flex slot (Sat) + one long
(Sun) is the working structure. **Strength training goes on or after
quality days (or on the Monday rest day at low load) — never bundled
onto the easy days.** Bakken explicitly shields easy days: *"rolig
løping, under 70 % av makspuls, ingenting annet"* — extra load belongs
on the threshold days, where the recovery cost is already being paid.

### The X-økt — Saturday's flex slot

Bakken calls Saturday the "X-økt" — a flexible third quality slot whose
character changes week to week and across the block:

- **Progressive 45/15** (X-økt form: 10k → 5k effort) when you want
  short-rep variation or have less time
- **Hills** when neuromuscular strength matters
- **Race-specific work** (closer to race day): mile reps, 6×1k at goal
  pace, or a tune-up effort / test race
- **A third threshold session** — explicitly book-sanctioned:
  *"Alternativt kan denne dagen inneholde en tredje terskeløkt for uken"*
- **Extra easy / skipped entirely** in deload weeks or when recovery is
  marginal

The X-økt is not a *fixed* third threshold — its value is variation —
but a third threshold session is a valid choice for the slot. What must
NOT be stacked on top of the two threshold days is additional
*intensive* (over-threshold) work: intensity is never the extra-load
lever in this framework. Treat the X-økt as what you cycle through to
keep the framework from going stale across a 12-week block.

### Advanced variant: double-threshold days (NOT current default)

**⚠ Gatekept. Default is Norwegian Singles. Don't recommend
double-threshold without explicit user confirmation.**

The elite protocol clusters two sub-threshold sessions into the same day
(preferably 6-8 h apart; the book notes 2-3 h can work): morning long
reps (5×6 min, 4×8 min), evening short reps (10×1k or 45/15). Both in
the Golden Zone — neither at-threshold. Muscle tone recovers enough
between sessions that the second lands on fresh legs despite partial
glycogen depletion, compounding weekly threshold volume well beyond
singles.

**Bakken's own gate (the book's "Advarsel") — he advises AGAINST doubles
unless you have ALL of:**
- full control of intensity around threshold,
- an extended injury-free period,
- experience that you tolerate double sessions generally,
- the discipline to abstain when it doesn't feel right.

Note the book has NO volume gate: it explicitly opens doubles to
amateurs — a double **every other week** is "more than enough" inside
the 5-hour week, and the entry ladder starts with easy+threshold on the
same day (morning 2-6 min reps / evening 30 s-1 min reps, the two days
around it fully easy). High weekly volume (70+ km) and 8-12 weeks of
consistent sub-threshold singles are additional advisory context used
here, not book requirements.

Preconditions before recommending:
- The four book criteria above, all true.
- ≥ 8-12 weeks of consistent sub-threshold singles (house rule).
- User explicitly wants to try it.

If any fail, stay on Singles. Adopt gradually via Bakken's ramp:
easy+threshold same-day first, first true doubles 10-15 sec/km slower
than normal sub-threshold pace, cadence starting at every other week.

**Frequency cap once adopted:** for a *godt erfaren mosjonist* the book's
cap is **maximum one double-day per week, two only very exceptionally**;
two per week is the sub-elite/elite figure (s. 71). Full easy days between. Feeling good every day tempts a third double —
that breaks the recovery loop the format depends on.

---

## The standard warm-up (this is where the readiness data comes from)

Bakken's recommended warm-up is not a jog — it is a **measurement** (s. 43-44):

> 5 min easy jog → **one threshold rep of 3-6 min at a fixed, familiar
> pace** → main session, adjusted on what that rep told you.

He used it before every session *and* every race; Jakob Ingebrigtsen does
the same. Run it at the same pace and the same length every time and it
becomes a standardized probe of the day's form. The warm-up is also the
check-in for niggles and for how the thighs feel.

Two smaller warm-up notes from the book: on double-threshold mornings,
check **resting HR and warm-up HR together** as a readiness pair (s. 69);
and threshold work should **open 5-10 sec/km slower than target pace**,
moving to target after 5-10 min if it feels right (s. 69).

## Recovery and the "traffic light" check

Bakken used a traffic-light system to decide whether a planned hard
session should go ahead. Crucially, the book's model is built on
**active tests, not passive metrics** — *"informasjon ingen passiv test
kan gi"*:

**Primary signals (the book's):**
- **Standardized test interval in the warm-up:** after ~5 min jog, run a
  fixed-pace 3-6 min interval you know well and compare HR to normal:
  **+8-10 bpm** over normal → reduce or abort (red); **within ±3-5
  bpm** → run as planned (yellow/green); **-3-5 bpm with light legs** →
  green.
- **Stair test:** how the thighs feel up the stairs that morning.
- **Resting HR** and **warm-up HR** vs your own normal.

**Responses:**
- **🟢 Green:** run as planned; with repeated green signals **add extra
  reps/volume — never raise intensity** (*"legg til ekstra drag. Ikke øk
  intensitet"*).
- **🟡 Yellow:** standard session, conservative start.
- **🔴 Red:** **reduce or abort** (the book's rule is that simple). Our
  house heuristics for "reduce": cut volume 30-50%, sit at the bottom of
  the band, or convert to easy. The next session matters more than this
  one.

**Wearable metrics are corroboration only** (via `morning_check_in`):
Garmin readiness, HRV, resting-HR trend, sleep, body battery can prompt
a yellow flag — but the active warm-up test and body feel decide.
(Wearable HRV/RHR are easily confounded by fragmented sleep, illness,
and life stress; any athlete-specific weighting belongs in
`coach://user_profile`.)

House rule (not book): two consecutive yellow/red days → skip the
planned quality session; the framework only works if you can repeat
sessions.

---

## Overreaching: cut intensity, not volume (chapter 12)

The book's single most counter-intuitive rule, and the one most likely to
be got backwards (s. 212, s. 217):

> **VED OVERTRENING: REDUSER INTENSITET, IKKE VOLUM.**

Keep the easy running going; take the intensity out. Bakken specifically
uses 45/15 and 30/30 during a back-off to keep stimulating the
musculature — dropping volume while keeping hard reps is the failure
mode, and makes the way back longer.

Two cheap checks the book leans on hardest:

- **The stair test.** Same stairs, same time of day, every day. Thighs
  suddenly heavy is often the *first* warning that recovery is not
  keeping up — it shows before resting HR moves (s. 205-207).
- **The ten-day rule.** Within any ten-day training period **at least one
  session should feel genuinely good** — light legs, tempted to add more.
  If that feeling doesn't arrive inside that window, put the brakes on
  (s. 205).

Ordering of the warning signs, in the book's sequence: muscular status
first (heavy thighs, raised muscle tone), then sleep and resting HR, then
performance. Resting HR is "bekymringsverdig" at **+5 bpm over your own
baseline for 3+ days** — a resting HR at or below baseline argues against
overtraining regardless of how a session felt.

A confounder worth knowing: an overtrained athlete shows *lower* HR,
lactate and cortisol response to a standard hard session, not higher —
the body can no longer mobilise a normal stress response (s. 205).

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

## Does the model fit? (individualization, from the book's genetics chapter)

Bakken's warning signs that threshold-heavy training may not suit an
athlete (type II / fast-fiber dominant):
- Sessions are executed correctly but progress stalls for months.
- Musculature feels constantly heavy, even after easy days and rest.
- Repeated muscle/tendon injuries despite correct intensity control.
- Short intense sessions feel natural; sustained threshold work feels
  like a fight.
- Background in explosive sports / always been fast rather than durable.

If several apply, the book's adaptations: cut the longest threshold
sessions (shorter reps, less total volume); shorter easy runs at higher
frequency; more explosive elements (sprint/strength tolerated well);
progressive threshold sessions that are allowed to finish through the
threshold; and every 2-3 weeks a week without threshold focus at all.
(This section exists so the check is made deliberately per athlete, not
assumed.)

---

## What this framework is NOT

- **Not "polarized 80/20"** — the hard portion is sub-threshold, not
  VO2max. Closer to 70-30 with the 30% being controlled threshold.
- **Not "more easy volume = better"** — at amateur volume, easy km don't
  substitute for threshold quality.
- **Not "long runs as quality"** — pure aerobic base, no fast finishes.
- **Not "harder is better"** — slightly under target HR is often better;
  over target is a problem.

---

## When this file gets out of date

Edit when methodology itself shifts (e.g., away from Bakken). Do NOT
edit for week-level adjustments — those go in `plan.json`.
