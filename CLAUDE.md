# Energy Supply Chain Resilience System
# CLAUDE.md — Project Instructions for Claude Code

---

## Project Identity

**Project Name:** AI-Driven Energy Supply Chain Resilience System
**Type:** B.E. Major Thesis Project
**Institution:** Fr. Conceicao Rodrigues College of Engineering (FR. CRCE)
**Department:** Artificial Intelligence & Data Science
**Academic Year:** 2026–2027
**Developer:** Joshua (solo backend developer)
**GitHub:** Josh-codes

---

## What This Project Does

Models India's crude oil import network as a risk-weighted knowledge graph.
Continuously monitors geopolitical news to update corridor risk scores.
Applies graph-theoretic criticality analysis to identify vulnerable corridors.
Simulates disruption scenarios and generates rerouting + SPR drawdown recommendations.
Presents all outputs via a REST API consumed by a React frontend dashboard.

**Core Research Contribution:**
The delta between static baseline criticality rankings and risk-weighted criticality rankings —
quantifying how current geopolitical conditions shift India's structural import vulnerability.

---

## System Architecture

### Overview
This system has six pipeline layers that run sequentially via LangGraph orchestration.
The pipeline is designed to run automatically every 6 hours via Celery Beat in a
production deployment. **Celery Beat / Celery worker / Redis are NOT being implemented
for this thesis build** — there is no always-on host to run them, and the project is
demonstrated by manually triggering the pipeline (`python manage.py run_pipeline`).
The `CELERY_BEAT_SCHEDULE` config below is retained as documentation of the intended
production cadence only. See the note in the Celery Configuration section.
The frontend never triggers the pipeline — it only reads pre-computed results from PostgreSQL.
The only real-time computation triggered by the frontend is scenario simulation via NetworkX.

---

### Full Pipeline Flow

[External Sources]
│
├── GDELT API ──────────────────────────────────┐
├── Reuters Energy RSS ──────────────────────────┤
├── Lloyd's List RSS ────────────────────────────┤──► [Celery Beat - every 6h]
└── OFAC SDN CSV ───────────────────────────────┘ │
│
[Ingest Service]
feedparser + requests
URL deduplication
│
▼
[PostgreSQL - RawArticle]
Temporary staging table
Deleted after 14 days
│
▼
[LLM Event Extraction]
OpenAI gpt-4o-mini
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
│
┌─────────────────────────────┘
│
▼
[Risk Scoring Service]
numpy + pandas
Formula: Σ[severity × confidence × e^(-0.1 × Δt)]
Computed per corridor
Normalized to 0-1
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
├── Weighted betweenness centrality
├── Max-flow analysis (baseline vs risk-weighted)
└── Cascading failure simulation (10% increments)
│
▼
[Threshold Trigger]
condition: criticality_score > 0.65
AND risk_score > 0.50
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
│ PuLP linear program
│ Minimizes total drawdown
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
│ │ ofac.py │ │ OpenAI API │ │ time-decay │ │
│ │ feedparser │ │ gpt-4o-mini │ │ formula │ │
│ └──────────────┘ └──────────────┘ └──────────────────┘ │
│ │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐ │
│ │ Graph │ │ Criticality │ │ Response │ │
│ │ Service │ │ Engine │ │ Service │ │
│ │ │ │ │ │ │ │
│ │ builder.py │ │ engine.py │ │ reroute.py │ │
│ │ algorithms.py│ │ cascade.py │ │ spr.py │ │
│ │ updater.py │ │ scenarios.py │ │ gap.py │ │
│ │ state.py │ │ NetworkX │ │ scipy + PuLP │ │
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
│ │ Celery Beat Scheduler │ │
│ │ config/celery.py │ │
│ │ Triggers full pipeline every 6 hours │ │
│ │ OFAC download weekly │ │
│ │ Redis as message broker │ │
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
│ │ GDELT │ │ Reuters │ │ OFAC │ │ OpenAI API │ │
│ │ API │ │ RSS │ │ SDN │ │ gpt-4o-mini │ │
│ │ free │ │ free │ │ CSV │ │ paid │ │
│ │ no key │ │ no key │ │ weekly │ │ ~$4/month │ │
│ └──────────┘ └──────────┘ └──────────┘ └──────────────┘ │
│ │
│ ┌──────────┐ ┌──────────┐ ┌──────────────────────────┐ │
│ │ EIA API │ │ Lloyd's │ │ PPAC/IEA/EIA reports │ │
│ │ free │ │ List │ │ Hardcoded JSON │ │
│ │ backtest│ │ RSS │ │ Static seed data │ │
│ │ only │ │ free │ │ Loaded once at setup │ │
│ └──────────┘ └──────────┘ └──────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘


---

### LangGraph Pipeline State Flow

PipelineState (TypedDict)
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

### REST API Request-Response Cycle

User opens dashboard
│
▼
React component mounts
│
▼
useRiskScores() hook fires
│
▼
axios.get('http://localhost:8000/api/risk-scores/')
│
▼
Django URL router → RiskScoresView
│
▼
RiskScore.objects.filter(
computed_at__gte=last_hour
).order_by('-computed_at')
│
▼
RiskScoreSerializer → JSON
│
▼
{"Hormuz": 0.72, "Red Sea": 0.45,
"Suez": 0.12, "Cape": 0.05}
│
▼
React updates state
│
▼
Leaflet corridor colors update
Recharts risk timeline updates
│
▼
Repeat every 60 seconds (polling)


---

### Scenario Simulation Request-Response Cycle
(Only real-time computation triggered by frontend)

User sets Hormuz slider to 50%
│
▼
axios.post('/api/simulate/', {
corridor: "Hormuz",
degradation: 0.50,
duration_days: 14
})
│
▼
Django SimulateView
│
▼
GraphState.get_instance().get_graph()
(retrieves in-memory NetworkX graph)
│
▼
cascading_failure_simulation(G, "Hormuz", 0.50)
(runs in < 1 second on small graph)
│
▼
supply_gap = baseline_flow - disrupted_flow
│
├──► reroute_optimizer(gap, crisis_level)
│ returns ranked alternatives
│
└──► compute_spr_schedule(gap, duration, transit)
returns drawdown schedule
│
▼
Combined result serialized to JSON
│
▼
React updates:

Cascade curve chart
Reroute recommendations panel
SPR drawdown chart
Map shows recommended route

---

### Database Schema Overview

PostgreSQL Tables:
┌─────────────────┬──────────────────────────────────────────┐
│ Table │ Purpose │
├─────────────────┼──────────────────────────────────────────┤
│ core_supplier │ Supplier country nodes │
│ core_corridor │ Corridor nodes + PostGIS geometry │
│ core_port │ Port nodes + PostGIS point │
│ core_refinery │ Refinery nodes + PostGIS point │
│ core_rawarticle │ Temp staging (deleted after 14 days) │
│ core_extractedevent│ Permanent event store │
│ core_riskscore │ Permanent score history (backtest) │
│ core_alternativesupplier│ Reroute alternatives table │
└─────────────────┴──────────────────────────────────────────┘

PostGIS geometry columns:
core_corridor.geometry → LineStringField (maritime route)
core_port.location → PointField (lat/lon)
core_refinery.location → PointField (lat/lon)
core_alternativesupplier.route_geometry → LineStringField

Permanent tables (never delete rows):

core_extractedevent
core_riskscore

Temporary table (clean up after 14 days):

core_rawarticle (after processed=True)

---

### Threshold Trigger Logic

After criticality engine runs:

for each corridor in criticality_ranking:
centrality_score = betweenness_centrality[corridor]
risk_score = latest RiskScore for corridor

if centrality_score > 0.65 AND risk_score > 0.50:
    threshold_crossed = True
    triggered_corridor = corridor.name
    break

if threshold_crossed:
→ fire reroute optimizer
→ fire SPR drawdown model
→ store recommendations in PostgreSQL
→ dashboard shows alert + recommendations

if not threshold_crossed:
→ update dashboard with latest scores only
→ no recommendations generated


---

### Backtest Architecture

Historical validation against two events:

2025 US-Iran standoff (Brent +8% in single session)
2026 Hormuz closure (Brent $69 → $114/barrel)

For each event:
1. Reconstruct historical GDELT data for event period
(GDELT archives all data — fully queryable historically)

2. Run extraction pipeline retrospectively
   (same Claude/OpenAI extraction, historical articles)

3. Run risk scoring formula on historical events
   (same time-decay formula, historical timestamps)

4. Plot risk score timeline vs Brent price data
   (EIA API provides historical daily Brent prices)

5. Measure lead time:
   days between risk_score > 0.6 and observed price spike

Validation criterion:
risk signal must elevate to > 0.6
within 48 hours of OR before observed price move

Output:
{
"event": "2025_iran_standoff",
"signal_elevated_at": "2025-XX-XX",
"price_spiked_at": "2025-XX-XX",
"lead_time_days": 2,
"max_risk_score": 0.81,
"brent_spike_pct": 8.2,
"validation": "PASSED"
}


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

pipeline/ingest/gdelt.py → GDELT API polling function
pipeline/ingest/rss.py → RSS feed parsing function
pipeline/ingest/ofac.py → OFAC SDN download + parse
pipeline/extract/extractor.py → OpenAI API extraction call
pipeline/extract/prompt.py → extraction prompt template
pipeline/score/risk_scorer.py → time-decay formula + normalization
pipeline/tasks.py → ALL Celery task definitions

criticality/engine.py → main criticality computation
criticality/cascade.py → cascading failure simulation loop
criticality/scenarios.py → named scenario definitions + parameters

response/reroute.py → MCDM alternative supplier ranking
response/spr.py → PuLP SPR linear program
response/gap.py → supply gap estimation

orchestrator/pipeline.py → LangGraph graph + node + edge definitions
orchestrator/nodes.py → individual node functions
orchestrator/state.py → PipelineState TypedDict

backtest/runner.py → backtest execution controller
backtest/validator.py → signal vs price comparison
backtest/eia.py → EIA API historical price fetcher

management/commands/seed_db.py → loads data/*.json to DB
management/commands/build_graph.py → builds + prints graph
management/commands/run_pipeline.py → manual pipeline trigger
management/commands/test_extraction.py → test one article extraction
management/commands/run_backtest.py → run historical validation

---

## Repository Structure

energy-resilience/
├── CLAUDE.md ← you are here
├── API_DOCS.md ← REST API documentation for frontend teammate
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
│ │ ├── red_sea.geojson
│ │ ├── suez.geojson
│ │ └── cape.geojson
│ │
│ ├── management/
│ │ └── commands/
│ │ ├── seed_db.py
│ │ ├── build_graph.py
│ │ ├── run_pipeline.py
│ │ ├── run_backtest.py
│ │ └── test_extraction.py
│ │
│ └── tests/
│ ├── test_graph.py
│ ├── test_scoring.py
│ ├── test_criticality.py
│ ├── test_reroute.py
│ ├── test_spr.py
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

## Rules — Read Before Writing Any Code

1. Work only inside backend/ unless explicitly told otherwise
2. Never modify anything inside frontend/
3. Always use Django ORM — no raw SQL unless absolutely necessary
4. Never hardcode credentials — always use environment variables from .env
5. All external API calls must be wrapped in try/except — pipeline must never crash
6. Use Python logging module everywhere — never use print() for errors
7. Every new function must have a corresponding test in tests/
8. Read and understand code before moving to the next component
9. Follow the build phase order — do not skip ahead
10. When in doubt about where code belongs — check the app structure below

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
- scipy 1.11 (normalization, optimization utilities)
- PuLP 2.7 (SPR linear program)

### Pipeline & Scheduling
- Celery 5.3 (task queue)
- Redis 5.0 (Celery broker + result backend)
- django-celery-beat 2.5 (periodic task scheduling)
- feedparser 6.0 (RSS XML parsing)
- requests 2.31 (HTTP calls to GDELT, EIA, OFAC)

### AI
- openai 1.x
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
- spr.py — PuLP SPR drawdown linear program
- gap.py — supply gap estimation from cascade results

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

## All Django Models (core/models.py)

```python
from django.contrib.gis.db import models

class Supplier(models.Model):
    name = models.CharField(max_length=100)
    country_code = models.CharField(max_length=3)
    region = models.CharField(max_length=50)
    avg_export_mbd = models.FloatField()        # million barrels/day to India
    sanctioned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Corridor(models.Model):
    name = models.CharField(max_length=100, unique=True)
    geometry = models.LineStringField()          # PostGIS maritime route
    capacity_mbd = models.FloatField()           # million barrels/day
    transit_days = models.IntegerField()
    baseline_risk = models.FloatField(default=0.1)
    live_risk_score = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

class Port(models.Model):
    name = models.CharField(max_length=100)
    location = models.PointField()               # PostGIS lat/lon
    state = models.CharField(max_length=50)
    throughput_mbd = models.FloatField()

    def __str__(self):
        return self.name

class Refinery(models.Model):
    name = models.CharField(max_length=100)
    company = models.CharField(max_length=100)
    location = models.PointField()
    capacity_mbd = models.FloatField()
    min_run_rate = models.FloatField(default=0.70)
    api_gravity_min = models.FloatField()
    api_gravity_max = models.FloatField()
    sulfur_tolerance = models.FloatField()       # max % sulfur
    port = models.ForeignKey(Port, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return self.name

class RawArticle(models.Model):
    url = models.URLField(unique=True, max_length=500)
    source = models.CharField(max_length=50)    # gdelt / reuters / lloyds
    title = models.CharField(max_length=500)
    raw_text = models.TextField()
    ingested_at = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=['processed', 'ingested_at'])]

class ExtractedEvent(models.Model):
    CORRIDOR_CHOICES = [
        ('Hormuz', 'Strait of Hormuz'),
        ('Red Sea', 'Red Sea / Bab-el-Mandeb'),
        ('Suez', 'Suez Canal'),
        ('Cape', 'Cape of Good Hope'),
        ('None', 'Not corridor-specific'),
    ]
    EVENT_TYPE_CHOICES = [
        ('sanction', 'Sanction'),
        ('military', 'Military'),
        ('shipping', 'Shipping'),
        ('policy', 'Policy'),
        ('other', 'Other'),
    ]
    corridor = models.ForeignKey(
        Corridor, on_delete=models.SET_NULL, null=True, blank=True
    )
    actor = models.CharField(max_length=200)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    severity = models.IntegerField()             # 1-5
    confidence = models.FloatField()             # 0.0-1.0
    timestamp = models.DateTimeField()
    article_url = models.URLField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['corridor', 'timestamp'])]

class RiskScore(models.Model):
    corridor = models.ForeignKey(Corridor, on_delete=models.CASCADE)
    score = models.FloatField()                  # 0.0-1.0 normalized
    raw_score = models.FloatField()              # unnormalized
    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['corridor', 'computed_at'])]
        ordering = ['-computed_at']

class AlternativeSupplier(models.Model):
    name = models.CharField(max_length=100)
    country = models.CharField(max_length=100)
    route_description = models.CharField(max_length=200)
    avoids_corridor = models.CharField(max_length=50)  # which corridor this avoids
    transit_days = models.IntegerField()
    price_premium_usd = models.FloatField()      # $/barrel vs disrupted source
    api_gravity = models.FloatField()
    sulfur_pct = models.FloatField()
    sanctioned = models.BooleanField(default=False)
    route_geometry = models.LineStringField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} via {self.route_description}"
```

---

## REST API Endpoints (core/urls.py + core/views.py)

GET /api/risk-scores/
Returns latest risk score per corridor
Response: {"Hormuz": 0.72, "Red Sea": 0.45, "Suez": 0.12, "Cape": 0.05}

GET /api/criticality/
Returns current criticality ranking (both static and risk-weighted)
Response: [{"corridor": "Hormuz", "static_rank": 1, "risk_rank": 1,
"centrality": 0.89, "capacity_loss_mbd": 1.4}, ...]

GET /api/cascade/?corridor=Hormuz&degradation=30
Returns cascade simulation for given corridor and degradation %
Response: [{"degradation_pct": 10, "capacity_loss_mbd": 0.3,
"affected_refineries": [], "affected_count": 0}, ...]

GET /api/reroute/?corridor=Hormuz&crisis=normal
Returns ranked alternative suppliers
Response: [{"source": "Saudi Yanbu", "score": 0.88, "cost_score": 0.92,
"transit_score": 0.90, "compat_score": 0.80,
"transit_days": 10, "price_premium": 2.0}, ...]

GET /api/spr/?gap_mbd=2.0&duration_days=10&transit_days=10
Returns SPR drawdown schedule
Response: {"daily_schedule": [1.0, 1.0, 1.0, ...],
"total_released_mb": 10.0,
"insufficient": false,
"days_until_threshold": 15}

GET /api/corridors/geojson/
Returns all corridor geometries as GeoJSON for Leaflet
Response: GeoJSON FeatureCollection with risk_score property per feature

GET /api/events/live/
Returns latest 50 extracted events
Response: [{"corridor": "Red Sea", "actor": "Houthi",
"event_type": "military", "severity": 4,
"confidence": 0.9, "timestamp": "2026-08-24T..."}, ...]

GET /api/scenarios/
Returns all named scenario definitions
Response: [{"name": "Hormuz 30%", "corridor": "Hormuz",
"degradation": 0.30, "price_impact_usd": 20,
"affected_volume_mbd": 5.1}, ...]

POST /api/simulate/
Triggers custom scenario simulation
Body: {"corridor": "Hormuz", "degradation": 0.50, "duration_days": 14}
Response: full cascade + reroute + SPR combined result

GET /api/backtest/?event=2025_iran_standoff
Returns backtest results for named historical event
Response: {"event": "2025_iran_standoff", "signal_elevated_days_before": 3,
"max_risk_score": 0.81, "brent_spike_pct": 8.2}


---

## Key Algorithms and Formulas

### Risk Scoring Formula
```python
import numpy as np
from datetime import datetime, timezone

def compute_risk_score(corridor_name, lambda_decay=0.1):
    events = ExtractedEvent.objects.filter(
        corridor__name=corridor_name
    ).order_by('-timestamp')

    now = datetime.now(timezone.utc)
    raw_score = 0.0

    for event in events:
        delta_t = (now - event.timestamp).days
        decay = np.exp(-lambda_decay * delta_t)
        raw_score += event.severity * event.confidence * decay

    return raw_score  # normalize across corridors after computing all
```

### Graph Edge Weight Update
```python
def update_edge_weights(G, risk_scores):
    # risk_scores = {"Hormuz": 0.72, "Red Sea": 0.45, ...}
    for u, v, data in G.edges(data=True):
        corridor_name = data.get('corridor')
        if corridor_name in risk_scores:
            risk = risk_scores[corridor_name]
            data['effective_capacity'] = data['volume'] * (1 - risk)
    return G
```

### Betweenness Centrality
```python
import networkx as nx
centrality = nx.betweenness_centrality(G, weight='effective_capacity')
# Returns dict: {"Hormuz": 0.89, "Red Sea": 0.45, ...}
```

### Max-Flow
```python
baseline_flow, _ = nx.maximum_flow(
    G, 'SOURCE', 'SINK', capacity='volume'
)
risk_flow, _ = nx.maximum_flow(
    G, 'SOURCE', 'SINK', capacity='effective_capacity'
)
capacity_loss = baseline_flow - risk_flow
```

### Cascading Failure Loop
```python
def cascading_failure_simulation(G, corridor_name):
    results = []
    baseline_flow, _ = nx.maximum_flow(G, 'SOURCE', 'SINK',
                                        capacity='effective_capacity')
    for pct in range(10, 101, 10):
        G_copy = G.copy()
        for u, v, data in G_copy.edges(data=True):
            if data.get('corridor') == corridor_name:
                data['effective_capacity'] *= (1 - pct/100)
        disrupted_flow, flow_dict = nx.maximum_flow(
            G_copy, 'SOURCE', 'SINK', capacity='effective_capacity'
        )
        capacity_loss = baseline_flow - disrupted_flow
        affected = check_refineries(flow_dict)
        results.append({
            'degradation_pct': pct,
            'capacity_loss_mbd': capacity_loss,
            'affected_refineries': affected,
            'affected_count': len(affected)
        })
    return results
```

### MCDM Reroute Scoring
```python
def normalize(value, all_values, invert=False):
    min_v, max_v = min(all_values), max(all_values)
    if max_v == min_v:
        return 1.0
    score = (value - min_v) / (max_v - min_v)
    return 1 - score if invert else score

def score_alternative(alt, all_alts, crisis='normal'):
    weights = (0.20, 0.55, 0.25) if crisis == 'severe' else (0.40, 0.35, 0.25)
    w_cost, w_transit, w_compat = weights

    all_premiums = [a.price_premium_usd for a in all_alts]
    all_transits = [a.transit_days for a in all_alts]

    cost_score = normalize(alt.price_premium_usd, all_premiums, invert=True)
    transit_score = normalize(alt.transit_days, all_transits, invert=True)
    compat_score = compute_grade_compatibility(alt)

    return (w_cost * cost_score) + (w_transit * transit_score) + (w_compat * compat_score)
```

### SPR Linear Program
```python
from pulp import LpMinimize, LpProblem, LpVariable, lpSum, value, PULP_CBC_CMD

def compute_spr_schedule(gap_mbd, duration_days, transit_days):
    SPR_TOTAL = 36.87        # million barrels
    SPR_SAFETY_PCT = 0.20
    MAX_DAILY = 1.0          # mb/day physical limit
    available = SPR_TOTAL * (1 - SPR_SAFETY_PCT)

    prob = LpProblem("SPR_Drawdown", LpMinimize)
    release = [LpVariable(f"d_{t}", lowBound=0, upBound=MAX_DAILY)
               for t in range(duration_days)]

    prob += lpSum(release)                           # minimize total
    prob += lpSum(release) <= available              # within reserves

    for t in range(duration_days):
        if t < transit_days:
            prob += release[t] >= min(gap_mbd, MAX_DAILY)
        else:
            prob += release[t] == 0

    prob.solve(PULP_CBC_CMD(msg=0))

    schedule = [value(release[t]) for t in range(duration_days)]
    total = sum(s for s in schedule if s)
    insufficient = total < gap_mbd * transit_days

    return {
        'daily_schedule': schedule,
        'total_released_mb': total,
        'insufficient': insufficient,
        'gap_covered_pct': (total / (gap_mbd * transit_days)) * 100
    }
```

### LangGraph Pipeline State
```python
from typing import TypedDict, List, Dict, Optional

class PipelineState(TypedDict):
    raw_articles: List[dict]
    extracted_events: List[dict]
    risk_scores: Dict[str, float]
    graph_updated: bool
    criticality_ranking: List[dict]
    capacity_loss_mbd: float
    threshold_crossed: bool
    triggered_corridor: Optional[str]
    reroute_recommendations: List[dict]
    spr_schedule: Optional[dict]
    pipeline_run_at: str
```

---

## Claude API Extraction Prompt

Stored in pipeline/extract/prompt.py:

```python
EXTRACTION_PROMPT = """
You are an expert analyst extracting geopolitical risk information
from news articles related to oil supply chains.

Extract information and return ONLY a valid JSON object.
No explanation, no preamble, no markdown — just the JSON.

Required fields:
{
    "corridor": "Hormuz" | "Red Sea" | "Suez" | "Cape" | "None",
    "actor": "country or entity name as string",
    "event_type": "sanction" | "military" | "shipping" | "policy" | "other",
    "severity": integer 1-5 where 1=minor 5=critical,
    "confidence": float 0.0-1.0 how confident you are in this extraction,
    "is_relevant": true if article relates to oil supply chain risk else false
}

Corridor definitions:
- Hormuz: Strait of Hormuz, Persian Gulf, Gulf of Oman
- Red Sea: Red Sea, Bab-el-Mandeb, Gulf of Aden, Houthi attacks on shipping
- Suez: Suez Canal, Egypt
- Cape: Cape of Good Hope, rerouting via Africa

If is_relevant is false, still return all fields but set severity=1 confidence=0.1

Article:
{article_text}
"""
```

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

## Environment Variables (.env)
Database

DB_NAME=energy_resilience
DB_USER=postgres
DB_PASSWORD=your_password_here
DB_HOST=localhost
DB_PORT=5432

Django

SECRET_KEY=generate-a-long-random-string-here
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

Redis

REDIS_URL=redis://localhost:6379/0

External APIs

OPENAI_API_KEY=sk-ant-your-key-here
EIA_API_KEY=your-eia-key-here

CORS (frontend origin)

CORS_ALLOWED_ORIGINS=http://localhost:3000


---

## Data Sources Reference

| Source | What | How | Update |
|--------|------|-----|--------|
| PPAC | Refinery capacities, import volumes, SPR | Hardcoded JSON from PDF reports | Once at setup |
| IEA | Corridor volumes, elasticities | Hardcoded JSON from PDF reports | Once at setup |
| EIA API | Historical Brent prices | api.eia.gov REST API (free key) | Once for backtest |
| GDELT | Live geopolitical events | REST API, no key needed | Every 6 hours |
| Reuters RSS | Energy news | feedparser, no key needed | Every 6 hours |
| Lloyd's List RSS | Shipping news | feedparser, no key needed | Every 6 hours |
| OFAC SDN | Sanctions registry | CSV download from sanctions.ofac.treas.gov | Weekly |
| Claude API | LLM extraction | anthropic SDK, paid per token | Every 6 hours |

---

## Named Corridors and Baseline Data

3-corridor model (Suez folded into Red Sea). Capacity = global chokepoint throughput (all nations); India flow = FY 2025-26 modelled inflow, reconciled against supplier_to_corridor totals (Σ = 4.926 mb/d).

| Corridor | Capacity (mb/day) | India flow (mb/day) | Baseline risk |
|----------|-------------------|---------------------|---------------|
| Hormuz | 17.0 | 2.316 | 0.20 |
| Red Sea (incl. Suez + Bab-el-Mandeb) | 5.8 | 0.355 | 0.15 |
| Cape | unlimited (999.0 sentinel) | 2.255 | 0.05 |

---

## Named Scenarios (criticality/scenarios.py)

```python
SCENARIOS = {
    "hormuz_30": {
        "name": "Hormuz 30% Disruption",
        "corridor": "Hormuz",
        "degradation": 0.30,
        "price_impact_usd": 20,
        "affected_volume_mbd": 5.1,
        "source": "IEA Chokepoints 2023"
    },
    "hormuz_full": {
        "name": "Hormuz Full Closure",
        "corridor": "Hormuz",
        "degradation": 1.00,
        "price_impact_usd": 65,
        "affected_volume_mbd": 17.0,
        "source": "IEA Chokepoints 2023"
    },
    "red_sea": {
        "name": "Red Sea Suspension",
        "corridor": "Red Sea",
        "degradation": 0.80,
        "price_impact_usd": 5,
        "transit_day_increase": 14,
        "source": "IEA Red Sea Assessment 2024"
    },
    "opec_cut": {
        "name": "OPEC+ Emergency Cut",
        "corridor": None,
        "supply_reduction_mbd": 2.0,
        "india_impact_mbd": 0.8,
        "price_impact_usd": 15,
        "source": "IEA Oil Market Report 2024"
    }
}
```

---

## Refinery Data (from PPAC)

| Refinery | Company | Capacity (mb/day) | API range | Sulfur % | Port |
|----------|---------|-------------------|-----------|----------|------|
| Jamnagar | Reliance | 1.24 | 22-45 | 0-4 | Vadinar |
| Panipat | IOC | 0.30 | 25-45 | 0-3 | Mundra |
| Mumbai | BPCL | 0.24 | 28-42 | 0-2 | Mumbai |
| Vizag | HPCL | 0.17 | 28-40 | 0-2 | Vizag |
| Kochi | BPCL | 0.15 | 28-38 | 0-2 | Kochi |
| Paradip | IOC | 0.30 | 22-40 | 0-3 | Paradip |
| Mangaluru | MRPL | 0.18 | 28-42 | 0-2 | Mangaluru |

---

## Alternative Supplier Table (data/alternatives.json)

| Source | Route | Transit | Premium | API | Sulfur | Sanctioned |
|--------|-------|---------|---------|-----|--------|------------|
| Saudi (non-Hormuz) | Yanbu → Suez | 10 days | +$2 | 32 | 1.8% | No |
| UAE (Fujairah) | Direct | 8 days | +$1 | 38 | 0.7% | No |
| Kuwait (non-Hormuz) | Shuaiba → Suez | 12 days | +$3 | 31 | 2.5% | No |
| Iraq (Ceyhan) | Turkey pipeline | 14 days | +$4 | 33 | 2.0% | No |
| USA (WTI) | Gulf Coast → Cape | 28 days | +$9 | 39 | 0.3% | No |
| Nigeria (Bonny) | Atlantic → Cape | 18 days | +$5 | 34 | 0.1% | No |
| Angola | Atlantic → Cape | 20 days | +$6 | 31 | 0.2% | No |
| Russia (Urals) | Baltic → Indian Ocean | 22 days | -$3 | 31 | 1.5% | Partial |
| Kazakhstan (CPC) | Black Sea → Suez | 16 days | +$3 | 44 | 0.5% | No |
| Libya | Mediterranean → Suez | 12 days | +$4 | 36 | 0.4% | No |

---

## Development Commands

```bash
# Navigate to backend first
cd backend

# Django server
python manage.py runserver

# Load seed data into database
python manage.py seed_db

# Build NetworkX graph and print summary
python manage.py build_graph

# Trigger full pipeline manually (no need to wait for Celery)
python manage.py run_pipeline

# Test Claude extraction on one article URL
python manage.py test_extraction --url "https://reuters.com/article-url"

# Run historical backtest
python manage.py run_backtest --event "2025_iran_standoff"
python manage.py run_backtest --event "2026_hormuz_closure"

# Run all tests
python manage.py test

# Celery worker (separate terminal)
celery -A config worker --loglevel=info

# Celery Beat scheduler (separate terminal)
celery -A config beat --loglevel=info

# Redis (separate terminal if not running as service)
redis-server
```

---

## How to Run Locally (4 terminals)

Terminal 1: cd backend && python manage.py runserver
Terminal 2: cd backend && celery -A config worker --loglevel=info
Terminal 3: cd backend && celery -A config beat --loglevel=info
Terminal 4: redis-server


---

## Build Phases — Check Off as Completed

- [x] Phase 1 — Django setup + PostgreSQL + PostGIS + models + seed data + NetworkX graph  ✅ COMPLETE
  - [x] Repo skeleton — all backend/ package dirs + __init__.py, requirements.txt, README, .gitignore
  - [x] Django project — config/ (settings, urls, wsgi, asgi, celery), manage.py at backend root
  - [x] All 7 apps created (core, graph, pipeline, criticality, response, orchestrator, backtest) + registered in settings.py + migrations/ packages
  - [x] Celery app wired (config/celery.py, autodiscover, celery_app in config/__init__.py)
  - [x] .env.example + local .env; full settings (PostGIS DB from DB_* env, DRF, CORS, logging, Celery beat schedule, OpenAI key/model)
  - [x] Switch DB to PostgreSQL + PostGIS; install GDAL/GEOS/PROJ (venv wheels: gdal 3.13.3 / pyproj 3.7.2 / shapely 2.1.2, cgohlke); enable django.contrib.gis; GDAL_LIBRARY_PATH/GEOS_LIBRARY_PATH/PROJ_LIB in .env
  - [x] core/models.py — all 8 models verbatim to spec (indexes + Meta)
  - [x] Initial migration created + applied — core/migrations/0001_initial.py on PG16 DB `energy_resilience` (PostGIS 3.6.2); 8 core_* tables, 4 SRID-4326 geometry cols + GiST indexes; `makemigrations --check` clean
  - [x] `manage.py check` → 0 issues; dev server starts clean (/admin/ 302 → login page renders against PostGIS). Django 6.0.8 (not 4.2), Python 3.12
  - [x] Seed data JSON (data/*.json + geometries/*.geojson) + seed_db command — 58 node rows (12 suppliers, 3 corridors w/ LineString 4326, 10 ports, 23 refineries all port-linked, 10 alternatives); idempotent via update_or_create + `--flush`; NOTE: management commands live in core/management/commands/ (Django per-app discovery), not top-level management/
  - [x] edges.json cleaned: 5 refinery-name mismatches fixed; **Suez dropped as standalone corridor** — merged into "Red Sea" (Suez Canal + Bab-el-Mandeb are serial chokepoints on the same Med/Black-Sea route); Libya/Kazakhstan moved to Red Sea, Russia split Cape/Red Sea
  - [x] Data reconciliation (FY 2025-26 baseline, sources in each JSON's `source`/`validation` fields): all supplier→corridor volumes tie to suppliers.json `avg_export_mbd` (Σ = 4.926 mb/d modelled, `share_pct` sums to 100.00); **corridor inflows: Hormuz 2.316, Red Sea 0.355, Cape 2.255 mb/d**. port_to_refinery volumes rescaled off refinery nameplate so no port's outflow exceeds its corridor-delivered inflow → TWO ceilings, kept distinct in edges.json `validation`: `corridor_throughput_mbd` 4.927 (crude reaching ports) vs `refinery_deliverable_mbd` 4.228 (hard downstream cap after refinery offtake). The 0.699 gap is crude stranded at oversupplied ports {Chennai .272, Mumbai JNPT .168, Paradip .157, Sikka .053, Vizag .049}; Vizag's is partly real (ISPR/SPR site). Use **4.228 as baseline max-flow** for criticality/reroute; score per-port criticality on *residual refinery need* after removing one corridor, not the full corridor→port edge value
  - [x] ports.json `throughput_mbd` set to sourced crude-terminal capacities (SBM/SPM + pipeline egress, cited per-port); metadata only — builder.py never uses it as an edge capacity. Mundra 0.90 / Chennai 0.25 / Mumbai JNPT 0.45 still below modelled routing (notional corridor→port split; real inland refineries also draw via Vadinar's Salaya–Mathura pipeline)
  - [x] NetworkX graph — graph/state.py (GraphState thread-safe singleton), graph/builder.py (build_graph_from_db: SOURCE→supplier→corridor→port→refinery→SINK, 50 nodes/93 edges; every edge has volume+capacity+effective_capacity; only corridor→port edges tagged `corridor`; refinery→SINK capped at nameplate = over-allocation guard), core/management/commands/build_graph.py
  - [x] tests/test_graph.py — 12 tests pass. **Baseline max-flow 4.228 mb/d = refinery-deliverable ceiling** (85.8% of 4.926 modelled supply; 0.698 unroutable by design — see two-ceiling note above). Corridor cut test (flow lost, redundancy-aware — multi-corridor ports backfill): Hormuz −1.784, Cape −1.609, Red Sea −0.202. Betweenness ranks Hormuz > Cape > Red Sea. `test_baseline_maxflow_matches_refinery_deliverable` asserts flow == Σ(port→refinery volumes), not the old ">90% of raw supply"
  - [ ] RIPPLE (do in later phases): pipeline/extract/prompt.py must map LLM "Suez" → Red Sea corridor; `/api/risk-scores/` example in this doc + ExtractedEvent.CORRIDOR_CHOICES still list Suez (CORRIDOR_CHOICES is vestigial — field is FK to Corridor, no migration needed). [DONE: CLAUDE.md's Named Corridors table updated to 3-corridor reconciled model]
- [ ] Phase 2 — News ingestion (GDELT + RSS + OFAC)
  - NOTE: Celery Beat / Celery worker / Redis are OUT OF SCOPE for this build (no always-on host; demo triggers the pipeline manually). Write `pipeline/tasks.py` as plain functions with NO Celery imports and NO `.delay()`/`.apply_async()` calls — the orchestrator calls them directly and synchronously. Adding `@shared_task` wrappers + Beat later is then a bolt-on, not a refactor.
- [ ] Phase 3 — Openai api extraction + risk scoring + graph weight update
- [ ] Phase 4 — Criticality engine (centrality + max-flow + cascading failure)
- [ ] Phase 5 — Response layer (reroute optimizer + SPR drawdown LP)
- [ ] Phase 6 — LangGraph orchestration + all REST API endpoints
- [ ] Phase 7 — Backtest validation + integration testing + API documentation

---

## Common Errors and Fixes

**PostGIS not found**

django.core.exceptions.ImproperlyConfigured: Could not find the GDAL library

Fix: Install GDAL — `brew install gdal` (Mac) or `sudo apt install gdal-bin` (Ubuntu)

**Celery task not found**

NotRegistered: pipeline.tasks.poll_gdelt

Fix: Make sure `config/celery.py` has `app.autodiscover_tasks()` and all apps are in INSTALLED_APPS

**Claude API JSON parse failure**

json.JSONDecodeError: Expecting value

Fix: Use regex to extract JSON from response — `re.search(r'\{.*\}', text, re.DOTALL)`

**NetworkX graph not building**

NetworkXError: node not in graph

Fix: Check seed data JSON — node names must match exactly between suppliers.json, corridors.json, and edges.json

**Redis connection refused**

redis.exceptions.ConnectionError: Error connecting to Redis

Fix: Start Redis server — `redis-server` in a separate terminal

---

## Important Reminders

- RawArticle rows are TEMPORARY — delete after extraction (processed=True + age > 14 days)
- ExtractedEvent rows are PERMANENT — never delete, needed for backtest
- RiskScore rows are PERMANENT — needed for historical trend charts
- Graph singleton in graph/state.py must be thread-safe (use threading.Lock)
- All REST responses must include CORS headers (django-cors-headers handles this)
- Serve corridor geometries as GeoJSON — Leaflet expects this format
- Test every component with management commands before wiring into Celery
- LLM model to be used: gpt-4o-mini
- Max tokens for extraction: 500 (JSON output is small)