"""Tests for the near-miss path.

Keyword failures on executive-relevant item codes (5.02/1.01/1.02) still get
a model look, because a hand-written keyword list cannot cover how differently
companies phrase the same event. 8.01-only misses stay dropped: "Other Events"
is the highest-volume, lowest-hit-rate item, and reviewing every keyword miss
there would multiply the daily bill for almost no recall.

The relevance gate is what keeps the rescued filings from becoming noise.
"""
from unittest.mock import patch

import pytest


def _meta(items, accession="0001-26-000042"):
    return [{
        "accession_no": accession,
        "company": "Oddly Worded Corp", "ticker": "ODD", "cik": "123",
        "filed_date": "2026-06-02", "item_codes": ",".join(items),
        "filing_url": "https://sec.gov/index.htm",
        "items_list": items,
    }]


def _fetch_no_keywords(url, cik, accession):
    """Text that matches none of the KEYWORD_CATEGORIES entries."""
    return ("The registrant entered into an arrangement regarding its principal "
            "financial figurehead."), "https://sec.gov/doc.htm"


def _relevant_response():
    """v4 extraction output — an abrupt CFO exit with no successor."""
    return {
        "relevant": True, "relevant_reason": None,
        "reasoning": "CFO transition phrased unusually.",
        "top_level_category": "Management Change",
        "subcategories": ["CFO Departure"],
        "is_complex": False, "narrative_summary": None,
        "departures": [{"name": "A. Person", "title": "CFO", "role_class": "CFO",
                        "effective_immediately": True, "days_notice": 0,
                        "stated_reason": "resigned", "is_retirement": False,
                        "is_merger_related": False, "successor_named": False,
                        "successor_info": "search underway",
                        "forfeiture_flag": "not_disclosed"}],
        "appointments": [], "comp_events": [], "insider_transactions": [],
        "other": [], "filing_flags": {},
        "_tokens_in": 100, "_tokens_out": 50,
    }


@pytest.fixture(autouse=True)
def _stub_analysis(monkeypatch):
    """Context and judgment have their own suites; here they are constants so
    the near-miss policy is what's under test."""
    monkeypatch.setattr("pipeline._build_context", lambda filing: {})
    monkeypatch.setattr("pipeline._judge", lambda *a, **k: None)


def _filter(metadata, fetch=_fetch_no_keywords):
    from filter import filter_filings
    return filter_filings(metadata, fetch_text_func=fetch,
                          apply_universe=False, skip_existing=False)


def test_non_502_keyword_failure_still_reaches_the_model():
    """A 1.01-only filing with zero keyword hits used to be dropped unseen."""
    with patch("llm.classify_and_summarize", return_value=_relevant_response()) as mock_llm:
        result = _filter(_meta(["1.01"]))

    assert mock_llm.called
    assert len(result) == 1
    # An abrupt CFO exit with no successor is real signal — it must not be
    # ranked PASS just because the keyword list missed the phrasing.
    assert result[0]["triage_verdict"] == "MONITOR"
    assert "ABRUPT_CSUITE_EXIT" in result[0]["signal_types"]


def test_non_502_keyword_failure_rejected_by_the_model_is_dropped():
    rejection = {"relevant": False, "relevant_reason": "Routine commercial contract.",
                 "_tokens_in": 100, "_tokens_out": 20}
    with patch("llm.classify_and_summarize", return_value=rejection):
        assert _filter(_meta(["1.01"])) == []


def test_keywordless_non_502_near_miss_dropped_when_extraction_fails():
    """No keywords and no verdict is zero evidence of relevance."""
    with patch("llm.classify_and_summarize", return_value=None):
        assert _filter(_meta(["1.01"])) == []


def test_502_near_miss_still_kept_when_extraction_fails():
    """5.02 filings keep their historical auto-pass fallback."""
    with patch("llm.classify_and_summarize", return_value=None):
        result = _filter(_meta(["5.02"]))

    assert len(result) == 1
    assert result[0]["auto_category"] == "Management Change"


def test_801_only_keyword_failure_is_still_dropped():
    """8.01 is the high-volume, low-hit-rate catch-all — reviewing every
    keyword miss there would multiply daily cost for little recall."""
    with patch("llm.classify_and_summarize") as mock_llm:
        result = _filter(_meta(["8.01"]))

    assert result == []
    assert not mock_llm.called
