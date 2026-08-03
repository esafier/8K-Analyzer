#!/usr/bin/env python3
"""Render a validated briefing JSON as self-contained HTML."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MARKER = "get-up-to-speed-chart-v1"
ALLOWED_SVG_TAGS = {"svg", "metadata", "rect", "text", "line", "polyline", "circle"}
LIST_FIELDS = {"snapshot", "timeline", "bull", "bear", "drivers", "drift", "catalysts", "sources"}

CSS = """
:root{--paper:#fbfbf8;--card:#fff;--ink:#16181d;--muted:#6b6e76;--line:#e7e6e0;--accent:#30457a;--gain:#157f4b;--loss:#be3a33}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:980px;margin:auto;padding:34px 30px 44px}.head{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;flex-wrap:wrap}.ticker{font:700 40px/1 ui-monospace,monospace}.company{font-size:18px;font-weight:650;margin-top:6px}.muted{color:var(--muted)}.price{text-align:right;font:700 28px ui-monospace,monospace}
.crux{margin:24px 0;padding:14px 18px;background:#f3f5f9;border:1px solid #d9deea;border-left:4px solid var(--accent);border-radius:7px}.label,.eyebrow{font-size:11px;font-weight:750;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}hr{border:0;border-top:1px solid var(--line);margin:27px 0}.eyebrow{color:var(--muted);margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));border:1px solid var(--line);border-radius:7px;overflow:hidden}.cell{background:var(--card);padding:12px 14px;border-right:1px solid var(--line)}.cell b{display:block;font:650 17px ui-monospace,monospace}.chart{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}.chart img{display:block;width:100%;height:auto}.rows>div{display:grid;grid-template-columns:105px 1fr auto;gap:14px;padding:9px 0;border-top:1px solid var(--line)}.move{font:700 12px ui-monospace,monospace;padding:2px 8px;border-radius:4px}.move.up{color:var(--gain);background:#edf8f2}.move.down{color:var(--loss);background:#fbefee}.move.flat{color:var(--muted);background:#f1f0ea}
.bb{display:grid;grid-template-columns:1fr 1fr;gap:16px}.case{border:1px solid var(--line);border-radius:8px;padding:15px}.case.bull{background:#f4faf7}.case.bear{background:#fcf5f4}.case h3{margin:0 0 8px;font-size:13px}.case ul{margin:0;padding-left:20px}.drivers>div,.cats>div,.sources>div{padding:9px 0;border-top:1px solid var(--line)}.drift{border:1px solid var(--line);border-radius:8px;overflow:hidden}.drift>div{display:grid;grid-template-columns:1fr 30px 1fr;padding:10px 14px;border-top:1px solid var(--line)}.drift .arrow{text-align:center;color:var(--accent)}.primary{background:#f3f5f9}.source-note{color:var(--muted)}.foot{margin-top:28px;padding-top:14px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}a{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:2px}
@media(max-width:720px){.wrap{padding:24px 18px}.ticker{font-size:32px}.bb{grid-template-columns:1fr}.drift>div{grid-template-columns:1fr}.drift .arrow{display:none}.rows>div{grid-template-columns:88px 1fr}.rows>div>:last-child{grid-column:2}}
@media print{body{background:#fff}.wrap{max-width:none;padding:0}.chart,.case,.crux{break-inside:avoid}a{color:inherit;text-decoration:none}}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _require_string(mapping: dict[str, Any], key: str, path: str = "") -> str:
    value = mapping.get(key)
    field = f"{path}.{key}" if path else key
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def validate_content(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise ValueError("content must be a JSON object")
    _require_string(content, "ticker")
    _require_string(content, "company")
    for key in ("as_of", "descriptor", "price", "change_note", "crux", "what_it_is", "drift_intro", "footer"):
        if key in content and not isinstance(content[key], str):
            raise ValueError(f"{key} must be a string")
    for key in LIST_FIELDS:
        if key in content and not isinstance(content[key], list):
            raise ValueError(f"{key} must be a list")
    for key in ("bull", "bear"):
        if not all(isinstance(item, str) for item in content.get(key, [])):
            raise ValueError(f"{key} entries must be strings")
    object_contracts = {
        "snapshot": ("label", "value"), "timeline": ("date", "event"),
        "drivers": ("name", "note"), "drift": ("then", "now"),
        "catalysts": ("when", "text"), "sources": ("label", "note"),
    }
    for list_name, keys in object_contracts.items():
        for index, item in enumerate(content.get(list_name, [])):
            if not isinstance(item, dict):
                raise ValueError(f"{list_name}[{index}] must be an object")
            for key in keys:
                _require_string(item, key, f"{list_name}[{index}]")
    for index, row in enumerate(content.get("timeline", [])):
        if "move" in row and not isinstance(row["move"], str):
            raise ValueError(f"timeline[{index}].move must be a string")
        if "dir" in row and row["dir"] not in {"up", "down", "flat"}:
            raise ValueError(f"timeline[{index}].dir must be up, down, or flat")
    for index, row in enumerate(content.get("catalysts", [])):
        if "primary" in row and type(row["primary"]) is not bool:
            raise ValueError(f"catalysts[{index}].primary must be a boolean")
    for index, source in enumerate(content.get("sources", [])):
        if "url" in source:
            url = source["url"]
            if not isinstance(url, str) or urlparse(url).scheme.lower() != "https" or not urlparse(url).netloc:
                raise ValueError(f"sources[{index}].url must be an absolute https URL")
    return content


def validate_chart_svg(svg: str) -> str:
    if not isinstance(svg, str) or not svg.strip():
        raise ValueError("chart SVG must be non-empty")
    if "<!doctype" in svg.casefold() or "<!entity" in svg.casefold():
        raise ValueError("chart SVG declarations are not allowed")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError(f"chart SVG is not well formed: {exc}") from exc
    if root.tag.split("}")[-1].casefold() != "svg":
        raise ValueError("chart must have an SVG root")
    marker_found = False
    for element in root.iter():
        tag = element.tag.split("}")[-1].casefold()
        if tag not in ALLOWED_SVG_TAGS:
            raise ValueError(f"chart SVG contains forbidden element: {tag}")
        if tag == "metadata" and element.attrib.get("id") == MARKER and (element.text or "").strip() == MARKER:
            marker_found = True
        for raw_name, value in element.attrib.items():
            name = raw_name.split("}")[-1].casefold()
            if name.startswith("on"):
                raise ValueError(f"chart SVG contains event handler: {name}")
            if name in {"href", "src"} or "url(" in value.casefold() or "javascript:" in value.casefold():
                raise ValueError(f"chart SVG contains external or unsafe reference: {name}")
    if not marker_found:
        raise ValueError("chart SVG was not produced by the bundled chart generator")
    return svg


def _section(title: str, body: str) -> str:
    return f'<section><div class="eyebrow">{esc(title)}</div>{body}</section>'


def _render_sources(sources: list[dict[str, Any]]) -> str:
    rows = []
    for source in sources:
        label = esc(source["label"])
        if source.get("url"):
            label = f'<a href="{esc(source["url"])}" rel="noreferrer">{label}</a>'
        rows.append(f'<div><b>{label}</b><div class="source-note">{esc(source["note"])}</div></div>')
    return "".join(rows)


def build_html(content: Any, chart_svg: str = "") -> str:
    c = validate_content(content)
    chart_block = ""
    if chart_svg:
        passive_svg = validate_chart_svg(chart_svg)
        encoded = base64.b64encode(passive_svg.encode("utf-8")).decode("ascii")
        chart_block = '<hr>' + _section("The tape", f'<div class="chart"><img alt="Annotated price chart" src="data:image/svg+xml;base64,{encoded}"></div>')
    snapshot = "".join(f'<div class="cell"><span class="muted">{esc(row["label"])}</span><b>{esc(row["value"])}</b></div>' for row in c.get("snapshot", []))
    timeline = "".join(f'<div><span class="muted">{esc(row["date"])}</span><span>{esc(row["event"])}</span><b class="move {row.get("dir", "flat")}">{esc(row.get("move", "—"))}</b></div>' for row in c.get("timeline", []))
    bull = "".join(f'<li>{esc(item)}</li>' for item in c.get("bull", []))
    bear = "".join(f'<li>{esc(item)}</li>' for item in c.get("bear", []))
    drivers = "".join(f'<div><b>{esc(row["name"])}</b> — {esc(row["note"])}</div>' for row in c.get("drivers", []))
    drift = "".join(f'<div><span>{esc(row["then"])}</span><span class="arrow">→</span><b>{esc(row["now"])}</b></div>' for row in c.get("drift", []))
    catalysts = "".join(f'<div class="{"primary" if row.get("primary") else ""}"><b>{esc(row["when"])}</b> — {esc(row["text"])}</div>' for row in c.get("catalysts", []))
    sources = _render_sources(c.get("sources", []))
    optional_sections = []
    if snapshot: optional_sections.append('<hr>' + _section("Snapshot", f'<div class="grid">{snapshot}</div>'))
    if timeline: optional_sections.append('<hr>' + _section("How we got here", f'<div class="rows">{timeline}</div>'))
    if bull or bear: optional_sections.append('<hr>' + _section("Bull vs. bear", f'<div class="bb"><div class="case bull"><h3>Bull case</h3><ul>{bull}</ul></div><div class="case bear"><h3>Bear case</h3><ul>{bear}</ul></div></div>'))
    if drivers: optional_sections.append('<hr>' + _section("The drivers", f'<div class="drivers">{drivers}</div>'))
    if drift: optional_sections.append('<hr>' + _section("What they are asking now — the drift", f'<p class="muted">{esc(c.get("drift_intro", ""))}</p><div class="drift">{drift}</div>'))
    if catalysts: optional_sections.append('<hr>' + _section("Next catalysts", f'<div class="cats">{catalysts}</div>'))
    if sources: optional_sections.append('<hr>' + _section("Sources", f'<div class="sources">{sources}</div>'))
    descriptor = f'<div class="muted">{esc(c.get("descriptor", ""))}</div>' if c.get("descriptor") else ""
    crux = f'<div class="crux"><div class="label">The crux</div>{esc(c.get("crux", ""))}</div>' if c.get("crux") else ""
    what = '<hr>' + _section("What it is", f'<p>{esc(c["what_it_is"])}</p>') if c.get("what_it_is") else ""
    generated = dt.date.today().isoformat()
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(c["ticker"])} — Up to Speed</title><style>{CSS}</style></head><body><main class="wrap">
<header class="head"><div><div class="ticker">{esc(c["ticker"])}</div><div class="company">{esc(c["company"])}</div><div class="muted">Up to Speed · {esc(c.get("as_of", ""))}</div>{descriptor}</div><div class="price">{esc(c.get("price", ""))}<div class="muted">{esc(c.get("change_note", ""))}</div></div></header>
{crux}{what}{chart_block}{''.join(optional_sections)}
<footer class="foot">{esc(c.get("footer", ""))}<br>Generated {generated} · get-up-to-speed skill. Not investment advice; verify before acting.</footer>
</main></body></html>'''


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a self-contained get-up-to-speed HTML tearsheet.")
    parser.add_argument("content")
    parser.add_argument("out")
    parser.add_argument("--chart", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        content = json.loads(Path(args.content).read_text(encoding="utf-8-sig"))
        chart = Path(args.chart).read_text(encoding="utf-8-sig") if args.chart else ""
        _atomic_write(args.out, build_html(content, chart) + "\n")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
