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
        # busy_timeout retries briefly instead of raising "database is locked"
        # the instant two writers collide. (journal_mode=WAL is persistent per
        # database file and is set once in initialize_database.)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass  # best-effort
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

    if not _using_postgres():
        # WAL lets the background cache-refresh threads write while a web
        # request reads. It's a persistent property of the DB file — setting
        # it once here is enough for every later connection.
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass  # best-effort (e.g., unsupported filesystem)

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

    # Any 'running' row at boot time belongs to a worker that's already dead
    # (Render restart killed the daemon thread before it could update status).
    # Mark those as failed so the UI stops lying about them.
    _cleanup_stuck_backfill_runs(conn)

    # Create market_caps table for caching stock market cap data
    _create_market_caps_table(conn)

    # Create earnings_cache table for caching next earnings dates
    _create_earnings_cache_table(conn)

    # Create stock_prices table for caching current stock prices
    _create_stock_prices_table(conn)

    # Create departure_extractions table for caching 5.02 LLM extractions
    _create_departure_extractions_table(conn)

    # Log which database we're using and how many filings are stored
    # This helps us debug data loss issues on Render
    cursor.execute("SELECT COUNT(*) FROM filings")
    count = cursor.fetchone()[0]
    if _using_postgres():
        print(f"[STARTUP] Using PostgreSQL — {count} filings in database")
    else:
        print(f"[STARTUP] Using SQLite — {count} filings in database")

    conn.close()


def _add_column(conn, cursor, existing, name, ddl):
    """ALTER TABLE ADD COLUMN, tolerant of a concurrent worker adding the
    same column first — multiple gunicorn workers boot simultaneously on
    Render, and both can see the column as missing before either ALTERs.
    Commits per column so rolling back a benign duplicate failure doesn't
    discard earlier successful ALTERs.

    Returns True when this call actually added the column."""
    if name in existing:
        return False
    try:
        cursor.execute(ddl)
        conn.commit()
        print(f"[MIGRATE] Added '{name}' column")
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            print(f"[MIGRATE] '{name}' added by another worker — continuing")
            return False
        raise


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

    # Urgent flag (0 or 1)
    _add_column(conn, cursor, existing, "urgent",
                "ALTER TABLE filings ADD COLUMN urgent INTEGER DEFAULT 0")

    # comp_details JSON blob
    _add_column(conn, cursor, existing, "comp_details",
                "ALTER TABLE filings ADD COLUMN comp_details TEXT DEFAULT NULL")

    # deep_analysis text column for comprehensive investor analysis
    _add_column(conn, cursor, existing, "deep_analysis",
                "ALTER TABLE filings ADD COLUMN deep_analysis TEXT DEFAULT NULL")

    # filing_document_url (primary 8-K document URL for one-click navigation)
    _add_column(conn, cursor, existing, "filing_document_url",
                "ALTER TABLE filings ADD COLUMN filing_document_url TEXT DEFAULT NULL")

    # is_complex flag — set when filing doesn't fit structured buckets cleanly
    _add_column(conn, cursor, existing, "is_complex",
                "ALTER TABLE filings ADD COLUMN is_complex INTEGER DEFAULT 0")

    # narrative_summary — free-text fallback for complex filings
    _add_column(conn, cursor, existing, "narrative_summary",
                "ALTER TABLE filings ADD COLUMN narrative_summary TEXT DEFAULT NULL")

    # relevant_reason — LLM's justification when relevant:false
    _add_column(conn, cursor, existing, "relevant_reason",
                "ALTER TABLE filings ADD COLUMN relevant_reason TEXT DEFAULT NULL")

    # structured_summary — JSON blob holding the full v3 payload (departures[], etc.)
    _add_column(conn, cursor, existing, "structured_summary",
                "ALTER TABLE filings ADD COLUMN structured_summary TEXT DEFAULT NULL")

    # has_market_targets — 1 when filing discloses market-based comp targets
    # (stock-price, market-cap, or TSR). Powers the dashboard "Market Targets" filter.
    _add_column(conn, cursor, existing, "has_market_targets",
                "ALTER TABLE filings ADD COLUMN has_market_targets INTEGER DEFAULT 0")

    # read_at timestamp for tracking which filings the user has reviewed.
    # NULL = unread; non-null timestamp = read at that time.
    if _add_column(conn, cursor, existing, "read_at",
                   "ALTER TABLE filings ADD COLUMN read_at TIMESTAMP DEFAULT NULL"):
        # Clean-slate: mark every pre-existing row as read so the user isn't
        # buried under thousands of "unread" items the moment the feature ships.
        # Runs only once — only when this worker actually added the column.
        # Commit immediately: a later _add_column losing a concurrent-worker
        # race calls rollback(), which would silently discard this UPDATE.
        cursor.execute("UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE read_at IS NULL")
        print(f"[MIGRATE] Marked {cursor.rowcount} pre-existing filings as read (clean slate)")
        conn.commit()

    # Index on read_at — the "Show unread only" dashboard query uses WHERE read_at IS NULL
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_filings_read_at ON filings(read_at)")

    # --- Triage columns: verdict / score / direction / top signal ---
    # Populated by the v3 prompt at ingest. NULL on legacy rows = "unrated".
    _add_column(conn, cursor, existing, "triage_verdict",
                "ALTER TABLE filings ADD COLUMN triage_verdict TEXT DEFAULT NULL")
    _add_column(conn, cursor, existing, "signal_score",
                "ALTER TABLE filings ADD COLUMN signal_score INTEGER DEFAULT NULL")
    _add_column(conn, cursor, existing, "signal_direction",
                "ALTER TABLE filings ADD COLUMN signal_direction TEXT DEFAULT NULL")
    _add_column(conn, cursor, existing, "top_signal",
                "ALTER TABLE filings ADD COLUMN top_signal TEXT DEFAULT NULL")

    # Number of departures extracted from THIS filing (len of structured
    # departures[]). Used to decide which filings need EDGAR history enrichment.
    _add_column(conn, cursor, existing, "departure_count",
                "ALTER TABLE filings ADD COLUMN departure_count INTEGER DEFAULT NULL")

    # EDGAR-based 24-month departure history, stamped at ingest by
    # departures.enrich_new_filings(). departure_count_24mo is the deduped
    # person count (dashboard cluster badge); departure_history is the full
    # JSON list (detail-page card renders without a click).
    _add_column(conn, cursor, existing, "departure_count_24mo",
                "ALTER TABLE filings ADD COLUMN departure_count_24mo INTEGER DEFAULT NULL")
    _add_column(conn, cursor, existing, "departure_history",
                "ALTER TABLE filings ADD COLUMN departure_history TEXT DEFAULT NULL")

    # Same-CIK lookups by date (departure history, related-filing queries)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_filings_cik_date ON filings(cik, filed_date)")

    # --- Bearish departure sub-signals, promoted to real columns so the
    # dashboard can filter on them (they used to live only inside the
    # structured_summary JSON). NULL = legacy row / no departures.
    _add_column(conn, cursor, existing, "forfeited_comp",
                "ALTER TABLE filings ADD COLUMN forfeited_comp INTEGER DEFAULT NULL")
    _add_column(conn, cursor, existing, "has_successor",
                "ALTER TABLE filings ADD COLUMN has_successor INTEGER DEFAULT NULL")

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

    _add_column(conn, cursor, existing, "email_sent_at",
                "ALTER TABLE watchlist ADD COLUMN email_sent_at TIMESTAMP NULL")

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

    # New v3 fields
    is_complex_val = 1 if filing_data.get("is_complex") else 0
    filing_document_url_val = _to_str(filing_data.get("filing_document_url"))
    narrative_summary_val = _to_str(filing_data.get("narrative_summary"))
    relevant_reason_val = _to_str(filing_data.get("relevant_reason"))
    structured_summary_val = _to_str(filing_data.get("structured_summary"))
    has_market_targets_val = 1 if filing_data.get("has_market_targets") else 0

    # Triage fields — NULL when the LLM didn't produce them (legacy prompt,
    # LLM failure, or rate-limited rows with no text)
    triage_verdict_val = _to_str(filing_data.get("triage_verdict"))
    signal_score_val = filing_data.get("signal_score")
    signal_direction_val = _to_str(filing_data.get("signal_direction"))
    top_signal_val = _to_str(filing_data.get("top_signal"))
    departure_count_val = filing_data.get("departure_count")
    forfeited_comp_val = filing_data.get("forfeited_comp")
    has_successor_val = filing_data.get("has_successor")

    insert_values = (
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
        filing_document_url_val,
        is_complex_val,
        narrative_summary_val,
        relevant_reason_val,
        structured_summary_val,
        has_market_targets_val,
        triage_verdict_val,
        signal_score_val,
        signal_direction_val,
        top_signal_val,
        departure_count_val,
        forfeited_comp_val,
        has_successor_val,
    )

    insert_columns = """
            (accession_no, company, ticker, cik, filed_date, item_codes,
             summary, auto_category, auto_subcategory, filing_url, raw_text,
             matched_keywords, urgent, comp_details,
             filing_document_url, is_complex, narrative_summary,
             relevant_reason, structured_summary, has_market_targets,
             triage_verdict, signal_score, signal_direction, top_signal,
             departure_count, forfeited_comp, has_successor)
    """
    placeholders_sql = ", ".join([p] * len(insert_values))

    if _using_postgres():
        # PostgreSQL: use ON CONFLICT instead of INSERT OR IGNORE
        cursor.execute(f"""
            INSERT INTO filings {insert_columns}
            VALUES ({placeholders_sql})
            ON CONFLICT (accession_no) DO NOTHING
        """, insert_values)
    else:
        # SQLite: original INSERT OR IGNORE
        cursor.execute(f"""
            INSERT OR IGNORE INTO filings {insert_columns}
            VALUES ({placeholders_sql})
        """, insert_values)

    # Check if the row was actually inserted (not a duplicate)
    was_new = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return was_new


# Sorting filings by triage tier: DEEP_LOOK first, then MONITOR, then legacy
# unrated rows (NULL verdict), then PASS at the bottom. Within a tier, higher
# score first, then newest. Works identically on SQLite and PostgreSQL.
_SIGNAL_SORT_SQL = (
    " ORDER BY CASE COALESCE(triage_verdict, '')"
    "   WHEN 'DEEP_LOOK' THEN 0"
    "   WHEN 'MONITOR' THEN 1"
    "   WHEN 'PASS' THEN 3"
    "   ELSE 2 END,"
    " COALESCE(signal_score, -1) DESC,"
    " filed_date DESC, created_at DESC"
)


def _build_filing_filters(p, category=None, search=None, date_from=None,
                          date_to=None, urgent_only=False, market_targets_only=False,
                          unread_only=False, verdict=None, direction=None,
                          forfeited_only=False, clusters_only=False):
    """Build the shared WHERE-clause suffix + params for filing list queries.

    Shared by get_filings() and get_filtered_filing_count() so the list and
    its pagination count can never disagree about what matches.
    """
    where = ""
    params = []

    if category:
        # Check both auto_category and user_tag (user override takes priority)
        where += f" AND (COALESCE(user_tag, auto_category) = {p})"
        params.append(category)

    if search:
        # Search company name, ticker, or summary text
        where += f" AND (company LIKE {p} OR ticker LIKE {p} OR summary LIKE {p})"
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term])

    if date_from:
        where += f" AND filed_date >= {p}"
        params.append(date_from)

    if date_to:
        where += f" AND filed_date <= {p}"
        params.append(date_to)

    if urgent_only:
        where += " AND urgent = 1"

    if market_targets_only:
        where += " AND has_market_targets = 1"

    if unread_only:
        where += " AND read_at IS NULL"

    if verdict == "actionable":
        # Deep Look + Monitor — everything worth at least a glance
        where += " AND triage_verdict IN ('DEEP_LOOK', 'MONITOR')"
    elif verdict in ("DEEP_LOOK", "MONITOR", "PASS"):
        where += f" AND triage_verdict = {p}"
        params.append(verdict)

    if direction in ("BEARISH", "BULLISH", "MIXED", "NEUTRAL"):
        where += f" AND signal_direction = {p}"
        params.append(direction)

    if forfeited_only:
        # Departing exec walked away from unvested comp — loudest bearish tell
        where += " AND forfeited_comp = 1"

    if clusters_only:
        # 2+ departures at this company in 24 months (EDGAR-based)
        where += " AND COALESCE(departure_count_24mo, 0) >= 2"

    return where, params


def get_filings(category=None, search=None, date_from=None, date_to=None,
                urgent_only=False, market_targets_only=False, unread_only=False,
                verdict=None, direction=None, forfeited_only=False,
                clusters_only=False, sort="date", limit=100, offset=0):
    """Fetch filings from the database with optional filters.
    Used by the dashboard to display results.

    sort: "date" (newest first, default) or "signal" (triage tier, then score).
    verdict: "DEEP_LOOK" | "MONITOR" | "PASS" | "actionable" (Deep Look + Monitor).
    direction: "BEARISH" | "BULLISH" | "MIXED" | "NEUTRAL".
    forfeited_only: only filings where a departing exec forfeits comp.
    clusters_only: only filings with >= 2 departures at the company in 24mo.
    """
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    where, params = _build_filing_filters(
        p, category=category, search=search, date_from=date_from, date_to=date_to,
        urgent_only=urgent_only, market_targets_only=market_targets_only,
        unread_only=unread_only, verdict=verdict, direction=direction,
        forfeited_only=forfeited_only, clusters_only=clusters_only,
    )
    query = "SELECT * FROM filings WHERE 1=1" + where

    if sort == "signal":
        query += _SIGNAL_SORT_SQL
    else:
        query += " ORDER BY filed_date DESC, created_at DESC"
    query += f" LIMIT {p} OFFSET {p}"
    params.extend([limit, offset])

    cursor.execute(query, params)
    results = _dict_rows(cursor.fetchall(), cursor)
    conn.close()
    return results


def get_filtered_filing_count(category=None, search=None, date_from=None,
                              date_to=None, urgent_only=False, market_targets_only=False,
                              unread_only=False, verdict=None, direction=None,
                              forfeited_only=False, clusters_only=False):
    """Count filings matching the current filters (for pagination)."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    where, params = _build_filing_filters(
        p, category=category, search=search, date_from=date_from, date_to=date_to,
        urgent_only=urgent_only, market_targets_only=market_targets_only,
        unread_only=unread_only, verdict=verdict, direction=direction,
        forfeited_only=forfeited_only, clusters_only=clusters_only,
    )
    cursor.execute("SELECT COUNT(*) FROM filings WHERE 1=1" + where, params)
    count = cursor.fetchone()[0]
    conn.close()
    return count


def update_departure_history(filing_id, count_24mo, history_json):
    """Stamp a filing with its company's EDGAR-based 24-month departure history.

    Separate from update_filing_analysis() — this data comes from EDGAR
    enrichment, not from re-running the classification LLM, so re-summarizing
    a filing never wipes it.
    """
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(
        f"UPDATE filings SET departure_count_24mo = {p}, departure_history = {p} WHERE id = {p}",
        (count_24mo, history_json, filing_id),
    )
    conn.commit()
    conn.close()


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


def mark_filings_read(filing_ids):
    """Mark the given filings as read by setting their read_at timestamp.

    Only updates rows where read_at IS NULL (idempotent — already-read rows are
    untouched). Returns the number of rows actually updated. Empty list → 0.

    Called by:
    - POST /api/filings/mark-read (batched from dashboard scroll-tracking)
    - GET /filing/<id> (single-filing case when user opens detail page)
    """
    if not filing_ids:
        return 0

    # Defensive cap — client batches should be much smaller than this
    filing_ids = list(filing_ids)[:100]

    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    # Build the parameterised IN (...) clause manually since SQLite/pg8000 differ
    placeholders = ",".join([p] * len(filing_ids))
    query = (
        f"UPDATE filings SET read_at = CURRENT_TIMESTAMP "
        f"WHERE id IN ({placeholders}) AND read_at IS NULL"
    )
    cursor.execute(query, filing_ids)
    rowcount = cursor.rowcount
    conn.commit()
    conn.close()
    return rowcount


def get_filing_by_accession(accession_no):
    """Fetch a single filing by its accession_no. Returns a dict or None.
    Explicitly builds a dict from cursor columns to ensure cross-db consistency
    (SQLite sqlite3.Row has different access patterns than Postgres)."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(f"SELECT * FROM filings WHERE accession_no = {p}", (accession_no,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return None
    columns = [desc[0] for desc in cursor.description]
    conn.close()
    return dict(zip(columns, row))


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


def update_filing_analysis(
    filing_id,
    summary,
    auto_category,
    auto_subcategory,
    urgent,
    comp_details,
    structured_summary=None,
    is_complex=False,
    narrative_summary=None,
    relevant_reason=None,
    has_market_targets=False,
    triage_verdict=None,
    signal_score=None,
    signal_direction=None,
    top_signal=None,
    departure_count=None,
    forfeited_comp=None,
    has_successor=None,
):
    """Update a filing's LLM-generated fields after re-analysis.
    Only touches analysis fields — leaves user_tag, raw_text, etc. untouched."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    urgent_val = 1 if urgent else 0
    complex_val = 1 if is_complex else 0
    comp_val = _to_str(comp_details)
    has_mt_val = 1 if has_market_targets else 0
    cursor.execute(f"""
        UPDATE filings
        SET summary = {p}, auto_category = {p}, auto_subcategory = {p},
            urgent = {p}, comp_details = {p},
            structured_summary = {p}, is_complex = {p},
            narrative_summary = {p}, relevant_reason = {p},
            has_market_targets = {p},
            triage_verdict = {p}, signal_score = {p},
            signal_direction = {p}, top_signal = {p},
            departure_count = {p}, forfeited_comp = {p}, has_successor = {p}
        WHERE id = {p}
    """, (summary, auto_category, auto_subcategory, urgent_val, comp_val,
          structured_summary, complex_val, narrative_summary, relevant_reason,
          has_mt_val, triage_verdict, signal_score, signal_direction,
          top_signal, departure_count, forfeited_comp, has_successor, filing_id))
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


def get_filings_missing_text(date_from=None, date_to=None):
    """Get filings that are missing raw_text (failed SEC fetch during backfill).

    These are the ones that got saved with empty summary because the LLM never
    ran on them. This returns id, cik, accession_no, filing_url so we can
    retry the SEC document fetch.
    """
    from datetime import datetime, timedelta

    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    if not date_from or not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")
        date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    query = f"""
        SELECT * FROM filings
        WHERE (raw_text IS NULL OR raw_text = '')
          AND filed_date >= {p} AND filed_date <= {p}
        ORDER BY filed_date DESC
    """
    cursor.execute(query, (date_from, date_to))
    results = _dict_rows(cursor.fetchall(), cursor)
    conn.close()
    return results


def update_filing_raw_text(filing_id, raw_text, filing_document_url=None):
    """Store the freshly-fetched filing text for a row that was missing it.

    Separate from update_filing_analysis() so callers can re-fetch text first,
    then analyze. Also stores the resolved document URL when available.
    """
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    if filing_document_url is not None:
        cursor.execute(
            f"UPDATE filings SET raw_text = {p}, filing_document_url = {p} WHERE id = {p}",
            (raw_text, filing_document_url, filing_id),
        )
    else:
        cursor.execute(
            f"UPDATE filings SET raw_text = {p} WHERE id = {p}",
            (raw_text, filing_id),
        )
    conn.commit()
    conn.close()


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


def _cleanup_stuck_backfill_runs(conn):
    """Mark any 'running' backfill_runs as 'failed' on app boot.

    Backfills run as daemon threads inside the gunicorn worker. When the worker
    is killed (Render restart, idle spin-down, deploy, OOM), the thread dies
    without ever updating its status row — so it appears stuck at 'running'
    forever. If we're booting up and a row says 'running', that worker is gone."""
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE backfill_runs "
        "SET status = 'failed', completed_at = CURRENT_TIMESTAMP "
        "WHERE status = 'running'"
    )
    affected = cursor.rowcount
    conn.commit()
    if affected and affected > 0:
        print(f"[STARTUP] Marked {affected} orphaned backfill run(s) as failed", flush=True)


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


def get_cached_market_caps(tickers, max_age_hours=None):
    """Look up cached market caps for a list of tickers.

    Args:
        tickers: List of ticker strings.
        max_age_hours: If set, only return rows fetched within this many hours.
                       If None (default), return all cached rows regardless of age —
                       useful for fast read paths that show stale data and refresh
                       in the background.

    Returns a dict like {'AAPL': 3500000000000, 'MSFT': 2800000000000}.
    """
    if not tickers:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    placeholders = ", ".join([p] * len(tickers))

    if max_age_hours is None:
        # No age filter — return whatever's cached. Fast read path.
        cursor.execute(
            f"SELECT ticker, market_cap FROM market_caps WHERE ticker IN ({placeholders})",
            tuple(tickers),
        )
    elif _using_postgres():
        # int() guards against SQL injection (we're inlining the value)
        hours = int(max_age_hours)
        cursor.execute(f"""
            SELECT ticker, market_cap FROM market_caps
            WHERE ticker IN ({placeholders})
            AND fetched_at > NOW() - INTERVAL '{hours} hours'
        """, tuple(tickers))
    else:
        hours = int(max_age_hours)
        cursor.execute(f"""
            SELECT ticker, market_cap FROM market_caps
            WHERE ticker IN ({placeholders})
            AND fetched_at > datetime('now', '-{hours} hours')
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


def get_cached_earnings(tickers, max_age_hours=None):
    """Look up cached earnings dates for a list of tickers.

    Args:
        tickers: List of ticker strings.
        max_age_hours: If set, only return rows fetched within this many hours.
                       If None (default), return all cached rows regardless of age.

    Returns a dict like {'AAPL': {'date': '2026-04-25', 'timing': 'after_market'}}.
    """
    if not tickers:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    placeholders = ", ".join([p] * len(tickers))

    if max_age_hours is None:
        # No age filter — return whatever's cached. Fast read path.
        cursor.execute(
            f"SELECT ticker, earnings_date, earnings_timing FROM earnings_cache "
            f"WHERE ticker IN ({placeholders})",
            tuple(tickers),
        )
    elif _using_postgres():
        hours = int(max_age_hours)
        cursor.execute(f"""
            SELECT ticker, earnings_date, earnings_timing FROM earnings_cache
            WHERE ticker IN ({placeholders})
            AND fetched_at > NOW() - INTERVAL '{hours} hours'
        """, tuple(tickers))
    else:
        hours = int(max_age_hours)
        cursor.execute(f"""
            SELECT ticker, earnings_date, earnings_timing FROM earnings_cache
            WHERE ticker IN ({placeholders})
            AND fetched_at > datetime('now', '-{hours} hours')
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


def _create_departure_extractions_table(conn):
    """Create the departure_extractions table — caches per-filing LLM extractions
    of executive departures (5.02 filings). Keyed by accession number, which is
    immutable, so cached rows never go stale."""
    cursor = conn.cursor()

    if _using_postgres():
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS departure_extractions (
                accession_number TEXT PRIMARY KEY,
                cik TEXT NOT NULL,
                filed_date TEXT NOT NULL,
                extractions_json TEXT NOT NULL,
                has_error INTEGER NOT NULL DEFAULT 0,
                extracted_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS departure_extractions (
                accession_number TEXT PRIMARY KEY,
                cik TEXT NOT NULL,
                filed_date TEXT NOT NULL,
                extractions_json TEXT NOT NULL,
                has_error INTEGER NOT NULL DEFAULT 0,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_departures_cik ON departure_extractions(cik)")

    conn.commit()
    print("[STARTUP] Departure extractions table ready")


def get_cached_stock_price(ticker, max_age_hours=1):
    """Look up cached stock price for a single ticker.

    Args:
        ticker: Single ticker string.
        max_age_hours: Only return if fetched within this many hours. Default 1.
                       Pass None to return regardless of age.

    Returns the price as a float, or None if not cached/stale.
    """
    if not ticker:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    if max_age_hours is None:
        cursor.execute(
            f"SELECT price FROM stock_prices WHERE ticker = {p}",
            (ticker.upper(),),
        )
    elif _using_postgres():
        hours = int(max_age_hours)
        cursor.execute(f"""
            SELECT price FROM stock_prices
            WHERE ticker = {p}
            AND fetched_at > NOW() - INTERVAL '{hours} hours'
        """, (ticker.upper(),))
    else:
        hours = int(max_age_hours)
        cursor.execute(f"""
            SELECT price FROM stock_prices
            WHERE ticker = {p}
            AND fetched_at > datetime('now', '-{hours} hours')
        """, (ticker.upper(),))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None
    if _using_postgres():
        return row[0]
    else:
        return row["price"]


def get_cached_stock_prices(tickers, max_age_hours=None):
    """Batch version of get_cached_stock_price — looks up many tickers in one query.

    Args:
        tickers: List of ticker strings.
        max_age_hours: If set, only return rows fetched within this many hours.
                       If None (default), return all cached rows regardless of age.

    Returns a dict like {'AAPL': 175.23, 'MSFT': 412.50}.
    """
    if not tickers:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    upper_tickers = [t.upper() for t in tickers]
    placeholders = ", ".join([p] * len(upper_tickers))

    if max_age_hours is None:
        cursor.execute(
            f"SELECT ticker, price FROM stock_prices WHERE ticker IN ({placeholders})",
            tuple(upper_tickers),
        )
    elif _using_postgres():
        hours = int(max_age_hours)
        cursor.execute(f"""
            SELECT ticker, price FROM stock_prices
            WHERE ticker IN ({placeholders})
            AND fetched_at > NOW() - INTERVAL '{hours} hours'
        """, tuple(upper_tickers))
    else:
        hours = int(max_age_hours)
        cursor.execute(f"""
            SELECT ticker, price FROM stock_prices
            WHERE ticker IN ({placeholders})
            AND fetched_at > datetime('now', '-{hours} hours')
        """, tuple(upper_tickers))

    rows = cursor.fetchall()
    conn.close()

    if _using_postgres():
        return {row[0]: row[1] for row in rows}
    else:
        return {row["ticker"]: row["price"] for row in rows}


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


# ============================================================
# DEPARTURE EXTRACTIONS CACHE FUNCTIONS
# ============================================================

def get_cached_departure_extraction(accession_number):
    """Look up cached LLM extraction for a 5.02 filing.

    Returns a real Python dict with keys: accession_number, cik, filed_date,
    extractions (list of {date, person, position, reason}), has_error, extracted_at.
    Returns None if not cached.
    """
    if not accession_number:
        return None
    import json
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    cursor.execute(f"""
        SELECT accession_number, cik, filed_date, extractions_json, has_error, extracted_at
        FROM departure_extractions
        WHERE accession_number = {p}
    """, (accession_number,))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    if _using_postgres():
        return {
            "accession_number": row[0],
            "cik": row[1],
            "filed_date": row[2],
            "extractions": json.loads(row[3] or "[]"),
            "has_error": int(row[4] or 0),
            "extracted_at": row[5],
        }
    else:
        return {
            "accession_number": row["accession_number"],
            "cik": row["cik"],
            "filed_date": row["filed_date"],
            "extractions": json.loads(row["extractions_json"] or "[]"),
            "has_error": int(row["has_error"] or 0),
            "extracted_at": row["extracted_at"],
        }


def upsert_departure_extraction(accession_number, cik, filed_date, extractions, has_error):
    """Insert or update a cached LLM extraction.

    Args:
        accession_number: SEC accession (e.g., "0001234567-25-000123")
        cik: zero-padded or unpadded CIK string
        filed_date: filing date as "YYYY-MM-DD"
        extractions: list of dicts [{date, person, position, reason}]
        has_error: True if extraction failed (cache the failure to avoid retrying forever)
    """
    if not accession_number:
        return
    import json
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    extractions_json = json.dumps(extractions or [])
    err_int = 1 if has_error else 0

    if _using_postgres():
        cursor.execute(f"""
            INSERT INTO departure_extractions
            (accession_number, cik, filed_date, extractions_json, has_error, extracted_at)
            VALUES ({p}, {p}, {p}, {p}, {p}, CURRENT_TIMESTAMP)
            ON CONFLICT (accession_number) DO UPDATE
            SET cik = {p}, filed_date = {p}, extractions_json = {p},
                has_error = {p}, extracted_at = CURRENT_TIMESTAMP
        """, (
            accession_number, cik, filed_date, extractions_json, err_int,
            cik, filed_date, extractions_json, err_int,
        ))
    else:
        cursor.execute(f"""
            INSERT OR REPLACE INTO departure_extractions
            (accession_number, cik, filed_date, extractions_json, has_error, extracted_at)
            VALUES ({p}, {p}, {p}, {p}, {p}, CURRENT_TIMESTAMP)
        """, (accession_number, cik, filed_date, extractions_json, err_int))

    conn.commit()
    conn.close()


# When this file is run directly, create the database
if __name__ == "__main__":
    initialize_database()
    if _using_postgres():
        print("PostgreSQL database initialized")
    else:
        print(f"SQLite database initialized at {DATABASE_PATH}")
