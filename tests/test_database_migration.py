"""Tests that database migrations add the expected columns."""
import sqlite3


def test_new_columns_exist_after_init(tmp_sqlite_db):
    """After initialize_database() runs, the filings table must have the new v3 columns."""
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(filings)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert "filing_document_url" in columns, "filing_document_url column missing"
    assert "is_complex" in columns, "is_complex column missing"
    assert "narrative_summary" in columns, "narrative_summary column missing"
    assert "relevant_reason" in columns, "relevant_reason column missing"
    assert "structured_summary" in columns, "structured_summary column missing"


def test_migration_is_idempotent(tmp_sqlite_db):
    """Calling initialize_database() a second time must not fail (columns already exist)."""
    import database
    # initialize_database was called by the fixture; call it again
    database.initialize_database()  # Should not raise
