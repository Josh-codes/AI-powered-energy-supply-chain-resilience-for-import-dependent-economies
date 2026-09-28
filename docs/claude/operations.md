# Operations: commands, env vars, errors, reminders

> Full command reference (stage-by-stage and orchestrated), .env keys, common errors and fixes, and data-retention reminders.
>
> Split out of CLAUDE.md on 2026-09-28, text moved verbatim. CLAUDE.md holds the current summary and wins where the two differ.

> Section map (cross-references in this text still use the old CLAUDE.md section names): **Thesis Snapshot** → `thesis-snapshot.md` · **Threshold Trigger Logic / Key Algorithms / Extraction Prompt** → `algorithms.md` · **REST API Endpoints / Models / Request cycles** → `models-and-api.md` · **Development Commands / Common Errors** → `operations.md` · **Named Corridors / Scenarios / Refinery / Alternatives / Data Sources** → `data-reference.md` · **Phase 1–3, 2.5, 2.6** → `history-phases-1-3.md` · **Phase 4, 4.5, 5, 6** → `history-phases-4-6.md` · **Phase 7 / Backtest** → `phase-7-backtest.md` · **Architecture / File map / Repo structure / Tech stack / Celery** → `architecture.md`.

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

## Development Commands

```bash
cd backend          # all commands run from here; venv is ../venv

python manage.py runserver
python manage.py seed_db                  # load data/*.json into the DB
python manage.py build_graph              # build graph + print cut summary
python manage.py test                     # full suite (574 tests)
python manage.py capture_api_samples      # refresh data/api_samples/ (read-only)

# ---- the orchestrated pipeline (Phase 6) ----
python manage.py run_pipeline             # analysis only on stored scores: free,
                                          # snapshot-safe, writes one PipelineRun
python manage.py run_pipeline --full      # WHOLE cycle: rss + gdelt-fallback ingest,
                                          # extract (PAID), score (writes NEW scores).
#    --full is safe to repeat: every run is persisted, so the cited PipelineRun is
#    never damaged. What it does is move `Corridor.live_risk_score`, which is what
#    the CLI and the API read live -- so after a --full, regenerate thesis figures
#    from the cited run's JSON, not from run_criticality. See Thesis Snapshot.
python manage.py run_pipeline --ingest rss,gkg --extract --score   # pick stages
#    --ingest takes rss, gkg, gdelt, gdelt-fallback. gdelt-fallback = one single-shot
#    DOC request per corridor; on the first 429 it stops hitting DOC and polls GKG
#    (~280 MB/24h) for ALL corridors. Each corridor's path is reported.
#    Also: --last-minutes, --max-records, --extract-limit, --crisis,
#    --duration-days, --include-sanctioned, --no-persist

# ---- the pipeline, stage by stage (the careful path) ----

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
python manage.py extract_events [--limit N] [--workers N]
#    --workers (default 4) = concurrent LLM calls. Same cost, ~4x less wall
#    time (serial measured 2.9 s/article). Rising "call failures" means the
#    provider is rate-limiting: re-run with fewer workers; failed articles are
#    left unprocessed, so a re-run retries only those. Safe to Ctrl+C.
python manage.py test_extraction --url "<url already in RawArticle>"  # 1 call

# 3. SCORE. Free, no API calls, safe to re-run.
python manage.py score_risk
#    Compare candidate formulas against the stored corpus. Read-only: no writes,
#    no API calls, no network. Prints volume-sensitivity per ingestion path,
#    normalized scores, and what Phase 4's criticality does under each.
python manage.py compare_scoring
python manage.py compare_scoring --scanned-per-day 61000   # true GPR denominator
python manage.py compare_scoring --as-of 2026-09-20T12:00:00Z  # decay as of a date
#    ⚠ --as-of does NOT reproduce old figures: it still admits every event dated
#    after the as-of time, at full weight (the lookahead defect Phase 7 fixed in
#    risk_scorer via exclude_future, but NOT in this harness). Not fixed here
#    because "reproduce" needs created_at <= as_of (what the corpus held then),
#    not timestamp <= as_of (what a backtest wants) — decide which before fixing.

# 4. CRITICALITY — the Phase 4 deliverable.
python manage.py run_criticality --port-view
python manage.py run_criticality --corridor Hormuz --cascade
python manage.py run_criticality --scenario hormuz_full
python manage.py run_criticality --rank-by centrality   # alt ranking key

# 5. RESPONSE — the Phase 5 deliverable. Free, no API calls, writes nothing.
python manage.py run_response --auto                    # respond to what the trigger picks
python manage.py run_response --corridor Hormuz --crisis severe
python manage.py run_response --corridor Cape --duration-days 60   # shows the reserve running dry
python manage.py run_response --scenario hormuz_full    # or opec_cut (supply shock)
python manage.py run_response --corridor Hormuz --include-sanctioned

# ---- one-offs ----
python manage.py backfill_event_timestamps --dry-run   # seendate repair

# ---- BACKTEST (Phase 7). Pull and score are free; only extract_events pays ----
python manage.py run_backtest --list
python manage.py run_backtest --event 2026_hormuz_closure --status
python manage.py run_backtest --event 2026_hormuz_closure --pull   # ~12 GB, ~1 h,
#    resumable: re-run to retry only days not yet sampled. --day YYYY-MM-DD for
#    one day, --workers N (default 4), --force to re-pull ok days.
python manage.py extract_events                                     # PAID
python manage.py run_backtest --event 2026_hormuz_closure           # score + verdict
#    Writes data/backtests/<event>.json (commit it). No RiskScore rows.

# ---- NOT IMPLEMENTED ----
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
If it refuses even one corridor at a time, use `--source gkg` (or `run_pipeline --ingest gdelt-fallback`, which switches automatically on the first 429) — the bulk files
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