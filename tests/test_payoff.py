"""The hurdle payoff ladder: what a price-hurdle grant takes and pays.

The fixture is the filing that motivated the feature — a CEO's 800,000 PSUs
vesting 30/30/40 at 60-day VWAPs of $21.50 / $41.00 / $61.50 over five years,
alongside 1.2M at-the-money options — stored in the pre-v4.5 schema, where
the tranche split lives only in free text.
"""
import json
from datetime import date

import pytest

import payoff
from market_targets import extract_price_values
from payoff import (build_payoffs, parse_tranches, parse_units, required_cagr,
                    touch_probability, money)

CEO = "Wayne Paterson, Vice Chairman and Chief Executive Officer"
FMV = "Exercise price equal to the fair market value of a share of common stock on the grant date."
SPLIT = ("30% vests at VWAP of at least $21.50, an additional 30% at $41.00, "
         "and the remaining 40% at $61.50.")


def _legacy_facts():
    return {"comp_events": [
        {"executive": CEO, "grant_type": "Stock Options",
         "grant_value": "1,200,000 nonqualified employee stock options",
         "market_based_targets": {"stock_price": FMV},
         "grant_date": "2026-09-13", "vesting_years": 4, "hurdle_prices": []},
        {"executive": "Wayne Paterson", "grant_type": "PSUs",
         "grant_value": "800,000 performance-based restricted stock units",
         "vesting_schedule": "Performance-based vesting over a five-year performance "
                             "period beginning on the grant date.",
         "market_based_targets": {"stock_price": "VWAP of at least $21.50, $41.00, and "
                                                 "$61.50 over a 60-trading-day period."},
         "stock_price_targets": SPLIT,
         "hurdle_prices": [21.5, 41, 61.5], "grant_date": "2026-09-13"},
        {"executive": "Matthew McDonnell", "grant_type": "Stock Options",
         "grant_value": "200,000 options",
         "market_based_targets": {"stock_price": FMV}},
    ]}


TODAY = date(2026, 9, 28)


def _build(**kw):
    args = dict(price_at_grant=8.42, price_today=7.65, filed_date="2026-09-15", today=TODAY)
    args.update(kw)
    return build_payoffs(kw.pop("facts", None) or _legacy_facts(), **args)


# --- parsing ---------------------------------------------------------------

def test_price_list_with_commas_keeps_the_cents():
    """Regression: '$21.50, $41.00' parsed as $21.00 — the lookahead rejected
    the list comma and the regex backtracked to the whole-dollar part."""
    assert extract_price_values("VWAP of at least $21.50, $41.00, and $61.50") == [21.5, 41.0, 61.5]
    assert extract_price_values("$1,000 and $1000") == [1000.0, 1000.0]


def test_tranche_split_read_from_fraction_before_each_price():
    assert parse_tranches(SPLIT) == [(21.5, 0.3), (41.0, 0.3), (61.5, pytest.approx(0.4))]


def test_tranche_split_read_from_fraction_after_each_price():
    got = parse_tranches("$20.00 (50% of the units) and $30.00 (50% of the units)")
    assert got == [(20.0, 0.5), (30.0, 0.5)]


def test_equal_tranches():
    got = parse_tranches("vests in three equal tranches at $10, $15 and $20")
    assert [f for _, f in got] == [pytest.approx(1 / 3)] * 3


def test_unknown_split_is_none_not_a_guess():
    assert parse_tranches("VWAP of at least $21.50, $41.00, and $61.50") == [
        (21.5, None), (41.0, None), (61.5, None)]


def test_units_from_grant_value_text_ignore_dollar_amounts():
    assert parse_units({"grant_value": "800,000 performance-based RSUs"}) == (800000, False)
    assert parse_units({"grant_value": "1.2 million options"}) == (1_200_000, False)
    assert parse_units({"grant_value": "$5.0 million", "grant_value_usd": 5e6}, 10.0) == (500_000, True)
    assert parse_units({"grant_value": "$5.0 million"}) == (None, False)


# --- the ladder --------------------------------------------------------------

def test_ladder_for_the_motivating_filing():
    [ceo] = _build()   # McDonnell has only options: no hurdle, no ladder
    assert ceo["executive"] == "Wayne Paterson"
    assert ceo["title"] == "Vice Chairman and Chief Executive Officer"
    assert ceo["deadline"] == date(2031, 9, 13)
    assert ceo["measurement"] == "60-day VWAP"
    assert ceo["split_known"] is True
    assert ceo["strike_approx"] is True          # FMV → price at filing

    today, t1, t2, t3 = ceo["rows"]
    assert today["is_today"] and today["payout"] == 0   # options under water, PSUs unvested

    assert [r["price"] for r in (t1, t2, t3)] == [21.5, 41.0, 61.5]
    assert round(t1["pct_today"]) == 181
    assert round(t3["pct_today"]) == 704
    assert t1["unlocks"] == ["30% of PSUs · 240,000 units"]

    # Take = vested PSUs × price + option spread over the $8.42 strike.
    assert t1["payout"] == pytest.approx(240_000 * 21.5 + 1_200_000 * (21.5 - 8.42))
    assert t3["payout"] == pytest.approx(800_000 * 61.5 + 1_200_000 * (61.5 - 8.42))
    assert t3["increment"] == pytest.approx(t3["payout"] - t2["payout"])

    # CAGR from today's price over the ~4.96 years left.
    years = (date(2031, 9, 13) - TODAY).days / 365.25
    assert t3["cagr"] == pytest.approx(((61.5 / 7.65) ** (1 / years) - 1) * 100)
    assert t1["difficulty"] == "Hard" and t3["difficulty"] == "Moonshot"
    assert ceo["at_risk_pct"] == 100
    assert "$112.9M" in ceo["headline"] and "+704%" in ceo["headline"]


def test_structured_v45_fields_win_over_text():
    facts = {"comp_events": [{
        "executive": "Jane Doe, CEO", "grant_type": "PSUs", "share_count": 100_000,
        "price_hurdles": [{"price": 20, "vest_pct": 50}, {"price": 30, "vest_pct": 50}],
        "performance_period_years": 3, "grant_date": "2026-01-01",
        "stock_price_targets": "vests at $99", "base_salary_usd": 1_000_000,
        "target_bonus_pct": 100,
    }]}
    [p] = build_payoffs(facts, price_at_grant=10, price_today=10, today=date(2026, 1, 1))
    assert [r["price"] for r in p["rows"][1:]] == [20, 30]
    assert p["deadline"] == date(2029, 1, 1)
    assert p["annual_pay"] == 2_000_000
    assert p["rows"][-1]["pay_multiple"] == pytest.approx(3_000_000 / 2_000_000)


def test_unknown_split_counts_nothing_until_the_top():
    facts = {"comp_events": [{"executive": "A B", "grant_type": "PSUs", "share_count": 1000,
                              "stock_price_targets": "$20 and $30", "vesting_years": 3}]}
    [p] = build_payoffs(facts, price_at_grant=10, today=date(2026, 1, 1), filed_date="2026-01-01")
    assert p["split_known"] is False
    assert p["rows"][1]["payout"] == 0
    assert p["rows"][2]["payout"] == 30_000


def test_option_with_one_price_is_a_strike_not_a_hurdle():
    facts = {"comp_events": [{"executive": "A B", "grant_type": "Stock Options",
                              "grant_value": "10,000 options", "hurdle_prices": [12.0]}]}
    assert build_payoffs(facts, price_at_grant=12.0) == []


def test_time_vested_rsus_are_not_at_risk():
    facts = {"comp_events": [
        {"executive": "A B", "grant_type": "PSUs", "share_count": 1000,
         "price_hurdles": [{"price": 20, "vest_pct": 100}]},
        {"executive": "A B", "grant_type": "RSUs", "share_count": 1000},
    ]}
    [p] = build_payoffs(facts, price_at_grant=10)
    assert p["value_today"] == 10_000
    assert p["at_risk_pct"] == pytest.approx(50.0)


def test_nothing_computable_returns_empty():
    assert build_payoffs(None) == []
    assert build_payoffs({"comp_events": "oops"}) == []
    assert build_payoffs(_legacy_facts()) == []            # no price at all
    assert build_payoffs({"comp_events": [{"executive": "X", "grant_type": "Cash Bonus",
                                           "hurdle_prices": [20]}]}, price_at_grant=10) == []


# --- math --------------------------------------------------------------------

def test_required_cagr():
    assert required_cagr(20, 10, 1) == pytest.approx(100.0)
    assert required_cagr(20, 10, None) is None


def test_touch_probability_is_sane():
    assert touch_probability(10, 9, 1, 0.3) == 1.0
    near = touch_probability(10, 11, 3, 0.4)
    far = touch_probability(10, 80, 3, 0.4)
    assert 0 < far < near < 1
    assert touch_probability(10, 20, 3, None) is None


def test_money():
    assert money(112_900_000) == "$112.9M"
    assert money(450_000) == "$450K"
    assert money(None) == "—"


# --- detail page ---------------------------------------------------------------

def _insert(structured, **extra):
    from database import insert_filing, get_filing_by_accession
    row = {
        "accession_no": "acc-payoff-1", "company": "Hurdle Corp", "ticker": "HRDL",
        "cik": "0001234567", "filed_date": "2026-09-15", "item_codes": "5.02",
        "summary": "PSU grant", "auto_category": "Compensation", "auto_subcategory": None,
        "filing_url": "https://example.com", "raw_text": "", "matched_keywords": "",
        "structured_summary": json.dumps(structured), "has_market_targets": 1,
        "price_at_ingest": 8.42,
    }
    row.update(extra)
    insert_filing(row)
    return get_filing_by_accession("acc-payoff-1")


def test_detail_page_shows_ladder_instead_of_raw_targets(tmp_sqlite_db):
    from app import app
    from database import upsert_stock_price
    from market_targets import detect_market_targets

    structured = _legacy_facts()
    mt = detect_market_targets(structured)
    structured.update(has_market_targets=True, market_targets=mt["targets"])
    row = _insert(structured)
    upsert_stock_price("HRDL", 7.65)

    app.config["TESTING"] = True
    resp = app.test_client().get(f"/filing/{row['id']}")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Hurdle payoff" in html
    assert "$61.50" in html and "+704%" in html
    assert "240,000 units" in html
    assert "Moonshot" in html
    assert "grant terms as filed" in html          # comp list folded away
    assert "MARKET-BASED TARGETS" not in html.upper().replace("—", "")  # old box gone
    assert "Odds*" not in html                      # no volatility in tests


def test_detail_page_shows_odds_when_volatility_known(tmp_sqlite_db, monkeypatch):
    from app import app
    structured = _legacy_facts()
    structured.update(has_market_targets=True)
    row = _insert(structured)
    monkeypatch.setattr(payoff, "volatility_for", lambda t, timeout=3.0: 0.65)
    app.config["TESTING"] = True
    html = app.test_client().get(f"/filing/{row['id']}").data.decode()
    assert "Odds*" in html and "65%" in html


def test_detail_page_without_hurdles_is_unchanged(tmp_sqlite_db):
    from app import app
    row = _insert({"comp_events": [{"executive": "A B", "grant_type": "RSUs",
                                    "grant_value": "1,000 RSUs"}]},
                  has_market_targets=0)
    app.config["TESTING"] = True
    html = app.test_client().get(f"/filing/{row['id']}").data.decode()
    assert "Hurdle payoff" not in html
