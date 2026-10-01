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


def test_unknown_split_is_not_disclosed_between_first_and_top_tier():
    """Showing $0 at the middle tier would read as fact; the filing just
    didn't say. At the top tier everything has vested, so that one is known."""
    facts = {"comp_events": [{"executive": "A B", "grant_type": "PSUs", "share_count": 1000,
                              "stock_price_targets": "$20 and $30", "vesting_years": 3}]}
    [p] = build_payoffs(facts, price_at_grant=10, today=date(2026, 1, 1), filed_date="2026-01-01")
    assert p["split_known"] is False
    assert p["rows"][0]["payout"] == 0                       # below the first tier: nothing
    assert p["rows"][1]["payout"] is None and p["rows"][1]["payout_incomplete"]
    assert p["rows"][2]["payout"] == 30_000
    assert p["rows"][2]["increment"] is None                 # step from an unknown isn't known


def test_single_tier_is_the_whole_award():
    facts = {"comp_events": [{"executive": "Joseph Hayek, CEO", "grant_type": "PSUs",
                              "grant_value": "150,000 Performance Shares", "hurdle_prices": [100],
                              "stock_price_targets": "$100 average over a consecutive 90-day period"}]}
    [p] = build_payoffs(facts, price_at_grant=60.47)
    assert p["split_known"] is True
    assert p["rows"][1]["unlocks"] == ["100% of PSUs · 150,000 units"]


# --- real filings that misread before (stored facts, trimmed) -------------------

def test_dollar_millions_are_not_share_prices():
    """WRAP: market-cap thresholds; SPAI: revenue milestones. Both read as
    per-share hurdles of +30,000% before."""
    assert extract_price_values("$150.0 million, $225.0 million, and $506.25 million") == []
    assert extract_price_values("milestones of $5 million, $10 million and $25M") == []
    assert extract_price_values("$12.50 per share") == [12.5]
    wrap = {"comp_events": [{"executive": "Scot Cohen", "grant_type": "PSUs", "share_count": 4_000_000,
            "vesting_schedule": "1,000,000 shares vest at each of four market capitalization thresholds"
                                ": $150.0 million, $225.0 million, $337.5 million, and $506.25 million."}]}
    assert build_payoffs(wrap, price_at_grant=1.645) == []


def test_merger_cash_outs_and_investor_warrants_get_no_ladder():
    events = [
        {"executive": "Company equity award holders", "grant_type": "Cash-out of PSUs",
         "market_based_targets": {"stock_price": "$9.50 per share"}, "hurdle_prices": [9.5]},
        {"executive": "Company RSU Award holders", "grant_type": "Merger consideration / RSU cancellation",
         "stock_price_targets": "$17.00 per Ordinary Share", "hurdle_prices": [17]},
        {"executive": "Eagle Equity Partners IV, LLC (Sponsor)", "grant_type": "Earn-Out Shares",
         "share_count": 2_035_000, "hurdle_prices": [12.5, 15, 17.5]},
        {"executive": "Twenty-three November 2024 investors", "grant_type": "Warrants",
         "share_count": 657_876, "hurdle_prices": [5]},
    ]
    assert build_payoffs({"comp_events": events}, price_at_grant=4.0) == []


def test_tranche_split_read_from_unit_counts():
    """ANGI: '300,000 PSUs on ... a $10.00 stock price hurdle; 300,000 on ...'"""
    text = ("300,000 PSUs on the later of the first anniversary of the Effective Date and achievement "
            "of a $10.00 stock price hurdle; 300,000 on the later of the second anniversary and a $12.00 "
            "hurdle; 300,000 on the later of the third anniversary and a $14.00 hurdle; and 100,000 on "
            "the later of the fourth anniversary and a $20.00 hurdle.")
    got = parse_tranches(text, total_units=1_000_000)
    assert got == [(10.0, 0.3), (12.0, 0.3), (14.0, 0.3), (20.0, 0.1)]


def test_tranche_split_from_respective_list_and_cumulative_tiers():
    # FBIN: percentages listed after the prices
    got = parse_tranches("$80, $100, and $125, with 30%, 40%, and 30% of the shares vesting "
                         "based on attainment of the respective goals")
    assert [round(f, 2) for _, f in got] == [0.3, 0.4, 0.3]
    # COTY: cumulative — 50% at the low tier, 100% at the high one
    got = parse_tranches("100% vesting at $9.00 per share, 50% vesting at $5.56 per share")
    assert dict(got) == {9.0: pytest.approx(0.5), 5.56: pytest.approx(0.5)}


def test_strike_written_before_the_words_exercise_price():
    """SPAI: '$4.50 exercise price' — the strike, not a $4.50 hurdle."""
    facts = {"comp_events": [{"executive": "D E", "grant_type": "Stock Options",
                              "grant_value": "750,000 options", "share_count": 750_000,
                              "stock_price_targets": "$4.50 exercise price", "hurdle_prices": [4.5]}]}
    assert build_payoffs(facts, price_at_grant=6.15) == []


def test_sizing_price_below_the_market_is_not_a_hurdle():
    """NINE: '$9 stock price used to determine the number of RSUs' at $10.34."""
    facts = {"comp_events": [{"executive": "Ann Fox, CEO", "grant_type": "RSUs",
                              "grant_value": "$2,980,000", "grant_value_usd": 2_980_000,
                              "hurdle_prices": [9],
                              "stock_price_targets": "$9 stock price used to determine the number of RSUs"}]}
    assert build_payoffs(facts, price_at_grant=10.34) == []


def test_dollar_denominated_award_granted_at_the_hurdle_pays_its_dollar_amount():
    """GRND: RSUs worth $1.6M granted when the VWAP first reaches $26, by a date."""
    facts = {"comp_events": [{"executive": "John North", "grant_type": "RSUs",
        "grant_value": "$1,600,000", "grant_value_usd": 1_600_000, "hurdle_prices": [26],
        "market_based_targets": {"stock_price": "Average VWAP equals or exceeds $26 for 15 consecutive trading days"},
        "vesting_schedule": "Upon the first occurrence on or before December 31, 2027 of the first performance "
                            "threshold, a number of RSUs equal to $1,600,000 divided by the average VWAP for the "
                            "preceding 90 trading days will be granted and fully vested on grant."}]}
    [p] = build_payoffs(facts, price_at_grant=13.56, today=date(2026, 7, 1))
    assert p["rows"][1]["payout"] == 1_600_000
    assert p["deadline"] == date(2027, 12, 31)


def test_years_and_option_dollar_values_are_not_unit_counts():
    assert parse_units({"grant_value": "2026 PSUs under the LTIP"}) == (None, False)
    facts = {"comp_events": [
        {"executive": "T H", "grant_type": "PSUs", "share_count": 1000,
         "price_hurdles": [{"price": 20, "vest_pct": 100}]},
        {"executive": "T H", "grant_type": "Stock Options", "grant_value": "$4.5 million",
         "grant_value_usd": 4_500_000},
    ]}
    [p] = build_payoffs(facts, price_at_grant=10)
    assert p["uncounted"] == ["Stock Options"]
    assert p["rows"][-1]["payout"] == 20_000


def test_salary_and_bonus_read_from_text():
    facts = {"comp_events": [
        {"executive": "Amanda Busby, COO", "grant_type": "Employment Agreement Compensation",
         "grant_value": "$450,000 annual base salary; annual cash bonus target of 70% of annual salary"},
        {"executive": "Amanda Busby, COO", "grant_type": "PSUs", "share_count": 10_000,
         "price_hurdles": [{"price": 20, "vest_pct": 100}]},
    ]}
    [p] = build_payoffs(facts, price_at_grant=10)
    assert p["salary"] == 450_000
    assert p["annual_pay"] == pytest.approx(450_000 * 1.7)
    assert p["package"] == ["10,000 PSUs · 1 price hurdle"]   # the salary isn't an award


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


def test_tiered_exercise_prices_are_strikes_not_vesting_hurdles():
    """SUIG: warrants struck at four premium prices, time-vested. Valued as
    shares before, which put $1.4M on a $1.17 stock's warrants."""
    events = [{"executive": "Kristina Campbell (Director)", "grant_type": "Warrants",
               "grant_value": "Warrants to purchase 207,565 shares of Common Stock",
               "share_count": 207_565, "hurdle_prices": [5.42, 5.962, 6.504, 7.046],
               "market_based_targets": {"stock_price": "$5.420, $5.962, $6.504, and $7.046 per share exercise prices"}}]
    [p] = build_payoffs({"comp_events": events}, price_at_grant=1.17)
    assert p["package"] == ["207,565 Warrants · 4 premium strikes"]
    assert p["rows"][1]["payout"] == 0                          # at the lowest strike: worth nothing
    assert p["rows"][-1]["payout"] is None                      # split not stated: not guessed
    # With the split stated, each tranche is valued over its own strike.
    events[0]["price_hurdles"] = [{"price": x, "vest_pct": 25} for x in (5.42, 5.962, 6.504, 7.046)]
    [p] = build_payoffs({"comp_events": events}, price_at_grant=1.17)
    top = p["rows"][-1]
    assert top["payout"] == pytest.approx(207_565 * 0.25 * ((7.046 - 5.42) + (7.046 - 5.962) + (7.046 - 6.504)))



# --- v4.5 structured extraction, as the re-extraction run produced it ---------

def test_payout_above_target_is_cumulative_and_not_capped():
    """BBWI: earn 75% / 100% / 150% / 200% of target at $40 / $60 / $80 / $100.
    Read as slices and capped at 100%, the top take was half what it is."""
    facts = {"comp_events": [{"executive": "Daniel Heaf, CEO", "grant_type": "PSUs",
        "grant_value": "$10 million (equal to 591,366 shares of Company common stock)",
        "grant_value_usd": 10_000_000, "performance_period_years": 4, "grant_date": "2026-09-20",
        "price_hurdles": [{"price": 40, "vest_pct": 75}, {"price": 60, "vest_pct": 100},
                          {"price": 80, "vest_pct": 150}, {"price": 100, "vest_pct": 200}]}]}
    [p] = build_payoffs(facts, price_at_grant=16.675, today=date(2026, 10, 1))
    takes = [r["payout"] for r in p["rows"][1:]]
    assert takes == pytest.approx([591_366 * 0.75 * 40, 591_366 * 1.0 * 60,
                                   591_366 * 1.5 * 80, 591_366 * 2.0 * 100])
    assert p["rows"][3]["unlocks"] == ["+50% of target PSUs · 295,683 units"]
    assert p["package"] == ["591,366 PSUs · 4 price hurdles, 4-yr window, up to 200% of target"]


def test_overlapping_slices_that_exceed_the_award_are_unreadable():
    facts = {"comp_events": [{"executive": "A B", "grant_type": "PSUs", "share_count": 1000,
        "price_hurdles": [{"price": 20, "vest_pct": 60}, {"price": 30, "vest_pct": 50}]}]}
    [p] = build_payoffs(facts, price_at_grant=10)
    assert p["split_known"] is False


def test_token_salary_is_stated_not_divided_by():
    """ANGI: a $1 salary gave '40,000,000x annual cash pay'."""
    facts = {"comp_events": [{"executive": "Michael Steib (CEO)", "grant_type": "PSUs",
        "share_count": 1_000_000, "base_salary_usd": 1,
        "price_hurdles": [{"price": 20, "vest_pct": 100}]}]}
    [p] = build_payoffs(facts, price_at_grant=5.05)
    assert p["annual_pay"] is None and p["rows"][-1]["pay_multiple"] is None
    assert p["token_salary"] == 1
    assert "Takes a $1 salary." in p["headline"] and "cash pay" not in p["headline"]


def test_sentence_long_measurement_is_condensed():
    def m(text):
        return build_payoffs({"comp_events": [{"executive": "A B", "grant_type": "PSUs",
            "share_count": 1000, "hurdle_measurement": text,
            "price_hurdles": [{"price": 20, "vest_pct": 100}]}]}, price_at_grant=10)[0]["measurement"]
    assert m("The volume-weighted average closing price of Common Stock must equal or exceed "
             "the applicable hurdle for 30 consecutive trading days.") == "30-day VWAP"
    assert m("60 consecutive calendar days, described as a consecutive 60-day calendar average") \
        == "60-day average price"
    assert m("Twenty-day VWAP of the Company's common stock") == "20-day VWAP"
    assert m("Closing price of the Company's common stock on the Principal Market on each RSU "
             "vesting date").endswith("…")
