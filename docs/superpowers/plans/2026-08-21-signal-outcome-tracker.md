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

## Permanent-write inventory (the invariant this feature rests on)

Four separate review findings came from a guard applied in one place and missed in
another. An earlier sweep reported clean and was **wrong**, because it grepped for
where guards *exist* rather than where one is *missing*. The durable fix is to keep
this list: every site that writes a state which stops future work, and what makes
that write safe. **If you add a permanent write, add it here.**

| Site | Writes | Safe because |
|---|---|---|
| `price_history.get_daily_closes` | `meta.status = not_found` | Only when `fetch_from_yahoo` saw a real symbol miss — HTTP 404, or Yahoo's own "may be delisted" wording. Any other error returns `None` and stays retryable. |
| `price_history.get_daily_closes` | `meta.span_start/end` | Only the span actually fetched. Disjoint spans are never merged, so coverage can't be claimed over a gap. |
| `outcomes.capture_baselines` | `OUTCOME_DELISTED` | `_ticker_is_gone()` — the price source definitively denied the symbol. |
| `outcomes.capture_baselines` | `OUTCOME_NO_PRICE` | `_answer_is_final()` — the source answered for this window and had no bars. |
| `outcomes.mark_due_outcomes` | `OUTCOME_DELISTED` + horizon settled | `_ticker_is_gone()`. |
| `outcomes.mark_due_outcomes` | horizon settled | Overdue **and** `_answer_is_final()`. |
| `outcomes.mark_due_outcomes` | `set_outcome_mark` (a price, forever) | Both legs non-None; the DB layer refuses `None` and never overwrites an existing mark. |

Two properties hold the whole thing up, and both were bugs that had to be fixed:
`has_coverage()` must never report coverage over an unfetched gap (round 7), and
`not_found` must never be written for a recoverable error (round 8). Every gate above
reads one of those two, so if either regresses, all seven sites start lying at once.

Retry suppression audited separately: the only bare `return {}` in `get_daily_closes`
are input validation (empty ticker, inverted range). Every other early return serves
the cache **without** recording coverage, so a retry can always still happen.

Verified 2026-08-21 at head 56097fd.

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
- 2026-08-21 — CODEX ROUND 8 on head 3a2fdfd. Two findings, both real, both FIXED.
  16. **P2 FIXED (severe in effect)** — a 200 response carrying ANY `chart.error` was
      treated as a permanent symbol miss. Probed Yahoo live: a genuine miss is HTTP 404
      with `code: "Not Found"`, `description: "No data found, symbol may be delisted"` —
      already handled by the 404 branch. The 200+error branch therefore only ever catches
      OTHER errors (throttling, auth, internal), and recording those as `not_found`
      permanently blanks a LIVE ticker with no retry path. A long backfill is exactly when
      such an error is likeliest, so this could have silently removed many live names.
      Now matched on Yahoo's actual miss wording; anything else returns None (retryable).
      This is a genuine gap in my own early "transient failures must not be permanent"
      fix — the 05:05Z sweep missed it because the pattern appears in a different form.
  17. **P2 FIXED** — the page asserted "Stopped trading at some point (delisted after the
      filing)" for any ticker that stopped resolving. The price source cannot distinguish
      a delisting from a ticker RENAME — Yahoo itself hedges with "may be delisted" — so a
      company alive under a new symbol was reported as dead, and counted in the
      delisting-bias narrative. Relabelled to "Symbol stopped resolving (delisted, or the
      company renamed its ticker)", and the disclosure now says the count is an upper bound
      on real delistings. Copy only; the internal OUTCOME_DELISTED constant is unchanged
      (renaming it would be a migration for no behavioural gain). Resolving successor
      tickers would need a corporate-actions source — noted as possible future work, not done.
  4 more tests, suite 297 green, re-verified live.
- 2026-08-21 — CODEX ROUND 7 on head 6632ab5. Two findings, both real, both FIXED —
  the P1 cleared the "severe and unambiguous data corruption" bar.
  14. **P1 FIXED** — `upsert_price_history_meta` merged spans unconditionally, so two
      interleaved runs (double-clicked backfill button, or the scheduled job landing on
      a manual one) could each fetch a different slice of one ticker and collapse them
      into a single continuous claim over a gap neither had fetched. A claimed-but-empty
      span reads downstream as "we looked and there are nothing", so `_answer_is_final()`
      would permanently write those filings off as unpriceable — silent, irreversible.
      Two layers: disjoint spans are now REFUSED (keep the newer; older bars stay cached
      and are simply refetched if asked for again), and a process-local lock stops two
      outcome runs overlapping at all. The span fix is the real safety property — it
      holds across processes; the lock is belt-and-braces for the common case.
      NOTE the normal path is unaffected: a refetch always spans the union of the request
      and the stored span, so it can never be disjoint. Tested both ways.
  15. **P2 FIXED** — `count_signal_outcomes()` still derived `priced` from `status = 'ok'`
      while `build_scorecard()` had been moved to `baseline_close` in round 4. A filing
      that priced and later went dark was counted unpriced in the backfill log and the
      scorecard's empty state, disagreeing with the scorecard itself. Same half-applied
      pattern as round 5's copy bug. Now derived from `baseline_close`; the end-to-end
      check asserts the two totals agree at every horizon.
  7 more tests, suite 293 green, re-verified live.
- 2026-08-21 — CODEX ROUND 6 on head c45e440. Two findings; one REFUTED, one real.
  12. **REFUTED** — "raw quote.close corrupts returns across a split." Tested empirically
      against NVDA's 10-for-1 split (2024-06-10): the series runs 120.89 → 121.79 straight
      through with no discontinuity. Yahoo's `quote.close` is ALREADY split-adjusted;
      only `adjclose` adds dividend adjustment (0.17% on NVDA over ~2y). No change made.
      There IS a smaller, different issue the finding did not state: dividends are
      unadjusted, so a big ex-div inside a 90-day window reads as a small mechanical
      drop. Immaterial for the non-dividend micro-caps this scanner mostly surfaces.
      FIRST BOT FINDING THAT DID NOT HOLD UP — relevant to the convergence judgment.
  13. **REAL, SURFACED — the most important methodological issue on the PR.** The baseline
      is the close on `filed_date`. An 8-K filed after the 4pm close was not public at
      that price, so for those filings the measured move includes the overnight reaction
      — untradeable, and it INFLATES apparent performance. 5.02 departure filings land
      after hours often, so the affected share is probably large.
      NOT silently fixed: the choice is a real tradeoff and it is the user's to make.
        - Conservative: baseline = close on the first trading day AFTER filed_date.
          Always tradeable; gives up genuine same-day alpha on filings made during hours.
          One-line change. RECOMMENDED.
        - Precise: use EDGAR's acceptance timestamp to pick D or D+1 per filing. Needs
          data we do not store — `filed_date` comes from full-text search `file_date`,
          a date with no time — so it means a fetcher change plus a backfill.
      NOTE the bot's own proposed fix (anchor on the verdict/ingest timestamp) is WRONG
      for this codebase: in a retrospective backfill the verdict timestamp is "now", which
      would make the whole historical scorecard unscoreable.
      WHAT WAS DONE: the page now states plainly that these are not tradeable returns and
      why. Shipping a scorecard that silently includes untradeable overnight moves would
      contradict the one thing the page exists to do. Copy only; no scoring change.
- 2026-08-21 — CODEX ROUND 5 on head f708084. Two findings, both verified real. Posture
  had already switched to surface-don't-fix, so these were split:
  10. **FIXED** — the header caveat still said delisted names are "excluded from the rates
      ... which flatters bearish calls". Both halves were wrong after the round-2
      per-horizon fix: they ARE scored at horizons they traded through, and dropping only
      the LATER horizons removes disproportionately bearish-successful outcomes, so the
      bias runs AGAINST the bearish signal. This was my own round-2 fix left half-applied
      (I updated the bottom card and missed the header), producing an actively false
      statement on the deliverable. Fixed + pinned with a test; zero logic risk.
  11. **SURFACED, NOT FIXED** — the backfill makes ~3 requests per ticker-filing, not 1.
      SPAN_PAD_DAYS=10 means the baseline fetch covers filed±20, which absorbs the 7d
      lookup but not 30d or 90d, so each of those triggers a widened refetch. Roughly
      triples backfill wall-clock (~27min → ~80min at 4,118 filings) and rate-limit
      exposure, and makes the PR's "one request per ticker" claim untrue. Clean fix
      exists (prewarm filed-pad → filed+90+pad once per filing before the baseline
      lookup, ~5 lines) but it is a prefetch-strategy change with a real tradeoff, so it
      is the user's call. NOT a correctness issue.
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
