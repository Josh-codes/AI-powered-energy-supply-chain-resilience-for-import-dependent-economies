"""Admin registrations.

Everything is read-only: the admin is a browser over the pipeline's outputs
and the seeded network, not a way to edit them. Editing a Corridor's
live_risk_score here would silently move every API response, and
ExtractedEvent / RiskScore / PipelineRun are permanent records.
"""
from django.contrib import admin

from core.models import (
    AlternativeSupplier,
    Corridor,
    ExtractedEvent,
    PipelineRun,
    RiskScore,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PipelineRun)
class PipelineRunAdmin(ReadOnlyAdmin):
    list_display = ("id", "started_at", "status", "triggered_corridor", "capacity_loss_mbd")
    list_filter = ("status",)


@admin.register(Corridor)
class CorridorAdmin(ReadOnlyAdmin):
    list_display = ("name", "live_risk_score", "baseline_risk", "capacity_mbd",
                    "transit_days", "updated_at")


@admin.register(ExtractedEvent)
class ExtractedEventAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "corridor", "severity", "confidence", "event_type", "title")
    list_filter = ("corridor", "event_type", "severity")
    search_fields = ("title", "actor")
    date_hierarchy = "timestamp"
    list_select_related = ("corridor",)


@admin.register(RiskScore)
class RiskScoreAdmin(ReadOnlyAdmin):
    list_display = ("id", "computed_at", "corridor", "score", "raw_score")
    list_filter = ("corridor",)
    list_select_related = ("corridor",)


@admin.register(AlternativeSupplier)
class AlternativeSupplierAdmin(ReadOnlyAdmin):
    list_display = ("name", "route_description", "transit_days", "price_premium_usd",
                    "max_incremental_mbd", "sanctioned")
    list_filter = ("sanctioned",)
