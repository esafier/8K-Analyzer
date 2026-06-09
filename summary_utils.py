"""Helpers for working with the structured summary fields stored in the filings table.

Subcategories are stored as JSON arrays in a single TEXT column (auto_subcategory)
for backward compatibility with existing rows that hold a single subcategory string.
"""
import json
from typing import Optional


def parse_subcategories(raw: Optional[str]) -> list[str]:
    """Convert the stored auto_subcategory string into a list.

    Handles three shapes:
      - JSON array string  -> parse normally
      - Plain string       -> wrap in a one-element list (legacy rows)
      - None / empty       -> empty list
    """
    if not raw or not str(raw).strip():
        return []

    raw = str(raw).strip()

    # Try JSON array first (new shape)
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except (json.JSONDecodeError, ValueError):
            pass  # Fall through to single-string handling

    # Legacy single-subcategory string — wrap it
    return [raw]


def serialize_subcategories(subcats: Optional[list[str]]) -> Optional[str]:
    """Convert a list of subcategories into the stored JSON array string.

    Returns None when nothing to store (empty list or None input).
    Filters out empty / None values defensively.
    """
    if not subcats:
        return None

    cleaned = [str(s).strip() for s in subcats if s and str(s).strip()]
    if not cleaned:
        return None

    return json.dumps(cleaned)


# Allowed values for the triage fields. Anything else from the LLM is dropped
# (better an unrated row than a garbage badge the dashboard sorts on).
TRIAGE_VERDICTS = {"DEEP_LOOK", "MONITOR", "PASS"}
TRIAGE_DIRECTIONS = {"BEARISH", "BULLISH", "MIXED", "NEUTRAL"}


def parse_triage(llm_result):
    """Validate the `triage` object from a v3 LLM response.

    Single source of truth for all three ingest paths (backfill, re-summarize,
    retry-missing) so the validation rules can't drift apart.

    Returns a dict with keys verdict, score, direction, top_signal — each None
    when missing or invalid. Never raises on malformed input.
    """
    empty = {"verdict": None, "score": None, "direction": None, "top_signal": None}
    if not isinstance(llm_result, dict):
        return empty

    triage = llm_result.get("triage")
    if not isinstance(triage, dict):
        return empty

    out = dict(empty)

    verdict = str(triage.get("verdict") or "").strip().upper().replace(" ", "_")
    if verdict in TRIAGE_VERDICTS:
        out["verdict"] = verdict

    direction = str(triage.get("direction") or "").strip().upper()
    if direction in TRIAGE_DIRECTIONS:
        out["direction"] = direction

    # Score: accept int, float, or numeric string; clamp to 0-10
    raw_score = triage.get("score")
    try:
        score = int(round(float(raw_score)))
        out["score"] = max(0, min(10, score))
    except (TypeError, ValueError):
        pass

    top_signal = triage.get("top_signal")
    if top_signal and str(top_signal).strip():
        # Cap length so a runaway LLM response can't bloat the dashboard
        out["top_signal"] = str(top_signal).strip()[:400]

    return out


def count_departures(structured):
    """Count departure entries in a structured_summary dict (or JSON string).

    Used at ingest and by the retrofit script to populate the departure_count
    column, which powers dashboard cluster badges. Returns 0 on bad input.
    """
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except (json.JSONDecodeError, ValueError, TypeError):
            return 0
    if not isinstance(structured, dict):
        return 0
    departures = structured.get("departures")
    if not isinstance(departures, list):
        return 0
    return len([d for d in departures if isinstance(d, dict)])


def structured_summary_for_display(raw):
    """Parse the structured_summary JSON column into a dict safe for templates.

    Always returns a dict with the four event arrays, a reasoning field,
    and a has_any_event convenience flag. Never raises on malformed input.
    """
    empty = {
        "departures": [], "appointments": [], "comp_events": [], "other": [],
        "reasoning": None, "has_any_event": False,
        "has_market_targets": False,
        "market_targets": {"stock_price": [], "market_cap": [], "tsr": []},
    }
    if not raw:
        return empty
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return empty
    except (json.JSONDecodeError, ValueError):
        return empty

    out = {
        "departures": parsed.get("departures") or [],
        "appointments": parsed.get("appointments") or [],
        "comp_events": parsed.get("comp_events") or [],
        "other": parsed.get("other") or [],
        "reasoning": parsed.get("reasoning"),
        "has_market_targets": bool(parsed.get("has_market_targets")),
        # market_targets is a dict of three lists; default to empty lists if absent.
        "market_targets": parsed.get("market_targets") or {
            "stock_price": [], "market_cap": [], "tsr": [],
        },
    }
    out["has_any_event"] = any([
        out["departures"], out["appointments"], out["comp_events"], out["other"],
    ])
    return out
