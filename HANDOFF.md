# HANDOFF — read me first (session continuity)

**Purpose:** This file catches a new Claude Code chat up on work that spanned a
previous session. If you are a fresh session, read this top-to-bottom before
acting. It records state that lives in Render + past conversation, NOT in code.

**Last updated:** 2026-08-21
**Working branch:** `claude/autonomous-improvement-strategy-nfgkcp` — the signal
outcome tracker (see §5). The earlier branch `claude/project-improvement-review-ne5ryf`
holds the work described in §2 and is what Render is still serving (see §3).

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

### ⚠️ NOTHING SCHEDULED EVER RUNS IN PRODUCTION (discovered 2026-08-21)

`render.yaml` defines one service: `gunicorn app:app`. The Render account was
checked directly and contains **exactly one service, the web app** — no cron job,
no background worker. `app.py` never imports `scheduler`, and `scheduler.py`'s
loop sits under `if __name__ == "__main__"`.

**So `daily_fetch_job()` has never run automatically.** This is pre-existing and
affects the whole pipeline, not just outcome scoring: the 7am fetch, market-cap
prefetch, earnings prefetch and departure enrichment are all dead code in prod.
Everything that has ever populated the database came from manual runs on
`/backfill`. (§4 note about "the 7am job's LLM calls" assumes a job that does not
exist.)

The same applies to the new outcome scoring: the **"Backfill Outcome Prices"
button works and is the supported path**, but nothing marks new horizons on its
own. Pressing the button again picks up whatever has come due.

To actually automate it, add a Render Cron Job. **Not done — Render cron jobs are
billable and this is the user's call:**

```yaml
  - type: cron
    name: 8k-analyzer-daily
    runtime: python
    schedule: "0 11 * * *"        # 7am ET
    buildCommand: pip install -r requirements.txt
    startCommand: python scheduler.py --now
    envVars:
      - key: DATABASE_URL
        fromDatabase:
          name: 8k-analyzer-db
          property: connectionString
      - key: OPENAI_API_KEY
        sync: false
      - key: API_NINJAS_KEY
        sync: false
```

`scheduler.py --now` runs one pass and exits, which is the right shape for cron.

### ⚠️ RENDER IS SERVING `main`, NOT THE TRIAL BRANCH (corrected 2026-08-21)

The Render API reports `branch: main` with `autoDeploy: yes` on commit. The note
above saying the service points at `claude/project-improvement-review-ne5ryf` is
**stale**. Consequence: **merging anything to `main` deploys it immediately.**
Treat a merge as a deploy.

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

## 5. SIGNAL OUTCOME TRACKER — BUILT (branch `claude/autonomous-improvement-strategy-nfgkcp`)

No longer deferred. Answers the question the scanner could not: *do its verdicts
predict anything?* Full plan and run log in
`docs/superpowers/plans/2026-08-21-signal-outcome-tracker.md`.

**The assumption that changed.** This was scoped as prospective-only ("scores from
deploy day forward") because API Ninjas serves current prices only. That was wrong.
Yahoo's keyless chart endpoint returns daily closes for arbitrary historical windows
and covers the micro-caps this scanner surfaces, so the scorecard is **retrospective**:
the ~4,118 filings already in Postgres can be scored now rather than in 90 days.

**What shipped**
- `price_history.py` — the only module that knows where prices come from. Cached in
  `price_history` / `price_history_meta`. Yahoo's endpoint is unofficial; when it
  breaks, replace `fetch_from_yahoo()` and keep the two public signatures.
- `signal_outcomes` table — one row per scored filing, holding a **snapshot** of what
  the scanner claimed (prompts change; re-scoring history against today's prompt
  measures nothing) plus baseline and 7/30/90-day marks for the stock and SPY.
- `outcomes.py` — baseline capture + horizon marking. Runs daily inside the existing
  scheduler job as a non-critical step. Zero LLM cost.
- `outcome_scoring.py` + `/scorecard` — direction-aware hit rates and median excess
  return vs SPY, broken out by verdict, direction, score bucket and signal type,
  plus best/worst individual calls.
- **Backfill button** on `/backfill` ("Backfill Outcome Prices") — this is the one to
  press first. It prices the whole archive and marks every elapsed horizon.

**Method decisions worth not re-litigating**
- SPY is priced on the stock's OWN bar date, so excess return compares identical windows.
- Horizons anchor on the filing date (the event), not the baseline trading day.
- BEARISH hits when the stock lags SPY; BULLISH when it leads. NEUTRAL/MIXED are
  counted but never scored — grading them would grade a prediction nobody made.
- Hit rates are **withheld entirely** below 10 scored calls. A caveated number still
  reads as a number.
- Delisted names are counted and shown but NOT auto-scored as bearish wins. Scoring
  them would hand the bearish signal its best outcomes for free. **This is the one
  open judgment call — the user may want them scored.**
- A transient price-source failure never writes a filing off as unpriceable. This was
  a real bug caught in review: one network outage would otherwise have silently
  deleted the archive from the scorecard, permanently.

**Not done / next**
- Nothing has been run against the live Postgres archive yet — the backfill button
  has never been pressed. Expect it to take a while (one price request per ticker,
  throttled, then cached).
- The scorecard has no significance testing. With a few thousand filings that may be
  worth adding; right now it just refuses to show thin rates.
- Prompt-quality loop (the second half of the overnight brief) not started.

## 6. GRANT-TIMING SCREEN (spring-load), added 2026-08-21

A triage-grade port of the `spring-load-detector` skill into the app. **Not** the
full forensic pass — it sees one 8-K plus the price tape, and every result lists
what it could not test. A high score means "run the real skill on this one".

- `prompts/prompt_spring_load.txt` — extraction ONLY. The prompt forbids judgment,
  price analysis and scoring, because all of that is deterministic Python.
- `spring_load.py` — the screen. Price path (run-in, +1..+5 pop, +30d vs SPY,
  monthly-low, V-shape) via the `price_history` layer built for the outcome
  tracker; price hurdles converted to **required CAGR**; cross-recipient service-
  condition asymmetry; scoring with the skill's bands.
- `spring_load_analyses` table — cached by accession (immutable filing text), so
  a clear-and-repopulate keeps the work and re-runs are free.
- **Run on one filing:** button on the filing detail page (`POST /spring-load/<id>`).
- **Backtest saved filings:** button on `/watchlist` (`POST /backtest-spring-load`).

**Deliberate ceiling: this screen never scores above 8.** The 9-10 band needs Form
4 history, the proxy's 402(x) narrative, or committee composition — none of which
it can see. Underneath that, the skill's single-observation rule holds: without
repetition or a self-contradiction in the paperwork, a lone grant is held at 7 on
price evidence alone.

Real result on PROP (2026-06-23, the filing v3 called MIXED): stock fell 35% into
the grant, rose 20% within ten days, +22% vs SPY over 30 days; hurdles at $4.50/
$6.50 are +586%/+891% from the $0.66 grant price, needing 47-58%/yr. The incoming
CEO's tranche carries **no service condition** while the CFO's has three-year
ratable vesting — CEO 8/10, CFO 7/10.

Costs one cheap extraction per filing. Everything else is arithmetic.

## 6. Env / test notes

- Local dev uses SQLite; prod uses Postgres. `sqlite3.Row` supports `row["k"]`
  but NOT `.get()` — convert rows to real dicts before `.get()` (see `CLAUDE.md`).
- Tests: `python -m pytest tests/ -q` (151 passing). No network/LLM needed — all mocked.
- Render MCP loses workspace selection on reconnect; re-select `tea-d5ttm7fgi27c73ebtjvg` (only workspace).
