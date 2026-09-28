# Seed data and external sources

> Corridor / refinery / alternative-supplier tables, named scenarios, and the external data sources (with why Reuters and Lloyd's were dropped).
>
> Split out of CLAUDE.md on 2026-09-28, text moved verbatim. CLAUDE.md holds the current summary and wins where the two differ.

> Section map (cross-references in this text still use the old CLAUDE.md section names): **Thesis Snapshot** → `thesis-snapshot.md` · **Threshold Trigger Logic / Key Algorithms / Extraction Prompt** → `algorithms.md` · **REST API Endpoints / Models / Request cycles** → `models-and-api.md` · **Development Commands / Common Errors** → `operations.md` · **Named Corridors / Scenarios / Refinery / Alternatives / Data Sources** → `data-reference.md` · **Phase 1–3, 2.5, 2.6** → `history-phases-1-3.md` · **Phase 4, 4.5, 5, 6** → `history-phases-4-6.md` · **Phase 7 / Backtest** → `phase-7-backtest.md` · **Architecture / File map / Repo structure / Tech stack / Celery** → `architecture.md`.

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

