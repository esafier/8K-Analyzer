"""Tests for outcome tracking and the scorecard.

The scoring rule that matters: a call is right when the stock moves the way
it said RELATIVE TO THE MARKET. A bearish filing whose stock fell 5% while
SPY fell 10% was a bad bearish call, whatever the raw return says.
"""
import pytest

import database
import outcomes


def _flag(accession, ticker="AAA", verdict="MONITOR", direction="BEARISH",
          price=10.0, filed="2026-06-01", types="FORFEITURE_EXIT"):
    database.insert_filing({
        "accession_no": accession, "company": f"Co {accession}", "ticker": ticker,
        "cik": "1", "filed_date": filed, "item_codes": "5.02", "filing_url": "u",
        "raw_text": "t", "triage_verdict": verdict, "signal_score": 6,
        "signal_direction": direction, "signal_types": types,
        "pipeline_version": "v4.2-signals", "price_at_ingest": price,
    })
    return database.get_filing_by_accession(accession)["id"]


@pytest.fixture
def quotes(monkeypatch):
    """Controllable price feed."""
    book = {"SPY": 500.0}
    monkeypatch.setattr(outcomes, "_quote", lambda t: book.get(t))
    return book


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def test_flagged_filings_get_a_baseline(tmp_sqlite_db, quotes):
    _flag("a-1")
    assert outcomes.record_baselines() == 1
    row = database.get_all_outcomes()[0]
    assert row["price_0"] == 10.0     # the price at analysis time, not today's
    assert row["spy_0"] == 500.0


def test_pass_filings_are_not_tracked(tmp_sqlite_db, quotes):
    """PASS is the system saying "nothing here" — tracking it would bury the
    flagged rows in noise."""
    _flag("a-1", verdict="PASS")
    assert outcomes.record_baselines() == 0


def test_baselines_are_recorded_once(tmp_sqlite_db, quotes):
    _flag("a-1")
    outcomes.record_baselines()
    assert outcomes.record_baselines() == 0
    assert len(database.get_all_outcomes()) == 1


def test_missing_ingest_price_falls_back_to_a_current_quote(tmp_sqlite_db, quotes):
    _flag("a-1", ticker="BBB", price=None)
    quotes["BBB"] = 42.0
    outcomes.record_baselines()
    assert database.get_all_outcomes()[0]["price_0"] == 42.0


def test_no_benchmark_means_no_baselines(tmp_sqlite_db, quotes):
    """A baseline without SPY can never be read net of the market, and can't
    be fixed later. Better to wait a day."""
    quotes.pop("SPY")
    _flag("a-1")
    assert outcomes.record_baselines() == 0


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------

def test_rows_are_marked_only_when_due(tmp_sqlite_db, quotes):
    _flag("a-1", filed="2026-06-01")
    outcomes.record_baselines()
    quotes["AAA"] = 9.0

    assert outcomes.mark_due(today="2026-06-05") == {7: 0, 30: 0, 90: 0}
    assert outcomes.mark_due(today="2026-06-08") == {7: 1, 30: 0, 90: 0}
    assert outcomes.mark_due(today="2026-07-01") == {7: 0, 30: 1, 90: 0}


def test_a_late_run_still_marks_the_horizon(tmp_sqlite_db, quotes):
    """A skipped day (weekend, outage) is marked on the next run, not lost."""
    _flag("a-1", filed="2026-06-01")
    outcomes.record_baselines()
    quotes["AAA"] = 9.0
    assert outcomes.mark_due(today="2026-06-12")[7] == 1


def test_an_unpriceable_ticker_stays_due(tmp_sqlite_db, quotes):
    _flag("a-1", ticker="GONE", filed="2026-06-01", price=5.0)
    outcomes.record_baselines()
    assert outcomes.mark_due(today="2026-06-10")[7] == 0
    quotes["GONE"] = 4.0
    assert outcomes.mark_due(today="2026-06-11")[7] == 1


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _row(direction, p0, pn, s0=100.0, sn=100.0, types="FORFEITURE_EXIT"):
    return {"direction": direction, "signal_types": types,
            "price_0": p0, "spy_0": s0, "price_30": pn, "spy_30": sn}


def test_bearish_call_is_right_when_the_stock_lags_the_market():
    assert outcomes.signed_excess(_row("BEARISH", 10, 9), 30) == pytest.approx(10.0)


def test_a_falling_stock_can_still_be_a_wrong_bearish_call():
    """Stock -5%, SPY -10%: the stock OUTPERFORMED. The bearish call was wrong."""
    value = outcomes.signed_excess(_row("BEARISH", 10, 9.5, s0=100, sn=90), 30)
    assert value == pytest.approx(-5.0)


def test_bullish_call_is_right_when_the_stock_leads():
    assert outcomes.signed_excess(_row("BULLISH", 10, 12), 30) == pytest.approx(20.0)


def test_mixed_calls_are_not_scored():
    """No direction, nothing to be right about."""
    assert outcomes.signed_excess(_row("MIXED", 10, 12), 30) is None


def test_incomplete_rows_are_not_scored():
    assert outcomes.signed_excess({"direction": "BEARISH", "price_0": 10}, 30) is None


def test_scorecard_groups_by_signal_type_and_overall():
    rows = [
        _row("BEARISH", 10, 9, types="FORFEITURE_EXIT"),
        _row("BEARISH", 10, 11, types="FORFEITURE_EXIT,NO_SUCCESSOR"),
        _row("BULLISH", 10, 13, types="HURDLE_CONVICTION"),
    ]
    table = outcomes.scorecard(rows)

    assert table["FORFEITURE_EXIT"][30]["n"] == 2
    assert table["FORFEITURE_EXIT"][30]["hit_rate"] == 0.5
    assert table["HURDLE_CONVICTION"][30]["hit_rate"] == 1.0
    assert table["ALL"][30]["n"] == 3
    assert 7 not in table["ALL"]  # no 7-day marks in these rows


def test_pending_counts():
    rows = [_row("BEARISH", 10, 9), {"price_0": 10, "price_30": None}]
    counts = outcomes.pending_counts(rows)
    assert counts["tracked"] == 2
    assert counts[30] == 1


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def test_scorecard_page_renders_empty(tmp_sqlite_db):
    from app import app
    app.config["TESTING"] = True
    body = app.test_client().get("/scorecard").get_data(as_text=True)
    assert "Nothing to score yet" in body


def test_scorecard_page_renders_results(tmp_sqlite_db, quotes):
    from app import app
    app.config["TESTING"] = True
    _flag("a-1", filed="2026-06-01")
    outcomes.record_baselines()
    quotes["AAA"] = 8.0
    outcomes.mark_due(today="2026-07-02")

    body = app.test_client().get("/scorecard").get_data(as_text=True)
    assert "All flagged filings" in body
    assert "Forfeiture Exit" in body
