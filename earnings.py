# earnings.py — Fetch and cache next earnings date for stock tickers
# Uses API Ninjas Earnings Calendar API as data source, with database caching
# so we don't hit the API on every page load.
#
# Two public entry points (mirror market_cap.py):
#   get_earnings_map(tickers)   — fast read-only cache lookup. Spawns a
#                                 background thread for stale tickers so they
#                                 are ready next time. Never blocks the caller.
#   refresh_earnings_sync(...)  — blocking fetch + cache write. Use only when
#                                 the caller genuinely needs fresh data now.

import threading
import time
import requests
from datetime import datetime
from config import API_NINJAS_KEY
from database import get_cached_earnings, upsert_earnings

# How many tickers to fetch per batch (avoids rate limiting)
CHUNK_SIZE = 5
CHUNK_DELAY = 1.0  # seconds between chunks

# How long a cached earnings date is considered fresh
FRESH_TTL_HOURS = 48

# API Ninjas endpoint for earnings calendar
API_URL = "https://api.api-ninjas.com/v1/earningscalendar"

# Tracks in-flight refreshes so multiple page loads don't spawn duplicate threads
_in_flight_lock = threading.Lock()
_in_flight = set()


def fetch_from_api_ninjas(tickers):
    """Fetch next upcoming earnings date for each ticker from API Ninjas.
    Returns dict like {'AAPL': {'date': '2026-04-25', 'timing': 'after_market'}, ...}.
    Tickers with no upcoming earnings get None so they don't keep getting retried."""
    if not tickers:
        return {}

    result = {}
    today = datetime.now().strftime("%Y-%m-%d")

    # Process in small chunks to avoid rate limits
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]

        if i > 0:
            time.sleep(CHUNK_DELAY)

        for ticker in chunk:
            try:
                response = requests.get(
                    API_URL,
                    params={"ticker": ticker, "show_upcoming": "true"},
                    headers={"X-Api-Key": API_NINJAS_KEY},
                    timeout=10,
                )
                if response.status_code == 200:
                    data = response.json()
                    # API returns a list of earnings entries (past + upcoming)
                    # Find the next future date
                    next_earnings = _find_next_earnings(data, today)
                    if next_earnings:
                        result[ticker] = next_earnings
                    else:
                        # No upcoming earnings found — cache None so we don't retry
                        result[ticker] = None
                else:
                    print(f"[EARNINGS] API returned {response.status_code} for {ticker}")
            except Exception as e:
                # Individual ticker failed — skip (don't cache), retry next time
                print(f"[EARNINGS] Could not fetch {ticker}: {e}")

    return result


def _find_next_earnings(entries, today):
    """Given a list of earnings entries from the API, find the next upcoming one.
    Returns {'date': 'YYYY-MM-DD', 'timing': 'before_market'} or None."""
    if not entries:
        return None

    # Collect all future dates
    future = []
    for entry in entries:
        date_str = entry.get("date", "")
        if date_str and date_str >= today:
            future.append(entry)

    if not future:
        return None

    # Sort by date and take the earliest upcoming one
    future.sort(key=lambda x: x.get("date", ""))
    closest = future[0]

    return {
        "date": closest.get("date", ""),
        "timing": closest.get("earnings_timing", ""),
    }


def _refresh_worker(tickers):
    """Background-thread worker. Fetches from API and updates the DB cache.
    Always clears the in-flight set so failures don't permanently block retries."""
    try:
        fresh = fetch_from_api_ninjas(tickers)
        upsert_earnings(fresh)
    except Exception as e:
        print(f"[EARNINGS] Background refresh failed: {e}")
    finally:
        with _in_flight_lock:
            _in_flight.difference_update(tickers)


def _refresh_in_background(tickers):
    """Spawn a daemon thread to refresh stale tickers without blocking the caller."""
    with _in_flight_lock:
        new_tickers = [t for t in tickers if t not in _in_flight]
        if not new_tickers:
            return
        _in_flight.update(new_tickers)

    thread = threading.Thread(target=_refresh_worker, args=(new_tickers,), daemon=True)
    thread.start()


def refresh_earnings_sync(tickers):
    """Blocking fetch — populates cache for missing/stale tickers.
    Use when the caller needs fresh data immediately.
    Returns the full {ticker: {date, timing} | None} map."""
    if not tickers:
        return {}
    tickers = [t.strip().upper() for t in tickers if t]
    cached = get_cached_earnings(tickers, max_age_hours=FRESH_TTL_HOURS)
    stale = [t for t in tickers if t not in cached]
    if stale:
        fresh = fetch_from_api_ninjas(stale)
        upsert_earnings(fresh)
        cached.update(fresh)
    return cached


def get_earnings_map(tickers):
    """Fast read-only lookup — returns whatever's cached for the given tickers,
    regardless of age, no network calls. Spawns a background refresh thread
    for any missing/stale entries so they'll be current on the next page load.

    Returns dict {ticker: {'date': ..., 'timing': ...} or None}.
    """
    if not tickers:
        return {}

    tickers = [t.strip().upper() for t in tickers if t]

    cached_any_age = get_cached_earnings(tickers, max_age_hours=None)

    # Decide what's stale by checking against the freshness TTL
    cached_fresh = get_cached_earnings(tickers, max_age_hours=FRESH_TTL_HOURS)
    needs_refresh = [t for t in tickers if t not in cached_fresh]
    if needs_refresh:
        _refresh_in_background(needs_refresh)

    return cached_any_age
