"""Form 4 scanner — insider buys and top-officer grants, as inbox rows.

8-Ks announce decisions; Form 4s record what insiders actually did with
stock, within two business days. Two kinds of Form 4 are worth a slot in the
inbox, and nothing else is:

  - **Open-market purchases** by officers or directors. Comp is handed out;
    a purchase is chosen, with the buyer's own money.
  - **Equity grants to the CEO, CFO or Chair** — which the existing
    OFF_CYCLE_GRANT and OVERSIZED_GRANT detectors score against the company's
    own grant history. A grant in the usual month at the usual size is
    silence, and is not stored.

Everything else — routine sales, tax withholding, option exercises, gifts —
is ignored. Insider SELLING is deliberately left out: most sales are
diversification or tax-driven, and scoring them would put hundreds of rows a
week into the inbox for a signal that is mostly noise.

No model is needed to read a Form 4: the XML already says who, what, when,
how many and at what price. So the extraction step is skipped and the facts
go straight into pipeline.analyze_facts — the same context, detectors, judge
and storage every 8-K uses.

Scope: only issuers already in the database (companies that have filed an
8-K we analyzed), and only inside the market-cap universe. That keeps the
daily scan to a few hundred SEC requests.

    python form4.py --date 2026-09-10            # scan one day, store signals
    python form4.py --date 2026-09-10 --dry-run  # show what would be stored
"""

import argparse
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{ymd}.idx"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"

# Titles whose grants are worth scoring. Grants to the wider officer group are
# overwhelmingly the annual cycle.
TOP_OFFICER_WORDS = ("chief executive", "ceo", "chief financial", "cfo",
                     "chair", "president")

MIN_BUY_USD = 50_000

# Transaction codes (SEC Form 4 instructions, Table 1):
#   P = open-market or private purchase     A = grant/award from the issuer
CODE_PURCHASE = "P"
CODE_GRANT = "A"


# ---------------------------------------------------------------------------
# The daily index
# ---------------------------------------------------------------------------

_INDEX_LINE = re.compile(
    r"^(?P<form>\S+)\s+(?P<company>.+?)\s+(?P<cik>\d{1,10})\s+"
    r"(?P<date>\d{8}|\d{4}-\d{2}-\d{2})\s+(?P<path>edgar/data/\S+)\s*$"
)


def parse_index(text):
    """Parse EDGAR's fixed-width form.idx into Form 4 rows.

    Each Form 4 is listed once per filer — the issuer AND every reporting
    owner — so the same accession appears several times. The caller filters
    to issuer CIKs it knows and dedupes by accession.
    """
    rows = []
    started = False
    for line in (text or "").splitlines():
        if not started:
            started = line.startswith("-----")
            continue
        match = _INDEX_LINE.match(line.rstrip())
        if not match or match.group("form") not in ("4", "4/A"):
            continue
        path = match.group("path")
        accession = os.path.basename(path).replace(".txt", "")
        rows.append({
            "form": match.group("form"),
            "company": match.group("company").strip(),
            "cik": match.group("cik").lstrip("0") or "0",
            "accession": accession,
        })
    return rows


def fetch_index(date):
    """Download one day's form index. Returns [] on weekends and holidays.

    Fetched with a plain paced request rather than the retrying SEC client:
    a missing index answers 403/404, and the retrying client reads 403 as a
    rate-limit block and parks ALL SEC traffic for minutes. A holiday must
    not stall the whole daily job.
    """
    import requests
    from fetcher import FILING_HEADERS, _sec_gate

    day = datetime.strptime(date, "%Y-%m-%d").date()
    if day.weekday() >= 5:
        return []

    url = DAILY_INDEX_URL.format(year=day.year, qtr=(day.month - 1) // 3 + 1,
                                 ymd=day.strftime("%Y%m%d"))
    _sec_gate()
    try:
        resp = requests.get(url, headers=FILING_HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f"[FORM4] Index fetch failed for {date}: {e}", flush=True)
        return None
    if resp.status_code != 200:
        print(f"[FORM4] No index for {date} (HTTP {resp.status_code}) — holiday or not yet published",
              flush=True)
        return []
    return parse_index(resp.text)


# ---------------------------------------------------------------------------
# One Form 4
# ---------------------------------------------------------------------------

def _text(node, path):
    """Text of a Form 4 leaf. Most leaves wrap their content in <value>."""
    if node is None:
        return None
    found = node.find(path)
    if found is None:
        return None
    inner = found.find("value")
    raw = (inner.text if inner is not None else found.text) or ""
    raw = raw.strip()
    return raw or None


def _flag(node, path):
    """Form 4 booleans appear as "1"/"0" and as "true"/"false"."""
    return (_text(node, path) or "").lower() in ("1", "true")


def _float(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_form4(xml_text):
    """Parse an ownershipDocument into a plain dict. Never raises."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if root.tag != "ownershipDocument":
        root = root.find(".//ownershipDocument") or root

    owner = root.find("reportingOwner")
    relationship = owner.find("reportingOwnerRelationship") if owner is not None else None

    transactions = []
    for table, derivative in (("nonDerivativeTable", False), ("derivativeTable", True)):
        tag = "derivativeTransaction" if derivative else "nonDerivativeTransaction"
        for txn in root.findall(f"{table}/{tag}"):
            transactions.append({
                "derivative": derivative,
                "security": _text(txn, "securityTitle"),
                "date": _text(txn, "transactionDate"),
                "code": (_text(txn, "transactionCoding/transactionCode") or "").upper(),
                "shares": _float(_text(txn, "transactionAmounts/transactionShares")),
                "price": _float(_text(txn, "transactionAmounts/transactionPricePerShare")),
                "acquired": (_text(txn, "transactionAmounts/transactionAcquiredDisposedCode") or "") == "A",
            })

    return {
        "issuer_cik": (_text(root, "issuer/issuerCik") or "").lstrip("0"),
        "issuer_name": _text(root, "issuer/issuerName"),
        "ticker": (_text(root, "issuer/issuerTradingSymbol") or "").upper(),
        "owner_name": _text(owner, "reportingOwnerId/rptOwnerName"),
        "owner_cik": _text(owner, "reportingOwnerId/rptOwnerCik"),
        "is_director": _flag(relationship, "isDirector"),
        "is_officer": _flag(relationship, "isOfficer"),
        "is_ten_percent": _flag(relationship, "isTenPercentOwner"),
        "officer_title": _text(relationship, "officerTitle"),
        "ten_b5_1": _flag(root, "aff10b5One"),
        "period": _text(root, "periodOfReport"),
        "transactions": transactions,
    }


def _display_name(raw):
    """EDGAR writes owners surname-first ("Kanders Warren B"). Leave it — a
    reordering heuristic gets hyphenated and multi-part names wrong, and the
    raw form still matches the grant-cadence keys built from the same data."""
    return " ".join(str(raw or "").split()) or "Unknown insider"


def _role_class(title):
    text = str(title or "").lower()
    if "chief executive" in text or re.search(r"\bceo\b", text):
        return "CEO"
    if "chief financial" in text or re.search(r"\bcfo\b", text):
        return "CFO"
    if "chief accounting" in text:
        return "CAO"
    if "chief operating" in text:
        return "COO"
    if "chief" in text or "president" in text:
        return "OTHER_CSUITE"
    return "OTHER"


def to_facts(parsed):
    """Turn a parsed Form 4 into pipeline facts, or None if it isn't worth
    scoring.

    A filing qualifies with an open-market purchase of at least MIN_BUY_USD
    by an officer or director, or with an equity grant to a top officer.
    """
    if not parsed:
        return None
    insider = parsed["is_officer"] or parsed["is_director"]
    title = parsed.get("officer_title") or ("Director" if parsed["is_director"] else "")
    name = _display_name(parsed.get("owner_name"))

    buys, grants = [], []
    for txn in parsed["transactions"]:
        if txn["code"] == CODE_PURCHASE and txn["acquired"] and not txn["derivative"] and insider:
            value = (txn["shares"] or 0) * (txn["price"] or 0)
            buys.append({"person": name, "title": title, "type": "open_market_buy",
                         "shares": txn["shares"], "value_usd": value,
                         "ten_b5_1": parsed["ten_b5_1"], "date": txn["date"],
                         "note": f"Bought {int(txn['shares'] or 0):,} shares at "
                                 f"${txn['price'] or 0:,.2f} on {txn['date']}"})
        elif (txn["code"] == CODE_GRANT and txn["acquired"]
              and any(w in str(title).lower() for w in TOP_OFFICER_WORDS)):
            grants.append(txn)

    buy_total = sum(b["value_usd"] for b in buys)
    if buy_total < MIN_BUY_USD:
        buys = []
    if not buys and not grants:
        return None

    comp_events = []
    for txn in grants:
        option = txn["derivative"] or "option" in str(txn.get("security") or "").lower()
        comp_events.append({
            "executive": f"{name} ({title})",
            "role_class": _role_class(title),
            "grant_type": txn.get("security") or ("Stock Options" if option else "Stock Award"),
            # An option's "price" is its strike, not what it's worth; only a
            # stock award's price times shares is a dollar value.
            "grant_value_usd": None if option else ((txn["shares"] or 0) * (txn["price"] or 0) or None),
            "share_count": txn["shares"],
            "grant_date": txn["date"],
            "filing_date": txn["date"],
            # Unknown from a Form 4 alone — the company's grant cadence
            # decides whether it is off-cycle.
            "is_annual_cycle": None,
            "grant_rationale": None,
            "recipient_count": 1,
            "hurdle_prices": [],
        })

    subcategories = (["Insider Buy"] if buys else []) + (["Officer Grant"] if comp_events else [])
    return {
        "relevant": True,
        "top_level_category": "Insider Transaction" if buys else "Compensation",
        "subcategories": subcategories,
        "is_complex": False,
        "narrative_summary": None,
        "departures": [], "appointments": [], "other": [],
        "comp_events": comp_events,
        "insider_transactions": buys,
        "filing_flags": {},
    }


def render_text(parsed):
    """A plain-text rendering stored in raw_text, so search and the detail
    page have something to show and the repair queries never see an empty
    body. (Repair queries also exclude FORM4 rows outright.)"""
    lines = [f"Form 4 — {parsed.get('issuer_name')} ({parsed.get('ticker')})",
             f"Reporting person: {parsed.get('owner_name')} — "
             f"{parsed.get('officer_title') or ('Director' if parsed.get('is_director') else '')}",
             f"Rule 10b5-1 plan: {'yes' if parsed.get('ten_b5_1') else 'no'}", ""]
    for txn in parsed.get("transactions", []):
        lines.append(f"{txn['date']}  code {txn['code']}  {txn.get('security') or ''}  "
                     f"{txn['shares'] or 0:,.0f} @ ${txn['price'] or 0:,.2f}  "
                     f"{'acquired' if txn['acquired'] else 'disposed'}")
    return "\n".join(lines)


def fetch_form4(cik, accession):
    """Fetch and parse the XML for one Form 4. Returns (parsed, xml_url)."""
    from fetcher import FILING_HEADERS, _sec_get_with_retry

    folder = f"{ARCHIVE_BASE}/{cik}/{accession.replace('-', '')}"
    try:
        listing = _sec_get_with_retry(f"{folder}/index.json", FILING_HEADERS, timeout=20).json()
    except Exception as e:
        print(f"[FORM4] Folder listing failed for {accession}: {e}", flush=True)
        return None, None

    names = [item.get("name", "") for item in listing.get("directory", {}).get("item", [])]
    xml_names = [n for n in names if n.lower().endswith(".xml") and not n.lower().startswith("xsl")]
    if not xml_names:
        return None, None

    xml_url = f"{folder}/{xml_names[0]}"
    try:
        xml_text = _sec_get_with_retry(xml_url, FILING_HEADERS, timeout=20).text
    except Exception as e:
        print(f"[FORM4] XML fetch failed for {accession}: {e}", flush=True)
        return None, None
    return parse_form4(xml_text), xml_url


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def known_issuers():
    """{cik_without_zeros: (ticker, company)} for companies already in the
    database. Only these are scanned — a Form 4 from an issuer the tool has
    never seen has no 8-K context and no grant history to be read against."""
    from database import get_connection, _dict_rows
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT cik, ticker, company FROM filings "
        "WHERE cik IS NOT NULL AND cik <> '' AND ticker IS NOT NULL AND ticker <> ''"
    )
    issuers = {}
    for row in _dict_rows(cursor.fetchall(), cursor):
        row = dict(row)
        issuers[str(row["cik"]).lstrip("0")] = (row["ticker"], row["company"])
    conn.close()
    return issuers


# How far back an 8-K appointment explains a grant. Inducement and sign-on
# awards are usually granted within days of the start date; 60 days covers a
# delayed grant without excusing the next annual cycle.
APPOINTMENT_LOOKBACK_DAYS = 60


def recent_appointees(cik, as_of):
    """Names appointed at this company in 8-Ks we analyzed recently.

    A Form 4 cannot say WHY a grant was made. Seen on the first live scan: a
    newly hired CFO's inducement options and a new Executive Chair's sign-on
    award both scored as off-cycle, because nothing in the XML marks them as
    hiring packages. The company's own recent 8-K does.
    """
    import json
    from database import get_connection, _placeholder, _dict_rows

    cutoff = (datetime.strptime(as_of, "%Y-%m-%d")
              - timedelta(days=APPOINTMENT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(
        f"SELECT structured_summary FROM filings WHERE cik = {p} AND filed_date >= {p} "
        f"AND structured_summary IS NOT NULL AND COALESCE(source, '8-K') = '8-K'",
        (cik, cutoff),
    )
    names = []
    for row in _dict_rows(cursor.fetchall(), cursor):
        try:
            structured = json.loads(dict(row)["structured_summary"])
        except (ValueError, TypeError, KeyError):
            continue
        for appointment in (structured.get("appointments") or []):
            if isinstance(appointment, dict) and appointment.get("name"):
                names.append({"name": appointment["name"], "title": appointment.get("title")})
    conn.close()
    return names


def scan_day(date, allow_judge=True, dry_run=False, apply_universe=True):
    """Scan one day's Form 4s. Returns a stats dict."""
    from database import filing_exists, insert_filing
    from pipeline import analyze_facts, apply_to_filing

    stats = {"date": date, "index_rows": 0, "known_issuers": 0, "fetched": 0,
             "qualified": 0, "stored": 0, "tokens_in": 0, "tokens_out": 0}

    rows = fetch_index(date)
    if rows is None:
        stats["error"] = "index unavailable"
        return stats
    stats["index_rows"] = len(rows)

    issuers = known_issuers()
    candidates, seen = [], set()
    for row in rows:
        if row["cik"] not in issuers or row["accession"] in seen:
            continue
        seen.add(row["accession"])
        ticker, company = issuers[row["cik"]]
        candidates.append({"accession_no": row["accession"], "cik": row["cik"].zfill(10),
                           "ticker": ticker, "company": company, "filed_date": date})
    stats["known_issuers"] = len(candidates)

    if apply_universe and candidates:
        from universe import screen_filings
        candidates, _ = screen_filings(candidates)

    for candidate in candidates:
        if filing_exists(candidate["accession_no"]):
            continue
        parsed, xml_url = fetch_form4(candidate["cik"].lstrip("0"), candidate["accession_no"])
        stats["fetched"] += 1
        facts = to_facts(parsed)
        if facts is None:
            continue
        stats["qualified"] += 1
        if facts["comp_events"]:
            # Lets OFF_CYCLE_GRANT recognise a hiring package it can't see.
            facts["appointments"] = recent_appointees(candidate["cik"], date)

        filing = dict(candidate)
        filing.update({
            "item_codes": "FORM4",
            "source": "FORM4",
            "filing_url": f"{ARCHIVE_BASE}/{candidate['cik'].lstrip('0')}/"
                          f"{candidate['accession_no'].replace('-', '')}/",
            "filing_document_url": xml_url,
            "raw_text": render_text(parsed),
            "matched_keywords": "form4",
        })

        result = analyze_facts(filing, facts, allow_judge=allow_judge)
        stats["tokens_in"] += result.tokens_in
        stats["tokens_out"] += result.tokens_out
        if not result.detection or not result.detection.signals:
            continue  # a grant in the usual month at the usual size: silence

        apply_to_filing(filing, result)
        label = f"{filing['company']} — {','.join(result.signal_types)} — {filing['top_signal']}"
        if dry_run:
            print(f"  WOULD STORE: {label}", flush=True)
            continue
        if insert_filing(filing):
            stats["stored"] += 1
            print(f"  [FORM4] {label}", flush=True)

    print(f"[FORM4] {date}: {stats}", flush=True)
    return stats


def scan_range(start_date, end_date, allow_judge=True, dry_run=False):
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    results = []
    day = start
    while day <= end:
        results.append(scan_day(day.strftime("%Y-%m-%d"), allow_judge=allow_judge, dry_run=dry_run))
        day += timedelta(days=1)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan Form 4s for insider buys and top-officer grants")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD (start date)")
    parser.add_argument("--end", help="YYYY-MM-DD (end date, inclusive)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args()

    from database import initialize_database
    initialize_database()
    scan_range(args.date, args.end or args.date, allow_judge=not args.no_judge, dry_run=args.dry_run)
