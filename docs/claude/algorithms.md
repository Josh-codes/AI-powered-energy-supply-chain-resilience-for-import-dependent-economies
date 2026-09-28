# Algorithms, formulas, threshold trigger, extraction prompt

> Read before touching risk scoring, betweenness, max-flow, cascade, MCDM reroute, the SPR LP, the trigger or the LLM prompt. Several blocks here are the ORIGINAL SPEC and are marked superseded: never implement a block without reading the warning above it.
>
> Split out of CLAUDE.md on 2026-09-28, text moved verbatim. CLAUDE.md holds the current summary and wins where the two differ.

> Section map (cross-references in this text still use the old CLAUDE.md section names): **Thesis Snapshot** → `thesis-snapshot.md` · **Threshold Trigger Logic / Key Algorithms / Extraction Prompt** → `algorithms.md` · **REST API Endpoints / Models / Request cycles** → `models-and-api.md` · **Development Commands / Common Errors** → `operations.md` · **Named Corridors / Scenarios / Refinery / Alternatives / Data Sources** → `data-reference.md` · **Phase 1–3, 2.5, 2.6** → `history-phases-1-3.md` · **Phase 4, 4.5, 5, 6** → `history-phases-4-6.md` · **Phase 7 / Backtest** → `phase-7-backtest.md` · **Architecture / File map / Repo structure / Tech stack / Celery** → `architecture.md`.

---

### Threshold Trigger Logic

> **RE-SPECIFIED IN PHASE 5 (`response/trigger.py`).** The original condition
> `centrality_score > 0.65 AND risk_score > 0.50` could never fire, for three
> measured reasons: (1) corridor betweenness on this graph is 0.02-0.08, an
> order of magnitude below 0.65; (2) post-Phase-4.5 risk scores (Hormuz 0.615,
> Red Sea 0.537) always pass 0.50, so that clause discriminates nothing; and
> (3), found during Phase 5, **any `AND risk > X` gate vetoes the corridor the
> engine ranks most critical.** Cape loses the most flow precisely *because* it
> is intact, and it sits at risk 0.050. `test_original_spec_threshold_could_never_fire`
> and `test_low_risk_corridor_can_still_trigger` pin all of this.

As built, a corridor crosses when EITHER clause holds:

```python
LOSS_FRACTION_THRESHOLD = 0.15   # of risk-weighted baseline max-flow
RISK_ALONE_THRESHOLD    = 0.75   # OR-clause, not AND

loss_fraction = capacity_loss_mbd / risk_weighted_baseline_flow
crossed = loss_fraction > 0.15  OR  live_risk_score > 0.75
triggered_corridor = the crossed corridor with the LARGEST loss_fraction
```

- **A fraction, not an absolute mb/d.** An absolute figure would hardcode
  today's graph size the same way 0.65 hardcoded today's graph shape. At
  ~3.04 mb/d, 15% is ~0.46 mb/d, which is roughly a week of SPR drawdown at the
  1.0 mb/d physical limit. This is a *policy* threshold with a stated physical
  meaning, not a fitted constant.
- **Risk is an OR-clause, not a gate.** It is already inside
  `effective_capacity`, so gating on it double-counts. As an independent
  clause at a high bar (0.75 is roughly three severity-4 stories at full
  confidence today) it still lets an emerging crisis on a structurally minor
  corridor fire.
- **The choice is made by value, not by input order**, so the trigger does not
  depend on how the criticality rows happen to be sorted.

On the live DB scores (Hormuz 0.888 / Red Sea 0.741 / Cape 0.050, after
`run_pipeline --full` on 2026-09-27 — the Thesis Snapshot), with a
risk-weighted baseline of 2.400 mb/d:

| corridor | loss (mb/d) | of flow | risk | crosses |
|---|---|---|---|---|
| Cape | 2.048 | 85.4% | 0.050 | **loss clause, triggered** |
| Hormuz | 0.247 | 10.3% | 0.888 | **risk clause only** |
| Red Sea | 0.069 | 2.9% | 0.741 | no |

The trigger discriminates: it neither never fires nor always fires. **Both
clauses are live on real data.** Hormuz is already 88.8% degraded, so cutting it
removes little *additional* flow and the loss clause misses it. The risk
OR-clause is what catches it, which is exactly the case it exists for. Note
Red Sea sits **0.009** below the risk bar, the closest it has come across four
scoring runs (0.738 → 0.012 below, 0.722 → 0.028, now 0.741 → 0.009), so a
modestly worse week would add it — quote that margin rather than presenting Red
Sea as a clean non-crossing.
(The test suite uses synthetic Hormuz 0.615 / Red Sea 0.537, under which Cape
65.2% and Hormuz 27.4% cross on loss. Those are fixture values, not today's.)

`check_threshold(rows, baseline_flow_mbd)` is the pure core. `evaluate_graph(G)`
runs `compute_criticality` + `risk_weighted_max_flow` and calls it. Its
`threshold_crossed` / `triggered_corridor` keys are the two PipelineState
fields Phase 6's orchestrator reads.

**Persistence: built in Phase 6 as `PipelineRun`** (one row per orchestrator
run, outputs as JSON snapshots). The trigger is invoked by the orchestrator's
`check_threshold` node, which calls the pure `check_threshold` directly, and
the result lands in `PipelineRun.threshold` / `.response`.


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

> **The block below is the original spec, kept for reference. `response/spr.py`
> differs in three ways.** (1) The spec's LP has no optimization freedom: every
> variable is pinned, and `sum(release) <= available` is a hard constraint, so
> whenever the reserve cannot cover the gap the problem is infeasible and every
> day comes back `None`. The built version adds an `unmet[t]` slack, so it always
> solves and `insufficient` is a computed result. (2) The objective is *minimize
> unmet gap, then minimize the peak daily shortfall*. When the reserve binds it
> spreads the shortfall evenly instead of letting the solver pick an arbitrary
> vertex. The spec's "minimize total drawdown" term is dropped because it is
> inert under `release + unmet == gap`. (3) The solver is scipy HiGHS, not
> PuLP/CBC. The four documented return keys are kept, and `days_of_cover` is
> what `/api/spr/` calls `days_until_threshold`.

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

