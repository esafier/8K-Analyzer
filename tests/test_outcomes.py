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


def _series(start, prices, skip=()):
    """Consecutive calendar-day closes from `start`, minus `skip` dates."""
    import price_history
    out, day = {}, start
    for p in prices:
        while day in skip:
            day = price_history.add_days(day, 1)
        out[day] = p
        day = price_history.add_days(day, 1)
    return out


@pytest.fixture
def history(monkeypatch):
    """Controllable close history: {ticker: {date: close}}."""
    import price_history
    book = {"SPY": _series("2026-05-25", [500.0] * 120)}
    monkeypatch.setattr(price_history, "closes", lambda t, start, end=None: book.get(t, {}))
    return book


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def test_flagged_filings_are_priced_at_the_filing_date_close(tmp_sqlite_db, history):
    """The baseline is the close on the filing date, not the price whenever
    the job happened to run — a backfilled filing used to get a baseline
    weeks after it was filed."""
    _flag("a-1", filed="2026-06-01", price=99.0)
    history["AAA"] = _series("2026-05-25", [8.0] * 7 + [10.0] + [11.0] * 100)
    result = outcomes.run(today="2026-06-05")

    row = database.get_all_outcomes()[0]
    assert result["started"] == 1 and result["priced"] == 1
    assert row["price_0"] == 10.0     # 2026-06-01's close, not price_at_ingest
    assert row["spy_0"] == 500.0
    assert row["price_source"] == "history"
    assert row["company"] == "Co a-1"


def test_a_weekend_filing_uses_the_next_session(tmp_sqlite_db, history):
    _flag("a-1", filed="2026-06-06")   # Saturday
    history["AAA"] = {"2026-06-05": 9.0, "2026-06-08": 12.0}
    history["SPY"] = {"2026-06-05": 500.0, "2026-06-08": 505.0}
    outcomes.run(today="2026-06-10")
    row = database.get_all_outcomes()[0]
    assert (row["price_0"], row["spy_0"]) == (12.0, 505.0)


def test_pass_filings_are_not_tracked(tmp_sqlite_db, history):
    """PASS is the system saying "nothing here" — tracking it would bury the
    flagged rows in noise."""
    _flag("a-1", verdict="PASS")
    assert outcomes.record_baselines() == 0


def test_baselines_are_recorded_once(tmp_sqlite_db, history):
    _flag("a-1")
    outcomes.record_baselines()
    assert outcomes.record_baselines() == 0
    assert len(database.get_all_outcomes()) == 1


def test_no_benchmark_means_nothing_is_priced(tmp_sqlite_db, history):
    """A move without SPY can never be read net of the market. Wait a day."""
    history.pop("SPY")
    history["AAA"] = _series("2026-05-25", [10.0] * 30)
    _flag("a-1")
    result = outcomes.run(today="2026-06-10")
    assert result["priced"] == 0
    assert database.get_all_outcomes()[0]["price_0"] is None


def test_todays_close_is_not_used(tmp_sqlite_db, history):
    """A bar dated today is still moving; it isn't a close yet."""
    _flag("a-1", filed="2026-06-01")
    history["AAA"] = _series("2026-06-01", [10.0] * 20)
    outcomes.run(today="2026-06-01")
    assert database.get_all_outcomes()[0]["price_0"] is None
    outcomes.run(today="2026-06-02")
    assert database.get_all_outcomes()[0]["price_0"] == 10.0


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------

def test_marks_appear_only_once_their_session_has_closed(tmp_sqlite_db, history):
    _flag("a-1", filed="2026-06-01")
    history["AAA"] = _series("2026-06-01", [10.0] + [9.0] * 110)

    outcomes.run(today="2026-06-08")          # 06-08 is the 7-day session: not closed yet
    assert database.get_all_outcomes()[0]["price_7"] is None
    outcomes.run(today="2026-06-09")
    row = database.get_all_outcomes()[0]
    assert row["price_7"] == 9.0 and row["price_30"] is None


def test_a_late_run_still_marks_the_right_date(tmp_sqlite_db, history):
    """Nothing is taken late: a run months after the filing prices every
    horizon at its own session, not at today's quote."""
    _flag("a-1", filed="2026-06-01")
    prices = [10.0] * 7 + [9.0] * 23 + [8.0] * 60 + [7.0] * 20
    history["AAA"] = _series("2026-06-01", prices)
    outcomes.run(today="2026-09-15")
    row = database.get_all_outcomes()[0]
    assert (row["price_0"], row["price_7"], row["price_30"], row["price_90"]) == (10.0, 9.0, 8.0, 7.0)


def test_a_mark_on_a_holiday_rolls_to_the_next_session(tmp_sqlite_db, history):
    _flag("a-1", filed="2026-06-01")
    history["AAA"] = _series("2026-06-01", [10.0] * 7 + [11.0] + [12.0] * 30,
                             skip={"2026-06-08"})
    outcomes.run(today="2026-06-20")
    assert database.get_all_outcomes()[0]["price_7"] == 11.0   # 06-09


def test_an_unpriceable_ticker_stays_pending(tmp_sqlite_db, history):
    _flag("a-1", ticker="GONE", filed="2026-06-01")
    assert outcomes.run(today="2026-06-10")["unpriced"] == 1
    history["GONE"] = _series("2026-06-01", [4.0] * 20)
    assert outcomes.run(today="2026-06-11")["priced"] == 1


def test_rows_priced_from_live_quotes_are_repriced(tmp_sqlite_db, history):
    """The first version stored a baseline from whatever day the job ran and
    marked it immediately. Those rows must be corrected, marks included."""
    filing_id = _flag("a-1", filed="2026-06-01")
    database.insert_outcome_baseline(filing_id, "AAA", "BEARISH", "FORFEITURE_EXIT",
                                     "2026-06-01", 50.0, 600.0)
    oid = database.get_all_outcomes()[0]["id"]
    database.set_outcome_prices(oid, {"price_0": 50.0, "spy_0": 600.0,
                                      "price_7": 50.0, "spy_7": 600.0}, source=None)
    history["AAA"] = _series("2026-06-01", [10.0] * 5 + [9.0] * 30)

    outcomes.run(today="2026-06-05")      # only the baseline is due
    row = database.get_all_outcomes()[0]
    assert (row["price_0"], row["spy_0"]) == (10.0, 500.0)
    assert row["price_7"] is None          # the stale mark is gone, not kept


def test_live_quote_rows_that_history_cant_price_are_cleared(tmp_sqlite_db, history):
    """A delisted ticker can't be re-priced, but its live-quote marks are
    still wrong. They must not keep counting on the scorecard."""
    filing_id = _flag("a-1", ticker="GONE", filed="2026-06-01")
    database.insert_outcome_baseline(filing_id, "GONE", "BEARISH", "FORFEITURE_EXIT",
                                     "2026-06-01", 50.0, 600.0)
    oid = database.get_all_outcomes()[0]["id"]
    database.set_outcome_prices(oid, {"price_0": 50.0, "spy_0": 600.0,
                                      "price_7": 45.0, "spy_7": 600.0}, source=None)
    assert outcomes.run(today="2026-06-20")["unpriced"] == 1
    row = database.get_all_outcomes()[0]
    assert row["price_0"] is None and row["price_7"] is None
    assert outcomes.scorecard() == {}


def test_finished_rows_are_not_refetched(tmp_sqlite_db, history, monkeypatch):
    _flag("a-1", filed="2026-06-01")
    history["AAA"] = _series("2026-06-01", [10.0] * 120)
    outcomes.run(today="2026-06-05")

    import price_history
    calls = []
    monkeypatch.setattr(price_history, "closes",
                        lambda t, start, end=None: calls.append(t) or history.get(t, {}))
    outcomes.run(today="2026-06-06")       # nothing new is due
    assert calls == []


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


def test_filing_results_lists_scored_filings_newest_first():
    rows = [
        dict(_row("BEARISH", 10, 9), filing_id=1, ingest_date="2026-06-01", ticker="OLD"),
        dict(_row("BULLISH", 10, 12), filing_id=2, ingest_date="2026-06-09", ticker="NEW"),
        dict(_row("MIXED", 10, 12), filing_id=3, ingest_date="2026-06-10", ticker="MIX"),
        {"filing_id": 4, "direction": "BEARISH", "price_0": 10, "ingest_date": "2026-06-11"},
    ]
    results = outcomes.filing_results(rows)
    assert [r["ticker"] for r in results] == ["NEW", "OLD"]
    assert results[0]["marks"][30] == pytest.approx(20.0)
    assert results[0]["marks"][7] is None


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


def test_scorecard_page_renders_results(tmp_sqlite_db, history):
    from app import app
    app.config["TESTING"] = True
    _flag("a-1", filed="2026-06-01")
    history["AAA"] = _series("2026-06-01", [10.0] + [8.0] * 60)
    outcomes.run(today="2026-07-15")

    body = app.test_client().get("/scorecard").get_data(as_text=True)
    assert "All flagged filings" in body
    assert "Forfeiture Exit" in body
    assert "Filings behind the numbers" in body
    assert "/filing/" in body and "+20.0%" in body
