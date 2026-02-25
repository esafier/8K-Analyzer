# market_cap.py — Fetch and cache market cap data for stock tickers
# Uses Yahoo Finance (via yfinance) as data source, with database caching
# so we don't hit the API on every page load.

import time
import yfinance as yf
from database import get_cached_market_caps, upsert_market_caps

# How many tickers to fetch per yfinance call (avoids rate limiting)
CHUNK_SIZE = 5
CHUNK_DELAY = 1.0  # seconds between chunks


def fetch_from_yfinance(tickers):
    """Batch-fetch market caps from Yahoo Finance in small chunks.
    Returns dict like {'AAPL': 3500000000000, 'MSFT': 2800000000000}.
    Only includes tickers where we got a definitive answer from Yahoo —
    failed lookups are omitted so they can be retried next time."""
    if not tickers:
        return {}

    result = {}

    # Process in small chunks to avoid Yahoo rate limits
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]

        if i > 0:
            time.sleep(CHUNK_DELAY)

        try:
            data = yf.Tickers(" ".join(chunk))
            for ticker in chunk:
                try:
                    info = data.tickers[ticker].info
                    cap = info.get("marketCap")
                    # Treat zero or negative as "no data" — cache None
                    # so we don't keep retrying tickers that genuinely have none
                    result[ticker] = cap if cap and cap > 0 else None
                except Exception:
                    # Individual ticker failed — skip it (don't cache),
                    # so it gets retried on the next page load
                    print(f"[MARKET CAP] Could not fetch {ticker}, will retry later")
        except Exception as e:
            # Whole chunk failed (rate limit, network, etc.) — skip all,
            # don't cache anything so they get retried next time
            print(f"[MARKET CAP] Chunk failed ({', '.join(chunk)}): {e}")

    return result


def get_market_cap_map(tickers):
    """Main entry point — returns {ticker: market_cap_int_or_None} for all tickers.

    1. Checks the database cache for fresh values (< 24 hours old)
    2. Fetches any missing/stale tickers from Yahoo Finance
    3. Saves fresh values back to the database
    4. Returns the complete map for template rendering
    """
    if not tickers:
        return {}

    # Normalize tickers to uppercase (yfinance expects uppercase)
    tickers = [t.strip().upper() for t in tickers if t]

    # Step 1: Check what we already have cached
    cached = get_cached_market_caps(tickers)

    # Step 2: Figure out which tickers need fetching
    # (anything not in the cache is either missing or stale)
    stale_tickers = [t for t in tickers if t not in cached]

    # Step 3: Fetch missing ones from Yahoo Finance
    if stale_tickers:
        fresh = fetch_from_yfinance(stale_tickers)
        # Step 4: Save to database so next page load is instant
        upsert_market_caps(fresh)
        cached.update(fresh)

    return cached
