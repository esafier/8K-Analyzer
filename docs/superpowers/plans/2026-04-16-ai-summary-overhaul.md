# AI Summary & Subcategory Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 1–2 sentence prose summary with a rich, event-based structured summary (departures / appointments / comp_events / other / narrative), store subcategories as an array, add a one-click "Open filing" link, and re-analyze recent filings with the new prompt.

**Architecture:** Single LLM call returns structured JSON with event arrays plus an optional narrative fallback for complex filings. Existing SQLite↔Postgres abstraction and `_migrate_add_columns` pattern are preserved. New prompt `prompt_v3.txt` lives alongside `prompt_v2.txt` (selectable via `ACTIVE_PROMPT` in [config.py](config.py)). Dashboard uses a shared Jinja partial to render the structured summary.

**Tech Stack:** Python 3, Flask 3, Jinja2, OpenAI SDK, SQLite (local) + PostgreSQL via pg8000 (Render). Tests with pytest.

**Spec:** [docs/superpowers/specs/2026-04-16-ai-summary-overhaul-design.md](../specs/2026-04-16-ai-summary-overhaul-design.md)

**Branch:** `feature/ai-summary-overhaul`

---

## File Structure

### New files
- `prompts/prompt_v3.txt` — the new LLM prompt with event-based JSON schema
- `summary_utils.py` — helpers for subcategory parsing and structured-summary access
- `templates/_structured_summary.html` — shared Jinja partial for rendering summaries
- `tests/__init__.py` — (empty) makes tests a package
- `tests/conftest.py` — pytest fixtures (tmp SQLite DB)
- `tests/test_summary_utils.py` — unit tests for summary_utils
- `tests/test_database_migration.py` — migration + column existence tests
- `tests/test_filter_v3.py` — filter.py integration tests with mocked LLM
- `tests/test_fetcher_doc_url.py` — fetcher tests with mocked SEC response

### Files to modify
- `requirements.txt` — add pytest
- `config.py` — flip `ACTIVE_PROMPT` pointer (done in Task 12, not earlier)
- `database.py` — new columns migration; extend insert/update signatures
- `fetcher.py` — return primary document URL alongside text
- `filter.py` — consume tuple from fetcher; handle v3 JSON shape; write subcategories as JSON array
- `llm.py` — no signature change; new prompt file is passed via existing `prompt_file` param
- `app.py` — register new Jinja filter; pass new filing fields to template; update `run_resummarize` to persist v3 fields
- `templates/index.html` — render structured summary; multiple subcategory badges; "Open filing" button
- `templates/filing.html` — render structured summary; collapsed debug section for `reasoning` + `relevant_reason`

---

## Task 1: Add pytest and create tests/ scaffold

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add pytest to requirements.txt**

Add this line at the bottom of [requirements.txt](../../../requirements.txt):

```
pytest==8.3.4
```

- [ ] **Step 2: Install pytest locally**

Run: `pip install pytest==8.3.4`
Expected: `Successfully installed pytest-8.3.4`

- [ ] **Step 3: Create empty package marker**

Create `tests/__init__.py` with empty content.

- [ ] **Step 4: Create conftest.py with temp DB fixture**

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures for the 8K analyzer test suite."""
import os
import pytest


@pytest.fixture
def tmp_sqlite_db(tmp_path, monkeypatch):
    """Point the app's SQLite DATABASE_PATH at a fresh temp file per test.

    Forces SQLite (not Postgres) by ensuring DATABASE_URL is unset.
    Imports database.py AFTER patching so module-level state picks up the temp path.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_file = tmp_path / "test_filings.db"

    # Patch both the config module and any already-imported reference in database.py
    import config
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(config, "DATABASE_URL", None)

    import database
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_file), raising=False)

    # Initialize schema
    database.initialize_database()
    yield str(db_file)
```

- [ ] **Step 5: Verify pytest runs**

Run: `pytest tests/ -v`
Expected: `no tests ran in 0.XXs` (pytest found no tests yet, but it works)

- [ ] **Step 6: Commit**

```bash
git add requirements.txt tests/__init__.py tests/conftest.py
git commit -m "Add pytest and tests/ scaffold with temp SQLite fixture"
```

---

## Task 2: Summary utilities — `parse_subcategories` helper

**Files:**
- Create: `summary_utils.py`
- Create: `tests/test_summary_utils.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_summary_utils.py`:

```python
"""Tests for summary_utils helpers."""
import json
from summary_utils import parse_subcategories, serialize_subcategories


class TestParseSubcategories:
    def test_parses_json_array(self):
        raw = '["CFO Departure", "CFO Appointment"]'
        assert parse_subcategories(raw) == ["CFO Departure", "CFO Appointment"]

    def test_single_string_becomes_one_element_list(self):
        raw = "CFO Departure"
        assert parse_subcategories(raw) == ["CFO Departure"]

    def test_none_returns_empty_list(self):
        assert parse_subcategories(None) == []

    def test_empty_string_returns_empty_list(self):
        assert parse_subcategories("") == []

    def test_whitespace_string_returns_empty_list(self):
        assert parse_subcategories("   ") == []

    def test_malformed_json_falls_back_to_single_string(self):
        # If someone wrote bad JSON, treat it as a literal string
        raw = '["unclosed'
        assert parse_subcategories(raw) == ['["unclosed']

    def test_json_non_array_falls_back_to_single_string(self):
        # JSON that parses but isn't a list
        raw = '{"foo": "bar"}'
        assert parse_subcategories(raw) == ['{"foo": "bar"}']


class TestSerializeSubcategories:
    def test_serializes_list_to_json_array(self):
        result = serialize_subcategories(["CFO Departure", "CFO Appointment"])
        assert json.loads(result) == ["CFO Departure", "CFO Appointment"]

    def test_empty_list_returns_none(self):
        # Preserve the ability to store NULL in the DB when nothing to say
        assert serialize_subcategories([]) is None

    def test_none_returns_none(self):
        assert serialize_subcategories(None) is None

    def test_strips_empty_strings_from_list(self):
        result = serialize_subcategories(["CFO Departure", "", None, "CFO Appointment"])
        assert json.loads(result) == ["CFO Departure", "CFO Appointment"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_summary_utils.py -v`
Expected: ImportError — `summary_utils` module does not exist yet.

- [ ] **Step 3: Create summary_utils.py with minimal implementation**

Create `summary_utils.py`:

```python
"""Helpers for working with the structured summary fields stored in the filings table.

Subcategories are stored as JSON arrays in a single TEXT column (auto_subcategory)
for backward compatibility with existing rows that hold a single subcategory string.
"""
import json
from typing import Optional


def parse_subcategories(raw: Optional[str]) -> list[str]:
    """Convert the stored auto_subcategory string into a list.

    Handles three shapes:
      - JSON array string  -> parse normally
      - Plain string       -> wrap in a one-element list (legacy rows)
      - None / empty       -> empty list
    """
    if not raw or not str(raw).strip():
        return []

    raw = str(raw).strip()

    # Try JSON array first (new shape)
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except (json.JSONDecodeError, ValueError):
            pass  # Fall through to single-string handling

    # Legacy single-subcategory string — wrap it
    return [raw]


def serialize_subcategories(subcats: Optional[list[str]]) -> Optional[str]:
    """Convert a list of subcategories into the stored JSON array string.

    Returns None when nothing to store (empty list or None input).
    Filters out empty / None values defensively.
    """
    if not subcats:
        return None

    cleaned = [str(s).strip() for s in subcats if s and str(s).strip()]
    if not cleaned:
        return None

    return json.dumps(cleaned)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_summary_utils.py -v`
Expected: All 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add summary_utils.py tests/test_summary_utils.py
git commit -m "Add summary_utils with parse/serialize subcategory helpers"
```

---

## Task 3: Database migration — add new columns

**Files:**
- Modify: `database.py` (extend `_migrate_add_columns`)
- Create: `tests/test_database_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_database_migration.py`:

```python
"""Tests that database migrations add the expected columns."""
import sqlite3


def test_new_columns_exist_after_init(tmp_sqlite_db):
    """After initialize_database() runs, the filings table must have the new v3 columns."""
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(filings)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert "filing_document_url" in columns, "filing_document_url column missing"
    assert "is_complex" in columns, "is_complex column missing"
    assert "narrative_summary" in columns, "narrative_summary column missing"
    assert "relevant_reason" in columns, "relevant_reason column missing"


def test_migration_is_idempotent(tmp_sqlite_db):
    """Calling initialize_database() a second time must not fail (columns already exist)."""
    import database
    # initialize_database was called by the fixture; call it again
    database.initialize_database()  # Should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_database_migration.py -v`
Expected: `AssertionError: filing_document_url column missing` (and others).

- [ ] **Step 3: Extend `_migrate_add_columns` in database.py**

Open [database.py](../../../database.py) and locate `_migrate_add_columns` (around line 271). Add these blocks **after** the existing `deep_analysis` migration and **before** `conn.commit()`:

```python
    # Add filing_document_url (primary 8-K document URL for one-click navigation)
    if "filing_document_url" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN filing_document_url TEXT DEFAULT NULL")
        print("[MIGRATE] Added 'filing_document_url' column")

    # Add is_complex flag — set when filing doesn't fit structured buckets cleanly
    if "is_complex" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN is_complex INTEGER DEFAULT 0")
        print("[MIGRATE] Added 'is_complex' column")

    # Add narrative_summary — free-text fallback for complex filings
    if "narrative_summary" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN narrative_summary TEXT DEFAULT NULL")
        print("[MIGRATE] Added 'narrative_summary' column")

    # Add relevant_reason — LLM's justification when relevant:false
    if "relevant_reason" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN relevant_reason TEXT DEFAULT NULL")
        print("[MIGRATE] Added 'relevant_reason' column")

    # Add structured_summary — JSON blob holding the full v3 payload (departures[], etc.)
    if "structured_summary" not in existing:
        cursor.execute("ALTER TABLE filings ADD COLUMN structured_summary TEXT DEFAULT NULL")
        print("[MIGRATE] Added 'structured_summary' column")
```

Note: we add a **fifth** column `structured_summary` (not mentioned in the spec explicitly but needed to persist the full event arrays from the LLM response). The spec's schema (`departures[]`, `appointments[]`, `comp_events[]`, `other[]`) has to live somewhere — we store the whole JSON blob in one column for simplicity.

Update the test to include this column:

```python
    assert "structured_summary" in columns, "structured_summary column missing"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_database_migration.py -v`
Expected: Both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add database.py tests/test_database_migration.py
git commit -m "Add migrations for filing_document_url, is_complex, narrative_summary, relevant_reason, structured_summary"
```

---

## Task 4: Extend `insert_filing` and `update_filing_analysis` to persist new fields

**Files:**
- Modify: `database.py` (functions `insert_filing`, `update_filing_analysis`)
- Modify: `tests/test_database_migration.py` (add persistence test)

- [ ] **Step 1: Add failing persistence test**

Append to `tests/test_database_migration.py`:

```python
def test_insert_and_read_new_fields(tmp_sqlite_db):
    """Insert a filing with the new fields set; read it back and verify."""
    import database
    from summary_utils import serialize_subcategories

    filing_data = {
        "accession_no": "0001234567-26-000001",
        "company": "Test Co",
        "ticker": "TEST",
        "cik": "0001234567",
        "filed_date": "2026-04-16",
        "item_codes": "5.02",
        "summary": "Legacy summary string (kept for display fallback).",
        "auto_category": "Both",
        "auto_subcategory": serialize_subcategories(["CFO Departure", "CFO Appointment"]),
        "filing_url": "https://www.sec.gov/Archives/.../index.htm",
        "filing_document_url": "https://www.sec.gov/Archives/.../filing.htm",
        "raw_text": "Full filing text.",
        "matched_keywords": "resigned,appointed",
        "urgent": True,
        "comp_details": None,
        "is_complex": False,
        "narrative_summary": None,
        "relevant_reason": None,
        "structured_summary": '{"departures":[{"name":"J. Smith"}],"appointments":[{"name":"J. Doe"}]}',
    }

    database.insert_filing(filing_data)

    # Read back by accession
    row = database.get_filing_by_accession("0001234567-26-000001")
    assert row is not None
    assert row["filing_document_url"] == "https://www.sec.gov/Archives/.../filing.htm"
    assert row["is_complex"] in (0, False)
    assert row["narrative_summary"] is None
    assert row["structured_summary"].startswith('{"departures"')
    assert row["auto_subcategory"] == '["CFO Departure", "CFO Appointment"]'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_database_migration.py::test_insert_and_read_new_fields -v`
Expected: FAIL — either `insert_filing` doesn't accept the new keys, or `get_filing_by_accession` doesn't return them.

- [ ] **Step 3: Extend `insert_filing` in database.py**

Locate `insert_filing` (around line 385). Update the INSERT column list and VALUES to include the new columns. Both the postgres and sqlite branches need the same change. Pattern:

```python
def insert_filing(filing_data):
    """Insert a filing into the database. Silently skips if accession_no already exists."""
    conn = get_connection()
    cursor = conn.cursor()
    p = "%s" if _using_postgres() else "?"

    try:
        cursor.execute(f"""
            INSERT INTO filings
            (accession_no, company, ticker, cik, filed_date, item_codes,
             summary, auto_category, auto_subcategory, filing_url, raw_text,
             matched_keywords, urgent, comp_details,
             filing_document_url, is_complex, narrative_summary,
             relevant_reason, structured_summary)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
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
            1 if filing_data.get("urgent") else 0,
            _to_str(filing_data.get("comp_details")),
            _to_str(filing_data.get("filing_document_url")),
            1 if filing_data.get("is_complex") else 0,
            _to_str(filing_data.get("narrative_summary")),
            _to_str(filing_data.get("relevant_reason")),
            _to_str(filing_data.get("structured_summary")),
        ))
        conn.commit()
    except Exception as e:
        # Likely duplicate accession_no — that's OK
        print(f"  Skipped (duplicate or error): {filing_data.get('company')} — {e}")
    finally:
        conn.close()
```

Apply the equivalent change to the postgres branch of `insert_filing` if it's separate. (If the function already has a single body that handles both, the above is sufficient.)

- [ ] **Step 4: Extend `update_filing_analysis` in database.py**

Locate `update_filing_analysis` (around line 552). Update its signature and body to accept and persist the new fields:

```python
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
):
    """Re-write the LLM-derived fields for an existing filing (used by resummarize)."""
    conn = get_connection()
    cursor = conn.cursor()
    p = "%s" if _using_postgres() else "?"

    urgent_val = 1 if urgent else 0
    complex_val = 1 if is_complex else 0
    comp_val = comp_details if isinstance(comp_details, str) or comp_details is None else None

    cursor.execute(f"""
        UPDATE filings
        SET summary = {p}, auto_category = {p}, auto_subcategory = {p},
            urgent = {p}, comp_details = {p},
            structured_summary = {p}, is_complex = {p},
            narrative_summary = {p}, relevant_reason = {p}
        WHERE id = {p}
    """, (summary, auto_category, auto_subcategory, urgent_val, comp_val,
          structured_summary, complex_val, narrative_summary, relevant_reason,
          filing_id))

    conn.commit()
    conn.close()
```

- [ ] **Step 5: Ensure `get_filing_by_accession` exists and returns a dict**

Search [database.py](../../../database.py) for a function that fetches a single filing by accession number. If it doesn't exist, add it:

```python
def get_filing_by_accession(accession_no):
    """Fetch a single filing by its accession_no. Returns a dict or None."""
    conn = get_connection()
    cursor = conn.cursor()
    p = "%s" if _using_postgres() else "?"
    cursor.execute(f"SELECT * FROM filings WHERE accession_no = {p}", (accession_no,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return None

    columns = [desc[0] for desc in cursor.description]
    conn.close()
    return dict(zip(columns, row))
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_database_migration.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add database.py tests/test_database_migration.py
git commit -m "Persist new v3 fields (structured_summary, is_complex, etc.) in insert and update paths"
```

---

## Task 5: Fetcher returns primary document URL alongside text

**Files:**
- Modify: `fetcher.py` (function `fetch_filing_text`)
- Modify: `filter.py` (the single caller of `fetch_filing_text`)
- Create: `tests/test_fetcher_doc_url.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fetcher_doc_url.py`:

```python
"""Tests that fetch_filing_text returns both text and the primary document URL."""
from unittest.mock import patch, Mock


def test_fetch_returns_text_and_doc_url():
    """fetch_filing_text should return (text, doc_url) tuple."""
    from fetcher import fetch_filing_text

    index_html = """
        <table class="tableFile">
          <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>
          <tr>
            <td>1</td><td>Form 8-K</td>
            <td><a href="/Archives/edgar/data/123/0001/acme-8k.htm">acme-8k.htm</a></td>
            <td>8-K</td>
          </tr>
        </table>
    """
    filing_body = "<html><body>Full 8-K body text with relevant content.</body></html>"

    mock_index = Mock(status_code=200, text=index_html)
    mock_index.raise_for_status = Mock()
    mock_doc = Mock(status_code=200, text=filing_body)
    mock_doc.raise_for_status = Mock()

    with patch("fetcher.requests.get", side_effect=[mock_index, mock_doc]):
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/123/0001/acme-index.htm",
            "123",
            "0001-23-000001",
        )

    assert "Full 8-K body text" in text
    assert doc_url == "https://www.sec.gov/Archives/edgar/data/123/0001/acme-8k.htm"


def test_fetch_returns_empty_on_failure():
    """When the index page has no 8-K doc link, return (empty_string, None)."""
    from fetcher import fetch_filing_text

    mock_index = Mock(status_code=200, text="<html><body>No table here</body></html>")
    mock_index.raise_for_status = Mock()

    with patch("fetcher.requests.get", return_value=mock_index):
        text, doc_url = fetch_filing_text(
            "https://www.sec.gov/index.htm", "123", "0001-23-000001"
        )

    assert text == ""
    assert doc_url is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetcher_doc_url.py -v`
Expected: FAIL — current `fetch_filing_text` returns just a string, not a tuple.

- [ ] **Step 3: Update `fetch_filing_text` to return a tuple**

Open [fetcher.py](../../../fetcher.py). Locate `fetch_filing_text` (around line 175). Modify it to return `(text, doc_url)`:

1. At every `return ""` inside the function, replace with `return "", None`.
2. At the end of the function, replace `return text` (or whatever the success return is) with `return text, doc_url`.

Expected changes to the tail of `fetch_filing_text`:

```python
        if not doc_url:
            return "", None

        # Now fetch the actual filing document
        time.sleep(REQUEST_DELAY)
        doc_response = requests.get(doc_url, headers=FILING_HEADERS, timeout=30)
        doc_response.raise_for_status()

        # Parse HTML and extract text
        doc_soup = BeautifulSoup(doc_response.text, "html.parser")
        for script in doc_soup(["script", "style"]):
            script.decompose()
        text = doc_soup.get_text(separator=" ", strip=True)

        return text, doc_url

    except Exception as e:
        print(f"  Error fetching filing text: {e}")
        return "", None
```

- [ ] **Step 4: Update filter.py caller to unpack the tuple**

Open [filter.py](../../../filter.py) around line 239. Change:

```python
        text = fetch_text_func(
            filing.get("filing_url", ""),
            filing.get("cik", ""),
            filing.get("accession_no", "")
        )
```

To:

```python
        text, doc_url = fetch_text_func(
            filing.get("filing_url", ""),
            filing.get("cik", ""),
            filing.get("accession_no", "")
        )
        filing["filing_document_url"] = doc_url
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_fetcher_doc_url.py -v`
Expected: Both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add fetcher.py filter.py tests/test_fetcher_doc_url.py
git commit -m "fetch_filing_text returns (text, doc_url); store doc URL on filings"
```

---

## Task 6: Create `prompts/prompt_v3.txt`

**Files:**
- Create: `prompts/prompt_v3.txt`

- [ ] **Step 1: Write the new prompt**

Create `prompts/prompt_v3.txt` with this content:

```text
You are an expert SEC filing analyst helping hedge fund investors quickly triage 8-K filings. Your job is to produce a structured summary that is so complete the user never needs to read the underlying filing.

## YOUR TASK
Read the 8-K filing below. Identify every material event (management change, compensation, or insider transaction). Return a single JSON object with the schema defined below. Your summary must act as a full substitute for reading the filing — not a teaser.

## RELEVANCE
Set "relevant": true for any filing that discloses:
- Executive or director departures, appointments, promotions, or role changes
- Compensation events: grants, severance, accelerated vesting, clawbacks, plan amendments, inducement awards
- Insider transactions: forward sales, equity swaps, collars, pledges of shares, 10b5-1 plan adoption/amendment/termination, material open-market insider sales
- Other material executive-related events: employment agreement amendments, retention bonuses, succession plans

Set "relevant": false only for:
- Routine commercial contracts unrelated to executives
- Real estate leases
- Earnings releases (unless tied to exec comp)
- Reg FD disclosures unrelated to executives
- Director-independence determinations unrelated to a departure

When you set "relevant": false, you MUST provide a one-sentence "relevant_reason" explaining the rejection.

Bias toward inclusion. When in doubt, mark relevant: true, set is_complex: true, and write a narrative_summary.

## CHAIN-OF-THOUGHT
Before filling structured sections, use the "reasoning" field to list every event you identified in the filing. Example: "Identified 3 events: (1) CFO Smith resigned effective 3/15, (2) Jane Doe appointed CFO, (3) sign-on package for Doe." This enumeration reduces dropped events on multi-event filings.

## EVENT ROUTING RULES
| Event type | Put it here |
|------------|-------------|
| Death of an executive | departures[] with stated_reason: "death" |
| Retirement with future effective date | departures[] with effective_date in the future |
| Board member joining or leaving | departures[] / appointments[] with title: "Director" |
| Role change / promotion (no one is leaving the company) | other[] |
| Employment agreement amendment with $$/equity | comp_events[] |
| Employment agreement amendment with no $$ | other[] |
| Severance or accelerated vesting tied to a departure | comp_events[] with executive: "Name (departing role)" |
| Inducement / sign-on award for a new hire | comp_events[] paired with an appointments[] entry |
| Insider forward sale, swap, collar, pledge, 10b5-1 plan, material insider sale | other[] with structured facts and a Signal line |
| Comp plan amendments that affect many executives broadly | other[] AND set is_complex: true with narrative |

## COMPLEXITY ESCAPE HATCH
If the filing is dense, unusual, or has content that doesn't fit cleanly into the structured sections, set "is_complex": true and write a "narrative_summary" that captures everything material that the structured sections miss. The narrative should be 3–6 plain-English bullets. Always include structured data where you can AND the narrative — they complement each other.

## URGENCY FLAG
Set "urgent": true ONLY for:
- Sudden CEO/CFO/CAO departure with no successor named
- Termination of a CEO/CFO/CAO for cause
- Death of a CEO/CFO/CAO
- CEO/CFO/CAO departure effective immediately or within days

Do NOT set urgent true when:
- The departure is due to a merger or change of control (planned transition)
- The departure is a planned retirement announced with lead time

## RESPONSE FORMAT
Return ONLY valid JSON in this exact shape:

{
  "relevant": true,
  "relevant_reason": null,
  "reasoning": "Brief enumeration of events identified in this filing.",

  "top_level_category": "Management Change" | "Compensation" | "Both" | "Other",
  "subcategories": ["CFO Departure", "CFO Appointment"],
  "urgent": false,

  "is_complex": false,
  "narrative_summary": null,

  "departures": [
    {
      "name": "Full Name",
      "title": "Exact title from filing",
      "effective_date": "YYYY-MM-DD or descriptive date string",
      "stated_reason": "resigned | retired | terminated for cause | terminated without cause | death | mutually agreed | not standing for re-election — quote filing language when specific",
      "successor_info": "Who is filling the role, interim or permanent, or 'search underway' / null",
      "signal": "One sentence calling out unusual timing, missing successor, force vs voluntary signal, etc. or null"
    }
  ],

  "appointments": [
    {
      "name": "Full Name",
      "title": "Exact title",
      "effective_date": "YYYY-MM-DD or descriptive",
      "has_comp_details": true
    }
  ],

  "comp_events": [
    {
      "executive": "Full Name (role context)",
      "grant_type": "RSUs | Stock Options | PSUs | Cash Bonus | Severance | Retention Bonus | Accelerated Vesting | etc.",
      "grant_value": "Dollar amount or share count as disclosed",
      "grant_date": "YYYY-MM-DD or null",
      "filing_date": "YYYY-MM-DD (copy from filing metadata)",
      "vesting_schedule": "Plain-English description of vesting or null",
      "performance_hurdles": "Non-stock-price metrics (revenue, EBITDA, TSR, etc.) or null",
      "stock_price_targets": "Option exercise prices or PSU stock-price hurdles (comma-separated) or null"
    }
  ],

  "other": [
    "Plain-English bullet about the event with facts (name, instrument, shares, date, counterparty).",
    "Signal: One-sentence interpretation if the transaction has investor significance."
  ]
}

Empty arrays ([]) mean no events of that type. null is appropriate for fields that don't apply.

## SUBCATEGORY VALUES
Use labels from this list when they apply. Multiple labels per filing are expected:
"CEO Departure", "CFO Departure", "COO Departure", "CAO Departure", "CTO Departure", "CLO Departure", "CHRO Departure", "President Departure", "Board Member Departure", "Executive Departure",
"CEO Appointment", "CFO Appointment", "COO Appointment", "President Appointment", "Board Member Appointment", "Executive Appointment",
"Role Change", "Inducement Award", "Accelerated Vesting", "Comp Plan Change", "Severance / Separation", "Retention Bonus", "Performance Grant",
"Insider Transaction", "Forward Sale", "Equity Swap", "Share Pledge", "10b5-1 Plan", "Insider Sale",
"Employment Agreement Amendment", "Clawback Adoption"

## PRICE-TARGET EXTRACTION (high priority)
When options have exercise prices or PSUs have stock-price hurdles, ALWAYS extract them into stock_price_targets and mention them in any relevant narrative. These reveal board expectations.

## SUMMARY QUALITY RULES
- Never copy boilerplate or form headers from the filing.
- Write in plain English sentences.
- Include names, titles, dates, dollar amounts, and reasons whenever the filing discloses them.
- Be specific. "CFO John Smith resigned" beats "An executive departed."

## FILING TEXT
{filing_text}
```

- [ ] **Step 2: Verify file exists**

Run: `ls prompts/prompt_v3.txt`
Expected: file shown.

- [ ] **Step 3: Commit**

```bash
git add prompts/prompt_v3.txt
git commit -m "Add prompt_v3.txt: event-based structured schema with complexity fallback"
```

---

## Task 7: Update `filter.py` to handle v3 JSON shape

**Files:**
- Modify: `filter.py` (Stage 3 LLM result handling)
- Create: `tests/test_filter_v3.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_filter_v3.py`:

```python
"""Tests that filter.py correctly persists v3 LLM output fields on filings."""
import json
from unittest.mock import patch


def _v3_llm_response(**overrides):
    """Build a realistic v3-shaped LLM response."""
    base = {
        "relevant": True,
        "relevant_reason": None,
        "reasoning": "Identified CFO departure with severance.",
        "top_level_category": "Both",
        "subcategories": ["CFO Departure", "Severance / Separation"],
        "urgent": False,
        "is_complex": False,
        "narrative_summary": None,
        "departures": [{
            "name": "John Smith", "title": "CFO", "effective_date": "2026-04-01",
            "stated_reason": "resigned", "successor_info": "interim CFO named",
            "signal": None,
        }],
        "appointments": [],
        "comp_events": [{
            "executive": "John Smith (departing CFO)",
            "grant_type": "Severance",
            "grant_value": "$2.4M",
            "grant_date": None, "filing_date": "2026-04-02",
            "vesting_schedule": None, "performance_hurdles": None,
            "stock_price_targets": None,
        }],
        "other": [],
        "_tokens_in": 1000, "_tokens_out": 400,
    }
    base.update(overrides)
    return base


def test_filter_maps_v3_fields_onto_filing():
    """A single filing through Stage 3 with v3 output should have all new fields set."""
    from filter import filter_filings

    def fake_fetch(url, cik, accession):
        return "Filing text with CFO resignation details.", "https://sec.gov/filing.htm"

    filings_meta = [{
        "accession_no": "0001-26-000001",
        "company": "Acme Corp", "ticker": "ACME", "cik": "123",
        "filed_date": "2026-04-02", "item_codes": "5.02",
        "filing_url": "https://sec.gov/index.htm",
        "items_list": ["5.02"],
    }]

    with patch("filter.classify_and_summarize", return_value=_v3_llm_response()):
        result = filter_filings(filings_meta, fetch_text_func=fake_fetch)

    assert len(result) == 1
    f = result[0]
    assert f["auto_category"] == "Both"
    # Subcategory is serialized as a JSON array string
    assert json.loads(f["auto_subcategory"]) == ["CFO Departure", "Severance / Separation"]
    assert f["is_complex"] == 0 or f["is_complex"] is False
    assert f["narrative_summary"] is None
    assert f["relevant_reason"] is None
    # structured_summary blob contains the event arrays
    structured = json.loads(f["structured_summary"])
    assert structured["departures"][0]["name"] == "John Smith"
    assert structured["comp_events"][0]["grant_value"] == "$2.4M"
    # filing_document_url was captured from fetch
    assert f["filing_document_url"] == "https://sec.gov/filing.htm"


def test_filter_persists_narrative_when_complex():
    """is_complex: true with narrative_summary should be stored."""
    from filter import filter_filings

    def fake_fetch(url, cik, accession):
        return "Complex filing text.", "https://sec.gov/filing.htm"

    filings_meta = [{
        "accession_no": "0001-26-000002",
        "company": "Mega Pharma", "ticker": "MPHI", "cik": "456",
        "filed_date": "2026-04-15", "item_codes": "5.02",
        "filing_url": "https://sec.gov/index.htm",
        "items_list": ["5.02"],
    }]

    complex_response = _v3_llm_response(
        is_complex=True,
        narrative_summary="Buyback + clawback + CEO transition all in one filing.",
    )

    with patch("filter.classify_and_summarize", return_value=complex_response):
        result = filter_filings(filings_meta, fetch_text_func=fake_fetch)

    assert result[0]["is_complex"] in (1, True)
    assert "Buyback" in result[0]["narrative_summary"]


def test_filter_records_relevant_reason_when_rejected():
    """When LLM returns relevant:false, filing is dropped from results but
    the rejection reason is logged (we rely on stdout/log output for now)."""
    from filter import filter_filings

    def fake_fetch(url, cik, accession):
        return "Irrelevant earnings release text.", "https://sec.gov/filing.htm"

    filings_meta = [{
        "accession_no": "0001-26-000003",
        "company": "Boring Co", "ticker": "BORE", "cik": "789",
        "filed_date": "2026-04-10", "item_codes": "8.01",
        "filing_url": "https://sec.gov/index.htm",
        "items_list": ["8.01"],
    }]

    rejected = _v3_llm_response(
        relevant=False,
        relevant_reason="Earnings release with no executive or comp content.",
    )
    # Clear optional fields on rejection
    rejected.update({"departures": [], "appointments": [], "comp_events": [], "other": []})

    with patch("filter.classify_and_summarize", return_value=rejected):
        result = filter_filings(filings_meta, fetch_text_func=fake_fetch)

    # Rejected filings are filtered out (existing behavior preserved)
    assert len(result) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_filter_v3.py -v`
Expected: FAIL — `filter.py` doesn't map v3 fields onto filings yet.

- [ ] **Step 3: Update the Stage 3 result-handling block in filter.py**

Open [filter.py](../../../filter.py). Replace the existing Stage 3 result-handling block (roughly lines 303–332) with this version:

```python
        if llm_result is not None:
            is_relevant = llm_result.get("relevant", False)

            if is_relevant:
                # --- v3 fields ---
                filing["auto_category"] = (
                    llm_result.get("top_level_category")
                    or llm_result.get("category")
                    or filing.get("auto_category")
                )

                # Subcategories: prefer v3 array, fall back to legacy single string
                from summary_utils import serialize_subcategories
                subcats = llm_result.get("subcategories")
                if subcats is None:
                    legacy = llm_result.get("subcategory")
                    subcats = [legacy] if legacy else []
                filing["auto_subcategory"] = serialize_subcategories(subcats)

                filing["urgent"] = bool(llm_result.get("urgent", False))
                filing["is_complex"] = bool(llm_result.get("is_complex", False))
                filing["narrative_summary"] = llm_result.get("narrative_summary")
                filing["relevant_reason"] = None  # only set on rejection path

                # Build structured_summary blob from the event arrays
                structured = {
                    "reasoning": llm_result.get("reasoning"),
                    "departures": llm_result.get("departures", []),
                    "appointments": llm_result.get("appointments", []),
                    "comp_events": llm_result.get("comp_events", []),
                    "other": llm_result.get("other", []),
                }
                filing["structured_summary"] = json.dumps(structured)

                # Legacy "summary" field stays populated for older templates/emails.
                # Use narrative if present, else a brief assembly from the first event.
                filing["summary"] = _build_legacy_summary(llm_result)

                # Legacy comp_details stays supported for backward compat
                comp_details = llm_result.get("comp_details")
                if comp_details and any(v for v in comp_details.values()):
                    filing["comp_details"] = json.dumps(comp_details)
                else:
                    filing["comp_details"] = None

                final_passed.append(filing)
                tokens = llm_result.get("_tokens_in", 0) + llm_result.get("_tokens_out", 0)
                cats_display = subcats[0] if subcats else "—"
                print(f"    LLM: RELEVANT — {filing['auto_category']} / {cats_display} ({tokens} tokens)")
            else:
                reason = llm_result.get("relevant_reason") or "(no reason given)"
                print(f"    LLM: NOT RELEVANT — {reason}")
        else:
            # LLM failed — fall back to keyword classification + sentence-scorer summary
            print(f"    LLM FAILED — falling back to keyword classification")
            filing["summary"] = extract_summary(text, filing.get("matched_keywords", "").split(","))
            final_passed.append(filing)
```

Add the legacy-summary helper at module level in `filter.py`:

```python
def _build_legacy_summary(llm_result):
    """Build a short display summary for older templates/emails from v3 output."""
    narrative = llm_result.get("narrative_summary")
    if narrative:
        return narrative

    parts = []
    for d in llm_result.get("departures", [])[:2]:
        parts.append(f"{d.get('name')} ({d.get('title')}) — {d.get('stated_reason') or 'departure'}")
    for a in llm_result.get("appointments", [])[:2]:
        parts.append(f"{a.get('name')} appointed {a.get('title')}")
    for c in llm_result.get("comp_events", [])[:1]:
        parts.append(f"Comp: {c.get('executive')} — {c.get('grant_type')} {c.get('grant_value') or ''}")
    return "; ".join(parts) if parts else (llm_result.get("summary") or "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_filter_v3.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Point the active prompt at v3**

Open [config.py](../../../config.py) and change:

```python
ACTIVE_PROMPT = "prompt_v2.txt"
```

to:

```python
ACTIVE_PROMPT = "prompt_v3.txt"
```

- [ ] **Step 6: Commit**

```bash
git add filter.py config.py tests/test_filter_v3.py
git commit -m "Handle v3 LLM output in filter.py; activate prompt_v3 as default"
```

---

## Task 8: Render helper and Jinja partial for structured summary

**Files:**
- Modify: `summary_utils.py` (add `structured_summary_for_display`)
- Create: `templates/_structured_summary.html`
- Modify: `app.py` (register the template as a Jinja include path, no change if templates is already the default)

- [ ] **Step 1: Add display helper with test**

Append to `tests/test_summary_utils.py`:

```python
import json as _json
from summary_utils import structured_summary_for_display


class TestStructuredSummaryForDisplay:
    def test_parses_json_blob(self):
        raw = _json.dumps({
            "departures": [{"name": "J. Smith", "title": "CFO"}],
            "appointments": [], "comp_events": [], "other": [],
            "reasoning": "one event",
        })
        result = structured_summary_for_display(raw)
        assert result["departures"][0]["name"] == "J. Smith"
        assert result["appointments"] == []
        assert result["has_any_event"] is True

    def test_handles_none(self):
        result = structured_summary_for_display(None)
        assert result["departures"] == []
        assert result["has_any_event"] is False

    def test_handles_malformed_json(self):
        result = structured_summary_for_display("{broken")
        assert result["has_any_event"] is False
```

Append to `summary_utils.py`:

```python
def structured_summary_for_display(raw):
    """Parse the structured_summary JSON column into a dict safe for templates.

    Always returns a dict with the four event arrays, a reasoning field,
    and a has_any_event convenience flag. Never raises on malformed input.
    """
    empty = {
        "departures": [], "appointments": [], "comp_events": [], "other": [],
        "reasoning": None, "has_any_event": False,
    }
    if not raw:
        return empty
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return empty
    except (json.JSONDecodeError, ValueError):
        return empty

    out = {
        "departures": parsed.get("departures") or [],
        "appointments": parsed.get("appointments") or [],
        "comp_events": parsed.get("comp_events") or [],
        "other": parsed.get("other") or [],
        "reasoning": parsed.get("reasoning"),
    }
    out["has_any_event"] = any([
        out["departures"], out["appointments"], out["comp_events"], out["other"],
    ])
    return out
```

- [ ] **Step 2: Verify tests pass**

Run: `pytest tests/test_summary_utils.py -v`
Expected: 13 tests PASS.

- [ ] **Step 3: Create the Jinja partial**

Create `templates/_structured_summary.html`:

```html
{# Renders a structured summary for a filing. Expects two context vars:
     - structured: dict returned by structured_summary_for_display()
     - filing: the filing row (for narrative_summary, is_complex, comp_details badges)
#}
<div class="structured-summary">
  {% if filing['is_complex'] %}
    <span class="badge bg-purple" style="background:#6f42c1;color:#fff;margin-bottom:6px;display:inline-block;">Complex</span>
  {% endif %}

  {% if structured.departures %}
    <div class="ss-section ss-dep" style="margin-top:6px;">
      <div class="ss-label" style="font-size:10px;font-weight:700;text-transform:uppercase;color:#dc3545;">Departure</div>
      <ul class="ss-body" style="padding-left:14px;margin:2px 0 6px 0;">
        {% for d in structured.departures %}
          <li><b>{{ d.name }}</b>{% if d.title %}, {{ d.title }}{% endif %}
            {% if d.effective_date %}<br><span class="text-muted small">Effective: {{ d.effective_date }}</span>{% endif %}
            {% if d.stated_reason %}<br><span class="text-muted small">Reason: {{ d.stated_reason }}</span>{% endif %}
            {% if d.successor_info %}<br><span class="text-muted small">Successor: {{ d.successor_info }}</span>{% endif %}
            {% if d.signal %}<br><em class="small" style="color:#b45309;">Signal: {{ d.signal }}</em>{% endif %}
          </li>
        {% endfor %}
      </ul>
    </div>
  {% endif %}

  {% if structured.appointments %}
    <div class="ss-section ss-app">
      <div class="ss-label" style="font-size:10px;font-weight:700;text-transform:uppercase;color:#0d6efd;">Appointment</div>
      <ul class="ss-body" style="padding-left:14px;margin:2px 0 6px 0;">
        {% for a in structured.appointments %}
          <li><b>{{ a.name }}</b>{% if a.title %}, {{ a.title }}{% endif %}
            {% if a.effective_date %} — effective {{ a.effective_date }}{% endif %}
            {% if a.has_comp_details %} <span class="small text-muted">(see Comp below)</span>{% endif %}
          </li>
        {% endfor %}
      </ul>
    </div>
  {% endif %}

  {% if structured.comp_events %}
    <div class="ss-section ss-comp">
      <div class="ss-label" style="font-size:10px;font-weight:700;text-transform:uppercase;color:#198754;">Compensation</div>
      <ul class="ss-body" style="padding-left:14px;margin:2px 0 6px 0;">
        {% for c in structured.comp_events %}
          <li><b>{{ c.executive }}</b>
            {% if c.grant_type %}<br><span class="text-muted small">Type: {{ c.grant_type }}</span>{% endif %}
            {% if c.grant_value %}<br><span class="text-muted small">Value: {{ c.grant_value }}</span>{% endif %}
            {% if c.vesting_schedule %}<br><span class="text-muted small">Vesting: {{ c.vesting_schedule }}</span>{% endif %}
            {% if c.performance_hurdles %}<br><span class="text-muted small">Hurdles: {{ c.performance_hurdles }}</span>{% endif %}
            {% if c.stock_price_targets %}<br><span class="text-muted small">Price targets: <b>{{ c.stock_price_targets }}</b></span>{% endif %}
            {% if c.grant_date and c.grant_date != c.filing_date %}<br><span class="text-muted small">Granted: {{ c.grant_date }}</span>{% endif %}
          </li>
        {% endfor %}
      </ul>
    </div>
  {% endif %}

  {% if structured.other %}
    <div class="ss-section ss-other">
      <div class="ss-label" style="font-size:10px;font-weight:700;text-transform:uppercase;color:#6c757d;">Other</div>
      <ul class="ss-body" style="padding-left:14px;margin:2px 0 6px 0;">
        {% for o in structured.other %}
          <li>{{ o }}</li>
        {% endfor %}
      </ul>
    </div>
  {% endif %}

  {% if filing['narrative_summary'] %}
    <div class="ss-section ss-narrative" style="background:linear-gradient(to right,#f8f3ff,#fff);border-left:3px solid #6f42c1;padding:6px 10px;margin-top:6px;border-radius:4px;">
      <div class="ss-label" style="font-size:10px;font-weight:700;text-transform:uppercase;color:#6f42c1;">Narrative</div>
      <div class="small">{{ filing['narrative_summary'] }}</div>
    </div>
  {% endif %}

  {% if not structured.has_any_event and not filing['narrative_summary'] %}
    {# Fallback for legacy rows that only have the old `summary` field. #}
    <div class="small">{{ filing['summary'] }}</div>
  {% endif %}
</div>
```

- [ ] **Step 4: Verify Flask sees the partial**

No code change needed — Flask auto-discovers templates in the `templates/` folder.

- [ ] **Step 5: Commit**

```bash
git add summary_utils.py tests/test_summary_utils.py templates/_structured_summary.html
git commit -m "Add structured_summary_for_display helper and Jinja partial"
```

---

## Task 9: Wire the structured summary + subcategory array + Open filing button into the dashboard

**Files:**
- Modify: `app.py` (add Jinja helper; enrich filings passed to index template)
- Modify: `templates/index.html`

- [ ] **Step 1: Add Jinja helper filter in app.py**

Open [app.py](../../../app.py). Near the top (after the Flask app is created), add:

```python
from summary_utils import (
    parse_subcategories,
    structured_summary_for_display,
)

@app.template_filter("parse_subcategories")
def _jinja_parse_subcategories(raw):
    return parse_subcategories(raw)

@app.template_filter("structured_summary")
def _jinja_structured_summary(raw):
    return structured_summary_for_display(raw)
```

- [ ] **Step 2: Replace the index.html Summary cell**

Open [templates/index.html](../../../templates/index.html). Find the `<thead>` and `<tbody>` region (around lines 72–146).

Replace the `<thead>` with:

```html
<thead class="table-dark">
    <tr>
        <th style="width: 40px;"></th>
        <th></th>
        <th>Date</th>
        <th>Company</th>
        <th>Ticker</th>
        <th>Category</th>
        <th>Summary</th>
        <th>Link</th>
    </tr>
</thead>
```

(Removes the separate `Sub-Category` column — subcategory pills now live inline with the category badge.)

In the `<tbody>` row, replace the Category cell (lines ~117–127), the Sub-Category cell (line ~128), and the Summary cell (lines ~129–138) with:

```html
<td>
    {% set display_category = filing['user_tag'] or filing['auto_category'] %}
    <span class="badge
        {% if display_category == 'Management Change' %}bg-primary
        {% elif display_category == 'Compensation' %}bg-success
        {% elif display_category == 'Both' %}bg-warning text-dark
        {% else %}bg-info
        {% endif %}">
        {{ display_category or 'Uncategorized' }}
    </span>
    {% set subcats = filing['auto_subcategory'] | parse_subcategories %}
    {% if subcats %}
      <br>
      {% for s in subcats %}
        <span class="badge bg-light text-dark border" style="font-size:10px;margin-top:2px;">{{ s }}</span>
      {% endfor %}
    {% endif %}
</td>
<td class="summary-cell" style="min-width: 320px; white-space: normal;">
    {% set structured = filing['structured_summary'] | structured_summary %}
    {% include '_structured_summary.html' %}
</td>
```

Replace the Link cell (lines ~139–142) with:

```html
<td>
    {% set doc_url = filing['filing_document_url'] or filing['filing_url'] %}
    <a href="{{ doc_url }}" target="_blank" class="btn btn-sm btn-outline-primary"
       onclick="event.stopPropagation()" title="Open 8-K document">
       Open filing
    </a>
</td>
```

- [ ] **Step 3: Run the Flask dev server and check manually**

Run: `flask run` (or the existing start command from [app.py](../../../app.py))
Expected: the app starts; opening `http://localhost:5000/` shows filings with the new structured summary cells and "Open filing" buttons. Legacy rows (no `structured_summary`) gracefully fall back to the old `summary` text.

- [ ] **Step 4: Commit**

```bash
git add app.py templates/index.html
git commit -m "Dashboard: render structured summary, subcategory array badges, one-click Open filing"
```

---

## Task 10: Update filing detail view with structured summary + debug info

**Files:**
- Modify: `templates/filing.html`

- [ ] **Step 1: Update filing.html to render the structured summary**

Open [templates/filing.html](../../../templates/filing.html). Locate the Auto Sub-Category row (around line 55):

```html
<tr><th>Auto Sub-Category</th><td>{{ filing['auto_subcategory'] or '—' }}</td></tr>
```

Replace with:

```html
<tr><th>Auto Sub-Categories</th><td>
    {% set subcats = filing['auto_subcategory'] | parse_subcategories %}
    {% if subcats %}
      {% for s in subcats %}
        <span class="badge bg-light text-dark border" style="margin-right:3px;">{{ s }}</span>
      {% endfor %}
    {% else %}—{% endif %}
</td></tr>
```

- [ ] **Step 2: Add a structured-summary block and a debug details section**

Find the "Filing metadata" card's closing `</div>` (after the two-column metadata tables) and insert **before the Tag Override form** (around line 71):

```html
<!-- Structured summary -->
<div class="card mb-4">
    <div class="card-body">
        <h5>Summary</h5>
        {% set structured = filing['structured_summary'] | structured_summary %}
        {% include '_structured_summary.html' %}

        {% if filing['relevant_reason'] %}
        <div class="alert alert-warning mt-3 small">
            <b>LLM marked this filing as not relevant:</b> {{ filing['relevant_reason'] }}
        </div>
        {% endif %}

        {% if structured.reasoning %}
        <details class="mt-3 small text-muted">
            <summary>LLM reasoning (debug)</summary>
            <div style="margin-top:6px;">{{ structured.reasoning }}</div>
        </details>
        {% endif %}
    </div>
</div>
```

- [ ] **Step 3: Update the "SEC Filing" link row to add a primary button**

Find the `<tr>` with `<th>SEC Filing</th>` (around line 35–38) and replace it with:

```html
<tr>
    <th>Filing link</th>
    <td>
        {% if filing['filing_document_url'] %}
        <a href="{{ filing['filing_document_url'] }}" target="_blank" class="btn btn-sm btn-primary">Open filing</a>
        {% endif %}
        <a href="{{ filing['filing_url'] }}" target="_blank" class="btn btn-sm btn-outline-secondary">SEC index page</a>
    </td>
</tr>
```

- [ ] **Step 4: Manual smoke test**

Run: `flask run`
Expected: opening a filing detail page shows the structured summary card, the Auto Sub-Categories pills, the new "Open filing" button, and the collapsed debug "LLM reasoning" section.

- [ ] **Step 5: Commit**

```bash
git add templates/filing.html
git commit -m "Filing detail: structured summary card, subcategory pills, Open filing button, debug details"
```

---

## Task 11: Re-analysis command for recent filings

**Files:**
- Modify: `app.py` (locate `run_resummarize` — wire new fields through)

- [ ] **Step 1: Locate `run_resummarize`**

Search [app.py](../../../app.py) for `def run_resummarize`. It currently loads filings, calls `classify_and_summarize`, and calls `update_filing_analysis`.

- [ ] **Step 2: Update `run_resummarize` to pass v3 fields through**

Find the call to `update_filing_analysis` inside `run_resummarize` (around line 778) and replace the surrounding block with:

```python
            # v3-shaped LLM result — map to update call
            from summary_utils import serialize_subcategories

            category = llm_result.get("top_level_category") or llm_result.get("category") or filing.get("auto_category")
            subcats = llm_result.get("subcategories")
            if subcats is None:
                legacy = llm_result.get("subcategory")
                subcats = [legacy] if legacy else []
            auto_subcategory = serialize_subcategories(subcats)

            urgent = bool(llm_result.get("urgent", False))
            is_complex = bool(llm_result.get("is_complex", False))
            narrative = llm_result.get("narrative_summary")

            structured = {
                "reasoning": llm_result.get("reasoning"),
                "departures": llm_result.get("departures", []),
                "appointments": llm_result.get("appointments", []),
                "comp_events": llm_result.get("comp_events", []),
                "other": llm_result.get("other", []),
            }
            structured_json = json.dumps(structured)

            comp_details_blob = llm_result.get("comp_details")
            comp_json = json.dumps(comp_details_blob) if comp_details_blob else None

            # Derive a legacy one-liner summary for backward compat
            from filter import _build_legacy_summary
            summary = _build_legacy_summary(llm_result)

            update_filing_analysis(
                filing_id,
                summary,
                category,
                auto_subcategory,
                urgent,
                comp_json,
                structured_summary=structured_json,
                is_complex=is_complex,
                narrative_summary=narrative,
                relevant_reason=None if llm_result.get("relevant") else llm_result.get("relevant_reason"),
            )
```

- [ ] **Step 3: Add a since-days helper at the top of `run_resummarize`**

Confirm that `run_resummarize` accepts a `since_days` parameter. If not, add it to the signature and filter the queried filings by `filed_date >= today - since_days`.

- [ ] **Step 4: Manual one-off re-analysis**

Run, from the project root:

```bash
python -c "from app import run_resummarize; run_resummarize(prompt_version='v3', since_days=60)"
```

Expected: console output showing each filing being re-analyzed, tokens used, and fields updated. At the end: a summary of how many rows were updated.

**NOTE:** This makes real OpenAI API calls. Estimated cost $5–20 depending on volume. Stop at any time with Ctrl+C.

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "run_resummarize: persist v3 structured_summary and complexity fields"
```

---

## Task 12: Final smoke test checklist (manual)

**Files:** None — manual verification

- [ ] **Run the full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS.

- [ ] **Local dev server checks**

Run: `flask run`

Open `http://localhost:5000/` and verify:
- [ ] Dashboard loads without 500 errors.
- [ ] Summary cell renders structured sections (Departure / Appointment / Comp / Other) with color-coded labels.
- [ ] Subcategory pills appear inline with the category badge — multiple pills when multiple events.
- [ ] "Complex" purple badge appears on the few filings that got `is_complex: true`.
- [ ] "Open filing" button in the last column opens the actual 8-K document in a new tab (NOT the index page).
- [ ] Clicking into a filing shows the structured summary card, pills, debug reasoning section, and "Open filing" + "SEC index page" buttons.
- [ ] Filings analyzed on v2 (not yet re-analyzed) still render correctly via legacy summary fallback.

- [ ] **Branch review**

Run: `git log feature/ai-summary-overhaul ^main --oneline`
Expected: a clean list of ~11 commits, one per task, in order.

- [ ] **Next step — decide to merge or iterate**

When you're satisfied, either:
- Merge to main: `git checkout main && git merge --no-ff feature/ai-summary-overhaul && git push origin main` (Render auto-deploys on push)
- Or open a PR on GitHub for a review-then-merge flow: `gh pr create --title "AI summary & subcategory overhaul" --body "See docs/superpowers/specs/2026-04-16-ai-summary-overhaul-design.md"`

---

## Self-Review

**Spec coverage:**
- Section 1 (JSON schema) → Task 6 (prompt) + Task 7 (filter handling) + Task 8 (display helper)
- Section 2 (prompt changes) → Task 6 (prompt_v3.txt) + Task 7 Step 5 (ACTIVE_PROMPT flip)
- Section 3 (subcategory array) → Task 2 (helpers) + Task 4 (storage) + Task 9 (display)
- Section 4 (dashboard display) → Task 8 (partial) + Task 9 (index) + Task 10 (detail)
- Section 5 (SEC link) → Task 3 (column) + Task 5 (fetcher) + Task 9/10 (Open filing button)
- Section 6 (migration strategy) → Task 11 (run_resummarize)
- Safety architecture (reasoning, is_complex, relevant_reason, narrative) → Task 3 (columns) + Task 6 (prompt rules) + Task 7 (persistence) + Task 8/9/10 (display)
- Testing strategy → Tasks 1–7, 11 (automated) + Task 12 (manual smoke)
- Rollback → feature branch + additive columns (spec Section 6)

**Placeholder scan:** No TBD / TODO / "similar to" / "add appropriate error handling" found. Each code block is complete.

**Type consistency:**
- `serialize_subcategories` returns `Optional[str]` — used identically in Tasks 4, 7, 11.
- `fetch_filing_text` now returns `tuple[str, Optional[str]]` — all callers updated in Task 5.
- `update_filing_analysis` keyword args (`structured_summary`, `is_complex`, `narrative_summary`, `relevant_reason`) match across Tasks 4 and 11.
- `structured_summary_for_display` always returns a dict with identical keys — Jinja partial references them safely.

All good.
