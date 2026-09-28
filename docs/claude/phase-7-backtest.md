# Phase 7: backtest (active phase)

> Backtest design, the pre-registered readings, both results and what may / may not be cited, plus the open Phase 7 items and frontend handoff.
>
> Split out of CLAUDE.md on 2026-09-28, text moved verbatim. CLAUDE.md holds the current summary and wins where the two differ.

> Section map (cross-references in this text still use the old CLAUDE.md section names): **Thesis Snapshot** → `thesis-snapshot.md` · **Threshold Trigger Logic / Key Algorithms / Extraction Prompt** → `algorithms.md` · **REST API Endpoints / Models / Request cycles** → `models-and-api.md` · **Development Commands / Common Errors** → `operations.md` · **Named Corridors / Scenarios / Refinery / Alternatives / Data Sources** → `data-reference.md` · **Phase 1–3, 2.5, 2.6** → `history-phases-1-3.md` · **Phase 4, 4.5, 5, 6** → `history-phases-4-6.md` · **Phase 7 / Backtest** → `phase-7-backtest.md` · **Architecture / File map / Repo structure / Tech stack / Celery** → `architecture.md`.

---

### Backtest Architecture

Historical validation against two events:

2025 US-Iran standoff (Brent **+7.28%** in one session, 2025-06-13 — "+8%" was approximate)
2026 Hormuz closure (Brent $70.69 on 2026-02-25 → $118.09 on 2026-03-18, 21 days;
  first ≥5% session 2026-03-02, +8.30%; peak $138.21 on 2026-04-07)

> **As built (Phase 7): `backtest/runner.py` + `validator.py` + `eia.py`,
> `manage.py run_backtest`.** All dates above are read off the committed EIA
> series (`data/brent_cache.json`), not memory, and pinned by
> `DocumentedDatesTests`. Differences from the steps below: (a) each charted
> day is scored **point-in-time at 16:30 UTC** (≈ Brent's London assessment)
> with `exclude_future=True` — without it the scorer admitted every later
> event at full weight and scored Hormuz **0.947 on 2026-02-11** from
> September news; (b) the price event is the first session ≥ **5%** in the
> window, found from prices alone so lead time is not circular; (c) the
> verdict is PASSED / FAILED / **INCONCLUSIVE** (already above 0.6 on the first
> charted day) / NO_PRICE_EVENT, downgraded to **INCOMPLETE** while any pull day
> is unsampled or any in-scope article unextracted; (d) early excursions above
> 0.6 that fall back before the price moves are reported as **false alarms**, not
> credited as lead. Backtest scoring writes NO `RiskScore` rows and never touches
> `live_risk_score`.

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

- [ ] Phase 7 — Backtest validation + integration testing + API documentation
  - [x] **Backtest machinery built (2026-09-27); data not yet pulled.** `backtest/eia.py`, `runner.py`, `validator.py`, `run_backtest`, `/api/backtest/`. `gdelt_gkg.fetch_between(start, end, workers)` adds explicit historical windows with bounded parallel downloads (measured 2.5 → 0.9 s/slice at 4 workers; `fetch_by_corridor` unchanged). **57 new tests, full suite 547.**
  - [x] **Concurrent extraction** (`extract_pending_events(workers=N)`, `extract_events --workers`, default 4 on the command, 1 everywhere else so `run_pipeline` and the ordered test mocks are unchanged). Serial throughput measured from Run 3: **830 articles in 39.7 min = 2.9 s/article**, almost all network wait, so a 5-10k-article backtest would have taken 4-8 h. Worker threads only make the HTTP call; every DB write stays on the main thread (Django connections are per-thread), and each answer is persisted as it completes, so Ctrl+C loses at most the calls in flight. `test_calls_actually_run_concurrently` proves overlap with a barrier that serial execution cannot pass. **7 new tests, full suite 554.**
  - [x] **Pull + extraction DONE (2026-09-28): 43/43 days, 4,128/4,128 slices, 16,338 articles, 0 pending. Events in window: Hormuz 14,481 / Red Sea 231 / Cape 13** — ~10x the pre-pull estimate; crisis coverage syndicates heavily.
  - [x] **SCORING WAS INFEASIBLE AT THAT VOLUME — fixed with zero change to results.** Story clustering was O(events x clusters) with a full `difflib` ratio on almost every pair: **112 s for the 2,652 Hormuz events of 2026-03-05**, growing roughly quadratically to 14,483 by 03-18, and the runner re-clustered from scratch for each of 36 days (hours). Profiling showed `quick_ratio` rejected only **1.6%** of pairs (any two English headlines share most letters), so nearly every pair paid the full ~157 us `ratio()`. Now `risk_scorer.StoryClusterer`, shared by production and the backtest (still ONE implementation):
    - **LCS bound** (bit-parallel, Hyyro 2004): difflib's matching blocks form a common subsequence, so `ratio <= 2*LCS/(la+lb)`. **0 violations on 90,109 real in-window pairs**, rejects 70% at 25 us.
    - **Day-bucket index** (only reps within 3 days scanned, merged back into creation order), **cached reps** (SequenceMatcher seq2 + LCS masks built once), **repeat-headline shortcut** (exact only in date order; switches itself off otherwise).
    - **`corridor_events` now returns date order** (`order_by("timestamp", "id")`). Clustering is greedy, so storage order used to leak into results: live story counts moved 224→223 (Hormuz) / 88→89 (Red Sea), **top-3 bit-identical**. Also makes a dump/restore score identically.
    - **Backtest scores each corridor in ONE incremental pass** (`runner.corridor_series`): clustering a date-ordered prefix = the state after that prefix, so each day is a snapshot. Days are grouped by where their 180-day lookback starts, so the edge passing an old event costs one extra pass rather than an approximation.
    - **Verified on real data, not just tests:** new clustering == the original loop (kept verbatim as `_reference_cluster_stories`) on live Hormuz/Red Sea and three backtest days, story for story; `corridor_series` == from-scratch `score_day` on 4 days x 3 corridors (scores, event/story counts, top-3 headlines). **Mar 5: 105 s → 5.4 s. Whole 36-day Hormuz series: ~55 s.** Live Hormuz clustering 11.6 s → 0.4 s as a side effect. 9 new tests (random-corpus equivalence in and out of date order, LCS vs DP, LCS bound never rejects a match, incremental == from-scratch with the lookback edge moving mid-window). **Full suite 563.**
  - [x] **FIRST RESULT (2026-09-28): PASSED, but the reported 15-day lead is FRAGILE — do not cite it as the lead time.** Report: `data/backtests/2026_hormuz_closure.json`. Hormuz opened the window at **0.710** (US warnings to shipping, Feb 10-11), decayed to **0.578 on Feb 14 with no new stories**, and a new warning story lifted it to 0.616 on Feb 15, which the validator counted as a fresh crossing (Feb 11-13 recorded as a false alarm). Had Feb 14 read >= 0.601 the verdict would have been INCONCLUSIVE. What IS robust:
    - Hormuz stayed **> 0.6 without a break from Feb 15 to the Mar 2 price event** (minimum 0.681), so the criterion holds under any reading.
    - **0.953 on Feb 17**, 13 days early, on *"Iran temporarily closes Strait of Hormuz"* during Geneva talks — **Brent did not move** ($69.77). Either the signal saw escalation the market discounted, or it cannot tell a temporary closure from a real one (both rated sev 5). Direction-blind severity (Snapshot caveat 1), now observed in a real crisis.
    - The **actual closure broke Saturday Feb 28** (0.730 → 0.946; "Iran Announces Closure", US strikes); Brent repriced **at the first session after, Mon Mar 2 (+8.30%)**. On the event itself signal and market moved together — detection, not a lead.
    - **Volume-insensitivity in the wild:** 0.953 at 69 stories (Feb 17) vs 0.942 at **3,715** stories (Mar 18).
    - **Ceiling:** 0.93-0.96 through March while Brent went $77 → $118 — the signal detects a crisis, it does not measure its depth.
    - Cape rose 0.05 → 0.54 in March on only 13 events (likely rerouting coverage — the Phase 4 Cape caveat applies).
  - [x] **DECIDED (2026-09-28): window widened, `chart_start` 2026-02-11 → 2026-01-01** (pull from 2025-12-25; 41 new days, ~11.5 GB; the 43 already-pulled days are kept). Reason: the Feb 11 window failed its own premise of opening on calm. Jan 15 was the first idea but was rejected on the price data: **Brent rose 18% from Jan 7 to Jan 30** ($61.08 → $72.25; sessions of +3.70% / +3.33% / +4.12% / +3.81%, then −6.27% on Feb 2), so the only flat stretch is mid-December to Jan 7 ($60-63). No January session reaches +5%, so the price event is still **2026-03-02** (pinned by `DocumentedDatesTests`). First report archived as `data/backtests/2026_hormuz_closure.window_feb11.json`.
  - [x] **HOW THE WIDENED RESULT WILL BE READ — written down BEFORE the pull, so it cannot be fitted to the answer:**
    1. **Jan 1 below 0.6, first sustained crossing in February or later** → the standard reading; cite the verdict and lead as reported.
    2. **A crossing in January that stays above 0.6 without a break into March** → do NOT cite the resulting ~2-month "lead" against Mar 2. It spans two episodes. Report the January rise as co-movement with January's gradual +18% price climb (which the 5%-session rule does not register as an event), and describe February–March as in the Feb 11 result.
    3. **Jan 1 already above 0.6** → INCONCLUSIVE; the tension predates any calm baseline available. **Stop there: no further widening.** Each extension is another researcher degree of freedom, and this is the last one.
    - In every case, report the first (Feb 11) result alongside, with the reason the window changed.
  - [x] **WIDENED RESULT (2026-09-28): validator says PASSED, 15-day lead from Feb 15 — but the outcome fell BETWEEN readings 1 and 2, and is read under reading 2. Cite NO lead time for this event.** Report: `data/backtests/2026_hormuz_closure.json`; coverage 84/84 days, 8,063/8,064 slices, 0 pending. Events in window: Hormuz 14,651 / Red Sea 421 / Cape 14.
    - **Jan 1 = 0.434** (below the bar; the Dec 26-28 tanker seizure is already in it, so not calm either). **First crossing Jan 28 (0.761)**, above 0.6 for 17 days, **one day below on Feb 14 (0.578, 0.022 under)**, then unbroken Feb 15 → Mar 18. Above 0.6 on **49 of the 50 days** Jan 28 → Mar 18.
    - **Why neither reading fits literally:** reading 1 needs the first *sustained* crossing in February (Jan 28's ran 17 days); reading 2 needs *no break* into March (one day breaks it). At 0.601 on Feb 14 the validator would have reported a **33-day** lead — exactly reading 2's case. Choosing reading 1 on a 0.022 dip would be fitting to the answer, so reading 2 is applied in substance. **This joining of a ≤2-day dip into one episode is a post-hoc judgement for THIS event** (it is pre-registered for the 2025 event below); say so in the write-up.
    - **What to cite instead:** (a) the verdict criterion is met under any reading; (b) the January rise **co-moves** with the gradual repricing rather than leading it — Hormuz 0.54 on Jan 12 ("Iran Crisis Escalates, Threatening Strait of Hormuz") while Brent went $61 → $69, crossing on Jan 28 ("Iran says it has 'complete control'…", live drills) the day **after** Brent passed $70; (c) the closure itself is **detection, not lead** — Sat Feb 28 0.730 → 0.946, Brent +8.30% at the next session.
    - **Do not call Jan 28 – Feb 13 a "false alarm" in prose.** That is the validator's mechanical label (fell back before the price moved). Its stories are tanker seizures, live-fire drills and US warnings (peak 0.779 on Feb 6, "Iran seizes oil tankers, threatens 'massacre'…") — the same escalation that ended in the closure.
    - **The Feb 15 re-crossing that produces the 15-day figure is a semantic duplicate**: "US warning on Iranian waters puts global shipping on edge" (Feb 15) is the Feb 10 "US Advises Ships to Steer Clear of Iranian Waters…" story again (Snapshot caveat 2), so the lead depends on a known clustering defect.
    - **Robustness, measured:** every Hormuz score in the 36 overlapping days is **bit-identical** to the Feb 11 report (only Cape differs, from January events decaying in). The 7-day warm-up was sufficient; the Feb 11 result was not an artefact of where the window began.
    - **Final for this event.** Reading 3's no-further-widening rule applies regardless.
  - [x] **2025_iran_standoff — window EXTENDED before any pull (2026-09-28): `end` 2025-06-27 → 2025-07-11** (pull 2025-05-23 → 07-11, 50 days). The price faded with the ceasefire (Jun 23 −5.58%, **Jun 24 −7.01%**, $80.37 peak Jun 19 → $68.40 Jun 25). At 0.1/day decay a ~0.95 score needs **~6.4 story-free days** to reach 0.6, so a Jun 27 end could not show a fall-back whatever the news did. Price event unchanged (**Jun 13, +7.28%**; Jul 2's +4.69% is under 5%). Pinned by `test_2025_window_leaves_room_for_the_signal_to_fall_back`. The DB held **1** event in this range beforehand (Jun 23, severity 1, from the live corpus).
  - [x] **HOW THE 2025 RESULT WILL BE READ — written down BEFORE the pull.** Context: Brent flat $64-66 through May; Jun 11 +4.21% (US embassy drawdown), **Jun 13 +7.28% (Israel strikes Iran)**, Jun 17 +4.86%, US strikes Jun 22, ceasefire Jun 24. The Jun 13 shock was an Israel-Iran strike, **not a Hormuz event**, so the Hormuz corridor may only register once Hormuz is named.
    - **Episode rule (fixed now):** excursions above 0.6 separated by ≤ 2 days below it (= the validator's `TOLERANCE_DAYS`) are ONE episode. Lead = Jun 13 minus the episode's first day, reported alongside the validator's own figure.
    1. **May 30 below 0.6, episode starts Jun 6 – Jun 15** → PASSED; cite the lead as the episode rule gives it (≤ 7 days lead, or up to 2 days late).
    2. **Episode starts on or before Jun 5 (> 7-day lead)** → cite the verdict, not the lead; report the crossing day's top 3 stories and the Brent path, since a week-plus lead on a strike-triggered move needs its own explanation.
    3. **No crossing by Jun 15** → FAILED. Report it as the signal lagging a shock that did not originate at the corridor. **Do not re-run on another corridor, threshold or formula.**
    4. **May 30 already above 0.6** → INCONCLUSIVE. **No widening.**
    - **Fall-back (secondary; does not change the verdict):** falls back = Hormuz below 0.6 on some day **by Jul 8** (14 days after the ceasefire) and **stays below through Jul 11**. If it does not, report it as persistence alongside the 2026 ceiling finding, naming the cause from the top stories (continued aftermath coverage vs. decay alone — half-life is 6.9 days).
    - A FAILED or INCONCLUSIVE here is an honest result and is reported as such.
  - [x] **2025 RESULT (2026-09-28): verdict INCOMPLETE, and it STAYS INCOMPLETE — GDELT publishes no GKG files for 2025-06-15 → 07-01.** Confirmed upstream, not our pull: 17 contiguous days read 0/96 slices, the edges are partial (Jun 14 72/96, Jul 2 87/96), gap days "pulled" in ~18 s (fast 404s) against ~80 s for a real day, and Jun 15-22 read 0 again on retry. The DOC API only reaches back ~3 months, so nothing can fill it. **The gap swallows the US strikes (Jun 22), Iran's parliament voting to close Hormuz, and the Jun 24 ceasefire.** Do not re-pull, change the rules to upgrade the verdict, or widen: report it as INCOMPLETE with a named source gap. Report: `data/backtests/2025_iran_standoff.json`.
    - **Why both findings still stand: missing data can only ADD stories, and a score is its strongest 3, so every score in or after the gap is a LOWER BOUND** (bar a greedy-clustering edge case). Now flagged per row: `day_sampled` / `unsampled_days_in_lookback`, report-level `gaps`, and `[no data: decay only]` / `[lower bound]` markers in the CLI.
    - **Primary — reading 1, lead 1 day (series alone says PASSED).** Every day May 23 → Jun 11 was sampled 96/96 and Hormuz sat at the 0.200 baseline with **zero** Hormuz stories; it crossed on **Jun 12 (0.615)** on two genuine *pre-strike* warnings — Lloyd's List *"Conflict between Israel and Iran would 'most certainly' close the Strait of Hormuz"* (15:15 UTC) and *"UK Raises Maritime Threat Level in Hormuz…"* (09:30 UTC) — hours before Israel's strikes that night; Jun 13 0.797, Jun 14 0.845. **State alongside it:** Brent had already moved **+4.21% on Jun 11** (US embassy drawdown) while Hormuz had no stories at all, so the signal led the ≥5% session, not the first move.
    - **Fall-back — did NOT fall back.** Hormuz is above 0.6 every day Jul 2 → Jul 11 (min 0.649), and as lower bounds that holds despite the gap. Cause, from the top stories: *"Iran Made Preparations To Mine Strait Of Hormuz After Israel Strikes, US Sources"* (Jul 2, 29-member cluster) — a **retrospective** report about the finished 12-day war, rated severity 5 like a live threat. This extends Snapshot caveat 1: the extractor is blind to tense as well as direction. It is that, not continued risk, that holds the score up.
    - **NEVER cite or plot the Jun 15 → Jul 1 decline (0.784 → 0.334) as a fall-back.** It is pure decay over missing data and is shaped exactly like the thing the test was looking for.
    - Side observation, not pre-registered: Red Sea rose to **0.918** on Jul 10 on a burst of 259-399 matched articles/day from Jul 6, with Brent flat — a Red Sea shipping episode the signal tracked with no price response.
  - [x] **TOP-K SENSITIVITY TOOLING (2026-09-28): `run_backtest --event KEY --top-k N`.** Same events, clustering, validator and gap flags; only the number of stories averaged changes (normalization stays `raw / 5.0`, so 1.0 still = k severity-5 stories today). Writes `data/backtests/<event>.top<N>.json` and **never** `<event>.json`; `/api/backtest/` only ever serves the cited file; the report carries `method.top_k_is_production`. `top_k` threads through `score_day` / `corridor_series` / `score_series` / `run_backtest`, defaulting to `TOP_K_STORIES` so every existing caller is unchanged. 6 tests (statistic == production `top_k_severity` at k = 1/3/5, incremental == from-scratch at k = 5, separate file, dilution direction, k < 1 rejected by runner and command). **Full suite 580.**
  - [x] **TOP-5 RESULT (2026-09-28): ROBUST to k ∈ {3, 5} under the pre-registered definition — both verdicts and both readings unchanged. Only lead times move, by one day each, in the predicted direction.** Reports: `data/backtests/<event>.top5.json`. (k = 1 not run; not needed for the conclusion, and adding k values after seeing results was ruled out below.)

    | | k = 3 (cited) | k = 5 |
    |---|---|---|
    | 2026 verdict | PASSED (reading 2: no lead cited) | PASSED (reading unchanged) |
    | 2026 validator lead | 15 d (from Feb 15) | 14 d (from Feb 16) |
    | 2026 first crossing | Jan 28 (0.761) | Jan 28 (0.754) |
    | 2026 closure day Feb 27 → 28 | 0.730 → 0.946 | 0.688 → 0.934 |
    | 2026 peak | 0.959 | 0.958 |
    | 2026 Jan 1 | 0.434 | 0.341 |
    | 2025 verdict | INCOMPLETE (series PASSED) | INCOMPLETE (series PASSED) |
    | 2025 crossing / lead | Jun 12, **1 d** | Jun 13, **0 d** (Jun 12 = **0.449**, exactly as predicted) |
    | 2025 fall-back by Jul 8 | no (min Jul 2-11 0.649) | no (min 0.615, thinner) |

    - **Cite as:** "Verdicts and readings are unchanged for k = 3 and k = 5. The 2025 lead is 1 day at k = 3 and 0 days at k = 5 (still inside the 2-day tolerance)." Never "1 day" alone.
    - **What k actually controls, now measured:** days resting on a THIN story base (Jan 1: 0.434 → 0.341; 2025 Jun 12: 0.615 → 0.449) drop, because 1-2 early warnings are divided by 5 instead of 3. **Crisis levels barely move** (peak 0.959 → 0.958; closure day 0.946 → 0.934), because a real crisis supplies ≥ 5 strong stories. So k trades early sensitivity against caution; it does not change what the signal detects. That is the substantive answer to "why 3?".
    - **2026 dip structure differs, and is NOT used to change the reading.** At k = 5 the gap before the final crossing is 3 days (Feb 13-15, min 0.555), not 1 (Feb 14, 0.578), so under the ≤ 2-day episode rule the January excursion would count as separate and a 14-day lead would look citable. Reading 2 stays: the reading is fixed at production k, and choosing a reading by k is exactly the degree of freedom the rules below forbid.
    - **k = 5's Feb 16 crossing is a genuinely NEW event** (IRGC "Smart Control of Hormuz Strait" drills, Feb 16) rather than k = 3's Feb 15 rehash of the Feb 10 US warning (now 5th of 5). But its top 4 "stories" are **one event under four headlines** — the semantic-duplicate caveat again, so it is not a cleaner lead either.
  - [x] **HOW THE TOP-5 RESULT WILL BE READ — written down BEFORE running it.** Runs: `--top-k 5` on both events (k = 1 optional, as the opposite extreme).
    - **Robust** = for each event the validator's `validation_if_complete` / `validation` is unchanged AND the pre-registered reading is unchanged (2026: reading 2, no lead cited; 2025: reading 1). Then write "results are robust to k ∈ {3, 5}".
    - **Lead times WILL be reported as they move, and are expected to shrink**: top-5 divides a thin early story base by 5, not 3. Back-of-envelope before running: 2025 Jun 12 has 2 stories (4.477 + 3.302) → 0.615 at k = 3 but **0.449** at k = 5, so the 2025 crossing most likely moves to Jun 13, a **0-day** lead, still inside the 2-day tolerance. If so, cite the 2025 lead as "1 day at k = 3, 0 days at k = 5", never "1 day" alone.
    - **A verdict change (PASSED → FAILED or similar) is reported as a finding** that the signal's timing depends on k. Do NOT switch production k to whichever value passes, and do not add more k values after seeing the result.
    - k stays 3 in production regardless: it was chosen in Phase 4.5 on volume-insensitivity grounds, not on backtest performance, and choosing it now from the backtest would fit the constant to the validation data.
  - [ ] Still open: the gpt-4o-mini hindsight control on February articles; optionally a signal-vs-Brent co-movement analysis over the whole window, which would use January's gradual repricing that the first-5%-session rule ignores.
  - [x] **LOOKAHEAD DEFECT FOUND AND FIXED before any backtest ran.** `corridor_events` had no `timestamp <= now` bound, and `event_weight` clamps negative age to 0, so scoring a past date admitted every later event at FULL weight. Measured on the real DB: **Hormuz on 2026-02-11 scored 0.947** from September news; with the fix, 0.200 (baseline). The filter is opt-in (`exclude_future=True`) because live scoring legitimately sees skew — **1 of 1,642 real events was dated 2 h after its own extraction** — and turning it on globally would silently change live scores. `test_default_still_admits_future_events` pins live behaviour unchanged. `compare_scoring --as-of` still has the defect; see its note in Development Commands.
  - [x] **Event dates read off EIA, not memory.** Hormuz: onset 2026-02-25 ($70.69), first ≥5% session **2026-03-02 (+8.30%)**, first close above $114 on 2026-03-18. 2025: **2025-06-13, +7.28%** (not +8%). The 5% session threshold is 2.6σ of Brent's 2025 daily returns (σ = 1.92%); only 2 sessions in all of 2025 cleared it.
  - [x] **Window (ORIGINAL, superseded 2026-09-28 — see below): pull 2026-02-04 → 03-18 (43 days), chart 02-11 → 03-18.** The 7-day warm-up is so the first charted scores have decayed history behind them; without it the "calm baseline" would read low for want of data. Every window must end on or before 2026-03-31, or its events enter the 180-day lookback of a live score computed on 2026-09-27 (`test_no_window_reaches_the_live_lookback`).
  - [x] **Run it (both events DONE, 2026-09-28):** `run_backtest --event 2026_hormuz_closure --pull` (~12 GB, ~1 h) → `extract_events` (PAID, several hours) → `run_backtest --event 2026_hormuz_closure`. Commit `data/brent_cache.json` and `data/backtests/*.json`. Pre-flight on one real slice found a Hormuz story on 2026-02-11 ("US Flags Iranian Boarding and Seizure Threat…"), so the calm period is not news-silent — an INCONCLUSIVE verdict is a real possibility and would be an honest result.
  - [ ] **The risk-score time series is a Phase 7 deliverable in its own right**, not just backtest scaffolding. Every `--full` appends to `RiskScore` and `PipelineRun`, so the sequence of runs is a live record of the system tracking a real crisis — which is the claim the thesis makes and a single snapshot cannot demonstrate. Plot it from `GET /api/risk-scores/history/?corridor=&days=`, **from 2026-09-26 onward only** (`TOP_K_FORMULA_SINCE`; rows before it hold the superseded sum and the endpoint labels each row's `formula`). The Red Sea margin narrowing toward the 0.75 trigger bar across runs (0.012 → 0.028 → 0.009 below) is the most citable thing in the series so far.
  - [ ] **Also open, from the 2026-09-27 snapshot:** `TOP_K_STORIES = 3 vs 5` needs the calm-plus-crisis sensitivity check (`top5pad` separated marginally better — gap 0.159 vs 0.147 — and sits further from the ceiling, 4.206 vs 4.298); semantic story clustering, now that lexical duplicates are demonstrably reaching the production top 3 (Thesis Snapshot caveat 2); and the Phase 2.6 maritime-gate question, which is now the binding constraint on Red Sea's 88-story base rather than a footnote.
  - [x] **FRONTEND HANDOFF BUILT (2026-09-28)** — 574 tests passing; samples captured; fixture: `python -X utf8 manage.py dumpdata core --exclude core.RawArticle --indent 2 -o data/fixtures/snapshot.json.gz` (create `data/fixtures/` first; **`-X utf8` is required on Windows** — without it the write goes through cp1252 and dies on headline characters like `ʼ` U+02BC):
    - `manage.py capture_api_samples [--run-id 3] [--out]` writes 24 samples (20 routes/variants + 4 error-contract cases) to `backend/data/api_samples/`, each `{name, description, request, status, response}`, plus `index.json` recording the live risk scores at capture. Test client with `HTTP_HOST=localhost` (the default `testserver` host is not in `ALLOWED_HOSTS` outside `manage.py test`). A status other than expected (e.g. no PipelineRun on a fresh DB) is recorded and warned, never raised. 5 tests pin files, real payloads, error codes, and **no DB writes**.
    - `API_DOCS.md` (repo root), written for the frontend teammate: conventions, every param with its range/default from `core/serializers.py`, the error contract, the fraction-vs-percent `degradation` trap, the `formula` label, the backtest gap flags, and setup. Checked against the captured samples (2026-09-28, live scores 0.888 / 0.741 / 0.050 = the snapshot); three corrections made: `opec_cut` has no `degradation_pct` key at all (not `null`), cascade `affected_refineries` are objects, and numbers are unrounded solver floats.
    - **Fixture load order corrected: `migrate` → `loaddata`, NOT `seed_db` → `loaddata`.** `seed_db` uses `update_or_create` by name, so on a fresh DB its pks need not match the fixture's; `loaddata` then writes e.g. Cape at Hormuz's pk and hits the unique `name` constraint. The fixture holds everything `seed_db` loads.
    - `core/admin.py`: `Corridor`, `ExtractedEvent`, `RiskScore`, `AlternativeSupplier` registered alongside `PipelineRun`, **all read-only** (no add/delete, every field read-only) — editing `live_risk_score` in the admin would silently move every API response. 1 test.
    - `.env.example` no longer hardcodes this machine's DLL paths (`<repo>` placeholder).
  - [ ] ~~**FRONTEND HANDOFF — deferred here by decision (2026-09-27), bundled with `API_DOCS.md` because they are the same work from two angles.**~~ Original plan, kept for the reasoning: `frontend/` is empty (0 files); the teammate builds it and **Rule 2 stands — never write inside it.** Deliverables:
    - [ ] **Capture each endpoint's real response to JSON** (suggested home `backend/data/api_samples/`, since Rule 1 keeps work inside `backend/`). Write `API_DOCS.md` *from* the samples: a real payload per route is an unambiguous contract that cannot drift from the code, and the prose then only has to cover what samples can't show — status codes, `degradation` (0-1, `/api/simulate/`) vs `degradation_pct` (0-100, `/api/cascade/`), and the `formula` label on history rows. Use `django.test.Client` rather than curl so no server has to be running. **Read-only: capturing writes nothing and cannot move the snapshot.**
    - [ ] **Commit a fixture so the DB is reproducible off this machine** — right now the cited snapshot exists only in one local Postgres instance, which is a submission risk independent of the frontend: `python manage.py dumpdata core --exclude core.RawArticle --natural-foreign --indent 2 -o data/fixtures/snapshot.json`. Excluding `RawArticle` keeps it small and loses nothing the API serves, since `ExtractedEvent` already carries its own `title` and `timestamp`. Setup then becomes `seed_db` → `loaddata`.
    - [ ] **⚠ THE TRAP TO WARN HIM ABOUT — `seed_db` alone produces a silently wrong world.** It loads the 58 static node rows and sets `baseline_risk`, but **never sets `live_risk_score`**, which stays at the model default `0.0`. `/api/risk-scores/` reads that field directly, so a fresh clone returns `{"Hormuz": 0.0, "Red Sea": 0.0, "Cape": 0.0}` — every corridor green — and `/api/criticality/` returns `rank_shift: 0` everywhere, i.e. a dashboard built against a world where the thesis finding does not exist. **No warning fires:** `_warn_if_risk_never_reached_the_edges` only triggers when a corridor has *nonzero* risk that failed to reach its edges, and a genuinely riskless graph is a legal thing to analyse. The fixture above is what fixes it, because `dumpdata` captures the column.
    - [ ] Tell him: **don't run `score_risk` or `--full`** (both write new scores; bare `run_pipeline` is free and snapshot-safe); `"Red Sea"` needs URL encoding (`Red%20Sea`); `/api/backtest/` returns **404** per event until `run_backtest` has written its report; `AlternativeSupplier.route_geometry` is NULL for every row, so the map cannot draw reroute lines yet; and there are no ports/refineries GeoJSON endpoints — add them if the dashboard wants them.
    - [ ] Optional, cheap: only `PipelineRun` is registered in `core/admin.py`. Four more `admin.site.register` lines would give him a read-only browser over `Corridor` / `ExtractedEvent` / `RiskScore` / `AlternativeSupplier`.
    - [ ] **Standing blocker if he starts before this lands:** the models are `django.contrib.gis`, so there is no SQLite fallback — he needs Postgres + PostGIS + the GDAL/GEOS wheels before `runserver` will boot, and `.env.example` still hardcodes this machine's DLL paths (`C:/Joshua/energy-resilience/venv/...`). The captured samples exist precisely so he needs none of that.

---

