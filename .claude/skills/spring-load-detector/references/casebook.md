# Casebook — real precedents and what the timing looked like

Use these to calibrate severity. When a grant under review resembles one of these fact patterns,
say so by name — "this rhymes with Kodak" is more useful to the user than an abstract score.

## Eastman Kodak (July 2020) — the marquee modern spring-load

Exec. Chairman granted 1.75M options on **July 27, 2020 — one trading day before** announcement of
a $765M government loan for pharma production. Stock rose ~1,500%; options worth ~$30M within days.
Internal review blamed an "overwhelmed GC" and outdated grant policies; SEC probed but never
brought an enforcement action over the grant itself.
**Lessons for detection:** T-1 day proximity to a company-known catalyst; off-cycle timing; the
grant was real-time visible in the Form 4 before the news broke. Also a lesson in outcomes: even
egregious timing often ends in embarrassment, not enforcement — so absence of enforcement is NOT
evidence of innocence.
Sources: CNN 2020-12-07 coverage of the internal review; House Oversight letters.

## Vaxart (June 2020) — spring-load ahead of a government-program press release

Insiders received options weeks before an "Operation Warp Speed" press release; award value went
~$4.3M → $28M+. NYT investigation ("Corporate Insiders Pocket $1 Billion in Rush for Coronavirus
Vaccine", July 2020). Delaware derivative suit *In re Vaxart, Inc. Stockholder Litigation* (Del.
Ch. 2021): spring-loading claims partly survived dismissal.
**Lessons:** the catalyst was a press release the company controlled the timing of — controlling
BOTH the grant date and the news date is the aggravated form. Window was ~2 weeks, not 1 day —
distance alone doesn't clear a grant.

## COVID-trough grant wave (March–April 2020)

Afzali, Khan & Rajgopal, "Sharing the Pain?" (SSRN 4053005): executives who took publicized salary
"pay cuts" were **1.5x more likely to receive unscheduled option grants**, opportunistically timed;
average gain ~$444,774 per grant within 60 trading days. Congress held a Sept 2020 hearing on it.
**Lessons:** (1) a market-wide crash is a spring-loading *environment* — scrutinize any off-cycle
grant made into a drawdown; (2) optics management (salary cut + quiet mega-grant) is itself a
pattern — check whether a grant coincides with a publicized sacrifice.

## Novavax / Moderna (2020) — variants

Novavax repriced/reconfigured options to pay out on Phase 2 trial entry (~$100M potential) —
repricing near lows and milestone-triggered payouts set just before likely milestones are
spring-loading in different clothes. Moderna insiders sold $100M+ around announcements (exit-side
timing rather than grant-side).

## Cyberonics (2004) — the special-meeting tell

Board granted the CEO 150,000 options at a special meeting held the **evening of** a favorable
FDA advisory panel vote, priced at the prior day's close ($15.23); stock hit $34.81 the next day.
SEC investigated; executives departed; no charges.
**Lessons:** an approval meeting convened on an unusual date, hours before news the company knew
was coming, is the smoking-gun version of the written-consent flag. Reconstruct WHEN the approval
happened, not just the grant date.

## Howland v. Kumar (Del. Ch. 2019) — repricing + news-sitting

Directors learned Aug 3, 2017 that a patent would issue Aug 22; repriced underwater options at
$0.67 on Sept 6; announced the patent Sept 18; stock rose ~85%. Loyalty claims survived; entire
fairness applied.
**Lessons:** (1) repricing is a grant for timing purposes; (2) sitting on good news until after
the pricing date is the disclosure-timing variant — check the gap between when the company
plausibly KNEW and when it TOLD.

## The legal landscape (calibrate your language with this)

- **The SEC has never brought a standalone spring-loading enforcement action.** Analog Devices
  (2008) is the closest — and the SEC expressly declined to charge the pre-good-news grants.
  Backdating, by contrast, produced real cases (Brocade/Reyes; UnitedHealth's $468M McGuire
  settlement; Comverse) because it involves falsified records. Implication for output language:
  call timing patterns what they are, but don't imply illegality — the exposure is fiduciary
  (Delaware) and accounting (SAB 120), not classic insider trading.
- **In re Tyson Foods (Del. Ch. 2007)**: spring-loading while proxy disclosures implied
  market-priced grants supports a non-exculpable bad-faith claim — "intended to conceal a pattern
  of unfairly stocking up insiders' larders." **Weiss v. Swanson (2008)** extends the same theory
  to bullet-dodging. **Ryan v. Gifford (2007)**: a statistical timing analysis (nine grants at
  monthly/annual lows) was enough to survive dismissal.
- **The dividing line in every case is disclosure/concealment**, not the timing itself. That is
  why the policy-vs-practice contradiction check is the highest-value test in this skill: a
  company that says "we do not time grants" while its Form 4 record shows otherwise has converted
  lawful-but-grubby timing into a potential misrepresentation.
- **The "board knew" defense** (spring-loading isn't insider trading because the board granting
  the award has the same MNPI — it "cannot deceive itself") has limits: it fails when shareholders
  were misled (Tyson), when management drove the timing without full board knowledge (Howland),
  and post-SAB 120, when the grant was booked at unadjusted fair value (understated comp expense
  = misstatement exposure without any trading theory).
- **Proxy advisors**: Glass Lewis recommends against all comp-committee members where
  backdated/spring-loaded options reflect lax controls; ISS codifies problematic option-grant
  practices as say-on-pay strikes. A confirmed pattern has real vote consequences.

## WSJ "Perfect Payday" (2006) — the statistical method that generalizes

Forelle & Bandler ranked each company's actual grant dates against alternative dates and computed
the odds the observed pattern was luck (Monte Carlo). Famous results: 1-in-300 billion odds at
Affiliated Computer Services. Pulitzer for Public Service; ~140 companies investigated, dozens of
executives fired.
**The generalizable single-company version:** for each historical grant, percentile-rank the
grant-date closing price (and the forward 20-trading-day return) against all trading days in that
year. One lucky grant is luck; grants landing in the bottom price-decile or top forward-return
decile REPEATEDLY across years is a pattern with computable odds. Roughly: P(k of n independent
annual grants each landing in the best decile by chance) ≈ C(n,k)·(0.1)^k·(0.9)^(n-k) — quote the
odds in the output when history is deep enough.

## Regulatory responses worth citing in output

- **SAB 120** (Nov 2021): grants made while holding positive MNPI may require upward fair-value
  adjustment under ASC 718; "non-routine" grants merit heightened scrutiny. If a company
  spring-loaded and did NOT adjust fair value, that's a second (accounting) issue.
- **Item 402(x) of Reg S-K** (adopted Dec 2022; in proxies from ~2024): companies must disclose
  their option-grant timing policy AND a table of any NEO option/SAR grants made **4 business days
  before through 1 business day after** filing a 10-K/10-Q or MNPI-containing 8-K — including the
  % change in market price from the day before to the day after the disclosure. A populated 402(x)
  table is the company self-reporting grants in the red zone. An asserted "fixed calendar" policy
  is a testable claim — verify it against the Form 4 record.
