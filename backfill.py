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
from llm import OutOfCredits
from database import initialize_database


def run(start, end, do_form4=True):
    initialize_database()

    stats = {}
    blocked = None
    try:
        stats = ingest.ingest_range(start, end, backfill_type="gap_backfill")
        print(f"8-K STATS: {stats}", flush=True)
    except ingest.IngestBlocked as e:
        # Keep going to the Form 4s, which need neither the market-data
        # provider nor extraction — but remember it, so the job ends red.
        blocked = e
        print(f"8-K BLOCKED: {e}", flush=True)

    stored = 0
    if do_form4:
        try:
            results = form4.scan_range(start, end)
            stored = sum(r.get("stored", 0) for r in results)
            print(f"FORM4 STORED: {stored}", flush=True)
        except OutOfCredits as e:
            blocked = blocked or e
            print(f"FORM4 STOPPED: {e}", flush=True)

    print("BACKFILL DONE" if blocked is None else f"BACKFILL INCOMPLETE: {blocked}", flush=True)
    return {"filings": stats, "form4_stored": stored, "blocked": str(blocked) if blocked else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--no-form4", action="store_true",
                        help="8-Ks only, skip the Form 4 scan")
    args = parser.parse_args()
    result = run(args.start, args.end, do_form4=not args.no_form4)
    # Non-zero so the Actions run goes red: a backfill that stopped halfway
    # must not look like one that finished.
    return 2 if result["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
