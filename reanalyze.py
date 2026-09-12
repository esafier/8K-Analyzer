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

import database as db
from pipeline import analyze_filing, persist


def rows_missing_analysis(since=None, until=None, limit=0):
    """Rows with text but no structured_summary — never seen by the pipeline."""
    conn = db.get_connection()
    cursor = conn.cursor()
    p = db._placeholder()

    where = ["structured_summary IS NULL",
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


def run(since=None, until=None, limit=0, dry_run=False):
    rows = rows_missing_analysis(since, until, limit)
    print(f"{len(rows)} unscored filings"
          f"{f' since {since}' if since else ''}"
          f"{f' through {until}' if until else ''}", flush=True)

    if dry_run:
        for row in rows:
            print(f"  WOULD ANALYZE {row['filed_date']} {row['company']}", flush=True)
        return {"candidates": len(rows), "scored": 0, "dry_run": True}

    scored = irrelevant = failed = 0
    tokens_in = tokens_out = 0

    for i, row in enumerate(rows, 1):
        company = row.get("company", "Unknown")
        result = analyze_filing(row)
        tokens_in += result.tokens_in
        tokens_out += result.tokens_out

        if result.error:
            failed += 1
            print(f"  [{i}/{len(rows)}] {company} — FAILED: {result.error}", flush=True)
            continue
        if not result.relevant:
            # Left as-is rather than deleted: the row predates the rebuild, and
            # a stored filing the pipeline considers irrelevant is still a
            # filing the archive at /all should keep showing.
            irrelevant += 1
            print(f"  [{i}/{len(rows)}] {company} — not relevant", flush=True)
            continue

        persist(row["id"], result)
        scored += 1
        fields = result.fields
        print(f"  [{i}/{len(rows)}] {company} — {fields.get('triage_verdict')} "
              f"{fields.get('signal_score')}/10 {fields.get('top_signal') or ''}",
              flush=True)

    stats = {"candidates": len(rows), "scored": scored, "irrelevant": irrelevant,
             "failed": failed, "tokens_in": tokens_in, "tokens_out": tokens_out}
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
    args = parser.parse_args()
    run(since=args.since, until=args.until, limit=args.limit, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
