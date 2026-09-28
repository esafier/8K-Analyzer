"""What a price-hurdle grant pays, and what it takes to get paid.

The Market Targets badge says a grant *has* a price hurdle. That is not the
signal. The signal is how hard the board made it: where the stock has to go,
by when, what rate of return that implies, and how much of the executive's
money rides on it. "$21.50 / $41.00 / $61.50 VWAP" reads as a list of
numbers; "+181% by Sep 2031, 23%/yr, first $5.2M" reads as a bet.

build_payoffs() turns stored extraction facts into one payoff ladder per
executive:

    price level → move needed → CAGR needed by the deadline → what unlocks
                → the executive's cumulative take at that price → rough odds

It reads the same `comp_events` every other stage reads (stored facts, zero
model calls), so it works on every filing already in the database. v4.5
extraction adds structured `price_hurdles` / `performance_period_years` /
`exercise_price` / `base_salary_usd`; older rows fall back to parsing the
free-text hurdle description, and anything that can't be parsed is shown as
unknown rather than guessed.
"""

import math
import re
import threading
from datetime import date, datetime, timedelta

from market_targets import price_matches

# Long-run equity return assumed by the odds estimate. Zero drift would call
# every hurdle less likely than it is; a stock's own trailing return would
# call last year's winners sure things.
ASSUMED_ANNUAL_RETURN = 0.08

# Required-CAGR bands for the difficulty label. The market compounds at
# roughly 10%/yr; sustaining 35%+ for years is what very few stocks do.
_DIFFICULTY = ((10.0, "Market pace"), (20.0, "Stretch"), (35.0, "Hard"))
_MOONSHOT = "Moonshot"

_OPTION_WORDS = ("option", "sar", "stock appreciation")
_EQUITY_WORDS = ("rsu", "psu", "restricted", "performance share", "performance stock",
                 "performance unit", "stock unit", "stock award", "share award", "equity",
                 "shares")
_SALARY_WORDS = ("salary",)

_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10}

_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)", re.IGNORECASE)
_WORD_FRACTIONS = (
    (re.compile(r"\btwo[- ]thirds\b", re.I), 2 / 3),
    (re.compile(r"\b(?:one|a)[- ]third\b|\b1/3\b", re.I), 1 / 3),
    (re.compile(r"\b(?:one|a)[- ](?:quarter|fourth)\b|\b1/4\b", re.I), 1 / 4),
    (re.compile(r"\b(?:one[- ])?half\b|\b1/2\b", re.I), 1 / 2),
)
_REMAINDER_RE = re.compile(r"\b(?:remaining|remainder|balance)\b", re.I)
_EQUAL_RE = re.compile(r"\bequal(?:ly)?\b|\bpro[- ]rata\b|\bratabl[ey]\b", re.I)
_PERIOD_RE = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ]year\s+"
    r"(?:performance|measurement)\s+period", re.I)
_DAYS_RE = re.compile(
    r"\b(\d+|twenty|thirty|sixty|ninety)[- ](?:consecutive[- ])?(?:trading[- ])?day\b", re.I)
_MEASURE_KIND_RE = re.compile(r"\b(vwap|volume[- ]weighted|average|closing)", re.I)
_EXERCISE_RE = re.compile(r"(?:exercise|strike)\s+price[^$.;]{0,60}", re.I)
_UNITS_RE = re.compile(
    r"(?<![$\d.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(million|thousand|m\b|k\b)?"
    r"(?!\s*%)(?![\d,.])", re.I)


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------

def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _text(value):
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("null", "none", "n/a") else s


def _date(value):
    s = _text(value)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _add_years(d, years):
    whole = int(years)
    try:
        out = d.replace(year=d.year + whole)
    except ValueError:  # Feb 29
        out = d.replace(year=d.year + whole, day=28)
    return out + timedelta(days=round((years - whole) * 365.25))


def parse_units(event, price_at_grant=None):
    """(unit count, approximate?) for one comp event.

    share_count when the model gave one; otherwise the first bare number in
    grant_value that isn't a dollar amount or a percentage ("800,000
    performance-based restricted stock units"). A dollar-only grant ("$5.0
    million in PSUs") is converted at the grant-date price and flagged as an
    approximation — that is how target units are sized, but it is our
    arithmetic, not the filing's number.
    """
    units = _num(event.get("share_count"))
    if units and units > 0:
        return units, False
    text = _text(event.get("grant_value"))
    for m in _UNITS_RE.finditer(text):
        v = _num(m.group(1))
        scale = (m.group(2) or "").lower()
        if scale in ("million", "m"):
            v *= 1_000_000
        elif scale in ("thousand", "k"):
            v *= 1_000
        if v and v >= 100:
            return v, False
    usd = _num(event.get("grant_value_usd"))
    if usd and usd > 0 and price_at_grant:
        return usd / price_at_grant, True
    return None, False


def _fraction_in(segment, take_last):
    """A tranche fraction named in a stretch of text, or 'rest', or None."""
    pcts = _PCT_RE.findall(segment)
    if pcts:
        return float(pcts[-1] if take_last else pcts[0]) / 100.0
    for pattern, value in _WORD_FRACTIONS:
        if pattern.search(segment):
            return value
    if _REMAINDER_RE.search(segment):
        return "rest"
    return None


def _resolve(fractions):
    """Fill a single 'rest' and validate. None unless every tranche is known
    and they sum to no more than the whole award."""
    if any(f is None for f in fractions) or fractions.count("rest") > 1:
        return None
    known = sum(f for f in fractions if f != "rest")
    fractions = [max(0.0, 1.0 - known) if f == "rest" else f for f in fractions]
    total = sum(fractions)
    if total <= 0 or total > 1.03 or any(f <= 0 for f in fractions):
        return None
    return fractions


def parse_tranches(text):
    """[(price, fraction or None)] from a free-text hurdle description.

    Handles the two ways filings write it:
      "30% vests at $21.50, an additional 30% at $41.00, and the remaining
       40% at $61.50"                       (fraction before each price)
      "$20.00 (50% of the units) and $30.00 (50%)"   (fraction after)
    plus "in three equal tranches". Returns fractions of None when the split
    can't be read — the caller shows it as unknown rather than guessing.
    """
    text = _text(text)
    matches = price_matches(text)
    if not matches:
        return []
    prices = [v for v, _, _ in matches]

    before, after = [], []
    for i, (_, start, end) in enumerate(matches):
        prev_end = matches[i - 1][2] if i else 0
        next_start = matches[i + 1][1] if i + 1 < len(matches) else len(text)
        before.append(_fraction_in(text[prev_end:start], take_last=True))
        after.append(_fraction_in(text[end:next_start], take_last=False))

    for candidate in (before, after):
        resolved = _resolve(candidate)
        if resolved:
            return list(zip(prices, resolved))
    if len(prices) > 1 and _EQUAL_RE.search(text):
        return [(p, 1.0 / len(prices)) for p in prices]
    return [(p, None) for p in prices]


def _period_years(event):
    for key in ("performance_period_years",):
        v = _num(event.get(key))
        if v and v > 0:
            return v
    for key in ("vesting_schedule", "market_based_targets", "stock_price_targets"):
        m = _PERIOD_RE.search(_text(event.get(key)))
        if m:
            word = m.group(1).lower()
            return float(_WORD_NUMBERS.get(word) or word)
    v = _num(event.get("vesting_years"))
    return v if v and v > 0 else None


def _measurement(event):
    m = _text(event.get("hurdle_measurement"))
    if m:
        return m
    blob = " ".join(_text(event.get(k)) for k in ("vesting_schedule", "stock_price_targets"))
    blob += " " + _text((event.get("market_based_targets") or {}).get("stock_price")
                        if isinstance(event.get("market_based_targets"), dict) else "")
    # Either order: "60-trading-day VWAP" or "VWAP ... over a 60-trading-day period".
    days, kind = _DAYS_RE.search(blob), _MEASURE_KIND_RE.search(blob)
    if not days or not kind:
        return None
    n = days.group(1).lower()
    n = {"twenty": "20", "thirty": "30", "sixty": "60", "ninety": "90"}.get(n, n)
    k = kind.group(1).lower()
    k = "VWAP" if k.startswith(("vwap", "volume")) else f"{k} price"
    return f"{n}-day {k}"


def _hurdle_texts(event):
    mbt = event.get("market_based_targets")
    sp = mbt.get("stock_price") if isinstance(mbt, dict) else None
    return [_text(sp), _text(event.get("stock_price_targets")),
            _text(event.get("vesting_schedule"))]


def _event_prices(event):
    """(tranches, split_known, strike) for one comp event.

    Structured `price_hurdles` wins; then whichever free-text field states a
    complete split; then bare `hurdle_prices` with the split unknown.
    """
    tranches, strike = [], _num(event.get("exercise_price"))

    structured = event.get("price_hurdles")
    if isinstance(structured, list) and structured:
        rows = []
        for h in structured:
            if not isinstance(h, dict):
                continue
            p = _num(h.get("price"))
            if p and p > 0:
                pct = _num(h.get("vest_pct"))
                rows.append((p, pct / 100.0 if pct else None, _num(h.get("units"))))
        if rows:
            return rows, all(r[1] or r[2] for r in rows), strike

    texts = _hurdle_texts(event)
    if strike is None:
        for t in texts:
            ex = _EXERCISE_RE.search(t)
            if ex:
                tail = t[ex.start():ex.end() + 20]
                vals = [v for v, _, _ in price_matches(tail)]
                if vals:
                    strike = vals[0]
                    break

    best = []
    for t in texts:
        parsed = [(p, f) for p, f in parse_tranches(t) if strike is None or abs(p - strike) > 0.005]
        if parsed and all(f is not None for _, f in parsed):
            best = parsed
            break
        if len(parsed) > len(best):
            best = parsed

    raw_listed = event.get("hurdle_prices")
    listed = ([p for p in (_num(v) for v in raw_listed) if p and p > 0]
              if isinstance(raw_listed, list) else [])
    known = {round(p, 2) for p, _ in best}
    for p in listed:
        if round(p, 2) not in known and (strike is None or abs(p - strike) > 0.005):
            best.append((p, None))
            known.add(round(p, 2))

    # A split that covers only some of the prices isn't a split.
    split_known = bool(best) and all(f is not None for _, f in best)
    if not split_known:
        best = [(p, None) for p, _ in best]
    tranches = [(p, f, None) for p, f in sorted(best)]
    return tranches, split_known, strike


_PRICE_VESTED_RE = re.compile(r"\b(?:vwap|volume[- ]weighted|closing price|stock price|share price|"
                              r"price hurdle|price target|market[- ]based|reach(?:es)?|achiev)", re.I)


def _looks_price_vested(event):
    return any(_PRICE_VESTED_RE.search(t) for t in _hurdle_texts(event)[1:])


def _kind(event):
    blob = f"{_text(event.get('grant_type'))} {_text(event.get('grant_value'))}".lower()
    if any(w in blob for w in _OPTION_WORDS):
        return "option"
    if any(w in _text(event.get("grant_type")).lower() for w in _SALARY_WORDS):
        return "salary"
    if any(w in blob for w in _EQUITY_WORDS):
        return "equity"
    return None


def _person(executive):
    """(name, title) from 'Wayne Paterson, Vice Chairman and CEO'."""
    s = _text(executive) or "Executive"
    s = re.sub(r"\s*\(.*?\)\s*", " ", s).strip()
    name, _, title = s.partition(",")
    return name.strip(), title.strip()


def _tokens(name):
    tokens = {t for t in re.findall(r"[a-z]+", name.lower()) if len(t) > 1}
    return tokens or {name.lower().strip()}


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def required_cagr(target, start, years):
    """Annual % return needed to go from `start` to `target` in `years`."""
    if not target or not start or start <= 0 or not years or years <= 0:
        return None
    return ((target / start) ** (1.0 / years) - 1.0) * 100.0


def touch_probability(spot, barrier, years, vol, annual_return=ASSUMED_ANNUAL_RETURN):
    """Chance a lognormal price touches `barrier` at some point within `years`.

    First-passage probability for geometric Brownian motion with the stock's
    own volatility and an ordinary equity return. A hurdle measured as a
    60-day average is harder to hit than a one-day touch, so read this as an
    upper bound — its job is to separate "plausible" from "lottery ticket",
    not to price the award.
    """
    if not spot or not barrier or not years or not vol or spot <= 0 or years <= 0 or vol <= 0:
        return None
    b = math.log(barrier / spot)
    if b <= 0:
        return 1.0
    nu = math.log(1.0 + annual_return) - vol * vol / 2.0
    s = vol * math.sqrt(years)
    phi = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))  # noqa: E731
    p = phi((-b + nu * years) / s) + math.exp(2.0 * nu * b / (vol * vol)) * phi((-b - nu * years) / s)
    return max(0.0, min(1.0, p))


def annualized_volatility(series):
    """Annualized stdev of daily log returns from a {date: close} series."""
    closes = [c for _, c in sorted(series.items()) if c and c > 0]
    if len(closes) < 120:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


_vol_cache = {}


def volatility_for(ticker, timeout=3.0):
    """Trailing one-year volatility, without ever stalling a page render.

    The history fetch runs in a thread; if it doesn't answer within
    `timeout` the page renders without odds and the thread finishes into the
    cache for the next view. None on any failure.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None
    today = date.today().isoformat()
    hit = _vol_cache.get(ticker)
    if hit and hit[0] == today:
        return hit[1]

    def work():
        try:
            import price_history
            start = (date.today() - timedelta(days=380)).isoformat()
            _vol_cache[ticker] = (today, annualized_volatility(price_history.closes(ticker, start)))
        except Exception as e:  # odds are a nicety; never break the page
            print(f"[PAYOFF] volatility for {ticker} failed: {e}", flush=True)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    hit = _vol_cache.get(ticker)
    return hit[1] if hit else None


def difficulty(cagr, pct):
    if pct is not None and pct <= 0:
        return "Already met"
    if cagr is None:
        return None
    for ceiling, label in _DIFFICULTY:
        if cagr <= ceiling:
            return label
    return _MOONSHOT


def money(value):
    """$1.2B / $15.7M / $450K / $900 — the scale the eye needs, no more."""
    if value is None:
        return "—"
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{sign}${v / 1e3:.0f}K"
    return f"{sign}${v:,.0f}"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _awards_for(events, price_at_grant, filed):
    """Normalize one executive's comp events into awards the ladder can value."""
    awards, salary, bonus_pct, bonus_usd = [], None, None, None
    for ev in events:
        s = _num(ev.get("base_salary_usd"))
        if s and s > 0:
            salary = max(salary or 0, s)
        bp = _num(ev.get("target_bonus_pct"))
        if bp and bp > 0:
            bonus_pct = bp
        kind = _kind(ev)
        if kind == "salary":
            s = _num(ev.get("grant_value_usd"))
            if s and s > 0:
                salary = max(salary or 0, s)
            continue
        if kind is None:
            continue

        units, units_approx = parse_units(ev, price_at_grant)
        tranches, split_known, strike = _event_prices(ev)
        grant_date = _date(ev.get("grant_date")) or filed
        if kind == "option":
            strike_approx = False
            if strike is None and len(tranches) == 1 and not _looks_price_vested(ev):
                # One price on an option with no hurdle language is its strike.
                strike, tranches = tranches[0][0], []
            if strike is None:
                strike, strike_approx = price_at_grant, True
                # hurdle_prices carries exercise prices too (prompt asks for
                # them). An at-the-money strike is not a hurdle.
                if strike:
                    tranches = [t for t in tranches if t[0] > strike * 1.03]
            else:
                tranches = [t for t in tranches if t[0] > strike + 0.005]
            split_known = split_known and bool(tranches)
            hurdle_kind = "option"
        else:
            strike_approx = False
            hurdle_kind = "hurdle" if tranches else "time"

        years = _period_years(ev) if tranches else None
        deadline = _date(ev.get("hurdle_deadline"))
        if deadline is None and years and grant_date:
            deadline = _add_years(grant_date, years)

        # Unit-level tranches from vest_pct or explicit units.
        resolved = []
        for p, frac, tranche_units in tranches:
            if frac is None and tranche_units and units:
                frac = tranche_units / units
            resolved.append({"price": p, "fraction": frac})

        awards.append({
            "kind": hurdle_kind,
            "grant_type": _text(ev.get("grant_type")) or ("Options" if kind == "option" else "Equity"),
            "units": units,
            "units_approx": units_approx,
            "strike": strike,
            "strike_approx": strike_approx,
            "tranches": resolved,
            "split_known": split_known and all(t["fraction"] for t in resolved),
            "deadline": deadline,
            "period_years": years,
            "measurement": _measurement(ev) if tranches else None,
            "vesting": _text(ev.get("vesting_schedule")),
        })
    if bonus_pct and salary:
        bonus_usd = salary * bonus_pct / 100.0
    return awards, salary, bonus_usd


def _vested_fraction(award, price):
    """Share of the award earned once the stock sits at `price`."""
    if not award["tranches"]:
        return 1.0
    if not award["split_known"]:
        # Unknown split: nothing is certain until the top hurdle clears it all.
        return 1.0 if price >= max(t["price"] for t in award["tranches"]) - 1e-9 else 0.0
    return min(1.0, sum(t["fraction"] for t in award["tranches"] if price >= t["price"] - 1e-9))


def _value_at(award, price):
    if not award["units"]:
        return None
    earned = award["units"] * _vested_fraction(award, price)
    if award["kind"] == "option":
        if award["strike"] is None:
            return None
        return earned * max(0.0, price - award["strike"])
    return earned * price


def _unlock_lines(awards, price):
    lines = []
    for a in awards:
        for t in a["tranches"]:
            if abs(t["price"] - price) > 0.005:
                continue
            label = "options" if a["kind"] == "option" else a["grant_type"]
            if t["fraction"] and a["units"]:
                lines.append(f"{t['fraction'] * 100:.0f}% of {label} · "
                             f"{a['units'] * t['fraction']:,.0f} units")
            elif t["fraction"]:
                lines.append(f"{t['fraction'] * 100:.0f}% of {label}")
            else:
                lines.append(f"{label} tier (split not disclosed)")
    return lines


def _package_line(a):
    units = f"{'~' if a['units_approx'] else ''}{a['units']:,.0f} " if a["units"] else ""
    if a["kind"] == "option":
        strike = (f" @ {'~' if a['strike_approx'] else ''}${a['strike']:,.2f}"
                  if a["strike"] else "")
        tail = " · price-vested" if a["tranches"] else ""
        return f"{units}options{strike}{tail}"
    if a["kind"] == "hurdle":
        n = len(a["tranches"])
        yrs = f", {a['period_years']:g}-yr window" if a["period_years"] else ""
        return f"{units}{a['grant_type']} · {n} price hurdle{'s' if n != 1 else ''}{yrs}"
    return f"{units}{a['grant_type']} · time-vested"


def build_payoffs(structured, price_at_grant=None, price_today=None, filed_date=None,
                  today=None, volatility=None):
    """One payoff ladder per executive whose package has a stock-price hurdle.

    Args:
        structured: the stored structured_summary dict (needs comp_events).
        price_at_grant: share price when the filing came out (price_at_ingest).
        price_today: current share price; falls back to price_at_grant.
        filed_date: YYYY-MM-DD, used when a comp event has no grant_date.
        today: date override for tests.
        volatility: annualized vol (0.65 = 65%) for the odds column, or None.

    Returns [] when nothing is computable — no hurdle, or no price to measure
    it from. Never raises on malformed facts.
    """
    if not isinstance(structured, dict):
        return []
    events = [e for e in (structured.get("comp_events") or []) if isinstance(e, dict)]
    if not events:
        return []
    price_at_grant = _num(price_at_grant) or None
    price_today = _num(price_today) or None
    spot = price_today or price_at_grant
    if not spot:
        return []
    today = today or date.today()
    filed = _date(filed_date) or today

    # Group events by person. Filings name the same executive two ways
    # ("Wayne Paterson, Vice Chairman and CEO" / "Wayne Paterson").
    groups = []
    for ev in events:
        name, title = _person(ev.get("executive"))
        toks = _tokens(name)
        for g in groups:
            if toks and g["tokens"] and (toks <= g["tokens"] or g["tokens"] <= toks):
                g["events"].append(ev)
                if len(title) > len(g["title"]):
                    g["title"] = title
                break
        else:
            groups.append({"name": name, "title": title, "tokens": toks, "events": [ev]})

    out = []
    for g in groups:
        try:
            ladder = _ladder(g, price_at_grant, price_today, spot, filed, today, volatility)
        except (TypeError, ValueError, ZeroDivisionError, OverflowError) as e:
            print(f"[PAYOFF] skipped {g['name']}: {e}", flush=True)
            ladder = None
        if ladder:
            out.append(ladder)
    # Biggest bet first.
    out.sort(key=lambda x: -(x["top_payout"] or 0))
    return out


def _ladder(group, price_at_grant, price_today, spot, filed, today, vol):
    awards, salary, bonus = _awards_for(group["events"], price_at_grant or spot, filed)
    hurdle_awards = [a for a in awards if a["tranches"]]
    if not hurdle_awards:
        return None
    annual_pay = (salary or 0) + (bonus or 0) or None

    levels = sorted({round(t["price"], 4) for a in hurdle_awards for t in a["tranches"]})
    deadlines = sorted({a["deadline"] for a in hurdle_awards if a["deadline"]})

    def payout(price):
        vals = [_value_at(a, price) for a in awards]
        known = [v for v in vals if v is not None]
        return sum(known) if known else None

    def row(label, price, is_today=False):
        deadline = None
        if not is_today:
            ds = [a["deadline"] for a in hurdle_awards if a["deadline"]
                  and any(abs(t["price"] - price) < 0.005 for t in a["tranches"])]
            deadline = min(ds) if ds else None
        years_left = ((deadline - today).days / 365.25) if deadline else None
        pct_today = (price / spot - 1.0) * 100.0
        pct_grant = (price / price_at_grant - 1.0) * 100.0 if price_at_grant else None
        cagr = required_cagr(price, spot, years_left) if years_left and years_left > 0.05 else None
        board_years = ((deadline - filed).days / 365.25) if deadline else None
        cagr_grant = (required_cagr(price, price_at_grant, board_years)
                      if price_at_grant and board_years and board_years > 0.05 else None)
        value = payout(price)
        breakdown = []
        for award_kind, part in (("hurdle", "PSUs"), ("option", "options"), ("time", "time-vested")):
            v = sum(_value_at(a, price) or 0 for a in awards if a["kind"] == award_kind)
            if v > 0:
                breakdown.append(f"{part} {money(v)}")
        odds = touch_probability(spot, price, years_left, vol) if (years_left and not is_today) else None
        return {
            "label": label,
            "price": price,
            "is_today": is_today,
            "pct_today": pct_today,
            "pct_grant": pct_grant,
            "cagr": cagr,
            "cagr_from_grant": cagr_grant,
            "deadline": deadline,
            "years_left": years_left,
            "difficulty": None if is_today else difficulty(cagr, pct_today),
            "unlocks": [] if is_today else _unlock_lines(awards, price),
            "payout": value,
            "breakdown": breakdown if len(breakdown) > 1 else [],
            "pay_multiple": (value / annual_pay) if (value is not None and annual_pay) else None,
            "odds": odds,
        }

    rows = [row("Today", spot, is_today=True)]
    prev = rows[0]["payout"]
    for p in levels:
        r = row(f"${p:,.2f}", p)
        r["increment"] = (r["payout"] - prev) if (r["payout"] is not None and prev is not None) else None
        prev = r["payout"]
        rows.append(r)

    top, first = rows[-1], rows[1]
    at_risk = None
    if top["payout"]:
        time_based = sum(_value_at(a, top["price"]) or 0 for a in awards if a["kind"] == "time")
        at_risk = max(0.0, min(100.0, (1.0 - time_based / top["payout"]) * 100.0))

    return {
        "executive": group["name"],
        "title": group["title"],
        "awards": awards,
        "package": [_package_line(a) for a in awards],
        "rows": rows,
        "first": first,
        "top": top,
        "top_payout": top["payout"],
        "value_today": rows[0]["payout"],
        "at_risk_pct": at_risk,
        "annual_pay": annual_pay,
        "salary": salary,
        "deadline": deadlines[0] if len(deadlines) == 1 else None,
        "mixed_deadlines": len(deadlines) > 1,
        "measurement": next((a["measurement"] for a in hurdle_awards if a["measurement"]), None),
        "split_known": all(a["split_known"] for a in hurdle_awards),
        "units_approx": any(a["units_approx"] for a in awards),
        "strike_approx": any(a["strike_approx"] for a in awards if a["kind"] == "option"),
        "price_at_grant": price_at_grant,
        "price_today": price_today,
        "spot": spot,
        "volatility": vol,
        "headline": _headline(group["name"], first, top, rows[0]["payout"], annual_pay),
    }


def _headline(name, first, top, value_today, annual_pay):
    """The sentence that answers 'how strong a bet is this?'"""
    parts = []
    when = f" by {top['deadline']:%b %Y}" if top["deadline"] else ""
    rate = f", {top['cagr']:.0f}%/yr" if top["cagr"] is not None else ""
    if top["payout"] is not None:
        parts.append(f"{name} makes up to {money(top['payout'])} if the stock reaches "
                     f"${top['price']:,.2f}{when} ({top['pct_today']:+.0f}% from today{rate}).")
    else:
        parts.append(f"{name}'s top hurdle is ${top['price']:,.2f}{when} "
                     f"({top['pct_today']:+.0f}% from today{rate}).")
    if first is not top and first["pct_today"] > 0:
        first_rate = f", {first['cagr']:.0f}%/yr" if first["cagr"] is not None else ""
        parts.append(f"The first tier needs {first['pct_today']:+.0f}%{first_rate}.")
    if value_today is not None and top["payout"]:
        parts.append(f"Worth {money(value_today)} at today's price.")
    if annual_pay and top["payout"]:
        parts.append(f"Top payout is {top['payout'] / annual_pay:.0f}× annual cash pay.")
    return " ".join(parts)
