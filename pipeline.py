"""The single analysis path: extract → context → detect → judge → persist.

There used to be three copies of this logic — filter.py's Stage 3, app.py's
run_resummarize, and app.py's run_retry_missing_summaries. They drifted, as
duplicated pipelines do: for months the retry path silently skipped
market-target detection, so every filing rescued from a rate-limit block came
back permanently missing its hurdle flag. Nobody could see it, because the
three paths were only comparable by reading all three.

Now there is one function. The three callers differ only in where the filing
comes from and where the result goes.

Cost shape, which is the design constraint:

    extraction   every in-universe filing   cheap model   ~$0.002
    context      cached per company         API + SEC     free after the first
    detectors    everything                 deterministic free
    judge        ~1 filing in 4             strong model  ~$0.03

So the expensive model reads roughly a dozen filings a day instead of a
hundred, and it reads them with the market context that makes its answer
worth having.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

import signals as signal_engine
from config import ACTIVE_PROMPT, MAX_EXTRACTION_CHARS, PIPELINE_VERSION
from market_targets import detect_market_targets
from summary_utils import serialize_subcategories, count_departures, derive_departure_flags

# A filing carrying a signal this severe is one the user wants to see today,
# not on the next scan. Drives the red URGENT badge the dashboard already has.
URGENT_SEVERITY = 5


@dataclass
class AnalysisResult:
    """Everything one filing's analysis produced.

    `fields` is the only part the database sees — a dict of column names to
    values, deliberately kept as data so the three ingest paths can share it
    without each hard-coding its own column tuple (which is exactly how they
    drifted apart before).
    """
    relevant: bool = True
    fields: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    detection: object = None
    judgment: dict = None
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = None

    @property
    def judged(self):
        return self.judgment is not None

    @property
    def signal_types(self):
        return self.detection.types if self.detection else []


def analyze_filing(filing, text=None, model=None, judge_model=None, allow_judge=True):
    """Run the full pipeline over one filing.

    Args:
        filing: filing dict/row — needs cik, ticker, company, filed_date,
                accession_no, and raw_text unless `text` is passed.
        text: filing text override (the backfill path has it in hand already).
        model / judge_model: model overrides for extraction and judgment.
        allow_judge: False runs detectors only. Used by the backtest's
                     dry-run and by anything that must not spend.

    Never raises. A failure at any stage returns a result with `error` set and
    whatever earlier stages produced — a filing is never lost to an exception.
    """
    from llm import classify_and_summarize

    filing = dict(filing or {})
    body = text if text is not None else (filing.get("raw_text") or "")
    if not body.strip():
        return AnalysisResult(relevant=True, error="no_text")

    # 1. Extraction — facts only, no verdicts.
    facts = classify_and_summarize(
        body[:MAX_EXTRACTION_CHARS], prompt_file=ACTIVE_PROMPT, model=model
    )
    if facts is None:
        return AnalysisResult(relevant=True, error="extraction_failed")

    tokens_in = facts.get("_tokens_in", 0)
    tokens_out = facts.get("_tokens_out", 0)

    if not facts.get("relevant", True):
        return AnalysisResult(
            relevant=False, facts=facts, tokens_in=tokens_in, tokens_out=tokens_out,
            fields={"relevant_reason": facts.get("relevant_reason")},
        )

    # 2. Context — the relative data the filing text cannot contain.
    context = _build_context(filing)

    # 3. Detection — deterministic, typed, evidenced.
    detection = signal_engine.detect(facts, context)

    # 4. Judgment — only where a signal already exists.
    judgment = None
    if allow_judge and signal_engine.is_judge_candidate(detection):
        judgment = _judge(filing, facts, context, detection, judge_model)
        if judgment:
            tokens_in += judgment.get("_tokens_in", 0)
            tokens_out += judgment.get("_tokens_out", 0)

    result = AnalysisResult(
        relevant=True, facts=facts, context=context, detection=detection,
        judgment=judgment, tokens_in=tokens_in, tokens_out=tokens_out,
    )
    result.fields = build_fields(facts, context, detection, judgment)
    return result


def _build_context(filing):
    """Context is an enhancement, not a precondition. If SEC or the market
    APIs are down, the filing is still worth extracting and the text-only
    detectors still fire."""
    try:
        from context import build_context
        return build_context(filing)
    except Exception as e:
        print(f"[PIPELINE] Context build failed for "
              f"{filing.get('company', 'unknown')}: {type(e).__name__}: {e}", flush=True)
        return {}


def _judge(filing, facts, context, detection, judge_model):
    try:
        from judge import judge as run_judge
        return run_judge(filing, facts, context, detection, model=judge_model)
    except Exception as e:
        print(f"[PIPELINE] Judge failed for {filing.get('company', 'unknown')}: "
              f"{type(e).__name__}: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Field assembly
# ---------------------------------------------------------------------------

def build_fields(facts, context, detection, judgment=None):
    """Turn an analysis into the column values to store.

    Writes BOTH the new signal columns and the legacy quartet
    (triage_verdict / signal_score / signal_direction / top_signal). That is
    what lets the rebuild ship without touching the existing dashboard,
    watchlist, detail page, or email composer — they keep reading the columns
    they always read, now filled in by a better process.
    """
    structured = _structured_summary(facts)
    market_targets = detect_market_targets(structured)
    structured["has_market_targets"] = market_targets["has_any"]
    structured["market_targets"] = market_targets["targets"]

    flags = derive_departure_flags(structured)
    verdict, score, direction, top_signal = _rank(detection, judgment)

    fields = {
        # Legacy columns — the existing UI reads these.
        "summary": _legacy_summary(facts, top_signal),
        "auto_category": facts.get("top_level_category") or "Other",
        "auto_subcategory": serialize_subcategories(facts.get("subcategories")),
        "urgent": 1 if _is_urgent(detection) else 0,
        "is_complex": 1 if facts.get("is_complex") else 0,
        "narrative_summary": facts.get("narrative_summary"),
        "relevant_reason": None,
        "structured_summary": json.dumps(structured),
        "has_market_targets": 1 if market_targets["has_any"] else 0,
        "comp_details": _legacy_comp_details(facts),
        "departure_count": count_departures(structured),
        "forfeited_comp": flags["forfeited_comp"],
        "has_successor": flags["has_successor"],
        "triage_verdict": verdict,
        "signal_score": score,
        "signal_direction": direction,
        "top_signal": top_signal,

        # Signal-first columns.
        "signals_json": detection.to_json() if detection else "[]",
        "signal_types": ",".join(detection.types) if detection and detection.types else None,
        "judge_json": json.dumps(judgment) if judgment else None,
        "context_json": json.dumps(context, default=str) if context else None,
        "pipeline_version": PIPELINE_VERSION,
        "price_at_ingest": (context or {}).get("price"),
        "market_cap_at_ingest": (context or {}).get("market_cap"),
        "accepted_at": (context or {}).get("accepted_at"),
        # ISO string rather than a datetime object: Python 3.12 deprecated
        # sqlite3's implicit datetime adapter, and Postgres casts the string
        # to TIMESTAMP on assignment. One value that works on both engines.
        "judged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if judgment else None,
    }
    return fields


def _rank(detection, judgment):
    """Resolve the final verdict/score/direction/one-liner.

    The judge wins where it ran. Where it didn't, detectors still produce a
    real ranking — capped at 6 so an unjudged row can never outrank one a
    strong model actually read and rated highly. Nothing is left unrated;
    "unrated" rows were what made the old signal sort untrustworthy.
    """
    if detection is None:
        return "PASS", 0, "NEUTRAL", None

    verdict = signal_engine.detector_verdict(detection)
    score = signal_engine.detector_score(detection)
    direction = detection.direction
    top_signal = signal_engine.top_signal_line(detection)

    if judgment:
        if judgment.get("score") is not None:
            score = judgment["score"]
        if judgment.get("verdict"):
            verdict = judgment["verdict"]
        if judgment.get("direction"):
            direction = judgment["direction"]
        if judgment.get("thesis"):
            top_signal = judgment["thesis"]

    return verdict, score, direction, top_signal


def _is_urgent(detection):
    return bool(detection and detection.max_severity >= URGENT_SEVERITY)


def _structured_summary(facts):
    """Build the v3-shaped blob the existing templates render.

    The field names here are load-bearing: derive_departure_flags,
    detect_market_targets, count_departures, and _structured_summary.html all
    read them. v4 extraction deliberately kept the same array names so this
    stays a copy rather than a translation.
    """
    return {
        "reasoning": facts.get("reasoning"),
        "departures": facts.get("departures") or [],
        "appointments": facts.get("appointments") or [],
        "comp_events": facts.get("comp_events") or [],
        "other": facts.get("other") or [],
        "insider_transactions": facts.get("insider_transactions") or [],
        "filing_flags": facts.get("filing_flags") or {},
    }


def _legacy_summary(facts, top_signal):
    """The old `summary` column, still read by emails, the watchlist card, and
    the dashboard's search. The signal line is the best one-sentence summary
    the system now has, so it leads."""
    if top_signal:
        return top_signal
    narrative = facts.get("narrative_summary")
    if narrative:
        return narrative

    parts = []
    for dep in (facts.get("departures") or [])[:2]:
        parts.append(f"{dep.get('name')} ({dep.get('title')}) — "
                     f"{dep.get('stated_reason') or 'departure'}")
    for app in (facts.get("appointments") or [])[:2]:
        parts.append(f"{app.get('name')} appointed {app.get('title')}")
    for comp in (facts.get("comp_events") or [])[:1]:
        parts.append(f"Comp: {comp.get('executive')} — "
                     f"{comp.get('grant_type')} {comp.get('grant_value') or ''}")
    for other in (facts.get("other") or []):
        parts.append(str(other))
        if len(parts) >= 4:
            break
    return "; ".join(parts) if parts else ""


def _legacy_comp_details(facts):
    """Populate the old comp_details card from the first comp event.

    Kept so the filing detail page's Compensation Details table keeps
    rendering for new filings, not just historical ones.
    """
    events = facts.get("comp_events") or []
    if not events or not isinstance(events[0], dict):
        return None
    event = events[0]
    targets = event.get("market_based_targets") or {}
    details = {
        "grant_value": event.get("grant_value"),
        "grant_type": event.get("grant_type"),
        "vesting_target_price": targets.get("stock_price") if isinstance(targets, dict) else None,
        "performance_hurdles": event.get("operating_hurdles"),
        "stock_vs_cash_election": None,
    }
    return json.dumps(details) if any(details.values()) else None


# ---------------------------------------------------------------------------
# Persistence — the two shapes the callers need
# ---------------------------------------------------------------------------

def apply_to_filing(filing, result):
    """Merge an analysis into a filing dict that hasn't been inserted yet.

    Used by the ingest path, where the row goes to insert_filing() rather than
    an UPDATE.
    """
    filing.update(result.fields)
    filing.setdefault("source", "8-K")
    return filing


def persist(filing_id, result):
    """Write an analysis onto an existing row.

    Snapshots the pre-rebuild verdict into legacy_triage_json the first time a
    row is re-analyzed, so a re-scoring run stays reversible and the old and
    new judgments can be compared rather than just replaced.
    """
    from database import get_filing_by_id, update_filing_fields

    fields = dict(result.fields)

    # get_filing_by_id returns a sqlite3.Row locally and a real dict on
    # Postgres. Row supports row["k"] but not .get() (see CLAUDE.md), so
    # convert before touching it — this exact mismatch has bitten this
    # codebase three times.
    existing = get_filing_by_id(filing_id)
    existing = dict(existing) if existing is not None else None

    if existing and not existing.get("legacy_triage_json") and existing.get("triage_verdict"):
        fields["legacy_triage_json"] = json.dumps({
            "triage_verdict": existing.get("triage_verdict"),
            "signal_score": existing.get("signal_score"),
            "signal_direction": existing.get("signal_direction"),
            "top_signal": existing.get("top_signal"),
        })

    return update_filing_fields(filing_id, **fields)
