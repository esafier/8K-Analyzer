"""Tests for outcome scoring.

The scorecard's only value is that it can tell the user their scanner is not
working. These tests exist to make sure it stays capable of saying that:
direction-aware hits, no silent zeros, no rate without an n, and no rate at all
on a sample too small to mean anything.
"""
import pytest

import outcome_scoring
from outcome_scoring import (
    MIN_SAMPLE,
    build_scorecard,
    best_and_worst,
    excess_return,
    is_hit,
    pct_change,
)
import database


def _row(direction="BEARISH", verdict="DEEP_LOOK", score=8, base=10.0, close=9.0,
         spy_base=500.0, spy_close=505.0, horizon=30, status=database.OUTCOME_OK,
         ticker="AAPL", **extra):
    row = {
        "filing_id": extra.pop("filing_id", 1),
        "ticker": ticker,
        "filed_date": "2026-01-05",
        "verdict": verdict,
        "direction": direction,
        "signal_score": score,
        "forfeited_comp": extra.pop("forfeited_comp", 0),
        "has_successor": extra.pop("has_successor", 1),
        "departure_count_24mo": extra.pop("departure_count_24mo", 0),
        "has_market_targets": extra.pop("has_market_targets", 0),
        "baseline_close": base,
        "baseline_spy": spy_base,
        "status": status,
    }
    for h in database.OUTCOME_HORIZONS:
        row[f"close_{h}d"] = None
        row[f"spy_{h}d"] = None
    row[f"close_{horizon}d"] = close
    row[f"spy_{horizon}d"] = spy_close
    row.update(extra)
    return row


# ---------- the arithmetic ----------

def test_pct_change_basic():
    assert pct_change(10.0, 11.0) == pytest.approx(0.1)
    assert pct_change(10.0, 9.0) == pytest.approx(-0.1)


def test_pct_change_refuses_unusable_baselines():
    """A zero baseline is not a 0% move — returning 0.0 would plant a fake
    datapoint in the middle of the scorecard."""
    assert pct_change(0, 5.0) is None
    assert pct_change(-1.0, 5.0) is None
    assert pct_change(None, 5.0) is None
    assert pct_change(10.0, None) is None


def test_excess_return_is_stock_minus_benchmark():
    # stock -10%, SPY +1% → excess -11%
    row = _row(base=10.0, close=9.0, spy_base=500.0, spy_close=505.0)
    assert excess_return(row, 30) == pytest.approx(-0.11)


def test_excess_return_is_none_when_horizon_unmarked():
    row = _row(horizon=30)
    assert excess_return(row, 7) is None


def test_unsupported_horizon_raises():
    with pytest.raises(ValueError):
        excess_return(_row(), 14)


# ---------- direction awareness: the whole point ----------

def test_bearish_call_hits_when_the_stock_lags():
    assert is_hit("BEARISH", -0.11) is True
    assert is_hit("BEARISH", 0.11) is False


def test_bullish_call_hits_when_the_stock_leads():
    assert is_hit("BULLISH", 0.11) is True
    assert is_hit("BULLISH", -0.11) is False


def test_non_directional_calls_are_never_scored_as_hits():
    """NEUTRAL and MIXED make no directional claim. Scoring them either way
    would be grading a prediction nobody made."""
    assert is_hit("NEUTRAL", -0.11) is None
    assert is_hit("MIXED", 0.11) is None
    assert is_hit(None, -0.11) is None


def test_direction_is_case_insensitive():
    assert is_hit("bearish", -0.05) is True


# ---------- aggregation honesty ----------

def _many(n, **kw):
    return [_row(filing_id=i, **kw) for i in range(n)]


def test_small_samples_get_no_hit_rate_at_all():
    """A greyed-out number still reads as a number, so it is withheld."""
    card = build_scorecard(30, rows=_many(MIN_SAMPLE - 1))
    assert card["overall"]["n"] == MIN_SAMPLE - 1
    assert card["overall"]["hit_rate"] is None
    assert card["overall"]["below_min_sample"] is True


def test_rate_appears_once_the_sample_is_large_enough():
    card = build_scorecard(30, rows=_many(MIN_SAMPLE))
    assert card["overall"]["hit_rate"] == pytest.approx(1.0)
    assert card["overall"]["below_min_sample"] is False


def test_every_cell_carries_its_n():
    card = build_scorecard(30, rows=_many(MIN_SAMPLE))
    for group in ("by_verdict", "by_direction", "by_score", "by_signal"):
        for cell in card[group]:
            assert "n" in cell and cell["n"] > 0


def test_hit_rate_reflects_a_mixed_record():
    winners = _many(MIN_SAMPLE, close=9.0)    # bearish, stock down → hits
    losers = [_row(filing_id=100 + i, close=12.0) for i in range(MIN_SAMPLE)]  # misses
    card = build_scorecard(30, rows=winners + losers)
    assert card["overall"]["n"] == MIN_SAMPLE * 2
    assert card["overall"]["hit_rate"] == pytest.approx(0.5)


def test_rows_with_no_baseline_never_enter_the_numerator_or_denominator(tmp_sqlite_db):
    """A row that was never priced has no baseline, so there is nothing to score."""
    unpriced = _row(filing_id=999, status=database.OUTCOME_NO_PRICE, base=None, close=None)
    gone = _row(filing_id=998, status=database.OUTCOME_DELISTED, base=None, close=None)
    card = build_scorecard(30, rows=_many(MIN_SAMPLE) + [unpriced, gone])

    assert card["overall"]["n"] == MIN_SAMPLE
    assert card["coverage"]["no_price"] == 1
    assert card["coverage"]["delisted"] == 1
    assert card["coverage"]["total_rows"] == MIN_SAMPLE + 2


def test_a_later_delisting_does_not_erase_the_horizons_it_traded_through(tmp_sqlite_db):
    """A name delisted at day 40 still has a real, tradable 30-day result.
    Dropping it because of what happened afterwards would quietly remove exactly
    the cases where a bearish call was working."""
    traded_then_died = _row(filing_id=42, status=database.OUTCOME_DELISTED,
                            base=10.0, close=5.0, horizon=30)
    card = build_scorecard(30, rows=_many(MIN_SAMPLE) + [traded_then_died])

    assert card["overall"]["n"] == MIN_SAMPLE + 1, "a real 30-day result was thrown away"
    assert card["coverage"]["delisted"] == 1


def test_a_settled_but_unpriceable_horizon_is_reported_separately(tmp_sqlite_db):
    """Stamped with no close = resolved, no price. Distinct from still-waiting."""
    settled = _row(filing_id=7, base=10.0, close=None)
    settled["marked_30d_at"] = "2026-03-01T00:00:00"
    card = build_scorecard(30, rows=[settled])

    assert card["coverage"]["unpriceable_horizon"] == 1
    assert card["coverage"]["awaiting_horizon"] == 0
    assert card["overall"]["n"] == 0


def test_coverage_reports_what_the_numbers_exclude():
    """The page must be able to say what it is not counting."""
    rows = _many(3) + [_row(filing_id=50 + i, horizon=7) for i in range(2)]
    rows.append(_row(filing_id=90, direction="NEUTRAL"))
    card = build_scorecard(30, rows=rows)
    assert card["coverage"]["awaiting_horizon"] == 2
    assert card["coverage"]["non_directional"] == 1


def test_non_directional_rows_are_excluded_from_scoring():
    rows = _many(MIN_SAMPLE) + [_row(filing_id=500 + i, direction="NEUTRAL", close=1.0)
                                for i in range(20)]
    card = build_scorecard(30, rows=rows)
    assert card["overall"]["n"] == MIN_SAMPLE, "non-directional calls leaked into the score"


def test_breakdowns_split_by_verdict_and_direction():
    rows = ([_row(filing_id=i, verdict="DEEP_LOOK") for i in range(3)]
            + [_row(filing_id=10 + i, verdict="MONITOR") for i in range(2)]
            + [_row(filing_id=20 + i, direction="BULLISH", close=12.0) for i in range(4)])
    card = build_scorecard(30, rows=rows)
    verdicts = {c["label"]: c["n"] for c in card["by_verdict"]}
    assert verdicts["DEEP_LOOK"] == 7  # 3 + the 4 bullish rows default to DEEP_LOOK
    assert verdicts["MONITOR"] == 2
    directions = {c["label"]: c["n"] for c in card["by_direction"]}
    assert directions["BEARISH"] == 5 and directions["BULLISH"] == 4


def test_signal_buckets_overlap_by_design():
    """One filing can carry several signals, so these counts do not sum to the
    total — which is why they are reported separately rather than as a split."""
    rows = [_row(filing_id=1, forfeited_comp=1, has_successor=0,
                 departure_count_24mo=3, has_market_targets=1)]
    card = build_scorecard(30, rows=rows)
    labels = {c["label"] for c in card["by_signal"]}
    assert labels == {
        "Forfeited comp", "No successor named",
        "Departure cluster (2+ in 24mo)", "Market-based vesting hurdle",
    }


def test_median_excess_is_reported_even_below_min_sample():
    """The rate is withheld on thin samples, but the raw magnitude still helps."""
    card = build_scorecard(30, rows=_many(2))
    assert card["overall"]["hit_rate"] is None
    assert card["overall"]["median_excess"] == pytest.approx(-0.11)


# ---------- best / worst ----------

def test_best_calls_are_ranked_by_the_verdicts_own_direction():
    """A bearish call on a stock that collapsed is the best call on the board,
    not the worst — the ranking must respect what was actually predicted."""
    rows = [
        _row(filing_id=1, direction="BEARISH", close=5.0),    # -50% vs SPY: great bearish call
        _row(filing_id=2, direction="BEARISH", close=15.0),   # +50%: terrible bearish call
        _row(filing_id=3, direction="BULLISH", close=15.0),   # great bullish call
    ]
    result = best_and_worst(30, rows=rows)
    assert result["best"][0]["filing_id"] in (1, 3)
    assert result["worst"][0]["filing_id"] == 2
    assert result["best"][0]["hit"] is True
    assert result["worst"][0]["hit"] is False


def test_best_and_worst_respects_the_limit():
    result = best_and_worst(30, rows=_many(25), limit=5)
    assert len(result["best"]) == 5 and len(result["worst"]) == 5


def test_scorecard_reads_from_the_database_when_no_rows_passed(tmp_sqlite_db):
    card = build_scorecard(30)
    assert card["overall"]["n"] == 0
    assert card["coverage"]["total_rows"] == 0


def test_priced_count_cannot_be_lower_than_the_scored_count(tmp_sqlite_db):
    """A filing that priced fine and only later went dark is still priced.
    Deriving the count from the lifecycle status made the table self-contradict:
    fewer rows 'priced at baseline' than were actually scored."""
    rows = _many(MIN_SAMPLE) + [
        _row(filing_id=42, status=database.OUTCOME_DELISTED, base=10.0, close=5.0),
    ]
    card = build_scorecard(30, rows=rows)

    assert card["coverage"]["priced"] >= card["coverage"]["scored"]
    assert card["coverage"]["priced"] == MIN_SAMPLE + 1


def test_awaiting_mirrors_what_the_marking_queue_actually_selects(tmp_sqlite_db):
    """This assertion used to be the opposite, and was right at the time: while
    the marking queue gated on status, a delisted row genuinely never came back,
    so calling it 'awaiting' promised work that would never happen.

    The queue no longer gates on status — a delisted row keeps resolving
    horizons it has cached bars for — so the coverage view has to follow. A row
    the queue will still pick up must be reported as waiting, or the table
    understates the remaining gap."""
    gone = _row(filing_id=42, status=database.OUTCOME_DELISTED,
                base=10.0, close=5.0, horizon=7)
    card = build_scorecard(30, rows=[gone])

    assert card["coverage"]["awaiting_horizon"] == 1, \
        "a row the queue will still mark was reported as settled"
    assert card["coverage"]["delisted"] == 1
