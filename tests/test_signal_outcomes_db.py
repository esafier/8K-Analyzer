"""Tests for the signal_outcomes storage layer.

The scorecard is only worth building if this layer cannot quietly lose or
rewrite history, so the tests here concentrate on exactly that: refreshing a
snapshot must not blank horizon marks, a mark must never be overwritten, and
a horizon must not be marked before it has happened.
"""
import database


def _insert(accession, ticker="AAPL", filed_date="2026-01-05",
            verdict="DEEP_LOOK", direction="BEARISH", score=8,
            forfeited=1, successor=0, dep24=2, targets=0):
    database.insert_filing({
        "accession_no": accession,
        "company": "Test Co",
        "ticker": ticker,
        "cik": "0000001",
        "filed_date": filed_date,
        "item_codes": "5.02",
        "triage_verdict": verdict,
        "signal_direction": direction,
        "signal_score": score,
        "forfeited_comp": forfeited,
        "has_successor": successor,
        "has_market_targets": targets,
    })
    row = database.get_filing_by_accession(accession)
    filing_id = row["id"]
    if dep24 is not None:
        database.update_departure_history(filing_id, dep24, "[]")
    return filing_id


def _pending(**kw):
    """The one filing awaiting a baseline, as the picker returns it."""
    _insert(kw.pop("accession", "0000-01"), **kw)
    rows = database.get_filings_needing_outcome_baseline()
    assert len(rows) == 1
    return rows[0]


# ---------- picking filings to score ----------

def test_picks_up_scored_filings_with_a_ticker(tmp_sqlite_db):
    _insert("0000-01")
    rows = database.get_filings_needing_outcome_baseline()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    # CLAUDE.md: downstream uses .get(), so these must be real dicts.
    assert rows[0].get("triage_verdict") == "DEEP_LOOK"


def test_pass_verdicts_are_not_scored(tmp_sqlite_db):
    """PASS is the noise the scanner exists to discard — scoring it would
    triple the price fetches without telling us anything about signal quality."""
    _insert("0000-01", verdict="PASS")
    assert database.get_filings_needing_outcome_baseline() == []


def test_filings_without_a_ticker_are_skipped(tmp_sqlite_db):
    _insert("0000-01", ticker="")
    assert database.get_filings_needing_outcome_baseline() == []


def test_already_scored_filings_are_not_returned_again(tmp_sqlite_db):
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    assert database.get_filings_needing_outcome_baseline() == []


def test_verdict_filter_is_configurable(tmp_sqlite_db):
    _insert("0000-01", verdict="MONITOR")
    assert database.get_filings_needing_outcome_baseline(verdicts=("DEEP_LOOK",)) == []
    assert len(database.get_filings_needing_outcome_baseline(verdicts=("MONITOR",))) == 1


# ---------- the snapshot ----------

def test_baseline_stores_the_verdict_as_it_was(tmp_sqlite_db):
    """Prompts change. The scorecard must measure what the scanner actually
    claimed, not what today's prompt would say about the same filing."""
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)

    out = database.get_signal_outcomes()[0]
    assert out["verdict"] == "DEEP_LOOK"
    assert out["direction"] == "BEARISH"
    assert out["signal_score"] == 8
    assert out["forfeited_comp"] == 1
    assert out["has_successor"] == 0
    assert out["departure_count_24mo"] == 2
    assert out["baseline_close"] == 10.0
    assert out["baseline_spy"] == 500.0
    assert out["status"] == database.OUTCOME_OK


def test_refreshing_a_baseline_does_not_blank_existing_marks(tmp_sqlite_db):
    """The regression this design exists to prevent: an INSERT OR REPLACE here
    would wipe every horizon mark the marking job had already earned."""
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    database.set_outcome_mark(filing["id"], 7, 9.0, 505.0)

    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)

    out = database.get_signal_outcomes()[0]
    assert out["close_7d"] == 9.0, "horizon mark was destroyed by a snapshot refresh"
    assert out["spy_7d"] == 505.0
    assert out["marked_7d_at"] is not None


# ---------- marking ----------

def test_mark_records_both_closes(tmp_sqlite_db):
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    assert database.set_outcome_mark(filing["id"], 30, 8.0, 510.0) is True

    out = database.get_signal_outcomes()[0]
    assert out["close_30d"] == 8.0
    assert out["spy_30d"] == 510.0


def test_an_existing_mark_is_never_overwritten(tmp_sqlite_db):
    """A marked horizon is history. Re-pricing it later would rewrite the
    scorecard's past without anyone noticing."""
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    database.set_outcome_mark(filing["id"], 7, 9.0, 505.0)

    assert database.set_outcome_mark(filing["id"], 7, 1.0, 1.0) is False
    out = database.get_signal_outcomes()[0]
    assert out["close_7d"] == 9.0


def test_mark_with_a_missing_price_is_refused(tmp_sqlite_db):
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    assert database.set_outcome_mark(filing["id"], 7, None, 505.0) is False
    assert database.set_outcome_mark(filing["id"], 7, 9.0, None) is False
    assert database.get_signal_outcomes()[0]["close_7d"] is None


def test_unsupported_horizon_raises(tmp_sqlite_db):
    """Horizons are whitelisted because they are interpolated into SQL."""
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    for bad in (14, "7; DROP TABLE filings", None):
        try:
            database.set_outcome_mark(filing["id"], bad, 1.0, 1.0)
        except ValueError:
            continue
        raise AssertionError(f"horizon {bad!r} was accepted")


# ---------- due-for-marking selection ----------

def test_only_horizons_that_have_actually_passed_are_due(tmp_sqlite_db):
    filing = _pending(filed_date="2026-01-05")
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)

    # 2026-01-08 is three days after filing — the 7d horizon has not happened.
    assert database.get_outcomes_needing_mark(7, "2026-01-08") == []
    assert len(database.get_outcomes_needing_mark(7, "2026-01-20")) == 1


def test_marked_rows_drop_out_of_the_due_list(tmp_sqlite_db):
    filing = _pending(filed_date="2026-01-05")
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    database.set_outcome_mark(filing["id"], 7, 9.0, 505.0)
    assert database.get_outcomes_needing_mark(7, "2026-01-20") == []


def test_unpriced_rows_are_not_due_for_marking(tmp_sqlite_db):
    """No baseline means nothing to compare against — marking it is pointless."""
    filing = _pending(filed_date="2026-01-05")
    database.upsert_signal_outcome(filing, status=database.OUTCOME_NO_PRICE)
    assert database.get_outcomes_needing_mark(7, "2026-01-20") == []


def test_status_can_be_flagged_after_the_fact(tmp_sqlite_db):
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    database.set_outcome_status(filing["id"], database.OUTCOME_DELISTED)
    assert database.get_signal_outcomes()[0]["status"] == database.OUTCOME_DELISTED
    assert database.get_signal_outcomes(status=database.OUTCOME_OK) == []


def test_counts_split_priced_from_unpriced(tmp_sqlite_db):
    a = _pending(accession="0000-01")
    database.upsert_signal_outcome(a, "2026-01-05", 10.0, 500.0)
    _insert("0000-02", ticker="MSFT")
    b = [r for r in database.get_filings_needing_outcome_baseline()
         if r["accession_no"] == "0000-02"][0]
    database.upsert_signal_outcome(b, status=database.OUTCOME_NO_PRICE)

    assert database.count_signal_outcomes() == {"total": 2, "priced": 1, "unpriced": 1}


# ---------- clearing the database ----------

def test_clearing_filings_also_clears_their_outcomes(tmp_sqlite_db):
    """signal_outcomes is keyed on filing_id with no cascade. Survivors would
    show on the scorecard as calls against filings that no longer exist, with
    dead links, and every clear/repopulate cycle would duplicate the sample."""
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    assert len(database.get_signal_outcomes()) == 1

    database.clear_all_filings()

    assert database.get_signal_outcomes() == []
    assert database.get_filing_count() == 0


def test_clearing_filings_keeps_the_price_cache(tmp_sqlite_db):
    """Price history is keyed by ticker and date, not by filing, so it stays
    valid across a repopulate — refetching it every time would be waste."""
    database.upsert_closes("AAPL", {"2026-01-05": 10.0})
    database.upsert_price_history_meta("AAPL", "2026-01-01", "2026-01-31")

    database.clear_all_filings()

    assert database.get_cached_closes("AAPL", "2026-01-01", "2026-01-31") == {"2026-01-05": 10.0}
    assert database.get_price_history_meta("AAPL") is not None


def test_the_priced_count_agrees_with_the_scorecard(tmp_sqlite_db):
    """count_signal_outcomes feeds the backfill log and the scorecard's empty
    state. Counting by status would drop a filing that priced fine and only
    later went dark, disagreeing with what the scorecard itself reports."""
    filing = _pending()
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    database.set_outcome_status(filing["id"], database.OUTCOME_DELISTED)

    assert database.count_signal_outcomes() == {"total": 1, "priced": 1, "unpriced": 0}


def test_a_delisted_row_keeps_resolving_horizons_it_has_bars_for(tmp_sqlite_db):
    """The page claims a name is scored at every horizon it actually traded
    through. Gating the marking queue on status broke that claim: a symbol that
    went dark before the backfill ran would have its later horizons dropped even
    when the price cache still held real bars for them."""
    filing = _pending(filed_date="2026-01-05")
    database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
    database.set_outcome_status(filing["id"], database.OUTCOME_DELISTED)

    due = database.get_outcomes_needing_mark(30, "2026-06-01")
    assert len(due) == 1, "a delisted row with a baseline must still be markable"
    assert due[0]["filing_id"] == filing["id"]


def test_a_row_with_no_baseline_is_still_never_marked(tmp_sqlite_db):
    """Removing the status gate must not let unpriced rows into the queue."""
    filing = _pending(filed_date="2026-01-05")
    database.upsert_signal_outcome(filing, status=database.OUTCOME_NO_PRICE)
    assert database.get_outcomes_needing_mark(30, "2026-06-01") == []
