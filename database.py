# database.py — Database setup and helper functions
# Supports PostgreSQL (when DATABASE_URL is set, e.g. on Render)
# and SQLite (local development fallback)

import os
import sqlite3
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


def get_connection():
    """Open a database connection. Uses PostgreSQL if DATABASE_URL is set,
    otherwise falls back to local SQLite file."""
    if _using_postgres():
        # Parse the DATABASE_URL into components that pg8000 needs
        url = _get_database_url()
        # URL format: postgres://user:password@host:port/dbname
        # Strip the scheme prefix
        if url.startswith("postgres://"):
            url = url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = url[len("postgresql://"):]

        # Split into user_info@host_info/dbname
        user_info, rest = url.split("@", 1)
        host_info, dbname = rest.split("/", 1)
        user, password = user_info.split(":", 1)
        if ":" in host_info:
            host, port = host_info.split(":", 1)
            port = int(port)
        else:
            host = host_info
            port = 5432

        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        conn = pg8000.dbapi.connect(
            user=user,
            password=password,
            host=host,
            port=port,
            database=dbname,
            ssl_context=ssl_context
        )
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
    filing_data is a dictionary with keys matching the column names."""
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

    conn.commit()
    conn.close()


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
    Returns filing data joined with watchlist notes."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT f.*, w.notes as watchlist_notes, w.added_at as watchlist_added_at
        FROM filings f
        JOIN watchlist w ON f.id = w.filing_id
        ORDER BY w.added_at DESC
    """)
    results = _dict_rows(cursor.fetchall(), cursor)
    conn.close()
    return results


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


# When this file is run directly, create the database
if __name__ == "__main__":
    initialize_database()
    if _using_postgres():
        print("PostgreSQL database initialized")
    else:
        print(f"SQLite database initialized at {DATABASE_PATH}")
