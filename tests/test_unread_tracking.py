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
