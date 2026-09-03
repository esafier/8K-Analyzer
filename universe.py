"""Universe filter — decides which companies are worth analyzing at all.

This runs BEFORE any SEC document fetch or LLM call, which is the whole point:
the cheapest filing is the one you never download. Roughly half of daily 8-K
volume comes from issuers with no tradeable common equity (trusts, financing
subsidiaries, funds) or from nano-caps below the size floor, and every one of
those used to cost a document fetch plus a model call before being shown to
the user as noise.

Two gates, both fail-closed:

  1. **No ticker → skip.** An issuer with no ticker in EDGAR and none in SEC's
     CIK→ticker map has no equity to trade on the signal.
  2. **Market cap below the floor (or unknown) → skip.** "Unknown" is treated
     as a failure, not a pass. The alternative — keeping unknowns — quietly
     re-admits exactly the OTC and shell issuers the floor exists to exclude,
     because those are the tickers market-cap providers have no data for.

Because gate 2 fails closed, a broken market-cap API would empty the feed
rather than flood it. `screen_filings` counts every skip by reason and the
caller logs the breakdown, so that failure is loud rather than silent.
"""

import os

# Minimum market capitalization, in dollars. The user invests in micro caps,
# so this is deliberately low — it excludes shells and sub-scale issuers
# without excluding the small companies where governance signals matter most.
MIN_MARKET_CAP = int(os.environ.get("MIN_MARKET_CAP", 50_000_000))


def screen_filings(filings_metadata, market_caps=None):
    """Split filing metadata into (kept, skipped) by the universe rules.

    Args:
        filings_metadata: list of filing dicts from fetcher.parse_filing_metadata
                          (needs 'ticker'; 'company' only for logging).
        market_caps: optional {TICKER: cap_or_None} map. When omitted, it is
                     fetched for the distinct tickers in the batch — one
                     blocking call for the whole batch rather than per filing.

    Returns:
        (kept, skipped) where `skipped` entries are the original dicts with a
        '_skip_reason' key added ('no_ticker' | 'unknown_market_cap' |
        'below_floor'). Reasons are kept rather than discarded so the daily
        job can report *why* a day was quiet.
    """
    if not filings_metadata:
        return [], []

    if market_caps is None:
        tickers = sorted({
            (f.get("ticker") or "").strip().upper()
            for f in filings_metadata
            if (f.get("ticker") or "").strip()
        })
        market_caps = _lookup_market_caps(tickers)

    kept, skipped = [], []
    for filing in filings_metadata:
        reason = _skip_reason(filing, market_caps)
        if reason:
            filing["_skip_reason"] = reason
            skipped.append(filing)
        else:
            kept.append(filing)

    return kept, skipped


def _skip_reason(filing, market_caps):
    """Why this filing is out of universe, or None when it's in."""
    ticker = (filing.get("ticker") or "").strip().upper()
    if not ticker:
        return "no_ticker"

    cap = market_caps.get(ticker)
    if cap is None:
        return "unknown_market_cap"
    if cap < MIN_MARKET_CAP:
        return "below_floor"
    return None


def _lookup_market_caps(tickers):
    """Fetch market caps for a batch of tickers, tolerating a dead provider.

    Uses the blocking refresh (not the dashboard's read-through cache) because
    a screening decision made against an empty cache would drop the entire
    day's filings on the first run for a new ticker.
    """
    if not tickers:
        return {}
    try:
        from market_cap import refresh_market_caps_sync
        return refresh_market_caps_sync(tickers)
    except Exception as e:
        print(f"[UNIVERSE] Market cap lookup failed: {e}", flush=True)
        return {}


def summarize_skips(skipped):
    """Count skips by reason, e.g. {'no_ticker': 12, 'below_floor': 40}."""
    counts = {}
    for filing in skipped:
        reason = filing.get("_skip_reason", "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts
