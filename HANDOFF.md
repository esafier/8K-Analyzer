# HANDOFF — read me first (session continuity)

**Purpose:** catch a new session up on state that lives in Render, GitHub, and
past conversation rather than in code.

**Last updated:** 2026-09-03
**Working branch:** `feature/signal-first` (the signal-first rebuild; `main` is
still the pre-rebuild version)

---

## 1. What happened and why

The user stopped using the tool around July 2026. Their words: they still had
to click into many filings, the triage score "is not very helpful", and it
"feels like a way to read 8-Ks with a certain item code".

Diagnosis (see `docs/superpowers/specs/2026-09-02-signal-first-design.md`):

1. The funnel never narrowed — `prompt_v3` biased toward inclusion and the
   relevance gate asked "is this about an executive?", not "is this
   interesting?", so the dashboard was the raw 5.02 feed with badges.
2. Triage scored filings blind — no price, cadence, earnings date, or
   departure history. Nearly every signal the user hunts is *relative*, so
   scores bunched in the middle.
3. The best analysis (`signal_analyze`) ran only after a click.
4. Nothing learned: stars, notes, and tags were never read back.
5. Nothing ran automatically — `scheduler.py` existed but nothing invoked it.
6. Form 4 was never ingested, so "off-cycle" and "oversized" were undetectable.

## 2. What the rebuild does

`EDGAR → universe gate → dedupe → extract (cheap) → context → typed detectors
→ judge (strong, ~1 in 4) → ranked inbox + digest → labels → evaluation`

Read `CLAUDE.md` for the architecture and the rules that came out of it. The
single most important one: **there is one analysis path**
(`pipeline.analyze_filing`). Three used to exist and they drifted.

## 3. Ground truth measured this session (not estimated)

- Live day 2026-08-27: 185 filings → 116 (item codes) → 72 (universe;
  10 no-ticker, 6 unknown cap, 28 below the $50M floor) → 52 analyzed.
- Judge gate opens on ~30% of analyzed filings.
- Extraction ≈ $0.28 per 100 filings (gpt-5.6-luna). Judge ≈ $0.03/filing
  (gpt-5.6-terra). A 200-filing backtest estimated $1.17.
- 408 tests pass locally (SQLite); 14 Postgres parity tests run only in CI.

## 4. Four false-positive patterns found by running REAL filings

Unit tests with hand-built fixtures could not have caught any of these. Each
is now both a prompt rule and a code guard, with a regression test.

1. **Disagreement boilerplate.** Item 5.02 requires companies to address
   whether a departure involved a disagreement, so nearly every one contains
   "was not the result of any disagreement". The extractor set the flag true
   on the first real filing tested.
2. **"Cause" defined vs. invoked.** Every employment agreement defines Cause
   as a contractual term. Fired on 4 of 20 filings against a real base rate
   nearer 1–2%.
3. **COMP_MIX_TO_EQUITY on 27% of filings** — that is simply what an annual
   PSU grant looks like at any large company.
4. **Lone Friday-night filings** produced inbox rows whose entire thesis was
   "Accepted by SEC after Friday's close".

**If you add a detector, run it over ≥30 stored filings and look at what
fires before believing it.**

## 5. What still needs the user (nothing blocks the build)

1. **Render → `8k-analyzer` → Settings → Build & Deploy → Branch** — confirm
   it is `main`. An older handoff said it was pointed at
   `claude/project-improvement-review-ne5ryf`; if that is still true, merging
   to `main` deploys nothing.
2. **GitHub secrets** (repo → Settings → Secrets and variables → Actions):
   - `DATABASE_URL` — Render → `8k-analyzer-db` → *External* Database URL
   - `SECRET_KEY` — must be the SAME value as on Render; it signs the digest's
     label links, which the web app has to verify
   - `OPENAI_API_KEY`, `API_NINJAS_KEY`
   - Optional: `DIGEST_SMTP_USER` / `DIGEST_SMTP_PASS` / `DIGEST_TO` (Gmail
     app password), or `DIGEST_SLACK_WEBHOOK`. Without them the digest
     dry-runs to the job log.
3. **Label ~100–150 filings** in `/review` after deploy. Everything in
   `evaluate.py` and the judge's few-shot examples depends on them.

`RENDER_API_KEY` was NOT set in this session's environment, so the Render MCP
returned "unauthorized" and no production state could be read or changed.
Setting it (`setx RENDER_API_KEY ...`, then relaunch) removes items 1–2.

## 6. Deployment shape

- Web service on Render, auto-deploys from `main`. Migrations are additive and
  run at boot, so a rollback loses no data.
- The daily job runs on **GitHub Actions**, not Render — the free tier spins
  the web service down. `.github/workflows/daily.yml`, weekday 11:30 UTC, plus
  `workflow_dispatch`.
- `.github/workflows/tests.yml` runs the suite on SQLite *and* against a real
  Postgres service container.

## 7. Deferred (Phase 2)

- **Form 4 daily scan** (`form4.py`): insider buys and off-cycle C-suite
  grants as their own inbox rows. Grant *cadence* already works via the API
  Ninjas insider endpoint (`context._grant_cadence`), which is what
  OFF_CYCLE_GRANT and OVERSIZED_GRANT need; the daily scan is additive.
- **Outcome tracking** (`outcomes.py` + `/scorecard`): 7/30/90-day return vs.
  SPY per signal type — learning from the market, not only from the user.
- **Judge-panel spot check** of the top-30 ranked filings.
