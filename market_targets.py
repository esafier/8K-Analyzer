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
    # LLM occasionally returns a dict/list here instead of a string — coerce so
    # regex.search doesn't crash. Stringified JSON still contains the keywords.
    if not isinstance(text, str):
        try:
            text = json.dumps(text)
        except (TypeError, ValueError):
            text = str(text)
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


# Dollar amounts inside a free-text price-target string, e.g.
# "$12.50 and $15.00 sustained over 60 days" -> [12.50, 15.00]
# The comma branch requires an actual comma group and the trailing (?!\d)
# stops partial matches — without it "$1000" parsed as 100.0.
_PRICE_VALUE_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,4})?|\d+(?:\.\d{1,4})?)(?![\d,])")


def extract_price_values(text):
    """Pull per-share dollar amounts out of a free-text stock-price target.

    Returns a list of floats (may be empty). Values outside (0, 100000) are
    discarded as parse noise.
    """
    if not _is_meaningful(text):
        return []
    if not isinstance(text, str):
        text = str(text)
    values = []
    for m in _PRICE_VALUE_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 0 < v < 100_000:
            values.append(v)
    return values


def annotate_price_targets(market_targets, current_price):
    """Compute the % appreciation required to hit each stock-price hurdle.

    This is what turns "vests at $10" into "needs +100% from here" — the
    number that actually ranks bullish comp conviction.

    Args:
        market_targets: the targets dict stored in structured_summary
                        ({"stock_price": [{executive, value}], ...})
        current_price: current share price (float)

    Returns:
        dict with:
          - by_value: {original value string: [{"target": 12.5, "pct": 25.0}, ...]}
          - min_pct / max_pct: across all parsed targets
          - current_price
        or None when nothing is computable (no price, no parseable targets).
    """
    if not isinstance(market_targets, dict) or not current_price or current_price <= 0:
        return None

    entries = market_targets.get("stock_price") or []
    if not isinstance(entries, list):
        return None
    by_value = {}
    all_pcts = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        raw = e.get("value")
        vals = extract_price_values(raw)
        if not vals:
            continue
        points = [{"target": v, "pct": (v / current_price - 1.0) * 100.0} for v in vals]
        by_value[str(raw).strip()] = points
        all_pcts.extend(pt["pct"] for pt in points)

    if not all_pcts:
        return None

    return {
        "by_value": by_value,
        "min_pct": min(all_pcts),
        "max_pct": max(all_pcts),
        "current_price": current_price,
    }


def detect_from_json_string(structured_summary_json):
    """Convenience wrapper: parse JSON string then run detect_market_targets."""
    if not structured_summary_json:
        return detect_market_targets({})
    try:
        parsed = json.loads(structured_summary_json)
    except (json.JSONDecodeError, ValueError, TypeError):
        return detect_market_targets({})
    return detect_market_targets(parsed)
