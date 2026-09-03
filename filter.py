# filter.py — Two-stage filtering for 8-K filings
# Stage 1: Filter by item codes (fast, metadata-only check)
# Stage 2: Keyword scan on filing text (more thorough)

import json
from config import TARGET_ITEM_CODES, KEYWORD_CATEGORIES, SUB_CATEGORIES
from pipeline import analyze_filing, apply_to_filing
from summarizer import extract_summary

# Common SEC boilerplate phrases that contain our keywords but aren't relevant.
# If a keyword match is ONLY found inside these phrases, we skip it.
BOILERPLATE_PHRASES = [
    "elected not to use the extended transition period",
    "emerging growth company",
    "check mark if the registrant has elected",
    "transition period for complying",
    "election with respect to",
    "terminated in accordance with its terms",
    "terminated upon completion",
    "appointed as agent",
]


def _build_legacy_summary(llm_result):
    """Build a short display summary for older templates/emails from v3 output.

    Uses `or []` (not default arg) because the LLM sometimes emits explicit null
    for event arrays instead of [], which would make `.get(key, [])` return None.
    """
    narrative = llm_result.get("narrative_summary")
    if narrative:
        return narrative

    parts = []
    for d in (llm_result.get("departures") or [])[:2]:
        parts.append(f"{d.get('name')} ({d.get('title')}) — {d.get('stated_reason') or 'departure'}")
    for a in (llm_result.get("appointments") or [])[:2]:
        parts.append(f"{a.get('name')} appointed {a.get('title')}")
    for c in (llm_result.get("comp_events") or [])[:1]:
        parts.append(f"Comp: {c.get('executive')} — {c.get('grant_type')} {c.get('grant_value') or ''}")
    # Include other[] bullets so insider-transaction / role-change / edge-case filings
    # still get a sensible legacy summary (used by emails, watchlist cards, DB search).
    for o in (llm_result.get("other") or []):
        parts.append(str(o))
        if len(parts) >= 4:
            break
    return "; ".join(parts) if parts else (llm_result.get("summary") or "")


def stage1_item_code_filter(filing_metadata):
    """Stage 1: Check if the filing has any of our target item codes.

    This is a quick check using just the metadata — no need to download
    the actual filing document yet.

    Args:
        filing_metadata: Dictionary with an 'item_codes' string (comma-separated)
                         or 'items_list' (list of strings)

    Returns:
        True if the filing has at least one target item code
    """
    # Get item codes as a list
    items = filing_metadata.get("items_list", [])
    if not items:
        # Fall back to comma-separated string
        item_codes_str = filing_metadata.get("item_codes", "")
        items = [code.strip() for code in item_codes_str.split(",") if code.strip()]

    # Check if ANY of the filing's item codes match our targets
    for item in items:
        if item in TARGET_ITEM_CODES:
            return True

    return False


def stage2_keyword_filter(text):
    """Stage 2: Scan the filing text for our target keywords.

    Looks through the text for keywords from each category.
    Returns which categories matched and which specific keywords were found.

    Args:
        text: Plain text content of the filing (already extracted from HTML)

    Returns:
        Dictionary with:
            'matched': True/False — did any keywords match?
            'categories': list of matched category names (e.g., ["Management Change"])
            'keywords': list of specific keywords that were found
            'category': best single category label
            'subcategory': more specific label if possible
    """
    if not text:
        return {"matched": False, "categories": [], "keywords": [], "category": None, "subcategory": None}

    text_lower = text.lower()

    # Remove known boilerplate phrases so they don't trigger false matches
    cleaned_text = text_lower
    for phrase in BOILERPLATE_PHRASES:
        cleaned_text = cleaned_text.replace(phrase.lower(), "")

    matched_categories = []
    matched_keywords = []

    # Check each category's keyword list against the cleaned text
    for category_name, keywords in KEYWORD_CATEGORIES.items():
        category_matched = False
        for keyword in keywords:
            if keyword.lower() in cleaned_text:
                if not category_matched:
                    matched_categories.append(category_name)
                    category_matched = True
                matched_keywords.append(keyword)

    if not matched_categories:
        return {"matched": False, "categories": [], "keywords": [], "category": None, "subcategory": None}

    # Determine the best single category
    if len(matched_categories) > 1:
        category = "Both"  # Filing covers both management change AND compensation
    else:
        category = matched_categories[0]

    # Try to assign a more specific sub-category
    subcategory = determine_subcategory(text_lower, matched_keywords)

    return {
        "matched": True,
        "categories": matched_categories,
        "keywords": matched_keywords,
        "category": category,
        "subcategory": subcategory,
    }


def _detect_departure_role(text_lower):
    """Look at the filing text to figure out which executive is departing.

    Scans for role titles near departure-related words. Returns the most
    specific role found (e.g., "CFO") or None if unclear.
    """
    import re

    # Role keywords we want to detect, checked in order of specificity
    roles = [
        ("CEO", ["ceo", "chief executive officer", "chief executive"]),
        ("CFO", ["cfo", "chief financial officer", "chief financial"]),
        ("COO", ["coo", "chief operating officer", "chief operating"]),
        ("CTO", ["cto", "chief technology officer", "chief technology"]),
        ("CLO", ["clo", "chief legal officer", "general counsel"]),
        ("CHRO", ["chro", "chief human resources", "chief people officer"]),
        ("President", ["president"]),
        ("Chairman", ["chairman", "chair of the board"]),
        ("Director", ["director"]),
    ]

    departure_words = ["resign", "departure", "stepping down", "retire",
                       "separated from", "no longer serving", "cease to serve",
                       "will depart", "terminated"]

    # Split text into sentences for proximity matching
    sentences = re.split(r'[.!?\n]', text_lower)

    for sentence in sentences:
        # Check if this sentence mentions a departure
        has_departure = any(dw in sentence for dw in departure_words)
        if not has_departure:
            continue

        # Check which role is mentioned in the same sentence
        for role_label, role_keywords in roles:
            if any(rk in sentence for rk in role_keywords):
                return role_label

    return None


def determine_subcategory(text_lower, matched_keywords):
    """Figure out the most specific sub-category label for a filing.

    Uses the SUB_CATEGORIES config to match keywords to finer-grained labels.
    For departure filings, also detects the specific executive role (CEO, CFO, etc.).

    Args:
        text_lower: Lowercased filing text
        matched_keywords: List of keywords that were already found

    Returns:
        String sub-category label, or None if no specific match
    """
    matched_keywords_lower = [kw.lower() for kw in matched_keywords]

    best_subcategory = None
    best_score = 0

    for subcat_name, subcat_keywords in SUB_CATEGORIES.items():
        # Count how many of this sub-category's keywords appear in our matches
        score = 0
        for kw in subcat_keywords:
            if kw.lower() in matched_keywords_lower or kw.lower() in text_lower:
                score += 1

        if score > best_score:
            best_score = score
            best_subcategory = subcat_name

    # If we detected a departure, try to identify the specific role
    if best_subcategory == "Executive Departure" and text_lower:
        role = _detect_departure_role(text_lower)
        if role:
            best_subcategory = f"{role} Departure"
        # Otherwise stays as "Executive Departure" (generic)

    return best_subcategory


def filter_filings(filings_metadata, fetch_text_func=None, model=None,
                   judge_model=None, apply_universe=True, skip_existing=True):
    """Run the ingest funnel over a list of filings.

    Stage 1 runs on metadata only (fast).
    Stage 1b drops filings outside the investable universe and ones already
    stored — both BEFORE any download or model call, because the cheapest
    filing is the one never fetched.
    Stage 2 downloads and keyword-scans the text.
    Stage 3 hands each survivor to the shared analysis pipeline.

    Args:
        filings_metadata: List of filing metadata dicts from fetcher.py
        fetch_text_func: Function to call to get filing text (from fetcher.py).
                         Signature: fetch_text_func(filing_url, cik, accession_no) -> str
        model / judge_model: model overrides for extraction and judgment
        apply_universe: False disables the market-cap floor (tests, and any
                        caller that has already screened)
        skip_existing: False re-processes filings already in the database

    Returns:
        List of filing dicts that passed every stage, enriched with analysis
    """
    print(f"Filtering {len(filings_metadata)} filings...", flush=True)

    # Stage 1: Item code filter
    stage1_passed = []
    stage1_skipped = 0
    for filing in filings_metadata:
        if stage1_item_code_filter(filing):
            stage1_passed.append(filing)
        else:
            stage1_skipped += 1

    print(f"  Stage 1 (item codes): {len(stage1_passed)} passed, {stage1_skipped} filtered out", flush=True)

    # Stage 1b: universe + dedupe, before we spend anything on a document.
    if apply_universe and stage1_passed:
        from universe import screen_filings, summarize_skips
        stage1_passed, out_of_universe = screen_filings(stage1_passed)
        if out_of_universe:
            print(f"  Stage 1b (universe): {len(out_of_universe)} skipped "
                  f"{summarize_skips(out_of_universe)}", flush=True)

    if skip_existing and stage1_passed:
        # Deduplication used to happen only at insert_filing() — i.e. after the
        # SEC fetch and the model call had already been paid for. Re-running an
        # overlapping date range charged full price for rows that were then
        # thrown away.
        from database import filing_exists
        fresh = []
        already = 0
        for filing in stage1_passed:
            try:
                seen = filing_exists(filing.get("accession_no"))
            except Exception:
                seen = False  # DB unavailable — better to re-fetch than to drop
            if seen:
                already += 1
            else:
                fresh.append(filing)
        if already:
            print(f"  Stage 1c (dedupe): {already} already in the database", flush=True)
        stage1_passed = fresh

    if not fetch_text_func:
        print("  Warning: No text fetch function provided, skipping Stage 2", flush=True)
        return stage1_passed

    # Stage 2: Keyword filter (requires downloading each filing)
    # Filings that pass keywords go to Stage 3 (LLM).
    # Keyword failures with executive-relevant item codes (5.02/1.01/1.02)
    # also go to Stage 3 as "near-misses" — keyword lists miss unusual
    # phrasing, and the LLM's relevance gate keeps irrelevant filings out of
    # the database. 8.01-only keyword failures are still dropped (too broad).
    stage2_passed = []   # Keyword matches — will get LLM review
    near_misses = []     # Keyword failures on in-scope items — LLM gets a look
    fetch_failures = 0   # In-scope filings SEC wouldn't give us text for

    for i, filing in enumerate(stage1_passed):
        print(f"  Stage 2: Checking filing {i + 1}/{len(stage1_passed)} — {filing.get('company', 'Unknown')}", flush=True)

        items = filing.get("items_list", [])

        # Download the filing text
        text, doc_url = fetch_text_func(
            filing.get("filing_url", ""),
            filing.get("cik", ""),
            filing.get("accession_no", "")
        )
        filing["filing_document_url"] = doc_url

        if not text:
            # A failed fetch tells us nothing about the filing — we can't run
            # keywords or the LLM on text we never got. So the decision has to
            # come from the item codes alone, and it must match the near-miss
            # policy below: 5.02/1.01/1.02 are in scope, 8.01-only is not.
            #
            # This used to keep 5.02 only, which meant a SEC rate-limit block
            # silently deleted every in-scope 1.01/1.02 filing it touched —
            # no row, no count, nothing in the logs to say they ever existed.
            # Now anything in scope is parked as a retryable row instead.
            if any(code in items for code in ("5.02", "1.01", "1.02")):
                filing["raw_text"] = ""
                filing["auto_category"] = "Management Change" if "5.02" in items else None
                filing["auto_subcategory"] = None
                filing["matched_keywords"] = "item " + ",".join(items) if items else "fetch-failed"
                filing["summary"] = "SEC rate-limited — pending retry"
                stage2_passed.append(filing)
                fetch_failures += 1
                print(f"    FETCH FAILED (items {','.join(items)}) — saved for retry", flush=True)
            else:
                print(f"    Could not fetch text (8.01-only), skipping", flush=True)
            continue

        filing["raw_text"] = text

        # Run keyword matching
        result = stage2_keyword_filter(text)

        if result["matched"]:
            filing["auto_category"] = result["category"]
            filing["auto_subcategory"] = result["subcategory"]
            filing["matched_keywords"] = ",".join(result["keywords"])
            stage2_passed.append(filing)
            print(f"    KEYWORD MATCH — {result['category']} / {result['subcategory']}", flush=True)
        elif any(code in items for code in ("5.02", "1.01", "1.02")):
            # Near-miss: keywords didn't fire, but the item codes are in scope.
            # The LLM decides relevance — it catches the unusual phrasing the
            # keyword list can't. 8.01-only filings are excluded: "Other
            # Events" is the highest-volume, lowest-hit-rate item, and LLM-
            # reviewing every keyword miss there would multiply daily cost
            # for little recall.
            filing["auto_category"] = "Management Change" if "5.02" in items else None
            filing["auto_subcategory"] = None
            filing["matched_keywords"] = "item " + ",".join(items) if items else "near-miss"
            filing["_near_miss"] = True
            near_misses.append(filing)
            print(f"    NEAR-MISS (no keywords, items {','.join(items)} — sending to LLM)", flush=True)
        else:
            print(f"    No keyword match (8.01-only), filtered out", flush=True)

    print(f"  Stage 2 (keywords): {len(stage2_passed)} matched, {len(near_misses)} near-misses", flush=True)
    if fetch_failures:
        # Loud on purpose — this is the number that silently ate ~70 filings
        # before, and it's the signal to press "Retry Missing Summaries".
        print(f"  Stage 2 WARNING: {fetch_failures} filing(s) had no text from SEC "
              f"(rate limited or unavailable). They are saved with a placeholder "
              f"summary — run 'Retry Missing Summaries' to fill them in.", flush=True)

    # Stage 3: analysis — extract, contextualize, detect, judge.
    #
    # Delegates to pipeline.analyze_filing, which is the single analysis path
    # shared with re-summarize and retry-missing. This used to be ~100 lines
    # of field mapping duplicated in three places; they drifted, and the retry
    # copy silently lost market-target detection for months.
    all_for_llm = stage2_passed + near_misses
    final_passed = []

    print(f"  Stage 3 (analysis): Reviewing {len(all_for_llm)} filings...", flush=True)

    for i, filing in enumerate(all_for_llm):
        company = filing.get("company", "Unknown")
        text = filing.get("raw_text", "")

        if not text:
            # No text to analyze — keep keyword-based info plus whatever
            # placeholder Stage 2 set on `summary` (e.g. the rate-limit notice).
            # Don't overwrite that with "" — leaves the dashboard ambiguous.
            filing.setdefault("summary", "")
            filing.setdefault("source", "8-K")
            final_passed.append(filing)
            continue

        print(f"  Stage 3: analyzing {i + 1}/{len(all_for_llm)} — {company}", flush=True)

        result = analyze_filing(filing, text=text, model=model, judge_model=judge_model)

        if result.error:
            # Extraction failed. Keyword matches and 5.02 near-misses fall back
            # to the keyword classification + sentence-scorer summary (previous
            # behavior). Keywordless non-5.02 near-misses are dropped — with no
            # keywords and no verdict there's zero evidence of relevance, and
            # storing them would just be noise.
            if filing.get("_near_miss") and "5.02" not in filing.get("items_list", []):
                print(f"    ANALYSIS FAILED on keywordless near-miss — dropping", flush=True)
                continue
            print(f"    ANALYSIS FAILED ({result.error}) — falling back to keywords", flush=True)
            filing["summary"] = extract_summary(text, filing.get("matched_keywords", "").split(","))
            filing.setdefault("source", "8-K")
            final_passed.append(filing)
            continue

        if not result.relevant:
            reason = result.fields.get("relevant_reason") or "(no reason given)"
            print(f"    NOT RELEVANT — {reason}", flush=True)
            continue

        apply_to_filing(filing, result)
        final_passed.append(filing)

        tokens = result.tokens_in + result.tokens_out
        badge = ",".join(result.signal_types) or "no signals"
        print(f"    {filing['triage_verdict']} {filing['signal_score']}/10 "
              f"[{badge}] ({tokens} tokens)", flush=True)

    print(f"  Stage 3 (analysis): {len(final_passed)} passed out of {len(all_for_llm)}", flush=True)
    print(f"  Final result: {len(final_passed)} filings match your criteria", flush=True)

    return final_passed
