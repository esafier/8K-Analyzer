# repopulate.py — Delete old data and re-fetch the last 7 days of filings
# Run this: python repopulate.py

import os
from datetime import datetime, timedelta

from database import initialize_database, insert_filing
from fetcher import fetch_filings, fetch_filing_text
from filter import filter_filings
from summarizer import extract_summary
from config import DATABASE_PATH

# Step 1: Delete the old database so we start fresh
if os.path.exists(DATABASE_PATH):
    os.remove(DATABASE_PATH)
    print(f"Deleted old database ({DATABASE_PATH})")
else:
    print("No existing database found, starting fresh")

# Step 2: Create a new empty database
initialize_database()
print("Created fresh database")

# Step 3: Set the date range — last 7 days
end_date = datetime.now().strftime("%Y-%m-%d")
start_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
print(f"\nFetching filings from {start_date} to {end_date}...")

# Step 4: Fetch filing metadata from SEC EDGAR
filings_metadata = fetch_filings(start_date, end_date)

if not filings_metadata:
    print("No filings found in this date range")
else:
    # Step 5: Filter by item codes and keywords
    matched_filings = filter_filings(filings_metadata, fetch_text_func=fetch_filing_text)

    # Step 6: Store each matched filing (LLM summary set in filter stage 3;
    # sentence scorer is the fallback if LLM didn't provide one)
    stored_count = 0
    for filing in matched_filings:
        if not filing.get("summary"):
            keywords = filing.get("matched_keywords", "").split(",")
            filing["summary"] = extract_summary(filing.get("raw_text", ""), keywords)
        insert_filing(filing)
        stored_count += 1

    print(f"\nDone! {stored_count} filings stored in the database.")
    print("You can now run 'python app.py' to view them in the dashboard.")
