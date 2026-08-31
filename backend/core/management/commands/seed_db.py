"""Load the static seed data (data/*.json + data/geometries/*.geojson) into the
database.

Idempotent: every row is keyed on its natural name and upserted, so running the
command repeatedly is safe. Node models only — Supplier, Corridor, Port,
Refinery, AlternativeSupplier. The graph *edges* (data/edges.json) are not a
model; they are consumed later by graph/builder.py.

    python manage.py seed_db
    python manage.py seed_db --flush     # delete existing rows first
"""
import json
import logging
from pathlib import Path

from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry, Point
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import AlternativeSupplier, Corridor, Port, Refinery, Supplier

logger = logging.getLogger(__name__)

DATA_DIR = Path(settings.BASE_DIR) / "data"
GEOM_DIR = DATA_DIR / "geometries"


def _read_json(name):
    path = DATA_DIR / name
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _corridor_slug(name):
    return name.strip().lower().replace(" ", "_")


def _load_linestring(slug):
    """Return a GEOSGeometry LineString (SRID 4326) from geometries/<slug>.geojson.

    Accepts a bare geometry, a Feature, or a FeatureCollection.
    """
    path = GEOM_DIR / f"{slug}.geojson"
    if not path.exists() or path.stat().st_size == 0:
        raise FileNotFoundError(
            f"missing or empty corridor geometry: {path} "
            f"(every Corridor needs a LineString)"
        )
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("type") == "FeatureCollection":
        obj = obj["features"][0]["geometry"]
    elif obj.get("type") == "Feature":
        obj = obj["geometry"]
    geom = GEOSGeometry(json.dumps(obj), srid=4326)
    if geom.geom_type != "LineString":
        raise ValueError(f"{path}: expected LineString, got {geom.geom_type}")
    return geom


class Command(BaseCommand):
    help = "Load static seed data (suppliers, corridors, ports, refineries, alternatives)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing rows in the seeded tables before loading.",
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            if options["flush"]:
                self._flush()

            counts = {
                "Supplier": self._seed_suppliers(),
                "Corridor": self._seed_corridors(),
                "Port": self._seed_ports(),
                "Refinery": self._seed_refineries(),
                "AlternativeSupplier": self._seed_alternatives(),
            }

        for model, (created, updated) in counts.items():
            self.stdout.write(
                self.style.SUCCESS(f"  {model:20} created={created:<4} updated={updated}")
            )

        self._check_edges_reference_integrity()
        self.stdout.write(self.style.SUCCESS("seed_db complete."))

    # ------------------------------------------------------------------ flush
    def _flush(self):
        for model in (AlternativeSupplier, Refinery, Port, Corridor, Supplier):
            deleted, _ = model.objects.all().delete()
            logger.info("flushed %s (%s rows)", model.__name__, deleted)
        self.stdout.write(self.style.WARNING("  flushed seeded tables"))

    # ---------------------------------------------------------------- seeders
    def _seed_suppliers(self):
        created = updated = 0
        for row in _read_json("suppliers.json"):
            _, was_created = Supplier.objects.update_or_create(
                name=row["name"],
                defaults={
                    "country_code": row["country_code"],
                    "region": row["region"],
                    "avg_export_mbd": row["avg_export_mbd"],
                    "sanctioned": row.get("sanctioned", False),
                },
            )
            created, updated = (created + 1, updated) if was_created else (created, updated + 1)
        return created, updated

    def _seed_corridors(self):
        created = updated = 0
        for row in _read_json("corridors.json"):
            geom = _load_linestring(_corridor_slug(row["name"]))
            _, was_created = Corridor.objects.update_or_create(
                name=row["name"],
                defaults={
                    "geometry": geom,
                    "capacity_mbd": row["capacity_mbd"],
                    "transit_days": row["transit_days"],
                    "baseline_risk": row.get("baseline_risk", 0.1),
                },
            )
            created, updated = (created + 1, updated) if was_created else (created, updated + 1)
        return created, updated

    def _seed_ports(self):
        created = updated = 0
        for row in _read_json("ports.json"):
            _, was_created = Port.objects.update_or_create(
                name=row["name"],
                defaults={
                    "location": Point(float(row["lon"]), float(row["lat"]), srid=4326),
                    "state": row["state"],
                    "throughput_mbd": row["throughput_mbd"],
                },
            )
            created, updated = (created + 1, updated) if was_created else (created, updated + 1)
        return created, updated

    def _seed_refineries(self):
        created = updated = 0
        for row in _read_json("refineries.json"):
            port_name = row.get("connected_port")
            port = Port.objects.filter(name=port_name).first()
            if port_name and port is None:
                logger.warning(
                    "refinery %r references unknown port %r; leaving port unset",
                    row["name"], port_name,
                )
            _, was_created = Refinery.objects.update_or_create(
                name=row["name"],
                defaults={
                    "company": row["company"],
                    "location": Point(float(row["lon"]), float(row["lat"]), srid=4326),
                    "capacity_mbd": row["capacity_mbd"],
                    "min_run_rate": row.get("min_run_rate", 0.70),
                    "api_gravity_min": row["api_gravity_min"],
                    "api_gravity_max": row["api_gravity_max"],
                    "sulfur_tolerance": row["sulfur_tolerance"],
                    "port": port,
                },
            )
            created, updated = (created + 1, updated) if was_created else (created, updated + 1)
        return created, updated

    def _seed_alternatives(self):
        created = updated = 0
        for row in _read_json("alternatives.json"):
            _, was_created = AlternativeSupplier.objects.update_or_create(
                name=row["name"],
                defaults={
                    "country": row["country"],
                    "route_description": row["route_description"],
                    "avoids_corridor": row["avoids_corridor"],
                    "transit_days": row["transit_days"],
                    "price_premium_usd": row["price_premium_usd"],
                    "api_gravity": row["api_gravity"],
                    "sulfur_pct": row["sulfur_pct"],
                    "sanctioned": row.get("sanctioned", False),
                },
            )
            created, updated = (created + 1, updated) if was_created else (created, updated + 1)
        return created, updated

    # -------------------------------------------------- edges.json sanity check
    def _check_edges_reference_integrity(self):
        """Warn (do not fail) if data/edges.json names don't resolve to seeded
        rows — the graph builder in Phase 1 needs these to line up exactly."""
        try:
            edges = _read_json("edges.json")
        except FileNotFoundError:
            return

        suppliers = set(Supplier.objects.values_list("name", flat=True))
        corridors = set(Corridor.objects.values_list("name", flat=True))
        ports = set(Port.objects.values_list("name", flat=True))
        refineries = set(Refinery.objects.values_list("name", flat=True))

        problems = []
        for e in edges.get("supplier_to_corridor", []):
            if e["supplier"] not in suppliers:
                problems.append(f"supplier_to_corridor: unknown supplier {e['supplier']!r}")
            if e["corridor"] not in corridors:
                problems.append(f"supplier_to_corridor: unknown corridor {e['corridor']!r}")
        for e in edges.get("corridor_to_port", []):
            if e["corridor"] not in corridors:
                problems.append(f"corridor_to_port: unknown corridor {e['corridor']!r}")
            if e["port"] not in ports:
                problems.append(f"corridor_to_port: unknown port {e['port']!r}")
        for e in edges.get("port_to_refinery", []):
            if e["port"] not in ports:
                problems.append(f"port_to_refinery: unknown port {e['port']!r}")
            if e["refinery"] not in refineries:
                problems.append(f"port_to_refinery: unknown refinery {e['refinery']!r}")

        if problems:
            self.stdout.write(self.style.WARNING(
                f"\n  edges.json references {len(problems)} name(s) that don't match seeded rows:"
            ))
            for p in sorted(set(problems)):
                self.stdout.write(self.style.WARNING(f"    - {p}"))
            self.stdout.write(self.style.WARNING(
                "  fix these in edges.json before running build_graph.\n"
            ))
