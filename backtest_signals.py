# backtest_signals.py — Score past signals against what the stock actually did.
#
# Usage:
#   python backtest_signals.py                     30-day horizon, whole archive
#   python backtest_signals.py --horizon 7         7-day horizon
#   python backtest_signals.py --min-score 7       only high-conviction calls
#   python backtest_signals.py --limit 100         cap the sample (fewer Yahoo calls)
#   python backtest_signals.py --json out.json     also dump the per-filing rows
#
# This writes NOTHING. It reads filings through database.get_connection() (so it
# follows DATABASE_URL to the live archive) and pulls prices straight from Yahoo
# without touching the price_history cache. Safe to run against production.
#
# Why it reports a base rate instead of just a hit rate:
#   A hit rate compared against 50% is misleading whenever the sample drifts. If
#   most filings in the window lagged SPY, "BEARISH" scores well and "BULLISH"
#   scores badly for reasons that have nothing to do with the filing. A direction
#   label carrying no information scores its own base rate, so that — not 50% —
#   is the number an edge has to beat.

import argparse
import json
import math
import statistics
from datetime import datetime, timedelta

import database
import price_history as ph
from outcome_scoring import excess_return, is_hit

BENCHMARK = "SPY"
LOOKAHEAD_DAYS = 10
SPAN_PAD_BEFORE = 10
SPAN_PAD_AFTER = 20
DIRECTIONAL = ("BULLISH", "BEARISH")


def _d(text):
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def wilson(hits, n, z=1.96):
    """95% confidence interval for a proportion.

    A bare percentage hides its sample size, and most buckets here are small
    enough that the interval is the whole story.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((center - margin) / denom, (center + margin) / denom)


def load_filings(min_score=None, limit=None):
    """Scored filings with a ticker, newest first. Read-only."""
    conn = database.get_connection()
    try:
        cursor = conn.cursor()
        p = database._placeholder()
        query = ("SELECT accession_no, company, ticker, filed_date, triage_verdict, "
                 "signal_direction, signal_score FROM filings "
                 "WHERE signal_score IS NOT NULL AND ticker IS NOT NULL AND ticker <> '' "
                 "ORDER BY filed_date DESC")
        params = []
        if min_score is not None:
            query = query.replace("WHERE signal_score IS NOT NULL",
                                  f"WHERE signal_score >= {p}")
            params.append(min_score)
        if limit:
            query += " LIMIT " + str(int(limit))
        cursor.execute(query, tuple(params)) if params else cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def first_close_on_or_after(closes, target_date, max_days=LOOKAHEAD_DAYS):
    """(bar_date, close) for the first bar on/after target_date, or (None, None).

    Filing dates and computed horizons land on weekends and holidays. A gap is
    not a price, so this never interpolates — it takes the next real close or
    reports that there wasn't one.
    """
    start = _d(target_date)
    for offset in range(max_days + 1):
        key = (start + timedelta(days=offset)).isoformat()
        if key in closes:
            return key, closes[key]
    return None, None


def fetch_price_map(tickers, span_start, span_end, verbose=True):
    """One Yahoo call per ticker covering the whole span.

    Fetching the entire span in a single request matters: Yahoo re-bases closes
    for splits as of fetch time, so two requests made at different moments can
    return different bases for the same date. Within one response every bar
    shares one basis, which is what keeps a baseline and its horizon comparable.
    """
    prices = {}
    for i, ticker in enumerate(sorted(set(tickers)), 1):
        result = ph.fetch_from_yahoo(ticker, span_start, span_end)
        prices[ticker] = result if isinstance(result, dict) else {}
        if verbose and (i % 25 == 0 or i == len(set(tickers))):
            print(f"  [{i}/{len(set(tickers))}] priced")
    return prices


def score_rows(rows, horizons, verbose=True):
    """Attach baseline, horizon closes and excess returns. Returns new dicts."""
    rows = [r for r in rows if r.get("ticker") and r.get("filed_date")]
    if not rows:
        return []

    dates = sorted(str(r["filed_date"])[:10] for r in rows)
    span_start = (_d(dates[0]) - timedelta(days=SPAN_PAD_BEFORE)).isoformat()
    span_end = (_d(dates[-1]) + timedelta(days=max(horizons) + SPAN_PAD_AFTER)).isoformat()

    if verbose:
        print(f"{len(rows)} filings, {len(set(r['ticker'] for r in rows))} tickers, "
              f"span {span_start}..{span_end}")
        print("fetching benchmark...")
    bench = ph.fetch_from_yahoo(BENCHMARK, span_start, span_end)
    if not isinstance(bench, dict) or not bench:
        raise RuntimeError(f"no {BENCHMARK} data for {span_start}..{span_end}")

    prices = fetch_price_map([r["ticker"] for r in rows], span_start, span_end, verbose)

    scored = []
    for row in rows:
        rec = dict(row)
        rec["filed_date"] = str(row["filed_date"])[:10]
        closes = prices.get(row["ticker"]) or {}

        base_date, base_close = first_close_on_or_after(closes, rec["filed_date"])
        if base_date is None:
            rec["unpriced_reason"] = "no_baseline"
            scored.append(rec)
            continue
        # The benchmark is read on the stock's OWN bar date, not the calendar
        # date. A halt or a listing-specific holiday would otherwise shift one
        # leg relative to the other and show up as a fake excess return.
        _, base_bench = first_close_on_or_after(bench, base_date)
        if base_bench is None:
            rec["unpriced_reason"] = "no_benchmark_baseline"
            scored.append(rec)
            continue

        rec["baseline_date"] = base_date
        rec["baseline_close"] = base_close
        rec["baseline_spy"] = base_bench

        for h in horizons:
            target = (_d(base_date) + timedelta(days=h)).isoformat()
            bar_date, close = first_close_on_or_after(closes, target)
            if bar_date is None:
                continue
            _, bench_close = first_close_on_or_after(bench, bar_date)
            if bench_close is None:
                continue
            rec[f"close_{h}d"] = close
            rec[f"spy_{h}d"] = bench_close
            rec[f"bar_{h}d"] = bar_date

        for h in horizons:
            ex = excess_return(rec, h)
            rec[f"excess_{h}d"] = ex
            rec[f"hit_{h}d"] = is_hit(rec.get("signal_direction"), ex)
        scored.append(rec)
    return scored


def report(scored, horizon):
    key = f"excess_{horizon}d"
    priced = [r for r in scored if r.get(key) is not None]
    if not priced:
        print(f"\nNo filing could be priced at {horizon} days.")
        return

    base_up = sum(1 for r in priced if r[key] > 0) / len(priced)
    print(f"\n{'=' * 72}")
    print(f"{horizon}-DAY EXCESS RETURN vs {BENCHMARK}")
    print(f"  priced {len(priced)} of {len(scored)} filings")
    print(f"  base rate: {base_up:.1%} beat {BENCHMARK}, {1 - base_up:.1%} lagged it")
    print(f"  a direction with no information scores its own base rate, not 50%")

    print(f"\n  {'direction':10} {'n':>4} {'hit':>7} {'95% CI':>17} {'base':>7} {'edge':>9}")
    for direction in DIRECTIONAL:
        rows = [r for r in priced if (r.get("signal_direction") or "").upper() == direction]
        if not rows:
            continue
        hits = sum(1 for r in rows if r.get(f"hit_{horizon}d") is True)
        lo, hi = wilson(hits, len(rows))
        rate = hits / len(rows)
        base = base_up if direction == "BULLISH" else 1 - base_up
        print(f"  {direction:10} {len(rows):4} {rate:7.1%} [{lo:6.1%},{hi:6.1%}]"
              f" {base:7.1%} {100 * (rate - base):+7.1f}pp")

    print(f"\n  hit rate by score, held within one direction"
          f" (mixing directions across score bands is a confound):")
    for direction in DIRECTIONAL:
        base = base_up if direction == "BULLISH" else 1 - base_up
        for lo_s, hi_s in ((1, 5), (6, 6), (7, 10)):
            rows = [r for r in priced
                    if (r.get("signal_direction") or "").upper() == direction
                    and r.get("signal_score") is not None
                    and lo_s <= r["signal_score"] <= hi_s]
            if len(rows) < 5:
                continue
            hits = sum(1 for r in rows if r.get(f"hit_{horizon}d") is True)
            lo, hi = wilson(hits, len(rows))
            band = str(lo_s) if lo_s == hi_s else f"{lo_s}-{hi_s}"
            print(f"    {direction:8} score {band:5} n={len(rows):3}"
                  f" hit={hits / len(rows):6.1%} [{lo:5.1%},{hi:5.1%}]  base={base:5.1%}")

    print(f"\n  direction mix by score band (shows the confound directly):")
    for lo_s, hi_s in ((1, 5), (6, 6), (7, 10)):
        rows = [r for r in priced
                if (r.get("signal_direction") or "").upper() in DIRECTIONAL
                and r.get("signal_score") is not None
                and lo_s <= r["signal_score"] <= hi_s]
        if not rows:
            continue
        bearish = sum(1 for r in rows if (r["signal_direction"] or "").upper() == "BEARISH")
        band = str(lo_s) if lo_s == hi_s else f"{lo_s}-{hi_s}"
        print(f"    score {band:5} n={len(rows):3}  BEARISH share={bearish / len(rows):5.1%}")

    print(f"\n  does the score track how far the stock moves?")
    for score in sorted({r["signal_score"] for r in priced if r.get("signal_score") is not None}):
        rows = [r for r in priced if r.get("signal_score") == score]
        if len(rows) < 8:
            continue
        median_abs = statistics.median(abs(r[key]) for r in rows)
        print(f"    score {score:<4} n={len(rows):3}  median |excess| = {median_abs:6.2%}")


def main():
    parser = argparse.ArgumentParser(
        description="Score past signals against realized returns. Writes nothing.")
    parser.add_argument("--horizon", type=int, default=30, choices=[7, 30, 90])
    parser.add_argument("--min-score", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap the sample — one Yahoo call per distinct ticker")
    parser.add_argument("--json", default=None, help="Dump per-filing rows here")
    args = parser.parse_args()

    backend = "PostgreSQL (DATABASE_URL is set)" if database._using_postgres() \
        else f"SQLite ({database.DATABASE_PATH})"
    print(f"Reading from: {backend}")

    rows = load_filings(min_score=args.min_score, limit=args.limit)
    if not rows:
        print("No scored filings with a ticker found.")
        return

    scored = score_rows(rows, (args.horizon,))
    report(scored, args.horizon)

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(scored, handle, indent=1, default=str)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
