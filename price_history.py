"""Daily closing prices from history — what the scorecard is measured in.

The live quote feed (stock_price.py, API Ninjas) only knows today's price, so
an outcome could only be marked on the day it came due, and a filing stored
by a backfill got a baseline weeks after it was filed. Daily closes let every
mark be taken at the right date, whenever the job happens to run.

Source: Yahoo Finance's public chart endpoint. No key, adjusted closes (splits
and dividends folded in, so a stock and SPY compare on total return). A
failure returns {} and the caller defers the row to the next run — a missing
mark is never filled with a guess.
"""

import math
import time
from datetime import datetime, timedelta, timezone

import requests

_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; 8k-analyzer outcome tracker)"}

# One fetch per ticker per process: the daily job prices every outcome row
# for a ticker from the same series.
_cache = {}


def _yahoo_symbol(ticker):
    """SEC/EDGAR share-class tickers use a dot (BRK.B); Yahoo uses a dash."""
    return ticker.strip().upper().replace(".", "-")


def _epoch(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _splits(splits, offset):
    """[(date, factor)] for each split big enough to detect, where `factor`
    is what a pre-split price is multiplied by to be split-adjusted."""
    found = []
    for split in (splits or {}).values():
        try:
            factor = float(split["denominator"]) / float(split["numerator"])
            day = datetime.fromtimestamp(int(split["date"]) + offset, tz=timezone.utc).strftime("%Y-%m-%d")
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if factor > 0 and abs(math.log(factor)) >= math.log(1.5):
            found.append((day, factor))   # smaller ones can't be told from an ordinary move
    return found


def _is_unadjusted(out, day, factor):
    """Whether the series still jumps by the split ratio across `day` —
    nearer the ratio than no change, in log terms. None if it can't tell."""
    before = [d for d in out if d < day]
    after = [d for d in out if d >= day]
    if not before or not after:
        return None
    jump = out[min(after)] / out[max(before)]
    return abs(math.log(jump) - math.log(factor)) < abs(math.log(jump))


def _apply_splits(out, splits, offset, nominal=False):
    """Make every close consistent with one convention across splits.

    Adjusted (the default, for returns): Yahoo's closes are normally already
    split-adjusted, but right after a split sometimes aren't — NFE's 1:50
    reverse split (2026-09-14) showed up as a 3,600% gain. A split is folded
    in only when the price actually jumps by its ratio, so an already-adjusted
    series is never adjusted twice.

    Nominal (for comparing with a dollar figure in a filing): the price the
    stock actually traded at on each day. An adjusted series is un-adjusted
    for every later split; an unadjusted one is left as it is.
    """
    for day, factor in _splits(splits, offset):
        unadjusted = _is_unadjusted(out, day, factor)
        if unadjusted is None:
            continue
        if not nominal and unadjusted:
            multiplier = factor
        elif nominal and not unadjusted:
            multiplier = 1.0 / factor
        else:
            continue
        for d in [d for d in out if d < day]:
            out[d] *= multiplier
    return out


def _parse(payload, nominal=False):
    """{YYYY-MM-DD: close} from a chart response, local exchange dates.

    Adjusted closes (dividends and splits) by default: what a holder earned,
    which is what the scorecard measures. `nominal=True` gives the traded
    price instead, which is what a hurdle price in a filing is written in.
    """
    result = ((payload or {}).get("chart") or {}).get("result") or []
    if not result:
        return {}
    series = result[0]
    offset = (series.get("meta") or {}).get("gmtoffset") or 0
    stamps = series.get("timestamp") or []
    indicators = series.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0].get("close")
    adjusted = (indicators.get("adjclose") or [{}])[0].get("adjclose")
    closes = (quote if nominal else (adjusted or quote)) or []
    out = {}
    for ts, close in zip(stamps, closes):
        if isinstance(close, (int, float)) and close > 0:
            day = datetime.fromtimestamp(ts + offset, tz=timezone.utc).strftime("%Y-%m-%d")
            out[day] = float(close)
    return _apply_splits(out, (series.get("events") or {}).get("splits"), offset, nominal=nominal)


def fetch_closes(ticker, start, end, nominal=False):
    """Daily closes for [start, end] (YYYY-MM-DD), or {} on failure.
    Adjusted unless `nominal` (see _parse)."""
    params = {
        "period1": _epoch(start),
        "period2": _epoch(end) + 86400,
        "interval": "1d",
        "includeAdjustedClose": "true",
        "events": "div,splits",
    }
    symbol = _yahoo_symbol(ticker)
    for attempt, host in enumerate(_HOSTS):
        try:
            resp = requests.get(f"{host}/v8/finance/chart/{symbol}",
                                params=params, headers=_HEADERS, timeout=15)
            if resp.status_code == 404:
                return {}  # unknown symbol: another host won't know it either
            if resp.status_code == 200:
                return _parse(resp.json(), nominal=nominal)
            print(f"[PRICE HISTORY] {symbol}: HTTP {resp.status_code} from {host}", flush=True)
        except Exception as e:
            print(f"[PRICE HISTORY] {symbol}: {type(e).__name__}: {e}", flush=True)
        if attempt + 1 < len(_HOSTS):
            time.sleep(1.0)
    return {}


def closes(ticker, start, end=None, nominal=False):
    """Cached fetch_closes. `end` defaults to today; the cache covers the
    widest window asked for so far."""
    end = end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = (ticker.strip().upper(), nominal)
    hit = _cache.get(key)
    if hit and hit[0] <= start and hit[1] >= end:
        return hit[2]
    series = fetch_closes(key[0], start, end, nominal=nominal)
    if series:
        _cache[key] = (start, end, series)
    return series


def close_on_or_after(series, day, before=None):
    """(date, close) for the first trading day on or after `day`.

    `before` bounds it: a close dated on or after `before` is not used, which
    is how the caller keeps today's still-moving bar out of a mark.
    """
    for d in sorted(series):
        if d < day:
            continue
        if before and d >= before:
            return None
        return d, series[d]
    return None


def close_on_or_before(series, day):
    """(date, close) for the last trading day on or before `day` — the price
    the market had set when a filing dated `day` came out."""
    for d in sorted(series, reverse=True):
        if d <= day:
            return d, series[d]
    return None


def add_days(day, n):
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def clear_cache():
    _cache.clear()
