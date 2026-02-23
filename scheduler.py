# scheduler.py — Runs the daily fetch job on a schedule
# This checks for new 8-K filings once a day, filters them, and stores matches
#
# How to use:
#   python scheduler.py          — runs the scheduler (checks once daily)
#   python scheduler.py --now    — run a fetch immediately, then exit

import sys
import time
import schedule
from datetime import datetime, timedelta
from fetcher import fetch_filings, fetch_filing_text
from filter import filter_filings
from summarizer import extract_summary
from database import initialize_database, insert_filing, update_last_backfill


def daily_fetch_job():
    """Fetch yesterday's 8-K filings, filter them, and store matches.
    This is the function that runs on schedule."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Running daily fetch for {yesterday}...")

    # Step 1: Fetch filing metadata
    filings_metadata = fetch_filings(yesterday, today)

    if not filings_metadata:
        print("  No filings found")
        return

    # Step 2: Filter
    matched = filter_filings(filings_metadata, fetch_text_func=fetch_filing_text)

    # Step 3: Store results (LLM summary is already set in filter stage 3;
    # only fall back to sentence scorer if LLM didn't provide one)
    stored = 0
    for filing in matched:
        if not filing.get("summary"):
            keywords = filing.get("matched_keywords", "").split(",")
            filing["summary"] = extract_summary(filing.get("raw_text", ""), keywords)
        insert_filing(filing)
        stored += 1

    print(f"  Daily fetch complete: {stored} new filings stored")

    # Pre-fetch market caps so the dashboard has them ready
    tickers_to_fetch = list({f['ticker'] for f in matched if f.get('ticker')})
    if tickers_to_fetch:
        try:
            from market_cap import get_market_cap_map
            print(f"  [MARKET CAP] Pre-fetching for {len(tickers_to_fetch)} tickers...")
            get_market_cap_map(tickers_to_fetch)
        except Exception as e:
            print(f"  [MARKET CAP] Pre-fetch failed (not critical): {e}")

    # Record that a scheduled fetch completed (for front page display)
    update_last_backfill("scheduled")


if __name__ == "__main__":
    initialize_database()

    # If --now flag is passed, run once immediately and exit
    if "--now" in sys.argv:
        daily_fetch_job()
        sys.exit(0)

    # Schedule the job to run daily at 7:00 AM
    schedule.every().day.at("07:00").do(daily_fetch_job)

    print("8-K Filing Scheduler started")
    print("Daily fetch scheduled for 7:00 AM")
    print("Press Ctrl+C to stop\n")

    # Run once at startup too, so you don't have to wait until 7 AM
    daily_fetch_job()

    # Keep running and check the schedule every 60 seconds
    while True:
        schedule.run_pending()
        time.sleep(60)
