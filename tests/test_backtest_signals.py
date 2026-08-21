"""Tests for backtest_signals.py — the read-only signal scorer.

The measurement rules this pins down are the ones that quietly corrupt a
backtest when they are wrong: the benchmark must be read on the stock's own bar
date, gaps must never be interpolated, and a bucket's hit rate is meaningless
without its sample size.
"""
import math

import pytest

import backtest_signals as bt
import database


def test_wilson_matches_known_values():
    lo, hi = bt.wilson(50, 100)
    assert math.isclose(lo, 0.4038, abs_tol=0.001)
    assert math.isclose(hi, 0.5962, abs_tol=0.001)

    # A tiny sample must produce a wide interval, not a confident-looking one.
    lo, hi = bt.wilson(3, 4)
    assert hi - lo > 0.5

    assert bt.wilson(0, 0) == (0.0, 0.0)


def test_first_close_takes_next_real_bar_not_an_interpolation():
    closes = {"2026-06-01": 10.0, "2026-06-05": 12.0}

    # Exact hit
    assert bt.first_close_on_or_after(closes, "2026-06-01") == ("2026-06-01", 10.0)
    # Weekend/holiday gap rolls forward to the next real close
    assert bt.first_close_on_or_after(closes, "2026-06-02") == ("2026-06-05", 12.0)
    # Beyond the lookahead there is no honest answer
    assert bt.first_close_on_or_after(closes, "2026-06-02", max_days=1) == (None, None)
    assert bt.first_close_on_or_after({}, "2026-06-01") == (None, None)


def test_benchmark_is_read_on_the_stocks_own_bar_date(monkeypatch):
    """If the stock is halted on the horizon date, its bar lands later. Reading
    SPY on the calendar date instead would compare two different spans and
    invent an excess return out of the mismatch."""
    stock = {"2026-06-01": 100.0, "2026-06-10": 110.0}   # halted 06-08, trades 06-10
    spy = {"2026-06-01": 100.0, "2026-06-08": 100.0, "2026-06-10": 110.0}

    def fake_fetch(ticker, start, end):
        return dict(spy) if ticker == bt.BENCHMARK else dict(stock)

    monkeypatch.setattr(bt.ph, "fetch_from_yahoo", fake_fetch)

    rows = [{"ticker": "HALT", "filed_date": "2026-06-01",
             "signal_direction": "BULLISH", "signal_score": 8}]
    scored = bt.score_rows(rows, (7,), verbose=False)[0]

    assert scored["bar_7d"] == "2026-06-10"
    # SPY read on 06-10 (110.0) too, so both legs rose 10% and the excess is 0 —
    # not the +10% a calendar-date read of SPY on 06-08 would have produced.
    assert scored["spy_7d"] == 110.0
    assert math.isclose(scored["excess_7d"], 0.0, abs_tol=1e-9)


def test_excess_return_and_hit_direction(monkeypatch):
    stock = {"2026-06-01": 100.0, "2026-06-08": 90.0}
    spy = {"2026-06-01": 100.0, "2026-06-08": 100.0}

    monkeypatch.setattr(bt.ph, "fetch_from_yahoo",
                        lambda t, s, e: dict(spy) if t == bt.BENCHMARK else dict(stock))

    bearish = bt.score_rows([{"ticker": "DOWN", "filed_date": "2026-06-01",
                              "signal_direction": "BEARISH", "signal_score": 8}],
                            (7,), verbose=False)[0]
    bullish = bt.score_rows([{"ticker": "DOWN", "filed_date": "2026-06-01",
                              "signal_direction": "BULLISH", "signal_score": 8}],
                            (7,), verbose=False)[0]

    assert math.isclose(bearish["excess_7d"], -0.10, abs_tol=1e-9)
    assert bearish["hit_7d"] is True      # lagged SPY, which is what BEARISH claimed
    assert bullish["hit_7d"] is False


def test_unpriceable_rows_are_flagged_not_zeroed(monkeypatch):
    """A delisted name has no return. Recording 0.0 would plant a fake datapoint
    right in the middle of the base rate."""
    spy = {"2026-06-01": 100.0, "2026-06-08": 100.0}
    monkeypatch.setattr(bt.ph, "fetch_from_yahoo",
                        lambda t, s, e: dict(spy) if t == bt.BENCHMARK else bt.ph.STATUS_NOT_FOUND)

    scored = bt.score_rows([{"ticker": "GONE", "filed_date": "2026-06-01",
                             "signal_direction": "BEARISH", "signal_score": 8}],
                           (7,), verbose=False)[0]

    assert scored["unpriced_reason"] == "no_baseline"
    assert "excess_7d" not in scored
    assert "baseline_close" not in scored


def test_missing_horizon_bar_leaves_excess_none(monkeypatch):
    """Baseline priced, horizon beyond the data — the row stays unscored."""
    stock = {"2026-06-01": 100.0}
    spy = {"2026-06-01": 100.0}
    monkeypatch.setattr(bt.ph, "fetch_from_yahoo",
                        lambda t, s, e: dict(spy) if t == bt.BENCHMARK else dict(stock))

    scored = bt.score_rows([{"ticker": "NEW", "filed_date": "2026-06-01",
                             "signal_direction": "BULLISH", "signal_score": 8}],
                           (30,), verbose=False)[0]

    assert scored["baseline_close"] == 100.0
    assert scored["excess_30d"] is None
    assert scored["hit_30d"] is None


def test_missing_benchmark_raises_rather_than_scoring_unbenchmarked(monkeypatch):
    monkeypatch.setattr(bt.ph, "fetch_from_yahoo", lambda t, s, e: {})
    with pytest.raises(RuntimeError, match="SPY"):
        bt.score_rows([{"ticker": "X", "filed_date": "2026-06-01",
                        "signal_direction": "BULLISH", "signal_score": 5}],
                      (7,), verbose=False)


def _insert(accession, ticker, filed_date, direction, score):
    conn = database.get_connection()
    cursor = conn.cursor()
    p = database._placeholder()
    cursor.execute(
        f"INSERT INTO filings (accession_no, company, cik, ticker, filed_date, "
        f"item_codes, signal_direction, signal_score) "
        f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})",
        (accession, f"{ticker} Inc", "0000000001", ticker, filed_date,
         "5.02", direction, score),
    )
    conn.commit()
    conn.close()


def test_load_filings_returns_dicts_and_filters(tmp_sqlite_db):
    _insert("a-1", "AAA", "2026-06-01", "BULLISH", 8)
    _insert("a-2", "BBB", "2026-06-02", "BEARISH", 4)
    _insert("a-3", "CCC", "2026-06-03", None, None)      # unscored
    _insert("a-4", "", "2026-06-04", "BULLISH", 7)        # no ticker

    rows = bt.load_filings()
    assert all(type(r) is dict for r in rows)
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
    assert rows[0]["filed_date"] == "2026-06-02"          # newest first

    high = bt.load_filings(min_score=7)
    assert [r["ticker"] for r in high] == ["AAA"]

    assert len(bt.load_filings(limit=1)) == 1


def test_load_filings_limit_is_coerced_to_int(tmp_sqlite_db):
    _insert("a-1", "AAA", "2026-06-01", "BULLISH", 8)
    with pytest.raises(ValueError):
        bt.load_filings(limit="1; DROP TABLE filings")


def test_report_survives_an_all_unpriced_sample(capsys):
    bt.report([{"ticker": "X", "signal_direction": "BULLISH", "signal_score": 5}], 30)
    assert "No filing could be priced" in capsys.readouterr().out
