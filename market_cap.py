# market_cap.py — Fetch and cache market cap data for stock tickers
# Uses API Ninjas Market Cap API as data source, with database caching
# so we don't hit the API on every page load.
#
# Two public entry points:
#   get_market_cap_map(tickers)   — fast read-only cache lookup. Spawns a
#                                   background thread to refresh stale tickers
#                                   so they're ready for the next page load.
#                                   Never blocks the caller on the network.
#   refresh_market_caps_sync(...) — blocking fetch + cache write. Use only
#                                   when you genuinely need fresh data right
#                                   now (e.g. backfill jobs, LLM context).

import threading
import time
import requests
from config import API_NINJAS_KEY
from database import get_cached_market_caps, upsert_market_caps

# How many tickers to fetch per batch (avoids rate limiting)
CHUNK_SIZE = 5
CHUNK_DELAY = 1.0  # seconds between chunks

# How long a cached market cap is considered fresh before we trigger a refresh
FRESH_TTL_HOURS = 24 * 7  # 7 days

# API Ninjas endpoint for market cap data
API_URL = "https://api.api-ninjas.com/v1/marketcap"

# Tracks which tickers are currently being refreshed by a background thread,
# so we don't spawn duplicate fetches if the dashboard is refreshed quickly.
_in_flight_lock = threading.Lock()
_in_flight = set()


def fetch_from_api_ninjas(tickers):
    """Batch-fetch market caps from API Ninjas in small chunks.
    Returns dict like {'AAPL': 3500000000000, 'MSFT': 2800000000000}.
    Only includes tickers where we got a definitive answer —
    failed lookups are omitted so they can be retried next time."""
    if not tickers:
        return {}

    result = {}

    # Process in small chunks to avoid rate limits
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]

        if i > 0:
            time.sleep(CHUNK_DELAY)

        for ticker in chunk:
            try:
                response = requests.get(
                    API_URL,
                    params={"ticker": ticker},
                    headers={"X-Api-Key": API_NINJAS_KEY},
                    timeout=10,
                )
                if response.status_code == 200:
                    data = response.json()
                    # API returns a list with one item, or a dict directly
                    if isinstance(data, list) and len(data) > 0:
                        data = data[0]
                    cap = data.get("market_cap")
                    # Treat zero or negative as "no data" — cache None
                    result[ticker] = cap if cap and cap > 0 else None
                else:
                    print(f"[MARKET CAP] API returned {response.status_code} for {ticker}")
            except Exception as e:
                # Individual ticker failed — skip it (don't cache),
                # so it gets retried on the next page load
                print(f"[MARKET CAP] Could not fetch {ticker}: {e}")

    return result


def _refresh_worker(tickers):
    """Background-thread worker that fetches from API and updates the DB cache.
    Always clears the in-flight set so failures don't permanently block retries."""
    try:
        fresh = fetch_from_api_ninjas(tickers)
        upsert_market_caps(fresh)
    except Exception as e:
        print(f"[MARKET CAP] Background refresh failed: {e}")
    finally:
        with _in_flight_lock:
            _in_flight.difference_update(tickers)


def _refresh_in_background(tickers):
    """Spawn a daemon thread to refresh ticker data without blocking the caller.
    Skips tickers that another thread is already refreshing."""
    with _in_flight_lock:
        # Filter to tickers not already being refreshed
        new_tickers = [t for t in tickers if t not in _in_flight]
        if not new_tickers:
            return
        _in_flight.update(new_tickers)

    # daemon=True so the thread doesn't block app shutdown on Render
    thread = threading.Thread(target=_refresh_worker, args=(new_tickers,), daemon=True)
    thread.start()


def refresh_market_caps_sync(tickers):
    """Blocking fetch — populates the cache for any missing/stale tickers.
    Use when the caller genuinely needs fresh data before continuing
    (backfill jobs, scheduler, LLM context-gathering).
    Returns the full {ticker: cap} dict including pre-existing fresh entries."""
    if not tickers:
        return {}
    tickers = [t.strip().upper() for t in tickers if t]
    cached = get_cached_market_caps(tickers, max_age_hours=FRESH_TTL_HOURS)
    stale = [t for t in tickers if t not in cached]
    if stale:
        fresh = fetch_from_api_ninjas(stale)
        upsert_market_caps(fresh)
        cached.update(fresh)
    return cached


def get_market_cap_map(tickers):
    """Fast read-only lookup — returns whatever's cached for the given tickers,
    regardless of age, with no network calls. If any ticker is missing or
    stale (>FRESH_TTL_HOURS old), spawns a background thread to refresh it,
    so the next page load will have current data.

    Returns dict {ticker: market_cap_int_or_None}. Tickers with no cached
    row at all are simply absent from the result (template guards on
    `market_caps.get(...)` will skip them).
    """
    if not tickers:
        return {}

    # Normalize tickers to uppercase
    tickers = [t.strip().upper() for t in tickers if t]

    # Return everything we have, regardless of age — instant DB read
    cached_any_age = get_cached_market_caps(tickers, max_age_hours=None)

    # Separately, figure out which tickers need a background refresh
    cached_fresh = get_cached_market_caps(tickers, max_age_hours=FRESH_TTL_HOURS)
    needs_refresh = [t for t in tickers if t not in cached_fresh]
    if needs_refresh:
        _refresh_in_background(needs_refresh)

    return cached_any_age
