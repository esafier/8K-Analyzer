"""Tests for the unread filing tracking feature."""
import sqlite3
import pytest


def test_read_at_column_exists(tmp_sqlite_db):
    """After initialize_database() runs, filings must have a read_at column."""
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(filings)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert "read_at" in columns


def test_read_at_index_exists(tmp_sqlite_db):
    """An index on read_at should exist to keep 'unread only' queries fast."""
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='filings'")
    indexes = {row[0] for row in cursor.fetchall()}
    conn.close()
    assert "idx_filings_read_at" in indexes


def test_read_at_default_is_null_for_new_inserts(tmp_sqlite_db):
    """Any direct INSERT that doesn't supply read_at leaves it NULL.

    This is the property the dashboard depends on: brand-new filings from the
    SEC arrive unread (NULL). The clean-slate migration only touched
    pre-existing rows at the moment the column was added; future inserts get
    NULL by default and need scroll-tracking / detail-view to mark them read.
    """
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO filings (accession_no, company, ticker, filed_date)
        VALUES ('0000000001-26-000001', 'NewArrival Co', 'NEW', '2026-01-01')
    """)
    conn.commit()
    cursor.execute("SELECT read_at FROM filings WHERE accession_no = '0000000001-26-000001'")
    read_at = cursor.fetchone()[0]
    conn.close()
    assert read_at is None, "New inserts must default to read_at IS NULL (unread)"


def test_mark_filings_read_sets_read_at(tmp_sqlite_db):
    """mark_filings_read([ids]) sets read_at for each unread row."""
    import database

    # Insert two unread filings
    for i, accession in enumerate(["A-1", "A-2"]):
        database.insert_filing({
            "accession_no": accession,
            "company": f"Co {i}",
            "ticker": "X",
            "cik": "0001",
            "filed_date": "2026-05-01",
            "item_codes": "5.02",
            "summary": "",
            "auto_category": "Compensation",
            "filing_url": "https://example.com",
            "raw_text": "",
            "matched_keywords": "",
            "urgent": False,
            "comp_details": None,
            "is_complex": False,
            "narrative_summary": None,
            "relevant_reason": None,
            "structured_summary": None,
        })

    # Force them unread (insert_filing should not set read_at, but be explicit)
    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no IN ('A-1','A-2')")
    conn.commit()
    conn.close()

    row1 = database.get_filing_by_accession("A-1")
    row2 = database.get_filing_by_accession("A-2")

    marked = database.mark_filings_read([row1["id"], row2["id"]])
    assert marked == 2

    # Both should now have read_at set
    assert database.get_filing_by_accession("A-1")["read_at"] is not None
    assert database.get_filing_by_accession("A-2")["read_at"] is not None


def test_mark_filings_read_is_idempotent(tmp_sqlite_db):
    """Re-marking a filing that's already read returns 0 (no rows updated)."""
    import database

    database.insert_filing({
        "accession_no": "B-1", "company": "Co", "ticker": "X", "cik": "1",
        "filed_date": "2026-05-01", "item_codes": "5.02", "summary": "",
        "auto_category": "Compensation", "filing_url": "https://example.com",
        "raw_text": "", "matched_keywords": "", "urgent": False,
        "comp_details": None, "is_complex": False, "narrative_summary": None,
        "relevant_reason": None, "structured_summary": None,
    })
    row = database.get_filing_by_accession("B-1")

    # First call: insert_filing leaves read_at NULL → 1 row updated
    first = database.mark_filings_read([row["id"]])
    assert first == 1

    # Second call: row is already read → 0 rows updated
    second = database.mark_filings_read([row["id"]])
    assert second == 0


def test_mark_filings_read_empty_list(tmp_sqlite_db):
    """Empty list is a no-op, returns 0."""
    import database
    assert database.mark_filings_read([]) == 0


def test_get_filings_unread_only(tmp_sqlite_db):
    """get_filings(unread_only=True) returns only filings with read_at IS NULL."""
    import database

    base = {
        "ticker": "X", "cik": "1", "filed_date": "2026-05-01",
        "item_codes": "5.02", "summary": "", "auto_category": "Compensation",
        "filing_url": "https://example.com", "raw_text": "", "matched_keywords": "",
        "urgent": False, "comp_details": None, "is_complex": False,
        "narrative_summary": None, "relevant_reason": None, "structured_summary": None,
    }
    database.insert_filing({**base, "accession_no": "U-1", "company": "Unread1"})
    database.insert_filing({**base, "accession_no": "U-2", "company": "Unread2"})
    database.insert_filing({**base, "accession_no": "R-1", "company": "Read1"})

    # Mark one as read; force the other two unread
    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE accession_no = 'R-1'")
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no IN ('U-1','U-2')")
    conn.commit()
    conn.close()

    unread = database.get_filings(unread_only=True)
    accessions = {f["accession_no"] for f in unread}
    assert accessions == {"U-1", "U-2"}


def test_get_filtered_filing_count_unread_only(tmp_sqlite_db):
    """get_filtered_filing_count(unread_only=True) only counts NULL read_at."""
    import database

    base = {
        "ticker": "X", "cik": "1", "filed_date": "2026-05-01",
        "item_codes": "5.02", "summary": "", "auto_category": "Compensation",
        "filing_url": "https://example.com", "raw_text": "", "matched_keywords": "",
        "urgent": False, "comp_details": None, "is_complex": False,
        "narrative_summary": None, "relevant_reason": None, "structured_summary": None,
    }
    database.insert_filing({**base, "accession_no": "C-1", "company": "C1"})
    database.insert_filing({**base, "accession_no": "C-2", "company": "C2"})
    database.insert_filing({**base, "accession_no": "C-3", "company": "C3"})

    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE accession_no = 'C-3'")
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no IN ('C-1','C-2')")
    conn.commit()
    conn.close()

    assert database.get_filtered_filing_count(unread_only=True) == 2
    assert database.get_filtered_filing_count(unread_only=False) == 3


@pytest.fixture
def flask_client(tmp_sqlite_db, monkeypatch):
    """Flask test client with auth disabled (no TRIAL_CODE set).

    Relies on `tmp_sqlite_db` having monkeypatched `database.DATABASE_PATH` first.
    App's `from database import ...` resolves function names at call time, so
    requests hitting the routes will use the patched DB path automatically.
    """
    monkeypatch.delenv("TRIAL_CODE", raising=False)
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def test_mark_read_endpoint_marks_filings(flask_client, tmp_sqlite_db):
    """POST /api/filings/mark-read with filing_ids updates the DB."""
    import database
    import json

    base = {
        "ticker": "X", "cik": "1", "filed_date": "2026-05-01",
        "item_codes": "5.02", "summary": "", "auto_category": "Compensation",
        "filing_url": "https://example.com", "raw_text": "", "matched_keywords": "",
        "urgent": False, "comp_details": None, "is_complex": False,
        "narrative_summary": None, "relevant_reason": None, "structured_summary": None,
    }
    database.insert_filing({**base, "accession_no": "E-1", "company": "E1"})
    database.insert_filing({**base, "accession_no": "E-2", "company": "E2"})

    # Force unread
    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no IN ('E-1','E-2')")
    conn.commit()
    conn.close()

    ids = [
        database.get_filing_by_accession("E-1")["id"],
        database.get_filing_by_accession("E-2")["id"],
    ]
    resp = flask_client.post(
        "/api/filings/mark-read",
        data=json.dumps({"filing_ids": ids}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"marked": 2}


def test_mark_read_endpoint_rejects_non_list(flask_client):
    """Bad payload (filing_ids not a list) returns 400."""
    import json
    resp = flask_client.post(
        "/api/filings/mark-read",
        data=json.dumps({"filing_ids": "not a list"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_mark_read_endpoint_rejects_non_int_ids(flask_client):
    """Bad payload (non-int IDs) returns 400."""
    import json
    resp = flask_client.post(
        "/api/filings/mark-read",
        data=json.dumps({"filing_ids": ["not", "ints"]}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_mark_read_endpoint_empty_list_returns_zero(flask_client):
    """Empty list is valid, returns marked=0."""
    import json
    resp = flask_client.post(
        "/api/filings/mark-read",
        data=json.dumps({"filing_ids": []}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"marked": 0}


def test_filing_detail_marks_unread_filing_as_read(flask_client, tmp_sqlite_db):
    """GET /filing/<id> marks an unread filing as read."""
    import database

    database.insert_filing({
        "accession_no": "D-1", "company": "DetailCo", "ticker": "X", "cik": "1",
        "filed_date": "2026-05-01", "item_codes": "5.02", "summary": "",
        "auto_category": "Compensation", "filing_url": "https://example.com",
        "raw_text": "", "matched_keywords": "", "urgent": False,
        "comp_details": None, "is_complex": False, "narrative_summary": None,
        "relevant_reason": None, "structured_summary": None,
    })

    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no = 'D-1'")
    conn.commit()
    conn.close()

    filing_id = database.get_filing_by_accession("D-1")["id"]

    # Confirm precondition
    assert database.get_filing_by_id(filing_id)["read_at"] is None

    # Hit the detail page
    resp = flask_client.get(f"/filing/{filing_id}")
    assert resp.status_code == 200

    # Now should be marked read
    assert database.get_filing_by_id(filing_id)["read_at"] is not None


def test_index_unread_param_filters_results(flask_client, tmp_sqlite_db):
    """GET /?unread=1 only shows filings with NULL read_at."""
    import database

    base = {
        "ticker": "X", "cik": "1", "filed_date": "2026-05-01",
        "item_codes": "5.02", "summary": "",
        "auto_category": "Compensation", "filing_url": "https://example.com",
        "raw_text": "", "matched_keywords": "", "urgent": False,
        "comp_details": None, "is_complex": False, "narrative_summary": None,
        "relevant_reason": None, "structured_summary": None,
    }
    database.insert_filing({**base, "accession_no": "Idx-Read", "company": "WasRead"})
    database.insert_filing({**base, "accession_no": "Idx-Unread", "company": "StillUnread"})

    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE accession_no = 'Idx-Read'")
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no = 'Idx-Unread'")
    conn.commit()
    conn.close()

    resp = flask_client.get("/?unread=1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "StillUnread" in body
    assert "WasRead" not in body
