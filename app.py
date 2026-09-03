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
    update_departure_history,
    get_inbox_filings, count_inbox_filings, get_review_queue, count_review_queue,
    get_signal_type_counts, count_judgments, get_judgment,
    get_guidelines, add_guideline, deactivate_guideline,
    seed_judgments_from_watchlist,
    add_to_watchlist, remove_from_watchlist, update_watchlist_notes,
    get_watchlist_item, get_all_watchlist_ids, get_watchlist_filings,
    get_watchlist_filings_by_ids, mark_filings_email_sent,
    update_last_backfill, get_last_backfill, update_filing_analysis,
    update_deep_analysis, get_filings_for_resummarize,
    get_filings_missing_text, count_filings_missing_text, update_filing_raw_text,
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


def format_timestamp(value):
    """Render a datetime as 'Sep 02, 7:30 PM'.

    Uses only strftime directives that exist on every platform. The obvious
    way to drop the hour's leading zero is '%-I', which works on Linux and
    raises ValueError on Windows — and because it's inside a template, the
    failure surfaces as a 500 on the page rather than anything traceable.
    Strip the zero in Python instead.
    """
    if not value:
        return ""
    try:
        formatted = value.strftime("%b %d, %I:%M %p")
    except (AttributeError, ValueError):
        return str(value)
    # "Sep 02, 07:30 PM" -> "Sep 02, 7:30 PM"
    return formatted.replace(", 0", ", ", 1) if ", 0" in formatted else formatted

app.jinja_env.filters["format_timestamp"] = format_timestamp


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

    # Allow the login page itself (otherwise infinite redirect loop).
    #
    # label_from_token is also open: it arrives from a signed link in a digest
    # email, often on a phone with no session. The token IS the authentication
    # — it is signed with SECRET_KEY, scoped to one filing and one label, and
    # expires. Requiring a login here would mean the one-tap labelling path
    # costs a login, which is exactly the friction that stops it happening.
    if request.endpoint in ("login", "static", "label_from_token"):
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
def inbox():
    """The signal inbox — ranked by what the system thinks is worth reading.

    This is the answer to the complaint that started the rebuild: the old
    dashboard was a chronological archive of everything, so finding the three
    filings worth reading meant scanning past ninety that weren't. Here PASS
    is hidden, the strongest signal sorts first, and every row carries the
    named reason it is on screen.

    The chronological view still exists at /all — nothing was taken away.
    """
    import json

    days = _int_arg("days", 7, minimum=1, maximum=365)
    min_score = _int_arg("min_score", 0, minimum=0, maximum=10)
    page = _int_arg("page", 1, minimum=1)
    per_page = 50

    direction = request.args.get("direction", "").upper()
    if direction not in ("BEARISH", "BULLISH", "MIXED", "NEUTRAL"):
        direction = ""
    signal_type = request.args.get("signal_type", "").strip().upper() or None
    include_pass = request.args.get("include_pass", "") == "1"
    unlabeled_only = request.args.get("unlabeled", "") == "1"

    filters = dict(days=days, min_score=min_score, direction=direction or None,
                   signal_type=signal_type, include_pass=include_pass,
                   unlabeled_only=unlabeled_only)

    total = count_inbox_filings(**filters)
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)

    filings = get_inbox_filings(limit=per_page, offset=(page - 1) * per_page, **filters)

    # Parse the stored signals so the template can render chips without
    # re-deriving anything. Corrupt JSON degrades to an empty list rather than
    # taking down the page.
    for filing in filings:
        try:
            filing["_signals"] = json.loads(filing.get("signals_json") or "[]")
        except (ValueError, TypeError):
            filing["_signals"] = []

    tickers = list({f["ticker"] for f in filings if f.get("ticker")})
    market_caps, stock_prices = {}, {}
    try:
        from market_cap import get_market_cap_map
        market_caps = get_market_cap_map(tickers)
    except Exception as e:
        print(f"[INBOX] market caps unavailable: {e}")
    try:
        from stock_price import get_stock_price_map
        stock_prices = get_stock_price_map(tickers)
    except Exception as e:
        print(f"[INBOX] stock prices unavailable: {e}")

    from urllib.parse import urlencode
    filter_qs = urlencode([
        ("days", days), ("min_score", min_score), ("direction", direction),
        ("signal_type", signal_type or ""),
        ("include_pass", "1" if include_pass else ""),
        ("unlabeled", "1" if unlabeled_only else ""),
    ])

    return render_template(
        "inbox.html",
        filings=filings,
        total=total,
        total_pages=total_pages,
        current_page=page,
        filter_qs=filter_qs,
        signal_counts=get_signal_type_counts(days=max(days, 30)),
        labeled_counts=count_judgments(),
        review_remaining=count_review_queue(),
        watchlist_ids=get_all_watchlist_ids(),
        market_caps=market_caps,
        stock_prices=stock_prices,
        last_backfill=get_last_backfill(),
        current_days=days,
        current_min_score=min_score,
        current_direction=direction,
        current_signal_type=signal_type or "",
        current_include_pass=include_pass,
        current_unlabeled=unlabeled_only,
    )


def _int_arg(name, default, minimum=None, maximum=None):
    """Read an int query parameter without ever 500ing on junk input."""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


@app.route("/all")
def index():
    """The full chronological archive — the original dashboard, unchanged.

    Deliberately still named `index`: url_for("index") is used by the login
    redirect and by every row's back-link, and renaming the view would have
    broken them for no benefit.
    """
    # Get filter parameters from the URL query string
    category = request.args.get("category", "")
    search = request.args.get("search", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    urgent_only = request.args.get("urgent", "") == "1"
    market_targets_only = request.args.get("market_targets", "") == "1"
    unread_only = request.args.get("unread", "") == "1"
    # Triage verdict filter: DEEP_LOOK / MONITOR / PASS / actionable (= first two)
    verdict = request.args.get("verdict", "")
    # Direction filter: BEARISH / BULLISH / MIXED / NEUTRAL
    direction = request.args.get("direction", "").upper()
    if direction not in ("BEARISH", "BULLISH", "MIXED", "NEUTRAL"):
        direction = ""
    # Bearish sub-signal toggles: forfeited comp, departure clusters
    forfeited_only = request.args.get("forfeited", "") == "1"
    clusters_only = request.args.get("clusters", "") == "1"
    # Sort: "date" (newest first) or "signal" (Deep Look first, then by score)
    sort = request.args.get("sort", "date")
    if sort not in ("date", "signal"):
        sort = "date"
    # Page must never 500 on garbage input, and an out-of-range page must
    # clamp to the last real page instead of dead-ending on an empty list.
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)

    per_page = 50

    # Count first so we can clamp the page before fetching
    filtered_count = get_filtered_filing_count(
        category=category if category else None,
        search=search if search else None,
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        urgent_only=urgent_only,
        market_targets_only=market_targets_only,
        unread_only=unread_only,
        verdict=verdict if verdict else None,
        direction=direction if direction else None,
        forfeited_only=forfeited_only,
        clusters_only=clusters_only,
    )
    total_pages = max(1, math.ceil(filtered_count / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    # Fetch filtered filings from the database
    filings = get_filings(
        category=category if category else None,
        search=search if search else None,
        date_from=date_from if date_from else None,
        date_to=date_to if date_to else None,
        urgent_only=urgent_only,
        market_targets_only=market_targets_only,
        unread_only=unread_only,
        verdict=verdict if verdict else None,
        direction=direction if direction else None,
        forfeited_only=forfeited_only,
        clusters_only=clusters_only,
        sort=sort,
        limit=per_page,
        offset=offset,
    )

    # Convert to plain dicts so .get() works on both SQLite and PostgreSQL
    filings = [dict(f) for f in filings]

    import json

    # Get all categories for the filter dropdown
    categories = get_categories()
    total_count = get_filing_count()

    # Get last backfill info for the header display
    last_backfill = get_last_backfill()

    # Get watchlisted filing IDs so we can show star icons
    watchlist_ids = get_all_watchlist_ids()

    # Fetch market cap data for tickers on this page
    # Wrapped in try/except so a yfinance failure never breaks the dashboard
    unique_tickers = list({f['ticker'] for f in filings if f.get('ticker')})
    market_caps = {}
    try:
        from market_cap import get_market_cap_map
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

    # Fetch current stock prices for tickers on this page
    stock_prices = {}
    try:
        from stock_price import get_stock_price_map
        stock_prices = get_stock_price_map(unique_tickers)
    except Exception as e:
        print(f"[STOCK PRICE] Failed to load stock prices: {e}")

    # % appreciation required for stock-price hurdles vs the current price —
    # turns the bare 🎯 badge into "🎯 +120%" so bullish conviction is
    # rankable at a glance. Cached prices only; rows without a price skip it.
    from market_targets import annotate_price_targets
    target_pcts = {}
    for filing in filings:
        if not filing.get("has_market_targets"):
            continue
        price = stock_prices.get((filing.get("ticker") or "").strip().upper())
        if not price:
            continue
        try:
            structured = json.loads(filing.get("structured_summary") or "{}")
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(structured, dict):
            continue  # corrupt/legacy non-object JSON must not 500 the dashboard
        tp = annotate_price_targets(structured.get("market_targets"), price)
        if tp:
            target_pcts[filing["id"]] = tp

    # Canonical query string for the current filter state (page excluded).
    # Pagination links and row back-links both use this, so they can't drift
    # apart — and values are properly encoded (a search containing '&' or
    # spaces used to break every pagination link).
    from urllib.parse import urlencode
    filter_qs = urlencode([
        ("category", category),
        ("search", search),
        ("date_from", date_from),
        ("date_to", date_to),
        ("urgent", "1" if urgent_only else ""),
        ("market_targets", "1" if market_targets_only else ""),
        ("unread", "1" if unread_only else ""),
        ("verdict", verdict),
        ("direction", direction),
        ("forfeited", "1" if forfeited_only else ""),
        ("clusters", "1" if clusters_only else ""),
        ("sort", sort),
    ])

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
        current_market_targets=market_targets_only,
        current_unread=unread_only,
        current_verdict=verdict,
        current_direction=direction,
        current_forfeited=forfeited_only,
        current_clusters=clusters_only,
        current_sort=sort,
        current_page=page,
        per_page=per_page,
        total_pages=total_pages,
        filter_qs=filter_qs,
        watchlist_ids=watchlist_ids,
        market_caps=market_caps,
        earnings=earnings,
        stock_prices=stock_prices,
        target_pcts=target_pcts,
    )


@app.route("/filing/<int:filing_id>")
def filing_detail(filing_id):
    """Detail page for a single filing."""
    # Opening a filing counts as reading it. Idempotent — no-op if already read.
    from database import mark_filings_read
    mark_filings_read([filing_id])
    return _render_filing_detail(filing_id)


def _render_filing_detail(filing_id, departures=None):
    """Render the filing detail page. Optional `departures` dict shows the
    Executive Departures card (used by the /deep-analysis dispatch)."""
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

    # No fresh departures passed in? Render the card from the history stamped
    # at ingest (EDGAR-based) so the 24mo view shows up without any clicking.
    # The dropdown's "Executive Departures (24mo)" option still does a live
    # refresh and overwrites the stored copy.
    if departures is None and filing.get("departure_history"):
        try:
            stored = json.loads(filing["departure_history"])
        except (json.JSONDecodeError, TypeError):
            stored = None
        if stored:
            from departures import render_prose_lines
            departures = {
                "lines": render_prose_lines(stored),
                "count_filings": len({d.get("_accession") for d in stored if d.get("_accession")}),
                "company": filing.get("company", "Unknown"),
                "cik": filing.get("cik", ""),
            }

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

    # Current price + % appreciation required for any stock-price hurdles
    stock_price = None
    target_pcts = {}
    if filing.get("ticker"):
        try:
            from stock_price import get_stock_price_map
            price_map = get_stock_price_map([filing["ticker"]])
            stock_price = price_map.get(filing["ticker"].strip().upper())
        except Exception as e:
            print(f"[STOCK PRICE] Failed for {filing.get('ticker')}: {e}")
    if stock_price and filing.get("has_market_targets"):
        try:
            from market_targets import annotate_price_targets
            structured = json.loads(filing.get("structured_summary") or "{}")
            if isinstance(structured, dict):
                tp = annotate_price_targets(structured.get("market_targets"), stock_price)
                if tp:
                    target_pcts[filing["id"]] = tp
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    return render_template(
        "filing.html",
        filing=filing,
        tag_options=tag_options,
        back_url=back_url,
        is_watchlisted=is_watchlisted,
        watchlist_notes=watchlist_notes,
        market_cap=market_cap,
        earnings_info=earnings_info,
        stock_price=stock_price,
        target_pcts=target_pcts,
        departures=departures,
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
        filing = dict(filing)  # Convert sqlite3.Row to real dict so .get() works (CLAUDE.md compatibility)

        # If the user picked the "Executive Departures (24mo)" option, run that
        # pipeline and re-render the filing page directly (no LLM signal-analysis call).
        if request.form.get("prompt_version") == "departures_24mo":
            from departures import get_departures_for_filing, render_prose_lines, count_real_departures

            cik = filing.get("cik", "") or ""
            current_accession = filing.get("accession_no", "") or ""

            if not cik:
                flash("This filing has no CIK on record — cannot look up departures.", "error")
                return redirect(url_for("filing_detail", filing_id=filing_id))

            try:
                departures_data = get_departures_for_filing(cik=cik, current_accession=current_accession)
            except RuntimeError:
                # EDGAR lookup failed (transient) — tell the user plainly
                # instead of surfacing a raw exception via the generic handler.
                flash("EDGAR is unreachable right now — departure history lookup failed. Try again in a minute.", "error")
                return redirect(url_for("filing_detail", filing_id=filing_id))
            departures_lines = render_prose_lines(departures_data)

            # Persist so the badge + auto-rendered card stay current (and so a
            # single click stamps legacy filings ingested before enrichment).
            import json as _json
            update_departure_history(
                filing_id, count_real_departures(departures_data), _json.dumps(departures_data)
            )

            departures_context = {
                "lines": departures_lines,
                "count_filings": len({d["_accession"] for d in departures_data}),
                "company": filing.get("company", "Unknown"),
                "cik": cik,
            }

            return _render_filing_detail(filing_id, departures=departures_context)

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
            # Market cap — use sync refresh so the LLM gets current data,
            # not whatever stale value the dashboard happens to be showing
            try:
                from market_cap import refresh_market_caps_sync
                caps = refresh_market_caps_sync([ticker])
                mcap_val = caps.get(ticker.strip().upper())
                mcap_str = format_market_cap(mcap_val) if mcap_val else "Not available"
            except Exception:
                mcap_str = "Not available"

            # Next earnings date — same reasoning: sync fetch for the LLM
            try:
                from earnings import refresh_earnings_sync
                e_map = refresh_earnings_sync([ticker])
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
            if departures is None:
                # EDGAR lookup failed — tell the LLM the data gap explicitly
                # (its prompt has data-quality gates) instead of implying zero.
                departures = []
                departure_str = "Departure history unavailable — EDGAR lookup failed"
            elif departures:
                dep_lines = []
                for dep in departures:
                    date = dep.get("filing_date", "Unknown date")
                    # Snippets are now full 5.02 sections (up to 6k chars) —
                    # cap what goes into the context block so a serial filer
                    # can't flood the prompt.
                    snippet = (dep.get("snippet", "") or "")[:1500]
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


@app.route("/review")
def review():
    """One filing at a time, two keys, no scrolling.

    The labels are what make everything downstream measurable — evaluation,
    few-shot examples for the judge, per-signal-type precision. None of that
    exists without a few hundred of them, so the only design goal here is that
    a label costs a single keystroke and the next filing is already on screen.

    Ordered by signal strength rather than by date: the labels worth having
    are on the filings the system was most confident about, because that is
    where being wrong is most expensive.
    """
    import json

    queue = get_review_queue(limit=1)
    filing = queue[0] if queue else None
    if filing:
        try:
            filing["_signals"] = json.loads(filing.get("signals_json") or "[]")
        except (ValueError, TypeError):
            filing["_signals"] = []
        try:
            filing["_judge"] = json.loads(filing.get("judge_json") or "null")
        except (ValueError, TypeError):
            filing["_judge"] = None

    return render_template(
        "review.html",
        filing=filing,
        remaining=count_review_queue(),
        labeled_counts=count_judgments(),
        guidelines=get_guidelines(),
    )


@app.route("/api/label", methods=["POST"])
def api_label():
    """Record a label from the review page or an inbox row (AJAX)."""
    from labels import record, undo

    payload = request.get_json(silent=True) or {}
    try:
        filing_id = int(payload.get("filing_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "filing_id must be an integer"}), 400

    label = payload.get("label")
    if label == "undo":
        return jsonify({"success": undo(filing_id), "label": None})

    if not record(filing_id, label, note=payload.get("note"), source="review_ui"):
        return jsonify({"error": f"unknown label {label!r}"}), 400
    return jsonify({"success": True, "label": label, "remaining": count_review_queue()})


@app.route("/label/<token>", methods=["GET", "POST"])
def label_from_token(token):
    """Labelling from a digest email — no login, no app.

    Exempt from the trial gate (see check_trial_access): the signed token is
    the credential.

    **GET never writes.** Corporate mail scanners (Outlook SafeLinks, Gmail's
    prefetch) request every URL in an email before the recipient sees it. A
    digest row carries both a Signal and a Noise link, so a scanner would
    fetch both and the last one would win — quietly filling the training set
    with labels nobody chose, and overwriting deliberate ones made in
    /review. So GET renders a confirm page and the POST does the work: one
    tap in the mail client, one tap on the page.
    """
    from labels import read_token, record, undo

    filing_id, label = read_token(token)
    if not filing_id or not label:
        return render_template("label_done.html", filing=None, label=None,
                               state="invalid", token=None), 400

    filing = get_filing_by_id(filing_id)
    if filing is None:
        return render_template("label_done.html", filing=None, label=None,
                               state="invalid", token=None), 404
    filing = dict(filing)

    if request.method == "GET":
        return render_template("label_done.html", filing=filing, label=label,
                               state="confirm", token=token,
                               existing=get_judgment(filing_id))

    if request.form.get("action") == "undo":
        undo(filing_id)
        return render_template("label_done.html", filing=filing, label=None,
                               state="undone", token=token)

    record(filing_id, label, source="digest_link")
    return render_template("label_done.html", filing=filing, label=label,
                           state="saved", token=token)


@app.route("/guidelines", methods=["POST"])
def add_guideline_route():
    """Add a standing rule for the judge, in the user's own words.

    Cheaper than a prompt edit and immediately effective: the rules are loaded
    into every judgment. "Ignore SPAC director shuffles" is a one-line fix for
    a whole category of noise.
    """
    rule = (request.form.get("rule") or "").strip()
    if rule:
        add_guideline(rule)
        flash("Guideline added — it applies to the next filings analyzed.", "success")
    return redirect(request.referrer or url_for("review"))


@app.route("/guidelines/<int:guideline_id>/remove", methods=["POST"])
def remove_guideline_route(guideline_id):
    deactivate_guideline(guideline_id)
    flash("Guideline removed.", "success")
    return redirect(request.referrer or url_for("review"))


@app.route("/seed-labels", methods=["POST"])
def seed_labels():
    """Turn existing watchlist stars into positive labels, once.

    Starring was the only "this matters" gesture the old UI had, so it is the
    closest thing to a pre-existing training set.
    """
    seeded = seed_judgments_from_watchlist()
    flash(f"Seeded {seeded} label(s) from your watchlist.", "success")
    return redirect(url_for("review"))


@app.route("/api/filings/mark-read", methods=["POST"])
def api_mark_filings_read():
    """Batch-mark filings as read.

    Called from dashboard scroll-tracking JS. Idempotent — already-read filings
    are silently skipped (see database.mark_filings_read).

    Request body (JSON): {"filing_ids": [1, 2, 3]}
    Response (JSON):     {"marked": <int>}
    """
    payload = request.get_json(silent=True) or {}
    filing_ids = payload.get("filing_ids")

    # Validate it's a list
    if not isinstance(filing_ids, list):
        return jsonify({"error": "filing_ids must be a list"}), 400

    # Validate every entry is an int (and reject bool, which is a subclass of int)
    cleaned = []
    for fid in filing_ids:
        if isinstance(fid, bool):
            return jsonify({"error": "filing_ids must be integers"}), 400
        if isinstance(fid, int):
            cleaned.append(fid)
        else:
            return jsonify({"error": "filing_ids must be integers"}), 400

    from database import mark_filings_read
    marked = mark_filings_read(cleaned)
    return jsonify({"marked": marked})


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

        model_label = model or "GPT-5.4-nano"
        flash(f"Backfill started for {start_date} to {end_date} using {model_label}. This runs in the background — refresh the main page to see new filings as they appear.", "success")
        return redirect(url_for("index"))

    # Show recent backfill runs so the user can see stats, plus how many rows
    # a rate-limited run left stranded without text (and so without a summary).
    recent_runs = get_recent_backfill_runs(limit=10)
    try:
        missing_text_count = count_filings_missing_text()
    except Exception as e:
        print(f"[BACKFILL PAGE] WARN: could not count stranded filings: {e}", flush=True)
        missing_text_count = None
    return render_template(
        "backfill.html",
        recent_runs=recent_runs,
        missing_text_count=missing_text_count,
    )


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
    model_label = model or "GPT-5.4-nano"
    flash(f"Re-summarize started for {date_label} using {model_label}. Refresh the main page to see updated summaries.", "success")
    return redirect(url_for("index"))


def run_resummarize(date_from=None, date_to=None, model=None):
    """Background task: re-run analysis on filings whose text is already stored.

    No SEC fetching — this reads raw_text from the database and sends it back
    through the shared pipeline. Use it after a prompt or signal-weight change
    to re-rank history, or when the model was unavailable during a backfill.
    """
    from pipeline import analyze_filing, persist

    model_label = model or "default"
    print(f"\n--- Re-analysis started (model: {model_label}) ---", flush=True)

    filings = get_filings_for_resummarize(date_from, date_to)
    if not filings:
        print("No filings found to re-analyze.", flush=True)
        return

    print(f"Found {len(filings)} filings to re-analyze", flush=True)

    updated = failed = no_text = irrelevant = 0
    tokens_in = tokens_out = 0

    for i, filing in enumerate(filings):
        filing = dict(filing)
        company = filing.get("company", "Unknown")

        if not filing.get("raw_text"):
            no_text += 1
            print(f"  [{i+1}/{len(filings)}] {company} — no stored text, skipping", flush=True)
            continue

        print(f"  [{i+1}/{len(filings)}] {company} — analyzing...", flush=True)
        result = analyze_filing(filing, model=model)
        tokens_in += result.tokens_in
        tokens_out += result.tokens_out

        if result.error:
            failed += 1
            print(f"    FAILED ({result.error}) — row unchanged", flush=True)
            continue
        if not result.relevant:
            irrelevant += 1
            print(f"    Not relevant — keeping existing row", flush=True)
            continue

        persist(filing["id"], result)
        updated += 1
        badge = ",".join(result.signal_types) or "no signals"
        print(f"    {result.fields['triage_verdict']} "
              f"{result.fields['signal_score']}/10 [{badge}]", flush=True)

    print(f"--- Re-analysis complete: {updated} updated, {failed} failed, "
          f"{irrelevant} not relevant, {no_text} without text "
          f"({tokens_in:,} in / {tokens_out:,} out) ---", flush=True)

    # Re-analysis only reads text already in the database, so filings stranded
    # by a SEC rate-limit block are invisible to it — it would otherwise report
    # success while never touching the very rows that look broken on the
    # dashboard. Say so, and name the button that does fix them.
    try:
        stranded = count_filings_missing_text()
    except Exception:
        stranded = None
    if stranded:
        print(f"    NOTE: {stranded} filing(s) still have no stored SEC text and "
              f"cannot be re-analyzed. Use 'Retry Missing Summaries' — it "
              f"re-fetches from SEC first.", flush=True)


@app.route("/retrofit-market-targets", methods=["POST"])
def retrofit_market_targets_route():
    """Walk every existing filing's structured_summary JSON and flag the ones
    that disclose market-based comp targets (stock-price, market-cap, or TSR).
    No LLM calls — pure data transform on what's already stored."""
    from retrofit_market_targets import run_retrofit
    from database import create_backfill_run

    # Track this as a backfill_run so it shows up in the recent-runs list
    try:
        run_id = create_backfill_run(
            backfill_type="market_targets_retrofit",
            date_start=None,
            date_end=None,
            model=None,
        )
    except Exception as e:
        print(f"[RETROFIT] WARN: could not create backfill_run row: {e}", flush=True)
        run_id = None

    def _worker():
        run_retrofit(verbose=True, run_id=run_id)

    thread = threading.Thread(target=_worker)
    thread.daemon = True
    thread.start()

    flash("Market-targets retrofit started. Refresh the dashboard in a minute to see flagged filings.", "success")
    return redirect(url_for("backfill"))


@app.route("/backfill-departure-history", methods=["POST"])
def backfill_departure_history_route():
    """Stamp EDGAR-based 24-month departure history onto existing filings that
    contain departures. New filings get this automatically at ingest; this
    button covers everything ingested before the feature existed.

    Cost: one EDGAR lookup per company + a small LLM extraction per historical
    5.02 filing (cached per accession, so re-runs and overlapping companies
    are nearly free)."""
    from departures import run_history_backfill
    from database import create_backfill_run

    try:
        run_id = create_backfill_run(
            backfill_type="departure_history",
            date_start=None,
            date_end=None,
            model=None,
        )
    except Exception as e:
        print(f"[DEPARTURES BACKFILL] WARN: could not create backfill_run row: {e}", flush=True)
        run_id = None

    def _worker():
        run_history_backfill(run_id=run_id, verbose=True)

    thread = threading.Thread(target=_worker)
    thread.daemon = True
    thread.start()

    flash("Departure-history backfill started. Cluster badges will appear on the dashboard as filings are processed — watch the logs.", "success")
    return redirect(url_for("backfill"))


@app.route("/retry-missing-summaries", methods=["POST"])
def retry_missing_summaries():
    """Re-fetch SEC text + run LLM for filings that got saved with empty raw_text.

    These are the rows that Re-Summarize can't fix (it requires raw_text),
    caused by transient SEC fetch failures during the original backfill."""
    date_from = request.form.get("date_from", "")
    date_to = request.form.get("date_to", "")
    model = request.form.get("model", "")

    thread = threading.Thread(
        target=run_retry_missing_summaries,
        args=(date_from or None, date_to or None, model if model else None),
    )
    thread.daemon = True
    thread.start()

    date_label = f"{date_from} to {date_to}" if date_from and date_to else "all dates"
    model_label = model or "GPT-5.4-nano"
    flash(f"Retry started for filings missing summaries ({date_label}, {model_label}). "
          f"If SEC is rate-limiting, the job now waits out the block instead of failing — "
          f"it can pause for several minutes at a time. Watch the logs.", "success")
    return redirect(url_for("index"))


def run_retry_missing_summaries(date_from=None, date_to=None, model=None):
    """Background task: re-fetch SEC text for rows that have none, then analyze.

    These are the rows Re-Analyze cannot fix, because it reads stored text and
    these have none — the residue of a SEC rate-limit block during a backfill.
    """
    from pipeline import analyze_filing, persist

    model_label = model or "default"
    print(f"\n--- Retry missing summaries started (model: {model_label}) ---", flush=True)

    filings = get_filings_missing_text(date_from, date_to)
    if not filings:
        print("No filings found with missing text.", flush=True)
        return

    scope = f"{date_from} to {date_to}" if date_from and date_to else "all dates"
    print(f"Found {len(filings)} filings missing raw_text ({scope})", flush=True)

    fetched = updated = fetch_failed = analysis_failed = 0

    for i, filing in enumerate(filings):
        filing = dict(filing)
        company = filing.get("company", "Unknown")

        print(f"  [{i+1}/{len(filings)}] {company} — re-fetching SEC text...", flush=True)
        text, doc_url = fetch_filing_text(
            filing.get("filing_url", ""), filing.get("cik", ""),
            filing.get("accession_no", ""),
        )

        if not text:
            print(f"    Fetch failed — still no text available", flush=True)
            fetch_failed += 1
            continue

        update_filing_raw_text(filing["id"], text, filing_document_url=doc_url)
        filing["raw_text"] = text
        fetched += 1
        print(f"    Fetched {len(text)} chars, analyzing...", flush=True)

        # Same pipeline as every other path — which is the point. This path
        # used to be its own copy of the field mapping and silently lost
        # market-target detection, so rescued filings were permanently missing
        # their hurdle flag with nothing to indicate it.
        result = analyze_filing(filing, text=text, model=model)
        if result.error:
            print(f"    ANALYSIS FAILED ({result.error}) — text saved", flush=True)
            analysis_failed += 1
            continue
        if not result.relevant:
            print(f"    Not relevant — keeping placeholder", flush=True)
            continue

        persist(filing["id"], result)
        updated += 1
        badge = ",".join(result.signal_types) or "no signals"
        print(f"    {result.fields['triage_verdict']} "
              f"{result.fields['signal_score']}/10 [{badge}]", flush=True)

    print(f"--- Retry complete: {fetched} fetched, {updated} analyzed, "
          f"{fetch_failed} fetch-failed, {analysis_failed} analysis-failed ---", flush=True)


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
    Pass model="gpt-5.4" to use the premium model for this backfill.

    NOTE: flush=True on every print() — Gunicorn runs with buffered stdout,
    so without it the daemon thread's output never appears in Render logs."""
    run_id = None
    try:
        import sys
        model_label = model or "GPT-5.4-nano"
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

        # Step 3b: Stamp departure filings with their company's 24-month
        # EDGAR departure history (cluster badge + instant detail card).
        try:
            from departures import enrich_new_filings
            enrich_new_filings(matched_filings)
        except Exception as e:
            print(f"[DEPARTURES] History enrichment failed (not critical): {e}", flush=True)

        # Step 4: Pre-fetch market caps / earnings / stock prices so the
        # dashboard has data ready. Use the *_sync variants — this is a
        # background job, nobody is waiting on a page load.
        tickers_to_fetch = list({f['ticker'] for f in matched_filings if f.get('ticker')})
        if tickers_to_fetch:
            try:
                from market_cap import refresh_market_caps_sync
                print(f"[MARKET CAP] Pre-fetching market caps for {len(tickers_to_fetch)} tickers...", flush=True)
                refresh_market_caps_sync(tickers_to_fetch)
                print(f"[MARKET CAP] Done", flush=True)
            except Exception as e:
                print(f"[MARKET CAP] Pre-fetch failed (not critical): {e}", flush=True)

            try:
                from earnings import refresh_earnings_sync
                print(f"[EARNINGS] Pre-fetching earnings for {len(tickers_to_fetch)} tickers...", flush=True)
                refresh_earnings_sync(tickers_to_fetch)
                print(f"[EARNINGS] Done", flush=True)
            except Exception as e:
                print(f"[EARNINGS] Pre-fetch failed (not critical): {e}", flush=True)

            try:
                from stock_price import refresh_stock_prices_sync
                print(f"[STOCK PRICE] Pre-fetching prices for {len(tickers_to_fetch)} tickers...", flush=True)
                refresh_stock_prices_sync(tickers_to_fetch)
                print(f"[STOCK PRICE] Done", flush=True)
            except Exception as e:
                print(f"[STOCK PRICE] Pre-fetch failed (not critical): {e}", flush=True)

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
