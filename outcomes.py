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
from price_history import STATUS_NOT_FOUND, get_close_on_or_after, has_coverage

# The benchmark every signal is measured against.
BENCHMARK_TICKER = "SPY"

# How long a horizon can sit unpriceable before we stop retrying it. A live
# ticker always prints a close within days; a month of silence means the name
# stopped trading, which is a real outcome rather than a pending one.
GIVE_UP_AFTER_DAYS = 30

# Must match get_close_on_or_after's lookahead, so the coverage check asks
# about exactly the span the lookup actually searched.
LOOKAHEAD_DAYS = 10


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


def _answer_is_final(ticker, anchor_date):
    """Did the price source actually answer for this window, or did we just
    fail to reach it?

    Everything that permanently writes a filing off depends on this. A network
    outage returns no price for every ticker alike; treating that as a verdict
    would burn thousands of rows in a single bad run, and they would never be
    retried because the status stops them being picked up again.
    """
    anchor = _to_date(anchor_date)
    if not anchor:
        return False
    return has_coverage(ticker, anchor, anchor + timedelta(days=LOOKAHEAD_DAYS))


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
                if _ticker_is_gone(ticker):
                    # A definitive answer: the symbol does not exist.
                    upsert_signal_outcome(filing, status=OUTCOME_DELISTED)
                    stats["unpriced"] += 1
                elif _answer_is_final(ticker, filed_date):
                    # We reached the source and it has no bars here. Record it,
                    # so the row is an honest gap rather than quietly leaving
                    # the denominator.
                    upsert_signal_outcome(filing, status=OUTCOME_NO_PRICE)
                    stats["unpriced"] += 1
                else:
                    # We never got an answer. Leave the row uncreated so the
                    # next run retries it — writing it off here would let one
                    # network outage silently delete the archive from the
                    # scorecard.
                    stats["skipped"] += 1
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
                    elif days_overdue > GIVE_UP_AFTER_DAYS and _answer_is_final(ticker, target):
                        # Long overdue AND the source actually answered — the
                        # name has stopped printing closes. Giving up on a
                        # request that merely failed would be permanent.
                        set_outcome_status(row["filing_id"], OUTCOME_NO_PRICE)
                        stats["gave_up"] += 1
                    else:
                        # Recent enough that the bar may not exist yet, or we
                        # never reached the source. Either way, retry later.
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


def run_outcome_backfill(run_id=None, verbose=True, as_of=None, batch_size=200,
                         max_batches=200):
    """Walk the whole archive: price every scored filing, then mark every
    horizon that has already elapsed.

    This is what makes the scorecard useful immediately instead of in 90 days —
    the filings already in the database are mostly old enough that their 7/30/90
    day horizons are in the past and can be scored right now.

    Costs no LLM spend. Network cost is one price request per ticker per span,
    cached thereafter, throttled inside price_history.
    """
    from database import complete_backfill_run, count_signal_outcomes

    totals = {"priced": 0, "unpriced": 0, "skipped": 0}
    batches = 0

    try:
        while batches < max_batches:
            stats = capture_baselines(limit=batch_size)
            batches += 1
            for key in totals:
                totals[key] += stats[key]

            if verbose:
                print(f"[OUTCOMES BACKFILL] Batch {batches}: "
                      f"priced {stats['priced']}, unpriced {stats['unpriced']}, "
                      f"skipped {stats['skipped']}", flush=True)

            # Nothing priced and nothing recorded means either the queue is
            # empty or every remaining row is failing for the same reason.
            # Either way, looping again will not help.
            if stats["priced"] == 0 and stats["unpriced"] == 0:
                break

        if batches >= max_batches and verbose:
            print(f"[OUTCOMES BACKFILL] Stopped at the {max_batches}-batch cap — "
                  f"run again to continue.", flush=True)

        # Marking is batched too — get_outcomes_needing_mark applies a LIMIT, so
        # a single call would silently leave a large archive part-scored and
        # still print "Done". Keep going until a round makes no progress.
        marks = {h: {"marked": 0, "pending": 0, "gave_up": 0} for h in OUTCOME_HORIZONS}
        mark_batches = 0
        while mark_batches < max_batches:
            batch = mark_due_outcomes(as_of=as_of, limit=batch_size * 10)
            mark_batches += 1
            progressed = False
            for horizon, stats in batch.items():
                marks[horizon]["marked"] += stats["marked"]
                marks[horizon]["gave_up"] += stats["gave_up"]
                # pending is what is still outstanding right now, not a running
                # total — take the latest reading instead of summing rounds.
                marks[horizon]["pending"] = stats["pending"]
                if stats["marked"] or stats["gave_up"]:
                    progressed = True
            if not progressed:
                break

        if mark_batches >= max_batches and verbose:
            print(f"[OUTCOMES BACKFILL] Marking stopped at the {max_batches}-batch cap — "
                  f"run again to continue.", flush=True)

        if verbose:
            for horizon, stats in marks.items():
                print(f"[OUTCOMES BACKFILL] {horizon}d — marked {stats['marked']}, "
                      f"pending {stats['pending']}, gave up {stats['gave_up']}", flush=True)

        counts = count_signal_outcomes()
        if verbose:
            print(f"[OUTCOMES BACKFILL] Done — {counts['priced']} priced, "
                  f"{counts['unpriced']} unpriceable, {counts['total']} total", flush=True)

        if run_id:
            complete_backfill_run(
                run_id,
                fetched=totals["priced"] + totals["unpriced"],
                filtered=totals["priced"],
                new=sum(m["marked"] for m in marks.values()),
                skipped=totals["skipped"],
            )

        return {"baselines": totals, "marks": marks, "counts": counts}

    except Exception as e:
        print(f"[OUTCOMES BACKFILL] Failed: {e}", flush=True)
        if run_id:
            try:
                complete_backfill_run(run_id, status="failed")
            except Exception:
                pass
        raise
