"""SPR drawdown linear program: cover a supply gap with India's Strategic
Petroleum Reserve while replacement crude is on its way.

Units — the easiest thing in this module to get wrong:
  * ``SPR_TOTAL_MB`` / ``available_mb`` / ``total_released_mb`` are a STOCK
    (million barrels).
  * ``gap_mbd`` / ``MAX_DAILY_MBD`` / every ``daily_schedule`` entry are a RATE
    (million barrels per day). One day of release at rate r draws r mb.

The gap is a PER-DAY PROFILE, not a constant. Replacement cargoes land on
different days (UAE day 8, Saudi day 10, ...) and each one shrinks the gap, and
when the alternatives cannot cover the whole gap a residual stays open after
the last one lands. An earlier version modelled "constant gap until the slowest
cargo lands, zero after", which overstated the need before that day and then
stopped releasing while a residual gap was still open and reserve was left —
on Cape it quit on day 16 with 13.5 mb unused against a permanent 0.745 mb/d
shortfall. Pass ``gap_profile`` (see ``response.reroute.replacement_timeline``)
to model arrivals; without it the profile is CLAUDE.md's constant-gap
``[gap] * transit_days`` followed by zeros, so the documented signature and
behaviour are unchanged.

Why this is not CLAUDE.md's LP verbatim. The spec pins every variable
(``release[t] >= min(gap, MAX_DAILY)`` before transit, ``== 0`` after) and adds
``sum(release) <= available`` as a HARD constraint, so (a) there is nothing left
to optimize and (b) whenever the reserve cannot cover the gap the problem is
infeasible and the solver hands back ``None`` for every day. The version below
adds an ``unmet[t]`` slack per day so the problem is always feasible, and
``insufficient`` becomes a computed result instead of a solver failure.

Objective, lexicographic via weights:
  1. minimize total unmet gap (``UNMET_PENALTY``) — use the reserve fully;
  2. minimize the PEAK daily shortfall — when the reserve cannot cover every
     day, do not cover some days in full while others fall off a cliff;
  3. tie-break (``EARLY_TIE_BREAK``): among schedules equal on 1 and 2, cover
     EARLIER days first. With a stepped profile the rate cap can force the
     peak on the early days, after which the peak term no longer constrains the
     rest; without this term the solver would return an arbitrary vertex, and a
     schedule that changes with the solver version is not a reproducible
     result. Earlier-first is also the defensible policy: the near days are
     certain, later ones may be eased by events. It makes
     ``reserve_exhausted_day`` a meaningful output.
The spec's "minimize total drawdown" term is not carried: under
``release[t] + unmet[t] == gap[t]`` total release is fixed once total unmet
is, so that term is inert.

Solver: scipy's HiGHS (``linprog``), not PuLP+CBC. PuLP 3.3.2's bundled CBC
fails on this machine whenever its output is redirected (``msg=0`` and
``logPath`` both raise "Error while executing"), which is the only mode usable
in a server or a test run. HiGHS runs in-process — no subprocess, no temp
files, thread-safe under Django. The LP formulation is solver-independent;
``pip install highspy`` would let PuLP drive the same HiGHS in-process.
"""
import logging

import numpy as np
from scipy.optimize import linprog

logger = logging.getLogger(__name__)

SPR_TOTAL_MB = 36.87    # million barrels (STOCK) — ISPR Phase I: Vizag + Mangaluru + Padur
SPR_SAFETY_PCT = 0.20   # fraction held back as a strategic floor, never released
MAX_DAILY_MBD = 1.0     # million barrels/day (RATE) — physical drawdown limit
UNMET_PENALTY = 1000.0  # >> 1, so the peak term can never trade against total unmet
# Total tie-break weight is < EARLY_TIE_BREAK per mb of shortfall moved, and
# raising the peak by d frees at most D*d mb to move, so it cannot trade
# against the peak term while EARLY_TIE_BREAK * D < 1 (D < 1,000 days).
EARLY_TIE_BREAK = 1e-3

EPS = 1e-9
MAX_DURATION_DAYS = 999


def _days_of_cover(available_mb, gap_mbd, max_daily_mbd):
    """How long the releasable reserve lasts at the rate the initial gap
    demands (capped at the physical limit). CLAUDE.md's /api/spr/ example
    calls this ``days_until_threshold``. None when there is no gap."""
    rate = min(gap_mbd, max_daily_mbd)
    return available_mb / rate if rate > EPS else None


def _reserve_exhausted_day(schedule, unmet, available_mb):
    """First day (0-indexed) on which the cumulative release reaches the
    releasable reserve while gap is still unmet on a later day — i.e. the day
    the SPR runs dry mid-crisis. None if it never does."""
    running = 0.0
    for t, release in enumerate(schedule):
        running += release
        if running >= available_mb - 1e-6 and any(u > 1e-6 for u in unmet[t + 1:]):
            return t
    return None


def _result(schedule, unmet, profile, *, gap_mbd, duration_days, transit_days,
            available_mb, max_daily_mbd, status, profiled):
    required = sum(profile)
    total = sum(schedule)
    total_unmet = sum(unmet)
    return {
        "daily_schedule": schedule,
        "total_released_mb": total,
        "insufficient": total_unmet > 1e-6,
        "gap_covered_pct": 100.0 if required <= EPS else min(100.0, total / required * 100.0),
        "daily_gap_mbd": list(profile),
        "unmet_mbd": unmet,
        "total_unmet_mb": total_unmet,
        "required_mb": required,
        "available_mb": available_mb,
        "bridge_days": sum(1 for g in profile if g > EPS),
        # Constant-gap mode only: gap days past the horizon. With a profile the
        # gap may never close (a residual), so this is None and
        # gap_at_horizon_end_mbd says what is still open instead.
        "uncovered_tail_days": None if profiled else max(0, transit_days - duration_days),
        "gap_at_horizon_end_mbd": profile[-1] if profile else 0.0,
        "reserve_exhausted_day": _reserve_exhausted_day(schedule, unmet, available_mb),
        "days_of_cover": _days_of_cover(available_mb, gap_mbd, max_daily_mbd),
        "status": status,
    }


def compute_spr_schedule(
    gap_mbd, duration_days, transit_days, *, gap_profile=None,
    spr_total_mb=SPR_TOTAL_MB, safety_pct=SPR_SAFETY_PCT, max_daily_mbd=MAX_DAILY_MBD,
):
    """Daily SPR release (mb/d) over ``duration_days``.

    Without ``gap_profile``: CLAUDE.md's model — ``gap_mbd`` every day until
    replacement crude lands after ``transit_days``, zero after.

    With ``gap_profile`` (length ``duration_days``, mb/d per day): the gap
    actually faced each day, e.g. stepping down as cargoes land and leaving a
    residual. ``gap_mbd`` is then only used for ``days_of_cover`` and
    ``transit_days`` is ignored.

    Always returns a full-length numeric schedule — never ``None`` entries.

    Returns (the four CLAUDE.md keys first, then additions)::

        {"daily_schedule", "total_released_mb", "insufficient", "gap_covered_pct",
         "daily_gap_mbd", "unmet_mbd", "total_unmet_mb", "required_mb",
         "available_mb", "bridge_days", "uncovered_tail_days",
         "gap_at_horizon_end_mbd", "reserve_exhausted_day", "days_of_cover",
         "status"}
    """
    if gap_mbd < 0:
        raise ValueError(f"gap_mbd must be >= 0, got {gap_mbd}")
    if not (0 < duration_days <= MAX_DURATION_DAYS):
        raise ValueError(f"duration_days must be in (0, {MAX_DURATION_DAYS}], got {duration_days}")
    if transit_days < 0:
        raise ValueError(f"transit_days must be >= 0, got {transit_days}")
    if not (0 <= safety_pct < 1):
        raise ValueError(f"safety_pct must be in [0, 1), got {safety_pct}")
    if max_daily_mbd <= 0:
        raise ValueError(f"max_daily_mbd must be > 0, got {max_daily_mbd}")

    D = int(duration_days)
    transit_days = int(transit_days)
    if gap_profile is None:
        profile = [float(gap_mbd) if t < transit_days else 0.0 for t in range(D)]
    else:
        profile = [float(g) for g in gap_profile]
        if len(profile) != D:
            raise ValueError(f"gap_profile has {len(profile)} days, duration_days is {D}")
        if any(g < 0 for g in profile):
            raise ValueError("gap_profile entries must be >= 0")

    available_mb = spr_total_mb * (1 - safety_pct)
    common = dict(
        gap_mbd=gap_mbd, duration_days=D, transit_days=transit_days,
        available_mb=available_mb, max_daily_mbd=max_daily_mbd,
        profiled=gap_profile is not None,
    )

    if all(g <= EPS for g in profile):
        return _result([0.0] * D, [0.0] * D, profile, status="Optimal", **common)

    # variable layout: [release_0..release_{D-1}, unmet_0..unmet_{D-1}, peak]
    n = 2 * D + 1
    peak = 2 * D

    c = np.zeros(n)
    for t in range(D):
        c[D + t] = UNMET_PENALTY + EARLY_TIE_BREAK * (D - t) / D  # earlier shortfall costs more
    c[peak] = 1.0

    # release[t] + unmet[t] == gap[t]
    A_eq = np.zeros((D, n))
    for t in range(D):
        A_eq[t, t] = 1.0
        A_eq[t, D + t] = 1.0
    b_eq = np.array(profile)

    # row 0: sum(release) <= available ; rows 1..D: unmet[t] - peak <= 0
    A_ub = np.zeros((1 + D, n))
    A_ub[0, :D] = 1.0
    for t in range(D):
        A_ub[1 + t, D + t] = 1.0
        A_ub[1 + t, peak] = -1.0
    b_ub = np.zeros(1 + D)
    b_ub[0] = available_mb

    bounds = [(0.0, max_daily_mbd)] * D + [(0.0, None)] * (D + 1)

    try:
        res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    except Exception:
        logger.exception("SPR linear program raised")
        return _result([0.0] * D, list(profile), profile, status="Error", **common)
    if res.status != 0:
        # Should be unreachable — the slack makes every instance feasible — so
        # reaching here means the formulation is wrong. Log loudly, never crash.
        logger.error("SPR linear program did not solve: %s", res.message)
        return _result([0.0] * D, list(profile), profile, status=f"Not solved: {res.message}", **common)

    x = np.clip(res.x, 0.0, None)  # HiGHS can return -1e-12 dust
    return _result(
        [float(v) for v in x[:D]], [float(v) for v in x[D:2 * D]], profile,
        status="Optimal", **common,
    )
