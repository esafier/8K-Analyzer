# database.py — Database setup and helper functions
# Supports PostgreSQL (when DATABASE_URL is set, e.g. on Render)
# and SQLite (local development fallback)

import sqlite3
from config import DATABASE_PATH, DATABASE_URL

# Try to import psycopg2 for PostgreSQL support
# If it's not installed (local dev without it), that's fine — we'll use SQLite
try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


def _using_postgres():
    """Check if we should use PostgreSQL (DATABASE_URL is set and psycopg2 available)."""
    return DATABASE_URL is not None and HAS_PSYCOPG2


def get_connection():
    """Open a database connection. Uses PostgreSQL if DATABASE_URL is set,
    otherwise falls back to local SQLite file."""
    if _using_postgres():
        # Render provides DATABASE_URL starting with "postgres://" but psycopg2
        # needs "postgresql://" — fix it if needed
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url)
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
    SQLite rows are already dict-like, but psycopg2 rows need conversion."""
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
