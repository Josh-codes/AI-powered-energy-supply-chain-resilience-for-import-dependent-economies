# Frontend guide: building the dashboard on this backend

This is the brief for the React dashboard. It covers what the backend does, which
screens to build, which endpoint feeds each one, and which ways of displaying the
numbers would get them wrong.

- **The API reference is [`../API_DOCS.md`](../API_DOCS.md).** It covers parameters,
  status codes and units. This guide doesn't repeat it.
- **The response contract is [`../backend/data/api_samples/`](../backend/data/api_samples/)**:
  one real captured response per route. Build against these files.
- Backend owner: Joshua. If you need an endpoint, a field or a route geometry that
  isn't there, ask him. Don't work around it in the UI.

> **Scaffolding into this folder?** `npm create vite@latest .` will say the directory
> isn't empty. Pick **"Ignore files and continue"**, not "Remove existing files", or
> this guide gets deleted.

---

## 1. What the backend does (60-second version)

It models **India's crude-oil imports as a network**: 12 suppliers → 3 shipping
corridors → 10 Indian ports → 23 refineries. It reads geopolitical news, turns it
into a **risk score (0–1) per corridor**, and cuts each corridor's capacity by that
risk. It then asks two questions:

1. **Which corridor can India least afford to lose?** (criticality)
2. **If that corridor goes, what do we do?** Size the supply gap, rank the alternative
   suppliers, and schedule a drawdown of the Strategic Petroleum Reserve (SPR).

The three corridors are `Hormuz`, `Red Sea` and `Cape`. There is no `Suez`: it is
part of the Red Sea route, and the API returns 400 if you ask for it.

### The story the dashboard has to tell

This is the thesis's headline finding, and the dashboard's main job is to make it visible:

| corridor | risk now | rank with no risk ("static") | rank with today's risk | flow lost if cut (mb/d) |
|---|---|---|---|---|
| Hormuz | **0.888** (red) | 1 | **2** | 1.784 static → **0.247** |
| Cape | **0.050** (green) | 2 | **1** | 1.609 static → **2.048** |
| Red Sea | 0.741 | 3 | 3 | 0.202 → 0.069 |

Hormuz is the most important corridor on paper, but it is already about 89% impaired,
so losing it now removes little more. **Cape is the quiet, green corridor everything
now leans on, so it has become the one India can least afford to lose.** On the map,
the red corridor and the most critical corridor are different ones. That is the finding,
not a bug. Every screen should make the difference between *static* and
*risk-weighted* easy to see.

---

## 2. Getting started

### Work against the samples first (recommended)

You don't need Postgres, PostGIS or Python. Each sample file looks like this:

```json
{ "name": "...", "request": {"method": "GET", "url": "/api/..."}, "status": 200, "response": { ... } }
```

The `response` field is exactly what the live API returns. Suggested setup:

- Put all HTTP calls behind **one API module** (e.g. `src/api.js`) with a switch such
  as `VITE_USE_SAMPLES=true`. In sample mode, return `sample.response` from the
  matching file. In live mode, `fetch` the real URL. Components never know which mode
  is on.
- Copy the sample files into `src/mocks/` (or `public/`) rather than importing across
  into `../backend/`, so the frontend builds on its own.
- `index.json` maps each URL to its sample file.

### Then against the live backend

- Base URL: `http://localhost:8000/api/`. **Every route needs its trailing slash.**
- **CORS only allows `http://localhost:3000`.** Vite's default port is 5173, so either
  set `server: { port: 3000 }` in `vite.config.js`, or use Vite's `server.proxy` to
  forward `/api` to `:8000`. The proxy avoids CORS completely. You can also ask Joshua
  to add your origin to the backend `.env`.
- Setting up the backend yourself is in `API_DOCS.md` → *Running the backend*. Read
  the `seed_db` warning there: a backend loaded the wrong way shows every corridor
  green with no rank shift, and nothing warns you.
- There's no auth. Nothing you do from the UI can change the risk scores.

---

## 3. Screens to build

The original design names **React + Leaflet (map) + Recharts (charts)**. The backend
doesn't depend on that choice.

### 3.1 Overview / map (landing page)

| data | endpoint | sample |
|---|---|---|
| corridor lines + risk | `GET /api/corridors/geojson/` | `corridors_geojson.json` |
| risk per corridor | `GET /api/risk-scores/` | `risk_scores.json` |
| "response triggered" banner | `GET /api/pipeline/runs/3/` (or `/latest/`) → `threshold` | `pipeline_run_snapshot.json` |

- Colour each LineString by `properties.risk_score`. Use a sequential scale, and
  make **0.75** a visible boundary: that is where risk alone triggers a response.
- Coordinates are GeoJSON `[lon, lat]`. Leaflet's `L.geoJSON` handles the swap. If you
  draw the lines yourself with `L.polyline`, flip them to `[lat, lon]`.
- **Cape has `capacity_mbd: null` and `capacity_unlimited: true`** (the open ocean has
  no chokepoint). Show "unlimited", not `null` or `0`.
- Banner: `threshold.triggered_corridor` and `threshold.reason`
  (`"capacity loss 85.4% of baseline flow > 15%"`). `threshold.evaluated` has one row
  per corridor with `loss_crossed` / `risk_crossed`, which is enough for a small
  "why" tooltip.
- Not available yet: ports / refineries on the map, and reroute lines. See §5.

### 3.2 Criticality: static vs risk-weighted (the core screen)

| data | endpoint | sample |
|---|---|---|
| ranking | `GET /api/criticality/` | `criticality.json` |
| per-port dependency | `GET /api/criticality/ports/` | `criticality_ports.json` |

- Chart idea: grouped bars per corridor, `static_capacity_loss_mbd` next to
  `capacity_loss_mbd`, plus a rank column showing `static_rank → risk_rank` with a
  ▲/▼ from `rank_shift` (+1 = became more critical).
- **Rank by capacity loss (the default).** `?rank_by=centrality` and the `centrality`
  fields are diagnostics: the Hormuz/Cape order flips with tiny score changes. If you
  show them at all, put them in a secondary "details" view.
- Ports table: `corridors` is `{corridor: mb/d}` and **leaves out corridors that
  don't reach that port** (Sikka has only Hormuz). Treat a missing key as "no route",
  not zero risk. `stranded_mbd` is crude that arrives but can't be refined there.

### 3.3 Cascade: "what if this corridor degrades further?"

`GET /api/cascade/?corridor=Hormuz` (sample `cascade_hormuz.json`) returns 10 rows
at 10%, 20%, …, 100%.

- Line chart: x = `degradation_pct`, y = `capacity_loss_mbd` (or `flow_after_mbd`).
- `affected_refineries` is a list of **objects**
  (`refinery, baseline_mbd, current_mbd, pct_of_baseline, shortfall_mbd`), not names.
  `pct_of_baseline` is a **fraction** (0.85 = 85%).
- These losses are **on top of today's risk**, so they don't line up with the
  static numbers on the criticality screen. Label the axis "additional flow lost
  (mb/d)".

### 3.4 Scenario simulator (the only live computation)

`POST /api/simulate/`. Samples: `simulate_cape_full.json`, `simulate_scenario_hormuz_30.json`.

```js
fetch('/api/simulate/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ corridor: 'Cape', degradation: 0.6, duration_days: 30 })
})
```

- Controls: corridor picker (or a named scenario from `GET /api/scenarios/`), a
  degradation slider, a duration input (1–365, default 14), a `normal`/`severe` crisis
  toggle, and an "include sanctioned suppliers" checkbox.
- **Send exactly one of `corridor` or `scenario`**, never both.
- **Here `degradation` is a fraction in (0, 1].** On `GET /api/cascade/` the same word
  means a **percent** (1–100). Keep them separate in your code.
- Debounce the slider (about 300 ms). One call is cheap, but don't fire on every pixel.
- It returns `{input, cascade, gap, reroute, timeline, spr}`, everything the response
  screen (3.5) needs, so reuse those components here.
- `opec_cut` is a supply shock, not a corridor cut: `corridor` and `degradation` are
  `null` and there is no `degradation_pct`. Disable the slider for it.

### 3.5 Response plan: gap → alternatives → SPR

Get this from the snapshot run (`GET /api/pipeline/runs/3/` → `response`) or from a
simulate result. Both have the same `gap / reroute / timeline / spr` shape.

**Gap card** (`gap`): `gap_mbd` (the headline, 2.048 for Cape), `affected_count`
refineries, `total_shortfall_mb` over the horizon, and `load_bearing_ports`.

**Alternative suppliers table** (`reroute`, also `GET /api/reroute/`): already ranked
best first. Show `source`, `transit_days`, `max_incremental_mbd`, `price_premium`
($/bbl), `score`, and a running coverage bar from `cumulative_coverage_pct` (present
when a gap is known). Flag `sanctioned: true` rows.
**Scores are normalised within this one response**, so a 1.0 here is not comparable
with a 1.0 for another corridor. Don't chart scores from different corridors
together.

**Replacement timeline** (`timeline`): alternatives replace `covered_mbd` from day
`transit_days` onward, and `residual_gap_mbd` stays open after that (Cape: 1.3 of
2.048 covered, **0.748 mb/d open after day 16**). `covers_gap: false` should be
clearly visible.

**SPR chart** (`spr`): one row per day, for a stacked or overlaid bar chart:
- `daily_gap_mbd[t]`: the shortfall that day (the outline)
- `daily_schedule[t]`: reserve released (capped at 1.0 mb/d)
- `unmet_mbd[t]`: what's still short

Then the summary numbers: `gap_covered_pct`, `total_released_mb` out of
`available_mb` (29.5 mb usable, since 20% is a safety floor), `insufficient`, and
`reserve_exhausted_day` (`null` = not exhausted within the horizon).

### 3.6 News feed

`GET /api/events/live/?limit=50` (sample `events_live.json`).

- Each row is one LLM-extracted event: `corridor, actor, event_type, severity (1–5),
  confidence (0–1), timestamp, title, article_url`.
- **Syndication means many rows share a headline** (the sample's first rows are the
  same story from several radio-station sites). Group by `title` and show "N sources".
- `corridor` can be `null` (a general oil-market story). Give it an "Oil market" tag.

### 3.7 Risk trend

`GET /api/risk-scores/history/?corridor=Hormuz` (sample `risk_scores_history_hormuz.json`).

- **Filter to `formula === "top3pad"` before plotting.** Older rows
  (`sum_saturating`) use a different formula on a different scale. Mixing them in one
  line makes a meaningless jump on 2026-09-26.
- The top3pad history only starts on 2026-09-26, so it's short. That's expected.

### 3.8 Validation: backtests against oil prices

| data | endpoint | sample |
|---|---|---|
| list + verdicts | `GET /api/backtest/` | `backtest_list.json` |
| one event, no series | `?event=2026_hormuz_closure&series=false` | `backtest_2026_verdict.json` |
| one event + daily series | `?event=2026_hormuz_closure` | `backtest_2026_full.json`, `backtest_2025_full.json` |

Chart: a dual-axis line over `series`, with `scores.Hormuz` (0–1, left) and `brent_usd`
(right; **`null` on weekends/holidays**, so use `connectNulls`). Add a horizontal line
at `method.signal_threshold` (0.6) and a vertical marker at `price_spiked_at`.

What to show for each event:
- **2026_hormuz_closure**: show `validation: "PASSED"`. **Don't headline
  `lead_time_days` (15)** or say "predicted 15 days early". The thesis doesn't claim a
  lead time for this event: the score rose alongside a gradual price climb, and the
  closure itself was detected, not predicted. The JSON has a field called
  `false_alarms`, but **don't label it "false alarm" in the UI**. Call those ranges
  "elevated periods" (the `excursions` array covers the same ground).
- **2025_iran_standoff**: `validation: "INCOMPLETE"`, permanently. The news source has
  no data for 2025-06-15 → 07-01 (the `gaps` array). For rows with
  **`day_sampled: false`**, grey out or break the line. The smooth decline across
  that gap is old news fading, **not** the risk falling, so it must not look like a
  real drop. Rows with `unsampled_days_in_lookback > 0` are lower bounds, so mark them
  too (dashed line, or a note in the tooltip).
- `series[].top_stories` (title, weight, severity) make a good tooltip: "why was the
  score this high today?"

---

## 4. Rules for displaying numbers

These are about the thesis being right, not about style. A wrong display here shows
up as a wrong claim in the write-up.

1. **Numbers arrive unrounded** (`1.7839999999999998`). Round **only for display**
   (2–3 decimals for mb/d, 3 for risk), never before comparing or computing.
2. **Don't round a risk score across the 0.75 line.** Red Sea is `0.7414`: show it as
   0.741 or 0.74, **never 0.75**, which would make it look as if it triggered.
3. **Units:** flows are **mb/d** (million barrels per day), reserves and totals are
   **mb** (million barrels), and `price_premium` is $/bbl. Label every axis.
4. **For thesis figures, read pipeline run 3** (`/api/pipeline/runs/3/`). `/latest/`
   is run 5 (a 60-day variant of the same scores). The standalone routes read the live
   state, which matches today but drifts if the backend re-scores. When a panel is
   meant to show "the thesis numbers", pin it to run 3 and label it.
5. **Don't mix figures from different sources in one panel** (e.g. the gap from run 3
   with SPR numbers from a live `/api/spr/` call). Each run's figures depend on each
   other.
6. **Headline capacity loss, not centrality**, everywhere.
7. `price_impact_usd` on scenarios is an external IEA estimate, not model output.
   Label it as such, or leave it out.

---

## 5. What the backend doesn't provide yet

Ask Joshua before building a workaround:

- **No ports / refineries GeoJSON.** The map can only draw the 3 corridors for now.
  Port *names* and numbers are available (`/api/criticality/ports/`).
- **No reroute lines**: `route_geometry` is empty for every alternative supplier. Use
  `route_description` (text) instead.
- **Nothing updates live.** Data changes only when Joshua runs the pipeline by hand.
  Polling every 60 s is harmless but pointless. Load once, and add a "refresh" button
  if you want.
- **No auth, no writes.** The only POST is `/api/simulate/`, which computes on a copy
  and writes nothing.

## 6. Error handling

| status | body | show |
|---|---|---|
| 400 | `{"field": ["msg"]}` (validation) or `{"error": "..."}` | the message next to the control |
| 404 | `{"error": "..."}` | "not available" (e.g. a backtest not run yet) |
| 503 | `{"detail": "..."}` | "backend database not loaded" |
| 500 | `{"error": "internal error - see server log"}` | generic error, and tell Joshua |

Samples: `error_*.json`. Handle both `error` and `detail` keys.

## 7. Checklist before demo

- [ ] Map: Hormuz red, Red Sea amber-to-red, Cape green, and Cape shows "unlimited" capacity.
- [ ] Criticality screen shows Cape moving **2 → 1** and Hormuz **1 → 2**.
- [ ] Red Sea risk never shows as 0.75.
- [ ] Simulator sends `degradation` as a fraction; cascade uses percent.
- [ ] Response panel: Cape gap 2.048 mb/d, 0.748 mb/d still open after day 16, SPR 57.4% covered (run 3, 14 days).
- [ ] Risk trend filtered to `top3pad`.
- [ ] Backtest 2025 greys out the 06-15 → 07-01 gap; no "15-day lead" or "false alarm" labels anywhere.
- [ ] Works fully in sample mode with the backend switched off.
