# HANDOFF — read me first (session continuity)

**Purpose:** catch a new session up on state that lives in Render, GitHub, and
past conversation rather than in code.

**Last updated:** 2026-09-12
**Branch:** everything is on `main` (Render auto-deploys from it).

---

## 1. What this is and why it was rebuilt

A ranked signal inbox over SEC 8-Ks (and now Form 4s) for a buy-side investor
hunting two theses: insiders losing confidence (abrupt C-suite exits,
forfeited comp, clusters) and board conviction via comp design (hurdles far
above the price, off-cycle or oversized grants, insider buying).

The old version was abandoned because it was "a way to read 8-Ks with a
certain item code": nothing narrowed, scores were blind to price and history,
nothing learned, nothing ran by itself. The rebuild is documented in
`docs/superpowers/specs/2026-09-02-signal-first-design.md`; the architecture
and the rules it paid for are in `CLAUDE.md`.

## 2. Live state (2026-09-11)

- **Web:** https://eightk-analyzer.onrender.com — no login (TRIAL_CODE unset
  by the user's choice). Pages: `/` Signals, `/review`, `/scorecard`, `/all`,
  `/watchlist`, `/backfill`.
- **Daily job:** GitHub Actions `daily.yml`, weekdays ~11:30 UTC. Ingests the
  watermark window of 8-Ks, scans Form 4s for the same window, records and
  marks outcomes, then sends the digest. Green every weekday since 2026-09-04.
- **Database:** Render Postgres `dpg-d5ttp9aqcgvc73ev04dg-a` (basic_256mb, no
  expiry). ~4,900 filings, Jan 29 – present, with a hole 2026-07-11 → 08-19.
  The 08-20 → 09-03 backfill was re-run on 2026-09-12 via
  `.github/workflows/backfill.yml` after two local attempts stalled (see
  below). Gap backfills belong on Actions now — `backfill.py` and
  `reanalyze.py` are the entry points, and the workflow shares the daily
  job's concurrency group.
- **2026-09-03 needed a separate repair.** The first daily run ingested that
  day under the pre-rebuild code: 46 rows have text and a summary but no
  structured_summary, signals, or verdict, so they never appeared in the
  ranked inbox. The gap backfill cannot reach them — Stage 1c dedupe skips
  accession numbers already stored with text. `scratchpad/reanalyze_missing.py`
  re-runs `pipeline.analyze_filing` on exactly the unscored rows.
- **Secrets:** GitHub Actions has OPENAI_API_KEY, API_NINJAS_KEY, DATABASE_URL
  (external), SECRET_KEY. Render has the same SECRET_KEY (it signs digest
  label links — the two must match).
- **Labels:** 203, all seeded from the old watchlist stars. None from /review
  yet.

## 3. Measured, not estimated

- A live day: ~270 8-Ks fetched → ~180 in item scope → ~120 in universe →
  ~30–60 analyzed.
- First production week (160 filings): 40% landed in MONITOR. Root cause was
  two context signals firing without an event in the filing (see CLAUDE.md,
  "A signal needs its event in THIS filing"). After the fix and a free
  rescore: 41 MONITOR / 111 PASS / 8 DEEP_LOOK.
- Form 4 scan, one live day (2026-09-10): 1,076 Form 4s → 297 at known
  issuers → 11 signals (discretionary CEO buys at Uber $10.0M, Upstart $1.3M,
  Alphatec $1.0M, Celsius $494K).
- Cost: extraction ≈ $0.28 per 100 8-Ks; judge ≈ $0.03 per candidate; Form 4s
  need no extraction.

## 4. Still waiting on the user

1. **Digest email is not configured.** Every run prints the digest to the job
   log instead of sending it. Add GitHub secrets `DIGEST_SMTP_USER` (gmail
   address), `DIGEST_SMTP_PASS` (a Google *app password*), `DIGEST_TO`.
2. **Label in /review.** ~100–150 labels make evaluate.py and the judge's
   few-shot examples meaningful. Start with the DEEP_LOOKs.
3. **Rotate the Render API key** — it was exposed in the session transcript.
4. **Backtest (optional, ~$7):** `python backtest.py --days 100` covers
   May 26 – Jul 10. Deliberately not run; decide after checking OpenAI
   credits.

## 5. Known limits

- **A long job's log tail lies.** The first gap backfill sat inside one hung
  OpenAI request for 16 hours — process alive, last log line a normal
  "Stage 3: analyzing 32/489". The client had no timeout. Fixed in `f90f14b`
  (`llm._client()`: 180s, 3 retries, both env-overridable), but the habit
  matters more than the fix: compare the log's **mtime** to now before
  believing a job is progressing.

- Outcomes are prospective: the price API has no history, so a missed daily
  run means a mark taken a day late (never lost).
- The Form 4 scan covers only issuers already in the database.
- Two insiders buying the same week in separate Form 4s appear as two rows;
  the breadth bonus in INSIDER_BUY only sees buyers within one filing.
