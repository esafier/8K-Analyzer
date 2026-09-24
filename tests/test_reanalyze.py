"""Tests for reanalyze.py — scoring rows the pipeline never saw.

These rows are the archive's blind spot: stored by an older version, so they
have text and a summary but no signals and no verdict, and dedupe hides them
from every subsequent ingest of the same date. The job here is to find
exactly those rows, spend nothing on the ones already scored, and route the
work through the one analysis path.
"""
import json

import pytest
from types import SimpleNamespace

import database
import reanalyze


def _insert(accession, filed_date="2026-09-03", structured=None, raw_text="t",
            pipeline_version=None, ticker="AAA"):
    database.insert_filing({
        "accession_no": accession, "company": f"Co {accession}", "ticker": ticker,
        "cik": "1", "filed_date": filed_date, "item_codes": "5.02",
        "filing_url": "u", "raw_text": raw_text,
        "structured_summary": json.dumps(structured) if structured else None,
        "summary": "old-style prose summary",
        "pipeline_version": pipeline_version,
    })
    return database.get_filing_by_accession(accession)["id"]


@pytest.fixture(autouse=True)
def _everyone_in_universe(monkeypatch):
    """Offline, no market cap resolves. Treat every test company as in
    universe; the gate itself has its own test."""
    monkeypatch.setattr("database.get_cached_market_caps",
                        lambda tickers, max_age_hours=None: {t: 1e9 for t in tickers})


def _result(verdict="DEEP_LOOK", score=8, relevant=True, error=None):
    return SimpleNamespace(
        error=error, relevant=relevant, tokens_in=100, tokens_out=20,
        signal_types=["FORFEITURE_EXIT"],
        fields={
            "triage_verdict": verdict, "signal_score": score,
            "signal_direction": "BEARISH", "top_signal": "CFO walks from unvested comp.",
            "structured_summary": json.dumps({"departures": []}),
            "signal_types": "FORFEITURE_EXIT",
        },
    )


def test_selects_only_unscored_rows_with_text(tmp_sqlite_db):
    unscored = _insert("a-1")
    _insert("a-2", structured={"departures": []}, pipeline_version="v4.2-signals")  # scored
    _insert("a-3", raw_text="")                          # no text to re-analyze

    rows = reanalyze.rows_missing_analysis(since="2026-08-20")

    assert [row["id"] for row in rows] == [unscored]


def test_rows_with_pre_rebuild_facts_are_still_unscored(tmp_sqlite_db):
    """March-July rows carry facts from the old pipeline in a schema today's
    detectors can't read, and no verdict."""
    old = _insert("old", structured={"category": "Management Change"})
    assert [row["id"] for row in reanalyze.rows_missing_analysis()] == [old]


def test_the_universe_gate_applies_to_history(tmp_sqlite_db, monkeypatch):
    big = _insert("big", ticker="BIG")
    _insert("tiny", ticker="TINY")
    monkeypatch.setattr("database.get_cached_market_caps",
                        lambda tickers, max_age_hours=None: {"BIG": 2e9, "TINY": 10e6})
    seen = []
    monkeypatch.setattr("reanalyze.analyze_filing",
                        lambda row, **k: seen.append(row["id"]) or _result(relevant=False))
    reanalyze.run()
    assert seen == [big]


def test_date_window_is_honored(tmp_sqlite_db):
    _insert("before", filed_date="2026-08-19")
    inside = _insert("inside", filed_date="2026-08-25")
    _insert("after", filed_date="2026-09-10")

    rows = reanalyze.rows_missing_analysis(since="2026-08-20", until="2026-09-03")

    assert [row["id"] for row in rows] == [inside]


def test_dry_run_spends_nothing_and_writes_nothing(tmp_sqlite_db, monkeypatch):
    filing_id = _insert("a-1")

    def boom(*a, **k):
        raise AssertionError("dry run called the pipeline")

    monkeypatch.setattr("reanalyze.analyze_filing", boom)
    stats = reanalyze.run(since="2026-08-20", dry_run=True)

    assert stats["candidates"] == 1
    assert database.get_filing_by_id(filing_id)["triage_verdict"] is None


def test_scored_row_gets_its_verdict_written(tmp_sqlite_db, monkeypatch):
    filing_id = _insert("a-1")
    monkeypatch.setattr("reanalyze.analyze_filing", lambda *a, **k: _result())

    stats = reanalyze.run(since="2026-08-20")

    row = database.get_filing_by_id(filing_id)
    assert stats["scored"] == 1
    assert row["triage_verdict"] == "DEEP_LOOK"
    assert row["signal_score"] == 8
    assert row["top_signal"] == "CFO walks from unvested comp."


def test_irrelevant_filing_is_kept_passed_and_never_paid_for_twice(tmp_sqlite_db, monkeypatch):
    """The archive keeps it and the inbox doesn't rank it (PASS) — and it is
    stamped as seen, so a rerun or the next history chunk doesn't re-extract it."""
    filing_id = _insert("a-1")
    monkeypatch.setattr("reanalyze.analyze_filing",
                        lambda *a, **k: _result(relevant=False))

    stats = reanalyze.run(since="2026-08-20")

    assert stats["irrelevant"] == 1 and stats["scored"] == 0
    row = database.get_filing_by_id(filing_id)
    assert row["triage_verdict"] == "PASS"
    assert row["raw_text"] == "t"                    # still in the archive
    assert reanalyze.rows_missing_analysis(since="2026-08-20") == []


def test_workers_score_every_row_once(tmp_sqlite_db, monkeypatch):
    ids = [_insert(f"w-{i}", filed_date=f"2026-09-{10 + i:02d}") for i in range(8)]
    seen = []
    monkeypatch.setattr("reanalyze.analyze_filing",
                        lambda row, **k: seen.append(row["id"]) or _result())
    stats = reanalyze.run(since="2026-09-01", workers=4)
    assert sorted(seen) == sorted(ids)
    assert stats["scored"] == 8


def test_a_failure_costs_one_filing_not_the_run(tmp_sqlite_db, monkeypatch):
    first = _insert("a-1", filed_date="2026-09-03")
    second = _insert("a-2", filed_date="2026-09-02")

    def analyze(filing, *a, **k):
        if filing["id"] == first:
            return _result(error="timeout")
        return _result()

    monkeypatch.setattr("reanalyze.analyze_filing", analyze)
    stats = reanalyze.run(since="2026-08-20")

    assert stats["failed"] == 1 and stats["scored"] == 1
    assert database.get_filing_by_id(first)["triage_verdict"] is None
    assert database.get_filing_by_id(second)["triage_verdict"] == "DEEP_LOOK"


def test_tokens_are_reported(tmp_sqlite_db, monkeypatch):
    """The user decides on further spend from this number."""
    _insert("a-1")
    monkeypatch.setattr("reanalyze.analyze_filing", lambda *a, **k: _result())

    stats = reanalyze.run(since="2026-08-20")

    assert stats["tokens_in"] == 100 and stats["tokens_out"] == 20


def test_one_bad_row_costs_one_row(tmp_sqlite_db, monkeypatch):
    """A persist error is reported and counted; the rest of the run goes on,
    in parallel mode too."""
    ids = [_insert(f"e-{i}", filed_date=f"2026-09-{10 + i:02d}") for i in range(4)]

    def analyze(row, **k):
        if row["id"] == ids[1]:
            raise ValueError("invalid input syntax for type bigint")
        return _result()

    monkeypatch.setattr("reanalyze.analyze_filing", analyze)
    stats = reanalyze.run(since="2026-09-01", workers=3)
    assert stats["scored"] == 3 and stats["failed"] == 1
    assert list(stats["errors"].values()) == [1]
