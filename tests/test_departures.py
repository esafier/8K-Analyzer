"""Tests for the departures pipeline and prose renderer."""
from unittest.mock import patch


def test_render_prose_lines_basic():
    """render_prose_lines turns extraction dicts into clean bullet text."""
    from departures import render_prose_lines

    deps = [
        {
            "date": "2025-09-12", "person": "Jane Doe", "position": "CFO",
            "reason": "Resigned to pursue other opportunities",
            "_accession": "0001234-25-000123", "_filing_url": "https://sec.gov/x",
            "_is_current_filing": False, "_error": False,
        }
    ]
    lines = render_prose_lines(deps)

    assert len(lines) == 1
    line = lines[0]
    assert "2025-09-12" in line
    assert "Jane Doe" in line
    assert "CFO" in line
    assert "Resigned to pursue other opportunities" in line
    assert "https://sec.gov/x" in line
    assert "(this filing)" not in line


def test_render_prose_marks_current_filing():
    from departures import render_prose_lines

    deps = [{
        "date": "2025-01-01", "person": "X", "position": "Y", "reason": "Z",
        "_accession": "a", "_filing_url": "u", "_is_current_filing": True, "_error": False,
    }]
    lines = render_prose_lines(deps)
    assert "(this filing)" in lines[0]


def test_render_prose_handles_failed_extraction():
    """Failed extractions render as a placeholder with the SEC link preserved."""
    from departures import render_prose_lines

    deps = [{
        "date": "2024-06-15", "person": None, "position": None, "reason": None,
        "_accession": "0001234-24-000099", "_filing_url": "https://sec.gov/y",
        "_is_current_filing": False, "_error": True,
    }]
    lines = render_prose_lines(deps)
    assert len(lines) == 1
    assert "extraction failed" in lines[0].lower()
    assert "2024-06-15" in lines[0]
    assert "https://sec.gov/y" in lines[0]


def test_get_departures_for_filing_uses_cache(tmp_sqlite_db):
    """If an accession is already cached, the LLM should NOT be called for it."""
    from database import upsert_departure_extraction
    from departures import get_departures_for_filing

    upsert_departure_extraction(
        "0001234-25-000111", "0001234567", "2025-08-01",
        [{"date": "2025-08-01", "person": "Cached Person", "position": "CEO", "reason": "Retired"}],
        has_error=False,
    )

    fake_history = [
        {"filing_date": "2025-08-01", "items": "5.02", "accession_no": "0001234-25-000111", "snippet": "ignored — cached"},
        {"filing_date": "2024-03-15", "items": "5.02", "accession_no": "0001234-24-000050", "snippet": "Item 5.02 ... Bob Smith ... resigned ..."},
    ]

    fake_extract = {
        "departures": [{"date": "2024-03-15", "person": "Bob Smith", "position": "COO", "reason": "Resigned"}],
        "error": False, "_tokens_in": 50, "_tokens_out": 25,
    }

    with patch("departures.get_edgar_departure_history", return_value=fake_history), \
         patch("departures.extract_departures", return_value=fake_extract) as mock_extract:
        result = get_departures_for_filing(cik="0001234567", current_accession="0001234-25-XXXXXX")

    assert mock_extract.call_count == 1

    assert len(result) == 2
    assert result[0]["date"] == "2025-08-01"
    assert result[0]["person"] == "Cached Person"
    assert result[1]["person"] == "Bob Smith"


def test_get_departures_marks_current_filing(tmp_sqlite_db):
    """When current_accession matches a result, _is_current_filing must be True."""
    from departures import get_departures_for_filing

    fake_history = [{
        "filing_date": "2025-09-12", "items": "5.02",
        "accession_no": "0001234-25-CURRENT", "snippet": "Item 5.02 ... Jane ...",
    }]
    fake_extract = {
        "departures": [{"date": "2025-09-12", "person": "Jane", "position": "CFO", "reason": "Quit"}],
        "error": False, "_tokens_in": 0, "_tokens_out": 0,
    }

    with patch("departures.get_edgar_departure_history", return_value=fake_history), \
         patch("departures.extract_departures", return_value=fake_extract):
        result = get_departures_for_filing(cik="0001234567", current_accession="0001234-25-CURRENT")

    assert len(result) == 1
    assert result[0]["_is_current_filing"] is True


def test_get_departures_handles_empty_history(tmp_sqlite_db):
    from departures import get_departures_for_filing

    with patch("departures.get_edgar_departure_history", return_value=[]):
        result = get_departures_for_filing(cik="0001234567", current_accession="x")
    assert result == []


def test_render_prose_escapes_html():
    """Values interpolated into HTML must be escaped to prevent XSS."""
    from departures import render_prose_lines

    deps = [{
        "date": "2025-01-01",
        "person": "<script>alert(1)</script>",
        "position": "CFO & Director",
        "reason": "\"Quoted\" reason",
        "_accession": "x", "_filing_url": "https://sec.gov/y?a=1&b=2",
        "_is_current_filing": False, "_error": False,
    }]
    line = render_prose_lines(deps)[0]

    # The raw script tag must NOT appear unescaped
    assert "<script>" not in line
    assert "&lt;script&gt;" in line
    # Ampersands and quotes also escaped
    assert "&amp;" in line  # both "& Director" and "?a=1&b=2"
    assert "&quot;" in line


def test_pipeline_handles_thread_exception(tmp_sqlite_db):
    """A raise from extract_departures inside a thread must not crash the pipeline."""
    from departures import get_departures_for_filing

    fake_history = [{
        "filing_date": "2025-01-01", "items": "5.02",
        "accession_no": "0001234-25-BOOM", "snippet": "Item 5.02 ...",
    }]

    with patch("departures.get_edgar_departure_history", return_value=fake_history), \
         patch("departures.extract_departures", side_effect=RuntimeError("boom")):
        result = get_departures_for_filing(cik="0001234567", current_accession="x")

    # Should produce an error placeholder row, not raise
    assert len(result) == 1
    assert result[0]["_error"] is True
    assert result[0]["_accession"] == "0001234-25-BOOM"
