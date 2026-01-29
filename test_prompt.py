# test_prompt.py — Test and compare LLM prompts against your existing filings
#
# Usage:
#   python test_prompt.py                          Test active prompt on 10 recent filings
#   python test_prompt.py --prompt prompt_v2.txt   Test a specific prompt
#   python test_prompt.py --all                    Test against ALL filings in the database
#   python test_prompt.py --count 20               Test against 20 filings
#   python test_prompt.py --compare prompt_v1.txt prompt_v2.txt   Compare two prompts side-by-side
#
# Workflow:
#   1. Create a new prompt file in the prompts/ folder (e.g., prompt_v2.txt)
#   2. Run: python test_prompt.py --compare prompt_v1.txt prompt_v2.txt
#   3. See which prompt does better
#   4. Update ACTIVE_PROMPT in config.py to the winner

import argparse
import sqlite3
import json
import os
import time
from config import DATABASE_PATH, PROMPTS_DIR, ACTIVE_PROMPT, LLM_MODEL
from llm import classify_and_summarize


def get_test_filings(count=None):
    """Pull filings from the database to use as test cases.
    Returns most recent filings first (they're the ones you remember best)."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = "SELECT * FROM filings WHERE raw_text IS NOT NULL AND raw_text != '' ORDER BY filed_date DESC"
    if count:
        query += f" LIMIT {count}"

    cursor.execute(query)
    filings = cursor.fetchall()
    conn.close()
    return filings


def run_prompt_on_filings(filings, prompt_file):
    """Run a specific prompt against a list of filings. Returns results list."""
    results = []
    total_tokens_in = 0
    total_tokens_out = 0

    for i, filing in enumerate(filings):
        company = filing["company"] or "Unknown"
        date = filing["filed_date"] or "?"
        items = filing["item_codes"] or ""

        print(f"  [{i + 1}/{len(filings)}] {company} ({date}) — {items}")

        # Call the LLM with this prompt
        llm_result = classify_and_summarize(filing["raw_text"], prompt_file=prompt_file)

        if llm_result:
            total_tokens_in += llm_result.get("_tokens_in", 0)
            total_tokens_out += llm_result.get("_tokens_out", 0)

        results.append({
            "filing": filing,
            "llm": llm_result,
        })

        # Small delay to avoid rate limits
        time.sleep(0.2)

    return results, total_tokens_in, total_tokens_out


def print_single_results(results, tokens_in, tokens_out):
    """Print results for a single prompt run — compare LLM output vs existing keyword labels."""
    print("\n" + "=" * 80)
    print("RESULTS: LLM vs Existing Keywords")
    print("=" * 80)

    category_matches = 0
    total = len(results)

    for r in results:
        filing = r["filing"]
        llm = r["llm"]

        company = filing["company"] or "Unknown"
        date = filing["filed_date"] or "?"
        items = filing["item_codes"] or ""
        old_cat = filing["auto_category"] or "None"
        old_subcat = filing["auto_subcategory"] or "None"
        old_summary = (filing["summary"] or "")[:120]

        print(f"\n--- {company} ({date}) — {items} ---")

        if llm is None:
            print("  LLM:  FAILED (API error)")
            continue

        llm_cat = llm.get("category") or "None"
        llm_subcat = llm.get("subcategory") or "None"
        llm_relevant = llm.get("relevant", False)
        llm_summary = (llm.get("summary") or "")[:120]

        # Check if categories match
        cat_match = old_cat == llm_cat
        if cat_match:
            category_matches += 1

        match_icon = "+" if cat_match else "DIFFERS"

        print(f"  OLD category:    {old_cat} / {old_subcat}")
        print(f"  LLM category:    {llm_cat} / {llm_subcat}  {match_icon}")
        print(f"  LLM relevant:    {llm_relevant}")
        print(f"  OLD summary:     {old_summary}")
        print(f"  LLM summary:     {llm_summary}")

    # Print summary stats
    print("\n" + "=" * 80)
    print("SUMMARY")
    print(f"  Category matches: {category_matches}/{total}")
    print(f"  Tokens used:      {tokens_in:,} in / {tokens_out:,} out")

    # Cost estimate (GPT-4o mini pricing)
    cost_in = tokens_in * 0.15 / 1_000_000
    cost_out = tokens_out * 0.60 / 1_000_000
    print(f"  Cost (GPT-4o mini): ${cost_in + cost_out:.4f}")
    print("=" * 80)


def print_compare_results(results_a, results_b, prompt_a, prompt_b, tokens_a, tokens_b):
    """Print side-by-side comparison of two different prompts."""
    print("\n" + "=" * 80)
    print(f"COMPARISON: {prompt_a} vs {prompt_b}")
    print("=" * 80)

    differences = 0

    for ra, rb in zip(results_a, results_b):
        filing = ra["filing"]
        llm_a = ra["llm"]
        llm_b = rb["llm"]

        company = filing["company"] or "Unknown"
        date = filing["filed_date"] or "?"
        old_cat = filing["auto_category"] or "None"

        cat_a = (llm_a or {}).get("category") or "None"
        cat_b = (llm_b or {}).get("category") or "None"
        rel_a = (llm_a or {}).get("relevant", "?")
        rel_b = (llm_b or {}).get("relevant", "?")
        sum_a = ((llm_a or {}).get("summary") or "")[:100]
        sum_b = ((llm_b or {}).get("summary") or "")[:100]

        # Only show filings where the two prompts disagree
        if cat_a == cat_b and rel_a == rel_b:
            continue

        differences += 1
        print(f"\n--- {company} ({date}) ---")
        print(f"  Keyword category: {old_cat}")
        print(f"  {prompt_a}:  relevant={rel_a}  category={cat_a}")
        print(f"    summary: {sum_a}")
        print(f"  {prompt_b}:  relevant={rel_b}  category={cat_b}")
        print(f"    summary: {sum_b}")

    # Summary
    total = len(results_a)
    agreed = total - differences
    print("\n" + "=" * 80)
    print("COMPARISON SUMMARY")
    print(f"  Total filings tested: {total}")
    print(f"  Prompts agreed:       {agreed}/{total}")
    print(f"  Prompts disagreed:    {differences}/{total}")

    # Show which prompt matched keywords better
    match_a = sum(1 for r in results_a if r["llm"] and r["llm"].get("category") == (r["filing"]["auto_category"] or "None"))
    match_b = sum(1 for r in results_b if r["llm"] and r["llm"].get("category") == (r["filing"]["auto_category"] or "None"))
    print(f"  {prompt_a} matched keywords: {match_a}/{total}")
    print(f"  {prompt_b} matched keywords: {match_b}/{total}")

    # Cost comparison
    cost_a = tokens_a[0] * 0.15 / 1_000_000 + tokens_a[1] * 0.60 / 1_000_000
    cost_b = tokens_b[0] * 0.15 / 1_000_000 + tokens_b[1] * 0.60 / 1_000_000
    print(f"  {prompt_a} cost: ${cost_a:.4f}")
    print(f"  {prompt_b} cost: ${cost_b:.4f}")
    print("=" * 80)


def list_prompts():
    """Show all available prompt files in the prompts/ folder."""
    print(f"Prompts folder: {PROMPTS_DIR}")
    print(f"Active prompt:  {ACTIVE_PROMPT}")
    print()
    for f in sorted(os.listdir(PROMPTS_DIR)):
        if f.endswith(".txt"):
            active = " <-- ACTIVE" if f == ACTIVE_PROMPT else ""
            path = os.path.join(PROMPTS_DIR, f)
            size = os.path.getsize(path)
            print(f"  {f}  ({size} bytes){active}")


def main():
    parser = argparse.ArgumentParser(description="Test LLM prompts against existing filings")
    parser.add_argument("--prompt", default=None, help="Prompt file to test (default: active prompt)")
    parser.add_argument("--count", type=int, default=10, help="Number of filings to test (default: 10)")
    parser.add_argument("--all", action="store_true", help="Test against ALL filings")
    parser.add_argument("--compare", nargs=2, metavar=("PROMPT_A", "PROMPT_B"),
                        help="Compare two prompts side-by-side")
    parser.add_argument("--list", action="store_true", help="List available prompts")
    args = parser.parse_args()

    if args.list:
        list_prompts()
        return

    # Figure out how many filings to test
    count = None if args.all else args.count

    # Load test filings from the database
    filings = get_test_filings(count)
    if not filings:
        print("No filings with text found in the database. Run the fetcher first.")
        return

    print(f"Testing against {len(filings)} filings from database")
    print(f"Model: {LLM_MODEL}")

    if args.compare:
        # Compare two prompts
        prompt_a, prompt_b = args.compare
        print(f"\nRunning prompt A: {prompt_a}")
        results_a, tin_a, tout_a = run_prompt_on_filings(filings, prompt_a)

        print(f"\nRunning prompt B: {prompt_b}")
        results_b, tin_b, tout_b = run_prompt_on_filings(filings, prompt_b)

        print_compare_results(results_a, results_b, prompt_a, prompt_b,
                              (tin_a, tout_a), (tin_b, tout_b))
    else:
        # Single prompt test
        prompt_file = args.prompt or ACTIVE_PROMPT
        print(f"Prompt: {prompt_file}\n")
        results, tokens_in, tokens_out = run_prompt_on_filings(filings, prompt_file)
        print_single_results(results, tokens_in, tokens_out)


if __name__ == "__main__":
    main()
