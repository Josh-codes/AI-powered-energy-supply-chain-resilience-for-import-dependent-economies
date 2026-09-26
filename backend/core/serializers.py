"""DRF serializers: query/body validation for the API, plus the DB-backed
output shapes.

Computed results (criticality rows, cascade steps, reroute rankings, SPR
schedules) are plain dicts produced by the Phase 4/5 modules, whose docstrings
own their shapes; the views return them as-is rather than re-declaring every
key here, so an additive key in a module never needs a serializer change.
"""
from rest_framework import serializers

from core.models import ExtractedEvent, PipelineRun, RiskScore
from criticality.engine import RANK_KEYS
from criticality.scenarios import SCENARIOS
from pipeline.score.risk_scorer import TOP_K_FORMULA_SINCE
from response.reroute import CRISIS_WEIGHTS
from response.spr import MAX_DURATION_DAYS

CRISIS_CHOICES = sorted(CRISIS_WEIGHTS)


# ---- inputs -------------------------------------------------------------------

class CriticalityQuery(serializers.Serializer):
    rank_by = serializers.ChoiceField(choices=RANK_KEYS, default="capacity_loss")


class CascadeQuery(serializers.Serializer):
    corridor = serializers.CharField()
    degradation = serializers.IntegerField(min_value=1, max_value=100, default=100)


class RerouteQuery(serializers.Serializer):
    corridor = serializers.CharField()
    crisis = serializers.ChoiceField(choices=CRISIS_CHOICES, default="normal")
    gap_mbd = serializers.FloatField(min_value=0, required=False)
    include_sanctioned = serializers.BooleanField(default=False)


class SPRQuery(serializers.Serializer):
    gap_mbd = serializers.FloatField(min_value=0)
    duration_days = serializers.IntegerField(min_value=1, max_value=MAX_DURATION_DAYS)
    transit_days = serializers.IntegerField(min_value=0)


class RiskHistoryQuery(serializers.Serializer):
    corridor = serializers.CharField(required=False)
    days = serializers.IntegerField(min_value=1, max_value=3650, required=False)


class EventsQuery(serializers.Serializer):
    corridor = serializers.CharField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=500, default=50)


class RunsQuery(serializers.Serializer):
    limit = serializers.IntegerField(min_value=1, max_value=200, default=20)


class SimulateInput(serializers.Serializer):
    """``degradation`` is a FRACTION (0-1], as the dashboard slider sends it;
    the modules underneath take a percentage."""
    corridor = serializers.CharField(required=False)
    scenario = serializers.ChoiceField(choices=sorted(SCENARIOS), required=False)
    degradation = serializers.FloatField(min_value=0.0, max_value=1.0, default=1.0)
    duration_days = serializers.IntegerField(min_value=1, max_value=365, default=14)
    crisis = serializers.ChoiceField(choices=CRISIS_CHOICES, default="normal")
    include_sanctioned = serializers.BooleanField(default=False)

    def validate(self, data):
        if ("corridor" in data) == ("scenario" in data):
            raise serializers.ValidationError("pass exactly one of corridor or scenario")
        if "corridor" in data and data["degradation"] <= 0:
            raise serializers.ValidationError({"degradation": "must be > 0"})
        return data


# ---- outputs ------------------------------------------------------------------

class ExtractedEventSerializer(serializers.ModelSerializer):
    corridor = serializers.CharField(source="corridor.name", default=None)

    class Meta:
        model = ExtractedEvent
        fields = ["id", "corridor", "actor", "event_type", "severity", "confidence",
                  "timestamp", "title", "article_url"]


class RiskScoreSerializer(serializers.ModelSerializer):
    corridor = serializers.CharField(source="corridor.name")
    formula = serializers.SerializerMethodField()

    class Meta:
        model = RiskScore
        fields = ["id", "corridor", "score", "raw_score", "computed_at", "formula"]

    def get_formula(self, obj):
        # raw_score changed meaning on 2026-09-26: see TOP_K_FORMULA_SINCE.
        return "top3pad" if obj.computed_at >= TOP_K_FORMULA_SINCE else "sum_saturating"


class PipelineRunSummarySerializer(serializers.ModelSerializer):
    error_count = serializers.SerializerMethodField()

    class Meta:
        model = PipelineRun
        fields = ["id", "started_at", "finished_at", "status", "stages_run",
                  "triggered_corridor", "capacity_loss_mbd", "error_count"]

    def get_error_count(self, obj):
        return len(obj.errors or [])


class PipelineRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = PipelineRun
        fields = "__all__"
