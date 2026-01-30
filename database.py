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

    # Log which database we're using and how many filings are stored
    # This helps us debug data loss issues on Render
    cursor.execute("SELECT COUNT(*) FROM filings")
    count = cursor.fetchone()[0]
    if _using_postgres():
        print(f"[STARTUP] Using PostgreSQL — {count} filings in database")
    else:
        print(f"[STARTUP] Using SQLite — {count} filings in database")

    conn.close()


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

    if _using_postgres():
        # PostgreSQL: use ON CONFLICT instead of INSERT OR IGNORE
        cursor.execute(f"""
            INSERT INTO filings
            (accession_no, company, ticker, cik, filed_date, item_codes,
             summary, auto_category, auto_subcategory, filing_url, raw_text, matched_keywords)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
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
        ))
    else:
        # SQLite: original INSERT OR IGNORE
        cursor.execute(f"""
            INSERT OR IGNORE INTO filings
            (accession_no, company, ticker, cik, filed_date, item_codes,
             summary, auto_category, auto_subcategory, filing_url, raw_text, matched_keywords)
            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
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
        ))

    conn.commit()
    conn.close()


def get_filings(category=None, search=None, date_from=None, date_to=None, limit=100, offset=0):
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

    query += f" ORDER BY filed_date DESC LIMIT {p} OFFSET {p}"
    params.extend([limit, offset])

    cursor.execute(query, params)
    results = _dict_rows(cursor.fetchall(), cursor)
    conn.close()
    return results


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


def update_user_tag(filing_id, tag):
    """Update the user's manual tag for a filing.
    This overrides the auto-assigned category in the dashboard."""
    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()
    cursor.execute(f"UPDATE filings SET user_tag = {p} WHERE id = {p}", (tag, filing_id))
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


# When this file is run directly, create the database
if __name__ == "__main__":
    initialize_database()
    if _using_postgres():
        print("PostgreSQL database initialized")
    else:
        print(f"SQLite database initialized at {DATABASE_PATH}")
