"""Admin registrations."""
from django.contrib import admin

from core.models import PipelineRun


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    list_display = ("id", "started_at", "status", "triggered_corridor", "capacity_loss_mbd")
    list_filter = ("status",)
    readonly_fields = [f.name for f in PipelineRun._meta.fields]
