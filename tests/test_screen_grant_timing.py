"""Tests for screen_grant_timing.py — the no-LLM grant-timing price screen.

The screen's whole value is that its number is legible: every point corresponds
to one stated observation about the tape. These tests pin the tally to that
promise, and pin the guard rails that stop a suggestive-looking row from being
read as a finding.
"""
import pytest

import database
import screen_grant_timing as sgt


def _path(**kwargs):
    base = {"run_in": None, "pop": None, "run_out": None, "run_out_vs_spy": None,
            "is_monthly_low": None, "v_shape": False, "has_data": True,
            "windows_mature": True}
    base.update(kwargs)
    return base


def test_quiet_tape_scores_zero():
    points, reasons = sgt.evidence_score(_path(run_in=0.01, pop=0.02, run_out_vs_spy=0.01))
    assert points == 0
    assert reasons == []


def test_each_point_names_the_observation_behind_it():
    points, reasons = sgt.evidence_score(
        _path(v_shape=True, run_in=-0.25, pop=0.30, is_monthly_low=True,
              run_out_vs_spy=0.18))
    assert points == 6
    joined = " ".join(reasons)
    assert "V-shape" in joined
    assert "pop" in joined
    assert "cheapest close" in joined
    assert "vs SPY" in joined
    # One reason per scoring component, so the tally can always be audited.
    assert len(reasons) == 4


def test_v_shape_supersedes_the_plain_run_in_point():
    """A V-shape already includes the decline. Paying for both would double-count
    one observation and quietly inflate every classic-shape row by a point."""
    v = sgt.evidence_score(_path(v_shape=True, run_in=-0.25, run_out=0.25))
    plain = sgt.evidence_score(_path(v_shape=False, run_in=-0.25))
    assert v[0] == 2
    assert plain[0] == 1
    assert len(v[1]) == 1


def test_pop_is_graded_not_binary():
    small = sgt.evidence_score(_path(pop=0.12))
    large = sgt.evidence_score(_path(pop=0.25))
    none = sgt.evidence_score(_path(pop=0.05))
    assert none[0] == 0
    assert small[0] == 1
    assert large[0] == 2


def test_score_is_capped_at_six():
    points, _ = sgt.evidence_score(
        _path(v_shape=True, run_in=-0.9, pop=5.0, is_monthly_low=True,
              run_out_vs_spy=3.0))
    assert points == 6


def test_missing_legs_never_score():
    """A leg that could not be priced is an absence of evidence, not evidence of
    absence — and certainly not a point."""
    points, reasons = sgt.evidence_score(_path())
    assert points == 0
    assert reasons == []


def test_screen_marks_rows_without_price_data(monkeypatch):
    monkeypatch.setattr(sgt, "price_path", lambda t, d: _path(has_data=False))
    result = sgt.screen([{"ticker": "GONE", "filed_date": "2026-06-01",
                          "company": "Gone Inc"}], verbose=False)[0]
    assert result["points"] is None
    assert result["reasons"] == []


def test_report_excludes_immature_windows(monkeypatch, capsys):
    """A pop window that has not elapsed is pending, not a zero. Ranking it
    against settled rows would bury real candidates under fresh ones."""
    rows = [
        {"ticker": "OPEN", "filed_date": "2026-08-20", "company": "Still Open",
         "points": 5, "reasons": ["pop +30.0% within 10d"],
         "path": _path(pop=0.30, windows_mature=False)},
        {"ticker": "DONE", "filed_date": "2026-06-01", "company": "Settled",
         "points": 2, "reasons": ["pop +12.0% within 10d"],
         "path": _path(pop=0.12, windows_mature=True)},
    ]
    sgt.report(rows)
    out = capsys.readouterr().out
    assert "windows still open 1" in out
    assert "scored 1" in out
    # The immature row must not appear in the ranking table.
    assert "OPEN " not in out.split("points distribution")[0].split("ticker")[1]


def test_report_states_the_screen_cannot_confirm_a_grant(capsys):
    sgt.report([{"ticker": "X", "filed_date": "2026-06-01", "company": "X",
                 "points": 6, "reasons": ["pop +50.0% within 10d"],
                 "path": _path(pop=0.5, windows_mature=True)}])
    out = capsys.readouterr().out
    assert "does not" in out.lower() or "NOT mean a grant occurred" in out
    assert "FILING date" in out


def test_load_from_file_parses_records(tmp_path):
    f = tmp_path / "rows.txt"
    f.write_text("AAA|2026-06-01|Acme Corp;BBB|2026-06-02|Beta Inc")
    rows = sgt.load_from_file(str(f))
    assert [r["ticker"] for r in rows] == ["AAA", "BBB"]
    assert rows[0]["company"] == "Acme Corp"
    assert rows[0]["filed_date"] == "2026-06-01"


def test_load_from_file_tolerates_a_missing_company(tmp_path):
    f = tmp_path / "rows.txt"
    f.write_text("AAA|2026-06-01")
    assert sgt.load_from_file(str(f))[0]["company"] == ""


def _insert_filing(accession, ticker, filed_date, items="5.02"):
    conn = database.get_connection()
    cursor = conn.cursor()
    p = database._placeholder()
    cursor.execute(
        f"INSERT INTO filings (accession_no, company, cik, ticker, filed_date, item_codes) "
        f"VALUES ({p}, {p}, {p}, {p}, {p}, {p})",
        (accession, f"{ticker} Inc", "0000000001", ticker, filed_date, items))
    conn.commit()
    cursor.execute(f"SELECT id FROM filings WHERE accession_no = {p}", (accession,))
    filing_id = cursor.fetchone()[0]
    conn.close()
    return filing_id


def test_load_from_db_reads_watchlist_and_all(tmp_sqlite_db):
    saved = _insert_filing("a-1", "AAA", "2026-06-01")
    _insert_filing("a-2", "BBB", "2026-06-02")
    _insert_filing("a-3", "CCC", "2026-06-03", items="2.02")   # not a 5.02
    _insert_filing("a-4", "", "2026-06-04")                     # no ticker

    conn = database.get_connection()
    cursor = conn.cursor()
    p = database._placeholder()
    cursor.execute(f"INSERT INTO watchlist (filing_id) VALUES ({p})", (saved,))
    conn.commit()
    conn.close()

    watchlist = sgt.load_from_db()
    assert [r["ticker"] for r in watchlist] == ["AAA"]
    assert all(type(r) is dict for r in watchlist)

    every = sgt.load_from_db(all_filings=True)
    assert {r["ticker"] for r in every} == {"AAA", "BBB"}


def _scored_row(ticker, points, mature=True):
    return {"ticker": ticker, "filed_date": "2026-06-01", "company": ticker,
            "points": points, "reasons": [], "path": _path(windows_mature=mature)}


def test_compare_report_calls_out_an_unselective_screen(capsys):
    """The whole point of the control: if a high score is no rarer among the
    screened filings, the ranking is a volatility list wearing a suit."""
    subject = [_scored_row(f"S{i}", 4 if i < 7 else 0) for i in range(190)]
    control = [_scored_row(f"C{i}", 4 if i < 13 else 0) for i in range(181)]

    sgt.compare_report(subject, control)
    out = capsys.readouterr().out

    assert "INDISTINGUISHABLE" in out
    assert "not evidence of grant timing" in out
    assert "z on >=4 points" in out


def test_compare_report_reports_a_real_separation(capsys):
    subject = [_scored_row(f"S{i}", 5 if i < 60 else 0) for i in range(150)]
    control = [_scored_row(f"C{i}", 5 if i < 3 else 0) for i in range(150)]

    sgt.compare_report(subject, control)
    out = capsys.readouterr().out

    assert "higher than chance" in out
    assert "INDISTINGUISHABLE" not in out


def test_compare_report_ignores_immature_rows(capsys):
    """An open window is not a zero, on either side of the comparison."""
    subject = [_scored_row("A", 5), _scored_row("B", 5, mature=False)]
    control = [_scored_row("C", 0)]
    sgt.compare_report(subject, control)
    out = capsys.readouterr().out
    assert "1/1" in out          # only the settled subject row counted


def test_compare_report_survives_an_empty_side(capsys):
    sgt.compare_report([_scored_row("A", 5)], [])
    assert "Not enough scored rows" in capsys.readouterr().out


def test_control_query_shuffles_on_both_backends(tmp_sqlite_db):
    """Postgres and SQLite share no hashing syntax; a query that only parses on
    one of them fails exactly where it matters — against the real archive."""
    saved = _insert_filing("a-1", "AAA", "2026-06-01")
    _insert_filing("a-2", "BBB", "2026-06-02")
    _insert_filing("a-3", "CCC", "2026-06-03")

    conn = database.get_connection()
    cursor = conn.cursor()
    p = database._placeholder()
    cursor.execute(f"INSERT INTO watchlist (filing_id) VALUES ({p})", (saved,))
    conn.commit()
    conn.close()

    control = sgt.load_control(limit=10)
    # The saved filing must be excluded — that is what makes it a control.
    assert {r["ticker"] for r in control} == {"BBB", "CCC"}
    assert len(sgt.load_control(limit=1)) == 1


def test_wilson_widens_for_small_samples():
    wide = sgt.wilson(1, 5)
    narrow = sgt.wilson(200, 1000)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])
    assert sgt.wilson(0, 0) == (0.0, 0.0)
