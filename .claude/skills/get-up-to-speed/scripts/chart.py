#!/usr/bin/env python3
"""Generate a passive, marked SVG price chart from price_action.py output."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

COLORS = {"up": "#2f9e44", "down": "#d6453d", "flat": "#6b6760", "deal": "#30457a"}
ANCHORS = {"start", "middle", "end"}
MARKER = "get-up-to-speed-chart-v1"


def _date(value: Any, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _atomic_write(path: str | Path, text: str) -> None:
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


def _validate_annotations(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("annotations must be a JSON list")
    validated = []
    for index, item in enumerate(raw):
        field = f"annotations[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{field} must be an object")
        annotation_date = _date(item.get("date"), f"{field}.date")
        color = item.get("color", "flat")
        if color not in COLORS:
            raise ValueError(f"{field}.color must be one of {', '.join(COLORS)}")
        anchor = item.get("anchor", "start")
        if anchor not in ANCHORS:
            raise ValueError(f"{field}.anchor must be start, middle, or end")
        lines = item.get("lines")
        if not isinstance(lines, list) or not lines or not all(isinstance(line, str) for line in lines):
            raise ValueError(f"{field}.lines must be a non-empty list of strings")
        price = _number(item["price"], f"{field}.price") if "price" in item else None
        label_date = _date(item["lx_date"], f"{field}.lx_date") if item.get("lx_date") else None
        label_price = _number(item["ly_price"], f"{field}.ly_price") if item.get("ly_price") is not None else None
        validated.append({"date": annotation_date, "color": color, "anchor": anchor, "lines": lines,
                          "price": price, "lx_date": label_date, "ly_price": label_price})
    return validated


def render_chart(price_data: dict[str, Any], annotations: Any, *, title: str = "", subtitle: str = "",
                 ymin: float | None = None, ymax: float | None = None,
                 yticks: list[float] | None = None) -> str:
    raw_series = price_data.get("chart_series_weekly") if isinstance(price_data, dict) else None
    if not isinstance(raw_series, list) or not raw_series:
        raise ValueError("chart_series_weekly must be a non-empty list")
    points = []
    for index, row in enumerate(raw_series):
        if not isinstance(row, dict):
            raise ValueError(f"chart_series_weekly[{index}] must be an object")
        points.append((_date(row.get("date"), f"chart_series_weekly[{index}].date"),
                       _number(row.get("close"), f"chart_series_weekly[{index}].close")))
    points.sort(key=lambda pair: pair[0])
    if len({day for day, _ in points}) != len(points):
        raise ValueError("chart_series_weekly dates must be unique")
    validated_annotations = _validate_annotations(annotations)

    start_date, end_date = points[0][0], points[-1][0]
    date_span = max((end_date - start_date).days, 1)
    closes = [value for _, value in points]
    lower = _number(ymin, "ymin") if ymin is not None else min(closes) * 0.92
    upper = _number(ymax, "ymax") if ymax is not None else max(closes) * 1.05
    if upper < lower:
        raise ValueError("ymax must be greater than or equal to ymin")
    if upper == lower:
        padding = max(abs(upper) * 0.05, 1.0)
        lower -= padding
        upper += padding

    width, height, left, right, top, bottom = 980, 560, 70, 936, 64, 408

    def x_coordinate(value: dt.date) -> float:
        return left + (right - left) * ((value - start_date).days / date_span)

    def y_coordinate(value: float) -> float:
        return bottom - (bottom - top) * ((value - lower) / (upper - lower))

    def nearest(value: dt.date):
        return min(points, key=lambda point: abs((point[0] - value).days))

    polyline = " ".join(f"{x_coordinate(day):.1f},{y_coordinate(close):.1f}" for day, close in points)
    tick_values = ([_number(value, f"yticks[{index}]") for index, value in enumerate(yticks)]
                   if yticks else [lower + (upper - lower) * index / 5 for index in range(6)])
    grid = "".join(
        f'<line x1="{left}" y1="{y_coordinate(value):.1f}" x2="{right}" y2="{y_coordinate(value):.1f}" stroke="#ebe9e4" stroke-width="1"/>'
        f'<text x="{left - 10}" y="{y_coordinate(value) + 4:.1f}" text-anchor="end" font-size="12" fill="#8a8578">${value:.0f}</text>'
        for value in tick_values)
    x_ticks = ""
    for year in range(start_date.year, end_date.year + 1):
        for month, label in ((1, "Jan"), (7, "Jul")):
            day = dt.date(year, month, 1)
            if start_date <= day <= end_date:
                xpos = x_coordinate(day)
                x_ticks += (f'<line x1="{xpos:.1f}" y1="{top}" x2="{xpos:.1f}" y2="{bottom}" stroke="#f3f1ec" stroke-width="1"/>'
                            f'<text x="{xpos:.1f}" y="{bottom + 22}" text-anchor="middle" font-size="12" fill="#8a8578">{label} ’{str(year)[2:]}</text>')

    dots, labels, leaders = [], [], []
    for annotation in validated_annotations:
        _, nearest_close = nearest(annotation["date"])
        plotted_price = annotation["price"] if annotation["price"] is not None else nearest_close
        plotted_date = min(max(annotation["date"], start_date), end_date)
        point_x, point_y = x_coordinate(plotted_date), y_coordinate(plotted_price)
        color = COLORS[annotation["color"]]
        label_date = min(max(annotation["lx_date"], start_date), end_date) if annotation["lx_date"] else None
        label_x = x_coordinate(label_date) if label_date else point_x
        label_y = y_coordinate(annotation["ly_price"]) if annotation["ly_price"] is not None else point_y - 20
        dots.append(f'<circle cx="{point_x:.1f}" cy="{point_y:.1f}" r="4.5" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        leaders.append(f'<line x1="{point_x:.1f}" y1="{point_y:.1f}" x2="{label_x - 3:.1f}" y2="{label_y - 11:.1f}" stroke="{color}" stroke-width="1" stroke-dasharray="2,2" opacity="0.55"/>')
        for line_index, line in enumerate(annotation["lines"]):
            labels.append(f'<text x="{label_x:.1f}" y="{label_y + line_index * 14:.1f}" text-anchor="{annotation["anchor"]}" font-size="11.5" fill="{color}" font-weight="600">{html.escape(line, quote=True)}</text>')

    return f'''<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">
<metadata id="{MARKER}" data-generator="{MARKER}">{MARKER}</metadata>
<rect x="0" y="0" width="{width}" height="{height}" rx="14" fill="#fdfdfb"/>
<text x="{left - 58}" y="28" font-size="16" font-weight="700" fill="#26241f">{html.escape(str(title), quote=True)}</text>
<text x="{left - 58}" y="47" font-size="12" fill="#8a8578">{html.escape(str(subtitle), quote=True)}</text>
{grid}{x_ticks}
<polyline points="{polyline}" fill="none" stroke="#2563eb" stroke-width="2" stroke-linejoin="round"/>
{"".join(leaders)}{"".join(dots)}{"".join(labels)}
</svg>'''


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a marked, passive SVG price chart.")
    parser.add_argument("price")
    parser.add_argument("out")
    parser.add_argument("--annotations", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--ymin", type=float)
    parser.add_argument("--ymax", type=float)
    parser.add_argument("--yticks", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        price_data = json.loads(Path(args.price).read_text(encoding="utf-8-sig"))
        annotations = json.loads(Path(args.annotations).read_text(encoding="utf-8-sig")) if args.annotations else []
        ticks = [float(value) for value in args.yticks.split(",") if value.strip()] if args.yticks else None
        svg = render_chart(price_data, annotations, title=args.title, subtitle=args.subtitle,
                           ymin=args.ymin, ymax=args.ymax, yticks=ticks)
        _atomic_write(args.out, svg + "\n")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
