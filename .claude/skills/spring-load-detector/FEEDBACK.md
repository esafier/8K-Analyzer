# spring-load-detector — feedback ledger

How this works. Every correction lands here first, in **Open**. An open item is a known
weakness the skill has NOT yet been fixed for, so a run should read the Open section and
compensate. When a correction is folded into `SKILL.md` and an eval expectation exists that
would catch a regression, it moves to **Resolved** with its commit SHA.

Keep **Open** short. If it grows past ~5 items, the loop has stalled — promote or drop them.

Repos: skill = `C:\Users\ely\Skills` (git) · evals = `C:\Users\ely\skills-workspace\spring-load-detector-workspace` (git)

---

## Open

*(none — see Resolved)*

---

## Resolved

### 2026-08-03 — Price path was crowding out the real analysis
**Run:** CDRE, group of Form 4s filed 2026-06-18 (grants dated 2026-06-16).
**Correction (user):** "It was following the spring load rules too strongly, meaning it was
basing much of the analysis off of the stock price after the grant was filed. The fact that
this is an off-cycle and massive grant itself is worth digging into and highlighting. That's
the most important part here."

**Diagnosis — structural, not a slip.** The workflow put price-path work at steps 4–5 with the
most concrete, testable signals and demoted "why off-cycle" to a soft confounder at step 6.
Followed faithfully it produced a memo centred on post-grant returns. Worse, the eval rubric
had no expectation for off-cycle reason, magnitude, award structure, or approver — so the
flawed memo would still have scored 8/8. The rubric was certifying the failure mode.

**What the first memo missed**, all of it invisible from the price tape:
- Kanders' employment agreement expires 2026-11-08 — 145 days after the grant. The likely
  anchor for the date, and never surfaced.
- The 2026 annual cycle had already run on 2026-03-30, making this purely incremental.
- Hurdles quoted as "+109%/+178%" are only ~11.1%/~15.7% required CAGR over the 7-year window.
- Kanders' tranche has **no service condition**; Williams' and Browers' require the later of
  hurdle achievement and the third anniversary — the CEO has the weakest retention mechanics
  in a retention-rationalised award.
- Two of three Compensation Committee members were not re-nominated 18 days before the grant;
  Norton (Chair) drew 28.5% withheld; new director Sokolow has sat on Kanders' boards since
  January 1996 (Armor Holdings, Clarus).

**Fixed in:** `ab37559` — new mandatory step 4 "Interrogate the anomaly" (why this date /
how big / what the structure reveals / who approved it); price path demoted to confirmatory
with a clean result routing *back into* step 4 rather than lowering the score; posture gains
"the anomaly is the thesis"; scoring rules decouple the spring-load sub-verdict from the
timing-opportunism score and admit structural contradictions; output format gains
Why This Date / Size & Structure / Who Approved It and requires dated falsification tests.

**Regression caught by:** evals `97e5efc` — eval-0 expectations extended; new eval-2 is the
CDRE case with eight required findings and explicit fail conditions.

**Also worth keeping:** the Form 4 `<aff10b5One>` flag (`0` = not a 10b5-1 plan) is what turned
"CEO sold near the grant" into "CEO sold *discretionarily* on the day that set his own strike."
Cheap to check, easy to miss, now called out in step 6.

---

## Known gaps (not corrections — open questions about the skill itself)

- **Rubric saturation.** iteration-1 scored 100% ± 0% with-skill. A rubric that always passes
  detects neither improvement nor regression. Re-baseline after the eval-2 addition; if it
  still returns 100%, the expectations are too easy.
- **No version binding.** `iteration-1/benchmark.json` carries literal `<path/to/skill>` and
  `<model-name>` placeholders, so no benchmark result is tied to a skill version. Worse,
  `SKILL.md` was edited 2026-07-29 00:22Z — 2h26m *after* the 2026-07-28 21:56Z benchmark —
  so the 100% result never described the file that shipped. Future runs must stamp the
  `Skills` repo git SHA.
- **Price history depth.** The Massive/Polygon key reaches back ~2 years only, which blocked
  percentile-ranking grants before 2024-08. Stooq is behind a JS bot-check. Unresolved.
