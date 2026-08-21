# price_history.py — Daily closing prices for outcome scoring.
#
# This is the ONLY module that knows where historical prices come from.
# Everything downstream (outcome baselines, horizon marks, the scorecard)
# depends solely on get_daily_closes() / get_close_on_or_after(), so the
# source can be replaced without touching any scoring logic.
#
# Source: Yahoo Finance's chart endpoint. It needs no API key and covers the
# micro-caps this scanner surfaces, which is why it was chosen over the paid
# API Ninjas endpoint (current price only) and over Stooq (now gated behind a
# JavaScript proof-of-work challenge).
#
# It is also UNOFFICIAL and carries no stability guarantee. When it breaks,
# replace fetch_from_yahoo() and keep the two public functions' signatures —
# that is the entire migration.
#
# Everything is cached in the price_history table: a full backfill covers
# thousands of filings, and each ticker's series should cost one request.

import time
import requests
from datetime import datetime, timedelta

from database import (
    get_cached_closes,
    get_price_history_meta,
    upsert_closes,
    upsert_price_history_meta,
)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# Yahoo rejects the python-requests default agent often enough to matter.
USER_AGENT = "Mozilla/5.0 (compatible; 8K-Analyzer/1.0)"

# Seconds between network calls. This is someone else's free endpoint and a
# backfill walks thousands of tickers — go slowly enough to stay welcome.
REQUEST_DELAY = 0.4

# Days of padding added to each fetched span. Fetching a slightly wider window
# than asked costs nothing extra (one request either way) and means the next
# horizon mark for the same filing usually hits cache instead of the network.
SPAN_PAD_DAYS = 10

# A daily bar for the session in progress is not final — its "close" is just the
# last trade so far. Anything stored from it would be frozen permanently, since
# a horizon mark is never revisited. So only bars strictly before the current UTC
# date are ever cached or returned. Worst case that costs a day of latency on a
# horizon; the alternative is a wrong number that never gets corrected.
#
# UTC is deliberately conservative: the US session for date D closes at 20:00-21:00
# UTC on D, so waiting for D+1 UTC can never accept a partial bar.

# Sentinel statuses stored in price_history_meta.status
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"


def latest_complete_date():
    """The most recent date whose daily bar can be considered final."""
    return datetime.utcnow().date() - timedelta(days=1)


def _to_date(value):
    """Accept 'YYYY-MM-DD' or a date/datetime; return a date. None passes through."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "year") and not isinstance(value, str):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _iso(value):
    """Normalize any accepted date form to an ISO 'YYYY-MM-DD' string."""
    d = _to_date(value)
    return d.isoformat() if d else None


def fetch_from_yahoo(ticker, start_date, end_date):
    """Fetch daily closes for [start_date, end_date] from Yahoo.

    Returns:
        dict {('YYYY-MM-DD'): close}  on success (may be empty for a quiet span)
        STATUS_NOT_FOUND              if the ticker does not resolve (delisted,
                                      renamed, or never real) — a durable answer
                                      worth caching so it is not retried forever
        None                          on a transient failure (network, 5xx, bad
                                      payload) — caller should not cache this
    """
    if not ticker:
        return None

    start = _to_date(start_date)
    end = _to_date(end_date)
    if not start or not end or start > end:
        return None

    # Yahoo's period bounds are exclusive-ish at the edges; pad by a day on each
    # side so a bar falling exactly on a boundary is not silently dropped.
    period1 = int(datetime(start.year, start.month, start.day).timestamp()) - 86400
    period2 = int(datetime(end.year, end.month, end.day).timestamp()) + 86400

    try:
        response = requests.get(
            CHART_URL.format(ticker=ticker.upper()),
            params={"period1": period1, "period2": period2, "interval": "1d"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
    except Exception as e:
        print(f"[PRICE HISTORY] Request failed for {ticker}: {e}")
        return None

    if response.status_code == 404:
        # Yahoo is explicit here: the symbol does not exist. For this project
        # that usually means the company was delisted or renamed after filing.
        print(f"[PRICE HISTORY] {ticker}: not found (delisted or renamed)")
        return STATUS_NOT_FOUND

    if response.status_code != 200:
        print(f"[PRICE HISTORY] {ticker}: HTTP {response.status_code}")
        return None

    try:
        payload = response.json()
    except Exception as e:
        print(f"[PRICE HISTORY] {ticker}: unreadable response ({e})")
        return None

    chart = payload.get("chart") or {}
    results = chart.get("result")
    if not results:
        # An error block with no result is Yahoo's other way of saying
        # "no such symbol"; treat it the same as a 404 rather than retrying.
        if chart.get("error"):
            return STATUS_NOT_FOUND
        return None

    result = results[0]
    timestamps = result.get("timestamp") or []
    try:
        closes = result["indicators"]["quote"][0].get("close") or []
    except (KeyError, IndexError, TypeError):
        return None

    cutoff = latest_complete_date().isoformat()
    series = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue  # halted or untraded day — no usable close
        bar_date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        if bar_date > cutoff:
            continue  # session still in progress — not a real close yet
        series[bar_date] = float(close)

    return series


def get_daily_closes(ticker, start_date, end_date):
    """Return {('YYYY-MM-DD'): close} for a ticker over [start_date, end_date].

    Cache-first: hits the network only when the requested range is not already
    covered by a previous fetch. This is the single entry point the rest of the
    app should use.

    Returns an empty dict when the ticker cannot be priced — callers must treat
    "no data" as a skip, never as a zero.
    """
    if not ticker:
        return {}

    ticker = ticker.strip().upper()
    start_iso = _iso(start_date)
    end_iso = _iso(end_date)
    if not start_iso or not end_iso or start_iso > end_iso:
        return {}

    meta = get_price_history_meta(ticker)

    # A ticker Yahoo has already denied stays denied — don't spend a request
    # per filing rediscovering that a 2019 shell company no longer trades.
    # Still serve whatever was cached before it went dark: a company delisted
    # last month traded perfectly normally the month before, and those bars are
    # exactly what scoring its filings needs.
    if meta and meta.get("status") == STATUS_NOT_FOUND:
        return get_cached_closes(ticker, start_iso, end_iso)

    # Only complete sessions can ever be fetched, so coverage is judged against
    # the clamped end — otherwise a request reaching into today would look
    # permanently uncovered and refetch on every single call.
    complete_through = latest_complete_date().isoformat()
    effective_end = min(end_iso, complete_through)

    covered = (
        meta
        and meta.get("span_start")
        and meta.get("span_end")
        and meta["span_start"] <= start_iso
        and meta["span_end"] >= effective_end
    )
    if covered or start_iso > complete_through:
        return get_cached_closes(ticker, start_iso, end_iso)

    # Widen the fetch to cover both the request and anything already recorded,
    # so one call replaces the cached span rather than fragmenting it.
    fetch_start = _to_date(start_iso) - timedelta(days=SPAN_PAD_DAYS)
    fetch_end = _to_date(effective_end) + timedelta(days=SPAN_PAD_DAYS)
    if meta:
        if meta.get("span_start"):
            fetch_start = min(fetch_start, _to_date(meta["span_start"]))
        if meta.get("span_end"):
            fetch_end = max(fetch_end, _to_date(meta["span_end"]))

    # Never ask for bars that do not exist yet, or for the session in progress.
    if fetch_end > latest_complete_date():
        fetch_end = latest_complete_date()
    if fetch_start > fetch_end:
        return get_cached_closes(ticker, start_iso, end_iso)

    time.sleep(REQUEST_DELAY)
    fetched = fetch_from_yahoo(ticker, fetch_start, fetch_end)

    if isinstance(fetched, str) and fetched == STATUS_NOT_FOUND:
        upsert_price_history_meta(ticker, None, None, status=STATUS_NOT_FOUND)
        # Serve the cache on THIS call, not just on later ones. A widened fetch
        # can 404 while the requested window is already cached and perfectly
        # good — returning nothing here would let the caller see "no price"
        # alongside a freshly-written not_found status and write the filing off
        # as delisted, after which the row is no longer eligible for a retry.
        return get_cached_closes(ticker, start_iso, end_iso)

    if fetched is None:
        # Transient failure. Do NOT record coverage — a retry must be able to
        # fill this span later. Serve whatever was already cached.
        return get_cached_closes(ticker, start_iso, end_iso)

    if fetched:
        upsert_closes(ticker, fetched)
    upsert_price_history_meta(
        ticker, fetch_start.isoformat(), fetch_end.isoformat(), status=STATUS_OK
    )

    return get_cached_closes(ticker, start_iso, end_iso)


def has_coverage(ticker, start_date, end_date):
    """True if this span was actually fetched successfully for this ticker.

    This is the difference between "we looked and the market had no bars" and
    "we never got an answer" — and callers must not conflate them. A network
    outage makes get_daily_closes return {} for every ticker; without this
    check, a caller would read that as a permanent verdict and write thousands
    of rows off as unpriceable.
    """
    if not ticker:
        return False
    meta = get_price_history_meta(ticker)
    if not meta or meta.get("status") != STATUS_OK:
        return False
    span_start = meta.get("span_start")
    span_end = meta.get("span_end")
    if not span_start or not span_end:
        return False
    return span_start <= _iso(start_date) and span_end >= _iso(end_date)


def get_close_on_or_after(ticker, target_date, max_lookahead_days=10):
    """Return (bar_date, close) for the first trading day on or after target_date.

    Filing dates land on weekends and holidays, and so do the +7/+30/+90 day
    horizons computed from them. Scoring wants the next real close, not a gap.

    Returns (None, None) if no close is available within max_lookahead_days —
    which is the honest answer for a delisted name or a span Yahoo has no data
    for, and must never be smoothed into a price.
    """
    target = _to_date(target_date)
    if not ticker or not target:
        return (None, None)

    window_end = target + timedelta(days=max_lookahead_days)
    closes = get_daily_closes(ticker, target, window_end)
    if not closes:
        return (None, None)

    target_iso = target.isoformat()
    for bar_date in sorted(closes):
        if bar_date >= target_iso:
            return (bar_date, closes[bar_date])

    return (None, None)
