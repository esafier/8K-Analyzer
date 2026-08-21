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

## Task 0 — RESOLVED 2026-08-21 (do not re-litigate)

The HANDOFF assumed this feature was **prospective-only** (scores only from deploy
day forward) because API Ninjas serves current prices, not historical. That
assumption is wrong, and overturning it is the single biggest win available here.

**Tried and rejected — Stooq** (`stooq.com/q/d/l/?s=spy.us&i=d`): now gated behind a
JavaScript proof-of-work challenge. Solvable in principle, but defeating an
anti-bot challenge is not an acceptable production dependency. Do not revisit.

**Chosen — Yahoo Finance chart endpoint**, keyless:
`https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?period1={unix}&period2={unix}&interval=1d`

Verified working from this container:
- Returns daily closes for arbitrary historical windows (67 bars over a 100-day
  window ending ~6 months back).
- Covers the micro-caps this scanner actually surfaces — GEVO ($2), BTAI ($1.95),
  SOAR ($1.09), XELA ($0.04) all returned clean series.
- Returns a clean **HTTP 404 on delisted/renamed tickers** (AULT, NUZE), which is a
  usable signal rather than silent garbage.

**Consequence — the scorecard is retrospective, not prospective.** It can be
backfilled across the ~4,118 filings already in Postgres and be genuinely useful
the morning it ships, instead of in November. Build for backfill first.

- [x] Confirm a free keyless historical source exists
- [x] `price_history.py` with one entry point `get_daily_closes(ticker, start, end)`,
      so the source can be swapped when Yahoo breaks — it is an **unofficial
      endpoint with no stability guarantee**, and this abstraction is the whole
      insurance policy. Everything downstream depends only on this signature.
- [x] Cache fetched series in a `price_history` table — 4,118 filings must not mean
      4,118 network calls. Fetch each ticker's full span once, slice locally.
- [x] Throttle politely (sequential, small sleep). This is someone else's free
      endpoint; do not hammer it.
- [x] **Delisting is signal, not an error.** (recorded as `status='not_found'`; scoring treatment still open — see Task 4) A bearish call on a company that later
      went dark is the strongest possible hit. Record 404-after-baseline distinctly
      rather than dropping the row — but do NOT auto-score it as a win without the
      user's input; surface it as its own bucket on the page.

## Task 1: Schema — `signal_outcomes` table

- [x] New table keyed on `filing_id`, holding: ticker, accession_no, the verdict
      snapshot at ingest (verdict / direction / signal_score), baseline date +
      baseline stock close + baseline SPY close, and per-horizon marks for 7d /
      30d / 90d (stock close, SPY close, marked_at).
- [x] Store the **verdict snapshot**, not a live join to `filings`. Prompts change;
      a scorecard that silently re-scores history against today's prompt is worthless.
- [x] Follow the existing pattern: `_create_signal_outcomes_table(conn)` called from
      `initialize_database()`, additive only, safe under concurrent gunicorn workers.
- [x] CLAUDE.md rule applies: return **real dicts**, not `sqlite3.Row`, from any query
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

- 2026-08-21 — Plan created. Baseline 176 tests passing.
- 2026-08-21 — Task 1 COMPLETE: `signal_outcomes` table + storage layer, 16 tests,
  suite 210 green. Two design points worth keeping: horizons anchor on the FILING
  date (the event), and the cutoff is computed in Python because TEXT-date
  arithmetic differs between SQLite and Postgres. Starting Task 2.
- 2026-08-21 — Task 0 COMPLETE: `price_history.py` + `price_history`/`price_history_meta`
  cache tables shipped, 18 tests, verified live against real bars (GEVO/SPY/delisted).
  Suite 194 green. Starting Task 1.
- 2026-08-21 — Task 0 resolved: Stooq is PoW-gated; Yahoo chart endpoint works
  keylessly incl. micro-caps. Feature is now **retrospective** — backfill the
  existing ~4,118 filings rather than waiting 90 days. Starting Task 1.
