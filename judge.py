"""The judge — a strong model weighing filings the detectors already flagged.

Division of labour, and the reason for it:

  detectors  find signals   — deterministic, free, run on everything
  judge      weighs them    — expensive, runs on roughly one filing in four

The old system had this backwards. Its one model call read raw filing text
with no market context and produced a number, so it was simultaneously doing
the job it was worst at (spotting relative signals it couldn't see) and not
doing the job it was best at (weighing evidence and writing the sentence a
human reads). Here it only does the second.

Two things make the judgment personal rather than generic:

  guidelines — rules the user wrote, which override the prompt's defaults
  examples   — filings the user labelled that share a signal type with this
               one, which are far better evidence of their taste than any
               amount of prompt text

Every failure path degrades to the detector verdict. A filing is never lost
because a model call failed.
"""

import json

from config import MAX_JUDGE_CHARS, PIPELINE_VERSION
from database import get_guidelines, get_labeled_examples
from llm import judge_filing

# How many labelled examples to show. Enough to convey taste, few enough that
# they don't crowd out the filing itself or double the prompt cost.
MAX_EXAMPLES = 4

VALID_VERDICTS = {"DEEP_LOOK", "MONITOR", "PASS"}
VALID_DIRECTIONS = {"BEARISH", "BULLISH", "MIXED", "NEUTRAL"}


def judge(filing, facts, context, detection, model=None, guidelines=None, examples=None):
    """Score and explain one filing.

    Args:
        filing: the filing row (company, ticker, filed_date, item_codes...).
        facts: extraction output.
        context: context.build_context() output.
        detection: signals.DetectionResult.
        guidelines / examples: injected for tests; loaded from the DB otherwise.

    Returns a validated judgment dict, or None when the call failed or came
    back unusable. None means "fall back to detectors", never "drop it".
    """
    payload = build_payload(filing, facts, context, detection,
                            guidelines=guidelines, examples=examples)
    raw = judge_filing(payload, model=model)
    if raw is None:
        return None

    judgment = validate(raw)
    if judgment is None:
        print(f"    Judge returned an unusable payload for "
              f"{filing.get('company', 'unknown')}", flush=True)
        return None

    judgment["_tokens_in"] = raw.get("_tokens_in", 0)
    judgment["_tokens_out"] = raw.get("_tokens_out", 0)
    judgment["_model"] = raw.get("_model")
    judgment["_pipeline_version"] = PIPELINE_VERSION
    return judgment


def build_payload(filing, facts, context, detection, guidelines=None, examples=None):
    """Assemble what the judge sees.

    Deliberately does NOT include the raw filing text by default. The
    detectors and the extraction have already read it; paying to send 120k
    characters of exhibits on every candidate is how a $1/day budget becomes
    $15/day. `facts` is the filing, distilled.
    """
    signals = [s.to_dict() for s in detection.signals]

    if guidelines is None:
        guidelines = [g["rule"] for g in _safe(get_guidelines, [])]
    if examples is None:
        examples = _relevant_examples(detection)

    return {
        "filing": {
            "company": filing.get("company"),
            "ticker": filing.get("ticker"),
            "filed_date": filing.get("filed_date"),
            "item_codes": filing.get("item_codes"),
        },
        "context": _trim_context(context),
        "signals": signals,
        "pass_reasons": detection.pass_reasons,
        "facts": _trim_facts(facts),
        "guidelines": guidelines,
        "examples": examples,
    }


def _relevant_examples(detection):
    """Past labels that share a signal type with this filing.

    Type-matched examples teach far more than recent ones: the user's view of
    departure clusters says little about how they read comp hurdles. Falls
    back to any recent labels when nothing matches, and to nothing at all
    before the user has labelled anything.
    """
    seen, examples = set(), []
    for signal_type in detection.types:
        for row in _safe(lambda: get_labeled_examples(signal_type=signal_type, limit=2), []):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            examples.append(_format_example(row))
            if len(examples) >= MAX_EXAMPLES:
                return examples

    if not examples:
        for row in _safe(lambda: get_labeled_examples(limit=MAX_EXAMPLES), []):
            examples.append(_format_example(row))
    return examples[:MAX_EXAMPLES]


def _format_example(row):
    return {
        "id": row.get("id"),
        "company": row.get("company"),
        "signal_types": row.get("signal_types"),
        "what_the_system_said": row.get("top_signal"),
        "user_label": row.get("label"),
        "user_note": row.get("note"),
    }


# Context keys worth the tokens. The rest (raw item-code history, full
# per-executive cadence) informed the detectors already and would only pad the
# prompt.
_CONTEXT_KEYS = (
    "price", "market_cap", "next_earnings_date", "days_to_earnings",
    "last_earnings_date", "days_since_earnings", "ipo_date", "months_since_ipo",
    "departures_24mo", "accepted_et", "is_after_hours_friday", "filed_date",
)


def _trim_context(context):
    return {k: (context or {}).get(k) for k in _CONTEXT_KEYS}


def _trim_facts(facts):
    """Drop the extraction's bookkeeping and cap the size.

    `reasoning` is the extractor's chain-of-thought — useful for debugging the
    extraction, worthless to the judge, and not free.
    """
    if not isinstance(facts, dict):
        return {}
    trimmed = {k: v for k, v in facts.items()
               if not k.startswith("_") and k not in ("reasoning", "relevant_reason")}
    encoded = json.dumps(trimmed, default=str)
    if len(encoded) <= MAX_JUDGE_CHARS:
        return trimmed

    # Oversized extraction (a filing with dozens of events). Keep the event
    # arrays, which carry the signal, and drop the free-text narrative.
    trimmed.pop("narrative_summary", None)
    trimmed["other"] = (trimmed.get("other") or [])[:10]
    return trimmed


def validate(raw):
    """Coerce a model response into a usable judgment, or None.

    Returns None only when the response has no usable score AND no thesis —
    at that point there is nothing to rank or display and the detector verdict
    is strictly better. Individual bad fields are repaired rather than
    discarding an otherwise good judgment.
    """
    if not isinstance(raw, dict):
        return None

    score = _score(raw.get("score"))
    thesis = _text(raw.get("thesis"), 400)
    if score is None and not thesis:
        return None

    verdict = str(raw.get("verdict") or "").strip().upper().replace(" ", "_")
    if verdict not in VALID_VERDICTS:
        verdict = None

    direction = str(raw.get("direction") or "").strip().upper()
    if direction not in VALID_DIRECTIONS:
        direction = None

    # Keep score and verdict from contradicting each other on the dashboard —
    # a row badged PASS while sorting near the top reads as a broken tool.
    if score is not None:
        if verdict is None:
            verdict = "DEEP_LOOK" if score >= 7 else ("PASS" if score <= 3 else "MONITOR")
        elif verdict == "PASS" and score >= 7:
            verdict = "DEEP_LOOK"
        elif verdict == "DEEP_LOOK" and score <= 3:
            verdict = "MONITOR"

    why = raw.get("why")
    if isinstance(why, str):
        why = [why]
    why = [_text(w, 300) for w in why if _text(w, 300)] if isinstance(why, list) else []

    disputed = raw.get("disputed_signals")
    if isinstance(disputed, str):
        disputed = [disputed]
    disputed = ([str(d).strip().upper() for d in disputed if str(d or "").strip()]
                if isinstance(disputed, list) else [])

    return {
        "score": score,
        "verdict": verdict,
        "direction": direction,
        "thesis": thesis,
        "why": why[:5],
        "anti_thesis": _text(raw.get("anti_thesis"), 400),
        "disputed_signals": disputed,
        "matched_example": raw.get("matched_example"),
        "applied_guideline": _text(raw.get("applied_guideline"), 300),
    }


def _score(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, min(10, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _text(value, limit):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _safe(fn, default):
    """Run a DB lookup, tolerating an empty or unavailable table.

    Guidelines and labels are enhancements. A judgment without them is still
    a judgment; a crash here would cost the filing entirely.
    """
    try:
        return fn()
    except Exception as e:
        print(f"[JUDGE] optional lookup failed: {type(e).__name__}: {e}", flush=True)
        return default
