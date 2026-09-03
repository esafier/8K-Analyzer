"""Typed signal detection — the layer that decides what is interesting.

This replaces a single opaque 0-10 score from a model reading filing text in
a vacuum. Two things were wrong with that:

  1. The signals this user hunts are RELATIVE — off-cycle versus the company's
     own grant cadence, a hurdle versus today's price, this exit versus the
     last three. A model shown only the filing text cannot see any of that, so
     its scores bunched in the middle and stopped discriminating.
  2. A number with no name can't be checked, tuned, or learned from. A typed
     signal with an evidence string can: the user reads "walks from ~$4.2M
     unvested" and immediately knows whether the system is right.

So detection is deterministic and lives here. The strong model runs afterward
and only on filings that already carry a signal — it explains and weighs, it
doesn't discover.

Inputs:
  facts   — the extraction output (prompts/prompt_v4.txt schema)
  context — market/history context (context.py schema)

Output: DetectionResult with a list of Signals and, when nothing fired, the
PASS rules that explain why. Both halves matter — "no signal" with a reason
is a usable answer; "no signal" with no reason looks like a bug.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "signal_weights.json")

# Roles whose departure carries real information. A VP of Sales leaving is
# noise at this altitude; the finance seats are where accounting trouble and
# lost confidence show up first.
CSUITE_ROLES = {"CEO", "CFO", "CAO", "COO", "OTHER_CSUITE"}
TOP_ROLES = {"CEO", "CFO", "CAO"}
FINANCE_ROLES = {"CFO", "CAO"}


def _load_config():
    """Weights and thresholds live in JSON so tuning is a data change that
    evaluate.py can score, not a commit."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"signals: could not load {_CONFIG_PATH}: {e}")


CONFIG = _load_config()
SEVERITIES = CONFIG["severities"]
THRESHOLDS = CONFIG["thresholds"]
JUDGE_GATE = CONFIG["judge_gate"]


@dataclass
class Signal:
    """One named, evidenced reason a filing is worth attention.

    `evidence` is written to be read by a human on a dashboard row — it is the
    product, not a debug string. `data` carries the numbers a downstream
    consumer (judge prompt, scorecard) needs without re-deriving them.
    """
    type: str
    direction: str          # "BEARISH" | "BULLISH"
    severity: int           # 1-5
    evidence: str
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class DetectionResult:
    signals: list = field(default_factory=list)
    pass_reasons: list = field(default_factory=list)

    @property
    def max_severity(self):
        return max((s.severity for s in self.signals), default=0)

    @property
    def types(self):
        return [s.type for s in self.signals]

    @property
    def direction(self):
        """Net direction across signals, weighted by severity.

        MIXED is a real answer, not a cop-out: a CEO who forfeits equity on the
        way out while the incoming CEO gets a 100%-appreciation hurdle package
        genuinely points both ways, and flattening that to one arrow would
        throw away the most interesting filings in the feed.
        """
        if not self.signals:
            return "NEUTRAL"
        bear = sum(s.severity for s in self.signals if s.direction == "BEARISH")
        bull = sum(s.severity for s in self.signals if s.direction == "BULLISH")
        if bear and bull:
            stronger, weaker = max(bear, bull), min(bear, bull)
            # A faint counter-signal shouldn't dilute a strong call; only treat
            # it as genuinely two-sided when the weaker side is material.
            return "MIXED" if weaker >= stronger * 0.5 else ("BEARISH" if bear > bull else "BULLISH")
        if bear:
            return "BEARISH"
        if bull:
            return "BULLISH"
        return "NEUTRAL"

    def to_json(self):
        return json.dumps([s.to_dict() for s in self.signals])


# ---------------------------------------------------------------------------
# Safe accessors — extraction output is model-generated and routinely ragged
# ---------------------------------------------------------------------------

def _list(facts, key):
    """A list field that might be missing, null, or the wrong type."""
    value = (facts or {}).get(key)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _flags(facts):
    value = (facts or {}).get("filing_flags")
    return value if isinstance(value, dict) else {}


def _num(value):
    """Coerce to float, tolerating '$4.2M'-style strings by returning None.

    Deliberately strict: a half-parsed number is worse than no number, because
    a wrong denominator silently produces a wrong percentage on the dashboard.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _truthy(value):
    """True only for a real affirmative. null ('the filing didn't say') is not
    the same as false ('the filing said no'), and detectors must not confuse
    a silent filing with a clean one."""
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() == "true")


def _role(entry):
    role = (entry or {}).get("role_class")
    role = str(role).strip().upper() if role else ""
    if role in CSUITE_ROLES or role in {"DIRECTOR", "OTHER"}:
        return role
    # Fall back to reading the title when the model didn't classify it.
    title = str((entry or {}).get("title") or "").lower()
    if "chief executive" in title or title.strip() == "ceo":
        return "CEO"
    if "chief financial" in title or title.strip() == "cfo":
        return "CFO"
    if "chief accounting" in title or "controller" in title:
        return "CAO"
    if "chief operating" in title:
        return "COO"
    if "chief" in title or "president" in title:
        return "OTHER_CSUITE"
    if "director" in title:
        return "DIRECTOR"
    return "OTHER"


def _name(entry, key="name"):
    return str((entry or {}).get(key) or "the executive").strip()


def _clamp(severity):
    return max(1, min(5, int(severity)))


def _parse_date(value):
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _days_between(earlier, later):
    a, b = _parse_date(earlier), _parse_date(later)
    if a is None or b is None:
        return None
    return (b - a).days


def _money(value):
    """Format a dollar amount the way it reads on a dashboard row."""
    if value is None:
        return "an undisclosed amount"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


# ---------------------------------------------------------------------------
# Bearish detectors
# ---------------------------------------------------------------------------

def _detect_forfeiture_exit(facts, context):
    """The loudest bearish tell: an insider leaving money on the table.

    Severance and accelerated vesting are what a negotiated exit looks like.
    Forfeiture is what it looks like when someone would rather go than wait
    for the equity to pay out — which is a statement about the equity.
    """
    out = []
    for dep in _list(facts, "departures"):
        flag = str(dep.get("forfeiture_flag") or "").strip().lower()
        if flag not in ("forfeited", "mixed"):
            continue
        role = _role(dep)
        severity = SEVERITIES["FORFEITURE_EXIT"] - (1 if flag == "mixed" else 0)
        if role == "DIRECTOR":
            severity -= 1  # directors hold far less unvested equity
        impact = dep.get("comp_impact")
        detail = f" — {impact}" if impact else ""
        out.append(Signal(
            type="FORFEITURE_EXIT",
            direction="BEARISH",
            severity=_clamp(severity),
            evidence=(
                f"{_name(dep)} ({dep.get('title') or role}) "
                f"{'forfeits' if flag == 'forfeited' else 'partly forfeits'} unvested "
                f"compensation on the way out{detail}"
            ),
            data={"person": _name(dep), "role": role, "forfeiture_flag": flag,
                  "comp_impact": impact},
        ))
    return out


# Item 5.02 requires companies to address whether a departure involved a
# disagreement, so virtually every director resignation carries a DENIAL:
# "was not the result of any disagreement with the Company...". An extractor
# reading that sentence can easily record "disagreement: true" — and did, on
# the first real filing tested. Firing on it would put a severity-5 signal on
# almost every 5.02 in existence, which is precisely the noise problem this
# rebuild exists to end. So the phrasing is checked in code as well as in the
# prompt: belt and braces on the single highest-frequency false positive.
_DISAGREEMENT_DENIALS = (
    "not the result of any disagreement",
    "not due to any disagreement",
    "not because of any disagreement",
    "not as a result of any disagreement",
    "no disagreement",
    "not involve any disagreement",
    "without any disagreement",
    "was not related to any disagreement",
)


def _disagreement_denied(text):
    """True when the text is the standard no-disagreement boilerplate."""
    lowered = str(text or "").lower()
    return any(phrase in lowered for phrase in _DISAGREEMENT_DENIALS)


def _detect_for_cause(facts, context):
    """Termination for cause, or an acknowledged disagreement with the company.

    The rare filing that admits a real disagreement, or fires someone for
    cause, is saying something it would much rather not have said. The common
    filing that denies a disagreement is saying nothing at all — see the
    denial list above.
    """
    out = []
    flags = _flags(facts)
    for dep in _list(facts, "departures"):
        reason = str(dep.get("stated_reason") or "").lower()
        # Accept the old field name too: re-analysis runs and stored rows can
        # still carry `mentions_disagreement` from the first v4 draft.
        disagreement = _truthy(dep.get("disagreement_disclosed")) or _truthy(dep.get("mentions_disagreement"))
        if disagreement and _disagreement_denied(reason):
            disagreement = False  # the filing denied it; the flag misread the boilerplate
        for_cause = "for cause" in reason or "terminated for cause" in reason
        if not (for_cause or disagreement):
            continue
        label = "terminated for cause" if for_cause else "departed citing a disagreement with the company"
        out.append(Signal(
            type="FOR_CAUSE_OR_DISAGREEMENT",
            direction="BEARISH",
            severity=_clamp(SEVERITIES["FOR_CAUSE_OR_DISAGREEMENT"]),
            evidence=f"{_name(dep)} ({dep.get('title') or _role(dep)}) {label}",
            data={"person": _name(dep), "stated_reason": dep.get("stated_reason")},
        ))

    # A filing-level flag is deliberately NOT enough on its own.
    #
    # Measured on a 20-filing sample: a filing-flag fallback fired on 4 of 20
    # (20%), against a real base rate nearer 1-2%. The cause is contractual
    # boilerplate — every employment agreement DEFINES "Cause" ("the Company
    # may terminate the Executive for Cause, meaning..."), so the phrase
    # appears in a large share of 8-K exhibits where nobody was fired. The
    # word is not the event.
    #
    # So the flag can only corroborate a departure that is already in the
    # filing; with nobody leaving, there is nobody to have been terminated.
    if out:
        return out

    departures = _list(facts, "departures")
    flag_for_cause = _truthy(flags.get("terminated_for_cause"))
    flag_disagreement = (_truthy(flags.get("disagreement_disclosed"))
                         or _truthy(flags.get("mentions_disagreement")))
    if flag_disagreement and any(_disagreement_denied(d.get("stated_reason")) for d in departures):
        flag_disagreement = False

    if departures and (flag_for_cause or flag_disagreement):
        label = ("a for-cause termination" if flag_for_cause
                 else "a disagreement with the company")
        out.append(Signal(
            type="FOR_CAUSE_OR_DISAGREEMENT",
            direction="BEARISH",
            severity=_clamp(SEVERITIES["FOR_CAUSE_OR_DISAGREEMENT"] - 1),
            evidence=f"Filing reports a departure alongside {label}",
            data={"source": "filing_flags",
                  "people": [_name(d) for d in departures][:3]},
        ))
    return out


def _detect_abrupt_exit(facts, context):
    """A C-suite officer gone with no notice.

    Orderly successions are announced months ahead. "Effective immediately" is
    what it looks like when the decision was not the executive's to schedule.
    Retirements and merger-driven exits are excluded — both are mechanical.
    """
    out = []
    max_days = THRESHOLDS["abrupt_exit_max_days_notice"]
    for dep in _list(facts, "departures"):
        role = _role(dep)
        if role not in CSUITE_ROLES:
            continue
        if _truthy(dep.get("is_retirement")) or _truthy(dep.get("is_merger_related")):
            continue
        days = _num(dep.get("days_notice"))
        immediate = _truthy(dep.get("effective_immediately"))
        if not immediate and (days is None or days > max_days):
            continue
        severity = SEVERITIES["ABRUPT_CSUITE_EXIT"] + (1 if role in TOP_ROLES else 0)
        timing = "effective immediately" if immediate else f"on {int(days)} days' notice"
        out.append(Signal(
            type="ABRUPT_CSUITE_EXIT",
            direction="BEARISH",
            severity=_clamp(severity),
            evidence=f"{_name(dep)} ({dep.get('title') or role}) departs {timing}",
            data={"person": _name(dep), "role": role, "days_notice": days,
                  "effective_immediately": immediate},
        ))
    return out


def _detect_no_successor(facts, context):
    """Nobody named to take the seat.

    A board that has a replacement lined up announces one. An empty CFO chair
    with a "search underway" means the exit outran the succession plan.
    """
    out = []
    for dep in _list(facts, "departures"):
        role = _role(dep)
        if role not in CSUITE_ROLES:
            continue
        if _truthy(dep.get("is_merger_related")):
            continue
        # A planned retirement with a search underway is succession planning,
        # not a gap. The signal is meant to catch the seat that emptied faster
        # than the board could fill it.
        if _truthy(dep.get("is_retirement")) and (_num(dep.get("days_notice")) or 0) >= 30:
            continue
        # Only fire when the filing affirmatively shows no successor. A null
        # here means the filing was silent, which is not the same claim.
        if dep.get("successor_named") is None and not dep.get("successor_info"):
            continue
        if _truthy(dep.get("successor_named")):
            continue
        severity = SEVERITIES["NO_SUCCESSOR"] + (1 if role in TOP_ROLES else 0)
        out.append(Signal(
            type="NO_SUCCESSOR",
            direction="BEARISH",
            severity=_clamp(severity),
            evidence=f"No successor named for {dep.get('title') or role} ({_name(dep)})",
            data={"person": _name(dep), "role": role,
                  "successor_info": dep.get("successor_info")},
        ))
    return out


def _detect_departure_cluster(facts, context):
    """Two or more executives out of the same company inside 24 months.

    One exit is a person. Three is a condition — and it is the kind of pattern
    that is invisible filing-by-filing, which is exactly why the tool should
    be the one to notice it.
    """
    count = _num((context or {}).get("departures_24mo"))
    if count is None or count < THRESHOLDS["cluster_min_departures"]:
        return []

    severity = SEVERITIES["DEPARTURE_CLUSTER"]
    if count >= THRESHOLDS["cluster_high_departures"]:
        severity += 1

    # Finance-seat turnover is the version that most often precedes an
    # accounting problem, so it outranks general churn.
    roles = {_role(d) for d in _list(facts, "departures")}
    if roles & FINANCE_ROLES:
        severity += 1

    return [Signal(
        type="DEPARTURE_CLUSTER",
        direction="BEARISH",
        severity=_clamp(severity),
        evidence=f"{int(count)} executive departures at this company in the last 24 months",
        data={"departures_24mo": int(count), "roles_in_filing": sorted(roles)},
    )]


def _detect_exit_after_ipo(facts, context):
    """A C-suite exit inside the first year as a public company.

    The people who took the company public are the people who signed the S-1.
    One of them leaving this early is a comment on what they found.
    """
    departures = [d for d in _list(facts, "departures") if _role(d) in CSUITE_ROLES]
    if not departures:
        return []
    months = _num((context or {}).get("months_since_ipo"))
    if months is None or months > THRESHOLDS["ipo_recent_months"]:
        return []
    return [Signal(
        type="EXIT_AFTER_IPO",
        direction="BEARISH",
        severity=_clamp(SEVERITIES["EXIT_AFTER_IPO"]),
        evidence=f"C-suite departure {months:.0f} months after the company's first SEC filing",
        data={"months_since_ipo": months,
              "people": [_name(d) for d in departures]},
    )]


def _detect_restatement_context(facts, context):
    """A departure landing next to a non-reliance determination (Item 4.02).

    Item 4.02 means previously issued financials can no longer be relied on.
    An executive leaving inside that window is a different event from the same
    executive leaving in a quiet quarter.
    """
    flags = _flags(facts)
    lookback = THRESHOLDS["restatement_lookback_days"]
    recent = (context or {}).get("recent_item_codes") or {}
    restatement_dates = recent.get("4.02") if isinstance(recent, dict) else None

    if _truthy(flags.get("is_restatement")):
        return [Signal(
            type="RESTATEMENT_CONTEXT",
            direction="BEARISH",
            severity=_clamp(SEVERITIES["RESTATEMENT_CONTEXT"] + 1),
            evidence="This filing itself reports a restatement / non-reliance determination",
            data={"source": "filing"},
        )]

    if not restatement_dates:
        return []
    filed = (context or {}).get("filed_date")
    nearest = None
    for date_str in restatement_dates:
        days = _days_between(date_str, filed)
        if days is not None and 0 <= days <= lookback:
            nearest = days if nearest is None else min(nearest, days)
    if nearest is None:
        return []
    return [Signal(
        type="RESTATEMENT_CONTEXT",
        direction="BEARISH",
        severity=_clamp(SEVERITIES["RESTATEMENT_CONTEXT"]),
        evidence=f"Company filed an Item 4.02 non-reliance notice {nearest} days ago",
        data={"days_since_restatement": nearest},
    )]


def _detect_value_extraction(facts, context):
    """Comp mechanics that move value to insiders without requiring performance.

    Each of these has a legitimate explanation and a self-serving one. What
    they share is that the payout no longer depends on the stock doing well —
    which is the opposite of the alignment the equity is supposed to create.
    """
    out = []
    min_retention = THRESHOLDS["retention_award_min_usd"]
    for event in _list(facts, "comp_events"):
        who = _name(event, "executive")
        reasons = []
        if _truthy(event.get("is_repricing")):
            reasons.append("underwater awards repriced or exchanged")
        if _truthy(event.get("has_single_trigger_cic")):
            reasons.append("single-trigger change-of-control acceleration")
        if _truthy(event.get("has_tax_gross_up")):
            reasons.append("tax gross-up")

        value = _num(event.get("grant_value_usd"))
        if (_truthy(event.get("is_retention_award"))
                and not _truthy(event.get("has_performance_condition"))
                and value is not None and value >= min_retention):
            reasons.append(f"{_money(value)} retention award with no performance condition")

        if not reasons:
            continue
        out.append(Signal(
            type="VALUE_EXTRACTION",
            direction="BEARISH",
            severity=_clamp(SEVERITIES["VALUE_EXTRACTION"] + (1 if len(reasons) > 1 else 0)),
            evidence=f"{who}: {'; '.join(reasons)}",
            data={"executive": who, "reasons": reasons, "grant_value_usd": value},
        ))
    return out


_MONETIZATION_SEVERITY = {
    "forward_sale": 3, "collar": 3, "swap": 3, "pledge": 3,
    "open_market_sale": 2, "10b5-1_termination": 2,
}
_MONETIZATION_LABEL = {
    "forward_sale": "entered a prepaid forward sale (cash now, shares later — economically a sale without a public sale)",
    "collar": "put a collar on their position (downside hedged, upside capped)",
    "swap": "entered an equity swap on their holdings",
    "pledge": "pledged shares as loan collateral (forced-sale risk if the stock falls)",
    "open_market_sale": "sold shares on the open market",
    "10b5-1_termination": "terminated a 10b5-1 trading plan",
}


def _detect_insider_monetization(facts, context):
    """Insiders taking money off the table, or hedging away their own exposure.

    A pledge or a collar is the interesting version: the insider keeps the
    shares — and the votes — while transferring the risk to someone else.
    """
    out = []
    for txn in _list(facts, "insider_transactions"):
        kind = str(txn.get("type") or "").strip().lower()
        severity = _MONETIZATION_SEVERITY.get(kind)
        if severity is None:
            continue
        person = _name(txn, "person")
        value = _num(txn.get("value_usd"))
        amount = f" ({_money(value)})" if value else ""
        out.append(Signal(
            type="INSIDER_MONETIZATION",
            direction="BEARISH",
            severity=_clamp(severity),
            evidence=f"{person} {_MONETIZATION_LABEL[kind]}{amount}",
            data={"person": person, "type": kind, "value_usd": value,
                  "shares": _num(txn.get("shares"))},
        ))
    return out


def _detect_exit_near_earnings(facts, context):
    """An executive leaving days before the company reports.

    Weak on its own — someone always leaves near a print. It earns its keep by
    stacking: an abrupt CFO exit two weeks before earnings is a different
    filing from an abrupt CFO exit in the middle of a quiet quarter.
    """
    departures = [d for d in _list(facts, "departures") if _role(d) in CSUITE_ROLES]
    if not departures:
        return []
    days = _num((context or {}).get("days_to_earnings"))
    if days is None or days < 0 or days > THRESHOLDS["exit_near_earnings_days"]:
        return []
    return [Signal(
        type="EXIT_NEAR_EARNINGS",
        direction="BEARISH",
        severity=_clamp(SEVERITIES["EXIT_NEAR_EARNINGS"]),
        evidence=f"C-suite departure {int(days)} days before the next scheduled earnings report",
        data={"days_to_earnings": int(days)},
    )]


def _detect_friday_night(facts, context):
    """Filed after the close on a Friday — the traditional burial slot.

    Worth one point and no more. It is a hint about how the company wanted the
    news received, not evidence about the news itself.
    """
    if not _truthy((context or {}).get("is_after_hours_friday")):
        return []
    return [Signal(
        type="FRIDAY_NIGHT_FILING",
        direction="BEARISH",
        severity=_clamp(SEVERITIES["FRIDAY_NIGHT_FILING"]),
        evidence=f"Accepted by SEC after Friday's close ({(context or {}).get('accepted_et') or 'Friday evening'})",
        data={"accepted_et": (context or {}).get("accepted_et")},
    )]


# ---------------------------------------------------------------------------
# Bullish detectors
# ---------------------------------------------------------------------------

def _detect_hurdle_conviction(facts, context):
    """Vesting hurdles that require the stock to appreciate materially.

    This is the cleanest read on what a board actually expects. A PSU that
    pays out only above $20 when the stock is $10 is the comp committee
    writing down its price target — and unlike an investor-relations deck,
    they had to put their own executives' money behind it.

    Reported as required CAGR wherever the vesting window is known: "+120%"
    over seven years is 12%/yr, which is a much less exciting number and the
    one that should drive the decision.
    """
    price = _num((context or {}).get("price"))
    if not price or price <= 0:
        return []

    out = []
    for event in _list(facts, "comp_events"):
        prices = [p for p in (_num(v) for v in (event.get("hurdle_prices") or [])) if p]
        if not prices:
            continue
        top = max(prices)
        appreciation = (top / price - 1.0) * 100.0
        if appreciation < THRESHOLDS["hurdle_appreciation_pct"]:
            continue

        severity = SEVERITIES["HURDLE_CONVICTION"]
        if appreciation >= THRESHOLDS["hurdle_high_appreciation_pct"]:
            severity += 1

        years = _num(event.get("vesting_years"))
        cagr_text = ""
        cagr = None
        if years and years > 0:
            cagr = ((top / price) ** (1.0 / years) - 1.0) * 100.0
            cagr_text = f" — {cagr:.0f}%/yr over {years:.0f} years"
            # A hurdle that sounds huge but only needs a normal rate of return
            # isn't the conviction signal it appears to be.
            if cagr < 15:
                severity -= 1

        out.append(Signal(
            type="HURDLE_CONVICTION",
            direction="BULLISH",
            severity=_clamp(severity),
            evidence=(
                f"{_name(event, 'executive')}: vesting requires ${top:,.2f} "
                f"vs. ${price:,.2f} today (+{appreciation:.0f}%){cagr_text}"
            ),
            data={"executive": _name(event, "executive"), "hurdle_prices": prices,
                  "top_hurdle": top, "current_price": price,
                  "appreciation_pct": round(appreciation, 1),
                  "implied_cagr_pct": round(cagr, 1) if cagr is not None else None,
                  "vesting_years": years},
        ))
    return out


def _detect_off_cycle_grant(facts, context):
    """A grant made outside the company's own annual rhythm.

    Boards grant on a calendar. A grant off that calendar was triggered by
    something — a contract expiry, a retention worry, a view about the price —
    and the trigger is the story. Solo grants to the CEO score higher: the
    post-grant abnormal return in the academic work is roughly 50% larger for
    grants where the chief executive is the only recipient.
    """
    out = []
    cadence = (context or {}).get("grant_cadence") or {}
    annual_months = cadence.get("_annual_months") if isinstance(cadence, dict) else None

    for event in _list(facts, "comp_events"):
        explicit = event.get("is_annual_cycle")
        off_cycle = False
        why = ""

        if explicit is not None and not _truthy(explicit):
            off_cycle = True
            rationale = event.get("grant_rationale")
            why = f"filing calls it {rationale}" if rationale else "filing does not call it an annual grant"
        elif explicit is None and annual_months:
            # Fall back to the company's observed grant months from Form 4
            # history — the cadence the company actually keeps, not the one
            # the filing claims.
            grant_date = _parse_date(event.get("grant_date")) or _parse_date(event.get("filing_date"))
            if grant_date and grant_date.month not in annual_months:
                off_cycle = True
                months = ", ".join(str(m) for m in sorted(annual_months))
                why = f"granted in month {grant_date.month}; this company's grants cluster in month(s) {months}"

        if not off_cycle:
            continue

        severity = SEVERITIES["OFF_CYCLE_GRANT"]
        role = _role(event)
        recipients = _num(event.get("recipient_count"))
        if role == "CEO" and recipients is not None and recipients <= 1:
            severity += 1
            why += "; sole recipient is the CEO"
        if "option" in str(event.get("grant_type") or "").lower():
            severity += 1
            why += "; granted as options (higher leverage to the price)"

        out.append(Signal(
            type="OFF_CYCLE_GRANT",
            direction="BULLISH",
            severity=_clamp(severity),
            evidence=f"Off-cycle grant to {_name(event, 'executive')} — {why}",
            data={"executive": _name(event, "executive"), "role": role,
                  "grant_date": event.get("grant_date"), "why": why},
        ))
    return out


def _detect_oversized_grant(facts, context):
    """A grant far larger than this company's normal award.

    Size is measured two ways because the disclosures are inconsistent: as a
    multiple of the same executive's prior grants (from Form 4 history), and
    as a percentage of market cap. Either can fire; a grant worth 1% of a
    company is a different instrument from the annual cycle and should be
    analyzed as one.
    """
    out = []
    cadence = (context or {}).get("grant_cadence") or {}
    market_cap = _num((context or {}).get("market_cap"))
    multiple_threshold = THRESHOLDS["oversized_multiple_of_prior"]
    pct_threshold = THRESHOLDS["oversized_pct_of_market_cap"]

    for event in _list(facts, "comp_events"):
        who = _name(event, "executive")
        reasons, data = [], {"executive": who}

        shares = _num(event.get("share_count"))
        prior = cadence.get(_cadence_key(who)) if isinstance(cadence, dict) else None
        median_prior = _num((prior or {}).get("median_shares")) if isinstance(prior, dict) else None
        if shares and median_prior and median_prior > 0:
            multiple = shares / median_prior
            if multiple >= multiple_threshold:
                reasons.append(f"{multiple:.1f}x this executive's typical grant "
                               f"({int(shares):,} vs. {int(median_prior):,} shares)")
                data["multiple_of_prior"] = round(multiple, 1)

        value = _num(event.get("grant_value_usd"))
        if value and market_cap and market_cap > 0:
            pct = value / market_cap * 100.0
            if pct >= pct_threshold:
                reasons.append(f"{_money(value)} is {pct:.1f}% of the company's market cap")
                data["pct_of_market_cap"] = round(pct, 2)

        if not reasons:
            continue
        out.append(Signal(
            type="OVERSIZED_GRANT",
            direction="BULLISH",
            severity=_clamp(SEVERITIES["OVERSIZED_GRANT"] + (1 if len(reasons) > 1 else 0)),
            evidence=f"Outsized grant to {who}: {'; '.join(reasons)}",
            data=data,
        ))
    return out


def _cadence_key(name):
    """Normalize an executive name for cadence lookup ('Warren B Kanders' ->
    'warren b kanders'). Form 4 and 8-K spell the same person differently
    often enough that exact matching loses most of the history."""
    return " ".join(str(name or "").lower().split())


def _detect_pre_earnings_grant(facts, context):
    """A grant dated shortly before the company reports.

    The compensation committee sets the grant date. If they set it days ahead
    of a print whose contents they already know, the strike or the starting
    price is being fixed against information the market does not have yet.
    Not an accusation — a date next to a date, which is the fact worth seeing.
    """
    next_earnings = (context or {}).get("next_earnings_date")
    if not next_earnings:
        return []
    window = THRESHOLDS["pre_earnings_grant_days"]

    out = []
    for event in _list(facts, "comp_events"):
        grant_date = event.get("grant_date") or event.get("filing_date") or (context or {}).get("filed_date")
        days = _days_between(grant_date, next_earnings)
        if days is None or days < 0 or days > window:
            continue
        out.append(Signal(
            type="PRE_EARNINGS_GRANT",
            direction="BULLISH",
            severity=_clamp(SEVERITIES["PRE_EARNINGS_GRANT"]),
            evidence=(
                f"{_name(event, 'executive')} granted {_parse_date(grant_date)} — "
                f"{days} days before earnings on {next_earnings}"
            ),
            data={"executive": _name(event, "executive"), "grant_date": str(grant_date),
                  "next_earnings_date": next_earnings, "days_before_earnings": days},
        ))
    return out


def _detect_comp_mix_to_equity(facts, context):
    """A newly structured package weighted toward long-vesting at-risk equity.

    Deliberately excludes the annual cycle. Measured on a 30-filing sample,
    firing on any performance-conditioned multi-year grant hit 27% of filings
    — because that describes essentially every routine annual PSU award at
    every large company. A signal present on a quarter of all filings carries
    no information and, worse, stacked with one other weak hit to push
    ordinary filings through the judge gate.

    Restricted to non-annual packages, it means what it is supposed to mean:
    this executive's pay was just restructured toward equity that only pays if
    the stock does.
    """
    out = []
    min_years = THRESHOLDS["long_vesting_years"]
    for event in _list(facts, "comp_events"):
        if _truthy(event.get("is_annual_cycle")):
            continue
        years = _num(event.get("vesting_years"))
        if not _truthy(event.get("has_performance_condition")) or years is None or years < min_years:
            continue
        out.append(Signal(
            type="COMP_MIX_TO_EQUITY",
            direction="BULLISH",
            severity=_clamp(SEVERITIES["COMP_MIX_TO_EQUITY"]),
            evidence=(
                f"{_name(event, 'executive')}: performance-conditioned award vesting "
                f"over {years:.0f} years"
            ),
            data={"executive": _name(event, "executive"), "vesting_years": years},
        ))
    return out


DETECTORS = (
    _detect_forfeiture_exit,
    _detect_for_cause,
    _detect_abrupt_exit,
    _detect_no_successor,
    _detect_departure_cluster,
    _detect_exit_after_ipo,
    _detect_restatement_context,
    _detect_value_extraction,
    _detect_insider_monetization,
    _detect_exit_near_earnings,
    _detect_friday_night,
    _detect_hurdle_conviction,
    _detect_off_cycle_grant,
    _detect_oversized_grant,
    _detect_pre_earnings_grant,
    _detect_comp_mix_to_equity,
)


# ---------------------------------------------------------------------------
# PASS rules — why a filing is noise, stated in words
# ---------------------------------------------------------------------------

def _pass_reasons(facts, context):
    """Named reasons a filing is routine.

    These do not suppress a signal (a detector hit always wins); they exist so
    that "nothing here" arrives with an explanation instead of a blank. Every
    one of them is a pattern the old system surfaced daily and the user had to
    dismiss by hand.
    """
    reasons = []
    flags = _flags(facts)
    departures = _list(facts, "departures")
    comp_events = _list(facts, "comp_events")

    if _truthy(flags.get("is_annual_meeting_only")):
        reasons.append("Annual meeting results and director elections only")

    if departures and all(_truthy(d.get("is_merger_related")) for d in departures):
        reasons.append("Departures are mechanical consequences of an announced merger")

    if departures and all(
        _truthy(d.get("is_retirement"))
        and _truthy(d.get("successor_named"))
        and (_num(d.get("days_notice")) or 0) >= THRESHOLDS["planned_retirement_min_days"]
        for d in departures
    ):
        reasons.append("Planned retirement announced well ahead with a named successor")

    if comp_events and all(
        _truthy(e.get("is_annual_cycle")) and not (e.get("hurdle_prices") or [])
        for e in comp_events
    ):
        reasons.append("Routine annual grants with no market-based hurdles")

    if _truthy(flags.get("is_plan_share_increase")) and not departures and not comp_events:
        reasons.append("Equity-plan housekeeping (share reserve / ESPP amendment)")

    if _truthy(flags.get("is_financing_only")):
        reasons.append("Financing mechanics only — no executive or compensation content")

    return reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Signals that describe HOW something was disclosed rather than WHAT was
# disclosed. They sharpen a real finding — a forfeiture exit buried on a
# Friday evening is worse than the same exit on a Tuesday — but alone they
# are not a reason to look at anything.
#
# Observed on the first live backtest: three of twelve inbox rows were
# Friday-night filings with nothing else attached, whose entire displayed
# thesis was "Accepted by SEC after Friday's close". A row that cannot say
# what happened is noise however cheap it was to produce.
MODIFIER_ONLY_TYPES = {"FRIDAY_NIGHT_FILING"}


def _drop_lone_modifiers(found):
    """Remove modifier signals when nothing substantive fired alongside them."""
    if any(s.type not in MODIFIER_ONLY_TYPES for s in found):
        return found
    return []


def _dedupe_by_type(found):
    """Collapse repeats of the same signal type into one.

    A filing granting four executives the same package produced four identical
    COMP_MIX_TO_EQUITY signals. That broke two things at once: the dashboard
    row repeated itself, and `len(signals) >= 2` — the judge gate — opened on
    what was really a single observation, sending routine filings to the
    expensive model.

    Keeps the highest-severity instance and records how many there were, since
    "three executives" is itself information.
    """
    best = {}
    counts = {}
    for signal in found:
        counts[signal.type] = counts.get(signal.type, 0) + 1
        current = best.get(signal.type)
        if current is None or signal.severity > current.severity:
            best[signal.type] = signal

    for signal_type, signal in best.items():
        if counts[signal_type] > 1:
            signal.data["occurrences"] = counts[signal_type]
            signal.evidence += f" (+{counts[signal_type] - 1} more in this filing)"
    return list(best.values())


def detect(facts, context=None):
    """Run every detector over one filing's facts and context.

    Never raises on ragged model output: a detector that trips over an
    unexpected shape is skipped and the rest still run, because losing one
    signal is much better than losing the filing.
    """
    context = context or {}
    found = []
    for detector in DETECTORS:
        try:
            found.extend(detector(facts or {}, context) or [])
        except Exception as e:  # pragma: no cover - defensive
            print(f"[SIGNALS] {detector.__name__} failed: {type(e).__name__}: {e}", flush=True)

    found = _drop_lone_modifiers(_dedupe_by_type(found))
    found.sort(key=lambda s: -s.severity)
    return DetectionResult(signals=found, pass_reasons=_pass_reasons(facts or {}, context))


def is_judge_candidate(result):
    """Whether a filing has earned a strong-model read.

    The gate is what keeps the daily cost near a dollar: roughly one filing in
    four clears it. Everything else is ranked by its detectors alone.
    """
    if not result.signals:
        return False
    return (result.max_severity >= JUDGE_GATE["min_max_severity"]
            or len(result.signals) >= JUDGE_GATE["min_signal_count"])


def detector_score(result):
    """Fallback 0-10 score for filings the judge never saw.

    Capped at 6 on purpose: a row that no strong model has read should not be
    able to outrank one that has been read and scored highly.
    """
    if not result.signals:
        return 0
    base = min(2 * result.max_severity, 6)
    # A second corroborating signal is worth something; a fifth is not.
    if len(result.signals) >= 2:
        base = min(base + 1, 6)
    return base


def detector_verdict(result):
    """DEEP_LOOK / MONITOR / PASS from detectors alone.

    A single lowest-severity hit is not worth a slot in the inbox. MONITOR
    means "know that this happened"; earning it requires at least one signal
    the user would recognise as an event.
    """
    if not result.signals or result.max_severity < 2:
        return "PASS"
    return "MONITOR"


def top_signal_line(result):
    """The one line a dashboard row shows when the judge hasn't written one.

    Leads with the strongest signal's evidence and names what else is
    stacked on it — the "why", not just the "what".
    """
    if not result.signals:
        return None
    lead = result.signals[0]
    others = [s.type.replace("_", " ").title() for s in result.signals[1:3]]
    suffix = f" (also: {', '.join(others)})" if others else ""
    return f"{lead.evidence}{suffix}"
