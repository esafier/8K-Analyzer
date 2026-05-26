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
