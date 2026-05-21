# Market-Based Comp Targets — Plan

**Goal:** Detect when a filing's compensation disclosure includes a **stock-price target, market-cap target, or TSR (total shareholder return) target**, surface it as a badge + dedicated section on the filing card, and expose a filtered dashboard view. Backfill historical filings so the new view is useful immediately.

**Why these matter:** These are market-based hurdles — they tell us what price/value the board thinks the stock should hit. They're a different signal from operating hurdles (revenue, EBITDA, EPS), which is why they belong in their own bucket.

---

## Schema decisions

Two storage layers, on purpose:

1. **`has_market_targets` (INTEGER 0/1) column on `filings`** — fast filter for the dashboard. Indexed by virtue of being a single column.
2. **`market_targets` nested object inside `structured_summary` JSON** — holds the actual extracted values (executive, target value, vesting condition). Used by the UI callout.

Splitting the prompt's `performance_hurdles` field:
- **`market_based_targets`** (new structured object) → `{stock_price, market_cap, tsr}` per comp_event
- **`operating_hurdles`** (free text, replaces today's `performance_hurdles`) → revenue / EBITDA / EPS / milestones

Old rows keep working: template falls back to legacy `performance_hurdles` when the new fields are absent.

---

## Steps

- [ ] **1. Migration** — add `has_market_targets INTEGER DEFAULT 0` column to `filings` in `database.py` `_migrate_filings_columns()`
- [ ] **2. Prompt** — update `prompts/prompt_v3.txt`:
  - Split `performance_hurdles` → `operating_hurdles` (free text) + `market_based_targets` (structured object with stock_price / market_cap / tsr keys)
  - Add an explicit instruction block clarifying the split
- [ ] **3. filter.py** — when persisting v3 output:
  - Detect `market_based_targets` presence across all comp_events
  - Set `filing["has_market_targets"]` (0/1) for the column
  - Embed `market_targets` aggregate object inside the `structured_summary` JSON
- [ ] **4. database.py** — `insert_filing` and update paths need to write the new column; `get_filings` and `get_filtered_filing_count` need a `market_targets_only` filter param
- [ ] **5. summary_utils.py** — `structured_summary_for_display` passes through `has_market_targets` and `market_targets` fields
- [ ] **6. Retrofit module** — new file `retrofit_market_targets.py`:
  - Scan every row in `filings` with non-null `structured_summary`
  - For each comp_event: check `stock_price_targets` (dedicated field), and regex/keyword-scan `performance_hurdles` for TSR + market-cap mentions
  - Update the row's `has_market_targets` column + write a derived `market_targets` block into the JSON
  - Zero LLM calls — pure data transform on what's already in the DB
- [ ] **7. app.py route** — `POST /retrofit-market-targets` runs the retrofit in a background thread; track via `backfill_runs` table
- [ ] **8. Template `_structured_summary.html`**:
  - Add yellow "🎯 Market Targets" badge next to existing Urgent/Complex badges
  - Add a dedicated callout section showing extracted targets (stock price, market cap, TSR) grouped by executive
  - Render new fields when present; fall back to legacy `performance_hurdles` for old rows
- [ ] **9. Dashboard `index.html`**:
  - Add "Show only Market Targets" toggle filter (mirror the existing Urgent toggle)
  - Wire `?market_targets=1` query param through `index()` in app.py
- [ ] **10. Backfill UI** — add "Retrofit market targets" button on the backfill page that POSTs to `/retrofit-market-targets`
- [ ] **11. Tests**:
  - Unit test for retrofit detection logic (stock price field, TSR phrase, market cap phrase, none of the above)
  - Test that filter.py sets `has_market_targets` correctly when LLM returns new schema
  - Test that `get_filings(market_targets_only=True)` filters correctly
- [ ] **12. Verify** — run pytest, manually start the Flask app, render a flagged filing
- [ ] **13. Commit + push** to `main` → Render auto-deploys
- [ ] **14. After deploy** — kick off retrofit from the deployed dashboard to populate the filtered view

---

## Backfill strategy (after deploy)

1. **Retrofit (free, instant)** — POST `/retrofit-market-targets` from the dashboard. Walks every existing filing's `structured_summary` JSON and flags rows where the LLM already extracted price/TSR/market-cap info. Expected coverage: ~70–80% of historical filings that should be flagged.

2. **Re-LLM (optional, costs API calls)** — use existing `/resummarize` route on a date window. Re-runs the updated prompt against `raw_text` already stored. Pick up the ~20–30% the retrofit missed because the LLM had buried market-based language inside the old `performance_hurdles` field instead of the dedicated targets field.

Decision rule: run the retrofit first, look at the resulting filter page, then decide whether the missed cases warrant the LLM spend.

---

## Review

(to be filled in after implementation)
