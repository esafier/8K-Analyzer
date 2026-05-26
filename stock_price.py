# stock_price.py — Fetch and cache current stock price for a ticker
# Uses API Ninjas Stock Price API as data source, with database caching
# so we don't hit the API redundantly. Used by signal analysis to
# evaluate price hurdles, and by the dashboard to display current prices.
#
# Three public entry points:
#   get_stock_price(ticker)        — sync single-ticker lookup. Used by signal
#                                    analysis where fresh price is important.
#                                    Keeps the original blocking behavior.
#   get_stock_price_map(tickers)   — fast read-only lookup for the dashboard.
#                                    Never blocks; spawns a background refresh
#                                    for stale tickers.
#   refresh_stock_prices_sync(...) — blocking batch fetch + cache write. For
#                                    backfill/scheduler use.

import threading
import requests
from config import API_NINJAS_KEY
from database import (
    get_cached_stock_price,
    get_cached_stock_prices,
    upsert_stock_price,
)

# How long a cached price is considered fresh for the dashboard.
# We accept somewhat stale prices on the dashboard in exchange for instant loads;
# the background refresh keeps them up to date for the next view.
FRESH_TTL_HOURS = 1

# API Ninjas endpoint for stock price data
API_URL = "https://api.api-ninjas.com/v1/stockprice"

# Tracks in-flight refreshes so we don't spawn duplicate threads for the same ticker
_in_flight_lock = threading.Lock()
_in_flight = set()


def fetch_from_api_ninjas(ticker):
    """Fetch current stock price for a single ticker from API Ninjas.
    Returns the price as a float, or None if the lookup failed."""
    if not ticker:
        return None

    try:
        response = requests.get(
            API_URL,
            params={"ticker": ticker.upper()},
            headers={"X-Api-Key": API_NINJAS_KEY},
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            # API returns a dict with 'price' field
            price = data.get("price")
            return price if price and price > 0 else None
        else:
            print(f"[STOCK PRICE] API returned {response.status_code} for {ticker}")
            return None
    except Exception as e:
        print(f"[STOCK PRICE] Could not fetch {ticker}: {e}")
        return None


def get_stock_price(ticker):
    """Sync single-ticker entry point. Returns fresh-or-cached price.

    1. Checks database cache for a fresh value (< FRESH_TTL_HOURS old)
    2. If stale or missing, fetches from API Ninjas
    3. Caches the result for next time
    4. Returns the price as a float, or None if unavailable

    Used by signal analysis where having a current price matters.
    """
    if not ticker:
        return None

    ticker = ticker.strip().upper()

    # Step 1: Check the cache
    cached = get_cached_stock_price(ticker, max_age_hours=FRESH_TTL_HOURS)
    if cached is not None:
        return cached

    # Step 2: Fetch from API Ninjas
    price = fetch_from_api_ninjas(ticker)

    # Step 3: Cache it (even None, so we don't retry immediately)
    upsert_stock_price(ticker, price)

    return price


def _refresh_worker(tickers):
    """Background-thread worker. Fetches prices one by one and updates cache.
    Always clears the in-flight set on exit so failures don't block retries."""
    try:
        for ticker in tickers:
            try:
                price = fetch_from_api_ninjas(ticker)
                upsert_stock_price(ticker, price)
            except Exception as e:
                print(f"[STOCK PRICE] Background refresh failed for {ticker}: {e}")
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


def refresh_stock_prices_sync(tickers):
    """Blocking batch fetch — populates cache for missing/stale tickers.
    Use when the caller needs fresh prices immediately (backfill, scheduler).
    Per-ticker failures are swallowed so one bad ticker doesn't break the batch.
    Returns the full {ticker: price} dict (omitting tickers with no price)."""
    if not tickers:
        return {}
    tickers = [t.strip().upper() for t in tickers if t]
    cached = get_cached_stock_prices(tickers, max_age_hours=FRESH_TTL_HOURS)
    stale = [t for t in tickers if t not in cached]
    for ticker in stale:
        try:
            price = fetch_from_api_ninjas(ticker)
            upsert_stock_price(ticker, price)
            if price is not None:
                cached[ticker] = price
        except Exception as e:
            # Don't let a single ticker break the batch
            print(f"[STOCK PRICE] Sync refresh failed for {ticker}: {e}")
    # Strip None values from any pre-cached entries so callers only see real prices
    return {t: p for t, p in cached.items() if p is not None}


def get_stock_price_map(tickers):
    """Fast read-only lookup for the dashboard. Returns whatever's cached for
    the given tickers regardless of age — no network calls, no sleeps, just
    one database query. Spawns a background thread to refresh stale prices
    so they're current next time.

    Returns dict {ticker: price}. Tickers with no cached row are omitted
    (template guards on `stock_prices.get(...)` will skip them).
    """
    if not tickers:
        return {}

    # Normalize and dedupe
    norm_tickers = list({t.strip().upper() for t in tickers if t})

    # One batched DB query for everything we have cached
    cached_any_age = get_cached_stock_prices(norm_tickers, max_age_hours=None)

    # Identify stale entries and refresh in background
    cached_fresh = get_cached_stock_prices(norm_tickers, max_age_hours=FRESH_TTL_HOURS)
    needs_refresh = [t for t in norm_tickers if t not in cached_fresh]
    if needs_refresh:
        _refresh_in_background(needs_refresh)

    # Strip None values — template only wants real prices to display
    return {t: p for t, p in cached_any_age.items() if p is not None}
