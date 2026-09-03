"""Evaluation — the thing that makes "better" a measurable claim.

Before this, no prompt or weight change could be shown to be an improvement.
`test_prompt.py` compared the model against the KEYWORD labels, which only
measured how well an expensive model imitated a hand-written keyword list —
not whether the ranking was any good.

Here the ground truth is the user's own labels, so the questions are the ones
that actually matter:

  precision@K        of the K filings ranked highest, how many were real?
  per-signal-type    which detectors earn their place, and which cry wolf?
  worst misses       what did the system rank high that the user called noise
                     (and vice versa) — the list to read before tuning anything

Run it before and after any change to prompts or config/signal_weights.json.
"""

import argparse
import json

from database import get_connection, _placeholder, _dict_rows

# Labels split into "was this worth my attention" — `meh` counts as neither,
# since a borderline call is not evidence of a mistake in either direction.
POSITIVE = {"signal"}
NEGATIVE = {"noise"}


def load_labeled():
    """Every labelled filing with its score, signals, and verdict."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT f.id, f.company, f.ticker, f.filed_date, f.signal_score,
               f.signal_direction, f.triage_verdict, f.signal_types,
               f.top_signal, f.pipeline_version,
               j.label, j.note
        FROM judgments j JOIN filings f ON f.id = j.filing_id
        WHERE f.signal_score IS NOT NULL
        ORDER BY f.signal_score DESC, f.filed_date DESC
    """)
    rows = [dict(r) for r in _dict_rows(cursor.fetchall(), cursor)]
    conn.close()
    return rows


def precision_at_k(rows, k):
    """Of the top K by score, what share did the user call signal?

    The number that matters most: it is literally "if I read the top ten
    things this tool showed me, how many were worth reading".
    """
    scored = [r for r in rows if r["label"] in POSITIVE | NEGATIVE][:k]
    if not scored:
        return None, 0
    hits = sum(1 for r in scored if r["label"] in POSITIVE)
    return hits / len(scored), len(scored)


def per_signal_precision(rows):
    """Precision for each signal type.

    A detector that fires often and is usually called noise is worse than
    useless — it is what trains someone to stop trusting the whole feed. This
    is how such a detector gets found and demoted.
    """
    stats = {}
    for row in rows:
        if row["label"] not in POSITIVE | NEGATIVE:
            continue
        for signal_type in (row.get("signal_types") or "").split(","):
            signal_type = signal_type.strip()
            if not signal_type:
                continue
            entry = stats.setdefault(signal_type, {"signal": 0, "noise": 0})
            entry["signal" if row["label"] in POSITIVE else "noise"] += 1

    out = {}
    for signal_type, counts in stats.items():
        total = counts["signal"] + counts["noise"]
        out[signal_type] = {
            "n": total,
            "precision": counts["signal"] / total if total else None,
            **counts,
        }
    return dict(sorted(out.items(), key=lambda kv: (-(kv[1]["precision"] or 0), -kv[1]["n"])))


def score_calibration(rows):
    """Does a higher score actually mean a higher hit rate?

    If the 8-10 band isn't cleaner than the 4-6 band, the score is decorative
    and sorting by it is doing nothing.
    """
    bands = {"0-3": [], "4-6": [], "7-8": [], "9-10": []}
    for row in rows:
        if row["label"] not in POSITIVE | NEGATIVE:
            continue
        score = row["signal_score"] or 0
        band = "0-3" if score <= 3 else "4-6" if score <= 6 else "7-8" if score <= 8 else "9-10"
        bands[band].append(1 if row["label"] in POSITIVE else 0)

    return {band: {"n": len(v), "precision": (sum(v) / len(v)) if v else None}
            for band, v in bands.items()}


def worst_misses(rows, limit=10):
    """High-scored filings the user called noise, and low-scored ones they
    called signal. The reading list before touching any weight."""
    false_positives = [r for r in rows
                       if r["label"] in NEGATIVE and (r["signal_score"] or 0) >= 6]
    false_negatives = [r for r in rows
                       if r["label"] in POSITIVE and (r["signal_score"] or 0) <= 4]
    false_positives.sort(key=lambda r: -(r["signal_score"] or 0))
    false_negatives.sort(key=lambda r: (r["signal_score"] or 0))
    return false_positives[:limit], false_negatives[:limit]


def evaluate():
    rows = load_labeled()
    return {
        "labeled_total": len(rows),
        "precision_at": {k: precision_at_k(rows, k) for k in (10, 25, 50)},
        "per_signal": per_signal_precision(rows),
        "calibration": score_calibration(rows),
        "misses": worst_misses(rows),
        "rows": rows,
    }


def report(as_json=False):
    result = evaluate()

    if as_json:
        printable = {k: v for k, v in result.items() if k not in ("rows", "misses")}
        printable["misses"] = {
            "false_positives": [_slim(r) for r in result["misses"][0]],
            "false_negatives": [_slim(r) for r in result["misses"][1]],
        }
        print(json.dumps(printable, indent=2, default=str))
        return result

    total = result["labeled_total"]
    print(f"\n{'=' * 66}\nSIGNAL EVALUATION — {total} labelled filings\n{'=' * 66}")

    if total < 30:
        print("\nToo few labels for the numbers below to mean much.")
        print(f"Label about {max(0, 100 - total)} more in /review and re-run.\n")

    print("\nPrecision@K  (of the top K by score, share the user called signal)")
    for k, (precision, n) in result["precision_at"].items():
        if precision is None:
            print(f"  @{k:<3} —      (no labelled rows yet)")
        else:
            print(f"  @{k:<3} {precision:5.0%}  (n={n})")

    print("\nScore calibration  (a higher band should be a cleaner band)")
    for band, stats in result["calibration"].items():
        if stats["n"]:
            print(f"  {band:<6} {stats['precision']:5.0%}  (n={stats['n']})")
        else:
            print(f"  {band:<6}    —   (n=0)")

    print("\nPer-signal precision  (which detectors earn their place)")
    for signal_type, stats in result["per_signal"].items():
        print(f"  {signal_type:<28} {stats['precision']:5.0%}  "
              f"({stats['signal']} signal / {stats['noise']} noise)")

    false_positives, false_negatives = result["misses"]
    if false_positives:
        print("\nRanked high, called noise  (over-firing detectors live here)")
        for row in false_positives:
            print(f"  [{row['signal_score']}] {row['company'][:28]:<28} {row['signal_types'] or ''}")
            if row.get("note"):
                print(f"        note: {row['note']}")
    if false_negatives:
        print("\nRanked low, called signal  (missed patterns live here)")
        for row in false_negatives:
            print(f"  [{row['signal_score']}] {row['company'][:28]:<28} {row['signal_types'] or ''}")
            if row.get("note"):
                print(f"        note: {row['note']}")

    print()
    return result


def _slim(row):
    return {k: row.get(k) for k in
            ("id", "company", "ticker", "signal_score", "signal_types", "label", "note")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate ranking against the user's labels")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()
    report(as_json=args.json)
