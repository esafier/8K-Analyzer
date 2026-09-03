"""The daily job — the thing that makes this a system rather than a tool.

Run by GitHub Actions each weekday morning (and by scheduler.py locally).
Ingest the window that hasn't been covered, then send the digest.

Two rules it exists to enforce:

  **Never claim success when SEC blocked us.** A run where most documents
  came back empty is not a quiet news day; it looks identical to one in the
  logs unless the job fails loudly. `ingest.IngestBlocked` makes it exit
  non-zero so the Actions run goes red.

  **Never skip a day.** The window comes from a stored watermark rather than
  "yesterday", so a Monday run covers Friday and Saturday too, and a job that
  was down for a week catches up on the next run.
"""

import argparse
import sys
import traceback

from database import initialize_database
from ingest import IngestBlocked, ingest_range, mark_ingested, pending_window


def run(date=None, days=None, model=None, judge_model=None,
        send_digest=True, dry_run=False, base_url=None):
    """Execute one daily cycle. Returns a stats dict.

    Args:
        date: analyze this single date instead of the pending window.
        days: force a window of the last N days (ignores the watermark).
        dry_run: print the digest instead of sending it.
    """
    initialize_database()

    if date:
        start = end = date
    elif days:
        from datetime import datetime, timedelta
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    else:
        start, end = pending_window()
        if not start:
            print("[DAILY] Already up to date — nothing to ingest.", flush=True)
            start = end = None

    stats = {}
    blocked = None

    if start:
        print(f"[DAILY] Ingesting {start}..{end}", flush=True)
        try:
            stats = ingest_range(start, end, model=model, judge_model=judge_model,
                                 backfill_type="scheduled")
            mark_ingested(end)
        except IngestBlocked as e:
            # The rows that did arrive are already stored, and the watermark is
            # deliberately NOT advanced — the next run re-covers this window.
            blocked = e
            print(f"[DAILY] {e}", flush=True)
        except Exception as e:
            print(f"[DAILY] Ingest failed: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            raise

    # The digest still goes out after a blocked ingest: whatever was analyzed
    # is worth seeing, and silence would be the wrong signal on a bad day.
    if send_digest:
        try:
            window = 1 if date else (int(days) if days else 3)
            stats["digest"] = __import__("digest").send(
                days=window, dry_run=dry_run, base_url=base_url)
        except Exception as e:
            print(f"[DAILY] Digest failed (ingest still succeeded): {e}", flush=True)

    if blocked:
        raise blocked

    print(f"[DAILY] Done: {stats}", flush=True)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Run the daily 8-K signal job")
    parser.add_argument("--date", help="Analyze a single date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Force a window of the last N days")
    parser.add_argument("--model", help="Override the extraction model")
    parser.add_argument("--judge-model", help="Override the judge model")
    parser.add_argument("--no-digest", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the digest, don't send")
    parser.add_argument("--base-url", help="Public URL, for label links in the digest")
    args = parser.parse_args()

    try:
        run(date=args.date, days=args.days, model=args.model,
            judge_model=args.judge_model, send_digest=not args.no_digest,
            dry_run=args.dry_run, base_url=args.base_url)
    except IngestBlocked as e:
        # Exit non-zero so the scheduled run goes red. A silent SEC block that
        # reports success is worse than no run at all — it looks like a day
        # with no news.
        print(f"[DAILY] FAILED: {e}", file=sys.stderr, flush=True)
        sys.exit(2)
    except Exception as e:
        print(f"[DAILY] FAILED: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
