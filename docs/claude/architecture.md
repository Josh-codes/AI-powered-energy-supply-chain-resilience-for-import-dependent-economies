# Architecture, file map and tech stack

> Read when you need the pipeline/component diagrams, which file owns what, the repo layout, or why Celery is absent.
>
> Split out of CLAUDE.md on 2026-09-28, text moved verbatim. CLAUDE.md holds the current summary and wins where the two differ.

> Section map (cross-references in this text still use the old CLAUDE.md section names): **Thesis Snapshot** → `thesis-snapshot.md` · **Threshold Trigger Logic / Key Algorithms / Extraction Prompt** → `algorithms.md` · **REST API Endpoints / Models / Request cycles** → `models-and-api.md` · **Development Commands / Common Errors** → `operations.md` · **Named Corridors / Scenarios / Refinery / Alternatives / Data Sources** → `data-reference.md` · **Phase 1–3, 2.5, 2.6** → `history-phases-1-3.md` · **Phase 4, 4.5, 5, 6** → `history-phases-4-6.md` · **Phase 7 / Backtest** → `phase-7-backtest.md` · **Architecture / File map / Repo structure / Tech stack / Celery** → `architecture.md`.

---

## System Architecture

### Overview
This system has six pipeline layers that run sequentially via LangGraph orchestration.
The pipeline is designed to run automatically every 6 hours via Celery Beat in a
production deployment. **Celery Beat / Celery worker / Redis are NOT being implemented
for this thesis build** — there is no always-on host to run them, and the project is
demonstrated by triggering each stage manually.

**`manage.py run_pipeline` (Phase 6) chains the stages through the LangGraph
orchestrator in one synchronous process.** A bare run is ANALYSIS ONLY on the
stored scores (update_graph → criticality → threshold → response → persist a
`PipelineRun`), so it is free and leaves the Thesis Snapshot untouched. The data
stages are opt-in: `--full` = `--ingest rss,gdelt-fallback --extract --score`.
The four individual commands still work and remain the careful path:

```bash
python manage.py poll_sources --corridor Hormuz   # repeat per corridor, minutes apart
python manage.py extract_events                   # the only step that costs money
python manage.py score_risk
python manage.py run_criticality --port-view
```
The `CELERY_BEAT_SCHEDULE` config below is retained as documentation of the intended
production cadence only. See the note in the Celery Configuration section.
The frontend never triggers the pipeline — it only reads pre-computed results from PostgreSQL.
The only real-time computation triggered by the frontend is scenario simulation via NetworkX.

---

### Full Pipeline Flow

[External Sources]
│
├── GDELT API (one query/corridor) ─────────────┐
├── OilPrice.com RSS ───────────────────────────┤   [MANUAL trigger — Celery
├── gCaptain RSS ───────────────────────────────┤──► Beat not implemented;
└── OFAC SDN CSV ───────────────────────────────┘    6h cadence is aspirational]
│
[Ingest Service]
feedparser + requests
URL deduplication
GDELT window: lastminutes:1440
│
▼
[PostgreSQL - RawArticle]
Temporary staging table
Deleted after 14 days  ← NOT IMPLEMENTED YET
│
▼
[LLM Event Extraction]
OpenRouter (OpenAI-compatible)
deepseek/deepseek-v4-flash-0731
response_format: json_object
Returns: corridor, actor,
event_type, severity,
confidence, is_relevant
│
▼
[PostgreSQL - ExtractedEvent]
Permanent store
Never deleted
Indexed by corridor + timestamp
timestamp = GDELT seendate
(NOT ingest time — see Phase 3)
│
┌─────────────────────────────┘
│
▼
[Risk Scoring Service]
numpy + pandas
Per event: severity × confidence × e^(-0.1 × Δt)
Per corridor: zero-padded mean of the
3 strongest STORIES (not a sum —
a sum tracked sampling depth, see
Phase 4.5), bounded by 5.0
score = baseline + (1-baseline)×(raw/5)
│
┌───────────────┴────────────────┐
│ │
▼ ▼
[PostgreSQL - RiskScore] [NetworkX Knowledge Graph]
Permanent store In-memory singleton
Timestamped Edge weights updated:
Used for backtest effective_capacity =
Used for trend charts volume × (1 - risk_score)
│
┌─────────────────────────────── ┘
│ Also fed by:
│ PPAC/IEA/EIA static data
│ (loaded once at startup from data/*.json)
▼
[Criticality Engine]
NetworkX algorithms
├── structural_betweenness (unweighted)
├── capacity_weighted_betweenness (1/capacity
│   distance — NOT weight='effective_capacity')
├── Max-flow analysis (baseline vs risk-weighted)
├── Residual/redundancy-aware port criticality
└── Cascading failure simulation (10% increments)
│
▼
[Threshold Trigger]  response/trigger.py
condition: capacity_loss_mbd >
  15% of risk-weighted baseline flow
OR live_risk_score > 0.75
← re-specified in Phase 5; the original
  centrality>0.65 AND risk>0.50 never fired
│
┌─────────┴──────────┐
[No] │ │ [Yes]
│ ▼
│ [Response Layer]
│ ├── Reroute Optimizer
│ │ scipy MCDM scoring
│ │ Score = 0.40×cost + 0.35×transit + 0.25×compat
│ │ Returns ranked alternatives
│ │
│ └── SPR Drawdown Model
│ linear program (scipy HiGHS)
│ Minimizes unmet gap, then peak shortfall
│ Subject to physical constraints
│
└─────────┬──────────┘
│
▼
[PostgreSQL - Results]
Risk scores, criticality rankings,
reroute recommendations,
SPR schedules — all stored
│
▼
[Django REST Framework]
Reads pre-computed results
Serializes to JSON
Serves to frontend via REST API
│
▼
[React Frontend - separate]
Reads from REST API only
Never triggers pipeline
Leaflet.js — corridor map
Recharts — charts + analytics


---

### Component Architecture

┌─────────────────────────────────────────────────────────────┐
│ FRONTEND │
│ React + Leaflet.js + Recharts │
│ http://localhost:3000 │
│ (teammate builds this — do not modify) │
└──────────────────────┬──────────────────────────────────────┘
│ HTTP REST / JSON
│ GET /api/risk-scores/
│ GET /api/criticality/
│ GET /api/cascade/
│ GET /api/reroute/
│ GET /api/spr/
│ GET /api/corridors/geojson/
│ GET /api/events/live/
│ POST /api/simulate/
▼
┌─────────────────────────────────────────────────────────────┐
│ API GATEWAY │
│ Django REST Framework │
│ http://localhost:8000 │
│ gunicorn (production) │
│ django-cors-headers (allow localhost:3000) │
└──────────────────────┬──────────────────────────────────────┘
│
┌────────────┼────────────────────┐
│ │ │
▼ ▼ ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐
│ Read from │ │ Trigger │ │ In-memory graph │
│ PostgreSQL │ │ NetworkX │ │ singleton │
│ (pre-comp) │ │ (real-time │ │ graph/state.py │
│ │ │ scenario) │ │ │
└──────┬──────┘ └──────┬──────┘ └──────────┬──────────┘
│ │ │
└───────────────┴────────────────────┘
│
▼
┌─────────────────────────────────────────────────────────────┐
│ BACKEND SERVICES │
│ │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐ │
│ │ Ingest │ │ Extraction │ │ Risk Scoring │ │
│ │ Service │ │ Service │ │ Service │ │
│ │ │ │ │ │ │ │
│ │ gdelt.py │ │ extractor.py │ │ risk_scorer.py │ │
│ │ rss.py │ │ prompt.py │ │ numpy/pandas │ │
│ │ ofac.py │ │ OpenRouter │ │ time-decay + │ │
│ │ feedparser │ │ deepseek-v4 │ │ story dedup │ │
│ └──────────────┘ └──────────────┘ └──────────────────┘ │
│ │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐ │
│ │ Graph │ │ Criticality │ │ Response │ │
│ │ Service │ │ Engine │ │ Service │ │
│ │ │ │ │ │ │ │
│ │ builder.py │ │ engine.py │ │ reroute.py │ │
│ │ algorithms.py│ │ cascade.py │ │ spr.py │ │
│ │ updater.py │ │ scenarios.py │ │ gap.py │ │
│ │ state.py │ │ NetworkX │ │ scipy HiGHS  │ │
│ │ NetworkX │ │ algorithms │ │ MCDM + LP │ │
│ └──────────────┘ └──────────────┘ └──────────────────┘ │
│ │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ LangGraph Orchestrator │ │
│ │ orchestrator/pipeline.py │ │
│ │ Manages state across all services │ │
│ │ Handles conditional threshold trigger │ │
│ │ Ensures correct execution order │ │
│ └────────────────────────────────────────────────────────┘ │
│ │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ Celery Beat Scheduler — NOT IMPLEMENTED │ │
│ │ config/celery.py (app wired, no tasks registered) │ │
│ │ Intended: full pipeline every 6h, OFAC weekly │ │
│ │ Redis as message broker │ │
│ │ Today: run the manage.py commands by hand │ │
│ └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
│
┌────────────┼───────────────────┐
▼ ▼ ▼
┌──────────────┐ ┌──────────┐ ┌──────────────────────────┐
│ PostgreSQL │ │ PostGIS │ │ NetworkX (in-memory) │
│ │ │ │ │ │
│ RawArticle │ │ Corridor │ │ DiGraph G=(V,E) │
│ Extracted │ │ geometry │ │ Nodes: suppliers, │
│ Event │ │ LineStr. │ │ corridors, ports, │
│ RiskScore │ │ Port/ │ │ refineries │
│ Supplier │ │ Refinery │ │ Edges: volume, │
│ Corridor │ │ Points │ │ transit, grade, │
│ Port │ │ │ │ effective_capacity │
│ Refinery │ │ Served │ │ │
│ Alternative │ │ as │ │ Rebuilt from DB │
│ Supplier │ │ GeoJSON │ │ on startup │
│ │ │ to │ │ Updated each cycle │
│ │ │ Leaflet │ │ │
└──────────────┘ └──────────┘ └──────────────────────────┘
│
▼
┌─────────────────────────────────────────────────────────────┐
│ EXTERNAL SOURCES │
│ │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│ │ GDELT │ │ OilPrice │ │ OFAC │ │ OpenRouter   │ │
│ │ DOC 2.0  │ │ .com RSS │ │ SDN │ │ deepseek-v4  │ │
│ │ free │ │ free │ │ CSV │ │ paid, tiny   │ │
│ │ no key │ │ no key │ │ weekly │ │ ~$0.00003/art│ │
│ │ THROTTLES│ │ │ │ │ │              │ │
│ └──────────┘ └──────────┘ └──────────┘ └──────────────┘ │
│ │
│ ┌──────────┐ ┌──────────┐ ┌──────────────────────────┐ │
│ │ EIA API │ │ gCaptain │ │ PPAC/IEA/EIA reports │ │
│ │ free │ │ RSS │ │ Hardcoded JSON │ │
│ │ backtest│ │ free │ │ Static seed data │ │
│ │ only │ │ │ │ Loaded once at setup │ │
│ └──────────┘ └──────────┘ └──────────────────────────┘ │
│ (Reuters + Lloyd's List from the original spec are gone:  │
│  Reuters retired public RSS in 2020, Lloyd's is paywalled)│
└─────────────────────────────────────────────────────────────┘


---

### LangGraph Pipeline State Flow

> **As built (`orchestrator/state.py`), with two deviations.** `raw_articles` /
> `extracted_events` became `ingest_report` / `extraction_report`: every stage
> persists to PostgreSQL and downstream stages read from there, so carrying
> hundreds of article dicts through the state bought nothing. Added:
> `threshold` (full `check_threshold` result), `baseline_flow_mbd`, `response`
> (gap + reroute + timeline + SPR), `run_id`, and `stages_run` / `errors`
> (append-reducers). Run options travel in `config["configurable"]`, not the state.

PipelineState (TypedDict) — original spec
{
raw_articles: List[dict] # set by ingest node
extracted_events: List[dict] # set by extraction node
risk_scores: Dict[str, float] # set by scoring node
graph_updated: bool # set by graph update node
criticality_ranking: List[dict] # set by criticality node
capacity_loss_mbd: float # set by criticality node
threshold_crossed: bool # set by threshold node
triggered_corridor: str | None # set by threshold node
reroute_recommendations: List # set by response node
spr_schedule: dict | None # set by response node
pipeline_run_at: str # set at pipeline start
}

Node execution order:
ingest → extract → score → update_graph → criticality
→ check_threshold
├── [threshold_crossed=True] → response → dashboard_update
└── [threshold_crossed=False] → dashboard_update


---

### Knowledge Graph Structure

Node Types:
┌─────────────────────────────────────────────────────┐
│ SUPPLIER (e.g. "Saudi Arabia") │
│ attrs: name, country_code, region, │
│ avg_export_mbd, sanctioned │
├─────────────────────────────────────────────────────┤
│ CORRIDOR (e.g. "Hormuz") │
│ attrs: name, capacity_mbd, transit_days, │
│ baseline_risk, live_risk_score │
├─────────────────────────────────────────────────────┤
│ PORT (e.g. "Vadinar") │
│ attrs: name, location (PostGIS Point), │
│ throughput_mbd │
├─────────────────────────────────────────────────────┤
│ REFINERY (e.g. "Reliance Jamnagar") │
│ attrs: name, company, capacity_mbd, │
│ min_run_rate (0.70), api_gravity_min/max, │
│ sulfur_tolerance │
└─────────────────────────────────────────────────────┘

Edge Types:
┌─────────────────────────────────────────────────────┐
│ SUPPLIER → CORRIDOR │
│ attrs: volume (mb/day), crude_grade │
├─────────────────────────────────────────────────────┤
│ CORRIDOR → PORT │
│ attrs: volume (mb/day), transit_days, │
│ effective_capacity (dynamic) │
├─────────────────────────────────────────────────────┤
│ PORT → REFINERY │
│ attrs: volume (mb/day), grade_compatible (bool) │
└─────────────────────────────────────────────────────┘

Special nodes:
SOURCE — virtual super-source connected to all suppliers
SINK — virtual super-sink connected from all refineries
(Used for max-flow computation)

Graph update cycle:

Compute risk_score per corridor (risk_scorer.py)
effective_capacity = volume × (1 - risk_score)
Update all corridor edges with new effective_capacity
Run algorithms on updated graph

---

### Celery Task Dependency Order

Every 6 hours:

poll_gdelt ──────────────────────────────────┐
poll_rss ───────────────────────────────────┤
poll_ofac (weekly) ──────────────────────────┘
│
▼
extract_events
(waits for ingest)
│
▼
score_and_update_graph
(waits for extraction)
│
▼
run_criticality
(waits for score update)
│
▼
check_and_run_response
(conditional on threshold)


---

### File Responsibility Map

config/settings.py → all Django + Celery + DB configuration
config/celery.py → Celery app + beat schedule
config/urls.py → root URL routing (includes core/urls.py)

core/models.py → ALL database models
core/serializers.py → ALL DRF serializers
core/views.py → ALL REST API views
core/urls.py → ALL API endpoint URL patterns

graph/builder.py → builds NetworkX graph from PostgreSQL
graph/algorithms.py → centrality, max-flow, cascade functions
graph/updater.py → updates edge weights from risk scores
graph/state.py → thread-safe in-memory graph singleton

pipeline/ingest/gdelt.py → GDELT DOC API polling (rate-limited)
pipeline/ingest/gdelt_gkg.py → GDELT GKG 15-min bulk files (un-throttled)
pipeline/ingest/rss.py → RSS feed parsing function
pipeline/ingest/ofac.py → OFAC SDN download + parse
pipeline/extract/extractor.py → OpenAI API extraction call
pipeline/extract/prompt.py → extraction prompt template
pipeline/score/risk_scorer.py → time-decay formula + normalization + story clustering
pipeline/score/candidates.py → alternative scoring statistics, for comparison only
                               (nothing in the live pipeline calls these yet)
pipeline/tasks.py → ALL pipeline entry functions (plain, NO Celery), incl. the
                    Phase 6 poll_gdelt_with_fallback (DOC → GKG on first 429)

criticality/engine.py → main criticality computation
criticality/cascade.py → cascading failure simulation loop
criticality/scenarios.py → named scenario definitions + parameters

response/reroute.py → MCDM alternative supplier ranking + replacement timeline
response/spr.py → SPR drawdown linear program (scipy HiGHS, NOT PuLP; see Phase 5)
response/gap.py → supply gap estimation (risk-weighted / cascade baseline)
response/trigger.py → re-specified threshold trigger (added in Phase 5)
response/plan.py → build_response: gap → reroute → timeline → SPR in one call
                   (shared by run_response, /api/simulate/, the orchestrator)

orchestrator/pipeline.py → LangGraph graph + node + edge definitions, run_pipeline()
orchestrator/nodes.py → individual node functions (never raise; errors → state)
orchestrator/state.py → PipelineState TypedDict + DEFAULT_OPTIONS

graph/updater.py::load_live_graph → singleton + stored risk, auto-refreshed (on a
                   copy, then swapped) when score_risk ran in another process

backtest/runner.py → BACKTEST_EVENTS, resumable day-by-day GKG pull + ledger,
                      point-in-time scoring, report → data/backtests/<event>.json
backtest/validator.py → signal vs price comparison (pure; excursions, verdicts)
backtest/eia.py → EIA v2 Brent (RBRTE) fetcher + committed cache
                  (data/brent_cache.json) + price-event locators

NOTE: commands live in core/management/commands/ — Django discovers them
per-app, so the top-level management/ shown in the original spec does not exist.

core/management/commands/seed_db.py → loads data/*.json to DB
core/management/commands/build_graph.py → builds + prints graph
core/management/commands/visualize_graph.py → graph rendering
core/management/commands/poll_sources.py → GDELT/RSS/OFAC ingest (--corridor,
                                            --last-minutes, --max-records)
core/management/commands/extract_events.py → LLM extraction (PAID)
core/management/commands/test_extraction.py → one article, exactly one paid call
core/management/commands/score_risk.py → recompute risk + update graph (free)
core/management/commands/compare_scoring.py → score the stored corpus under every
                                            candidate formula (read-only, no writes)
core/management/commands/run_criticality.py → Phase 4 static vs risk-weighted
core/management/commands/run_response.py → Phase 5 trigger → gap → reroute → SPR
core/management/commands/backfill_event_timestamps.py → seendate repair (one-off)
core/management/commands/run_pipeline.py → Phase 6 orchestrator (analysis-only by default)
core/management/commands/run_backtest.py → Phase 7: --list | --status | --pull | score
core/management/commands/capture_api_samples.py → Phase 7: one real response per
                    route → data/api_samples/ (read-only; API_DOCS.md is written from these)

---

## Repository Structure

energy-resilience/
├── CLAUDE.md ← you are here
├── API_DOCS.md ← frontend contract (Phase 7); payloads in backend/data/api_samples/
├── README.md
├── .gitignore
│
├── backend/ ← PRIMARY WORKING DIRECTORY
│ ├── manage.py
│ ├── requirements.txt
│ ├── .env
│ │
│ ├── config/
│ │ ├── init.py
│ │ ├── settings.py
│ │ ├── urls.py
│ │ ├── wsgi.py
│ │ └── celery.py
│ │
│ ├── core/ ← Django models + REST API
│ │ ├── models.py
│ │ ├── serializers.py
│ │ ├── views.py
│ │ ├── urls.py
│ │ └── migrations/
│ │
│ ├── graph/ ← NetworkX knowledge graph
│ │ ├── builder.py
│ │ ├── algorithms.py
│ │ ├── updater.py
│ │ └── state.py
│ │
│ ├── pipeline/ ← Data pipeline
│ │ ├── ingest/
│ │ │ ├── gdelt.py
│ │ │ ├── gdelt_gkg.py
│ │ │ ├── rss.py
│ │ │ └── ofac.py
│ │ ├── extract/
│ │ │ ├── extractor.py
│ │ │ └── prompt.py
│ │ ├── score/
│ │ │ └── risk_scorer.py
│ │ └── tasks.py
│ │
│ ├── criticality/ ← Criticality engine
│ │ ├── engine.py
│ │ ├── cascade.py
│ │ └── scenarios.py
│ │
│ ├── response/ ← Response optimization
│ │ ├── reroute.py
│ │ ├── spr.py
│ │ └── gap.py
│ │
│ ├── orchestrator/ ← LangGraph pipeline
│ │ ├── pipeline.py
│ │ ├── nodes.py
│ │ └── state.py
│ │
│ ├── backtest/ ← Historical validation
│ │ ├── runner.py
│ │ ├── validator.py
│ │ └── eia.py
│ │
│ ├── data/ ← Static seed data (JSON)
│ │ ├── suppliers.json
│ │ ├── corridors.json
│ │ ├── ports.json
│ │ ├── refineries.json
│ │ ├── edges.json
│ │ ├── alternatives.json
│ │ └── geometries/
│ │ ├── hormuz.geojson
│ │ ├── red_sea.geojson   (Suez folded in — no suez.geojson)
│ │ └── cape.geojson
│ │
│ ├── core/management/commands/  ← NOT top-level management/
│ │ ├── seed_db.py, build_graph.py, visualize_graph.py
│ │ ├── poll_sources.py, extract_events.py, test_extraction.py
│ │ ├── score_risk.py, run_criticality.py
│ │ ├── backfill_event_timestamps.py
│ │ ├── run_response.py, run_pipeline.py
│ │ └── run_backtest.py  (empty stub)
│ │
│ └── tests/
│ ├── test_graph.py
│ ├── test_scoring.py
│ ├── test_criticality.py
│ ├── test_reroute.py
│ ├── test_spr.py
│ ├── test_gap.py
│ ├── test_trigger.py
│ ├── test_plan.py
│ ├── test_orchestrator.py
│ ├── test_extraction.py
│ └── test_api.py
│
└── frontend/ ← FRONTEND — DO NOT MODIFY
└── src/
├── api/
├── components/
├── pages/
├── hooks/
└── utils/


---

## Tech Stack

### Backend
- Python 3.10+
- Django 4.2
- Django REST Framework 3.14
- django-cors-headers (CORS for frontend)
- django-environ (environment variables)
- psycopg2-binary (PostgreSQL driver)
- django.contrib.gis + PostGIS (geospatial)

### Data & Graph
- NetworkX 3.2 (knowledge graph + algorithms)
- numpy 1.26 (risk scoring math)
- pandas 2.1 (data manipulation, backtest)
- scipy 1.18.1 as installed (normalization; **solves the SPR LP via HiGHS**)
- PuLP 3.3.2 as installed. **Not used.** Its bundled CBC fails on this machine
  whenever output is redirected (`msg=0` or `logPath`), which is the only mode
  a server or a test run can use. See Phase 5.

### Pipeline & Scheduling
- Celery 5.3 (task queue)
- Redis 5.0 (Celery broker + result backend)
- django-celery-beat 2.5 (periodic task scheduling)
- feedparser 6.0 (RSS XML parsing)
- requests 2.31 (HTTP calls to GDELT, EIA, OFAC)

### AI
- openai 3.6.0 SDK — pointed at **OpenRouter** (`https://openrouter.ai/api/v1`),
  which speaks the OpenAI wire protocol. Active model: `deepseek/deepseek-v4-flash-0731`
  (~$0.04/M input, $0.08/M output — roughly 20x cheaper than gpt-4o-mini).
  Switching provider/model is a `.env` edit (`LLM_BASE_URL` / `LLM_MODEL`), not a code change.
- langgraph 0.1 (pipeline orchestration)
- langchain 0.1 (LangGraph dependency)

### Utilities
- python-dotenv 1.0 (load .env file)
- Pillow (if image processing needed)

---

## Django Apps — What Goes Where

### core/
All Django models. All REST API views and serializers. All URL routing.
Nothing else. Keep this clean.

### graph/
Everything related to the NetworkX knowledge graph.
- builder.py — constructs graph from database
- algorithms.py — centrality, max-flow, cascading failure functions
- updater.py — updates edge weights from new risk scores
- state.py — thread-safe in-memory graph singleton

### pipeline/
Everything related to data collection and processing.
- ingest/ — pulling raw data from external sources
- extract/ — Claude API event extraction
- score/ — risk scoring formula
- tasks.py — all Celery task definitions (calls functions from submodules)

### criticality/
Everything related to network analysis.
- engine.py — main criticality computation (calls graph/algorithms.py)
- cascade.py — cascading failure simulation loop
- scenarios.py — named scenario definitions with IEA-cited parameters

### response/
Everything related to generating recommendations.
- reroute.py — MCDM alternative supplier ranking
- spr.py — SPR drawdown linear program (scipy HiGHS)
- gap.py — supply gap estimation from cascade results
- trigger.py — threshold trigger deciding whether reroute/SPR run

### orchestrator/
LangGraph pipeline definition only.
- pipeline.py — graph definition, nodes, edges, conditional logic
- nodes.py — individual node functions (thin wrappers calling other apps)
- state.py — TypedDict pipeline state schema

### backtest/
Historical validation only.
- runner.py — reconstructs historical GDELT data and runs pipeline
- validator.py — compares risk signal against Brent price movements
- eia.py — EIA API client for historical price data

---

## Celery Configuration (config/celery.py + config/settings.py)

> **STATUS: NOT IMPLEMENTED for this build.** Celery Beat, the Celery worker, and Redis
> are deferred to a future production deployment (they require an always-on host, and
> Beat does not backfill missed runs so it is useless on an intermittently-on laptop).
> For the thesis demo the pipeline is invoked manually via `python manage.py run_pipeline`,
> which calls the LangGraph orchestrator directly in a single synchronous process — no
> broker, no worker, no scheduler. The config below is kept verbatim as the spec for the
> intended 6-hourly production cadence. To enable it later: `pip install` the Celery
> stack (already in requirements.txt), add `@shared_task` to the `pipeline/tasks.py`
> entry functions, paste this schedule into settings, and run Redis + worker + beat on
> an always-on host.

```python
# config/settings.py
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'

CELERY_BEAT_SCHEDULE = {
    'poll-gdelt': {
        'task': 'pipeline.tasks.poll_gdelt',
        'schedule': 21600,          # every 6 hours
    },
    'poll-rss': {
        'task': 'pipeline.tasks.poll_rss',
        'schedule': 21600,
    },
    'extract-events': {
        'task': 'pipeline.tasks.extract_events',
        'schedule': 21600,
    },
    'score-and-update': {
        'task': 'pipeline.tasks.score_and_update_graph',
        'schedule': 21600,
    },
    'run-criticality': {
        'task': 'pipeline.tasks.run_criticality',
        'schedule': 21600,
    },
    'download-ofac': {
        'task': 'pipeline.tasks.download_ofac',
        'schedule': 604800,         # weekly
    },
}
```

---

