# app.py — Flask web dashboard for browsing filtered 8-K filings
# Run this file to start the dashboard: python app.py

import os
import re
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from markupsafe import escape
import math
from database import (
    initialize_database, get_filings, get_filing_by_id, update_user_tag,
    get_categories, get_filing_count, get_filtered_filing_count,
    add_to_watchlist, remove_from_watchlist, update_watchlist_notes,
    get_watchlist_item, get_all_watchlist_ids, get_watchlist_filings,
    get_watchlist_filings_by_ids, mark_filings_email_sent,
    update_last_backfill, get_last_backfill, update_filing_analysis,
    update_deep_analysis, get_filings_for_resummarize,
    create_backfill_run, complete_backfill_run, get_recent_backfill_runs
)
from fetcher import fetch_filings, fetch_filing_text
from filter import filter_filings
from summarizer import extract_summary
from database import insert_filing
import threading

app = Flask(__name__)
# SECRET_KEY is needed for sessions & flash messages.
# On Render, set this to a random string. Locally, the fallback works fine.
app.secret_key = os.environ.get("SECRET_KEY", "8k-analyzer-secret-key")


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


def format_earnings_date(earnings_info):
    """Turn {'date': '2026-04-25', 'timing': 'before_market'} into 'Apr 25 (BMO)'.
    BMO = before market open, AMC = after market close."""
    if not earnings_info or not earnings_info.get("date"):
        return ""
    date_str = earnings_info["date"]
    timing = earnings_info.get("timing", "")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        formatted = dt.strftime("%b %d")
    except ValueError:
        return date_str  # If parsing fails, show raw date
    # Add timing abbreviation if available
    timing_map = {"before_market": "BMO", "after_market": "AMC", "during_market": "DMH"}
    abbrev = timing_map.get(timing, "")
    if abbrev:
        return f"{formatted} ({abbrev})"
    return formatted

app.jinja_env.filters["format_earnings_date"] = format_earnings_date


# --- Jinja filters for v3 structured summary ---
from summary_utils import parse_subcategories, structured_summary_for_display

@app.template_filter("parse_subcategories")
def _jinja_parse_subcategories(raw):
    return parse_subcategories(raw)

@app.template_filter("structured_summary")
def _jinja_structured_summary(raw):
    return structured_summary_for_display(raw)


def render_deep_analysis(text):
    """Convert deep analysis text (### headers, - bullets, **bold**) into HTML.
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
    # Convert **bold** text to <strong> tags (for BULL/BEAR labels etc.)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Convert bullet points to list items
    text = re.sub(r'^- (.+)$', r'<li>\1</li>', text, flags=re.MULTILINE)
    # Wrap consecutive <li> items in <ul> tags
    text = re.sub(r'((?:<li>.*?</li>\n?)+)', r'<ul class="mb-2">\1</ul>', text)
    # Paragraph spacing
    text = text.replace('\n\n', '<br><br>')
    text = text.replace('\n', '<br>')
    return text

app.jinja_env.filters["render_deep_analysis"] = render_deep_analysis


# ============================================================
# TRIAL ACCESS GATE
# If TRIAL_CODE env var is set, visitors must enter the code
# to use the app. If not set, the app works with no login.
# ============================================================

@app.before_request
def check_trial_access():
    """Block unauthenticated visitors when a trial code is configured."""
    trial_code = os.environ.get("TRIAL_CODE")

    # No trial code set → app is open (backwards compatible)
    if not trial_code:
        return None

    # Allow the login page itself (otherwise infinite redirect loop)
    if request.endpoint in ("login", "static"):
        return None

    # Check if user has a valid session
    if session.get("authenticated"):
        # Log page visits from trial users (visible in Render's Logs tab)
        if request.endpoint != "static":
            print(f"[TRIAL] {request.method} {request.path}")
        return None

    # Not authenticated → send them to the login page
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login page where trial users enter their access code."""
    trial_code = os.environ.get("TRIAL_CODE")

    # If no trial code is configured, skip straight to the dashboard
    if not trial_code:
        return redirect(url_for("index"))

    error = None

    if request.method == "POST":
        entered_code = request.form.get("access_code", "").strip()

        # Check if the trial has expired
        trial_expires = os.environ.get("TRIAL_EXPIRES", "")
        if trial_expires:
            try:
                expiry_date = datetime.strptime(trial_expires, "%Y-%m-%d").date()
                if datetime.now().date() > expiry_date:
                    error = "This trial has expired."
                    return render_template("login.html", error=error)
            except ValueError:
                pass  # Bad date format → ignore expiry check

        # Check the code
        if entered_code == trial_code:
            session["authenticated"] = True
            return redirect(url_for("index"))
        else:
            error = "Invalid access code. Please try again."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    """Clear the session and return to login page."""
    session.clear()
    return redirect(url_for("login"))


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

    # Convert to plain dicts so .get() works on both SQLite and PostgreSQL
    filings = [dict(f) for f in filings]

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

    # Fetch next earnings dates for tickers on this page
    earnings = {}
    try:
        from earnings import get_earnings_map
        earnings = get_earnings_map(unique_tickers)
    except Exception as e:
        print(f"[EARNINGS] Failed to load earnings: {e}")

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
        earnings=earnings,
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

    # Fetch next earnings date for this ticker
    earnings_info = None
    if filing.get("ticker"):
        try:
            from earnings import get_earnings_map
            e_map = get_earnings_map([filing["ticker"]])
            earnings_info = e_map.get(filing["ticker"].strip().upper())
        except Exception as e:
            print(f"[EARNINGS] Failed for {filing.get('ticker')}: {e}")

    return render_template(
        "filing.html",
        filing=filing,
        tag_options=tag_options,
        back_url=back_url,
        is_watchlisted=is_watchlisted,
        watchlist_notes=watchlist_notes,
        market_cap=market_cap,
        earnings_info=earnings_info,
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
    """Run skeptical buy-side signal analysis on a filing.

    Gathers company context (market cap, stock price, earnings date,
    departure history, and optionally web search results) and sends
    it to the LLM along with the filing text."""
    try:
        from llm import signal_analyze, web_search_context
        from fetcher import get_edgar_departure_history

        filing = get_filing_by_id(filing_id)
        if not filing:
            flash("Filing not found", "error")
            return redirect(url_for("index"))

        raw_text = filing["raw_text"] or ""
        if not raw_text:
            flash("No filing text available to analyze", "error")
            return redirect(url_for("filing_detail", filing_id=filing_id))

        # --- Gather context to pre-inject into the prompt ---
        ticker = filing.get("ticker", "")
        mcap_str = ""
        earnings_str = ""
        price_str = ""

        if ticker:
            # Market cap (reuse existing cache)
            try:
                from market_cap import get_market_cap_map
                caps = get_market_cap_map([ticker])
                mcap_val = caps.get(ticker.strip().upper())
                mcap_str = format_market_cap(mcap_val) if mcap_val else "Not available"
            except Exception:
                mcap_str = "Not available"

            # Next earnings date (reuse existing cache)
            try:
                from earnings import get_earnings_map
                e_map = get_earnings_map([ticker])
                e_info = e_map.get(ticker.strip().upper())
                earnings_str = format_earnings_date(e_info) if e_info else "Not available"
            except Exception:
                earnings_str = "Not available"

            # Current stock price (new — from API Ninjas)
            try:
                from stock_price import get_stock_price
                price = get_stock_price(ticker)
                price_str = f"${price:.2f}" if price else "Not available"
            except Exception:
                price_str = "Not available"

        # Parse comp_details for injection
        import json
        comp_str = "None extracted"
        raw_comp = filing.get("comp_details") or ""
        if raw_comp and isinstance(raw_comp, str):
            try:
                comp_data = json.loads(raw_comp)
                # Format comp details as readable text
                parts = []
                if comp_data.get("grant_value"):
                    parts.append(f"Grant Value: {comp_data['grant_value']}")
                if comp_data.get("grant_type"):
                    parts.append(f"Grant Type: {comp_data['grant_type']}")
                if comp_data.get("vesting_target_price"):
                    parts.append(f"Vesting Target Price: {comp_data['vesting_target_price']}")
                if comp_data.get("performance_hurdles"):
                    parts.append(f"Performance Hurdles: {comp_data['performance_hurdles']}")
                if comp_data.get("stock_vs_cash_election"):
                    parts.append(f"Stock vs Cash Election: {comp_data['stock_vs_cash_election']}")
                if parts:
                    comp_str = "; ".join(parts)
            except (json.JSONDecodeError, TypeError):
                pass

        # --- Departure clustering: query EDGAR for other 5.02 filings from same company ---
        item_codes = filing.get("item_codes", "")
        departure_str = ""
        departures = []
        if "5.02" in item_codes:
            cik = filing.get("cik", "")
            accession = filing.get("accession_no", "")
            departures = get_edgar_departure_history(cik, accession)
            if departures:
                dep_lines = []
                for dep in departures:
                    date = dep.get("filing_date", "Unknown date")
                    snippet = dep.get("snippet", "")
                    if snippet:
                        dep_lines.append(f"  [{date}] {snippet}")
                    else:
                        items = dep.get("items", "5.02")
                        dep_lines.append(f"  [{date}] 8-K with Items: {items} (details unavailable)")
                departure_str = "\n".join(dep_lines)
            else:
                departure_str = "No other Item 5.02 filings found in past 12 months"

        # --- Optional web search: gather recent news if user checked the box ---
        web_search_str = ""
        web_search_tokens = 0
        if request.form.get("web_search"):
            company = filing.get("company", "")
            ws_result = web_search_context(company, ticker)
            if ws_result:
                web_search_str = ws_result["context"]
                web_search_tokens = ws_result.get("_tokens_in", 0) + ws_result.get("_tokens_out", 0)

        # Build the context block that gets injected into the prompt
        context_block = (
            f"- Company: {filing.get('company', 'Unknown')}\n"
            f"- Ticker: {ticker or 'Unknown'}\n"
            f"- Market Cap: {mcap_str}\n"
            f"- Current Stock Price: {price_str}\n"
            f"- Next Earnings Date: {earnings_str}\n"
            f"- Filing Date: {filing.get('filed_date', 'Unknown')}\n"
            f"- Item Codes: {filing.get('item_codes', 'Unknown')}\n"
            f"- Auto Category: {filing.get('auto_category', '')} / {filing.get('auto_subcategory', '')}\n"
            f"- Extracted Comp Details: {comp_str}"
        )

        # Append departure history if this is a 5.02 filing
        if departure_str:
            context_block += f"\n- Recent Departures at This Company (from SEC filings):\n{departure_str}"

        # Append web search results if the user requested them
        if web_search_str:
            context_block += f"\n- Recent News (web search):\n{web_search_str}"

        # Check which prompt version the user selected (default to v1)
        prompt_version = request.form.get("prompt_version", "v1")

        # Call the LLM with signal analysis prompt (all context pre-gathered)
        result = signal_analyze(raw_text, context_block, prompt_version=prompt_version)

        if result is None:
            flash("Signal analysis failed — the API call didn't go through. Try again.", "error")
            return redirect(url_for("filing_detail", filing_id=filing_id))

        # Store the analysis text in its own column (doesn't touch summary/category)
        update_deep_analysis(filing_id, result["analysis"])

        # Show token breakdown and context info in the flash message
        analysis_tokens = result.get("_tokens_in", 0) + result.get("_tokens_out", 0)
        total_tokens = analysis_tokens + web_search_tokens
        token_parts = [f"{analysis_tokens:,} analysis"]
        if web_search_tokens:
            token_parts.append(f"{web_search_tokens:,} web search")
        context_notes = []
        if "5.02" in item_codes:
            context_notes.append(f"{len(departures)} prior departure(s) found")
        if web_search_str:
            context_notes.append("web search included")
        msg = f"Signal analysis complete ({total_tokens:,} tokens: {', '.join(token_parts)})."
        if context_notes:
            msg += f" Context: {'; '.join(context_notes)}."
        flash(msg, "success")
        return redirect(url_for("filing_detail", filing_id=filing_id))

    except Exception as e:
        import traceback
        print(f"[ERROR] Signal analysis failed: {traceback.format_exc()}")
        flash(f"Signal analysis error: {e}", "error")
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

    # Fetch next earnings dates for watchlist tickers
    earnings = {}
    try:
        from earnings import get_earnings_map
        earnings = get_earnings_map(unique_tickers)
    except Exception as e:
        print(f"[EARNINGS] Failed to load earnings for watchlist: {e}")

    return render_template("watchlist.html", filings=filings, market_caps=market_caps, earnings=earnings)


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

    # Fetch market caps for the selected filings' tickers
    market_caps = {}
    try:
        from market_cap import get_market_cap_map
        unique_tickers = list({f['ticker'] for f in filings if f.get('ticker')})
        market_caps = get_market_cap_map(unique_tickers)
    except Exception as e:
        print(f"[MARKET CAP] Failed to load market caps for email composer: {e}")

    return render_template("compose_email.html", filings=filings, market_caps=market_caps)


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

    # Show recent backfill runs so the user can see stats
    recent_runs = get_recent_backfill_runs(limit=10)
    return render_template("backfill.html", recent_runs=recent_runs)


@app.route("/resummarize", methods=["POST"])
def resummarize():
    """Re-run LLM summaries on existing filings (no re-fetch from SEC needed).

    Useful when the LLM was down or credits ran out during a backfill.
    Uses the raw_text already stored in the database."""
    date_from = request.form.get("date_from", "")
    date_to = request.form.get("date_to", "")
    model = request.form.get("model", "")

    # Run in background so the page doesn't hang
    thread = threading.Thread(
        target=run_resummarize,
        args=(date_from or None, date_to or None, model if model else None),
    )
    thread.daemon = True
    thread.start()

    date_label = f"{date_from} to {date_to}" if date_from else "most recent filings"
    model_label = model or "GPT-4o-mini"
    flash(f"Re-summarize started for {date_label} using {model_label}. Refresh the main page to see updated summaries.", "success")
    return redirect(url_for("index"))


def run_resummarize(date_from=None, date_to=None, model=None):
    """Background task: re-run LLM classification + summary on existing filings.

    Pulls raw_text from the database (already stored from the original backfill),
    sends it through the LLM, and updates the summary/category/subcategory fields.
    No SEC fetching needed — just LLM calls."""
    import json
    from llm import classify_and_summarize
    from fetcher import strip_cover_page

    model_label = model or "GPT-4o-mini"
    print(f"\n--- Re-summarize started (model: {model_label}) ---", flush=True)

    filings = get_filings_for_resummarize(date_from, date_to)

    if not filings:
        print("No filings found to re-summarize.", flush=True)
        return

    print(f"Found {len(filings)} filings to re-summarize", flush=True)

    updated = 0
    failed = 0

    for i, filing in enumerate(filings):
        company = filing.get("company", "Unknown")
        filing_id = filing["id"]
        raw_text = filing.get("raw_text", "")

        if not raw_text:
            print(f"  [{i+1}/{len(filings)}] {company} — no raw_text, skipping", flush=True)
            continue

        print(f"  [{i+1}/{len(filings)}] {company} — sending to LLM...", flush=True)

        # Strip cover page before sending to LLM (cleaner input = better output)
        cleaned_text = strip_cover_page(raw_text)

        llm_result = classify_and_summarize(cleaned_text, model=model)

        if llm_result and llm_result.get("relevant"):
            # LLM succeeded — update the database with new summary and classification
            summary = llm_result.get("summary") or ""
            category = llm_result.get("category") or filing.get("auto_category")
            subcategory = llm_result.get("subcategory") or filing.get("auto_subcategory")
            urgent = llm_result.get("urgent", False)
            comp_details = llm_result.get("comp_details")

            # Only store comp_details if it has real values
            comp_json = None
            if comp_details and any(v for v in comp_details.values()):
                comp_json = json.dumps(comp_details)

            update_filing_analysis(filing_id, summary, category, subcategory, urgent, comp_json)
            updated += 1

            tokens = llm_result.get("_tokens_in", 0) + llm_result.get("_tokens_out", 0)
            print(f"    Updated — {category} / {subcategory} ({tokens} tokens)", flush=True)

        elif llm_result and not llm_result.get("relevant"):
            # LLM says not relevant — keep existing data but log it
            print(f"    LLM says not relevant — keeping existing summary", flush=True)

        else:
            # LLM call failed again
            failed += 1
            print(f"    LLM FAILED — summary unchanged", flush=True)

    print(f"--- Re-summarize complete: {updated} updated, {failed} failed ---", flush=True)


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
    Pass model="gpt-5.2" to use the premium model for this backfill.

    NOTE: flush=True on every print() — Gunicorn runs with buffered stdout,
    so without it the daemon thread's output never appears in Render logs."""
    run_id = None
    try:
        import sys
        model_label = model or "GPT-4o-mini"
        print(f"\n--- Starting backfill: {start_date} to {end_date} (model: {model_label}) ---", flush=True)

        # Create a tracking record so we can see stats when this finishes
        run_id = create_backfill_run("web", start_date, end_date, model_label)

        # Step 1: Fetch filing metadata from EDGAR
        filings_metadata = fetch_filings(start_date, end_date)

        if not filings_metadata:
            print("No filings found in this date range", flush=True)
            complete_backfill_run(run_id, fetched=0, filtered=0, new=0, skipped=0)
            return

        print(f"[BACKFILL] {len(filings_metadata)} filings fetched, starting filter pipeline...", flush=True)

        # Step 2: Filter (Stage 1 + Stage 2), with optional model override for Stage 3
        matched_filings = filter_filings(filings_metadata, fetch_text_func=fetch_filing_text, model=model)

        print(f"[BACKFILL] Filter done — {len(matched_filings)} filings passed all stages", flush=True)

        # Step 3: Summarize and store each matched filing
        new_count = 0
        skipped_count = 0
        for filing in matched_filings:
            # Only generate a fallback summary if the LLM didn't already provide one
            if not filing.get("summary"):
                keywords = filing.get("matched_keywords", "").split(",")
                filing["summary"] = extract_summary(filing.get("raw_text", ""), keywords)

            # Save to database — returns True if this was a new filing
            was_new = insert_filing(filing)
            if was_new:
                new_count += 1
            else:
                skipped_count += 1

        print(f"--- Backfill complete: {new_count} new, {skipped_count} already existed ---", flush=True)

        # Record final stats for this run
        complete_backfill_run(run_id,
                             fetched=len(filings_metadata),
                             filtered=len(matched_filings),
                             new=new_count,
                             skipped=skipped_count)

        # Step 4: Pre-fetch market caps so the dashboard loads instantly
        # One batch call to yfinance for all new tickers, results get cached in DB
        tickers_to_fetch = list({f['ticker'] for f in matched_filings if f.get('ticker')})
        if tickers_to_fetch:
            try:
                from market_cap import get_market_cap_map
                print(f"[MARKET CAP] Pre-fetching market caps for {len(tickers_to_fetch)} tickers...", flush=True)
                get_market_cap_map(tickers_to_fetch)
                print(f"[MARKET CAP] Done — cached for next 24 hours", flush=True)
            except Exception as e:
                print(f"[MARKET CAP] Pre-fetch failed (not critical): {e}", flush=True)

            # Also pre-fetch earnings dates
            try:
                from earnings import get_earnings_map
                print(f"[EARNINGS] Pre-fetching earnings for {len(tickers_to_fetch)} tickers...", flush=True)
                get_earnings_map(tickers_to_fetch)
                print(f"[EARNINGS] Done — cached for next 12 hours", flush=True)
            except Exception as e:
                print(f"[EARNINGS] Pre-fetch failed (not critical): {e}", flush=True)

        # Record that a backfill completed (for front page display)
        update_last_backfill("web")
        print(f"[BACKFILL] All done — last_backfill timestamp updated", flush=True)

    except Exception as e:
        # Without this, daemon thread crashes are completely silent
        print(f"[BACKFILL ERROR] Backfill failed with exception: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        # Mark the run as failed so it shows up on the backfill page
        if run_id:
            try:
                complete_backfill_run(run_id, status="failed")
            except Exception:
                pass


# Initialize the database when the app starts
initialize_database()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Starting 8-K Filing Analyzer Dashboard...")
    print(f"Open http://127.0.0.1:{port} in your browser")
    app.run(host="0.0.0.0", port=port, debug=False)
