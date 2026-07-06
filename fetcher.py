# fetcher.py — Pulls 8-K filings from SEC EDGAR full-text search API
# Uses the free EFTS (EDGAR Full-Text Search) API at efts.sec.gov

import random
import requests
import time
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from config import USER_AGENT, TARGET_ITEM_CODES, REQUEST_DELAY, RESULTS_PER_PAGE

# How many times to retry a SEC request that comes back 429 (rate limited)
# or a transient 5xx before giving up. Each retry uses exponential backoff
# with jitter. EDGAR occasionally throws one-off 500s that succeed on retry.
SEC_MAX_RETRIES = 3

# HTTP statuses worth retrying: rate limits and transient server errors.
# Other 4xx (bad request, not found) are permanent and fail immediately.
SEC_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def strip_cover_page(text):
    """Remove SEC cover page boilerplate from the beginning of filing text.

    Every 8-K filing starts with a cover page that has the company name,
    CIK, checkboxes for filer status (emerging growth, accelerated filer, etc.),
    and other form metadata. The actual content starts at "Item X.XX".

    This function finds the first Item reference and strips everything before it,
    so the summarizer only sees the real filing content.

    Args:
        text: Full plain text of the filing (already extracted from HTML)

    Returns:
        Text with cover page removed, or original text if no Item marker found
    """
    # Look for the first "Item X.XX" pattern — that's where real content begins
    # Matches patterns like "Item 5.02", "Item 1.01", "ITEM 8.01", etc.
    item_match = re.search(r'\bItem\s+\d+\.\d{2}\b', text, re.IGNORECASE)
    if item_match:
        # Keep everything from the Item marker onward
        return text[item_match.start():]

    # No Item marker found — return original text as-is (unusual but possible)
    return text


# Headers for the EFTS search API. Uses the SEC-compliant USER_AGENT — a fake
# browser UA gets throttled harder by SEC's edge tier than an identifying one.
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.sec.gov",
    "Referer": "https://www.sec.gov/",
}

# Headers for fetching actual filing documents from sec.gov
FILING_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html",
}


def _sec_get_with_retry(url, headers, timeout=30, max_retries=SEC_MAX_RETRIES, params=None):
    """GET a SEC URL, retrying on 429/transient-5xx with exponential backoff + jitter.

    Honors the `Retry-After` response header when SEC sends one (sometimes a
    number of seconds, sometimes an HTTP-date — we only handle the seconds form
    since that's what SEC actually uses).

    Returns the requests.Response on success. Raises the last RequestException
    if every attempt fails (so callers can keep their existing try/except).
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, params=params)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            last_exc = e
            # Retry rate limits and transient server errors — other 4xx are permanent
            if status not in SEC_RETRYABLE_STATUSES or attempt == max_retries:
                raise
            # Prefer SEC's Retry-After hint if present; otherwise exponential backoff
            retry_after = None
            if e.response is not None:
                ra_header = e.response.headers.get("Retry-After")
                if ra_header:
                    try:
                        retry_after = float(ra_header)
                    except ValueError:
                        retry_after = None
            backoff = retry_after if retry_after is not None else (2 ** attempt)
            # Jitter spreads concurrent retries so they don't pile on at once
            sleep_for = backoff + random.uniform(0, 0.5)
            print(f"  SEC {status} on attempt {attempt + 1}/{max_retries + 1} — sleeping {sleep_for:.1f}s before retry: {url}", flush=True)
            time.sleep(sleep_for)
        except requests.exceptions.RequestException as e:
            # Connection errors, timeouts, etc. — also worth one retry
            last_exc = e
            if attempt == max_retries:
                raise
            sleep_for = (2 ** attempt) + random.uniform(0, 0.5)
            print(f"  SEC request failed on attempt {attempt + 1}/{max_retries + 1} ({type(e).__name__}) — sleeping {sleep_for:.1f}s: {url}", flush=True)
            time.sleep(sleep_for)
    # Defensive — loop above always returns or raises, but keeps type-checkers happy
    raise last_exc if last_exc else RuntimeError("unreachable")

# The EDGAR full-text search endpoint (free, no API key needed)
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

# --- Exhibit fetching ---
# The main 8-K body often just references its exhibits — the real signal
# (separation agreements with comp terms, resignation letters, press releases)
# lives in them. We pull the highest-value exhibit types into the text the LLM
# sees, in priority order, with size caps to bound cost.
#   EX-17 — resignation letters (rare, but pure signal on why someone left)
#   EX-10 — material agreements (employment/separation terms, grant values, hurdles)
#   EX-99 — press releases (narrative context the 8-K body omits)
EXHIBIT_PRIORITY = ("EX-17", "EX-10", "EX-99")
MAX_EXHIBITS_PER_FILING = 4
MAX_EXHIBIT_CHARS = 30_000       # per exhibit
MAX_FILING_TEXT_CHARS = 120_000  # main doc + all exhibits combined (~30k tokens)


def _exhibit_sort_key(doc_type):
    """Rank an exhibit type by EXHIBIT_PRIORITY (lower = fetched first)."""
    upper = doc_type.upper()
    for rank, prefix in enumerate(EXHIBIT_PRIORITY):
        if upper.startswith(prefix):
            return rank
    return len(EXHIBIT_PRIORITY)


def _html_to_text(html):
    """Extract whitespace-normalized plain text from a filing HTML document."""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r'\s+', ' ', text)


def search_8k_filings(start_date, end_date, page=0):
    """Search EDGAR for 8-K filings within a date range.

    Uses GET request with query parameters (POST is blocked by SEC).

    Args:
        start_date: Start date as "YYYY-MM-DD" string
        end_date: End date as "YYYY-MM-DD" string
        page: Which page of results (0-indexed, 100 results per page)

    Returns:
        Dictionary with 'total' count and list of 'hits' (filing metadata)

    Raises:
        requests.exceptions.RequestException if the search fails after
        retries. Errors must propagate — swallowing them here made failed
        backfills look like successful runs with 0 results.
    """
    params = {
        "forms": "8-K",              # Only 8-K filings
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "from": page * RESULTS_PER_PAGE,
    }

    try:
        response = _sec_get_with_retry(SEARCH_URL, REQUEST_HEADERS, timeout=30, params=params)
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"  Error searching EDGAR (after retries): {e}", flush=True)
        raise

    total = data.get("hits", {}).get("total", {}).get("value", 0)
    hits = data.get("hits", {}).get("hits", [])

    return {"total": total, "hits": hits}


def parse_filing_metadata(hit):
    """Extract useful fields from a single EDGAR search result.

    Note: The API returns one hit per FILE within a filing (exhibits, main doc, etc).
    Multiple hits can share the same accession number. We deduplicate later.

    Args:
        hit: One item from the search API's 'hits' array

    Returns:
        Dictionary with cleaned-up filing metadata, or None if parsing fails
    """
    try:
        source = hit.get("_source", {})
        hit_id = hit.get("_id", "")

        # Accession number — unique ID for each filing
        accession_no = source.get("adsh", "")
        if not accession_no and ":" in hit_id:
            accession_no = hit_id.split(":")[0]

        # CIK is in an array
        ciks = source.get("ciks", [])
        cik = ciks[0] if ciks else ""

        # Company name — display_names format: "Company Name  (TICKER)  (CIK 0001855644)"
        display_names = source.get("display_names", [])
        company_name = ""
        ticker = ""
        if display_names:
            full_name = display_names[0]
            company_name = full_name

            # Extract ticker from parentheses — handles both "(AAPL)" and "(AXL, DCH)"
            # The [\),] at the end matches either closing paren or comma,
            # so we grab the first ticker when multiple are listed
            ticker_match = re.search(r'\(([A-Z]{1,5})[\),]', full_name)
            if ticker_match:
                ticker = ticker_match.group(1)

            # Fallback: if EDGAR didn't include a ticker, look it up by CIK
            # using SEC's master company_tickers.json (cached locally)
            if not ticker and cik:
                from cik_lookup import get_ticker_by_cik
                ticker = get_ticker_by_cik(cik)
                if ticker:
                    print(f"    CIK lookup found ticker: {ticker} for {company_name}")

            # Clean company name — remove (TICKER) and (CIK ...) parts
            company_name = re.sub(r'\s*\([^)]*\)\s*', ' ', full_name).strip()

        filed_date = source.get("file_date", "")
        items = source.get("items", [])  # Item codes like ["5.02", "1.01"]

        # root_forms is an array in this API
        root_forms = source.get("root_forms", [])
        if root_forms and root_forms[0] not in ["8-K", "8-K/A"]:
            return None

        # Build the URL to the filing index page on SEC.gov
        # CIK in URLs has leading zeros stripped
        cik_stripped = cik.lstrip("0") or "0"
        accession_no_clean = accession_no.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{accession_no_clean}/{accession_no}-index.htm"

        return {
            "accession_no": accession_no,
            "company": company_name,
            "ticker": ticker,
            "cik": cik,
            "filed_date": filed_date,
            "item_codes": ",".join(items) if items else "",
            "filing_url": filing_url,
            "items_list": items,
        }

    except Exception as e:
        print(f"  Error parsing filing metadata: {e}")
        return None


def fetch_filing_text(filing_url, cik, accession_no):
    """Download and extract the text content from an 8-K filing.

    First fetches the filing index page to find the main document,
    then downloads and parses that document.

    Args:
        filing_url: URL to the filing's index page on SEC.gov
        cik: Company's CIK number
        accession_no: Filing's accession number

    Returns:
        Tuple of (text, doc_url): plain text content and the primary
        document URL. Returns ("", None) on failure.
    """
    try:
        # First, get the index page to find the actual 8-K document.
        # _sec_get_with_retry handles 429s with exponential backoff + Retry-After.
        time.sleep(REQUEST_DELAY)
        response = _sec_get_with_retry(filing_url, FILING_HEADERS, timeout=30)

        soup = BeautifulSoup(response.text, "html.parser")

        # Walk the filing index table once, collecting the main 8-K document
        # link AND the high-value exhibits (EX-17 / EX-10 / EX-99).
        doc_url = None
        exhibit_links = []  # (doc_type, url)
        table = soup.find("table", class_="tableFile")
        if table:
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue
                doc_type = cells[3].get_text(strip=True)
                link = cells[2].find("a")
                if not (link and link.get("href")):
                    continue
                href = link["href"]
                # Some links use the inline XBRL viewer: /ix?doc=/Archives/...
                # We need the direct URL, so strip the /ix?doc= prefix
                if "/ix?doc=" in href:
                    href = href.split("/ix?doc=")[-1]
                # Only text-bearing documents — skip graphics, XBRL, zips
                if not href.lower().endswith((".htm", ".html", ".txt")):
                    continue
                url = "https://www.sec.gov" + href if href.startswith("/") else href
                if doc_type in ("8-K", "8-K/A"):
                    if doc_url is None:
                        doc_url = url
                elif doc_type.upper().startswith(EXHIBIT_PRIORITY):
                    exhibit_links.append((doc_type, url))

        if not doc_url:
            # Fallback: look for any .htm link in the filing
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if accession_no.replace("-", "") in href and href.endswith((".htm", ".html")):
                    doc_url = "https://www.sec.gov" + href if href.startswith("/") else href
                    break

        if not doc_url:
            # Index page parsed but no 8-K doc link found — log so it doesn't
            # silently turn into a blank-summary row with no trace in the logs.
            print(f"  WARN: could not find 8-K document link in index page: {filing_url}", flush=True)
            return "", None

        # Now fetch the actual filing document — same retry treatment.
        time.sleep(REQUEST_DELAY)
        doc_response = _sec_get_with_retry(doc_url, FILING_HEADERS, timeout=30)

        text = _html_to_text(doc_response.text)

        # Strip the SEC cover page — it's all boilerplate before the actual content.
        # The real 8-K content starts at "Item X.XX" (e.g., "Item 5.02", "Item 1.01").
        # Everything before that is filer info, checkboxes, and form headers.
        text = strip_cover_page(text)

        # Append high-value exhibits, best-signal types first. A failed exhibit
        # fetch never fails the filing — the main document text still returns.
        exhibit_links.sort(key=lambda e: _exhibit_sort_key(e[0]))
        sections = [text]
        total_chars = len(text)
        for ex_type, ex_url in exhibit_links[:MAX_EXHIBITS_PER_FILING]:
            if total_chars >= MAX_FILING_TEXT_CHARS:
                break
            if ex_url == doc_url:
                continue
            try:
                time.sleep(REQUEST_DELAY)
                ex_response = _sec_get_with_retry(ex_url, FILING_HEADERS, timeout=30)
                ex_text = _html_to_text(ex_response.text)
            except requests.exceptions.RequestException as e:
                print(f"  WARN: exhibit fetch failed ({ex_type}): {e}", flush=True)
                continue
            if not ex_text:
                continue
            if len(ex_text) > MAX_EXHIBIT_CHARS:
                ex_text = ex_text[:MAX_EXHIBIT_CHARS] + " [exhibit truncated]"
            section = f"===== EXHIBIT {ex_type} =====\n{ex_text}"
            remaining = MAX_FILING_TEXT_CHARS - total_chars
            if len(section) > remaining:
                section = section[:remaining] + " [truncated]"
            sections.append(section)
            total_chars += len(section)
        text = "\n\n".join(sections)

        return text, doc_url

    except requests.exceptions.RequestException as e:
        print(f"  Error fetching filing text: {e}")
        return "", None


def fetch_filings(start_date, end_date, max_filings=None):
    """Main function: fetch all 8-K filings in a date range.

    Handles pagination and deduplication (the API returns one hit per file
    within a filing, so the same accession number can appear multiple times).

    Args:
        start_date: Start date as "YYYY-MM-DD"
        end_date: End date as "YYYY-MM-DD"
        max_filings: Optional cap on how many filings to return (None = all)

    Returns:
        List of filing metadata dictionaries (deduplicated by accession number)
    """
    print(f"Fetching 8-K filings from {start_date} to {end_date}...", flush=True)

    seen_accessions = set()  # Track accession numbers to avoid duplicates
    all_filings = []
    page = 0

    while True:
        if page > 0:
            time.sleep(REQUEST_DELAY)

        result = search_8k_filings(start_date, end_date, page=page)
        total = result["total"]
        hits = result["hits"]

        if page == 0:
            print(f"  Found {total} total results (includes duplicates from exhibits)", flush=True)

        if not hits:
            break

        for hit in hits:
            metadata = parse_filing_metadata(hit)
            if metadata and metadata["accession_no"] not in seen_accessions:
                seen_accessions.add(metadata["accession_no"])
                all_filings.append(metadata)

        print(f"  Processed page {page + 1} ({len(all_filings)} unique filings so far)", flush=True)

        # Check if we've gotten all results or hit our limit
        if (page + 1) * RESULTS_PER_PAGE >= total:
            break
        if max_filings and len(all_filings) >= max_filings:
            all_filings = all_filings[:max_filings]
            break

        page += 1

    print(f"  Done! Retrieved {len(all_filings)} unique filings", flush=True)
    return all_filings


def fetch_daily():
    """Convenience function: fetch yesterday's 8-K filings.
    Called by the scheduler for daily updates."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    return fetch_filings(yesterday, today)


# Cap on the Item 5.02 section text kept for LLM extraction. Full sections
# are usually 1-4k chars; the cap only guards against pathological documents.
MAX_502_SECTION_CHARS = 6000

# The signature block reliably starts with this phrase — a safe end marker.
_SIGNATURE_BLOCK_RE = re.compile(
    r"Pursuant\s+to\s+the\s+requirements\s+of\s+the\s+Securities\s+Exchange\s+Act",
    re.IGNORECASE,
)


def _fetch_502_snippet(cik, accession_no, primary_doc):
    """Fetch the full Item 5.02 section text from a filing.

    Downloads the filing HTML, extracts text, and returns the Item 5.02
    section — from the "Item 5.02" heading to the next different Item
    heading or the signature block, capped at MAX_502_SECTION_CHARS.

    This used to grab only the first ~800 characters, which silently
    dropped later departures in multi-executive filings and undercounted
    the departure clusters the dashboard badge is built on.

    Returns:
        String section text, or empty string on failure.
    """
    acc_nodash = accession_no.replace("-", "")
    # CIK in the URL path should not have leading zeros
    cik_stripped = cik.lstrip("0") or "0"
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_nodash}/{primary_doc}"

    try:
        resp = requests.get(url, headers=FILING_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)

        match = re.search(r"Item\s+5\.02", text, re.IGNORECASE)
        if not match:
            return ""

        section = text[match.start():]
        heading_len = match.end() - match.start()

        # End at the next DIFFERENT item heading. In-prose references to
        # "Item 5.02(e)" etc. must not truncate the section early.
        end = None
        for m2 in re.finditer(r"\bItem\s+(\d+\.\d{2})\b", section[heading_len:], re.IGNORECASE):
            if m2.group(1) != "5.02":
                end = heading_len + m2.start()
                break

        # ...or at the signature block, whichever comes first.
        sig = _SIGNATURE_BLOCK_RE.search(section)
        if sig and (end is None or sig.start() < end):
            end = sig.start()

        if end:
            section = section[:end]
        section = section[:MAX_502_SECTION_CHARS]

        # Trim to last complete sentence
        last_period = section.rfind(".")
        if last_period > 200:
            section = section[:last_period + 1]
        return section
    except Exception as e:
        print(f"  Failed to fetch 5.02 snippet from {url}: {e}")

    return ""


def get_edgar_departure_history(cik, exclude_accession="", months=12):
    """Query EDGAR directly for other 5.02 filings from the same company.

    Uses SEC's company submissions API (data.sec.gov) to look back up to
    12 months for departure/management change filings, then fetches each
    filing to extract who departed and their role.

    Args:
        cik: The company's CIK number (e.g., "0000796343")
        exclude_accession: Accession number to skip (the current filing)
        months: How far back to look (default 12)

    Returns:
        List of dicts: [{filing_date, items, accession_no, snippet}, ...]
        Empty list means "no matching filings". Returns None when the EDGAR
        lookup itself failed — callers MUST distinguish the two, otherwise a
        transient network error gets recorded as "zero departures" and the
        cluster signal is permanently suppressed.
    """
    if not cik:
        return []

    # SEC API expects CIK zero-padded to 10 digits
    padded_cik = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"

    try:
        resp = requests.get(url, headers=FILING_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  EDGAR departure history lookup failed: {e}")
        return None

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    items_list = recent.get("items", [])
    primary_docs = recent.get("primaryDocument", [])

    cutoff = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")

    # Normalize the accession we want to skip (EDGAR uses dashes, our DB may not)
    skip = exclude_accession.replace("-", "")

    # First pass: identify all 5.02 filings within the lookback period
    matches = []
    for i in range(len(forms)):
        if forms[i] not in ("8-K", "8-K/A"):
            continue
        if dates[i] < cutoff:
            break  # Dates are sorted newest-first, so we can stop early
        item_str = items_list[i] if i < len(items_list) else ""
        if "5.02" not in item_str:
            continue
        if accessions[i].replace("-", "") == skip:
            continue
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        matches.append({
            "filing_date": dates[i],
            "items": item_str,
            "accession_no": accessions[i],
            "primary_doc": primary_doc,
        })

    # Second pass: fetch each filing to extract departure details
    results = []
    for m in matches:
        snippet = ""
        if m["primary_doc"]:
            snippet = _fetch_502_snippet(padded_cik, m["accession_no"], m["primary_doc"])
            time.sleep(0.2)  # Respect SEC rate limits
        results.append({
            "filing_date": m["filing_date"],
            "items": m["items"],
            "accession_no": m["accession_no"],
            "snippet": snippet,
        })

    return results


# When run directly, do a quick test fetch
if __name__ == "__main__":
    filings = fetch_filings("2026-01-20", "2026-01-28", max_filings=10)
    for f in filings:
        print(f"  {f['filed_date']} | {f['company']} ({f['ticker']}) | Items: {f['item_codes']}")
