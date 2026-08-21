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


def test_bars_cached_before_a_delisting_are_still_served(tmp_sqlite_db):
    """A company that went dark last month traded normally the month before,
    and those bars are exactly what scoring its filings needs."""
    with patch("price_history.fetch_from_yahoo", return_value=SERIES):
        price_history.get_daily_closes("GONE", "2026-01-02", "2026-01-06")

    with patch("price_history.fetch_from_yahoo", return_value=STATUS_NOT_FOUND):
        # A later lookup past the cached span discovers the ticker is dead.
        price_history.get_daily_closes("GONE", "2026-06-01", "2026-06-10")

    with patch("price_history.fetch_from_yahoo") as mock:
        recovered = price_history.get_daily_closes("GONE", "2026-01-02", "2026-01-06")
        assert mock.call_count == 0, "a dead ticker must not cost a request"
    assert recovered == SERIES, "cached history was discarded when the ticker went dark"


def test_cache_is_served_on_the_very_first_not_found_response(tmp_sqlite_db):
    """A widened fetch can 404 while the requested window is already cached and
    perfectly good. Returning nothing on that first call would let the caller
    see 'no price' next to a freshly-written not_found status and write the
    filing off as delisted — after which it is no longer retryable."""
    with patch("price_history.fetch_from_yahoo", return_value=SERIES):
        price_history.get_daily_closes("GONE", "2026-01-02", "2026-01-06")

    # Same call, but the window now reaches past the cached span, forcing a
    # refetch that comes back 404.
    with patch("price_history.fetch_from_yahoo", return_value=STATUS_NOT_FOUND):
        result = price_history.get_daily_closes("GONE", "2026-01-02", "2026-06-01")

    assert result == SERIES, "cached bars were dropped on the first not-found response"


# ---------- a session in progress is not a close ----------

def test_todays_forming_candle_is_never_cached(tmp_sqlite_db):
    """Yahoo returns a bar for the session in progress whose 'close' is just the
    last trade so far. A horizon mark is never revisited, so storing that would
    freeze an intraday price into the scorecard permanently."""
    from datetime import datetime, timedelta

    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    payload_series = {
        (today - timedelta(days=4)).isoformat(): 10.0,
        yesterday.isoformat(): 11.0,
        today.isoformat(): 99.0,          # still forming
    }

    def fake_fetch(ticker, start, end):
        # Mimic the real parser's cutoff behaviour via the real function's rule.
        return {d: c for d, c in payload_series.items()
                if d <= price_history.latest_complete_date().isoformat()}

    with patch("price_history.fetch_from_yahoo", side_effect=fake_fetch):
        result = price_history.get_daily_closes(
            "AAPL", (today - timedelta(days=5)).isoformat(), today.isoformat()
        )

    assert today.isoformat() not in result, "an in-progress session was cached as a close"
    assert result.get(yesterday.isoformat()) == 11.0


def test_parser_drops_bars_from_the_current_session():
    """The cutoff lives in the parser too, so nothing can reach the cache."""
    from datetime import datetime, timedelta

    today = datetime.utcnow()
    yesterday = today - timedelta(days=1)
    payload = _chart(
        [int(yesterday.timestamp()), int(today.timestamp())],
        [11.0, 99.0],
    )
    with patch("price_history.requests.get", return_value=_Resp(200, payload)):
        series = price_history.fetch_from_yahoo("AAPL", "2026-01-01", today.date())

    assert today.strftime("%Y-%m-%d") not in series
    assert series.get(yesterday.strftime("%Y-%m-%d")) == 11.0


def test_a_window_reaching_into_today_still_reports_coverage(tmp_sqlite_db):
    """Coverage is judged against the last complete session. Judging it against
    the raw requested end would leave any window touching today permanently
    'uncovered', refetching on every single call."""
    from datetime import datetime, timedelta

    today = datetime.utcnow().date()
    start = (today - timedelta(days=5)).isoformat()

    def fake_fetch(ticker, s, e):
        return {(today - timedelta(days=2)).isoformat(): 10.0}

    with patch("price_history.fetch_from_yahoo", side_effect=fake_fetch) as mock:
        price_history.get_daily_closes("AAPL", start, today.isoformat())
        assert mock.call_count == 1
        price_history.get_daily_closes("AAPL", start, today.isoformat())
        assert mock.call_count == 1, "refetched a window that was already covered"


# ---------- coverage must never be claimed over an unfetched gap ----------

def test_disjoint_spans_are_not_merged_into_a_false_claim(tmp_sqlite_db):
    """Two interleaved runs can each fetch a different slice of the same ticker.
    Merging Jan-Feb with Jun-Jul would claim Mar-May was fetched, and a
    claimed-but-empty span reads downstream as 'we looked and there is nothing',
    which permanently writes filings off as unpriceable."""
    import database

    database.upsert_price_history_meta("AAPL", "2026-01-01", "2026-02-01")
    database.upsert_price_history_meta("AAPL", "2026-06-01", "2026-07-01")

    meta = database.get_price_history_meta("AAPL")
    assert not (meta["span_start"] <= "2026-04-01" <= meta["span_end"]), \
        "March-May was claimed as covered but never fetched"
    assert price_history.has_coverage("AAPL", "2026-04-01", "2026-04-10") is False


def test_overlapping_spans_still_widen(tmp_sqlite_db):
    """The normal path must be untouched — a refetch always spans the union of
    the request and what was stored, so it can never be disjoint."""
    import database

    database.upsert_price_history_meta("AAPL", "2026-01-01", "2026-03-01")
    database.upsert_price_history_meta("AAPL", "2026-02-01", "2026-06-01")

    meta = database.get_price_history_meta("AAPL")
    assert meta["span_start"] == "2026-01-01"
    assert meta["span_end"] == "2026-06-01"


def test_adjacent_spans_still_widen(tmp_sqlite_db):
    import database

    database.upsert_price_history_meta("AAPL", "2026-01-01", "2026-03-01")
    database.upsert_price_history_meta("AAPL", "2026-03-01", "2026-05-01")

    meta = database.get_price_history_meta("AAPL")
    assert (meta["span_start"], meta["span_end"]) == ("2026-01-01", "2026-05-01")


# ---------- only a genuine symbol miss may be recorded permanently ----------

def test_a_recoverable_error_payload_is_not_treated_as_a_symbol_miss():
    """A 200 carrying a throttling or internal error is recoverable. Recording
    it as not_found would permanently blank a live ticker with no retry path —
    and a long backfill is exactly when such an error is likeliest to arrive."""
    for code in ("Too Many Requests", "Internal Server Error", "Unauthorized"):
        payload = {"chart": {"result": None,
                             "error": {"code": code, "description": code}}}
        with patch("price_history.requests.get", return_value=_Resp(200, payload)):
            result = price_history.fetch_from_yahoo("AAPL", "2026-01-01", "2026-01-06")
        assert result is None, f"{code!r} was treated as a permanent symbol miss"


def test_yahoos_real_symbol_miss_wording_is_still_permanent():
    """Yahoo's actual miss payload, verified live: code 'Not Found',
    description 'No data found, symbol may be delisted'."""
    payload = {"chart": {"result": None, "error": {
        "code": "Not Found", "description": "No data found, symbol may be delisted"}}}
    with patch("price_history.requests.get", return_value=_Resp(200, payload)):
        assert price_history.fetch_from_yahoo("DEADCO", "2026-01-01", "2026-01-06") is STATUS_NOT_FOUND


def test_a_recoverable_error_leaves_the_ticker_retryable(tmp_sqlite_db):
    """End to end: the recoverable error must not persist a not_found status."""
    payload = {"chart": {"result": None,
                         "error": {"code": "Too Many Requests", "description": "rate limited"}}}
    with patch("price_history.requests.get", return_value=_Resp(200, payload)):
        assert price_history.get_daily_closes("AAPL", "2026-01-02", "2026-01-06") == {}

    import database
    meta = database.get_price_history_meta("AAPL")
    assert meta is None or meta.get("status") != STATUS_NOT_FOUND, \
        "a rate-limit response permanently blanked a live ticker"

    with patch("price_history.fetch_from_yahoo", return_value=SERIES) as mock:
        assert price_history.get_daily_closes("AAPL", "2026-01-02", "2026-01-06") == SERIES
        assert mock.call_count == 1, "the ticker was not retried"
