"""Tests for context building.

Two properties matter most here:

  1. **Unknown must stay unknown.** A failed EDGAR call has to produce None,
     never 0 or False. The old departure pipeline learned this the hard way:
     a transient error recorded as "zero departures" permanently suppressed
     the cluster signal for that company.
  2. **Friday-night detection keys off acceptance time, not filed_date.** SEC
     stamps anything accepted after 17:30 ET with the next business day, so a
     Friday 18:42 filing carries a Monday date. Reading the date column would
     miss every burial it is supposed to catch.
"""
import json

import pytest

import context


SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "8-K", "8-K", "10-Q"],
            "filingDate": ["2026-09-01", "2026-07-01", "2026-05-02", "2026-05-10"],
            "items": ["5.02", "4.02", "2.02,9.01", ""],
            "accessionNumber": [
                "0001104659-26-101500",
                "0001104659-26-090000",
                "0001104659-26-080000",
                "0001104659-26-081000",
            ],
            # SEC stamps acceptance in UTC. Verified against live data: an 8-K
            # accepted at 20:25Z keeps a same-day filing date, which is only
            # possible if that is 16:25 ET — i.e. before the 17:30 ET rollover.
            # So 21:42Z on Friday 2026-08-28 is 17:42 ET, after the close, and
            # SEC pushes its filed_date to Monday the 31st.
            "acceptanceDateTime": [
                "2026-08-28T21:42:00.000Z",   # Friday 17:42 ET — the burial slot
                "2026-07-01T11:00:44.000Z",   # Wednesday morning
                "2026-05-02T09:15:00.000Z",
                "2026-05-10T16:05:00.000Z",
            ],
        },
        "files": [{"filingFrom": "2021-11-04"}],
    }
}


@pytest.fixture(autouse=True)
def _clear_submissions_cache():
    """context memoises submissions per process to halve SEC calls. That cache
    would otherwise carry one test's stub into the next."""
    context._submissions_cache.clear()
    yield
    context._submissions_cache.clear()


def _stub_sec(monkeypatch, submissions=SUBMISSIONS):
    monkeypatch.setattr("fetcher.fetch_company_submissions", lambda cik: submissions)


def _stub_markets(monkeypatch, price=10.0, cap=310_000_000, earnings="2026-09-14"):
    monkeypatch.setattr(context, "_price", lambda t: price)
    monkeypatch.setattr(context, "_market_cap", lambda t: cap)
    monkeypatch.setattr(context, "_next_earnings", lambda t: earnings)
    monkeypatch.setattr(context, "_grant_cadence", lambda t, years=3: {})


def _filing(**overrides):
    base = {
        "company": "Acme Corp", "ticker": "ACME", "cik": "0001234567",
        "filed_date": "2026-09-01", "accession_no": "0001104659-26-101500",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def test_context_carries_market_and_calendar_data(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing())

    assert ctx["price"] == 10.0
    assert ctx["market_cap"] == 310_000_000
    assert ctx["next_earnings_date"] == "2026-09-14"
    assert ctx["days_to_earnings"] == 13
    assert ctx["ticker"] == "ACME"


def test_recent_item_codes_are_indexed_for_the_detectors(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing())

    assert ctx["recent_item_codes"]["4.02"] == ["2026-07-01"]
    assert ctx["last_earnings_date"] == "2026-05-02"


def test_ipo_proxy_uses_the_earliest_filing_on_record(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing())

    assert ctx["ipo_date"] == "2021-11-04"
    assert ctx["months_since_ipo"] > 55


def test_recent_ipo_is_measured_in_months(tmp_sqlite_db, monkeypatch):
    submissions = json.loads(json.dumps(SUBMISSIONS))
    submissions["filings"]["files"] = [{"filingFrom": "2026-03-01"}]
    _stub_sec(monkeypatch, submissions)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing())
    assert 5.5 < ctx["months_since_ipo"] < 6.5


# ---------------------------------------------------------------------------
# Friday-night detection
# ---------------------------------------------------------------------------

def test_friday_evening_acceptance_is_flagged(tmp_sqlite_db, monkeypatch):
    """The filing carries a Monday filed_date; only the acceptance timestamp
    reveals it went out after Friday's close."""
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing(filed_date="2026-08-31"))

    assert ctx["is_after_hours_friday"] is True
    assert ctx["accepted_et"] == "2026-08-28 17:42 ET"


def test_weekday_morning_acceptance_is_not_flagged(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing(accession_no="0001104659-26-090000"))
    assert ctx["is_after_hours_friday"] is False


def test_missing_acceptance_time_is_not_a_burial(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch, {"filings": {"recent": {}}})
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing())
    assert ctx["accepted_at"] is None
    assert ctx["is_after_hours_friday"] is False


# ---------------------------------------------------------------------------
# Failures must produce None, not a confident wrong answer
# ---------------------------------------------------------------------------

def test_sec_failure_leaves_history_unknown(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch, None)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing())

    assert ctx["recent_item_codes"] == {}
    assert ctx["ipo_date"] is None
    assert ctx["months_since_ipo"] is None
    # And the price side still works — one failure doesn't take the rest down
    assert ctx["price"] == 10.0


def test_market_data_failure_does_not_raise(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch, price=None, cap=None, earnings=None)

    ctx = context.build_context(_filing())

    assert ctx["price"] is None
    assert ctx["days_to_earnings"] is None


def test_missing_departure_history_is_none_not_zero(tmp_sqlite_db, monkeypatch):
    """A failed EDGAR lookup recorded as 0 would suppress the cluster signal
    for that company permanently."""
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing())
    assert ctx["departures_24mo"] is None


def test_stamped_departure_count_is_reused_without_refetching(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    ctx = context.build_context(_filing(departure_count_24mo=3))
    assert ctx["departures_24mo"] == 3


def test_departure_count_falls_back_to_stored_history(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    history = json.dumps([
        {"person": "Jane Doe", "_error": False},
        {"person": "jane  doe", "_error": False},   # same person, different spelling
        {"person": "John Roe", "_error": False},
        {"person": None, "_error": True},           # failed extraction, not a person
    ])
    ctx = context.build_context(_filing(departure_history=history))
    assert ctx["departures_24mo"] == 2


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_profile_is_fetched_once_per_company(tmp_sqlite_db, monkeypatch):
    """Five filings from one issuer should cost one round of lookups."""
    _stub_sec(monkeypatch)
    calls = []
    monkeypatch.setattr(context, "_price", lambda t: calls.append(t) or 10.0)
    monkeypatch.setattr(context, "_market_cap", lambda t: 310_000_000)
    monkeypatch.setattr(context, "_next_earnings", lambda t: "2026-09-14")
    monkeypatch.setattr(context, "_grant_cadence", lambda t, years=3: {})

    for i in range(5):
        context.build_context(_filing(accession_no=f"0001104659-26-10150{i}"))

    assert len(calls) == 1


def test_refresh_bypasses_the_cache(tmp_sqlite_db, monkeypatch):
    _stub_sec(monkeypatch)
    calls = []
    monkeypatch.setattr(context, "_price", lambda t: calls.append(t) or 10.0)
    monkeypatch.setattr(context, "_market_cap", lambda t: None)
    monkeypatch.setattr(context, "_next_earnings", lambda t: None)
    monkeypatch.setattr(context, "_grant_cadence", lambda t, years=3: {})

    context.build_context(_filing())
    context.build_context(_filing(), refresh=True)

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Grant cadence
# ---------------------------------------------------------------------------

def _insider_rows(*specs):
    return [
        {"insider_name": name, "filing_date": date, "shares": shares,
         "transaction_code": code}
        for name, date, shares, code in specs
    ]


def test_cadence_finds_the_companys_annual_grant_month(monkeypatch):
    monkeypatch.setattr(context, "_fetch_insider_transactions", lambda t, s, limit=250: _insider_rows(
        ("Warren B Kanders", "2024-03-12", 40000, "A"),
        ("Warren B Kanders", "2025-03-14", 50000, "A"),
        ("Brad Williams", "2025-03-14", 20000, "A"),
        ("Warren B Kanders", "2026-03-30", 60000, "A"),
    ))
    cadence = context._grant_cadence("CDRE")

    assert cadence["_annual_months"] == [3]
    assert cadence["warren b kanders"]["median_shares"] == 50000
    assert cadence["warren b kanders"]["grant_count"] == 3


def test_cadence_ignores_sales_and_exercises(monkeypatch):
    """Only code A is a grant. Counting sales would put the 'grant calendar'
    wherever the executive happened to sell."""
    monkeypatch.setattr(context, "_fetch_insider_transactions", lambda t, s, limit=250: _insider_rows(
        ("Warren B Kanders", "2026-08-26", 44655, "S"),
        ("Brad Williams", "2026-08-19", 42770, "M"),
        ("Warren B Kanders", "2026-03-30", 60000, "A"),
    ))
    cadence = context._grant_cadence("CDRE")

    assert "brad williams" not in cadence
    assert cadence["warren b kanders"]["grant_count"] == 1


def test_thin_history_yields_no_cadence_baseline(monkeypatch):
    """One or two grants is not a rhythm. Declaring a grant 'off-cycle'
    against a one-observation baseline would manufacture signal."""
    monkeypatch.setattr(context, "_fetch_insider_transactions", lambda t, s, limit=250: _insider_rows(
        ("Solo Exec", "2026-06-16", 100000, "A"),
    ))
    cadence = context._grant_cadence("NEWCO")
    assert "_annual_months" not in cadence


def test_cadence_survives_a_dead_api(monkeypatch):
    def boom(ticker, start, limit=250):
        raise RuntimeError("api down")

    monkeypatch.setattr(context, "_fetch_insider_transactions", boom)
    assert context._grant_cadence("ACME") == {}


def test_cadence_without_a_ticker_is_empty():
    assert context._grant_cadence("") == {}


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def test_day_math_handles_missing_and_malformed_dates():
    assert context._days_from(None, "2026-09-01") is None
    assert context._days_from("2026-09-01", "not a date") is None
    assert context._days_from("2026-09-01", "2026-09-14") == 13


def test_accepted_timestamp_parsing_tolerates_junk():
    assert context._parse_accepted("garbage") is None
    assert context._parse_accepted(None) is None
    assert context._parse_accepted("2026-08-28T18:42:00.000Z") is not None


# ---------------------------------------------------------------------------
# Departure clusters must be knowable AT analysis time
# ---------------------------------------------------------------------------

def test_local_history_supplies_a_cluster_count_on_first_ingest(tmp_sqlite_db, monkeypatch):
    """EDGAR enrichment runs AFTER analysis, so on the day a filing arrives
    neither the stamped count nor the stored history exists yet. Without a
    local fallback the cluster signal could never fire when it mattered: a
    third CFO exit in 18 months scored like a first one, while the dashboard
    chip beside it read "3 DEP / 24MO"."""
    import database
    from datetime import datetime, timedelta

    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)

    recent = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    for i in range(2):
        database.insert_filing({
            "accession_no": f"prior-{i}", "company": "Acme Corp", "ticker": "ACME",
            "cik": "0001234567", "filed_date": recent, "item_codes": "5.02",
            "filing_url": "u", "raw_text": "t", "summary": "departure",
        })

    ctx = context.build_context(_filing(accession_no="new-1"))
    assert ctx["departures_24mo"] == 3   # two priors plus this filing


def test_no_prior_filings_leaves_the_count_unknown(tmp_sqlite_db, monkeypatch):
    """Still None, never 0 — a company we have never seen is not a company
    where nobody left."""
    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch)
    assert context.build_context(_filing())["departures_24mo"] is None


def test_submissions_are_fetched_once_per_company(tmp_sqlite_db, monkeypatch):
    """Both the item calendar and the acceptance timestamp need this document.
    Two calls per filing doubles SEC load exactly when a rate-limit block
    makes each call cost minutes."""
    calls = []
    monkeypatch.setattr("fetcher.fetch_company_submissions",
                        lambda cik: calls.append(cik) or SUBMISSIONS)
    _stub_markets(monkeypatch)

    context.build_context(_filing())
    assert len(calls) == 1


def test_profile_cache_writes_nulls_so_both_engines_agree(tmp_sqlite_db, monkeypatch):
    """Omitting None fields diverges by engine: SQLite's INSERT OR REPLACE
    blanks them, Postgres' ON CONFLICT DO UPDATE keeps the old value AND bumps
    refreshed_at — so a dead price lookup would leave a stale quote looking
    fresh, and hurdle percentages would be computed against a price nobody
    could see."""
    import database

    _stub_sec(monkeypatch)
    _stub_markets(monkeypatch, price=10.0)
    context.build_context(_filing())
    assert database.get_company_profile("0001234567")["price"] == 10.0

    context._submissions_cache.clear()
    _stub_markets(monkeypatch, price=None)
    context.build_context(_filing(), refresh=True)
    assert database.get_company_profile("0001234567")["price"] is None
