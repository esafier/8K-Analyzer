# Unread Filing Tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add scroll-tracked read/unread state to the filings dashboard so the user can pick up where they left off after a backfill.

**Architecture:** One nullable `read_at` column on the `filings` table. Server-side: a `/api/filings/mark-read` POST endpoint and a one-line side-effect on the existing `/filing/<id>` route. Client-side: IntersectionObserver tracks each row's dwell time, batches IDs, POSTs every 2s. Unread rows get a left border + bold company name; a new "Unread only" filter checkbox lives next to the existing URGENT / Market Targets toggles.

**Tech Stack:** Flask, Jinja2, vanilla JS (IntersectionObserver), SQLite (local) + PostgreSQL (Render), pytest.

**Spec:** [docs/superpowers/specs/2026-05-26-unread-filing-tracking-design.md](../specs/2026-05-26-unread-filing-tracking-design.md)

---

## File structure overview

**Modified:**
- `database.py` — add migration step, `mark_filings_read()` helper, `unread_only` arg on `get_filings` / `get_filtered_filing_count`.
- `app.py` — add `POST /api/filings/mark-read`, add `unread` query-param handling on `/`, add `read_at` mark on `/filing/<id>`.
- `templates/index.html` — `data-filing-id` and `.unread` class on rows, new filter checkbox, JS block.
- `static/style.css` — `.unread` row styles.

**Created:**
- `tests/test_unread_tracking.py` — DB and route tests for the new behavior.

No new modules. The feature is small enough to live alongside existing code.

---

## Task 1: Schema migration — add `read_at` column with clean-slate backfill

**Files:**
- Modify: `database.py:279-330` (the `_migrate_add_columns` function)
- Test: `tests/test_unread_tracking.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_unread_tracking.py`:

```python
"""Tests for the unread filing tracking feature."""
import sqlite3
import pytest


def test_read_at_column_exists(tmp_sqlite_db):
    """After initialize_database() runs, filings must have a read_at column."""
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(filings)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert "read_at" in columns


def test_read_at_index_exists(tmp_sqlite_db):
    """An index on read_at should exist to keep 'unread only' queries fast."""
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='filings'")
    indexes = {row[0] for row in cursor.fetchall()}
    conn.close()
    assert "idx_filings_read_at" in indexes


def test_read_at_default_is_null_for_new_inserts(tmp_sqlite_db):
    """Any direct INSERT that doesn't supply read_at leaves it NULL.

    This is the property the dashboard depends on: brand-new filings from the
    SEC arrive unread (NULL). The clean-slate migration only touched
    pre-existing rows at the moment the column was added; future inserts get
    NULL by default and need scroll-tracking / detail-view to mark them read.
    """
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO filings (accession_no, company, ticker, filed_date)
        VALUES ('0000000001-26-000001', 'NewArrival Co', 'NEW', '2026-01-01')
    """)
    conn.commit()
    cursor.execute("SELECT read_at FROM filings WHERE accession_no = '0000000001-26-000001'")
    read_at = cursor.fetchone()[0]
    conn.close()
    assert read_at is None, "New inserts must default to read_at IS NULL (unread)"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_unread_tracking.py -v`
Expected: All three FAIL — `read_at` column missing, index missing, migration not populating.

- [ ] **Step 3: Add migration code**

In `database.py`, inside `_migrate_add_columns(conn)` (around line 279), add a new block after the existing column-add checks but before the function returns:

```python
    # Add read_at timestamp for tracking which filings the user has reviewed.
    # NULL = unread; non-null timestamp = read at that time.
    if "read_at" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN read_at TIMESTAMP DEFAULT NULL")
        print("[MIGRATE] Added 'read_at' column")

        # Clean-slate: mark every pre-existing row as read so the user isn't
        # buried under thousands of "unread" items the moment the feature ships.
        # Runs only once because this branch only fires when the column is freshly added.
        cursor.execute("UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE read_at IS NULL")
        print(f"[MIGRATE] Marked {cursor.rowcount} pre-existing filings as read (clean slate)")

    # Index on read_at — the "Show unread only" dashboard query uses WHERE read_at IS NULL
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_filings_read_at ON filings(read_at)")

    conn.commit()
```

Note: the existing `_migrate_add_columns` already calls `conn.commit()` at the end. Verify there is exactly one commit at the end of the function after this addition (don't duplicate it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_unread_tracking.py -v`
Expected: All three PASS.

- [ ] **Step 5: Run the existing migration test to make sure nothing else broke**

Run: `pytest tests/test_database_migration.py -v`
Expected: All three existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add database.py tests/test_unread_tracking.py
git commit -m "feat: add read_at column to filings with clean-slate migration"
```

---

## Task 2: DB helper — `mark_filings_read(filing_ids)`

**Files:**
- Modify: `database.py` (add new function after `get_filing_by_id`, around line 615)
- Test: `tests/test_unread_tracking.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_unread_tracking.py`:

```python
def test_mark_filings_read_sets_read_at(tmp_sqlite_db):
    """mark_filings_read([ids]) sets read_at for each unread row."""
    import database

    # Insert two unread filings
    for i, accession in enumerate(["A-1", "A-2"]):
        database.insert_filing({
            "accession_no": accession,
            "company": f"Co {i}",
            "ticker": "X",
            "cik": "0001",
            "filed_date": "2026-05-01",
            "item_codes": "5.02",
            "summary": "",
            "auto_category": "Compensation",
            "filing_url": "https://example.com",
            "raw_text": "",
            "matched_keywords": "",
            "urgent": False,
            "comp_details": None,
            "is_complex": False,
            "narrative_summary": None,
            "relevant_reason": None,
            "structured_summary": None,
        })

    # Force them unread (insert_filing should not set read_at, but be explicit)
    import sqlite3
    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no IN ('A-1','A-2')")
    conn.commit()
    conn.close()

    row1 = database.get_filing_by_accession("A-1")
    row2 = database.get_filing_by_accession("A-2")

    marked = database.mark_filings_read([row1["id"], row2["id"]])
    assert marked == 2

    # Both should now have read_at set
    assert database.get_filing_by_accession("A-1")["read_at"] is not None
    assert database.get_filing_by_accession("A-2")["read_at"] is not None


def test_mark_filings_read_is_idempotent(tmp_sqlite_db):
    """Re-marking a filing that's already read returns 0 (no rows updated)."""
    import database

    database.insert_filing({
        "accession_no": "B-1", "company": "Co", "ticker": "X", "cik": "1",
        "filed_date": "2026-05-01", "item_codes": "5.02", "summary": "",
        "auto_category": "Compensation", "filing_url": "https://example.com",
        "raw_text": "", "matched_keywords": "", "urgent": False,
        "comp_details": None, "is_complex": False, "narrative_summary": None,
        "relevant_reason": None, "structured_summary": None,
    })
    row = database.get_filing_by_accession("B-1")

    # First call: insert_filing leaves read_at NULL → 1 row updated
    first = database.mark_filings_read([row["id"]])
    assert first == 1

    # Second call: row is already read → 0 rows updated
    second = database.mark_filings_read([row["id"]])
    assert second == 0


def test_mark_filings_read_empty_list(tmp_sqlite_db):
    """Empty list is a no-op, returns 0."""
    import database
    assert database.mark_filings_read([]) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_unread_tracking.py::test_mark_filings_read_sets_read_at tests/test_unread_tracking.py::test_mark_filings_read_is_idempotent tests/test_unread_tracking.py::test_mark_filings_read_empty_list -v`
Expected: All three FAIL — `database.mark_filings_read` does not exist.

- [ ] **Step 3: Implement `mark_filings_read`**

In `database.py`, add after the `get_filing_by_id` function (around line 615):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_unread_tracking.py -v`
Expected: All tests (including the migration ones from Task 1) PASS.

- [ ] **Step 5: Commit**

```bash
git add database.py tests/test_unread_tracking.py
git commit -m "feat: add mark_filings_read DB helper"
```

---

## Task 3: Extend `get_filings` and `get_filtered_filing_count` with `unread_only`

**Files:**
- Modify: `database.py:505-547` (the `get_filings` function)
- Modify: `database.py:550-585` (the `get_filtered_filing_count` function)
- Test: `tests/test_unread_tracking.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_unread_tracking.py`:

```python
def test_get_filings_unread_only(tmp_sqlite_db):
    """get_filings(unread_only=True) returns only filings with read_at IS NULL."""
    import database
    import sqlite3

    base = {
        "ticker": "X", "cik": "1", "filed_date": "2026-05-01",
        "item_codes": "5.02", "summary": "", "auto_category": "Compensation",
        "filing_url": "https://example.com", "raw_text": "", "matched_keywords": "",
        "urgent": False, "comp_details": None, "is_complex": False,
        "narrative_summary": None, "relevant_reason": None, "structured_summary": None,
    }
    database.insert_filing({**base, "accession_no": "U-1", "company": "Unread1"})
    database.insert_filing({**base, "accession_no": "U-2", "company": "Unread2"})
    database.insert_filing({**base, "accession_no": "R-1", "company": "Read1"})

    # Mark one as read; force the other two unread
    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE accession_no = 'R-1'")
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no IN ('U-1','U-2')")
    conn.commit()
    conn.close()

    unread = database.get_filings(unread_only=True)
    accessions = {f["accession_no"] for f in unread}
    assert accessions == {"U-1", "U-2"}


def test_get_filtered_filing_count_unread_only(tmp_sqlite_db):
    """get_filtered_filing_count(unread_only=True) only counts NULL read_at."""
    import database
    import sqlite3

    base = {
        "ticker": "X", "cik": "1", "filed_date": "2026-05-01",
        "item_codes": "5.02", "summary": "", "auto_category": "Compensation",
        "filing_url": "https://example.com", "raw_text": "", "matched_keywords": "",
        "urgent": False, "comp_details": None, "is_complex": False,
        "narrative_summary": None, "relevant_reason": None, "structured_summary": None,
    }
    database.insert_filing({**base, "accession_no": "C-1", "company": "C1"})
    database.insert_filing({**base, "accession_no": "C-2", "company": "C2"})
    database.insert_filing({**base, "accession_no": "C-3", "company": "C3"})

    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE accession_no = 'C-3'")
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no IN ('C-1','C-2')")
    conn.commit()
    conn.close()

    assert database.get_filtered_filing_count(unread_only=True) == 2
    assert database.get_filtered_filing_count(unread_only=False) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_unread_tracking.py::test_get_filings_unread_only tests/test_unread_tracking.py::test_get_filtered_filing_count_unread_only -v`
Expected: FAIL — `get_filings()` does not accept `unread_only`.

- [ ] **Step 3: Modify `get_filings`**

In `database.py`, change the function signature and add the new clause. The new signature:

```python
def get_filings(category=None, search=None, date_from=None, date_to=None, urgent_only=False, market_targets_only=False, unread_only=False, limit=100, offset=0):
```

Inside the function body, after the existing `if market_targets_only:` block (around line 540) and before the `ORDER BY ... LIMIT` line, add:

```python
    if unread_only:
        query += " AND read_at IS NULL"
```

- [ ] **Step 4: Modify `get_filtered_filing_count` identically**

In `database.py`, change the signature to add `unread_only=False`:

```python
def get_filtered_filing_count(category=None, search=None, date_from=None, date_to=None, urgent_only=False, market_targets_only=False, unread_only=False):
```

After the existing `if market_targets_only:` block in `get_filtered_filing_count`, add the same line:

```python
    if unread_only:
        query += " AND read_at IS NULL"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_unread_tracking.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add database.py tests/test_unread_tracking.py
git commit -m "feat: add unread_only filter to get_filings and count"
```

---

## Task 4: API endpoint — `POST /api/filings/mark-read`

**Files:**
- Modify: `app.py` (add new route near other API routes, e.g. after `/deep-analysis` around line 580)
- Test: `tests/test_unread_tracking.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_unread_tracking.py`:

```python
@pytest.fixture
def flask_client(tmp_sqlite_db, monkeypatch):
    """Flask test client with auth disabled (no TRIAL_CODE set).

    Relies on `tmp_sqlite_db` having monkeypatched `database.DATABASE_PATH` first.
    App's `from database import ...` resolves function names at call time, so
    requests hitting the routes will use the patched DB path automatically.
    """
    monkeypatch.delenv("TRIAL_CODE", raising=False)
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def test_mark_read_endpoint_marks_filings(flask_client, tmp_sqlite_db):
    """POST /api/filings/mark-read with filing_ids updates the DB."""
    import database
    import sqlite3
    import json

    base = {
        "ticker": "X", "cik": "1", "filed_date": "2026-05-01",
        "item_codes": "5.02", "summary": "", "auto_category": "Compensation",
        "filing_url": "https://example.com", "raw_text": "", "matched_keywords": "",
        "urgent": False, "comp_details": None, "is_complex": False,
        "narrative_summary": None, "relevant_reason": None, "structured_summary": None,
    }
    database.insert_filing({**base, "accession_no": "E-1", "company": "E1"})
    database.insert_filing({**base, "accession_no": "E-2", "company": "E2"})

    # Force unread
    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no IN ('E-1','E-2')")
    conn.commit()
    conn.close()

    ids = [
        database.get_filing_by_accession("E-1")["id"],
        database.get_filing_by_accession("E-2")["id"],
    ]
    resp = flask_client.post(
        "/api/filings/mark-read",
        data=json.dumps({"filing_ids": ids}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"marked": 2}


def test_mark_read_endpoint_rejects_non_list(flask_client):
    """Bad payload (filing_ids not a list) returns 400."""
    import json
    resp = flask_client.post(
        "/api/filings/mark-read",
        data=json.dumps({"filing_ids": "not a list"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_mark_read_endpoint_rejects_non_int_ids(flask_client):
    """Bad payload (non-int IDs) returns 400."""
    import json
    resp = flask_client.post(
        "/api/filings/mark-read",
        data=json.dumps({"filing_ids": ["not", "ints"]}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_mark_read_endpoint_empty_list_returns_zero(flask_client):
    """Empty list is valid, returns marked=0."""
    import json
    resp = flask_client.post(
        "/api/filings/mark-read",
        data=json.dumps({"filing_ids": []}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"marked": 0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_unread_tracking.py -v`
Expected: The new endpoint tests FAIL with 404 — endpoint doesn't exist yet.

- [ ] **Step 3: Add the endpoint**

In `app.py`, near the other API routes (a logical spot is right after the `/deep-analysis/<int:filing_id>` route, around line 575). Add:

```python
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

    # Validate every entry is an int (or strict-int string)
    cleaned = []
    for fid in filing_ids:
        if isinstance(fid, bool):
            # bool is a subclass of int in Python — reject explicitly
            return jsonify({"error": "filing_ids must be integers"}), 400
        if isinstance(fid, int):
            cleaned.append(fid)
        else:
            return jsonify({"error": "filing_ids must be integers"}), 400

    from database import mark_filings_read
    marked = mark_filings_read(cleaned)
    return jsonify({"marked": marked})
```

Make sure `jsonify` and `request` are already imported at the top of `app.py` (they should be — they're used by other routes).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_unread_tracking.py -v`
Expected: All endpoint tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_unread_tracking.py
git commit -m "feat: add POST /api/filings/mark-read endpoint"
```

---

## Task 5: Detail-page side-effect — mark filing read on view

**Files:**
- Modify: `app.py:293-296` (the `filing_detail` route)
- Test: `tests/test_unread_tracking.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_unread_tracking.py`:

```python
def test_filing_detail_marks_unread_filing_as_read(flask_client, tmp_sqlite_db):
    """GET /filing/<id> marks an unread filing as read (and is idempotent)."""
    import database
    import sqlite3

    database.insert_filing({
        "accession_no": "D-1", "company": "DetailCo", "ticker": "X", "cik": "1",
        "filed_date": "2026-05-01", "item_codes": "5.02", "summary": "",
        "auto_category": "Compensation", "filing_url": "https://example.com",
        "raw_text": "", "matched_keywords": "", "urgent": False,
        "comp_details": None, "is_complex": False, "narrative_summary": None,
        "relevant_reason": None, "structured_summary": None,
    })

    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no = 'D-1'")
    conn.commit()
    conn.close()

    filing_id = database.get_filing_by_accession("D-1")["id"]

    # Confirm precondition
    assert database.get_filing_by_id(filing_id)["read_at"] is None

    # Hit the detail page
    resp = flask_client.get(f"/filing/{filing_id}")
    assert resp.status_code == 200

    # Now should be marked read
    assert database.get_filing_by_id(filing_id)["read_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_unread_tracking.py::test_filing_detail_marks_unread_filing_as_read -v`
Expected: FAIL — `read_at` stays None after GET.

- [ ] **Step 3: Modify the detail route**

In `app.py`, change the `filing_detail` route (around line 293) so it marks the filing read before rendering:

```python
@app.route("/filing/<int:filing_id>")
def filing_detail(filing_id):
    """Detail page for a single filing."""
    # Opening a filing counts as reading it. Idempotent — no-op if already read.
    from database import mark_filings_read
    mark_filings_read([filing_id])
    return _render_filing_detail(filing_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_unread_tracking.py::test_filing_detail_marks_unread_filing_as_read -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_unread_tracking.py
git commit -m "feat: mark filing as read on detail page view"
```

---

## Task 6: Dashboard route — pass `unread` query param through to DB + template

**Files:**
- Modify: `app.py:182-290` (the `index` route)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_unread_tracking.py`:

```python
def test_index_unread_param_filters_results(flask_client, tmp_sqlite_db):
    """GET /?unread=1 only shows filings with NULL read_at."""
    import database
    import sqlite3

    base = {
        "ticker": "X", "cik": "1", "filed_date": "2026-05-01",
        "item_codes": "5.02", "summary": "",
        "auto_category": "Compensation", "filing_url": "https://example.com",
        "raw_text": "", "matched_keywords": "", "urgent": False,
        "comp_details": None, "is_complex": False, "narrative_summary": None,
        "relevant_reason": None, "structured_summary": None,
    }
    database.insert_filing({**base, "accession_no": "Idx-Read", "company": "WasRead"})
    database.insert_filing({**base, "accession_no": "Idx-Unread", "company": "StillUnread"})

    conn = sqlite3.connect(tmp_sqlite_db)
    conn.execute("UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE accession_no = 'Idx-Read'")
    conn.execute("UPDATE filings SET read_at = NULL WHERE accession_no = 'Idx-Unread'")
    conn.commit()
    conn.close()

    resp = flask_client.get("/?unread=1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "StillUnread" in body
    assert "WasRead" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_unread_tracking.py::test_index_unread_param_filters_results -v`
Expected: FAIL — both companies appear (unread param is ignored).

- [ ] **Step 3: Modify the `index` route**

In `app.py`, in the `index()` function around line 190, add the query-param read:

```python
    urgent_only = request.args.get("urgent", "") == "1"
    market_targets_only = request.args.get("market_targets", "") == "1"
    unread_only = request.args.get("unread", "") == "1"
    page = int(request.args.get("page", 1))
```

Pass `unread_only` into both `get_filings(...)` and `get_filtered_filing_count(...)` — add `unread_only=unread_only,` to each call's keyword args.

In the `render_template(...)` call at the end of the function (around line 271-290), add:

```python
        current_unread=unread_only,
```

next to the other `current_*` kwargs.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_unread_tracking.py::test_index_unread_param_filters_results -v`
Expected: PASS.

- [ ] **Step 5: Run the full test suite to confirm nothing else broke**

Run: `pytest -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_unread_tracking.py
git commit -m "feat: dashboard accepts ?unread=1 to filter to unread filings"
```

---

## Task 7: Template — unread filter checkbox + row markup

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Add the filter checkbox**

In `templates/index.html`, inside the filter card's flex container that already holds the URGENT and Market Targets checkboxes (around lines 50-65), add a third checkbox after Market Targets:

```jinja
                <div class="form-check mb-2">
                    <input type="checkbox" name="unread" value="1" class="form-check-input"
                           id="unreadCheck" {{ 'checked' if current_unread }}>
                    <label class="form-check-label" for="unreadCheck">
                        <span class="badge bg-primary">Unread</span> only
                    </label>
                </div>
```

- [ ] **Step 2: Add `data-filing-id` and `unread` class on rows**

In `templates/index.html`, find the `<tr class="clickable-row" onclick="...">` line (around line 93). Change to:

```jinja
            <tr class="clickable-row{% if not filing['read_at'] %} unread{% endif %}"
                data-filing-id="{{ filing['id'] }}"
                onclick="window.location='/filing/{{ filing['id'] }}?back={{ ('/?page=' ~ current_page ~ '&category=' ~ current_category ~ '&search=' ~ current_search ~ '&date_from=' ~ current_date_from ~ '&date_to=' ~ current_date_to ~ '&urgent=' ~ ('1' if current_urgent else '') ~ '&market_targets=' ~ ('1' if current_market_targets else '') ~ '&unread=' ~ ('1' if current_unread else ''))|urlencode }}'">
```

(The `&unread=` segment is added to the `back` URL so that returning from a filing detail keeps the unread filter active.)

- [ ] **Step 3: Add `unread` to the pagination URL builder**

In `templates/index.html`, find the `{% set q = ... %}` line (around line 164). Change to:

```jinja
{% set q = '&category=' ~ current_category ~ '&search=' ~ current_search ~ '&date_from=' ~ current_date_from ~ '&date_to=' ~ current_date_to ~ '&urgent=' ~ ('1' if current_urgent else '') ~ '&market_targets=' ~ ('1' if current_market_targets else '') ~ '&unread=' ~ ('1' if current_unread else '') %}
```

- [ ] **Step 4: Manual smoke check via app boot**

Run: `python app.py` (or whatever the local dev command is)
Visit `http://localhost:<port>/?unread=1` — confirm the page renders without a Jinja error. Confirm the new checkbox is visible in the filter card. Confirm filings inserted before the migration (now read) look normal; any new filing added after the migration should have a left border.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat: add unread filter checkbox and row markup to dashboard"
```

---

## Task 8: CSS — unread row styling

**Files:**
- Modify: `static/style.css`

- [ ] **Step 1: Add the unread styles**

Append to `static/style.css`:

```css
/* Unread filing row: subtle left-edge accent + bolder company name.
   Marker reflects state at page load time; we deliberately do NOT repaint
   rows as the scroll-tracking JS marks them read mid-session — that
   would feel jumpy. Next page refresh picks up the new state. */
tr.unread {
    border-left: 4px solid #0d6efd;
}

tr.unread td:nth-child(4) {
    /* "Company" is the 4th visible column (star, badges, date, company, ...) */
    font-weight: 600;
}
```

- [ ] **Step 2: Hard-reload the dashboard in a browser**

Confirm a brand-new filing (added after the migration ran) has the left border + bold company. Confirm pre-migration filings (now read) do not.

- [ ] **Step 3: Commit**

```bash
git add static/style.css
git commit -m "feat: add unread row styling (left border + bold company)"
```

---

## Task 9: Scroll-tracking JavaScript

**Files:**
- Modify: `templates/index.html` (append a `<script>` block at the bottom, alongside the existing watchlist-star script)

- [ ] **Step 1: Add the JS block**

In `templates/index.html`, find the existing `<script>` block at the bottom (it starts around `document.addEventListener('DOMContentLoaded', function() {`). Append a new `<script>` block AFTER the existing one (do not modify the watchlist-star script):

```jinja
<script>
// ---- Scroll-tracked unread marking ----
// When a filing row sits at >=50% visible in the viewport for >=1.5s,
// queue its id. Every 2s, batch-POST the queue to /api/filings/mark-read.
// Visual unread markers are NOT removed mid-session — too jumpy.
// Next page refresh reflects the new state.
(function() {
    const DWELL_MS = 1500;
    const BATCH_INTERVAL_MS = 2000;
    const queue = new Set();
    const dwellTimers = new Map();

    const rows = document.querySelectorAll('tr.clickable-row[data-filing-id]');
    if (rows.length === 0) return;  // nothing to track

    const observer = new IntersectionObserver(function(entries) {
        for (const entry of entries) {
            const rawId = entry.target.dataset.filingId;
            const id = parseInt(rawId, 10);
            if (!Number.isFinite(id)) continue;

            // Only track rows that are currently unread on this page-load.
            // (Already-read rows have no .unread class; tracking them is wasted work.)
            if (!entry.target.classList.contains('unread')) continue;

            if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
                // Begin dwell — fires after DWELL_MS if it stays visible
                if (!dwellTimers.has(rawId)) {
                    dwellTimers.set(rawId, setTimeout(function() {
                        queue.add(id);
                        dwellTimers.delete(rawId);
                    }, DWELL_MS));
                }
            } else {
                // Left the viewport (or dipped below 50%) before timer fired
                if (dwellTimers.has(rawId)) {
                    clearTimeout(dwellTimers.get(rawId));
                    dwellTimers.delete(rawId);
                }
            }
        }
    }, { threshold: [0.5] });

    rows.forEach(function(row) { observer.observe(row); });

    function flushQueue() {
        if (queue.size === 0) return;
        const ids = [...queue];
        queue.clear();
        fetch('/api/filings/mark-read', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filing_ids: ids }),
        }).catch(function() {
            // Network failed — requeue so the next batch retries
            ids.forEach(function(id) { queue.add(id); });
        });
    }

    setInterval(flushQueue, BATCH_INTERVAL_MS);

    // Best-effort flush when the user closes the tab or navigates away.
    // sendBeacon is fire-and-forget; can't be cancelled and won't block unload.
    window.addEventListener('beforeunload', function() {
        if (queue.size === 0) return;
        const ids = [...queue];
        const blob = new Blob(
            [JSON.stringify({ filing_ids: ids })],
            { type: 'application/json' }
        );
        navigator.sendBeacon('/api/filings/mark-read', blob);
    });
})();
</script>
```

- [ ] **Step 2: Manual end-to-end check**

1. Run the app locally (`python app.py` or equivalent).
2. Insert a few new filings or run a tiny backfill so there are unread rows on the dashboard.
3. Open the dashboard in a browser with DevTools → Network tab open.
4. Scroll slowly so the unread rows enter the viewport. After ~1.5s of dwell per row, watch for a `POST /api/filings/mark-read` request to fire within the next 2s. Response should be 200 with `{"marked": N}`.
5. Hard-reload. The rows you scrolled past should no longer have the unread styling.
6. Toggle the "Unread only" checkbox → those rows should disappear from the filtered view.
7. Click into a filing detail page from an unread row, then back. Reload. That row should no longer be marked unread.

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: scroll-tracked unread marking on dashboard"
```

---

## Task 10: Final verification

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: All tests PASS, including the brand-new `tests/test_unread_tracking.py` (10 tests).

- [ ] **Step 2: Verify the diff against `main`**

Run: `git log --oneline main..HEAD` to list the new commits. There should be 9 commits, one per task (Tasks 1–9), with Task 10 just being verification.

- [ ] **Step 3: Spot-check the Render / PostgreSQL path**

The migration uses `CURRENT_TIMESTAMP` and `TIMESTAMP DEFAULT NULL` — both standard SQL, fine on PG. The `CREATE INDEX IF NOT EXISTS` is supported on PG ≥ 9.5 (Render uses ≥ 14). The `_placeholder()` helper is already swapping `?` → `%s` for both the `IN (...)` clause and the count/list queries. No PG-specific testing is set up locally, but the patterns mirror existing code that's already known to work on Render.

- [ ] **Step 4: (Optional) Push to a branch and let Render deploy a preview**

Not strictly required — but if you have a staging or preview branch on Render, push there first and run a quick smoke test before merging to `main`.

---

## What's verified end-to-end after this plan ships

- A filing with `read_at IS NULL` shows the unread visual marker.
- Scrolling past a row for >=1.5 sec triggers a batched POST that marks it read.
- Opening a filing detail page marks it read regardless of scroll-tracking state.
- `?unread=1` on the dashboard hides already-read filings.
- The migration is idempotent and runs cleanly on a fresh DB and the existing one.
- Pagination preserves the unread filter.
