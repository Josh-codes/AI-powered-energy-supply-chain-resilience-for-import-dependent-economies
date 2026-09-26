"""MCDM reroute optimizer: rank alternative crude sources for a disrupted
corridor.

    score = w_cost * cost_score + w_transit * transit_score + w_compat * compat_score

with CLAUDE.md's weights — ``(0.40, 0.35, 0.25)`` normally, ``(0.20, 0.55, 0.25)``
in a severe crisis, where speed matters more than price.

Eligibility reads ``AlternativeSupplier.transits_corridors``, NOT
``avoids_corridor``: every seeded row's ``avoids_corridor`` is "Hormuz", so
eligibility keyed on it left Cape and Red Sea with no candidates, and it let
Saudi-via-Yanbu be offered as a Red Sea replacement although the route runs
through Suez. An alternative is eligible iff it does not transit the disrupted
corridor.

Scores are min-max normalized over the candidate set actually being ranked
(after the eligibility and sanctions filters), so a score says how good an
option is relative to what is available for THIS disruption. Scores are not
comparable across corridors or across ``include_sanctioned`` settings.

Row shape keeps CLAUDE.md's ``/api/reroute/`` keys exactly (``source``,
``score``, ``cost_score``, ``transit_score``, ``compat_score``,
``transit_days``, ``price_premium``) and extends them additively.
"""
import logging

from core.models import AlternativeSupplier, Corridor, Refinery

logger = logging.getLogger(__name__)

CRISIS_WEIGHTS = {
    "normal": (0.40, 0.35, 0.25),  # cost, transit, compat
    "severe": (0.20, 0.55, 0.25),
}

EPS = 1e-9


def normalize(value, all_values, invert=False):
    """CLAUDE.md's min-max normalizer, verbatim: 1.0 on a degenerate range, so
    a criterion every candidate ties on neither rewards nor penalizes anyone."""
    min_v, max_v = min(all_values), max(all_values)
    if max_v == min_v:
        return 1.0
    score = (value - min_v) / (max_v - min_v)
    return 1 - score if invert else score


def compute_grade_compatibility(alt, refineries=None):
    """Fraction of Indian refinery nameplate capacity (mb/d) whose API-gravity
    window and sulfur tolerance admit this alternative's crude. In [0, 1].

    Deliberately NOT ``graph.builder._grade_compatible``: that one answers a
    boolean graph-construction question (does ANY crude reaching a port fit
    this refinery) and defaults open when nothing is known. MCDM needs a
    continuous score, and weighting by capacity means fitting Jamnagar counts
    for more than fitting a 1,000 b/d unit.

    ``refineries`` is any iterable of objects with ``capacity_mbd``,
    ``api_gravity_min``, ``api_gravity_max``, ``sulfur_tolerance``; defaults to
    every ``Refinery`` row.
    """
    refineries = list(Refinery.objects.all() if refineries is None else refineries)
    total = sum(r.capacity_mbd for r in refineries)
    if total <= EPS:
        return 0.0
    fit = sum(
        r.capacity_mbd for r in refineries
        if r.api_gravity_min <= alt.api_gravity <= r.api_gravity_max
        and alt.sulfur_pct <= r.sulfur_tolerance
    )
    return fit / total


def score_alternative(alt, all_alts, crisis="normal", refineries=None):
    """Component scores and weighted total for one alternative, normalized
    against ``all_alts``. Returns a dict (the spec returned a bare float, but
    the documented API row needs the components)."""
    if crisis not in CRISIS_WEIGHTS:
        raise ValueError(f"crisis must be one of {sorted(CRISIS_WEIGHTS)}, got {crisis!r}")
    w_cost, w_transit, w_compat = CRISIS_WEIGHTS[crisis]

    cost = normalize(alt.price_premium_usd, [a.price_premium_usd for a in all_alts], invert=True)
    transit = normalize(alt.transit_days, [a.transit_days for a in all_alts], invert=True)
    compat = compute_grade_compatibility(alt, refineries)
    return {
        "cost_score": cost,
        "transit_score": transit,
        "compat_score": compat,
        "score": w_cost * cost + w_transit * transit + w_compat * compat,
    }


def rank_alternatives(
    corridor_name, *, crisis="normal", gap_mbd=None, include_sanctioned=False,
    alternatives=None, refineries=None,
):
    """Ranked replacement options for a disruption of ``corridor_name``.

    ``corridor_name=None`` means no corridor is disrupted (a supply-side shock
    such as ``opec_cut``), so no candidate is excluded on route. Sanctioned
    rows are dropped unless ``include_sanctioned``.

    When ``gap_mbd`` is given, each row also carries the cumulative
    ``max_incremental_mbd`` down the ranking (``cumulative_coverage_mbd`` /
    ``_pct``) and ``covers_gap``. Those volumes are ESTIMATES (see
    alternatives.json ``source``), so coverage is indicative, not a balance.

    Returns ``[]`` when nothing is eligible — never raises for that case.
    """
    if crisis not in CRISIS_WEIGHTS:
        raise ValueError(f"crisis must be one of {sorted(CRISIS_WEIGHTS)}, got {crisis!r}")
    if corridor_name is not None and not Corridor.objects.filter(name=corridor_name).exists():
        # An unknown name would exclude nothing and silently rank everything.
        raise ValueError(f"unknown corridor {corridor_name!r}")
    if gap_mbd is not None and gap_mbd < 0:
        raise ValueError(f"gap_mbd must be >= 0, got {gap_mbd}")

    alts = list(AlternativeSupplier.objects.all() if alternatives is None else alternatives)
    refineries = list(Refinery.objects.all() if refineries is None else refineries)

    candidates = [
        a for a in alts
        if (corridor_name is None or corridor_name not in (a.transits_corridors or []))
        and (include_sanctioned or not a.sanctioned)
    ]
    if not candidates:
        logger.warning("no eligible alternatives for corridor %r", corridor_name)
        return []

    rows = []
    for a in candidates:
        s = score_alternative(a, candidates, crisis, refineries)
        rows.append(
            {
                "source": a.name,
                "score": s["score"],
                "cost_score": s["cost_score"],
                "transit_score": s["transit_score"],
                "compat_score": s["compat_score"],
                "transit_days": a.transit_days,
                "price_premium": a.price_premium_usd,
                "country": a.country,
                "route_description": a.route_description,
                "transits_corridors": list(a.transits_corridors or []),
                "sanctioned": a.sanctioned,
                "max_incremental_mbd": a.max_incremental_mbd,
                "crisis": crisis,
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["source"]))

    if gap_mbd is not None:
        cumulative = 0.0
        for r in rows:
            cumulative += r["max_incremental_mbd"]
            r["cumulative_coverage_mbd"] = cumulative
            r["cumulative_coverage_pct"] = (
                100.0 if gap_mbd <= EPS else min(100.0, cumulative / gap_mbd * 100.0)
            )
            r["covers_gap"] = cumulative >= gap_mbd - EPS

    return rows


def replacement_timeline(ranked, gap_mbd, duration_days=None):
    """When replacement crude arrives and how much of the gap it closes,
    procuring alternatives in rank order until their volumes cover the gap.

    Returns ``{"transit_days", "alternatives_used", "covered_mbd",
    "residual_gap_mbd", "covers_gap"}``, plus ``"daily_gap_mbd"`` when
    ``duration_days`` is given. ``transit_days`` is when the SLOWEST cargo
    needed lands (None when ``ranked`` is empty). When the ranking never
    covers the gap, every alternative is used and ``residual_gap_mbd`` is what
    stays uncovered even after all of them arrive.

    ``daily_gap_mbd`` is the gap faced on each day 0..duration_days-1: the
    full gap, minus each used alternative's volume from the day its cargo lands
    (a cargo with ``transit_days`` = 8 covers from day index 8, matching the
    SPR's constant-gap convention). It is what ``compute_spr_schedule``'s
    ``gap_profile`` takes — modelling arrivals one by one instead of as one
    step at the slowest, and keeping the residual open after the last lands.
    """
    if gap_mbd < 0:
        raise ValueError(f"gap_mbd must be >= 0, got {gap_mbd}")
    used, covered = [], 0.0
    for r in ranked:
        if covered >= gap_mbd - EPS:
            break
        used.append(r)
        covered += r["max_incremental_mbd"]
    result = {
        "transit_days": max((r["transit_days"] for r in used), default=None),
        "alternatives_used": [r["source"] for r in used],
        "covered_mbd": covered,
        "residual_gap_mbd": max(0.0, gap_mbd - covered),
        "covers_gap": covered >= gap_mbd - EPS,
    }
    if duration_days is not None:
        if duration_days <= 0:
            raise ValueError(f"duration_days must be > 0, got {duration_days}")
        result["daily_gap_mbd"] = [
            max(0.0, gap_mbd - sum(r["max_incremental_mbd"] for r in used if r["transit_days"] <= t))
            for t in range(duration_days)
        ]
    return result
