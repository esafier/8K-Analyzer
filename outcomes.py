"""Outcome tracking — did the stock actually move the way the signal said?

Labels tell us whether the user agreed with a ranking. Outcomes tell us
whether the market did. The two disagree in useful ways: a filing the user
called noise that preceded a 30% drop is the most instructive row in the
database.

How it works, run once per daily job:

  1. Every newly flagged filing (DEEP_LOOK or MONITOR) gets a baseline: its
     price when the signal fired, and SPY at the same moment.
  2. Each row is re-priced when it turns 7, 30 and 90 days old.
  3. The scorecard compares the stock's move to SPY's, signed by direction:
     a bearish call is right when the stock lags the market, a bullish call
     when it leads.

Prospective only. The price source returns current quotes, not history, so a
mark can only be taken on the day it comes due. A missed day is marked late
(on the next run), which is noted rather than hidden: the horizon is "at
least N days", not "exactly N".
"""

from datetime import datetime

from database import (
    OUTCOME_HORIZONS, get_all_outcomes, get_filings_needing_baseline,
    get_outcomes_due, insert_outcome_baseline, mark_outcome,
)

BENCHMARK = "SPY"


def _quote(ticker):
    """Current price, or None. One cached lookup per ticker per hour."""
    try:
        from stock_price import get_stock_price
        return get_stock_price(ticker)
    except Exception as e:
        print(f"[OUTCOMES] Price lookup failed for {ticker}: {e}", flush=True)
        return None


def record_baselines(limit=200):
    """Start tracking newly flagged filings. Returns how many were started.

    Uses the price stored at analysis time when there is one. Filings
    analyzed without a price get today's quote as their baseline — slightly
    later than the signal, which is the honest best available.
    """
    rows = get_filings_needing_baseline(limit=limit)
    if not rows:
        return 0

    spy = _quote(BENCHMARK)
    if spy is None:
        # Without the benchmark the move can't be read net of the market, and
        # a baseline recorded now can't be corrected later. Wait for a day
        # when SPY resolves.
        print("[OUTCOMES] No SPY quote — skipping baselines this run", flush=True)
        return 0

    started = 0
    for row in rows:
        price = row.get("price_at_ingest") or _quote(row["ticker"])
        if not price:
            continue
        insert_outcome_baseline(
            row["id"], row["ticker"], row.get("signal_direction"),
            row.get("signal_types"), row.get("filed_date"), price, spy,
        )
        started += 1
    return started


def mark_due(today=None):
    """Price every outcome row that has reached a horizon. Returns counts."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    spy = _quote(BENCHMARK)
    if spy is None:
        print("[OUTCOMES] No SPY quote — marks deferred to the next run", flush=True)
        return {h: 0 for h in OUTCOME_HORIZONS}

    marked = {}
    for horizon in OUTCOME_HORIZONS:
        count = 0
        for row in get_outcomes_due(horizon, today):
            price = _quote(row["ticker"])
            if price is None:
                continue  # stays due; tried again next run
            mark_outcome(row["id"], horizon, price, spy)
            count += 1
        marked[horizon] = count
    return marked


def run():
    """The daily step: start new rows, mark due ones."""
    started = record_baselines()
    marked = mark_due()
    print(f"[OUTCOMES] started {started}, marked {marked}", flush=True)
    return {"started": started, "marked": marked}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def excess_return(row, horizon):
    """Stock return minus SPY return over the horizon, in percent, or None."""
    p0, s0 = row.get("price_0"), row.get("spy_0")
    pn, sn = row.get(f"price_{horizon}"), row.get(f"spy_{horizon}")
    if not all(isinstance(v, (int, float)) and v > 0 for v in (p0, s0, pn, sn)):
        return None
    return ((pn / p0) - (sn / s0)) * 100.0


def signed_excess(row, horizon):
    """Excess return in the direction the signal called.

    Positive means the call was right: a bearish filing whose stock lagged
    SPY, or a bullish one whose stock led. MIXED and NEUTRAL calls have no
    direction to be right about and are excluded.
    """
    excess = excess_return(row, horizon)
    if excess is None:
        return None
    direction = (row.get("direction") or "").upper()
    if direction == "BEARISH":
        return -excess
    if direction == "BULLISH":
        return excess
    return None


def scorecard(rows=None):
    """Per-signal-type results at each horizon.

    Returns {signal_type: {horizon: {"n", "hit_rate", "avg_signed_excess"}}},
    plus an "ALL" row. Types are counted once per filing they appear in, so a
    filing with three signals contributes to three rows — which is the point:
    each detector is judged by the filings it helped surface.
    """
    rows = rows if rows is not None else get_all_outcomes()
    table = {}

    for row in rows:
        types = [t for t in (row.get("signal_types") or "").split(",") if t] or ["UNTYPED"]
        for horizon in OUTCOME_HORIZONS:
            value = signed_excess(row, horizon)
            if value is None:
                continue
            for key in types + ["ALL"]:
                cell = table.setdefault(key, {}).setdefault(horizon, [])
                cell.append(value)

    result = {}
    for key, horizons in table.items():
        result[key] = {}
        for horizon, values in horizons.items():
            result[key][horizon] = {
                "n": len(values),
                "hit_rate": sum(1 for v in values if v > 0) / len(values),
                "avg_signed_excess": sum(values) / len(values),
            }
    return result


def pending_counts(rows=None):
    """How many rows are tracked, and how many have each mark — so the
    scorecard can say "nothing to show yet" instead of looking broken."""
    rows = rows if rows is not None else get_all_outcomes()
    counts = {"tracked": len(rows)}
    for horizon in OUTCOME_HORIZONS:
        counts[horizon] = sum(1 for r in rows if r.get(f"price_{horizon}") is not None)
    return counts


if __name__ == "__main__":
    from database import initialize_database
    initialize_database()
    run()
