"""The daily outcome job must not be hostage to the ingest step.

Horizons elapse on weekends and on days EDGAR fails — and a weekend is exactly
a day with no filings, so tying outcome scoring to a successful ingest would
skip it precisely when there is scoring work waiting.
"""
from unittest.mock import patch

import scheduler


def test_outcomes_run_when_edgar_returns_no_filings():
    with patch("scheduler.fetch_filings", return_value=[]), \
         patch("scheduler.create_backfill_run", return_value=1), \
         patch("scheduler.complete_backfill_run"), \
         patch("scheduler.score_signal_outcomes") as scored:
        scheduler.daily_fetch_job()
    assert scored.call_count == 1, "outcome scoring was skipped on a day with no filings"


def test_outcomes_run_when_the_edgar_fetch_fails():
    with patch("scheduler.fetch_filings", side_effect=RuntimeError("EDGAR 503")), \
         patch("scheduler.create_backfill_run", return_value=1), \
         patch("scheduler.complete_backfill_run"), \
         patch("scheduler.score_signal_outcomes") as scored:
        scheduler.daily_fetch_job()
    assert scored.call_count == 1, "outcome scoring was skipped after an EDGAR failure"


def test_outcomes_run_even_if_ingest_raises_unexpectedly():
    with patch("scheduler.create_backfill_run", side_effect=RuntimeError("db down")), \
         patch("scheduler.score_signal_outcomes") as scored:
        try:
            scheduler.daily_fetch_job()
        except RuntimeError:
            pass
    assert scored.call_count == 1


def test_a_price_source_outage_does_not_fail_the_scheduled_job():
    with patch("outcomes.run_outcome_job", side_effect=RuntimeError("yahoo down")):
        scheduler.score_signal_outcomes()  # must not raise
