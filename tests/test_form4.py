"""Tests for the Form 4 scanner and the INSIDER_BUY detector.

The scanner reads XML that is already structured, so the risks are all in the
plumbing: parsing the fixed-width index, reading leaves wrapped in <value>,
booleans spelled two ways, and deciding which filings deserve a row at all.
"""
from unittest.mock import patch

import pytest

import form4
import signals

INDEX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    September 10, 2026

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
4           Cadre Holdings, Inc.                                          1860543     20260910    edgar/data/1860543/0001104659-26-101500.txt
4           Kanders Warren B                                              1018396     20260910    edgar/data/1018396/0001104659-26-101500.txt
4/A         Some Issuer Corp                                              123456      20260910    edgar/data/123456/0000123456-26-000001.txt
8-K         Cadre Holdings, Inc.                                          1860543     20260910    edgar/data/1860543/0001104659-26-101501.txt
"""

BUY_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <periodOfReport>2026-09-08</periodOfReport>
  <aff10b5One>0</aff10b5One>
  <issuer>
    <issuerCik>0001860543</issuerCik>
    <issuerName>Cadre Holdings, Inc.</issuerName>
    <issuerTradingSymbol>cdre</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001018396</rptOwnerCik><rptOwnerName>Kanders Warren B</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>true</isDirector><isOfficer>1</isOfficer>
      <officerTitle>CEO and Chairman</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-09-08</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>31.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-09-08</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>31.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def _parsed(**overrides):
    base = form4.parse_form4(BUY_XML)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def test_index_keeps_only_form_4_rows():
    rows = form4.parse_index(INDEX)
    assert {r["form"] for r in rows} == {"4", "4/A"}
    assert len(rows) == 3  # the 8-K line is dropped


def test_index_normalizes_cik_and_extracts_accession():
    row = form4.parse_index(INDEX)[0]
    assert row["cik"] == "1860543"
    assert row["accession"] == "0001104659-26-101500"
    assert row["company"] == "Cadre Holdings, Inc."


def test_one_filing_listed_under_issuer_and_owner_shares_an_accession():
    rows = form4.parse_index(INDEX)
    assert rows[0]["accession"] == rows[1]["accession"]


def test_weekend_has_no_index_and_makes_no_request():
    with patch("requests.get") as mock_get:
        assert form4.fetch_index("2026-09-12") == []   # a Saturday
    assert not mock_get.called


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------

def test_parse_reads_value_wrapped_leaves_and_both_boolean_spellings():
    parsed = form4.parse_form4(BUY_XML)
    assert parsed["ticker"] == "CDRE"
    assert parsed["issuer_cik"] == "1860543"
    assert parsed["is_director"] is True      # "true"
    assert parsed["is_officer"] is True       # "1"
    assert parsed["ten_b5_1"] is False
    purchase = parsed["transactions"][0]
    assert purchase["code"] == "P"
    assert purchase["shares"] == 10000
    assert purchase["price"] == 31.50
    assert purchase["acquired"] is True


def test_malformed_xml_returns_none():
    assert form4.parse_form4("<not xml") is None


# ---------------------------------------------------------------------------
# Which Form 4s qualify
# ---------------------------------------------------------------------------

def test_a_large_officer_purchase_qualifies():
    facts = form4.to_facts(_parsed())
    assert facts["insider_transactions"][0]["value_usd"] == pytest.approx(315_000)
    assert facts["subcategories"] == ["Insider Buy"]


def test_tax_withholding_is_ignored():
    """Code F — shares withheld for taxes — is not a decision."""
    parsed = _parsed()
    parsed["transactions"] = [t for t in parsed["transactions"] if t["code"] == "F"]
    assert form4.to_facts(parsed) is None


def test_a_small_purchase_does_not_qualify():
    parsed = _parsed()
    parsed["transactions"][0]["shares"] = 100   # $3,150
    assert form4.to_facts(parsed) is None


def test_a_purchase_by_a_ten_percent_holder_who_is_not_an_insider_is_ignored():
    """A fund buying more is a different signal from management buying."""
    assert form4.to_facts(_parsed(is_director=False, is_officer=False)) is None


def test_a_grant_to_the_ceo_becomes_a_comp_event_without_a_cycle_claim():
    parsed = _parsed()
    parsed["transactions"] = [{"derivative": True, "security": "Stock Option (right to buy)",
                               "date": "2026-06-16", "code": "A", "shares": 500_000,
                               "price": 24.0, "acquired": True}]
    facts = form4.to_facts(parsed)
    event = facts["comp_events"][0]
    assert event["role_class"] == "CEO"
    assert event["share_count"] == 500_000
    assert event["is_annual_cycle"] is None      # cadence decides, not the XML
    assert event["grant_value_usd"] is None      # an option's price is its strike


def test_grants_to_the_wider_officer_group_are_ignored():
    parsed = _parsed(officer_title="SVP, Operations")
    parsed["transactions"] = [{"derivative": False, "security": "RSU", "date": "2026-03-01",
                               "code": "A", "shares": 5000, "price": 0, "acquired": True}]
    assert form4.to_facts(parsed) is None


def test_rendered_text_is_never_empty():
    assert "Kanders Warren B" in form4.render_text(_parsed())


# ---------------------------------------------------------------------------
# INSIDER_BUY
# ---------------------------------------------------------------------------

def _facts(buys):
    return {"departures": [], "appointments": [], "comp_events": [], "other": [],
            "filing_flags": {}, "insider_transactions": buys}


def _buy(person="A", value=100_000, plan=False, title="CEO"):
    return {"person": person, "title": title, "type": "open_market_buy",
            "value_usd": value, "ten_b5_1": plan}


def test_insider_buy_fires_bullish_with_the_amount():
    sig = next(s for s in signals.detect(_facts([_buy(value=315_000)])).signals
               if s.type == "INSIDER_BUY")
    assert sig.direction == "BULLISH"
    assert "$315K" in sig.evidence
    assert sig.severity == 4    # base 3, +1 for >= $250K


def test_two_buyers_outrank_one():
    one = signals.detect(_facts([_buy("A", 100_000)])).signals[0].severity
    two = signals.detect(_facts([_buy("A", 60_000), _buy("B", 60_000)])).signals[0].severity
    assert two > one


def test_a_planned_10b5_1_purchase_scores_lower():
    """The date was fixed months earlier; it says nothing about today's price."""
    discretionary = signals.detect(_facts([_buy(value=100_000)])).signals[0]
    scheduled = signals.detect(_facts([_buy(value=100_000, plan=True)])).signals[0]
    assert scheduled.severity < discretionary.severity
    assert "10b5-1" in scheduled.evidence


def test_tiny_buys_do_not_fire():
    assert signals.detect(_facts([_buy(value=10_000)])).signals == []


def test_an_insider_sale_is_not_a_buy():
    sale = dict(_buy(value=900_000), type="open_market_sale")
    assert "INSIDER_BUY" not in {s.type for s in signals.detect(_facts([sale])).signals}


# ---------------------------------------------------------------------------
# The scan, end to end, with SEC stubbed
# ---------------------------------------------------------------------------

def test_scan_stores_a_signal_and_skips_silence(tmp_sqlite_db, monkeypatch):
    import database
    database.insert_filing({
        "accession_no": "8k-1", "company": "Cadre Holdings, Inc.", "ticker": "CDRE",
        "cik": "0001860543", "filed_date": "2026-09-01", "item_codes": "5.02",
        "filing_url": "u", "raw_text": "t",
    })
    monkeypatch.setattr(form4, "fetch_index", lambda date: form4.parse_index(INDEX))
    monkeypatch.setattr(form4, "fetch_form4",
                        lambda cik, acc: (form4.parse_form4(BUY_XML), "https://sec.gov/x.xml"))
    monkeypatch.setattr("pipeline._build_context", lambda filing: {"price": 31.5})
    monkeypatch.setattr("pipeline._judge", lambda *a, **k: None)

    stats = form4.scan_day("2026-09-10", apply_universe=False)

    assert stats["stored"] == 1
    row = database.get_filing_by_accession("0001104659-26-101500")
    assert row["source"] == "FORM4"
    assert "INSIDER_BUY" in row["signal_types"]
    assert row["raw_text"]

    # Re-running the same day stores nothing new.
    assert form4.scan_day("2026-09-10", apply_universe=False)["stored"] == 0


def test_scan_ignores_issuers_the_tool_has_never_seen(tmp_sqlite_db, monkeypatch):
    monkeypatch.setattr(form4, "fetch_index", lambda date: form4.parse_index(INDEX))
    fetched = []
    monkeypatch.setattr(form4, "fetch_form4", lambda cik, acc: fetched.append(acc) or (None, None))
    stats = form4.scan_day("2026-09-10", apply_universe=False)
    assert stats["known_issuers"] == 0
    assert fetched == []


def test_pipeline_analyze_facts_makes_no_extraction_call(monkeypatch):
    """Form 4 facts are already structured — the extraction model must not run."""
    import pipeline
    monkeypatch.setattr("llm.classify_and_summarize",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("extraction ran")))
    monkeypatch.setattr(pipeline, "_build_context", lambda filing: {})
    monkeypatch.setattr(pipeline, "_judge", lambda *a, **k: None)
    result = pipeline.analyze_facts({"company": "X"}, form4.to_facts(_parsed()))
    assert "INSIDER_BUY" in result.signal_types


def test_a_grant_to_someone_appointed_in_a_recent_8k_is_a_hiring_package(tmp_sqlite_db, monkeypatch):
    """Seen on the first live scan: a newly hired CFO's inducement options
    scored as off-cycle, because a Form 4 cannot say why a grant was made.
    Names are matched in either order — Form 4 writes "Fulk Jennifer"."""
    import json
    import database

    database.insert_filing({
        "accession_no": "8k-appoint", "company": "Kura Oncology, Inc.", "ticker": "KURA",
        "cik": "0001422143", "filed_date": "2026-09-08", "item_codes": "5.02",
        "filing_url": "u", "raw_text": "t",
        "structured_summary": json.dumps({"appointments": [
            {"name": "Jennifer Fulk", "title": "Chief Financial Officer"}]}),
    })
    appointees = form4.recent_appointees("0001422143", "2026-09-10")
    assert appointees[0]["name"] == "Jennifer Fulk"

    facts = {"departures": [], "insider_transactions": [], "other": [], "filing_flags": {},
             "appointments": appointees,
             "comp_events": [{"executive": "Fulk Jennifer (Chief Financial Officer)",
                              "role_class": "CFO", "grant_type": "Stock Option",
                              "grant_date": "2026-09-09", "is_annual_cycle": None,
                              "grant_rationale": None, "recipient_count": 1,
                              "hurdle_prices": []}]}
    result = signals.detect(facts, {"grant_cadence": {"_annual_months": [1, 6]}})
    assert "OFF_CYCLE_GRANT" not in {s.type for s in result.signals}


def test_an_appointment_older_than_the_lookback_does_not_excuse_a_grant(tmp_sqlite_db):
    import json
    import database

    database.insert_filing({
        "accession_no": "8k-old", "company": "Old Co", "ticker": "OLD",
        "cik": "0000000099", "filed_date": "2026-01-02", "item_codes": "5.02",
        "filing_url": "u", "raw_text": "t",
        "structured_summary": json.dumps({"appointments": [{"name": "Pat Longago"}]}),
    })
    assert form4.recent_appointees("0000000099", "2026-09-10") == []


def test_a_single_shared_surname_is_not_a_match():
    """'Smith' alone must not excuse a grant to a different Smith."""
    facts = {"departures": [], "insider_transactions": [], "other": [], "filing_flags": {},
             "appointments": [{"name": "Smith"}],
             "comp_events": [{"executive": "Smith Robert (CEO)", "role_class": "CEO",
                              "grant_type": "Stock Option", "grant_date": "2026-09-09",
                              "is_annual_cycle": None, "grant_rationale": None,
                              "recipient_count": 1, "hurdle_prices": []}]}
    result = signals.detect(facts, {"grant_cadence": {"_annual_months": [3]}})
    assert "OFF_CYCLE_GRANT" in {s.type for s in result.signals}
