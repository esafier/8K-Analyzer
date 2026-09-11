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
"""

import argparse
import json

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


def rescore_row(row):
    """Return the ranking fields a row should have now, or None if it can't
    be re-scored (no stored facts)."""
    structured = _loads(row.get("structured_summary"), None)
    if not isinstance(structured, dict):
        return None

    # structured_summary carries every array the detectors read.
    facts = {key: structured.get(key) for key in
             ("departures", "appointments", "comp_events", "insider_transactions",
              "other", "filing_flags")}
    context = _loads(row.get("context_json"), {}) or {}
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


def load_rows(only_version=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, company, triage_verdict, signal_score, top_signal, "
        "structured_summary, context_json, judge_json, pipeline_version "
        "FROM filings WHERE pipeline_version IS NOT NULL AND structured_summary IS NOT NULL"
    )
    rows = [dict(r) for r in _dict_rows(cursor.fetchall(), cursor)]
    conn.close()
    return rows


def run(dry_run=False, verbose=True):
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
    if dry_run:
        print("Dry run — nothing written.")
    return {"rows": len(rows), "changed": changed, "before": before, "after": after}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-rank stored filings, no model calls")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    from database import initialize_database
    initialize_database()
    run(dry_run=args.dry_run, verbose=not args.quiet)
