"""Tests for the expanded near-miss path: keyword failures for ALL target
item codes now get an LLM look (previously only 5.02), so unusual phrasing
can't slip past the keyword list. The LLM relevance gate keeps noise out."""
from unittest.mock import patch


def _meta(items, accession="0001-26-000042"):
    return [{
        "accession_no": accession,
        "company": "Oddly Worded Corp", "ticker": "ODD", "cik": "123",
        "filed_date": "2026-06-02", "item_codes": ",".join(items),
        "filing_url": "https://sec.gov/index.htm",
        "items_list": items,
    }]


def _fetch_no_keywords(url, cik, accession):
    # Text that matches none of the KEYWORD_CATEGORIES entries
    return "The registrant entered into an arrangement regarding its principal financial figurehead.", "https://sec.gov/doc.htm"


def _relevant_response():
    return {
        "relevant": True, "relevant_reason": None,
        "reasoning": "CFO transition phrased unusually.",
        "top_level_category": "Management Change",
        "subcategories": ["CFO Departure"],
        "urgent": False, "is_complex": False, "narrative_summary": None,
        "departures": [{"name": "A. Person", "title": "CFO",
                        "stated_reason": "resigned", "successor_info": None,
                        "forfeiture_flag": "not_disclosed", "signal": None}],
        "appointments": [], "comp_events": [], "other": [],
        "triage": {"verdict": "MONITOR", "score": 5, "direction": "BEARISH",
                   "top_signal": "CFO out, oddly phrased."},
        "_tokens_in": 100, "_tokens_out": 50,
    }


def test_non_502_keyword_failure_still_reaches_llm():
    """A 1.01-only filing with zero keyword hits used to be dropped unseen.
    Now the LLM reviews it and a relevant one is kept."""
    from filter import filter_filings

    with patch("filter.classify_and_summarize", return_value=_relevant_response()) as mock_llm:
        result = filter_filings(_meta(["1.01"]), fetch_text_func=_fetch_no_keywords)

    assert mock_llm.called
    assert len(result) == 1
    assert result[0]["triage_verdict"] == "MONITOR"


def test_non_502_keyword_failure_rejected_by_llm_is_dropped():
    from filter import filter_filings

    rejection = {"relevant": False, "relevant_reason": "Routine commercial contract.",
                 "_tokens_in": 100, "_tokens_out": 20}
    with patch("filter.classify_and_summarize", return_value=rejection):
        result = filter_filings(_meta(["1.01"]), fetch_text_func=_fetch_no_keywords)

    assert result == []


def test_keywordless_non_502_near_miss_dropped_when_llm_fails():
    """No keywords + no LLM verdict = zero evidence of relevance — drop it."""
    from filter import filter_filings

    with patch("filter.classify_and_summarize", return_value=None):
        result = filter_filings(_meta(["1.01"]), fetch_text_func=_fetch_no_keywords)

    assert result == []


def test_502_near_miss_still_kept_when_llm_fails():
    """5.02 filings keep their historical auto-pass fallback on LLM failure."""
    from filter import filter_filings

    with patch("filter.classify_and_summarize", return_value=None):
        result = filter_filings(_meta(["5.02"]), fetch_text_func=_fetch_no_keywords)

    assert len(result) == 1
    assert result[0]["auto_category"] == "Management Change"
