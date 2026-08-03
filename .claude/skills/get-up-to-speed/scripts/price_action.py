#!/usr/bin/env python3
"""Analyze target and comparison price action without emitting raw daily series."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-^=]{0,19}$")


def validate_ticker(value: str) -> str:
    ticker = str(value).strip().upper()
    if not TICKER_RE.fullmatch(ticker):
        raise ValueError(f"invalid ticker: {value!r}")
    return ticker


def validate_positive(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return number


def atomic_write_text(path: str | Path, text: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def fetch_closes(ticker: str, start: str, end: str):
    """Fetch adjusted closes. yfinance end is exclusive."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("missing optional dependency yfinance; install it to fetch live prices") from exc
    frame = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True, threads=False)
    if frame is None or len(frame) == 0 or "Close" not in frame:
        return None
    closes = frame["Close"]
    try:
        import pandas as pd
        if isinstance(closes, pd.DataFrame):
            closes = closes.iloc[:, 0]
    except ImportError:
        pass
    return closes.dropna()


def fetch_fundamentals(ticker: str) -> dict[str, Any]:
    """Return yfinance snapshot values as optional, unverified leads."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).get_info() or {}
    except Exception:
        return {}
    market_cap = info.get("marketCap")
    enterprise_value = info.get("enterpriseValue")
    revenue = info.get("totalRevenue")
    return {
        "market_cap": market_cap,
        "enterprise_value": enterprise_value,
        "shares_out": info.get("sharesOutstanding"),
        "float_shares": info.get("floatShares"),
        "short_pct_float": info.get("shortPercentOfFloat"),
        "days_to_cover": info.get("shortRatio"),
        "insider_pct": info.get("heldPercentInsiders"),
        "institution_pct": info.get("heldPercentInstitutions"),
        "ttm_revenue": revenue,
        "ev_to_ttm_rev": round(enterprise_value / revenue, 2) if enterprise_value and revenue else None,
        "wk52_high": info.get("fiftyTwoWeekHigh"),
        "wk52_low": info.get("fiftyTwoWeekLow"),
    }


def _pct(last: float, first: float) -> float | None:
    if first == 0:
        return None
    return round((last / first - 1.0) * 100, 1)


def _normalize_series(values):
    if values is None:
        return None
    import pandas as pd

    numeric = pd.to_numeric(values, errors="coerce")
    cleaned = numeric.dropna().sort_index()
    cleaned = cleaned[cleaned.map(lambda value: math.isfinite(float(value)) and float(value) > 0)]
    if getattr(cleaned.index, "tz", None) is not None:
        cleaned.index = cleaned.index.tz_localize(None)
    cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    return cleaned.astype(float)


def _json_safe(value):
    """Replace optional non-finite snapshot values before strict JSON serialization."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _return_block(target, comparison=None, lookback_days: int | None = None):
    import pandas as pd

    if comparison is None:
        aligned = target
    else:
        aligned = pd.concat([target.rename("target"), comparison.rename("comparison")], axis=1, join="inner").dropna()
        if len(aligned) < 2:
            return None
    if lookback_days is not None:
        cutoff = aligned.index[-1] - pd.Timedelta(days=lookback_days)
        aligned = aligned[aligned.index >= cutoff]
    if len(aligned) < 2:
        return None
    first_date, last_date = aligned.index[0], aligned.index[-1]
    if comparison is None:
        return {
            "start": first_date.strftime("%Y-%m-%d"),
            "end": last_date.strftime("%Y-%m-%d"),
            "target_pct": _pct(float(aligned.iloc[-1]), float(aligned.iloc[0])),
        }
    target_pct = _pct(float(aligned["target"].iloc[-1]), float(aligned["target"].iloc[0]))
    comparison_pct = _pct(float(aligned["comparison"].iloc[-1]), float(aligned["comparison"].iloc[0]))
    return {
        "start": first_date.strftime("%Y-%m-%d"),
        "end": last_date.strftime("%Y-%m-%d"),
        "target_pct": target_pct,
        "comparison_pct": comparison_pct,
        "excess_pct": round(target_pct - comparison_pct, 1) if target_pct is not None and comparison_pct is not None else None,
    }


def _weekly_actual_dates(closes):
    rows = []
    periods = closes.index.to_period("W-FRI")
    for _, group in closes.groupby(periods):
        actual_date = group.index[-1]
        rows.append({"date": actual_date.strftime("%Y-%m-%d"), "close": round(float(group.iloc[-1]), 2)})
    return rows


def analyze(
    ticker: str,
    years: float,
    benchmarks: list[str],
    peers: list[str],
    sigma: float,
    floor: float,
    *,
    fetcher: Callable = fetch_closes,
    fundamentals_fetcher: Callable = fetch_fundamentals,
    today: str | date | None = None,
) -> dict[str, Any]:
    ticker = validate_ticker(ticker)
    years = validate_positive(years, "years")
    sigma = validate_positive(sigma, "sigma")
    floor = validate_positive(floor, "floor")
    comparisons = [validate_ticker(item) for item in benchmarks + peers if item]
    current = date.fromisoformat(today) if isinstance(today, str) else today
    current = current or datetime.now(timezone.utc).date()
    # yfinance's end parameter is exclusive, so add one day to include today's bar if posted.
    fetch_end = current + timedelta(days=1)
    fetch_start = current - timedelta(days=int(years * 365.25) + 7)
    diagnostics: list[str] = []
    try:
        closes = _normalize_series(fetcher(ticker, fetch_start.isoformat(), fetch_end.isoformat()))
    except Exception as exc:
        diagnostics.append(f"{ticker}: {type(exc).__name__}: {exc}")
        closes = None
    if closes is None or len(closes) < 2:
        count = 0 if closes is None else len(closes)
        diagnostics.append(f"{ticker}: received {count} usable bars")
        return {
            "error": f"could not retrieve a usable target price series for {ticker}",
            "diagnostics": diagnostics,
            "requested": {"start": fetch_start.isoformat(), "end_exclusive": fetch_end.isoformat()},
        }

    returns = closes.pct_change().dropna()
    standard_deviation = float(returns.std()) if len(returns) > 1 else 0.0
    if not math.isfinite(standard_deviation):
        standard_deviation = 0.0
    move_bar = max(sigma * standard_deviation, floor)
    flagged = []
    for trading_date, daily_return in returns.items():
        value = float(daily_return)
        if abs(value) >= move_bar:
            flagged.append({
                "date": trading_date.strftime("%Y-%m-%d"),
                "return_pct": round(value * 100, 1),
                "close": round(float(closes.loc[trading_date]), 2),
                "direction": "up" if value > 0 else "down",
            })
    flagged.sort(key=lambda row: abs(row["return_pct"]), reverse=True)

    running_max = closes.cummax()
    drawdowns = closes / running_max - 1.0
    trough_date = drawdowns.idxmin()
    peak_date = closes.loc[:trough_date].idxmax()
    running_min = closes.cummin()
    runups = closes / running_min - 1.0
    top_date = runups.idxmax()
    bottom_date = closes.loc[:top_date].idxmin()

    target_performance = {
        "window": _return_block(closes),
        "trailing_1y": _return_block(closes, lookback_days=365),
    }
    comparison_results: dict[str, Any] = {}
    for symbol in comparisons:
        try:
            other = _normalize_series(fetcher(symbol, fetch_start.isoformat(), fetch_end.isoformat()))
        except Exception as exc:
            diagnostics.append(f"{symbol}: {type(exc).__name__}: {exc}")
            other = None
        if other is None:
            comparison_results[symbol] = {"status": "unavailable", "diagnostic": "no usable series"}
            continue
        window = _return_block(closes, other)
        trailing = _return_block(closes, other, lookback_days=365)
        if window is None:
            comparison_results[symbol] = {"status": "unavailable", "diagnostic": "fewer than two common trading dates"}
        else:
            comparison_results[symbol] = {"status": "ok", "window": window, "trailing_1y": trailing}

    try:
        supplemental = fundamentals_fetcher(ticker) or {}
    except Exception as exc:
        diagnostics.append(f"{ticker} fundamentals: {type(exc).__name__}: {exc}")
        supplemental = {}

    return {
        "ticker": ticker,
        "data_cutoff": closes.index[-1].strftime("%Y-%m-%d"),
        "requested": {"start": fetch_start.isoformat(), "end_exclusive": fetch_end.isoformat()},
        "window": {
            "start": closes.index[0].strftime("%Y-%m-%d"),
            "end": closes.index[-1].strftime("%Y-%m-%d"),
            "trading_days": int(len(closes)),
            "calendar_days": int((closes.index[-1] - closes.index[0]).days),
        },
        "snapshot": {
            "status": "supplemental_unverified_yfinance",
            "values": _json_safe(supplemental),
            "instruction": "Verify current price, market cap, shares, and valuation using an active market-data capability or primary source before publishing.",
        },
        "summary": {
            "last_close": round(float(closes.iloc[-1]), 2),
            "annualized_vol_pct": round(standard_deviation * math.sqrt(252) * 100, 1),
            "daily_move_bar_pct": round(move_bar * 100, 1),
            "sigma_used": sigma,
            "floor_used_pct": round(floor * 100, 1),
        },
        "performance": {"target": target_performance, "comparisons": comparison_results},
        "max_drawdown": {
            "peak_date": peak_date.strftime("%Y-%m-%d"),
            "peak": round(float(closes.loc[peak_date]), 2),
            "trough_date": trough_date.strftime("%Y-%m-%d"),
            "trough": round(float(closes.loc[trough_date]), 2),
            "drawdown_pct": round(float(drawdowns.loc[trough_date]) * 100, 1),
        },
        "max_runup": {
            "trough_date": bottom_date.strftime("%Y-%m-%d"),
            "trough": round(float(closes.loc[bottom_date]), 2),
            "peak_date": top_date.strftime("%Y-%m-%d"),
            "peak": round(float(closes.loc[top_date]), 2),
            "runup_pct": round(float(runups.loc[top_date]) * 100, 1),
        },
        "significant_moves": flagged,
        "significant_move_count": len(flagged),
        "chart_series_weekly": _weekly_actual_dates(closes),
        "diagnostics": diagnostics,
        "_note": "Map each move to a dated source; say 'no identifiable catalyst' instead of inventing one.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze target, benchmark, and peer price action.")
    parser.add_argument("ticker")
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--benchmarks", default="IWM")
    parser.add_argument("--peers", default="")
    parser.add_argument("--sigma", type=float, default=2.5)
    parser.add_argument("--floor", type=float, default=0.05)
    parser.add_argument("--out", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        ticker = validate_ticker(args.ticker)
        validate_positive(args.years, "years")
        validate_positive(args.sigma, "sigma")
        validate_positive(args.floor, "floor")
        benchmarks = [validate_ticker(item) for item in args.benchmarks.split(",") if item.strip()]
        peers = [validate_ticker(item) for item in args.peers.split(",") if item.strip()]
        result = analyze(ticker, args.years, benchmarks, peers, args.sigma, args.floor)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    try:
        serialized = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        print(json.dumps({"error": f"result is not strict JSON: {exc}"}), file=sys.stderr)
        return 2
    if args.out:
        atomic_write_text(args.out, serialized + "\n")
        print(f"wrote {args.out}")
    else:
        print(serialized)
    if "error" in result:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
