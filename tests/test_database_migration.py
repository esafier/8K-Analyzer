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


def test_insert_and_read_new_fields(tmp_sqlite_db):
    """Insert a filing with the new fields set; read it back and verify."""
    import database
    from summary_utils import serialize_subcategories

    filing_data = {
        "accession_no": "0001234567-26-000001",
        "company": "Test Co",
        "ticker": "TEST",
        "cik": "0001234567",
        "filed_date": "2026-04-16",
        "item_codes": "5.02",
        "summary": "Legacy summary string (kept for display fallback).",
        "auto_category": "Both",
        "auto_subcategory": serialize_subcategories(["CFO Departure", "CFO Appointment"]),
        "filing_url": "https://www.sec.gov/Archives/.../index.htm",
        "filing_document_url": "https://www.sec.gov/Archives/.../filing.htm",
        "raw_text": "Full filing text.",
        "matched_keywords": "resigned,appointed",
        "urgent": True,
        "comp_details": None,
        "is_complex": False,
        "narrative_summary": None,
        "relevant_reason": None,
        "structured_summary": '{"departures":[{"name":"J. Smith"}],"appointments":[{"name":"J. Doe"}]}',
    }

    database.insert_filing(filing_data)

    # Read back by accession
    row = database.get_filing_by_accession("0001234567-26-000001")
    assert row is not None
    assert row["filing_document_url"] == "https://www.sec.gov/Archives/.../filing.htm"
    assert row["is_complex"] in (0, False)
    assert row["narrative_summary"] is None
    assert row["structured_summary"].startswith('{"departures"')
    assert row["auto_subcategory"] == '["CFO Departure", "CFO Appointment"]'
