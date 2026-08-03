# Artifact Contract

Save requested artifacts under `outputs/get-up-to-speed/<safe-ticker>/` in the active task. Resolve every bundled script relative to the installed skill directory. Both generators create parent directories and write UTF-8 files atomically.

## Chart input

`price.json` must contain a non-empty `chart_series_weekly` list of objects with `date` (`YYYY-MM-DD`) and finite numeric `close`. `annotations.json` is a list of:

```json
{
  "date": "2026-07-20",
  "price": 123.4,
  "color": "up",
  "lines": ["Q2 guide raised"],
  "lx_date": "2026-06-20",
  "ly_price": 140,
  "anchor": "start"
}
```

`price`, `lx_date`, and `ly_price` are optional. `color` is `up`, `down`, `flat`, or `deal`; `anchor` is `start`, `middle`, or `end`. The generator safely escapes visible text and marks its SVG for downstream validation. Annotation dates use their exact calendar position on the x-axis and are clamped to the visible chart domain; when `price` is omitted, only the y-value comes from the nearest weekly point.

## Tearsheet input

`ticker` and `company` are required non-empty strings. Every provided optional scalar below must also be a string: `as_of`, `descriptor`, `price`, `change_note`, `crux`, `what_it_is`, `drift_intro`, and `footer`.

Structured lists:

- `snapshot`: `{label, value}`
- `timeline`: `{date, event, move?, dir?}` where `move` is a string and `dir` is `up`, `down`, or `flat`
- `bull`, `bear`: strings
- `drivers`: `{name, note}`
- `drift`: `{then, now}`
- `catalysts`: `{when, text, primary?}` where `primary`, when present, is a Boolean
- `sources`: `{label, url?, note}` where `url`, when present, must be an absolute `https` URL

The tearsheet embeds only a well-formed SVG bearing the bundled generator marker. It rejects scripts, `foreignObject`, event handlers, external references, unsafe URLs, and unsupported SVG elements. The validated SVG is embedded as a base64 data image, not raw markup. Omit `--chart` when there is no chart; the chart section is then omitted.
