"""Tests for dashboard page handling: garbage page params must not 500,
and out-of-range pages must clamp to the last real page."""
from unittest.mock import patch


def _client(tmp_sqlite_db):
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def _insert_filings(n):
    from database import insert_filing
    for i in range(n):
        insert_filing({
            "accession_no": f"0001234-26-{i:06d}",
            "company": f"Company {i}",
            "ticker": "",
            "cik": "0001234567",
            "filed_date": "2026-06-01",
            "item_codes": "5.02",
            "summary": f"Filing {i}",
            "auto_category": "Management Change",
            "auto_subcategory": None,
            "filing_url": "https://example.com",
            "raw_text": "",
            "matched_keywords": "",
        })


def test_non_numeric_page_does_not_500(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    _insert_filings(3)
    resp = client.get("/?page=abc")
    assert resp.status_code == 200
    assert b"Company 0" in resp.data


def test_empty_page_param_does_not_500(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    _insert_filings(1)
    resp = client.get("/?page=")
    assert resp.status_code == 200


def test_zero_and_negative_page_clamp_to_first(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    _insert_filings(3)
    for bad in ("0", "-5"):
        resp = client.get(f"/?page={bad}")
        assert resp.status_code == 200
        assert b"Company 0" in resp.data


def test_over_range_page_clamps_to_last_page(tmp_sqlite_db):
    """With 3 filings (1 page), ?page=99 must show the filings, not an
    empty 'No filings found' dead-end with no pagination controls."""
    client = _client(tmp_sqlite_db)
    _insert_filings(3)
    resp = client.get("/?page=99")
    assert resp.status_code == 200
    assert b"No filings found" not in resp.data
    assert b"Company 0" in resp.data


def test_search_with_special_chars_keeps_pagination_links_valid(tmp_sqlite_db):
    """A search term containing '&' used to be interpolated raw into
    pagination hrefs, splitting the query string. Now it must be encoded."""
    from database import insert_filing
    client = _client(tmp_sqlite_db)
    insert_filing({
        "accession_no": "0001234-26-999999",
        "company": "AT&T Inc",
        "ticker": "T",
        "cik": "0001234567",
        "filed_date": "2026-06-01",
        "item_codes": "5.02",
        "summary": "CFO departed",
        "auto_category": "Management Change",
        "auto_subcategory": None,
        "filing_url": "https://example.com",
        "raw_text": "",
        "matched_keywords": "",
    })
    resp = client.get("/?search=AT%26T")
    assert resp.status_code == 200
    # The company matched, so pagination links rendered — and the ampersand
    # inside the search term must be %-encoded, not a raw query separator
    assert b"search=AT%26T" in resp.data
