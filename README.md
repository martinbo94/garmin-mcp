# garmin-mcp

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
│  ┌─ garmin-mcp (this repo) ────────────────────────────────┐  │
│  │                                                         │  │
│  │  GARMIN WRITE   create / schedule / reschedule /        │  │
│  │                 swap / delete workouts                  │  │
│  │                                                         │  │
│  │  ACTIVITY DATA  sync from Garmin → SQLite cache         │  │
│  │                 weekly_summary, list_activities,         │  │
│  │                 activity_breakdown, query_activity_cache │  │
│  │                                                         │  │
│  │  ANALYSIS       progress / PRs / form / elevation /     │  │
│  │                 ACWR / deload / taper / recovery         │  │
│  │                                                         │  │
│  │  WELLNESS       morning_check_in (HRV, sleep, HRR,      │  │
│  │                 readiness), illness + stress + sleep     │  │
│  │                                                         │  │
│  │  CALCULATORS    pace, intervals, heat (dew-point),      │  │
│  │                 weather (Open-Meteo), GPX race pacing    │  │
│  │                                                         │  │
│  │  PLAN           get / save / materialize /              │  │
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

- Python 3.10+ ([download](https://www.python.org/downloads/))
- Garmin Connect account (MFA supported)
- Claude Desktop ([download](https://claude.ai/download)) **or** Claude Code CLI

### Quick setup (Mac)

Open **Terminal** (search "Terminal" in Spotlight) and run these three commands,
replacing the path in the last one with wherever you cloned the repo:

```bash
# 1. Install Git if you don't have it (opens Xcode tools installer)
git --version

# 2. Clone the repo
git clone https://github.com/martinbo94/garmin-mcp.git
cd garmin-mcp

# 3. Run the setup script
bash setup.sh
```

The script installs dependencies, creates `.env`, authenticates with Garmin
(including MFA if enabled), and registers the server with Claude Desktop.

**First run:** the script will stop after creating `.env` and ask you to fill
in your Garmin email and password. Do that, then run `bash setup.sh` again —
it will authenticate and complete setup.

After setup finishes:
1. Restart Claude Desktop
2. Start a new chat and say **"Let's set up my profile"**

### Manual setup (Claude Code or Windows)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env with your Garmin credentials
```

**Claude Desktop** — add to
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin-mcp": {
      "command": "/absolute/path/to/garmin-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/garmin-mcp/server.py"]
    }
  }
}
```

**Claude Code** — run once:

```bash
claude mcp add -s user garmin-mcp -- \
  <repo>/.venv/bin/python <repo>/server.py
```

### First-time setup in Claude

Restart Claude, then in a fresh session:

```
# Verify the server is registered and healthy
> test garmin connection

# Pull activity history into the local cache (syncs last 12 weeks by default)
> sync my activities

# To sync more history (e.g. a full year):
> sync my activities for the last 52 weeks

# Set up your training profile — Claude walks you through it
> Let's set up my profile
```

The profile setup (`user_profile_status` → `init_user_profile`) takes
max HR (required) plus optional HR zone ceilings, LT1/LT2, VO2max, weight,
and race PRs. Only max HR is required — everything else can be skipped and
added later. (There's no fixed "athlete type" — the limiter, if known, is
recorded as a revisable note; see `coach://plan_design`.)

If you prefer to hand-write the profile instead:

```bash
cp coach_data/examples/user_profile.example.md coach_data/user_profile.md
# Edit to fill in your own values
```

Both `coach_data/user_profile.md` and `coach_data/plan.json` are
gitignored — they hold personal data and never leave your machine.

### Garmin auth

`setup.sh` handles authentication interactively and caches tokens to
`~/.garminconnect`. After the first run, the MCP logs in automatically
from the token cache — no password or MFA code needed again until the
refresh token expires (typically several months).

**MFA is supported.** If your Garmin account has two-factor
authentication enabled, `setup.sh` will prompt you to enter the code
once. Subsequent logins use the cached token automatically.

Garmin rate-limits login attempts aggressively. You may see `429`
warnings on cold start — the library retries automatically and this is
normal.

## What's in the repo

```
server.py                       # MCP entrypoint — all tools + resources
garmin_sync.py                  # Garmin cache (SQLite), sync, weekly_summary, wellness
plan.py                         # plan.json I/O + compliance comparison
gpx_analysis.py                 # GPX course parsing + grade-adjusted race pacing
requirements.txt
.env.example                    # credential template
.env                            # your secrets (gitignored)
coach_data/
  workout_classification.md           # activity classification rules → coach://classification
  training_philosophy.md      # Bakken framework             → coach://training_philosophy
  plan_design.md               # planning guide              → coach://plan_design
  user_profile.md             # YOUR values (gitignored)     → coach://user_profile
  plan.json                   # YOUR active plan (gitignored)
  cache.db                    # SQLite activity cache (gitignored)
  examples/
    user_profile.example.md   # profile template
    plan.example.json         # sample 1-week Bakken plan structure
```

The framework docs (`workout_classification.md`, `training_philosophy.md`,
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
| `sync_activities` | Pull new activities + HR streams + laps + recent wellness into the cache |
| `list_activities` | Flat, filterable activity list (date, start time, distance, HR, pace, classification) |
| `query_activity_cache` | Read-only SQL SELECT against the local cache for custom analysis |
| `weekly_summary` | Per-week volume, sessions, time-in-zone |
| `activity_breakdown` | One activity's zone breakdown + lap classification |
| `weekly_retrospective` | `weekly_summary` + plan compliance for Sunday review |
| `progress_report` | Track one session type's HR/pace trend over time |
| `detect_personal_records` | Scan cached runs for PRs at common distances |

### Form & terrain

| Tool | What it does |
|---|---|
| `running_form_trends` | Cadence, ground contact, vertical oscillation, stride trends |
| `elevation_impact` | Grade-adjusted (flat-equivalent) pace for a hilly run |

### Wellness / readiness

| Tool | What it does |
|---|---|
| `morning_check_in` | HRV, RHR, sleep, body battery, training readiness + 7-day trends |
| `get_wellness_history` | Multi-day wellness trends |
| `recovery_prediction` | Predicted days back to baseline HRV after a quality session |
| `illness_risk_check` | Early illness-onset signals from today's wellness |
| `sleep_performance_correlation` | Pre-run-night sleep vs performance, within one session class |
| `stress_training_balance` | Training load vs life stress over a window |

### Training load & periodization

| Tool | What it does |
|---|---|
| `training_load_balance` | Acute:chronic workload ratio (ACWR), rolling 7d/28d |
| `deload_check` | Whether the current week is a recovery/deload week (plan-aware) |
| `return_from_break` | Detect an inactivity gap and generate a ramp-back plan |
| `taper_plan` | Race taper schedule from today to race day |
| `double_day_advisor` | Whether to adopt double-threshold days (gated on base **or** informed opt-in) |

### Calculators

| Tool | What it does |
|---|---|
| `pace_calculator` | Convert between pace, speed, distance, duration |
| `plan_interval_session` | Interval structure + rep distances from profile paces |
| `forecast_conditions` | Weather (Open-Meteo) for a date/time; auto-locates from latest activity |
| `heat_pace_adjustment` | Adjust pace for heat + humidity (dew-point method) |
| `analyze_race_course` | GPX course analysis (sustained climbs/descents + short steep "wall" pitches) + grade-adjusted per-km race pacing for a goal (even-effort default, optional conservative-start negative-split) |

### Gear

| Tool | What it does |
|---|---|
| `list_gear` | Shoes and gear in rotation, with total mileage |
| `get_gear_for_activity` | Which gear was used on a specific activity |
| `shoe_wear_check` | Flag shoes nearing retirement by mileage |

### Profile setup

| Tool | What it does |
|---|---|
| `user_profile_status` | Check whether `user_profile.md` exists / is filled in |
| `init_user_profile` | Generate `user_profile.md` from structured inputs |
| `get_athlete_profile` | Parse current profile into structured fields for tools |
| `read_coach_doc` | Read a `coach://` framework doc as a tool call |

### Plan management

| Tool | What it does |
|---|---|
| `get_plan` | Read current `plan.json` |
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

### Race-course pacing from a GPX
```
> Analyze this course (course.gpx) and give me a 1:45 pacing plan
```
Claude calls `analyze_race_course`, profiles the course (sustained climbs
and short steep "wall" pitches detected at the point level, not just per-km
averages), and returns a grade-adjusted per-km plan that sums to the goal —
even-effort by default, with an optional conservative-start negative-split.
For hot races it can pull conditions via `forecast_conditions` and
heat-adjust the target (it'll ask your planned start time).

## Notes

**Background sync:** every MCP server start runs an incremental
`sync_activities` in a daemon thread — new activities (with HR streams,
laps, and per-activity detail) plus the trailing ~10 days of wellness, so
HRV/RHR/sleep stay current for the readiness tools. Cheap when nothing's
new (seconds), 45-60 s for a cold 12-week backfill. `new_activities: 0`
means "already up to date," not stale — the sync result returns
`cache_newest_activity` / `cache_newest_wellness` to confirm freshness.

**Zone calibration:** HR zones live in `coach_data/user_profile.md`. When
Garmin's zones change, update the table verbatim. Zone time is recomputed
from raw streams on every query, so updates apply retroactively.

**Plan persistence:** `coach_data/plan.json` is the source of truth for
intent. Garmin calendar is operational state. The two can drift if you
reschedule a lot — compliance comparison still works against the original
plan.

**Local-only:** runs on your machine. Not reachable from Claude on phone
or web without remote hosting (which is out of scope for this project).
