# Signal Taxonomy — variants, tests, and severity

The literature behind every claim here: Yermack (1997, JF) — origin of the spring-loading finding;
Aboody & Kasznik (2000, JAE) — disclosure-timing around scheduled grants; Lie (2005, Mgmt Sci) and
Heron & Lie (2007 JFE, 2009 Mgmt Sci) — backdating detection and base rates; Bebchuk, Grinstein &
Peyer (2010, JF) — "lucky" grants; Sen (SSRN) — post-SOX spring-loading returns; Daines, McQueen &
Schonlau (2018, JFQA) — scheduled-grant talk-downs; Fich, Cai & Tran (2011, JFE) — grants during
merger talks; Larcker et al. (2021) — 10b5-1 red flags.

## The variants (know which game you're looking at)

| Variant | Mechanic | Primary tell |
|---|---|---|
| **Spring-loading** | Grant BEFORE positive news the insiders already know | Off-cycle grant + catalyst within days/weeks after |
| **Bullet-dodging** | Delay grant until AFTER bad news is out (grant into the crater) | Grant shortly after negative 8-K/earnings drop |
| **Backdating** | Paper the grant date retroactively to a past low | Strike = a *different* day's close; late Form 4 |
| **Disclosure-timing game** | Grant date is fixed → move the NEWS instead: rush bad news out before the grant, sit on good news until after | Guidance cuts / negative 8-Ks clustering 2–4 weeks pre-grant, reversal after (Aboody-Kasznik; Daines et al. — "scheduled ≠ safe") |
| **Trough opportunism** | Off-cycle "retention" mega-grant at a market/stock low | Crash + unscheduled grant + "retention" rationale (COVID 2020 wave) |
| **Deal spring-load** | Unscheduled grant during private M&A negotiations | Grant weeks before deal announcement (>13% of acquisition targets did this, 1999–2007) |
| **Milestone rigging** | Reprice/restructure awards to pay out on an imminent known milestone | Repricing or new performance triggers just before a likely trigger event |

## Step 0: Scheduled or unscheduled? (the master fork)

Classify the grant FIRST — everything downstream depends on it:
- **Scheduled** = within ±1 day of the anniversary of prior years' grants (the strict Lie/Heron-Lie
  test; a ±1 week match is only weak evidence of scheduling). Confirm against the proxy's stated
  grant policy and the Grants of Plan-Based Awards tables across 3+ years.
- **Unscheduled/off-cycle** = everything else. Base rates justify starting skeptical: ~19% of
  unscheduled at-the-money option grants in the 1996–2005 sample were manipulated; ~30% of firms
  manipulated at least once; post-2002 rules halved backdating but spring-loading survived
  (buying on disclosure of unscheduled CEO grants earned ~1.1%/month abnormal returns post-SOX).
- If scheduled → pivot the investigation to the **news flow** around the fixed date
  (disclosure-timing game), not the grant date itself.

## Signal table — test each one, cite what you find

Severity: 🔴 high (each alone justifies "likely"), 🟠 medium (two+ together justify "likely"),
🟡 low (context/prior-raisers).

### Timing vs. events
- 🔴 **Grant ≤5 trading days before positive catalyst** (earnings beat, deal, contract, FDA,
  guidance raise). The Kodak zone. The 402(x) regulatory red zone is 4 business days before → 1
  after an MNPI filing.
- 🔴 **Grant during a window the company controlled the news timing of** (their own press release,
  their own 8-K) — controlling both dials is the aggravated form (Vaxart).
- 🔴 **Unscheduled grant during private M&A negotiations** (visible only in retrospect — if a deal
  was announced within ~3 months after an off-cycle grant, flag it hard).
- 🟠 **Grant 6–30 days before positive catalyst** — investigate; distance alone doesn't clear it.
- 🟠 **Grant within days AFTER a big negative disclosure** (bullet-dodge / crater grant).
- 🟡 **Grant just after earnings release** — this is actually the CLEAN convention (open-window
  granting); note it as a mitigant IF the cadence matches prior years.

### Price path (fetch daily closes, grant −90d → +90d)
- 🔴 **V-shape**: negative drift into the grant, positive abnormal move after. Manipulated-era
  benchmark: −5.6% (30d before) / +3.7% (30d after) for unscheduled grants. Modern timely-filed
  average is near zero — so a pronounced V on an unscheduled grant is anomalous.
- 🔴 **Post-grant pop concentrated in days +1..+5** → grant sat immediately ahead of a disclosure.
  Spread evenly over +30d → much weaker signal.
- 🟠 **Grant at/near the monthly low close** ("lucky grant"). One is ~1-in-20 luck; REPEATED
  monthly-low grants across years, or luck shared by CEO and directors on the same date, is the
  Bebchuk persistence signature — compute the binomial odds and print them.
- 🟠 **Strike/grant price ≠ grant-date close** — reconcile the Form 4 price against actual OHLC.
  Matching a different recent day's close (especially a low) = backdating tell. Averaging
  conventions must be disclosed in the proxy; verify, don't assume.

### Filing hygiene
- 🔴 **Form 4 filed >2 business days after grant date** — the single cleanest modern flag: in the
  post-2002 data, suspicious return patterns exist ONLY in late-filed grants, scaling with delay.
- 🟠 **8-K Item 5.02(e) filed for the grant** — the company itself judged it off-plan/material
  (routine on-plan grants are exempt from 5.02(e)). Read that 8-K closely.
- 🟡 **Grant papered by unanimous written consent on a non-meeting date** (visible via the proxy's
  committee action-date column vs. regular meeting calendar) — the approval had no calendared
  reason to exist on that date.
- 🟡 **Grant approved at a special/unscheduled board or committee meeting** dated just before an
  announcement (the Cyberonics pattern — approval meeting the evening of an FDA panel vote).
- 🔴 **Policy-vs-practice contradiction**: the proxy's Item 402(x) narrative or CD&A says "we do
  not time grants" / "fixed calendar" but the Form 4 record shows otherwise. This is the
  highest-value flag in the whole skill — it converts lawful-but-grubby timing into potential
  misrepresentation (the Tyson dividing line), and it's checkable from public documents alone.
- 🟡 **Item 402(x) mechanics**: a populated 402(x) table = self-reported red-zone option grants
  with the price pop quantified — read it first. But know its gaps: it covers **options/SARs
  granted to NEOs only — RSUs and other full-value awards are excluded**, and only grants near
  MNPI *filings* trigger it. Never treat an empty 402(x) table as exculpatory for RSU grants or
  non-filing catalysts; the Form 4-vs-event cross-reference is the only screen for those.

### Structure of the award itself
- 🟠 **Solo grant** — CEO/chairman is the only recipient (post-grant abnormal returns were ~50%
  larger for solo grants). Broad annual grants to all NEOs on one date lean routine.
- 🟠 **Time-vesting-only mega-grant labeled "retention"/"transformation"** made off-cycle — payout
  needs no performance, only the stock pop the insiders may already foresee.
- 🟠 **Options rather than RSUs granted off-cycle at a low** — options are the higher-leverage
  spring-load instrument (Kodak used options; SAB 120 and 402(x) target options/SARs).
- 🟠 **Repricing/exchange or new milestone triggers just before a plausible trigger** (Novavax
  pattern).
- 🟡 **Vesting terms unusually short or with acceleration** vs. the company's own prior grants.

### Behavioral corroboration
- 🟠 **Insiders also bought stock in the open market near the grant** — grant timing plus personal
  buying doubles the signal (and the grant may be the *legal* expression of the same knowledge).
- 🟠 **10b5-1 plan adopted/modified near the grant** with cooling-off <30–60 days or a single-trade
  design (Larcker red flags; adoption/modification is disclosed quarterly under Item 408(a)).
- 🟡 **Optics management**: grant coincides with publicized executive sacrifice (salary cut) —
  COVID-2020 pattern: pay-cut firms were 1.5x more likely to make unscheduled grants.
- 🟡 **Governance covariates that raise the prior**: non-independent board, long-tenured CEO,
  history of "lucky" grants, prior timing controversies (search the executive's OTHER companies
  via the reporting-owner CIK).

## Confounders — the innocent explanations, and the tests that kill or confirm them

Do not hand these out for free; each must be AFFIRMATIVELY evidenced to count:
- **"Boards grant after declines for retention"** — a run-down into the grant alone proves
  nothing. The discriminating half of the V is the POST-grant abnormal run-up. No pop, no case.
- **"The move after the grant was just earnings"** — only exculpatory if the grant date matches
  the strict anniversary test (±1 day) or the proxy's stated fixed calendar. A "coincidentally"
  pre-earnings unscheduled grant gets no such credit.
- **"New-hire grant"** — new-hire timing is set by the start date, which is itself negotiable.
  Check whether the start/grant date landed suspiciously close to known catalysts anyway.
- **"It was market-wide, everything rallied"** — compute the stock's move NET of sector/market. A
  grant before a market-wide rally that the stock merely matched is luck; before an
  idiosyncratic pop is not. (Inverse of Lie's market-component test.)
- **Small sample honesty**: one grant with good luck is one observation. State it. The verdict
  strengthens with repetition — that's why the historical cadence reconstruction is mandatory,
  not optional.

## Severity ordering (single-company review)

1. Late-filed unscheduled grant at a monthly low with a +1..+5-day pop = worst
2. Unscheduled grant inside the 402(x) window / ≤5 days before positive MNPI = high
3. Off-cycle "retention" options at a trough, catalyst within 30d = high
4. Repeated anniversary grants preceded by guidance cuts that reverse = moderate (disclosure game)
5. Isolated V-pattern, timely filed, on a scheduled date = low — say so and move on
