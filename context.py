"""Company context — the half of the judgment the filing text cannot supply.

Almost every signal this system hunts is *relative*:

  "off-cycle"  → relative to the company's own grant months
  "oversized"  → relative to that executive's prior grants
  "conviction" → relative to today's share price
  "cluster"    → relative to who else left in the last two years
  "buried"     → relative to the market's clock, not the filing's date column

None of that is in the 8-K. The old pipeline scored filings without it, which
is why the scores stopped discriminating: shown only the text, a model can
detect the handful of signals a filing spells out and is blind to the rest.

Everything here is cached per company (`company_profiles`), because the
expensive work is per issuer, not per filing — five filings from one company
in a week should cost one SEC fetch, not five. Every lookup degrades to None
rather than raising: a missing price costs one signal, a crash costs the
filing.
"""

import json
import statistics
from datetime import datetime, timedelta, timezone

from database import get_company_profile, upsert_company_profile

# Cached company context older than this is refreshed. A day-old price is
# accurate enough to test a ">50% above current" hurdle, and the SEC-derived
# half (IPO date, filing history) barely moves at all.
PROFILE_TTL_HOURS = 24

# How far back to look for restatements and prior earnings dates.
ITEM_HISTORY_DAYS = 400

# Item codes worth remembering from a company's recent 8-K history.
#   2.02 — results announcement; the real earnings date, days before the 10-Q
#   4.02 — non-reliance / restatement; recontextualizes any departure near it
#   1.01 — material agreement, often the M&A trail
#   5.02 — the departure history itself
TRACKED_ITEMS = ("2.02", "4.02", "1.01", "5.02")

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo ships with 3.9+
    EASTERN = None


def build_context(filing, refresh=False):
    """Assemble the context dict the detectors and judge see.

    `filing` is a filing row (or dict) with at least cik, ticker, filed_date,
    accession_no. Returns a plain dict — JSON-serializable, because it is
    stored on the filing so a verdict can be audited later against the numbers
    it was actually formed from, not against prices that have since moved.
    """
    filing = dict(filing or {})
    cik = str(filing.get("cik") or "").strip()
    ticker = str(filing.get("ticker") or "").strip().upper()
    filed_date = filing.get("filed_date")

    profile = _company_profile(cik, ticker, refresh=refresh)

    context = {
        "company": filing.get("company"),
        "ticker": ticker or None,
        "cik": cik or None,
        "filed_date": filed_date,
        "price": profile.get("price"),
        "market_cap": profile.get("market_cap"),
        "next_earnings_date": profile.get("next_earnings"),
        "days_to_earnings": _days_from(filed_date, profile.get("next_earnings")),
        "last_earnings_date": profile.get("last_202_date"),
        "days_since_earnings": _days_from(profile.get("last_202_date"), filed_date),
        "ipo_date": profile.get("ipo_date"),
        "months_since_ipo": _months_between(profile.get("ipo_date"), filed_date),
        "recent_item_codes": _loads(profile.get("recent_8k_items_json")) or {},
        "grant_cadence": _loads(profile.get("grant_cadence_json")) or {},
    }

    # Acceptance timestamp for THIS filing — needed for Friday-night burial,
    # and only knowable from the company's submissions index.
    accepted = _accepted_at(cik, filing.get("accession_no"))
    context["accepted_at"] = accepted
    context["accepted_et"] = _to_eastern_string(accepted)
    context["is_after_hours_friday"] = _is_after_hours_friday(accepted)

    # Departure history: prefer what enrichment already stamped on the row,
    # so a normal ingest costs zero extra SEC calls.
    context["departures_24mo"] = _departures_24mo(filing)

    return context


# ---------------------------------------------------------------------------
# Per-company profile (cached)
# ---------------------------------------------------------------------------

def _company_profile(cik, ticker, refresh=False):
    """Load the cached company profile, rebuilding it when missing or stale."""
    if not refresh and cik:
        cached = get_company_profile(cik, max_age_hours=PROFILE_TTL_HOURS)
        if cached:
            return cached

    profile = {
        "ticker": ticker or None,
        "price": _price(ticker),
        "market_cap": _market_cap(ticker),
        "next_earnings": _next_earnings(ticker),
    }
    profile.update(_sec_history(cik))
    profile["grant_cadence_json"] = json.dumps(_grant_cadence(ticker))

    if cik:
        try:
            upsert_company_profile(cik, **{k: v for k, v in profile.items() if v is not None})
        except Exception as e:
            print(f"[CONTEXT] Could not cache profile for CIK {cik}: {e}", flush=True)

    return profile


def _price(ticker):
    if not ticker:
        return None
    try:
        from stock_price import get_stock_price
        return get_stock_price(ticker)
    except Exception as e:
        print(f"[CONTEXT] Price lookup failed for {ticker}: {e}", flush=True)
        return None


def _market_cap(ticker):
    if not ticker:
        return None
    try:
        from market_cap import refresh_market_caps_sync
        return refresh_market_caps_sync([ticker]).get(ticker)
    except Exception as e:
        print(f"[CONTEXT] Market cap lookup failed for {ticker}: {e}", flush=True)
        return None


def _next_earnings(ticker):
    if not ticker:
        return None
    try:
        from earnings import refresh_earnings_sync
        info = refresh_earnings_sync([ticker]).get(ticker)
        return (info or {}).get("date") if isinstance(info, dict) else None
    except Exception as e:
        print(f"[CONTEXT] Earnings lookup failed for {ticker}: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# SEC submissions — item calendar + IPO proxy
# ---------------------------------------------------------------------------

def _sec_history(cik):
    """Recent 8-K item calendar, last earnings date, and first-filing date.

    One submissions fetch answers all three. `recent` holds ~1000 filings,
    which for an 8-K-heavy filer is a few years — plenty for a 400-day window.
    """
    empty = {"recent_8k_items_json": None, "last_202_date": None, "ipo_date": None}
    if not cik:
        return empty

    try:
        from fetcher import fetch_company_submissions
        data = fetch_company_submissions(cik)
    except Exception as e:
        print(f"[CONTEXT] Submissions fetch failed for CIK {cik}: {e}", flush=True)
        return empty
    if not data:
        return empty

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items = recent.get("items") or []

    cutoff = (datetime.now() - timedelta(days=ITEM_HISTORY_DAYS)).strftime("%Y-%m-%d")
    by_item = {}
    last_202 = None

    for i, form in enumerate(forms):
        if form not in ("8-K", "8-K/A"):
            continue
        filed = dates[i] if i < len(dates) else None
        if not filed or filed < cutoff:
            continue
        item_str = items[i] if i < len(items) else ""
        for code in TRACKED_ITEMS:
            if code in (item_str or ""):
                by_item.setdefault(code, []).append(filed)
                if code == "2.02" and (last_202 is None or filed > last_202):
                    last_202 = filed

    return {
        "recent_8k_items_json": json.dumps(by_item) if by_item else None,
        "last_202_date": last_202,
        "ipo_date": _earliest_filing_date(data),
    }


def _earliest_filing_date(submissions):
    """Best available proxy for when the company went public.

    SEC gives no IPO date. The earliest date in the filing history is close
    enough for "is this an exit inside the first year", which is the only
    question asked of it — and it is conservative: a company with older
    filings simply won't trip the recent-IPO signal.
    """
    candidates = []

    older = (submissions.get("filings") or {}).get("files") or []
    for chunk in older:
        value = chunk.get("filingFrom")
        if value:
            candidates.append(value)

    recent_dates = ((submissions.get("filings") or {}).get("recent") or {}).get("filingDate") or []
    if recent_dates:
        candidates.append(min(recent_dates))

    return min(candidates) if candidates else None


def _accepted_at(cik, accession_no):
    """SEC acceptance timestamp (UTC ISO) for one filing, or None.

    Deliberately reads the same cached-per-company submissions document rather
    than scraping the filing index page — one JSON fetch already on hand
    versus one HTML fetch per filing.
    """
    if not cik or not accession_no:
        return None
    try:
        from fetcher import fetch_company_submissions
        data = fetch_company_submissions(cik)
    except Exception:
        return None
    if not data:
        return None

    recent = (data.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    accepted = recent.get("acceptanceDateTime") or []
    target = str(accession_no).replace("-", "")

    for i, acc in enumerate(accessions):
        if str(acc).replace("-", "") == target:
            return accepted[i] if i < len(accepted) else None
    return None


# ---------------------------------------------------------------------------
# Grant cadence — the baseline that makes "off-cycle" and "oversized" mean
# something
# ---------------------------------------------------------------------------

MIN_GRANTS_FOR_CADENCE = 3


def _grant_cadence(ticker, years=3):
    """Reconstruct this company's grant rhythm from Form 4 history.

    Returns per-executive grant sizes plus `_annual_months`: the calendar
    months this company reliably grants in. Without that baseline "off-cycle"
    is unknowable — a June grant is unremarkable at a company that grants in
    June and is the whole story at one that grants every March.

    Uses the API Ninjas insider endpoint rather than walking EDGAR: the same
    history costs one paginated call instead of one document fetch per Form 4,
    which for a serial filer is hundreds of SEC requests.
    """
    if not ticker:
        return {}

    start = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    try:
        rows = _fetch_insider_transactions(ticker, start)
    except Exception as e:
        print(f"[CONTEXT] Insider history failed for {ticker}: {e}", flush=True)
        return {}
    if not rows:
        return {}

    by_person = {}
    months = []
    for row in rows:
        # Code A is a grant/award. Everything else (sales, exercises, gifts)
        # says nothing about the grant calendar.
        if str(row.get("transaction_code") or "").upper() != "A":
            continue
        name = " ".join(str(row.get("insider_name") or "").lower().split())
        date = str(row.get("filing_date") or "")[:10]
        shares = row.get("shares")
        if not name or not date:
            continue

        entry = by_person.setdefault(name, {"dates": [], "shares": []})
        entry["dates"].append(date)
        if isinstance(shares, (int, float)) and shares > 0:
            entry["shares"].append(float(shares))

        try:
            months.append(int(date[5:7]))
        except (ValueError, IndexError):
            pass

    cadence = {}
    for name, entry in by_person.items():
        cadence[name] = {
            "grant_count": len(entry["dates"]),
            "last_grant_date": max(entry["dates"]),
            "median_shares": statistics.median(entry["shares"]) if entry["shares"] else None,
        }

    annual_months = _dominant_months(months)
    if annual_months:
        cadence["_annual_months"] = annual_months
    return cadence


def _dominant_months(months):
    """The months a company actually grants in.

    Only returned once there is enough history to be a rhythm rather than a
    coincidence — calling a grant "off-cycle" against a one-observation
    baseline would manufacture signal out of nothing. A month qualifies when it
    holds at least a quarter of all grants.
    """
    if len(months) < MIN_GRANTS_FOR_CADENCE:
        return []
    counts = {}
    for month in months:
        counts[month] = counts.get(month, 0) + 1
    threshold = max(2, len(months) * 0.25)
    return sorted(m for m, c in counts.items() if c >= threshold)


def _fetch_insider_transactions(ticker, start_date, limit=250):
    """Fetch Form 4 rows from API Ninjas. Isolated so tests can stub it."""
    import requests
    from config import API_NINJAS_KEY

    if not API_NINJAS_KEY:
        return []
    resp = requests.get(
        "https://api.api-ninjas.com/v1/insidertransactions",
        params={"ticker": ticker, "start_date": start_date, "limit": limit},
        headers={"X-Api-Key": API_NINJAS_KEY},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[CONTEXT] Insider API returned {resp.status_code} for {ticker}", flush=True)
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Departure history
# ---------------------------------------------------------------------------

def _departures_24mo(filing):
    """Deduped 24-month departure count for this company.

    Prefers the value enrichment already stamped on the row. Returns None —
    not 0 — when unknown, so a failed EDGAR lookup can't be read as "nobody
    left"; that mistake silently suppresses the cluster signal forever.
    """
    count = filing.get("departure_count_24mo")
    if isinstance(count, int):
        return count

    history = filing.get("departure_history")
    if history:
        try:
            parsed = json.loads(history) if isinstance(history, str) else history
            if isinstance(parsed, list):
                return len({
                    " ".join(str(d.get("person") or "").lower().split())
                    for d in parsed
                    if isinstance(d, dict) and d.get("person") and not d.get("_error")
                })
        except (ValueError, TypeError):
            pass
    return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _loads(value):
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _days_from(start, end):
    a, b = _parse_date(start), _parse_date(end)
    return (b - a).days if a and b else None


def _months_between(start, end):
    days = _days_from(start, end)
    return round(days / 30.44, 1) if days is not None else None


def _parse_accepted(accepted_at):
    """Parse SEC's acceptance timestamp ('2026-08-17T11:00:44.000Z') as UTC."""
    if not accepted_at:
        return None
    text = str(accepted_at).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _to_eastern_string(accepted_at):
    parsed = _parse_accepted(accepted_at)
    if parsed is None or EASTERN is None:
        return None
    return parsed.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M ET")


def _is_after_hours_friday(accepted_at):
    """Accepted after Friday's close — the traditional burial slot.

    Uses acceptance time in Eastern, never filed_date: SEC stamps anything
    accepted after 17:30 ET with the NEXT business day, so a Friday-evening
    filing shows a Monday date and is invisible from the date column alone.
    """
    parsed = _parse_accepted(accepted_at)
    if parsed is None or EASTERN is None:
        return False
    local = parsed.astimezone(EASTERN)
    return local.weekday() == 4 and local.hour >= 16
