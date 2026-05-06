# Stock Price + Executive Departures (24mo) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add (1) current stock price to each row of the filings dashboard, and (2) a new "Executive Departures (24mo)" option in the signal-analyzer dropdown that lists all 5.02 filings for the company over the last 24 months as short prose with name + role + reason, using LLM extraction cached per accession number.

**Architecture:**
- Reuse [stock_price.py](../../../stock_price.py) (already has cache + API call) — just add a batch map.
- Reuse [fetcher.py:410 `get_edgar_departure_history`](../../../fetcher.py#L410) which already queries EDGAR submissions and extracts 5.02 snippets.
- New `departures.py` orchestrates: snippet → LLM → JSON → cache → prose renderer.
- Dispatch on the dropdown value in the existing `/deep-analysis/<id>` route — no new route, no JS changes.

**Tech Stack:** Python 3.11 + Flask + Jinja2, SQLite (local) / PostgreSQL (Render), OpenAI client (already wired in [llm.py](../../../llm.py)), `concurrent.futures` (stdlib).

**Spec:** [docs/superpowers/specs/2026-05-05-stock-price-and-departures-design.md](../specs/2026-05-05-stock-price-and-departures-design.md)

---

## File Structure

**Create:**
- `departures.py` — orchestration (cache lookup, parallel LLM extraction, prose rendering)
- `prompts/prompt_departures.txt` — LLM extraction prompt
- `tests/test_stock_price_map.py` — unit test for `get_stock_price_map`
- `tests/test_departures.py` — unit tests for cache helpers, prose renderer, and pipeline (mocked LLM)

**Modify:**
- `stock_price.py` — add `get_stock_price_map(tickers)` function
- `database.py` — add `departure_extractions` table and helper functions
- `llm.py` — add `extract_departures(snippet, filed_date)` function
- `app.py` — call `get_stock_price_map` from `/` handler; dispatch departures path from `/deep-analysis/<id>`; pass `departures` context to filing detail
- `templates/index.html` — render stock price under ticker
- `templates/filing.html` — add `departures_24mo` dropdown option; new card for departures result
- `static/style.css` (or wherever existing styles live) — add `.stock-price` muted style

---

# PART A — Stock Price on Dashboard

### Task 1: Batch stock-price helper with test

**Files:**
- Modify: `stock_price.py`
- Create: `tests/test_stock_price_map.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stock_price_map.py`:

```python
"""Tests for get_stock_price_map — the dashboard's batch stock-price helper."""
from unittest.mock import patch


def test_returns_dict_mapping_ticker_to_price(tmp_sqlite_db):
    """get_stock_price_map should return {TICKER: price} for each input."""
    from stock_price import get_stock_price_map

    # Mock the per-ticker fetch to avoid hitting the real API
    with patch("stock_price.fetch_from_api_ninjas") as mock_fetch:
        mock_fetch.side_effect = lambda t: {"AAPL": 200.50, "MSFT": 410.10}.get(t)
        result = get_stock_price_map(["AAPL", "MSFT"])

    assert result == {"AAPL": 200.50, "MSFT": 410.10}


def test_omits_tickers_with_no_price(tmp_sqlite_db):
    """If a ticker fetch returns None, it should be omitted from the result map."""
    from stock_price import get_stock_price_map

    with patch("stock_price.fetch_from_api_ninjas") as mock_fetch:
        mock_fetch.side_effect = lambda t: 99.0 if t == "AAPL" else None
        result = get_stock_price_map(["AAPL", "BADX"])

    assert result == {"AAPL": 99.0}
    assert "BADX" not in result


def test_empty_input_returns_empty_dict(tmp_sqlite_db):
    from stock_price import get_stock_price_map
    assert get_stock_price_map([]) == {}
    assert get_stock_price_map(None) == {}


def test_individual_failure_does_not_break_batch(tmp_sqlite_db):
    """If one ticker raises, the others still come through."""
    from stock_price import get_stock_price_map

    def flaky(t):
        if t == "BOOM":
            raise RuntimeError("simulated network fail")
        return 50.0

    with patch("stock_price.fetch_from_api_ninjas", side_effect=flaky):
        result = get_stock_price_map(["AAPL", "BOOM", "GOOG"])

    assert result == {"AAPL": 50.0, "GOOG": 50.0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_stock_price_map.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_stock_price_map' from 'stock_price'`

- [ ] **Step 3: Add `get_stock_price_map` to `stock_price.py`**

Append to the end of `stock_price.py`:

```python
def get_stock_price_map(tickers):
    """Return {ticker: price} for every ticker that successfully resolved.

    Uses the existing get_stock_price() (which caches in the stock_prices table
    with a 1-hour TTL). Per-ticker failures are swallowed so the dashboard
    never breaks on a flaky API call. Tickers with no price are omitted.

    Args:
        tickers: iterable of ticker strings (case-insensitive)

    Returns:
        dict mapping uppercase ticker -> float price
    """
    if not tickers:
        return {}

    result = {}
    for raw in tickers:
        if not raw:
            continue
        ticker = raw.strip().upper()
        try:
            price = get_stock_price(ticker)
            if price:
                result[ticker] = price
        except Exception as e:
            # Don't let a single ticker failure break the dashboard
            print(f"[STOCK PRICE MAP] Skipping {ticker}: {e}")

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_stock_price_map.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add stock_price.py tests/test_stock_price_map.py
git commit -m "Add get_stock_price_map for batch dashboard rendering"
```

---

### Task 2: Wire stock prices into the dashboard

**Files:**
- Modify: `app.py:232-277`
- Modify: `templates/index.html:107-114`

- [ ] **Step 1: Pass `stock_prices` from the index handler**

Open `app.py`. Find the block at lines 242-248 that fetches earnings:

```python
    # Fetch next earnings dates for tickers on this page
    earnings = {}
    try:
        from earnings import get_earnings_map
        earnings = get_earnings_map(unique_tickers)
    except Exception as e:
        print(f"[EARNINGS] Failed to load earnings: {e}")
```

Immediately after the `except` block (before the line `# Count filings matching current filters...`), add:

```python
    # Fetch current stock prices for tickers on this page
    stock_prices = {}
    try:
        from stock_price import get_stock_price_map
        stock_prices = get_stock_price_map(unique_tickers)
    except Exception as e:
        print(f"[STOCK PRICE] Failed to load stock prices: {e}")
```

Then, in the `render_template("index.html", ...)` call at lines 260-277, add `stock_prices=stock_prices,` as the last keyword argument before the closing paren:

```python
    return render_template(
        "index.html",
        # ... existing kwargs ...
        market_caps=market_caps,
        earnings=earnings,
        stock_prices=stock_prices,
    )
```

- [ ] **Step 2: Render the stock price in the template**

Open `templates/index.html`. Find the ticker cell (lines 106-114):

```jinja
                <td>
                    <strong>{{ filing['ticker'] or '—' }}</strong>
                    {% if filing['ticker'] and market_caps.get(filing['ticker']) %}
                    <br><span class="market-cap">{{ market_caps[filing['ticker']] | format_market_cap }}</span>
                    {% endif %}
                    {% if filing['ticker'] and earnings.get(filing['ticker']) and earnings[filing['ticker']].get('date') %}
                    <br><span class="earnings-date">Earnings: {{ earnings[filing['ticker']] | format_earnings_date }}</span>
                    {% endif %}
                </td>
```

After the earnings-date `{% endif %}` and BEFORE the closing `</td>`, add:

```jinja
                    {% if filing['ticker'] and stock_prices.get(filing['ticker']) %}
                    <br><span class="stock-price">${{ "%.2f"|format(stock_prices[filing['ticker']]) }}</span>
                    {% endif %}
```

- [ ] **Step 3: Add CSS for `.stock-price`**

Find the file with `.market-cap` styling. Run:

```bash
grep -rn "market-cap" templates static 2>/dev/null
```

In whichever file holds `.market-cap` (likely `static/style.css` or inline in `templates/base.html`), find the rule and add a sibling rule right after it:

```css
.stock-price {
    font-size: 0.8em;
    color: #198754;  /* Bootstrap success green — visual cue this is a live number */
    font-weight: 500;
}
```

If `.market-cap` is not in a CSS file (some projects inline styles), add the same rule in the `<style>` block of `templates/base.html`.

- [ ] **Step 4: Manual smoke test**

Start the app:

```bash
python app.py
```

Open http://localhost:5000/ in a browser. Confirm:
- Each row's ticker cell now shows a small green "$XXX.XX" line under the ticker (or under the earnings date if both are present).
- Rows with no ticker, or tickers where the API has no price, show no extra line (no "$—" placeholder).
- Page loads without 500 errors.

If there are no filings with tickers visible, run a small backfill or visit `/?search=` with a known-ticker company.

- [ ] **Step 5: Commit**

```bash
git add app.py templates/index.html static/style.css templates/base.html
git commit -m "Show current stock price under ticker on dashboard rows"
```

(If `static/style.css` or `templates/base.html` weren't modified, drop them from the `git add` — staging non-existent paths is harmless but the commit message stays the same.)

---

# PART B — Executive Departures (24mo)

### Task 3: Database schema + helpers (with tests)

**Files:**
- Modify: `database.py`
- Create: `tests/test_departure_extractions_db.py`

- [ ] **Step 1: Write failing tests for the DB helpers**

Create `tests/test_departure_extractions_db.py`:

```python
"""Tests for the departure_extractions cache table."""
import json
import sqlite3


def test_table_exists_after_init(tmp_sqlite_db):
    """initialize_database must create the departure_extractions table."""
    conn = sqlite3.connect(tmp_sqlite_db)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='departure_extractions'")
    row = cursor.fetchone()
    conn.close()
    assert row is not None, "departure_extractions table was not created"


def test_get_returns_none_when_missing(tmp_sqlite_db):
    from database import get_cached_departure_extraction
    assert get_cached_departure_extraction("0001234-25-000999") is None


def test_upsert_then_get_roundtrip(tmp_sqlite_db):
    from database import upsert_departure_extraction, get_cached_departure_extraction

    extractions = [
        {"date": "2025-09-12", "person": "Jane Doe", "position": "CFO", "reason": "Resigned to pursue other opportunities"},
    ]
    upsert_departure_extraction(
        accession_number="0001234-25-000123",
        cik="0001234567",
        filed_date="2025-09-12",
        extractions=extractions,
        has_error=False,
    )

    cached = get_cached_departure_extraction("0001234-25-000123")
    assert cached is not None
    assert cached["cik"] == "0001234567"
    assert cached["filed_date"] == "2025-09-12"
    assert cached["has_error"] == 0  # SQLite stores bool as int
    assert cached["extractions"] == extractions  # JSON parsed back


def test_upsert_overwrites_existing_row(tmp_sqlite_db):
    """Calling upsert twice with the same accession should replace, not duplicate."""
    from database import upsert_departure_extraction, get_cached_departure_extraction

    upsert_departure_extraction("0001234-25-000123", "0001234567", "2025-09-12", [], has_error=True)
    upsert_departure_extraction(
        "0001234-25-000123", "0001234567", "2025-09-12",
        [{"date": "2025-09-12", "person": "Jane", "position": "CFO", "reason": "Retired"}],
        has_error=False,
    )

    cached = get_cached_departure_extraction("0001234-25-000123")
    assert cached["has_error"] == 0
    assert len(cached["extractions"]) == 1


def test_returns_real_dict_supports_get(tmp_sqlite_db):
    """Per CLAUDE.md: cached row must be a real dict (.get works), not sqlite3.Row."""
    from database import upsert_departure_extraction, get_cached_departure_extraction
    upsert_departure_extraction("0001234-25-000123", "0001234567", "2025-09-12", [], False)
    cached = get_cached_departure_extraction("0001234-25-000123")
    # .get() works on dicts but not on sqlite3.Row
    assert cached.get("nonexistent_key") is None
    assert cached.get("cik") == "0001234567"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_departure_extractions_db.py -v`
Expected: FAIL with import errors and missing-table errors.

- [ ] **Step 3: Add the table-creation function to `database.py`**

In `database.py`, locate the existing `_create_stock_prices_table` function (around line 1393). Immediately after it, add:

```python
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
```

- [ ] **Step 4: Wire the new table into `initialize_database`**

In `database.py`, find the line at ~256-257:

```python
    # Create stock_prices table for caching current stock prices
    _create_stock_prices_table(conn)
```

Immediately after it, add:

```python
    # Create departure_extractions table for caching 5.02 LLM extractions
    _create_departure_extractions_table(conn)
```

- [ ] **Step 5: Add `get_cached_departure_extraction` and `upsert_departure_extraction`**

In `database.py`, append at the very end (after `upsert_stock_price`, before the `if __name__ == "__main__":` block):

```python
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

    # Build a real dict so callers can use .get() on either DB engine
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_departure_extractions_db.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 7: Commit**

```bash
git add database.py tests/test_departure_extractions_db.py
git commit -m "Add departure_extractions cache table and helpers"
```

---

### Task 4: LLM extraction prompt + function

**Files:**
- Create: `prompts/prompt_departures.txt`
- Modify: `llm.py`
- Create: `tests/test_extract_departures.py`

- [ ] **Step 1: Create the prompt file**

Create `prompts/prompt_departures.txt`:

```
You will be given the Item 5.02 section of an SEC 8-K filing. Your job is to extract every executive or director departure mentioned in that section.

For each departure, output an object with these exact keys:
- "date": the effective date of the departure in YYYY-MM-DD format. If no effective date is given, use the filed_date provided below.
- "person": the full name of the person departing.
- "position": their position or title at the company (e.g., "CFO", "Chief Operating Officer", "Director").
- "reason": a short phrase (15 words or fewer) describing the reason given. Examples: "resigned to pursue other opportunities", "terminated without cause", "retired", "no reason stated".

Return ONLY a JSON array — no prose, no markdown, no commentary. If the filing mentions no departures (e.g., it's only about a new appointment), return an empty array: [].

The output must parse as valid JSON. Do NOT wrap it in code fences.

filed_date: {filed_date}

Filing text:
{filing_text}
```

- [ ] **Step 2: Write a failing test for `extract_departures`**

Create `tests/test_extract_departures.py`:

```python
"""Tests for llm.extract_departures — LLM extraction of departures from a 5.02 snippet."""
import json
from unittest.mock import patch, MagicMock


def _mock_response(content):
    """Build a fake OpenAI ChatCompletion response object."""
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message.content = content
    mock.usage.prompt_tokens = 100
    mock.usage.completion_tokens = 50
    return mock


def test_returns_list_of_departures_on_valid_json():
    from llm import extract_departures

    fake_json = '[{"date": "2025-09-12", "person": "Jane Doe", "position": "CFO", "reason": "Resigned"}]'
    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.return_value = _mock_response(fake_json)

        result = extract_departures("Item 5.02 ... Jane Doe ...", filed_date="2025-09-12")

    assert result["departures"] == [
        {"date": "2025-09-12", "person": "Jane Doe", "position": "CFO", "reason": "Resigned"}
    ]
    assert result["error"] is False


def test_returns_empty_list_when_llm_returns_empty_array():
    from llm import extract_departures

    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.return_value = _mock_response("[]")
        result = extract_departures("some text", filed_date="2025-01-01")

    assert result["departures"] == []
    assert result["error"] is False


def test_strips_markdown_code_fence_if_present():
    """Some models stubbornly wrap JSON in ```json fences. Tolerate that."""
    from llm import extract_departures

    wrapped = '```json\n[{"date": "2025-01-01", "person": "X", "position": "Y", "reason": "Z"}]\n```'
    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.return_value = _mock_response(wrapped)
        result = extract_departures("text", filed_date="2025-01-01")

    assert len(result["departures"]) == 1
    assert result["departures"][0]["person"] == "X"


def test_marks_error_when_json_parse_fails():
    from llm import extract_departures

    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.return_value = _mock_response("this is not json")
        result = extract_departures("text", filed_date="2025-01-01")

    assert result["departures"] == []
    assert result["error"] is True


def test_marks_error_when_api_call_raises():
    from llm import extract_departures

    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.side_effect = RuntimeError("network down")
        result = extract_departures("text", filed_date="2025-01-01")

    assert result["departures"] == []
    assert result["error"] is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_extract_departures.py -v`
Expected: FAIL — `ImportError: cannot import name 'extract_departures' from 'llm'`.

- [ ] **Step 4: Implement `extract_departures` in `llm.py`**

Append to the end of `llm.py`:

```python
def extract_departures(filing_snippet, filed_date, model=None):
    """Extract executive departures from an Item 5.02 filing snippet.

    Args:
        filing_snippet: text of (or starting with) the Item 5.02 section
        filed_date: the filing's filed_date as fallback when the snippet
                    doesn't state an effective departure date
        model: override LLM model (default: LLM_MODEL from config)

    Returns:
        Dict: {"departures": [...], "error": bool, "_tokens_in": int, "_tokens_out": int}
        Each departure: {"date", "person", "position", "reason"}.
    """
    use_model = model or LLM_MODEL

    template = _load_prompt("prompt_departures.txt")
    prompt = template.replace("{filing_text}", filing_snippet or "").replace("{filed_date}", filed_date or "")

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=use_model,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content or ""
        usage = response.usage

        # Strip markdown code fences if the model added them despite instructions
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            # remove opening fence (```json or ```)
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            # remove trailing fence
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            print(f"    extract_departures: JSON parse failed. Raw: {raw[:200]!r}", flush=True)
            return {
                "departures": [],
                "error": True,
                "_tokens_in": usage.prompt_tokens,
                "_tokens_out": usage.completion_tokens,
            }

        # Be lenient: accept either a bare list or {"departures": [...]}
        if isinstance(parsed, dict) and "departures" in parsed:
            departures = parsed["departures"]
        elif isinstance(parsed, list):
            departures = parsed
        else:
            departures = []

        # Drop any entries that aren't well-formed dicts
        departures = [d for d in departures if isinstance(d, dict) and d.get("person")]

        return {
            "departures": departures,
            "error": False,
            "_tokens_in": usage.prompt_tokens,
            "_tokens_out": usage.completion_tokens,
        }

    except Exception as e:
        print(f"    extract_departures failed [model={use_model}]: {type(e).__name__}: {e!r}", flush=True)
        return {"departures": [], "error": True, "_tokens_in": 0, "_tokens_out": 0}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_extract_departures.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 6: Commit**

```bash
git add prompts/prompt_departures.txt llm.py tests/test_extract_departures.py
git commit -m "Add LLM departure extraction prompt and function"
```

---

### Task 5: Departures pipeline + prose renderer

**Files:**
- Create: `departures.py`
- Create: `tests/test_departures.py`

- [ ] **Step 1: Write failing tests for the prose renderer (pure function, easy to TDD)**

Create `tests/test_departures.py`:

```python
"""Tests for the departures pipeline and prose renderer."""
from unittest.mock import patch


def test_render_prose_lines_basic():
    """render_prose_lines turns extraction dicts into clean bullet text."""
    from departures import render_prose_lines

    deps = [
        {
            "date": "2025-09-12", "person": "Jane Doe", "position": "CFO",
            "reason": "Resigned to pursue other opportunities",
            "_accession": "0001234-25-000123", "_filing_url": "https://sec.gov/x",
            "_is_current_filing": False, "_error": False,
        }
    ]
    lines = render_prose_lines(deps)

    assert len(lines) == 1
    line = lines[0]
    assert "2025-09-12" in line
    assert "Jane Doe" in line
    assert "CFO" in line
    assert "Resigned to pursue other opportunities" in line
    assert "https://sec.gov/x" in line
    assert "(this filing)" not in line


def test_render_prose_marks_current_filing():
    from departures import render_prose_lines

    deps = [{
        "date": "2025-01-01", "person": "X", "position": "Y", "reason": "Z",
        "_accession": "a", "_filing_url": "u", "_is_current_filing": True, "_error": False,
    }]
    lines = render_prose_lines(deps)
    assert "(this filing)" in lines[0]


def test_render_prose_handles_failed_extraction():
    """Failed extractions render as a placeholder with the SEC link preserved."""
    from departures import render_prose_lines

    deps = [{
        "date": "2024-06-15", "person": None, "position": None, "reason": None,
        "_accession": "0001234-24-000099", "_filing_url": "https://sec.gov/y",
        "_is_current_filing": False, "_error": True,
    }]
    lines = render_prose_lines(deps)
    assert len(lines) == 1
    assert "extraction failed" in lines[0].lower()
    assert "2024-06-15" in lines[0]
    assert "https://sec.gov/y" in lines[0]


def test_get_departures_for_filing_uses_cache(tmp_sqlite_db):
    """If an accession is already cached, the LLM should NOT be called for it."""
    from database import upsert_departure_extraction
    from departures import get_departures_for_filing

    # Pre-populate the cache for one accession
    upsert_departure_extraction(
        "0001234-25-000111", "0001234567", "2025-08-01",
        [{"date": "2025-08-01", "person": "Cached Person", "position": "CEO", "reason": "Retired"}],
        has_error=False,
    )

    # Mock get_edgar_departure_history to return one cached + one uncached filing
    fake_history = [
        {"filing_date": "2025-08-01", "items": "5.02", "accession_no": "0001234-25-000111", "snippet": "ignored — cached"},
        {"filing_date": "2024-03-15", "items": "5.02", "accession_no": "0001234-24-000050", "snippet": "Item 5.02 ... Bob Smith ... resigned ..."},
    ]

    fake_extract = {
        "departures": [{"date": "2024-03-15", "person": "Bob Smith", "position": "COO", "reason": "Resigned"}],
        "error": False, "_tokens_in": 50, "_tokens_out": 25,
    }

    with patch("departures.get_edgar_departure_history", return_value=fake_history), \
         patch("departures.extract_departures", return_value=fake_extract) as mock_extract:
        result = get_departures_for_filing(cik="0001234567", current_accession="0001234-25-XXXXXX")

    # extract_departures should be called once (for the uncached filing only)
    assert mock_extract.call_count == 1

    # Result should contain departures from BOTH filings, sorted newest first
    assert len(result) == 2
    # Newest first
    assert result[0]["date"] == "2025-08-01"
    assert result[0]["person"] == "Cached Person"
    assert result[1]["person"] == "Bob Smith"


def test_get_departures_marks_current_filing(tmp_sqlite_db):
    """When current_accession matches a result, _is_current_filing must be True."""
    from departures import get_departures_for_filing

    fake_history = [{
        "filing_date": "2025-09-12", "items": "5.02",
        "accession_no": "0001234-25-CURRENT", "snippet": "Item 5.02 ... Jane ...",
    }]
    fake_extract = {
        "departures": [{"date": "2025-09-12", "person": "Jane", "position": "CFO", "reason": "Quit"}],
        "error": False, "_tokens_in": 0, "_tokens_out": 0,
    }

    with patch("departures.get_edgar_departure_history", return_value=fake_history), \
         patch("departures.extract_departures", return_value=fake_extract):
        result = get_departures_for_filing(cik="0001234567", current_accession="0001234-25-CURRENT")

    assert len(result) == 1
    assert result[0]["_is_current_filing"] is True


def test_get_departures_handles_empty_history(tmp_sqlite_db):
    from departures import get_departures_for_filing

    with patch("departures.get_edgar_departure_history", return_value=[]):
        result = get_departures_for_filing(cik="0001234567", current_accession="x")
    assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_departures.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'departures'`.

- [ ] **Step 3: Implement `departures.py`**

Create `departures.py`:

```python
"""Executive Departures (24mo) — orchestration.

Pipeline (per click on the dropdown option):

  1. Fetch the company's 5.02 filings from EDGAR over the last 24 months
     using the existing fetcher.get_edgar_departure_history helper.
  2. For each filing, look up the departure_extractions cache by accession.
  3. For uncached filings, run extract_departures (LLM) in parallel and
     upsert results into the cache.
  4. Aggregate cached + fresh into a single list, sorted newest-first,
     with metadata (accession, filing URL, is-current-filing marker).
  5. render_prose_lines turns the structured list into bullet strings
     for display in the filing detail template.

Caches per-filing extractions, so re-clicks for the same company are essentially
free after the first run, and across companies a 5.02 is processed exactly once.
"""

from concurrent.futures import ThreadPoolExecutor

from database import get_cached_departure_extraction, upsert_departure_extraction
from fetcher import get_edgar_departure_history
from llm import extract_departures

MAX_PARALLEL_EXTRACTIONS = 5
MAX_FILINGS = 20  # safety cap — prevents runaway cost on serial-filer CIKs


def _direct_filing_url(cik, accession_no):
    """The direct filing index page (lists all docs in this filing)."""
    cik_stripped = (cik or "").lstrip("0") or "0"
    acc_nodash = (accession_no or "").replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_nodash}/"


def _normalize_accession(s):
    """Strip dashes for comparison (EDGAR uses 0001234-25-000001; some places strip)."""
    return (s or "").replace("-", "")


def get_departures_for_filing(cik, current_accession):
    """Return a flat, newest-first list of departure entries for this CIK
    over the last 24 months.

    Each entry shape:
        {
            "date":       "YYYY-MM-DD",
            "person":     "Full Name" or None on extraction error,
            "position":   "Title" or None,
            "reason":     "..." or None,
            "_accession": "0001234-25-000123",
            "_filing_url": "https://www.sec.gov/Archives/edgar/data/...",
            "_filing_date": "YYYY-MM-DD",  # date of the underlying 8-K
            "_is_current_filing": bool,
            "_error": bool,
        }
    """
    if not cik:
        return []

    history = get_edgar_departure_history(cik, exclude_accession="", months=24)
    if not history:
        return []

    # Cap to most recent N
    history = history[:MAX_FILINGS]

    # Partition into cached vs needs-extraction
    cached_results = {}        # accession_no -> cache row dict
    needs_extraction = []      # list of history items
    for item in history:
        accession = item["accession_no"]
        cached = get_cached_departure_extraction(accession)
        if cached is not None:
            cached_results[accession] = cached
        else:
            needs_extraction.append(item)

    # Extract uncached filings in parallel
    fresh_results = {}
    if needs_extraction:
        def _do_extract(item):
            snippet = item.get("snippet") or ""
            filed_date = item.get("filing_date") or ""
            if not snippet:
                # No snippet text → cache an error row so we don't retry forever
                upsert_departure_extraction(
                    item["accession_no"], cik, filed_date,
                    extractions=[], has_error=True,
                )
                return item["accession_no"], {"departures": [], "error": True}

            result = extract_departures(snippet, filed_date)
            upsert_departure_extraction(
                item["accession_no"], cik, filed_date,
                extractions=result["departures"], has_error=result["error"],
            )
            return item["accession_no"], result

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_EXTRACTIONS) as pool:
            for accession_no, result in pool.map(_do_extract, needs_extraction):
                fresh_results[accession_no] = result

    # Build the flat list, preserving the newest-first order from `history`
    flat = []
    current_norm = _normalize_accession(current_accession)
    for item in history:
        accession = item["accession_no"]
        filing_date = item["filing_date"]
        is_current = (_normalize_accession(accession) == current_norm)
        filing_url = _direct_filing_url(cik, accession)

        if accession in cached_results:
            row = cached_results[accession]
            departures = row.get("extractions") or []
            had_error = bool(row.get("has_error"))
        else:
            r = fresh_results.get(accession, {"departures": [], "error": True})
            departures = r.get("departures") or []
            had_error = bool(r.get("error"))

        if had_error and not departures:
            # Render a single placeholder entry so the user still sees the filing existed
            flat.append({
                "date": filing_date,
                "person": None, "position": None, "reason": None,
                "_accession": accession, "_filing_url": filing_url,
                "_filing_date": filing_date,
                "_is_current_filing": is_current,
                "_error": True,
            })
            continue

        if not departures:
            # Filing matched 5.02 but extraction returned no people. Skip silently.
            continue

        for dep in departures:
            flat.append({
                "date": dep.get("date") or filing_date,
                "person": dep.get("person"),
                "position": dep.get("position"),
                "reason": dep.get("reason"),
                "_accession": accession,
                "_filing_url": filing_url,
                "_filing_date": filing_date,
                "_is_current_filing": is_current,
                "_error": False,
            })

    # Sort by date descending (newest first). Fall back to filing_date.
    flat.sort(key=lambda d: (d.get("date") or d.get("_filing_date") or ""), reverse=True)
    return flat


def render_prose_lines(departures):
    """Turn a list of departure entries into bullet strings (HTML-ready prose).

    Each line looks like:
        **2025-09-12** — Jane Doe, CFO. Resigned to pursue other opportunities. ([filing](url))

    Returns a list of pre-formatted markdown-ish strings; the template renders
    them with the `safe` filter. Keep formatting minimal and predictable.
    """
    lines = []
    for d in departures:
        date = d.get("date") or d.get("_filing_date") or "Unknown date"
        url = d.get("_filing_url") or ""
        marker = " (this filing)" if d.get("_is_current_filing") else ""

        if d.get("_error"):
            line = f"<li><strong>{date}</strong> — (extraction failed; <a href=\"{url}\" target=\"_blank\" rel=\"noopener\">open filing</a>){marker}</li>"
        else:
            person = d.get("person") or "Unknown"
            position = d.get("position") or "Unknown role"
            reason = d.get("reason") or "no reason stated"
            # Period at end of reason if it doesn't already have one
            if not reason.rstrip().endswith((".", "!", "?")):
                reason = reason.rstrip() + "."
            line = (
                f"<li><strong>{date}</strong> — {person}, {position}. "
                f"{reason} (<a href=\"{url}\" target=\"_blank\" rel=\"noopener\">filing</a>){marker}</li>"
            )
        lines.append(line)
    return lines
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_departures.py -v`
Expected: PASS — all 6 tests green.

- [ ] **Step 5: Commit**

```bash
git add departures.py tests/test_departures.py
git commit -m "Add departures pipeline with per-accession LLM cache and prose renderer"
```

---

### Task 6: Wire dispatch into `/deep-analysis/<id>` and pass departures to template

**Files:**
- Modify: `app.py:362-527` (the `deep_analysis` route) and `app.py:281-346` (the `filing_detail` route)

- [ ] **Step 1: Add the departures branch to the `deep_analysis` route**

Open `app.py`. The current handler starts at line 362. Find the line near the top of the function body that reads:

```python
        from llm import signal_analyze, web_search_context
        from fetcher import get_edgar_departure_history
```

(at lines 370-371)

Immediately after `if not raw_text:` block ends (around line 382, before the `# --- Gather context to pre-inject into the prompt ---` comment), add this early-dispatch block:

```python
        # If the user picked the "Executive Departures (24mo)" option, run that
        # pipeline and re-render the filing page directly (no LLM signal-analysis call).
        if request.form.get("prompt_version") == "departures_24mo":
            from departures import get_departures_for_filing, render_prose_lines

            cik = filing.get("cik", "") or ""
            current_accession = filing.get("accession_no", "") or ""

            if not cik:
                flash("This filing has no CIK on record — cannot look up departures.", "error")
                return redirect(url_for("filing_detail", filing_id=filing_id))

            departures_data = get_departures_for_filing(cik=cik, current_accession=current_accession)
            departures_lines = render_prose_lines(departures_data)

            departures_context = {
                "lines": departures_lines,
                "count_filings": len({d["_accession"] for d in departures_data}),
                "company": filing.get("company", "Unknown"),
                "cik": cik,
            }

            # Re-render the filing detail page directly (no redirect — we'd lose the data)
            return _render_filing_detail(filing_id, departures=departures_context)
```

Note the call to `_render_filing_detail` — we'll factor the existing `filing_detail` body into a helper in the next step so both routes share it.

- [ ] **Step 2: Refactor `filing_detail` so the deep-analysis route can re-render it**

In `app.py`, replace the existing `filing_detail` function (lines 281-346) with a thin wrapper that delegates to a helper:

```python
@app.route("/filing/<int:filing_id>")
def filing_detail(filing_id):
    """Detail page for a single filing."""
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
        departures=departures,
    )
```

- [ ] **Step 3: Smoke test that the existing route still works**

Start the app:

```bash
python app.py
```

Open any existing filing detail page in the browser. Confirm it still renders normally (no departures card, since we didn't trigger that path).

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "Dispatch departures_24mo from deep-analysis route; factor filing_detail render helper"
```

---

### Task 7: Add the dropdown option and the departures card to the template

**Files:**
- Modify: `templates/filing.html`

- [ ] **Step 1: Add the new dropdown option in BOTH dropdown blocks**

Open `templates/filing.html`. Find the first `<select name="prompt_version">` block (lines 189-192):

```html
                    <select name="prompt_version" class="form-select form-select-sm d-inline-block" style="width: auto;">
                        <option value="v1">Prompt V1</option>
                        <option value="v2" selected>Prompt V2</option>
                    </select>
```

Replace with:

```html
                    <select name="prompt_version" class="form-select form-select-sm d-inline-block" style="width: auto;">
                        <option value="v1">Prompt V1</option>
                        <option value="v2" selected>Prompt V2</option>
                        <option value="departures_24mo">Executive Departures (24mo)</option>
                    </select>
```

Find the second dropdown (re-run, around lines 215-218) and apply the same change.

- [ ] **Step 2: Add the Executive Departures card**

Open `templates/filing.html`. Find the closing of the existing Deep Analysis card — the line:

```html
        {% endif %}

    </div>
</div>
```

near lines 232-235 (the `{% endif %}` closes the `{% if filing.get('deep_analysis') %}` block).

Right BEFORE the final `</div></div>` (still inside the inner content `<div>`), insert the new card:

```jinja
        <!-- Executive Departures (last 24 months) — populated when user picks the dropdown option -->
        {% if departures is not none %}
        <div class="card mt-4">
            <div class="card-header">
                <h5 class="mb-0">Executive Departures — Last 24 Months</h5>
                <small class="text-muted">{{ departures.company }} (CIK {{ departures.cik }})</small>
            </div>
            <div class="card-body">
                {% if departures.lines %}
                <ul class="mb-2">
                    {% for line in departures.lines %}
                        {{ line | safe }}
                    {% endfor %}
                </ul>
                <small class="text-muted">Source: {{ departures.count_filings }} SEC filing{{ '' if departures.count_filings == 1 else 's' }}.</small>
                {% else %}
                <p class="mb-0 text-muted">No executive-departure filings found for this company in the last 24 months.</p>
                {% endif %}
            </div>
        </div>
        {% endif %}
```

- [ ] **Step 3: Manual smoke test**

Start the app:

```bash
python app.py
```

In the browser:
1. Navigate to any filing detail page.
2. In the dropdown next to "Signal Analysis" or "Re-run", select **Executive Departures (24mo)**.
3. Click the button.
4. Wait — first run for a CIK will hit EDGAR + the LLM (10-30s for a serial filer). Subsequent clicks for the same CIK should return in ~2s (cache hits).

Confirm:
- The page re-renders with the new "Executive Departures — Last 24 Months" card.
- Each line shows: bold date, person, role, reason, link to SEC filing.
- If the current filing is itself a 5.02, its line carries the "(this filing)" marker.
- If the company has no 5.02 history, the empty-state message appears.
- If the filing has no CIK, a flash error explains.

- [ ] **Step 4: Click the button a second time on the same filing**

Confirm the second click returns much faster (<2s) — that's the per-accession cache working.

- [ ] **Step 5: Commit**

```bash
git add templates/filing.html
git commit -m "Add Executive Departures (24mo) dropdown option and card"
```

---

### Task 8: Final verification

- [ ] **Step 1: Run the full test suite**

```bash
pytest tests/ -v
```

Expected: all green.

- [ ] **Step 2: Run a clean end-to-end test**

```bash
python app.py
```

Walk through both features once more:
1. Visit `/` — confirm stock prices appear under tickers.
2. Open a filing — confirm normal signal analysis still works (Prompt V2).
3. Re-run with the new dropdown option — confirm the departures card renders.
4. Re-click — confirm caching kicks in (faster).

- [ ] **Step 3: Final commit if anything changed during smoke testing**

```bash
git status
```

If nothing's modified, you're done. If a small fix was needed, commit it with a `fix:` prefix.

---

## Self-Review

**Spec coverage:**
- Feature 1 stock price (dashboard rendering) → Tasks 1-2 ✓
- Feature 2 EFTS query → reused `get_edgar_departure_history` (already covers EDGAR submissions API) ✓
- Per-accession cache → Task 3 (DB) ✓
- LLM extraction → Task 4 ✓
- Parallel pipeline → Task 5 ✓
- Prose renderer → Task 5 ✓
- Dropdown dispatch (no new route) → Task 6 ✓
- Template integration → Task 7 ✓
- Failure handling (per-filing error rows, no-CIK guard) → Tasks 5 + 6 ✓
- Cap N=20 filings → Task 5 (`MAX_FILINGS=20`) ✓
- Cost guardrail (parallelism + caching) → Task 5 ✓

**Placeholder scan:** No "TBD", no "implement later", no "similar to". All steps contain executable code or exact commands.

**Type consistency:**
- `extract_departures` returns `{"departures": list, "error": bool, "_tokens_in": int, "_tokens_out": int}` — used consistently in Task 5.
- `get_cached_departure_extraction` returns dict with key `extractions` (list) — Task 5 reads `row.get("extractions")`. ✓
- Departure entry shape is documented in `get_departures_for_filing` docstring; tests in Task 5 verify the same shape; renderer in Task 5 reads the same fields. ✓
- `_filing_url` field name is consistent across pipeline output, prose renderer, and tests. ✓

Plan is ready to execute.
