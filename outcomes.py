"""Outcome tracking — did the stock actually move the way the signal said?

Labels tell us whether the user agreed with a ranking. Outcomes tell us
whether the market did. The two disagree in useful ways: a filing the user
called noise that preceded a 30% drop is the most instructive row in the
database.

How it works, run once per daily job (or by hand: `python outcomes.py`):

  1. Every flagged filing (DEEP_LOOK or MONITOR) gets an outcome row.
  2. Each row is priced from daily closing history (price_history.py): the
     baseline is the close on the filing date — the next trading day's if it
     was filed on a weekend or holiday — and each mark is the first close at
     least 7, 30 and 90 calendar days later. SPY is priced on the same dates.
  3. The scorecard compares the stock's move to SPY's, signed by direction:
     a bearish call is right when the stock lags the market, a bullish call
     when it leads.

Because the marks come from history, a late or skipped run changes nothing:
the next run prices the row at the right dates. The first version priced
from live quotes on whatever day the job ran, which gave filings stored by a
backfill a baseline weeks after they were filed; rows priced that way
(price_source NULL) are re-priced from history on the next run.

Only completed sessions count — a close dated today is still moving, so a
mark that lands on today waits for tomorrow's run.
"""

from datetime import datetime, timezone

import price_history
from database import (
    OUTCOME_HORIZONS, get_all_outcomes, get_filings_needing_baseline,
    get_outcomes_to_price, insert_outcome_baseline, set_outcome_prices,
)

BENCHMARK = "SPY"

# How far before the earliest filing to start a history fetch: enough to span
# a long weekend plus a holiday, so the first row always finds its session.
_LOOKBACK_DAYS = 7


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_baselines(limit=2000):
    """Start tracking newly flagged filings. Returns how many were started.

    Rows are created unpriced; price_rows fills them from history.
    """
    started = 0
    for row in get_filings_needing_baseline(limit=limit):
        if not row.get("filed_date"):
            continue
        insert_outcome_baseline(
            row["id"], row["ticker"], row.get("signal_direction"),
            row.get("signal_types"), str(row["filed_date"])[:10],
        )
        started += 1
    return started


def price_from_history(ingest_date, stock, spy, today):
    """Baseline and marks for one filing from two close series.

    Returns {column: price} with only the columns that could be priced.
    A mark needs a stock close AND a SPY close on the same session, or it is
    left out — excess return with the benchmark on a different day is noise.
    """
    base = price_history.close_on_or_after(stock, ingest_date, before=today)
    if not base or base[0] not in spy:
        return {}
    base_day, base_price = base
    prices = {"price_0": base_price, "spy_0": spy[base_day]}
    for horizon in OUTCOME_HORIZONS:
        mark = price_history.close_on_or_after(
            stock, price_history.add_days(base_day, horizon), before=today)
        if mark and mark[0] in spy:
            prices[f"price_{horizon}"] = mark[1]
            prices[f"spy_{horizon}"] = spy[mark[0]]
    return prices


def _needs_work(row, today):
    """Whether pricing this row could change anything today."""
    if row.get("price_source") != "history" or row.get("price_0") is None:
        return True
    for horizon in OUTCOME_HORIZONS:
        if row.get(f"price_{horizon}") is None:
            # The next missing mark is due once its date is a completed session.
            return price_history.add_days(row["ingest_date"], horizon) < today
    return False


def price_rows(today=None):
    """Price every outcome row that needs it. Returns counts.

    One history fetch per ticker, one for SPY. A ticker the source can't
    price (delisted, renamed) stays unpriced and is retried next run.
    """
    today = today or _today()
    rows = [r for r in get_outcomes_to_price()
            if r.get("ticker") and r.get("ingest_date") and _needs_work(r, today)]
    counts = {"priced": 0, "unpriced": 0}
    if not rows:
        return counts

    start = price_history.add_days(min(r["ingest_date"] for r in rows), -_LOOKBACK_DAYS)
    spy = price_history.closes(BENCHMARK, start, today)
    if not spy:
        # Without the benchmark no move can be read net of the market.
        print("[OUTCOMES] No SPY history — pricing deferred to the next run", flush=True)
        counts["unpriced"] = len(rows)
        return counts

    by_ticker = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"].strip().upper(), []).append(row)

    for ticker, ticker_rows in by_ticker.items():
        first = min(r["ingest_date"] for r in ticker_rows)
        stock = price_history.closes(ticker, price_history.add_days(first, -_LOOKBACK_DAYS), today)
        for row in ticker_rows:
            prices = price_from_history(row["ingest_date"], stock, spy, today) if stock else {}
            if not prices:
                counts["unpriced"] += 1
                continue
            set_outcome_prices(row["id"], prices)
            counts["priced"] += 1
    return counts


def run(today=None):
    """The daily step: start rows for new flags, price everything due."""
    started = record_baselines()
    priced = price_rows(today=today)
    print(f"[OUTCOMES] started {started}, {priced}", flush=True)
    return {"started": started, **priced}


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


def filing_results(rows=None, limit=100):
    """The individual filings behind the table, newest first: each row's
    signed excess at every horizon it has reached. Only directional calls
    with at least one mark — the rest have nothing to show."""
    rows = rows if rows is not None else get_all_outcomes()
    results = []
    for row in rows:
        marks = {h: signed_excess(row, h) for h in OUTCOME_HORIZONS}
        if all(v is None for v in marks.values()):
            continue
        results.append({
            "filing_id": row.get("filing_id"),
            "ticker": row.get("ticker"),
            "company": row.get("company"),
            "filed": row.get("ingest_date"),
            "direction": (row.get("direction") or "").upper(),
            "signal_types": [t for t in (row.get("signal_types") or "").split(",") if t],
            "marks": marks,
        })
    results.sort(key=lambda r: (r["filed"] or "", r["filing_id"] or 0), reverse=True)
    return results[:limit]


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
    counts = pending_counts()
    print(f"[OUTCOMES] tracked {counts['tracked']}, marks: "
          + ", ".join(f"{h}d={counts[h]}" for h in OUTCOME_HORIZONS), flush=True)
