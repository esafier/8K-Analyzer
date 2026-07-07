# HANDOFF — read me first (session continuity)

**Purpose:** This file catches a new Claude Code chat up on work that spanned a
previous session. If you are a fresh session, read this top-to-bottom before
acting. It records state that lives in Render + past conversation, NOT in code.

**Last updated:** 2026-07-07
**Working branch:** `claude/project-improvement-review-ne5ryf` (all work below is here; `main` is the untouched original)

---

## 1. What this project is (and the user's goals)

8K Analyzer is a **buy-side signal scanner** over SEC 8-K filings — not a news
reader. The user backfills date ranges and **scans a pre-scored triage inbox**
on the Render webapp to generate stock ideas. They hunt two signals:

- **BEARISH — insiders losing confidence:** sudden C-suite departures (CEO/CFO/CAO
  matter most), **no successor named**, terminations for cause, **departure
  clusters** (2+ at a company in 24mo), and the loudest tell — an executive who
  **forfeits unvested comp** to leave (walking away from money).
- **BULLISH — board conviction via comp design:** **market-based vesting hurdles**
  (stock-price / market-cap / TSR targets requiring big appreciation vs current
  price), **spring-loaded grant timing**, comp shifted to long-vesting at-risk equity.

Priorities: maximum **signal-to-noise**, fast scanning. Routine noise (equity-plan
housekeeping like share-pool increases; pure financing/dilution filings) should be
rated PASS, not surfaced as signal. NOTE: user explicitly decided **do NOT hide
PASS by default** — leave the dashboard default as "All verdicts".

---

## 2. What shipped this session (14 commits on the branch)

All committed + pushed to `origin/claude/project-improvement-review-ne5ryf`, 151 tests passing.

1. **Exhibit fetching** (`fetcher.py`) — the LLM now reads EX-17/EX-10/EX-99
   exhibits (separation agreements, resignation letters, press releases), where
   the actual forfeiture/severance/hurdle numbers live. Was body-only before.
2. **Departure-history integrity** (`fetcher.py`, `departures.py`) — EDGAR
   failures return `None` (retryable) instead of stamping a false "0 departures";
   full Item 5.02 section extracted (not first 800 chars).
3. **Page handling** (`app.py`, `templates/index.html`) — no more 500s on bad
   `?page=`, clamps to valid range, filter query string built once + URL-encoded.
4. **Queryable bearish signals** (`database.py`, `summary_utils.py`) — new columns
   `forfeited_comp` + `has_successor` derived from v3 output; dashboard filters for
   Direction (BEARISH/BULLISH/MIXED/NEUTRAL), "Forfeits comp", "Dep cluster";
   row badges FORFEITS COMP / NO SUCCESSOR.
5. **% appreciation on price hurdles** (`market_targets.py`) — 🎯 badge shows
   "+100%" (target vs current price); detail page shows per-tier breakdown.
6. **Dilution glossary in prompts** (`prompts/prompt_v3.txt`, `_signal_analysis_v2`)
   — pre-funded warrants / ownership blockers / ATM translated to plain English,
   pure financing defaults to PASS.
7. **Dashboard scan density** — MONITOR collapses to one-line signal (PASS already
   did); score is a color chip; fixed CSS collision where FORFEITS COMP rows lost
   the unread indicator; watchlist cards carry verdict/score/badges.
8. **Keyword recall** (`filter.py`) — keyword misses on 5.02/1.01/1.02 now get an
   LLM look (8.01-only misses still dropped — too high-volume).
9. **DB startup hardening** (`database.py`) — concurrent-worker migrations can't
   crash a gunicorn worker; SQLite gets WAL + busy_timeout.
10. **Self-review fixes** — 9 real bugs caught by an adversarial multi-agent review
    of this branch (price regex `$1000`→100.0, false NO-SUCCESSOR substring match,
    5.02 truncation on "incorporated by reference", EX-101/104 mis-classified as
    material agreements, dashboard 500 on non-object JSON, migration rollback wiping
    the read_at backfill, etc.). See commit `c09f1b1`.
11. **Model upgrade** — default daily model `gpt-4o-mini` → `gpt-5.4-nano`;
    premium → `gpt-5.4`; signal analysis → `gpt-5.4`; backfill dropdowns offer
    nano/mini/full tiers. Model IDs are **env-overridable** (`LLM_MODEL`,
    `LLM_MODEL_PREMIUM`, `LLM_MODEL_SIGNAL`) so they can be changed from the Render
    dashboard with no redeploy.

---

## 3. LIVE DEPLOY STATE (this is the part not in the repo)

- **Render web service** `8k-analyzer` (`srv-d5ttphaqcgvc73ev08f0`) is CURRENTLY
  POINTED AT THE TRIAL BRANCH `claude/project-improvement-review-ne5ryf`, not
  `main`. Deploy `dep-d96gdc6rnols73bdqmag` went live 2026-07-07 ~14:02 UTC.
  Boot logs confirmed the `forfeited_comp`/`has_successor` migration ran cleanly
  and the concurrent-worker hardening worked (second worker skipped, no crash).
- **Postgres** `8k-analyzer-db` (`dpg-d5ttp9aqcgvc73ev04dg-a`): **4,118 filings
  intact**; 1,586 have `structured_summary` (retrofit-eligible).
- URL: https://eightk-analyzer.onrender.com
- All schema changes are **additive** → rolling back to `main` loses no data.

### Rollback (if the user wants the original back)
Render dashboard → service Settings → Build & Deploy → **Branch** → set back to
`main` → Save (auto-redeploys the original). Or Deploys tab → any prior deploy →
Rollback. The MCP `update_web_service` tool CANNOT change the branch — this is a
manual dashboard action only (confirmed this session).

---

## 4. OPEN DECISIONS (pending user input — do not assume)

1. **Run the retrofit?** The "Run Retrofit" button on `/backfill` (POST
   `/retrofit-market-targets`) will populate `forfeited_comp`/`has_successor` and
   market-target %s on the 1,586 structured rows — free, no LLM. Not yet run.
2. **Pin the daily model?** Whether to set `LLM_MODEL=gpt-4o-mini` as a Render env
   var to trial the pipeline on the known-good old model, OR leave it on
   `gpt-5.4-nano` (requires confirming that model ID is enabled on the user's
   OpenAI account, or the 7am job's LLM calls fail). Not yet decided.
3. **Keep or roll back** after the trial — user is evaluating.
4. **Merge to `main`** — only after the user is happy. `main` auto-deploys on push.

---

## 5. DEFERRED WORK — signal outcome tracker (scoped, not started)

User wants this eventually but paused it. Plan: record each DEEP_LOOK/MONITOR
filing's stock price at ingest, re-mark at 7/30/90 days vs SPY, and a `/scorecard`
page showing hit rates by signal type (direction-aware: bearish "hits" when the
stock lags SPY). New `signal_outcomes` table; daily marking job in the scheduler;
prospective-only (API serves current prices, not historical, so it scores from
deploy day forward). ~4 commits. Zero LLM cost. Full plan is in this session's
history if resumed.

---

## 6. Env / test notes

- Local dev uses SQLite; prod uses Postgres. `sqlite3.Row` supports `row["k"]`
  but NOT `.get()` — convert rows to real dicts before `.get()` (see `CLAUDE.md`).
- Tests: `python -m pytest tests/ -q` (151 passing). No network/LLM needed — all mocked.
- Render MCP loses workspace selection on reconnect; re-select `tea-d5ttm7fgi27c73ebtjvg` (only workspace).
