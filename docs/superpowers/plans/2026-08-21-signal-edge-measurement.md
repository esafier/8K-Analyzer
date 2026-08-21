# Does the signal have an edge? — first measurement

**Date:** 2026-08-21
**Method:** `backtest_signals.py`, read-only, against the live Render archive
**Sample:** 433 filings, 401 distinct tickers, filed 2026-05-04 → 2026-07-10
**Reproduce:** `DATABASE_URL=... python backtest_signals.py --horizon 30`

## Why this exists

The prompt was hedging: of 1,084 scored filings in the archive, 899 (83%) came
back NEUTRAL or MIXED. `prompt_v4.txt` was written to stop that — it adds a
dominance order and a consistency rule ("a score of 7 or above requires
BEARISH or BULLISH").

Before switching `ACTIVE_PROMPT` to v4, the obvious question: *is the direction
label worth committing to in the first place?* v3's calls are already stored,
and Yahoo has the prices. That makes it answerable today, with no API key, no
writes and no deploy.

## Headline

**On this sample, neither the direction nor the score shows a measurable edge.**

30-day excess return vs SPY, n=414 priced:

| direction | n | hit rate | 95% CI | base rate | edge |
|---|---|---|---|---|---|
| BULLISH | 121 | 40.5% | [32.2%, 49.4%] | 42.3% | −1.8pp |
| BEARISH | 53 | 60.4% | [46.9%, 72.4%] | 57.7% | +2.6pp |

7-day, n=413:

| direction | n | hit rate | 95% CI | base rate | edge |
|---|---|---|---|---|---|
| BULLISH | 121 | 46.3% | [37.6%, 55.1%] | 49.9% | −3.6pp |
| BEARISH | 53 | 52.8% | [39.7%, 65.6%] | 50.1% | +2.7pp |

Every interval straddles the base rate.

## The base rate is the number to beat, not 50%

Only 42.3% of these filings beat SPY over 30 days. In a sample that drifts down,
a label that says BEARISH about everything scores 57.7% while knowing nothing.
So 60.4% for BEARISH is not "better than a coin flip" — it is the base rate,
give or take noise.

The 7-day base rate lands at 49.9%/50.1%, which is a useful check on the
measurement itself: over one week these names were a coin flip against SPY, as
they should be. A broken benchmark join would not produce that.

## The high-conviction lift was a confound

A first pass showed score 7–8 hitting 58.8% against 41.2% for score 5, which
looks like the score working. It isn't. The score bands have wildly different
direction mixes:

| score band | n | BEARISH share |
|---|---|---|
| 3–5 | 110 | 22.7% |
| 6 | 30 | 10.0% |
| 7–8 | 34 | 73.5% |

Score 7–8 is three-quarters bearish in a window where 57.7% of names lagged
SPY. The bucket inherits the drift. Held *within* one direction the lift is
gone:

| direction | score | n | hit | 95% CI | base |
|---|---|---|---|---|---|
| BULLISH | 3–5 | 85 | 37.6% | [28.1%, 48.3%] | 42.3% |
| BULLISH | 6 | 27 | 48.1% | [30.7%, 66.0%] | 42.3% |
| BULLISH | 7–8 | 9 | 44.4% | [18.9%, 73.3%] | 42.3% |
| BEARISH | 3–5 | 25 | 64.0% | [44.5%, 79.8%] | 57.7% |
| BEARISH | 7–8 | 25 | 64.0% | [44.5%, 79.8%] | 57.7% |

`backtest_signals.py` prints the within-direction table and the mix table side
by side so this specific mistake is hard to make twice.

## The score doesn't track magnitude either

Median |excess| at 30 days by score: 9.2% (4), 10.3% (5), 12.8% (6), 8.4% (7),
10.3% (8). Flat. A 8/10 filing does not move further than a 5/10 filing.

## The extracted facts don't separate either

The facts/judgment split predicts that the *facts* — read straight off the
filing — should carry information even where the judgment doesn't. They don't,
on this sample. Every arm's CI overlaps both the base rate and its complement:

| fact | value | n | beat SPY (30d) | 95% CI |
|---|---|---|---|---|
| forfeited_comp | yes | 29 | 44.8% | [28.4%, 62.5%] |
| forfeited_comp | no | 385 | 42.1% | [37.2%, 47.1%] |
| has_successor | yes | 103 | 39.8% | [30.9%, 49.5%] |
| has_successor | no | 75 | 41.3% | [30.9%, 52.6%] |
| cluster (≥2 in 24mo) | yes | 116 | 45.7% | [36.9%, 54.7%] |
| cluster (≥2 in 24mo) | no | 39 | 38.5% | [24.9%, 54.1%] |
| has_market_targets | yes | 33 | 36.4% | [22.2%, 53.4%] |
| has_market_targets | no | 381 | 42.8% | [37.9%, 47.8%] |

The largest-looking gap (cluster at 7 days, 50.9% vs 34.2%) has overlapping
intervals, n=38 in the smaller arm, and points the *wrong way*: clustered
departures are supposed to read bearish, and the clustered group did better.

## What this does and does not license

**Does not license shipping v4 as a fix for hedging.** v4 forces a direction
whenever the score reaches 7. If the underlying direction call carries no
information, that converts a visible hedge into an invisible coin flip — the
output looks more confident and is not more correct. That is a strictly worse
failure mode, because a hedge is at least legible as one.

**Does not condemn v4 either.** This measures v3's stored labels. v4 has never
been run. The right test is to run v4 over the same archive and put its
direction through this same table. That needs an API key; the harness is ready.

**Does not condemn the spring-load screen.** That makes a different kind of
claim: it is a mechanical statement about issuer behaviour (grant struck after
a run-in, hurdles requiring implausible CAGR, no service condition), scored by
deterministic Python rather than by model judgment. It is untested, not
disproven — and it is the one part of the pipeline this result gives a reason
to prefer.

**Does confirm the outcome tracker was worth building.** This is the first
falsifiable statement the project has ever made about itself, and it took an
afternoon of read-only queries once the machinery existed.

## Caveats, stated plainly

- **One regime, six weeks.** 2026-05-04 → 2026-07-10. Nothing here generalises
  to a different market.
- **90-day horizon untestable.** The archive starts too recently; only 6
  filings are old enough. 90d is where a governance signal would most plausibly
  show up.
- **Small arms.** BEARISH is n=53; BULLISH score 7–8 is n=9.
- **Multiple comparisons.** Roughly a dozen buckets were examined. At that
  count one "significant" result would be expected by chance — and none
  appeared, which is the finding.
- **Survivorship.** 19 of 433 filings could not be priced (delisted or renamed
  tickers). They are excluded, not zeroed, but a delisting is not a neutral
  outcome and their absence tilts the base rate upward.
- **`has_successor` is only populated on 480 of 1,049 rows**, so that arm tests
  a subset selected by whenever the column started being written.

## What would change the answer

1. Run v4 over the same archive and compare direction quality head to head.
2. Wait for 90-day windows to mature (November 2026 for the June filings) and
   re-run — governance effects are slow.
3. Score the spring-load screen the same way once it has produced calls.
4. Get more BEARISH samples; n=53 is too thin to rule out a real effect of the
   size that would matter.

---

# Second measurement: does the price path alone find spring-loading?

**Added:** 2026-08-21, same session
**Tool:** `screen_grant_timing.py`, no LLM, no API key
**Sample:** 197 saved (watchlist) filings, 2026-02-02 → 2026-07-01, vs a
200-filing control of same-period 5.02 filings that were *not* saved

## Why this was worth running without a key

The spring-load screen has two halves. One reads the filing with an LLM to find
the grant, the recipient, the strike and the hurdles. The other is arithmetic on
the tape: down into the grant, sharp move out of it, strike struck on the
month's cheapest close. The arithmetic half needs nothing but prices, so it can
sweep every saved filing for free.

The hope was a cheap shortlist — the filings worth spending LLM calls on.

## It produced a shortlist, and the shortlist is worthless

Seven of 190 scored watchlist filings reached 4+ of 6 points, led by KD at 6/6
(−61.1% run-in, +28.3% pop, month's cheapest close, +22.1% vs SPY over 30 days).
That reads like a smoking gun.

Then the control:

| threshold | saved filings | control (not saved) |
|---|---|---|
| ≥3 points | 18/190 = 9.5% [6.1, 14.5] | 22/186 = 11.8% [7.9, 17.3] |
| ≥4 points | 7/190 = 3.7% [1.8, 7.4] | 13/186 = 7.0% [4.1, 11.6] |
| ≥5 points | 2/190 = 1.1% [0.3, 3.8] | 5/186 = 2.7% [1.2, 6.1] |

Two-proportion z on ≥4 points: **−1.43**. Mean points 0.96 vs 0.94.

A random 5.02 filing is *more* likely to show the classic spring-load price
shape than one you flagged as interesting, and the difference is not
significant either way. The control's own 6/6 is TNDM; its 5/6 list includes
SMCI and AXON.

**The price path on its own is a volatility measure, not evidence of grant
timing.** Down-then-up over 60 days is what any volatile microcap does several
times a year.

## What this means for the spring-load screen

It does *not* invalidate `spring_load.py`. It localises where that module's
discrimination actually lives: entirely in the LLM extraction half — whether a
grant exists at all, whether it was off-cycle, whether it lacks a service
condition its peers in the same filing carry, whether the hurdles imply an
implausible CAGR. The price path is confirmatory, exactly as `price_path`'s
docstring claims, and it cannot be promoted to primary evidence to save an API
call.

Concretely: running the spring-load screen without the LLM half is not a cheap
approximation of it. It is a different, useless thing.

## The check is now part of the tool

`screen_grant_timing.py --control` screens a same-period sample of unsaved
filings and prints both distributions with confidence intervals and the
two-proportion z. When the sets are indistinguishable it says so in plain
language rather than leaving a suggestive ranking to speak for itself.

This exists because the ranking without it is a trap: 6/6 reads as damning, and
the number alone gives the reader no way to know that 7% of random filings hit
the same mark.

## Caveats

- **Anchor is the filing date, not the grant date.** Item 5.02 is due within
  four business days of the event, so the anchor sits 0–4 business days late.
  That biases the pop *downward*, so it under-counts rather than over-counts —
  but it also means a genuinely well-timed grant could be missed, which weakens
  the null result slightly. The LLM extraction supplies the true grant date and
  would tighten both arms.
- **The watchlist is not a random sample of anything.** It is what one reader
  found interesting, so "saved" encodes no ground truth about spring-loading.
  The comparison establishes that the price screen adds nothing to that
  selection, not that the selection was wrong.
- **Same six-week-plus regime problem** as the first measurement, over a
  slightly wider Feb–Jul window.
- 6 watchlist and 19 control filings had no usable price data (delisted or
  renamed) and are excluded from both denominators.
