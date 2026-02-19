# market_cap.py — Fetch and cache market cap data for stock tickers
# Uses Yahoo Finance (via yfinance) as data source, with database caching
# so we don't hit the API on every page load.

import yfinance as yf
from database import get_cached_market_caps, upsert_market_caps


def fetch_from_yfinance(tickers):
    """Batch-fetch market caps from Yahoo Finance.
    Returns dict like {'AAPL': 3500000000000, 'MSFT': 2800000000000}.
    Tickers with no data get None (so we cache "no data" and don't refetch)."""
    if not tickers:
        return {}

    result = {}
    try:
        # yfinance handles multiple tickers in one call
        data = yf.Tickers(" ".join(tickers))
        for ticker in tickers:
            try:
                info = data.tickers[ticker].info
                cap = info.get("marketCap")
                # Treat zero or negative as "no data"
                result[ticker] = cap if cap and cap > 0 else None
            except Exception:
                result[ticker] = None
    except Exception as e:
        print(f"[MARKET CAP] yfinance batch call failed: {e}")
        # Return None for all tickers so we don't retry immediately
        for ticker in tickers:
            result[ticker] = None

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
