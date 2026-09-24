# reanalyze.py — re-run the pipeline on rows that were never scored by it.
#
# The first daily run ingested 2026-09-03 under the pre-rebuild code: those
# rows have raw_text and a summary but no structured_summary, no signals and
# no verdict, so they never appear in the ranked inbox. A gap backfill cannot
# reach them — Stage 1c dedupe skips accession numbers already stored with
# text, which is exactly what makes re-running a date range cheap.
#
# Unlike rescore.py this does cost money: there are no stored facts to
# re-detect from, so extraction has to run. It is still the cheap option,
# because the SEC fetch is already paid for — the text is in the database.
#
# Runs through pipeline.analyze_filing, the single analysis path (CLAUDE.md).
import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import database as db
from llm import OutOfCredits
from config import PIPELINE_VERSION
from database import update_filing_fields
from pipeline import analyze_filing, persist


def rows_missing_analysis(since=None, until=None, limit=0):
    """Rows with text that the current pipeline never scored.

    Keyed on pipeline_version, not structured_summary: ~2,100 rows from March
    to July carry facts from the pre-rebuild pipeline, in a schema today's
    detectors can't read, and no verdict. They are as unscored as a row with
    no facts at all.
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    p = db._placeholder()

    where = ["pipeline_version IS NULL",
             "raw_text IS NOT NULL", "raw_text != ''",
             "COALESCE(source, '8-K') = '8-K'"]
    params = []
    if since:
        where.append(f"filed_date >= {p}")
        params.append(since)
    if until:
        where.append(f"filed_date <= {p}")
        params.append(until)

    cursor.execute(
        f"SELECT * FROM filings WHERE {' AND '.join(where)} ORDER BY filed_date DESC",
        tuple(params),
    )
    # dict() before returning: on SQLite these come back as sqlite3.Row, which
    # supports row["k"] but not .get() (CLAUDE.md — this mismatch has bitten
    # the codebase four times now, including in this file).
    rows = [dict(row) for row in db._dict_rows(cursor.fetchall(), cursor)]
    conn.close()
    return rows[:limit] if limit else rows


def run(since=None, until=None, limit=0, dry_run=False, allow_judge=True,
        apply_universe=True, workers=1):
    rows = rows_missing_analysis(since, until, 0)
    print(f"{len(rows)} unscored filings"
          f"{f' since {since}' if since else ''}"
          f"{f' through {until}' if until else ''}", flush=True)
    if apply_universe and rows:
        # The same gate live ingest applies, so history is scored on the same
        # universe the inbox is. Uses today's market cap — a company that
        # crossed the floor since is judged by where it is now.
        #
        # Cached caps of any age, no refresh: history spans ~2,000 tickers and
        # a refresh would spend that many API Ninjas calls on a quota the
        # daily job depends on. A ticker never cached reads as unknown and is
        # skipped, exactly as the live gate skips it.
        from database import get_cached_market_caps
        from universe import screen_filings, summarize_skips
        tickers = sorted({(r.get("ticker") or "").strip().upper() for r in rows} - {""})
        caps = get_cached_market_caps(tickers, max_age_hours=None) if tickers else {}
        rows, skipped = screen_filings(rows, market_caps=caps)
        print(f"  universe: {len(rows)} in, skipped {summarize_skips(skipped)}", flush=True)
    if limit:
        rows = rows[:limit]

    if dry_run:
        for row in rows[:25]:
            print(f"  WOULD ANALYZE {row['filed_date']} {row['company']}", flush=True)
        if len(rows) > 25:
            print(f"  ... and {len(rows) - 25} more", flush=True)
        chars = sum(min(len(row.get("raw_text") or ""), 120_000) for row in rows)
        print(f"  {chars:,} characters of filing text to extract", flush=True)
        return {"candidates": len(rows), "scored": 0, "dry_run": True}

    counts = {"scored": 0, "irrelevant": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0}
    lock = threading.Lock()
    stop = threading.Event()
    stopped = []
    total = len(rows)

    def work(i, row):
        if stop.is_set():
            return
        company = row.get("company", "Unknown")
        try:
            _work(i, row, company)
        except OutOfCredits as e:
            # Every remaining row would fail the same way. Stop with what's
            # scored so far; re-running picks up exactly the rows left.
            stop.set()
            with lock:
                stopped.append(str(e))
            print(f"  [{i}/{total}] STOPPED: {e}", flush=True)
        except Exception as e:
            # One bad row costs one row, reported, and stays unscored for the
            # next run. The first parallel run instead failed the whole job
            # after the fact — on a Postgres type error every relevant row in
            # the window shared.
            with lock:
                counts["failed"] += 1
                counts.setdefault("errors", {})
                key = f"{type(e).__name__}: {str(e)[:120]}"
                counts["errors"][key] = counts["errors"].get(key, 0) + 1
            print(f"  [{i}/{total}] {company} — ERROR {type(e).__name__}: {e}", flush=True)

    def _work(i, row, company):
        result = analyze_filing(row, allow_judge=allow_judge)
        with lock:
            counts["tokens_in"] += result.tokens_in
            counts["tokens_out"] += result.tokens_out

        if result.error:
            with lock:
                counts["failed"] += 1
            print(f"  [{i}/{total}] {company} — FAILED: {result.error}", flush=True)
            return
        if not result.relevant:
            # Kept, not deleted — the archive at /all should still show it —
            # but stamped as seen, so a rerun or the next chunk of a history
            # backfill doesn't pay to extract it again.
            update_filing_fields(
                row["id"], pipeline_version=PIPELINE_VERSION, triage_verdict="PASS",
                signal_score=0, relevant_reason=result.fields.get("relevant_reason"),
            )
            with lock:
                counts["irrelevant"] += 1
            print(f"  [{i}/{total}] {company} — not relevant", flush=True)
            return

        persist(row["id"], result)
        with lock:
            counts["scored"] += 1
        fields = result.fields
        print(f"  [{i}/{total}] {company} — {fields.get('triage_verdict')} "
              f"{fields.get('signal_score')}/10 {fields.get('top_signal') or ''}",
              flush=True)

    if workers <= 1:
        for i, row in enumerate(rows, 1):
            work(i, row)
            if stop.is_set():
                break
    else:
        # Model calls dominate (Flex answers slowly), so a few in flight at
        # once is most of the speedup. SEC requests stay paced: fetcher's
        # gate is process-wide and thread-safe.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in [pool.submit(work, i, row) for i, row in enumerate(rows, 1)]:
                future.result()

    stats = {"candidates": total, **counts}
    if stopped:
        stats["stopped"] = stopped[0]
        print(f"REANALYZE STOPPED {stats}", flush=True)
    else:
        print(f"REANALYZE DONE {stats}", flush=True)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="earliest filed_date (YYYY-MM-DD)")
    parser.add_argument("--until", help="latest filed_date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap the number of filings (0 = no cap)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be analyzed, spend nothing")
    parser.add_argument("--workers", type=int, default=1,
                        help="filings in flight at once (Flex is slow per call; 6-8 is sensible)")
    parser.add_argument("--all", action="store_true",
                        help="skip the market-cap universe gate")
    parser.add_argument("--no-judge", action="store_true",
                        help="detectors only — no judge calls. For history: about a "
                             "third of the cost, and it grades the detectors on their own")
    args = parser.parse_args()
    stats = run(since=args.since, until=args.until, limit=args.limit, dry_run=args.dry_run,
                allow_judge=not args.no_judge, apply_universe=not args.all,
                workers=args.workers)
    return 2 if stats.get("stopped") else 0


if __name__ == "__main__":
    sys.exit(main())
