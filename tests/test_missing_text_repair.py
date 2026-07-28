"""Tests for the queries behind "Retry Missing Summaries".

Two separate traps caused the same symptom — "I clicked the button and
nothing happened":

  1. get_filings_missing_text() defaulted to the last 7 days, so rows
     stranded by a rate-limit block weeks earlier were unreachable. The
     button reported success while never seeing them.
  2. get_filings_for_resummarize() only returns rows that HAVE stored text,
     so "Re-Summarize" structurally cannot fix a textless row — it is the
     wrong button, not a broken one.
"""
import database


def _insert(accession, filed_date, raw_text, company="Acme Corp"):
    database.insert_filing({
        "accession_no": accession,
        "company": company,
        "ticker": "ACME",
        "cik": "123",
        "filed_date": filed_date,
        "item_codes": "5.02",
        "summary": "SEC rate-limited — pending retry" if not raw_text else "Real summary",
        "auto_category": "Management Change",
        "auto_subcategory": None,
        "filing_url": f"https://sec.gov/{accession}-index.htm",
        "raw_text": raw_text,
        "matched_keywords": "item 5.02",
    })


def test_missing_text_query_reaches_old_rows_by_default(tmp_sqlite_db):
    """With no dates, the repair query must span all of history.

    A block that happened in March strands March rows; a 7-day default meant
    the only way to reach them was to already know the date it broke.
    """
    _insert("0001-26-000001", "2026-03-18", "")
    _insert("0001-26-000002", "2026-07-01", "")
    _insert("0001-26-000003", "2026-07-01", "Full filing text here")

    rows = database.get_filings_missing_text()

    accessions = {r["accession_no"] for r in rows}
    assert accessions == {"0001-26-000001", "0001-26-000002"}


def test_missing_text_query_still_honors_an_explicit_range(tmp_sqlite_db):
    _insert("0001-26-000001", "2026-03-18", "")
    _insert("0001-26-000002", "2026-07-01", "")

    rows = database.get_filings_missing_text("2026-06-01", "2026-07-31")

    assert [r["accession_no"] for r in rows] == ["0001-26-000002"]


def test_missing_text_query_treats_null_and_empty_the_same(tmp_sqlite_db):
    """Postgres rows arrive as NULL, SQLite ones as ''. Both are stranded."""
    _insert("0001-26-000001", "2026-07-01", "")
    _insert("0001-26-000002", "2026-07-01", None)

    assert len(database.get_filings_missing_text()) == 2


def test_count_matches_the_query(tmp_sqlite_db):
    """The number shown on the backfill page must equal what the job will touch."""
    _insert("0001-26-000001", "2026-03-18", "")
    _insert("0001-26-000002", "2026-07-01", "")
    _insert("0001-26-000003", "2026-07-01", "Has text")

    assert database.count_filings_missing_text() == len(database.get_filings_missing_text())
    assert database.count_filings_missing_text() == 2


def test_count_is_zero_when_nothing_is_stranded(tmp_sqlite_db):
    _insert("0001-26-000003", "2026-07-01", "Has text")
    assert database.count_filings_missing_text() == 0


def test_resummarize_query_cannot_see_stranded_rows(tmp_sqlite_db):
    """Documents why "Re-Summarize" appears to do nothing on broken rows.

    It is not a bug in the LLM step — the rows never make it out of the SQL.
    """
    _insert("0001-26-000001", "2026-07-01", "")
    _insert("0001-26-000002", "2026-07-01", "Full filing text here")

    rows = database.get_filings_for_resummarize("2026-07-01", "2026-07-01")

    assert [r["accession_no"] for r in rows] == ["0001-26-000002"]


def test_missing_text_rows_expose_what_the_refetch_needs(tmp_sqlite_db):
    """The retry job re-fetches from SEC, so it needs the URL/CIK/accession."""
    _insert("0001-26-000001", "2026-07-01", "")

    row = database.get_filings_missing_text()[0]

    assert row["filing_url"]
    assert row["cik"] == "123"
    assert row["accession_no"] == "0001-26-000001"
    assert row["id"]
