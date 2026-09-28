# AI-Driven Energy Supply-Chain Resilience System

Models **India's crude-oil import network as a risk-weighted knowledge graph**, keeps
corridor risk scores current from geopolitical news, and asks two questions: *which
maritime corridor can India least afford to lose right now?* and *what should it do
if that corridor is disrupted?*

B.E. major project — Department of Artificial Intelligence & Data Science,
Fr. Conceicao Rodrigues College of Engineering (FR. CRCE), 2026–27.

---

## What it does

1. **Ingests news** from GDELT (search API and 15-minute bulk files), OilPrice.com and
   gCaptain RSS, plus the OFAC sanctions list.
2. **Extracts structured events** with an LLM: corridor, actor, event type, severity (1–5),
   confidence.
3. **Scores each corridor's risk**: time-decayed, with syndicated copies of one story
   merged, and bounded so it measures how severe events are rather than how much news
   was collected.
4. **Updates the knowledge graph**: 12 supplier countries → 3 corridors (Hormuz, Red Sea,
   Cape of Good Hope) → 10 Indian ports → 23 refineries, with
   `effective_capacity = volume × (1 − risk)` on corridor edges.
5. **Ranks corridor criticality** by max-flow loss, in the static network and in the
   risk-weighted one. The difference between the two rankings is the core research output.
6. **Triggers a response** when a corridor crosses a threshold: supply gap → ranked
   alternative suppliers (multi-criteria) → replacement timeline → Strategic Petroleum
   Reserve (SPR) drawdown schedule (linear program).
7. **Serves everything over a REST API** to a React dashboard, and saves every run.

## Headline findings

From the cited snapshot (`PipelineRun 3`, 27 Sep 2026, 1,629 corridor-attributed events):

| Corridor | Risk score | Static rank | Risk-weighted rank | Flow lost if cut (mb/d) |
|---|---|---|---|---|
| Cape of Good Hope | 0.050 | 2 | **1** | 1.609 → **2.048** |
| Strait of Hormuz | **0.888** | 1 | **2** | 1.784 → 0.247 |
| Red Sea | 0.741 | 3 | 3 | 0.202 → 0.069 |

- **Current conditions reverse the ranking.** Hormuz is India's most structurally
  important corridor, but it is already ~89% impaired, so losing the rest of it removes
  little. The near-intact Cape route has become load-bearing.
- **Cape is also the hardest to replace.** Eligible alternative suppliers cover 63% of
  its 2.048 mb/d gap; the SPR covers 57.4% of the need over 14 days and runs dry on
  day 34 of a 60-day disruption.
- **43.2% of India's modelled crude deliverability** (4.228 → 2.400 mb/d) is impaired
  under current conditions.
- **Validated as a crisis detector.** Point-in-time backtests on the 2026 Hormuz closure
  and the 2025 Iran–Israel standoff show the risk signal rising at or before the first
  ≥5% Brent move, robust to the main scoring parameter. It does not predict prices or
  measure how deep a crisis is.

The full method, data, results and limitations are written up in the project's research
report (submitted separately; not kept in this repository). The measurements behind
every figure are in [docs/claude/](docs/claude/), starting with
[thesis-snapshot.md](docs/claude/thesis-snapshot.md) and
[phase-7-backtest.md](docs/claude/phase-7-backtest.md).

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, Django 6.0, Django REST Framework, django-cors-headers, django-environ |
| Database | PostgreSQL 16 + PostGIS 3.6 (GDAL / GEOS / PROJ) |
| Graph & maths | NetworkX, NumPy, pandas, SciPy (HiGHS solver for the SPR linear program) |
| LLM | DeepSeek V4 Flash via OpenRouter, using the OpenAI-compatible SDK |
| Orchestration | LangGraph |
| Ingestion | requests, feedparser |
| Frontend | React, Leaflet, Recharts (built separately by a teammate) |

There is **no Celery, Redis or scheduler**. The pipeline is run by hand with one command
(`python manage.py run_pipeline`), and the server is the only long-running process.

---

## Repository structure

```
energy-resilience/
├── README.md               this file
├── COMMANDS.md             every terminal command, tagged by cost and effect
├── API_DOCS.md             REST API reference for the frontend
├── CLAUDE.md               working notes for AI-assisted development
├── docs/
│   └── claude/             detailed design notes, history and measurements
├── backend/                Django project (all commands run from here)
│   ├── config/             settings, URLs, WSGI
│   ├── core/               models, serializers, views, URLs, admin,
│   │   └── management/commands/   all manage.py commands
│   ├── graph/              knowledge-graph builder, algorithms, risk updater, singleton
│   ├── pipeline/           ingest (GDELT, GKG, RSS, OFAC), LLM extraction, risk scoring
│   ├── criticality/        static vs risk-weighted ranking, cascade, named scenarios
│   ├── response/           threshold trigger, supply gap, reroute ranking, SPR LP, plan
│   ├── orchestrator/       LangGraph pipeline
│   ├── backtest/           historical validation against Brent prices
│   ├── data/               seed JSON, corridor geometries, backtest reports,
│   │                       API response samples, database fixture
│   └── tests/              580 automated tests
└── frontend/               React dashboard (maintained separately)
```

---

## Quick start

Prerequisites: Python 3.12, PostgreSQL 16 with PostGIS, and the GDAL/GEOS/PROJ libraries
(on Windows, the GDAL wheels from `cgohlke/geospatial-wheels`).

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
cd backend
pip install -r requirements.lock.txt   # exact tested versions (incl. GDAL wheels on Win/Py3.12)
copy .env.example .env          # fill in DB_*, SECRET_KEY and the GDAL paths

createdb -U postgres energy_resilience
psql -U postgres -d energy_resilience -c "CREATE EXTENSION postgis;"
python manage.py migrate
python -X utf8 manage.py loaddata data/fixtures/snapshot.json.gz

python manage.py runserver      # http://localhost:8000/api/
```

> **Load the fixture into an empty database and do not run `seed_db` first.**
> `seed_db` on its own leaves every risk score at 0.0, which gives a dashboard with
> every corridor green and no rank shift, with no warning. The fixture contains the
> seed data plus the scores, events and cited runs.

`OPENROUTER_API_KEY` is needed only for extraction (the one paid step) and
`EIA_API_KEY` only to refresh Brent prices; everything else runs without keys.

Frontend developers can build against the captured responses in
`backend/data/api_samples/` without installing any of this.

---

## Common commands

```powershell
python manage.py run_pipeline                  # analysis on stored scores (free)
python manage.py run_pipeline --full           # full cycle: ingest + extract (paid) + score
python manage.py run_criticality --port-view   # static vs risk-weighted ranking
python manage.py run_response --auto           # response plan for the triggered corridor
python manage.py run_backtest --event 2026_hormuz_closure
python manage.py test                          # 580 tests
```

See **[COMMANDS.md](COMMANDS.md)** for every command, flag, cost and side effect.

---

## API

15 endpoints under `/api/`, including risk scores and history, criticality, cascade,
scenarios, reroute, SPR, a live scenario simulator (`POST /api/simulate/`), corridor
GeoJSON, live events, pipeline runs and backtest reports. See **[API_DOCS.md](API_DOCS.md)**.

---

## Data sources

| Source | Used for |
|---|---|
| PPAC, IEA, EIA reports; trade statistics | Supplier volumes, corridor capacity, ports, refineries (static seed data) |
| GDELT DOC 2.0 API and GKG 2.0 bulk files | Live and historical news |
| OilPrice.com and gCaptain RSS | Energy and shipping news |
| OFAC SDN list | Sanctions |
| EIA Open Data API (Brent, RBRTE) | Backtest price series |
| OpenRouter (DeepSeek V4 Flash) | Event extraction, about $0.00003 per article |

---

## Status and limitations

All seven build phases are complete: data model and graph, ingestion, extraction and
scoring, criticality, response layer, orchestration and API, and backtest validation.

The main limitations, stated in full in the research report:

- Risk is treated as a physical closure fraction (a score of 0.888 is modelled as 88.8%
  of capacity unavailable), so absolute magnitudes are scenario estimates.
- The extractor rates a threat equally whether it is escalating or easing, and whether it
  is live or being reported after the fact.
- Differently worded headlines about the same event are not merged (semantic duplicates).
- Validation rests on two historical events, one permanently incomplete because GDELT has
  no data for 15 Jun – 1 Jul 2025.
- Spare capacity of alternative suppliers is estimated, so reroute coverage is indicative.

---

## Author

Joshua Vaz ([Josh-codes](https://github.com/Josh-codes)) — backend, data pipeline and
analysis. The React frontend is developed by a project teammate.
