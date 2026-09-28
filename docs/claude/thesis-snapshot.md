# Thesis Snapshot (full)

> THE figures to cite, where they live in PipelineRun 3/4/5, and the six caveats. Read in full before writing or quoting any number.
>
> Split out of CLAUDE.md on 2026-09-28, text moved verbatim. CLAUDE.md holds the current summary and wins where the two differ.

> Section map (cross-references in this text still use the old CLAUDE.md section names): **Thesis Snapshot** → `thesis-snapshot.md` · **Threshold Trigger Logic / Key Algorithms / Extraction Prompt** → `algorithms.md` · **REST API Endpoints / Models / Request cycles** → `models-and-api.md` · **Development Commands / Common Errors** → `operations.md` · **Named Corridors / Scenarios / Refinery / Alternatives / Data Sources** → `data-reference.md` · **Phase 1–3, 2.5, 2.6** → `history-phases-1-3.md` · **Phase 4, 4.5, 5, 6** → `history-phases-4-6.md` · **Phase 7 / Backtest** → `phase-7-backtest.md` · **Architecture / File map / Repo structure / Tech stack / Celery** → `architecture.md`.

---

## Thesis Snapshot — the ONE set of figures to cite

> **Every number in the write-up comes from one `PipelineRun`.** Figures quoted
> in the Build Phases changelog below are historical: each was accurate for the
> corpus and date it was measured on, which is why they differ from this table.
> Cite them only as history (e.g. "before the Phase 4.5 fix the score read
> 0.99"), never as current results.

**THE CITED RUNS ARE `PipelineRun 3` (scores + 14-day response), `4` (30-day)
and `5` (60-day).** That is what "the snapshot" means — persisted rows, not the
current state of the database. All three share one set of risk scores.

**Why pin a run at all, when the system is supposed to keep re-scoring?** Not
for freshness. Because the figures *chain*: risk 0.888 → `effective_capacity`
→ risk-weighted flow 2.400 → Cape loss 2.048 (85.4% *of that flow*) → gap 2.048
→ 28.67 mb required → SPR 57.4%. Quote risk from one run and the gap from
another and the arithmetic stops closing — 2.045 is not 85.4% of 2.400 — which
is precisely what an examiner checks. The constraint is **internal consistency,
all-or-nothing**, not recency.

**So keep running `--full`.** It cannot damage the snapshot: `RiskScore` rows
and `PipelineRun` rows are both permanent, so run 3 is frozen in the DB
regardless of what runs after it. Re-basing the write-up onto a later run is a
deliberate act — when you choose to, move **every** figure together and update
this section's run id, row ids and timestamp in one pass.

**The run sequence is itself a deliverable, not a nuisance.** This is a
continuous-monitoring system, and a single frozen number slightly undersells
that. Hormuz 0.895 → 0.875 → 0.888 while Red Sea creeps 0.738 → 0.722 → 0.741
toward the 0.75 trigger bar demonstrates the thing the thesis claims the system
does. `GET /api/risk-scores/history/` already serves it. **Two constraints:**
the series is only valid from `TOP_K_FORMULA_SINCE` (2026-09-26) because earlier
`raw_score` rows hold the superseded sum — the endpoint labels every row with
`formula`, so never plot across that boundary; and expect little movement per
run, since volume-insensitivity is working as designed (Hormuz sits at 4.300 of
a 5.0 ceiling). Each `--full` costs ~$0.025 and ~40 min and buys the time
series, not a better score.

**Snapshot:** `RiskScore` rows **id 43-45**, `computed_at` **2026-09-27 07:01
UTC** (12:31 IST), written by **`PipelineRun 3`** (`run_pipeline --full`,
`status=succeeded`), top-3-story formula (Phase 4.5), corpus **1,629
corridor-attributed events** of 1,642 total (Hormuz 1,465 → **227 stories**,
Red Sea 164 → **88 stories**, Cape 0), drawn from 1,835 `RawArticle` rows
(gdelt_gkg 1,554 / gdelt 195 / oilprice 46 / gcaptain 40).
**Story counts are as clustered by Run 3.** Since 2026-09-28 `corridor_events`
returns events in date order (clustering is order-dependent and storage order
was not stable). Re-deriving today gives Hormuz 223 / Red Sea 89. The top-3
statistic, and therefore every score in this section, was measured
**bit-identical** under both orders. Quote the counts as Run 3's.

Supersedes ids 40-42 (2026-09-26 13:53 UTC, 795 attributed events, Hormuz 0.875
/ Red Sea 0.722, flow 2.432, 42.5% impaired). That run is history now, not a
result. **The corpus doubled between the two and no conclusion changed** — see
the volume-insensitivity note below, which is the single best piece of
validation evidence the project has.

**Regenerating the figures — read from the persisted run, not from the CLI.**
`run_criticality` and `run_response` read `Corridor.live_risk_score`, the
*mutable current* value, so they reproduce this table only while no scoring
stage has run since. `PipelineRun 3` does not have that problem — everything in
the table below is in its JSON columns:

| figure | where it lives in `PipelineRun 3` |
|---|---|
| risk scores | `risk_scores` |
| ranks, shifts, losses, centralities | `criticality[]` — keys `static_rank`, `risk_rank`, `rank_shift`, `capacity_loss_mbd`, `static_capacity_loss_mbd`, `centrality`, `static_centrality`, `residual_load_bearing_mbd` |
| risk-weighted baseline flow, both thresholds, which clause fired | `threshold` — keys `baseline_flow_mbd`, `triggered`, `reason`, `loss_fraction_threshold`, `risk_alone_threshold` |
| gap, reroute ranking, replacement timeline, SPR schedule | `response` — keys `gap`, `reroute`, `timeline`, `spr` |

```powershell
python manage.py shell -c "import json; from core.models import PipelineRun; print(json.dumps(PipelineRun.objects.get(pk=3).criticality, indent=2))"
```
Or `GET /api/pipeline/runs/3/`.

**One `PipelineRun` holds one SPR horizon, so the three horizons are three runs.
All are pinned:**

| horizon | run | released / required | covered | reserve dry |
|---|---|---|---|---|
| 14 days | **`PipelineRun 3`** (`--full`, wrote the scores) | 14.00 / 24.37 mb | **57.4%** | — |
| 30 days | **`PipelineRun 4`** (analysis only) | 26.17 / 36.54 mb | **71.6%** | — |
| 60 days | **`PipelineRun 5`** (analysis only) | 29.50 / 58.99 mb | **50.0%** | **day 34** |

Runs 4 and 5 were produced with `run_pipeline --duration-days 30` / `60` on
2026-09-27 13:11-13:12 UTC. Both carry `options.score = False` and all three
carry an identical `risk_scores` (Hormuz 0.8879) and identical `criticality`,
which is the executable proof that analysis-only runs do not move the snapshot —
**that is the property to rely on, not a promise in this document.** Verify it
any time with:

```powershell
python manage.py shell -c "from core.models import PipelineRun as P; [print(p.pk, p.options['duration_days'], p.options.get('score'), p.risk_scores) for p in P.objects.filter(pk__in=(3,4,5))]"
```

**Generalize the pattern:** any further cut of the response layer — a different
`--crisis`, `--corridor`, `--include-sanctioned`, or degradation — should be
pinned as its own analysis-only run and cited by id, rather than regenerated
from the CLI at write-up time. It is free, it never touches `live_risk_score`,
and it survives every later `--full`.

Scores also decay daily with no new data (Hormuz went 0.895 → 0.875 in one day),
which is a second reason the CLI drifts from this table even without an ingest.

| corridor | top-3 raw (of 5) | risk score | static rank | risk rank | shift | static loss | risk-weighted loss | trigger |
|---|---|---|---|---|---|---|---|---|
| Cape | 0.000 | 0.050 (baseline) | 2 | **1** | **+1** | 1.609 | **2.048** | loss clause, 85.4% of flow — TRIGGERED |
| Hormuz | 4.300 | **0.888** | 1 | **2** | **−1** | 1.784 | 0.247 | risk clause only (loss 10.3%) |
| Red Sea | 3.479 | **0.741** | 3 | 3 | 0 | 0.202 | 0.069 | none (2.9%; **0.009** below the risk bar) |

- **Max-flow:** static 4.228 → risk-weighted **2.400 mb/d, 43.2% of
  deliverability impaired.** (Supersedes 42.5% on the previous snapshot and the
  28.1% in the Phase 4.5 entry, both smaller corpora.)
- **VOLUME-INSENSITIVITY, MEASURED ACROSS THIS SNAPSHOT AND THE LAST — cite
  this.** The corpus went 795 → 1,629 corridor-attributed events in one
  `--full` run (Hormuz alone +828 events), and Hormuz's top-3 statistic moved
  **4.222 → 4.300, a factor of 1.018.** Decay was pulling the other way over
  the same day (Phase 4.5 measured −0.020/day with no new data), so the new
  articles contributed roughly +0.033 net. The superseded sum over the same
  corpus reads **398.5, which normalizes to exactly 1.0000** — see the Phase 4.5
  entry. This is a stronger natural experiment than the one recorded there,
  because both measurements came through the *same* ingestion path, isolating
  volume from method.
- **Headline finding:** Hormuz remains India's most *structurally* important
  corridor (static rank 1, **static** centrality 0.0756 vs Cape's 0.0532), but
  it is already ~88.8% impaired, so losing it entirely removes only 0.247 mb/d
  more. Cape, near-intact at baseline risk, carries the load, and losing it
  removes 2.048 mb/d. **Cite capacity loss, never centrality:** the
  risk-weighted centrality ordering has now *crossed over* on this snapshot
  (Cape 0.0655 > Hormuz 0.0651, where the previous snapshot had Hormuz 0.0672 >
  Cape 0.0634) — exactly the knife-edge Phase 4.5 documented, flipping inside
  the predicted 0.85–0.895 band. Do not claim it in either direction.
- **Response layer (Phase 5):**
  - **Cape gap 2.048 mb/d** (28.67 mb over a 14-day horizon). Its six eligible
    alternatives cover **63%**, leaving **0.748 mb/d uncovered** even after the
    last cargo lands on day 16. The SPR covers **57.4%** of the need over 14
    days (run 3) and **71.6%** over 30 (run 4). Over 60 days it covers only
    **50.0%** and **runs dry on day 34** (run 5), after which the residual is
    entirely unmet.
  - **Hormuz gap 0.247 mb/d.** UAE via Fujairah alone closes it by day 8, and
    the SPR bridges it completely (**1.98 mb, 100%**).
  - **Conclusion:** Cape is not only the corridor India can least afford to
    lose, it is also the one it can least replace.
- **Caveats to state alongside these figures:**
  1. **Direction-blind severity.** Hormuz's whole top 3 is the same Iran-US
     negotiation over reopening the strait (*"Trump rejects Iran's proposal to
     reopen the Strait"*, *"Iran Gives US Seven-Day Deadline To Reopen…"*), every
     one rated severity 5 / confidence 0.9. The extractor scores how serious the
     *topic* is, not which way it is moving — on the previous snapshot the
     top-weighted story was Iran *offering* to reopen, rated identically. A
     limitation, not a change of conclusion.
  2. **Semantic duplicates reach the top 3, so the statistic double-counts one
     story.** Hormuz's two strongest clusters are *"US-Iran war news updates:
     Donald Trump rejects Iran's proposal to reopen the Strait"* (9 members,
     w=4.311) and *"Iran war live: Trump rejects Tehran's proposal to reopen
     Hormuz within days"* (17 members, w=4.307) — the same real event under two
     outlets' headline conventions, below the 0.60 lexical similarity bar. Taking
     only one moves Hormuz **0.888 → ~0.876**. This is the known limitation
     pinned by `test_known_limitation_semantic_duplicates_are_not_caught`, now
     visible inside the production statistic rather than only in a test. It
     biases *upward*, and it is the strongest argument for semantic clustering
     in future work.
  3. **Risk is treated as a physical closure fraction.**
     `effective_capacity = volume × (1 − risk)` reads 0.888 as 88.8% of
     capacity unavailable. This is consistent with the corpus's measured
     indicator ("tanker transits crash to single digits") but remains an
     assumption.
  4. **Cape's rank shift is partly structural.** Cape has 0 events by
     verified-correct classification (see the Phase 4 caveat).
  5. **Red Sea rests on a thin story base and sits 0.009 under the risk bar.**
     88 stories against Hormuz's 227, and it gained only **6** in the run that
     added 69 Hormuz stories — a consequence of the Phase 2.6 maritime gate
     (`CORRIDOR_WEAK_KEYWORDS` requires a maritime signal, so Houthi land
     escalation is excluded) plus `gdelt_gkg.py`'s first-match corridor
     assignment always resolving overlaps to Hormuz. One story in or out moves
     whether a third corridor triggers, so state "Red Sea does not cross" with
     that margin attached, never as a clean negative.
  6. **Coverage figures are indicative.** They rest on `max_incremental_mbd`,
     which is estimated, not reconciled.

---

