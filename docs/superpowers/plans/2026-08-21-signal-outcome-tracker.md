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

- [x] Capture at ingest for filings with a ticker and a verdict of DEEP_LOOK or
      MONITOR (PASS is noise — do not spend rows on it, but do record the verdict
      so PASS can be scored later if wanted).
- [x] Must be non-fatal: a price-fetch failure never blocks storing a filing.
- [x] Backfill entry point for existing rows — `capture_baselines()` walks any
      filing with a verdict, so the same call serves ingest and retrospective backfill.
      UI button shipped on /backfill as 'Backfill Outcome Prices'.

## Task 3: Marking job

- [x] Daily pass in `scheduler.py`: find rows past each horizon whose mark is null,
      fetch closes, store. Idempotent — safe to re-run same day.
- [x] Never re-mark a horizon that already has a value.

## Task 4: Scoring + `/scorecard` page

- [x] Direction-aware: a BEARISH call **hits when the stock lags SPY**; BULLISH hits
      when it leads. Excess return = stock % change − SPY % change over the horizon.
- [x] Break out hit rate and median excess return by: verdict, direction, signal
      score bucket, and signal type (forfeits comp / no successor / departure
      cluster / market-based hurdle).
- [x] **Always show n.** A 100% hit rate on n=3 is not a signal, and the page must
      not let it look like one. Suppress or grey out cells below a minimum n.
- [x] Be honest about survivorship and horizon truncation on the page itself.

## Task 5: Tests

- [x] Unit tests, all mocked, no network — matching the existing suite's style.
- [x] Cover: direction-aware hit logic (both directions), the null-mark idempotency,
      small-n suppression, missing-price handling, and the SQLite/Postgres dict rule.
- [x] Full suite green (baseline 176) before every commit.

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
- 2026-08-21 — CODEX ROUND 4 on head 292342d. Three findings, all verified real:
  7. **P2** — a still-forming daily candle could be cached. Yahoo returns a bar for the
     session in progress whose "close" is just the last trade so far, and since a horizon
     mark is never revisited that intraday price would be frozen into the scorecard
     permanently. Only bars strictly before the current UTC date are cached now
     (`latest_complete_date()`); coverage is judged against that same cutoff so a window
     touching today does not refetch forever.
  8. **P2** — coverage counted "priced at baseline" from the row's lifecycle status, so a
     filing that priced fine and later went dark dropped out — letting the table report
     fewer priced rows than scored rows. Now derived from `baseline_close`; "awaiting"
     still requires status ok, since a delisted row is never re-marked.
  9. **P2** — the baseline backfill re-read transiently-skipped rows forever. Skipped rows
     get no outcome row, so they returned at the front of every batch; a run of failures
     at the head could hide the whole older archive and still report completion. Now
     carries an exclusion list across batches, and aborts at 500 failures because that
     many means the source is down rather than the data being patchy.
  5 more tests, suite 284 green, re-verified live (max cached bar <= last complete session).
  CONVERGENCE NOTE: rounds ran 4 → 2 → 1 → 3. #8 was a direct consequence of the round-2
  fix and #9 was the round-1 batching fix left half-applied, so findings are no longer
  purely pre-existing. Stopping the fix-and-rereview cycle here and handing back to the
  user rather than grinding further.
- 2026-08-21 — CODEX BOT RE-REVIEW of head ff86205. Two more findings, both verified real:
  5. **P1** — a per-horizon price failure set a ROW-WIDE status, and `build_scorecard`
     filtered on that status, so one dead horizon erased every other horizon's valid
     marks. A name delisted at day 40 lost its real 7d and 30d results — systematically
     removing the cases where a bearish call was working. Fixed two ways: horizons are
     now settled individually via `give_up_on_horizon()` (stamps `marked_{n}d_at`,
     leaves closes NULL, so a stuck horizon can't starve the LIMIT-ed batch either),
     and scoring now reads the DATA rather than the row status.
  6. **P2** — the benchmark lookup discarded the returned date, so `get_close_on_or_after`
     could roll SPY forward up to 10 days and silently measure the two legs over
     different windows — breaking the same-window guarantee the excess-return number
     rests on. `_benchmark_close_on()` now requires an exact bar-date match.
  Page copy corrected: a delisted name IS scored at every horizon it actually traded
  through; only the horizons after it went dark are left unscored.
  9 more tests, suite 276 green, re-verified live across all three horizons.
- 2026-08-21 — CODEX BOT REVIEW on PR #2. All four findings verified as real and fixed:
  1. **P1** — `clear_all_filings()` left `signal_outcomes` behind (no FK/cascade), so a
     clear-and-repopulate would strand calls against deleted filings with dead links and
     duplicate the sample under fresh IDs. Now cleared; the ticker-keyed price cache is
     deliberately kept.
  2. **P2** — the FIRST not-found response discarded cached bars, so a widened fetch that
     404'd could make a perfectly cached filing get written off as delisted. Now serves
     the cache on that call, not just on later ones.
  3. **P2** — the backfill marked one batch (LIMIT-capped) then printed "Done", leaving a
     large archive part-scored. Now loops until a round makes no progress.
  4. **P2** — outcome scoring sat after the scheduler's early returns, so it never ran on
     a day with no filings or an EDGAR failure — i.e. every weekend, which is exactly
     when horizons elapse. Moved into a `finally` around the ingest step.
  9 regression tests added, suite 269 green, re-verified live end-to-end.
- 2026-08-21 — ADVERSARIAL REVIEW of the branch diff. Found and fixed:
  1. **Real bug** — a transient price-source failure was recorded as "unpriceable",
     which is permanent and stops the row being retried. One network outage during
     a backfill would have silently deleted the archive from the scorecard. Now
     nothing is written off unless the source actually answered (`_answer_is_final`).
  2. Bars cached before a ticker went dark were being discarded once it 404'd —
     a company delisted last month traded normally the month before.
  3. best/worst could print the same filing in both tables on a short list.
  4. `/scorecard` read the outcome table twice per page load.
  Verified end-to-end against live market data: 5 real tickers, delisted name
  correctly flagged, all horizons marked, page renders. Suite 260 green.
- 2026-08-21 — Tasks 4+5 COMPLETE: `outcome_scoring.py`, `/scorecard` page, nav link,
  and the retrospective backfill button. 31 tests (22 scoring + 9 route). Suite 256 green.
  The page states what it excludes; delisted names are counted but deliberately NOT
  scored as bearish wins. Next: adversarial review of the branch diff, then PR.
- 2026-08-21 — Tasks 2+3 COMPLETE: `outcomes.py` (capture_baselines / mark_due_outcomes
  / run_outcome_job), wired into the daily scheduler as a non-critical step. 15 tests,
  suite 225 green. Benchmark is priced on the stock's OWN bar date so excess return
  compares identical windows. Starting Task 4 (scoring + /scorecard + backfill button).
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
