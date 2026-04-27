"""Tests that fetch_filing_text returns both text and the primary document URL."""
import requests
from unittest.mock import patch, Mock


def _http_error(status, retry_after=None):
    """Build a Mock that raises HTTPError with a given status code on raise_for_status()."""
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    resp = Mock(status_code=status, text="", headers=headers)
    err = requests.exceptions.HTTPError(f"{status} Client Error")
    err.response = Mock(status_code=status, headers=headers)
    resp.raise_for_status = Mock(side_effect=err)
    return resp


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


# --- Retry-on-429 behavior ---
# SEC rate-limits aggressively on shared cloud IPs. fetch_filing_text must
# retry transient 429s with backoff so a momentary throttle doesn't permanently
# leave a filing with blank summaries in the DB.

def test_fetch_retries_index_page_on_429_then_succeeds():
    """A single 429 on the index fetch should be retried, not give up."""
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
    filing_body = "<html><body>Recovered after a 429.</body></html>"

    rate_limited = _http_error(429)
    ok_index = Mock(status_code=200, text=index_html)
    ok_index.raise_for_status = Mock()
    ok_doc = Mock(status_code=200, text=filing_body)
    ok_doc.raise_for_status = Mock()

    # Sequence: index 429 -> index 200 -> doc 200
    with patch("fetcher.requests.get", side_effect=[rate_limited, ok_index, ok_doc]), \
         patch("fetcher.time.sleep"):  # don't actually sleep in tests
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert "Recovered after a 429" in text
    assert doc_url == "https://www.sec.gov/Archives/edgar/data/123/0001/acme-8k.htm"


def test_fetch_retries_document_on_429_then_succeeds():
    """A 429 on the *document* fetch (not just the index) should also be retried."""
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
    filing_body = "<html><body>Doc finally fetched.</body></html>"

    ok_index = Mock(status_code=200, text=index_html)
    ok_index.raise_for_status = Mock()
    rate_limited_doc = _http_error(429)
    ok_doc = Mock(status_code=200, text=filing_body)
    ok_doc.raise_for_status = Mock()

    # Sequence: index 200 -> doc 429 -> doc 200
    with patch("fetcher.requests.get", side_effect=[ok_index, rate_limited_doc, ok_doc]), \
         patch("fetcher.time.sleep"):
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert "Doc finally fetched" in text


def test_fetch_gives_up_after_max_429_retries():
    """If 429s never stop, eventually return ('', None) instead of looping forever."""
    from fetcher import fetch_filing_text

    # Always 429 — should hit retry cap and bail out
    with patch("fetcher.requests.get", side_effect=lambda *a, **kw: _http_error(429)), \
         patch("fetcher.time.sleep"):
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert text == ""
    assert doc_url is None


def test_fetch_honors_retry_after_header_on_429():
    """When SEC sends Retry-After: N, sleep at least N seconds before retrying."""
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
    filing_body = "<html><body>OK</body></html>"

    rate_limited = _http_error(429, retry_after=7)
    ok_index = Mock(status_code=200, text=index_html)
    ok_index.raise_for_status = Mock()
    ok_doc = Mock(status_code=200, text=filing_body)
    ok_doc.raise_for_status = Mock()

    with patch("fetcher.requests.get", side_effect=[rate_limited, ok_index, ok_doc]), \
         patch("fetcher.time.sleep") as mock_sleep:
        fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    # At least one sleep call must have been >= 7 (the Retry-After value)
    sleep_durations = [call.args[0] for call in mock_sleep.call_args_list if call.args]
    assert any(d >= 7 for d in sleep_durations), \
        f"Expected a sleep >= 7s honoring Retry-After, got: {sleep_durations}"


def test_search_and_filing_headers_use_same_user_agent():
    """Both EFTS search and filing-document fetches must use the SEC-compliant UA.
    Previously REQUEST_HEADERS used a fake browser UA; SEC throttles non-compliant agents."""
    from fetcher import REQUEST_HEADERS, FILING_HEADERS

    assert REQUEST_HEADERS["User-Agent"] == FILING_HEADERS["User-Agent"], \
        "Search and filing fetch should use the same SEC-compliant User-Agent"
