"""API URL routing, mounted under /api/ by config/urls.py."""
from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("risk-scores/", views.risk_scores, name="risk-scores"),
    path("risk-scores/history/", views.risk_score_history, name="risk-score-history"),
    path("criticality/", views.criticality, name="criticality"),
    path("criticality/ports/", views.port_criticality, name="port-criticality"),
    path("cascade/", views.cascade, name="cascade"),
    path("scenarios/", views.scenarios, name="scenarios"),
    path("reroute/", views.reroute, name="reroute"),
    path("spr/", views.spr, name="spr"),
    path("simulate/", views.simulate, name="simulate"),
    path("corridors/geojson/", views.corridors_geojson, name="corridors-geojson"),
    path("events/live/", views.events_live, name="events-live"),
    path("pipeline/latest/", views.pipeline_latest, name="pipeline-latest"),
    path("pipeline/runs/", views.pipeline_runs, name="pipeline-runs"),
    path("pipeline/runs/<int:pk>/", views.pipeline_run_detail, name="pipeline-run-detail"),
    path("backtest/", views.backtest, name="backtest"),
]
