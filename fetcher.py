# fetcher.py — Pulls 8-K filings from SEC EDGAR full-text search API
# Uses the free EFTS (EDGAR Full-Text Search) API at efts.sec.gov

import requests
import time
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from config import USER_AGENT, TARGET_ITEM_CODES, REQUEST_DELAY, RESULTS_PER_PAGE


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


# Headers for the EFTS search API — needs browser-like headers to work
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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

# The EDGAR full-text search endpoint (free, no API key needed)
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


def search_8k_filings(start_date, end_date, page=0):
    """Search EDGAR for 8-K filings within a date range.

    Uses GET request with query parameters (POST is blocked by SEC).

    Args:
        start_date: Start date as "YYYY-MM-DD" string
        end_date: End date as "YYYY-MM-DD" string
        page: Which page of results (0-indexed, 100 results per page)

    Returns:
        Dictionary with 'total' count and list of 'hits' (filing metadata)
    """
    params = {
        "forms": "8-K",              # Only 8-K filings
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "from": page * RESULTS_PER_PAGE,
    }

    try:
        response = requests.get(SEARCH_URL, headers=REQUEST_HEADERS, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        total = data.get("hits", {}).get("total", {}).get("value", 0)
        hits = data.get("hits", {}).get("hits", [])

        return {"total": total, "hits": hits}

    except requests.exceptions.RequestException as e:
        print(f"  Error searching EDGAR: {e}", flush=True)
        return {"total": 0, "hits": []}


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
        Plain text content of the filing, or empty string on failure
    """
    try:
        # First, get the index page to find the actual 8-K document
        time.sleep(REQUEST_DELAY)
        response = requests.get(filing_url, headers=FILING_HEADERS, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Look for the main 8-K document link in the filing index table
        doc_url = None
        table = soup.find("table", class_="tableFile")
        if table:
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 4:
                    doc_type = cells[3].get_text(strip=True)
                    if doc_type in ["8-K", "8-K/A"]:
                        link = cells[2].find("a")
                        if link and link.get("href"):
                            href = link["href"]
                            # Some links use the inline XBRL viewer: /ix?doc=/Archives/...
                            # We need the direct URL, so strip the /ix?doc= prefix
                            if "/ix?doc=" in href:
                                href = href.split("/ix?doc=")[-1]
                            doc_url = "https://www.sec.gov" + href
                            break

        if not doc_url:
            # Fallback: look for any .htm link in the filing
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if accession_no.replace("-", "") in href and href.endswith((".htm", ".html")):
                    doc_url = "https://www.sec.gov" + href if href.startswith("/") else href
                    break

        if not doc_url:
            return ""

        # Now fetch the actual filing document
        time.sleep(REQUEST_DELAY)
        doc_response = requests.get(doc_url, headers=FILING_HEADERS, timeout=30)
        doc_response.raise_for_status()

        # Parse HTML and extract just the text
        doc_soup = BeautifulSoup(doc_response.text, "html.parser")
        for element in doc_soup(["script", "style"]):
            element.decompose()

        text = doc_soup.get_text(separator=" ", strip=True)
        text = re.sub(r'\s+', ' ', text)

        # Strip the SEC cover page — it's all boilerplate before the actual content.
        # The real 8-K content starts at "Item X.XX" (e.g., "Item 5.02", "Item 1.01").
        # Everything before that is filer info, checkboxes, and form headers.
        text = strip_cover_page(text)

        return text

    except requests.exceptions.RequestException as e:
        print(f"  Error fetching filing text: {e}")
        return ""


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


def _fetch_502_snippet(cik, accession_no, primary_doc):
    """Fetch the Item 5.02 section from a filing and return a brief snippet.

    Downloads the filing HTML, extracts text, and grabs the first ~800
    characters after "Item 5.02" — enough to capture who departed,
    their title, and the basic circumstances.

    Returns:
        String snippet, or empty string on failure.
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

        # Find the Item 5.02 section and grab enough to identify who/what
        match = re.search(r"Item\s+5\.02", text, re.IGNORECASE)
        if match:
            # Skip the standard header ("Departure of Directors or Certain Officers...")
            # and grab the substance
            snippet = text[match.start():match.start() + 800]
            # Trim to last complete sentence
            last_period = snippet.rfind(".")
            if last_period > 200:
                snippet = snippet[:last_period + 1]
            return snippet
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
        Returns empty list on failure.
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
        return []

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
