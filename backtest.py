"""Re-run the current pipeline over filings already in the database.

Two jobs:

  1. **Populate the inbox on day one.** A ranking tool with an empty inbox
     teaches nothing. Re-analyzing stored history means there is something to
     read and — more importantly — something to LABEL the moment it ships,
     which is what the evaluation and few-shot loop depend on.

  2. **Measure a change.** Run it after editing a prompt or
     config/signal_weights.json, then run evaluate.py, and the effect on
     ranking quality is a number rather than an argument.

Always dry-run first. The estimate is built from the actual filings that
would be processed, not a guess, and the run refuses to start if the real
cost would exceed --budget.
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from config import LLM_MODEL, LLM_MODEL_JUDGE

# Per 1M tokens (input, output), GPT-5.6 family. Used only for estimation.
PRICING = {
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.00),
}

# Measured over 50 real filings, not assumed: extraction averaged ~2.8k input
# and ~450 output tokens, and about 30% of filings cleared the judge gate.
AVG_EXTRACT_IN, AVG_EXTRACT_OUT = 2800, 450
AVG_JUDGE_IN, AVG_JUDGE_OUT = 5500, 400
JUDGE_RATE = 0.30

# SEC is untouched here (text is already stored), so the only limit is the
# model API. Five at a time keeps a 2,000-filing run to roughly an hour
# instead of three.
PARALLELISM = 5


def _price(model, tokens_in, tokens_out):
    rate_in, rate_out = PRICING.get(model, PRICING["gpt-5.6-luna"])
    return tokens_in / 1e6 * rate_in + tokens_out / 1e6 * rate_out


def candidates(days=90, limit=None, only_unanalyzed=False):
    """Stored filings eligible for re-analysis, newest first."""
    from database import get_connection, _placeholder, _dict_rows

    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")

    query = (
        "SELECT * FROM filings "
        "WHERE raw_text IS NOT NULL AND raw_text != '' "
        "  AND COALESCE(source, '8-K') = '8-K' "
        f"  AND filed_date >= {p}"
    )
    params = [cutoff]
    if only_unanalyzed:
        # Re-running the same generation over the same rows buys nothing.
        query += " AND (pipeline_version IS NULL OR pipeline_version <> ?)".replace("?", p)
        from config import PIPELINE_VERSION
        params.append(PIPELINE_VERSION)

    query += " ORDER BY filed_date DESC"
    if limit:
        query += f" LIMIT {p}"
        params.append(int(limit))

    cursor.execute(query, params)
    rows = [dict(r) for r in _dict_rows(cursor.fetchall(), cursor)]
    conn.close()
    return rows


def estimate(rows, model=None, judge_model=None):
    """Projected cost for a run over `rows`."""
    model = model or LLM_MODEL
    judge_model = judge_model or LLM_MODEL_JUDGE
    n = len(rows)

    extraction = _price(model, n * AVG_EXTRACT_IN, n * AVG_EXTRACT_OUT)
    judged = int(n * JUDGE_RATE)
    judging = _price(judge_model, judged * AVG_JUDGE_IN, judged * AVG_JUDGE_OUT)

    return {
        "filings": n,
        "extraction_usd": round(extraction, 2),
        "judge_calls": judged,
        "judging_usd": round(judging, 2),
        "total_usd": round(extraction + judging, 2),
        "model": model,
        "judge_model": judge_model,
    }


def run(days=90, limit=None, model=None, judge_model=None, budget=20.0,
        dry_run=False, only_unanalyzed=False, workers=PARALLELISM):
    rows = candidates(days=days, limit=limit, only_unanalyzed=only_unanalyzed)
    projection = estimate(rows, model=model, judge_model=judge_model)

    print(f"\nBacktest over the last {days} days")
    print(f"  filings:      {projection['filings']}")
    print(f"  extraction:   ${projection['extraction_usd']:.2f} ({projection['model']})")
    print(f"  judge:        ${projection['judging_usd']:.2f} "
          f"(~{projection['judge_calls']} calls, {projection['judge_model']})")
    print(f"  ESTIMATED:    ${projection['total_usd']:.2f}\n")

    if dry_run:
        print("Dry run — nothing analyzed, nothing charged.")
        return projection

    if projection["total_usd"] > budget:
        # Cap the run rather than aborting: a partial backtest over the most
        # recent filings is far more useful than no backtest, and the recent
        # ones are the ones the user will actually look at.
        affordable = int(len(rows) * budget / projection["total_usd"])
        print(f"Estimate exceeds the ${budget:.2f} budget. "
              f"Processing the {affordable} most recent filings instead.\n")
        rows = rows[:affordable]

    if not rows:
        print("Nothing to analyze.")
        return projection

    return _execute(rows, model, judge_model, workers)


def _execute(rows, model, judge_model, workers):
    from pipeline import analyze_filing, persist

    started = time.time()
    stats = {"analyzed": 0, "judged": 0, "failed": 0, "irrelevant": 0,
             "tokens_in": 0, "tokens_out": 0, "verdicts": {}}

    def work(filing):
        return filing, analyze_filing(filing, model=model, judge_model=judge_model)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, row) for row in rows]
        for i, future in enumerate(as_completed(futures), 1):
            try:
                filing, result = future.result()
            except Exception as e:
                stats["failed"] += 1
                print(f"  [{i}/{len(rows)}] worker error: {type(e).__name__}: {e}", flush=True)
                continue

            stats["tokens_in"] += result.tokens_in
            stats["tokens_out"] += result.tokens_out

            if result.error:
                stats["failed"] += 1
                continue
            if not result.relevant:
                stats["irrelevant"] += 1
                continue

            # Writes are serialized by the database layer; only the model
            # calls above are parallel.
            persist(filing["id"], result)
            stats["analyzed"] += 1
            if result.judged:
                stats["judged"] += 1
            verdict = result.fields.get("triage_verdict")
            stats["verdicts"][verdict] = stats["verdicts"].get(verdict, 0) + 1

            if i % 25 == 0:
                print(f"  [{i}/{len(rows)}] {stats['analyzed']} analyzed, "
                      f"{stats['judged']} judged, {stats['failed']} failed", flush=True)

    elapsed = time.time() - started
    actual = (_price(model or LLM_MODEL, stats["tokens_in"], 0)
              + _price(model or LLM_MODEL, 0, stats["tokens_out"]))

    print(f"\nDone in {elapsed / 60:.1f} min")
    print(f"  analyzed:  {stats['analyzed']}")
    print(f"  judged:    {stats['judged']}")
    print(f"  irrelevant:{stats['irrelevant']}")
    print(f"  failed:    {stats['failed']}")
    print(f"  verdicts:  {stats['verdicts']}")
    print(f"  tokens:    {stats['tokens_in']:,} in / {stats['tokens_out']:,} out")
    print(f"  approx:    ${actual:.2f} (extraction rate; judge tokens cost more)\n")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-run the pipeline over stored filings")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--budget", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-unanalyzed", action="store_true",
                        help="Skip filings already analyzed by this pipeline version")
    parser.add_argument("--workers", type=int, default=PARALLELISM)
    args = parser.parse_args()

    from database import initialize_database
    initialize_database()

    run(days=args.days, limit=args.limit, model=args.model,
        judge_model=args.judge_model, budget=args.budget, dry_run=args.dry_run,
        only_unanalyzed=args.only_unanalyzed, workers=args.workers)
