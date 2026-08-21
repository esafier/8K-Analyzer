# Signal Outcome Tracker — Implementation Plan

> **Overnight autonomous run.** Branch: `claude/autonomous-improvement-strategy-nfgkcp`.
> Steps use checkbox (`- [ ]`) syntax. Update this file as work completes — it is
> the state handoff between loop iterations. If you are a fresh iteration, read
> this file top-to-bottom before acting, then continue at the first unchecked box.

**Goal:** Answer the question the scanner currently cannot: *are its verdicts any
good?* Record each scored filing's stock price at ingest, re-mark it at 7/30/90
days against SPY, and expose a `/scorecard` page showing direction-aware hit
rates by verdict and by signal type.

**Why this first:** every other improvement to prompts, scoring, or triage is
unfalsifiable without this. Once it exists, prompt changes can be judged on
realized excess return instead of taste.

**Baseline at start:** 176 tests passing, `main` untouched, Render service still
pointed at the older trial branch (do not touch Render).

---

## Task 0 (INVESTIGATE FIRST — changes everything downstream)

The HANDOFF assumed this feature is **prospective-only** because API Ninjas serves
current prices, not historical — meaning the scorecard would show nothing useful
for 90 days. Before building on that assumption, spend one iteration checking
whether a **free, keyless historical daily price source** exists.

- [ ] Test Stooq daily CSV: `https://stooq.com/q/d/l/?s=aapl.us&i=d` (no key, no
      account). Confirm it returns OHLC history for common US tickers and for
      `spy.us`. Check coverage on a handful of the small/mid-cap tickers this
      project actually surfaces — that is where a free source usually fails.
- [ ] If Stooq works: the scorecard can be **backfilled across the ~4,118 existing
      filings on day one** rather than accruing from deploy day forward. This is
      the difference between a page that is useful tomorrow morning and one that
      is useful in November. Take it.
- [ ] If it does not work: fall back to the prospective-only design, and record in
      this file exactly what was tried and why it failed, so it is not re-litigated.
- [ ] Either way, write the price source behind a small module (`price_history.py`)
      with a single `get_daily_closes(ticker, start, end)` entry point, so the
      source can be swapped without touching the scoring logic.

---

## Task 1: Schema — `signal_outcomes` table

- [ ] New table keyed on `filing_id`, holding: ticker, accession_no, the verdict
      snapshot at ingest (verdict / direction / signal_score), baseline date +
      baseline stock close + baseline SPY close, and per-horizon marks for 7d /
      30d / 90d (stock close, SPY close, marked_at).
- [ ] Store the **verdict snapshot**, not a live join to `filings`. Prompts change;
      a scorecard that silently re-scores history against today's prompt is worthless.
- [ ] Follow the existing pattern: `_create_signal_outcomes_table(conn)` called from
      `initialize_database()`, additive only, safe under concurrent gunicorn workers.
- [ ] CLAUDE.md rule applies: return **real dicts**, not `sqlite3.Row`, from any query
      whose results reach `.get()`.

## Task 2: Baseline capture

- [ ] Capture at ingest for filings with a ticker and a verdict of DEEP_LOOK or
      MONITOR (PASS is noise — do not spend rows on it, but do record the verdict
      so PASS can be scored later if wanted).
- [ ] Must be non-fatal: a price-fetch failure never blocks storing a filing.
- [ ] Backfill entry point for existing rows (uses Task 0's historical source).

## Task 3: Marking job

- [ ] Daily pass in `scheduler.py`: find rows past each horizon whose mark is null,
      fetch closes, store. Idempotent — safe to re-run same day.
- [ ] Never re-mark a horizon that already has a value.

## Task 4: Scoring + `/scorecard` page

- [ ] Direction-aware: a BEARISH call **hits when the stock lags SPY**; BULLISH hits
      when it leads. Excess return = stock % change − SPY % change over the horizon.
- [ ] Break out hit rate and median excess return by: verdict, direction, signal
      score bucket, and signal type (forfeits comp / no successor / departure
      cluster / market-based hurdle).
- [ ] **Always show n.** A 100% hit rate on n=3 is not a signal, and the page must
      not let it look like one. Suppress or grey out cells below a minimum n.
- [ ] Be honest about survivorship and horizon truncation on the page itself.

## Task 5: Tests

- [ ] Unit tests, all mocked, no network — matching the existing suite's style.
- [ ] Cover: direction-aware hit logic (both directions), the null-mark idempotency,
      small-n suppression, missing-price handling, and the SQLite/Postgres dict rule.
- [ ] Full suite green (baseline 176) before every commit.

---

## Guardrails for this run

- All work on `claude/autonomous-improvement-strategy-nfgkcp`. **Never push `main`.**
- **No Render dashboard, env var, branch, or deploy changes.** The service is
  serving an older trial branch and the user is asleep.
- No live LLM or paid-API spend. Tests stay fully mocked.
- Commit per completed task with the suite green. Push as you go — the container
  is ephemeral and unpushed work is lost work.
- If a design question is genuinely ambiguous, pick the reversible option, note the
  assumption in this file, and keep moving. Do not stall waiting for an answer.

## Run log

- 2026-08-21 — Plan created. Baseline 176 tests passing. Starting Task 0.
