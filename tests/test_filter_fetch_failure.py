"""Tests for what Stage 2 does when SEC won't hand over a filing's text.

A rate-limit block makes fetch_text_func return ("", None) for a long run of
filings. The old code kept only 5.02 rows and dropped everything else on the
floor — no database row, no count, no log line. An in-scope 1.01/1.02 filing
caught by the block simply ceased to exist, which is indistinguishable from
"SEC had nothing that day".

Now anything in scope is parked as a retryable row so "Retry Missing
Summaries" can find it later, and the counts stay honest.
"""
from unittest.mock import patch


def _meta(items, company="Blocked Corp", accession="0001-26-000042"):
    return [{
        "accession_no": accession,
        "company": company, "ticker": "BLK", "cik": "123",
        "filed_date": "2026-07-01", "item_codes": ",".join(items),
        "filing_url": "https://sec.gov/index.htm",
        "items_list": items,
    }]


def _fetch_blocked(url, cik, accession):
    """What fetch_filing_text returns while SEC has the IP in the penalty box."""
    return "", None


def test_502_fetch_failure_is_kept_for_retry():
    """The original behavior, still intact."""
    from filter import filter_filings

    result = filter_filings(_meta(["5.02"]), fetch_text_func=_fetch_blocked)

    assert len(result) == 1
    assert result[0]["raw_text"] == ""
    assert result[0]["summary"] == "SEC rate-limited — pending retry"
    assert result[0]["auto_category"] == "Management Change"


def test_101_fetch_failure_is_no_longer_silently_dropped():
    """The bug: an in-scope 1.01 filing vanished entirely when SEC blocked us."""
    from filter import filter_filings

    result = filter_filings(_meta(["1.01"]), fetch_text_func=_fetch_blocked)

    assert len(result) == 1, "in-scope filing was dropped instead of parked for retry"
    assert result[0]["raw_text"] == ""
    assert result[0]["summary"] == "SEC rate-limited — pending retry"


def test_102_fetch_failure_is_kept_for_retry():
    from filter import filter_filings

    result = filter_filings(_meta(["1.02"]), fetch_text_func=_fetch_blocked)

    assert len(result) == 1
    assert result[0]["summary"] == "SEC rate-limited — pending retry"


def test_801_only_fetch_failure_is_still_dropped():
    """8.01 is the high-volume, low-hit-rate catch-all. A textless 8.01-only
    filing is dropped on success too — keeping it would flood the retry queue
    with rows that are worthless even once fetched."""
    from filter import filter_filings

    result = filter_filings(_meta(["8.01"]), fetch_text_func=_fetch_blocked)

    assert result == []


def test_fetch_failure_never_calls_the_llm():
    """No text means nothing to classify — don't burn tokens on an empty string."""
    from filter import filter_filings

    with patch("filter.classify_and_summarize") as mock_llm:
        filter_filings(_meta(["5.02", "1.01"]), fetch_text_func=_fetch_blocked)

    mock_llm.assert_not_called()


def test_fetch_failure_is_reported_in_the_logs(capsys):
    """The count that used to be invisible must now be loud — it's the cue to
    press "Retry Missing Summaries"."""
    from filter import filter_filings

    filter_filings(_meta(["5.02"]), fetch_text_func=_fetch_blocked)

    out = capsys.readouterr().out
    assert "Stage 2 WARNING" in out
    assert "Retry Missing Summaries" in out


def test_no_warning_when_every_fetch_succeeds(capsys):
    from filter import filter_filings

    def _fetch_ok(url, cik, accession):
        return "The CFO submitted his resignation effective immediately.", "https://sec.gov/d.htm"

    with patch("filter.classify_and_summarize", return_value=None):
        filter_filings(_meta(["5.02"]), fetch_text_func=_fetch_ok)

    assert "Stage 2 WARNING" not in capsys.readouterr().out
