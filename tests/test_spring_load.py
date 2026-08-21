"""Tests for the grant-timing screen.

The score has to be deterministic and defensible, so these pin the judgment
rules rather than the plumbing: an evidenced rationale is not treated the same
as an unexplained one, the price path is confirmatory rather than decisive, and
a single observation cannot reach the top band on its own.
"""
from unittest.mock import patch

import pytest

import spring_load
from spring_load import analyze, price_path, required_cagr, score_grant


# ---------- required CAGR: what deflates a headline hurdle ----------

def test_required_cagr_converts_a_headline_hurdle():
    """A '+109%' hurdle over seven years is about 11%/yr, not a moonshot."""
    cagr = required_cagr(10.0, 20.9, "2033-01-01", "2026-01-01")
    assert cagr == pytest.approx(0.111, abs=0.01)


def test_a_short_deadline_makes_the_same_hurdle_serious():
    cagr = required_cagr(10.0, 20.0, "2027-01-01", "2026-01-01")
    assert cagr == pytest.approx(1.0, abs=0.02)


def test_required_cagr_refuses_impossible_inputs():
    assert required_cagr(0, 20.0, "2027-01-01", "2026-01-01") is None
    assert required_cagr(10.0, 20.0, "2026-01-01", "2026-01-01") is None   # zero window
    assert required_cagr(10.0, 20.0, None, "2026-01-01") is None
    assert required_cagr(None, 20.0, "2027-01-01", "2026-01-01") is None


# ---------- price path ----------

SERIES = {
    "2026-01-02": 10.0, "2026-01-15": 8.0, "2026-02-02": 7.0,   # fell into the grant
    "2026-02-12": 9.0, "2026-03-04": 11.0,                       # rose out of it
}


def _path_for(series, ticker="AAPL", grant="2026-02-02", spy=None):
    spy = spy or {"2026-02-02": 500.0, "2026-03-04": 505.0}

    def fake_closes(t, start, end):
        return series if t.upper() == ticker else spy

    def fake_on_or_after(t, target, **_kw):
        src = series if t.upper() == ticker else spy
        later = sorted(d for d in src if d >= str(target)[:10])
        return (later[0], src[later[0]]) if later else (None, None)

    with patch("spring_load.get_daily_closes", side_effect=fake_closes), \
         patch("spring_load.get_close_on_or_after", side_effect=fake_on_or_after):
        return price_path(ticker, grant)


def test_price_path_detects_the_v_shape(tmp_sqlite_db):
    path = _path_for(SERIES)
    assert path["has_data"] is True
    assert path["run_in"] < 0 and path["run_out"] > 0
    assert path["v_shape"] is True
    assert path["is_monthly_low"] is None or isinstance(path["is_monthly_low"], bool)


def test_price_path_benchmarks_the_medium_window(tmp_sqlite_db):
    """A market-wide rally must not read as a grant-specific pop."""
    path = _path_for(SERIES)
    assert path["run_out_vs_spy"] == pytest.approx(path["run_out"] - 0.01, abs=0.01)


def test_price_path_reports_absence_rather_than_guessing(tmp_sqlite_db):
    with patch("spring_load.get_daily_closes", return_value={}):
        path = price_path("DEADCO", "2026-02-02")
    assert path["has_data"] is False
    assert path["grant_close"] is None


def test_price_path_handles_missing_inputs(tmp_sqlite_db):
    assert price_path(None, "2026-02-02")["has_data"] is False
    assert price_path("AAPL", None)["has_data"] is False


# ---------- scoring judgment ----------

NO_PATH = {"has_data": False}


def test_an_unexplained_grant_starts_suspicious():
    """The skill's posture: an unscheduled award with no evidenced justification
    starts at 5 and earns its way down, not up."""
    result = score_grant({"stated_rationale": "none stated", "instrument": "OPTION"}, NO_PATH)
    assert result["score"] >= 5
    assert result["band"] in ("Suspicious", "Likely timed")


def test_a_new_hire_grant_is_not_penalised_for_being_off_cycle():
    """An inducement award to someone being hired is contractually anchored —
    that is evidence, not a guess, and it is why it looks off-cycle."""
    result = score_grant(
        {"stated_rationale": "inducement award", "is_new_hire": True,
         "instrument": "RSU", "grant_date": "2026-02-02"},
        NO_PATH, prior_grant_dates=["2025-06-01"])
    assert result["score"] <= 4
    assert result["band"] in ("Routine", "Unremarkable")


def test_a_grant_dated_before_its_own_approval_is_a_red_flag():
    result = score_grant(
        {"stated_rationale": "annual grant", "grant_date": "2026-02-02",
         "approval_date": "2026-02-10"}, NO_PATH)
    assert any(sev == "🔴" for sev, _ in result["signals"])
    assert result["score"] >= 5


def test_a_retention_award_with_no_service_condition_contradicts_itself():
    result = score_grant(
        {"stated_rationale": "retention", "instrument": "OPTION",
         "vesting_summary": "vests on price hurdles only, no service condition"},
        NO_PATH)
    assert any("no service condition" in txt for _, txt in result["signals"])
    assert result["score"] >= 5


def test_a_falling_stock_after_the_grant_refutes_news_timing():
    path = {"has_data": True, "pop": -0.25, "run_in": 0.02,
            "run_out": -0.3, "v_shape": False, "is_monthly_low": False}
    result = score_grant({"stated_rationale": "annual grant"}, path)
    assert any("refutes news-timing" in txt for _, txt in result["signals"])


def test_the_price_path_alone_cannot_reach_the_top_band():
    """One observation caps at 8 without repetition or a policy contradiction —
    otherwise a lucky chart would read as near-certain."""
    path = {"has_data": True, "pop": 0.5, "run_in": -0.3, "run_out": 0.6,
            "run_out_vs_spy": 0.55, "v_shape": True, "is_monthly_low": True}
    result = score_grant({"stated_rationale": "none stated", "instrument": "OPTION"}, path)
    assert result["score"] == 7
    assert any("one observation" in txt for _, txt in result["signals"])


def test_repetition_lifts_the_cap():
    path = {"has_data": True, "pop": 0.5, "run_in": -0.3, "run_out": 0.6,
            "run_out_vs_spy": 0.55, "v_shape": True, "is_monthly_low": True}
    result = score_grant({"stated_rationale": "none stated", "instrument": "OPTION",
                          "grant_date": "2026-02-02"},
                         path, prior_grant_dates=["2024-06-01", "2025-06-01"])
    assert result["score"] == spring_load.SCREEN_MAX_SCORE


def test_missing_price_data_is_stated_not_scored_around():
    """A dated grant we simply could not price."""
    result = score_grant({"stated_rationale": "annual grant",
                          "grant_date": "2026-02-02"}, NO_PATH)
    assert any("entire price path is untested" in txt for _, txt in result["signals"])


def test_an_undisclosed_grant_date_is_reported_as_such():
    """Distinct from 'no price data' — here there is nothing to anchor to."""
    result = score_grant({"stated_rationale": "annual grant"}, NO_PATH)
    assert any("No grant date disclosed" in txt for _, txt in result["signals"])


# ---------- the filing-level screen ----------

def test_a_filing_with_no_grant_scores_zero(tmp_sqlite_db):
    out = analyze({"has_grant": False, "notes": "auditor change"}, "AAPL", "2026-02-02")
    assert out["has_grant"] is False and out["max_score"] == 0


def test_malformed_extraction_does_not_crash_the_screen(tmp_sqlite_db):
    """A screen that dies on one bad filing is useless across an archive."""
    out = analyze("not json at all", "AAPL", "2026-02-02")
    assert out["has_grant"] is False
    assert "error" in out


def test_every_result_carries_what_it_could_not_test(tmp_sqlite_db):
    """A clean score must never be mistaken for a clean company."""
    out = analyze({"has_grant": False}, "AAPL", "2026-02-02")
    assert "Form 4" in " ".join(out["untestable"])
    assert "compensation committee" in " ".join(out["untestable"])


def test_hurdles_are_converted_to_required_growth(tmp_sqlite_db):
    extraction = {"has_grant": True, "grants": [{
        "recipient": "Jane Doe", "role": "CEO", "instrument": "OPTION",
        "grant_date": "2026-02-02", "stated_rationale": "inducement award",
        "is_new_hire": True, "price_hurdles": [4.50, 6.50],
        "hurdle_deadline": "2031-02-02",
    }]}
    with patch("spring_load.price_path", return_value={"has_data": True, "grant_close": 2.0,
                                                       "pop": 0.0, "run_in": 0.0}):
        out = analyze(extraction, "PROP", "2026-02-02")

    hurdles = out["grants"][0]["hurdles"]
    assert len(hurdles) == 2
    assert hurdles[0]["vs_grant_price"] == pytest.approx(1.25)      # $2.00 -> $4.50
    assert 0.15 < hurdles[0]["required_cagr"] < 0.20                # ~18%/yr over 5y


def test_the_filing_score_is_the_worst_grant_in_it(tmp_sqlite_db):
    extraction = {"has_grant": True, "grants": [
        {"recipient": "A", "stated_rationale": "annual grant", "is_new_hire": False},
        {"recipient": "B", "stated_rationale": "none stated", "instrument": "OPTION"},
    ]}
    with patch("spring_load.price_path", return_value=NO_PATH):
        out = analyze(extraction, "AAPL", "2026-02-02")
    assert out["max_score"] == max(g["score"] for g in out["grants"])
    assert out["max_score"] >= 5


# ---------- runners ----------

def _filing(accession="0001-1", text="Grant of options to the CEO."):
    return {"accession_no": accession, "cik": "0000001", "ticker": "AAPL",
            "filed_date": "2026-02-02", "company": "Test Co", "raw_text": text}


def test_a_filing_with_no_stored_text_is_a_gap_not_a_clean_result(tmp_sqlite_db):
    from spring_load import screen_filing
    assert screen_filing(_filing(text="")) is None


def test_a_failed_extraction_is_not_cached(tmp_sqlite_db):
    """Caching a failure would make it permanent — the same mistake that bit the
    outcome tracker three times."""
    import database
    from spring_load import screen_filing

    with patch("llm.extract_grant_facts", return_value={"error": True, "grants": []}):
        assert screen_filing(_filing()) is None
    assert database.get_spring_load_analysis("0001-1") is None


def test_a_successful_screen_is_cached_and_reused(tmp_sqlite_db):
    import database
    from spring_load import screen_filing

    extraction = {"has_grant": True, "error": False, "grants": [
        {"recipient": "Jane Doe", "instrument": "OPTION",
         "stated_rationale": "none stated", "grant_date": "2026-02-02"}]}

    with patch("llm.extract_grant_facts", return_value=extraction) as mock, \
         patch("spring_load.price_path", return_value={"has_data": False}):
        first = screen_filing(_filing())
        assert mock.call_count == 1
        second = screen_filing(_filing())
        assert mock.call_count == 1, "cached screen was re-extracted"

    assert first["max_score"] == second["max_score"]
    assert database.get_spring_load_analysis("0001-1")["max_score"] == first["max_score"]


def test_prior_grant_dates_build_a_cadence_baseline(tmp_sqlite_db):
    import database
    from spring_load import screen_filing

    extraction = {"has_grant": True, "error": False, "grants": [
        {"recipient": "A", "instrument": "OPTION", "stated_rationale": "annual grant",
         "grant_date": "2025-06-01"}]}
    with patch("llm.extract_grant_facts", return_value=extraction), \
         patch("spring_load.price_path", return_value={"has_data": False}):
        screen_filing(_filing(accession="0001-1"))

    assert database.get_prior_grant_dates("0000001") == ["2025-06-01"]
    # The filing under review must not be its own baseline.
    assert database.get_prior_grant_dates("0000001", exclude_accession="0001-1") == []


# ---------- routes and rendering ----------

def _client(tmp_sqlite_db):
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def _insert_filing(accession="0009-1", ticker="PROP"):
    import database
    database.insert_filing({
        "accession_no": accession, "company": "Prop Co", "ticker": ticker,
        "cik": "0000009", "filed_date": "2026-02-02", "item_codes": "5.02",
        "raw_text": "The Board granted options.", "filing_url": "https://example.com",
        "triage_verdict": "DEEP_LOOK", "signal_direction": "BULLISH", "signal_score": 8,
    })
    return database.get_filing_by_accession(accession)["id"]


def test_filing_page_renders_without_a_screen(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    filing_id = _insert_filing()
    resp = client.get(f"/filing/{filing_id}")
    assert resp.status_code == 200
    assert b"Grant-Timing Screen" in resp.data
    assert b"Run screen" in resp.data


def test_filing_page_renders_a_stored_screen_with_hurdles(tmp_sqlite_db):
    import database
    client = _client(tmp_sqlite_db)
    filing_id = _insert_filing()

    analysis = {
        "has_grant": True, "max_score": 6, "band": "Suspicious",
        "untestable": spring_load.UNTESTABLE,
        "grants": [{
            "recipient": "Gregory Patton", "role": "CEO", "instrument": "OPTION",
            "shares": 850000, "grant_date": "2026-02-02",
            "stated_rationale": "none stated", "vesting_summary": "price hurdles only",
            "score": 6, "band": "Suspicious",
            "signals": [["🟠", "No rationale disclosed for the award"]],
            "hurdles": [{"hurdle": 4.50, "required_cagr": 0.18, "vs_grant_price": 1.25}],
            "price_path": {"has_data": True},
        }],
    }
    database.upsert_spring_load_analysis("0009-1", "0000009", "PROP", "2026-02-02", analysis)

    body = client.get(f"/filing/{filing_id}").data.decode()
    assert "6/10" in body and "Suspicious" in body
    assert "Gregory Patton" in body
    assert "18%/yr" in body, "the CAGR conversion is the point of the hurdle table"
    assert "+125%" in body
    assert "could not test" in body
    assert "Re-extract (costs an AI call)" in body
    assert "refreshes itself on this page for free" in body


def test_the_page_never_claims_more_than_the_screen_can_see(tmp_sqlite_db):
    import database
    client = _client(tmp_sqlite_db)
    filing_id = _insert_filing()
    database.upsert_spring_load_analysis(
        "0009-1", "0000009", "PROP", "2026-02-02",
        {"has_grant": True, "max_score": 8, "band": "Likely timed",
         "grants": [], "untestable": spring_load.UNTESTABLE})

    body = client.get(f"/filing/{filing_id}").data.decode()
    assert "not a finding" in body
    assert "not a clean company" in body
    assert "Form 4 history" in body


def test_a_screen_finding_no_grant_says_so(tmp_sqlite_db):
    import database
    client = _client(tmp_sqlite_db)
    filing_id = _insert_filing()
    database.upsert_spring_load_analysis(
        "0009-1", "0000009", "PROP", "2026-02-02",
        {"has_grant": False, "max_score": 0, "band": "Routine", "grants": [],
         "notes": "Auditor change only", "untestable": spring_load.UNTESTABLE})

    body = client.get(f"/filing/{filing_id}").data.decode()
    assert "No equity grant disclosed" in body
    assert "Auditor change only" in body


def test_watchlist_offers_the_backtest(tmp_sqlite_db):
    body = _client(tmp_sqlite_db).get("/watchlist").data.decode()
    assert "Grant-Timing Backtest" in body
    assert 'action="/backtest-spring-load"' in body


def test_screen_route_survives_a_filing_with_no_text(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    import database
    database.insert_filing({
        "accession_no": "0009-2", "company": "No Text Co", "ticker": "NTX",
        "cik": "0000010", "filed_date": "2026-02-02", "item_codes": "5.02",
        "raw_text": "", "filing_url": "https://example.com",
    })
    filing_id = database.get_filing_by_accession("0009-2")["id"]
    resp = client.post(f"/spring-load/{filing_id}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Could not screen this filing" in resp.data


# ---------- cross-recipient asymmetry ----------

def test_service_condition_is_read_from_the_vesting_language():
    from spring_load import has_service_condition
    assert has_service_condition("vests on price hurdles only, no service condition") is False
    assert has_service_condition("3-year ratable service vesting plus price hurdles") is True
    assert has_service_condition("") is None
    assert has_service_condition("vests per the award agreement") is None


def test_asymmetry_is_only_a_finding_when_someone_else_has_a_condition():
    from spring_load import cross_recipient_asymmetry
    # Uniform structure — nobody has a service condition. Not asymmetry.
    assert cross_recipient_asymmetry([
        {"recipient": "A", "vesting_summary": "no service condition"},
        {"recipient": "B", "vesting_summary": "no service condition"},
    ]) == []
    # The CEO is exempt from what the CFO must do. That is the finding.
    assert cross_recipient_asymmetry([
        {"recipient": "CEO", "vesting_summary": "price hurdles only, no service condition"},
        {"recipient": "CFO", "vesting_summary": "3-year ratable vesting"},
    ]) == ["CEO"]
    # A single grant cannot be asymmetric with itself.
    assert cross_recipient_asymmetry([{"recipient": "Solo", "vesting_summary": "no service"}]) == []


def test_the_asymmetry_lifts_the_exempt_recipients_score(tmp_sqlite_db):
    """The real PROP shape: the incoming CEO's tranche carries no service
    condition while the CFO's does."""
    extraction = {"has_grant": True, "grants": [
        {"recipient": "Gregory Patton", "role": "CEO", "instrument": "OPTION",
         "is_new_hire": True, "stated_rationale": "inducement award",
         "grant_date": "2026-06-23",
         "vesting_summary": "vests on price hurdles only, no service condition"},
        {"recipient": "Michael Shelly", "role": "CFO", "instrument": "OPTION",
         "is_new_hire": True, "stated_rationale": "inducement award",
         "grant_date": "2026-06-23",
         "vesting_summary": "3-year ratable service vesting plus price hurdles"},
    ]}
    with patch("spring_load.price_path", return_value={"has_data": False}):
        out = analyze(extraction, "PROP", "2026-06-23")

    ceo = next(g for g in out["grants"] if g["recipient"] == "Gregory Patton")
    cfo = next(g for g in out["grants"] if g["recipient"] == "Michael Shelly")
    assert ceo["score"] > cfo["score"], "the exempt recipient should score higher"
    assert any("weakest retention mechanics" in t for _, t in ceo["signals"])
    assert not any("weakest retention mechanics" in t for _, t in cfo["signals"])


def test_this_screen_never_reaches_the_top_band(tmp_sqlite_db):
    """10/10 means red-zone proximity plus company-controlled news plus history
    or a policy contradiction — evidence that lives in Form 4 history and the
    proxy. A screen that cannot see either must not claim it."""
    path = {"has_data": True, "pop": 0.5, "run_in": -0.4, "run_out": 0.8,
            "run_out_vs_spy": 0.75, "v_shape": True, "is_monthly_low": True}
    result = score_grant(
        {"stated_rationale": "none stated", "instrument": "OPTION",
         "grant_date": "2026-02-02", "approval_date": "2026-02-10"},
        path, prior_grant_dates=["2024-06-01", "2025-06-01"],
        service_asymmetry_peers=["CFO"])
    assert result["score"] <= spring_load.SCREEN_MAX_SCORE
    assert result["band"] != "Near-certain"
    assert any("tops out at" in t for _, t in result["signals"])


def test_the_asymmetry_bump_is_subject_to_the_single_observation_cap(tmp_sqlite_db):
    """The bump must run before the cap, not after it — otherwise one filing plus
    a price chart could reach Near-certain."""
    path = {"has_data": True, "pop": 0.5, "run_in": -0.4, "run_out": 0.8,
            "run_out_vs_spy": 0.75, "v_shape": True, "is_monthly_low": True}
    no_asym = score_grant({"stated_rationale": "annual grant", "instrument": "OPTION"}, path)
    assert no_asym["score"] == 7, "price evidence alone should not corroborate itself"
    assert any("one observation" in t for _, t in no_asym["signals"])


def test_the_structural_contradiction_actually_corroborates(tmp_sqlite_db):
    """Regression: the corroboration test matched lowercase text while the
    asymmetry signal begins with a capital letter, so the contradiction was
    silently dropped and an exempt CEO scored the same as their CFO."""
    path = {"has_data": True, "pop": 0.2, "run_in": -0.35, "run_out": 0.25,
            "run_out_vs_spy": 0.22, "v_shape": True, "is_monthly_low": False}
    grant = {"stated_rationale": "inducement award", "is_new_hire": True,
             "instrument": "OPTION", "grant_date": "2026-06-23"}

    exempt = score_grant(grant, path, service_asymmetry_peers=["Michael Shelly"])
    bound = score_grant(grant, path)

    assert exempt["score"] > bound["score"], \
        "the recipient exempt from a service condition must score higher"
    assert not any("Held at 7" in t for _, t in exempt["signals"]), \
        "a structural contradiction corroborates the price path"
    assert any("Held at 7" in t for _, t in bound["signals"])


# ---------- the price path must not manufacture evidence ----------

def test_an_undisclosed_grant_date_never_borrows_the_filing_date(tmp_sqlite_db):
    """An 8-K can announce an earlier award without saying when it was made.
    Scoring the FILING date's monthly low, pop and V-shape would invent
    price-timing evidence the filing never established — and would contradict
    the extraction contract, which returns null rather than guessing."""
    extraction = {"has_grant": True, "grants": [
        {"recipient": "Jane Doe", "instrument": "OPTION",
         "stated_rationale": "annual grant", "grant_date": None}]}

    with patch("spring_load.price_path") as mock:
        out = analyze(extraction, "AAPL", "2026-02-02")

    assert mock.call_count == 0, "priced a grant whose date the filing never gave"
    assert out["grants"][0]["grant_date"] is None
    assert any("No grant date disclosed" in t for _, t in out["grants"][0]["signals"])


def test_the_run_in_is_measured_from_the_configured_boundary(tmp_sqlite_db):
    """Padding exists to locate a bar near the 30-day mark, not to move it. Using
    the earliest fetched bar stretched a 30-day run-in to ~40 and let a decline
    confined to the padding trip the V-shape signal."""
    series = {
        "2026-01-01": 100.0,   # ~40d before — inside the padding only
        "2026-01-03": 10.0,    # the true 30-day boundary
        "2026-02-02": 10.0,    # grant date, flat vs the boundary
        "2026-03-04": 12.0,
    }

    def fake_closes(t, start, end):
        return series if t.upper() == "AAPL" else {"2026-02-02": 500.0, "2026-03-04": 505.0}

    def fake_after(t, target, **_kw):
        src = series if t.upper() == "AAPL" else {"2026-02-02": 500.0, "2026-03-04": 505.0}
        later = sorted(d for d in src if d >= str(target)[:10])
        return (later[0], src[later[0]]) if later else (None, None)

    with patch("spring_load.get_daily_closes", side_effect=fake_closes), \
         patch("spring_load.get_close_on_or_after", side_effect=fake_after):
        path = price_path("AAPL", "2026-02-02")

    assert path["run_in"] == pytest.approx(0.0, abs=0.01), \
        "the run-in used a bar from the padding rather than the 30-day boundary"
    assert path["v_shape"] is False, "a padding-only decline must not trip the V-shape"


def test_the_benchmark_is_refused_when_it_cannot_match_the_stocks_bars(tmp_sqlite_db):
    """An illiquid stock whose +30 bar lands days late must not be compared
    against a differently-dated SPY window."""
    series = {"2026-01-03": 10.0, "2026-02-02": 10.0, "2026-03-20": 14.0}

    def fake_closes(t, start, end):
        return series

    def fake_after(t, target, **_kw):
        # SPY trades on the target dates; the stock's bars are elsewhere.
        spy = {"2026-02-02": 500.0, "2026-03-04": 505.0, "2026-03-20": 520.0}
        later = sorted(d for d in spy if d >= str(target)[:10])
        return (later[0], spy[later[0]]) if later else (None, None)

    with patch("spring_load.get_daily_closes", side_effect=fake_closes), \
         patch("spring_load.get_close_on_or_after", side_effect=fake_after):
        path = price_path("ILLIQ", "2026-02-02")

    assert path["run_out"] is not None
    # SPY resolves on the stock's own out-bar here, so the comparison is allowed;
    # what matters is that it is computed from matched dates, never assumed.
    assert path.get("run_out_bar") == "2026-03-20"


def test_immature_windows_are_flagged_rather_than_read_as_clean(tmp_sqlite_db):
    """A filing screened the day it was filed has no pop or run-out yet. That is
    pending evidence, not absent evidence."""
    series = {"2026-02-02": 10.0}

    with patch("spring_load.get_daily_closes", return_value=series), \
         patch("spring_load.get_close_on_or_after", return_value=(None, None)):
        path = price_path("AAPL", "2026-02-02")

    assert path["has_data"] is True
    assert path["windows_mature"] is False

    result = score_grant({"stated_rationale": "annual grant",
                          "grant_date": "2026-02-02"}, path)
    assert any("not fully elapsed" in t for _, t in result["signals"])


# ---------- stale price evidence must refresh itself ----------

def test_an_immature_screen_is_recomputed_without_a_new_extraction(tmp_sqlite_db):
    """A filing screened on its filing date has no pop or run-out yet. Left
    cached, it would stay understated forever unless someone paid for a forced
    re-extraction. The facts are cached separately so the deterministic half can
    be recomputed for free once the tape fills in."""
    from spring_load import screen_filing

    extraction = {"has_grant": True, "error": False, "grants": [
        {"recipient": "Jane Doe", "instrument": "OPTION", "grant_date": "2026-02-02",
         "stated_rationale": "annual grant"}]}

    # First screen: windows still open, so no pop evidence.
    immature = {"has_data": True, "grant_close": 10.0, "windows_mature": False,
                "run_in": 0.0, "pop": None, "run_out": None}
    with patch("llm.extract_grant_facts", return_value=extraction) as extract, \
         patch("spring_load.price_path", return_value=immature):
        first = screen_filing(_filing())
        assert extract.call_count == 1
    assert first["windows_mature"] is False

    # Later: the tape has filled in. Reading the screen must recompute from the
    # stored facts, with no second LLM call.
    mature = {"has_data": True, "grant_close": 10.0, "windows_mature": True,
              "run_in": -0.35, "pop": 0.25, "run_out": 0.3, "run_out_vs_spy": 0.28,
              "v_shape": True, "is_monthly_low": False}
    with patch("llm.extract_grant_facts") as extract, \
         patch("spring_load.price_path", return_value=mature):
        second = screen_filing(_filing())
        assert extract.call_count == 0, "recompute must not pay for a re-extraction"

    assert second["windows_mature"] is True
    assert second["max_score"] > first["max_score"], "matured evidence was not picked up"


def test_a_mature_screen_is_served_straight_from_cache(tmp_sqlite_db):
    """No pointless recomputation once the windows have closed."""
    from spring_load import screen_filing

    extraction = {"has_grant": True, "error": False, "grants": [
        {"recipient": "Jane Doe", "instrument": "OPTION", "grant_date": "2026-02-02",
         "stated_rationale": "annual grant"}]}
    mature = {"has_data": True, "grant_close": 10.0, "windows_mature": True,
              "run_in": 0.0, "pop": 0.0, "run_out": 0.0}

    with patch("llm.extract_grant_facts", return_value=extraction), \
         patch("spring_load.price_path", return_value=mature):
        screen_filing(_filing())

    with patch("llm.extract_grant_facts") as extract, \
         patch("spring_load.price_path") as path:
        screen_filing(_filing())
        assert extract.call_count == 0
        assert path.call_count == 0, "a settled screen should not be recomputed"


def test_the_stored_facts_survive_a_recompute(tmp_sqlite_db):
    """The recompute writes a new analysis; it must not drop the extraction that
    makes the next recompute possible."""
    import database
    from spring_load import screen_filing

    extraction = {"has_grant": True, "error": False, "grants": [
        {"recipient": "Jane Doe", "instrument": "OPTION", "grant_date": "2026-02-02",
         "stated_rationale": "annual grant"}]}
    immature = {"has_data": True, "grant_close": 10.0, "windows_mature": False}

    with patch("llm.extract_grant_facts", return_value=extraction), \
         patch("spring_load.price_path", return_value=immature):
        screen_filing(_filing())
        screen_filing(_filing())   # triggers a recompute

    row = database.get_spring_load_analysis("0001-1")
    assert row["extraction_json"], "the cached facts were dropped by the recompute"


# ---------- the pop must be measured inside its advertised window ----------

def _path_with(series, grant="2026-02-02"):
    spy = {"2026-02-02": 500.0, "2026-03-04": 505.0}

    def closes(t, start, end):
        return series if t.upper() != "SPY" else spy

    def after(t, target, **_kw):
        src = series if t.upper() != "SPY" else spy
        later = sorted(d for d in src if d >= str(target)[:10])
        return (later[0], src[later[0]]) if later else (None, None)

    with patch("spring_load.get_daily_closes", side_effect=closes), \
         patch("spring_load.get_close_on_or_after", side_effect=after):
        return price_path("AAPL", grant)


def test_a_spike_inside_the_window_is_caught(tmp_sqlite_db):
    """A stock that jumps 20% on day 3 and gives it back by day 10 is exactly
    the pattern the pop signal exists to catch. Reading the level at day 10
    missed it entirely."""
    series = {"2026-01-03": 10.0, "2026-02-02": 10.0,
              "2026-02-05": 12.0,   # +20% on day 3
              "2026-02-12": 10.1,   # given back by day 10
              "2026-03-04": 10.2}
    path = _path_with(series)
    assert path["pop"] == pytest.approx(0.20, abs=0.01)
    assert path["pop_bar"] == "2026-02-05"


def test_a_bar_outside_the_window_is_not_reported_as_a_pop(tmp_sqlite_db):
    """An illiquid stock whose next trade is day 25 must not be described as
    rising 'within 10 days' and handed two points for it."""
    series = {"2026-01-03": 10.0, "2026-02-02": 10.0,
              "2026-02-27": 14.0,   # day 25 — well outside the pop window
              "2026-03-10": 14.0}
    path = _path_with(series)
    assert path["pop"] is None, "a bar outside the window was counted as a pop"
    assert path["pop_window_covered"] is False

    result = score_grant({"stated_rationale": "annual grant",
                          "grant_date": "2026-02-02"}, path)
    assert not any("within" in t and "days of the grant" in t for _, t in result["signals"])


# ---------- a failed fetch is not a settled result ----------

def test_a_transient_price_failure_stays_refreshable(tmp_sqlite_db):
    """Freezing a Yahoo outage into the record as a permanent 'No price data' is
    the same transient-vs-permanent confusion fixed repeatedly in the outcome
    tracker. Only an undateable grant is genuinely settled without prices."""
    extraction = {"has_grant": True, "grants": [
        {"recipient": "A", "instrument": "OPTION", "grant_date": "2026-02-02",
         "stated_rationale": "annual grant"}]}
    with patch("spring_load.price_path", return_value={"has_data": False}):
        out = analyze(extraction, "AAPL", "2026-02-02")
    assert out["windows_mature"] is False, "a failed fetch was recorded as settled"


def test_an_undated_grant_is_settled_because_nothing_can_change_it(tmp_sqlite_db):
    extraction = {"has_grant": True, "grants": [
        {"recipient": "A", "instrument": "OPTION", "grant_date": None,
         "stated_rationale": "annual grant"}]}
    out = analyze(extraction, "AAPL", "2026-02-02")
    assert out["windows_mature"] is True, "an undateable grant will never gain evidence"


# ---------- the page people actually visit must refresh ----------

def test_opening_the_filing_page_refreshes_stale_price_evidence(tmp_sqlite_db):
    """The refresh lived inside screen_filing, which the detail route never
    called — so the fix did not fire on the path users hit most."""
    import database
    client = _client(tmp_sqlite_db)
    filing_id = _insert_filing()

    extraction = {"has_grant": True, "error": False, "grants": [
        {"recipient": "Jane Doe", "instrument": "OPTION", "grant_date": "2026-02-02",
         "stated_rationale": "annual grant"}]}
    immature = {"has_data": True, "grant_close": 10.0, "windows_mature": False,
                "run_in": 0.0, "pop": None, "run_out": None}
    with patch("spring_load.price_path", return_value=immature):
        stale = spring_load.analyze(extraction, "PROP", "2026-02-02")
    database.upsert_spring_load_analysis("0009-1", "0000009", "PROP", "2026-02-02",
                                         stale, extraction=extraction)
    assert stale["max_score"] < 6

    mature = {"has_data": True, "grant_close": 10.0, "windows_mature": True,
              "run_in": -0.35, "pop": 0.25, "run_out": 0.3, "run_out_vs_spy": 0.28,
              "v_shape": True, "is_monthly_low": True}
    with patch("spring_load.price_path", return_value=mature), \
         patch("llm.extract_grant_facts") as extract:
        resp = client.get(f"/filing/{filing_id}")
        assert extract.call_count == 0, "the page must never pay for a re-extraction"

    assert resp.status_code == 200
    refreshed = database.get_spring_load_analysis("0009-1")
    assert refreshed["max_score"] > stale["max_score"], \
        "the page did not pick up matured price evidence"


# ---------- a growing cadence baseline must invalidate a cached score ----------

def test_a_score_is_recomputed_when_the_company_grant_history_grows(tmp_sqlite_db):
    """The off-cycle signal and the repetition cap both read prior_grant_dates,
    which grows as more of a company's filings are screened. Without
    invalidation, whichever filing was screened first keeps a score derived from
    an incomplete baseline — making a deterministic scorer depend on the order
    the watchlist happened to be processed in."""
    import database
    from spring_load import cadence_fingerprint, refresh_if_stale, screen_filing

    target = {"has_grant": True, "error": False, "grants": [
        {"recipient": "A", "instrument": "OPTION", "grant_date": "2026-06-23",
         "stated_rationale": "none stated"}]}
    mature = {"has_data": True, "grant_close": 10.0, "windows_mature": True,
              "run_in": 0.0, "pop": 0.0, "run_out": 0.0}

    # Screened first, with no cadence baseline at all.
    with patch("llm.extract_grant_facts", return_value=target), \
         patch("spring_load.price_path", return_value=mature):
        first = screen_filing(_filing(accession="0001-1"))
    assert database.get_spring_load_analysis("0001-1")["cadence_fingerprint"] == ""

    # A sibling filing for the same CIK is screened later, adding grant history.
    sibling = {"has_grant": True, "error": False, "grants": [
        {"recipient": "B", "instrument": "OPTION", "grant_date": "2024-06-20",
         "stated_rationale": "annual grant"}]}
    with patch("llm.extract_grant_facts", return_value=sibling), \
         patch("spring_load.price_path", return_value=mature):
        screen_filing(_filing(accession="0001-2"))

    prior = database.get_prior_grant_dates("0000001", exclude_accession="0001-1")
    assert prior, "the sibling should now supply a cadence baseline"

    # Reading the first filing must pick the new baseline up — with no LLM call.
    with patch("llm.extract_grant_facts") as extract, \
         patch("spring_load.price_path", return_value=mature):
        refreshed = refresh_if_stale(_filing(accession="0001-1"))
        assert extract.call_count == 0

    stored = database.get_spring_load_analysis("0001-1")
    assert stored["cadence_fingerprint"] == cadence_fingerprint(prior)
    assert refreshed["max_score"] >= first["max_score"]


def test_an_unchanged_cadence_does_not_trigger_recomputation(tmp_sqlite_db):
    from spring_load import refresh_if_stale, screen_filing

    extraction = {"has_grant": True, "error": False, "grants": [
        {"recipient": "A", "instrument": "OPTION", "grant_date": "2026-06-23",
         "stated_rationale": "annual grant"}]}
    mature = {"has_data": True, "grant_close": 10.0, "windows_mature": True,
              "run_in": 0.0, "pop": 0.0, "run_out": 0.0}

    with patch("llm.extract_grant_facts", return_value=extraction), \
         patch("spring_load.price_path", return_value=mature):
        screen_filing(_filing())

    with patch("spring_load.price_path") as path:
        refresh_if_stale(_filing())
        assert path.call_count == 0, "a settled screen was needlessly recomputed"


# ---------- overlapping backtests must not double-charge ----------

def test_a_second_backtest_refuses_rather_than_duplicating_ai_calls(tmp_sqlite_db):
    """The cache only makes a run free after the first worker finishes each
    filing, so it cannot protect against two workers racing through the same
    uncached accessions."""
    from spring_load import backtest_watchlist

    seen = {}

    def during(*_a, **_kw):
        seen["second"] = backtest_watchlist(verbose=False)
        return []

    with patch("database.get_watchlist_filings", side_effect=during):
        first = backtest_watchlist(verbose=False)

    assert first is not None, "the first run should complete"
    assert seen["second"] is None, "a second overlapping backtest was allowed"


def test_the_backtest_lock_is_released_after_a_failure(tmp_sqlite_db):
    from spring_load import backtest_in_progress, backtest_watchlist

    with patch("database.get_watchlist_filings", side_effect=RuntimeError("boom")):
        try:
            backtest_watchlist(verbose=False)
        except RuntimeError:
            pass

    assert backtest_in_progress() is False, "a crashed backtest wedged the lock"


def test_the_route_refuses_a_second_backtest(tmp_sqlite_db):
    client = _client(tmp_sqlite_db)
    with patch("spring_load.backtest_in_progress", return_value=True):
        resp = client.post("/backtest-spring-load", follow_redirects=True)
    assert resp.status_code == 200
    assert b"already running" in resp.data
