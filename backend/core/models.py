from django.contrib.gis.db import models


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
    route_geometry = models.LineStringField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} via {self.route_description}"
