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


def _no_baselines():
    """capture_baselines' shape for an empty queue."""
    return {"considered": 0, "priced": 0, "unpriced": 0, "skipped": 0, "skipped_ids": []}


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
        stats = outcomes.capture_baselines()
    assert (stats["priced"], stats["unpriced"], stats["skipped"]) == (1, 0, 0)

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
    out = database.get_signal_outcomes()[0]
    # Settled for THIS horizon only — the row itself is untouched, because the
    # name may be halted rather than dead and later horizons must stay open.
    assert out["marked_7d_at"] is not None
    assert out["close_7d"] is None
    assert out["status"] == database.OUTCOME_OK
    assert database.get_outcomes_needing_mark(7, "2026-03-01") == []


def test_ticker_that_went_dark_is_flagged_delisted(tmp_sqlite_db):
    _priced_filing(tmp_sqlite_db)
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=True):
        result = outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))

    assert result[7]["gave_up"] == 1
    out = database.get_signal_outcomes()[0]
    assert out["status"] == database.OUTCOME_DELISTED
    assert out["marked_7d_at"] is not None, "horizon must be settled so it stops being due"


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


# ---------- the backfill must actually finish ----------

def test_backfill_keeps_marking_until_every_due_row_is_drained(tmp_sqlite_db):
    """get_outcomes_needing_mark applies a LIMIT, so one marking call leaves a
    large archive part-scored while still printing 'Done'."""
    calls = {"n": 0}
    empty = {h: {"marked": 0, "pending": 0, "gave_up": 0} for h in database.OUTCOME_HORIZONS}

    def fake_mark(**_kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return {h: {"marked": 5, "pending": 40, "gave_up": 0}
                    for h in database.OUTCOME_HORIZONS}
        return empty

    with patch("outcomes.capture_baselines", return_value=_no_baselines()), \
         patch("outcomes.mark_due_outcomes", side_effect=fake_mark):
        result = outcomes.run_outcome_backfill(verbose=False)

    assert calls["n"] == 3, "stopped marking while rows were still being drained"
    assert result["marks"][7]["marked"] == 10
    # pending is a snapshot of what is left, not a sum across rounds
    assert result["marks"][7]["pending"] == 0


def test_backfill_marking_respects_the_batch_cap(tmp_sqlite_db):
    """A source that always reports progress must not loop forever."""
    def always_progress(**_kw):
        return {h: {"marked": 1, "pending": 99, "gave_up": 0}
                for h in database.OUTCOME_HORIZONS}

    with patch("outcomes.capture_baselines", return_value=_no_baselines()), \
         patch("outcomes.mark_due_outcomes", side_effect=always_progress):
        result = outcomes.run_outcome_backfill(verbose=False, max_batches=4)

    assert result["marks"][7]["marked"] == 4


# ---------- one dead horizon must not destroy the others ----------

def test_a_failed_horizon_leaves_earlier_marks_intact(tmp_sqlite_db):
    """The regression this design exists to prevent: a stock halted around its
    30-day target still has a perfectly real 7-day result, and a row-wide status
    flip would have removed it from the 7-day scorecard too."""
    filing_id = _priced_filing(tmp_sqlite_db, filed_date="2026-01-05")

    good_7d = _closes({
        ("AAPL", "2026-01-12"): ("2026-01-12", 9.0),
        ("SPY", "2026-01-12"): ("2026-01-12", 505.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=good_7d):
        outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))
    assert database.get_signal_outcomes()[0]["close_7d"] == 9.0

    # Now the 30-day horizon comes due and the stock has no bars at all.
    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=False), \
         patch("outcomes._answer_is_final", return_value=True):
        result = outcomes.mark_due_outcomes(as_of=date(2026, 4, 1))

    assert result[30]["gave_up"] == 1
    out = database.get_signal_outcomes()[0]
    assert out["close_7d"] == 9.0, "a valid 7-day mark was destroyed by a 30-day failure"
    assert out["close_30d"] is None
    assert out["status"] == database.OUTCOME_OK


def test_a_delisting_preserves_the_horizons_already_marked(tmp_sqlite_db):
    filing_id = _priced_filing(tmp_sqlite_db, filed_date="2026-01-05")
    good_7d = _closes({
        ("AAPL", "2026-01-12"): ("2026-01-12", 9.0),
        ("SPY", "2026-01-12"): ("2026-01-12", 505.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=good_7d):
        outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))

    with patch("outcomes.get_close_on_or_after", side_effect=_closes({})), \
         patch("outcomes._ticker_is_gone", return_value=True):
        outcomes.mark_due_outcomes(as_of=date(2026, 4, 1))

    out = database.get_signal_outcomes()[0]
    assert out["status"] == database.OUTCOME_DELISTED
    assert out["close_7d"] == 9.0, "the horizons it actually traded through were erased"


# ---------- the benchmark must land on the stock's own bar ----------

def test_baseline_rejects_a_benchmark_from_a_different_day(tmp_sqlite_db):
    """get_close_on_or_after can roll forward up to ten days. Accepting that
    silently would measure the stock and SPY over different windows — the exact
    same-window guarantee the excess-return number rests on."""
    _insert(filed_date="2026-01-05")

    def stub(ticker, target, **_kw):
        if ticker.upper() == "AAPL":
            return ("2026-01-05", 10.0)
        return ("2026-01-09", 500.0)   # SPY rolled forward four days

    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        stats = outcomes.capture_baselines()

    assert stats["priced"] == 0
    assert stats["skipped"] == 1
    assert database.get_signal_outcomes() == [], "windows were allowed to diverge"


def test_mark_rejects_a_benchmark_from_a_different_day(tmp_sqlite_db):
    _priced_filing(tmp_sqlite_db, filed_date="2026-01-05")

    def stub(ticker, target, **_kw):
        if ticker.upper() == "AAPL":
            return ("2026-01-12", 9.0)
        return ("2026-01-15", 505.0)   # SPY rolled forward three days

    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        result = outcomes.mark_due_outcomes(as_of=date(2026, 1, 20))

    assert result[7]["marked"] == 0
    assert result[7]["pending"] == 1
    assert database.get_signal_outcomes()[0]["close_7d"] is None


def test_matching_benchmark_bar_is_accepted(tmp_sqlite_db):
    """The guard must not reject the normal case."""
    _insert(filed_date="2026-01-05")
    stub = _closes({
        ("AAPL", "2026-01-05"): ("2026-01-05", 10.0),
        ("SPY", "2026-01-05"): ("2026-01-05", 500.0),
    })
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        assert outcomes.capture_baselines()["priced"] == 1


# ---------- failures at the front must not hide the rest of the archive ----------

def test_backfill_steps_past_rows_it_could_not_price(tmp_sqlite_db):
    """A transiently skipped filing gets no outcome row, so it comes back at the
    front of the next batch. Without stepping past it, a run of failures at the
    head would hide every older priceable filing behind it and the backfill
    would report completion having done almost nothing."""
    seen = []

    def fake_capture(limit=200, verdicts=None, exclude_ids=None):
        seen.append(sorted(exclude_ids or []))
        if len(seen) == 1:
            return {"considered": 2, "priced": 0, "unpriced": 0,
                    "skipped": 2, "skipped_ids": [1, 2]}
        if len(seen) == 2:
            # Only reachable because batch 1's failures were excluded.
            return {"considered": 1, "priced": 1, "unpriced": 0,
                    "skipped": 0, "skipped_ids": []}
        return _no_baselines()

    with patch("outcomes.capture_baselines", side_effect=fake_capture), \
         patch("outcomes.mark_due_outcomes", return_value={}):
        result = outcomes.run_outcome_backfill(verbose=False)

    assert seen[0] == []
    assert seen[1] == [1, 2], "failed rows were handed back instead of stepped past"
    assert result["baselines"]["priced"] == 1, "never reached the priceable filing"


def test_backfill_stops_when_failures_look_systemic(tmp_sqlite_db):
    """Hundreds of failures means the price source is down, not that the data is
    patchy. Walking the whole archive recording nothing helps nobody, and the
    exclusion list would grow past SQL parameter limits."""
    counter = {"n": 0}

    def always_skip(limit=200, verdicts=None, exclude_ids=None):
        counter["n"] += 1
        base = (counter["n"] - 1) * 100
        ids = list(range(base, base + 100))
        return {"considered": 100, "priced": 0, "unpriced": 0,
                "skipped": 100, "skipped_ids": ids}

    with patch("outcomes.capture_baselines", side_effect=always_skip), \
         patch("outcomes.mark_due_outcomes", return_value={}):
        outcomes.run_outcome_backfill(verbose=False, max_batches=100)

    assert counter["n"] <= (outcomes.MAX_SKIPPED_BEFORE_ABORT // 100) + 1


def test_skipped_ids_are_reported_so_the_caller_can_advance(tmp_sqlite_db):
    _insert("0000-01", ticker="AAPL")
    filing_id = database.get_filing_by_accession("0000-01")["id"]

    # Stock prices, benchmark does not — a skip, not a write-off.
    stub = _closes({("AAPL", "2026-01-05"): ("2026-01-05", 10.0)})
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        stats = outcomes.capture_baselines()

    assert stats["skipped_ids"] == [filing_id]
    assert stats["considered"] == 1

    # And excluding it makes the queue look empty.
    with patch("outcomes.get_close_on_or_after", side_effect=stub):
        again = outcomes.capture_baselines(exclude_ids=[filing_id])
    assert again["considered"] == 0


# ---------- two outcome runs must not interleave ----------

def test_a_second_backfill_refuses_to_start_while_one_is_running(tmp_sqlite_db):
    """Pressing the button twice used to launch two workers that could each
    fetch a different slice of the same ticker."""
    started = []

    def slow_capture(**_kw):
        started.append(1)
        # Re-entry attempt from "another request" while this run holds the lock.
        assert outcomes.run_outcome_backfill(verbose=False) is None
        return _no_baselines()

    with patch("outcomes.capture_baselines", side_effect=slow_capture), \
         patch("outcomes.mark_due_outcomes", return_value={}):
        result = outcomes.run_outcome_backfill(verbose=False)

    assert result is not None, "the first run should have completed"
    assert len(started) == 1, "a second worker ran concurrently"


def test_the_lock_is_released_after_a_failed_run(tmp_sqlite_db):
    """A crash must not wedge the lock and block every future run."""
    with patch("outcomes.capture_baselines", side_effect=RuntimeError("boom")), \
         patch("outcomes.mark_due_outcomes", return_value={}):
        try:
            outcomes.run_outcome_backfill(verbose=False)
        except RuntimeError:
            pass

    with patch("outcomes.capture_baselines", return_value=_no_baselines()), \
         patch("outcomes.mark_due_outcomes", return_value={}):
        assert outcomes.run_outcome_backfill(verbose=False) is not None


def test_the_scheduled_job_also_refuses_to_overlap(tmp_sqlite_db):
    """The same hazard exists between the daily job and a manual backfill."""
    def reentrant(**_kw):
        assert outcomes.run_outcome_job() is None
        return _no_baselines()

    with patch("outcomes.capture_baselines", side_effect=reentrant), \
         patch("outcomes.mark_due_outcomes", return_value={}):
        assert outcomes.run_outcome_job() is not None
