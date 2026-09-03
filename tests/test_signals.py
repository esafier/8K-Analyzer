"""Tests for the typed signal detectors.

Each detector gets: the case it must catch, and — more importantly — the
near-miss it must NOT catch. Precision is the whole product here. A detector
that fires on merger-driven departures or planned retirements recreates the
exact noise problem this rebuild exists to fix, so those negatives are tested
as carefully as the positives.
"""
import signals


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def facts(**overrides):
    base = {
        "relevant": True,
        "departures": [],
        "appointments": [],
        "comp_events": [],
        "insider_transactions": [],
        "other": [],
        "filing_flags": {},
    }
    base.update(overrides)
    return base


def departure(**overrides):
    base = {
        "name": "Jane Doe", "title": "Chief Financial Officer", "role_class": "CFO",
        "effective_date": "2026-09-15", "effective_immediately": False,
        "days_notice": 30, "stated_reason": "resigned",
        "is_retirement": False, "is_merger_related": False,
        "mentions_disagreement": False,
        "successor_named": True, "successor_info": "John Roe named interim CFO",
        "comp_impact": None, "forfeiture_flag": "not_disclosed", "tenure_months": 40,
    }
    base.update(overrides)
    return base


def comp_event(**overrides):
    base = {
        "executive": "Jane Doe (CEO)", "role_class": "CEO", "grant_type": "RSUs",
        "grant_value": "$1.0M", "grant_value_usd": 1_000_000, "share_count": 50_000,
        "grant_date": "2026-09-01", "filing_date": "2026-09-02",
        "grant_rationale": "annual grant", "is_annual_cycle": True, "recipient_count": 5,
        "vesting_schedule": "ratable over 3 years", "vesting_years": 3,
        "has_performance_condition": False, "operating_hurdles": None,
        "market_based_targets": {"stock_price": None, "market_cap": None, "tsr": None},
        "hurdle_prices": [], "stock_price_targets": None,
        "is_repricing": False, "has_single_trigger_cic": False,
        "has_tax_gross_up": False, "is_retention_award": False,
    }
    base.update(overrides)
    return base


def types_of(result):
    return set(result.types)


# ---------------------------------------------------------------------------
# FORFEITURE_EXIT — the user's single most important signal
# ---------------------------------------------------------------------------

def test_forfeiture_exit_fires_and_quotes_the_amount():
    result = signals.detect(facts(departures=[departure(
        forfeiture_flag="forfeited",
        comp_impact="forfeits all unvested RSUs (~$4.2M)",
    )]))
    sig = next(s for s in result.signals if s.type == "FORFEITURE_EXIT")
    assert sig.direction == "BEARISH"
    assert sig.severity == 5
    assert "$4.2M" in sig.evidence


def test_partial_forfeiture_scores_below_full():
    full = signals.detect(facts(departures=[departure(forfeiture_flag="forfeited")]))
    mixed = signals.detect(facts(departures=[departure(forfeiture_flag="mixed")]))
    full_sev = next(s.severity for s in full.signals if s.type == "FORFEITURE_EXIT")
    mixed_sev = next(s.severity for s in mixed.signals if s.type == "FORFEITURE_EXIT")
    assert mixed_sev == full_sev - 1


def test_severance_payout_is_not_a_forfeiture():
    """A negotiated exit with severance is the normal case, not a signal."""
    result = signals.detect(facts(departures=[departure(forfeiture_flag="paid_out")]))
    assert "FORFEITURE_EXIT" not in types_of(result)


def test_undisclosed_comp_treatment_is_not_a_forfeiture():
    result = signals.detect(facts(departures=[departure(forfeiture_flag="not_disclosed")]))
    assert "FORFEITURE_EXIT" not in types_of(result)


# ---------------------------------------------------------------------------
# FOR_CAUSE / DISAGREEMENT
# ---------------------------------------------------------------------------

def test_for_cause_termination_fires():
    result = signals.detect(facts(departures=[
        departure(stated_reason="terminated for cause following an internal review")
    ]))
    assert "FOR_CAUSE_OR_DISAGREEMENT" in types_of(result)


def test_acknowledged_disagreement_fires():
    result = signals.detect(facts(departures=[
        departure(role_class="DIRECTOR", title="Director", mentions_disagreement=True)
    ]))
    assert "FOR_CAUSE_OR_DISAGREEMENT" in types_of(result)


def test_boilerplate_no_disagreement_does_not_fire():
    """Almost every 5.02 says there was NO disagreement. If that fired, the
    signal would be on nearly every filing and mean nothing."""
    result = signals.detect(facts(departures=[departure(mentions_disagreement=False)]))
    assert "FOR_CAUSE_OR_DISAGREEMENT" not in types_of(result)


def test_termination_without_cause_does_not_fire():
    result = signals.detect(facts(departures=[
        departure(stated_reason="terminated without cause")
    ]))
    assert "FOR_CAUSE_OR_DISAGREEMENT" not in types_of(result)


# ---------------------------------------------------------------------------
# ABRUPT_CSUITE_EXIT
# ---------------------------------------------------------------------------

def test_immediate_ceo_exit_fires_at_top_severity():
    result = signals.detect(facts(departures=[departure(
        name="Sam Chief", title="Chief Executive Officer", role_class="CEO",
        effective_immediately=True, days_notice=0,
    )]))
    sig = next(s for s in result.signals if s.type == "ABRUPT_CSUITE_EXIT")
    assert sig.severity == 5  # base 4, +1 for a top seat
    assert "effective immediately" in sig.evidence


def test_orderly_transition_does_not_fire():
    result = signals.detect(facts(departures=[departure(days_notice=90)]))
    assert "ABRUPT_CSUITE_EXIT" not in types_of(result)


def test_immediate_retirement_does_not_fire():
    """Retirements are excluded even when immediate — the framing is the point."""
    result = signals.detect(facts(departures=[departure(
        is_retirement=True, effective_immediately=True, days_notice=0)]))
    assert "ABRUPT_CSUITE_EXIT" not in types_of(result)


def test_merger_driven_immediate_exit_does_not_fire():
    """Executives leaving 'upon closing of the Merger' are mechanical turnover.
    This pattern used to flood the dashboard with fake DEEP_LOOKs."""
    result = signals.detect(facts(departures=[departure(
        is_merger_related=True, effective_immediately=True, days_notice=0)]))
    assert "ABRUPT_CSUITE_EXIT" not in types_of(result)


def test_director_resignation_is_not_a_csuite_exit():
    result = signals.detect(facts(departures=[departure(
        role_class="DIRECTOR", title="Director", effective_immediately=True, days_notice=0)]))
    assert "ABRUPT_CSUITE_EXIT" not in types_of(result)


def test_role_is_inferred_from_title_when_unclassified():
    """The extractor sometimes omits role_class; the title still carries it."""
    result = signals.detect(facts(departures=[departure(
        role_class=None, title="Chief Executive Officer",
        effective_immediately=True, days_notice=0)]))
    assert "ABRUPT_CSUITE_EXIT" in types_of(result)


# ---------------------------------------------------------------------------
# NO_SUCCESSOR
# ---------------------------------------------------------------------------

def test_no_successor_named_fires_higher_for_top_seats():
    result = signals.detect(facts(departures=[departure(
        successor_named=False, successor_info="search underway")]))
    sig = next(s for s in result.signals if s.type == "NO_SUCCESSOR")
    assert sig.severity == 4  # base 3, +1 for CFO


def test_named_successor_does_not_fire():
    result = signals.detect(facts(departures=[departure(successor_named=True)]))
    assert "NO_SUCCESSOR" not in types_of(result)


def test_silent_filing_does_not_claim_no_successor():
    """null means the filing didn't say. Asserting 'no successor' from silence
    would put a scary badge on filings that never made the claim."""
    result = signals.detect(facts(departures=[departure(
        successor_named=None, successor_info=None)]))
    assert "NO_SUCCESSOR" not in types_of(result)


# ---------------------------------------------------------------------------
# DEPARTURE_CLUSTER
# ---------------------------------------------------------------------------

def test_cluster_fires_at_two_departures():
    result = signals.detect(facts(departures=[departure()]), {"departures_24mo": 2})
    assert "DEPARTURE_CLUSTER" in types_of(result)


def test_single_departure_in_24mo_is_not_a_cluster():
    result = signals.detect(facts(departures=[departure()]), {"departures_24mo": 1})
    assert "DEPARTURE_CLUSTER" not in types_of(result)


def test_finance_seat_cluster_outranks_general_churn():
    finance = signals.detect(facts(departures=[departure(role_class="CFO")]),
                             {"departures_24mo": 3})
    general = signals.detect(facts(departures=[departure(role_class="OTHER")]),
                             {"departures_24mo": 3})
    fin_sev = next(s.severity for s in finance.signals if s.type == "DEPARTURE_CLUSTER")
    gen_sev = next(s.severity for s in general.signals if s.type == "DEPARTURE_CLUSTER")
    assert fin_sev > gen_sev


def test_missing_cluster_data_does_not_fire():
    """An EDGAR lookup that failed must not read as 'zero departures'."""
    result = signals.detect(facts(departures=[departure()]), {"departures_24mo": None})
    assert "DEPARTURE_CLUSTER" not in types_of(result)


# ---------------------------------------------------------------------------
# EXIT_AFTER_IPO / RESTATEMENT_CONTEXT
# ---------------------------------------------------------------------------

def test_exit_within_a_year_of_ipo_fires():
    result = signals.detect(facts(departures=[departure()]), {"months_since_ipo": 7})
    assert "EXIT_AFTER_IPO" in types_of(result)


def test_exit_at_a_long_public_company_does_not_fire():
    result = signals.detect(facts(departures=[departure()]), {"months_since_ipo": 140})
    assert "EXIT_AFTER_IPO" not in types_of(result)


def test_restatement_in_this_filing_outranks_a_historical_one():
    here = signals.detect(facts(filing_flags={"is_restatement": True}))
    history = signals.detect(
        facts(departures=[departure()]),
        {"filed_date": "2026-09-02", "recent_item_codes": {"4.02": ["2026-07-01"]}},
    )
    assert next(s.severity for s in here.signals if s.type == "RESTATEMENT_CONTEXT") > \
           next(s.severity for s in history.signals if s.type == "RESTATEMENT_CONTEXT")


def test_old_restatement_falls_out_of_the_window():
    result = signals.detect(
        facts(departures=[departure()]),
        {"filed_date": "2026-09-02", "recent_item_codes": {"4.02": ["2024-01-01"]}},
    )
    assert "RESTATEMENT_CONTEXT" not in types_of(result)


# ---------------------------------------------------------------------------
# VALUE_EXTRACTION
# ---------------------------------------------------------------------------

def test_option_repricing_fires():
    result = signals.detect(facts(comp_events=[comp_event(is_repricing=True)]))
    assert "VALUE_EXTRACTION" in types_of(result)


def test_single_trigger_cic_fires():
    result = signals.detect(facts(comp_events=[comp_event(has_single_trigger_cic=True)]))
    assert "VALUE_EXTRACTION" in types_of(result)


def test_large_retention_award_without_performance_fires():
    result = signals.detect(facts(comp_events=[comp_event(
        is_retention_award=True, has_performance_condition=False,
        grant_value_usd=5_000_000)]))
    sig = next(s for s in result.signals if s.type == "VALUE_EXTRACTION")
    assert "$5.0M" in sig.evidence


def test_retention_award_with_real_hurdles_does_not_fire():
    result = signals.detect(facts(comp_events=[comp_event(
        is_retention_award=True, has_performance_condition=True,
        grant_value_usd=5_000_000)]))
    assert "VALUE_EXTRACTION" not in types_of(result)


def test_small_retention_award_does_not_fire():
    result = signals.detect(facts(comp_events=[comp_event(
        is_retention_award=True, has_performance_condition=False,
        grant_value_usd=50_000)]))
    assert "VALUE_EXTRACTION" not in types_of(result)


def test_stacked_extraction_mechanics_score_higher():
    one = signals.detect(facts(comp_events=[comp_event(is_repricing=True)]))
    two = signals.detect(facts(comp_events=[comp_event(
        is_repricing=True, has_tax_gross_up=True)]))
    assert next(s.severity for s in two.signals if s.type == "VALUE_EXTRACTION") > \
           next(s.severity for s in one.signals if s.type == "VALUE_EXTRACTION")


# ---------------------------------------------------------------------------
# INSIDER_MONETIZATION
# ---------------------------------------------------------------------------

def test_pledge_fires_and_explains_the_mechanic():
    result = signals.detect(facts(insider_transactions=[
        {"person": "Pat Founder", "type": "pledge", "shares": 900_000, "value_usd": None}
    ]))
    sig = next(s for s in result.signals if s.type == "INSIDER_MONETIZATION")
    assert "collateral" in sig.evidence


def test_forward_sale_outranks_an_open_market_sale():
    fwd = signals.detect(facts(insider_transactions=[
        {"person": "A", "type": "forward_sale", "value_usd": 84_000_000}]))
    sale = signals.detect(facts(insider_transactions=[
        {"person": "A", "type": "open_market_sale", "value_usd": 84_000_000}]))
    assert next(s.severity for s in fwd.signals if s.type == "INSIDER_MONETIZATION") > \
           next(s.severity for s in sale.signals if s.type == "INSIDER_MONETIZATION")


def test_insider_buy_is_not_treated_as_monetization():
    result = signals.detect(facts(insider_transactions=[
        {"person": "A", "type": "open_market_buy", "value_usd": 250_000}]))
    assert "INSIDER_MONETIZATION" not in types_of(result)


# ---------------------------------------------------------------------------
# Timing signals
# ---------------------------------------------------------------------------

def test_exit_shortly_before_earnings_fires():
    result = signals.detect(facts(departures=[departure()]), {"days_to_earnings": 9})
    assert "EXIT_NEAR_EARNINGS" in types_of(result)


def test_exit_long_before_earnings_does_not_fire():
    result = signals.detect(facts(departures=[departure()]), {"days_to_earnings": 60})
    assert "EXIT_NEAR_EARNINGS" not in types_of(result)


def test_friday_night_filing_fires_at_low_severity():
    result = signals.detect(
        facts(departures=[departure()]),
        {"is_after_hours_friday": True, "accepted_et": "2026-08-28 18:42 ET"},
    )
    sig = next(s for s in result.signals if s.type == "FRIDAY_NIGHT_FILING")
    assert sig.severity == 1
    assert "18:42" in sig.evidence


# ---------------------------------------------------------------------------
# HURDLE_CONVICTION — the flagship bullish signal
# ---------------------------------------------------------------------------

def test_hurdle_above_current_price_fires_with_percentage():
    result = signals.detect(
        facts(comp_events=[comp_event(hurdle_prices=[20], vesting_years=None)]),
        {"price": 10.0},
    )
    sig = next(s for s in result.signals if s.type == "HURDLE_CONVICTION")
    assert sig.direction == "BULLISH"
    assert "+100%" in sig.evidence
    assert sig.data["appreciation_pct"] == 100.0


def test_hurdle_near_the_current_price_does_not_fire():
    result = signals.detect(
        facts(comp_events=[comp_event(hurdle_prices=[10.5])]), {"price": 10.0})
    assert "HURDLE_CONVICTION" not in types_of(result)


def test_headline_appreciation_is_discounted_by_a_long_runway():
    """'+109%' over seven years is ~11%/yr. The percentage flatters; the CAGR
    is the number that should drive the decision, so a long window scores
    lower than the same hurdle on a short one."""
    long_run = signals.detect(
        facts(comp_events=[comp_event(hurdle_prices=[21], vesting_years=7)]), {"price": 10.0})
    short_run = signals.detect(
        facts(comp_events=[comp_event(hurdle_prices=[21], vesting_years=3)]), {"price": 10.0})
    long_sig = next(s for s in long_run.signals if s.type == "HURDLE_CONVICTION")
    short_sig = next(s for s in short_run.signals if s.type == "HURDLE_CONVICTION")
    assert long_sig.severity < short_sig.severity
    assert long_sig.data["implied_cagr_pct"] < 15
    assert "%/yr" in long_sig.evidence


def test_hurdles_without_a_price_cannot_be_scored():
    """No current price means no percentage. Better silent than wrong."""
    result = signals.detect(
        facts(comp_events=[comp_event(hurdle_prices=[20])]), {"price": None})
    assert "HURDLE_CONVICTION" not in types_of(result)


def test_highest_hurdle_tier_is_used():
    result = signals.detect(
        facts(comp_events=[comp_event(hurdle_prices=[12, 15, 25], vesting_years=None)]),
        {"price": 10.0})
    sig = next(s for s in result.signals if s.type == "HURDLE_CONVICTION")
    assert sig.data["top_hurdle"] == 25


# ---------------------------------------------------------------------------
# OFF_CYCLE / OVERSIZED / PRE_EARNINGS
# ---------------------------------------------------------------------------

def test_explicitly_off_cycle_grant_fires():
    result = signals.detect(facts(comp_events=[comp_event(
        is_annual_cycle=False, grant_rationale="special retention award")]))
    assert "OFF_CYCLE_GRANT" in types_of(result)


def test_solo_ceo_option_grant_scores_highest():
    solo = signals.detect(facts(comp_events=[comp_event(
        is_annual_cycle=False, role_class="CEO", recipient_count=1,
        grant_type="Stock Options")]))
    broad = signals.detect(facts(comp_events=[comp_event(
        is_annual_cycle=False, role_class="CEO", recipient_count=9,
        grant_type="RSUs")]))
    assert next(s.severity for s in solo.signals if s.type == "OFF_CYCLE_GRANT") > \
           next(s.severity for s in broad.signals if s.type == "OFF_CYCLE_GRANT")


def test_annual_grant_does_not_fire_as_off_cycle():
    result = signals.detect(facts(comp_events=[comp_event(is_annual_cycle=True)]))
    assert "OFF_CYCLE_GRANT" not in types_of(result)


def test_cadence_history_detects_an_unlabelled_off_cycle_grant():
    """When the filing doesn't say, the company's own Form 4 grant months do."""
    result = signals.detect(
        facts(comp_events=[comp_event(is_annual_cycle=None, grant_date="2026-06-16")]),
        {"grant_cadence": {"_annual_months": [3]}},
    )
    sig = next(s for s in result.signals if s.type == "OFF_CYCLE_GRANT")
    assert "month 6" in sig.evidence


def test_grant_matching_company_cadence_does_not_fire():
    result = signals.detect(
        facts(comp_events=[comp_event(is_annual_cycle=None, grant_date="2026-03-14")]),
        {"grant_cadence": {"_annual_months": [3]}},
    )
    assert "OFF_CYCLE_GRANT" not in types_of(result)


def test_grant_far_larger_than_the_executives_norm_fires():
    result = signals.detect(
        facts(comp_events=[comp_event(executive="Warren B Kanders", share_count=500_000)]),
        {"grant_cadence": {"warren b kanders": {"median_shares": 50_000}}},
    )
    sig = next(s for s in result.signals if s.type == "OVERSIZED_GRANT")
    assert sig.data["multiple_of_prior"] == 10.0


def test_grant_in_line_with_history_does_not_fire():
    result = signals.detect(
        facts(comp_events=[comp_event(executive="Warren B Kanders", share_count=55_000)]),
        {"grant_cadence": {"warren b kanders": {"median_shares": 50_000}}},
    )
    assert "OVERSIZED_GRANT" not in types_of(result)


def test_grant_worth_a_large_share_of_market_cap_fires():
    result = signals.detect(
        facts(comp_events=[comp_event(grant_value_usd=8_000_000, share_count=None)]),
        {"market_cap": 300_000_000},
    )
    sig = next(s for s in result.signals if s.type == "OVERSIZED_GRANT")
    assert sig.data["pct_of_market_cap"] > 1


def test_grant_days_before_earnings_fires():
    result = signals.detect(
        facts(comp_events=[comp_event(grant_date="2026-09-01")]),
        {"next_earnings_date": "2026-09-14"},
    )
    sig = next(s for s in result.signals if s.type == "PRE_EARNINGS_GRANT")
    assert sig.data["days_before_earnings"] == 13


def test_grant_after_earnings_does_not_fire():
    """Granting just after a print is the clean convention — open window."""
    result = signals.detect(
        facts(comp_events=[comp_event(grant_date="2026-09-20")]),
        {"next_earnings_date": "2026-09-14"},
    )
    assert "PRE_EARNINGS_GRANT" not in types_of(result)


# ---------------------------------------------------------------------------
# Aggregation: direction, scoring, judge gate, PASS reasons
# ---------------------------------------------------------------------------

def test_direction_is_bearish_when_only_bearish_signals_fire():
    result = signals.detect(facts(departures=[departure(forfeiture_flag="forfeited")]))
    assert result.direction == "BEARISH"


def test_direction_is_mixed_when_both_sides_are_material():
    result = signals.detect(
        facts(
            departures=[departure(forfeiture_flag="forfeited")],
            comp_events=[comp_event(hurdle_prices=[25], vesting_years=3)],
        ),
        {"price": 10.0},
    )
    assert result.direction == "MIXED"


def test_a_faint_counter_signal_does_not_dilute_a_strong_call():
    """A Friday-night timestamp shouldn't turn a forfeiture exit into MIXED."""
    result = signals.detect(
        facts(
            departures=[departure(forfeiture_flag="forfeited")],
            comp_events=[comp_event(vesting_years=3, has_performance_condition=True)],
        ),
    )
    assert result.direction == "BEARISH"


def test_no_signals_is_neutral_and_scores_zero():
    result = signals.detect(facts())
    assert result.direction == "NEUTRAL"
    assert signals.detector_score(result) == 0
    assert signals.detector_verdict(result) == "PASS"
    assert signals.top_signal_line(result) is None


def test_judge_gate_opens_on_one_severe_signal():
    result = signals.detect(facts(departures=[departure(forfeiture_flag="forfeited")]))
    assert signals.is_judge_candidate(result) is True


def test_judge_gate_opens_on_two_weak_signals():
    result = signals.detect(
        facts(departures=[departure()]),
        {"is_after_hours_friday": True, "days_to_earnings": 5},
    )
    assert len(result.signals) >= 2
    assert signals.is_judge_candidate(result) is True


def test_judge_gate_stays_shut_on_a_lone_weak_signal():
    """The gate is what keeps the daily bill near a dollar."""
    result = signals.detect(facts(), {"is_after_hours_friday": True})
    assert signals.is_judge_candidate(result) is False


def test_detector_score_is_capped_below_a_judged_score():
    """An unjudged row must never outrank one a strong model read and rated
    highly, however many detectors happened to fire."""
    result = signals.detect(
        facts(
            departures=[departure(forfeiture_flag="forfeited", successor_named=False,
                                  effective_immediately=True, days_notice=0)],
        ),
        {"departures_24mo": 4, "is_after_hours_friday": True},
    )
    assert len(result.signals) >= 4
    assert signals.detector_score(result) == 6


def test_top_signal_line_leads_with_the_strongest_and_names_the_rest():
    result = signals.detect(
        facts(departures=[departure(forfeiture_flag="forfeited", successor_named=False)]),
        {"departures_24mo": 3},
    )
    line = signals.top_signal_line(result)
    assert line.startswith("Jane Doe")
    assert "also:" in line


def test_annual_meeting_filing_gets_a_pass_reason():
    result = signals.detect(facts(filing_flags={"is_annual_meeting_only": True}))
    assert result.signals == []
    assert "Annual meeting" in result.pass_reasons[0]


def test_merger_wave_gets_a_pass_reason_instead_of_signals():
    """The pattern that most polluted the old dashboard: six people leaving
    'upon closing', rated DEEP_LOOK because six is more than one."""
    result = signals.detect(facts(departures=[
        departure(name=f"Exec {i}", is_merger_related=True,
                  effective_immediately=True, days_notice=0, successor_named=False)
        for i in range(6)
    ]))
    assert result.signals == []
    assert any("merger" in reason.lower() for reason in result.pass_reasons)


def test_planned_retirement_with_successor_passes():
    result = signals.detect(facts(departures=[departure(
        is_retirement=True, successor_named=True, days_notice=180)]))
    assert result.signals == []
    assert any("retirement" in reason.lower() for reason in result.pass_reasons)


def test_routine_annual_grant_passes():
    result = signals.detect(facts(comp_events=[comp_event(is_annual_cycle=True)]))
    assert result.signals == []
    assert any("annual grant" in reason.lower() for reason in result.pass_reasons)


def test_financing_only_filing_passes():
    result = signals.detect(facts(filing_flags={"is_financing_only": True}))
    assert any("Financing" in reason for reason in result.pass_reasons)


def test_a_real_signal_beats_a_pass_rule():
    """PASS rules explain silence; they never suppress a detector hit. A
    merger wave that also includes a forfeiture is still worth seeing."""
    result = signals.detect(facts(departures=[
        departure(is_merger_related=True, forfeiture_flag="forfeited"),
    ]))
    assert "FORFEITURE_EXIT" in types_of(result)


# ---------------------------------------------------------------------------
# Robustness — model output is routinely ragged
# ---------------------------------------------------------------------------

def test_detection_survives_null_arrays():
    result = signals.detect({"departures": None, "comp_events": None,
                             "insider_transactions": None, "filing_flags": None})
    assert result.signals == []


def test_detection_survives_wrong_types():
    result = signals.detect({"departures": "not a list", "comp_events": [None, "junk"],
                             "filing_flags": "nope"})
    assert result.signals == []


def test_detection_survives_empty_and_none_input():
    assert signals.detect({}).signals == []
    assert signals.detect(None).signals == []


def test_string_numbers_are_parsed():
    """Models emit "3" and "1000000" as often as 3 and 1000000."""
    result = signals.detect(
        facts(comp_events=[comp_event(hurdle_prices=["20"], vesting_years="3")]),
        {"price": "10"},
    )
    assert "HURDLE_CONVICTION" in types_of(result)


def test_unparseable_money_string_is_ignored_not_guessed():
    result = signals.detect(
        facts(comp_events=[comp_event(grant_value_usd="$5.0 million", share_count=None,
                                      is_retention_award=True,
                                      has_performance_condition=False)]))
    assert "VALUE_EXTRACTION" not in types_of(result)


def test_signals_serialize_to_json():
    result = signals.detect(facts(departures=[departure(forfeiture_flag="forfeited")]))
    import json
    payload = json.loads(result.to_json())
    assert payload[0]["type"] == "FORFEITURE_EXIT"
    assert payload[0]["severity"] == 5


def test_signals_are_ordered_by_severity():
    result = signals.detect(
        facts(departures=[departure(forfeiture_flag="forfeited", successor_named=False)]),
        {"is_after_hours_friday": True},
    )
    severities = [s.severity for s in result.signals]
    assert severities == sorted(severities, reverse=True)
