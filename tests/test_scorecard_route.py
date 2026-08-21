"""Tests for the /scorecard page.

A broken Jinja macro is a 500 in production and invisible to unit tests of the
scoring module, so these render the real template. They also pin the page's
honesty guarantees: no rate on a thin sample, and the excluded rows stated
rather than hidden.
"""
import database
import outcome_scoring


def _client(tmp_sqlite_db):
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def _scored_filing(i, direction="BEARISH", verdict="DEEP_LOOK", close=9.0,
                   ticker="AAPL", status=database.OUTCOME_OK, mark=True):
    accession = f"0001234-26-{i:06d}"
    database.insert_filing({
        "accession_no": accession, "company": f"Company {i}", "ticker": ticker,
        "cik": "0001234567", "filed_date": "2026-01-05", "item_codes": "5.02",
        "triage_verdict": verdict, "signal_direction": direction, "signal_score": 8,
        "filing_url": "https://example.com",
    })
    filing_id = database.get_filing_by_accession(accession)["id"]
    filing = {
        "id": filing_id, "accession_no": accession, "ticker": ticker,
        "filed_date": "2026-01-05", "triage_verdict": verdict,
        "signal_direction": direction, "signal_score": 8,
        "forfeited_comp": 1, "has_successor": 0,
        "departure_count_24mo": 2, "has_market_targets": 0,
    }
    if status == database.OUTCOME_OK:
        database.upsert_signal_outcome(filing, "2026-01-05", 10.0, 500.0)
        if mark:
            database.set_outcome_mark(filing_id, 30, close, 505.0)
    else:
        database.upsert_signal_outcome(filing, status=status)
    return filing_id


# ---------- rendering ----------

def test_empty_scorecard_renders_with_guidance(tmp_sqlite_db):
    resp = _client(tmp_sqlite_db).get("/scorecard")
    assert resp.status_code == 200
    assert b"No outcomes scored yet" in resp.data
    assert b"Backfill" in resp.data


def test_populated_scorecard_renders(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    for i in range(outcome_scoring.MIN_SAMPLE):
        _scored_filing(i)
    resp = client.get("/scorecard")
    assert resp.status_code == 200
    assert b"Signal Scorecard" in resp.data
    assert b"Hit rate" in resp.data
    # 10 bearish calls, all lagging SPY → 100%
    assert b"100%" in resp.data


def test_all_horizons_render(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    _scored_filing(1)
    for horizon in database.OUTCOME_HORIZONS:
        assert client.get(f"/scorecard?horizon={horizon}").status_code == 200


def test_garbage_horizon_falls_back_instead_of_500(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    for bad in ("abc", "", "14", "-1", "7; DROP TABLE filings"):
        resp = client.get(f"/scorecard?horizon={bad}")
        assert resp.status_code == 200, f"horizon={bad!r} broke the page"
    # The table must still be there afterwards.
    assert database.get_filing_count() is not None


# ---------- honesty guarantees, rendered ----------

def test_thin_sample_shows_no_rate_on_the_page(tmp_sqlite_db):
    """Below MIN_SAMPLE the page must not print a percentage at all."""
    client = _client(tmp_sqlite_db)
    for i in range(3):
        _scored_filing(i)
    body = client.get("/scorecard").data
    assert b"Scored calls" in body
    assert b"100%" not in body, "a hit rate was shown on a 3-call sample"


def test_excluded_rows_are_stated_on_the_page(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    _scored_filing(1)
    _scored_filing(2, ticker="DEADCO", status=database.OUTCOME_DELISTED)
    _scored_filing(3, ticker="NOPRICE", status=database.OUTCOME_NO_PRICE)
    body = client.get("/scorecard").data
    assert b"What these numbers leave out" in body
    assert b"Stopped trading" in body
    assert b"No price data" in body


def test_delisted_names_are_not_counted_as_bearish_wins(tmp_sqlite_db):
    """The page says it excludes them; this makes sure the numbers agree."""
    client = _client(tmp_sqlite_db)
    for i in range(outcome_scoring.MIN_SAMPLE):
        _scored_filing(i)
    for i in range(50):
        _scored_filing(100 + i, ticker="DEADCO", status=database.OUTCOME_DELISTED)

    card = outcome_scoring.build_scorecard(30)
    assert card["overall"]["n"] == outcome_scoring.MIN_SAMPLE
    assert card["coverage"]["delisted"] == 50
    assert client.get("/scorecard").status_code == 200


def test_best_and_worst_link_to_the_filings(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    good = _scored_filing(1, close=5.0)     # bearish, stock halved
    bad = _scored_filing(2, close=20.0)     # bearish, stock doubled
    body = client.get("/scorecard").data.decode()
    assert f'/filing/{good}' in body
    assert f'/filing/{bad}' in body


def test_nav_exposes_the_scorecard(tmp_sqlite_db):
    body = _client(tmp_sqlite_db).get("/").data
    assert b'href="/scorecard"' in body


def test_the_page_states_the_bias_direction_correctly(tmp_sqlite_db):
    """A delisted name is scored at the horizons it traded through; only its
    later horizons drop out, and those skew bearish-successful. So the omission
    understates bearish performance. The page said the opposite for a while —
    a leftover from before horizons were settled individually."""
    client = _client(tmp_sqlite_db)
    _scored_filing(1)
    body = client.get("/scorecard").data.decode()

    assert "flatters bearish" not in body, "page states the bias backwards"
    assert "against</em> the bearish signal" in body
    # And it must not claim delisted names are excluded outright.
    assert "excluded from the rates" not in body


def test_the_page_discloses_the_look_ahead_limitation(tmp_sqlite_db):
    """The baseline is the filing-date close, which for an after-hours 8-K
    predates the news being public. The measured move then includes an overnight
    reaction nobody could have traded. A page whose whole purpose is honesty
    about what its numbers mean has to say so."""
    client = _client(tmp_sqlite_db)
    _scored_filing(1)
    body = client.get("/scorecard").data.decode()
    assert "not tradeable returns" in body
    assert "after the 4pm close" in body
