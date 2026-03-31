# stock_price.py — Fetch and cache current stock price for a ticker
# Uses API Ninjas Stock Price API as data source, with database caching
# so we don't hit the API redundantly. Used by signal analysis to
# evaluate price hurdles and detect spring-loading patterns.

import requests
from config import API_NINJAS_KEY
from database import get_cached_stock_price, upsert_stock_price

# API Ninjas endpoint for stock price data
API_URL = "https://api.api-ninjas.com/v1/stockprice"


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
    """Main entry point — returns the current stock price for a ticker.

    1. Checks database cache for a fresh value (< 1 hour old)
    2. If stale or missing, fetches from API Ninjas
    3. Caches the result for next time
    4. Returns the price as a float, or None if unavailable
    """
    if not ticker:
        return None

    ticker = ticker.strip().upper()

    # Step 1: Check the cache
    cached = get_cached_stock_price(ticker)
    if cached is not None:
        return cached

    # Step 2: Fetch from API Ninjas
    price = fetch_from_api_ninjas(ticker)

    # Step 3: Cache it (even None, so we don't retry immediately)
    upsert_stock_price(ticker, price)

    return price
