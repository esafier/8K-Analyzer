"""Tests for the departure_extractions cache table."""
import json
import sqlite3


def test_table_exists_after_init(tmp_sqlite_db):
    """initialize_database must create the departure_extractions table."""
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='departure_extractions'")
    row = cursor.fetchone()
    conn.close()
    assert row is not None, "departure_extractions table was not created"


def test_get_returns_none_when_missing(tmp_sqlite_db):
    from database import get_cached_departure_extraction
    assert get_cached_departure_extraction("0001234-25-000999") is None


def test_upsert_then_get_roundtrip(tmp_sqlite_db):
    from database import upsert_departure_extraction, get_cached_departure_extraction

    extractions = [
        {"date": "2025-09-12", "person": "Jane Doe", "position": "CFO", "reason": "Resigned to pursue other opportunities"},
    ]
    upsert_departure_extraction(
        accession_number="0001234-25-000123",
        cik="0001234567",
        filed_date="2025-09-12",
        extractions=extractions,
        has_error=False,
    )

    cached = get_cached_departure_extraction("0001234-25-000123")
    assert cached is not None
    assert cached["cik"] == "0001234567"
    assert cached["filed_date"] == "2025-09-12"
    assert cached["has_error"] == 0  # SQLite stores bool as int
    assert cached["extractions"] == extractions  # JSON parsed back


def test_upsert_overwrites_existing_row(tmp_sqlite_db):
    """Calling upsert twice with the same accession should replace, not duplicate."""
    from database import upsert_departure_extraction, get_cached_departure_extraction

    upsert_departure_extraction("0001234-25-000123", "0001234567", "2025-09-12", [], has_error=True)
    upsert_departure_extraction(
        "0001234-25-000123", "0001234567", "2025-09-12",
        [{"date": "2025-09-12", "person": "Jane", "position": "CFO", "reason": "Retired"}],
        has_error=False,
    )

    cached = get_cached_departure_extraction("0001234-25-000123")
    assert cached["has_error"] == 0
    assert len(cached["extractions"]) == 1


def test_returns_real_dict_supports_get(tmp_sqlite_db):
    """Per CLAUDE.md: cached row must be a real dict (.get works), not sqlite3.Row."""
    from database import upsert_departure_extraction, get_cached_departure_extraction
    upsert_departure_extraction("0001234-25-000123", "0001234567", "2025-09-12", [], False)
    cached = get_cached_departure_extraction("0001234-25-000123")
    assert cached.get("nonexistent_key") is None
    assert cached.get("cik") == "0001234567"
