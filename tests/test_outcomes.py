"""Tests for the outcome baseline + marking jobs.

All prices are mocked. The behaviors under test are the ones that decide
whether the scorecard can be trusted: the benchmark must be priced on the same
bar as the stock, failures must not block ingest, unpriceable filings must stay
visible in the denominator, and nothing may be marked before it has happened.
"""
from datetime import date
from unittest.mock import patch

import database
import outcomes


def _insert(accession="0000-01", ticker="AAPL", filed_date="2026-01-05",
            verdict="DEEP_LOOK", direction="BEARISH"):
    database.insert_filing({
        "accession_no": accession, "company": "Test Co", "ticker": ticker,
        "cik": "0000001", "filed_date": filed_date, "item_codes": "5.02",
        "triage_verdict": verdict, "signal_direction": direction, "signal_score": 8,
    })
    return database.get_filing_by_accession(accession)["id"]


def _closes(mapping):
    """Build a get_close_on_or_after stub from {(ticker, date): (bar, close)}."""
    def stub(ticker, target, **_kw):
        key = (ticker.upper(), str(target)[:10])
        return mapping.get(key, (None, None))
    return stub


# ---------- capture_baselines ----------

def test_baseline_prices_stock_and_benchmark(tmp_sqlite_db):
    _insert()
    stub = _closes({
        ("AAPL", "2026-01-05"): ("2026-01-05", 10.0),
        ("SPY", "2026-01-05"): ("2026-01-05", 500.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        assert outcomes.capture_baselines() == {"priced": 1, "unpriced": 0, "skipped": 0}

    out = database.get_signal_outcomes()[0]
    assert (out["baseline_date"], out["baseline_close"], out["baseline_spy"]) == \
        ("2026-01-05", 10.0, 500.0)


def test_benchmark_is_priced_on_the_stocks_own_bar_date(tmp_sqlite_db):
    """If the filing lands on a Friday holiday and the stock's first close is
    Monday, SPY must be priced Monday too — otherwise the excess return is
    measuring a calendar offset rather than the signal."""
    _insert(filed_date="2026-01-03")
    calls = []

    def stub(ticker, target, **_kw):
        calls.append((ticker.upper(), str(target)[:10]))
        if ticker.upper() == "AAPL":
            return ("2026-01-05", 10.0)
        return ("2026-01-05", 500.0)

    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        outcomes.capture_baselines()

    assert ("SPY", "2026-01-05") in calls, f"benchmark priced off the stock's bar: {calls}"
    assert ("SPY", "2026-01-03") not in calls


def test_unpriceable_filing_is_recorded_not_dropped(tmp_sqlite_db):
    """A filing the source answered about but has no bars for still belongs in
    the denominator."""
    _insert()
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._answer_is_final", return_value=True):
        assert outcomes.capture_baselines()["unpriced"] == 1

    out = database.get_signal_outcomes()[0]
    assert out["status"] == database.OUTCOME_NO_PRICE
    assert out["baseline_close"] is None


def test_delisted_ticker_is_labelled_as_such(tmp_sqlite_db):
    _insert(ticker="DEADCO")
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=True):
        outcomes.capture_baselines()

    assert database.get_signal_outcomes()[0]["status"] == database.OUTCOME_DELISTED


def test_missing_benchmark_leaves_the_row_for_a_retry(tmp_sqlite_db):
    """A missing SPY close is our problem, not the filing's — do not burn the
    row by recording it as unpriceable."""
    _insert()
    stub = _closes({("AAPL", "2026-01-05"): ("2026-01-05", 10.0)})
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        assert outcomes.capture_baselines()["skipped"] == 1

    assert database.get_signal_outcomes() == []
    assert len(database.get_filings_needing_outcome_baseline()) == 1


def test_one_bad_ticker_does_not_stop_the_batch(tmp_sqlite_db):
    _insert("0000-01", ticker="BOOM")
    _insert("0000-02", ticker="AAPL")

    def stub(ticker, target, **_kw):
        if ticker.upper() == "BOOM":
            raise RuntimeError("simulated failure")
        return ("2026-01-05", 10.0) if ticker.upper() == "AAPL" else ("2026-01-05", 500.0)

    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        stats = outcomes.capture_baselines()

    assert stats["priced"] == 1 and stats["skipped"] == 1


def test_baselines_are_not_recaptured(tmp_sqlite_db):
    _insert()
    stub = _closes({
        ("AAPL", "2026-01-05"): ("2026-01-05", 10.0),
        ("SPY", "2026-01-05"): ("2026-01-05", 500.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=stub) as mock:
        outcomes.capture_baselines()
        first = mock.call_count
        outcomes.capture_baselines()
        assert mock.call_count == first, "already-priced filing was re-fetched"


# ---------- mark_due_outcomes ----------

def _priced_filing(tmp_db, filed_date="2026-01-05", ticker="AAPL"):
    _insert(ticker=ticker, filed_date=filed_date)
    stub = _closes({
        (ticker, filed_date): (filed_date, 10.0),
        ("SPY", filed_date): (filed_date, 500.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        outcomes.capture_baselines()
    return database.get_signal_outcomes()[0]["filing_id"]


def test_marks_a_horizon_once_it_has_elapsed(tmp_sqlite_db):
    _priced_filing(tmp_sqlite_db)
    stub = _closes({
        ("AAPL", "2026-01-12"): ("2026-01-12", 9.0),
        ("SPY", "2026-01-12"): ("2026-01-12", 505.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        result = outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))

    assert result[7]["marked"] == 1
    out = database.get_signal_outcomes()[0]
    assert (out["close_7d"], out["spy_7d"]) == (9.0, 505.0)
    assert out["close_30d"] is None, "a horizon that has not elapsed was marked"


def test_nothing_is_marked_before_the_horizon_elapses(tmp_sqlite_db):
    _priced_filing(tmp_sqlite_db)
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})) as mock:
        result = outcomes.mark_due_outcomes(as_of=date(2026, 1, 8))
    assert result[7]["marked"] == 0
    assert mock.call_count == 0, "priced a horizon that has not happened"


def test_marking_is_idempotent(tmp_sqlite_db):
    _priced_filing(tmp_sqlite_db)
    stub = _closes({
        ("AAPL", "2026-01-12"): ("2026-01-12", 9.0),
        ("SPY", "2026-01-12"): ("2026-01-12", 505.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))
        second = outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))

    assert second[7]["marked"] == 0
    assert database.get_signal_outcomes()[0]["close_7d"] == 9.0


def test_recent_missing_bar_stays_pending_for_retry(tmp_sqlite_db):
    """Just past the horizon with no bar yet is a timing gap, not a dead name."""
    _priced_filing(tmp_sqlite_db)
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=False):
        result = outcomes.mark_due_outcomes(as_of=date(2026, 1, 13))

    assert result[7]["pending"] == 1 and result[7]["gave_up"] == 0
    assert database.get_signal_outcomes()[0]["status"] == database.OUTCOME_OK


def test_long_silence_stops_being_retried(tmp_sqlite_db):
    """A month past the horizon with no close, from a source that answered,
    means the name stopped trading."""
    _priced_filing(tmp_sqlite_db)
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=False), \
         patch("outcomes._answer_is_final", return_value=True):
        result = outcomes.mark_due_outcomes(as_of=date(2026, 3, 1))

    assert result[7]["gave_up"] == 1
    assert database.get_signal_outcomes()[0]["status"] == database.OUTCOME_NO_PRICE


def test_ticker_that_went_dark_is_flagged_delisted(tmp_sqlite_db):
    _priced_filing(tmp_sqlite_db)
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=True):
        result = outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))

    assert result[7]["gave_up"] == 1
    assert database.get_signal_outcomes()[0]["status"] == database.OUTCOME_DELISTED


def test_mark_benchmark_uses_the_stocks_bar_date(tmp_sqlite_db):
    _priced_filing(tmp_sqlite_db)
    calls = []

    def stub(ticker, target, **_kw):
        calls.append((ticker.upper(), str(target)[:10]))
        if ticker.upper() == "AAPL":
            return ("2026-01-13", 9.0)   # 01-12 was a holiday
        return ("2026-01-13", 505.0)

    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))

    assert ("SPY", "2026-01-13") in calls, f"benchmark priced off the stock's bar: {calls}"


def test_full_job_captures_then_marks_in_one_pass(tmp_sqlite_db):
    """A filing ingested today should get its baseline in the same run rather
    than waiting a day."""
    _insert(filed_date="2026-01-05")
    stub = _closes({
        ("AAPL", "2026-01-05"): ("2026-01-05", 10.0),
        ("SPY", "2026-01-05"): ("2026-01-05", 500.0),
        ("AAPL", "2026-01-12"): ("2026-01-12", 9.0),
        ("SPY", "2026-01-12"): ("2026-01-12", 505.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        result = outcomes.run_outcome_job(as_of=date(2026, 1, 20))

    assert result["baselines"]["priced"] == 1
    assert result["marks"][7]["marked"] == 1


# ---------- an unreachable price source must not burn the archive ----------

def test_unreachable_source_does_not_write_filings_off(tmp_sqlite_db):
    """The bug this guard exists for: a network outage returns no price for
    every ticker alike. Recording that as 'unpriceable' would delete the
    archive from the scorecard in one bad run, and the status would stop those
    rows from ever being picked up again."""
    _insert()
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=False), \
         patch("outcomes._answer_is_final", return_value=False):
        stats = outcomes.capture_baselines()

    assert stats["unpriced"] == 0
    assert stats["skipped"] == 1
    assert database.get_signal_outcomes() == [], "filing was written off on a transient failure"
    assert len(database.get_filings_needing_outcome_baseline()) == 1, "row is no longer retryable"


def test_unreachable_source_does_not_give_up_on_a_horizon(tmp_sqlite_db):
    """Same guard on the marking side — being long overdue is not enough to
    give up if we never actually reached the source."""
    _priced_filing(tmp_sqlite_db)
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=False), \
         patch("outcomes._answer_is_final", return_value=False):
        result = outcomes.mark_due_outcomes(as_of=date(2026, 3, 1))

    assert result[7]["gave_up"] == 0
    assert result[7]["pending"] == 1
    assert database.get_signal_outcomes()[0]["status"] == database.OUTCOME_OK


def test_answer_is_final_tracks_real_coverage(tmp_sqlite_db):
    """The helper must read actual fetch coverage, not just any stored row."""
    assert outcomes._answer_is_final("AAPL", "2026-01-05") is False
    database.upsert_price_history_meta("AAPL", "2026-01-01", "2026-01-31", status="ok")
    assert outcomes._answer_is_final("AAPL", "2026-01-05") is True
    # A span that stops short of the lookahead window is not a final answer.
    assert outcomes._answer_is_final("AAPL", "2026-01-28") is False
