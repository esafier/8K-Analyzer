"""Re-rank stored filings under the current detectors — no model calls.

Detection is deterministic and runs on data the database already holds: the
extracted facts (inside structured_summary) and the context the filing was
analyzed against (context_json). So when a detector changes, every filing
can be re-ranked for free, instead of paying to re-extract and re-judge.

The judge's stored opinion is reused where the filing is still a judge
candidate. Where the fixed detectors no longer flag it, the old judgment is
dropped: it was formed in response to signals that no longer exist, and
keeping its score would leave the filing ranked for a reason the page can no
longer show.

    python rescore.py --dry-run     # what would change
    python rescore.py               # apply it

--judge also sends every judge candidate that no strong model has read yet
to the judge — history scored detector-only to save money. It costs one judge
call per candidate and nothing else: the judge reads the stored facts and
context, not the filing text, so no extraction is repeated.

    python rescore.py --judge --since 2026-06-01 --dry-run   # count, no spend
    python rescore.py --judge --since 2026-06-01 --workers 8
"""

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import signals as signal_engine
from config import PIPELINE_VERSION
from database import get_connection, _dict_rows, update_filing_fields
from pipeline import _rank, _is_urgent


def _loads(value, default):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return default
    return parsed if parsed is not None else default


def _stored_inputs(row):
    """(facts, context) from the row, or None without stored facts."""
    structured = _loads(row.get("structured_summary"), None)
    if not isinstance(structured, dict):
        return None
    # structured_summary carries every array the detectors read.
    facts = {key: structured.get(key) for key in
             ("departures", "appointments", "comp_events", "insider_transactions",
              "other", "filing_flags")}
    return facts, _loads(row.get("context_json"), {}) or {}


def rescore_row(row, judgment=None):
    """Return the ranking fields a row should have now, or None if it can't
    be re-scored (no stored facts). `judgment` replaces the stored one."""
    inputs = _stored_inputs(row)
    if inputs is None:
        return None
    facts, context = inputs
    if judgment is None:
        judgment = _loads(row.get("judge_json"), None)

    detection = signal_engine.detect(facts, context)
    if judgment and not signal_engine.is_judge_candidate(detection):
        judgment = None

    verdict, score, direction, top_signal = _rank(detection, judgment)
    return {
        "triage_verdict": verdict,
        "signal_score": score,
        "signal_direction": direction,
        "top_signal": top_signal or row.get("top_signal"),
        "signals_json": detection.to_json(),
        "signal_types": ",".join(detection.types) if detection.types else None,
        "urgent": 1 if _is_urgent(detection) else 0,
        "judge_json": json.dumps(judgment) if judgment else None,
        "pipeline_version": PIPELINE_VERSION,
    }


def needs_judge(row):
    """A judge candidate under today's detectors that nobody has judged."""
    if row.get("judge_json"):
        return False
    inputs = _stored_inputs(row)
    if inputs is None:
        return False
    return signal_engine.is_judge_candidate(signal_engine.detect(*inputs))


def load_rows(only_version=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, company, ticker, cik, filed_date, item_codes, "
        "triage_verdict, signal_score, top_signal, "
        "structured_summary, context_json, judge_json, pipeline_version "
        "FROM filings WHERE pipeline_version IS NOT NULL AND structured_summary IS NOT NULL"
    )
    rows = [dict(r) for r in _dict_rows(cursor.fetchall(), cursor)]
    conn.close()
    return rows


def run(dry_run=False, verbose=True, judge=False, since=None, until=None,
        limit=0, workers=1):
    rows = load_rows()
    before, after, changed = {}, {}, 0

    for row in rows:
        new = rescore_row(row)
        if new is None:
            continue
        before[row["triage_verdict"]] = before.get(row["triage_verdict"], 0) + 1
        after[new["triage_verdict"]] = after.get(new["triage_verdict"], 0) + 1

        if (new["triage_verdict"], new["signal_score"]) != (row["triage_verdict"], row["signal_score"]):
            changed += 1
            if verbose:
                print(f"  {row['triage_verdict']:9s} {row['signal_score']} -> "
                      f"{new['triage_verdict']:9s} {new['signal_score']}  "
                      f"{(row['company'] or '')[:40]}", flush=True)
        if not dry_run:
            update_filing_fields(row["id"], **new)

    print(f"\nRe-scored {len(rows)} filing(s); {changed} changed rank.")
    print(f"  before: {before}")
    print(f"  after:  {after}")
    if dry_run and not judge:
        print("Dry run — nothing written.")
    stats = {"rows": len(rows), "changed": changed, "before": before, "after": after}
    if judge:
        stats["judge"] = judge_unjudged(rows, dry_run=dry_run, since=since, until=until,
                                        limit=limit, workers=workers)
    return stats


def judge_unjudged(rows, dry_run=False, since=None, until=None, limit=0, workers=1):
    """Judge the candidates that were scored detector-only.

    Same judge and ranking as a live filing (pipeline._judge, pipeline._rank)
    on the facts and context stored when it was scored — so a filing judged
    here ranks exactly as it would have if the judge had run at the time.
    """
    from llm import OutOfCredits
    from pipeline import _judge

    todo = [r for r in rows
            if (not since or str(r.get("filed_date") or "") >= since)
            and (not until or str(r.get("filed_date") or "") <= until)
            and needs_judge(r)]
    todo.sort(key=lambda r: str(r.get("filed_date") or ""), reverse=True)
    if limit:
        todo = todo[:limit]
    print(f"\n{len(todo)} unjudged candidate(s)"
          f"{f' since {since}' if since else ''}{f' through {until}' if until else ''}",
          flush=True)
    if dry_run:
        for row in todo[:25]:
            print(f"  WOULD JUDGE {row.get('filed_date')} {row.get('company')}", flush=True)
        print("Dry run — nothing written.")
        return {"candidates": len(todo), "judged": 0, "dry_run": True}

    counts = {"candidates": len(todo), "judged": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0}
    lock = threading.Lock()
    stop = threading.Event()

    def one(i, row):
        if stop.is_set():
            return
        facts, context = _stored_inputs(row)
        detection = signal_engine.detect(facts, context)
        try:
            judgment = _judge(row, facts, context, detection, None)
        except OutOfCredits as e:
            # Every later call fails the same way; stop with what's done.
            stop.set()
            with lock:
                counts["stopped"] = str(e)
            print(f"  [{i}/{len(todo)}] STOPPED: {e}", flush=True)
            return
        if not judgment:
            with lock:
                counts["failed"] += 1
            print(f"  [{i}/{len(todo)}] {row.get('company')} — judge failed, left as is",
                  flush=True)
            return
        new = rescore_row(row, judgment=judgment)
        new["judged_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_filing_fields(row["id"], **new)
        with lock:
            counts["judged"] += 1
            counts["tokens_in"] += judgment.get("_tokens_in", 0)
            counts["tokens_out"] += judgment.get("_tokens_out", 0)
        print(f"  [{i}/{len(todo)}] {row.get('company')} — {new['triage_verdict']} "
              f"{new['signal_score']}/10", flush=True)

    if workers <= 1:
        for i, row in enumerate(todo, 1):
            one(i, row)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in [pool.submit(one, i, row) for i, row in enumerate(todo, 1)]:
                future.result()

    print(f"JUDGE {'STOPPED' if counts.get('stopped') else 'DONE'} {counts}", flush=True)
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-rank stored filings, no model calls")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--judge", action="store_true",
                        help="also judge candidates that were scored detector-only (spends)")
    parser.add_argument("--since", help="judge only filings filed on/after (YYYY-MM-DD)")
    parser.add_argument("--until", help="judge only filings filed on/before (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=0, help="judge at most N (0 = all)")
    parser.add_argument("--workers", type=int, default=1,
                        help="judge calls in flight at once (Flex is slow per call)")
    args = parser.parse_args()

    from database import initialize_database
    initialize_database()
    stats = run(dry_run=args.dry_run, verbose=not args.quiet, judge=args.judge,
                since=args.since, until=args.until, limit=args.limit, workers=args.workers)
    raise SystemExit(2 if (stats.get("judge") or {}).get("stopped") else 0)
