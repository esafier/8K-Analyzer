# spring_load.py — A triage-grade grant-timing screen.
#
# WHAT THIS IS: a cheap, repeatable screen that scores every equity grant in the
# archive for timing opportunism, so a forensic hour goes to the right dozen
# filings instead of the wrong four thousand.
#
# WHAT THIS IS NOT: the full spring-load-detector analysis. That reconstructs
# Form 4 history, the DEF 14A grant table, committee composition as of the grant
# date, employment-agreement expiries, and non-filing catalysts. None of that is
# available here. This screen sees one 8-K plus the price tape, and it says so —
# every result carries the list of signals it could not test. Treat a high score
# as "run the real skill on this one", never as a finding.
#
# ARCHITECTURE mirrors outcomes.py / outcome_scoring.py: the LLM extracts FACTS
# ONLY (prompts/prompt_spring_load.txt), and everything judgmental below is
# deterministic Python over those facts plus market data. That keeps the score
# reproducible, unit-testable, and immune to the model having a different opinion
# on a Tuesday.

import json
from datetime import datetime, timedelta

from price_history import get_daily_closes, get_close_on_or_after

BENCHMARK_TICKER = "SPY"

# Trading-day windows, expressed in calendar days with slack for weekends.
RUN_IN_DAYS = 30        # how far back to measure the approach to the grant
POP_WINDOW_DAYS = 10    # calendar days covering roughly +1..+5 trading days
RUN_OUT_DAYS = 30       # the confirmatory medium window

# A move has to clear this to count as a signal rather than noise. Micro-caps in
# this archive routinely move 10% on nothing, which is exactly why the price path
# is confirmatory here and never the whole case.
MATERIAL_MOVE = 0.10

# The highest score this screen may assign. See the cap logic in score_grant for
# why 9-10 is structurally out of reach here.
SCREEN_MAX_SCORE = 8

# Bands, matching the full skill's scale so scores mean the same thing in both.
BANDS = (
    (0, 2, "Routine"),
    (3, 4, "Unremarkable"),
    (5, 6, "Suspicious"),
    (7, 8, "Likely timed"),
    (9, 10, "Near-certain"),
)

# Signals this screen structurally cannot test. Reported on every result so a
# clean score is never mistaken for a clean company.
UNTESTABLE = [
    "Form 4 approval-vs-grant-vs-filing date lags",
    "prior grant history from Form 4 (cadence baseline beyond this archive)",
    "DEF 14A grant policy and Item 402(x) narrative",
    "compensation committee composition as of the grant date",
    "employment-agreement expiry as the anchoring event",
    "non-filing catalysts (FDA, contracts, deal rumours)",
    "contemporaneous insider sales and 10b5-1 status",
]


def _to_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "year") and not isinstance(value, str):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# Phrases that indicate an award does or does not carry a service/time condition.
_NO_SERVICE = ("no service", "no time-based", "no time based", "no time vesting",
               "without service", "no additional service", "price hurdles only",
               "solely on", "performance only", "no continued service")
_HAS_SERVICE = ("ratable", "cliff", "anniversary", "vests over", "service condition",
                "continued service", "monthly", "quarterly", "annual installments",
                "in equal", "three-year", "four-year", "3-year", "4-year")


def has_service_condition(vesting_summary):
    """Whether an award requires the recipient to stick around.

    Returns True, False, or None when the filing does not say. The distinction
    matters because an award with no service condition cannot retain anyone —
    and that becomes evidence when OTHER recipients in the same filing do have
    one (see cross_recipient_asymmetry).
    """
    text = (vesting_summary or "").lower()
    if not text.strip():
        return None
    if any(phrase in text for phrase in _NO_SERVICE):
        return False
    if any(phrase in text for phrase in _HAS_SERVICE):
        return True
    return None


def cross_recipient_asymmetry(grants):
    """Names recipients whose award lacks a service condition that others got.

    Straight from the full skill: if the CEO's tranche has no service condition
    while the CFO's does, a retention rationale is contradicted by its own
    paperwork — the award gives the weakest retention mechanics to the person it
    supposedly exists to retain. Visible only by reading the terms side by side,
    which is why a per-grant scorer alone will never catch it.
    """
    if not grants or len(grants) < 2:
        return []
    flags = {g.get("recipient"): has_service_condition(g.get("vesting_summary"))
             for g in grants}
    if True not in flags.values():
        return []   # nobody has one — no asymmetry, just a uniform structure
    return [name for name, has in flags.items() if has is False and name]


def _band(score):
    for low, high, name in BANDS:
        if low <= score <= high:
            return name
    return "Unknown"


def _pct(a, b):
    """Return (b - a) / a, or None if it cannot be computed."""
    if a is None or b is None:
        return None
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if a <= 0:
        return None
    return (b - a) / a


def required_cagr(price_at_grant, hurdle, deadline, grant_date):
    """Compound annual growth the stock must deliver to reach a price hurdle.

    A hurdle quoted as "+109%" is a very different award depending on whether it
    must be met in eighteen months or seven years. Converting to a CAGR is what
    makes hurdles comparable — and is what usually deflates them.

    Returns None when the inputs cannot support the arithmetic.
    """
    grant = _to_date(grant_date)
    end = _to_date(deadline)
    if not grant or not end or price_at_grant is None or hurdle is None:
        return None
    try:
        price_at_grant, hurdle = float(price_at_grant), float(hurdle)
    except (TypeError, ValueError):
        return None
    if price_at_grant <= 0 or hurdle <= 0:
        return None

    years = (end - grant).days / 365.25
    if years <= 0:
        return None
    return (hurdle / price_at_grant) ** (1.0 / years) - 1.0


def price_path(ticker, grant_date):
    """The confirmatory price evidence around a grant, computed not guessed.

    Answers one question only — was this grant timed against news? — and answers
    it honestly, including saying so when there is no usable price data.

    Every leg is anchored to a bar date the stock ACTUALLY traded on, and the
    benchmark is priced on those same dates. Comparing a stock window to a
    differently-dated SPY window would let an illiquid ticker score points purely
    for not having traded on the target date.
    """
    grant = _to_date(grant_date)
    result = {
        "grant_close": None, "run_in": None, "pop": None, "run_out": None,
        "run_out_vs_spy": None, "is_monthly_low": None, "v_shape": False,
        "has_data": False, "windows_mature": False,
    }
    if not ticker or not grant:
        return result

    # Padding exists to LOCATE a bar near each boundary, never to move it.
    window_start = grant - timedelta(days=RUN_IN_DAYS + 10)
    window_end = grant + timedelta(days=RUN_OUT_DAYS + 10)
    closes = get_daily_closes(ticker, window_start, window_end)
    if not closes:
        return result

    grant_iso = grant.isoformat()
    on_or_after = sorted(d for d in closes if d >= grant_iso)
    if not on_or_after:
        return result

    grant_bar = on_or_after[0]
    grant_close = closes[grant_bar]
    result["grant_close"] = grant_close
    result["grant_bar"] = grant_bar
    result["has_data"] = True

    # Run-in: the last bar at or before grant - RUN_IN_DAYS, not the earliest bar
    # fetched. Using the earliest would stretch an advertised 30-day run-in to
    # roughly 40, and let a decline that lives entirely in the padding trip the
    # V-shape signal and add two points.
    run_in_target = (grant - timedelta(days=RUN_IN_DAYS)).isoformat()
    at_or_before = [d for d in closes if d <= run_in_target]
    run_in_bar = max(at_or_before) if at_or_before else None
    if run_in_bar:
        result["run_in"] = _pct(closes[run_in_bar], grant_close)
        result["run_in_bar"] = run_in_bar

    def _bar_at_or_after(target_date):
        later = [d for d in on_or_after if d >= target_date.isoformat()]
        return later[0] if later else None

    # The pop is the BEST move inside the window, not the level at its far edge.
    # Taking the first bar at-or-after day 10 missed a stock that jumped 20% on
    # day 3 and gave it back by day 10 — and, worse, let an illiquid stock whose
    # next bar was day 25 be reported as rising "within 10 days".
    pop_end = (grant + timedelta(days=POP_WINDOW_DAYS)).isoformat()
    in_window = [d for d in on_or_after if grant_bar < d <= pop_end]
    if in_window:
        peak_bar = max(in_window, key=lambda d: closes[d])
        result["pop"] = _pct(grant_close, closes[peak_bar])
        result["pop_bar"] = peak_bar
    result["pop_window_covered"] = bool(in_window)

    out_bar = _bar_at_or_after(grant + timedelta(days=RUN_OUT_DAYS))
    if out_bar:
        result["run_out"] = _pct(grant_close, closes[out_bar])
        result["run_out_bar"] = out_bar

    # A window that has not elapsed yet is pending, not clean. Recorded so a
    # filing screened the day it was filed can be recomputed once the tape fills
    # in. The pop window counts as settled once the tape reaches past its end,
    # even if the stock did not trade inside it — that is a real absence of
    # movement, not a missing observation.
    pop_settled = bool(in_window) or any(d > pop_end for d in on_or_after)
    result["windows_mature"] = bool(pop_settled and out_bar)

    # Benchmark on the stock's OWN bar dates, both legs, or the excess return
    # measures the calendar rather than the grant.
    if out_bar:
        spy_start_bar, spy_at_grant = get_close_on_or_after(BENCHMARK_TICKER, grant_bar)
        spy_end_bar, spy_out = get_close_on_or_after(BENCHMARK_TICKER, out_bar)
        if spy_start_bar == grant_bar and spy_end_bar == out_bar:
            spy_move = _pct(spy_at_grant, spy_out)
            if result["run_out"] is not None and spy_move is not None:
                result["run_out_vs_spy"] = result["run_out"] - spy_move

    # Was the strike set on the cheapest close of its calendar month?
    month = grant_bar[:7]
    month_closes = [c for d, c in closes.items() if d.startswith(month)]
    if len(month_closes) >= 5:
        result["is_monthly_low"] = grant_close <= min(month_closes) + 1e-9

    # Down into the grant, up out of it — the classic shape.
    result["v_shape"] = bool(
        result["run_in"] is not None and result["run_in"] <= -MATERIAL_MOVE
        and result["run_out"] is not None and result["run_out"] >= MATERIAL_MOVE
    )
    return result


def score_grant(grant, path, prior_grant_dates=None, service_asymmetry_peers=None):
    """Score one grant 0-10 for timing opportunism, with the evidence behind it.

    Follows the full skill's posture: an unscheduled grant with no evidenced
    justification starts at 5 and earns its way down; a single observation cannot
    exceed 8 without repetition or a self-contradiction in the paperwork.

    `service_asymmetry_peers` names recipients in the SAME filing who do carry a
    service condition this one lacks — a structural contradiction, and the one
    thing this screen can see that lifts the single-observation cap.
    """
    signals = []
    score = 3  # a documented, on-cycle-looking grant

    rationale = (grant.get("stated_rationale") or "").strip().lower()
    unexplained = rationale in ("", "none stated", "none", "not stated")
    is_new_hire = bool(grant.get("is_new_hire"))

    # An inducement grant to someone being hired is contractually anchored — that
    # is an evidenced justification, not a guess, so it does not start at 5.
    if unexplained and not is_new_hire:
        score = 5
        signals.append(("🟠", "No rationale disclosed for the award"))
    elif is_new_hire:
        signals.append(("🟢", f"Anchored to an appointment ({rationale or 'inducement'})"))
    else:
        signals.append(("🟢", f"Stated rationale: {rationale}"))

    # Off-cycle against whatever cadence this archive can see.
    grant_date = _to_date(grant.get("grant_date"))
    if prior_grant_dates and grant_date:
        anniversaries = []
        for prior in prior_grant_dates:
            prior_date = _to_date(prior)
            if prior_date:
                delta_days = abs((grant_date - prior_date).days % 365)
                anniversaries.append(min(delta_days, 365 - delta_days))
        if anniversaries and min(anniversaries) > 21 and not is_new_hire:
            score += 1
            signals.append(("🟠", f"Off-cycle: {min(anniversaries)}d from any prior grant anniversary"))

    # Approval/grant gap — a grant dated before the approval that created it.
    approval = _to_date(grant.get("approval_date"))
    if approval and grant_date and grant_date < approval:
        score += 2
        signals.append(("🔴", f"Grant dated {grant_date} precedes its approval {approval}"))

    # Price path — confirmatory only.
    if path.get("has_data"):
        if path.get("is_monthly_low"):
            score += 1
            signals.append(("🟠", "Strike set on the lowest close of its calendar month"))
        if path.get("v_shape"):
            score += 2
            signals.append(("🔴", "V-shape: fell into the grant, rose out of it"))
        if not path.get("windows_mature"):
            signals.append(("⚪", "Price windows have not fully elapsed yet — "
                                  "re-check once the tape fills in"))
        pop = path.get("pop")
        if pop is not None and pop >= MATERIAL_MOVE:
            score += 2
            signals.append(("🔴", f"Stock +{pop:.0%} within {POP_WINDOW_DAYS} days of the grant"))
        elif pop is not None and pop <= -MATERIAL_MOVE:
            signals.append(("🟢", f"Stock {pop:.0%} after the grant — refutes news-timing"))
        excess = path.get("run_out_vs_spy")
        if excess is not None and excess >= MATERIAL_MOVE:
            score += 1
            signals.append(("🟠", f"+{excess:.0%} vs SPY over {RUN_OUT_DAYS} days"))
    elif grant.get("grant_date"):
        signals.append(("⚪", "No price data — the entire price path is untested"))
    else:
        signals.append(("⚪", "No grant date disclosed — the price path cannot be "
                              "anchored, so no timing evidence was computed"))

    # Structure. Options set a strike, so their timing is worth more.
    if (grant.get("instrument") or "").upper() in ("OPTION", "SAR"):
        signals.append(("🟡", "Option/SAR — the grant date fixes the strike"))

    # A "retention" award with no service condition contradicts its own paperwork.
    vesting = (grant.get("vesting_summary") or "").lower()
    if "retention" in rationale and ("no service" in vesting or "no time" in vesting):
        score += 2
        signals.append(("🔴", "Retention rationale, but the award carries no service condition"))

    if grant.get("filing_mentions_catalyst"):
        score += 1
        signals.append(("🟠", "The filing itself references upcoming news or a pending transaction"))

    # Structural contradiction: this recipient is exempt from a condition their
    # colleagues in the same filing must meet.
    if service_asymmetry_peers:
        peers = ", ".join(p for p in service_asymmetry_peers if p)
        score += 2
        signals.append(("🔴", f"No service condition, while {peers} in the same filing "
                              f"must stay — the weakest retention mechanics go to the "
                              f"most senior recipient"))

    # This screen tops out at 8 — "Likely timed". The 9-10 band means red-zone
    # proximity PLUS company-controlled news PLUS history or a policy
    # contradiction, and the evidence for that lives in Form 4 grant history,
    # the proxy's Item 402(x) narrative, and committee composition. This screen
    # sees one filing and the price tape, so it is structurally incapable of
    # earning the top band, and claiming it would make the number a lie.
    #
    # The skill's single-observation rule still applies underneath: without
    # repetition or a self-contradiction in the paperwork, a lone grant does not
    # even reach 8 on price evidence alone.
    # Case-insensitive: these phrases appear mid-sentence in some signals and
    # sentence-initial in others, and a capital letter must not silently drop a
    # structural contradiction from the corroboration test.
    contradiction = any(
        sev == "🔴" and ("no service condition" in txt.lower()
                         or "precedes its approval" in txt.lower())
        for sev, txt in signals
    )
    repetition = bool(prior_grant_dates and len(prior_grant_dates) >= 2)

    if score > 7 and not (contradiction or repetition):
        score = 7
        signals.append(("⚪", "Held at 7 — one observation, with no repetition or "
                              "self-contradiction to corroborate the price path"))

    if score > SCREEN_MAX_SCORE:
        score = SCREEN_MAX_SCORE
        signals.append(("⚪", f"This screen tops out at {SCREEN_MAX_SCORE}. The 9-10 band "
                              f"needs Form 4 history, the proxy, or a policy contradiction "
                              f"— run the full forensic pass to go higher."))

    score = max(0, min(10, score))
    return {"score": score, "band": _band(score), "signals": signals}


def analyze(extraction, ticker, filed_date, prior_grant_dates=None):
    """Screen every grant in one filing. `extraction` is the LLM's fact JSON.

    Returns a dict ready to store and render. Never raises on bad input — a
    screen that dies on one malformed filing is useless across an archive.
    """
    if isinstance(extraction, str):
        try:
            extraction = json.loads(extraction)
        except (ValueError, TypeError):
            return {"has_grant": False, "grants": [], "max_score": 0,
                    "band": _band(0), "untestable": UNTESTABLE,
                    "error": "extraction was not valid JSON"}

    if not extraction or not extraction.get("has_grant"):
        return {"has_grant": False, "grants": [], "max_score": 0,
                "band": _band(0), "untestable": UNTESTABLE,
                "notes": (extraction or {}).get("notes", "")}

    asymmetric = cross_recipient_asymmetry(extraction.get("grants") or [])

    results = []
    for grant in extraction.get("grants") or []:
        grant = dict(grant)
        grant.setdefault("filing_mentions_catalyst",
                         extraction.get("filing_mentions_catalyst"))
        # NEVER substitute the filing date. The extraction contract returns null
        # when the filing does not state a grant date, and an 8-K can announce an
        # earlier award without disclosing when it was made. Scoring the filing
        # date's monthly low, pop and V-shape would manufacture price-timing
        # evidence the filing never established.
        grant_date = grant.get("grant_date")

        try:
            path = price_path(ticker, grant_date) if grant_date else {"has_data": False}
        except Exception as e:
            print(f"[SPRING LOAD] price path failed for {ticker}: {e}")
            path = {"has_data": False}

        peers = None
        if grant.get("recipient") in asymmetric:
            peers = [g.get("recipient") for g in (extraction.get("grants") or [])
                     if g.get("recipient") != grant.get("recipient")
                     and has_service_condition(g.get("vesting_summary")) is True]

        scored = score_grant(grant, path, prior_grant_dates,
                             service_asymmetry_peers=peers)

        hurdles = []
        for hurdle in grant.get("price_hurdles") or []:
            cagr = required_cagr(path.get("grant_close"), hurdle,
                                 grant.get("hurdle_deadline"), grant_date)
            hurdles.append({
                "hurdle": hurdle,
                "required_cagr": cagr,
                "vs_grant_price": _pct(path.get("grant_close"), hurdle),
            })

        results.append({
            "recipient": grant.get("recipient"),
            "role": grant.get("role"),
            "instrument": grant.get("instrument"),
            "shares": grant.get("shares"),
            "grant_date": grant_date,
            "stated_rationale": grant.get("stated_rationale"),
            "vesting_summary": grant.get("vesting_summary"),
            "price_path": path,
            "hurdles": hurdles,
            **scored,
        })

    max_score = max([r["score"] for r in results], default=0)
    # Immature if ANY grant's evidence could still change — that is what decides
    # whether a cached analysis is worth recomputing later.
    #
    # A failed fetch is NOT settled. Treating "has_data is False" as final would
    # freeze a transient Yahoo outage into the record as a permanent "No price
    # data", exactly the transient-vs-permanent confusion fixed three times over
    # in the outcome tracker. The only genuinely settled no-price case is a grant
    # the filing never dated: nothing will ever anchor it, so nothing will change.
    def _settled(result_row):
        path = result_row.get("price_path") or {}
        if not result_row.get("grant_date"):
            return True                      # undateable — permanently unanchored
        if not path.get("has_data"):
            return False                     # we could not reach the tape — retry
        return bool(path.get("windows_mature"))

    all_mature = all(_settled(r) for r in results) if results else True

    return {
        "has_grant": True,
        "windows_mature": all_mature,
        "grants": results,
        "max_score": max_score,
        "band": _band(max_score),
        "concurrent_departure": extraction.get("concurrent_departure"),
        "concurrent_material_event": extraction.get("concurrent_material_event"),
        "concurrent_event_note": extraction.get("concurrent_event_note", ""),
        "untestable": UNTESTABLE,
    }


# ============================================================
# RUNNERS — one filing, or a backtest across saved filings
# ============================================================

def refresh_if_stale(filing):
    """Recompute a cached screen's deterministic half if its evidence could move.

    Never calls the LLM — it re-scores the stored facts against the current price
    tape. Safe to call on every read, which is the point: a filing screened the
    day it arrived should catch up on its own once the windows close, without
    anyone paying to re-extract it.

    Returns the freshest analysis available, or None if nothing is cached.
    """
    from database import (
        get_prior_grant_dates,
        get_spring_load_analysis,
        upsert_spring_load_analysis,
    )

    accession = filing.get("accession_no")
    if not accession:
        return None

    cached = get_spring_load_analysis(accession)
    if not cached:
        return None

    try:
        analysis = json.loads(cached["analysis_json"])
    except (ValueError, TypeError):
        return None

    if cached.get("windows_mature") or not cached.get("extraction_json"):
        return analysis

    try:
        extraction = json.loads(cached["extraction_json"])
    except (ValueError, TypeError):
        return analysis
    if not extraction:
        return analysis

    refreshed = analyze(
        extraction,
        ticker=filing.get("ticker"),
        filed_date=filing.get("filed_date"),
        prior_grant_dates=get_prior_grant_dates(
            filing.get("cik"), exclude_accession=accession),
    )
    upsert_spring_load_analysis(
        accession, filing.get("cik"), filing.get("ticker"),
        filing.get("filed_date"), refreshed,
        model=cached.get("model"), extraction=extraction,
    )
    return refreshed


def screen_filing(filing, model=None, force=False):
    """Screen one filing and cache the result.

    `filing` is a dict with at least accession_no, cik, ticker, filed_date and
    raw_text. Returns the analysis dict, or None when the filing has no text to
    read (which is a gap, not a clean result).

    Cached by accession because the filing text never changes. Pass force=True
    to re-run the extraction — worth doing after a prompt change, not otherwise.
    """
    from database import (
        get_prior_grant_dates,
        get_spring_load_analysis,
        upsert_spring_load_analysis,
    )
    from llm import extract_grant_facts

    accession = filing.get("accession_no")
    if not accession:
        return None

    if not force:
        refreshed = refresh_if_stale(filing)
        if refreshed is not None:
            return refreshed

    text = filing.get("raw_text") or ""
    if not text.strip():
        print(f"[SPRING LOAD] {accession}: no filing text stored — cannot screen")
        return None

    extraction = extract_grant_facts(text, model=model)
    if extraction.get("error"):
        print(f"[SPRING LOAD] {accession}: extraction failed — not caching, will retry")
        return None

    prior = get_prior_grant_dates(filing.get("cik"), exclude_accession=accession)
    analysis = analyze(
        extraction,
        ticker=filing.get("ticker"),
        filed_date=filing.get("filed_date"),
        prior_grant_dates=prior,
    )

    upsert_spring_load_analysis(
        accession, filing.get("cik"), filing.get("ticker"),
        filing.get("filed_date"), analysis, model=model, extraction=extraction,
    )
    return analysis


def backtest_watchlist(model=None, limit=None, force=False, verbose=True):
    """Screen the filings already saved to the watchlist.

    This is the "was that one a spring-load?" pass over names already flagged by
    hand — the highest-value use of the screen, because those filings were saved
    for a reason and the question was left open.

    Returns {'screened': n, 'skipped': n, 'flagged': [...]} where flagged lists
    anything scoring 5 or higher, worst first.
    """
    from database import get_watchlist_filings

    rows = get_watchlist_filings() or []
    if limit:
        rows = rows[:limit]

    stats = {"screened": 0, "skipped": 0, "flagged": []}

    for row in rows:
        filing = dict(row)
        accession = filing.get("accession_no")
        try:
            analysis = screen_filing(filing, model=model, force=force)
        except Exception as e:
            print(f"[SPRING LOAD] {accession}: screen failed: {e}", flush=True)
            stats["skipped"] += 1
            continue

        if analysis is None:
            stats["skipped"] += 1
            continue

        stats["screened"] += 1
        if analysis.get("max_score", 0) >= 5:
            stats["flagged"].append({
                "accession_no": accession,
                "ticker": filing.get("ticker"),
                "company": filing.get("company"),
                "filed_date": filing.get("filed_date"),
                "score": analysis["max_score"],
                "band": analysis["band"],
            })
            if verbose:
                print(f"[SPRING LOAD] {filing.get('ticker')} {filing.get('filed_date')}: "
                      f"{analysis['max_score']}/10 {analysis['band']}", flush=True)

    stats["flagged"].sort(key=lambda f: f["score"], reverse=True)

    if verbose:
        print(f"[SPRING LOAD] Backtest done — screened {stats['screened']}, "
              f"skipped {stats['skipped']}, flagged {len(stats['flagged'])} at 5+", flush=True)
    return stats
