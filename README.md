# garmin-coach-mcp

Personal MCP server for managing running training. Plan workouts, push
them to Garmin Connect, pull recent activities from Strava into a local
cache, and reason about training data through Claude.

Designed for one user; not packaged for public distribution. Built around
Marius Bakken's Norwegian threshold method (sub-threshold-focused
training), with the framework, benchmarks, and per-activity reasoning
exposed as MCP resources so Claude can act as a coach with consistent
priors across sessions.

## Architecture

```
┌─ Claude (Desktop / Code / API) ──────────────────────────────────────┐
│                                                                      │
│  ┌─ garmin-coach (this repo) ───────┐  ┌─ r-huijts/strava-mcp ─────┐ │
│  │ GARMIN WRITE: create / schedule  │  │ READ raw Strava:           │ │
│  │  / reschedule / swap / delete    │  │  activities, laps, streams,│ │
│  │  workouts on Garmin Connect      │  │  segments, athlete profile │ │
│  │                                  │  │                            │ │
│  │ STRAVA READ (cached locally):    │  │                            │ │
│  │  sync_activities → SQLite        │  │                            │ │
│  │  weekly_summary, activity_       │  │                            │ │
│  │  breakdown, weekly_retrospective │  │                            │ │
│  │                                  │  │                            │ │
│  │ PLAN: get / validate /           │  │                            │ │
│  │  summarize / save /              │  │                            │ │
│  │  materialize / compare_vs_actual │  │                            │ │
│  │                                  │  │                            │ │
│  │ PROFILE SETUP: user_profile_     │  │                            │ │
│  │  status / init_user_profile      │  │                            │ │
│  │                                  │  │                            │ │
│  │ GEAR: list_gear, get_gear_       │  │                            │ │
│  │  for_activity (shoe mileage)     │  │                            │ │
│  │                                  │  │                            │ │
│  │ RECOVERY: morning_check_in       │  │                            │ │
│  │  (HRV, sleep, readiness)         │  │                            │ │
│  │                                  │  │                            │ │
│  │ RESOURCES (markdown):            │  │                            │ │
│  │  coach://classification          │  │                            │ │
│  │  coach://user_profile            │  │                            │ │
│  │  coach://training_philosophy     │  │                            │ │
│  └──────────────────────────────────┘  └────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
                    │                                  │
                    ▼                                  ▼
                Garmin Connect                       Strava
            (workouts, calendar,                  (activities,
             gear, HRV/sleep/                     HR streams)
             readiness)
```

The split:
- **Garmin** is the *write* surface for workouts + the source of truth for
  recovery metrics and gear (shoe mileage).
- **Strava** is the *read* surface for activity history (cleaner naming,
  better stream API on free tier).
- A local **SQLite cache** sits between Strava and Claude so weekly
  analysis doesn't re-pull streams from Strava on every query.
- **Resources** (markdown) hold the strategic context — how to classify
  activities, the athlete's HR zones and PRs, the Bakken framework.
  Edited by hand or via Claude conversation; loaded by Claude on each
  session.
- A **`plan.json`** in `coach_data/` holds the active training block.
  Materialized to Garmin via `materialize_plan`. Compared to actuals via
  `compare_plan_vs_actual`.

## Setup

### Requirements
- Python 3.10+ with venv
- Strava API app (free, register at https://www.strava.com/settings/api)
- Garmin Connect account (no MFA — see notes below)
- `claude` CLI for MCP registration

### Initial install

Replace `<repo>` below with the absolute path to your clone of this repo.

```bash
cd <repo>
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create `.env` in the repo root:

```
GARMIN_EMAIL=your-garmin-email
GARMIN_PASSWORD=your-garmin-password
STRAVA_CLIENT_ID=your-strava-app-client-id
STRAVA_CLIENT_SECRET=your-strava-app-client-secret
```

### Register both MCPs with Claude Code

```bash
# This repo's MCP — replace <repo> with the absolute path
claude mcp add -s user garmin-coach -- \
  <repo>/.venv/bin/python <repo>/server.py

# Strava MCP for reads (re-using Strava credentials from .env)
set -a && source <repo>/.env && set +a && \
  JSON=$(printf '{"type":"stdio","command":"npx","args":["-y","@r-huijts/strava-mcp-server"],"env":{"STRAVA_CLIENT_ID":"%s","STRAVA_CLIENT_SECRET":"%s"}}' \
    "$STRAVA_CLIENT_ID" "$STRAVA_CLIENT_SECRET") && \
  claude mcp add-json -s user strava "$JSON"
```

### First-time setup in Claude

Restart Claude Code, then in a fresh session:

```
# Verify both servers registered + healthy
claude mcp list

# Authorize Strava (browser opens once for OAuth)
> connect my strava account

# Pull recent activities + HR streams into the local cache (12-week backfill)
> sync activities

# Set up your training profile — Claude walks you through it
> Let's set up my profile
```

The profile setup tool (`user_profile_status` → `init_user_profile`) asks
about 8 things: max HR (required), optional Olympiatoppen vs custom HR
zones, optional lactate / VO2max test values, weight, race PRs, and any
notes. Only max HR is required; everything else can be skipped and added
later by editing `coach_data/user_profile.md` directly.

If you'd rather hand-write the profile instead of the conversational
flow, copy and edit the template:

```bash
cp coach_data/examples/user_profile.example.md coach_data/user_profile.md
```

`coach_data/user_profile.md` and `coach_data/plan.json` are both
gitignored — they hold personal data.

Garmin auth happens lazily on the first tool call that needs it (no
explicit "connect garmin" step). The `mcp dev server.py` Inspector is
also available for debugging tools without going through Claude.

## What's in the repo

```
server.py                       # MCP entrypoint — all tools + resources
strava_sync.py                  # Strava cache (SQLite), sync, weekly_summary
plan.py                         # plan.json I/O + compliance comparison
requirements.txt
.env                            # secrets (gitignored)
coach_data/
  classification.md           # workout type rules → coach://classification
  training_philosophy.md      # Bakken framework   → coach://training_philosophy
  user_profile.md             # YOUR values        → coach://user_profile  (gitignored)
  plan.json                   # YOUR active plan                          (gitignored)
  cache.db                    # SQLite cache                              (gitignored)
  examples/
    user_profile.example.md   # template — Claude can also generate via init_user_profile
    plan.example.json         # template — sample 1-week Bakken structure
```

The framework docs (`classification.md`, `training_philosophy.md`) and
the code are committed and shareable. Your specific HR values, plan, and
cached activities live alongside them but stay out of git.

## MCP tools

### Workout creation (Garmin write)
| Tool | What it does |
|---|---|
| `test_garmin_connection` | Verifies login works |
| `list_workout_templates` | Library of saved workouts |
| `get_workout_template` | Full structure of one template |
| `create_continuous_run` | Easy / long / tempo (single-step) |
| `create_interval_workout` | Threshold / VO2 / structured intervals (warmup + sets + cooldown) |
| `delete_workout_template` | Remove a template (also unschedules) |

### Scheduling
| Tool | What it does |
|---|---|
| `list_scheduled_workouts` | Calendar between two dates |
| `schedule_workout` | Put a template on a date |
| `unschedule_workout` | Remove from calendar |
| `reschedule_workout` | Move to a new date |
| `swap_scheduled_workouts` | Swap two dates' workouts |

### Strava sync + analysis
| Tool | What it does |
|---|---|
| `sync_activities` | Pull new activities + HR streams into local cache |
| `weekly_summary` | Per-week volume, sessions, time-in-zone |
| `activity_breakdown` | One activity's zone breakdown + metadata |

### Profile setup
| Tool | What it does |
|---|---|
| `user_profile_status` | Check whether `user_profile.md` exists / is filled in |
| `init_user_profile` | Generate `user_profile.md` from structured params (max HR + optional test data, PRs). Derives sub-threshold band, easy cap, VO2 band automatically. Refuses to clobber without `overwrite=True`. |

On a fresh install, after registering the MCPs, you can say *"set up my
profile"* and Claude will call `user_profile_status`, walk you through
the questions, then `init_user_profile(...)` to write the file. The
generated profile is minimal but complete — you can enrich it manually
(test caveats, recent outdoor runs, etc.) once it's in place.

### Equipment / gear
| Tool | What it does |
|---|---|
| `list_gear` | Shoes (and other gear) in rotation, with mileage and activity count |
| `get_gear_for_activity` | Which gear was used on a specific Garmin activity |

Garmin is the source of truth for gear (it auto-tracks mileage from
activities). Note that `get_gear_for_activity` takes a **Garmin**
activity_id, not the Strava ID used elsewhere — find it in the Garmin
Connect URL.

### Plan management
| Tool | What it does |
|---|---|
| `get_plan` | Read current plan.json |
| `validate_plan` | Structural check on a draft dict before saving (errors + warnings) |
| `summarize_plan` | Preview per-week structure on a draft dict before saving |
| `save_plan` | Write plan.json |
| `materialize_plan` | Push planned workouts to Garmin, persist Garmin IDs back |
| `compare_plan_vs_actual` | Compliance per workout (medium-strict: type + ±15% distance) |

The flow during a planning conversation is typically:
draft (in chat) → `summarize_plan(draft)` → `validate_plan(draft)` →
`save_plan(draft)` → `materialize_plan()` later.

### Coaching helpers
| Tool | What it does |
|---|---|
| `morning_check_in` | Today's training readiness, sleep, HRV, body battery from Garmin |
| `weekly_retrospective` | Bundles weekly_summary + plan compliance for Sunday review |

## MCP resources

| URI | Content |
|---|---|
| `coach://classification` | How to classify activities (naming rules, target zone bands, ambiguity handling) |
| `coach://user_profile` | Max HR, Garmin zones, VO2max test data, derived HR target bands, race PRs, pace ↔ HR mapping |
| `coach://training_philosophy` | The framework — Bakken Norwegian threshold method, session formats, weekly structure, recovery cues |

## Typical workflows

### Weekly summary
```
> summarize last week
# Claude reads coach://classification + coach://user_profile,
# calls weekly_summary(start, end), interprets the zone distribution
# and flags deviations from the target band.
```

### Drafting a plan
```
> Help me draft a 12-week plan starting next Monday. Norwegian Singles structure,
> 2 sub-threshold sessions per week, one long run, easy filler. Race goal in week 12.
# Claude reads coach://training_philosophy + coach://user_profile,
# proposes the structure, asks for input, then writes plan.json via save_plan.
```

### Pushing the plan to Garmin
```
> materialize the plan
# Iterates plan.json, creates workouts on Garmin, schedules them on dates,
# writes garmin_workout_id back so subsequent runs skip already-materialized.
```

### Mid-week adjustments
```
> Move Tuesday's threshold to Wednesday this week
# Claude calls swap_scheduled_workouts or reschedule_workout.
```

### Sunday retrospective
```
> Weekly retrospective for the week starting last Monday
# Combines weekly_summary + compare_plan_vs_actual, surfaces deviations.
```

### Morning readiness check
```
> Should I do today's threshold session?
# Claude calls morning_check_in, weighs training readiness, HRV, sleep
# against the planned session and the Bakken framework's traffic-light cues.
```

### Shoe rotation
```
> Which shoes are getting close to retirement?
# Claude calls list_gear, sorts by total_distance_km, flags shoes near
# typical max-mileage thresholds.
```

## Notes & caveats

**Garmin auth:** uses `python-garminconnect` with username/password. No
MFA support in this setup. Garmin rate-limits aggressively when too many
fresh logins happen — you may see `429` warnings on cold-start which the
library retries through automatically.

**Strava token:** stored at `~/.config/strava-mcp/config.json` by the
Strava MCP. This repo's `strava_sync.py` reads the same file, and
refreshes the token (writing back) when expired. No second OAuth flow.

**Background sync:** every MCP server start runs an incremental
`sync_activities` in a daemon thread. Cheap when nothing's new (~1-2
Strava calls), 45-60 seconds for a cold 12-week backfill.

**Zone calibration:** HR zones live in `coach_data/user_profile.md` — when
Garmin's zones change, update the table verbatim. Zone time is recomputed
from raw streams on every query, so updates apply retroactively.

**Plan persistence:** `coach_data/plan.json` is the source of truth for
intent. Garmin calendar is operational state. The two can drift if you
reschedule a lot — that's fine, compliance comparison still works
against the original plan.

**Local-only:** runs on the user's Mac. Not reachable from Claude on
phone or web; that would require remote hosting with auth + OAuth flow
for Strava and a non-password Garmin auth path. Not currently planned.

## Status

Personal project, actively developed. Built incrementally with Claude
Code. Not packaged for public distribution, but the framework docs and
code are written to be re-usable by anyone who clones the repo, fills in
their own `user_profile.md`, and adopts a Bakken-style training framework.

If you're sharing the repo to a fresh machine, note that any
`user_profile.md` or `plan.json` that existed before the gitignore was
added may still be present in git history. If those values are
sensitive, scrub the history (e.g. `git filter-repo`) before pushing
publicly.
