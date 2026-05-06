"""Tests for get_stock_price_map — the dashboard's batch stock-price helper."""
from unittest.mock import patch


def test_returns_dict_mapping_ticker_to_price(tmp_sqlite_db):
    """get_stock_price_map should return {TICKER: price} for each input."""
    from stock_price import get_stock_price_map

    # Mock the per-ticker fetch to avoid hitting the real API
    with patch("stock_price.fetch_from_api_ninjas") as mock_fetch:
        mock_fetch.side_effect = lambda t: {"AAPL": 200.50, "MSFT": 410.10}.get(t)
        result = get_stock_price_map(["AAPL", "MSFT"])

    assert result == {"AAPL": 200.50, "MSFT": 410.10}


def test_omits_tickers_with_no_price(tmp_sqlite_db):
    """If a ticker fetch returns None, it should be omitted from the result map."""
    from stock_price import get_stock_price_map

    with patch("stock_price.fetch_from_api_ninjas") as mock_fetch:
        mock_fetch.side_effect = lambda t: 99.0 if t == "AAPL" else None
        result = get_stock_price_map(["AAPL", "BADX"])

    assert result == {"AAPL": 99.0}
    assert "BADX" not in result


def test_empty_input_returns_empty_dict(tmp_sqlite_db):
    from stock_price import get_stock_price_map
    assert get_stock_price_map([]) == {}
    assert get_stock_price_map(None) == {}


def test_individual_failure_does_not_break_batch(tmp_sqlite_db):
    """If one ticker raises, the others still come through."""
    from stock_price import get_stock_price_map

    def flaky(t):
        if t == "BOOM":
            raise RuntimeError("simulated network fail")
        return 50.0

    with patch("stock_price.fetch_from_api_ninjas", side_effect=flaky):
        result = get_stock_price_map(["AAPL", "BOOM", "GOOG"])

    assert result == {"AAPL": 50.0, "GOOG": 50.0}
