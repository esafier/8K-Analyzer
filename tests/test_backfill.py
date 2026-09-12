"""Tests for backfill.py — the ranged gap-fill entry point.

It is a thin wrapper on purpose, so the only things worth pinning are the two
behaviors that decide whether a lost window gets recovered: a blocked ingest
must not abort the run, and the Form 4 scan must cover the same range.
"""
import backfill
import ingest


def test_blocked_ingest_still_scans_form4(monkeypatch):
    """IngestBlocked means 'don't call this window covered', not 'give up'."""
    calls = {}

    def blocked(*a, **k):
        raise ingest.IngestBlocked("market-data provider is down")

    def scan(start, end):
        calls["form4"] = (start, end)
        return [{"stored": 2}]

    monkeypatch.setattr("backfill.initialize_database", lambda: None)
    monkeypatch.setattr("backfill.ingest.ingest_range", blocked)
    monkeypatch.setattr("backfill.form4.scan_range", scan)

    result = backfill.run("2026-08-20", "2026-09-03")

    assert calls["form4"] == ("2026-08-20", "2026-09-03")
    assert result["form4_stored"] == 2


def test_form4_covers_the_same_range(monkeypatch):
    calls = {}

    def ingest_range(start, end, **kwargs):
        calls["ingest"] = (start, end)
        return {"new": 7}

    def scan(start, end):
        calls["form4"] = (start, end)
        return [{"stored": 1}, {"stored": 2}]

    monkeypatch.setattr("backfill.initialize_database", lambda: None)
    monkeypatch.setattr("backfill.ingest.ingest_range", ingest_range)
    monkeypatch.setattr("backfill.form4.scan_range", scan)

    result = backfill.run("2026-08-20", "2026-09-03")

    assert calls["ingest"] == calls["form4"] == ("2026-08-20", "2026-09-03")
    assert result["filings"] == {"new": 7}
    assert result["form4_stored"] == 3


def test_no_form4_flag_skips_the_scan(monkeypatch):
    def never(*a, **k):
        raise AssertionError("scanned anyway")

    monkeypatch.setattr("backfill.initialize_database", lambda: None)
    monkeypatch.setattr("backfill.ingest.ingest_range", lambda *a, **k: {"new": 1})
    monkeypatch.setattr("backfill.form4.scan_range", never)

    assert backfill.run("2026-08-20", "2026-09-03", do_form4=False)["form4_stored"] == 0
