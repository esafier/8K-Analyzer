"""Tests for exhibit fetching: fetch_filing_text should pull high-value
exhibits (EX-17 / EX-10 / EX-99) into the returned text so the LLM sees the
actual agreement terms, resignation letters, and press releases."""
from unittest.mock import patch, Mock

import fetcher
from fetcher import fetch_filing_text, _exhibit_sort_key


INDEX_WITH_EXHIBITS = """
    <table class="tableFile">
      <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>
      <tr>
        <td>1</td><td>Form 8-K</td>
        <td><a href="/Archives/edgar/data/123/0001/acme-8k.htm">acme-8k.htm</a></td>
        <td>8-K</td>
      </tr>
      <tr>
        <td>2</td><td>Separation Agreement</td>
        <td><a href="/Archives/edgar/data/123/0001/ex10-1.htm">ex10-1.htm</a></td>
        <td>EX-10.1</td>
      </tr>
      <tr>
        <td>3</td><td>Press Release</td>
        <td><a href="/Archives/edgar/data/123/0001/ex99-1.htm">ex99-1.htm</a></td>
        <td>EX-99.1</td>
      </tr>
      <tr>
        <td>4</td><td>Chart</td>
        <td><a href="/Archives/edgar/data/123/0001/chart.jpg">chart.jpg</a></td>
        <td>GRAPHIC</td>
      </tr>
      <tr>
        <td>5</td><td>XBRL</td>
        <td><a href="/Archives/edgar/data/123/0001/acme.xml">acme.xml</a></td>
        <td>EX-101.SCH</td>
      </tr>
    </table>
"""


def _ok(text):
    resp = Mock(status_code=200, text=text)
    resp.raise_for_status = Mock()
    return resp


def test_exhibits_appended_with_labels():
    """EX-10 and EX-99 exhibit text should be appended after the main body,
    each under a labeled section header. Graphics and XBRL rows are skipped."""
    responses = [
        _ok(INDEX_WITH_EXHIBITS),
        _ok("<html><body>Item 5.02 CFO resigned.</body></html>"),
        _ok("<html><body>Separation agreement: forfeits all unvested RSUs.</body></html>"),
        _ok("<html><body>Press release: CFO steps down effective today.</body></html>"),
    ]
    with patch("fetcher.requests.get", side_effect=responses), \
         patch("fetcher.time.sleep"):
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert "Item 5.02 CFO resigned." in text
    assert "===== EXHIBIT EX-10.1 =====" in text
    assert "forfeits all unvested RSUs" in text
    assert "===== EXHIBIT EX-99.1 =====" in text
    assert "steps down effective today" in text
    # Main body must come before exhibits
    assert text.index("CFO resigned") < text.index("EX-10.1")
    assert doc_url == "https://www.sec.gov/Archives/edgar/data/123/0001/acme-8k.htm"


def test_exhibit_priority_order():
    """EX-17 (resignation letters) outranks EX-10, which outranks EX-99."""
    assert _exhibit_sort_key("EX-17.1") < _exhibit_sort_key("EX-10.1")
    assert _exhibit_sort_key("EX-10.1") < _exhibit_sort_key("EX-99.1")
    assert _exhibit_sort_key("ex-99.2") == _exhibit_sort_key("EX-99.1")
    # Unknown exhibit types rank last
    assert _exhibit_sort_key("EX-3.1") > _exhibit_sort_key("EX-99.1")


def test_exhibit_fetch_failure_keeps_main_text():
    """A failing exhibit fetch must not lose the main document text."""
    import requests as _requests

    responses = [
        _ok(INDEX_WITH_EXHIBITS),
        _ok("<html><body>Item 5.02 main body survives.</body></html>"),
    ]

    call_count = {"n": 0}

    def fake_get(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return responses[call_count["n"] - 1]
        raise _requests.exceptions.ConnectionError("exhibit fetch died")

    with patch("fetcher.requests.get", side_effect=fake_get), \
         patch("fetcher.time.sleep"):
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert "main body survives" in text
    assert doc_url is not None


def test_oversized_exhibit_is_truncated():
    """Exhibits longer than MAX_EXHIBIT_CHARS get truncated with a marker."""
    huge = "severance detail " * 5000  # ~85k chars, over the 30k cap
    responses = [
        _ok(INDEX_WITH_EXHIBITS),
        _ok("<html><body>Item 5.02 body.</body></html>"),
        _ok(f"<html><body>{huge}</body></html>"),
        _ok("<html><body>press release text</body></html>"),
    ]
    with patch("fetcher.requests.get", side_effect=responses), \
         patch("fetcher.time.sleep"):
        text, _ = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert "[exhibit truncated]" in text
    assert len(text) <= fetcher.MAX_FILING_TEXT_CHARS + 200  # headers/markers slack


def test_no_exhibits_behaves_as_before():
    """Filings without exhibit rows return just the main document text."""
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
    responses = [
        _ok(index_html),
        _ok("<html><body>Item 5.02 solo body.</body></html>"),
    ]
    with patch("fetcher.requests.get", side_effect=responses), \
         patch("fetcher.time.sleep"):
        text, _ = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert "solo body" in text
    assert "EXHIBIT" not in text


def test_xbrl_and_cover_page_exhibits_are_not_fetched():
    """Regression: startswith('EX-10') also matched EX-101/EX-104 (XBRL and
    cover-page artifacts), burning exhibit slots ahead of real press releases."""
    assert _exhibit_sort_key("EX-101.SCH") == 3  # not a fetchable priority
    assert _exhibit_sort_key("EX-104") == 3
    assert _exhibit_sort_key("EX-10.1") == 1
    assert _exhibit_sort_key("EX-17.1") == 0
    assert _exhibit_sort_key("EX-99.1") == 2
