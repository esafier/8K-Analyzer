"""Tests for % appreciation-required computation on stock-price hurdles."""
import json

from market_targets import extract_price_values, annotate_price_targets


# --- extract_price_values ---

def test_extracts_single_and_multiple_values():
    assert extract_price_values("$10.00 per share") == [10.0]
    assert extract_price_values("vests at $12.50 and $15") == [12.5, 15.0]
    assert extract_price_values("$1,250.00 sustained 60 days") == [1250.0]


def test_extract_handles_garbage():
    assert extract_price_values(None) == []
    assert extract_price_values("") == []
    assert extract_price_values("null") == []
    assert extract_price_values("no dollar amounts here") == []
    assert extract_price_values({"weird": "dict"}) == []


# --- annotate_price_targets ---

def _targets(*values):
    return {"stock_price": [{"executive": "CEO", "value": v} for v in values],
            "market_cap": [], "tsr": []}


def test_annotate_computes_pct_required():
    tp = annotate_price_targets(_targets("$10.00"), current_price=5.0)
    assert tp is not None
    assert tp["max_pct"] == 100.0
    assert tp["min_pct"] == 100.0
    pts = tp["by_value"]["$10.00"]
    assert pts[0]["target"] == 10.0
    assert pts[0]["pct"] == 100.0


def test_annotate_min_max_across_tiers():
    tp = annotate_price_targets(_targets("$12.50 and $15.00"), current_price=10.0)
    assert tp["min_pct"] == 25.0
    assert tp["max_pct"] == 50.0


def test_annotate_negative_pct_for_in_the_money_targets():
    tp = annotate_price_targets(_targets("$4.00"), current_price=5.0)
    assert round(tp["max_pct"]) == -20


def test_annotate_returns_none_when_not_computable():
    assert annotate_price_targets(None, 5.0) is None
    assert annotate_price_targets(_targets("$10"), None) is None
    assert annotate_price_targets(_targets("$10"), 0) is None
    assert annotate_price_targets(_targets("top quartile TSR"), 5.0) is None
    assert annotate_price_targets({"stock_price": []}, 5.0) is None


# --- dashboard rendering ---

def test_dashboard_shows_pct_chip(tmp_sqlite_db):
    from app import app
    from database import insert_filing, upsert_stock_price

    structured = {
        "departures": [], "appointments": [], "other": [],
        "comp_events": [{"executive": "CEO", "grant_type": "PSUs"}],
        "has_market_targets": True,
        "market_targets": _targets("$10.00"),
    }
    insert_filing({
        "accession_no": "acc-pct-1",
        "company": "Hurdle Corp",
        "ticker": "HRDL",
        "cik": "0001234567",
        "filed_date": "2026-06-01",
        "item_codes": "5.02",
        "summary": "PSU grant",
        "auto_category": "Compensation",
        "auto_subcategory": None,
        "filing_url": "https://example.com",
        "raw_text": "",
        "matched_keywords": "",
        "structured_summary": json.dumps(structured),
        "has_market_targets": 1,
        "triage_verdict": "DEEP_LOOK",
        "signal_score": 8,
        "signal_direction": "BULLISH",
        "top_signal": "PSUs need a double",
    })
    upsert_stock_price("HRDL", 5.0)

    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"+100%" in resp.data

    # Detail page renders the per-target breakdown through the shared partial
    from database import get_filing_by_accession
    row = get_filing_by_accession("acc-pct-1")
    resp = client.get(f"/filing/{row['id']}")
    assert resp.status_code == 200
    assert b"+100%" in resp.data


def test_extracts_four_digit_prices_without_commas():
    """Regression: '$1000' was parsed as 100.0 (comma branch matched greedily)."""
    assert extract_price_values("$1000 per share") == [1000.0]
    assert extract_price_values("$1250.50") == [1250.5]
    assert extract_price_values("$4500") == [4500.0]


def test_annotate_tolerates_non_dict_targets():
    """Regression: corrupt/legacy structured_summary shapes must not raise."""
    assert annotate_price_targets("not a dict", 5.0) is None
    assert annotate_price_targets({"stock_price": "oops"}, 5.0) is None
    assert annotate_price_targets({"stock_price": ["bare string"]}, 5.0) is None
