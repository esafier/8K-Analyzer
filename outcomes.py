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

import threading
from datetime import datetime, timedelta

from database import (
    OUTCOME_DELISTED,
    OUTCOME_HORIZONS,
    OUTCOME_NO_PRICE,
    OUTCOME_OK,
    get_cached_closes,
    get_filings_needing_outcome_baseline,
    get_outcomes_needing_mark,
    give_up_on_horizon,
    get_price_history_meta,
    set_outcome_mark,
    set_outcome_status,
    upsert_signal_outcome,
)
from price_history import STATUS_NOT_FOUND, get_close_on_or_after, has_coverage

# Guards against two outcome runs interleaving — a double-clicked backfill
# button, or a scheduled job landing on top of a manual one. Process-local, so
# it is belt-and-braces only: the actual safety property is that
# upsert_price_history_meta refuses to merge disjoint spans, which holds across
# processes too.
_run_lock = threading.Lock()


# The benchmark every signal is measured against.
BENCHMARK_TICKER = "SPY"

# How long a horizon can sit unpriceable before we stop retrying it. A live
# ticker always prints a close within days; a month of silence means the name
# stopped trading, which is a real outcome rather than a pending one.
GIVE_UP_AFTER_DAYS = 30

# If this many filings fail to price in one run, the price source is down rather
# than the data being patchy. Stop instead of walking the whole archive marking
# nothing, and keep the exclusion list from growing past SQL parameter limits.
MAX_SKIPPED_BEFORE_ABORT = 500

# A corporate action re-bases a whole price series retroactively: after a
# 10-for-1 split, every historical bar comes back divided by ten. Anything
# below this much disagreement between a stored baseline and the same date
# re-read today is noise; anything above it is a re-basing. Real splits move
# prices by 2x or more, so the threshold is not close to either case.
REBASE_TOLERANCE = 0.005

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


def _benchmark_close_on(bar_date):
    """The benchmark close for exactly `bar_date`, or None.

    get_close_on_or_after can roll forward up to ten days, and silently
    accepting that would leave the stock and the benchmark measured over
    different windows — which is precisely the same-window guarantee the whole
    excess-return number rests on. If SPY has no close on the stock's own bar,
    the honest answer is "not yet", not "close enough".
    """
    spy_date, spy_close = get_close_on_or_after(BENCHMARK_TICKER, bar_date)
    if spy_close is None or spy_date != bar_date:
        return None
    return spy_close


def _rebase_factor(ticker, baseline_date, stored_baseline):
    """How much a stored baseline disagrees with the same date re-read today.

    Yahoo applies split adjustments RETROACTIVELY at fetch time. A baseline
    captured before a 10-for-1 split is stored at, say, 1150; after the split
    the same historical bar comes back as 115. Comparing a horizon close
    fetched today against that stored 1150 reads as a ~90% collapse that never
    happened — and since a mark is never revisited, it would sit in the
    scorecard forever. Reverse splits, which are routine among the distressed
    micro-caps this scanner surfaces, distort it the other way and even harder.

    Returns a multiplier to bring a freshly-fetched price onto the stored
    baseline's basis. Rescaling the INCOMING value, rather than rewriting the
    baseline, is what keeps marks already recorded at shorter horizons valid —
    they were computed against that same stored basis.

    Returns 1.0 when nothing has changed or when the check cannot be made;
    a return is basis-invariant as long as both legs share one basis, so the
    safe default is to leave the numbers alone.
    """
    if not stored_baseline or not baseline_date:
        return 1.0

    bar_date = str(baseline_date)[:10]
    cached = get_cached_closes(ticker, bar_date, bar_date)
    current = cached.get(bar_date)
    if not current or current <= 0:
        return 1.0

    factor = float(stored_baseline) / float(current)
    if abs(factor - 1.0) < REBASE_TOLERANCE:
        return 1.0

    print(f"[OUTCOMES] {ticker}: price series re-based since baseline "
          f"({stored_baseline:.4f} -> {current:.4f} on {bar_date}); "
          f"scaling this horizon by {factor:.4f} to keep the legs comparable")
    return factor


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


def capture_baselines(limit=200, verdicts=("DEEP_LOOK", "MONITOR"), exclude_ids=None):
    """Stamp filings that have a verdict but no outcome row with their baseline.

    Safe to call repeatedly and safe to interrupt — each filing is committed as
    it is priced, so a run that dies halfway keeps everything it earned.

    `exclude_ids` lets a caller skip filings it already attempted this run.
    A skipped filing gets no outcome row, so it would otherwise reappear at the
    front of the next batch indefinitely.

    Returns {'considered': n, 'priced': n, 'unpriced': n, 'skipped': n,
             'skipped_ids': [...]}.
    """
    pending = get_filings_needing_outcome_baseline(
        limit=limit, verdicts=verdicts, exclude_ids=exclude_ids
    )
    stats = {"considered": len(pending), "priced": 0, "unpriced": 0,
             "skipped": 0, "skipped_ids": []}

    for filing in pending:
        ticker = (filing.get("ticker") or "").upper()
        filed_date = filing.get("filed_date")
        if not ticker or not filed_date:
            stats["skipped"] += 1
            stats["skipped_ids"].append(filing.get("id"))
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
                    stats["skipped_ids"].append(filing.get("id"))
                continue

            # Benchmark on the stock's own bar date — same window, or the
            # excess return is measuring the calendar instead of the signal.
            spy_close = _benchmark_close_on(bar_date)
            if spy_close is None:
                # A missing benchmark is a problem with us, not with the filing.
                # Leave the row uncreated so the next run retries it.
                print(f"[OUTCOMES] No {BENCHMARK_TICKER} close near {bar_date} — retrying later")
                stats["skipped"] += 1
                stats["skipped_ids"].append(filing.get("id"))
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
            stats["skipped_ids"].append(filing.get("id"))

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
                        # Went dark between the filing and this horizon. No
                        # later horizon will ever price either, so the row is
                        # flagged delisted for reporting — but this horizon is
                        # settled individually, and marks already earned at
                        # shorter horizons stay exactly as they are.
                        give_up_on_horizon(row["filing_id"], horizon)
                        set_outcome_status(row["filing_id"], OUTCOME_DELISTED)
                        stats["gave_up"] += 1
                    elif days_overdue > GIVE_UP_AFTER_DAYS and _answer_is_final(ticker, target):
                        # The source answered for this window and there are no
                        # bars in it. Settle THIS horizon only: the name may be
                        # halted rather than dead, and if it resumes trading the
                        # later horizons must still be free to price.
                        give_up_on_horizon(row["filing_id"], horizon)
                        stats["gave_up"] += 1
                    else:
                        # Recent enough that the bar may not exist yet, or we
                        # never reached the source. Either way, retry later.
                        stats["pending"] += 1
                    continue

                spy_close = _benchmark_close_on(bar_date)
                if spy_close is None:
                    stats["pending"] += 1
                    continue

                # A split between the baseline and now re-bases the whole
                # series. Put the freshly-fetched closes back onto the stored
                # baseline's basis before recording them, or the excess return
                # measures the corporate action instead of the signal.
                close *= _rebase_factor(ticker, row.get("baseline_date"),
                                        row.get("baseline_close"))
                spy_close *= _rebase_factor(BENCHMARK_TICKER, row.get("baseline_date"),
                                            row.get("baseline_spy"))

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

    Returns None without doing anything if another outcome run holds the lock.
    """
    if not _run_lock.acquire(blocking=False):
        print("[OUTCOMES] Another outcome run is already in progress — skipping this one.")
        return None
    try:
        return _run_outcome_job(as_of, baseline_limit, mark_limit)
    finally:
        _run_lock.release()


def _run_outcome_job(as_of, baseline_limit, mark_limit):
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

    if not _run_lock.acquire(blocking=False):
        print("[OUTCOMES BACKFILL] Another outcome run is already in progress — "
              "not starting a second one.", flush=True)
        if run_id:
            try:
                complete_backfill_run(run_id, status="failed")
            except Exception:
                pass
        return None

    totals = {"priced": 0, "unpriced": 0, "skipped": 0}
    # Filings attempted but not recorded this run. Carried between batches so a
    # run of transient failures at the front of the queue cannot hide every
    # older, priceable filing behind it.
    attempted_but_unrecorded = []
    batches = 0

    try:
        while batches < max_batches:
            if len(attempted_but_unrecorded) >= MAX_SKIPPED_BEFORE_ABORT:
                print(f"[OUTCOMES BACKFILL] {len(attempted_but_unrecorded)} filings could "
                      f"not be priced — that is a systemic failure, not bad luck. "
                      f"Stopping; run again once the price source is healthy.", flush=True)
                break

            stats = capture_baselines(
                limit=batch_size, exclude_ids=attempted_but_unrecorded
            )
            batches += 1
            for key in totals:
                totals[key] += stats[key]
            attempted_but_unrecorded.extend(
                i for i in stats["skipped_ids"] if i is not None
            )

            if verbose:
                print(f"[OUTCOMES BACKFILL] Batch {batches}: "
                      f"priced {stats['priced']}, unpriced {stats['unpriced']}, "
                      f"skipped {stats['skipped']}", flush=True)

            # The queue is genuinely empty — every remaining candidate has been
            # recorded or already attempted this run.
            if stats["considered"] == 0:
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

    finally:
        _run_lock.release()
