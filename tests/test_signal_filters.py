"""Tests for the bearish/bullish signal filters: derive_departure_flags,
the new forfeited_comp / has_successor columns, and the direction /
forfeited / clusters query filters."""
import json

from summary_utils import derive_departure_flags


# --- derive_departure_flags ---

def test_flags_forfeited_departure():
    structured = {"departures": [{
        "name": "Jane Doe", "title": "CFO",
        "forfeiture_flag": "forfeited",
        "successor_info": "John Smith appointed permanent CFO",
    }]}
    flags = derive_departure_flags(structured)
    assert flags["forfeited_comp"] == 1
    assert flags["has_successor"] == 1


def test_flags_mixed_forfeiture_counts():
    structured = {"departures": [{"name": "A", "forfeiture_flag": "mixed",
                                  "successor_info": "B named interim CEO"}]}
    assert derive_departure_flags(structured)["forfeited_comp"] == 1


def test_flags_paid_out_is_not_forfeiture():
    structured = {"departures": [{"name": "A", "forfeiture_flag": "paid_out",
                                  "successor_info": "B appointed"}]}
    assert derive_departure_flags(structured)["forfeited_comp"] == 0


def test_flags_no_successor_detected():
    for value in (None, "", "search underway", "Search is underway", "None", "TBD"):
        structured = {"departures": [{"name": "A", "forfeiture_flag": "not_disclosed",
                                      "successor_info": value}]}
        assert derive_departure_flags(structured)["has_successor"] == 0, value


def test_flags_no_departures_leaves_successor_null():
    flags = derive_departure_flags({"departures": [], "comp_events": [{}]})
    assert flags == {"forfeited_comp": 0, "has_successor": None}


def test_flags_accepts_json_string_and_garbage():
    structured = json.dumps({"departures": [{"name": "A", "forfeiture_flag": "forfeited",
                                             "successor_info": None}]})
    flags = derive_departure_flags(structured)
    assert flags["forfeited_comp"] == 1
    assert flags["has_successor"] == 0

    assert derive_departure_flags("not json") == {"forfeited_comp": 0, "has_successor": None}
    assert derive_departure_flags(None) == {"forfeited_comp": 0, "has_successor": None}
    assert derive_departure_flags({"departures": "oops"}) == {"forfeited_comp": 0, "has_successor": None}


# --- database filters ---

def _insert(accession, direction=None, forfeited=None, successor=None):
    from database import insert_filing
    insert_filing({
        "accession_no": accession,
        "company": f"Co {accession}",
        "ticker": "",
        "cik": "0001234567",
        "filed_date": "2026-06-01",
        "item_codes": "5.02",
        "summary": "x",
        "auto_category": "Management Change",
        "auto_subcategory": None,
        "filing_url": "https://example.com",
        "raw_text": "",
        "matched_keywords": "",
        "signal_direction": direction,
        "triage_verdict": "MONITOR",
        "forfeited_comp": forfeited,
        "has_successor": successor,
    })


def test_direction_filter(tmp_sqlite_db):
    from database import get_filings, get_filtered_filing_count
    _insert("acc-bear-1", direction="BEARISH")
    _insert("acc-bull-1", direction="BULLISH")
    _insert("acc-neut-1", direction="NEUTRAL")

    bearish = get_filings(direction="BEARISH")
    assert [f["accession_no"] for f in bearish] == ["acc-bear-1"]
    assert get_filtered_filing_count(direction="BEARISH") == 1
    assert get_filtered_filing_count(direction="BULLISH") == 1
    assert get_filtered_filing_count() == 3


def test_forfeited_filter(tmp_sqlite_db):
    from database import get_filings, get_filtered_filing_count
    _insert("acc-f1", forfeited=1)
    _insert("acc-f0", forfeited=0)
    _insert("acc-fnull")

    rows = get_filings(forfeited_only=True)
    assert [f["accession_no"] for f in rows] == ["acc-f1"]
    assert get_filtered_filing_count(forfeited_only=True) == 1


def test_clusters_filter(tmp_sqlite_db):
    from database import get_filings, update_departure_history, get_filing_by_accession
    _insert("acc-c1")
    _insert("acc-c2")
    row = get_filing_by_accession("acc-c1")
    update_departure_history(row["id"], 3, "[]")

    rows = get_filings(clusters_only=True)
    assert [f["accession_no"] for f in rows] == ["acc-c1"]


def test_insert_persists_flags(tmp_sqlite_db):
    from database import get_filing_by_accession
    _insert("acc-flags", direction="BEARISH", forfeited=1, successor=0)
    row = get_filing_by_accession("acc-flags")
    assert row["forfeited_comp"] == 1
    assert row["has_successor"] == 0


def test_update_filing_analysis_writes_flags(tmp_sqlite_db):
    from database import get_filing_by_accession, update_filing_analysis
    _insert("acc-update")
    row = get_filing_by_accession("acc-update")
    update_filing_analysis(
        row["id"], "sum", "Management Change", None, False, None,
        forfeited_comp=1, has_successor=0,
    )
    row = get_filing_by_accession("acc-update")
    assert row["forfeited_comp"] == 1
    assert row["has_successor"] == 0


def test_dashboard_direction_filter_route(tmp_sqlite_db):
    from app import app
    app.config["TESTING"] = True
    client = app.test_client()
    _insert("acc-route-bear", direction="BEARISH")
    _insert("acc-route-bull", direction="BULLISH")

    resp = client.get("/?direction=BEARISH")
    assert resp.status_code == 200
    assert b"Co acc-route-bear" in resp.data
    assert b"Co acc-route-bull" not in resp.data

    # garbage direction is ignored, not a 500
    resp = client.get("/?direction=SIDEWAYS")
    assert resp.status_code == 200
    assert b"Co acc-route-bull" in resp.data


def test_flags_named_successor_with_tricky_words_not_flagged():
    """Regression: 'none' matched inside 'nonexecutive'/'nonemployee', flagging
    filings that explicitly name an interim successor."""
    for info in ("A nonemployee director will serve as interim CEO",
                 "Jane Roe, nonexecutive director, appointed interim CEO"):
        structured = {"departures": [{"name": "X", "forfeiture_flag": "paid_out",
                                      "successor_info": info}]}
        assert derive_departure_flags(structured)["has_successor"] == 1, info
