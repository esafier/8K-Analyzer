"""Tests for the daily-close history the scorecard is priced from."""
from unittest.mock import MagicMock, patch

import pytest

import price_history

# Trimmed from a real chart response: timestamps are the session open in
# UTC, gmtoffset converts them back to the exchange's date.
PAYLOAD = {"chart": {"result": [{
    "meta": {"gmtoffset": -14400},
    "timestamp": [1780320600, 1780407000, 1780493400],   # Jun 1, 2, 3 2026 09:30 ET
    "indicators": {
        "quote": [{"close": [10.5, 11.5, None]}],
        "adjclose": [{"adjclose": [10.0, 11.0, None]}],
    },
}]}}


def test_parse_uses_adjusted_closes_on_exchange_dates():
    assert price_history._parse(PAYLOAD) == {"2026-06-01": 10.0, "2026-06-02": 11.0}


def _split_payload(pre, post, ratio=(1.0, 50.0)):
    return {"chart": {"result": [{
        "meta": {"gmtoffset": -14400},
        "timestamp": [1780320600, 1780407000],                # Jun 1, Jun 2
        "events": {"splits": {"1780407000": {
            "date": 1780407000, "numerator": ratio[0], "denominator": ratio[1]}}},
        "indicators": {"quote": [{"close": [pre, post]}],
                       "adjclose": [{"adjclose": [pre, post]}]},
    }]}}


def test_an_unadjusted_reverse_split_is_folded_in():
    """NFE, Sept 2026: a 1:50 reverse split Yahoo had not yet adjusted for
    read as a 3,600% gain."""
    closes = price_history._parse(_split_payload(0.33, 12.77))
    assert closes["2026-06-01"] == pytest.approx(16.5)
    assert closes["2026-06-02"] == 12.77


def test_an_already_adjusted_split_is_not_applied_twice():
    closes = price_history._parse(_split_payload(16.5, 12.77))
    assert closes["2026-06-01"] == 16.5


def test_an_unadjusted_forward_split_is_folded_in():
    closes = price_history._parse(_split_payload(200.0, 101.0, ratio=(2.0, 1.0)))
    assert closes["2026-06-01"] == pytest.approx(100.0)


def _nominal_payload(quote, ratio=(10.0, 1.0)):
    """A 10:1 forward split on Jun 2 that Yahoo has already adjusted for."""
    return {"chart": {"result": [{
        "meta": {"gmtoffset": -14400},
        "timestamp": [1780320600, 1780407000],
        "events": {"splits": {"1780407000": {
            "date": 1780407000, "numerator": ratio[0], "denominator": ratio[1]}}},
        "indicators": {"quote": [{"close": quote}],
                       "adjclose": [{"adjclose": [x * 0.98 for x in quote]}]},
    }]}}


def test_nominal_closes_undo_a_later_split():
    """A hurdle written as $500 on Jun 1 is compared with the $500 the stock
    traded at, not the $50 it reads as after a later 10:1 split."""
    closes = price_history._parse(_nominal_payload([50.0, 51.0]), nominal=True)
    assert closes["2026-06-01"] == pytest.approx(500.0)
    assert closes["2026-06-02"] == 51.0


def test_nominal_closes_skip_the_dividend_adjustment():
    closes = price_history._parse(_nominal_payload([50.0, 51.0], ratio=(1.0, 1.0)), nominal=True)
    assert closes["2026-06-01"] == 50.0


def test_nominal_leaves_an_unadjusted_split_alone():
    """Yahoo hasn't adjusted yet (the NFE case): the quote is already what traded."""
    closes = price_history._parse(_split_payload(0.33, 12.77), nominal=True)
    assert closes["2026-06-01"] == 0.33


def test_parse_tolerates_an_empty_response():
    assert price_history._parse({"chart": {"result": None, "error": {"code": "Not Found"}}}) == {}
    assert price_history._parse(None) == {}


def test_share_class_tickers_use_yahoos_dash():
    assert price_history._yahoo_symbol("brk.b") == "BRK-B"


def test_close_on_or_after_rolls_forward_and_respects_the_bound():
    series = {"2026-06-01": 10.0, "2026-06-03": 12.0}
    assert price_history.close_on_or_after(series, "2026-06-02") == ("2026-06-03", 12.0)
    assert price_history.close_on_or_after(series, "2026-06-02", before="2026-06-03") is None
    assert price_history.close_on_or_after(series, "2026-06-04") is None


def test_unknown_symbol_is_empty_not_an_error():
    resp = MagicMock(status_code=404)
    with patch("price_history.requests.get", return_value=resp) as get:
        assert price_history.fetch_closes("NOPE", "2026-06-01", "2026-06-05") == {}
    assert get.call_count == 1


def test_a_failing_host_falls_back_to_the_other(monkeypatch):
    monkeypatch.setattr(price_history.time, "sleep", lambda s: None)
    bad = MagicMock(status_code=429)
    good = MagicMock(status_code=200)
    good.json.return_value = PAYLOAD
    with patch("price_history.requests.get", side_effect=[bad, good]):
        assert price_history.fetch_closes("AAA", "2026-06-01", "2026-06-05")["2026-06-02"] == 11.0
