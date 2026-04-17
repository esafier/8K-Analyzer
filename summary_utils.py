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


def structured_summary_for_display(raw):
    """Parse the structured_summary JSON column into a dict safe for templates.

    Always returns a dict with the four event arrays, a reasoning field,
    and a has_any_event convenience flag. Never raises on malformed input.
    """
    empty = {
        "departures": [], "appointments": [], "comp_events": [], "other": [],
        "reasoning": None, "has_any_event": False,
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
    }
    out["has_any_event"] = any([
        out["departures"], out["appointments"], out["comp_events"], out["other"],
    ])
    return out
