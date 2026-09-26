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
demonstrated by triggering each stage manually.

**`manage.py run_pipeline` DOES NOT EXIST YET** — the file is an empty stub, as is
`orchestrator/pipeline.py`; the LangGraph orchestrator that would chain the stages
is Phase 6. Until then the pipeline is run as four commands in order:

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
[Threshold Trigger]
condition: criticality_score > 0.65
AND risk_score > 0.50
← 0.65 IS UNREACHABLE, see Threshold
  Trigger Logic section before using
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
{"Hormuz": 0.84, "Red Sea": 0.86, "Cape": 0.05}
(3 corridors — no Suez, folded into Red Sea)
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
cascading_failure_simulation("Hormuz", G=G)
(actual Phase 4 signature: corridor first, G keyword-only
 and defaulting to GraphState's copy; returns all 10 steps,
 so the view picks the step matching the requested slider %)
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
│ core_rawarticle │ Temp staging (14-day delete NOT BUILT) │
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

> **⚠ BLOCKER FOR PHASE 5/6 — `centrality_score > 0.65` CAN NEVER FIRE.**
> Betweenness centrality is normalized to [0,1], but on a graph of this shape
> (50 nodes, one dominant SOURCE→…→SINK spine) the corridor values are tiny.
> Measured on the real graph: **Hormuz 0.0756, Cape 0.0532, Red Sea 0.0238**
> — the maximum possible is roughly an order of magnitude below the 0.65
> threshold, so the condition is unreachable and the response layer would
> never trigger. The spec below is kept for reference but MUST be re-specified
> before Phase 5 wires up the reroute/SPR trigger. Options: threshold on a
> normalized *rank* (e.g. the top-ranked corridor), on `capacity_loss_mbd`
> (a physical unit, which is what `criticality/engine.py` already ranks by),
> or on centrality rescaled relative to the observed max. Do NOT simply lower
> 0.65 to 0.07 — that hardcodes a value specific to today's graph shape.
>
> **The `risk_score > 0.50` half is broken in the opposite direction, and the
> Phase 4.5 scoring fix did not rescue it.** Post-fix the scored corridors read
> Hormuz 0.615 and Red Sea 0.537 — both still above 0.50, so that clause always
> passes while the centrality clause never does, and the `AND` never fires.
> (Before the fix they were 0.99+, so this is less extreme but unchanged in
> kind.) Whether 0.50 is even the right level is now a *meaningful* question
> rather than an artefact one, since the score is bounded and interpretable:
> 0.50 corresponds to a corridor whose three worst stories average severity 2.5
> at full confidence. Re-specify BOTH clauses together before Phase 5 wires up
> the response layer.

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
   (same OpenRouter/deepseek extraction, historical articles)

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

pipeline/ingest/gdelt.py → GDELT DOC API polling (rate-limited)
pipeline/ingest/gdelt_gkg.py → GDELT GKG 15-min bulk files (un-throttled)
pipeline/ingest/rss.py → RSS feed parsing function
pipeline/ingest/ofac.py → OFAC SDN download + parse
pipeline/extract/extractor.py → OpenAI API extraction call
pipeline/extract/prompt.py → extraction prompt template
pipeline/score/risk_scorer.py → time-decay formula + normalization + story clustering
pipeline/score/candidates.py → alternative scoring statistics, for comparison only
                               (nothing in the live pipeline calls these yet)
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
core/management/commands/backfill_event_timestamps.py → seendate repair (one-off)
core/management/commands/run_pipeline.py → EMPTY STUB, Phase 6
core/management/commands/run_backtest.py → EMPTY STUB, Phase 7

---

## Repository Structure

energy-resilience/
├── CLAUDE.md ← you are here
├── API_DOCS.md ← DOES NOT EXIST YET (write it in Phase 7)
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
│ │ └── run_pipeline.py, run_backtest.py  (empty stubs)
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
    # VESTIGIAL — `corridor` below is a FK to Corridor, so these choices are
    # never enforced and 'Suez' is not a real corridor (folded into Red Sea in
    # Phase 1; extractor.CORRIDOR_ALIASES remaps it). Kept only because removing
    # it would be a no-op migration. Do not treat this list as the corridor set.
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
Response: {"Hormuz": 0.84, "Red Sea": 0.86, "Cape": 0.05}   (3 corridors, no Suez)

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

> **⚠ THE SUM BELOW WAS REPLACED ON 2026-09-26 — do not implement it.** A sum is
> unbounded in story *count*, so it measured sampling depth as much as danger:
> the same crisis scored Hormuz 16.6 via the GDELT DOC path, 44.1 via GKG and
> **75.3 via both**, which inverted the corridor ranking and pinned both scored
> corridors at ~0.99. Production now scores the **zero-padded mean of a
> corridor's 3 strongest stories**, which is bounded by 5.0 and moved **1.00x**
> across the same ingestion slices. `SATURATION_K` is off the production path.
> See the Phase 4.5 changelog and `manage.py compare_scoring`.

```python
# pipeline/score/risk_scorer.py — as built
TOP_K_STORIES = 3
MAX_EVENT_WEIGHT = 5.0          # severity 5 x confidence 1.0 x no decay

# per event, unchanged from the original spec:
weight = severity * confidence * math.exp(-0.1 * delta_days)

# per corridor: cluster syndicated coverage into stories, then take the
# zero-padded mean of the k strongest. Dividing by k even when fewer than k
# stories exist is what stops one headline reading like a sustained campaign.
raw = sum(sorted(story_weights, reverse=True)[:TOP_K_STORIES]) / TOP_K_STORIES

# normalization is a DEFINITION, not a fitted constant: 1.0 == three
# severity-5/confidence-1.0 stories today == a corridor reported closed.
score = baseline_risk + (1 - baseline_risk) * min(1.0, raw / MAX_EVENT_WEIGHT)
```

Superseded but retained: `compute_risk_score()` still returns the unbounded sum
as a diagnostic (it is what quantifies the syndication effect), and
`normalize_score()` keeps the saturating transform for re-reading `RiskScore`
rows written before 2026-09-26. Nothing in the live pipeline scores from either.

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

> **DO NOT USE THE SNIPPET BELOW — it is semantically backwards and Phase 4 does not
> implement it.** NetworkX's `weight=` is edge *distance* (lower = more central), not
> importance, so weighting by `effective_capacity` makes a corridor look *less* central
> exactly when it gets *safer*. `graph/algorithms.py` instead provides
> `structural_betweenness` (unweighted) and `capacity_weighted_betweenness` (distance-
> transformed as `1/capacity`). See the Phase 4 changelog.

```python
import networkx as nx
centrality = nx.betweenness_centrality(G, weight='effective_capacity')  # WRONG — see above
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

## LLM Extraction Prompt

Stored in pipeline/extract/prompt.py.

> **The block below is the original spec, kept for reference — the live prompt differs.**
> As built in Phase 3 it drops `"Suez"` from the corridor enum (3-corridor model; the
> model is told explicitly never to answer it), adds a 1-5 severity rubric, and tells the
> model that GDELT's `matched_corridor_query:` line is a weak hint only. Read the file,
> not this block.

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

OPENROUTER_API_KEY=sk-or-v1-your-key-here
# Optional — all have working defaults in settings.py:
# LLM_BASE_URL=https://openrouter.ai/api/v1
# LLM_MODEL=deepseek/deepseek-v4-flash-0731
# LLM_MAX_TOKENS=500
# LLM_REASONING=False
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
| GDELT DOC 2.0 | Live geopolitical events, one query per corridor (Hormuz/Red Sea/Cape) | REST API, no key needed — **rate-limited per IP, the project's most common failure** | Every 6 hours |
| GDELT GKG 2.0 | Same corpus via static 15-min bulk CSVs — the un-throttled fallback, and Phase 7's historical path | Plain file GETs, no key, **no limiter**; corridors filtered locally; ~280 MB/24h | On demand (`--source gkg`) |
| OilPrice.com RSS | General energy news — **replaces Reuters** (public RSS retired 2020) | feedparser, no key needed | Every 6 hours |
| gCaptain RSS | Shipping/tanker news — **replaces Lloyd's List** (subscription-only, no public feed) | feedparser, no key needed | Every 6 hours |
| OFAC SDN | Sanctions registry (19,388 entities as of first live pull) | CSV download from sanctions.ofac.treas.gov, cached to `data/ofac_sdn_cache.json` (gitignored) | Weekly |
| OpenRouter | LLM extraction — `deepseek/deepseek-v4-flash-0731` (**replaces** direct OpenAI gpt-4o-mini) | openai SDK with `base_url` swapped; paid per token, ~$0.00003/article | Every 6 hours |

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

> **The block below is the original spec, kept for reference — the live dict differs.**
> As built in Phase 4 each scenario carries an explicit `"mechanism"` (`"corridor"` vs
> `"supply"`) because `opec_cut` is a supply-side shock on SOURCE→supplier edges, not a
> corridor throttle, and `"degradation"` is named `"degradation_pct"` (0-100, not 0-1) to
> match `degrade_corridor`'s argument. Read the file, not this block.

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
cd backend          # all commands run from here; venv is ../venv

python manage.py runserver
python manage.py seed_db                  # load data/*.json into the DB
python manage.py build_graph              # build graph + print cut summary
python manage.py test                     # full suite (248 tests)

# ---- the pipeline, in order (no orchestrator yet — Phase 6) ----

# 1. INGEST. Poll ONE corridor at a time, minutes apart: GDELT rate-limits
#    per IP and a full 3-corridor sweep reliably trips it. A blocked corridor
#    stores nothing, so the corpus stays balanced.
python manage.py poll_sources --corridor Hormuz
python manage.py poll_sources --corridor "Red Sea"     # quote: has a space
python manage.py poll_sources --corridor Cape
#    --last-minutes N  widen the window after a gap (default 1440 = 24h)
#    --max-records N   articles per corridor (default 50, GDELT caps at 250)
python manage.py poll_sources --source rss             # or ofac / all
#    If GDELT 429s even one corridor at a time, bypass the DOC API entirely:
python manage.py poll_sources --source gkg             # ~280 MB, cannot be throttled
python manage.py poll_sources --source gkg --last-minutes 240
#    Reads GDELT's static 15-min bulk files; all 3 corridors filtered locally
#    from one identical corpus, so no corridor can be starved. NOT in --source all.
#    --last-minutes must exceed 90 (GDELT's publication lag).

# 2. EXTRACT. THE ONLY STEP THAT COSTS MONEY (OpenRouter).
#    Do NOT run until all three corridors are sampled, or the corpus skews.
python manage.py extract_events [--limit N]
python manage.py test_extraction --url "<url already in RawArticle>"  # 1 call

# 3. SCORE. Free, no API calls, safe to re-run.
python manage.py score_risk
#    Compare candidate formulas against the stored corpus. Read-only: no writes,
#    no API calls, no network. Prints volume-sensitivity per ingestion path,
#    normalized scores, and what Phase 4's criticality does under each.
python manage.py compare_scoring
python manage.py compare_scoring --scanned-per-day 61000   # true GPR denominator
python manage.py compare_scoring --as-of 2026-09-20T12:00:00Z  # reproduce old figures

# 4. CRITICALITY — the Phase 4 deliverable.
python manage.py run_criticality --port-view
python manage.py run_criticality --corridor Hormuz --cascade
python manage.py run_criticality --scenario hormuz_full
python manage.py run_criticality --rank-by centrality   # alt ranking key

# ---- one-offs ----
python manage.py backfill_event_timestamps --dry-run   # seendate repair

# ---- NOT IMPLEMENTED (empty stubs) ----
# python manage.py run_pipeline                  # Phase 6 (LangGraph)
# python manage.py run_backtest --event "..."    # Phase 7
# celery -A config worker / beat, redis-server   # out of scope, see above
```

---

## How to Run Locally (1 terminal)

```bash
cd backend && python manage.py runserver
```

That is all that is needed. The original spec called for 4 terminals (server +
Celery worker + Celery Beat + Redis); **none of the Celery stack is implemented
or required** — see the Celery Configuration section. Pipeline stages are
triggered by hand with the commands above.


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
  - [x] RIPPLE: ~~pipeline/extract/prompt.py must map LLM "Suez" → Red Sea corridor~~ [DONE in Phase 3 — prompt forbids "Suez" in its enum, `extractor.CORRIDOR_ALIASES` remaps it anyway]; `/api/risk-scores/` example in this doc + ExtractedEvent.CORRIDOR_CHOICES still list Suez (CORRIDOR_CHOICES is vestigial — field is FK to Corridor, no migration needed). [DONE: CLAUDE.md's Named Corridors table updated to 3-corridor reconciled model]
  
- [x] Phase 2 — News ingestion (GDELT + RSS + OFAC)  ✅ COMPLETE
  - NOTE: Celery Beat / Celery worker / Redis are OUT OF SCOPE for this build (no always-on host; demo triggers the pipeline manually). `pipeline/tasks.py` is plain functions with NO Celery imports and NO `.delay()`/`.apply_async()` calls — enforced by an executable guard test, not just a comment.
  - [x] **Source substitutions** — CLAUDE.md's named RSS sources don't exist anymore: Reuters retired public RSS in 2020, Lloyd's List is subscription-only with no feed. Replaced with two verified-live free feeds, same role split (general energy news / shipping-tanker news): **OilPrice.com** (`oilprice.com/rss/main`) and **gCaptain** (`gcaptain.com/feed/`). See Data Sources Reference table above for the corrected source list.
  - [x] `pipeline/ingest/gdelt.py` — GDELT DOC 2.0 API, one query per corridor (`CORRIDOR_QUERIES`: Hormuz / Red Sea / Cape, exact names matching `data/corridors.json`) rather than one OR'd keyword blob, because generic terms like "oil"/"tanker" alone starve the Cape query of its own `maxrecords` budget. `fetch_by_corridor()` returns per-corridor results (used for reporting); `fetch_all_corridors()` flattens + dedupes by URL for storage. GDELT throttles aggressively (~1 req/5s per IP, observed to be stricter in practice) — `_get_with_retry` retries 3x with 6s backoff, and `fetch_by_corridor` sleeps 5s between corridor queries. No API key needed.
  - [x] `pipeline/ingest/rss.py` — `fetch_rss_feed(feed_url, source_label)` parameterized core + `fetch_energy_news()`/`fetch_shipping_news()` thin wrappers. feedparser signals errors via `bozo` rather than raising; only escalates to a warning when bozo cost every entry (healthy feeds routinely set bozo on trivia, e.g. OilPrice's media-type header).
  - [x] `pipeline/ingest/ofac.py` — SDN CSV parsed and cached to `data/ofac_sdn_cache.json` (gitignored; **no new Django model** — it's a lookup list, not an event stream, so modeling it relationally is deferred to whichever future phase actually queries it). `is_sanctioned(name)` helper included for Phase 3's extractor. First live pull: 19,388 entities (1,540 vessels, 7,533 individuals).
  - [x] `pipeline/ingest/__init__.py` — shared `store_articles()`: dedup by URL via `get_or_create` (never resets an existing row's `processed` flag), used by both gdelt.py and rss.py.
  - [x] `pipeline/tasks.py` — `poll_gdelt()` / `poll_rss()` / `download_ofac()` (names match `CELERY_BEAT_SCHEDULE`'s documented task paths). `poll_gdelt_by_corridor()` also added, returning `{corridor: {"fetched", "stored"}}` — kept separate from `poll_gdelt()`'s plain-int contract, because collapsing "throttled/no match" (fetched=0) into "already known" (stored=0, fetched>0) would hide exactly the failure mode worth surfacing.
  - [x] `core/management/commands/poll_sources.py` — `python manage.py poll_sources [--source gdelt|rss|ofac|all]`; GDELT output shows the per-corridor breakdown so a starved corridor (real, observed under heavy testing volume) is visible instead of hiding inside a smaller total.
  - [x] `tests/test_ingestion.py` — 51 tests, no network access (unittest.mock.patch at the requests.get / feedparser.parse import sites — this is the first mocking convention introduced in the repo; documented in the module docstring for Phase 3+ to reuse). Includes a guard test asserting `pipeline/tasks.py` contains no `celery`/`shared_task`/`.delay(`/`apply_async` outside its docstring.
  - [x] Live-verified end to end: `manage.py poll_sources` run against real endpoints — 106 RawArticle rows stored (79 gdelt / 15 oilprice / 12 gcaptain at last count), re-run confirmed dedup (0 new on repeat), unicode/encoding checked clean in the DB.
  - [x] RIPPLE: ~~Phase 3's extraction prompt can read the `matched_corridor_query:` line~~ [DONE — prompt.py names the line and instructs the model to treat it as a weak hint only, judging corridor from article content]. `RawArticle.processed` cleanup (14-day deletion) still NOT implemented, and is now actionable: extraction marks rows `processed=True`, so the staging table will start accumulating deletable rows.

- [x] Phase 3 — OpenAI extraction + risk scoring + graph weight update  ✅ COMPLETE
  - [x] `pipeline/extract/prompt.py` — `EXTRACTION_PROMPT` per CLAUDE.md, with the corridor enum corrected to the 3-corridor model (`Hormuz | Red Sea | Cape | None`) and an explicit "never answer Suez" instruction (Suez folded into Red Sea in Phase 1). Added severity guidance (1-5 rubric) so severity is calibrated rather than vibes, and a line telling the model the GDELT `matched_corridor_query:` hint is weak evidence only. `build_prompt()` uses `str.replace`, NOT `str.format` — the template contains the literal JSON braces of the required-fields block, which `.format()` would raise on.
  - [x] `pipeline/extract/extractor.py` — `parse_extraction()` (JSON parse → regex `\{.*\}` fallback for fenced/prose-wrapped output → enum validation → clamping), `extract_event()` (one article, used by the test command), `extract_pending_events(limit=None)`. **The two failure modes are deliberately NOT collapsed**: a failed *call* (network/rate-limit/auth) leaves `processed=False` so the next cycle retries it, while an *unparseable answer* marks the article processed — re-asking would buy the same garbage twice. Counts returned as `{"articles", "events", "irrelevant", "unparseable", "call_failed"}`. `CORRIDOR_ALIASES` remaps Suez/Bab-el-Mandeb/Persian Gulf/etc. onto real Corridor rows.
  - [x] ~~**`ExtractedEvent.timestamp` = `RawArticle.ingested_at`** — no source gives a reliable publication date, so ingest time is the only one available for every source; at a 6-hourly cadence it is within hours of the real event.~~ **SUPERSEDED 2026-09-20 — both halves of that reasoning were wrong.** GDELT's `seendate` *is* recoverable (a one-line regex over `raw_text`, which `_build_raw_text` has always written), and the gap is days, not hours: measured across the 195 stored GDELT rows, articles arrived with seendates up to **3.86 days** before ingest, because GDELT returns coverage it first saw days earlier. `ExtractedEvent.timestamp` now uses `parse_seendate(raw_text) or ingested_at`. **The damage was concentrated, not uniform** — median correction only 0.04 days (~1 hour), so most events were fine, but **30 of 150 (20%) were dated 1+ day too recent**, the worst by 3.86 days, which under the 0.1/day decay over-weighted it by ~47%. `manage.py backfill_event_timestamps [--dry-run]` repoints existing rows by joining `ExtractedEvent.article_url` back to `RawArticle`; **that join only works until the 14-day RawArticle cleanup runs**, after which the original dates are unrecoverable. RSS rows still fall back to ingest time — `rss.py` never captured `published_parsed`, so there is nothing to recover; fixing that going forward is the remaining piece.
  - [x] Only `is_relevant=True` results are stored. A corridor-less relevant event is still stored with a NULL corridor FK (it feeds `/api/events/live/`, contributes to no corridor score).
  - [x] `pipeline/score/risk_scorer.py` — `compute_risk_score()` is CLAUDE.md's decay sum verbatim (`Σ severity × confidence × e^(-0.1Δt)`, Δt in fractional days, clamped ≥0 against clock skew). **`normalize_score()` DEVIATES from CLAUDE.md's "normalize across corridors"**: min-max normalization is *relative*, so the noisiest of the 3 corridors would read 1.0 and the quietest 0.0 in any week however calm — which would fire the `risk_score > 0.50` threshold trigger on ordinary news volume — and is undefined at cold start when all three are 0. Replaced with a saturating, absolute transform: `score = baseline_risk + (1 - baseline_risk) × (1 - e^(-raw/K))`. Monotonic in raw, sits exactly at the corridor's own `baseline_risk` when no events exist (finally giving that seeded field a consumer), saturates at 1.0 = closed corridor.
  - [x] **`SATURATION_K = 25.0` is an UNCALIBRATED guess** and the one number in Phase 3 that needs fitting against real event volume — do it in Phase 7's backtest. `RiskScore.raw_score` persists the untransformed sum precisely so recalibrating K never means paying for extraction again.
  - [x] `graph/updater.py` — `update_edge_weights(G, risk_scores)` per CLAUDE.md, scaling only the 20 `corridor→port` edges that carry a `corridor` tag (builder.py's single dynamic layer) so a corridor's risk applies exactly once. Risk clamped to [0,1] because a negative `effective_capacity` makes `nx.maximum_flow` raise rather than model a closed corridor. `volume` is never mutated — it stays the static baseline the criticality engine measures against, which also makes repeated updates idempotent. `refresh_graph_risk()` builds the singleton graph first if the process hasn't loaded one.
  - [x] `pipeline/tasks.py` — added `extract_events(limit=None)` and `score_and_update_graph()` (names match `CELERY_BEAT_SCHEDULE`'s documented task paths). Still plain functions, no Celery imports — the Phase 2 guard test still passes. Scoring persists before the graph update is attempted, so a graph failure can't lose the RiskScore history.
  - [x] Management commands (`core/management/commands/`): `test_extraction.py --url` (looks up an already-stored RawArticle — GDELT gives metadata only, there is no page to fetch; makes exactly ONE paid call and writes nothing), `extract_events.py --limit`, `score_risk.py` (free, no API).
  - [x] `tests/test_extraction.py` + `tests/test_scoring.py` — 58 tests, no network, no API spend. Mocking seam differs from Phase 2's convention and the docstring says why: the OpenAI client is built lazily inside `extractor._get_client()`, so that function (and `extractor._call_llm`) are the patch points, not a module-level import. **Full suite: 109 tests, all passing.** Includes explicit min-max regression guards (`test_quiet_corridor_is_not_zeroed_by_a_noisy_one`) and a seeded real-graph integration test to catch tagging drift between builder and updater.
  - [x] Verified against the real dev DB with zero events: all 3 corridors correctly sit at baseline (Hormuz 0.200 / Red Sea 0.150 / Cape 0.050) and all 20 corridor edges degrade accordingly. Extraction's no-key path verified to degrade gracefully (logs + reports, never crashes).
  - [x] **Provider switched to OpenRouter** (decided after the code was written): key is `OPENROUTER_API_KEY`, model `deepseek/deepseek-v4-flash-0731`. OpenRouter implements the OpenAI wire protocol, so this was a `base_url` swap on the existing openai SDK — **not** a rewrite to raw `requests`, which would have thrown away the retry/parse/error handling and all 58 tests. Verified against OpenRouter's `/api/v1/models`: the slug exists and advertises `response_format`, `structured_outputs`, `max_tokens`, `temperature`, `seed`, `reasoning`. Settings renamed to provider-agnostic `LLM_*` (`LLM_BASE_URL` / `LLM_MODEL` / `LLM_MAX_TOKENS` / `LLM_REASONING`) so going back to gpt-4o-mini is a `.env` edit; the key keeps its `OPENROUTER_API_KEY` name because it genuinely is one (`sk-or-…`).
  - [x] **`LLM_REASONING` defaults to False.** The model supports reasoning, but reasoning tokens are billed as output AND share the `max_tokens=500` budget, so a long think can truncate the JSON before it is emitted. Extraction is classification, not a task needing a scratchpad. Passed via `extra_body={"reasoning": {"enabled": ...}}` (OpenRouter-specific, not a named SDK arg).
  - [x] **LIVE-VERIFIED end to end 2026-09-13.** `test_extraction` → HTTP 200, valid JSON first try. `extract_events --limit 10` → **7 events / 10 articles, 3 irrelevant, 0 unparseable, 0 call failures** (the regex-repair fallback has not yet been needed against this model, but stays as insurance). Extraction quality is sane: corridor-shut article → `Hormuz / military / sev 5 / conf 0.95`; Houthi article → `Red Sea / military / sev 4 / conf 0.90`; two general oil-market pieces correctly got NULL corridor. `score_risk` on that corpus → **Hormuz raw 11.329 → 0.492, Red Sea raw 3.250 → 0.254, Cape 0 → 0.050 (baseline)**.
  - [x] **Full corpus extracted: 106 articles → 57 events** (49 irrelevant, **0 unparseable, 0 call failures** across the whole run — the model never once returned malformed JSON).
  - [x] **SYNDICATION BUG FOUND AND FIXED — the risk score was measuring press coverage, not risk.** One wire story is republished by many outlets under different URLs, so Phase 2's URL dedup never sees them and each becomes its own ExtractedEvent. Observed: Red Sea's 37 events were only **16 distinct stories** (one headline counted 10x, another 8x) while Hormuz's 14 events were 10 stories. The inflation was **uneven — 2.4x vs 1.3x** — so it could NOT be absorbed into `SATURATION_K`; it was enough to inflate Red Sea's lead over Hormuz from a true +0.124 to a reported +0.181. Corridor classification itself was verified correct; the bug was purely double-counting.
  - [x] **Fix: `ExtractedEvent.title` added (migration `0002_extractedevent_title`, with a RunPython backfill for the 57 pre-existing rows).** The field is required because `RawArticle` — the only other place the headline lives — is deleted at 14 days, so without it the permanent event store could not detect its own duplicates at backtest time. Bonus: `/api/events/live/` can now show headlines.
  - [x] **Dedup happens at SCORING time, not extraction time** — all 57 events are kept as evidence (the "37 articles → 16 stories" ratio is itself a citable finding) and clustered only when computing the score, so the rule stays re-tunable without re-extracting. `compute_risk_score(..., deduplicate=False)` reproduces the old inflated sum for comparison.
  - [x] Clustering rule: same corridor, headlines within `STORY_WINDOW_DAYS = 3`, `difflib.SequenceMatcher` ratio ≥ `STORY_SIMILARITY = 0.60`, each cluster contributing only its strongest member. **Threshold chosen empirically, not arbitrarily**: swept 0.40→0.80 against the real corpus and found 0.55–0.60 is a stable plateau (16/10 clusters), while **at 0.40 the corridor ranking FLIPS** (Hormuz 0.705 > Red Sea 0.671) because it over-merges distinct stories. Under-merging leaves some inflation; over-merging destroys real signal — so the threshold deliberately errs high.
  - [x] **Known limitation (tested and documented, not fixed):** lexical similarity cannot tell that "Houthis seize strategic Perim Island" and "Houthis reach strategic island at mouth of vital shipping lane" are the same event. Catching that needs semantic matching; the thresholds low enough to catch it lexically are the same ones that flip the ranking. `test_known_limitation_semantic_duplicates_are_not_caught` pins the behaviour.
  - [x] Added `DECAY_LOOKBACK_DAYS = 180` — events past it contribute e^(-18) ≈ 1.5e-8 (nothing) and excluding them bounds the O(n²) clustering against a permanently-growing event table.
  - [x] **Post-fix scores on the real corpus: Red Sea 46.021 → 0.865, Hormuz 28.167 → 0.741, Cape 0 → 0.050.** 120 tests passing.
  - [x] **K STILL UNCALIBRATED — and deliberately left at 25.0.** [OVERTAKEN BY PHASE 4.5 — K was not calibrated but removed from the production path; the formula was the problem, not the constant.] Post-dedup the scores are high (0.865 / 0.741) but arguably correct: the corpus describes Houthis seizing Perim Island and Mocha port, Saudi shutting a pipeline after a drone strike, Hormuz "effectively shut since March", Brent $104. That may genuinely be a 0.87 week. **The blocker is that every article ingested so far comes from this one crisis — there is no calm-period sample to calibrate against, and fitting K to a single crisis point would be worse than leaving it.** Phase 7's backtest supplies both crisis and calm on one scale; fit it there. Note the dedup sweep showed the corridor *ranking* is controlled by the similarity threshold, while K controls only the absolute *level* — two separable knobs. **SUPERSEDED IN PART 2026-09-20 — see Phase 2.6: after the GKG ingest, K is not merely uncalibrated but saturated (both corridors pinned at ~0.99), and a THIRD knob was found — `raw_score` scales with sampling depth, so the same crisis scores 2.5x higher purely from ingesting more articles.**
  - [ ] RIPPLE (do in later phases): severity/confidence rubric compliance is still only eyeballed (57 events, no inter-rater check). `RawArticle` 14-day cleanup is now genuinely overdue — all 106 rows are `processed=True`.

- [x] Phase 2.5 — GDELT sampling-bias fix (unplanned; forced by a Phase 3 finding)  ✅ COMPLETE
  - [x] **THE BUG: the corpus was one-sided and nothing said so.** Of 79 stored GDELT articles, the per-corridor queries had matched **Red Sea 29, Hormuz 0, Cape 0** — and a live probe confirmed all three queries were returning **HTTP 429**. `fetch_gdelt_articles` returned `[]` for *both* "throttled, never answered" and "answered, nothing matched", so a corridor missing because of rate-limiting was indistinguishable from a genuinely quiet one. Event provenance made the damage concrete: **Red Sea got 23 of its 37 events from the corridor-targeted GDELT path, Hormuz got 1 of 14.** The Red Sea > Hormuz risk ranking — the thesis's headline output — largely reflected *which query survived throttling*, not which corridor was at risk.
  - [x] **Core fix: `CorridorFetch(corridor, articles, status)` with explicit `FETCH_OK / FETCH_EMPTY / FETCH_THROTTLED / FETCH_ERROR`** and a `.sampled` property. `FETCH_EMPTY` (answered, no matches) is real evidence a corridor is quiet; `FETCH_THROTTLED`/`FETCH_ERROR` mean it was never sampled and its score is not comparable. `fetch_gdelt_result()` is the honest API; `fetch_gdelt_articles()` stays as the list-returning wrapper that discards status.
  - [x] **Fairness measures**, because throttling systematically kills whichever query runs last — which is exactly how Cape ended up with zero: corridor query order is now **shuffled every run**, and any corridor throttled on the first pass gets a **second attempt after a 30s cooldown**.
  - [x] Retry hardening: attempts 3 → 5, fixed 6s backoff → **exponential with jitter** (5s base, 60s cap), and `Retry-After` honoured when GDELT sends it. Jitter matters because all three corridor queries otherwise back off in lockstep and retry into the same rate-limit window. Inter-query delay 5s → 10s.
  - [x] **Starvation is now loud, not a debug line.** `fetch_by_corridor` logs at ERROR naming the unsampled corridors and stating scores are not comparable; `poll_gdelt_by_corridor` returns `{"fetched", "stored", "status", "sampled"}`; `manage.py poll_sources` prints `NOT SAMPLED` per corridor plus a **`CORPUS IS BIASED`** block telling the operator to re-run.
  - [x] 46 ingestion tests (up from 39), **127 total, all passing.** New coverage pins the exact bug: `test_throttled_query_is_not_reported_as_empty`, `test_answered_but_unmatched_query_counts_as_sampled`, `test_network_failure_is_error_not_throttled`, `test_retry_after_header_is_honoured`, `test_backoff_grows_between_attempts`, `test_throttled_corridor_gets_a_second_pass`, `test_corridor_query_order_is_not_fixed`.
  - [x] **RE-POLLED AND RE-EXTRACTED on a balanced corpus (2026-09-13, same day as the fix).** Live proof the fix works: on this run Hormuz was throttled 5/5 first-pass attempts, correctly marked `throttled` (not `empty`), and the **second pass recovered it in full** — all three corridors ended at 50 fetched. GDELT article counts (by corridor-query tag): Red Sea 79, Hormuz 16 (+34 pre-existing untagged legacy rows), Cape 50. Extracted the resulting 116 new articles: **93 events created, 23 irrelevant, 0 unparseable, 0 call failures.** Corpus is now 150 total events (up from 57).
  - [x] **RESULT — the sampling fix worked exactly as intended, closing the gap it was supposed to close:**

    | | biased corpus (37 vs 14 events) | balanced corpus (88 vs 55 events) |
    |---|---|---|
    | Red Sea | 0.865 | **0.981** |
    | Hormuz | 0.741 | **0.970** |
    | gap | 0.124 | **0.011** |

    Hormuz's score jumped from 0.741 to 0.970 once it received its fair share of articles instead of being GDELT-throttled out of the corpus. This is the sampling bug's fingerprint disappearing exactly as predicted.
  - [x] **Cape genuinely 0 events again — and this time it IS correct, verified by inspection, not assumed.** The Cape GDELT query fetched 50 articles fine (no throttling this run). Checked what the LLM did with all 50: **28 → Red Sea, 7 → Hormuz, 14 → irrelevant, 0 → Cape.** Titles are unambiguous ("Bab El-Mandeb," "Houthis Seize," articles about ships rerouting VIA Cape to avoid the Red Sea crisis) — GDELT's keyword search matched the word "Cape" but the LLM correctly read the actual content and reassigned the real corridor. This is `matched_corridor_query` behaving exactly as documented ("a hint, not a claim — the LLM still makes the real call") and is direct evidence the extraction step is doing real work, not rubber-stamping the query hint.
  - [x] **NEW, MORE URGENT PROBLEM SURFACED BY THE FIX ITSELF: both corridors are now saturated and the ranking has become NOISE.** A 0.011 gap between Red Sea (0.981) and Hormuz (0.970) is not a finding — the K=25 saturating transform has run out of room to discriminate once both corridors are reporting a genuinely severe, roughly comparable crisis. **The pipeline currently cannot support a claim like "Corridor X is more critical than Corridor Y."** This is not a new bug — it is the same K limitation flagged after the very first extraction, now unmasked because the sampling artifact that was previously (wrongly) creating an apparent gap is gone. Do not tune K from this single-crisis corpus; the Phase 7 backtest still needs calm-period data alongside crisis data to fit it properly (see [[project-risk-scoring-calibration]] memory).
  - [x] **SINGLE-CORRIDOR POLLING (added 2026-09-20, after live 429s made a full sweep impossible).** Observed pattern: the first query succeeds, then every later one returns 429 — including one fired **21 seconds** later, and still 429 **four minutes** on. So GDELT's "one request per 5s" notice understates it: there is a burst allowance followed by a multi-minute per-IP block, longer than `_RETRY_MAX_SECONDS = 60` can outlast. **The retry loop was making it worse** — a full `fetch_by_corridor` sweep fires up to ~21 requests in ~6 minutes (3 corridors × 5 attempts + second pass), and each blocked request re-extends the block. Fix: `gdelt.fetch_corridor(name)` issues exactly ONE request for one corridor (no inter-query delay, no second pass), `tasks.poll_gdelt_corridor(name)` fetches + stores it, and `manage.py poll_sources --corridor Hormuz` drives it. Run the three by hand minutes apart. **This preserves the Phase 2.5 fairness property, which a naive "abort the sweep on first 429" would have destroyed** — that would resample only whichever corridor happened to run first, reintroducing exactly the sampling bias Phase 2.5 fixed. Cross-corridor URL dedup still holds, just in the DB (`get_or_create` on url) rather than in the in-memory `seen_urls` set a single sweep uses; pinned by `test_separate_runs_dedupe_across_corridors_via_the_database`. An unknown corridor raises rather than reporting "not sampled", so a typo can't masquerade as throttling (`"Suez"` is the likely one — folded into Red Sea in Phase 1). 7 new tests, **184 total**.
  - [x] **EXPLICIT TIME WINDOW `lastminutes:` (2026-09-20).** GDELT's docs say the API "by default searches the last 24 hours", but the stored corpus disproves that for this mode: all 195 GDELT rows were ingested on 09-12/09-13, yet their seendates span **09-09 to 09-13**, so a query returned articles GDELT first saw 3-4 days earlier. The window is now set explicitly (`DEFAULT_LAST_MINUTES = 1440`) rather than trusting a default that measurably does not hold. **It is a GDELT *query command*, not a URL parameter** — it goes inside the query string next to `sourcelang:`; sending it as a URL param would be silently ignored and the window lost (pinned by `test_window_goes_in_the_query_not_the_url_params`). Must be a multiple of 15. Overridable via `--last-minutes` for catching up after a gap in polling (e.g. `4320` for 3 days) — important because Celery Beat is not implemented, so polling is manual and irregular, and a fixed 24h window would silently drop coverage after a pause. Secondary benefit under throttling: fewer slots wasted re-fetching already-stored articles. Also added `--max-records` (default 50, GDELT ceiling 250) to test whether smaller requests survive the limiter better — **untested, and confounded**, since the cooldown decays with time; the evidence so far points to request *spacing* mattering more than request *size* (a 50-record query succeeded, and only the second request of any size failed).
  - [x] RIPPLE: GDELT throttling is per-IP and was refusing every query during testing — a single laptop polling 3 queries every 6h may simply be near its ceiling. ~~spacing corridors across separate runs~~ [DONE — see single-corridor polling above]. ~~consider the GKG 15-minute bulk CSVs~~ [DONE 2026-09-20 — see Phase 2.6 below]. Still open if starvation persists: lowering `DEFAULT_MAX_RECORDS` (50 may itself be weighted heavily by GDELT), or accepting RSS as a corridor-agnostic supplement (RSS alone has NEVER produced a single Cape event across 27 articles — it cannot substitute for GDELT's per-corridor queries, only complement them).
  - [x] **Web NGrams 3.0 evaluated and REJECTED, for different reasons than previously recorded here.** The old note said ngrams were "word-level rather than article-level and unable to feed Phase 3's headline-similarity dedup". Measured against a real file, the first half is wrong: 3.0 records carry `url` inline plus `pre`/`post` context (~7 words each side), so they *are* resolvable to articles. The actual blockers are (a) **no title field**, which is what kills the headline dedup, and (b) **size — 14-36 MB gzipped per MINUTE** (539,000 lines, 935 distinct articles in the sample), i.e. **~30 GB/day**, roughly 100x GKG for less usable metadata. Also note `ngram` is a single word, so multi-word corridor terms (`"Red Sea"`, `"Bab el-Mandeb"`) return zero matches and would have to be matched against `pre`/`post` instead. GDELT's own "horizon scanning" workflow (grep one file, discard it) is a good fit for an always-on host; it is a bad fit here, where polling is manual and irregular and catching up on a missed day means 1,440 files.

- [x] Phase 2.6 — GDELT GKG bulk-file ingestion (unplanned; the answer to the 429s)  ✅ COMPLETE
  - [x] **Why: the DOC API limiter is not something more retry logic can fix.** `pipeline/ingest/gdelt_gkg.py` reads the same underlying corpus from GDELT's *static* 15-minute CSV drops (`data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.gkg.csv.zip`) — plain files on a CDN, no per-request limiter, and GDELT never deletes them, so **the identical code path serves live polling and Phase 7's historical reconstruction.** Filenames are deterministic from a datetime, so there is no index or search call to be throttled on.
  - [x] **Measured, not assumed** (one real slice, 2026-09-20): 3.1 MB zipped per slice, 672 usable rows, `PAGE_TITLE` on **672/672**, `PAGE_PRECISEPUBTIMESTAMP` on 377/672 (56%). A 24h window is **91 slices ≈ 280 MB** — 96 slices per day minus the newest 6, which are inside the publication lag.
  - [x] **Publication lag is real and bigger than the docs imply**: at 14:05 UTC the 13:15 slice was readable but 13:30 was not, *even though `lastupdate.txt` already named the 14:00 file*. `PUBLICATION_LAG_MINUTES = 90` skips that zone rather than spending a request per gap on a guaranteed 404. `--last-minutes` below the lag is rejected with an explanation, since it would otherwise resolve to zero slices and report as an unsampled window — true but useless.
  - [x] **GKG has no title COLUMN** — the headline lives inside V2EXTRASXML as `<PAGE_TITLE>`, extracted by regex because the field is not real XML (no root element, no escaping, so a parser rejects it). This matters beyond display: Phase 3's story dedup clusters on headline similarity, so a source with no title could not feed it at all.
  - [x] **Corridor selection moves server-side → local** (`CORRIDOR_KEYWORDS` + `TOPIC_KEYWORDS` + `TOPIC_THEMES`), mirroring the two-clause shape of each DOC query (corridor term AND topic signal — `"Red Sea"` alone matches coral-reef and tourism coverage). GKG's own theme codes are a second route past the topic gate, since a headline can describe an oil disruption without using any of our words. **This is a net WIN for the Phase 2.5 bias problem, not just a workaround: all three corridors are filtered from one identical corpus, so throttling can no longer starve one corridor relative to another.** Keywords deliberately over-match (a Riyadh missile story tags Red Sea via "Houthi") — same contract as the DOC path, where `matched_corridor_query` is a weak hint the extractor overrides.
  - [x] **Honesty property preserved and adapted.** Reuses `gdelt.CorridorFetch` / `FETCH_*` verbatim. A corridor matching nothing reports `FETCH_EMPTY` + `sampled=True` (real evidence it is quiet, since its slices were read like everyone else's), but a window where under 50% of slices were readable reports `FETCH_ERROR` for **every** corridor — otherwise a failed download masquerades as three quiet corridors, which is exactly the bug class Phase 2.5 existed to kill.
  - [x] **`raw_text` is byte-compatible with the DOC path's**, verified live: `_build_raw_text` emits the same `seendate:` line so `gdelt.parse_seendate` recovers it unchanged and Phase 3 needs **zero** changes. `PAGE_PRECISEPUBTIMESTAMP` is preferred over `V2.1DATE` when present — a real publication time, strictly better than the seendate the 2026-09-20 fix had to reverse-engineer. **GKG themes are deliberately NOT put in raw_text** even though they'd help the LLM: richer prompts for GKG rows would confound any later comparison of extraction quality between the two sources.
  - [x] **Rows stored under `source="gdelt_gkg"`, not `"gdelt"`.** The two paths select articles by different mechanisms (GDELT's search vs. our matcher), so recall differs and a mixed corpus is not homogeneous. Separate labels mean that difference can be measured in Phase 7 instead of assumed away. **State this in the write-up — a GKG-sourced corpus is not directly comparable with the existing 195 DOC-sourced rows.**
  - [x] `pipeline/tasks.py::poll_gdelt_gkg(last_minutes=None)` returns the same `{corridor: {"fetched","stored","status","sampled"}}` shape as `poll_gdelt_by_corridor`, so reporting code is shared. `manage.py poll_sources --source gkg`. **Excluded from `--source all` on purpose** — a default run must not silently pull 280 MB; pinned by `test_source_all_does_not_trigger_a_300mb_download`.
  - [x] `tests/test_gkg_ingestion.py` — 49 tests, no network (mocked at `gdelt_gkg.requests.get`, same convention as `test_ingestion.py`). **Full suite 248, all passing.** Live-verified against a real downloaded slice: 4 corridor matches from one 15-min slice (1 Hormuz, 3 Red Sea), seendate round-trip confirmed for both the precise-pub-timestamp and V2.1DATE cases.
  - [x] **FIRST LIVE RUN (2026-09-20) EXPOSED A MATCHER DEFECT — fixed, and measured against the real corpus rather than guessed at.** `--source gkg --last-minutes 240` read 11/11 slices with **zero throttling** and stored 41 articles. Hormuz's 14 were all genuinely on-topic; **Red Sea's 27 were ~24 Houthi ballistic-missile attacks on Riyadh** — Yemen-Saudi land conflict with no maritime content. Cause: `houthi` sat in `CORRIDOR_KEYWORDS` while `missile`/`attack`/`military` sat in `TOPIC_KEYWORDS`, so both clauses were satisfied by a land war. Over-matching is tolerable (the extractor overrides the hint) but at **89% of a corridor's intake it is the corpus, not a hint**.
  - [x] **Fix is a two-tier corridor gate**, not a keyword tweak. `CORRIDOR_KEYWORDS` holds unambiguous *geographic* names (Hormuz, Red Sea, Bab-el-Mandeb, Suez Canal, Gulf of Aden, Cape) and passes on a deliberately PERMISSIVE topic gate — a headline containing "Hormuz" is about the strait essentially always, so the gate only needs to reject coral reefs. `CORRIDOR_WEAK_KEYWORDS` holds *implied* corridors (`houthi`, `yanbu`, `hodeidah`, `djibouti`, …) and requires a stricter `MARITIME_KEYWORDS` signal, so a Houthi land offensive cannot enter as Red Sea shipping risk.
  - [x] **Two sub-bugs found only by measuring, both now pinned by tests.** (1) **Substring matching was wrong**: `"port"` fired on "Riyadh air*port*" and "sup*port*s measures", which was re-admitting the exact land-war stories the two-tier split removed (`"mine"` in "deter*mine*", `"close"` in "*close*ly" are the same trap). `_contains_term` now matches on word boundaries. (2) That change silently broke **plurals** — "Tankers divert from Hormuz" stopped matching `tanker` — so the pattern allows an optional trailing `s` and the term lists stay singular. (3) Also fixed: GDELT does not decode entities before writing `PAGE_TITLE`, so `&#xA0;` and `&amp;` reached both the prompt and Phase 3's headline dedup, where they depress the similarity ratio between two copies of one wire story; titles are now `html.unescape`d.
  - [x] **Result, replayed over the same 41 real titles** (keyword-only, so a lower bound — themes are not stored on RawArticle and cannot be replayed): **Hormuz 13/14 kept** (only drop is a genuinely borderline Trump/UN story) and **Red Sea 2/27 kept** — exactly the two with real maritime content ("…amid Red Sea tensions", "Houthis hit Saudi Aramco facilities in **Yanbu**", a Red Sea oil port). All 25 Riyadh land-war stories dropped. **253 tests passing.**
  - [x] **`V1LOCATIONS` / `V2ENHANCEDLOCATIONS` / `V2.1ALLNAMES` matching EVALUATED AND REJECTED (2026-09-20).** The idea was to recover the DOC API's full-text recall from columns GKG already ships — GKG extracts locations and proper names from the article *body*, which title+URL matching cannot see. Measured across **10 slices / 4,734 rows**, it fails on both precision and recall:
    - **The corridor names are essentially absent.** In 4,734 rows the only corridor-ish location names were `Red Sea, Djibouti (General), Djibouti` (×132) and `Bab El-Mandeb, , Djibouti` (×17); ALLNAMES gave `Red Sea` (×71) and `Mandeb Strait` (×12). **"Hormuz" appears ZERO times in either column** — so it adds nothing for the corridor that matters most.
    - **GDELT's "Red Sea" location is a Djiboutian ADMINISTRATIVE REGION, not the waterway.** Any article mentioning Yemen geocodes to it, making the field a proxy for "mentions Yemen" — exactly the false-positive class the two-tier gate exists to remove.
    - **Precision ~5%.** ALLNAMES added 43 articles of which ~2 were relevant; the rest included "Why the AI industry wants to be saved from itself", "Why Forex Traders Are Watching the Dollar and Yen Now", and five copies of "World leaders meet at UN". It would have tripled Red Sea intake with noise.
    - **Consequence for the DOC-vs-GKG decision:** GDELT's server-side full-text search is a capability GKG **cannot** replicate from metadata at usable precision. This is the argument for freezing rather than deleting `gdelt.py` — the DOC path remains the only route to body-text recall. Confirmed by the zero URL overlap between the two corpora.
  - [ ] **OPEN MODELLING QUESTION, deliberately not decided in code: should Houthi escalation count as Red Sea corridor risk at all?** The matcher now says "only when the story is maritime". The defensible counter-argument is that Houthi military escalation is a *leading indicator* of Red Sea shipping risk even when a given article is about Riyadh — which is how a human analyst would read it. Current behaviour is the conservative choice and matches the DOC query's shape; it also means Red Sea intake from GKG is small. Revisit when fitting `SATURATION_K` in Phase 7, since the two interact: a stricter corridor gate lowers raw scores, and K is what converts raw to normalized.
  - [ ] **The 41 rows already stored carry the OLD (buggy) `matched_corridor_query` hints and are all `processed=False`.** Nothing has been extracted, so no money has been spent and no ExtractedEvent is contaminated. Either delete them and re-poll with the fixed matcher (free, ~30s, gives a clean corpus), or extract them and rely on the LLM to reassign the 24 Riyadh stories — Phase 2.5 proved it does override bad hints, but that leaves 24 near-duplicate land-war articles in the permanent event store. **Deleting and re-polling is the recommendation.**
  - [x] **FULL 24h GKG RUN, 2026-09-20: 91/91 slices, 113 articles (Hormuz 83 / Red Sea 30 / Cape 0), zero throttling.** Extraction quality confirms the matcher fix: **106 events from 113 articles, only 7 irrelevant, 0 unparseable, 0 call failures** — a 94% relevance rate against ~50% on the DOC corpus. Red Sea's intake is now substantive rather than Riyadh noise, including *"Houthis Seize Bab al-Mandeb, Closing World's Second Oil Chokepoint"*, the most consequential single event in the corpus. Corpus now 256 ExtractedEvents (was 150).
  - [x] **The predicted uneven-dedup skew did NOT materialize** — worth recording because it was the stated worry. Hormuz 238.67 → 115.57 (**2.07x**), Red Sea 197.47 → 104.73 (**1.89x**). Near-identical ratios, so the 0.60 similarity threshold handled reworded wire copy better than expected and the truncated-title problem did not move the aggregate.
  - [x] **🚨 `SATURATION_K = 25` IS NOW DEFINITIVELY BROKEN — the score has run out of headroom entirely.** [FIXED IN PHASE 4.5 — the sum was replaced by a bounded top-3 statistic and K left the production path; the diagnosis in this item and the next was correct and led directly to the fix.] Hormuz raw 115.577 → **0.992**, Red Sea raw 104.732 → **0.987**. A **10.3% raw difference compresses to a 0.005 normalized difference** (`1 - e^(-115/25)` = 0.990), which is worse than the 0.011 gap Phase 2.5 already flagged as noise. Physical consequence, straight from `score_risk` output: `Hormuz -> Vadinar 0.855 -> 0.007` — **the model asserts Hormuz is 99.2% closed and Red Sea 98.7% closed.** There is no room left to represent an actual closure.
  - [x] **ROOT CAUSE IS BIGGER THAN K, AND IS NEW: `raw_score` is a SUM, so it scales with SAMPLING DEPTH, not just with severity.** [FIXED IN PHASE 4.5. This diagnosis was right, including the prediction that fitting K would be necessary but not sufficient.] Same crisis, two ingestion methods:

    | | DOC-only corpus | + GKG corpus |
    |---|---|---|
    | Hormuz raw | 39.4 | **115.6** |
    | Red Sea raw | 45.1 | **104.7** |
    | leader | **Red Sea** | **Hormuz** |

    The world did not become 2.5x more dangerous — we started ingesting ~2x more articles, because GKG surfaces more distinct stories per day than the DOC API's hard 50-per-corridor cap allowed. Scoring-time dedup removes duplicate *coverage* of one story; it does nothing about greater sampling *depth*. **The corridor ranking INVERTED as a result** — the same class of artefact as the Phase 2.5 sampling bug, one level up: "which corridor is riskier" depends on the ingestion method, not only on the world. **Fitting K on calm-vs-crisis data (the Phase 7 plan) is necessary but NOT sufficient — the volume-sensitivity has to be addressed too** (candidates: score on story count rather than summed weight, cap contribution per unit time, or normalize by articles sampled in the window; each has its own failure mode — e.g. a mean lets one severe event read like a sustained crisis).
  - [x] **RSS timestamp gap CLOSED (2026-09-20)** — the "remaining piece" flagged in Phase 3. `rss.py` now reads `published_parsed` / `updated_parsed` and emits the SAME `seendate:` line `gdelt.parse_seendate` reads, so Phase 3 dates RSS events from publication without knowing the source. Measured against the live feeds: **27/27 entries carry a usable date**, mean age **1.4-1.6 days**, oldest 2.24 days — so ingest-time dating was over-weighting RSS events by ~16% on average and ~25% at worst, and unlike GDELT's seendate there is nothing in an RSS row to repair it from later. 5 tests, 258 total. Note the feeds hold only ~2 days of history and there is no `--last-minutes` equivalent, so a week between manual polls loses ~5 days of RSS coverage permanently — poll RSS more often than GKG, it is free and takes seconds.
  - [x] **RSS confirmed a third time as supplement-only**: 10 events from 27 articles (17 irrelevant, 63%), of which **Hormuz +8, Red Sea +0, Cape +0** and ~2 stored with a NULL corridor. RSS has still never produced a single Cape event.
  - [x] **PHASE 4'S HEADLINE FINDING SURVIVES THE CORPUS CHANGE — real external validation.** Re-run on 266 events (77% larger than the 150 it was originally computed on) from a *different ingestion method*: `Cape 2→1 (+1), Hormuz 1→2 (-1), Red Sea 3→3`, identical to the original. The rank-shift mechanism was not an artefact of the DOC corpus. Capacity-weighted centrality also crossed over here (Cape 0.0655 > Hormuz 0.0651, from a static 0.0532 < 0.0756) — **but that crossover is knife-edge and is NOT a citable result; it flips between Hormuz risk 0.85 and 0.895 because betweenness on this graph is a step function. See the Phase 4.5 entry.**
  - [ ] **…BUT THE MAGNITUDES ARE NOW DEGENERATE AND MUST NOT BE QUOTED.** `Hormuz risk-weighted capacity_loss = 0.009 mb/d` (was 0.364) — the model says losing the Strait of Hormuz entirely would cost India nine *thousandths* of a mb/d, on a corridor carrying 2.316 mb/d of its inflow. Mechanically correct (at risk 0.996 there is nothing left to lose) and physically absurd. `risk_flow` fell 2.485 → **2.096**, i.e. **50.4% of deliverability already lost**, and 2.096 ≈ Cape's risk-weighted capacity alone (2.255 × 0.95 = 2.142) — the model has reduced India's whole supply chain to one corridor. **This is the cleanest available demonstration of WHY K needs calibrating: the mechanism is validated, the levels are worthless.** Good material for the write-up as a stated limitation; not a result.
  - [x] ~~**DO NOT PUT IN THE WRITE-UP until recalibrated:** the 0.992/0.987 figures as risk levels; any "Hormuz is riskier than Red Sea" claim (0.005 gap, and the ordering flips with ingestion method); the implied ~99% capacity loss.~~ **RESOLVED IN PHASE 4.5 by changing the formula, not by recalibrating K.** Current quotable figures: Hormuz 0.615, Red Sea 0.537, Cape 0.050, 28.1% impairment. The "Hormuz > Red Sea" ordering is now backed by a 0.078 gap that is stable across ingestion paths (ratio 1.00x), where the old one rested on 0.005 and flipped with the ingestion method. **Phase 4's rank-shift finding survived all of it** — unchanged under every one of the 8 candidate formulas tested.
  - [ ] RIPPLE: **zero URL overlap between the 41 GKG rows and the 195 DOC rows** — the two paths found completely disjoint articles, confirming that GKG genuinely adds coverage rather than re-finding what the DOC API already had, and reinforcing that the two corpora are not comparable. After the fixed matcher, a 4h window yields ~15 articles → a 24h window roughly 90, versus the DOC path's hard 50-per-corridor cap; still enough to shift `SATURATION_K` and the Phase 4 rank-shift finding, so re-check both after the first real ingest. Automatic DOC→GKG fallback is NOT wired: `--source gkg` is a deliberate manual choice, since an automatic fallback would trigger a 280 MB download from a throttle. Also observed: some `PAGE_TITLE` values are truncated at source ("Iran's Ghalibaf says", "Turkish FM Fidan says Ankara has") — GDELT's own data, not fixable here, and it degrades headline-similarity dedup for those rows.
- [x] Phase 4 — Criticality engine (centrality + max-flow + cascading failure)  ✅ COMPLETE
  - [x] `graph/algorithms.py` — pure NetworkX primitives, no GraphState/DB access (callers choose the graph): `structural_betweenness`, `capacity_weighted_betweenness`, `baseline_max_flow` / `risk_weighted_max_flow`, `degrade_corridor`, `degrade_supply`, `residual_port_criticality`, `corridor_load_bearing_ports`. `degrade_corridor`/`degrade_supply` both return a NEW graph — the input is never mutated (pinned by tests), so a simulation can never corrupt the singleton.
  - [x] **BETWEENNESS WEIGHTING — CLAUDE.md's original formula was semantically backwards and is NOT implemented as written.** The spec said `nx.betweenness_centrality(G, weight='effective_capacity')`, but NetworkX's `weight=` is edge **distance** (lower = more traversable = more central), not edge importance. Taken literally, a corridor becoming *safer* (higher `effective_capacity`) would read as a *longer* path and score as *less* central — the risk signal inverted. Fix: two distinct measures, neither claiming to be "the" centrality. `structural_betweenness` = plain unweighted (the structural/static half of the thesis comparison, and the measure `test_graph.py::test_betweenness_centrality_runs` already asserts — left untouched). `capacity_weighted_betweenness` distance-transforms **every** edge as `1/max(capacity, EPS)` before calling betweenness — uniformly across all layers, not just corridor edges, so the transform can't be accused of being cherry-picked. `test_capacity_weighted_betweenness_direction_is_correct` is the executable proof: throttling Hormuz to 99% must *lower* its weighted betweenness, which is exactly the assertion that would FAIL under the literal spec.
  - [x] **REDUNDANCY-AWARE RESIDUAL CRITICALITY — implements edges.json's own validation-block instruction** ("compute residual refinery need after removing one corridor at a time, rather than treating the full corridor_to_port edge value as always load-bearing"). `residual_port_criticality(G, port)` returns `{corridor: max(0, downstream_refinery_need - inflow_from_OTHER_corridors)}`. Verified behaviour: single-corridor ports (Kochi 0.110, Mangaluru 0.122) carry their **full** downstream need — no redundancy, no discount; heavily oversupplied Chennai discounts to **0.012 of a naive 0.483** because its corridors back each other up. This is the correction that stops naive full-edge cuts from overstating criticality at oversupplied ports and understating it at single-fed ones.
  - [x] **`stranded_mbd` is a PORT-level quantity, not the sum of per-corridor residuals** — a subtle distinction found during verification. Stranded crude = `max(0, naive_inflow - downstream_need)`; summing per-corridor residuals gives a *different* (and wrong) number because redundant corridors each discount independently. The port-level formula reproduces all five documented figures **exactly** (Chennai .272, Mumbai JNPT .168, Paradip .157, Sikka .053, Vizag .049) and is pinned by `test_stranded_volumes_reproduce_documented_figures`; the per-corridor residual is kept as its own separately-meaningful output.
  - [x] `criticality/engine.py` — `compute_criticality(G=None, rank_by="capacity_loss")` returns one row per corridor extending CLAUDE.md's `/api/criticality/` shape **additively** (every original key keeps its documented meaning; adds `rank_shift`, `static_centrality`, `static_capacity_loss_mbd`, `live_risk_score`, `residual_load_bearing_mbd`), so a Phase 6 view written against the original spec still works. `compute_port_criticality()` exposes the residual/stranded methodology at port granularity so it's inspectable in a thesis table, not buried inside one corridor number.
  - [x] **Ranking key is capacity-loss (mb/day), not centrality** — directly interpretable in a physical unit and matching the thesis abstract's framing, whereas betweenness is dimensionless. Both centralities are still reported per corridor as diagnostics, and `rank_by="centrality"` recomputes ranks off centrality for comparison in the write-up. Ties break on corridor name so ranks are deterministic across runs.
  - [x] Static ranking is computed on `volume` (which `graph/updater.py` never mutates) and risk-weighted on `effective_capacity`. `test_static_ranking_is_unaffected_by_live_risk` pins this — if risk leaked into the static half, the entire static-vs-risk delta would be meaningless.
  - [x] **THE CORE CONTRIBUTION PRODUCES A REAL, NON-TRIVIAL FINDING — and the saturation worry from Phase 2.5 turns out NOT to block it.** Current numbers, after the 2026-09-20 seendate timestamp correction (Hormuz 0.835 / Red Sea 0.860 / Cape 0.050; static flow 4.228, risk-weighted flow 2.485):

    | corridor | static_rank | risk_rank | shift | static loss | risk-weighted loss |
    |---|---|---|---|---|---|
    | Cape | 2 | **1** | **+1** | 1.609 | **2.053** |
    | Hormuz | 1 | **2** | **-1** | 1.784 | **0.364** |
    | Red Sea | 3 | 3 | 0 | 0.202 | 0.037 |

    Mechanism: Hormuz is *already* so degraded by live risk that cutting it removes little **additional** flow (0.364), while near-intact Cape has silently become the load-bearing corridor and its loss would now be the most damaging (2.053). **Phase 2.5 worried that saturated scores made the corridors indistinguishable — that is true of the *risk scores themselves* (Red Sea leads Hormuz by only 0.025, compressed by K from a 14% raw-score gap of 45.1 vs 39.4), but the rank-shift mechanism reads the graph's response to those scores, not the scores' spread, and remains discriminative.** `test_heavily_degraded_corridor_loses_rank` pins the mechanism on synthetic risk so it stays verified independent of the live corpus.
  - [x] **ROBUSTNESS: the finding is not knife-edge on the uncalibrated K.** A sweep of Hormuz risk from 0 to 0.97 (Red Sea and Cape held fixed) flips Cape into rank 1 from **Hormuz risk ≈ 0.10 onward** — so any plausible Phase 7 recalibration preserves it. Confirmed in practice: the seendate correction moved Hormuz 0.970 → 0.835 and Red Sea 0.981 → 0.860, and the ranking was unchanged (only the magnitudes moved, Hormuz's marginal loss 0.066 → 0.364).
  - [ ] **CAVEAT TO STATE IN THE WRITE-UP — the rank shift is partly STRUCTURAL, not purely a response to conditions.** Cape holds 0 extracted events and therefore sits permanently at its 0.050 `baseline_risk`. That is verified-correct (Phase 2.5 checked all 50 Cape-query articles by hand: 28 → Red Sea, 7 → Hormuz, 14 irrelevant, 0 → Cape), because news mentioning "the Cape route" is almost always news about ships *rerouting to avoid* the Red Sea — i.e. it is Red Sea risk by another name. The consequence: Cape can barely ever accumulate risk events, so once Hormuz and Red Sea carry any meaningful risk, Cape will rank 1 close to automatically. The shift is real and correctly computed, but a reviewer will rightly ask why the corridor with no data is ranked most critical; the honest answer is that Cape's criticality is *structural* (2.255 mb/d of India's inflow) and its low risk score is *evidence of quiet*, not absence of sampling — a distinction the Phase 2.5 `sampled` vs `empty` machinery exists specifically to support. **Do not present the +1 shift as a purely dynamic result.**
  - [x] ~~**Also state plainly: risk-weighted flow of 2.485 vs static 4.228 means the model asserts India has ALREADY lost ~41% of crude deliverability.**~~ **SUPERSEDED BY PHASE 4.5 — the figure is now 28.1% (4.228 → 3.039) and IS quotable**, because the ~41% (later ~50%) reading was an artefact of the summed score saturating, not a modelling claim. The underlying caveat still holds and should still be stated: `effective_capacity = volume × (1 - risk)` treats a *news-derived risk index* as a *literal physical closure fraction*, a strong assumption inherited from the original spec. What changed is that the index feeding it is now bounded and volume-insensitive, so the number is defensible as a model output rather than an artefact of how many articles were ingested.
  - [x] `criticality/cascade.py` — `cascading_failure_simulation(corridor, G=None, step_pct=10)` uses `get_graph_copy()` when `G is None` so the live singleton is never mutated. **Its baseline is deliberately DIFFERENT from engine.py's** and the docstring says so: the cascade starts *from* today's risk-weighted world and asks "what if this corridor degrades further", whereas the engine compares a static world against the risk-weighted one — the two `capacity_loss_mbd` figures are not comparable and must not be conflated in the write-up.
  - [x] `check_refineries` measures each refinery against **its own baseline realized inflow, not its nameplate `capacity_mbd`** — several refineries never run at nameplate even at baseline (grade incompatibility + the oversupply routing already documented in edges.json), so a nameplate comparison would flag them permanently and drown the real signal. `AFFECTED_THRESHOLD_PCT = 0.95` rather than 1.0 is a **numerical-stability** choice, not a modelling one: `nx.maximum_flow`'s solver leaves float dust that at 1.0 flags every refinery at every step (`test_no_refinery_is_flagged_when_nothing_changed` guards this).
  - [x] **`min_run_rate` DEFERRED, deliberately, with a documented landing spot.** A refinery going offline below its min run rate is *discrete* on/off behaviour needing an iterative re-solve (shut → re-run max-flow → re-check → repeat), a different mathematical object from this continuous max-flow model. `min_run_rate` is also still uncalibrated dead data (one hardcoded 0.70 for all 23 refineries). `check_refineries(..., min_run_rate_aware=True)` raises `NotImplementedError` so the extension is additive later rather than a rewrite; `test_min_run_rate_mode_is_explicitly_unimplemented` pins it.
  - [x] `criticality/scenarios.py` — `SCENARIOS` (hormuz_30 / hormuz_full / red_sea / opec_cut) + `run_scenario(key)`. **Two mechanisms, not one**: corridor shocks throttle corridor→port edges (`degrade_corridor`); `opec_cut` is a *supply* shock hitting SOURCE→supplier edges (`degrade_supply`, new in this phase). CLAUDE.md's original dict already implied this split by giving opec_cut a different key schema (`supply_reduction_mbd`, `corridor: None`) — this makes it explicit via a `mechanism` field. `test_supply_scenario_uses_the_supply_mechanism_not_a_corridor_cut` catches the failure mode where opec_cut is silently routed through `degrade_corridor` and becomes a no-op. Cited `price_impact_usd` figures are carried as **metadata only** and labelled in CLI output as external estimates, never as model output.
  - [x] `core/management/commands/run_criticality.py` — `[--rank-by] [--port-view] [--corridor X --cascade] [--scenario KEY] [--no-rebuild] [--ignore-risk]`. **Applies `Corridor.live_risk_score` to the edges after building** — without this a freshly-built graph has `effective_capacity == volume` and the risk-weighted ranking would silently be a second copy of the static one (a real bug caught during manual verification, exactly what the manage.py-before-API rule exists for). `--ignore-risk` opts out for structural-only inspection. Note the command writes nothing to the DB but is NOT side-effect free: it rebuilds into the GraphState singleton and mutates that graph's edges.
  - [x] **SILENT-DUPLICATION GUARD in `criticality/engine.py` — the above CLI fix alone was not enough.** The failure mode is dangerous because it is *plausible*: an un-risked graph yields `risk_rank == static_rank` and `rank_shift == 0` everywhere, which reads as a legitimate "current conditions don't shift the ranking" finding rather than as missing data. Fixing only the management command would have left Phase 6's API views — which call `compute_criticality()` against the singleton — free to reintroduce it. `_warn_if_risk_never_reached_the_edges()` logs a WARNING naming the affected corridors whenever a corridor carries a nonzero `live_risk_score` on its node but its edges are still at full `volume`. Warn, not raise: a genuinely risk-free graph is a valid thing to analyse. Three tests pin it (fires when stale, silent when risk applied, silent on a truly riskless graph). **177 tests total.**
  - [x] **Cross-validation, two independently-written paths agreeing:** engine.py's static cuts reproduce CLAUDE.md's Phase 1 figures exactly (Hormuz 1.784 / Cape 1.609 / Red Sea 0.202, baseline 4.228) and match `build_graph.py`'s own cut table; cascade.py's 100% step on a risk-neutral graph independently lands on the same 1.784, as does `run_scenario("hormuz_full")`. Cascade loss and affected-count are both monotonic across all 10 steps.
  - [x] `tests/test_criticality.py` — **47 tests, no network, no API spend. Full suite now 174, all passing** (up from 127). Pinning policy is explicit in the module docstring: *static* quantities (corridor cuts, stranded volumes) ARE hardcoded as regression anchors since they derive from committed seed data that live risk never mutates — same rationale as `test_graph.py`'s 4.228 anchor; *risk-weighted* quantities (`capacity_loss_mbd`, centrality) are **deliberately NOT** hardcoded because they move with the live corpus and the still-uncalibrated `SATURATION_K`, so pinning them would manufacture false regressions. Properties and bounds are asserted instead.
  - [ ] RIPPLE (later phases): Phase 6's `/api/criticality/`, `/api/cascade/`, `/api/scenarios/`, `/api/simulate/` are now thin serializer wrappers around `compute_criticality()` / `cascading_failure_simulation()` / `SCENARIOS` / `run_scenario()` — no internals should need to change. Phase 5's reroute/SPR layer consumes the supply gap, for which `cascade`'s `capacity_loss_mbd` and `check_refineries`' per-refinery `shortfall_mbd` are the inputs. `SATURATION_K` remains uncalibrated (Phase 7); note the rank-shift finding above is robust to K's *level* since it reads the graph's response rather than the score spread. Discrete `min_run_rate` shutdown modelling remains open.
- [x] Phase 4.5 — Risk-score volume-sensitivity fix (unplanned; unblocks Phase 4's magnitudes)  ✅ COMPLETE
  - [x] **THE DEFECT, stated precisely: `raw_score` was a SUM over stories, so it measured how deeply the corpus had been sampled as much as how dangerous the world was.** Not a calibration problem — a formula-shape problem, which is why "just tune `SATURATION_K`" could never have fixed it. Any K is a K for one corpus size.
  - [x] **Evidence, measured on the real corpus rather than argued** — `manage.py compare_scoring` (new, read-only) re-scores the stored 257 corridor-attributed events under 8 candidate formulas and prints three tables: raw statistic per ingestion slice, normalized score, and Phase 4 criticality re-run under each. The volume-sensitivity column is the ratio of the ALL-slice statistic to the widest single source:

    | candidate | ratio ALL/widest | Hormuz–Red Sea gap | Hormuz risk-weighted loss |
    |---|---|---|---|
    | `sum_sat` (old production) | **1.71x** | 0.039 | **0.087 mb/d** |
    | `log_sat` | 1.14x | 0.018 | 0.202 |
    | `share` (GPR-style) | 0.80x | 0.057 | 0.174 |
    | `mean` | 0.85x / 0.69x | 0.040 | 1.268 |
    | `top1` | **1.00x** | 0.061 | 0.806 |
    | **`top3pad` (chosen)** | **1.00x** | **0.078** | **0.833** |
    | `top5pad` | 1.00x | 0.075 | 0.873 |

  - [x] **CHOSEN: `top3pad` — the zero-padded mean of a corridor's 3 strongest stories.** Five reasons, in order of weight: (1) volume-sensitivity ratio **exactly 1.00** on real data — adding a whole second ingestion path does not move it, because it does not find anything *worse*; (2) **no free constant survives** — the statistic is bounded by `MAX_EVENT_WEIGHT = 5.0`, so `score = raw/5` is a definition (1.0 = three severity-5/confidence-1.0 stories today = a corridor reported closed), and `SATURATION_K` leaves the production path entirely; (3) widest corridor separation of any non-diluting candidate, **2x the old formula's**; (4) Phase 4 magnitudes become physical; (5) zero-padding means one moderate headline scores 1/3 of a sustained campaign of three — the distinction between "something happened" and "this is ongoing".
  - [x] **Why NOT the GPR-style `share`, despite it having the citation** ([[reference-gpr-epu-text-risk-indices]]): its denominator must be *articles scanned*, which is **not persisted** — GKG reads ~61,000 rows per 24h and stores only the ~113 that match — so it needs a schema addition before it is even the real measure. The available proxy (articles ingested) still left the scores saturated (0.92 / 0.86), still left Phase 4 degenerate (0.174 mb/d), and its own ratio moved 0.80x. `top3pad` honours GPR's *principle* — be insensitive to how much you read — by boundedness instead of by division, and needs no new column. Keep the citation for the framing; it justifies the diagnosis, not this particular estimator.
  - [x] **Why NOT `mean`, despite the healthiest magnitudes (1.268 mb/d):** it **dilutes**, and the dilution is measured, not hypothetical — its ratio is 0.85x/0.69x, i.e. combining ingestion paths *lowered* the score. Adding a harmless article would reduce a corridor's risk. Worse behaviour than the bug being fixed.
  - [x] **Why NOT `log_sat`:** confirmed to cure saturation only, not the count-vs-severity conflation. 70 minor stories → 4.443, 17 catastrophic ones → 4.454. Pinned by `test_log_compression_does_not_separate_them_either`.
  - [x] **PHASE 4'S HEADLINE FINDING IS UNCHANGED, AND NOW ROBUSTLY SO: `rank_shift` is IDENTICAL under all 8 candidates** — `Cape 2→1 (+1), Hormuz 1→2 (-1), Red Sea 3→3`. The mechanism reads the graph's structural response, not the score spread, exactly as Phase 4 claimed. This is the third independent confirmation (different corpus size, different ingestion method, now different scoring formula).
  - [x] **THE MAGNITUDES ARE NOW QUOTABLE.** New production scores: **Hormuz 2.596/5 → 0.6153, Red Sea 2.277/5 → 0.5372, Cape 0.000 → 0.0500** (baseline, 0 events). Max-flow **4.228 → 3.039 mb/d, 28.1% impaired** — replacing the indefensible 48–50%. Hormuz's risk-weighted `capacity_loss` is **0.833 mb/d** instead of 0.087, against a static 1.784 on a corridor carrying 2.316 mb/d of India's inflow. The previous entries' instruction to keep these out of the write-up is **lifted for the scores and the impairment figure**; the structural caveat on Cape (below) still stands.
  - [x] `pipeline/score/candidates.py` — the 8 candidate statistics as pure functions over clustered stories, plus the two normalizers (`saturating`, `linear_in_severity`). No DB, no network. `_top(..., pad=True)` **delegates to production's `top_k_severity`** so the harness's `top3pad` column is by construction the statistic the pipeline scores from, not a re-implementation that could drift.
  - [x] `risk_scorer.py` refactor — `cluster_stories()` / `event_weight()` / `corridor_events()` extracted so the harness and production measure the *same* stories; `top_k_severity()`, `normalize_severity()`, `compute_corridor_severity()` added. `compute_risk_score()` (the sum) and `normalize_score()` (saturating) are **deliberately kept and still tested** — the sum quantifies the syndication effect, and the saturating transform is needed to re-read pre-2026-09-26 `RiskScore.raw_score` rows.
  - [x] **`RiskScore.raw_score` semantics CHANGED — rows are not comparable across 2026-09-26.** Pre-fix rows hold the unbounded sum (tens to hundreds); post-fix rows hold the bounded top-k statistic (0–5). No migration: both are floats and the old rows are real history. **A trend chart or backtest spanning the date must not plot them on one axis** — noted in `compute_all_risk_scores`' docstring.
  - [x] `tests/test_score_candidates.py` (32 tests, DB-free) + a new `TopKSeverityTests` / `NormalizeSeverityTests` in `tests/test_scoring.py`. **Full suite 304, all passing** (up from 290). The load-bearing test is `test_more_coverage_of_the_same_week_does_not_raise_the_score`: 20 further stories leave the score bit-identical while the superseded sum grows >6x on the same fixture.
  - [x] **Test-fixture trap worth knowing, hit while writing these:** generated headlines like `"tanker incident 1"`, `"tanker incident 2"` cluster at `SequenceMatcher` ratio ≥ 0.60, so a fixture meant to hold N separate stories silently collapses to one and the arithmetic tests a different corpus than it claims. Fixed with a gibberish `_unique_title()` helper plus an `_assert_story_count()` guard on every fixture. Three tests failed this way on first run.
  - [x] **IMPORTANT CONSEQUENCE — THE FINDING IS A CAPACITY-LOSS FINDING, NOT A CENTRALITY FINDING. State it that way.** The two measures disagree for a legible reason worth writing up: capacity loss is *marginal* (how much additional flow a cut removes given today's state — Hormuz has little left to lose once heavily degraded, while near-intact Cape is carrying the load), whereas capacity-weighted betweenness is *structural* (Hormuz sits on more shortest paths, feeding 10 ports against Cape's 6). So: **Hormuz remains India's most structurally connected corridor, but is no longer the most consequential one to lose, because it is already impaired.**
  - [x] **THE CENTRALITY CROSSOVER IS KNIFE-EDGE AND MUST NOT BE LEANED ON IN EITHER DIRECTION — measured, after an initial wrong call.** It was first recorded in Phase 2.6 (Cape 0.0655 > Hormuz 0.0651) on the saturated corpus; at Hormuz risk 0.615 it was **absent** (Hormuz 0.0744 > Cape 0.0591), which was briefly written up here as "the crossover was an artefact and is gone"; at 0.895 it is **back**. Swept properly (Red Sea 0.738 / Cape 0.050 fixed), the centrality ordering flips between **Hormuz risk 0.85 and 0.895**, while the capacity-loss ordering is `Cape > Hormuz > Red Sea` across the **entire** 0.10–0.95 range. Root cause of the fragility: capacity-weighted betweenness on a 50-node graph is a **step function** — it only moves when the shortest-path structure changes, so the sweep shows it quantized at 0.0591 / 0.0634 / 0.0655 rather than varying smoothly, and a small score change can tip it. **Conclusion: rank by capacity loss (already the default), report centrality only as a diagnostic, and do not claim the centrality crossover as a result in either direction.** `--rank-by centrality` may legitimately show zero shift or a shift depending on the day's score; that is correct behaviour, not a contradiction of the headline result.
  - [x] **LIVE VALIDATION SAME DAY — a fresh 599-article GKG ingest became an unplanned natural experiment, and the fix passed it.** Corpus went 257 → 795 corridor-attributed events (Hormuz 140 → 637, Red Sea 117 → 158) in one pull. The new batch's **mean severity was LOWER than the old corpus** (Hormuz 2.65 vs 3.07) — i.e. mostly bulk coverage — yet it contained genuinely severe fresh stories. The two statistics responded completely differently:

    | Hormuz | old events only | all events | multiple |
    |---|---|---|---|
    | old summed `raw_score` | 75.2 | **263.9** | **3.51x** |
    | new `top3` statistic | 3.104 | **4.341** | 1.40x |
    | normalized score | 0.697 | **0.895** | — |

    **Under the superseded sum, 263.9 normalizes to 0.99998** (`1 - e^(-263.9/25)`), with Red Sea at 0.9928 — a **0.007 gap, straight back to noise** after one day's ingest. Under `top3pad` the same data gives 0.895 vs 0.738, a **0.157 gap — the widest and cleanest separation the project has produced.** This is the volume-insensitivity property demonstrated on live data rather than on synthetic fixtures.
  - [x] **The score rose for a defensible reason, checked by reading the actual top stories** — all severity 5, confidence 0.90, under 1 day old: *"Iran Adds Nuclear Inspections to Hormuz Reopening Offer"*, *"Tehran sets Hormuz deadline"*, *"What's in Iran's seven-day plan to reopen the Strait of Hormuz"*, and critically *"Hormuz Tanker Transits Crash to Single Digits as Crisis Deepens"* — a measured physical indicator, not rhetoric. Section 2 of the diagnostic showed **today's 81 stories alone produce the identical top3 of 4.341** that all 158 stories do, confirming the statistic is driven by the severe tail and not by accumulated bulk.
  - [x] **Syndication ratio is now 4.03x (Hormuz) vs 1.93x (Red Sea)** — up from the 2.07x/1.89x recorded in Phase 2.6, with one story counted **44 times** and another 14. The **2.1x unevenness between corridors is exactly the condition Phase 3 documented as flipping the ranking under a sum**, and under the top-k statistic it is structurally irrelevant (each cluster contributes once, and only the strongest 3 clusters are read at all). Note for the write-up: this ratio is itself a citable measurement of wire-service syndication in energy news.
  - [ ] **ONE RESIDUAL VOLUME EFFECT, honestly noted: top-k is invariant to adding stories that are not worse, but the EXPECTED top-k of n draws still rises with n** (order statistics — sample 500 articles instead of 80 and you are likelier to catch the severe tail). This is far weaker than a sum's linear growth and is bounded by `MAX_EVENT_WEIGHT`, which today's corpus is already close to (4.34 of 5.0), so there is little headroom for it to inflate further. It is nonetheless the honest limitation to state instead of claiming total volume-independence. Mitigation if it ever matters: hold the ingest window fixed (`--last-minutes 1440`) so n is roughly stationary across runs.
  - [ ] **STILL OPEN — the absolute LEVEL is not calibrated, only its shape is fixed.** Hormuz at 0.615 is a claim that ~62% of its capacity is unavailable, and that still comes from treating a news-derived index as a literal physical closure fraction (the `effective_capacity = volume × (1 - risk)` assumption inherited from the spec). What changed is the *nature* of the remaining question: it is no longer "fit an arbitrary K" but "is the mean of the 3 worst stories the right window, and is `TOP_K_STORIES = 3` right" — both answerable against Phase 7's calm-plus-crisis data, on a scale that is already interpretable. `raw_score` persistence means revisiting it never costs another extraction run.
  - [ ] **Phase 5's threshold trigger is still broken in BOTH directions and this fix does not rescue it.** `centrality > 0.65` remains unreachable (max observed 0.0756), and `risk_score > 0.50` now fires for Hormuz (0.615) *and* Red Sea (0.537) — so the conjunction never fires while the risk half alone always does. Re-specify before wiring the response layer; see the Threshold Trigger Logic section.
  - [ ] RIPPLE: `manage.py score_risk` now prints the bounded `top3/5` statistic as the score's input with the old sum beside it as a diagnostic. **The user must run `score_risk` for the new scores to reach the DB** — until then `Corridor.live_risk_score` still holds the old saturated values, and `run_criticality` will report the degenerate magnitudes. `SATURATION_K` is now vestigial on the production path but NOT removed (still used by `normalize_score` and the harness).

- [ ] Phase 5 — Response layer (reroute optimizer + SPR drawdown LP)
- [ ] Phase 6 — LangGraph orchestration + all REST API endpoints
- [ ] Phase 7 — Backtest validation + integration testing + API documentation

---

## Common Errors and Fixes

**PostGIS not found**

django.core.exceptions.ImproperlyConfigured: Could not find the GDAL library

Fix: Install GDAL — `brew install gdal` (Mac) or `sudo apt install gdal-bin` (Ubuntu)

**GDELT 429 Too Many Requests** (the most common failure in practice)

429 Client Error: Too Many Requests

The "one request per 5s" notice understates it: measured behaviour is a burst
allowance then a **multi-minute per-IP block** — a query 21s after a success
still 429'd, and was still blocked 4 minutes later. Retrying makes it worse,
since each blocked request re-extends the block.
Fix: poll ONE corridor per run, minutes apart (`--corridor`), and if a corridor
comes back NOT SAMPLED, wait longer rather than retrying. Nothing is stored on
a block, so the corpus stays balanced. Do not re-poll while debugging.
If it refuses even one corridor at a time, use `--source gkg` — the bulk files
have no limiter at all. Costs ~280 MB for 24h and stores rows under
`source="gdelt_gkg"` (different selection mechanism, see Phase 2.6).

**Celery task not found** — NOT APPLICABLE, Celery is not implemented.
`pipeline/tasks.py` is plain functions with no Celery imports, enforced by a
guard test. If you see this, something re-introduced Celery by mistake.

**LLM JSON parse failure**

json.JSONDecodeError: Expecting value

Fix: Use regex to extract JSON from response — `re.search(r'\{.*\}', text, re.DOTALL)`

**NetworkX graph not building**

NetworkXError: node not in graph

Fix: Check seed data JSON — node names must match exactly between suppliers.json, corridors.json, and edges.json

**Redis connection refused** — NOT APPLICABLE, Redis is not used. Nothing in
the implemented pipeline needs a broker.

**Risk-weighted criticality identical to static (all rank_shift = 0)**

Cause: the graph was rebuilt but risk was never pushed onto its edges, so
`effective_capacity == volume` and both rankings read the same world. This is
dangerous because it looks like a legitimate "no shift" finding.
Fix: `criticality/engine.py` logs a WARNING naming the affected corridors when
it detects this. Run `manage.py score_risk`, or use `run_criticality` which
applies stored risk itself.

---

## Important Reminders

- RawArticle rows are TEMPORARY — delete after extraction (processed=True + age > 14 days).
  **STILL NOT IMPLEMENTED.** Before building it, note that `ExtractedEvent.timestamp`
  is recovered from `RawArticle.raw_text` (GDELT seendate) — once these rows are
  deleted, historical event dates can no longer be repaired. Run
  `backfill_event_timestamps` first.
- ExtractedEvent rows are PERMANENT — never delete, needed for backtest
- RiskScore rows are PERMANENT — needed for historical trend charts
- Graph singleton in graph/state.py must be thread-safe (use threading.Lock)
- All REST responses must include CORS headers (django-cors-headers handles this)
- Serve corridor geometries as GeoJSON — Leaflet expects this format
- Test every component with management commands before wiring into Celery
- LLM provider: **OpenRouter** (OpenAI-compatible; use the openai SDK with `base_url`, never hand-rolled `requests`)
- LLM model in use: `deepseek/deepseek-v4-flash-0731` (gpt-4o-mini remains a drop-in fallback via `.env`)
- Max tokens for extraction: 500 (JSON output is small)
- Reasoning stays OFF for extraction — reasoning tokens are billed as output and share the 500-token budget, so a long think truncates the JSON