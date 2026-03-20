# earnings.py — Fetch and cache next earnings date for stock tickers
# Uses API Ninjas Earnings Calendar API as data source, with database caching
# so we don't hit the API on every page load.

import time
import requests
from datetime import datetime
from config import API_NINJAS_KEY
from database import get_cached_earnings, upsert_earnings

# How many tickers to fetch per batch (avoids rate limiting)
CHUNK_SIZE = 5
CHUNK_DELAY = 1.0  # seconds between chunks

# API Ninjas endpoint for earnings calendar
API_URL = "https://api.api-ninjas.com/v1/earningscalendar"


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


def get_earnings_map(tickers):
    """Main entry point — returns {ticker: {'date': '...', 'timing': '...'} or None}.

    1. Checks the database cache for fresh values (< 12 hours old)
    2. Fetches any missing/stale tickers from API Ninjas
    3. Saves fresh values back to the database
    4. Returns the complete map for template rendering
    """
    if not tickers:
        return {}

    # Normalize tickers to uppercase
    tickers = [t.strip().upper() for t in tickers if t]

    # Step 1: Check what we already have cached
    cached = get_cached_earnings(tickers)

    # Step 2: Figure out which tickers need fetching
    stale_tickers = [t for t in tickers if t not in cached]

    # Step 3: Fetch missing ones from API Ninjas
    if stale_tickers:
        fresh = fetch_from_api_ninjas(stale_tickers)
        # Step 4: Save to database so next page load is instant
        upsert_earnings(fresh)
        cached.update(fresh)

    return cached
