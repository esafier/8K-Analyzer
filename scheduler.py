# scheduler.py — local daily runner.
#
# In production the daily job runs on GitHub Actions (.github/workflows/daily.yml),
# which is free for this public repo and survives Render's free tier spinning
# the web service down. This file is the same job for a machine you control:
#
#   python scheduler.py          keep running, fire at 07:00 each day
#   python scheduler.py --now    run once and exit
#
# It delegates to daily.run() so there is exactly one definition of what the
# daily job does. The previous version had its own copy of the fetch/filter/
# store loop, which is how it quietly diverged from the web backfill path.

import sys
import time

import schedule

from daily import run as run_daily
from database import initialize_database


def daily_fetch_job():
    """Ingest whatever window hasn't been covered, then send the digest."""
    try:
        run_daily()
    except Exception as e:
        # Keep the scheduler alive: one bad morning (SEC block, API outage)
        # must not stop tomorrow's run.
        print(f"[SCHEDULER] Daily job failed: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    initialize_database()

    if "--now" in sys.argv:
        daily_fetch_job()
        sys.exit(0)

    schedule.every().day.at("07:00").do(daily_fetch_job)

    print("8-K signal scheduler started")
    print("Daily run scheduled for 7:00 AM")
    print("Press Ctrl+C to stop\n")

    daily_fetch_job()  # run once at startup so you don't wait until 7am

    while True:
        schedule.run_pending()
        time.sleep(60)
