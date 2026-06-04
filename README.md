# garmin-coach-mcp

Personal running coach MCP server. Plan workouts, push them to Garmin
Connect, pull activity history into a local cache, and reason about
training data through Claude.

Built around Marius Bakken's Norwegian threshold method (sub-threshold-
focused training). The framework, HR zones, and per-activity analysis are
exposed as MCP resources so Claude acts as a coach with consistent priors
across sessions.

## Architecture

```
┌─ Claude (Desktop / Code / API) ───────────────────────────────┐
│                                                               │
│  ┌─ garmin-coach (this repo) ──────────────────────────────┐  │
│  │                                                         │  │
│  │  GARMIN WRITE   create / schedule / reschedule /        │  │
│  │                 swap / delete workouts                  │  │
│  │                                                         │  │
│  │  ACTIVITY DATA  sync from Garmin → SQLite cache         │  │
│  │                 weekly_summary, activity_breakdown       │  │
│  │                                                         │  │
│  │  WELLNESS       morning_check_in (HRV, sleep, HRR,      │  │
│  │                 training readiness, body battery)        │  │
│  │                                                         │  │
│  │  PLAN           get / validate / save / materialize /   │  │
│  │                 compare_vs_actual                        │  │
│  │                                                         │  │
│  │  RESOURCES      coach://user_profile                    │  │
│  │  (markdown)     coach://training_philosophy             │  │
│  │                 coach://classification                   │  │
│  │                 coach://plan_design                      │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
                            │
                            ▼
                    Garmin Connect
              (workouts, calendar, gear,
               activity history, HRV/sleep)
```

Everything flows through Garmin Connect — no Strava account required.
A local SQLite cache sits between Garmin and Claude so weekly analysis
doesn't re-hit the API on every query.

## Setup

### Requirements

- Python 3.10+
- Garmin Connect account — **no MFA** (see caveat below)
- Claude Code CLI (`claude`)

### Install

```bash
git clone <this-repo> garmin-coach-mcp
cd garmin-coach-mcp
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create `.env` from the example:

```bash
cp .env.example .env
# edit .env — fill in your Garmin email and password
```

### Register the MCP with Claude Code

Replace `<repo>` with the absolute path to your clone:

```bash
claude mcp add -s user garmin-coach -- \
  <repo>/.venv/bin/python <repo>/server.py
```

### First-time setup in Claude

Restart Claude Code, then in a fresh session:

```
# Verify the server is registered and healthy
> test garmin connection

# Pull activity history into the local cache (syncs last 12 weeks)
> sync my activities

# Set up your training profile — Claude walks you through it
> Let's set up my profile
```

The profile setup (`user_profile_status` → `init_user_profile`) asks
about max HR (required), optional lactate/VO2max test results, HR zones
from Garmin Connect, race PRs, and athlete type (A/B/C). Only max HR is
required — everything else can be skipped and added later.

If you prefer to hand-write the profile instead:

```bash
cp coach_data/examples/user_profile.example.md coach_data/user_profile.md
# Edit to fill in your own values
```

Both `coach_data/user_profile.md` and `coach_data/plan.json` are
gitignored — they hold personal data and never leave your machine.

### Garmin auth caveat

`python-garminconnect` uses username/password login and does **not**
support Garmin's MFA (two-factor authentication). If you have MFA
enabled on your Garmin account, you must temporarily disable it in
[Garmin Connect settings](https://connect.garmin.com) before setting
this up.

Garmin rate-limits login attempts aggressively. You may see `429`
warnings on cold start — the library retries automatically and this is
normal.

## What's in the repo

```
server.py                       # MCP entrypoint — all tools + resources
garmin_sync.py                  # Garmin cache (SQLite), sync, weekly_summary
plan.py                         # plan.json I/O + compliance comparison
requirements.txt
.env.example                    # credential template
.env                            # your secrets (gitignored)
coach_data/
  classification.md           # activity classification rules → coach://classification
  training_philosophy.md      # Bakken framework             → coach://training_philosophy
  plan_design.md               # planning guide              → coach://plan_design
  user_profile.md             # YOUR values (gitignored)     → coach://user_profile
  plan.json                   # YOUR active plan (gitignored)
  cache.db                    # SQLite activity cache (gitignored)
  examples/
    user_profile.example.md   # profile template
    plan.example.json         # sample 1-week Bakken plan structure
```

The framework docs (`classification.md`, `training_philosophy.md`,
`plan_design.md`) and all code are committed and shareable. Your HR
zones, plan, and cached activities live alongside them but stay out of
git.

## MCP tools

### Workout creation (Garmin write)

| Tool | What it does |
|---|---|
| `test_garmin_connection` | Verify login works |
| `list_workout_templates` | Library of saved workouts |
| `get_workout_template` | Full structure of one template |
| `create_continuous_run` | Easy / long / tempo (single-step) |
| `create_interval_workout` | Threshold / VO2 / structured intervals |
| `delete_workout_template` | Remove a template |

### Scheduling

| Tool | What it does |
|---|---|
| `list_scheduled_workouts` | Calendar between two dates |
| `schedule_workout` | Put a template on a date |
| `unschedule_workout` | Remove from calendar |
| `reschedule_workout` | Move to a new date |
| `swap_scheduled_workouts` | Swap two dates' workouts |

### Activity sync + analysis

| Tool | What it does |
|---|---|
| `sync_activities` | Pull new activities + HR streams + laps from Garmin |
| `weekly_summary` | Per-week volume, sessions, time-in-zone |
| `activity_breakdown` | One activity's zone breakdown + lap classification |
| `weekly_retrospective` | `weekly_summary` + plan compliance for Sunday review |

### Wellness / readiness

| Tool | What it does |
|---|---|
| `morning_check_in` | HRV, RHR, sleep, body battery, training readiness |
| `get_wellness_history` | Multi-day wellness trends |

### Profile setup

| Tool | What it does |
|---|---|
| `user_profile_status` | Check whether `user_profile.md` exists / is filled in |
| `init_user_profile` | Generate `user_profile.md` from structured inputs |
| `get_athlete_profile` | Parse current profile into structured fields for tools |

### Gear

| Tool | What it does |
|---|---|
| `list_gear` | Shoes and gear in rotation, with total mileage |
| `get_gear_for_activity` | Which gear was used on a specific activity |

### Plan management

| Tool | What it does |
|---|---|
| `get_plan` | Read current `plan.json` |
| `validate_plan` | Structural check on a draft before saving |
| `summarize_plan` | Preview per-week structure before saving |
| `save_plan` | Write `plan.json` |
| `materialize_plan` | Push planned workouts to Garmin calendar |
| `compare_plan_vs_actual` | Compliance per workout (type + ±15% distance) |

## MCP resources

| URI | Content |
|---|---|
| `coach://classification` | Activity classification rules (naming conventions, zone bands, ambiguity handling) |
| `coach://user_profile` | Max HR, zones, VO2max test data, derived target bands, race PRs, pace ↔ HR mapping |
| `coach://training_philosophy` | The Bakken Norwegian threshold framework — session formats, weekly structure, recovery cues |
| `coach://plan_design` | Step-by-step planning guide with grounding checklist |

## Typical workflows

### Weekly summary
```
> summarize last week
```
Claude reads `coach://classification` + `coach://user_profile`, calls
`weekly_summary`, interprets zone distribution and flags deviations.

### Drafting a plan
```
> Help me draft a 12-week plan starting next Monday.
> Norwegian Singles structure, 2 sub-threshold sessions per week,
> one long run. Race goal in week 12.
```

### Pushing the plan to Garmin
```
> materialize the plan
```

### Morning readiness check
```
> Should I do today's threshold session?
```
Claude calls `morning_check_in`, weighs HRV, sleep, and training
readiness against the session and the Bakken traffic-light cues.

### Mid-week adjustments
```
> Move Tuesday's threshold to Wednesday
```

## Notes

**Background sync:** every MCP server start runs an incremental
`sync_activities` in a daemon thread. Cheap when nothing's new (seconds),
45-60 s for a cold 12-week backfill.

**Zone calibration:** HR zones live in `coach_data/user_profile.md`. When
Garmin's zones change, update the table verbatim. Zone time is recomputed
from raw streams on every query, so updates apply retroactively.

**Plan persistence:** `coach_data/plan.json` is the source of truth for
intent. Garmin calendar is operational state. The two can drift if you
reschedule a lot — compliance comparison still works against the original
plan.

**Local-only:** runs on your machine. Not reachable from Claude on phone
or web without remote hosting (which is out of scope for this project).
