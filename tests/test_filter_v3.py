"""Tests that the ingest funnel persists analysis output onto filing rows.

Field mapping itself is owned by tests/test_pipeline.py. What this file
protects is the seam: filter.py must hand each survivor to the shared
pipeline and merge the result back onto the metadata dict that goes to
insert_filing().

`apply_universe=False` throughout — the market-cap gate is exercised in
tests/test_universe.py, and leaving it on here would make every fixture
depend on a live market-cap lookup.
"""
import json
from unittest.mock import patch

from filter import filter_filings


def _v4_facts(**overrides):
    """Extraction output in the v4 schema."""
    base = {
        "relevant": True,
        "relevant_reason": None,
        "reasoning": "Identified CFO departure with severance.",
        "top_level_category": "Both",
        "subcategories": ["CFO Departure", "Severance / Separation"],
        "is_complex": False,
        "narrative_summary": None,
        "departures": [{
            "name": "John Smith", "title": "CFO", "role_class": "CFO",
            "effective_date": "2026-04-01", "effective_immediately": False,
            "days_notice": 30, "stated_reason": "resigned",
            "is_retirement": False, "is_merger_related": False,
            "successor_named": True, "successor_info": "interim CFO named",
            "comp_impact": "receives severance", "forfeiture_flag": "paid_out",
        }],
        "appointments": [],
        "comp_events": [{
            "executive": "John Smith (departing CFO)",
            "grant_type": "Severance", "grant_value": "$2.4M",
            "grant_value_usd": 2_400_000, "is_annual_cycle": None,
            "grant_date": None, "filing_date": "2026-04-02",
            "vesting_schedule": None, "operating_hurdles": None,
            "market_based_targets": {"stock_price": None, "market_cap": None, "tsr": None},
            "hurdle_prices": [], "stock_price_targets": None,
        }],
        "insider_transactions": [],
        "other": [],
        "filing_flags": {},
        "_tokens_in": 1000, "_tokens_out": 400,
    }
    base.update(overrides)
    return base


def _meta(items="5.02", accession="0001-26-000001", company="Acme Corp"):
    return [{
        "accession_no": accession, "company": company, "ticker": "ACME", "cik": "123",
        "filed_date": "2026-04-02", "item_codes": items,
        "filing_url": "https://sec.gov/index.htm",
        "items_list": items.split(","),
    }]


def _fetch_ok(url, cik, accession):
    return "Filing text with CFO resignation details.", "https://sec.gov/filing.htm"


def _run(metadata, facts, fetch=_fetch_ok, context=None):
    """Drive the funnel with extraction and context stubbed."""
    with patch("llm.classify_and_summarize", return_value=facts), \
         patch("pipeline._build_context", return_value=context or {}), \
         patch("pipeline._judge", return_value=None):
        return filter_filings(metadata, fetch_text_func=fetch,
                              apply_universe=False, skip_existing=False)


def test_analysis_fields_land_on_the_filing_row():
    result = _run(_meta(), _v4_facts())

    assert len(result) == 1
    filing = result[0]
    assert filing["auto_category"] == "Both"
    assert json.loads(filing["auto_subcategory"]) == ["CFO Departure", "Severance / Separation"]
    assert filing["source"] == "8-K"
    assert filing["filing_document_url"] == "https://sec.gov/filing.htm"

    structured = json.loads(filing["structured_summary"])
    assert structured["departures"][0]["name"] == "John Smith"
    assert structured["comp_events"][0]["grant_value"] == "$2.4M"


def test_every_row_gets_a_verdict_and_a_score():
    """Un-ranked rows are what made the old signal sort untrustworthy."""
    result = _run(_meta(), _v4_facts())
    filing = result[0]
    assert filing["triage_verdict"] in ("DEEP_LOOK", "MONITOR", "PASS")
    assert isinstance(filing["signal_score"], int)
    assert filing["signal_direction"] in ("BEARISH", "BULLISH", "MIXED", "NEUTRAL")
    assert filing["pipeline_version"]


def test_narrative_is_persisted_for_complex_filings():
    facts = _v4_facts(is_complex=True,
                      narrative_summary="Buyback + clawback + CEO transition in one filing.")
    result = _run(_meta(accession="0001-26-000002"), facts)
    assert result[0]["is_complex"] in (1, True)
    assert "Buyback" in result[0]["narrative_summary"]


def test_irrelevant_filings_are_dropped():
    facts = _v4_facts(relevant=False, relevant_reason="Earnings release, no exec content.",
                      departures=[], comp_events=[])
    assert _run(_meta(items="8.01"), facts) == []


def test_502_with_no_text_uses_placeholder_summary():
    """A rate-limited fetch must leave a visibly pending row rather than a
    blank cell that looks like a successful empty analysis."""
    def failed_fetch(url, cik, accession):
        return "", None

    result = _run(_meta(accession="0001-26-000099"), _v4_facts(), fetch=failed_fetch)

    assert len(result) == 1, "5.02 filings should still be kept when text fetch fails"
    summary = result[0]["summary"]
    assert summary
    assert "rate" in summary.lower() or "retry" in summary.lower()


def test_market_targets_flow_through_to_the_row():
    facts = _v4_facts(comp_events=[{
        "executive": "CEO Jane Doe", "role_class": "CEO", "grant_type": "PSUs",
        "grant_value": "$10M target", "is_annual_cycle": True,
        "operating_hurdles": "Relative TSR vs. peer group",
        "market_based_targets": {"stock_price": "$150, $200", "market_cap": None,
                                 "tsr": "Top quartile vs peers"},
        "hurdle_prices": [150, 200], "stock_price_targets": "$150, $200",
    }])
    result = _run(_meta(accession="0001-26-000050"), facts, context={"price": 100.0})

    filing = result[0]
    assert filing["has_market_targets"] == 1
    structured = json.loads(filing["structured_summary"])
    assert structured["has_market_targets"] is True
    assert len(structured["market_targets"]["stock_price"]) == 1
    assert len(structured["market_targets"]["tsr"]) == 1
    assert "HURDLE_CONVICTION" in filing["signal_types"]


def test_other_only_filing_with_null_arrays_survives():
    """Everything in other[], and the model emitted explicit null rather than
    [] for the empty arrays. Both at once."""
    facts = _v4_facts(
        top_level_category="Other",
        subcategories=["Insider Transaction", "Forward Sale"],
        departures=None, appointments=None, comp_events=None,
        other=["CEO Patricia Wong entered a variable prepaid forward contract "
               "covering 750K shares.",
               "Signal: monetizing ~30% of direct holdings without a public sale."],
        insider_transactions=[{"person": "Patricia Wong", "type": "forward_sale",
                               "shares": 750000, "value_usd": 84_000_000}],
    )
    result = _run(_meta(accession="0001-26-000004"), facts)

    filing = result[0]
    assert filing["summary"], "legacy summary is empty — other[] bullets weren't used"
    structured = json.loads(filing["structured_summary"])
    assert structured["departures"] == []      # null -> []
    assert structured["appointments"] == []
    assert structured["comp_events"] == []
    assert len(structured["other"]) == 2
    assert "INSIDER_MONETIZATION" in filing["signal_types"]


def test_signals_are_stored_with_their_evidence():
    facts = _v4_facts(departures=[{
        "name": "John Smith", "title": "CFO", "role_class": "CFO",
        "effective_immediately": True, "days_notice": 0, "stated_reason": "resigned",
        "is_retirement": False, "is_merger_related": False,
        "successor_named": False, "successor_info": "search underway",
        "comp_impact": "forfeits all unvested RSUs (~$4.2M)",
        "forfeiture_flag": "forfeited",
    }], comp_events=[])
    result = _run(_meta(accession="0001-26-000005"), facts)

    filing = result[0]
    stored = json.loads(filing["signals_json"])
    assert any(s["type"] == "FORFEITURE_EXIT" for s in stored)
    assert all(s.get("evidence") for s in stored)
    assert filing["forfeited_comp"] == 1
    assert filing["has_successor"] == 0
    assert filing["urgent"] == 1


def test_filings_already_in_the_database_are_not_reanalyzed(tmp_sqlite_db):
    """Dedupe used to happen at insert time — i.e. after the SEC fetch and the
    model call had already been paid for."""
    import database
    database.insert_filing({
        "accession_no": "0001-26-000001", "company": "Acme Corp", "ticker": "ACME",
        "cik": "123", "filed_date": "2026-04-02", "item_codes": "5.02",
        "filing_url": "https://sec.gov/index.htm", "raw_text": "x",
    })

    with patch("llm.classify_and_summarize") as mock_llm, \
         patch("pipeline._build_context", return_value={}):
        result = filter_filings(_meta(), fetch_text_func=_fetch_ok,
                                apply_universe=False, skip_existing=True)

    assert result == []
    assert not mock_llm.called
