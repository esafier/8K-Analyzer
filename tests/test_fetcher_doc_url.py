"""Tests that fetch_filing_text returns both text and the primary document URL."""
from unittest.mock import patch, Mock


def test_fetch_returns_text_and_doc_url():
    """fetch_filing_text should return (text, doc_url) tuple."""
    from fetcher import fetch_filing_text

    index_html = """
        <table class="tableFile">
          <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>
          <tr>
            <td>1</td><td>Form 8-K</td>
            <td><a href="/Archives/edgar/data/123/0001/acme-8k.htm">acme-8k.htm</a></td>
            <td>8-K</td>
          </tr>
        </table>
    """
    filing_body = "<html><body>Full 8-K body text with relevant content.</body></html>"

    mock_index = Mock(status_code=200, text=index_html)
    mock_index.raise_for_status = Mock()
    mock_doc = Mock(status_code=200, text=filing_body)
    mock_doc.raise_for_status = Mock()

    with patch("fetcher.requests.get", side_effect=[mock_index, mock_doc]):
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert "Full 8-K body text" in text
    assert doc_url == "https://www.sec.gov/Archives/edgar/data/123/0001/acme-8k.htm"


def test_fetch_returns_empty_on_failure():
    """When the index page has no 8-K doc link, return (empty_string, None)."""
    from fetcher import fetch_filing_text

    mock_index = Mock(status_code=200, text="<html><body>No table here</body></html>")
    mock_index.raise_for_status = Mock()

    with patch("fetcher.requests.get", return_value=mock_index):
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/index.htm", "123", "0001-23-000001"
        )

    assert text == ""
    assert doc_url is None
