# database.py — Database setup and helper functions
# Supports PostgreSQL (when DATABASE_URL is set, e.g. on Render)
# and SQLite (local development fallback)

import os
import ssl
import sqlite3
import threading
from config import DATABASE_PATH

# Try to import pg8000 for PostgreSQL support
# pg8000 is pure Python (no C compilation needed), so it installs reliably everywhere
try:
    import pg8000.dbapi
    HAS_PG = True
    print("[BOOT] pg8000 is installed ✓")
except ImportError:
    HAS_PG = False
    print("[BOOT] pg8000 is NOT installed — PostgreSQL unavailable")


def _get_database_url():
    """Read DATABASE_URL fresh from environment every time.
    This ensures gunicorn workers always pick it up."""
    return os.environ.get("DATABASE_URL")


def _using_postgres():
    """Check if we should use PostgreSQL (DATABASE_URL is set and pg8000 available)."""
    return _get_database_url() is not None and HAS_PG


# ============================================================
# CONNECTION POOL for PostgreSQL
# Reuses database connections instead of opening a new one for
# every query. Each new connection costs 50-200ms (TCP + SSL +
# auth over the network). A pool turns that into <1ms.
# ============================================================

_pg_pool = []           # list of idle PostgreSQL connections
_pg_pool_lock = threading.Lock()
_PG_POOL_MAX = 5        # max idle connections to keep around


def _parse_database_url():
    """Parse DATABASE_URL into the components pg8000 needs."""
    url = _get_database_url()
    if url.startswith("postgres://"):
        url = url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = url[len("postgresql://"):]

    user_info, rest = url.split("@", 1)
    host_info, dbname = rest.split("/", 1)
    user, password = user_info.split(":", 1)
    if ":" in host_info:
        host, port = host_info.split(":", 1)
        port = int(port)
    else:
        host = host_info
        port = 5432

    return user, password, host, port, dbname


def _create_pg_connection():
    """Create a fresh PostgreSQL connection."""
    user, password, host, port, dbname = _parse_database_url()
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    return pg8000.dbapi.connect(
        user=user,
        password=password,
        host=host,
        port=port,
        database=dbname,
        ssl_context=ssl_context
    )


def _get_pg_connection():
    """Get a PostgreSQL connection from the pool, or create a new one."""
    with _pg_pool_lock:
        while _pg_pool:
            conn = _pg_pool.pop()
            try:
                # Quick check that the connection is still alive
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                return conn
            except Exception:
                # Connection went stale — discard it and try next
                try:
                    conn.close()
                except Exception:
                    pass

    # Pool was empty (or all stale) — create a fresh connection
    return _create_pg_connection()


def _return_pg_connection(conn):
    """Return a PostgreSQL connection to the pool for reuse."""
    try:
        # Reset any uncommitted transaction state so the next user gets a clean connection
        conn.rollback()
    except Exception:
        # Connection is broken — discard it
        try:
            conn.close()
        except Exception:
            pass
        return

    with _pg_pool_lock:
        if len(_pg_pool) < _PG_POOL_MAX:
            _pg_pool.append(conn)
        else:
            # Pool is full — close the extra connection
            try:
                conn.close()
            except Exception:
                pass


def get_connection():
    """Get a database connection. Uses the PostgreSQL pool if DATABASE_URL is set,
    otherwise falls back to local SQLite file.
    IMPORTANT: Always call conn.close() when done — for PostgreSQL this returns
    the connection to the pool instead of actually closing it."""
    if _using_postgres():
        conn = _get_pg_connection()
        # Override close() so callers return the connection to the pool
        # instead of actually closing it. This means all existing code
        # (which calls conn.close()) works without any changes.
        conn._real_close = conn.close
        conn.close = lambda: _return_pg_connection(conn)
        return conn
    else:
        conn = sqlite3.connect(DATABASE_PATH)
        # This makes query results accessible by column name (e.g., row["company"])
        conn.row_factory = sqlite3.Row
        return conn


def _placeholder():
    """Return the right placeholder style for the current database.
    SQLite uses ?, PostgreSQL uses %s."""
    return "%s" if _using_postgres() else "?"


def _dict_row(row, cursor):
    """Convert a database row to a dictionary.
    SQLite rows are already dict-like, but pg8000 rows need conversion."""
    if row is None:
        return None
    if _using_postgres():
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
    else:
        return row  # sqlite3.Row already supports dict-like access


def _dict_rows(rows, cursor):
    """Convert multiple database rows to dictionaries."""
    if _using_postgres():
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]
    else:
        return rows


def initialize_database():
    """Create the filings table if it doesn't exist yet.
    Called once when the app starts up."""
    # Safety check: if DATABASE_URL is set but pg8000 isn't available,
    # crash instead of silently using SQLite (which loses data on Render)
    db_url = _get_database_url()
    print(f"[STARTUP] DATABASE_URL is {'SET' if db_url else 'NOT SET'}")
    print(f"[STARTUP] HAS_PG = {HAS_PG}")
    if db_url and not HAS_PG:
        raise RuntimeError(
            "DATABASE_URL is set but pg8000 is not installed! "
            "Run: pip install pg8000"
        )

    conn = get_connection()
    cursor = conn.cursor()

    if _using_postgres():
        # PostgreSQL version: uses SERIAL for auto-increment
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filings (
                id SERIAL PRIMARY KEY,
                accession_no TEXT UNIQUE,
                company TEXT,
                ticker TEXT,
                cik TEXT,
                filed_date TEXT,
                item_codes TEXT,
                summary TEXT,
                auto_category TEXT,
                auto_subcategory TEXT,
                user_tag TEXT,
                filing_url TEXT,
                raw_text TEXT,
                matched_keywords TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        # SQLite version (original)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                accession_no TEXT UNIQUE,
                company TEXT,
                ticker TEXT,
                cik TEXT,
                filed_date TEXT,
                item_codes TEXT,
                summary TEXT,
                auto_category TEXT,
                auto_subcategory TEXT,
                user_tag TEXT,
                filing_url TEXT,
                raw_text TEXT,
                matched_keywords TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    conn.commit()

    # Migrate: add new columns if they don't exist yet (safe to run repeatedly)
    _migrate_add_columns(conn)

    # Create watchlist table if it doesn't exist
    _create_watchlist_table(conn)

    # Create app_status table for tracking backfill times, etc.
    _create_app_status_table(conn)

    # Create backfill_runs table for tracking stats from each backfill
    _create_backfill_runs_table(conn)

    # Create market_caps table for caching stock market cap data
    _create_market_caps_table(conn)

    # Create earnings_cache table for caching next earnings dates
    _create_earnings_cache_table(conn)

    # Create stock_prices table for caching current stock prices
    _create_stock_prices_table(conn)

    # Log which database we're using and how many filings are stored
    # This helps us debug data loss issues on Render
    cursor.execute("SELECT COUNT(*) FROM filings")
    count = cursor.fetchone()[0]
    if _using_postgres():
        print(f"[STARTUP] Using PostgreSQL — {count} filings in database")
    else:
        print(f"[STARTUP] Using SQLite — {count} filings in database")

    conn.close()


def _migrate_add_columns(conn):
    """Add new columns for urgency flags and comp details.
    Uses ALTER TABLE so existing data is preserved (new columns get NULL/0)."""
    cursor = conn.cursor()

    # Figure out which columns already exist
    if _using_postgres():
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'filings'
        """)
        existing = {row[0] for row in cursor.fetchall()}
    else:
        cursor.execute("PRAGMA table_info(filings)")
        existing = {row[1] for row in cursor.fetchall()}

    # Add urgent flag (0 or 1) if missing
    if "urgent" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN urgent INTEGER DEFAULT 0")
        print("[MIGRATE] Added 'urgent' column")

    # Add comp_details JSON blob if missing
    if "comp_details" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN comp_details TEXT DEFAULT NULL")
        print("[MIGRATE] Added 'comp_details' column")

    # Add deep_analysis text column for comprehensive investor analysis
    if "deep_analysis" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN deep_analysis TEXT DEFAULT NULL")
        print("[MIGRATE] Added 'deep_analysis' column")

    conn.commit()


def _create_watchlist_table(conn):
    """Create the watchlist table for saving filings with notes.
    Called during database initialization."""
    cursor = conn.cursor()

    if _using_postgres():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                filing_id INTEGER NOT NULL UNIQUE,
                notes TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (filing_id) REFERENCES filings(id) ON DELETE CASCADE
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filing_id INTEGER NOT NULL UNIQUE,
                notes TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (filing_id) REFERENCES filings(id) ON DELETE CASCADE
            )
        """)

    conn.commit()

    # Migrate: add email_sent_at column if it doesn't exist yet
    # This tracks when a filing was included in a weekly email
    if _using_postgres():
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'watchlist'
        """)
        existing = {row[0] for row in cursor.fetchall()}
    else:
        cursor.execute("PRAGMA table_info(watchlist)")
        existing = {row[1] for row in cursor.fetchall()}

    if "email_sent_at" not in existing:
        cursor.execute("ALTER TABLE watchlist ADD COLUMN email_sent_at TIMESTAMP NULL")
        conn.commit()
        print("[MIGRATE] Added 'email_sent_at' column to watchlist")

    print("[STARTUP] Watchlist table ready")


def filing_exists(accession_no):
    """Check if we already have this filing in the database.
    Prevents duplicate entries when re-fetching."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(f"SELECT 1 FROM filings WHERE accession_no = {p}", (accession_no,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def _to_str(value):
    """Convert a value to a string for database storage.
    Lists get joined with commas, everything else passes through as-is."""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return value


def insert_filing(filing_data):
    """Save a new filing to the database.
    filing_data is a dictionary with keys matching the column names.
    Returns True if the filing was new (inserted), False if it already existed."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    # Convert urgent boolean to integer for database storage
    urgent_val = 1 if filing_data.get("urgent") else 0
    comp_details_val = _to_str(filing_data.get("comp_details"))

    if _using_postgres():
        # PostgreSQL: use ON CONFLICT instead of INSERT OR IGNORE
        cursor.execute(f"""
            INSERT INTO filings
            (accession_no, company, ticker, cik, filed_date, item_codes,
             summary, auto_category, auto_subcategory, filing_url, raw_text,
             matched_keywords, urgent, comp_details)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
            ON CONFLICT (accession_no) DO NOTHING
        """, (
            _to_str(filing_data.get("accession_no")),
            _to_str(filing_data.get("company")),
            _to_str(filing_data.get("ticker")),
            _to_str(filing_data.get("cik")),
            _to_str(filing_data.get("filed_date")),
            _to_str(filing_data.get("item_codes")),
            _to_str(filing_data.get("summary")),
            _to_str(filing_data.get("auto_category")),
            _to_str(filing_data.get("auto_subcategory")),
            _to_str(filing_data.get("filing_url")),
            _to_str(filing_data.get("raw_text")),
            _to_str(filing_data.get("matched_keywords")),
            urgent_val,
            comp_details_val,
        ))
    else:
        # SQLite: original INSERT OR IGNORE
        cursor.execute(f"""
            INSERT OR IGNORE INTO filings
            (accession_no, company, ticker, cik, filed_date, item_codes,
             summary, auto_category, auto_subcategory, filing_url, raw_text,
             matched_keywords, urgent, comp_details)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
        """, (
            _to_str(filing_data.get("accession_no")),
            _to_str(filing_data.get("company")),
            _to_str(filing_data.get("ticker")),
            _to_str(filing_data.get("cik")),
            _to_str(filing_data.get("filed_date")),
            _to_str(filing_data.get("item_codes")),
            _to_str(filing_data.get("summary")),
            _to_str(filing_data.get("auto_category")),
            _to_str(filing_data.get("auto_subcategory")),
            _to_str(filing_data.get("filing_url")),
            _to_str(filing_data.get("raw_text")),
            _to_str(filing_data.get("matched_keywords")),
            urgent_val,
            comp_details_val,
        ))

    # Check if the row was actually inserted (not a duplicate)
    was_new = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return was_new


def get_filings(category=None, search=None, date_from=None, date_to=None, urgent_only=False, limit=100, offset=0):
    """Fetch filings from the database with optional filters.
    Used by the dashboard to display results."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    # Build the query dynamically based on which filters are active
    query = "SELECT * FROM filings WHERE 1=1"
    params = []

    if category:
        # Check both auto_category and user_tag (user override takes priority)
        query += f" AND (COALESCE(user_tag, auto_category) = {p})"
        params.append(category)

    if search:
        # Search company name, ticker, or summary text
        query += f" AND (company LIKE {p} OR ticker LIKE {p} OR summary LIKE {p})"
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term])

    if date_from:
        query += f" AND filed_date >= {p}"
        params.append(date_from)

    if date_to:
        query += f" AND filed_date <= {p}"
        params.append(date_to)

    if urgent_only:
        query += " AND urgent = 1"

    query += f" ORDER BY filed_date DESC, created_at DESC LIMIT {p} OFFSET {p}"
    params.extend([limit, offset])

    cursor.execute(query, params)
    results = _dict_rows(cursor.fetchall(), cursor)
    conn.close()
    return results


def get_filtered_filing_count(category=None, search=None, date_from=None, date_to=None, urgent_only=False):
    """Count filings matching the current filters (for pagination)."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    query = "SELECT COUNT(*) FROM filings WHERE 1=1"
    params = []

    if category:
        query += f" AND (COALESCE(user_tag, auto_category) = {p})"
        params.append(category)

    if search:
        query += f" AND (company LIKE {p} OR ticker LIKE {p} OR summary LIKE {p})"
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term])

    if date_from:
        query += f" AND filed_date >= {p}"
        params.append(date_from)

    if date_to:
        query += f" AND filed_date <= {p}"
        params.append(date_to)

    if urgent_only:
        query += " AND urgent = 1"

    cursor.execute(query, params)
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_filing_by_id(filing_id):
    """Get a single filing by its database ID. Used for the detail page."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(f"SELECT * FROM filings WHERE id = {p}", (filing_id,))
    row = cursor.fetchone()
    result = _dict_row(row, cursor)
    conn.close()
    return result


def clear_all_filings():
    """Delete all filings from the database. Used when you want to
    repopulate everything with an updated prompt."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM filings")
    conn.commit()
    conn.close()


def update_user_tag(filing_id, tag):
    """Update the user's manual tag for a filing.
    This overrides the auto-assigned category in the dashboard."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(f"UPDATE filings SET user_tag = {p} WHERE id = {p}", (tag, filing_id))
    conn.commit()
    conn.close()


def update_filing_analysis(filing_id, summary, auto_category, auto_subcategory, urgent, comp_details):
    """Update a filing's LLM-generated fields after re-analysis.
    Only touches analysis fields — leaves user_tag, raw_text, etc. untouched."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    urgent_val = 1 if urgent else 0
    comp_val = _to_str(comp_details)
    cursor.execute(f"""
        UPDATE filings
        SET summary = {p}, auto_category = {p}, auto_subcategory = {p},
            urgent = {p}, comp_details = {p}
        WHERE id = {p}
    """, (summary, auto_category, auto_subcategory, urgent_val, comp_val, filing_id))
    conn.commit()
    conn.close()


def update_deep_analysis(filing_id, deep_analysis_text):
    """Store the deep analysis text for a filing.
    Separate from update_filing_analysis() so we don't touch
    the classification fields (summary, category, etc.)."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(
        f"UPDATE filings SET deep_analysis = {p} WHERE id = {p}",
        (deep_analysis_text, filing_id),
    )
    conn.commit()
    conn.close()


def get_departure_history(cik, exclude_accession, months=12):
    """Find other Item 5.02 (departure) filings from the same company.

    Used by signal analysis to detect departure clustering — multiple
    executives leaving the same company is a bearish signal. Queries
    the local database (not EDGAR) so it only finds filings we've
    already fetched and stored.

    Args:
        cik: The company's CIK number (10-digit string)
        exclude_accession: Accession number of the current filing (skip it)
        months: How far back to look (default 12 months)

    Returns:
        List of dicts with filed_date, auto_subcategory, summary (truncated)
    """
    from datetime import datetime, timedelta

    if not cik:
        return []

    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    cutoff_date = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")

    cursor.execute(f"""
        SELECT filed_date, auto_subcategory, summary
        FROM filings
        WHERE cik = {p}
          AND accession_no != {p}
          AND item_codes LIKE '%5.02%'
          AND filed_date >= {p}
        ORDER BY filed_date DESC
    """, (cik, exclude_accession, cutoff_date))

    # Convert to real dicts so .get() works on both SQLite and Postgres
    columns = [desc[0] for desc in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return results


def get_filings_for_resummarize(date_from=None, date_to=None):
    """Get filings that have raw_text stored, so we can re-run LLM on them.

    If dates are provided, only returns filings in that range.
    If no dates, returns the most recent batch (last 7 days of filed_date).

    Returns list of dicts with id, company, ticker, raw_text, etc.
    """
    from datetime import datetime, timedelta

    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    if not date_from or not date_to:
        # Default: last 7 days — compute in Python so it works on both SQLite and PostgreSQL
        date_to = datetime.now().strftime("%Y-%m-%d")
        date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    query = f"""
        SELECT * FROM filings
        WHERE raw_text IS NOT NULL AND raw_text != ''
          AND filed_date >= {p} AND filed_date <= {p}
        ORDER BY filed_date DESC
    """
    cursor.execute(query, (date_from, date_to))

    results = _dict_rows(cursor.fetchall(), cursor)
    conn.close()
    return results


def get_categories():
    """Get all unique categories (combining auto and user tags).
    Used to populate filter dropdowns in the dashboard."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT COALESCE(user_tag, auto_category) as category
        FROM filings
        WHERE COALESCE(user_tag, auto_category) IS NOT NULL
        ORDER BY category
    """)
    if _using_postgres():
        results = [row[0] for row in cursor.fetchall()]
    else:
        results = [row["category"] for row in cursor.fetchall()]
    conn.close()
    return results


def get_filing_count():
    """Get total number of filings in the database."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM filings")
    row = cursor.fetchone()
    if _using_postgres():
        count = row[0]
    else:
        count = row["count"]
    conn.close()
    return count


# ============================================================
# WATCHLIST FUNCTIONS
# ============================================================

def add_to_watchlist(filing_id):
    """Add a filing to the watchlist. Does nothing if already watchlisted."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    if _using_postgres():
        cursor.execute(f"""
            INSERT INTO watchlist (filing_id) VALUES ({p})
            ON CONFLICT (filing_id) DO NOTHING
        """, (filing_id,))
    else:
        cursor.execute(f"""
            INSERT OR IGNORE INTO watchlist (filing_id) VALUES ({p})
        """, (filing_id,))

    conn.commit()
    conn.close()


def remove_from_watchlist(filing_id):
    """Remove a filing from the watchlist."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(f"DELETE FROM watchlist WHERE filing_id = {p}", (filing_id,))
    conn.commit()
    conn.close()


def update_watchlist_notes(filing_id, notes):
    """Update the notes for a watchlisted filing."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(f"UPDATE watchlist SET notes = {p} WHERE filing_id = {p}", (notes, filing_id))
    conn.commit()
    conn.close()


def get_watchlist_item(filing_id):
    """Get the watchlist entry for a filing, or None if not watchlisted."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(f"SELECT * FROM watchlist WHERE filing_id = {p}", (filing_id,))
    row = cursor.fetchone()
    result = _dict_row(row, cursor)
    conn.close()
    return result


def get_all_watchlist_ids():
    """Get a set of all filing IDs that are in the watchlist.
    Used to show star icons on the dashboard."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT filing_id FROM watchlist")
    if _using_postgres():
        ids = {row[0] for row in cursor.fetchall()}
    else:
        ids = {row["filing_id"] for row in cursor.fetchall()}
    conn.close()
    return ids


def get_watchlist_filings():
    """Get all watchlisted filings with their notes, sorted by when they were added.
    Returns filing data joined with watchlist notes and email sent status."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT f.*, w.notes as watchlist_notes, w.added_at as watchlist_added_at,
               w.email_sent_at
        FROM filings f
        JOIN watchlist w ON f.id = w.filing_id
        ORDER BY w.added_at DESC
    """)
    results = _dict_rows(cursor.fetchall(), cursor)
    conn.close()
    return results


def get_watchlist_filings_by_ids(filing_ids):
    """Get specific watchlisted filings by their IDs.
    Used by the email composer to load only selected filings."""
    if not filing_ids:
        return []
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    placeholders = ", ".join([p] * len(filing_ids))
    cursor.execute(f"""
        SELECT f.*, w.notes as watchlist_notes, w.added_at as watchlist_added_at
        FROM filings f
        JOIN watchlist w ON f.id = w.filing_id
        WHERE f.id IN ({placeholders})
        ORDER BY w.added_at DESC
    """, tuple(filing_ids))
    results = _dict_rows(cursor.fetchall(), cursor)
    conn.close()
    return results


def mark_filings_email_sent(filing_ids):
    """Mark filings as included in a weekly email by setting email_sent_at timestamp."""
    if not filing_ids:
        return
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    placeholders = ", ".join([p] * len(filing_ids))
    cursor.execute(f"""
        UPDATE watchlist SET email_sent_at = CURRENT_TIMESTAMP
        WHERE filing_id IN ({placeholders})
    """, tuple(filing_ids))
    conn.commit()
    conn.close()


# ============================================================
# APP STATUS FUNCTIONS (tracks backfill timestamps, etc.)
# ============================================================

def _create_app_status_table(conn):
    """Create the app_status table for tracking system info like last backfill time.
    Uses a key-value design so we can store any status info we need."""
    cursor = conn.cursor()

    if _using_postgres():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_status (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_status (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    conn.commit()
    print("[STARTUP] App status table ready")


def update_last_backfill(backfill_type):
    """Record when a backfill completed and what type it was.
    backfill_type should be 'web', 'scheduled', or 'manual'."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    if _using_postgres():
        # PostgreSQL: use ON CONFLICT to upsert
        cursor.execute(f"""
            INSERT INTO app_status (key, value, updated_at)
            VALUES ('last_backfill_type', {p}, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = {p}, updated_at = CURRENT_TIMESTAMP
        """, (backfill_type, backfill_type))
    else:
        # SQLite: use INSERT OR REPLACE
        cursor.execute(f"""
            INSERT OR REPLACE INTO app_status (key, value, updated_at)
            VALUES ('last_backfill_type', {p}, CURRENT_TIMESTAMP)
        """, (backfill_type,))

    conn.commit()
    conn.close()
    print(f"[BACKFILL] Recorded backfill completion: type={backfill_type}")


def get_last_backfill():
    """Get info about the last backfill run.
    Returns dict with 'time' (datetime in Eastern Time) and 'type' (str), or None if no backfill recorded."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT value, updated_at FROM app_status WHERE key = 'last_backfill_type'
    """)
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo  # Built into Python 3.9+

    # Extract values based on database type
    if _using_postgres():
        utc_time = row[1]
        backfill_type = row[0]
    else:
        # SQLite returns timestamp as string, convert to datetime
        time_str = row["updated_at"]
        if isinstance(time_str, str):
            utc_time = datetime.fromisoformat(time_str.replace(" ", "T"))
        else:
            utc_time = time_str
        backfill_type = row["value"]

    # Convert UTC to Eastern Time for display
    if utc_time.tzinfo is None:
        utc_time = utc_time.replace(tzinfo=timezone.utc)
    eastern = ZoneInfo("America/New_York")
    local_time = utc_time.astimezone(eastern)

    return {"type": backfill_type, "time": local_time}


# ============================================================
# BACKFILL RUN TRACKING (logs stats for each backfill)
# ============================================================

def _create_backfill_runs_table(conn):
    """Create the backfill_runs table for tracking stats from each backfill.
    Records how many filings were fetched, filtered, and newly inserted
    so you can confirm you've reviewed all new filings."""
    cursor = conn.cursor()

    if _using_postgres():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backfill_runs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                backfill_type TEXT,
                date_range_start TEXT,
                date_range_end TEXT,
                model TEXT,
                fetched_count INTEGER DEFAULT 0,
                filtered_count INTEGER DEFAULT 0,
                new_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running'
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backfill_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                backfill_type TEXT,
                date_range_start TEXT,
                date_range_end TEXT,
                model TEXT,
                fetched_count INTEGER DEFAULT 0,
                filtered_count INTEGER DEFAULT 0,
                new_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running'
            )
        """)

    conn.commit()
    print("[STARTUP] Backfill runs table ready")


def create_backfill_run(backfill_type, date_start, date_end, model):
    """Start tracking a new backfill run. Returns the run's ID."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    if _using_postgres():
        cursor.execute(f"""
            INSERT INTO backfill_runs (backfill_type, date_range_start, date_range_end, model)
            VALUES ({p}, {p}, {p}, {p})
            RETURNING id
        """, (backfill_type, date_start, date_end, model))
        run_id = cursor.fetchone()[0]
    else:
        cursor.execute(f"""
            INSERT INTO backfill_runs (backfill_type, date_range_start, date_range_end, model)
            VALUES ({p}, {p}, {p}, {p})
        """, (backfill_type, date_start, date_end, model))
        run_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return run_id


def complete_backfill_run(run_id, fetched=0, filtered=0, new=0, skipped=0, status="completed"):
    """Record final stats for a backfill run once it finishes (or fails)."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    cursor.execute(f"""
        UPDATE backfill_runs
        SET completed_at = CURRENT_TIMESTAMP,
            fetched_count = {p},
            filtered_count = {p},
            new_count = {p},
            skipped_count = {p},
            status = {p}
        WHERE id = {p}
    """, (fetched, filtered, new, skipped, status, run_id))

    conn.commit()
    conn.close()


def get_recent_backfill_runs(limit=10):
    """Get the most recent backfill runs for display on the backfill page.
    Returns a list of dicts with all run stats."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    cursor.execute(f"""
        SELECT id, started_at, completed_at, backfill_type,
               date_range_start, date_range_end, model,
               fetched_count, filtered_count, new_count, skipped_count, status
        FROM backfill_runs
        ORDER BY started_at DESC
        LIMIT {p}
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    # Convert to dicts so the template can access by name
    columns = ["id", "started_at", "completed_at", "backfill_type",
               "date_range_start", "date_range_end", "model",
               "fetched_count", "filtered_count", "new_count", "skipped_count", "status"]

    if _using_postgres():
        return [dict(zip(columns, row)) for row in rows]
    else:
        return [dict(zip(columns, row)) for row in rows]


# ============================================================
# MARKET CAP CACHE FUNCTIONS
# ============================================================

def _create_market_caps_table(conn):
    """Create the market_caps table for caching stock market cap data.
    Stores one row per ticker so multiple filings sharing a ticker
    don't cause duplicate lookups."""
    cursor = conn.cursor()

    if _using_postgres():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_caps (
                ticker TEXT PRIMARY KEY,
                market_cap BIGINT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_caps (
                ticker TEXT PRIMARY KEY,
                market_cap INTEGER,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    conn.commit()
    print("[STARTUP] Market caps table ready")


def get_cached_market_caps(tickers):
    """Look up cached market caps for a list of tickers.
    Only returns entries that were fetched within the last 7 days.
    Returns a dict like {'AAPL': 3500000000000, 'MSFT': 2800000000000}."""
    if not tickers:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    placeholders = ", ".join([p] * len(tickers))

    # Only return rows fetched within the last 7 days
    if _using_postgres():
        cursor.execute(f"""
            SELECT ticker, market_cap FROM market_caps
            WHERE ticker IN ({placeholders})
            AND fetched_at > NOW() - INTERVAL '7 days'
        """, tuple(tickers))
    else:
        cursor.execute(f"""
            SELECT ticker, market_cap FROM market_caps
            WHERE ticker IN ({placeholders})
            AND fetched_at > datetime('now', '-7 days')
        """, tuple(tickers))

    rows = cursor.fetchall()
    conn.close()

    # Build the result dict — include entries even if market_cap is NULL
    # (NULL means "we checked and there's no data", which we still want to cache)
    if _using_postgres():
        return {row[0]: row[1] for row in rows}
    else:
        return {row["ticker"]: row["market_cap"] for row in rows}


def upsert_market_caps(market_cap_dict):
    """Insert or update cached market cap values.
    market_cap_dict is like {'AAPL': 3500000000000, 'MSFT': None}.
    None means the ticker was checked but has no market cap data."""
    if not market_cap_dict:
        return
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    for ticker, cap in market_cap_dict.items():
        if _using_postgres():
            cursor.execute(f"""
                INSERT INTO market_caps (ticker, market_cap, fetched_at)
                VALUES ({p}, {p}, CURRENT_TIMESTAMP)
                ON CONFLICT (ticker) DO UPDATE
                SET market_cap = {p}, fetched_at = CURRENT_TIMESTAMP
            """, (ticker, cap, cap))
        else:
            cursor.execute(f"""
                INSERT OR REPLACE INTO market_caps (ticker, market_cap, fetched_at)
                VALUES ({p}, {p}, CURRENT_TIMESTAMP)
            """, (ticker, cap))

    conn.commit()
    conn.close()


def clear_failed_market_caps():
    """Remove cached entries where market_cap is NULL (failed lookups).
    This lets them be retried on the next page load or backfill.
    Entries with actual values are kept."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM market_caps WHERE market_cap IS NULL")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


# ============================================================
# EARNINGS CACHE FUNCTIONS
# ============================================================

def _create_earnings_cache_table(conn):
    """Create the earnings_cache table for caching next earnings dates.
    Stores one row per ticker with the next upcoming earnings date
    and timing (before/after market)."""
    cursor = conn.cursor()

    if _using_postgres():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS earnings_cache (
                ticker TEXT PRIMARY KEY,
                earnings_date TEXT,
                earnings_timing TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS earnings_cache (
                ticker TEXT PRIMARY KEY,
                earnings_date TEXT,
                earnings_timing TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    conn.commit()

    # One-time fix: clear any cached NULL earnings dates from before
    # the show_upcoming parameter was added (they'll refetch correctly now)
    cursor.execute("DELETE FROM earnings_cache WHERE earnings_date IS NULL")
    conn.commit()

    print("[STARTUP] Earnings cache table ready")


def get_cached_earnings(tickers):
    """Look up cached earnings dates for a list of tickers.
    Only returns entries fetched within the last 48 hours.
    Returns a dict like {'AAPL': {'date': '2026-04-25', 'timing': 'after_market'}}."""
    if not tickers:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    placeholders = ", ".join([p] * len(tickers))

    # 48-hour TTL — earnings dates don't change often, refresh every couple days
    if _using_postgres():
        cursor.execute(f"""
            SELECT ticker, earnings_date, earnings_timing FROM earnings_cache
            WHERE ticker IN ({placeholders})
            AND fetched_at > NOW() - INTERVAL '48 hours'
        """, tuple(tickers))
    else:
        cursor.execute(f"""
            SELECT ticker, earnings_date, earnings_timing FROM earnings_cache
            WHERE ticker IN ({placeholders})
            AND fetched_at > datetime('now', '-2 days')
        """, tuple(tickers))

    rows = cursor.fetchall()
    conn.close()

    # Build the result dict — include entries even if date is NULL
    # (NULL means "we checked and there's no upcoming earnings")
    if _using_postgres():
        return {row[0]: {"date": row[1], "timing": row[2]} for row in rows}
    else:
        return {row["ticker"]: {"date": row["earnings_date"], "timing": row["earnings_timing"]} for row in rows}


def upsert_earnings(earnings_dict):
    """Insert or update cached earnings date values.
    earnings_dict is like {'AAPL': {'date': '2026-04-25', 'timing': 'after_market'}}.
    None value means the ticker was checked but has no upcoming earnings."""
    if not earnings_dict:
        return
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    for ticker, info in earnings_dict.items():
        # info is either a dict with 'date' and 'timing', or None
        if info is None:
            earnings_date = None
            earnings_timing = None
        else:
            earnings_date = info.get("date")
            earnings_timing = info.get("timing")

        if _using_postgres():
            cursor.execute(f"""
                INSERT INTO earnings_cache (ticker, earnings_date, earnings_timing, fetched_at)
                VALUES ({p}, {p}, {p}, CURRENT_TIMESTAMP)
                ON CONFLICT (ticker) DO UPDATE
                SET earnings_date = {p}, earnings_timing = {p}, fetched_at = CURRENT_TIMESTAMP
            """, (ticker, earnings_date, earnings_timing, earnings_date, earnings_timing))
        else:
            cursor.execute(f"""
                INSERT OR REPLACE INTO earnings_cache (ticker, earnings_date, earnings_timing, fetched_at)
                VALUES ({p}, {p}, {p}, CURRENT_TIMESTAMP)
            """, (ticker, earnings_date, earnings_timing))

    conn.commit()
    conn.close()


# ============================================================
# STOCK PRICE CACHE FUNCTIONS
# ============================================================

def _create_stock_prices_table(conn):
    """Create the stock_prices table for caching current stock prices.
    Used by signal analysis to evaluate price hurdles and detect spring-loading."""
    cursor = conn.cursor()

    if _using_postgres():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_prices (
                ticker TEXT PRIMARY KEY,
                price REAL,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_prices (
                ticker TEXT PRIMARY KEY,
                price REAL,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    conn.commit()
    print("[STARTUP] Stock prices table ready")


def get_cached_stock_price(ticker):
    """Look up cached stock price for a single ticker.
    Only returns if fetched within the last 1 hour (prices move fast).
    Returns the price as a float, or None if not cached/stale."""
    if not ticker:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    # 1-hour TTL — stock prices change throughout the day
    if _using_postgres():
        cursor.execute(f"""
            SELECT price FROM stock_prices
            WHERE ticker = {p}
            AND fetched_at > NOW() - INTERVAL '1 hour'
        """, (ticker.upper(),))
    else:
        cursor.execute(f"""
            SELECT price FROM stock_prices
            WHERE ticker = {p}
            AND fetched_at > datetime('now', '-1 hour')
        """, (ticker.upper(),))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None
    if _using_postgres():
        return row[0]
    else:
        return row["price"]


def upsert_stock_price(ticker, price):
    """Insert or update a cached stock price.
    price can be None if the lookup failed (caches the miss)."""
    if not ticker:
        return
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    if _using_postgres():
        cursor.execute(f"""
            INSERT INTO stock_prices (ticker, price, fetched_at)
            VALUES ({p}, {p}, CURRENT_TIMESTAMP)
            ON CONFLICT (ticker) DO UPDATE
            SET price = {p}, fetched_at = CURRENT_TIMESTAMP
        """, (ticker.upper(), price, price))
    else:
        cursor.execute(f"""
            INSERT OR REPLACE INTO stock_prices (ticker, price, fetched_at)
            VALUES ({p}, {p}, CURRENT_TIMESTAMP)
        """, (ticker.upper(), price))

    conn.commit()
    conn.close()


# When this file is run directly, create the database
if __name__ == "__main__":
    initialize_database()
    if _using_postgres():
        print("PostgreSQL database initialized")
    else:
        print(f"SQLite database initialized at {DATABASE_PATH}")
