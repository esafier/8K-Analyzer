# outcomes.py — Does the scanner's verdict actually predict anything?
#
# Two jobs live here:
#   capture_baselines()  — stamp each scored filing with the stock and SPY
#                          closes at the time of the filing
#   mark_due_outcomes()  — once 7 / 30 / 90 days have elapsed, record where
#                          the stock and SPY ended up
#
# The scoring itself (hit rates, excess return) lives in outcome_scoring.py.
# This module only gathers facts; it never judges them.
#
# Method: a filing is an event. The baseline is the first close on or after
# the filing date, and each horizon is the first close on or after
# filing date + N days. SPY is always priced on the SAME bar date as the
# stock, so the excess return compares two identical windows and cannot be
# skewed by a one-day offset.
#
# Everything is failure-tolerant by design. A price lookup that fails must
# never block ingest, and a filing that cannot be priced is recorded with a
# status rather than dropped — a scorecard that silently discards its
# hardest cases flatters itself.

from datetime import datetime, timedelta

from database import (
    OUTCOME_DELISTED,
    OUTCOME_HORIZONS,
    OUTCOME_NO_PRICE,
    OUTCOME_OK,
    get_filings_needing_outcome_baseline,
    get_outcomes_needing_mark,
    get_price_history_meta,
    set_outcome_mark,
    set_outcome_status,
    upsert_signal_outcome,
)
from price_history import STATUS_NOT_FOUND, get_close_on_or_after

# The benchmark every signal is measured against.
BENCHMARK_TICKER = "SPY"

# How long a horizon can sit unpriceable before we stop retrying it. A live
# ticker always prints a close within days; a month of silence means the name
# stopped trading, which is a real outcome rather than a pending one.
GIVE_UP_AFTER_DAYS = 30


def _today():
    """Isolated so tests can pin 'now' without patching datetime globally."""
    return datetime.utcnow().date()


def _to_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "year") and not isinstance(value, str):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _ticker_is_gone(ticker):
    """True if the price source has already ruled this symbol non-existent."""
    meta = get_price_history_meta(ticker)
    return bool(meta and meta.get("status") == STATUS_NOT_FOUND)


def capture_baselines(limit=200, verdicts=("DEEP_LOOK", "MONITOR")):
    """Stamp filings that have a verdict but no outcome row with their baseline.

    Safe to call repeatedly and safe to interrupt — each filing is committed as
    it is priced, so a run that dies halfway keeps everything it earned.

    Returns {'priced': n, 'unpriced': n, 'skipped': n}.
    """
    pending = get_filings_needing_outcome_baseline(limit=limit, verdicts=verdicts)
    stats = {"priced": 0, "unpriced": 0, "skipped": 0}

    for filing in pending:
        ticker = (filing.get("ticker") or "").upper()
        filed_date = filing.get("filed_date")
        if not ticker or not filed_date:
            stats["skipped"] += 1
            continue

        try:
            bar_date, close = get_close_on_or_after(ticker, filed_date)

            if close is None:
                # No price for this name at this time. Record why, so the row
                # shows up as an honest gap instead of quietly leaving the
                # denominator.
                status = OUTCOME_DELISTED if _ticker_is_gone(ticker) else OUTCOME_NO_PRICE
                upsert_signal_outcome(filing, status=status)
                stats["unpriced"] += 1
                continue

            # Benchmark on the stock's own bar date — same window, or the
            # excess return is measuring the calendar instead of the signal.
            _, spy_close = get_close_on_or_after(BENCHMARK_TICKER, bar_date)
            if spy_close is None:
                # A missing benchmark is a problem with us, not with the filing.
                # Leave the row uncreated so the next run retries it.
                print(f"[OUTCOMES] No {BENCHMARK_TICKER} close near {bar_date} — retrying later")
                stats["skipped"] += 1
                continue

            upsert_signal_outcome(
                filing,
                baseline_date=bar_date,
                baseline_close=close,
                baseline_spy=spy_close,
                status=OUTCOME_OK,
            )
            stats["priced"] += 1

        except Exception as e:
            # One bad ticker must never take down a backfill of thousands.
            print(f"[OUTCOMES] Baseline failed for {ticker} ({filing.get('accession_no')}): {e}")
            stats["skipped"] += 1

    return stats


def mark_due_outcomes(as_of=None, limit=200):
    """Fill in every horizon that has elapsed and is still unmarked.

    Idempotent: an already-marked horizon is never revisited, so this is safe
    to run daily, twice daily, or twice in a row.

    Returns {horizon: {'marked': n, 'pending': n, 'gave_up': n}}.
    """
    as_of = _to_date(as_of) or _today()
    results = {}

    for horizon in OUTCOME_HORIZONS:
        stats = {"marked": 0, "pending": 0, "gave_up": 0}
        due = get_outcomes_needing_mark(horizon, as_of.isoformat(), limit=limit)

        for row in due:
            ticker = (row.get("ticker") or "").upper()
            filed_date = _to_date(row.get("filed_date"))
            if not ticker or not filed_date:
                stats["pending"] += 1
                continue

            target = filed_date + timedelta(days=horizon)

            try:
                bar_date, close = get_close_on_or_after(ticker, target)

                if close is None:
                    days_overdue = (as_of - target).days
                    if _ticker_is_gone(ticker):
                        # Went dark between the filing and this horizon. That is
                        # an outcome, not a failure — but scoring it is a
                        # judgment call, so it is flagged, not scored.
                        set_outcome_status(row["filing_id"], OUTCOME_DELISTED)
                        stats["gave_up"] += 1
                    elif days_overdue > GIVE_UP_AFTER_DAYS:
                        set_outcome_status(row["filing_id"], OUTCOME_NO_PRICE)
                        stats["gave_up"] += 1
                    else:
                        # Recent enough that the bar may simply not exist yet.
                        stats["pending"] += 1
                    continue

                _, spy_close = get_close_on_or_after(BENCHMARK_TICKER, bar_date)
                if spy_close is None:
                    stats["pending"] += 1
                    continue

                if set_outcome_mark(row["filing_id"], horizon, close, spy_close):
                    stats["marked"] += 1
                else:
                    stats["pending"] += 1

            except Exception as e:
                print(f"[OUTCOMES] {horizon}d mark failed for {ticker}: {e}")
                stats["pending"] += 1

        results[horizon] = stats

    return results


def run_outcome_job(as_of=None, baseline_limit=200, mark_limit=200):
    """One full pass: capture any new baselines, then mark anything now due.

    This is what the daily scheduler calls. Ordering matters — a filing
    ingested today gets its baseline in the same run, rather than waiting a
    day for the next one.
    """
    captured = capture_baselines(limit=baseline_limit)
    print(f"[OUTCOMES] Baselines — priced {captured['priced']}, "
          f"unpriced {captured['unpriced']}, skipped {captured['skipped']}")

    marked = mark_due_outcomes(as_of=as_of, limit=mark_limit)
    for horizon, stats in marked.items():
        if stats["marked"] or stats["gave_up"]:
            print(f"[OUTCOMES] {horizon}d — marked {stats['marked']}, "
                  f"pending {stats['pending']}, gave up {stats['gave_up']}")

    return {"baselines": captured, "marks": marked}
