"""Tests for the unified analysis pipeline.

The property this file exists to protect: there is ONE analysis path. The
previous three copies drifted — the retry path silently skipped market-target
detection for months — so the tests here check that a filing analyzed through
the pipeline always comes out with the same field set, whichever caller asked.
"""
import json
from unittest.mock import patch

import pytest

import pipeline
import signals as signal_engine


def _facts(**overrides):
    base = {
        "relevant": True,
        "reasoning": "One departure identified.",
        "top_level_category": "Management Change",
        "subcategories": ["CFO Departure"],
        "is_complex": False,
        "narrative_summary": None,
        "departures": [{
            "name": "Jane Doe", "title": "Chief Financial Officer", "role_class": "CFO",
            "effective_date": "2026-09-02", "effective_immediately": True,
            "days_notice": 0, "stated_reason": "resigned",
            "is_retirement": False, "is_merger_related": False,
            "successor_named": False, "successor_info": "search underway",
            "comp_impact": "forfeits all unvested RSUs (~$4.2M)",
            "forfeiture_flag": "forfeited",
        }],
        "appointments": [],
        "comp_events": [],
        "insider_transactions": [],
        "other": [],
        "filing_flags": {},
        "_tokens_in": 900, "_tokens_out": 300,
    }
    base.update(overrides)
    return base


def _filing(**overrides):
    base = {
        "company": "Acme Corp", "ticker": "ACME", "cik": "0001234567",
        "filed_date": "2026-09-02", "accession_no": "0001-26-000001",
        "item_codes": "5.02", "raw_text": "CFO resigned effective immediately.",
    }
    base.update(overrides)
    return base


def _judgment(**overrides):
    base = {
        "score": 8, "verdict": "DEEP_LOOK", "direction": "BEARISH",
        "thesis": "CFO Jane Doe resigns immediately, forfeiting ~$4.2M unvested.",
        "why": ["No successor named.", "Second finance exit in 14 months."],
        "anti_thesis": "Could be a personal matter the filing declines to detail.",
        "_tokens_in": 2000, "_tokens_out": 400,
    }
    base.update(overrides)
    return base


@pytest.fixture
def no_context(monkeypatch):
    """Context is exercised in test_context.py; here it's a constant."""
    monkeypatch.setattr(pipeline, "_build_context",
                        lambda filing: {"price": 10.0, "market_cap": 310_000_000})


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def test_analysis_produces_signals_and_a_ranked_verdict(no_context):
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()):
        result = pipeline.analyze_filing(_filing())

    assert result.relevant is True
    assert "FORFEITURE_EXIT" in result.signal_types
    assert result.fields["signal_score"] == 8
    assert result.fields["triage_verdict"] == "DEEP_LOOK"
    assert result.fields["signal_direction"] == "BEARISH"
    assert "4.2M" in result.fields["top_signal"]


def test_legacy_columns_are_populated_so_the_old_ui_keeps_working(no_context):
    """The rebuild ships without touching the dashboard, watchlist, detail
    page, or email composer — which only works if these columns stay filled."""
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()):
        result = pipeline.analyze_filing(_filing())

    fields = result.fields
    assert fields["auto_category"] == "Management Change"
    assert json.loads(fields["auto_subcategory"]) == ["CFO Departure"]
    assert fields["summary"]
    assert fields["forfeited_comp"] == 1
    assert fields["has_successor"] == 0
    assert fields["departure_count"] == 1

    structured = json.loads(fields["structured_summary"])
    assert structured["departures"][0]["name"] == "Jane Doe"
    assert "has_market_targets" in structured


def test_market_targets_are_detected_on_every_path(no_context):
    """The bug this rebuild fixes by construction: one path used to skip
    market-target detection, so rescued filings never got the hurdle flag."""
    facts = _facts(
        departures=[],
        comp_events=[{
            "executive": "CEO Sam Chief", "role_class": "CEO", "grant_type": "PSUs",
            "grant_value": "$10M", "hurdle_prices": [25], "vesting_years": 3,
            "market_based_targets": {"stock_price": "$25", "market_cap": None, "tsr": None},
            "is_annual_cycle": True,
        }],
    )
    with patch("llm.classify_and_summarize", return_value=facts), \
         patch("pipeline._judge", return_value=None):
        result = pipeline.analyze_filing(_filing())

    assert result.fields["has_market_targets"] == 1
    assert json.loads(result.fields["structured_summary"])["has_market_targets"] is True
    assert "HURDLE_CONVICTION" in result.signal_types


def test_signals_and_context_are_stored_for_later_audit(no_context):
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()):
        result = pipeline.analyze_filing(_filing())

    stored = json.loads(result.fields["signals_json"])
    assert stored[0]["type"] == "FORFEITURE_EXIT"
    assert "evidence" in stored[0]
    assert json.loads(result.fields["context_json"])["price"] == 10.0
    assert result.fields["pipeline_version"]
    assert result.fields["price_at_ingest"] == 10.0


# ---------------------------------------------------------------------------
# The judge gate — the thing that keeps the daily bill near a dollar
# ---------------------------------------------------------------------------

def test_judge_runs_on_a_real_signal(no_context):
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()) as mock_judge:
        pipeline.analyze_filing(_filing())
    assert mock_judge.called


def test_judge_is_skipped_when_nothing_fired(no_context):
    quiet = _facts(departures=[], subcategories=[], filing_flags={"is_annual_meeting_only": True})
    with patch("llm.classify_and_summarize", return_value=quiet), \
         patch("pipeline._judge") as mock_judge:
        result = pipeline.analyze_filing(_filing())

    assert not mock_judge.called
    assert result.fields["triage_verdict"] == "PASS"
    assert result.fields["signal_score"] == 0


def test_allow_judge_false_never_spends(no_context):
    """Used by the backtest dry-run, which must be able to count candidates
    without paying for them."""
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge") as mock_judge:
        result = pipeline.analyze_filing(_filing(), allow_judge=False)

    assert not mock_judge.called
    assert result.fields["signal_score"] > 0  # still ranked by detectors


def test_unjudged_rows_cannot_outrank_judged_ones(no_context):
    """A detector-only score is capped at 6 so a filing no strong model read
    never sorts above one that was read and rated 7+."""
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=None):
        result = pipeline.analyze_filing(_filing())

    assert result.fields["signal_score"] <= 6
    assert result.fields["triage_verdict"] == "MONITOR"


# ---------------------------------------------------------------------------
# Failure handling — a filing is never lost
# ---------------------------------------------------------------------------

def test_extraction_failure_is_reported_not_raised(no_context):
    with patch("llm.classify_and_summarize", return_value=None):
        result = pipeline.analyze_filing(_filing())
    assert result.error == "extraction_failed"
    assert result.fields == {}


def test_empty_text_is_reported_without_calling_the_model():
    with patch("llm.classify_and_summarize") as mock_llm:
        result = pipeline.analyze_filing(_filing(raw_text=""))
    assert result.error == "no_text"
    assert not mock_llm.called


def test_irrelevant_filing_carries_its_reason(no_context):
    facts = _facts(relevant=False, relevant_reason="Pure earnings release.")
    with patch("llm.classify_and_summarize", return_value=facts):
        result = pipeline.analyze_filing(_filing())
    assert result.relevant is False
    assert result.fields["relevant_reason"] == "Pure earnings release."


def test_context_failure_still_yields_text_only_signals(monkeypatch):
    """If SEC and the market APIs are down, the filing is still worth
    extracting — forfeiture doesn't need a share price to be visible."""
    def boom(filing):
        raise RuntimeError("SEC down")

    monkeypatch.setattr("context.build_context", boom)
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=None):
        result = pipeline.analyze_filing(_filing())

    assert result.context == {}
    assert "FORFEITURE_EXIT" in result.signal_types


def test_judge_failure_falls_back_to_detectors(no_context):
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("judge.judge", side_effect=RuntimeError("api down")):
        result = pipeline.analyze_filing(_filing())

    assert result.judgment is None
    assert result.fields["triage_verdict"] == "MONITOR"
    assert result.fields["top_signal"]


# ---------------------------------------------------------------------------
# Urgency + token accounting
# ---------------------------------------------------------------------------

def test_top_severity_signal_flags_urgent(no_context):
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=None):
        result = pipeline.analyze_filing(_filing())
    assert result.fields["urgent"] == 1


def test_mild_filing_is_not_urgent(no_context):
    facts = _facts(departures=[{
        "name": "Bob", "title": "Director", "role_class": "DIRECTOR",
        "days_notice": 30, "successor_named": True, "forfeiture_flag": "not_disclosed",
        "is_retirement": False, "is_merger_related": False,
    }])
    with patch("llm.classify_and_summarize", return_value=facts), \
         patch("pipeline._judge", return_value=None):
        result = pipeline.analyze_filing(_filing())
    assert result.fields["urgent"] == 0


def test_tokens_from_both_calls_are_summed(no_context):
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()):
        result = pipeline.analyze_filing(_filing())
    assert result.tokens_in == 900 + 2000
    assert result.tokens_out == 300 + 400


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_persist_writes_fields_to_the_row(tmp_sqlite_db, no_context):
    import database
    database.insert_filing(_filing())
    filing_id = database.get_filing_by_accession("0001-26-000001")["id"]

    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()):
        result = pipeline.analyze_filing(_filing())
    pipeline.persist(filing_id, result)

    row = database.get_filing_by_id(filing_id)
    assert row["signal_score"] == 8
    assert row["triage_verdict"] == "DEEP_LOOK"
    assert "FORFEITURE_EXIT" in row["signal_types"]


def test_persist_snapshots_the_previous_verdict_once(tmp_sqlite_db, no_context):
    """Re-scoring must stay reversible, and the old and new judgments have to
    be comparable rather than one silently replacing the other."""
    import database
    database.insert_filing(_filing())
    filing_id = database.get_filing_by_accession("0001-26-000001")["id"]
    database.update_filing_fields(filing_id, triage_verdict="MONITOR", signal_score=5,
                                  signal_direction="NEUTRAL", top_signal="old line")

    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()):
        result = pipeline.analyze_filing(_filing())

    pipeline.persist(filing_id, result)
    snapshot = json.loads(database.get_filing_by_id(filing_id)["legacy_triage_json"])
    assert snapshot["signal_score"] == 5
    assert snapshot["top_signal"] == "old line"

    # A second pass must not overwrite the original snapshot with the new one
    pipeline.persist(filing_id, result)
    again = json.loads(database.get_filing_by_id(filing_id)["legacy_triage_json"])
    assert again["signal_score"] == 5


def test_apply_to_filing_prepares_a_row_for_insert(no_context):
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()):
        result = pipeline.analyze_filing(_filing())

    filing = pipeline.apply_to_filing(_filing(), result)
    assert filing["source"] == "8-K"
    assert filing["signal_score"] == 8
    assert filing["company"] == "Acme Corp"   # original keys survive


def test_every_field_written_is_an_allowed_column(tmp_sqlite_db, no_context):
    """build_fields() and the DB allow-list must not drift apart — a typo here
    would raise mid-backfill."""
    import database
    with patch("llm.classify_and_summarize", return_value=_facts()), \
         patch("pipeline._judge", return_value=_judgment()):
        result = pipeline.analyze_filing(_filing())

    assert set(result.fields) <= database.UPDATABLE_FILING_FIELDS
