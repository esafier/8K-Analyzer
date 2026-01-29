# filter.py — Two-stage filtering for 8-K filings
# Stage 1: Filter by item codes (fast, metadata-only check)
# Stage 2: Keyword scan on filing text (more thorough)

from config import TARGET_ITEM_CODES, KEYWORD_CATEGORIES, SUB_CATEGORIES
from llm import classify_and_summarize
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


def filter_filings(filings_metadata, fetch_text_func=None):
    """Run both filter stages on a list of filings.

    Stage 1 runs on metadata only (fast).
    Stage 2 downloads and scans the actual text (slower, only for Stage 1 passes).

    Args:
        filings_metadata: List of filing metadata dicts from fetcher.py
        fetch_text_func: Function to call to get filing text (from fetcher.py).
                         Signature: fetch_text_func(filing_url, cik, accession_no) -> str

    Returns:
        List of filing dicts that passed both stages, enriched with category info
    """
    print(f"Filtering {len(filings_metadata)} filings...")

    # Stage 1: Item code filter
    stage1_passed = []
    stage1_skipped = 0
    for filing in filings_metadata:
        if stage1_item_code_filter(filing):
            stage1_passed.append(filing)
        else:
            stage1_skipped += 1

    print(f"  Stage 1 (item codes): {len(stage1_passed)} passed, {stage1_skipped} filtered out")

    if not fetch_text_func:
        print("  Warning: No text fetch function provided, skipping Stage 2")
        return stage1_passed

    # Stage 2: Keyword filter (requires downloading each filing)
    # Filings that pass keywords go to Stage 3 (LLM).
    # "Near-miss" filings (5.02/8.01 that fail keywords) also go to Stage 3.
    stage2_passed = []   # Keyword matches — will get LLM review
    near_misses = []     # 5.02/8.01 filings keywords missed — LLM gets a look

    for i, filing in enumerate(stage1_passed):
        print(f"  Stage 2: Checking filing {i + 1}/{len(stage1_passed)} — {filing.get('company', 'Unknown')}")

        items = filing.get("items_list", [])
        is_near_miss_candidate = "5.02" in items  # Only 5.02 near-misses go to LLM (8.01 is too broad)

        # Download the filing text
        text = fetch_text_func(
            filing.get("filing_url", ""),
            filing.get("cik", ""),
            filing.get("accession_no", "")
        )

        if not text:
            if "5.02" in items:
                # Keep 5.02 filings even without text (can't LLM them though)
                filing["raw_text"] = ""
                filing["auto_category"] = "Management Change"
                filing["auto_subcategory"] = None
                filing["matched_keywords"] = "item 5.02"
                filing["summary"] = ""
                stage2_passed.append(filing)
                print(f"    MATCH (5.02 auto-pass, no text available)")
            else:
                print(f"    Could not fetch text, skipping")
            continue

        filing["raw_text"] = text

        # Run keyword matching
        result = stage2_keyword_filter(text)

        if result["matched"]:
            filing["auto_category"] = result["category"]
            filing["auto_subcategory"] = result["subcategory"]
            filing["matched_keywords"] = ",".join(result["keywords"])
            stage2_passed.append(filing)
            print(f"    KEYWORD MATCH — {result['category']} / {result['subcategory']}")
        elif is_near_miss_candidate:
            # Near-miss: keywords didn't fire, but item code suggests it might be relevant
            filing["auto_category"] = "Management Change"
            filing["auto_subcategory"] = None
            filing["matched_keywords"] = "item 5.02"
            near_misses.append(filing)
            print(f"    NEAR-MISS (no keywords, but {'5.02' if '5.02' in items else '8.01'} — sending to LLM)")
        else:
            print(f"    No keyword match, filtered out")

    print(f"  Stage 2 (keywords): {len(stage2_passed)} matched, {len(near_misses)} near-misses")

    # Stage 3: LLM review — classify, validate, and summarize
    # Runs on both keyword matches (for better summaries) and near-misses (to rescue good ones)
    all_for_llm = stage2_passed + near_misses
    final_passed = []

    print(f"  Stage 3 (LLM): Reviewing {len(all_for_llm)} filings...")

    for i, filing in enumerate(all_for_llm):
        company = filing.get("company", "Unknown")
        text = filing.get("raw_text", "")

        if not text:
            # No text to analyze — keep it with keyword-based info
            filing["summary"] = ""
            final_passed.append(filing)
            continue

        print(f"  Stage 3: LLM reviewing {i + 1}/{len(all_for_llm)} — {company}")

        llm_result = classify_and_summarize(text)

        if llm_result is not None:
            # LLM succeeded — use its classification and summary
            is_relevant = llm_result.get("relevant", False)

            if is_relevant:
                # LLM says it's relevant — use LLM's category/summary
                filing["auto_category"] = llm_result.get("category") or filing.get("auto_category")
                filing["auto_subcategory"] = llm_result.get("subcategory") or filing.get("auto_subcategory")
                filing["summary"] = llm_result.get("summary") or ""
                final_passed.append(filing)
                tokens = llm_result.get("_tokens_in", 0) + llm_result.get("_tokens_out", 0)
                print(f"    LLM: RELEVANT — {filing['auto_category']} / {filing['auto_subcategory']} ({tokens} tokens)")
            else:
                # LLM says not relevant — skip it (even if keywords matched)
                print(f"    LLM: NOT RELEVANT — filtered out")
        else:
            # LLM failed — fall back to keyword classification + sentence-scorer summary
            print(f"    LLM FAILED — falling back to keyword classification")
            filing["summary"] = extract_summary(text, filing.get("matched_keywords", "").split(","))
            final_passed.append(filing)

    print(f"  Stage 3 (LLM): {len(final_passed)} passed out of {len(all_for_llm)}")
    print(f"  Final result: {len(final_passed)} filings match your criteria")

    return final_passed
