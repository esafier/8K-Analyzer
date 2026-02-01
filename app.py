# app.py — Flask web dashboard for browsing filtered 8-K filings
# Run this file to start the dashboard: python app.py

import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import math
from database import initialize_database, get_filings, get_filing_by_id, update_user_tag, get_categories, get_filing_count, get_filtered_filing_count
from fetcher import fetch_filings, fetch_filing_text
from filter import filter_filings
from summarizer import extract_summary
from database import insert_filing
import threading

app = Flask(__name__)
app.secret_key = "8k-analyzer-secret-key"  # Needed for flash messages


@app.route("/")
def index():
    """Main dashboard page — shows the list of filtered filings."""
    # Get filter parameters from the URL query string
    category = request.args.get("category", "")
    search = request.args.get("search", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    page = int(request.args.get("page", 1))

    per_page = 50
    offset = (page - 1) * per_page

    # Fetch filtered filings from the database
    filings = get_filings(
        category=category if category else None,
        search=search if search else None,
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        limit=per_page,
        offset=offset,
    )

    # Get all categories for the filter dropdown
    categories = get_categories()
    total_count = get_filing_count()

    # Count filings matching current filters so we know total pages
    filtered_count = get_filtered_filing_count(
        category=category if category else None,
        search=search if search else None,
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
    )
    total_pages = max(1, math.ceil(filtered_count / per_page))

    return render_template(
        "index.html",
        filings=filings,
        categories=categories,
        total_count=total_count,
        current_category=category,
        current_search=search,
        current_date_from=date_from,
        current_date_to=date_to,
        current_page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@app.route("/filing/<int:filing_id>")
def filing_detail(filing_id):
    """Detail page for a single filing."""
    filing = get_filing_by_id(filing_id)
    if not filing:
        flash("Filing not found", "error")
        return redirect(url_for("index"))

    # All possible category/tag options for the dropdown
    tag_options = [
        "Management Change", "Compensation", "Both",
        "CEO Departure", "New Hire", "Inducement Award",
        "Accelerated Vesting", "Comp Plan Change", "Severance / Separation",
    ]

    # Remember where the user came from so "Back" returns to the right page
    back_url = request.args.get("back", "/")

    return render_template("filing.html", filing=filing, tag_options=tag_options, back_url=back_url)


@app.route("/update-tag/<int:filing_id>", methods=["POST"])
def update_tag(filing_id):
    """Update the user's manual tag for a filing (called from the detail page)."""
    new_tag = request.form.get("user_tag", "").strip()
    if new_tag:
        update_user_tag(filing_id, new_tag)
        flash(f"Tag updated to '{new_tag}'", "success")
    else:
        update_user_tag(filing_id, None)  # Clear the tag
        flash("Tag cleared", "success")
    return redirect(url_for("filing_detail", filing_id=filing_id))


@app.route("/clear-database", methods=["POST"])
def clear_database():
    """Wipe all filings so you can re-backfill with an updated prompt."""
    from database import clear_all_filings
    clear_all_filings()
    flash("Database cleared. Run a backfill to repopulate with the current prompt.", "success")
    return redirect(url_for("backfill"))


@app.route("/backfill", methods=["GET", "POST"])
def backfill():
    """Page to trigger a historical backfill of filings."""
    if request.method == "POST":
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")

        if not start_date or not end_date:
            flash("Please enter both start and end dates", "error")
            return render_template("backfill.html")

        # Run the fetch in a background thread so the page doesn't hang
        thread = threading.Thread(
            target=run_backfill,
            args=(start_date, end_date),
        )
        thread.daemon = True
        thread.start()

        flash(f"Backfill started for {start_date} to {end_date}. This runs in the background — refresh the main page to see new filings as they appear.", "success")
        return redirect(url_for("index"))

    return render_template("backfill.html")


def run_backfill(start_date, end_date):
    """Background task: fetch, filter, summarize, and store filings.
    This is the main pipeline that ties all the pieces together."""
    print(f"\n--- Starting backfill: {start_date} to {end_date} ---")

    # Step 1: Fetch filing metadata from EDGAR
    filings_metadata = fetch_filings(start_date, end_date)

    if not filings_metadata:
        print("No filings found in this date range")
        return

    # Step 2: Filter (Stage 1 + Stage 2)
    matched_filings = filter_filings(filings_metadata, fetch_text_func=fetch_filing_text)

    # Step 3: Summarize and store each matched filing
    stored_count = 0
    for filing in matched_filings:
        # Only generate a fallback summary if the LLM didn't already provide one
        if not filing.get("summary"):
            keywords = filing.get("matched_keywords", "").split(",")
            filing["summary"] = extract_summary(filing.get("raw_text", ""), keywords)

        # Save to database
        insert_filing(filing)
        stored_count += 1

    print(f"--- Backfill complete: {stored_count} filings stored ---\n")


# Initialize the database when the app starts
initialize_database()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Starting 8-K Filing Analyzer Dashboard...")
    print(f"Open http://127.0.0.1:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False)
