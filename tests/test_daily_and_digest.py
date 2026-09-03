"""Tests for the scheduled job, the digest, and the evaluation harness.

Two behaviours here are the difference between a system and a cron job that
lies:

  1. The ingest window comes from a watermark, so Friday is never skipped and
     no window is paid for twice.
  2. A run where SEC blocked most fetches fails LOUDLY. Reporting success on
     an empty day is indistinguishable, in the logs, from a genuinely quiet
     one — and that is how a broken feed goes unnoticed for weeks.
"""
import json
from datetime import datetime, timedelta

import pytest

import database
import digest
import ingest


def _add(accession, score=8, company="Acme Corp", filed_date=None, verdict="DEEP_LOOK"):
    database.insert_filing({
        "accession_no": accession, "company": company, "ticker": "ACME",
        "cik": "0001234567",
        "filed_date": filed_date or datetime.now().strftime("%Y-%m-%d"),
        "item_codes": "5.02", "summary": "CFO walks from $4.2M unvested.",
        "auto_category": "Management Change", "filing_url": "https://sec.gov/i.htm",
        "filing_document_url": "https://sec.gov/doc.htm", "raw_text": "text",
        "triage_verdict": verdict, "signal_score": score, "signal_direction": "BEARISH",
        "top_signal": "CFO walks from $4.2M unvested.",
        "signal_types": "FORFEITURE_EXIT",
        "signals_json": json.dumps([{"type": "FORFEITURE_EXIT", "direction": "BEARISH",
                                     "severity": 5, "evidence": "forfeits unvested comp",
                                     "data": {}}]),
    })
    return database.get_filing_by_accession(accession)["id"]


def _fake_filter(rows):
    """Stand-in for filter_filings that honours the incremental-store callback.

    ingest_range no longer loops over the returned list — it stores each
    filing the moment it is analyzed, so a run killed mid-way keeps what it
    already paid for. A fake that ignores on_analyzed would store nothing.
    """
    def _filter(*args, on_analyzed=None, stats=None, **kwargs):
        for row in rows:
            if on_analyzed:
                on_analyzed(row)
        return rows
    return _filter


# ---------------------------------------------------------------------------
# The ingest window
# ---------------------------------------------------------------------------

def test_first_run_covers_yesterday_and_today(tmp_sqlite_db):
    """With no watermark, don't silently backfill history nobody asked for."""
    start, end = ingest.pending_window(today="2026-09-02")
    assert (start, end) == ("2026-09-01", "2026-09-02")


def test_the_day_just_processed_is_re_covered_next_run(tmp_sqlite_db):
    """The job runs in the morning, so "today" has barely started — EDGAR
    holds only what was accepted overnight. Claiming today as covered would
    skip every filing made during business hours, forever. So the watermark
    records the last COMPLETE day and the next run re-covers today in full."""
    ingest.mark_ingested("2026-09-02")          # a run whose window ended today
    assert database.get_app_status(ingest.WATERMARK_KEY) == "2026-09-01"

    start, end = ingest.pending_window(today="2026-09-03")
    assert start == "2026-09-02"                # yesterday is re-covered, not skipped
    assert end == "2026-09-03"


def test_business_hours_filings_are_never_skipped(tmp_sqlite_db):
    """The concrete failure: an 8-K filed at 2pm on the 2nd, after that
    morning's run. It must be picked up on the 3rd."""
    ingest.mark_ingested("2026-09-02")
    start, end = ingest.pending_window(today="2026-09-03")
    assert start <= "2026-09-02" <= end


def test_monday_run_covers_friday(tmp_sqlite_db):
    """A weekday-only cron plus a naive window loses every Friday: Monday
    would ask for Sunday..Monday."""
    ingest.mark_ingested("2026-08-28")          # Friday morning's run
    start, end = ingest.pending_window(today="2026-08-31")  # Monday
    assert start == "2026-08-28"                # Friday, in full
    assert end == "2026-08-31"


def test_re_coverage_costs_nothing_because_of_dedupe(tmp_sqlite_db, monkeypatch):
    """Re-covering a day is only affordable because Stage 1c drops filings
    already stored BEFORE any fetch or model call."""
    from filter import filter_filings

    database.insert_filing({
        "accession_no": "seen-1", "company": "Already Stored", "ticker": "AAA",
        "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
        "filing_url": "u", "raw_text": "already has text",
    })
    meta = [{"accession_no": "seen-1", "company": "Already Stored", "ticker": "AAA",
             "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
             "filing_url": "u", "items_list": ["5.02"]}]

    fetched = []
    result = filter_filings(meta, fetch_text_func=lambda *a: fetched.append(1) or ("x", None),
                            apply_universe=False)
    assert result == []
    assert fetched == []


def test_a_row_parked_without_text_is_retried_not_deduped(tmp_sqlite_db):
    """The rows a SEC block parks are exactly the ones the next run must
    re-fetch. Treating them as duplicates would strand them permanently —
    invisible to the inbox, and only fixable by a button in the web UI that a
    scheduled job never presses."""
    from filter import filter_filings

    database.insert_filing({
        "accession_no": "parked-1", "company": "Rate Limited", "ticker": "AAA",
        "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
        "filing_url": "u", "raw_text": "",
        "summary": "SEC rate-limited — pending retry",
    })
    meta = [{"accession_no": "parked-1", "company": "Rate Limited", "ticker": "AAA",
             "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
             "filing_url": "u", "items_list": ["5.02"]}]

    fetched = []

    def fetch(*args):
        fetched.append(1)
        return "", None

    filter_filings(meta, fetch_text_func=fetch, apply_universe=False)
    assert fetched, "a parked row must be re-fetched, not skipped as a duplicate"


def test_nothing_to_do_when_already_current(tmp_sqlite_db):
    """After a run whose window ended tomorrow, today is already covered."""
    ingest.mark_ingested("2026-09-03")
    assert ingest.pending_window(today="2026-09-02") == (None, None)


def test_long_outage_is_capped(tmp_sqlite_db):
    """A month down shouldn't produce one enormous catch-up run."""
    ingest.mark_ingested("2026-01-02")
    start, end = ingest.pending_window(today="2026-09-02", max_days=10)
    assert start == "2026-08-23"


def test_corrupt_watermark_falls_back_to_yesterday(tmp_sqlite_db):
    database.set_app_status(ingest.WATERMARK_KEY, "not-a-date")
    start, end = ingest.pending_window(today="2026-09-02")
    assert start == "2026-09-01"


# ---------------------------------------------------------------------------
# Blocked runs must fail loudly
# ---------------------------------------------------------------------------

def test_mostly_textless_run_raises_rather_than_reporting_success(tmp_sqlite_db, monkeypatch):
    """SEC blocking the runner looks exactly like a quiet news day unless the
    job fails. It has to fail."""
    metadata = [{"accession_no": f"a-{i}", "company": f"Co {i}", "ticker": "AAA",
                 "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
                 "filing_url": "u", "items_list": ["5.02"]} for i in range(10)]
    blocked = [dict(m, raw_text="", summary="SEC rate-limited — pending retry") for m in metadata]

    monkeypatch.setattr(ingest, "fetch_filings", lambda s, e: metadata)
    monkeypatch.setattr(ingest, "filter_filings", _fake_filter(blocked))

    with pytest.raises(ingest.IngestBlocked):
        ingest.ingest_range("2026-09-02", "2026-09-02", enrich=False)


def test_partial_results_are_still_stored_when_a_run_is_blocked(tmp_sqlite_db, monkeypatch):
    """Failing the run must not throw away the rows that did arrive."""
    metadata = [{"accession_no": f"a-{i}", "company": f"Co {i}", "ticker": "AAA",
                 "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
                 "filing_url": "u", "items_list": ["5.02"]} for i in range(10)]
    rows = [dict(m, raw_text="" if i > 1 else "real text") for i, m in enumerate(metadata)]

    monkeypatch.setattr(ingest, "fetch_filings", lambda s, e: metadata)
    monkeypatch.setattr(ingest, "filter_filings", _fake_filter(rows))

    with pytest.raises(ingest.IngestBlocked):
        ingest.ingest_range("2026-09-02", "2026-09-02", enrich=False)

    assert database.get_filing_count() == 10


def test_a_healthy_run_does_not_raise(tmp_sqlite_db, monkeypatch):
    metadata = [{"accession_no": f"a-{i}", "company": f"Co {i}", "ticker": "AAA",
                 "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
                 "filing_url": "u", "items_list": ["5.02"]} for i in range(10)]
    rows = [dict(m, raw_text="real text") for m in metadata]

    monkeypatch.setattr(ingest, "fetch_filings", lambda s, e: metadata)
    monkeypatch.setattr(ingest, "filter_filings", _fake_filter(rows))

    stats = ingest.ingest_range("2026-09-02", "2026-09-02", enrich=False)
    assert stats["new"] == 10
    assert stats["no_text"] == 0


def test_daily_does_not_advance_the_watermark_after_a_block(tmp_sqlite_db, monkeypatch):
    """Otherwise the blocked window is skipped forever."""
    import daily

    ingest.mark_ingested("2026-09-02")   # watermark becomes 2026-09-01
    before = database.get_app_status(ingest.WATERMARK_KEY)

    monkeypatch.setattr(daily, "ingest_range",
                        lambda *a, **k: (_ for _ in ()).throw(ingest.IngestBlocked("blocked")))
    monkeypatch.setattr(daily, "pending_window", lambda: ("2026-09-02", "2026-09-03"))

    with pytest.raises(ingest.IngestBlocked):
        daily.run(send_digest=False)

    assert database.get_app_status(ingest.WATERMARK_KEY) == before


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def test_digest_includes_high_scoring_filings(tmp_sqlite_db):
    _add("a-1", score=8, company="BigSignal")
    subject, html, text = digest.render(digest.build_digest(days=7))
    assert "BigSignal" in text
    assert "BigSignal" in html
    assert "BigSignal" in subject


def test_digest_excludes_weak_filings(tmp_sqlite_db):
    """The inbox is for browsing; the email is for interrupting someone."""
    _add("a-1", score=3, company="Marginal", verdict="MONITOR")
    assert digest.build_digest(days=7, min_score=5) == []


def test_empty_digest_explains_the_silence(tmp_sqlite_db):
    subject, html, text = digest.render([])
    assert "nothing flagged" in subject.lower()
    assert "not skipped" in text


def test_digest_does_not_resend_recent_filings(tmp_sqlite_db):
    """Re-running the job, or an overlapping window, must not mail the same
    filings twice — that teaches the reader to ignore the digest."""
    filing_id = _add("a-1")
    database.record_digest("email", [filing_id])
    assert digest.build_digest(days=7) == []


def test_digest_renders_label_links_when_signing_is_available(tmp_sqlite_db, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-real-secret")
    _add("a-1")
    _, html, _ = digest.render(digest.build_digest(days=7), base_url="https://example.com")
    assert "/label/" in html
    assert ">Signal</a>" in html


def test_digest_omits_label_links_without_a_real_secret(tmp_sqlite_db, monkeypatch):
    """Signing with the public default would let anyone who read the repo
    forge labels, so the digest degrades to plain filing links instead."""
    monkeypatch.setenv("SECRET_KEY", "8k-analyzer-secret-key")
    _add("a-1")
    _, html, _ = digest.render(digest.build_digest(days=7), base_url="https://example.com")
    assert "/label/" not in html
    assert "Read the filing" in html


def test_digest_dry_run_sends_nothing(tmp_sqlite_db, capsys):
    _add("a-1")
    result = digest.send(days=7, dry_run=True)
    assert result["sent"] is False
    assert "DIGEST DRY RUN" in capsys.readouterr().out


def test_digest_delivery_failure_is_reported_not_raised(tmp_sqlite_db, monkeypatch):
    """A broken mail server must never fail the ingest that ran before it."""
    _add("a-1")
    monkeypatch.setenv("DIGEST_SMTP_USER", "u")
    monkeypatch.setenv("DIGEST_SMTP_PASS", "p")
    monkeypatch.setenv("DIGEST_TO", "to@example.com")
    monkeypatch.setattr(digest, "_send_email",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down")))

    result = digest.send(days=7)
    assert result["sent"] is False
    assert "smtp down" in result["error"]


def test_company_names_are_escaped_in_the_html(tmp_sqlite_db):
    _add("a-1", company="Acme <script>alert(1)</script> Corp")
    _, html, _ = digest.render(digest.build_digest(days=7))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def test_evaluation_reports_precision_against_the_users_labels(tmp_sqlite_db):
    import evaluate

    good = _add("a-1", score=9, company="Good")
    bad = _add("a-2", score=8, company="Bad")
    database.upsert_judgment(good, "signal")
    database.upsert_judgment(bad, "noise")

    result = evaluate.evaluate()
    assert result["labeled_total"] == 2
    precision, n = result["precision_at"][10]
    assert precision == 0.5 and n == 2


def test_per_signal_precision_identifies_a_crying_wolf_detector(tmp_sqlite_db):
    """The whole point: a detector that fires often and is usually called
    noise is what trains someone to stop trusting the feed."""
    import evaluate

    for i in range(3):
        filing_id = _add(f"noisy-{i}", score=7)
        database.update_filing_fields(filing_id, signal_types="FRIDAY_NIGHT_FILING")
        database.upsert_judgment(filing_id, "noise")

    good = _add("good-1", score=9)
    database.update_filing_fields(good, signal_types="FORFEITURE_EXIT")
    database.upsert_judgment(good, "signal")

    per_signal = evaluate.evaluate()["per_signal"]
    assert per_signal["FRIDAY_NIGHT_FILING"]["precision"] == 0.0
    assert per_signal["FORFEITURE_EXIT"]["precision"] == 1.0


def test_borderline_labels_count_for_neither_side(tmp_sqlite_db):
    import evaluate

    filing_id = _add("a-1", score=8)
    database.upsert_judgment(filing_id, "meh")
    precision, n = evaluate.evaluate()["precision_at"][10]
    assert n == 0


def test_worst_misses_surface_both_directions(tmp_sqlite_db):
    import evaluate

    over = _add("a-1", score=9, company="Overrated")
    under = _add("a-2", score=2, company="Underrated", verdict="MONITOR")
    database.upsert_judgment(over, "noise")
    database.upsert_judgment(under, "signal")

    false_positives, false_negatives = evaluate.evaluate()["misses"]
    assert [r["company"] for r in false_positives] == ["Overrated"]
    assert [r["company"] for r in false_negatives] == ["Underrated"]


def test_evaluation_on_an_empty_database_does_not_crash(tmp_sqlite_db):
    import evaluate
    result = evaluate.evaluate()
    assert result["labeled_total"] == 0
    assert result["precision_at"][10] == (None, 0)


def test_a_dead_market_cap_provider_fails_the_run(tmp_sqlite_db, monkeypatch):
    """The universe gate fails closed on an unknown cap — right for one
    ticker, catastrophic for all of them. Without this the run reports
    success, the watermark advances, and an entire day is lost with only a log
    line to show for it."""
    metadata = [{"accession_no": f"a-{i}", "company": f"Co {i}", "ticker": "AAA",
                 "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
                 "filing_url": "u", "items_list": ["5.02"]} for i in range(10)]

    def dead_provider(*args, on_analyzed=None, stats=None, **kwargs):
        if stats is not None:
            stats["screened"] = 10
            stats["unknown_market_cap"] = 10
        return []

    monkeypatch.setattr(ingest, "fetch_filings", lambda s, e: metadata)
    monkeypatch.setattr(ingest, "filter_filings", dead_provider)

    with pytest.raises(ingest.IngestBlocked, match="market cap"):
        ingest.ingest_range("2026-09-02", "2026-09-02", enrich=False)


def test_a_genuinely_quiet_day_does_not_fail(tmp_sqlite_db, monkeypatch):
    """Everything screened out for real reasons (below the floor, no ticker)
    is a normal day, not an outage."""
    metadata = [{"accession_no": f"a-{i}", "company": f"Co {i}", "ticker": "AAA",
                 "cik": "1", "filed_date": "2026-09-02", "item_codes": "5.02",
                 "filing_url": "u", "items_list": ["5.02"]} for i in range(10)]

    def quiet(*args, on_analyzed=None, stats=None, **kwargs):
        if stats is not None:
            stats["screened"] = 10
            stats["unknown_market_cap"] = 1   # the rest were below the floor
        return []

    monkeypatch.setattr(ingest, "fetch_filings", lambda s, e: metadata)
    monkeypatch.setattr(ingest, "filter_filings", quiet)

    stats = ingest.ingest_range("2026-09-02", "2026-09-02", enrich=False)
    assert stats["analyzed"] == 0


def test_a_digest_only_printed_to_the_log_is_not_recorded_as_sent(tmp_sqlite_db):
    """Otherwise the first real email after configuring SMTP would open by
    skipping its own backlog — every filing already 'sent' to a log nobody
    read."""
    filing_id = _add("a-1")
    result = digest.send(days=7)

    assert result["sent"] is False
    assert result["channel"] == "stdout"
    assert filing_id not in database.get_recently_digested_ids(days=7)
