"""Model bake-off: can a cheaper model do this job without losing signals?

Runs candidate models over the same stored 8-Ks, with each filing's stored
context, and compares what the detectors fire on and what the judge decides.
Read-only: nothing is written to the database. The first model in each list
is the baseline the others are compared against.

    python bakeoff.py --n 50 --judge-n 20
    python bakeoff.py --extract-models gpt-5.6-luna,gpt-6-luna \
                      --judge-models gpt-5.6-terra,gpt-6-sol,gpt-6-luna

Agreement with the current model is not the same as being right — the report
lists every disagreement with the company name so each can be read. A model
passes when its disagreements are ones the baseline got wrong, or are noise.
Cost is measured from real token counts, not estimated.
"""

import argparse
import json
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import database as db
from config import ACTIVE_PROMPT, MAX_EXTRACTION_CHARS

# Standard-tier USD per 1M tokens (input, output), September 2026. Flex and
# Batch are half. Cached input is ignored, so these slightly overstate.
PRICES = {
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6-sol": (2.00, 10.00),
}

# Fields that decide whether a detector fires. A model that gets these the
# same way is a model the detectors can't tell apart.
KEY_FLAGS = ("role_class", "forfeiture_flag", "disagreement_disclosed", "successor_named",
             "effective_immediately", "is_retirement", "is_merger_related")


def cost(model, tokens_in, tokens_out, tier):
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    usd = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    return usd / 2 if tier == "flex" else usd


def sample(n, seed=7):
    """Scored 8-Ks with stored text and context: half that reached the judge
    (they carry signals), half that didn't (they test for false positives)."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM filings WHERE pipeline_version IS NOT NULL "
        "AND COALESCE(source, '8-K') = '8-K' AND raw_text IS NOT NULL AND raw_text <> '' "
        "AND context_json IS NOT NULL"
    )
    rows = [dict(r) for r in db._dict_rows(cursor.fetchall(), cursor)]
    conn.close()
    rng = random.Random(seed)
    judged = [r for r in rows if r.get("judge_json")]
    unjudged = [r for r in rows if not r.get("judge_json")]
    rng.shuffle(judged)
    rng.shuffle(unjudged)
    half = n // 2
    return judged[:half] + unjudged[:n - min(half, len(judged))]


def _flags(facts):
    """Per-departure flags, flattened, so two extractions can be diffed."""
    out = []
    for dep in (facts or {}).get("departures") or []:
        if isinstance(dep, dict):
            out.append(tuple(dep.get(k) for k in KEY_FLAGS))
    return sorted(out, key=str)


def extract(filings, models, workers=1):
    from llm import classify_and_summarize
    import signals

    results = {m: {} for m in models}
    lock = threading.Lock()

    def one(i, filing):
        context = json.loads(filing["context_json"]) if filing.get("context_json") else {}
        text = (filing.get("raw_text") or "")[:MAX_EXTRACTION_CHARS]
        line = []
        for model in models:
            facts = classify_and_summarize(text, prompt_file=ACTIVE_PROMPT, model=model)
            if facts is None:
                entry = {"error": True}
                line.append(f"{model}: ERROR")
            else:
                relevant = facts.get("relevant", True)
                detection = signals.detect(facts, context) if relevant else None
                entry = {
                    "relevant": bool(relevant),
                    "types": sorted(detection.types) if detection else [],
                    "verdict": signals.detector_verdict(detection) if detection else "PASS",
                    "direction": detection.direction if detection else None,
                    "flags": _flags(facts),
                    "departures": len(facts.get("departures") or []),
                    "comp_events": len(facts.get("comp_events") or []),
                    "cost": cost(model, facts.get("_tokens_in", 0), facts.get("_tokens_out", 0),
                                 facts.get("_service_tier")),
                    "tokens": (facts.get("_tokens_in", 0), facts.get("_tokens_out", 0)),
                    "tier": facts.get("_service_tier"),
                    "_facts": facts, "_detection": detection,
                }
                line.append(f"{model}: {entry['verdict']} {entry['types']}")
            with lock:
                results[model][filing["id"]] = entry
        # Printed as each filing finishes, so a run cut short still reports.
        print(f"  [x {i}/{len(filings)}] {filing.get('company')} — " + " | ".join(line), flush=True)

    _run_all(one, filings, workers)
    return results


def _run_all(fn, items, workers):
    if workers <= 1:
        for i, item in enumerate(items, 1):
            fn(i, item)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(fn, i, item) for i, item in enumerate(items, 1)]:
            future.result()


def judge_all(filings, baseline, judge_models, judge_n, workers=1):
    """Judge the same baseline facts with each judge model — the judge is
    compared on identical input, so only the model differs."""
    import signals
    from judge import judge

    candidates = [f for f in filings
                  if baseline.get(f["id"], {}).get("_detection") is not None
                  and signals.is_judge_candidate(baseline[f["id"]]["_detection"])][:judge_n]
    results = {m: {} for m in judge_models}
    lock = threading.Lock()

    def one(i, filing):
        base = baseline[filing["id"]]
        context = json.loads(filing["context_json"]) if filing.get("context_json") else {}
        line = []
        for model in judge_models:
            j = judge(filing, base["_facts"], context, base["_detection"], model=model)
            if not j:
                entry = {"error": True}
                line.append(f"{model}: ERROR")
            else:
                entry = {
                    "verdict": j.get("verdict"), "score": j.get("score"),
                    "direction": j.get("direction"),
                    "cost": cost(model, j.get("_tokens_in", 0), j.get("_tokens_out", 0),
                                 j.get("_service_tier")),
                    "tokens": (j.get("_tokens_in", 0), j.get("_tokens_out", 0)),
                    "tier": j.get("_service_tier"),
                }
                line.append(f"{model}: {entry['verdict']} {entry['score']} {entry['direction']}")
            with lock:
                results[model][filing["id"]] = entry
        print(f"  [j {i}/{len(candidates)}] {filing.get('company')} — " + " | ".join(line),
              flush=True)

    _run_all(one, candidates, workers)
    return candidates, results


def report(filings, extractions, extract_models, candidates, judgments, judge_models):
    names = {f["id"]: f"{f.get('company')} ({f.get('ticker')}, {f.get('filed_date')})"
             for f in filings}
    base_x = extract_models[0]
    print("\n=== EXTRACTION ===")
    for model in extract_models:
        rows = [r for r in extractions[model].values() if not r.get("error")]
        spent = sum(r["cost"] for r in rows)
        tin = sum(r["tokens"][0] for r in rows) / max(len(rows), 1)
        tout = sum(r["tokens"][1] for r in rows) / max(len(rows), 1)
        tiers = sorted({str(r["tier"]) for r in rows})
        errors = sum(1 for r in extractions[model].values() if r.get("error"))
        print(f"{model}: {len(rows)} ok, {errors} errors, avg tokens in/out {tin:,.0f}/{tout:,.0f}, "
              f"avg ${spent / max(len(rows), 1):.5f}/filing (tiers {tiers})")
    for model in extract_models[1:]:
        same_types = same_verdict = same_flags = compared = 0
        diffs = []
        for fid, base in extractions[base_x].items():
            other = extractions[model].get(fid)
            if base.get("error") or not other or other.get("error"):
                continue
            compared += 1
            same_types += base["types"] == other["types"]
            same_verdict += base["verdict"] == other["verdict"]
            same_flags += base["flags"] == other["flags"]
            if base["types"] != other["types"] or base["verdict"] != other["verdict"]:
                diffs.append(f"  {names[fid]}\n    {base_x}: {base['verdict']} {base['types']}"
                             f"\n    {model}: {other['verdict']} {other['types']}")
        print(f"\n{model} vs {base_x} on {compared} filings: signals identical {same_types}, "
              f"verdict identical {same_verdict}, departure flags identical {same_flags}")
        print("\n".join(diffs) or "  (no signal or verdict differences)")

    if not candidates:
        return
    print(f"\n=== JUDGE ({len(candidates)} candidates, same facts for every model) ===")
    base_j = judge_models[0]
    for model in judge_models:
        rows = [r for r in judgments[model].values() if not r.get("error")]
        spent = sum(r["cost"] for r in rows)
        tin = sum(r["tokens"][0] for r in rows) / max(len(rows), 1)
        tout = sum(r["tokens"][1] for r in rows) / max(len(rows), 1)
        print(f"{model}: {len(rows)} ok, avg tokens in/out {tin:,.0f}/{tout:,.0f}, "
              f"avg ${spent / max(len(rows), 1):.4f}/judgment")
    for model in judge_models[1:]:
        agree = direction = compared = 0
        gaps, diffs = [], []
        for fid, base in judgments[base_j].items():
            other = judgments[model].get(fid)
            if base.get("error") or not other or other.get("error"):
                continue
            compared += 1
            agree += base["verdict"] == other["verdict"]
            direction += base["direction"] == other["direction"]
            if isinstance(base["score"], (int, float)) and isinstance(other["score"], (int, float)):
                gaps.append(abs(base["score"] - other["score"]))
            if base["verdict"] != other["verdict"]:
                diffs.append(f"  {names[fid]}: {base_j} {base['verdict']} {base['score']}"
                             f" / {model} {other['verdict']} {other['score']}")
        mean_gap = sum(gaps) / len(gaps) if gaps else float("nan")
        print(f"\n{model} vs {base_j} on {compared}: verdict agree {agree}, direction agree "
              f"{direction}, mean |score gap| {mean_gap:.1f}")
        print("\n".join(diffs) or "  (no verdict differences)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--judge-n", type=int, default=20)
    parser.add_argument("--extract-models", default="gpt-5.6-luna,gpt-6-luna")
    parser.add_argument("--judge-models", default="gpt-5.6-terra,gpt-6-sol,gpt-6-luna")
    parser.add_argument("--workers", type=int, default=8,
                        help="filings in flight at once (Flex is slow per call)")
    args = parser.parse_args()

    from llm import OutOfCredits
    db.initialize_database()
    extract_models = [m.strip() for m in args.extract_models.split(",") if m.strip()]
    judge_models = [m.strip() for m in args.judge_models.split(",") if m.strip()]
    filings = sample(args.n)
    print(f"Bake-off on {len(filings)} filings: extract {extract_models}, judge {judge_models}",
          flush=True)
    try:
        extractions = extract(filings, extract_models, workers=args.workers)
        candidates, judgments = judge_all(filings, extractions[extract_models[0]],
                                          judge_models, args.judge_n, workers=args.workers)
    except OutOfCredits as e:
        print(f"STOPPED: {e}", flush=True)
        return 2
    report(filings, extractions, extract_models, candidates, judgments, judge_models)
    return 0


if __name__ == "__main__":
    sys.exit(main())
