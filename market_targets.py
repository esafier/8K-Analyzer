"""Detection of market-based comp targets (stock price, market cap, TSR).

Used in two places:
  1. filter.py — when persisting a fresh LLM result, set the has_market_targets flag.
  2. retrofit_market_targets.py — backfill the flag onto previously-analyzed filings
     by scanning their existing structured_summary JSON.

The single source of truth lives here so the two paths can't drift apart.
"""
import json
import re


# Phrases that indicate TSR-based vesting. Case-insensitive substring match.
# "TSR" alone is a 3-letter token — match as a word boundary to avoid false hits.
_TSR_PATTERNS = [
    re.compile(r"\btsr\b", re.IGNORECASE),
    re.compile(r"total\s+shareholder\s+return", re.IGNORECASE),
    re.compile(r"relative\s+shareholder\s+return", re.IGNORECASE),
]

# Market-cap hurdle phrases.
_MARKET_CAP_PATTERNS = [
    re.compile(r"market\s+cap(?:italization)?", re.IGNORECASE),
]


def _is_meaningful(value):
    """Treat None, empty string, and the literal strings 'null'/'none' as absent."""
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    if s.lower() in ("null", "none", "n/a"):
        return False
    return True


def _detect_in_hurdle_text(text):
    """Given a free-text hurdle field, return which market-based types appear."""
    found = {"tsr": False, "market_cap": False}
    if not _is_meaningful(text):
        return found
    if any(p.search(text) for p in _TSR_PATTERNS):
        found["tsr"] = True
    if any(p.search(text) for p in _MARKET_CAP_PATTERNS):
        found["market_cap"] = True
    return found


def detect_market_targets(structured):
    """Inspect a parsed structured_summary dict and extract market-based targets.

    Handles both new-schema (comp_event has market_based_targets object) and
    old-schema (comp_event has performance_hurdles free text + stock_price_targets).

    Returns:
        dict with keys:
          - has_any (bool): true if at least one market-based target detected
          - targets (dict): {
                "stock_price": [{executive, value}, ...],
                "market_cap":  [{executive, value}, ...],
                "tsr":         [{executive, value}, ...],
            }
    """
    out = {
        "has_any": False,
        "targets": {"stock_price": [], "market_cap": [], "tsr": []},
    }

    if not isinstance(structured, dict):
        return out

    for ev in (structured.get("comp_events") or []):
        if not isinstance(ev, dict):
            continue
        executive = ev.get("executive") or "Executive"

        # --- New schema: market_based_targets object ---
        mbt = ev.get("market_based_targets")
        if isinstance(mbt, dict):
            sp = mbt.get("stock_price")
            mc = mbt.get("market_cap")
            tsr = mbt.get("tsr")
            if _is_meaningful(sp):
                out["targets"]["stock_price"].append({"executive": executive, "value": str(sp).strip()})
            if _is_meaningful(mc):
                out["targets"]["market_cap"].append({"executive": executive, "value": str(mc).strip()})
            if _is_meaningful(tsr):
                out["targets"]["tsr"].append({"executive": executive, "value": str(tsr).strip()})

        # --- Old/dedicated stock_price_targets field (still emitted by current prompt
        #     for backward compat). Only add if not already captured above. ---
        spt = ev.get("stock_price_targets")
        if _is_meaningful(spt):
            # Avoid duplicate if market_based_targets.stock_price already matched
            already = any(t["executive"] == executive and t["value"] == str(spt).strip()
                          for t in out["targets"]["stock_price"])
            if not already:
                out["targets"]["stock_price"].append({"executive": executive, "value": str(spt).strip()})

        # --- Old schema fallback: scan performance_hurdles text for TSR / market cap ---
        # (New schema uses operating_hurdles, but old rows have performance_hurdles.)
        hurdle_text = ev.get("operating_hurdles") or ev.get("performance_hurdles")
        found = _detect_in_hurdle_text(hurdle_text)
        if found["tsr"]:
            already = any(t["executive"] == executive for t in out["targets"]["tsr"])
            if not already:
                out["targets"]["tsr"].append({"executive": executive, "value": str(hurdle_text).strip()})
        if found["market_cap"]:
            already = any(t["executive"] == executive for t in out["targets"]["market_cap"])
            if not already:
                out["targets"]["market_cap"].append({"executive": executive, "value": str(hurdle_text).strip()})

    out["has_any"] = bool(
        out["targets"]["stock_price"]
        or out["targets"]["market_cap"]
        or out["targets"]["tsr"]
    )
    return out


def detect_from_json_string(structured_summary_json):
    """Convenience wrapper: parse JSON string then run detect_market_targets."""
    if not structured_summary_json:
        return detect_market_targets({})
    try:
        parsed = json.loads(structured_summary_json)
    except (json.JSONDecodeError, ValueError, TypeError):
        return detect_market_targets({})
    return detect_market_targets(parsed)
