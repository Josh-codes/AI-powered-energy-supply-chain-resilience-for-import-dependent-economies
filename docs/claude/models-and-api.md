# Database models, REST API, request cycles

> Read before changing core/models.py, serializers, views or URLs. For the frontend-facing contract see also /API_DOCS.md.
>
> Split out of CLAUDE.md on 2026-09-28, text moved verbatim. CLAUDE.md holds the current summary and wins where the two differ.

> Section map (cross-references in this text still use the old CLAUDE.md section names): **Thesis Snapshot** → `thesis-snapshot.md` · **Threshold Trigger Logic / Key Algorithms / Extraction Prompt** → `algorithms.md` · **REST API Endpoints / Models / Request cycles** → `models-and-api.md` · **Development Commands / Common Errors** → `operations.md` · **Named Corridors / Scenarios / Refinery / Alternatives / Data Sources** → `data-reference.md` · **Phase 1–3, 2.5, 2.6** → `history-phases-1-3.md` · **Phase 4, 4.5, 5, 6** → `history-phases-4-6.md` · **Phase 7 / Backtest** → `phase-7-backtest.md` · **Architecture / File map / Repo structure / Tech stack / Celery** → `architecture.md`.

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
│ core_pipelinerun│ Permanent orchestrator run log + recommendations (Phase 6) │
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
    transits_corridors = models.JSONField(default=list)   # Phase 5: eligibility reads THIS
    max_incremental_mbd = models.FloatField(default=0.0)  # Phase 5: spare mb/d (ESTIMATE)
    route_geometry = models.LineStringField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} via {self.route_description}"

class PipelineRun(models.Model):                 # Phase 6 — PERMANENT
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20)     # running/succeeded/partial/failed
    options = models.JSONField(default=dict)
    stages_run = models.JSONField(default=list)
    ingest_report = models.JSONField(null=True, blank=True)
    extraction_report = models.JSONField(null=True, blank=True)
    risk_scores = models.JSONField(default=dict)
    criticality = models.JSONField(default=list)
    threshold = models.JSONField(null=True, blank=True)
    triggered_corridor = models.CharField(max_length=100, null=True, blank=True)
    capacity_loss_mbd = models.FloatField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)   # {gap, reroute, timeline, spr}
    errors = models.JSONField(default=list)
```

---

## REST API Endpoints (core/urls.py + core/views.py)

> **As built in Phase 6.** Every documented key below is kept; responses are
> supersets. Differences and additions:
> - Graph-backed views read `graph.updater.load_live_graph()` and compute live
>   (0-30 ms each on this graph). They never mutate it.
> - Errors: 400 for bad input (`{"error": ...}` for domain errors like corridor
>   `"Suez"`, DRF field errors otherwise), 503 if the graph cannot be built
>   (unseeded DB), 500 otherwise (logged).
> - `/api/risk-scores/` returns `Corridor.live_risk_score`, NOT "RiskScore rows
>   from the last hour" (that filter returns `{}` whenever `score_risk` has not
>   run within the hour).
> - `/api/cascade/`: `degradation` = show steps up to that %, default 100.
> - `/api/spr/` and `/api/simulate/`'s `spr`: `days_until_threshold` is an alias
>   of `days_of_cover`.
> - `/api/simulate/` body: `{corridor | scenario, degradation (0-1], duration_days,
>   crisis, include_sanctioned}`. Returns `{input, cascade, gap, reroute,
>   timeline, spr}` computed at the EXACT degradation, via `response/plan.py`.
> - `/api/corridors/geojson/`: Cape's 999 sentinel becomes `capacity_mbd: null`
>   + `capacity_unlimited: true`.
> - `/api/scenarios/` rows carry `key` and both `degradation_pct` and the spec's
>   `degradation` fraction (null for `opec_cut`).
> - `/api/backtest/` (Phase 7): no `event` → list of defined events with their
>   verdicts; `?event=KEY` → the report `run_backtest` wrote (404 if not yet
>   run, 400 for an unknown key); `&series=false` drops the daily series. It
>   serves the file and never computes one.
> - NEW: `GET /api/risk-scores/history/?corridor=&days=` (each row labelled
>   `formula`: `top3pad` from 2026-09-26, `sum_saturating` before; never plot
>   both on one axis), `GET /api/criticality/ports/`,
>   `GET /api/pipeline/latest/`, `/api/pipeline/runs/?limit=`,
>   `/api/pipeline/runs/<id>/`.

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

