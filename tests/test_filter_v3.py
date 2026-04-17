"""Tests that filter.py correctly persists v3 LLM output fields on filings."""
import json
from unittest.mock import patch


def _v3_llm_response(**overrides):
    """Build a realistic v3-shaped LLM response."""
    base = {
        "relevant": True,
        "relevant_reason": None,
        "reasoning": "Identified CFO departure with severance.",
        "top_level_category": "Both",
        "subcategories": ["CFO Departure", "Severance / Separation"],
        "urgent": False,
        "is_complex": False,
        "narrative_summary": None,
        "departures": [{
            "name": "John Smith", "title": "CFO", "effective_date": "2026-04-01",
            "stated_reason": "resigned", "successor_info": "interim CFO named",
            "signal": None,
        }],
        "appointments": [],
        "comp_events": [{
            "executive": "John Smith (departing CFO)",
            "grant_type": "Severance",
            "grant_value": "$2.4M",
            "grant_date": None, "filing_date": "2026-04-02",
            "vesting_schedule": None, "performance_hurdles": None,
            "stock_price_targets": None,
        }],
        "other": [],
        "_tokens_in": 1000, "_tokens_out": 400,
    }
    base.update(overrides)
    return base


def test_filter_maps_v3_fields_onto_filing():
    """A single filing through Stage 3 with v3 output should have all new fields set."""
    from filter import filter_filings

    def fake_fetch(url, cik, accession):
        return "Filing text with CFO resignation details.", "https://sec.gov/filing.htm"

    filings_meta = [{
        "accession_no": "0001-26-000001",
        "company": "Acme Corp", "ticker": "ACME", "cik": "123",
        "filed_date": "2026-04-02", "item_codes": "5.02",
        "filing_url": "https://sec.gov/index.htm",
        "items_list": ["5.02"],
    }]

    with patch("filter.classify_and_summarize", return_value=_v3_llm_response()):
        result = filter_filings(filings_meta, fetch_text_func=fake_fetch)

    assert len(result) == 1
    f = result[0]
    assert f["auto_category"] == "Both"
    # Subcategory is serialized as a JSON array string
    assert json.loads(f["auto_subcategory"]) == ["CFO Departure", "Severance / Separation"]
    assert f["is_complex"] == 0 or f["is_complex"] is False
    assert f["narrative_summary"] is None
    assert f["relevant_reason"] is None
    # structured_summary blob contains the event arrays
    structured = json.loads(f["structured_summary"])
    assert structured["departures"][0]["name"] == "John Smith"
    assert structured["comp_events"][0]["grant_value"] == "$2.4M"
    # filing_document_url was captured from fetch
    assert f["filing_document_url"] == "https://sec.gov/filing.htm"


def test_filter_persists_narrative_when_complex():
    """is_complex: true with narrative_summary should be stored."""
    from filter import filter_filings

    def fake_fetch(url, cik, accession):
        return "Complex filing text.", "https://sec.gov/filing.htm"

    filings_meta = [{
        "accession_no": "0001-26-000002",
        "company": "Mega Pharma", "ticker": "MPHI", "cik": "456",
        "filed_date": "2026-04-15", "item_codes": "5.02",
        "filing_url": "https://sec.gov/index.htm",
        "items_list": ["5.02"],
    }]

    complex_response = _v3_llm_response(
        is_complex=True,
        narrative_summary="Buyback + clawback + CEO transition all in one filing.",
    )

    with patch("filter.classify_and_summarize", return_value=complex_response):
        result = filter_filings(filings_meta, fetch_text_func=fake_fetch)

    assert result[0]["is_complex"] in (1, True)
    assert "Buyback" in result[0]["narrative_summary"]


def test_filter_records_relevant_reason_when_rejected():
    """When LLM returns relevant:false, filing is dropped from results."""
    from filter import filter_filings

    def fake_fetch(url, cik, accession):
        return "Irrelevant earnings release text.", "https://sec.gov/filing.htm"

    filings_meta = [{
        "accession_no": "0001-26-000003",
        "company": "Boring Co", "ticker": "BORE", "cik": "789",
        "filed_date": "2026-04-10", "item_codes": "8.01",
        "filing_url": "https://sec.gov/index.htm",
        "items_list": ["8.01"],
    }]

    rejected = _v3_llm_response(
        relevant=False,
        relevant_reason="Earnings release with no executive or comp content.",
    )
    # Clear optional fields on rejection
    rejected.update({"departures": [], "appointments": [], "comp_events": [], "other": []})

    with patch("filter.classify_and_summarize", return_value=rejected):
        result = filter_filings(filings_meta, fetch_text_func=fake_fetch)

    # Rejected filings are filtered out (existing behavior preserved)
    assert len(result) == 0


def test_filter_handles_other_only_filing_with_null_arrays():
    """Filing where everything lives in other[] (e.g., CEO forward sale) AND
    the LLM emitted explicit null for empty event arrays. Both cases at once.

    Legacy `summary` must still be non-empty (populated from other[]), and
    `structured_summary` must not crash on null arrays.
    """
    from filter import filter_filings

    def fake_fetch(url, cik, accession):
        return "CEO entered forward sale contract.", "https://sec.gov/filing.htm"

    # Use item 5.02 so Stage 2 passes (5.02 is a near-miss if keywords don't fire)
    filings_meta = [{
        "accession_no": "0001-26-000004",
        "company": "Semiconductor Co", "ticker": "SMCO", "cik": "321",
        "filed_date": "2026-04-10", "item_codes": "5.02",
        "filing_url": "https://sec.gov/index.htm",
        "items_list": ["5.02"],
    }]

    # LLM emits explicit null for empty arrays — code must handle both null and []
    other_only = _v3_llm_response(
        top_level_category="Other",
        subcategories=["Insider Transaction", "Forward Sale"],
        departures=None,
        appointments=None,
        comp_events=None,
        other=[
            "CEO Patricia Wong entered a variable prepaid forward contract covering 750K shares.",
            "Signal: monetizing ~30% of direct holdings without a public sale.",
        ],
    )

    with patch("filter.classify_and_summarize", return_value=other_only):
        result = filter_filings(filings_meta, fetch_text_func=fake_fetch)

    assert len(result) == 1
    f = result[0]
    # Legacy summary should contain the other[] bullets — NOT be empty
    assert f["summary"], "legacy summary is empty — other[] bullets weren't included"
    assert "Patricia Wong" in f["summary"] or "forward" in f["summary"].lower()
    # structured_summary blob shouldn't crash and should contain the event arrays
    structured = json.loads(f["structured_summary"])
    assert structured["departures"] == []  # null -> []
    assert structured["appointments"] == []
    assert structured["comp_events"] == []
    assert len(structured["other"]) == 2
