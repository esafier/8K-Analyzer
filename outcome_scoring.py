# outcome_scoring.py — Turn recorded outcomes into a read on signal quality.
#
# outcomes.py gathers facts; this module judges them. Kept separate so the
# judgment can be changed — different horizons, different benchmark, a
# different definition of a hit — without re-fetching a single price.
#
# The central definition:
#
#     excess return = stock % change − SPY % change, over identical windows
#
# and a call HITS when the stock moved the way the verdict implied:
#
#     BEARISH hits when the stock LAGS SPY   (excess < 0)
#     BULLISH hits when the stock LEADS SPY  (excess > 0)
#
# NEUTRAL and MIXED calls make no directional claim, so they are counted and
# reported but never scored as hits — folding them in either way would be
# scoring a prediction nobody made.
#
# Two honesty rules are enforced here rather than left to the template:
#   1. n is always carried alongside every rate. A 100% hit rate on n=3 is
#      noise, and the page must not be able to show the rate without the count.
#   2. Below MIN_SAMPLE the rate is withheld entirely (None), because a
#      greyed-out number still gets read as a number.

from database import (
    OUTCOME_DELISTED,
    OUTCOME_HORIZONS,
    OUTCOME_NO_PRICE,
    OUTCOME_OK,
    get_signal_outcomes,
)

# Below this many scored calls, a hit rate is withheld rather than displayed.
# Ten is already generous for a rate you would act on; it is a floor against
# nonsense, not a claim of significance.
MIN_SAMPLE = 10

BEARISH = "BEARISH"
BULLISH = "BULLISH"
DIRECTIONAL = (BEARISH, BULLISH)

# Score buckets for the "does a higher score mean a better call?" breakdown.
SCORE_BUCKETS = (
    ("8-10 (highest conviction)", 8, 10),
    ("5-7", 5, 7),
    ("0-4", 0, 4),
)


def pct_change(start, end):
    """Return (end - start) / start, or None if it cannot be computed.

    A zero or negative start price is not a 0% move — it is unusable data, and
    returning 0.0 would quietly plant a fake datapoint in the middle of the
    scorecard.
    """
    if start is None or end is None:
        return None
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        return None
    if start <= 0:
        return None
    return (end - start) / start


def excess_return(row, horizon):
    """Stock return minus SPY return for one filing at one horizon.

    Returns None when the horizon is unmarked or either leg is unpriceable —
    an unscored row must stay unscored, never default to zero.
    """
    if horizon not in OUTCOME_HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon}")

    stock = pct_change(row.get("baseline_close"), row.get(f"close_{horizon}d"))
    bench = pct_change(row.get("baseline_spy"), row.get(f"spy_{horizon}d"))
    if stock is None or bench is None:
        return None
    return stock - bench


def is_hit(direction, excess):
    """Did the stock move the way the verdict implied?

    Returns None for a non-directional call or a missing number, so callers
    must decide explicitly what to do with unscorable rows instead of
    inheriting a silent False.
    """
    if excess is None or not direction:
        return None
    direction = str(direction).strip().upper()
    if direction == BEARISH:
        return excess < 0
    if direction == BULLISH:
        return excess > 0
    return None


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def summarize(scored):
    """Roll a list of (hit, excess) pairs into one reportable cell.

    `hit_rate` is None below MIN_SAMPLE. That is deliberate: a rate shown with
    a caveat still gets read as a rate.
    """
    hits = [h for h, _ in scored if h is not None]
    excesses = [e for _, e in scored if e is not None]
    n = len(hits)
    return {
        "n": n,
        "hits": sum(1 for h in hits if h),
        "hit_rate": (sum(1 for h in hits if h) / n) if n >= MIN_SAMPLE else None,
        "median_excess": _median(excesses),
        "below_min_sample": n < MIN_SAMPLE,
    }


def _score_rows(rows, horizon):
    """Attach (hit, excess) to each directional row that has a mark."""
    scored = []
    for row in rows:
        excess = excess_return(row, horizon)
        if excess is None:
            continue
        hit = is_hit(row.get("direction"), excess)
        if hit is None:
            continue  # non-directional call — counted elsewhere, never as a hit
        scored.append((row, hit, excess))
    return scored


def _group(scored, key_fn):
    """Group scored rows by a labelling function, dropping rows it rejects."""
    buckets = {}
    for row, hit, excess in scored:
        label = key_fn(row)
        if label is None:
            continue
        buckets.setdefault(label, []).append((hit, excess))
    return buckets


def _ordered_cells(buckets, order):
    """Render buckets in a fixed order so the table does not reshuffle between
    page loads as counts change."""
    cells = []
    for label in order:
        if label in buckets:
            cells.append({"label": label, **summarize(buckets[label])})
    for label in sorted(k for k in buckets if k not in order):
        cells.append({"label": label, **summarize(buckets[label])})
    return cells


def _score_bucket(row):
    score = row.get("signal_score")
    if score is None:
        return None
    try:
        score = float(score)
    except (TypeError, ValueError):
        return None
    for label, low, high in SCORE_BUCKETS:
        if low <= score <= high:
            return label
    return None


def _signal_labels(row):
    """Which named signals this filing carried. A filing can carry several, so
    these buckets deliberately overlap and their counts will not sum to the
    total."""
    labels = []
    if row.get("forfeited_comp"):
        labels.append("Forfeited comp")
    if row.get("has_successor") == 0:
        labels.append("No successor named")
    try:
        if (row.get("departure_count_24mo") or 0) >= 2:
            labels.append("Departure cluster (2+ in 24mo)")
    except TypeError:
        pass
    if row.get("has_market_targets"):
        labels.append("Market-based vesting hurdle")
    return labels


def build_scorecard(horizon=30, rows=None):
    """Build the full scorecard for one horizon.

    Returns a dict the template renders directly. Every rate carries its n, and
    the unscored counts are returned alongside so the page can show what the
    numbers exclude rather than pretending the sample is the whole archive.
    """
    if horizon not in OUTCOME_HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon}")

    rows = get_signal_outcomes() if rows is None else rows
    priced = [r for r in rows if r.get("status") == OUTCOME_OK]
    scored = _score_rows(priced, horizon)

    by_signal = {}
    for row, hit, excess in scored:
        for label in _signal_labels(row):
            by_signal.setdefault(label, []).append((hit, excess))

    # Rows that exist but contribute nothing to this horizon's numbers.
    awaiting = sum(
        1 for r in priced
        if r.get(f"close_{horizon}d") is None
        and str(r.get("direction") or "").upper() in DIRECTIONAL
    )
    non_directional = sum(
        1 for r in priced
        if str(r.get("direction") or "").upper() not in DIRECTIONAL
    )

    return {
        "horizon": horizon,
        "horizons": list(OUTCOME_HORIZONS),
        "min_sample": MIN_SAMPLE,
        "overall": summarize([(h, e) for _, h, e in scored]),
        "by_verdict": _ordered_cells(
            _group(scored, lambda r: r.get("verdict")),
            ["DEEP_LOOK", "MONITOR", "PASS"],
        ),
        "by_direction": _ordered_cells(
            _group(scored, lambda r: (str(r.get("direction") or "").upper() or None)),
            [BEARISH, BULLISH],
        ),
        "by_score": _ordered_cells(
            _group(scored, _score_bucket),
            [label for label, _, _ in SCORE_BUCKETS],
        ),
        "by_signal": _ordered_cells(by_signal, []),
        "coverage": {
            "total_rows": len(rows),
            "priced": len(priced),
            "scored": len(scored),
            "awaiting_horizon": awaiting,
            "non_directional": non_directional,
            "no_price": sum(1 for r in rows if r.get("status") == OUTCOME_NO_PRICE),
            "delisted": sum(1 for r in rows if r.get("status") == OUTCOME_DELISTED),
        },
    }


def best_and_worst(horizon=30, rows=None, limit=10):
    """The individual calls that worked best and worst at this horizon.

    Aggregates hide the cases worth reading. These are the filings to actually
    go and look at — both the ones that nailed it and the ones that did not.
    """
    if horizon not in OUTCOME_HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon}")

    rows = get_signal_outcomes() if rows is None else rows
    priced = [r for r in rows if r.get("status") == OUTCOME_OK]

    entries = []
    for row, hit, excess in _score_rows(priced, horizon):
        entries.append({
            "filing_id": row.get("filing_id"),
            "ticker": row.get("ticker"),
            "filed_date": row.get("filed_date"),
            "verdict": row.get("verdict"),
            "direction": row.get("direction"),
            "signal_score": row.get("signal_score"),
            "excess": excess,
            # Signed so the verdict's own direction decides what "worked"
            # means — a bearish call that fell 20% behind SPY is the single
            # best call on the board, not the worst.
            "signed_excess": -excess if str(row.get("direction") or "").upper() == BEARISH else excess,
            "hit": hit,
        })

    entries.sort(key=lambda e: e["signed_excess"], reverse=True)

    # Split at the midpoint so the two tables can never share a row — printing
    # one filing as both a best and a worst call would imply two results where
    # there is one. Both tables still populate on a short list; they just meet
    # in the middle instead of overlapping.
    half = len(entries) // 2
    n_best = min(limit, len(entries) - half)
    n_worst = min(limit, half)

    return {
        "best": entries[:n_best],
        "worst": list(reversed(entries[len(entries) - n_worst:])) if n_worst else [],
    }
