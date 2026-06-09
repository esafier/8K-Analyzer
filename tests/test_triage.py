"""Tests for the scored triage inbox: parse_triage validation, departure
counting, the verdict filter / signal sort, and departure-cluster counts."""
import json
import sqlite3

from summary_utils import parse_triage, count_departures


# ---------------------------------------------------------------------------
# parse_triage
# ---------------------------------------------------------------------------

def test_parse_triage_valid():
    result = parse_triage({
        "triage": {
            "verdict": "DEEP_LOOK",
            "score": 8,
            "direction": "BEARISH",
            "top_signal": "CFO resigned effective immediately, forfeiting $4.2M unvested RSUs.",
        }
    })
    assert result["verdict"] == "DEEP_LOOK"
    assert result["score"] == 8
    assert result["direction"] == "BEARISH"
    assert "forfeiting" in result["top_signal"]


def test_parse_triage_missing_object():
    """LLM omitted the triage block entirely → all fields None, no crash."""
    result = parse_triage({"relevant": True})
    assert result == {"verdict": None, "score": None, "direction": None, "top_signal": None}


def test_parse_triage_not_a_dict():
    assert parse_triage(None)["verdict"] is None
    assert parse_triage("garbage")["verdict"] is None
    assert parse_triage({"triage": "DEEP_LOOK"})["verdict"] is None


def test_parse_triage_normalizes_verdict_case_and_spaces():
    """LLMs sometimes emit 'deep look' or 'Deep_Look' — normalize them."""
    result = parse_triage({"triage": {"verdict": "deep look"}})
    assert result["verdict"] == "DEEP_LOOK"


def test_parse_triage_rejects_unknown_enum_values():
    result = parse_triage({"triage": {"verdict": "MAYBE", "direction": "SIDEWAYS"}})
    assert result["verdict"] is None
    assert result["direction"] is None


def test_parse_triage_clamps_score():
    assert parse_triage({"triage": {"score": 15}})["score"] == 10
    assert parse_triage({"triage": {"score": -3}})["score"] == 0
    assert parse_triage({"triage": {"score": "7"}})["score"] == 7
    assert parse_triage({"triage": {"score": "high"}})["score"] is None
    assert parse_triage({"triage": {"score": None}})["score"] is None


def test_parse_triage_truncates_runaway_top_signal():
    result = parse_triage({"triage": {"top_signal": "x" * 1000}})
    assert len(result["top_signal"]) == 400


# ---------------------------------------------------------------------------
# count_departures
# ---------------------------------------------------------------------------

def test_count_departures_from_dict():
    structured = {"departures": [{"name": "A"}, {"name": "B"}], "appointments": []}
    assert count_departures(structured) == 2


def test_count_departures_from_json_string():
    structured = json.dumps({"departures": [{"name": "A"}]})
    assert count_departures(structured) == 1


def test_count_departures_bad_input():
    assert count_departures(None) == 0
    assert count_departures("not json{") == 0
    assert count_departures({"departures": "oops"}) == 0
    assert count_departures({"departures": [{"name": "A"}, "not-a-dict"]}) == 1


# ---------------------------------------------------------------------------
# Database: columns, verdict filter, signal sort, cluster counts
# ---------------------------------------------------------------------------

def _insert(database, accession, cik, filed_date, verdict=None, score=None,
            departure_count=None, company="TestCo"):
    database.insert_filing({
        "accession_no": accession,
        "company": company,
        "ticker": "TST",
        "cik": cik,
        "filed_date": filed_date,
        "item_codes": "5.02",
        "summary": "test",
        "auto_category": "Management Change",
        "triage_verdict": verdict,
        "signal_score": score,
        "signal_direction": "BEARISH" if verdict else None,
        "top_signal": "test signal" if verdict else None,
        "departure_count": departure_count,
    })


def test_triage_columns_exist(tmp_sqlite_db):
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(filings)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    for col in ("triage_verdict", "signal_score", "signal_direction",
                "top_signal", "departure_count"):
        assert col in columns, f"missing column {col}"


def test_insert_and_filter_by_verdict(tmp_sqlite_db):
    import database

    _insert(database, "T-1", "0001", "2026-06-01", verdict="DEEP_LOOK", score=8)
    _insert(database, "T-2", "0002", "2026-06-02", verdict="MONITOR", score=5)
    _insert(database, "T-3", "0003", "2026-06-03", verdict="PASS", score=1)
    _insert(database, "T-4", "0004", "2026-06-04")  # legacy / unrated

    deep = database.get_filings(verdict="DEEP_LOOK")
    assert [f["accession_no"] for f in deep] == ["T-1"]

    actionable = database.get_filings(verdict="actionable")
    assert {f["accession_no"] for f in actionable} == {"T-1", "T-2"}

    # Count must agree with the list
    assert database.get_filtered_filing_count(verdict="actionable") == 2


def test_signal_sort_order(tmp_sqlite_db):
    """Signal sort: DEEP_LOOK > MONITOR > unrated > PASS; score breaks ties."""
    import database

    _insert(database, "S-pass", "0001", "2026-06-04", verdict="PASS", score=2)
    _insert(database, "S-deep-low", "0002", "2026-06-01", verdict="DEEP_LOOK", score=6)
    _insert(database, "S-deep-high", "0003", "2026-06-02", verdict="DEEP_LOOK", score=9)
    _insert(database, "S-monitor", "0004", "2026-06-03", verdict="MONITOR", score=5)
    _insert(database, "S-unrated", "0005", "2026-06-05")

    rows = database.get_filings(sort="signal")
    order = [f["accession_no"] for f in rows]
    assert order == ["S-deep-high", "S-deep-low", "S-monitor", "S-unrated", "S-pass"]


def test_date_sort_is_default(tmp_sqlite_db):
    import database

    _insert(database, "D-1", "0001", "2026-06-01", verdict="DEEP_LOOK", score=9)
    _insert(database, "D-2", "0002", "2026-06-05", verdict="PASS", score=1)

    rows = database.get_filings()
    assert [f["accession_no"] for f in rows] == ["D-2", "D-1"]


# ---------------------------------------------------------------------------
# EDGAR-based departure history (departure_count_24mo + departure_history)
# ---------------------------------------------------------------------------

def test_departure_history_columns_exist(tmp_sqlite_db):
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(filings)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert "departure_count_24mo" in columns
    assert "departure_history" in columns


def test_update_departure_history(tmp_sqlite_db):
    import database

    _insert(database, "H-1", "0001", "2026-06-01", departure_count=1)
    filing = dict(database.get_filings(limit=1)[0])

    history = [{"date": "2026-06-01", "person": "Jane Smith", "position": "CFO",
                "reason": "resigned", "_accession": "H-1", "_error": False}]
    database.update_departure_history(filing["id"], 3, json.dumps(history))

    updated = dict(database.get_filing_by_id(filing["id"]))
    assert updated["departure_count_24mo"] == 3
    assert json.loads(updated["departure_history"])[0]["person"] == "Jane Smith"


def test_count_real_departures_excludes_error_rows():
    from departures import count_real_departures

    rows = [
        {"person": "Jane Smith", "_error": False},
        {"person": "Bob Lee", "_error": False},
        {"person": None, "_error": True},      # extraction-failed placeholder
    ]
    assert count_real_departures(rows) == 2
    assert count_real_departures([]) == 0


def test_enrich_filing_departure_history(tmp_sqlite_db, monkeypatch):
    """Enrichment persists the deduped count + JSON; lookup failure leaves
    the row untouched so a later retry can fill it in."""
    import database
    import departures

    _insert(database, "E-1", "0001", "2026-06-01", departure_count=1)
    filing = dict(database.get_filings(limit=1)[0])

    fake_history = [
        {"date": "2026-06-01", "person": "Jane Smith", "position": "CFO", "_error": False},
        {"date": "2026-01-15", "person": "Bob Lee", "position": "COO", "_error": False},
    ]
    monkeypatch.setattr(departures, "get_departures_for_filing",
                        lambda cik, current_accession: fake_history)
    count = departures.enrich_filing_departure_history(filing["id"], "0001", "E-1")
    assert count == 2
    updated = dict(database.get_filing_by_id(filing["id"]))
    assert updated["departure_count_24mo"] == 2

    # Now simulate an EDGAR failure on a fresh row — must return None, not raise
    _insert(database, "E-2", "0002", "2026-06-02", departure_count=1)
    fresh = [dict(f) for f in database.get_filings(limit=10)
             if f["accession_no"] == "E-2"][0]

    def _boom(cik, current_accession):
        raise RuntimeError("EDGAR down")
    monkeypatch.setattr(departures, "get_departures_for_filing", _boom)
    assert departures.enrich_filing_departure_history(fresh["id"], "0002", "E-2") is None
    untouched = dict(database.get_filing_by_id(fresh["id"]))
    assert untouched["departure_count_24mo"] is None


def test_enrich_new_filings_skips_stamped_and_non_departure(tmp_sqlite_db, monkeypatch):
    import database
    import departures

    _insert(database, "N-1", "0001", "2026-06-01", departure_count=1)  # needs enrichment
    _insert(database, "N-2", "0002", "2026-06-01", departure_count=0)  # no departures
    _insert(database, "N-3", "0003", "2026-06-01", departure_count=1)  # already stamped
    stamped = [dict(f) for f in database.get_filings(limit=10)
               if f["accession_no"] == "N-3"][0]
    database.update_departure_history(stamped["id"], 1, "[]")

    enriched = []
    monkeypatch.setattr(departures, "enrich_filing_departure_history",
                        lambda fid, cik, acc: enriched.append(acc) or 1)

    departures.enrich_new_filings([
        {"accession_no": "N-1", "cik": "0001", "departure_count": 1, "company": "A"},
        {"accession_no": "N-2", "cik": "0002", "departure_count": 0, "company": "B"},
        {"accession_no": "N-3", "cik": "0003", "departure_count": 1, "company": "C"},
        {"accession_no": "N-4", "cik": "", "departure_count": 1, "company": "D"},  # no CIK
    ])

    assert enriched == ["N-1"]


def test_run_history_backfill_candidate_selection(tmp_sqlite_db, monkeypatch):
    """The one-time backfill enriches departure filings even when the
    departure_count column is NULL (pre-retrofit rows), and skips the rest."""
    import database
    import departures

    dep_json = json.dumps({"departures": [{"name": "Jane"}], "appointments": [],
                           "comp_events": [], "other": []})
    no_dep_json = json.dumps({"departures": [], "appointments": [],
                              "comp_events": [], "other": []})

    # Pre-retrofit row: departure_count NULL but structured JSON has a departure
    database.insert_filing({
        "accession_no": "B-1", "company": "OldDep", "cik": "0001",
        "filed_date": "2026-05-01", "structured_summary": dep_json,
    })
    # Row with departure_count set
    _insert(database, "B-2", "0002", "2026-05-02", departure_count=2)
    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET structured_summary = ? WHERE accession_no = 'B-2'", (dep_json,))
    conn.commit()
    conn.close()
    # Comp-only row — skipped
    database.insert_filing({
        "accession_no": "B-3", "company": "CompOnly", "cik": "0003",
        "filed_date": "2026-05-03", "structured_summary": no_dep_json,
    })

    enriched = []
    monkeypatch.setattr(departures, "enrich_filing_departure_history",
                        lambda fid, cik, acc: enriched.append(acc) or 1)

    stats = departures.run_history_backfill(verbose=False)
    assert sorted(enriched) == ["B-1", "B-2"]
    assert stats["enriched"] == 2
    assert stats["skipped_no_departures"] == 1
