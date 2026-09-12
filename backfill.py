# backfill.py — fill a hole in the archive: 8-Ks and Form 4s for a date range.
#
# `ingest_range` deliberately does not touch the daily watermark, so this can
# never disturb the scheduled job: a backfill of old days and tomorrow's
# ingest are independent.
#
# Run this on GitHub Actions (.github/workflows/backfill.yml), not on a
# laptop. A range of two weeks is hours of SEC fetches and model calls; a
# laptop that sleeps mid-run leaves the job frozen on a dead socket, which is
# exactly how the 2026-08-20 backfill was lost twice.
import argparse
import sys

import form4
import ingest
from database import initialize_database


def run(start, end, do_form4=True):
    initialize_database()

    stats = {}
    try:
        stats = ingest.ingest_range(start, end, backfill_type="gap_backfill")
        print(f"8-K STATS: {stats}", flush=True)
    except ingest.IngestBlocked as e:
        # Not a crash: the universe gate refused to call the window covered
        # because the market-data provider looked dead. Say so and keep going
        # to the Form 4s, which do not depend on it.
        print(f"8-K BLOCKED: {e}", flush=True)

    stored = 0
    if do_form4:
        results = form4.scan_range(start, end)
        stored = sum(r.get("stored", 0) for r in results)
        print(f"FORM4 STORED: {stored}", flush=True)

    print("BACKFILL DONE", flush=True)
    return {"filings": stats, "form4_stored": stored}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--no-form4", action="store_true",
                        help="8-Ks only, skip the Form 4 scan")
    args = parser.parse_args()
    run(args.start, args.end, do_form4=not args.no_form4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
