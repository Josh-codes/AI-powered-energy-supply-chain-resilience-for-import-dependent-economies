"""REST API views (Phase 6): thin wrappers over the Phase 4/5 modules.

Every graph-backed view reads ``graph.updater.load_live_graph()``: the
singleton with the stored ``Corridor.live_risk_score`` applied, refreshed
automatically if ``score_risk`` ran in another process. Nothing here mutates
that graph: every simulation function degrades a copy. The frontend never
triggers the pipeline; the only per-request computation is graph maths
(tens of milliseconds on this 50-node graph).

Error contract: invalid input -> 400 (DRF field errors, or ``{"error": msg}``
for a domain ValueError such as the unknown corridor "Suez"); graph cannot be
built (e.g. an unseeded DB) -> 503; anything else -> logged, 500.
"""
import functools
import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from core.models import Corridor, ExtractedEvent, PipelineRun, RiskScore
from core.serializers import (
    CascadeQuery,
    CriticalityQuery,
    EventsQuery,
    ExtractedEventSerializer,
    PipelineRunSerializer,
    PipelineRunSummarySerializer,
    RerouteQuery,
    RiskHistoryQuery,
    RiskScoreSerializer,
    RunsQuery,
    SimulateInput,
    SPRQuery,
)
from criticality.cascade import cascading_failure_simulation
from criticality.engine import compute_criticality, compute_port_criticality
from criticality.scenarios import SCENARIOS
from graph.updater import load_live_graph
from response.plan import build_response
from response.reroute import rank_alternatives
from response.spr import compute_spr_schedule

logger = logging.getLogger(__name__)

# Corridor.capacity_mbd sentinel for "no binding chokepoint" (Cape).
UNLIMITED_CAPACITY_MBD = 999.0


class GraphUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "knowledge graph unavailable - is the database seeded (`manage.py seed_db`)?"


def _graph():
    try:
        return load_live_graph()
    except Exception as exc:
        logger.exception("could not load the live graph")
        raise GraphUnavailable() from exc


def _validated(serializer_cls, data):
    s = serializer_cls(data=data)
    s.is_valid(raise_exception=True)
    return s.validated_data


def endpoint(methods):
    """``@api_view`` plus the error contract in the module docstring."""
    def wrap(fn):
        @api_view(methods)
        @functools.wraps(fn)
        def view(request, *args, **kwargs):
            try:
                return fn(request, *args, **kwargs)
            except APIException:
                raise
            except ValueError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            except Exception:
                logger.exception("unhandled error in %s", fn.__name__)
                return Response(
                    {"error": "internal error - see server log"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        return view
    return wrap


def _with_threshold_alias(spr):
    # CLAUDE.md's /api/spr/ example calls days_of_cover "days_until_threshold".
    return {**spr, "days_until_threshold": spr["days_of_cover"]}


# ---- risk -----------------------------------------------------------------

@endpoint(["GET"])
def risk_scores(request):
    """``{"Hormuz": 0.875, ...}`` — the stored live score per corridor, i.e.
    exactly what the graph and every other endpoint compute from. Not the
    spec's "RiskScore rows from the last hour", which returns ``{}`` whenever
    ``score_risk`` has not run within the hour."""
    return Response({c.name: c.live_risk_score for c in Corridor.objects.order_by("name")})


@endpoint(["GET"])
def risk_score_history(request):
    """RiskScore rows oldest-first, for the trend charts. Each row carries
    ``formula``: ``raw_score`` changed meaning on 2026-09-26, so plot one
    formula at a time."""
    q = _validated(RiskHistoryQuery, request.query_params)
    rows = RiskScore.objects.select_related("corridor").order_by("computed_at")
    if "corridor" in q:
        rows = rows.filter(corridor__name=q["corridor"])
    if "days" in q:
        rows = rows.filter(computed_at__gte=timezone.now() - timedelta(days=q["days"]))
    return Response(RiskScoreSerializer(rows, many=True).data)


# ---- criticality ----------------------------------------------------------

@endpoint(["GET"])
def criticality(request):
    q = _validated(CriticalityQuery, request.query_params)
    return Response(compute_criticality(_graph(), rank_by=q["rank_by"]))


@endpoint(["GET"])
def port_criticality(request):
    return Response(compute_port_criticality(_graph()))


@endpoint(["GET"])
def cascade(request):
    """Cascade curve for one corridor, 10% steps up to ``degradation``%."""
    q = _validated(CascadeQuery, request.query_params)
    steps = cascading_failure_simulation(q["corridor"], G=_graph())
    return Response([s for s in steps if s["degradation_pct"] <= q["degradation"]])


@endpoint(["GET"])
def scenarios(request):
    out = []
    for key, sc in SCENARIOS.items():
        pct = sc.get("degradation_pct")
        out.append({"key": key, **sc, "degradation": pct / 100 if pct is not None else None})
    return Response(out)


# ---- response -------------------------------------------------------------

@endpoint(["GET"])
def reroute(request):
    q = _validated(RerouteQuery, request.query_params)
    return Response(rank_alternatives(
        q["corridor"], crisis=q["crisis"], gap_mbd=q.get("gap_mbd"),
        include_sanctioned=q["include_sanctioned"],
    ))


@endpoint(["GET"])
def spr(request):
    q = _validated(SPRQuery, request.query_params)
    return Response(_with_threshold_alias(
        compute_spr_schedule(q["gap_mbd"], q["duration_days"], q["transit_days"])
    ))


@endpoint(["POST"])
def simulate(request):
    """The dashboard's scenario slider: the cascade curve plus the full
    response (gap -> reroute -> timeline -> SPR) at the EXACT requested
    degradation, not the nearest 10% step."""
    body = _validated(SimulateInput, request.data)
    G = _graph()
    common = dict(
        crisis=body["crisis"], duration_days=body["duration_days"],
        include_sanctioned=body["include_sanctioned"],
    )
    if "scenario" in body:
        resp = build_response(G, scenario=body["scenario"], **common)
    else:
        resp = build_response(
            G, corridor=body["corridor"], degradation_pct=body["degradation"] * 100, **common,
        )
    corridor = resp["gap"]["corridor"]
    curve = cascading_failure_simulation(corridor, G=G) if corridor else []
    return Response({
        "input": body,
        "cascade": curve,
        "gap": resp["gap"],
        "reroute": resp["reroute"],
        "timeline": resp["timeline"],
        "spr": _with_threshold_alias(resp["spr"]),
    })


# ---- map + events ---------------------------------------------------------

def _coords(value):
    return [_coords(v) for v in value] if isinstance(value[0], (tuple, list)) else list(value)


@endpoint(["GET"])
def corridors_geojson(request):
    features = []
    for c in Corridor.objects.order_by("name"):
        unlimited = c.capacity_mbd >= UNLIMITED_CAPACITY_MBD
        features.append({
            "type": "Feature",
            # Built from GEOS coords rather than `.geojson`, which goes through
            # GDAL/OGR and logs a PROJ proj.db version mismatch on this machine
            # on every call. Stored SRID is 4326, already GeoJSON's lon/lat.
            "geometry": {"type": c.geometry.geom_type, "coordinates": _coords(c.geometry.coords)},
            "properties": {
                "name": c.name,
                "risk_score": c.live_risk_score,
                "baseline_risk": c.baseline_risk,
                "capacity_mbd": None if unlimited else c.capacity_mbd,
                "capacity_unlimited": unlimited,
                "transit_days": c.transit_days,
            },
        })
    return Response({"type": "FeatureCollection", "features": features})


@endpoint(["GET"])
def events_live(request):
    q = _validated(EventsQuery, request.query_params)
    rows = ExtractedEvent.objects.select_related("corridor").order_by("-timestamp")
    if "corridor" in q:
        rows = rows.filter(corridor__name=q["corridor"])
    return Response(ExtractedEventSerializer(rows[: q["limit"]], many=True).data)


# ---- pipeline runs ----------------------------------------------------------

@endpoint(["GET"])
def pipeline_latest(request):
    run = PipelineRun.objects.first()  # Meta.ordering = -started_at
    if run is None:
        return Response(
            {"error": "no pipeline run yet - run `manage.py run_pipeline`"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(PipelineRunSerializer(run).data)


@endpoint(["GET"])
def pipeline_runs(request):
    q = _validated(RunsQuery, request.query_params)
    return Response(PipelineRunSummarySerializer(PipelineRun.objects.all()[: q["limit"]], many=True).data)


@endpoint(["GET"])
def pipeline_run_detail(request, pk):
    run = PipelineRun.objects.filter(pk=pk).first()
    if run is None:
        return Response({"error": f"no pipeline run {pk}"}, status=status.HTTP_404_NOT_FOUND)
    return Response(PipelineRunSerializer(run).data)


# ---- not yet built ------------------------------------------------------------

@endpoint(["GET"])
def backtest(request):
    return Response(
        {"error": "backtest is not implemented yet (Phase 7)"},
        status=status.HTTP_501_NOT_IMPLEMENTED,
    )
