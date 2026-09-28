# Energy Resilience API

REST API for the energy supply-chain resilience dashboard. Django REST
Framework, JSON in and out, served at `http://localhost:8000/api/`.

**Real responses for every route are in
[`backend/data/api_samples/`](backend/data/api_samples/)**, one file per call,
captured from the live database by `python manage.py capture_api_samples`.
Treat those files as the response contract. This page covers what the samples
can't show: parameters, units, status codes and the traps.

Each sample file is shaped like this:

```json
{
  "name": "risk_scores",
  "description": "...",
  "request": {"method": "GET", "url": "/api/risk-scores/"},
  "status": 200,
  "response": { ...exactly what the API returned... }
}
```

`index.json` lists every sample, when they were captured, and the live risk
scores at that moment, so you can tell which state of the world they describe.

---

## Two ways to work

1. **Against the samples only (no backend).** Point your API layer at the
   `response` field of each sample file. You need no Postgres, no PostGIS and
   no Python. This is the recommended way to start.
2. **Against a running backend.** See [Running the backend](#running-the-backend)
   at the bottom. It needs Postgres + PostGIS + GDAL, and it **must** be loaded
   from the committed fixture, not from `seed_db` alone (see the trap there).

---

## Conventions

| | |
|---|---|
| Base URL | `http://localhost:8000/api/` |
| CORS | `http://localhost:3000` is allowed (`CORS_ALLOWED_ORIGINS` in `.env`) |
| Auth | none |
| Volumes | **million barrels per day (mb/d)**; reserves in **million barrels (mb)** |
| Risk scores | 0.0–1.0 (1.0 = corridor reported closed) |
| Numbers | **unrounded** floats straight from the solver (e.g. `1.7839999999999998`); round for display, never for logic |
| Corridors | exactly three: `Hormuz`, `Red Sea`, `Cape`. **URL-encode `Red Sea`** (`Red%20Sea`). There is no `Suez`: it is folded into Red Sea, and asking for it returns 400. |
| Dates | ISO 8601; timestamps are UTC |
| Trailing slash | required on every route |

**`degradation` means two different things.** On `GET /api/cascade/` it is a
**percent** (1–100). On `POST /api/simulate/` it is a **fraction** (0–1], the
way a slider sends it. `/api/scenarios/` rows carry both `degradation_pct` and
`degradation`.

### Errors

| status | when | body |
|---|---|---|
| 400 | a parameter fails validation | DRF field errors, e.g. `{"corridor": ["This field is required."]}` |
| 400 | valid shape, bad domain value (e.g. corridor `Suez`, unknown backtest key) | `{"error": "..."}` |
| 404 | missing pipeline run, or a backtest that hasn't been run | `{"error": "..."}` |
| 503 | the database isn't loaded, so the graph can't be built | `{"detail": "..."}` |
| 500 | anything else (logged on the server) | `{"error": "internal error - see server log"}` |

Samples: `error_*.json`.

### How fresh is the data?

Everything except `POST /api/simulate/` reads pre-computed state, which only
changes when someone runs the pipeline from the command line. The frontend
never triggers it. Polling every 60 s is harmless but will almost always return
the same thing. Graph-backed endpoints compute in 0–30 ms.

---

## Endpoints

### Risk

#### `GET /api/risk-scores/`
Live risk score per corridor: `{"Cape": 0.05, "Hormuz": 0.888, "Red Sea": 0.741}`.
Sample: `risk_scores.json`.

#### `GET /api/risk-scores/history/`
Scoring runs, oldest first, for trend charts.

| param | type | default | |
|---|---|---|---|
| `corridor` | string | all | |
| `days` | int 1–3650 | all | only rows from the last N days |

Rows: `id, corridor, score, raw_score, computed_at, formula`.
**Plot one `formula` at a time.** The scoring formula changed on 2026-09-26:
rows labelled `sum_saturating` (before) and `top3pad` (after) are on different
scales, and `raw_score` means different things in each. For the dashboard, show
`top3pad` rows only.
Sample: `risk_scores_history_hormuz.json`.

### Criticality

#### `GET /api/criticality/`
One row per corridor, comparing the static (no-risk) network with the
risk-weighted one. Key fields: `corridor, static_rank, risk_rank, rank_shift,
capacity_loss_mbd, static_capacity_loss_mbd, centrality, static_centrality,
live_risk_score`.

| param | values | default |
|---|---|---|
| `rank_by` | `capacity_loss`, `centrality` | `capacity_loss` |

Rank by capacity loss (mb/d lost if the corridor is cut). The centrality
ranking is a diagnostic: it flips with small score changes, so don't headline it.
Samples: `criticality.json`, `criticality_by_centrality.json`.

#### `GET /api/criticality/ports/`
Per-port view: how much each port depends on each corridor after the other
corridors back it up, and how much crude is stranded there.
Sample: `criticality_ports.json`.

#### `GET /api/cascade/`
Flow lost as one corridor degrades further, in 10% steps.

| param | type | default |
|---|---|---|
| `corridor` | string, **required** | |
| `degradation` | int **percent** 1–100 | 100 (show every step) |

Rows: `degradation_pct, flow_after_mbd, capacity_loss_mbd, affected_refineries,
affected_count`. `affected_refineries` is a list of **objects**
(`refinery, baseline_mbd, current_mbd, pct_of_baseline, shortfall_mbd`), not
names. A refinery is listed once it falls below 95% of its own baseline intake.
The baseline is today's risk-weighted network. These losses are extra flow lost
on top of current risk, so they are not comparable with
`/api/criticality/`'s `static_capacity_loss_mbd`.
Sample: `cascade_hormuz.json`.

#### `GET /api/scenarios/`
Named scenarios (`hormuz_30`, `hormuz_full`, `red_sea`, `opec_cut`). Each row
has a `key`, a `mechanism` (`corridor` or `supply`), `degradation_pct` and
`degradation`. For `opec_cut`, `degradation` is `null` and `degradation_pct`
is **absent**, because it is a supply shock (`supply_reduction_mbd`), not a
corridor cut. Keys also vary by scenario (`red_sea` has
`transit_day_increase`), so read them defensively. `price_impact_usd` is an external estimate carried as
metadata, not model output.
Sample: `scenarios.json`.

### Response

#### `GET /api/reroute/`
Alternative suppliers that avoid a corridor, ranked best first.

| param | type | default |
|---|---|---|
| `corridor` | string, **required** | |
| `crisis` | `normal`, `severe` | `normal` |
| `gap_mbd` | float ≥ 0 | none (adds cumulative coverage when given) |
| `include_sanctioned` | bool | `false` |

Rows: `source, score, cost_score, transit_score, compat_score, transit_days,
price_premium`, …
Scores are normalised over the candidates in **this** response. Don't compare
them across corridors or between `include_sanctioned` on and off.
Sample: `reroute_cape.json`.

#### `GET /api/spr/`
Strategic Petroleum Reserve drawdown for a constant supply gap.

| param | type | |
|---|---|---|
| `gap_mbd` | float ≥ 0, **required** | |
| `duration_days` | int 1–999, **required** | |
| `transit_days` | int ≥ 0, **required** | days until replacement cargo arrives |

Key fields: `daily_schedule` (mb/d released per day), `total_released_mb`,
`insufficient`, `gap_covered_pct`, `unmet_mbd`, `days_until_threshold` (an alias
of `days_of_cover`).
Sample: `spr.json`.

#### `POST /api/simulate/`
The scenario slider. Returns the cascade curve plus the full response (gap →
reroute ranking → replacement timeline → SPR) at the **exact** degradation
requested. It computes on a copy and writes nothing.

Body (JSON), with **exactly one** of `corridor` / `scenario`:

| field | type | default |
|---|---|---|
| `corridor` | string | |
| `scenario` | scenario key | |
| `degradation` | float **fraction** (0, 1] | 1.0 |
| `duration_days` | int 1–365 | 14 |
| `crisis` | `normal`, `severe` | `normal` |
| `include_sanctioned` | bool | `false` |

Response: `{input, cascade, gap, reroute, timeline, spr}`.
Samples: `simulate_cape_full.json`, `simulate_scenario_hormuz_30.json`.

### Map and events

#### `GET /api/corridors/geojson/`
A GeoJSON `FeatureCollection` of the three corridor routes (LineStrings,
lon/lat) for Leaflet. `properties`: `name, risk_score, baseline_risk,
capacity_mbd, capacity_unlimited, transit_days`. **Cape has
`capacity_mbd: null` with `capacity_unlimited: true`**, since it has no
chokepoint.
Sample: `corridors_geojson.json`.

Not available yet: port / refinery GeoJSON, and routes for alternative
suppliers (`route_geometry` is empty for every one), so the map can't draw
reroute lines. Ask if you need them.

#### `GET /api/events/live/`
Latest extracted news events, newest first.

| param | type | default |
|---|---|---|
| `corridor` | string | all |
| `limit` | int 1–500 | 50 |

Rows: `id, corridor, actor, event_type, severity (1–5), confidence (0–1),
timestamp, title, article_url`. `corridor` can be `null` (an oil-market story
tied to no corridor).
Sample: `events_live.json`.

### Pipeline runs

Every pipeline run is stored with its full outputs. **Run 3 is the one the
thesis cites** (`pipeline_run_snapshot.json`).

| route | |
|---|---|
| `GET /api/pipeline/latest/` | newest run, every field (404 if none) |
| `GET /api/pipeline/runs/?limit=` | summaries, newest first (limit 1–200, default 20) |
| `GET /api/pipeline/runs/<id>/` | one run, every field (404 if missing) |

A full run carries `risk_scores`, `criticality`, `threshold` (which corridor
triggered a response, and why) and `response` (`gap`, `reroute`, `timeline`,
`spr`), so a "last run" panel can be built from this one call.
Samples: `pipeline_latest.json`, `pipeline_runs.json`, `pipeline_run_snapshot.json`.

### Backtest

#### `GET /api/backtest/`

| param | | |
|---|---|---|
| none | | list of defined events with their verdict (`null` if not yet run) |
| `event` | `2026_hormuz_closure`, `2025_iran_standoff` | that event's report (404 if not yet run, 400 if unknown) |
| `series` | bool, default `true` | `false` drops the daily series |

A report holds the verdict fields (`validation`, `signal_elevated_at`,
`price_spiked_at`, `lead_time_days`, `max_risk_score`, `brent_spike_pct`, …)
and a `series` with one row per day: `date, scores{corridor}, brent_usd`
(`null` on non-trading days), `stories`, `top_stories`, …

**Gap flags:**
- `day_sampled: false` means there was no source data that day, so the score is
  just older news decaying. Grey it out, or don't draw it.
- `unsampled_days_in_lookback > 0` means the score is a lower bound.
- The report-level `gaps` array lists the ranges.

This matters for `2025_iran_standoff`: GDELT has no data for 2025-06-15 to
07-01, and the smooth decline across those days is an artefact, not the signal
falling.
Samples: `backtest_list.json`, `backtest_2026_verdict.json`,
`backtest_2026_full.json`, `backtest_2025_full.json`.

---

## Running the backend

Prerequisites: Python 3.12, PostgreSQL 16 with PostGIS, and the GDAL/GEOS/PROJ
libraries. The models are geospatial, so there is no SQLite fallback. On
Windows the GDAL wheels install into the venv; point `GDAL_LIBRARY_PATH`,
`GEOS_LIBRARY_PATH` and `PROJ_LIB` in `.env` at **your** checkout's venv (see
`.env.example`).

```powershell
cd backend
cp .env.example .env            # fill in DB_* and SECRET_KEY; API keys can stay empty
python manage.py migrate
python -X utf8 manage.py loaddata data/fixtures/snapshot.json.gz   # -X utf8: headlines hold non-ASCII text
python manage.py runserver      # http://localhost:8000/api/
python manage.py createsuperuser   # optional: read-only browser at /admin/
```

**⚠ Load the fixture into an empty database, and don't run `seed_db`.**
`seed_db` alone produces a silently wrong world: it never sets the live risk
scores, so every corridor reads `0.0`, `/api/risk-scores/` shows everything
green, and `/api/criticality/` shows `rank_shift: 0` everywhere, i.e. a
dashboard without the thesis finding in it. Nothing warns you. Running
`seed_db` *before* `loaddata` also collides on primary keys. The fixture
contains everything `seed_db` loads, plus the scores, events and runs.

**Don't run `score_risk` or `run_pipeline --full`.** Both write new risk
scores, which moves what every endpoint returns away from the samples and
from the thesis figures. Plain `run_pipeline` (no flags) is free and safe.
Nothing the frontend does can trigger either.
