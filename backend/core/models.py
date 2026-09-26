from django.contrib.gis.db import models
from django.utils import timezone


class Supplier(models.Model):
    name = models.CharField(max_length=100)
    country_code = models.CharField(max_length=3)
    region = models.CharField(max_length=50)
    avg_export_mbd = models.FloatField()        # million barrels/day to India
    sanctioned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Corridor(models.Model):
    name = models.CharField(max_length=100, unique=True)
    geometry = models.LineStringField()          # PostGIS maritime route
    capacity_mbd = models.FloatField()           # million barrels/day
    transit_days = models.IntegerField()
    baseline_risk = models.FloatField(default=0.1)
    live_risk_score = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Port(models.Model):
    name = models.CharField(max_length=100)
    location = models.PointField()               # PostGIS lat/lon
    state = models.CharField(max_length=50)
    throughput_mbd = models.FloatField()

    def __str__(self):
        return self.name


class Refinery(models.Model):
    name = models.CharField(max_length=100)
    company = models.CharField(max_length=100)
    location = models.PointField()
    capacity_mbd = models.FloatField()
    min_run_rate = models.FloatField(default=0.70)
    api_gravity_min = models.FloatField()
    api_gravity_max = models.FloatField()
    sulfur_tolerance = models.FloatField()       # max % sulfur
    port = models.ForeignKey(Port, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return self.name


class RawArticle(models.Model):
    url = models.URLField(unique=True, max_length=500)
    source = models.CharField(max_length=50)    # gdelt / reuters / lloyds
    title = models.CharField(max_length=500)
    raw_text = models.TextField()
    ingested_at = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=['processed', 'ingested_at'])]


class ExtractedEvent(models.Model):
    CORRIDOR_CHOICES = [
        ('Hormuz', 'Strait of Hormuz'),
        ('Red Sea', 'Red Sea / Bab-el-Mandeb'),
        ('Suez', 'Suez Canal'),
        ('Cape', 'Cape of Good Hope'),
        ('None', 'Not corridor-specific'),
    ]
    EVENT_TYPE_CHOICES = [
        ('sanction', 'Sanction'),
        ('military', 'Military'),
        ('shipping', 'Shipping'),
        ('policy', 'Policy'),
        ('other', 'Other'),
    ]
    corridor = models.ForeignKey(
        Corridor, on_delete=models.SET_NULL, null=True, blank=True
    )
    actor = models.CharField(max_length=200)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    severity = models.IntegerField()             # 1-5
    confidence = models.FloatField()             # 0.0-1.0
    timestamp = models.DateTimeField()
    article_url = models.URLField(max_length=500)
    # Copied from the source RawArticle, which is deleted after 14 days. Without
    # it this permanent store could not detect that one syndicated wire story
    # produced a dozen events — see pipeline/score/risk_scorer.py clustering.
    title = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['corridor', 'timestamp'])]


class RiskScore(models.Model):
    corridor = models.ForeignKey(Corridor, on_delete=models.CASCADE)
    score = models.FloatField()                  # 0.0-1.0 normalized
    raw_score = models.FloatField()              # unnormalized
    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['corridor', 'computed_at'])]
        ordering = ['-computed_at']


class AlternativeSupplier(models.Model):
    name = models.CharField(max_length=100)
    country = models.CharField(max_length=100)
    route_description = models.CharField(max_length=200)
    avoids_corridor = models.CharField(max_length=50)  # which corridor this avoids
    transit_days = models.IntegerField()
    price_premium_usd = models.FloatField()      # $/barrel vs disrupted source
    api_gravity = models.FloatField()
    sulfur_pct = models.FloatField()
    sanctioned = models.BooleanField(default=False)
    # Phase 5: corridors this route physically transits (Suez folded into
    # "Red Sea"). Reroute eligibility reads THIS, not avoids_corridor, which
    # only ever named Hormuz and so left Cape/Red Sea with no candidates.
    transits_corridors = models.JSONField(default=list)
    max_incremental_mbd = models.FloatField(default=0.0)  # spare mb/d redirectable to India (estimate)
    route_geometry = models.LineStringField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} via {self.route_description}"


class PipelineRun(models.Model):
    """One orchestrator run (Phase 6): what ran, what it found, and the
    response it recommended. PERMANENT, like ExtractedEvent and RiskScore.

    Outputs are stored as JSON snapshots rather than normalized tables: they
    are nested (per-refinery shortfalls, daily SPR schedules) and their shapes
    are still additive-evolving, so a relational schema would need a migration
    every time a key is added. Writing a run never touches
    ``Corridor.live_risk_score``, so persisting one cannot move the thesis
    snapshot figures — only the ``score`` stage does that.
    """
    STATUS_RUNNING = 'running'
    STATUS_SUCCEEDED = 'succeeded'
    STATUS_PARTIAL = 'partial'      # completed, but at least one stage failed
    STATUS_FAILED = 'failed'        # the orchestrator itself raised
    STATUS_CHOICES = [
        (STATUS_RUNNING, 'Running'),
        (STATUS_SUCCEEDED, 'Succeeded'),
        (STATUS_PARTIAL, 'Partial'),
        (STATUS_FAILED, 'Failed'),
    ]

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    options = models.JSONField(default=dict)
    stages_run = models.JSONField(default=list)
    ingest_report = models.JSONField(null=True, blank=True)
    extraction_report = models.JSONField(null=True, blank=True)
    risk_scores = models.JSONField(default=dict)
    criticality = models.JSONField(default=list)
    threshold = models.JSONField(null=True, blank=True)
    triggered_corridor = models.CharField(max_length=100, null=True, blank=True)
    capacity_loss_mbd = models.FloatField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)  # {gap, reroute, timeline, spr}
    errors = models.JSONField(default=list)

    class Meta:
        indexes = [models.Index(fields=['started_at'])]
        ordering = ['-started_at']

    def __str__(self):
        return f"PipelineRun {self.pk} ({self.status}) {self.started_at:%Y-%m-%d %H:%M}"
