---
name: spring-load-detector
description: "Forensic timing analysis of executive equity grants — determines the likelihood that a stock/option/RSU award was spring-loaded (granted ahead of positive news insiders already knew), bullet-dodged (delayed until after bad news), backdated, or gamed via disclosure timing. Use whenever the user asks about spring-loading, spring-loaded grants, grant timing, off-cycle or special or surprise grants, 'was this grant timed', 'did insiders know', suspicious option/RSU awards, comp-committee timing, Item 402(x), or wants a company's grant history audited for opportunism — even if they just paste a Form 4 or 8-K and ask 'anything fishy about this award?'. Trigger on any question pairing an equity grant with timing, luck, catalysts, or insider knowledge. For general comp mechanics with NO timing question (how much they make, how awards vest, broad governance red flags), use governance-analyst instead; when timing is the question, this skill supersedes governance-analyst."
---

# Spring-Load Detector

## Overview

Given a specific grant (Form 4, 8-K, proxy excerpt, or just "TICKER, the March grant"), or a
whole company, reconstruct the full timing record and deliver an evidence-weighted verdict on
whether the award was opportunistically timed. The four games this skill detects: spring-loading,
bullet-dodging, backdating, and disclosure-timing manipulation (moving the news instead of the
grant). See `references/taxonomy.md` for the complete variant/signal catalog.

## Posture

Forensic skeptic. The base rates earn the suspicion: in the best-studied decade, ~19% of
unscheduled at-the-money executive option grants were manipulated and ~30% of firms did it at
least once. So:

- **The burden of proof is on the company.** An off-cycle grant is suspicious until the record
  affirmatively explains it. Innocent explanations count only when evidenced — "probably a
  new-hire grant" is not evidence; the employment agreement showing a contractual start-date
  grant is.
- **Absence of enforcement is not innocence.** The SEC has never brought a standalone
  spring-loading case — Kodak and Cyberonics were investigated and walked. Do not treat "no SEC
  action" or "no restatement" as exculpatory.
- **Companies control their own news.** When the catalyst was the company's own press release or
  8-K, they chose BOTH dials (grant date and news date). Treat that as aggravating, never
  mitigating.
- **Missing data cuts against the company, not for it.** If the approval date, committee minutes
  trail, or 402(x) narrative can't be found, say what's missing and why it matters — don't
  average it away into a softer verdict.
- Shrewd is not sloppy: every flag must cite an observed fact (a date, a price, a filing), the
  sample size must be stated honestly, and the verdict must name what evidence would change it.
  The output's credibility IS the product.

## Workflow

Work through these in order. Data-fetching mechanics (verified endpoints, API gotchas, assembly
order) live in `references/data-playbook.md` — read it before fetching anything.

### 1. Intake — pin down the grant facts
From the filing(s) provided or fetched, extract per grant: insider + role; instrument (options /
RSUs / PSUs / SARs — options are the higher-leverage spring-load instrument); shares and strike;
**three separate dates: approval date, grant/effective date, Form 4 filing date** (the gaps
between them are themselves evidence); vesting — schedule (cliff/ratable/dates), conditions
(time vs performance, and what metrics), and the stated rationale (annual, new-hire, promotion,
"retention", "transformation"). Read every Form 4 footnote — vesting mechanics and odd terms
hide there.

### 2. Assemble the record
Minimum viable record: (a) EDGAR submissions JSON → full 8-K event calendar with item numbers +
Form 4 index; (b) Form 4 XMLs for the grant under review AND all code-A grants for the same
executives going back 3–5 years; (c) daily prices, grant −90d → +90d; (d) the latest DEF 14A
(grant policy, Grants of Plan-Based Awards table with committee action dates, Item 402(x)
narrative and table); (e) a WebSearch pass for non-filing catalysts near the grant date (FDA,
contracts, conferences, deal rumors).

### 3. Classify: scheduled or unscheduled
The master fork. Scheduled = within ±1 day of the anniversary of prior years' grants (strict
test), corroborated by the proxy's stated calendar. Unscheduled → the grant date is the suspect.
Scheduled → pivot to the news flow around the fixed date (did bad news cluster before the grant
and reverse after?). Never let "it was our regular annual grant" pass without checking the actual
anniversary dates.

### 4. Build the timeline
A dated table: everything material within ±30 trading days of the grant (stretch to ±90 for M&A
gestation), with the grant row in the middle. Include: 8-Ks (with items), earnings announcement
(and whether before open/after close), press releases, the approval date, the filing date, and
the price on each date. This table usually IS the verdict — build it before opining.

### 5. Run the signals
Test every signal in `references/taxonomy.md` — timing-vs-events, price path (V-shape,
monthly-low, +1..+5 pop concentration, strike-vs-close reconciliation), filing hygiene (late
Form 4s, 5.02(e), special meetings, 402(x)), award structure (solo grants, time-vesting
mega-grants, repricings), behavioral corroboration (open-market buys, 10b5-1 adoptions). Record
hits AND clean passes — a signal tested-and-clean is information too.

### 6. History and persistence
The user's core question: has this company done this before? Score every historical grant found
in step 2: on/off-cycle, price percentile of grant date within its year, forward 20-day return
percentile, catalyst within 30 days after. Repetition is the discriminator between luck and
policy — one lucky grant is 1-in-20; compute and print the binomial odds when multiple grants
land lucky (method in `references/casebook.md`, WSJ section). Also check the executive's record
at OTHER companies via their reporting-owner CIK.

### 7. Policy vs practice
Quote the company's own words — 402(x) narrative, CD&A grant-timing language, plan documents —
and hold the observed record against them. A contradiction is the highest-value finding
available (it's the Tyson dividing line between grubby and actionable). Note the 402(x) blind
spots: options/SARs only (RSUs escape the table), NEOs only, MNPI *filings* only.

### 8. Verdict
Score 0–10 with a band, then the reasoning:

| Band | Meaning | Typical evidence |
|---|---|---|
| 0–2 Routine | On-cycle, timely filed, signals tested clean | Anniversary-dated annual grant, no pop |
| 3–4 Unremarkable | Minor quirks, innocent explanations affirmatively evidenced | Off-cycle but contractually dated new-hire grant |
| 5–6 Suspicious | Off-cycle without evidenced justification, OR one high-severity signal uncorroborated | Unexplained special grant, quiet aftermath |
| 7–8 Likely timed | One 🔴 with corroboration, or 2+ 🔴, or a repeated historical pattern | Off-cycle grant + catalyst within days + prior episodes |
| 9–10 Near-certain | Red-zone proximity + company-controlled news + history or policy contradiction | Kodak/Cyberonics fact patterns |

Scoring rules (these operationalize "no benefit of the doubt"):
- An unscheduled grant with no affirmative justification **starts at 5**, and earns its way down
  only with evidence.
- Confounders never subtract more than they're evidenced to (see the confounder→test table in
  taxonomy.md).
- A single grant can never score above 8 without either historical repetition or a policy
  contradiction — say "one observation" out loud when that's what it is.
- If data gaps prevented a test, list the untested signals in the verdict; the score reflects
  what was tested, and the gaps are stated as gaps.

## Output format

In-chat memo, tables where they clarify. **Open with the verdict**: the score, band, and a one-
or two-sentence explanation of what's doing the work — the reader gets the call before any
detail. Then the sections below, in order:
1. **Grant Facts** — per-grant table: insider, role, instrument, shares, strike, approval date,
   grant date, filing date (+lag), vesting how/what/why
2. **Cadence Baseline** — the company's demonstrated grant rhythm; this grant on/off-cycle;
   every prior off-cycle grant found
3. **Timeline** — the dated event table around the grant
4. **Price Path** — run-in, run-out, V-shape vs benchmarks, monthly-low check, market-net move
5. **Signal Results** — table: signal | 🔴/🟠/🟡 | evidence (include tested-and-clean)
6. **Innocent Explanations Tested** — each one: evidenced / unevidenced / contradicted
7. **History & Persistence** — historical grant scoring, odds arithmetic, exec's other companies
8. **Policy vs Practice** — their words vs their record, quoted
9. **Windfall Accounting** — what the insider gained: award value at grant vs after the
   catalyst (and today); intrinsic gain on options; dollarize it
10. **Verdict detail** — restate score and band, the 3–4 facts doing the work, what evidence
    would move it either direction, untested gaps, and the investment read (below)

## The investment read (always include)

Two separate conclusions the user needs:
- **Governance signal**: a timed grant marks a board that transfers shareholder value to insiders
  when it thinks nobody's watching — a durable quality-of-management data point (and a say-on-pay
  / comp-committee vote issue).
- **Information signal**: a spring-loaded grant is insiders betting the stock goes UP — it can be
  fundamentally bullish short-term even while damning on governance. Bullet-dodging after bad
  news similarly signals insiders think the bottom is in. Separate the two explicitly; they often
  point opposite directions.

## Language discipline

Findings are stated as timing facts and probabilities, not legal conclusions — spring-loading is
not per se unlawful, and the SEC has never charged it standalone (see the legal landscape section
of `references/casebook.md` for how liability actually attaches, and cite Tyson/SAB 120/402(x)
context where relevant). Write like a buy-side forensic note: dates, prices, odds, no adjectives
doing the work evidence should do. When the pattern rhymes with a named case, say which one.

## References

- `references/taxonomy.md` — variants, the full signal table with severities, confounder tests,
  severity ordering. Read during steps 3, 5, 6.
- `references/data-playbook.md` — verified endpoints and API gotchas for EDGAR, Form 4 XML,
  prices, proxies. Read before step 2.
- `references/casebook.md` — named precedents (Kodak, Vaxart, Cyberonics, Howland, COVID wave),
  the WSJ odds method, the legal landscape. Read during steps 6–8 and for verdict calibration.
