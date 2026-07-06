"""Tests that EDGAR failures can't masquerade as 'zero departures', and that
the Item 5.02 section extraction captures full multi-executive sections."""
import requests
from unittest.mock import patch, Mock

import pytest


# --- get_edgar_departure_history failure signaling ---

def test_edgar_history_returns_none_on_network_failure():
    """Lookup failure must return None (retryable), not [] (looks like clean history)."""
    from fetcher import get_edgar_departure_history

    with patch("fetcher.requests.get",
               side_effect=requests.exceptions.ConnectionError("edgar down")):
        result = get_edgar_departure_history("0001234567")

    assert result is None


def test_edgar_history_empty_cik_returns_empty_list():
    """No CIK is genuinely 'nothing to look up' — empty list, not None."""
    from fetcher import get_edgar_departure_history

    assert get_edgar_departure_history("") == []


def test_get_departures_raises_on_edgar_failure():
    """get_departures_for_filing must raise (not return []) when EDGAR fails,
    so enrich_filing_departure_history leaves the row unstamped for retry."""
    from departures import get_departures_for_filing

    with patch("departures.get_edgar_departure_history", return_value=None):
        with pytest.raises(RuntimeError):
            get_departures_for_filing(cik="0001234567", current_accession="x")


def test_enrich_leaves_row_unstamped_on_edgar_failure():
    """A transient EDGAR failure returns None and never writes a count."""
    from departures import enrich_filing_departure_history

    with patch("departures.get_edgar_departure_history", return_value=None), \
         patch("database.update_departure_history") as mock_update:
        result = enrich_filing_departure_history(1, "0001234567", "0001234-26-000001")

    assert result is None
    mock_update.assert_not_called()


# --- Full Item 5.02 section extraction ---

def _filing_html(body):
    return f"<html><body>{body}</body></html>"


def test_502_section_captures_multiple_departures_beyond_800_chars():
    """Departures mentioned after the first 800 chars must be included."""
    from fetcher import _fetch_502_snippet

    filler = "The Company announced organizational changes. " * 25  # ~1150 chars
    body = (
        "Item 5.02 Departure of Directors or Certain Officers. "
        "On July 1, 2026, John Alpha resigned as Chief Executive Officer. "
        + filler +
        "Additionally, Mary Beta, Chief Financial Officer, will depart effective July 15, 2026. "
        "Item 9.01 Financial Statements and Exhibits. Exhibit 99.1 press release."
    )
    resp = Mock(status_code=200, text=_filing_html(body))
    resp.raise_for_status = Mock()

    with patch("fetcher.requests.get", return_value=resp):
        section = _fetch_502_snippet("0001234567", "0001234-26-000001", "doc.htm")

    assert "John Alpha" in section
    assert "Mary Beta" in section          # would have been cut by the old 800-char cap
    assert "Exhibit 99.1" not in section   # stops at the Item 9.01 heading


def test_502_section_ignores_in_prose_item_502_references():
    """'pursuant to Item 5.02(e)' inside the section must not truncate it."""
    from fetcher import _fetch_502_snippet

    body = (
        "Item 5.02 Departure of Directors or Certain Officers. "
        "This report is furnished pursuant to Item 5.02(e) of Form 8-K. "
        "On July 1, 2026, Jane Gamma was terminated as Chief Accounting Officer. "
        "Item 8.01 Other Events. Unrelated other content."
    )
    resp = Mock(status_code=200, text=_filing_html(body))
    resp.raise_for_status = Mock()

    with patch("fetcher.requests.get", return_value=resp):
        section = _fetch_502_snippet("0001234567", "0001234-26-000001", "doc.htm")

    assert "Jane Gamma" in section
    assert "Unrelated other content" not in section


def test_502_section_stops_at_signature_block():
    from fetcher import _fetch_502_snippet

    body = (
        "Item 5.02 Departure of Directors or Certain Officers. "
        "On July 1, 2026, Sam Delta resigned as President. "
        "Pursuant to the requirements of the Securities Exchange Act of 1934, "
        "the registrant has duly caused this report to be signed."
    )
    resp = Mock(status_code=200, text=_filing_html(body))
    resp.raise_for_status = Mock()

    with patch("fetcher.requests.get", return_value=resp):
        section = _fetch_502_snippet("0001234567", "0001234-26-000001", "doc.htm")

    assert "Sam Delta" in section
    assert "duly caused" not in section


def test_502_section_survives_incorporation_by_reference_boilerplate():
    """Regression: 'The information set forth in Item 1.01 ... is incorporated
    by reference into this Item 5.02' truncated the section to zero names."""
    from fetcher import _fetch_502_snippet

    body = (
        "Item 5.02 Departure of Directors. The information set forth in Item 1.01 "
        "of this Current Report is incorporated by reference into this Item 5.02. "
        "On July 1, 2026, John Alpha resigned as CEO and Mary Beta resigned as CFO. "
        "Item 9.01 Financial Statements and Exhibits. Exhibit list follows."
    )
    resp = Mock(status_code=200, text=f"<html><body>{body}</body></html>")
    resp.raise_for_status = Mock()

    with patch("fetcher.requests.get", return_value=resp):
        section = _fetch_502_snippet("123", "0001-26-000001", "d.htm")

    assert "John Alpha" in section
    assert "Mary Beta" in section
    assert "Exhibit list follows" not in section
