"""Tests for price_history — the daily-close source behind outcome scoring.

All network calls are mocked. The behaviors that matter here are the ones that
protect the scorecard from lying: a transient failure must not be cached as
coverage, a delisted ticker must not be retried forever, and a missing close
must surface as None rather than a made-up price.
"""
from datetime import date
from unittest.mock import patch

import pytest

import price_history
from price_history import STATUS_NOT_FOUND


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Strip the politeness delay so the suite stays fast."""
    monkeypatch.setattr(price_history.time, "sleep", lambda *_a, **_k: None)


SERIES = {
    "2026-01-02": 10.0,
    "2026-01-05": 11.0,
    "2026-01-06": 12.0,
}


# ---------- get_daily_closes: caching ----------

def test_fetched_closes_are_returned_and_cached(tmp_sqlite_db):
    with patch("price_history.fetch_from_yahoo", return_value=SERIES) as mock:
        first = price_history.get_daily_closes("AAPL", "2026-01-02", "2026-01-06")
        assert first == SERIES
        assert mock.call_count == 1

        # Second identical request is covered by the recorded span — no network.
        second = price_history.get_daily_closes("AAPL", "2026-01-02", "2026-01-06")
        assert second == SERIES
        assert mock.call_count == 1


def test_range_slice_only_returns_requested_dates(tmp_sqlite_db):
    with patch("price_history.fetch_from_yahoo", return_value=SERIES):
        result = price_history.get_daily_closes("AAPL", "2026-01-05", "2026-01-05")
    assert result == {"2026-01-05": 11.0}


def test_transient_failure_is_not_cached_as_coverage(tmp_sqlite_db):
    """A network blip must leave the span refetchable — otherwise one bad night
    permanently blanks a ticker from the scorecard."""
    with patch("price_history.fetch_from_yahoo", return_value=None) as mock:
        assert price_history.get_daily_closes("AAPL", "2026-01-02", "2026-01-06") == {}
        assert mock.call_count == 1

    with patch("price_history.fetch_from_yahoo", return_value=SERIES) as mock:
        result = price_history.get_daily_closes("AAPL", "2026-01-02", "2026-01-06")
        assert mock.call_count == 1, "retry after a transient failure must hit the network"
    assert result == SERIES


def test_not_found_ticker_is_remembered_and_not_refetched(tmp_sqlite_db):
    """Delisted names are common in this dataset — one 404 should settle it."""
    with patch("price_history.fetch_from_yahoo", return_value=STATUS_NOT_FOUND) as mock:
        assert price_history.get_daily_closes("DEADCO", "2026-01-02", "2026-01-06") == {}
        assert mock.call_count == 1

    with patch("price_history.fetch_from_yahoo", return_value=STATUS_NOT_FOUND) as mock:
        assert price_history.get_daily_closes("DEADCO", "2026-01-02", "2026-01-06") == {}
        assert mock.call_count == 0, "a known-dead ticker must not cost a request"


def test_widening_range_refetches_but_narrowing_does_not(tmp_sqlite_db):
    with patch("price_history.fetch_from_yahoo", return_value=SERIES) as mock:
        price_history.get_daily_closes("AAPL", "2026-01-05", "2026-01-06")
        assert mock.call_count == 1
        # Well outside the padded span — needs a real fetch.
        price_history.get_daily_closes("AAPL", "2025-06-01", "2026-01-06")
        assert mock.call_count == 2
        # Inside what is now covered — no fetch.
        price_history.get_daily_closes("AAPL", "2026-01-05", "2026-01-06")
        assert mock.call_count == 2


def test_bad_inputs_return_empty_without_touching_network(tmp_sqlite_db):
    with patch("price_history.fetch_from_yahoo") as mock:
        assert price_history.get_daily_closes("", "2026-01-02", "2026-01-06") == {}
        assert price_history.get_daily_closes(None, "2026-01-02", "2026-01-06") == {}
        # end before start
        assert price_history.get_daily_closes("AAPL", "2026-01-06", "2026-01-02") == {}
        assert mock.call_count == 0


# ---------- get_close_on_or_after ----------

def test_close_on_exact_trading_day(tmp_sqlite_db):
    with patch("price_history.fetch_from_yahoo", return_value=SERIES):
        assert price_history.get_close_on_or_after("AAPL", "2026-01-05") == ("2026-01-05", 11.0)


def test_close_rolls_forward_over_a_market_holiday(tmp_sqlite_db):
    """2026-01-03 and 01-04 are a weekend — scoring should take Monday's close."""
    with patch("price_history.fetch_from_yahoo", return_value=SERIES):
        assert price_history.get_close_on_or_after("AAPL", "2026-01-03") == ("2026-01-05", 11.0)


def test_close_never_rolls_backward(tmp_sqlite_db):
    """Asking past the end of the series must return nothing, not the last close.
    Reusing a stale close would silently score a horizon that has not happened."""
    with patch("price_history.fetch_from_yahoo", return_value=SERIES):
        assert price_history.get_close_on_or_after("AAPL", "2026-02-01") == (None, None)


def test_missing_ticker_returns_none_pair(tmp_sqlite_db):
    with patch("price_history.fetch_from_yahoo", return_value=STATUS_NOT_FOUND):
        assert price_history.get_close_on_or_after("DEADCO", "2026-01-05") == (None, None)


def test_accepts_date_objects_as_well_as_strings(tmp_sqlite_db):
    with patch("price_history.fetch_from_yahoo", return_value=SERIES):
        assert price_history.get_close_on_or_after("AAPL", date(2026, 1, 5)) == ("2026-01-05", 11.0)


# ---------- fetch_from_yahoo: payload handling ----------

class _Resp:
    def __init__(self, status_code, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


def _chart(timestamps, closes):
    return {"chart": {"result": [{
        "timestamp": timestamps,
        "indicators": {"quote": [{"close": closes}]},
    }]}}


def test_fetch_parses_closes_and_skips_null_bars():
    # 2026-01-02 and 2026-01-05 UTC midnights
    payload = _chart([1767312000, 1767571200], [10.0, None])
    with patch("price_history.requests.get", return_value=_Resp(200, payload)):
        result = price_history.fetch_from_yahoo("AAPL", "2026-01-01", "2026-01-06")
    assert list(result.values()) == [10.0]


def test_fetch_404_reports_not_found():
    with patch("price_history.requests.get", return_value=_Resp(404)):
        assert price_history.fetch_from_yahoo("DEADCO", "2026-01-01", "2026-01-06") is STATUS_NOT_FOUND


def test_fetch_error_block_reports_not_found():
    payload = {"chart": {"result": None, "error": {"code": "Not Found"}}}
    with patch("price_history.requests.get", return_value=_Resp(200, payload)):
        assert price_history.fetch_from_yahoo("DEADCO", "2026-01-01", "2026-01-06") is STATUS_NOT_FOUND


def test_fetch_5xx_is_transient_not_not_found():
    with patch("price_history.requests.get", return_value=_Resp(503)):
        assert price_history.fetch_from_yahoo("AAPL", "2026-01-01", "2026-01-06") is None


def test_fetch_unreadable_payload_is_transient():
    with patch("price_history.requests.get", return_value=_Resp(200, raises=True)):
        assert price_history.fetch_from_yahoo("AAPL", "2026-01-01", "2026-01-06") is None


def test_fetch_network_exception_is_transient():
    with patch("price_history.requests.get", side_effect=RuntimeError("boom")):
        assert price_history.fetch_from_yahoo("AAPL", "2026-01-01", "2026-01-06") is None


def test_fetch_malformed_indicators_is_transient():
    payload = {"chart": {"result": [{"timestamp": [1767312000], "indicators": {}}]}}
    with patch("price_history.requests.get", return_value=_Resp(200, payload)):
        assert price_history.fetch_from_yahoo("AAPL", "2026-01-01", "2026-01-06") is None
