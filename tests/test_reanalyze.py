"""Tests for reanalyze.py — scoring rows the pipeline never saw.

These rows are the archive's blind spot: stored by an older version, so they
have text and a summary but no signals and no verdict, and dedupe hides them
from every subsequent ingest of the same date. The job here is to find
exactly those rows, spend nothing on the ones already scored, and route the
work through the one analysis path.
"""
import json
from types import SimpleNamespace

import database
import reanalyze


def _insert(accession, filed_date="2026-09-03", structured=None, raw_text="t"):
    database.insert_filing({
        "accession_no": accession, "company": f"Co {accession}", "ticker": "AAA",
        "cik": "1", "filed_date": filed_date, "item_codes": "5.02",
        "filing_url": "u", "raw_text": raw_text,
        "structured_summary": json.dumps(structured) if structured else None,
        "summary": "old-style prose summary",
    })
    return database.get_filing_by_accession(accession)["id"]


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
    _insert("a-2", structured={"departures": []})        # already scored
    _insert("a-3", raw_text="")                          # no text to re-analyze

    rows = reanalyze.rows_missing_analysis(since="2026-08-20")

    assert [row["id"] for row in rows] == [unscored]


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


def test_irrelevant_filing_is_left_alone(tmp_sqlite_db, monkeypatch):
    """The archive keeps it; the inbox still shouldn't rank it."""
    filing_id = _insert("a-1")
    monkeypatch.setattr("reanalyze.analyze_filing",
                        lambda *a, **k: _result(relevant=False))

    stats = reanalyze.run(since="2026-08-20")

    assert stats["irrelevant"] == 1 and stats["scored"] == 0
    assert database.get_filing_by_id(filing_id)["triage_verdict"] is None


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
