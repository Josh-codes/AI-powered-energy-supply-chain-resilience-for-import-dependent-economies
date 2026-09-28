# Energy Supply Chain Resilience System — CLAUDE.md

B.E. major thesis project (FR. CRCE, AI & Data Science, 2026–27). Solo backend
developer: Joshua (GitHub `Josh-codes`). A teammate builds the React frontend.

This file is the **working summary**: enough to understand the project and act
correctly. The full detail (every measurement, rationale and superseded spec
block) lives in `docs/claude/`, moved there verbatim. **Read the relevant doc
before changing that area.** Where a doc and this file disagree, this file is
newer.

| Doc | Read it when… |
|---|---|
| [docs/claude/thesis-snapshot.md](docs/claude/thesis-snapshot.md) | quoting ANY number, or re-basing the write-up onto a new run |
| [docs/claude/phase-7-backtest.md](docs/claude/phase-7-backtest.md) | working on the backtest, the frontend handoff, or anything still open |
| [docs/claude/algorithms.md](docs/claude/algorithms.md) | touching scoring, centrality, max-flow, cascade, reroute, SPR, the trigger, the LLM prompt |
| [docs/claude/models-and-api.md](docs/claude/models-and-api.md) | changing models, serializers, views, URLs (frontend contract: [API_DOCS.md](API_DOCS.md)) |
| [docs/claude/architecture.md](docs/claude/architecture.md) | pipeline/component diagrams, file responsibility map, repo layout, tech stack, Celery |
| [docs/claude/operations.md](docs/claude/operations.md) | full command reference, `.env` keys, common errors, retention rules |
| [docs/claude/data-reference.md](docs/claude/data-reference.md) | corridor / refinery / alternative tables, named scenarios, external sources |
| [docs/claude/history-phases-1-3.md](docs/claude/history-phases-1-3.md) | why ingestion, extraction and dating work the way they do (Phases 1, 2, 2.5, 2.6, 3) |
| [docs/claude/history-phases-4-6.md](docs/claude/history-phases-4-6.md) | why criticality, scoring, response and the orchestrator work the way they do (4, 4.5, 5, 6) |

Figures inside the history docs are **historical**. Cite only the Thesis Snapshot.

---

## What the project is

It models **India's crude-oil import network as a risk-weighted knowledge graph**,
keeps corridor risk scores current from geopolitical news, and asks which
corridor India can least afford to lose, and what to do about it.

1. **Ingest** news (GDELT DOC API, GDELT GKG bulk files, OilPrice + gCaptain RSS, OFAC SDN).
2. **Extract** structured events with an LLM (corridor, actor, type, severity 1–5, confidence).
3. **Score** each corridor's risk from those events (time-decayed, story-deduplicated, bounded).
4. **Update the graph**: `effective_capacity = volume × (1 − risk)` on corridor edges.
5. **Criticality**: static vs risk-weighted max-flow loss per corridor, betweenness as a diagnostic, cascade simulation, port-level residual criticality.
6. **Threshold trigger** → **response**: supply gap → ranked alternative suppliers (MCDM) → replacement timeline → SPR drawdown schedule (LP).
7. **Persist** a `PipelineRun`; Django REST serves everything to the React dashboard.

**Core research contribution:** the delta between the *static* criticality ranking
and the *risk-weighted* one, i.e. how current geopolitics shifts India's structural
import vulnerability. **Headline finding:** Hormuz is the most structurally
important corridor, but it is already ~89% impaired, so losing it removes little
more; near-intact **Cape has become load-bearing (rank 2 → 1)** and is also the
corridor India can least *replace*.

**Validation (Phase 7):** point-in-time backtests of the risk signal against Brent
price moves for the 2026 Hormuz closure and the 2025 Iran-Israel standoff.

## Current status (2026-09-28)

- **Phases 1–6 complete. Phase 7's required work is complete:** both backtests done and
  read (see below); frontend handoff built: `API_DOCS.md` (checked against real
  payloads), `backend/data/api_samples/` captured at the snapshot scores,
  `backend/data/fixtures/snapshot.json.gz` dumped, `core/admin.py` registers 5 models
  read-only, `.env.example` no longer hardcodes this machine's paths.
- Test suite: **580 tests**, all passing (`python manage.py test`, from `backend/`).
- Open items: see [Open items](#open-items--known-limitations) at the bottom.

---

## Rules — read before writing any code

1. Work only inside `backend/` unless explicitly told otherwise.
2. **Never modify anything inside `frontend/`** (the teammate owns it).
3. Always use the Django ORM, no raw SQL unless absolutely necessary.
4. Never hardcode credentials; always use environment variables from `.env`.
5. All external API calls wrapped in try/except: the pipeline must never crash.
6. Use the `logging` module everywhere, never `print()` for errors.
7. Every new function gets a corresponding test in `tests/`.
8. Read and understand code before moving to the next component.
9. Follow the build-phase order; do not skip ahead.
10. When in doubt about where code belongs, check the app responsibilities below.

**Working agreements with Joshua:**
- **Never run git commands.** He stages, commits and pushes himself.
- **He runs the pipeline commands** (poll / extract / score / `--full` / backtest pulls).
  Hand him copy-pasteable commands. `extract_events` costs money, and `score_risk` /
  `--full` move the live scores.
- Tests must never hit the network or the paid API (mock the seams; see test module docstrings).

---

## Architecture as built

**Stack:** Python 3.12, **Django 6.0.8** (spec said 4.2), DRF, PostgreSQL 16 +
PostGIS 3.6 (GDAL/GEOS/PROJ via venv wheels, paths in `.env`), NetworkX, numpy,
pandas, **scipy HiGHS** (the LP solver), **LangGraph 1.2.11** (spec said 0.1),
openai SDK pointed at **OpenRouter**. venv at `../venv` relative to `backend/`.

**No Celery / Redis / Beat.** There is no always-on host. `pipeline/tasks.py` holds
plain functions (a guard test forbids Celery imports there and in `orchestrator/`).
The 6-hourly cadence in `docs/claude/architecture.md` is documentation only. The
frontend never triggers the pipeline; its only live computation is `POST /api/simulate/`.

| App | Owns |
|---|---|
| `core/` | ALL models, serializers, views, URLs, **and management commands** (`core/management/commands/`; there is no top-level `management/`) |
| `graph/` | `builder.py` (DB → DiGraph), `algorithms.py` (pure NetworkX primitives; never mutate input), `updater.py` (risk → edge weights; `load_live_graph()`), `state.py` (thread-safe singleton) |
| `pipeline/` | `ingest/` (`gdelt.py`, `gdelt_gkg.py`, `rss.py`, `ofac.py`), `extract/` (`extractor.py`, `prompt.py`), `score/` (`risk_scorer.py`; `candidates.py` = comparison harness only), `tasks.py` |
| `criticality/` | `engine.py` (static vs risk-weighted ranking), `cascade.py`, `scenarios.py` |
| `response/` | `trigger.py`, `gap.py`, `reroute.py`, `spr.py`, `plan.py` (`build_response`, shared by CLI, API and orchestrator) |
| `orchestrator/` | `pipeline.py` (LangGraph graph, `run_pipeline()`), `nodes.py` (never raise; errors go into state), `state.py` |
| `backtest/` | `runner.py`, `validator.py` (pure), `eia.py` (Brent, cached in `data/brent_cache.json`) |

**The graph** (50 nodes / 93 edges): `SOURCE → supplier → corridor → port → refinery → SINK`.
12 suppliers, **3 corridors**, 10 ports, 23 refineries (+10 alternative suppliers
in their own table). Only the 20 corridor→port edges carry a `corridor` tag, and
they are the only dynamic layer. `volume` is never mutated (it is the static
baseline); `effective_capacity` carries risk. Refinery→SINK is capped at nameplate.
- **Baseline max-flow 4.228 mb/d** = refinery-deliverable ceiling (modelled supply
  is 4.926; the 0.698 difference is crude stranded at oversupplied ports, by design).
- Static single-corridor cuts: Hormuz −1.784, Cape −1.609, Red Sea −0.202.

**Corridors (3, Suez folded into Red Sea):**

| Corridor | Global capacity (mb/d) | India flow (mb/d) | Baseline risk |
|---|---|---|---|
| Hormuz | 17.0 | 2.316 | 0.20 |
| Red Sea (incl. Suez + Bab-el-Mandeb) | 5.8 | 0.355 | 0.15 |
| Cape | 999.0 sentinel (unlimited) | 2.255 | 0.05 |

**Models** (`core/models.py`): `Supplier`, `Corridor` (LineString, `live_risk_score`),
`Port`, `Refinery` (Points), `RawArticle` (staging), `ExtractedEvent` (has `title`),
`RiskScore`, `AlternativeSupplier` (`transits_corridors`, `max_incremental_mbd`),
`PipelineRun` (one row per orchestrator run, outputs as JSON). Migrations 0001–0004.

---

## Deviations from the original spec — do NOT "fix" these back

The original spec text is preserved in the docs, but these parts of it are wrong or superseded:

| Area | Spec said | As built, and why |
|---|---|---|
| Corridors | Hormuz / Red Sea / Suez / Cape | **3 corridors**; Suez is on the Red Sea route. `extractor.CORRIDOR_ALIASES` remaps "Suez". `ExtractedEvent.CORRIDOR_CHOICES` is vestigial. `"Suez"` as input raises / 400s. |
| News sources | Reuters, Lloyd's List | Dead / paywalled → **OilPrice.com + gCaptain RSS**. Plus **GDELT GKG bulk files** as the un-throttled path. |
| LLM | Claude / gpt-4o-mini | **OpenRouter, `deepseek/deepseek-v4-flash-0731`** via openai SDK `base_url`. `LLM_*` settings; `max_tokens` 500; **reasoning OFF**. ~$0.00003/article. |
| Event date | ingest time | **GDELT `seendate` / RSS publish date** (`parse_seendate(raw_text) or ingested_at`). Ingest time was up to 3.86 days late. |
| Risk score | decay **sum**, min-max normalized | **Zero-padded mean of the 3 strongest *stories*, ÷ 5.0.** A sum tracked sampling depth (inverted rankings, saturated at 0.99). See formula below. |
| Betweenness | `weight='effective_capacity'` | **Backwards** (NetworkX weight = distance). Use `structural_betweenness` (unweighted) and `capacity_weighted_betweenness` (distance = 1/capacity). |
| Ranking key | centrality | **Capacity loss (mb/d)**. Centrality is a diagnostic only; its ordering is knife-edge. |
| Trigger | centrality > 0.65 AND risk > 0.50 | Never fired. Now **loss > 15% of risk-weighted flow OR risk > 0.75**. |
| SPR LP | PuLP/CBC, hard reserve constraint | Infeasible exactly when needed; CBC breaks with redirected output. **scipy HiGHS**, `unmet[t]` slack, lexicographic objective (unmet → peak shortfall → early tie-break), **per-day gap profile**. |
| Reroute eligibility | `avoids_corridor` | Every row said "Hormuz". Now **`transits_corridors`** (eligible = does not transit the disrupted corridor). Russia Urals `sanctioned=true`. |
| Pipeline state | `raw_articles` / `extracted_events` lists | `ingest_report` / `extraction_report` (every stage persists to Postgres). Options travel in `config["configurable"]`. |
| `/api/risk-scores/` | RiskScore rows from the last hour | `Corridor.live_risk_score` (the old filter returned `{}`). |
| Scheduling | Celery Beat every 6 h | Manual: `run_pipeline` or the stage commands. |
| Backtest scoring | same scorer | Same scorer with **`exclude_future=True`** (without it, 2026-02-11 scored 0.947 from September news). Writes no `RiskScore` rows. |

---

## Key formulas (as built)

```python
# Per event (unchanged from spec); delta_days fractional, clamped >= 0
weight = severity * confidence * exp(-0.1 * delta_days)

# Stories: same corridor, headlines within STORY_WINDOW_DAYS=3,
# difflib ratio >= STORY_SIMILARITY=0.60 -> one story, weight = its strongest member.
# risk_scorer.StoryClusterer is the ONE implementation (production + backtest).
# corridor_events() returns events in (timestamp, id) order; clustering is order-dependent.
raw   = sum(sorted(story_weights, reverse=True)[:3]) / 3      # TOP_K_STORIES = 3, zero-padded
score = baseline_risk + (1 - baseline_risk) * min(1.0, raw / 5.0)   # MAX_EVENT_WEIGHT = 5.0
# DECAY_LOOKBACK_DAYS = 180

effective_capacity = volume * (1 - risk)            # corridor->port edges only, risk clamped [0,1]

# Threshold (response/trigger.py): per corridor
crossed = capacity_loss_mbd / risk_weighted_baseline_flow > 0.15  or  live_risk_score > 0.75
triggered_corridor = crossed corridor with the LARGEST loss fraction (by value, not row order)

# Reroute MCDM: score = 0.40*cost + 0.35*transit + 0.25*compat   (severe: 0.20/0.55/0.25)
# compat = share of refinery nameplate whose API/sulfur window admits the crude.
# Scores are normalized over the candidate set, so NOT comparable across corridors.

# SPR: 36.87 mb total, 20% safety floor, max 1.0 mb/d release.
```

- `compute_risk_score()` (the old sum) and `normalize_score()` (saturating, `SATURATION_K`)
  still exist, as diagnostics and to re-read old rows. **Nothing scores from them.**
- **`RiskScore.raw_score` changed meaning at `TOP_K_FORMULA_SINCE` = 2026-09-26 00:00 UTC**
  (ids ≤ 33 hold the sum, ≥ 34 the top-3 statistic). Never plot across the boundary;
  `/api/risk-scores/history/` labels each row's `formula`.
- **Two capacity-loss baselines, never conflate them:** `engine.py` compares static vs
  risk-weighted worlds; `cascade.py` and `gap.py` start *from* today's risk-weighted
  world (gap = extra flow lost on top of today's risk).

---

## Thesis Snapshot — the only figures to cite (summary)

Full version, with where each number lives in the JSON: [docs/claude/thesis-snapshot.md](docs/claude/thesis-snapshot.md).

**Cited runs: `PipelineRun 3`** (`run_pipeline --full`, wrote `RiskScore` ids 43–45 at
2026-09-27 07:01 UTC, 14-day response), **`4`** (30-day) and **`5`** (60-day). All three
share one set of scores. Read figures from the persisted runs
(`GET /api/pipeline/runs/3/`), **not** from `run_criticality` / `run_response`, which
read the *mutable* `live_risk_score`. The figures chain (risk → flow → loss → gap →
SPR), so never mix numbers from different runs. Re-basing onto a later run means
moving every figure in one pass.

| corridor | top-3 raw (/5) | risk | static → risk rank | static loss | risk-weighted loss | trigger |
|---|---|---|---|---|---|---|
| Cape | 0.000 | 0.050 | 2 → **1** (+1) | 1.609 | **2.048** | loss clause, 85.4% — **TRIGGERED** |
| Hormuz | 4.300 | **0.888** | 1 → **2** (−1) | 1.784 | 0.247 | risk clause only (loss 10.3%) |
| Red Sea | 3.479 | **0.741** | 3 → 3 | 0.202 | 0.069 | none (2.9%; **0.009 below** the 0.75 bar) |

- Max-flow **4.228 → 2.400 mb/d, 43.2% of deliverability impaired.**
- Corpus: 1,629 corridor-attributed events (Hormuz 1,465 → **227 stories**, Red Sea 164 → **88**, Cape 0). Always give story counts next to event counts (syndication ~6.5x on Hormuz).
- **Volume-insensitivity evidence:** corpus 795 → 1,629 events, Hormuz top-3 moved 4.222 → 4.300 (**1.018x**); the old sum reads 398.5 → normalizes to exactly 1.0000.
- **Response:** Cape gap **2.048 mb/d**; six eligible alternatives cover **63%**, leaving **0.748 mb/d** open after day 16. SPR covers **57.4%** over 14 d (run 3), **71.6%** over 30 d (run 4), **50.0%** over 60 d and **runs dry on day 34** (run 5). Hormuz gap 0.247: UAE via Fujairah closes it by day 8, SPR bridges it fully (1.98 mb).
- **Cite capacity loss, never centrality** (risk-weighted centrality flips between Hormuz and Cape within the 0.85–0.895 risk band).
- **Caveats to state:** (1) severity is direction- and tense-blind; (2) semantic duplicates reach the top 3 (Hormuz 0.888 → ~0.876 without); (3) risk is treated as a physical closure fraction; (4) Cape's rank shift is partly structural (0 events, correctly); (5) Red Sea has a thin story base and sits 0.009 under the bar; (6) coverage rests on estimated `max_incremental_mbd`.
- Scores decay daily with no new data, another reason the CLI drifts from this table.

## Backtest results (Phase 7) — what may be cited

Full detail and the pre-registered readings: [docs/claude/phase-7-backtest.md](docs/claude/phase-7-backtest.md).
Reports: `backend/data/backtests/<event>.json`. Days scored point-in-time at 16:30 UTC.
Price event = first Brent session ≥ 5% (from EIA prices alone).

- **2026_hormuz_closure** (chart 2026-01-01 → 03-18; price event **2026-03-02, +8.30%**).
  Validator says PASSED / 15-day lead, but it is read under pre-registered reading 2:
  **cite the verdict, cite NO lead time.** Crossed 0.6 on Jan 28 (co-moving with January's
  gradual +18% repricing), one day below on Feb 14 (0.578), then unbroken to Mar 18. The
  closure itself (Sat Feb 28, 0.730 → 0.946) is **detection, not lead**. Ceiling 0.93–0.96
  while Brent went $77 → $118: it detects a crisis, it does not measure depth. Don't call
  Jan 28–Feb 13 a "false alarm" in prose. Final: no further window widening.
  Earlier Feb-11 window archived as `2026_hormuz_closure.window_feb11.json`.
- **2025_iran_standoff** (price event **2025-06-13, +7.28%**): **INCOMPLETE, permanently**.
  GDELT publishes no GKG files for 2025-06-15 → 07-01. Scores in or after the gap are
  lower bounds. Signal crossed **Jun 12** (0.615) on pre-strike warnings, a 1-day lead,
  but Brent had already moved +4.21% on Jun 11. It did **not** fall back after the ceasefire
  (retrospective stories rated like live threats). **Never cite the Jun 15 → Jul 1 decline
  as a fall-back** (it is decay over missing data).
- **Sensitivity to `TOP_K_STORIES` (3 vs 5): robust.** Both verdicts and readings unchanged
  (`<event>.top5.json`). Leads move one day each: 2025 is **1 day at k = 3, 0 days at k = 5**
  (cite both); 2026 validator 15 → 14 (still not cited). k lowers thin-base days (2025 Jun 12
  0.615 → 0.449) but not crisis levels (peak 0.959 → 0.958): it trades early sensitivity for
  caution, it does not change what is detected. Production stays k = 3.

---

## Commands (run from `backend/`)

```bash
python manage.py runserver                # the only process needed locally
python manage.py test                     # full suite
python manage.py seed_db                  # static data only: leaves live_risk_score = 0.0 (see traps)

# Orchestrated (LangGraph), one synchronous process, writes one PipelineRun:
python manage.py run_pipeline             # ANALYSIS ONLY on stored scores: free, snapshot-safe
python manage.py run_pipeline --duration-days 30   # pin another cut as its own run and cite its id
python manage.py run_pipeline --full      # = --ingest rss,gdelt-fallback --extract --score
                                          #   PAID (~$0.025) + ~40 min + moves live scores

# Stage by stage (the careful path):
python manage.py poll_sources --corridor Hormuz      # one corridor per run, minutes apart (GDELT 429s)
python manage.py poll_sources --source rss | gkg     # gkg = ~280 MB/24h, cannot be throttled
python manage.py extract_events [--limit N] [--workers N]   # THE ONLY PAID STEP
python manage.py score_risk                           # free; writes RiskScore + live_risk_score
python manage.py run_criticality --port-view          # reads live_risk_score, writes nothing
python manage.py run_response --auto                  # trigger -> gap -> reroute -> SPR, writes nothing
python manage.py compare_scoring                      # read-only formula comparison (--as-of has a lookahead defect)

# Backtest
python manage.py run_backtest --list | --event KEY --status | --event KEY --pull | --event KEY
python manage.py run_backtest --event KEY --top-k 5   # sensitivity run -> <event>.top5.json, never the cited report

# Frontend handoff
python manage.py capture_api_samples [--run-id 3]     # read-only; writes data/api_samples/
```

Full reference with every flag: [docs/claude/operations.md](docs/claude/operations.md).

---

## Traps and invariants

- **`seed_db` alone yields a silently wrong world**: `live_risk_score` stays 0.0, every
  corridor reads green and `rank_shift` is 0 everywhere, with no warning. A fresh DB needs
  the committed fixture: **`migrate` → `python -X utf8 manage.py loaddata
  data/fixtures/snapshot.json.gz`, with NO `seed_db`** (seed_db's pks need not match the
  fixture's, so running it first collides on the unique corridor names).
- **Risk-weighted == static (all `rank_shift` 0)** usually means risk never reached the
  edges. `engine.py` warns; `run_criticality` applies stored risk itself.
- `graph.updater.load_live_graph()` detects cross-process staleness by comparing stored
  `live_risk_score` to the graph's node scores (`Corridor.updated_at` does NOT move when
  `score_risk` runs), then fixes a copy and swaps it in. Views never mutate the graph.
- `degrade_corridor` silently no-ops on a bad name, so `gap.py` and the views validate the
  corridor (unknown → raise / 400).
- **GDELT DOC API 429s**: a burst allowance, then a multi-minute per-IP block that each
  retry extends. Poll one corridor at a time, or use GKG. A throttled corridor is
  `FETCH_THROTTLED` (not sampled), never `FETCH_EMPTY` (sampled, quiet).
- GKG rows are stored as `source="gdelt_gkg"`, a different selection mechanism from the DOC
  path. Mixing them is safe only because the top-3 statistic is volume-insensitive.
- `prompt.py` fills the template with `str.replace`, not `.format` (literal JSON braces).
- Extraction: a failed *call* leaves `processed=False` (retried); an *unparseable answer*
  marks it processed.
- Backtest windows must end on or before 2026-03-31, so they never enter a live score's
  180-day lookback. Backtest events live in the same `ExtractedEvent` table.
- Windows console: keep CLI output ASCII (em-dashes render as `�`). `.geojson` is built from
  GEOS coords because GDAL/OGR logs a PROJ `proj.db` mismatch on every call.
- **Permanent tables, never delete rows:** `ExtractedEvent`, `RiskScore`, `PipelineRun`.
  `RawArticle` is staging (14-day cleanup NOT built; run `backfill_event_timestamps` before
  ever deleting, since event dates are recovered from `raw_text`).

---

## Open items / known limitations

- **Phase 7:** gpt-4o-mini hindsight control on February articles; optional signal-vs-Brent
  co-movement analysis; plot the live risk time series (from 2026-09-26 only) as a deliverable;
  semantic story clustering; the Red Sea maritime-gate
  question (does Houthi land escalation count as corridor risk?).
- **Fixture restore not yet verified on a clean DB** (scratch-DB check in
  `docs/claude/phase-7-backtest.md`). Re-dump with `python -X utf8 manage.py dumpdata core
  --exclude core.RawArticle --indent 2 -o data/fixtures/snapshot.json.gz` whenever the
  thesis is re-based.
- `README.md` is stale (Django 4.2, gpt-4o-mini, Celery/Redis 4-terminal setup, `seed_db`
  setup, top-level `management/`). `requirements.txt` is unpinned and lists unused
  packages (PuLP, celery, redis, django-celery-beat, langchain).
- `AlternativeSupplier.route_geometry` is NULL everywhere (map cannot draw reroutes); no
  ports/refineries GeoJSON endpoints.
- `max_incremental_mbd` is estimated, not reconciled: coverage figures are indicative.
- `min_run_rate`-aware refinery shutdown unimplemented (`check_refineries` raises `NotImplementedError`).
- `opec_cut` does not model OPEC membership of alternatives. Kuwait-via-Shuaiba's
  "avoids Hormuz" claim is dubious (Shuaiba is inside the Gulf) but preserved.
- `compare_scoring --as-of` still admits future events.
- `RawArticle` 14-day cleanup unbuilt.

## Maintaining this file

Keep this file a summary. Put new measurements, rationale and changelog entries in the
matching `docs/claude/` file and update only the affected lines here (status, snapshot,
deviations, open items). When the thesis is re-based onto a new run, update this summary
and `docs/claude/thesis-snapshot.md` together.
