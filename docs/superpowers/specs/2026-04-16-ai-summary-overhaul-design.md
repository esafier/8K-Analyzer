# AI Summary & Subcategory Overhaul — Design

**Date:** 2026-04-16
**Branch:** `feature/ai-summary-overhaul`
**Status:** Design — pending user review

---

## Problem

When scanning the dashboard, the current summary doesn't contain enough information to understand what an 8-K filing says. Three specific pain points:

1. **Summaries are too short and prose-based.** A 1–2 sentence summary forces the user to click into each filing to get the facts they care about (name, title, dates, severance, comp terms, stock price targets).
2. **Subcategory is a single value.** When a filing contains both a departure AND an appointment (or any mix of events), only one subcategory label shows up — the other events are invisible on the dashboard.
3. **Complex/dense filings risk falling through.** Unusual filings (insider forward sales, share pledges, plan amendments, multi-event restructurings) don't fit the current schema and may be dropped or under-summarized.

Additionally, two related UX issues (covered briefly here, deferred for execution):
- The "SEC" link on the dashboard goes to an index page, not the filing itself (requires a second click).
- Dashboard display layout needs to accommodate the richer summary.

## Goals

- Produce **event-based, structured summaries** that act as a substitute for reading the filing — not a teaser.
- Handle **multiple events per filing** cleanly (e.g., departure + appointment + comp all in one 8-K).
- Provide a **safety net for complex filings** so no material content is silently dropped.
- Keep the current single-pass LLM architecture (no extra API calls, no added latency) while improving accuracy.
- Stay backward-compatible with existing database rows.

## Non-goals

- Widening the upstream item-code filter in [filter.py](filter.py) (currently `5.02, 1.01, 1.02, 8.01`). Flagged as a known limitation for future work.
- Redesigning the dashboard from scratch. We will apply minimum viable layout changes to render the new structure, and revisit deeper UI in a follow-up.
- Refactoring the Chat Completions vs. Responses API split (`llm.py`). Out of scope.

---

## Section 1 — New summary JSON schema

The LLM returns a single JSON object per filing. Fields that don't apply are `null` or empty arrays.

```json
{
  "relevant": true,
  "relevant_reason": "Required when relevant is false — one-line justification for exclusion.",
  "reasoning": "Brief enumeration of every event identified before filling the structure (chain-of-thought).",

  "top_level_category": "Management Change | Compensation | Both | Other",
  "subcategories": ["CFO Departure", "CFO Appointment", "Inducement Award"],
  "urgent": false,

  "is_complex": false,
  "narrative_summary": null,

  "departures": [
    {
      "name": "John Smith",
      "title": "Chief Financial Officer",
      "effective_date": "2026-03-15",
      "stated_reason": "resigned to pursue other opportunities",
      "successor_info": "interim CFO named; permanent search underway",
      "signal": "Departure announced 7 days before Q1 earnings; no permanent successor named."
    }
  ],

  "appointments": [
    {
      "name": "Jane Doe",
      "title": "Chief Financial Officer",
      "effective_date": "2026-05-01",
      "has_comp_details": true
    }
  ],

  "comp_events": [
    {
      "executive": "Jane Doe (incoming CFO)",
      "grant_type": "RSUs + PSUs",
      "grant_value": "$5M total ($3M RSUs + $2M PSUs at target)",
      "grant_date": "2026-04-13",
      "filing_date": "2026-04-13",
      "vesting_schedule": "4-yr ratable, 25% cliff at 1 yr",
      "performance_hurdles": "Revenue > $500M AND EBITDA margin > 15% by FY28",
      "stock_price_targets": "PSUs vest if stock reaches $60, $75, $85 per share (current ~$47)"
    }
  ],

  "other": [
    "CEO Patricia Wong entered a variable prepaid forward contract covering 750K shares (~$84M).",
    "Signal: monetizing ~30% of direct holdings without a public sale; collar floor at $95."
  ]
}
```

### Key design decisions

1. **`reasoning` field** — chain-of-thought safety net. LLM lists every event it sees *before* filling structured fields. Reduces dropped events on multi-event filings. Not displayed.
2. **`subcategories` always an array** — even for single-event filings (`["CFO Departure"]`). Fixes the "only one shows up" bug.
3. **`top_level_category`** — kept for dashboard badge color coding. Auto-derived but explicit so the LLM commits.
4. **`appointments[].has_comp_details`** — a boolean flag pointing to a corresponding `comp_events[]` entry. Avoids duplicating data. Simple appointments (no comp disclosed) are just `name + title + effective_date`.
5. **Severance / accelerated vesting / retention bonuses for departing execs** → go in `comp_events[]`, not `departures[]`. Keeps the departure section focused on who/when/why.
6. **`is_complex` + `narrative_summary`** — the safety net. For dense or unusual filings, the LLM sets `is_complex: true` and provides a 3–6 bullet narrative capturing everything that didn't fit the structured buckets.
7. **`other[]`** — bulleted strings for edge-case events (role changes, employment amendments without $$, insider transactions, 10b5-1 plans, share pledges, buybacks, clawback updates).
8. **`relevant_reason`** — required one-liner when the LLM rejects a filing. Makes false negatives visible in logs.

### Event routing rules

| Event | Destination |
|-------|-------------|
| Death of executive | `departures[]` with `stated_reason: "death"` |
| Retirement announcement with far-off date | `departures[]` with future `effective_date` |
| Board member departure / appointment | `departures[]` / `appointments[]` with `title: "Director"` |
| Role change / promotion (no departure) | `other[]` |
| Employment agreement amendment with $$/equity | `comp_events[]` |
| Employment agreement amendment without $$ | `other[]` |
| Severance / accelerated vesting on a departure | `comp_events[]` with `executive: "Name (departing role)"` |
| Inducement award to new hire | `comp_events[]` linked to `appointments[]` |
| Insider transactions (forward sales, swaps, collars, pledges, 10b5-1, large open-market sales) | `other[]` with structured facts + Signal line |
| Comp plan amendment (broad, not to a specific exec) | `other[]` + set `is_complex: true` with narrative detail |

---

## Section 2 — Prompt changes

Save as **`prompts/prompt_v3.txt`**. `prompt_v2.txt` stays intact as a fallback. The existing prompt-version toggle in [filter.py](filter.py) and [app.py:re_summarize](app.py) already supports selecting which prompt to use.

The v3 prompt will:

- Return the schema defined in Section 1.
- Require the `reasoning` field to enumerate every event found **before** filling structured sections.
- **Widen `relevant: true` scope** to explicitly include insider transactions, hedges, pledges, 10b5-1 plans, and material executive share activity.
- Bias toward inclusion: *"When in doubt, mark relevant AND set is_complex AND narrate. Better to over-include than miss."*
- Require a `relevant_reason` when rejecting a filing.
- State the event routing rules (above) explicitly.
- List example scenarios for `is_complex: true`: comp plan overhauls, multi-event filings with rich detail, unusual governance events, clawback adoptions, buybacks tied to executive transactions.
- Keep existing **urgency criteria** unchanged (sudden CEO/CFO/CAO departures, terminations for cause, deaths, imminent effective dates).
- Keep **price target extraction** guidance from v2 (this logic is already good).
- Preserve the **"never copy boilerplate from the filing"** instruction.

### Prompt-rejection telemetry

We log every filing where the LLM returns `relevant: false` along with `relevant_reason`. These logs get persisted in a new column on the `filings` table (or existing log output — TBD in implementation plan). This lets the user audit what's being filtered out.

---

## Section 3 — Subcategory storage (database)

- **No schema migration required.**
- The existing `auto_subcategory TEXT` column in the `filings` table ([database.py](database.py)) will hold a **JSON-encoded array** (e.g., `'["CFO Departure","CFO Appointment"]'`).
- A new helper `parse_subcategories(raw: str) -> list[str]` in a utility module:
  - If the string starts with `[`, parse as JSON.
  - Otherwise treat as a single-element list (backward compatibility for existing rows).
- All code paths that read `auto_subcategory` go through the helper.
- Display code renders multiple badge pills from the list.

This preserves backward compatibility — every existing row continues to work as a one-element array.

---

## Section 4 — Dashboard display (minimum viable changes)

Scope for *this* spec: just enough UI work to render the new content. Deeper layout (card vs. expandable row vs. wide-table) is deferred to a follow-up spec.

**Changes to [templates/index.html](templates/index.html):**

1. **Summary column renders structured sections**, not a truncated string. Each section (Departure / Appointment / Comp / Other / Narrative) shown as a mini-block with:
   - A small colored label (e.g., red "DEPARTURE", blue "APPOINTMENT", green "COMPENSATION", purple "NARRATIVE")
   - Bulleted facts below
2. **Summary column no longer hard-truncates.** Let it wrap. Widen by dropping the separate Sub-Category column.
3. **Subcategory becomes badge pills** rendered from the array. Placed inline with the Category badge.
4. **Complex filings** get a small purple "Complex" badge near the URGENT badge.
5. **"Open filing" button** added alongside the existing "SEC" button (Section 5 below).

**Changes to [templates/filing.html](templates/filing.html):**

1. Same structured rendering for the summary.
2. Show full `reasoning` field in a collapsed debug section (useful for auditing LLM output during rollout).
3. Show `relevant_reason` if present.

We'll add a Jinja filter `render_structured_summary(filing)` that takes a filing row and returns the HTML for the structured summary. Stays DRY across templates.

---

## Section 5 — SEC link fix (one-click to filing)

**Current state:** `filing_url` in the database points to the filing's *index page* on SEC.gov (e.g., `.../000...-index.htm`). Clicking "SEC" on the dashboard takes the user there, requiring a second click to get to the actual `.htm` or `.pdf` of the 8-K.

**Fix:**

1. Add a new column `filing_document_url` to the `filings` table.
2. In [fetcher.py:fetch_filing_text](fetcher.py) — which already navigates from the index page to the primary document to extract text — also capture and return the primary document URL.
3. Store that URL in `filing_document_url` during ingest.
4. In templates, add an **"Open filing"** button that uses `filing_document_url`. Keep the existing "SEC" button (pointing to the index page) as a secondary fallback since it still works for older rows that don't have the new field populated.
5. For existing rows without `filing_document_url`, the "Open filing" button gracefully falls back to `filing_url`.

**Migration:** new column added with `ALTER TABLE IF NOT EXISTS` (already a pattern in [database.py](database.py)). Existing rows keep working via fallback.

---

## Section 6 — Migration strategy for existing filings

Use the existing `run_resummarize()` function in [app.py](app.py) (already supports re-running the LLM on existing filings).

**Plan:**
- **Default:** new filings going forward use v3 prompt automatically.
- **Backfill:** re-analyze filings from the **last 60 days** using v3. This is what the user actively scans on the dashboard. Cost estimate: ~$5–20 in OpenAI tokens depending on filing count.
- **Older filings:** remain on v2 output. They still render correctly because the subcategory helper (Section 3) treats single-string values as one-element arrays. Fields missing from v2 output (e.g., `narrative_summary`) are simply not rendered.

The migration itself is a one-command operation: `python -c "from app import run_resummarize; run_resummarize(prompt_version='v3', since_days=60)"` (exact API TBD in implementation plan).

---

## Safety architecture (summary)

| Gate | Protection |
|------|-----------|
| Item code filter (upstream) | Known limitation; not changed in this spec |
| Keyword scan | Unchanged; currently permissive |
| `relevant: false` decisions | Now require a `relevant_reason` — logged for review |
| Structured buckets drop content | `narrative_summary` catches overflow when `is_complex: true` |
| Schema can't express the filing | `is_complex: true` flag + narrative ensures rendering |
| LLM drops an event | `reasoning` field forces pre-enumeration (chain-of-thought) |
| Pipeline failure on one filing | Existing error handling in [filter.py](filter.py) preserves partial data; unchanged |

---

## Testing strategy

Before merging to main:

1. **Unit-test `parse_subcategories`** with both array and single-string inputs.
2. **Integration test** the v3 prompt on a curated set of 10–15 historical filings representing:
   - Single event (CFO departure only)
   - Multi-event (departure + appointment + comp)
   - Comp-only (inducement grant)
   - Role change (→ Other)
   - Insider transaction (forward sale / pledge)
   - Dense/complex filing (comp plan overhaul)
   - Edge rejection case (the LLM should mark `relevant: false` with reason)
3. **Compare v2 vs. v3 output** using the existing `test_prompt.py --compare` tool.
4. **Manual dashboard check** on local SQLite: verify structured rendering, multiple subcategory badges, Complex badge, Open filing button.
5. **Render deploy check** on the feature branch before merge (Render supports preview branches — to confirm during implementation).

## Rollback strategy

Everything lives on `feature/ai-summary-overhaul`. `main` is untouched. To revert:

- Delete the branch: `git branch -D feature/ai-summary-overhaul` (pre-merge)
- Or revert the merge commit: `git revert -m 1 <merge-sha>` (post-merge)

The new `filing_document_url` column is additive — safe to leave in place even after revert.

## Open questions

None at this time. All resolved during brainstorming.

## Out of scope (future work)

- Widening the upstream item-code filter to catch insider-transaction filings filed under Items 3.02, 7.01, etc.
- Rebuilding the dashboard layout (cards vs. expandable rows vs. other).
- Adding structured filtering on the dashboard (e.g., "show me all filings with sign-on > $5M") — the new schema enables this, but the UI work is separate.
- Adding a prompt-diff tool to the dashboard UI.
