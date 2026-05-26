"""Tests for the stock_price module.

Two surfaces are tested:
- refresh_stock_prices_sync: blocking batch fetch (used by backfill / scheduler)
- get_stock_price_map: fast read-only cache lookup (used by the dashboard)
"""
from unittest.mock import patch


# ---------- refresh_stock_prices_sync (the sync fetch path) ----------

def test_sync_fetch_returns_dict_mapping_ticker_to_price(tmp_sqlite_db):
    """refresh_stock_prices_sync should return {TICKER: price} for each input."""
    from stock_price import refresh_stock_prices_sync

    with patch("stock_price.fetch_from_api_ninjas") as mock_fetch:
        mock_fetch.side_effect = lambda t: {"AAPL": 200.50, "MSFT": 410.10}.get(t)
        result = refresh_stock_prices_sync(["AAPL", "MSFT"])

    assert result == {"AAPL": 200.50, "MSFT": 410.10}


def test_sync_fetch_omits_tickers_with_no_price(tmp_sqlite_db):
    """If a ticker fetch returns None, it should be omitted from the result map."""
    from stock_price import refresh_stock_prices_sync

    with patch("stock_price.fetch_from_api_ninjas") as mock_fetch:
        mock_fetch.side_effect = lambda t: 99.0 if t == "AAPL" else None
        result = refresh_stock_prices_sync(["AAPL", "BADX"])

    assert result == {"AAPL": 99.0}
    assert "BADX" not in result


def test_sync_fetch_individual_failure_does_not_break_batch(tmp_sqlite_db):
    """If one ticker raises during fetch, the others still come through."""
    from stock_price import refresh_stock_prices_sync

    def flaky(t):
        if t == "BOOM":
            raise RuntimeError("simulated network fail")
        return 50.0

    with patch("stock_price.fetch_from_api_ninjas", side_effect=flaky):
        result = refresh_stock_prices_sync(["AAPL", "BOOM", "GOOG"])

    assert result == {"AAPL": 50.0, "GOOG": 50.0}


# ---------- get_stock_price_map (the fast read-only dashboard path) ----------

def test_read_only_returns_cached_data_without_fetching(tmp_sqlite_db):
    """get_stock_price_map returns whatever's cached without calling the API."""
    from stock_price import get_stock_price_map
    from database import upsert_stock_price

    # Pre-populate the cache
    upsert_stock_price("AAPL", 175.0)
    upsert_stock_price("MSFT", 410.0)

    # Even with a broken API, cached values come back
    with patch("stock_price.fetch_from_api_ninjas") as mock_fetch:
        mock_fetch.side_effect = RuntimeError("API should not be called")
        result = get_stock_price_map(["AAPL", "MSFT"])

    assert result == {"AAPL": 175.0, "MSFT": 410.0}


def test_empty_input_returns_empty_dict(tmp_sqlite_db):
    from stock_price import get_stock_price_map, refresh_stock_prices_sync
    assert get_stock_price_map([]) == {}
    assert get_stock_price_map(None) == {}
    assert refresh_stock_prices_sync([]) == {}
    assert refresh_stock_prices_sync(None) == {}
