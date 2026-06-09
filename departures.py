"""Executive Departures (24mo) — orchestration.

Pipeline (runs automatically at ingest for departure filings, and on demand
from the filing-detail dropdown):

  1. Fetch the company's 5.02 filings from EDGAR over the last 24 months
     using the existing fetcher.get_edgar_departure_history helper.
  2. For each filing, look up the departure_extractions cache by accession.
  3. For uncached filings, run extract_departures (LLM) in parallel and
     upsert results into the cache.
  4. Aggregate cached + fresh into a single list, sorted newest-first,
     with metadata (accession, filing URL, is-current-filing marker).
  5. render_prose_lines turns the structured list into bullet strings
     for display in the filing detail template.

At ingest, enrich_new_filings() stamps each departure filing with the
deduped 24-month count (dashboard cluster badge) and the full history list
(detail-page card renders instantly, no click needed).

Caches per-filing extractions, so repeat lookups for the same company are
essentially free after the first run, and across companies a 5.02 is
processed exactly once.
"""

import json
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


def _normalize_person(name):
    """Lowercased, whitespace-collapsed name for dedupe grouping."""
    return " ".join((name or "").lower().split())


# Reasons that carry no real information — we'd rather merge in a more specific one.
_GENERIC_REASONS = {"no reason stated", "not stated", "unknown", "none", ""}


def _pick_best_reason(reasons):
    """Choose the most informative reason from a group of duplicates.

    Prefers any specific reason over generic 'no reason stated' filler.
    Among specific reasons, picks the longest (proxy for most detailed).
    """
    specific = [r for r in reasons if r and r.strip().lower() not in _GENERIC_REASONS]
    if specific:
        return max(specific, key=len)
    # Fall back to the first non-empty reason, otherwise None
    non_empty = [r for r in reasons if r]
    return non_empty[0] if non_empty else None


def _pick_best_position(positions):
    """Choose the most specific position (longest non-empty string)."""
    non_empty = [p for p in positions if p]
    return max(non_empty, key=len) if non_empty else None


def _dedupe_departures(rows):
    """Collapse duplicates by normalized person name.

    Rules:
      - Same person across multiple filings → keep the EARLIEST filing's row
        (the canonical announcement) but merge in the best reason/position
        seen across all duplicates.
      - Same person, same filing (LLM extracted them twice for different roles)
        → still collapses into one row via the same merge.
      - If ANY of the merged rows was the current filing, propagate that flag.
      - Error rows (person=None) are never deduped — they pass through as-is so
        the user can still see that a filing's extraction failed.
    """
    if not rows:
        return rows

    groups = {}
    passthrough = []  # error rows / rows without a person name
    order = []  # preserve first-seen order so the result is deterministic
    for r in rows:
        key = _normalize_person(r.get("person"))
        if not key:
            passthrough.append(r)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    merged = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            merged.append(group[0])
            continue
        # Sort earliest first by filing date — that row becomes our canonical entry
        group.sort(key=lambda r: r.get("_filing_date") or "")
        canonical = dict(group[0])
        canonical["reason"] = _pick_best_reason([r.get("reason") for r in group])
        canonical["position"] = _pick_best_position([r.get("position") for r in group])
        # If any merged row was the current filing, keep that marker visible
        canonical["_is_current_filing"] = any(r.get("_is_current_filing") for r in group)
        merged.append(canonical)

    return merged + passthrough


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

    # Collapse duplicates (same person across filings, or LLM extracting one
    # person multiple times within a single 5.02) into one row per person.
    flat = _dedupe_departures(flat)

    # Sort newest first by departure date, falling back to filing date
    flat.sort(key=lambda d: (d.get("date") or d.get("_filing_date") or ""), reverse=True)
    return flat


def count_real_departures(departures):
    """Count deduped departure entries, excluding extraction-error placeholders."""
    return len([d for d in departures if d.get("person") and not d.get("_error")])


def enrich_filing_departure_history(filing_id, cik, accession_no):
    """Fetch the 24-month EDGAR departure history for one filing and persist
    it on the row: departure_count_24mo (deduped person count, powers the
    dashboard cluster badge) and departure_history (JSON list, powers the
    detail-page card without a click).

    Returns the count, or None if the lookup failed (row left untouched so a
    later retry can fill it in).
    """
    from database import update_departure_history

    try:
        deps = get_departures_for_filing(cik=cik, current_accession=accession_no)
    except Exception as e:
        print(f"[DEPARTURES] History lookup failed for {accession_no}: {type(e).__name__}: {e!r}", flush=True)
        return None

    count = count_real_departures(deps)
    update_departure_history(filing_id, count, json.dumps(deps))
    return count


def enrich_new_filings(filings):
    """Post-ingest step: stamp newly stored departure filings with their
    company's 24-month EDGAR departure history.

    Only touches filings that (a) contain at least one departure, (b) have a
    CIK, and (c) aren't already stamped — so re-running a backfill over the
    same date range doesn't re-hit EDGAR. LLM extraction is cached per
    accession, so the marginal cost is mostly the EDGAR fetches.
    """
    from database import get_filing_by_accession

    targets = [
        f for f in filings
        if f.get("cik") and (f.get("departure_count") or 0) > 0
    ]
    if not targets:
        return

    print(f"[DEPARTURES] Gathering 24mo history for {len(targets)} departure filing(s)...", flush=True)

    done = 0
    for f in targets:
        row = get_filing_by_accession(f.get("accession_no", ""))
        if not row:
            continue
        if row.get("departure_count_24mo") is not None:
            continue  # already stamped on a previous run

        count = enrich_filing_departure_history(row["id"], f["cik"], f["accession_no"])
        if count is not None:
            done += 1
            print(f"  [DEPARTURES] {f.get('company', 'Unknown')}: {count} departure(s) in 24mo", flush=True)

    print(f"[DEPARTURES] History stamped on {done} filing(s)", flush=True)


def run_history_backfill(run_id=None, verbose=True):
    """One-time backfill: stamp EDGAR departure history onto existing filings
    that contain departures but were ingested before this feature.

    Computes the per-filing departure count from structured_summary on the
    fly when the departure_count column is NULL (pre-retrofit rows), so this
    works regardless of whether the flags retrofit ran first.

    Returns a stats dict. If run_id is given, closes out that backfill_runs row.
    """
    from database import get_connection, _placeholder, complete_backfill_run
    from summary_utils import count_departures

    stats = {"scanned": 0, "enriched": 0, "skipped_no_departures": 0, "failed": 0}

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, accession_no, cik, company, structured_summary, departure_count
        FROM filings
        WHERE cik IS NOT NULL AND cik != ''
          AND departure_count_24mo IS NULL
          AND structured_summary IS NOT NULL
        ORDER BY filed_date DESC
        """
    )
    columns = [desc[0] for desc in cursor.description]
    rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
    conn.close()

    if verbose:
        print(f"[DEPARTURES BACKFILL] Scanning {len(rows)} candidate filings...", flush=True)

    for row in rows:
        stats["scanned"] += 1

        dep_count = row.get("departure_count")
        if dep_count is None:
            dep_count = count_departures(row.get("structured_summary"))
        if not dep_count:
            stats["skipped_no_departures"] += 1
            continue

        count = enrich_filing_departure_history(row["id"], row["cik"], row["accession_no"])
        if count is None:
            stats["failed"] += 1
        else:
            stats["enriched"] += 1
            if verbose:
                print(f"  [DEPARTURES BACKFILL] {row.get('company', 'Unknown')}: {count} in 24mo "
                      f"({stats['enriched']} done)", flush=True)

    if verbose:
        print(f"[DEPARTURES BACKFILL] Done. Scanned {stats['scanned']}, enriched {stats['enriched']}, "
              f"no-departures {stats['skipped_no_departures']}, failed {stats['failed']}", flush=True)

    if run_id is not None:
        try:
            complete_backfill_run(
                run_id,
                fetched=stats["scanned"],
                filtered=stats["enriched"] + stats["failed"],
                new=stats["enriched"],
                skipped=stats["skipped_no_departures"],
                status="completed",
            )
        except Exception as e:
            if verbose:
                print(f"[DEPARTURES BACKFILL] WARN: could not mark run complete: {e}", flush=True)

    return stats


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
