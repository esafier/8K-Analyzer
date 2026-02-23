# cik_lookup.py — Look up ticker symbols by CIK using SEC's company_tickers.json
# Downloads the SEC's master list of CIK-to-ticker mappings and caches it locally.
# Used as a fallback when EDGAR search results don't include a ticker symbol.

import os
import json
import time
import requests
from config import USER_AGENT

# Where to save the cached copy of the SEC's company_tickers.json
CACHE_FILE = os.path.join(os.path.dirname(__file__), "cik_tickers_cache.json")

# How often to re-download (7 days in seconds — the mapping rarely changes)
CACHE_MAX_AGE = 7 * 24 * 60 * 60

# SEC publishes this free file mapping every public company's CIK to its ticker
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Module-level cache so we only build the lookup dict once per process
_cik_to_ticker = None


def _download_sec_tickers():
    """Download company_tickers.json from SEC and save it locally."""
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(SEC_TICKERS_URL, headers=headers, timeout=30)
    response.raise_for_status()

    # Save to disk so we don't re-download for 7 days
    with open(CACHE_FILE, "w") as f:
        f.write(response.text)

    return response.json()


def _load_tickers_data():
    """Load company_tickers.json — from cache file if fresh, otherwise download."""
    # Check if we already have a recent copy on disk
    if os.path.exists(CACHE_FILE):
        file_age = time.time() - os.path.getmtime(CACHE_FILE)
        if file_age < CACHE_MAX_AGE:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)

    # Cache is missing or stale — download a fresh copy
    print("[CIK LOOKUP] Downloading company_tickers.json from SEC...")
    return _download_sec_tickers()


def _build_cik_map():
    """Build a dictionary mapping zero-padded CIK strings to ticker symbols.

    SEC's file format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
    EDGAR search API uses 10-digit zero-padded CIK strings like "0000320193".
    We convert each integer CIK to match that format so lookups are simple.
    """
    global _cik_to_ticker

    data = _load_tickers_data()

    # Build the lookup: "0000320193" -> "AAPL"
    # Some companies have multiple tickers (common stock, preferred, warrants).
    # We prefer common stock — usually the shortest ticker without hyphens.
    _cik_to_ticker = {}
    for entry in data.values():
        cik_padded = str(entry["cik_str"]).zfill(10)
        ticker = entry["ticker"]
        existing = _cik_to_ticker.get(cik_padded)

        # Keep this ticker if it's the first we've seen for this CIK,
        # or if the existing one looks like preferred/warrant (has hyphen or W suffix)
        # and this one looks like common stock
        is_common = "-" not in ticker and not ticker.endswith("W")
        existing_is_common = existing and "-" not in existing and not existing.endswith("W")

        if existing is None or (not existing_is_common and is_common):
            _cik_to_ticker[cik_padded] = ticker

    print(f"[CIK LOOKUP] Loaded {len(_cik_to_ticker)} CIK-to-ticker mappings")


def get_ticker_by_cik(cik):
    """Look up a ticker symbol for a given CIK number.

    Args:
        cik: CIK as a string, with or without leading zeros
             (e.g., "0001855644" or "1855644" both work)

    Returns:
        Ticker string like "AAPL", or "" if not found
    """
    global _cik_to_ticker

    # Build the lookup dict on first use (lazy loading)
    if _cik_to_ticker is None:
        try:
            _build_cik_map()
        except Exception as e:
            print(f"[CIK LOOKUP] Failed to load CIK data: {e}")
            _cik_to_ticker = {}  # Empty dict so we don't retry every call
            return ""

    # Normalize CIK to 10-digit zero-padded string to match our lookup keys
    cik_padded = str(cik).zfill(10)

    return _cik_to_ticker.get(cik_padded, "")
