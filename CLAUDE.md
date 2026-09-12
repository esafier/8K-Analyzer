# 8K Analyzer — Project Instructions

## What this is

A **signal scanner**, not a filing reader. It ranks SEC 8-K filings by how
much they are worth a buy-side investor's attention, and hides the rest.

Two theses drive everything:

- **BEARISH — insiders losing confidence:** sudden C-suite exits (CEO/CFO/CAO
  matter most), no successor named, terminations for cause, acknowledged
  disagreements, departure clusters, and the loudest tell — an executive who
  **forfeits unvested comp** to leave.
- **BULLISH — board conviction via comp design:** vesting hurdles requiring
  real appreciation from today's price, off-cycle or oversized grants, grants
  dated just before a scheduled report, comp shifted to long-vesting at-risk
  equity.

The governing constraint: **precision over recall.** A signal that fires on a
quarter of all filings is worse than no signal, because it trains the user to
stop trusting the feed. That is what killed the previous version.

## Architecture

```
EDGAR search → universe filter → dedupe → fetch text + exhibits
                                      ↓
                     pipeline.analyze_filing()   ← the ONLY analysis path
   1. extract   llm.classify_and_summarize + prompts/prompt_v4.txt (cheap model, facts only)
   2. context   context.build_context()  — price, cap, earnings, cadence, history
   3. detect    signals.detect()         — typed detectors + PASS rules
   4. judge     judge.judge()            — strong model, candidates only (~1 in 4)
   5. persist   pipeline.build_fields()  — new columns + the legacy quartet
                                      ↓
        Inbox (/) · Review (/review) · Digest email · /all (archive)
                                      ↓
        judgments → evaluate.py → few-shot + guidelines → judge
```

**Form 4s enter at stage 2.** `form4.py` reads the XML (already structured —
no extraction model), builds facts, and calls `pipeline.analyze_facts`, the
same function 8-Ks use after extraction. Only open-market buys ≥ $50K by
officers/directors and top-officer grants are scored; rows are stored only
when a detector fires.

**Two feedback loops close the system:** `judgments` (the user's labels, read
by `evaluate.py` and fed to the judge as examples) and `outcomes` (price vs.
SPY at 7/30/90 days, shown on `/scorecard`). Both are updated by the daily
job; `outcomes` is prospective only because the price feed has no history.

**After changing a detector, run `rescore.py`.** It re-ranks every stored
filing from its stored facts and context with zero model calls. Detectors are
deterministic; paying to re-extract to test a threshold change is waste.

**Never add a second analysis path.** filter.py, app.run_resummarize and
app.run_retry_missing_summaries each used to carry their own copy of the field
mapping. They drifted, and the retry path silently lost market-target
detection for months. Everything goes through `pipeline.analyze_filing`.

## Rules that have cost real time

### Database compatibility (SQLite local, PostgreSQL on Render)
- `sqlite3.Row` supports `row["key"]` but **not** `.get()`. PostgreSQL rows
  come back as real dicts, which do. Convert with `dict(row)` before using
  `.get()`. This has caused production bugs three separate times.
- Placeholders differ (`?` vs `%s`) — always use `_placeholder()`.
- `RETURNING id` on Postgres vs `cursor.lastrowid` on SQLite.
- `NOW() - INTERVAL 'n hours'` vs `datetime('now', '-n hours')`.
- The local test fixture is SQLite-only, so none of the above is covered by
  the default suite. `tests/test_postgres_parity.py` runs in CI against a real
  Postgres — add to it when you touch SQL.

### Extraction fields: "mentions X" is a trap
An 8-K that *mentions* a concept is not an 8-K where that thing *happened*.
Both instances of this pattern produced false positives on 20%+ of filings:

- Item 5.02 **requires** companies to state whether a departure involved a
  disagreement, so nearly every one says "was not the result of any
  disagreement". The word is not the event.
- Every employment agreement **defines** "Cause" as a contractual term, so
  the phrase appears in filings where nobody was fired.

Name extraction fields for the event (`disagreement_disclosed`,
`terminated_for_cause`), never for the mention — and guard in code too.

### A signal needs its event in THIS filing
Context signals (departure clusters, company grant history) describe the
company, not the filing. In week one, DEPARTURE_CLUSTER fired on appointment-
only filings and director retirements purely because the company had
history, and OFF_CYCLE_GRANT fired on every new-hire package and severance
payment. 40% of analyzed filings landed in MONITOR. A context signal must be
anchored to an event actually disclosed here — an officer exit, a
discretionary equity grant — or it is company trivia, not a reason to read.

### null ≠ false
In extraction output, `null` means the filing was silent and `false` means the
filing said no. Detectors must not fire on silence — asserting "no successor
named" because a filing didn't discuss succession puts a scary badge on a
filing that never made the claim.

### Windows console
`config.py` reconfigures stdout/stderr to UTF-8 with `errors="replace"`. A
single `✓` in a boot-time print used to kill every CLI with
UnicodeEncodeError before it did any work. Real errors must be loud; a
decorative glyph must never be one of them.

## Conventions

- **Prompts** live in `prompts/` as `.txt` with `{filing_text}` / `{payload}`
  placeholders. `ACTIVE_PROMPT` (config.py) selects the extraction prompt.
- **Signal weights and thresholds** live in `config/signal_weights.json`, not
  in code, so tuning is a data change `evaluate.py` can score.
- **`PIPELINE_VERSION`** (config.py) is stamped on every analyzed filing. Bump
  it when a prompt or the weights change.
- **Models** are env-overridable: `LLM_MODEL` (extraction),
  `LLM_MODEL_JUDGE`, `LLM_MODEL_PREMIUM`. The GPT-5.6 family rejects an
  explicit `temperature` — `llm._chat_kwargs` handles that; don't re-add it.
- **Every signal carries an `evidence` sentence.** It is shown to the user; it
  is the product, not a debug string.

## Verifying a change

```bash
python -m pytest tests/ -q                   # ~500 tests, SQLite (Postgres in CI)
python rescore.py --dry-run                  # re-rank stored filings after a detector change — free
python reanalyze.py --since YYYY-MM-DD --dry-run  # find rows the pipeline never scored
python backtest.py --days 30 --dry-run       # cost estimate first, always
python form4.py --date YYYY-MM-DD --dry-run --no-judge   # Form 4 scan, no spend
python evaluate.py                           # ranking quality vs. the user's labels
python daily.py --date YYYY-MM-DD --dry-run  # one real day, end to end
```

Detector changes must be validated on **real filings**, not only fixtures.
Three false-positive patterns that unit tests could not have caught were found
by running 50 stored filings through detection and looking at what fired.

## Backfills run on Actions, not on a laptop

`backfill.py --start --end` fills a hole (8-Ks then Form 4s); `reanalyze.py`
scores rows an older pipeline version stored, which a backfill *cannot* reach
because Stage 1c dedupe skips accession numbers already stored with text.
Both are wired into `.github/workflows/backfill.yml` (workflow_dispatch,
sharing the daily job's concurrency group so the two never fight for SEC's
rate limit).

Run them there. The 2026-08-20 → 09-03 window was lost twice locally: once to
an OpenAI request with no timeout (now `llm._client()`, 180s, 3 retries), once
to the machine entering Modern Standby mid-run, which freezes the process on
a database socket the server has already dropped. Both failures look
identical from the log's last line — compare the file's **mtime** to now.

## Deploy

- Push to `main` → Render auto-deploys the web service.
- The daily job runs on **GitHub Actions** (`.github/workflows/daily.yml`),
  not on Render — the free tier spins the web service down.
- Migrations are additive and run at boot; rolling back loses no data.
- Secrets live in GitHub Actions and Render only. This repo is **public**:
  `SECRET_KEY` must match in both places (it signs the digest's label links),
  and `labels.py` refuses to sign with the committed fallback value.
