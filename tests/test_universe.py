"""Tests for the universe filter.

The critical property here is that both gates FAIL CLOSED. An earlier draft
kept filings whose market cap was unknown, which sounds cautious but does the
opposite: market-cap providers return nothing precisely for the OTC shells and
sub-scale issuers the floor exists to remove, so "keep unknowns" re-admits the
exact population being screened out.
"""
import universe


def _meta(ticker, company="Some Co"):
    return {"ticker": ticker, "company": company, "accession_no": "0001-26-000001"}


def test_keeps_company_above_floor():
    kept, skipped = universe.screen_filings(
        [_meta("BIG")], market_caps={"BIG": 500_000_000}
    )
    assert len(kept) == 1
    assert skipped == []


def test_skips_company_below_floor():
    kept, skipped = universe.screen_filings(
        [_meta("TINY")], market_caps={"TINY": 10_000_000}
    )
    assert kept == []
    assert skipped[0]["_skip_reason"] == "below_floor"


def test_boundary_value_is_kept():
    """Exactly at the floor is in — the rule is >= $50M, not > $50M."""
    kept, _ = universe.screen_filings(
        [_meta("EDGE")], market_caps={"EDGE": universe.MIN_MARKET_CAP}
    )
    assert len(kept) == 1


def test_unknown_market_cap_is_skipped_not_kept():
    kept, skipped = universe.screen_filings(
        [_meta("HUH")], market_caps={"HUH": None}
    )
    assert kept == []
    assert skipped[0]["_skip_reason"] == "unknown_market_cap"


def test_ticker_absent_from_cap_map_is_skipped():
    """A ticker the provider never answered for is 'unknown', not 'assume ok'."""
    kept, skipped = universe.screen_filings([_meta("GHOST")], market_caps={})
    assert kept == []
    assert skipped[0]["_skip_reason"] == "unknown_market_cap"


def test_no_ticker_is_skipped():
    kept, skipped = universe.screen_filings(
        [_meta("", company="Some Financing Trust")], market_caps={}
    )
    assert kept == []
    assert skipped[0]["_skip_reason"] == "no_ticker"


def test_ticker_is_matched_case_insensitively():
    kept, _ = universe.screen_filings(
        [_meta(" acme ")], market_caps={"ACME": 900_000_000}
    )
    assert len(kept) == 1


def test_empty_input_is_handled():
    assert universe.screen_filings([]) == ([], [])


def test_summarize_skips_counts_by_reason():
    _, skipped = universe.screen_filings(
        [_meta("TINY"), _meta("HUH"), _meta("")],
        market_caps={"TINY": 1_000_000, "HUH": None},
    )
    counts = universe.summarize_skips(skipped)
    assert counts == {"below_floor": 1, "unknown_market_cap": 1, "no_ticker": 1}


def test_caps_are_fetched_once_for_the_batch(monkeypatch):
    """Five filings from three issuers must cost one lookup of three tickers,
    not five lookups — the fetch is the expensive part of screening."""
    calls = []

    def fake_lookup(tickers):
        calls.append(tickers)
        return {t: 800_000_000 for t in tickers}

    monkeypatch.setattr(universe, "_lookup_market_caps", fake_lookup)

    filings = [_meta("AAA"), _meta("BBB"), _meta("AAA"), _meta("CCC"), _meta("BBB")]
    kept, skipped = universe.screen_filings(filings)

    assert len(calls) == 1
    assert calls[0] == ["AAA", "BBB", "CCC"]
    assert len(kept) == 5


def test_dead_provider_skips_everything_rather_than_admitting_it(monkeypatch):
    """If the market-cap API is down, nothing gets analyzed. That's the
    intended failure: a quiet feed is diagnosable, a flooded one isn't."""
    def boom(tickers):
        raise RuntimeError("provider down")

    monkeypatch.setattr("market_cap.refresh_market_caps_sync", boom)

    kept, skipped = universe.screen_filings([_meta("AAA"), _meta("BBB")])
    assert kept == []
    assert universe.summarize_skips(skipped) == {"unknown_market_cap": 2}
