"""Tests for test_prompt.py's database access.

The harness used to open sqlite3 directly, which made it blind to the Render
PostgreSQL archive. These tests pin the two properties that fix depends on:
rows come back as real dicts (so both bracket access and .get() work on either
backend, per CLAUDE.md), and the source database is reported before any tokens
are spent.
"""
import pytest

import database
import test_prompt


def _insert_filing(accession, company, filed_date, raw_text):
    conn = database.get_connection()
    cursor = conn.cursor()
    p = database._placeholder()
    cursor.execute(
        f"INSERT INTO filings (accession_no, company, cik, filed_date, "
        f"item_codes, raw_text) VALUES ({p}, {p}, {p}, {p}, {p}, {p})",
        (accession, company, "0000000001", filed_date, "5.02", raw_text),
    )
    conn.commit()
    conn.close()


def test_returns_real_dicts_not_sqlite_rows(tmp_sqlite_db):
    """sqlite3.Row supports row["k"] but not row.get("k"). Downstream code and
    anything a future caller writes should be able to use either."""
    _insert_filing("0001-24-000001", "Acme Corp", "2026-01-05", "body text")

    filings = test_prompt.get_test_filings()

    assert len(filings) == 1
    row = filings[0]
    assert type(row) is dict
    assert row["company"] == "Acme Corp"
    assert row.get("company") == "Acme Corp"
    assert row.get("no_such_column") is None


def test_skips_filings_without_text(tmp_sqlite_db):
    """A filing with no raw_text has nothing to send the LLM."""
    _insert_filing("0001-24-000001", "Has Text", "2026-01-05", "body text")
    _insert_filing("0001-24-000002", "No Text", "2026-01-06", "")
    _insert_filing("0001-24-000003", "Null Text", "2026-01-07", None)

    companies = [f["company"] for f in test_prompt.get_test_filings()]

    assert companies == ["Has Text"]


def test_orders_newest_first_and_honors_count(tmp_sqlite_db):
    for i, date in enumerate(["2026-01-05", "2026-03-05", "2026-02-05"]):
        _insert_filing(f"0001-24-00000{i}", f"Co {date}", date, "body")

    assert [f["filed_date"] for f in test_prompt.get_test_filings()] == [
        "2026-03-05", "2026-02-05", "2026-01-05",
    ]
    assert [f["filed_date"] for f in test_prompt.get_test_filings(count=2)] == [
        "2026-03-05", "2026-02-05",
    ]


def test_count_is_coerced_to_int(tmp_sqlite_db):
    """The count lands in the SQL string, so it must never carry raw text."""
    _insert_filing("0001-24-000001", "Acme Corp", "2026-01-05", "body")

    with pytest.raises(ValueError):
        test_prompt.get_test_filings(count="1; DROP TABLE filings")


def test_describe_database_names_the_backend(tmp_sqlite_db, monkeypatch):
    assert "SQLite" in test_prompt.describe_database()

    monkeypatch.setattr(database, "_using_postgres", lambda: True)
    assert "PostgreSQL" in test_prompt.describe_database()
