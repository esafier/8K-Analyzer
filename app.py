# app.py — Flask web dashboard for browsing filtered 8-K filings
# Run this file to start the dashboard: python app.py

import os
import re
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from markupsafe import escape
import math
from database import (
    initialize_database, get_filings, get_filing_by_id, update_user_tag,
    get_categories, get_filing_count, get_filtered_filing_count,
    add_to_watchlist, remove_from_watchlist, update_watchlist_notes,
    get_watchlist_item, get_all_watchlist_ids, get_watchlist_filings,
    get_watchlist_filings_by_ids, mark_filings_email_sent,
    update_last_backfill, get_last_backfill, update_filing_analysis,
    update_deep_analysis
)
from fetcher import fetch_filings, fetch_filing_text
from filter import filter_filings
from summarizer import extract_summary
from database import insert_filing
import threading

app = Flask(__name__)
app.secret_key = "8k-analyzer-secret-key"  # Needed for flash messages


# --- Jinja filter: turn raw market cap numbers into readable strings ---
def format_market_cap(value):
    """Turn 1234567890 into '$1.2B', 450000000 into '$450M', etc."""
    if value is None:
        return ""
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.1f}T"
    elif value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    elif value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    else:
        return f"${value:,.0f}"

app.jinja_env.filters["format_market_cap"] = format_market_cap


def render_deep_analysis(text):
    """Convert deep analysis text (### headers and - bullets) into HTML.
    Escapes the text first for safety, then adds formatting."""
    if not text:
        return ""
    text = str(escape(text))
    # Convert ### headers to styled headings
    text = re.sub(
        r'^### (.+)$',
        r'<h6 class="mt-3 mb-2 text-primary fw-bold">\1</h6>',
        text, flags=re.MULTILINE,
    )
    # Convert bullet points to list items
    text = re.sub(r'^- (.+)$', r'<li>\1</li>', text, flags=re.MULTILINE)
    # Wrap consecutive <li> items in <ul> tags
    text = re.sub(r'((?:<li>.*?</li>\n?)+)', r'<ul class="mb-2">\1</ul>', text)
    # Paragraph spacing
    text = text.replace('\n\n', '<br><br>')
    text = text.replace('\n', '<br>')
    return text

app.jinja_env.filters["render_deep_analysis"] = render_deep_analysis


@app.route("/")
def index():
    """Main dashboard page — shows the list of filtered filings."""
    # Get filter parameters from the URL query string
    category = request.args.get("category", "")
    search = request.args.get("search", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    urgent_only = request.args.get("urgent", "") == "1"
    page = int(request.args.get("page", 1))

    per_page = 50
    offset = (page - 1) * per_page

    # Fetch filtered filings from the database
    filings = get_filings(
        category=category if category else None,
        search=search if search else None,
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        urgent_only=urgent_only,
        limit=per_page,
        offset=offset,
    )

    # Parse comp_details JSON for each filing so templates can use it
    import json
    for filing in filings:
        raw = filing.get("comp_details") or filing.get("comp_details", None)
        if raw and isinstance(raw, str):
            try:
                filing["_comp"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                filing["_comp"] = None
        else:
            filing["_comp"] = None

    # Get all categories for the filter dropdown
    categories = get_categories()
    total_count = get_filing_count()

    # Get last backfill info for the header display
    last_backfill = get_last_backfill()

    # Get watchlisted filing IDs so we can show star icons
    watchlist_ids = get_all_watchlist_ids()

    # Fetch market cap data for tickers on this page
    # Wrapped in try/except so a yfinance failure never breaks the dashboard
    market_caps = {}
    try:
        from market_cap import get_market_cap_map
        unique_tickers = list({f['ticker'] for f in filings if f.get('ticker')})
        market_caps = get_market_cap_map(unique_tickers)
    except Exception as e:
        print(f"[MARKET CAP] Failed to load market caps: {e}")

    # Count filings matching current filters so we know total pages
    filtered_count = get_filtered_filing_count(
        category=category if category else None,
        search=search if search else None,
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        urgent_only=urgent_only,
    )
    total_pages = max(1, math.ceil(filtered_count / per_page))

    return render_template(
        "index.html",
        filings=filings,
        categories=categories,
        total_count=total_count,
        last_backfill=last_backfill,
        current_category=category,
        current_search=search,
        current_date_from=date_from,
        current_date_to=date_to,
        current_urgent=urgent_only,
        current_page=page,
        per_page=per_page,
        total_pages=total_pages,
        watchlist_ids=watchlist_ids,
        market_caps=market_caps,
    )


@app.route("/filing/<int:filing_id>")
def filing_detail(filing_id):
    """Detail page for a single filing."""
    filing = get_filing_by_id(filing_id)
    if not filing:
        flash("Filing not found", "error")
        return redirect(url_for("index"))

    # Parse comp_details JSON so the template can display individual fields
    import json
    raw_comp = filing.get("comp_details") if hasattr(filing, 'get') else (filing["comp_details"] if "comp_details" in filing else None)
    if raw_comp and isinstance(raw_comp, str):
        try:
            filing = dict(filing)  # Make mutable copy if needed
            filing["_comp"] = json.loads(raw_comp)
        except (json.JSONDecodeError, TypeError):
            filing["_comp"] = None
    else:
        if not hasattr(filing, '__setitem__'):
            filing = dict(filing)
        filing["_comp"] = None

    # All possible category/tag options for the dropdown
    tag_options = [
        "Management Change", "Compensation", "Both",
        "CEO Departure", "New Hire", "Inducement Award",
        "Accelerated Vesting", "Comp Plan Change", "Severance / Separation",
    ]

    # Remember where the user came from so "Back" returns to the right page
    back_url = request.args.get("back", "/")

    # Check if this filing is in the watchlist
    watchlist_entry = get_watchlist_item(filing_id)
    is_watchlisted = watchlist_entry is not None
    watchlist_notes = watchlist_entry.get("notes", "") if watchlist_entry else ""

    # Fetch market cap for this ticker
    market_cap = None
    if filing.get("ticker"):
        try:
            from market_cap import get_market_cap_map
            caps = get_market_cap_map([filing["ticker"]])
            market_cap = caps.get(filing["ticker"].strip().upper())
        except Exception as e:
            print(f"[MARKET CAP] Failed for {filing.get('ticker')}: {e}")

    return render_template(
        "filing.html",
        filing=filing,
        tag_options=tag_options,
        back_url=back_url,
        is_watchlisted=is_watchlisted,
        watchlist_notes=watchlist_notes,
        market_cap=market_cap,
    )


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


@app.route("/deep-analysis/<int:filing_id>", methods=["POST"])
def deep_analysis(filing_id):
    """Run a comprehensive investor-focused deep analysis on a filing."""
    from llm import deep_analyze

    filing = get_filing_by_id(filing_id)
    if not filing:
        flash("Filing not found", "error")
        return redirect(url_for("index"))

    raw_text = filing["raw_text"] or ""
    if not raw_text:
        flash("No filing text available to analyze", "error")
        return redirect(url_for("filing_detail", filing_id=filing_id))

    # Call the LLM with the deep analysis prompt (uses GPT-5.2 by default)
    result = deep_analyze(raw_text)

    if result is None:
        flash("Deep analysis failed — the API call didn't go through. Try again.", "error")
        return redirect(url_for("filing_detail", filing_id=filing_id))

    # Store the analysis text in its own column (doesn't touch summary/category)
    update_deep_analysis(filing_id, result["analysis"])

    tokens = result.get("_tokens_in", 0) + result.get("_tokens_out", 0)
    flash(f"Deep analysis complete ({tokens:,} tokens used).", "success")
    return redirect(url_for("filing_detail", filing_id=filing_id))


# ============================================================
# WATCHLIST ROUTES
# ============================================================

@app.route("/watchlist")
def watchlist():
    """Dedicated watchlist page showing all saved filings with notes."""
    import json
    # Convert to plain dicts so .get() works on both SQLite and PostgreSQL
    filings = [dict(f) for f in get_watchlist_filings()]

    # Parse comp_details JSON for each filing
    for filing in filings:
        raw = filing.get("comp_details")
        if raw and isinstance(raw, str):
            try:
                filing["_comp"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                filing["_comp"] = None
        else:
            filing["_comp"] = None

    # Fetch market caps for watchlist tickers
    market_caps = {}
    try:
        from market_cap import get_market_cap_map
        unique_tickers = list({f['ticker'] for f in filings if f.get('ticker')})
        market_caps = get_market_cap_map(unique_tickers)
    except Exception as e:
        print(f"[MARKET CAP] Failed to load market caps for watchlist: {e}")

    return render_template("watchlist.html", filings=filings, market_caps=market_caps)


@app.route("/watchlist/add/<int:filing_id>", methods=["POST"])
def watchlist_add(filing_id):
    """Add a filing to the watchlist."""
    add_to_watchlist(filing_id)

    # If this is an AJAX request, return JSON
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True, "action": "added"})

    # Otherwise redirect back (for form submission fallback)
    flash("Added to watchlist", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/watchlist/remove/<int:filing_id>", methods=["POST"])
def watchlist_remove(filing_id):
    """Remove a filing from the watchlist."""
    remove_from_watchlist(filing_id)

    # If this is an AJAX request, return JSON
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True, "action": "removed"})

    # Otherwise redirect back
    flash("Removed from watchlist", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/watchlist/notes/<int:filing_id>", methods=["POST"])
def watchlist_save_notes(filing_id):
    """Save or update notes for a watchlisted filing."""
    notes = request.form.get("notes", "").strip()
    update_watchlist_notes(filing_id, notes)

    # If this is an AJAX request, return JSON
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True})

    flash("Notes saved", "success")
    return redirect(request.referrer or url_for("filing_detail", filing_id=filing_id))


@app.route("/compose-email", methods=["POST"])
def compose_email():
    """Show the email composer page with selected watchlist filings.
    User can edit commentary and copy a formatted section for their weekly email."""
    import json

    # Get the comma-separated filing IDs from the hidden form
    selected_ids = request.form.get("selected_filings", "")
    if not selected_ids:
        flash("No filings selected", "warning")
        return redirect(url_for("watchlist"))

    # Parse IDs safely
    try:
        filing_ids = [int(x.strip()) for x in selected_ids.split(",") if x.strip()]
    except ValueError:
        flash("Invalid selection", "warning")
        return redirect(url_for("watchlist"))

    # Fetch the selected filings with their watchlist notes
    # Convert to plain dicts so .get() works on both SQLite and PostgreSQL
    filings = [dict(f) for f in get_watchlist_filings_by_ids(filing_ids)]

    # Parse comp_details JSON (same pattern used in the watchlist route)
    for filing in filings:
        raw = filing.get("comp_details")
        if raw and isinstance(raw, str):
            try:
                filing["_comp"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                filing["_comp"] = None
        else:
            filing["_comp"] = None

    return render_template("compose_email.html", filings=filings)


@app.route("/mark-as-sent", methods=["POST"])
def mark_as_sent():
    """Mark filings as included in a weekly email (AJAX endpoint)."""
    data = request.get_json()
    filing_ids = data.get("filing_ids", [])

    # Validate: must be a list of integers
    try:
        filing_ids = [int(x) for x in filing_ids]
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid filing IDs"}), 400

    mark_filings_email_sent(filing_ids)
    return jsonify({"success": True})


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
        model = request.form.get("model", "")  # Optional model override

        if not start_date or not end_date:
            flash("Please enter both start and end dates", "error")
            return render_template("backfill.html")

        # Run the fetch in a background thread so the page doesn't hang
        thread = threading.Thread(
            target=run_backfill,
            args=(start_date, end_date, model if model else None),
        )
        thread.daemon = True
        thread.start()

        model_label = model or "GPT-4o-mini"
        flash(f"Backfill started for {start_date} to {end_date} using {model_label}. This runs in the background — refresh the main page to see new filings as they appear.", "success")
        return redirect(url_for("index"))

    return render_template("backfill.html")


@app.route("/clear-market-cap-cache", methods=["POST"])
def clear_market_cap_cache():
    """Flush failed (NULL) market cap entries so they get retried."""
    from database import clear_failed_market_caps
    deleted = clear_failed_market_caps()
    flash(f"Cleared {deleted} failed market cap entries. They'll be refetched on next page load.", "success")
    return redirect(url_for("index"))


def run_backfill(start_date, end_date, model=None):
    """Background task: fetch, filter, summarize, and store filings.
    This is the main pipeline that ties all the pieces together.
    Pass model="gpt-5.2" to use the premium model for this backfill."""
    model_label = model or "GPT-4o-mini"
    print(f"\n--- Starting backfill: {start_date} to {end_date} (model: {model_label}) ---")

    # Step 1: Fetch filing metadata from EDGAR
    filings_metadata = fetch_filings(start_date, end_date)

    if not filings_metadata:
        print("No filings found in this date range")
        return

    # Step 2: Filter (Stage 1 + Stage 2), with optional model override for Stage 3
    matched_filings = filter_filings(filings_metadata, fetch_text_func=fetch_filing_text, model=model)

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

    # Step 4: Pre-fetch market caps so the dashboard loads instantly
    # One batch call to yfinance for all new tickers, results get cached in DB
    tickers_to_fetch = list({f['ticker'] for f in matched_filings if f.get('ticker')})
    if tickers_to_fetch:
        try:
            from market_cap import get_market_cap_map
            print(f"[MARKET CAP] Pre-fetching market caps for {len(tickers_to_fetch)} tickers...")
            get_market_cap_map(tickers_to_fetch)
            print(f"[MARKET CAP] Done — cached for next 24 hours")
        except Exception as e:
            print(f"[MARKET CAP] Pre-fetch failed (not critical): {e}")

    # Record that a backfill completed (for front page display)
    update_last_backfill("web")


# Initialize the database when the app starts
initialize_database()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Starting 8-K Filing Analyzer Dashboard...")
    print(f"Open http://127.0.0.1:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False)
