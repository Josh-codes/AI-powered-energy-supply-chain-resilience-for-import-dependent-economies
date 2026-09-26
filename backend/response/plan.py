"""The full response chain for one disruption: supply gap -> ranked
alternatives -> replacement timeline -> SPR drawdown.

One function so ``manage.py run_response``, ``POST /api/simulate/`` and the
orchestrator's response node cannot drift apart. It is exactly the chain
Phase 5 verified, in the same order:

  1. ``estimate_supply_gap`` (corridor) or ``gap_from_scenario`` (named),
     on the risk-weighted baseline;
  2. ``rank_alternatives`` excluding routes through the disrupted corridor;
  3. ``replacement_timeline`` — cargoes land one by one, a residual stays open;
  4. ``compute_spr_schedule`` over that per-day gap profile.
"""
from response.gap import estimate_supply_gap, gap_from_scenario
from response.reroute import rank_alternatives, replacement_timeline
from response.spr import compute_spr_schedule

DEFAULT_DURATION_DAYS = 14


def build_response(
    G, *, corridor=None, degradation_pct=100, scenario=None, crisis="normal",
    duration_days=DEFAULT_DURATION_DAYS, include_sanctioned=False,
):
    """Return ``{"gap", "reroute", "timeline", "spr"}`` for one disruption.

    Pass exactly one of ``corridor`` or ``scenario``. Never mutates ``G``.
    Raises ValueError on bad input (unknown corridor/scenario, bad crisis,
    non-positive duration) — callers map that to their own error channel.
    """
    if (corridor is None) == (scenario is None):
        raise ValueError("pass exactly one of corridor or scenario")
    if duration_days is None or duration_days <= 0:
        raise ValueError(f"duration_days must be > 0, got {duration_days}")

    if scenario is not None:
        gap = gap_from_scenario(scenario, G=G, duration_days=duration_days)
    else:
        gap = estimate_supply_gap(corridor, degradation_pct, G=G, duration_days=duration_days)

    ranked = rank_alternatives(
        gap["corridor"], crisis=crisis, gap_mbd=gap["gap_mbd"],
        include_sanctioned=include_sanctioned,
    )
    timeline = replacement_timeline(ranked, gap["gap_mbd"], duration_days)
    spr = compute_spr_schedule(
        gap["gap_mbd"], duration_days, timeline["transit_days"] or duration_days,
        gap_profile=timeline["daily_gap_mbd"],
    )
    return {"gap": gap, "reroute": ranked, "timeline": timeline, "spr": spr}
