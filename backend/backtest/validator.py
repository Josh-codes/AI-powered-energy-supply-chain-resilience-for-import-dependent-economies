"""Compare a backtest risk series against the Brent price series.

The validation criterion (CLAUDE.md, Backtest Architecture): the corridor's
risk signal must rise above 0.6 within 48 hours of, or before, the observed
price move. Everything here is pure — no database, no network — so the verdict
logic is tested in isolation from the data that feeds it.

Three choices that make the verdict honest rather than flattering:

1. **The price event is found from prices alone** (:func:`find_price_event`):
   the first session in the charted window that rises by at least
   ``SPIKE_SESSION_PCT``. It never looks at the risk series, so the lead-time
   measurement cannot be circular.

2. **A crossing must be a crossing.** If the signal is already above the
   threshold on the first charted day, the verdict is INCONCLUSIVE: the system
   may have "seen it coming", but the window cannot show the rise, and the lead
   time is only a lower bound.

3. **An early blip is not a lead.** The signal is split into *excursions*
   (maximal runs above the threshold). Only the excursion still running on the
   price-event day — or starting within the tolerance after it — counts.
   Excursions that ended before the price moved are reported as false alarms,
   not credited as a 17-day lead.
"""
from datetime import timedelta

from backtest.eia import daily_returns

#: CLAUDE.md: "risk signal must elevate to > 0.6".
SIGNAL_THRESHOLD = 0.6

#: CLAUDE.md: "within 48 hours of OR before observed price move".
TOLERANCE_DAYS = 2

#: A price event is the first session in the window that rises at least this
#: much. A POLICY choice with a stated meaning, not a fitted constant: measured
#: on the cached EIA series, Brent's 2025 daily returns had a standard
#: deviation of 1.92%, so 5% is 2.6 sigma, and only 2 sessions in all of 2025
#: cleared it. Over the 2026 Hormuz window it selects 2026-03-02 (+8.30%),
#: over the 2025 window 2025-06-13 (+7.28%).
SPIKE_SESSION_PCT = 5.0

PASSED = "PASSED"
FAILED = "FAILED"
INCONCLUSIVE = "INCONCLUSIVE"
NO_PRICE_EVENT = "NO_PRICE_EVENT"


def find_price_event(prices, start, end, min_session_pct=SPIKE_SESSION_PCT):
    """First session in ``[start, end]`` that rose at least ``min_session_pct``.

    ``prices`` must extend before ``start`` if a move on ``start`` itself is
    to be measurable — its return needs the previous session. Returns the
    :func:`backtest.eia.daily_returns` row, or None.
    """
    for move in daily_returns(prices):
        if start <= move["date"] <= end and move["pct_change"] >= min_session_pct:
            return move
    return None


def excursions(series, corridor, threshold=SIGNAL_THRESHOLD):
    """Maximal runs of consecutive days on which ``corridor`` scored above
    ``threshold``. ``series`` is a list of ``{"date", "scores": {...}}`` in
    date order. Returns ``[{"start", "end", "days", "peak", "peak_at"}]``.
    """
    runs, current = [], None
    for row in series:
        score = row["scores"].get(corridor)
        above = score is not None and score > threshold
        if above:
            if current is None:
                current = {"start": row["date"], "end": row["date"],
                           "peak": score, "peak_at": row["date"]}
            else:
                current["end"] = row["date"]
                if score > current["peak"]:
                    current["peak"], current["peak_at"] = score, row["date"]
        elif current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)
    for run in runs:
        run["days"] = (run["end"] - run["start"]).days + 1
    return runs


def _score_on(series, corridor, day):
    for row in series:
        if row["date"] == day:
            return row["scores"].get(corridor)
    return None


def validate(series, prices, corridor, chart_start, end,
             threshold=SIGNAL_THRESHOLD, tolerance_days=TOLERANCE_DAYS,
             spike_session_pct=SPIKE_SESSION_PCT):
    """Judge one event. Returns a flat dict with CLAUDE.md's documented keys
    (``signal_elevated_at``, ``price_spiked_at``, ``lead_time_days``,
    ``max_risk_score``, ``brent_spike_pct``, ``validation``) plus the evidence
    behind the verdict.

    ``lead_time_days`` is positive when the signal LED the price.
    """
    charted = [r for r in series if chart_start <= r["date"] <= end]
    scored = [(r["date"], r["scores"].get(corridor)) for r in charted
              if r["scores"].get(corridor) is not None]

    result = {
        "corridor": corridor,
        "signal_threshold": threshold,
        "tolerance_days": tolerance_days,
        "spike_session_pct": spike_session_pct,
        "price_spiked_at": None,
        "brent_spike_pct": None,
        "brent_before_usd": None,
        "brent_after_usd": None,
        "signal_elevated_at": None,
        "lead_time_days": None,
        "lead_time_is_lower_bound": False,
        "score_at_chart_start": _score_on(charted, corridor, chart_start),
        "score_at_price_spike": None,
        "max_risk_score": max((s for _, s in scored), default=None),
        "max_risk_at": None,
        "excursions": [],
        "false_alarms": [],
        "validation": None,
        "reason": None,
    }
    if result["max_risk_score"] is not None:
        result["max_risk_at"] = next(
            d for d, s in scored if s == result["max_risk_score"]
        )

    move = find_price_event(prices, chart_start, end, spike_session_pct)
    if move is None:
        result["validation"] = NO_PRICE_EVENT
        result["reason"] = (
            f"no session in {chart_start}..{end} rose {spike_session_pct}% or "
            f"more, so there is no price move to measure a lead against"
        )
        return result

    spike = move["date"]
    result.update({
        "price_spiked_at": spike,
        "brent_spike_pct": move["pct_change"],
        "brent_before_usd": move["prev_usd"],
        "brent_after_usd": move["price_usd"],
        "score_at_price_spike": _score_on(charted, corridor, spike),
    })

    runs = excursions(charted, corridor, threshold)
    result["excursions"] = runs
    latest_start = spike + timedelta(days=tolerance_days)
    relevant = next(
        (r for r in runs if r["end"] >= spike and r["start"] <= latest_start),
        None,
    )
    result["false_alarms"] = [r for r in runs if r["end"] < spike]

    if relevant is None:
        result["validation"] = FAILED
        result["reason"] = (
            f"{corridor} risk was not above {threshold} on {spike} and did not "
            f"cross it within {tolerance_days} days after"
        )
        return result

    lead = (spike - relevant["start"]).days
    result["signal_elevated_at"] = relevant["start"]
    result["lead_time_days"] = lead

    if relevant["start"] == chart_start:
        result["lead_time_is_lower_bound"] = True
        result["validation"] = INCONCLUSIVE
        result["reason"] = (
            f"{corridor} risk was already above {threshold} on the first charted "
            f"day ({chart_start}); the window cannot show it rising, so "
            f"{lead} days is only a lower bound on the lead"
        )
        return result

    result["validation"] = PASSED
    if lead >= 0:
        result["reason"] = (
            f"{corridor} risk crossed {threshold} on {relevant['start']}, "
            f"{lead} day(s) before Brent's {move['pct_change']:+.2f}% session on {spike}"
        )
    else:
        result["reason"] = (
            f"{corridor} risk crossed {threshold} on {relevant['start']}, "
            f"{-lead} day(s) AFTER the price move on {spike} - inside the "
            f"{tolerance_days}-day tolerance, so it passes, but it did not lead"
        )
    return result
