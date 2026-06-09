# Scored Triage Inbox — Plan

**Goal:** stop reviewing filings one-by-one. Every filing arrives pre-scored
(DEEP_LOOK / MONITOR / PASS + 0-10 score + direction + one-line top signal),
departure clusters get flagged automatically on the dashboard, and departures
now capture whether the exec forfeited comp (the core bearish tell).

**Why:** the dashboard previously sorted by date only with binary flags
(urgent, market targets), so the user did all triage manually. The signal
analysis prompt already produced verdicts — but only on-demand, per filing,
behind a dropdown. This moves a lightweight verdict into the ingest pipeline
so the list ranks itself.

---

## Steps

### 1. Prompt (prompts/prompt_v3.txt)
- [x] Add `triage` object to response schema: verdict, score 0-10, direction, top_signal
- [x] Add triage guidance section written around the investment thesis
- [x] Add `comp_impact` + `forfeiture_flag` fields to departures schema

### 2. Parsing helper (summary_utils.py)
- [x] `parse_triage(llm_result)` + `count_departures(structured)`

### 3. Database (database.py)
- [x] Migrate columns: triage_verdict, signal_score, signal_direction,
      top_signal, departure_count, departure_count_24mo, departure_history
      + index on (cik, filed_date)
- [x] insert_filing writes new columns (branches deduplicated into one
      shared column/values list while touching this)
- [x] update_filing_analysis accepts + writes new fields
- [x] get_filings / get_filtered_filing_count: `verdict` filter + `sort`
      (filters refactored into shared _build_filing_filters so list and
      pagination count can't disagree)
- [x] update_departure_history(filing_id, count, json)

### 3b. EDGAR-based departure history (revised per user feedback)
The local DB doesn't have enough history for cluster counts — replaced the
local-count approach with the EDGAR pipeline behind the "Executive
Departures (24mo)" button, now run automatically:
- [x] departures.enrich_filing_departure_history — fetch + persist per filing
- [x] departures.enrich_new_filings — post-ingest hook (backfill + scheduler)
- [x] departures.run_history_backfill — one-time stamp for existing filings
      (works even on rows where departure_count was never retrofitted)
- [x] /backfill-departure-history route + button on backfill page
- [x] Detail page auto-renders the 24mo departures card from stored history
      (no clicking); dropdown still does a live refresh and re-persists
- [x] Dashboard badge reads departure_count_24mo (EDGAR-based)
- [x] Removed the local-DB get_departure_cluster_counts approach

### 4. Ingest paths
- [x] filter.py sets triage fields + departure_count
- [x] app.py run_resummarize passes them through
- [x] app.py run_retry_missing_summaries: same — and fixed a pre-existing bug
      where this path never ran market-target detection, so rescued filings
      never got the 🎯 flag

### 5. Retrofit (retrofit_market_targets.py)
- [x] Also backfills departure_count from existing structured_summary JSON

### 6. Dashboard (app.py index + templates/index.html)
- [x] Verdict dropdown (All / Deep Look + Monitor / Deep Look / Monitor / Pass)
- [x] Sort dropdown (Newest first / Signal strength)
- [x] Row badges: verdict, direction arrow + score, cluster badge
- [x] top_signal line at top of summary cell
- [x] verdict + sort threaded through pagination and back-links

### 7. Detail page + summary partial
- [x] filing.html: verdict/score badges in header + top_signal line
- [x] _structured_summary.html: FORFEITS COMP badge + comp_impact line

### 8. Backfill page copy
- [x] Re-Summarize note (populates triage on old rows)
- [x] Retrofit section renamed to cover departure counts

### 9. Tests + verification
- [x] tests/test_triage.py — 18 tests (parse_triage, count_departures,
      verdict filter, signal sort, departure-history enrichment + backfill
      candidate selection with mocked EDGAR calls)
- [x] Full suite: 105 passed
- [x] Live verification on a scratch DB: signal sort order, verdict filters,
      EDGAR-based cluster badge ("3 dep/24mo"), auto-rendered detail-page
      departures card (all names render, no click), FORFEITS COMP badge,
      comp-impact lines, triage banner, backfill button; legacy rows render
      fine.

---

## Review

Shipped the scored triage inbox end-to-end. Key decisions:

- **One LLM call, no new cost for triage.** Verdict/score/direction ride
  along in the existing v3 classification call.
- **Chronological default preserved.** Signal sort and verdict filter are
  opt-in dropdowns; the verdict concept is additive and easy to ignore or
  remove if the user decides against it.
- **Departure history comes from EDGAR, not the local DB** (revised after
  user feedback — local DB too shallow). Reuses the cached
  departures.get_departures_for_filing pipeline, run automatically at ingest
  (backfill + daily scheduler) and exposed as a one-time backfill button.
  Counts are "last 24 months as of when scanned".
- **Legacy rows degrade gracefully.** NULL verdict = no badge; in signal sort
  they rank between MONITOR and PASS. Re-Summarize rates them (LLM cost);
  "Backfill Departure History" stamps cluster badges (EDGAR + cached LLM).
- **Bug fixed along the way:** retry-missing-summaries path never set
  has_market_targets — now consistent with the other two ingest paths.

Rollout after deploy: (1) press "Backfill Departure History" once for
cluster badges + instant detail cards on existing filings, (2) optionally
Re-Summarize a date range to get verdicts on recent historical filings,
(3) new backfills and the daily 7am job get everything automatically.
