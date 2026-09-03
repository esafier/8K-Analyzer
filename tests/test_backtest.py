"""Tests for the backtest tool.

The property worth protecting: a dry run costs nothing and tells the truth,
and a real run refuses to blow past its budget. Both matter because this is
the one command in the project that can spend real money in bulk.
"""
from unittest.mock import patch

import backtest
import database


def _add(accession, filed_date="2026-09-01", raw_text="8-K body text",
         source="8-K", pipeline_version=None):
    database.insert_filing({
        "accession_no": accession, "company": "Acme Corp", "ticker": "ACME",
        "cik": "0001234567", "filed_date": filed_date, "item_codes": "5.02",
        "summary": "s", "auto_category": "Management Change",
        "filing_url": "https://sec.gov/i.htm", "raw_text": raw_text,
        "source": source, "pipeline_version": pipeline_version,
    })
    return database.get_filing_by_accession(accession)["id"]


def test_candidates_require_stored_text(tmp_sqlite_db):
    _add("a-1", raw_text="real text")
    _add("a-2", raw_text="")
    assert [r["accession_no"] for r in backtest.candidates(days=3650)] == ["a-1"]


def test_candidates_exclude_form4_rows(tmp_sqlite_db):
    """Form 4 rows have no 8-K document to re-extract from."""
    _add("a-1")
    _add("a-2", source="FORM4")
    assert [r["accession_no"] for r in backtest.candidates(days=3650)] == ["a-1"]


def test_candidates_respect_the_window(tmp_sqlite_db):
    from datetime import datetime, timedelta
    recent = datetime.now().strftime("%Y-%m-%d")
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    _add("a-1", filed_date=recent)
    _add("a-2", filed_date=old)
    assert len(backtest.candidates(days=30)) == 1
    assert len(backtest.candidates(days=365)) == 2


def test_only_unanalyzed_skips_the_current_generation(tmp_sqlite_db):
    """Re-running the same pipeline version over the same rows buys nothing
    but costs full price."""
    from config import PIPELINE_VERSION
    _add("a-1", pipeline_version=PIPELINE_VERSION)
    _add("a-2", pipeline_version=None)
    _add("a-3", pipeline_version="v3-old")

    accessions = {r["accession_no"] for r in
                  backtest.candidates(days=3650, only_unanalyzed=True)}
    assert accessions == {"a-2", "a-3"}


def test_estimate_scales_with_filing_count():
    small = backtest.estimate([{}] * 10)
    large = backtest.estimate([{}] * 100)
    assert large["total_usd"] > small["total_usd"]
    assert large["judge_calls"] > small["judge_calls"]
    # The judge is the expensive half — that's why the gate exists.
    assert large["judging_usd"] > large["extraction_usd"]


def test_estimate_of_nothing_is_free():
    assert backtest.estimate([])["total_usd"] == 0


def test_dry_run_analyzes_nothing(tmp_sqlite_db):
    _add("a-1")
    with patch("pipeline.analyze_filing") as mock_analyze:
        result = backtest.run(days=3650, dry_run=True)
    assert not mock_analyze.called
    assert result["filings"] == 1


def test_run_truncates_to_the_budget_rather_than_aborting(tmp_sqlite_db, capsys):
    """A partial backtest over the most recent filings is far more useful
    than no backtest — and the recent ones are what the user will look at."""
    for i in range(20):
        _add(f"a-{i}")

    calls = []

    def fake_analyze(filing, **kwargs):
        calls.append(filing["accession_no"])
        from pipeline import AnalysisResult
        return AnalysisResult(relevant=True, error="extraction_failed")

    with patch("pipeline.analyze_filing", side_effect=fake_analyze):
        backtest.run(days=3650, budget=0.01, workers=1)

    assert 0 < len(calls) < 20
    assert "exceeds the $0.01 budget" in capsys.readouterr().out


def test_results_are_persisted(tmp_sqlite_db):
    filing_id = _add("a-1")

    facts = {
        "relevant": True, "top_level_category": "Management Change",
        "subcategories": ["CFO Departure"], "reasoning": "one event",
        "departures": [{"name": "Jane Doe", "title": "CFO", "role_class": "CFO",
                        "effective_immediately": True, "days_notice": 0,
                        "stated_reason": "resigned", "is_retirement": False,
                        "is_merger_related": False, "successor_named": False,
                        "successor_info": "search underway",
                        "forfeiture_flag": "forfeited", "comp_impact": "forfeits $4M"}],
        "appointments": [], "comp_events": [], "insider_transactions": [],
        "other": [], "filing_flags": {}, "_tokens_in": 100, "_tokens_out": 50,
    }
    with patch("llm.classify_and_summarize", return_value=facts), \
         patch("pipeline._build_context", return_value={"price": 10.0}), \
         patch("pipeline._judge", return_value=None):
        stats = backtest.run(days=3650, workers=1)

    assert stats["analyzed"] == 1
    row = database.get_filing_by_id(filing_id)
    assert "FORFEITURE_EXIT" in row["signal_types"]
    assert row["pipeline_version"]


def test_a_failing_filing_does_not_stop_the_run(tmp_sqlite_db):
    for i in range(3):
        _add(f"a-{i}")

    def flaky(filing, **kwargs):
        if filing["accession_no"] == "a-1":
            raise RuntimeError("model exploded")
        from pipeline import AnalysisResult
        return AnalysisResult(relevant=True, error="extraction_failed")

    with patch("pipeline.analyze_filing", side_effect=flaky):
        stats = backtest.run(days=3650, workers=1)

    assert stats["failed"] == 3  # one worker error + two extraction failures


def test_backtest_never_overwrites_the_original_market_snapshot(tmp_sqlite_db):
    """price_at_ingest exists so a signal can be audited against the price it
    was formed at. A 90-day backtest builds context from TODAY's price, so
    letting it write would replace what the analyst saw with a number from
    months later — precisely the look-ahead contamination the column prevents,
    and it would quietly corrupt any later outcome study."""
    filing_id = _add("a-1")
    database.update_filing_fields(filing_id, price_at_ingest=8.00,
                                  market_cap_at_ingest=200_000_000)

    facts = {
        "relevant": True, "top_level_category": "Management Change",
        "subcategories": ["CFO Departure"], "reasoning": "one event",
        "departures": [{"name": "Jane Doe", "title": "CFO", "role_class": "CFO",
                        "effective_immediately": True, "days_notice": 0,
                        "stated_reason": "resigned", "is_retirement": False,
                        "is_merger_related": False, "successor_named": False,
                        "successor_info": "search underway",
                        "forfeiture_flag": "forfeited", "comp_impact": "forfeits $4M"}],
        "appointments": [], "comp_events": [], "insider_transactions": [],
        "other": [], "filing_flags": {}, "_tokens_in": 100, "_tokens_out": 50,
    }
    with patch("llm.classify_and_summarize", return_value=facts), \
         patch("pipeline._build_context", return_value={"price": 25.00,
                                                        "market_cap": 900_000_000}), \
         patch("pipeline._judge", return_value=None):
        backtest.run(days=3650, workers=1)

    row = database.get_filing_by_id(filing_id)
    assert row["price_at_ingest"] == 8.00
    assert row["market_cap_at_ingest"] == 200_000_000


def test_a_first_analysis_does_record_the_snapshot(tmp_sqlite_db):
    filing_id = _add("a-1")
    facts = {"relevant": True, "top_level_category": "Other", "subcategories": [],
             "reasoning": "", "departures": [], "appointments": [], "comp_events": [],
             "insider_transactions": [], "other": [], "filing_flags": {},
             "_tokens_in": 10, "_tokens_out": 5}
    with patch("llm.classify_and_summarize", return_value=facts), \
         patch("pipeline._build_context", return_value={"price": 25.00}), \
         patch("pipeline._judge", return_value=None):
        backtest.run(days=3650, workers=1)

    assert database.get_filing_by_id(filing_id)["price_at_ingest"] == 25.00
