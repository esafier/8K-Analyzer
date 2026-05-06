"""Executive Departures (24mo) — orchestration.

Pipeline (per click on the dropdown option):

  1. Fetch the company's 5.02 filings from EDGAR over the last 24 months
     using the existing fetcher.get_edgar_departure_history helper.
  2. For each filing, look up the departure_extractions cache by accession.
  3. For uncached filings, run extract_departures (LLM) in parallel and
     upsert results into the cache.
  4. Aggregate cached + fresh into a single list, sorted newest-first,
     with metadata (accession, filing URL, is-current-filing marker).
  5. render_prose_lines turns the structured list into bullet strings
     for display in the filing detail template.

Caches per-filing extractions, so re-clicks for the same company are essentially
free after the first run, and across companies a 5.02 is processed exactly once.
"""

from concurrent.futures import ThreadPoolExecutor
from html import escape

from database import get_cached_departure_extraction, upsert_departure_extraction
from fetcher import get_edgar_departure_history
from llm import extract_departures

MAX_PARALLEL_EXTRACTIONS = 5
MAX_FILINGS = 20  # safety cap — prevents runaway cost on serial-filer CIKs


def _direct_filing_url(cik, accession_no):
    """The direct filing index page (lists all docs in this filing)."""
    cik_stripped = (cik or "").lstrip("0") or "0"
    acc_nodash = (accession_no or "").replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_nodash}/"


def _normalize_accession(s):
    """Strip dashes for comparison (EDGAR uses 0001234-25-000001; some places strip)."""
    return (s or "").replace("-", "")


def get_departures_for_filing(cik, current_accession):
    """Return a flat, newest-first list of departure entries for this CIK
    over the last 24 months.

    Each entry shape:
        {
            "date":       "YYYY-MM-DD",
            "person":     "Full Name" or None on extraction error,
            "position":   "Title" or None,
            "reason":     "..." or None,
            "_accession": "0001234-25-000123",
            "_filing_url": "https://www.sec.gov/Archives/edgar/data/...",
            "_filing_date": "YYYY-MM-DD",
            "_is_current_filing": bool,
            "_error": bool,
        }
    """
    if not cik:
        return []

    history = get_edgar_departure_history(cik, exclude_accession="", months=24)
    if not history:
        return []

    history = history[:MAX_FILINGS]

    # Split into cached hits (skip LLM) and misses (need LLM)
    cached_results = {}
    needs_extraction = []
    for item in history:
        accession = item["accession_no"]
        cached = get_cached_departure_extraction(accession)
        if cached is not None:
            cached_results[accession] = cached
        else:
            needs_extraction.append(item)

    # Run LLM in parallel for all cache misses
    fresh_results = {}
    if needs_extraction:
        def _do_extract(item):
            try:
                snippet = item.get("snippet") or ""
                filed_date = item.get("filing_date") or ""
                if not snippet:
                    # No text to extract from — cache the failure so we don't retry forever
                    upsert_departure_extraction(
                        item["accession_no"], cik, filed_date,
                        extractions=[], has_error=True,
                    )
                    return item["accession_no"], {"departures": [], "error": True}

                result = extract_departures(snippet, filed_date)
                upsert_departure_extraction(
                    item["accession_no"], cik, filed_date,
                    extractions=result["departures"], has_error=result["error"],
                )
                return item["accession_no"], result
            except Exception as e:
                print(f"[DEPARTURES] Failed to process {item.get('accession_no')}: {type(e).__name__}: {e!r}", flush=True)
                return item.get("accession_no", ""), {"departures": [], "error": True}

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_EXTRACTIONS) as pool:
            for accession_no, result in pool.map(_do_extract, needs_extraction):
                fresh_results[accession_no] = result

    # Aggregate into flat list, one row per departure (a single 5.02 can name multiple people)
    flat = []
    current_norm = _normalize_accession(current_accession)
    for item in history:
        accession = item["accession_no"]
        filing_date = item["filing_date"]
        is_current = (_normalize_accession(accession) == current_norm)
        filing_url = _direct_filing_url(cik, accession)

        if accession in cached_results:
            row = cached_results[accession]
            departures = row.get("extractions") or []
            had_error = bool(row.get("has_error"))
        else:
            r = fresh_results.get(accession, {"departures": [], "error": True})
            departures = r.get("departures") or []
            had_error = bool(r.get("error"))

        if had_error and not departures:
            # Keep a placeholder row so the user can see the filing existed
            flat.append({
                "date": filing_date,
                "person": None, "position": None, "reason": None,
                "_accession": accession, "_filing_url": filing_url,
                "_filing_date": filing_date,
                "_is_current_filing": is_current,
                "_error": True,
            })
            continue

        if not departures:
            # Extraction ran but found no departure events — skip silently
            continue

        for dep in departures:
            flat.append({
                "date": dep.get("date") or filing_date,
                "person": dep.get("person"),
                "position": dep.get("position"),
                "reason": dep.get("reason"),
                "_accession": accession,
                "_filing_url": filing_url,
                "_filing_date": filing_date,
                "_is_current_filing": is_current,
                "_error": False,
            })

    # Sort newest first by departure date, falling back to filing date
    flat.sort(key=lambda d: (d.get("date") or d.get("_filing_date") or ""), reverse=True)
    return flat


def render_prose_lines(departures):
    """Turn a list of departure entries into bullet strings (HTML-ready prose).

    Each line looks like:
        <li><strong>2025-09-12</strong> — Jane Doe, CFO. Resigned to pursue other opportunities. (<a href="...">filing</a>)</li>

    Returns a list of pre-formatted strings; the template renders them with the `safe` filter.
    """
    lines = []
    for d in departures:
        date = escape(d.get("date") or d.get("_filing_date") or "Unknown date")
        url = escape(d.get("_filing_url") or "", quote=True)
        marker = " (this filing)" if d.get("_is_current_filing") else ""

        if d.get("_error"):
            line = (
                f"<li><strong>{date}</strong> — (extraction failed; "
                f"<a href=\"{url}\" target=\"_blank\" rel=\"noopener\">open filing</a>){marker}</li>"
            )
        else:
            person = escape(d.get("person") or "Unknown")
            position = escape(d.get("position") or "Unknown role")
            reason_raw = d.get("reason") or "no reason stated"
            # Ensure reason ends with sentence-ending punctuation before escaping
            if not reason_raw.rstrip().endswith((".", "!", "?")):
                reason_raw = reason_raw.rstrip() + "."
            reason = escape(reason_raw)
            line = (
                f"<li><strong>{date}</strong> — {person}, {position}. "
                f"{reason} (<a href=\"{url}\" target=\"_blank\" rel=\"noopener\">filing</a>){marker}</li>"
            )
        lines.append(line)
    return lines
