"""An empty OpenAI account must stop the run, not degrade it.

From 2026-09-14 every extraction returned "429 — You have no credits
remaining". Each was caught as an ordinary per-filing failure: the filing was
stored with a keyword summary and no verdict, and the daily job went green for
ten days while 273 8-Ks never reached the inbox.
"""
from unittest.mock import MagicMock, patch

import httpx2 as httpx
import openai
import pytest

import database
import ingest
import llm


def _rate_limit(code, message):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError(
        f"Error code: 429 - {{'error': {{'message': '{message}', 'code': '{code}'}}}}",
        response=response,
        body={"message": message, "code": code, "type": code},
    )


NO_CREDITS = _rate_limit("insufficient_quota",
                         "You have no credits remaining. Add credits to continue using the API.")
SLOW_DOWN = _rate_limit("rate_limit_exceeded", "Rate limit reached for requests per min.")


def _client_raising(error):
    client = MagicMock()
    client.chat.completions.create.side_effect = error
    return client


def test_quota_refusal_is_told_apart_from_a_rate_limit():
    assert llm._is_out_of_credits(NO_CREDITS)
    assert not llm._is_out_of_credits(SLOW_DOWN)


def test_extraction_raises_when_the_account_is_empty():
    with patch("llm._client", return_value=_client_raising(NO_CREDITS)):
        with pytest.raises(llm.OutOfCredits):
            llm.classify_and_summarize("filing text")


def test_an_ordinary_failure_still_costs_only_that_filing():
    with patch("llm._client", return_value=_client_raising(SLOW_DOWN)):
        assert llm.classify_and_summarize("filing text") is None


def test_judge_raises_when_the_account_is_empty():
    with patch("llm._client", return_value=_client_raising(NO_CREDITS)):
        with pytest.raises(llm.OutOfCredits):
            llm.judge_filing({"facts": {}})


def test_pipeline_lets_it_through():
    """analyze_filing swallows per-filing errors; this one is the run's."""
    import pipeline
    with patch("llm._client", return_value=_client_raising(NO_CREDITS)):
        with pytest.raises(llm.OutOfCredits):
            pipeline.analyze_filing({"company": "Co", "raw_text": "text"})


def test_pipeline_lets_it_through_from_the_judge():
    import pipeline
    with patch("judge.judge", side_effect=llm.OutOfCredits("empty")):
        with pytest.raises(llm.OutOfCredits):
            pipeline._judge({"company": "Co"}, {}, {}, None, None)


def test_ingest_fails_the_window_but_keeps_what_it_stored(tmp_sqlite_db, monkeypatch):
    metadata = [{"accession_no": f"a-{i}", "company": f"Co {i}", "ticker": "AAA",
                 "cik": "1", "filed_date": "2026-09-15", "item_codes": "5.02",
                 "filing_url": "u", "items_list": ["5.02"]} for i in range(3)]

    def filter_until_broke(*args, on_analyzed=None, stats=None, **kwargs):
        on_analyzed(dict(metadata[0], raw_text="real text"))
        raise llm.OutOfCredits("OpenAI account is out of credits")

    monkeypatch.setattr(ingest, "fetch_filings", lambda s, e: metadata)
    monkeypatch.setattr(ingest, "filter_filings", filter_until_broke)

    with pytest.raises(ingest.IngestBlocked, match="out of credits"):
        ingest.ingest_range("2026-09-15", "2026-09-15", enrich=False)
    assert database.get_filing_count() == 1


def test_daily_goes_red_and_keeps_the_watermark(tmp_sqlite_db, monkeypatch):
    import daily
    ingest.mark_ingested("2026-09-14")
    before = database.get_app_status(ingest.WATERMARK_KEY)

    def broke(*a, **k):
        raise ingest.IngestBlocked("OpenAI account is out of credits")

    monkeypatch.setattr(daily, "ingest_range", broke)
    monkeypatch.setattr(daily, "pending_window", lambda: ("2026-09-15", "2026-09-16"))
    monkeypatch.setattr("form4.scan_range", lambda s, e: [])
    monkeypatch.setattr("outcomes.run", lambda: {})

    with pytest.raises(ingest.IngestBlocked):
        daily.run(send_digest=False)
    assert database.get_app_status(ingest.WATERMARK_KEY) == before


def test_daily_goes_red_when_only_the_form4_judge_hits_it(tmp_sqlite_db, monkeypatch):
    import daily
    monkeypatch.setattr(daily, "ingest_range", lambda *a, **k: {"new": 0})
    monkeypatch.setattr(daily, "pending_window", lambda: ("2026-09-15", "2026-09-16"))

    def broke(start, end):
        raise llm.OutOfCredits("OpenAI account is out of credits")

    monkeypatch.setattr("form4.scan_range", broke)
    monkeypatch.setattr("outcomes.run", lambda: {})

    with pytest.raises(ingest.IngestBlocked, match="out of credits"):
        daily.run(send_digest=False)


def test_backfill_exits_non_zero(monkeypatch):
    import backfill
    monkeypatch.setattr("backfill.initialize_database", lambda: None)
    monkeypatch.setattr("backfill.ingest.ingest_range",
                        lambda *a, **k: (_ for _ in ()).throw(ingest.IngestBlocked("out of credits")))
    monkeypatch.setattr("backfill.form4.scan_range", lambda s, e: [{"stored": 1}])
    monkeypatch.setattr("sys.argv", ["backfill.py", "--start", "2026-07-11", "--end", "2026-07-12"])
    assert backfill.main() == 2


def test_reanalyze_stops_at_the_first_refusal(tmp_sqlite_db, monkeypatch):
    import reanalyze
    for i in range(3):
        database.insert_filing({
            "accession_no": f"r-{i}", "company": f"Co {i}", "ticker": "AAA", "cik": "1",
            "filed_date": "2026-09-15", "item_codes": "5.02", "filing_url": "u",
            "raw_text": "t", "summary": "keyword fallback",
        })
    calls = []

    def broke(row):
        calls.append(row["accession_no"])
        raise llm.OutOfCredits("OpenAI account is out of credits")

    monkeypatch.setattr(reanalyze, "analyze_filing", broke)
    stats = reanalyze.run()
    assert len(calls) == 1
    assert stats["stopped"] and stats["scored"] == 0
