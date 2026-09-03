# Signal-First Rebuild — Design

**Date:** 2026-09-02
**Branch:** `feature/signal-first`
**Status:** Approved

---

## Problem

The tool degraded into "a way to read 8-Ks with a certain item code." Five root causes:

1. **The funnel never narrows.** `prompt_v3` says "bias toward inclusion," near-misses get an LLM
   look, and the LLM's relevance gate asks *"is this about an executive?"* — not *"is this
   interesting?"* Nearly every Item 5.02 lands in the database, so the dashboard is the raw 5.02
   feed with badges on it.
2. **Triage is blind.** The scoring call sees filing text only — no price, market cap, earnings
   date, grant cadence, or departure history. The user's signals are *relative* (off-cycle vs. the
   company's own cadence, hurdle vs. today's price, this exit vs. the last three), so a model
   rating filings in a vacuum can only catch what is spelled out in the text. Scores bunch in the
   middle and stop discriminating.
3. **The best judgment happens after the click.** The context-rich `signal_analyze` call is behind
   a button on the detail page — the smartest read in the system runs only after the user has
   already spent the time it was supposed to save.
4. **Nothing learns.** Watchlist stars, notes, tags, and deep-analysis runs are never read back.
   `test_prompt.py` compares the LLM against keyword labels, not against the user's judgment, so
   no prompt change can be shown to be an improvement.
5. **Nothing runs by itself.** `scheduler.py` exists but `render.yaml` deploys only the web
   service. Every day of data depended on a manual Backfill click, so the feed died when the user
   got busy.

Coverage gap: Form 4 is not ingested, so "off-cycle" and "oversized" grants — central to the
user's bullish thesis — are undetectable because the system has never seen the cycle.

## Goals

- Rank filings by **typed, evidenced signals** instead of one opaque 0–10 score.
- Spend the strong model **only on candidates**, automatically, before the user looks.
- Make judgment **contextual**: price, cap, earnings proximity, cadence, history.
- Close a **feedback loop** so the user's labels measurably improve ranking.
- Run **unattended** every weekday and push a digest.

## Non-goals

- Rebuilding the filing detail page or the watchlist/email-composer flow (they keep working).
- Replacing the SEC fetching, caching, or departure-history machinery — all reused.
- Per-user accounts. Read/label state stays global, as today.

---

## Architecture

```
EDGAR 8-K search → universe filter → already-in-DB check → fetch text + exhibits
                                                    ↓
                                   pipeline.analyze_filing()
   1. extract  (Luna, prompt_v4 — facts only, no verdicts)
   2. context  (price, cap, next earnings, last 2.02, IPO date, acceptance time,
                departures 24mo, grant cadence, 30/90d price change)
   3. signals  (deterministic typed detectors + PASS rules)
   4. judge    (Terra, candidates only; guidelines + few-shot from user labels)
   5. persist  (legacy columns keep the old UI working + new JSON columns)
                                                    ↓
              Inbox (/) · Review (/review) · Digest email
                                                    ↓
              judgments → evaluate.py → few-shot + guidelines → judge
```

### Universe

Market cap ≥ **$50M**; a filing whose ticker is missing or whose market cap is unknown is
**skipped before any text fetch or LLM call**. Skips are counted and logged so a broken market-cap
API is visible rather than silently emptying the feed.

### Signals

Every detector returns a `Signal(type, direction, severity 1-5, evidence, data)`. Severity is
fixed in `config/signal_weights.json` so weights can be tuned without code changes.

**Bearish:** FORFEITURE_EXIT (5) · FOR_CAUSE_OR_DISAGREEMENT (5) · ABRUPT_CSUITE_EXIT (4) ·
NO_SUCCESSOR (3, +1 CEO/CFO) · DEPARTURE_CLUSTER (3 at 2, 4 at ≥3; +1 finance) · EXIT_AFTER_IPO (3)
· RESTATEMENT_CONTEXT (3) · VALUE_EXTRACTION (3) · INSIDER_MONETIZATION (2–3) ·
EXIT_NEAR_EARNINGS (2) · FRIDAY_NIGHT_FILING (1)

**Bullish:** HURDLE_CONVICTION (3 at ≥50% above price, 4 at ≥100%) · OFF_CYCLE_GRANT (3) ·
OVERSIZED_GRANT (3) · PRE_EARNINGS_GRANT (3) · COMP_MIX_TO_EQUITY (2)

**PASS rules (no LLM spend):** annual-meeting-only · merger-completion departure waves · planned
retirement with a successor ≥60 days out · routine annual grants without hurdles · plan
share-reserve / ESPP amendments · financing-only.

### Judgment

A filing becomes a judge candidate when max severity ≥3, or it has ≥2 signals, or extraction set
`is_complex`. The judge receives facts + context + signals + the user's guidelines + up to 4
few-shot examples drawn from past labelled filings that share a signal type, and returns
`{score, direction, verdict, thesis, why[], anti_thesis}`.

Rows the judge never saw are still ranked: signals present → `MONITOR` with score
`min(2 × max_severity, 6)`; no signals → `PASS`. No row is left unrated.

### Persistence

New columns are additive and the legacy quartet (`triage_verdict`, `signal_score`,
`signal_direction`, `top_signal`) is still written, so the existing dashboard, watchlist, email
composer, and detail page keep rendering unchanged during and after the migration. The previous
verdict is snapshotted into `legacy_triage_json` before being overwritten.

### Learning loop

`judgments` records one label per filing (`signal` / `noise` / `meh`) from three sources: the
`/review` page, signed ✓/✗ links in the digest email, and a one-time seed from existing watchlist
stars. `guidelines` holds free-text rules the user writes; they are injected into the judge
prompt. `evaluate.py` reports precision@K and per-signal-type precision against the labels, which
is what makes any prompt or weight change measurable.

### Automation

A GitHub Actions cron (public repo, free minutes) runs `daily.py` each weekday: ingest the window
implied by the `ingested_through` watermark (so Fridays are never skipped and windows never
overlap), analyze, then send the digest. The job fails loudly if more than 20% of SEC fetches
fail, so a silent block cannot look like a quiet news day.

## Error handling

- SEC 403 is treated like 429 (shared CI egress IPs get blocked, and a 403 previously parked every
  filing as "pending retry" while the run reported success).
- LLM failure on extraction keeps the filing with no signals rather than dropping it.
- Judge failure falls back to the detector-derived verdict and score.
- Unknown market cap → skip (a filing is never analyzed on the assumption it qualifies).
- Every stage records `pipeline_version` so a regression can be traced to a prompt or weight set.

## Testing

Unit tests per detector with hand-built facts/context fixtures; contract tests for `parse_*`
helpers; route tests for the new inbox, review, and label endpoints; a Postgres CI job in addition
to the SQLite suite, because the dual-dialect SQL (`%s`, `RETURNING`, `INTERVAL`) is invisible to
a SQLite-only fixture. All 176 existing tests must stay green.

## Rollback

Everything lands on `feature/signal-first`. Migrations are additive, so rolling `main` back on
Render loses no data. `legacy_triage_json` preserves the pre-rebuild verdicts.
