"""Fetching and storing a date range of 8-K filings.

Lives outside app.py so the scheduled job can import it without importing
Flask. app.py runs initialize_database() at import time and registers routes;
a cron process that pulled all of that in would be booting a web app in order
to fetch some filings.

The functions here are the same ones the /backfill button drives, so the
manual and automated paths cannot diverge.
"""

from datetime import datetime, timedelta

from database import (
    insert_filing, update_last_backfill, create_backfill_run, complete_backfill_run,
    set_app_status, get_app_status,
)
from fetcher import fetch_filings, fetch_filing_text
from filter import filter_filings

# Key under which the last successfully ingested date is recorded.
WATERMARK_KEY = "ingested_through"

# If more than this share of in-scope filings come back with no text, the run
# is not a quiet news day — SEC is blocking us, and saying "done" would be a
# lie the logs would carry forever.
FETCH_FAILURE_THRESHOLD = 0.20


class IngestBlocked(RuntimeError):
    """Raised when SEC refused enough of the run to make it meaningless."""


def ingest_range(start_date, end_date, model=None, judge_model=None,
                 backfill_type="scheduled", enrich=True):
    """Fetch, screen, analyze, and store every 8-K in a date range.

    Returns a stats dict. Raises IngestBlocked when SEC withheld so much of
    the range that reporting success would be misleading.
    """
    stats = {"fetched": 0, "analyzed": 0, "new": 0, "skipped": 0, "no_text": 0}
    run_id = create_backfill_run(backfill_type, start_date, end_date, model or "default")

    try:
        metadata = fetch_filings(start_date, end_date)
    except Exception as e:
        print(f"[INGEST] EDGAR search failed: {e}", flush=True)
        complete_backfill_run(run_id, status="failed")
        raise

    stats["fetched"] = len(metadata)
    if not metadata:
        print(f"[INGEST] No filings found for {start_date}..{end_date}", flush=True)
        complete_backfill_run(run_id, fetched=0, filtered=0, new=0, skipped=0)
        return stats

    matched = filter_filings(metadata, fetch_text_func=fetch_filing_text,
                             model=model, judge_model=judge_model)
    stats["analyzed"] = len(matched)

    for filing in matched:
        if not filing.get("raw_text"):
            stats["no_text"] += 1
        if insert_filing(filing):
            stats["new"] += 1
        else:
            stats["skipped"] += 1

    complete_backfill_run(run_id, fetched=stats["fetched"], filtered=stats["analyzed"],
                          new=stats["new"], skipped=stats["skipped"])

    if enrich:
        _enrich(matched)

    update_last_backfill(backfill_type)
    print(f"[INGEST] {start_date}..{end_date}: {stats}", flush=True)

    # Checked last so the rows we did get are stored and counted before the
    # job is failed. A blocked run should leave partial data, not nothing.
    if stats["analyzed"] and stats["no_text"] / stats["analyzed"] > FETCH_FAILURE_THRESHOLD:
        raise IngestBlocked(
            f"{stats['no_text']} of {stats['analyzed']} filings came back with no text "
            f"— SEC is almost certainly blocking this IP. Rows are parked for retry."
        )

    return stats


def _enrich(matched):
    """Post-ingest enrichment that isn't worth failing the run over."""
    try:
        from departures import enrich_new_filings
        enrich_new_filings(matched)
    except Exception as e:
        print(f"[INGEST] Departure enrichment failed (not critical): {e}", flush=True)

    tickers = list({f["ticker"] for f in matched if f.get("ticker")})
    if not tickers:
        return
    for label, fn in (
        ("MARKET CAP", "market_cap.refresh_market_caps_sync"),
        ("EARNINGS", "earnings.refresh_earnings_sync"),
        ("STOCK PRICE", "stock_price.refresh_stock_prices_sync"),
    ):
        try:
            module_name, func_name = fn.rsplit(".", 1)
            module = __import__(module_name, fromlist=[func_name])
            getattr(module, func_name)(tickers)
        except Exception as e:
            print(f"[{label}] Pre-fetch failed (not critical): {e}", flush=True)


# ---------------------------------------------------------------------------
# Watermark — what "yesterday" actually means
# ---------------------------------------------------------------------------

def pending_window(today=None, max_days=10):
    """The date range that still needs ingesting.

    A naive "yesterday to today" window run on weekdays has two faults, and
    the daily job would hit both every week:

      - Friday's filings are never ingested. Monday's run asks for
        Sunday..Monday, and Friday falls in the gap.
      - Consecutive windows overlap, so every filing is fetched and analyzed
        twice before the duplicate is discarded at insert.

    Anchoring on the last successfully ingested date fixes both: the window
    is exactly what has not been covered, however long the job was down.
    Capped at `max_days` so a month-long outage doesn't produce one enormous
    catch-up run.
    """
    today = today or datetime.now().date()
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d").date()

    last = get_app_status(WATERMARK_KEY)
    if last:
        try:
            start = datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1)
        except ValueError:
            start = today - timedelta(days=1)
    else:
        # First run: yesterday and today, rather than silently backfilling
        # history the user didn't ask for.
        start = today - timedelta(days=1)

    if start > today:
        return None, None  # already current

    if (today - start).days > max_days:
        start = today - timedelta(days=max_days)

    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def mark_ingested(through_date):
    set_app_status(WATERMARK_KEY, through_date)
