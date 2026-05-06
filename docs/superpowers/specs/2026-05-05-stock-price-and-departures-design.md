# Stock Price on Dashboard + Executive Departures (24mo) — Design

**Date:** 2026-05-05
**Status:** Approved, ready for implementation plan

---

## Summary

Two features for the 8-K analyzer:

1. **Current stock price** displayed inline on every row of the filings dashboard at `/`.
2. **Executive Departures (last 24 months)** — a new option in the signal-analyzer dropdown on the filing detail page that fetches all of the company's prior 5.02 filings from SEC EDGAR over the last 24 months, runs LLM extraction per filing (date, person, position, reason), caches the result by accession number, and renders a clean prose summary on the filing page.

---

## Feature 1 — Stock price on the dashboard

### Where

Index page row, ticker cell ([templates/index.html:107-114](../../../templates/index.html#L107-L114)). Rendered under the existing market-cap and earnings-date lines, in the same muted style.

### Backend

- Add `get_stock_price_map(tickers)` to [stock_price.py](../../../stock_price.py).
  - Mirrors `get_market_cap_map` / `get_earnings_map`.
  - Iterates the existing `get_stock_price(ticker)` (which already caches via the `stock_prices` table with a 1-hour TTL).
  - Wrapped in per-ticker try/except so a single API failure cannot break the dashboard.
  - Returns `{ticker: price}` (omits tickers with no price).

- In `app.py`'s `/` handler ([app.py:232-248](../../../app.py#L232-L248)), call `get_stock_price_map(unique_tickers)` next to the existing market-cap and earnings calls. Pass `stock_prices` into `render_template`.

### Frontend

In [templates/index.html](../../../templates/index.html) ticker cell, add a third line:

```jinja
{% if filing['ticker'] and stock_prices.get(filing['ticker']) %}
<br><span class="stock-price">${{ "%.2f"|format(stock_prices[filing['ticker']]) }}</span>
{% endif %}
```

If the price is missing, render nothing (no placeholder).

Add a small CSS rule for `.stock-price` matching the muted style of `.market-cap`.

### Why this approach

Reuses the existing API key, cache table, and per-row enrichment pattern exactly. No schema or migration work.

---

## Feature 2 — Executive Departures (last 24 months)

### Where

- New option `"departures_24mo"` in the prompt-version `<select>` on [templates/filing.html:189-192](../../../templates/filing.html#L189-L192) and the re-run dropdown at line 215.
- New POST route `/departures/<filing_id>` in [app.py](../../../app.py).
- New file [departures.py](../../../departures.py) holds fetch + extraction logic.
- New prompt file [prompts/prompt_departures.txt](../../../prompts/prompt_departures.txt) for LLM extraction.
- New DB table `departure_extractions` defined in [database.py](../../../database.py).
- Result rendered in a new card on [templates/filing.html](../../../templates/filing.html) when `departures` context var is populated.

### Pipeline

When the user selects "Executive Departures (24mo)" and submits:

1. **EFTS query** — call `efts.sec.gov/LATEST/search-index` with:
   - `forms=8-K`
   - `ciks=<filing's CIK>`
   - `dateRange=custom`, `startdt=<today - 24 months>`, `enddt=<today>`
   - `q="5.02"` (server-side narrowing)
   - Filter response hits to those whose `_source.items` array contains `5.02` (authoritative). Reuse `_sec_get_with_retry` from [fetcher.py:61](../../../fetcher.py#L61).
   - Cap N=20 most recent (prevents runaway cost on serial-filer CIKs).

2. **Cache check** — for each accession number, query `departure_extractions`. Cached rows are reused as-is. Missing accessions go to step 3.

3. **Per-filing extraction (parallel)** using `concurrent.futures.ThreadPoolExecutor` (max_workers=5):
   - Fetch the filing's primary document HTML from SEC.
   - Extract just the Item 5.02 section: find first `\bItem\s+5\.02\b` marker → next `\bItem\s+\d+\.\d{2}\b` marker (or end of doc). This is an extension of the regex already in [fetcher.py:35](../../../fetcher.py#L35).
   - Send the 5.02 slice to the LLM with `prompt_departures.txt`. The prompt instructs: return JSON array `[{"date": "YYYY-MM-DD", "person": "Full Name", "position": "Title", "reason": "<= 15 words"}]`. One row per departure mentioned (a single filing may list multiple).
   - Validate JSON. On parse failure or empty result, store an empty array with an `error` flag so we don't retry forever.
   - Insert into `departure_extractions`.

4. **Aggregate & render** — merge all cached + freshly-extracted rows, flatten into a single list of departures sorted newest-first, render as prose on the filing page:

   > **Executive Departures — Last 24 Months (ACME, CIK 0001234567)**
   >
   > - **2025-09-12** — Jane Doe, CFO. Resigned to pursue other opportunities. ([filing](https://www.sec.gov/...))
   > - **2024-11-04** — John Smith, COO. Terminated without cause. ([filing](https://www.sec.gov/...))
   >
   > *Source: 4 SEC filings.*

   - One bullet per departure.
   - Bold date + name; comma + position; period + reason.
   - Each bullet's filing date links to the SEC filing.
   - If a filing's extraction failed, render: `- **<date>** — (extraction failed; [open filing](url))`.
   - If zero filings found: info banner, no LLM calls.
   - If the currently-viewed filing is itself a 5.02 in the result set, append a small "(this filing)" suffix to its bullet so the user knows where they are. Otherwise no marker.

### Database schema

```sql
CREATE TABLE IF NOT EXISTS departure_extractions (
    accession_number TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    filed_date TEXT NOT NULL,
    extractions_json TEXT NOT NULL,  -- JSON array of {date, person, position, reason}
    has_error INTEGER NOT NULL DEFAULT 0,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_departures_cik ON departure_extractions(cik);
```

PostgreSQL variant uses `TIMESTAMPTZ` and `INSERT ... ON CONFLICT (accession_number) DO UPDATE`. Follow the dual-dialect pattern in [database.py](../../../database.py) (`_create_stock_prices_table` is the closest analog).

DB helper functions (in [database.py](../../../database.py)):
- `get_cached_departure_extraction(accession_number) -> dict | None`
- `upsert_departure_extraction(accession_number, cik, filed_date, extractions, has_error)`

Both return real Python dicts (not `sqlite3.Row`) per the project's CLAUDE.md compatibility rule.

### Prompt

[prompts/prompt_departures.txt](../../../prompts/prompt_departures.txt) — short, JSON-only output, no prose:

> You will be given the Item 5.02 section of an 8-K filing. Extract every executive or director departure mentioned. For each, output: date of departure (YYYY-MM-DD; use the effective date from the filing, or the filing's filed_date if none stated), full name, position/title, and a reason in 15 words or fewer. Return a JSON array only — no prose, no markdown, no commentary. If no departures are mentioned, return `[]`.
>
> Filing text:
> {filing_text}

Filing's `filed_date` is passed in as fallback context.

### Routing

```python
@app.route("/departures/<int:filing_id>", methods=["POST"])
def departures_view(filing_id):
    # Look up filing → get CIK
    # Run pipeline → get list of departures
    # Re-render filing.html with departures context var
```

**Dispatch:** the existing `/deep-analysis/<filing_id>` POST handler inspects `request.form.get("prompt_version")`. If it equals `"departures_24mo"`, it dispatches to the departures pipeline and re-renders the filing page with a `departures` context var (skipping the LLM signal-analysis path). Otherwise the existing flow runs unchanged. No new route, no JS changes.

### Failure handling

- EFTS query fails → render error banner, no LLM calls.
- Filing fetch fails → that filing's row in DB is written with `has_error=1` and `extractions_json="[]"`. Rendered as "(extraction failed)".
- LLM JSON parse fails → same as above.
- Network/SEC rate limiting → reuse `_sec_get_with_retry`'s exponential backoff.

### Cost & latency

- Cold cache, N=10 filings: 1 EFTS call + 10 SEC document fetches + 10 LLM calls. With 5-way parallelism: ~3-8 seconds end-to-end. LLM input ~3K chars/filing × 10 = ~30K chars total spread across 10 calls.
- Warm cache: 1 EFTS call + 0 LLM calls. Sub-second.
- Cache stays fresh forever per accession (filings are immutable).

---

## What is NOT in scope

- No persistence of the rendered "departures view" itself — only the per-filing extractions are cached.
- No retry of failed extractions (a manual cache-clear could be a follow-up).
- No cross-company analysis or trends.
- No UI to clear or refresh the departure cache.

---

## Risks

- **EFTS `q="5.02"` recall** — covered by also filtering on `_source.items`.
- **5.02 section extraction edge cases** — some filings have unusual formatting or split items. Falls back to the full filing text (truncated to a sane cap, e.g., 20K chars) if the slice is implausibly short (< 200 chars).
- **Filings with no CIK** — show a friendly error; the feature requires a CIK to query EDGAR.
- **Render's PostgreSQL** — ensure the new DB helper returns real dicts on both engines.
