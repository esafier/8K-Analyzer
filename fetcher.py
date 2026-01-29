# fetcher.py — Pulls 8-K filings from SEC EDGAR full-text search API
# Uses the free EFTS (EDGAR Full-Text Search) API at efts.sec.gov

import requests
import time
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from config import USER_AGENT, TARGET_ITEM_CODES, REQUEST_DELAY, RESULTS_PER_PAGE


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
        print(f"  Error searching EDGAR: {e}")
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

            # Extract ticker from parentheses (look for 1-5 uppercase letters)
            ticker_match = re.search(r'\(([A-Z]{1,5})\)', full_name)
            if ticker_match:
                ticker = ticker_match.group(1)

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
    print(f"Fetching 8-K filings from {start_date} to {end_date}...")

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
            print(f"  Found {total} total results (includes duplicates from exhibits)")

        if not hits:
            break

        for hit in hits:
            metadata = parse_filing_metadata(hit)
            if metadata and metadata["accession_no"] not in seen_accessions:
                seen_accessions.add(metadata["accession_no"])
                all_filings.append(metadata)

        print(f"  Processed page {page + 1} ({len(all_filings)} unique filings so far)")

        # Check if we've gotten all results or hit our limit
        if (page + 1) * RESULTS_PER_PAGE >= total:
            break
        if max_filings and len(all_filings) >= max_filings:
            all_filings = all_filings[:max_filings]
            break

        page += 1

    print(f"  Done! Retrieved {len(all_filings)} unique filings")
    return all_filings


def fetch_daily():
    """Convenience function: fetch yesterday's 8-K filings.
    Called by the scheduler for daily updates."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    return fetch_filings(yesterday, today)


# When run directly, do a quick test fetch
if __name__ == "__main__":
    filings = fetch_filings("2026-01-20", "2026-01-28", max_filings=10)
    for f in filings:
        print(f"  {f['filed_date']} | {f['company']} ({f['ticker']}) | Items: {f['item_codes']}")
