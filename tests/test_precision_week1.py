"""Precision fixes from the first week of production.

In the first 160 filings the new pipeline analyzed live, 65 (about 40%) landed
in MONITOR. Two detectors caused nearly all of it:

  DEPARTURE_CLUSTER fired on filings where no officer was leaving: an
  audit-committee appointment, a promotion, a director retiring. The company
  simply had history, and the count itself included board seats and prior
  appointment-only 5.02s.

  OFF_CYCLE_GRANT fired on every new-hire and promotion package, and on
  severance, because none of those are "annual grants". A grant dated by a
  start date carries no view about the share price.

Each test below is a filing pattern taken from that week.
"""
import json

import context
import signals


def facts(**overrides):
    base = {"relevant": True, "departures": [], "appointments": [], "comp_events": [],
            "insider_transactions": [], "other": [], "filing_flags": {}}
    base.update(overrides)
    return base


def officer_exit(**overrides):
    base = {"name": "Jane Doe", "title": "Chief Accounting Officer", "role_class": "CAO",
            "effective_immediately": True, "days_notice": 0, "stated_reason": "resigned",
            "is_retirement": False, "is_merger_related": False,
            "successor_named": True, "forfeiture_flag": "not_disclosed"}
    base.update(overrides)
    return base


def grant(**overrides):
    base = {"executive": "Pat Newhire (CFO)", "role_class": "CFO", "grant_type": "RSUs",
            "grant_value_usd": 900_000, "share_count": 40_000, "is_annual_cycle": False,
            "grant_rationale": "new hire", "recipient_count": 1, "vesting_years": 3,
            "hurdle_prices": []}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# DEPARTURE_CLUSTER needs an exit in THIS filing
# ---------------------------------------------------------------------------

def test_cluster_does_not_fire_on_an_appointment_only_filing():
    """Twenty One Capital: a new audit-committee member, five prior departures."""
    result = signals.detect(
        facts(appointments=[{"name": "David Goldschmidt", "title": "Director",
                             "role_class": "DIRECTOR"}]),
        {"departures_24mo": 5},
    )
    assert "DEPARTURE_CLUSTER" not in {s.type for s in result.signals}


def test_cluster_does_not_fire_on_a_director_retirement():
    """DTE, NovoCure, Douglas Elliman: a board member retires at a company
    with officer turnover. The retirement is not itself an officer exit."""
    result = signals.detect(
        facts(departures=[officer_exit(title="Director", role_class="DIRECTOR",
                                       is_retirement=True)]),
        {"departures_24mo": 3},
    )
    assert "DEPARTURE_CLUSTER" not in {s.type for s in result.signals}


def test_cluster_does_not_fire_on_a_merger_driven_exit():
    result = signals.detect(
        facts(departures=[officer_exit(is_merger_related=True)]),
        {"departures_24mo": 4},
    )
    assert "DEPARTURE_CLUSTER" not in {s.type for s in result.signals}


def test_cluster_still_fires_on_an_officer_exit_with_history():
    """Harrow: the CAO leaves immediately — the company's second exit."""
    result = signals.detect(facts(departures=[officer_exit()]), {"departures_24mo": 2})
    assert "DEPARTURE_CLUSTER" in {s.type for s in result.signals}


def test_stored_history_counts_officers_not_board_seats():
    history = json.dumps([
        {"person": "A Officer", "position": "Chief Financial Officer"},
        {"person": "B Director", "position": "Director"},
        {"person": "C Director", "position": "Independent Director"},
        {"person": "D Managing", "position": "Managing Director, Head of Sales"},
    ])
    count = context._departures_24mo({"departure_history": history,
                                      "departure_count_24mo": 4})
    assert count == 2  # the CFO and the managing director; the board seats drop out


def test_local_fallback_ignores_appointment_and_board_only_filings(monkeypatch):
    prior = [
        {"auto_subcategory": '["CFO Appointment"]'},
        {"auto_subcategory": '["Board Member Departure"]'},
        {"auto_subcategory": '["COO Departure", "COO Appointment"]'},
    ]
    monkeypatch.setattr("database.get_departure_history", lambda *a, **k: prior)
    assert context._local_departure_count({"cik": "1", "accession_no": "x"}) == 2


def test_local_fallback_with_no_prior_exits_is_unknown(monkeypatch):
    monkeypatch.setattr("database.get_departure_history",
                        lambda *a, **k: [{"auto_subcategory": '["CEO Appointment"]'}])
    assert context._local_departure_count({"cik": "1", "accession_no": "x"}) is None


# ---------------------------------------------------------------------------
# OFF_CYCLE_GRANT needs a discretionary equity grant
# ---------------------------------------------------------------------------

def test_new_hire_package_is_not_off_cycle():
    """Kura Oncology, Carlsmed, MSC: the CFO's appointment package."""
    result = signals.detect(facts(comp_events=[grant()]))
    assert "OFF_CYCLE_GRANT" not in {s.type for s in result.signals}


def test_grant_to_someone_appointed_in_the_same_filing_is_not_off_cycle():
    """Even when the model leaves grant_rationale blank, a package for the
    person being appointed is a hiring package."""
    result = signals.detect(facts(
        appointments=[{"name": "Pat Newhire", "title": "Chief Financial Officer"}],
        comp_events=[grant(grant_rationale=None)],
    ))
    assert "OFF_CYCLE_GRANT" not in {s.type for s in result.signals}


def test_promotion_package_is_not_off_cycle():
    result = signals.detect(facts(comp_events=[grant(grant_rationale="promotion to President")]))
    assert "OFF_CYCLE_GRANT" not in {s.type for s in result.signals}


def test_severance_is_not_a_grant():
    """Zimmer Biomet: 12 months of salary on a restructuring exit."""
    result = signals.detect(facts(comp_events=[grant(grant_type="Severance",
                                                     grant_rationale=None)]))
    assert "OFF_CYCLE_GRANT" not in {s.type for s in result.signals}


def test_cash_bonus_is_not_an_off_cycle_grant():
    result = signals.detect(facts(comp_events=[grant(grant_type="Cash Bonus",
                                                     grant_rationale="retention")]))
    assert "OFF_CYCLE_GRANT" not in {s.type for s in result.signals}


def test_special_retention_equity_to_a_sitting_ceo_still_fires():
    """Park Hotels: 331,564 off-cycle retention shares to the sitting CEO.
    Discretionary, equity, and dated by the board — the real signal."""
    result = signals.detect(facts(comp_events=[grant(
        executive="Thomas Baltimore (CEO)", role_class="CEO",
        grant_type="Restricted Stock", grant_rationale="retention")]))
    assert "OFF_CYCLE_GRANT" in {s.type for s in result.signals}


# ---------------------------------------------------------------------------
# hurdle_prices returned as a string
# ---------------------------------------------------------------------------

def test_hurdle_prices_given_as_a_string_are_parsed_not_split_into_characters():
    """'$25.00' iterated character by character became hurdles of 2 and 5."""
    result = signals.detect(
        facts(comp_events=[grant(grant_rationale="annual", is_annual_cycle=True,
                                 hurdle_prices="$20.00 and $25.00")]),
        {"price": 10.0},
    )
    sig = next(s for s in result.signals if s.type == "HURDLE_CONVICTION")
    assert sig.data["top_hurdle"] == 25.0
