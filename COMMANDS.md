# Command Reference

Every terminal command for the Energy Supply-Chain Resilience backend, grouped by
task. Commands are written for **Windows PowerShell** and, unless stated otherwise,
are run **from the `backend/` folder with the virtual environment active**.

## Read this first: what each command costs and changes

Every command below is tagged with one of these:

| Tag | Meaning |
|---|---|
| **READ-ONLY** | Free. Reads the database, changes nothing. Run any time. |
| **FREE, WRITES** | Free, but writes to the database or to files. Safe, but know what it writes. |
| **DOWNLOADS** | Free, but pulls large amounts of data from GDELT (hundreds of MB to GB). |
| **PAID** | Calls the LLM through OpenRouter and costs money (small: about $0.00003 per article). |
| **MOVES SCORES** | Writes new risk scores. Everything the API and CLI show changes afterwards. |

**The cited thesis figures are safe from every command.** They live in
`PipelineRun` 3, 4 and 5, which are permanent. "MOVES SCORES" only changes the
*current* live scores; after running one, read thesis figures from the pinned
runs (see [Checking the database](#checking-the-database)), not from
`run_criticality` / `run_response`.

---

## Contents

1. [Opening a terminal](#1-opening-a-terminal)
2. [First-time setup on a new machine](#2-first-time-setup-on-a-new-machine)
3. [Running the server](#3-running-the-server)
4. [The whole pipeline in one command](#4-the-whole-pipeline-in-one-command)
5. [The pipeline stage by stage](#5-the-pipeline-stage-by-stage)
6. [Analysis: criticality and response](#6-analysis-criticality-and-response)
7. [Backtests](#7-backtests)
8. [Frontend handoff and database fixture](#8-frontend-handoff-and-database-fixture)
9. [Tests](#9-tests)
10. [Checking the database](#10-checking-the-database)
11. [Graph and diagnostics](#11-graph-and-diagnostics)
12. [One-off maintenance](#12-one-off-maintenance)
13. [Troubleshooting](#13-troubleshooting)
14. [Recipes](#14-recipes)

---

## 1. Opening a terminal

```powershell
cd C:\Joshua\energy-resilience\backend
..\venv\Scripts\Activate.ps1          # prompt now starts with (venv)
```

If PowerShell refuses to run the activation script, allow scripts for your user once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## 2. First-time setup on a new machine

Needs Python 3.12, PostgreSQL 16 with PostGIS, and the GDAL/GEOS/PROJ libraries
(on Windows, the GDAL wheels from `cgohlke/geospatial-wheels` install them into the venv).

```powershell
cd <repo>
python -m venv venv
.\venv\Scripts\Activate.ps1
cd backend
pip install -r requirements.txt

copy .env.example .env                # then edit .env: DB_*, SECRET_KEY, and the
                                      # GDAL/GEOS/PROJ paths for YOUR checkout
```

Create the database (once), then load the committed snapshot:

```powershell
createdb -U postgres energy_resilience
psql -U postgres -d energy_resilience -c "CREATE EXTENSION postgis;"

python manage.py migrate
python -X utf8 manage.py loaddata data/fixtures/snapshot.json.gz
```

**Do not run `seed_db` before `loaddata`.** The fixture already contains everything
`seed_db` loads, and running `seed_db` first makes `loaddata` fail on duplicate
corridor names. On its own, `seed_db` produces a database where every risk score
is 0.0, so the dashboard shows every corridor green and no rank shift, with no warning.

`-X utf8` is required on Windows: headlines contain characters the default
Windows encoding cannot write.

Optional, for the admin site at `/admin/`:

```powershell
python manage.py createsuperuser
```

---

## 3. Running the server

```powershell
python manage.py runserver            # API at http://localhost:8000/api/
```

That is the only process needed. There is no Celery, Redis or scheduler: the
pipeline is run by hand with the commands below. Endpoints are documented in
[API_DOCS.md](API_DOCS.md).

---

## 4. The whole pipeline in one command

`run_pipeline` chains every stage through the LangGraph orchestrator and saves the
result as one `PipelineRun` row.

| Command | Tag | What it does |
|---|---|---|
| `python manage.py run_pipeline` | FREE, WRITES | **Analysis only** on the stored scores: graph → criticality → threshold → response. Writes one `PipelineRun`. Does not change scores. |
| `python manage.py run_pipeline --full` | PAID, MOVES SCORES, DOWNLOADS | The whole cycle: ingest RSS + GDELT (falls back to GKG bulk files on the first rate-limit), extract, score, analyse. About $0.025 and ~40 minutes. |
| `python manage.py run_pipeline --duration-days 30` | FREE, WRITES | Analysis with a 30-day SPR horizon. Use this to pin another cut as its own run and cite its id. |
| `python manage.py run_pipeline --no-persist` | READ-ONLY | Analysis without saving a `PipelineRun`. |

Pick stages yourself instead of `--full`:

```powershell
python manage.py run_pipeline --ingest rss,gkg --extract --score
```

All options:

| Flag | Meaning |
|---|---|
| `--full` | Same as `--ingest rss,gdelt-fallback --extract --score` |
| `--ingest LIST` | Comma-separated sources: `rss`, `gkg`, `gdelt`, `gdelt-fallback` |
| `--last-minutes N` | Ingest window (default 1440 = 24 h) |
| `--max-records N` | GDELT DOC articles per corridor (default 50) |
| `--extract` | Run LLM extraction (PAID) |
| `--extract-limit N` | Cap the number of articles extracted in this run |
| `--score` | Re-score the corridors (MOVES SCORES) |
| `--crisis normal\|severe` | Reroute weighting (default `normal`) |
| `--duration-days N` | SPR planning horizon (default 14) |
| `--include-sanctioned` | Allow sanctioned alternative suppliers |
| `--no-persist` | Do not write a `PipelineRun` row |

---

## 5. The pipeline stage by stage

The careful path, when you want to see and control each step.

### 5.1 Ingest news — FREE, WRITES (DOWNLOADS for `gkg`)

```powershell
python manage.py poll_sources --corridor Hormuz
python manage.py poll_sources --corridor "Red Sea"     # quotes: the name has a space
python manage.py poll_sources --corridor Cape
```

GDELT's search API rate-limits per IP address, so poll **one corridor at a time,
a few minutes apart**. If a corridor reports `NOT SAMPLED`, wait longer; retrying
immediately extends the block.

| Command | What it does |
|---|---|
| `python manage.py poll_sources` | All of: GDELT DOC (all corridors), RSS, OFAC. Never pulls GKG. |
| `python manage.py poll_sources --source rss` | OilPrice.com + gCaptain RSS only. Seconds, never throttled. Poll often: feeds hold only ~2 days. |
| `python manage.py poll_sources --source ofac` | Refresh the OFAC sanctions list |
| `python manage.py poll_sources --source gdelt` | GDELT DOC API, all three corridors |
| `python manage.py poll_sources --source gkg` | GDELT bulk files instead of the API: ~280 MB per 24 h, **cannot be throttled**. Use when the API refuses you. |
| `python manage.py poll_sources --source gkg --last-minutes 240` | Bulk files for the last 4 hours only (must be more than 90) |

| Flag | Meaning |
|---|---|
| `--source gdelt\|rss\|ofac\|gkg\|all` | Which source (default `all`) |
| `--corridor Hormuz\|"Red Sea"\|Cape` | One GDELT corridor, one request |
| `--last-minutes N` | How far back to look (default 1440; multiple of 15). Widen it after a gap in polling, e.g. `4320` for 3 days. |
| `--max-records N` | Articles per corridor (default 50, GDELT maximum 250) |

### 5.2 Extract events — PAID

```powershell
python manage.py extract_events
python manage.py extract_events --limit 20            # at most 20 articles
python manage.py extract_events --workers 2           # fewer parallel calls
```

The only step that costs money. **Poll all three corridors before extracting**, or
the corpus is lopsided. `--workers` (default 4) is how many LLM calls run at once:
same cost, less waiting. If "call failures" rise, the provider is rate-limiting you;
re-run with fewer workers. Failed articles stay pending and are retried on the next
run. Safe to stop with Ctrl+C.

Test the LLM on a single stored article (one paid call, writes nothing):

```powershell
python manage.py test_extraction                       # oldest unprocessed article
python manage.py test_extraction --url "https://..."   # a specific stored article
```

### 5.3 Score — FREE, MOVES SCORES

```powershell
python manage.py score_risk
```

Recomputes every corridor's risk score from the stored events, writes a `RiskScore`
row per corridor, and updates the live scores and the graph. No API calls.

---

## 6. Analysis: criticality and response

Both commands read the **current live scores** and write nothing to the database.
They reproduce the thesis figures only while nothing has re-scored since Run 3.

### 6.1 Criticality — READ-ONLY

```powershell
python manage.py run_criticality                      # static vs risk-weighted ranking
python manage.py run_criticality --port-view          # + per-port breakdown
python manage.py run_criticality --corridor Hormuz --cascade
python manage.py run_criticality --scenario hormuz_full
python manage.py run_criticality --rank-by centrality # alternative ranking key (diagnostic)
python manage.py run_criticality --ignore-risk        # structure only, no risk applied
```

| Flag | Meaning |
|---|---|
| `--rank-by capacity_loss\|centrality` | Ranking key (default `capacity_loss`) |
| `--port-view` | Per-port residual criticality and stranded crude |
| `--corridor X --cascade` | 10%-step cascade simulation for one corridor |
| `--scenario KEY` | `hormuz_30`, `hormuz_full`, `red_sea`, `opec_cut` |
| `--no-rebuild` | Reuse the in-memory graph instead of rebuilding it |
| `--ignore-risk` | Do not apply stored risk (both rankings then read the same world) |

### 6.2 Response plan — READ-ONLY

```powershell
python manage.py run_response --auto                  # respond to what the trigger picks
python manage.py run_response --corridor Hormuz --crisis severe
python manage.py run_response --corridor Cape --duration-days 60   # shows the SPR running dry
python manage.py run_response --scenario opec_cut
python manage.py run_response --corridor Hormuz --include-sanctioned
python manage.py run_response --corridor "Red Sea" --degradation 50
```

Prints trigger → supply gap → ranked alternative suppliers → SPR schedule.
Choose exactly one of `--auto`, `--corridor` or `--scenario`.

| Flag | Meaning |
|---|---|
| `--degradation N` | Percent, on top of today's risk (default 100) |
| `--crisis normal\|severe` | Reroute weighting |
| `--duration-days N` | SPR horizon (default 14) |
| `--include-sanctioned` | Keep sanctioned alternatives |
| `--no-rebuild`, `--ignore-risk` | As for `run_criticality` |

---

## 7. Backtests

Replays the system over a past crisis and compares the risk score with Brent prices.
Two events are defined: `2026_hormuz_closure` and `2025_iran_standoff`.

| Command | Tag | What it does |
|---|---|---|
| `python manage.py run_backtest --list` | READ-ONLY | The defined events and their verdicts |
| `python manage.py run_backtest --event 2026_hormuz_closure --status` | READ-ONLY | Days pulled, days missing, articles still to extract |
| `python manage.py run_backtest --event 2026_hormuz_closure --pull` | DOWNLOADS | Pull the historical GDELT files (~280 MB per day; ~12 GB and ~1 h for this event). Resumable: re-run to retry only missing days. |
| `python manage.py extract_events` | PAID | Extract the pulled articles (the normal extraction command) |
| `python manage.py run_backtest --event 2026_hormuz_closure` | FREE, WRITES | Score every day, compare with Brent, print the verdict, write `data/backtests/<event>.json`. Writes **no** risk scores. |
| `python manage.py run_backtest --event 2026_hormuz_closure --top-k 5` | FREE, WRITES | Sensitivity run: average 5 stories instead of 3. Writes `<event>.top5.json`, never the cited report. |

| Flag | Meaning |
|---|---|
| `--event KEY` | Which event |
| `--pull` | Download the historical files |
| `--day YYYY-MM-DD` | With `--pull`: only this day (repeatable) |
| `--force` | With `--pull`: re-download days already marked OK |
| `--workers N` | With `--pull`: parallel downloads (default 4) |
| `--no-write` | Score and print, but do not write the report |
| `--top-k N` | Sensitivity run with N stories |

Full order for a new event: `--pull` → `--status` → `extract_events` → score.
Commit the reports in `data/backtests/` afterwards.

---

## 8. Frontend handoff and database fixture

| Command | Tag | What it does |
|---|---|---|
| `python manage.py capture_api_samples` | FREE, WRITES (files) | Save one real response per API route to `data/api_samples/`. Writes nothing to the database. |
| `python manage.py capture_api_samples --run-id 3` | FREE, WRITES (files) | Same, choosing which `PipelineRun` is the snapshot sample (default 3) |

Dump the database to the committed fixture (only when re-basing the thesis onto a
new run):

```powershell
New-Item -ItemType Directory -Force data\fixtures
python -X utf8 manage.py dumpdata core --exclude core.RawArticle --indent 2 -o data/fixtures/snapshot.json.gz
```

Prove the fixture restores, on a throwaway database (your real one is untouched):

```powershell
createdb -U postgres er_fixture_check
psql -U postgres -d er_fixture_check -c "CREATE EXTENSION postgis;"
$env:DB_NAME = "er_fixture_check"
python manage.py migrate
python -X utf8 manage.py loaddata data/fixtures/snapshot.json.gz
python manage.py shell -c "from core.models import Corridor, PipelineRun as P; print(dict(Corridor.objects.values_list('name','live_risk_score')), P.objects.get(pk=3).status)"
Remove-Item Env:DB_NAME                # IMPORTANT: switch back to the real database
dropdb -U postgres er_fixture_check
```

Expect Hormuz ≈ 0.888, Red Sea ≈ 0.741, Cape 0.05 and `succeeded`.

---

## 9. Tests

```powershell
python manage.py test                           # full suite (580 tests)
python manage.py test tests.test_backtest       # one module
python manage.py test tests.test_api.CaptureApiSamplesTests   # one class
```

READ-ONLY for your data: tests run in a separate temporary database and never touch
the network or the paid API. A `RuntimeError: disk full` traceback during the
backtest tests is deliberate (a test simulates it); only the final `OK` matters.

---

## 10. Checking the database

All READ-ONLY.

Current live risk scores:

```powershell
python manage.py shell -c "from core.models import Corridor; print(dict(Corridor.objects.values_list('name','live_risk_score')))"
```

The cited runs (should show identical risk scores for 3, 4 and 5):

```powershell
python manage.py shell -c "from core.models import PipelineRun as P; [print(p.pk, p.options['duration_days'], p.options.get('score'), p.risk_scores) for p in P.objects.filter(pk__in=(3,4,5))]"
```

Everything in the snapshot run, as JSON (swap `criticality` for `risk_scores`,
`threshold` or `response`):

```powershell
python manage.py shell -c "import json; from core.models import PipelineRun; print(json.dumps(PipelineRun.objects.get(pk=3).criticality, indent=2))"
```

Recent runs:

```powershell
python manage.py shell -c "from core.models import PipelineRun as P; [print(p.pk, p.started_at, p.status, p.triggered_corridor) for p in P.objects.all()[:10]]"
```

Articles still waiting for extraction:

```powershell
python manage.py shell -c "from core.models import RawArticle; print(RawArticle.objects.filter(processed=False).count())"
```

Or browse at `http://localhost:8000/admin/` (read-only) after `createsuperuser`.

---

## 11. Graph and diagnostics

| Command | Tag | What it does |
|---|---|---|
| `python manage.py build_graph` | READ-ONLY | Build the graph from the database and print its summary and corridor cuts |
| `python manage.py build_graph --no-persist` | READ-ONLY | Same, without keeping it in memory |
| `python manage.py visualize_graph` | FREE, WRITES (file) | Draw the network to `graph/graph.png` (`--out`, `--dpi`, `--width`, `--height`) |
| `python manage.py compare_scoring` | READ-ONLY | Re-score the stored events under 8 candidate formulas and compare them |
| `python manage.py compare_scoring --skip-criticality` | READ-ONLY | Same, without the per-formula criticality table |
| `python manage.py compare_scoring --scanned-per-day 61000` | READ-ONLY | Use a true articles-scanned denominator for the share formula |

`compare_scoring --as-of <timestamp>` exists but still admits events dated after
that time, so it does **not** reproduce historical figures.

---

## 12. One-off maintenance

| Command | Tag | What it does |
|---|---|---|
| `python manage.py seed_db` | FREE, WRITES | Load the static network (suppliers, corridors, ports, refineries, alternatives) from `data/*.json`. Leaves risk scores at 0.0; use the fixture instead on a new machine. |
| `python manage.py seed_db --flush` | FREE, WRITES | Delete and reload the seeded tables |
| `python manage.py backfill_event_timestamps --dry-run` | READ-ONLY | Show events whose dates can be repaired from their article's GDELT timestamp |
| `python manage.py backfill_event_timestamps` | FREE, WRITES | Apply that repair |
| `python manage.py migrate` | FREE, WRITES | Apply database schema migrations |
| `python manage.py makemigrations --check` | READ-ONLY | Confirm models and migrations are in sync |
| `python manage.py check` | READ-ONLY | Django's configuration check |

---

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not find the GDAL library` | Set `GDAL_LIBRARY_PATH`, `GEOS_LIBRARY_PATH` and `PROJ_LIB` in `.env` to your venv's `osgeo` folder |
| `429 Too Many Requests` / corridor `NOT SAMPLED` | GDELT is rate-limiting. Wait several minutes, poll one corridor at a time, or use `--source gkg` |
| `'charmap' codec can't encode character` | Add `-X utf8` after `python` |
| Every corridor 0.0 / all `rank_shift` 0 | Database seeded without the fixture. Load `data/fixtures/snapshot.json.gz` into an empty database |
| `loaddata` fails on duplicate names | You ran `seed_db` first. Start from an empty database: `migrate`, then `loaddata` |
| Backtest verdict `INCOMPLETE` | Run `--status`. Either days are missing (re-run `--pull`) or articles are unextracted (`extract_events`). For `2025_iran_standoff` it is permanent: GDELT has no data for 15 Jun – 1 Jul 2025 |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |

---

## 14. Recipes

**Demo the system (free, changes nothing that matters):**

```powershell
python manage.py runserver                             # terminal 1
python manage.py run_pipeline                          # terminal 2: analysis-only run
python manage.py run_response --auto
```

**Add a day to the live risk time series (paid, ~$0.025, ~40 min):**

```powershell
python manage.py run_pipeline --full
```

**Careful manual refresh (paid only at step 3):**

```powershell
python manage.py poll_sources --source rss
python manage.py poll_sources --source gkg               # or --corridor, one at a time
python manage.py extract_events
python manage.py score_risk
python manage.py run_pipeline                           # persist the new analysis as a run
```

**Regenerate the frontend samples after a re-score:**

```powershell
python manage.py capture_api_samples
```
