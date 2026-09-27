"""EIA API client for historical Brent crude spot prices.

The backtest validates the risk score against an *independent* measure of oil
supply risk — the price the market actually paid. EIA's Europe Brent Spot Price
FOB (series ``RBRTE``, daily, USD/barrel) is that measure: free, official, and
in no way derived from the news corpus this project scores, so it cannot be
accused of being fitted to.

API v2 shape (v1 is retired)::

    GET https://api.eia.gov/v2/petroleum/pri/spt/data/
        ?api_key=...&frequency=daily&data[0]=value
        &facets[series][]=RBRTE&start=YYYY-MM-DD&end=YYYY-MM-DD

Responses are paginated; ``length`` caps at 5000 rows per request.

Prices are immutable history, so a successful fetch is cached to
``data/brent_cache.json``. Unlike the OFAC cache it is small (~25 KB for two
years) and is meant to be COMMITTED: it is the price series the thesis cites,
and committing it lets anyone reproduce the backtest without an EIA key or a
network connection.
"""
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

#: Europe Brent Spot Price FOB, daily, USD per barrel.
BRENT_SERIES = "RBRTE"

EIA_BASE_URL = "https://api.eia.gov/v2/petroleum/pri/spt/data/"

#: EIA rejects anything above this and silently truncates nothing — it errors.
MAX_PAGE_LENGTH = 5000

BRENT_CACHE_PATH = Path(settings.BASE_DIR) / "data" / "brent_cache.json"

_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 2


class EIAConfigurationError(RuntimeError):
    """Raised when no API key is configured.

    This is the one failure mode that is *not* swallowed: a missing key is an
    operator error that silent-empty behaviour would hide until the backtest
    reported a flat price line and nobody knew why.
    """


def _api_key(explicit=None):
    key = explicit or getattr(settings, "EIA_API_KEY", "")
    if not key:
        raise EIAConfigurationError(
            "EIA_API_KEY is not set. Register free at "
            "https://www.eia.gov/opendata/register.php and add it to .env"
        )
    return key


def _as_date(value):
    """Accept a date, datetime or ISO 'YYYY-MM-DD' string; return a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _get_with_retry(params, timeout):
    """Return the decoded JSON body, or None after exhausting retries."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            response = requests.get(EIA_BASE_URL, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except ValueError as exc:
            logger.warning("EIA returned a non-JSON body: %s", exc)
            return None
        except requests.RequestException as exc:
            if attempt == _RETRY_ATTEMPTS:
                logger.warning(
                    "EIA fetch failed after %d attempts: %s", attempt, exc
                )
                return None
            time.sleep(_RETRY_BASE_SECONDS * attempt)
    return None


def _parse_rows(rows):
    """Turn EIA ``response.data`` entries into sorted {date, price_usd} dicts.

    Rows with a null value are dropped: EIA publishes holidays and non-trading
    days with ``value: null`` rather than omitting them, and a None would
    propagate into every downstream return calculation.
    """
    prices = []
    for row in rows or []:
        period = row.get("period")
        value = row.get("value")
        if not period or value is None:
            continue
        try:
            prices.append(
                {"date": _as_date(period), "price_usd": float(value)}
            )
        except (TypeError, ValueError):
            logger.debug("skipping unparseable EIA row: %r", row)
    prices.sort(key=lambda p: p["date"])
    return prices


def fetch_brent_prices(start, end, api_key=None, timeout=30, use_cache=True):
    """Return daily Brent spot prices over [start, end] inclusive.

    Returns a list of ``{"date": date, "price_usd": float}`` sorted ascending,
    or ``[]`` if the API could not be reached (logged, never raised — Rule 5).
    Raises :class:`EIAConfigurationError` only when no key is configured.
    """
    start, end = _as_date(start), _as_date(end)
    if start > end:
        raise ValueError(f"start {start} is after end {end}")

    if use_cache:
        cached = _read_cache(start, end)
        if cached is not None:
            logger.info(
                "Brent %s..%s served from cache (%d rows)",
                start, end, len(cached),
            )
            return cached

    key = _api_key(api_key)
    collected, offset = [], 0
    while True:
        params = [
            ("api_key", key),
            ("frequency", "daily"),
            ("data[0]", "value"),
            ("facets[series][]", BRENT_SERIES),
            ("start", start.isoformat()),
            ("end", end.isoformat()),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("offset", str(offset)),
            ("length", str(MAX_PAGE_LENGTH)),
        ]
        body = _get_with_retry(params, timeout)
        if body is None:
            # Partial data is worse than none: a truncated series would make
            # the backtest's lead-time measurement quietly wrong.
            return []
        if "error" in body:
            logger.warning("EIA error response: %s", body.get("error"))
            return []

        rows = (body.get("response") or {}).get("data") or []
        collected.extend(rows)
        if len(rows) < MAX_PAGE_LENGTH:
            break
        offset += MAX_PAGE_LENGTH

    prices = _parse_rows(collected)
    logger.info("fetched %d Brent observations %s..%s", len(prices), start, end)
    if prices and use_cache:
        _write_cache(prices, start)
    return prices


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _load_cache():
    """Return (covered_start, covered_end, rows) or None. Never raises."""
    if not BRENT_CACHE_PATH.exists():
        return None
    try:
        with open(BRENT_CACHE_PATH, encoding="utf-8") as fh:
            payload = json.load(fh)
        rows = [
            {"date": _as_date(r["date"]), "price_usd": float(r["price_usd"])}
            for r in payload.get("prices", [])
        ]
        covered = (
            _as_date(payload["covered_start"]),
            _as_date(payload["covered_end"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Brent cache unreadable, refetching: %s", exc)
        return None
    rows.sort(key=lambda p: p["date"])
    return covered[0], covered[1], rows


def _read_cache(start, end):
    """Return cached prices for [start, end], or None if not fully covered.

    Coverage is the span that was *requested and fetched*, not the span of the
    observations: EIA has no row for holidays, weekends or its ~5-day
    publication lag, so the first and last observation never line up with a
    request's edges and inferring coverage from them misses every time.
    """
    loaded = _load_cache()
    if loaded is None:
        return None
    covered_start, covered_end, rows = loaded
    if start < covered_start or end > covered_end:
        return None
    return [r for r in rows if start <= r["date"] <= end]


def _write_cache(prices, requested_start):
    """Merge *prices* into the cache and widen its coverage. Never raises.

    ``covered_end`` is the last *observed* date, not the requested end. Days
    after it may simply not be published yet, and recording them as covered
    would freeze the tail of the series at whatever EIA had out on the day
    of the first fetch.
    """
    new_start, new_end = requested_start, prices[-1]["date"]
    merged = {row["date"].isoformat(): row["price_usd"] for row in prices}

    loaded = _load_cache()
    if loaded is not None:
        old_start, old_end, old_rows = loaded
        one_day = timedelta(days=1)
        # Only contiguous spans may be unioned; merging disjoint ones would
        # claim coverage of the gap between them and serve it as complete.
        if old_start <= new_end + one_day and new_start <= old_end + one_day:
            new_start, new_end = min(old_start, new_start), max(old_end, new_end)
            for row in old_rows:
                merged.setdefault(row["date"].isoformat(), row["price_usd"])

    payload = {
        "series": BRENT_SERIES,
        "description": "Europe Brent Spot Price FOB, daily, USD/barrel",
        "source": "U.S. Energy Information Administration, api.eia.gov v2",
        "cached_at": datetime.now().isoformat(timespec="seconds"),
        "covered_start": new_start.isoformat(),
        "covered_end": new_end.isoformat(),
        "prices": [
            {"date": d, "price_usd": merged[d]} for d in sorted(merged)
        ],
    }
    try:
        BRENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(BRENT_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        logger.warning("could not write Brent cache: %s", exc)


# ---------------------------------------------------------------------------
# Event location helpers
# ---------------------------------------------------------------------------
# The backtest must identify its price events *independently* of the risk
# signal, or the lead-time measurement is circular. These read the price series
# alone and know nothing about corridors, events or scores.

def daily_returns(prices):
    """Session-over-session percentage changes.

    Returns ``{"date", "price_usd", "prev_usd", "pct_change"}`` per row after
    the first. "Session" means consecutive *published* observations, so a
    Monday's change is measured against the previous Friday.
    """
    out = []
    for prev, cur in zip(prices, prices[1:]):
        if prev["price_usd"] == 0:
            continue
        out.append({
            "date": cur["date"],
            "price_usd": cur["price_usd"],
            "prev_usd": prev["price_usd"],
            "pct_change": (cur["price_usd"] - prev["price_usd"])
            / prev["price_usd"] * 100.0,
        })
    return out


def largest_moves(prices, n=10, direction="up"):
    """The *n* biggest single-session moves, largest first.

    This is how the "2025 US-Iran standoff (Brent +8% in a single session)"
    event gets an actual date instead of a remembered one.
    """
    moves = daily_returns(prices)
    if direction == "up":
        moves = [m for m in moves if m["pct_change"] > 0]
        moves.sort(key=lambda m: m["pct_change"], reverse=True)
    elif direction == "down":
        moves = [m for m in moves if m["pct_change"] < 0]
        moves.sort(key=lambda m: m["pct_change"])
    else:
        moves.sort(key=lambda m: abs(m["pct_change"]), reverse=True)
    return moves[:n]


def find_run_up(prices, from_usd, to_usd, tolerance_usd=2.0):
    """Locate a sustained climb from ~*from_usd* to ~*to_usd*.

    Finds the last observation at or below ``from_usd + tolerance`` that is
    followed by a rise to ``to_usd - tolerance`` without falling back below the
    starting band — i.e. the onset of the climb, not an earlier unrelated dip.
    Returns ``{"start", "start_usd", "peak", "peak_usd", "days", "pct_change"}``
    or None. This is how the "$69 -> $114" closure gets its start date.
    """
    low_bar = from_usd + tolerance_usd
    high_bar = to_usd - tolerance_usd
    best = None

    for i, row in enumerate(prices):
        if row["price_usd"] > low_bar:
            continue
        for j in range(i + 1, len(prices)):
            nxt = prices[j]
            if nxt["price_usd"] <= low_bar:
                break          # fell back; this start did not lead to the run
            if nxt["price_usd"] >= high_bar:
                candidate = {
                    "start": row["date"],
                    "start_usd": row["price_usd"],
                    "peak": nxt["date"],
                    "peak_usd": nxt["price_usd"],
                    "days": (nxt["date"] - row["date"]).days,
                    "pct_change": (nxt["price_usd"] - row["price_usd"])
                    / row["price_usd"] * 100.0,
                }
                # Keep the tightest climb, so the start date is the onset
                # rather than the earliest qualifying trough.
                if best is None or candidate["days"] < best["days"]:
                    best = candidate
                break
    return best


def price_on(prices, when):
    """Price on *when*, or the most recent prior observation. None if before."""
    when = _as_date(when)
    latest = None
    for row in prices:
        if row["date"] > when:
            break
        latest = row
    return latest
