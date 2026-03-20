# market_cap.py — Fetch and cache market cap data for stock tickers
# Uses API Ninjas Market Cap API as data source, with database caching
# so we don't hit the API on every page load.

import time
import requests
from config import API_NINJAS_KEY
from database import get_cached_market_caps, upsert_market_caps

# How many tickers to fetch per batch (avoids rate limiting)
CHUNK_SIZE = 5
CHUNK_DELAY = 1.0  # seconds between chunks

# API Ninjas endpoint for market cap data
API_URL = "https://api.api-ninjas.com/v1/marketcap"


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


def get_market_cap_map(tickers):
    """Main entry point — returns {ticker: market_cap_int_or_None} for all tickers.

    1. Checks the database cache for fresh values (< 24 hours old)
    2. Fetches any missing/stale tickers from API Ninjas
    3. Saves fresh values back to the database
    4. Returns the complete map for template rendering
    """
    if not tickers:
        return {}

    # Normalize tickers to uppercase
    tickers = [t.strip().upper() for t in tickers if t]

    # Step 1: Check what we already have cached
    cached = get_cached_market_caps(tickers)

    # Step 2: Figure out which tickers need fetching
    # (anything not in the cache is either missing or stale)
    stale_tickers = [t for t in tickers if t not in cached]

    # Step 3: Fetch missing ones from API Ninjas
    if stale_tickers:
        fresh = fetch_from_api_ninjas(stale_tickers)
        # Step 4: Save to database so next page load is instant
        upsert_market_caps(fresh)
        cached.update(fresh)

    return cached
