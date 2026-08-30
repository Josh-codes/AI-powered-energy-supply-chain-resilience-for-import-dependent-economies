# AI-Driven Energy Supply Chain Resilience System

Models India's crude oil import network as a risk-weighted knowledge graph, continuously
monitors geopolitical news to update maritime corridor risk scores, applies graph-theoretic
criticality analysis to find vulnerable corridors, simulates disruption scenarios, and
generates rerouting + Strategic Petroleum Reserve (SPR) drawdown recommendations. All
outputs are served over a REST API consumed by a React dashboard.

**Core research contribution:** the delta between static baseline criticality rankings and
risk-weighted criticality rankings — quantifying how current geopolitical conditions shift
India's structural import vulnerability.

---

## Project Info

| | |
|---|---|
| Type | B.E. Major Thesis Project |
| Institution | Fr. Conceicao Rodrigues College of Engineering (FR. CRCE) |
| Department | Artificial Intelligence & Data Science |
| Academic Year | 2026–2027 |
| Developer | Joshua (solo backend developer) |

---

## Tech Stack

- **Backend:** Python 3.10+, Django 4.2, Django REST Framework, django-cors-headers,
  django-environ, PostgreSQL + PostGIS (`django.contrib.gis`)
- **Data & Graph:** NetworkX, NumPy, pandas, SciPy, PuLP
- **Pipeline & Scheduling:** Celery, Redis, django-celery-beat, feedparser, requests
- **AI:** OpenAI SDK (`gpt-4o-mini` for extraction), LangGraph, LangChain

---

## Repository Structure

```
energy-resilience/
├── CLAUDE.md          Project instructions / spec
├── API_DOCS.md        REST API documentation for the frontend
├── README.md
├── .gitignore
├── backend/           Primary working directory (Django project)
│   ├── config/        Django settings, URLs, WSGI, Celery
│   ├── core/          Models, serializers, views, API routing
│   ├── graph/         NetworkX knowledge graph (builder, algorithms, updater, state)
│   ├── pipeline/      Ingestion (GDELT/RSS/OFAC), extraction, risk scoring, Celery tasks
│   ├── criticality/   Centrality, max-flow, cascading-failure engine, named scenarios
│   ├── response/      Reroute optimizer (MCDM), SPR drawdown LP, supply-gap estimation
│   ├── orchestrator/  LangGraph pipeline definition
│   ├── backtest/      Historical validation against Brent price movements
│   ├── data/          Static seed data (JSON) + corridor geometries (GeoJSON)
│   ├── management/    Django management commands
│   └── tests/
└── frontend/          React dashboard (do not modify from backend work)
```

---

## Setup

```bash
cd backend
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Unix:     source .venv/bin/activate
pip install -r requirements.txt
```

Create `backend/.env` (see the Environment Variables section of `CLAUDE.md`):

```
DB_NAME=energy_resilience
DB_USER=postgres
DB_PASSWORD=...
DB_HOST=localhost
DB_PORT=5432
SECRET_KEY=...
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
REDIS_URL=redis://localhost:6379/0
OPENAI_API_KEY=...
EIA_API_KEY=...
CORS_ALLOWED_ORIGINS=http://localhost:3000
```

Then initialise the database and graph:

```bash
python manage.py migrate
python manage.py seed_db          # load static seed data
python manage.py build_graph      # build NetworkX graph + print summary
```

---

## Running Locally (4 terminals)

| Terminal | Command |
|---|---|
| 1 | `cd backend && python manage.py runserver` |
| 2 | `cd backend && celery -A config worker --loglevel=info` |
| 3 | `cd backend && celery -A config beat --loglevel=info` |
| 4 | `redis-server` |

---

## Common Commands

```bash
python manage.py run_pipeline                              # run the full pipeline once
python manage.py test_extraction --url "https://..."       # test LLM extraction on one article
python manage.py run_backtest --event "2025_iran_standoff" # historical backtest
python manage.py test                                      # run all tests
```

---

## Build Phases

1. Django setup + PostgreSQL/PostGIS + models + seed data + NetworkX graph
2. News ingestion (GDELT + RSS + OFAC) + Celery Beat
3. LLM extraction + risk scoring + graph weight update
4. Criticality engine (centrality + max-flow + cascading failure)
5. Response layer (reroute optimizer + SPR drawdown LP)
6. LangGraph orchestration + REST API endpoints
7. Backtest validation + integration testing + API documentation

---

## API

See `API_DOCS.md` for full endpoint documentation. The API is consumed by the React
frontend in `frontend/`.
