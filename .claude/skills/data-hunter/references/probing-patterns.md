# Probing patterns by domain

Tactical reference for Rungs 2–4 of the escalation ladder. Read the relevant section, not the whole file.

## Contents
1. Generic REST probing
2. Financial markets (exchanges, brokers, crypto)
3. SEC / company filings
4. Government & official statistics
5. Web archives & page-history reconstruction
6. GraphQL endpoints
7. Browser network reconnaissance
8. Cross-check recipes

---

## 1. Generic REST probing

Path conventions to try, in rough order of hit rate:
```
/api/v1/ /api/v2/ /api/v3/          # version roots — a JSON error here means the API is live
/{root}/markets/  /products/  /instruments/  /assets/  /symbols/   # enumeration endpoints
/{root}/ticker/{id}/  /stats/{id}/  /summary/                      # per-item current state
/{root}/ohlc/{id}/  /candles/  /klines/  /history/  /chart/        # time series
sitemap.xml  robots.txt  .well-known/                              # discovery
```
Parameter conventions for history endpoints: `step`/`interval`/`resolution`/`granularity` (seconds or `1d`), `limit` (push it — ask for 1000), `start`/`end`/`from`/`to` (epoch seconds or ms). If a limit caps results, paginate by shifting `start` to the last returned timestamp.

Useful curl flags: `-s` (quiet), `-L` (follow redirects), `--max-time 30`, `-H "Accept: application/json"`, `-A "Mozilla/5.0"` (some APIs gate on user-agent; a browser UA on a *public unauthenticated* endpoint is fine — do not use it to evade an actual block).

Signals to read:
- `{"message": "Not found."}` in JSON → API root live; wrong path, right idea.
- HTML/Incapsula/Cloudflare on a page but JSON on `/api/` → bot-wall protects pages only.
- Extra JSON fields beyond what the UI shows → new capabilities (hidden metrics, IDs, types). Always print the full first object.
- An enum-like field (`market_type`, `status`, `category`) → filter on it to find the hidden universe.

## 2. Financial markets

**Crypto exchanges** — nearly all expose free unauthenticated market data:
- Symbol conventions for derivatives: `{base}{quote}-perp`, `{BASE}-{QUOTE}-SWAP` (OKX), `{BASE}{QUOTE}` on a separate futures host (`fapi.binance.com`), `PERP_{BASE}_{QUOTE}`.
- Candles: `/ohlc/`, `/klines/`, `/candles/`, `/market/candles`. Depth of history varies; test `limit=1000` daily first.
- Open interest: usually only *current* (in ticker or `/openInterest`). History exists only on Binance futures (`/futures/data/openInterestHist`, capped ~30 days), Coinglass/Laevitas (paid), or by your own sampling. Flag this immediately.
- Funding rates: `/fundingRate`, `/funding_history` — often deep history, useful as an activity proxy.

**Aggregators with free APIs:**
- CoinGecko: `api.coingecko.com/api/v3/` — `/derivatives/exchanges` (per-venue OI + 24h volume, current only), `/exchanges/{id}/volume_chart?days=N` (historical venue volume, BTC-denominated).
- DeFiLlama: `api.llama.fi` — TVL, volumes, fees for on-chain venues; deep free history.
- FRED (`api.stlouisfed.org`, free key) for macro series; Treasury FiscalData (`api.fiscaldata.treasury.gov`) keyless.

**Traditional markets:** exchange websites publish monthly volume stats as PDFs/XLS on IR or "market statistics" pages — predictable URLs, enumerable by pattern (`.../statistics/2026/06/...`). OCC (theocc.com) has daily options/futures volume by exchange. CFTC publishes weekly Commitments of Traders (API on publicreporting.cftc.gov — Socrata, see §4).

## 3. SEC / company filings

- **EDGAR full-text search API**: `efts.sec.gov/LATEST/search-index?q=...` or UI-equivalent `efts.sec.gov/LATEST/search-index?q=%22exact+phrase%22&dateRange=custom` — finds any phrase across all filings. JSON: `https://efts.sec.gov/LATEST/search-index?q="perpetual futures"&forms=10-Q`.
- **Company facts API**: `data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json` — every XBRL-tagged number the company ever filed, as a time series. This is the fastest route to "historical values of metric X" for any US filer.
- `data.sec.gov/submissions/CIK{...}.json` — full filing index per company.
- Requires a descriptive `User-Agent` header (SEC policy): `-H "User-Agent: research name@email"`.
- IR pages: monthly-metrics PDFs often live at guessable static-file URLs; enumerate via the IR page's link list or sitemap.

## 4. Government & official statistics

- **Socrata portals** (data.gov, most US states/cities, CFTC): standard API at `{domain}/resource/{dataset-id}.json` with SoQL params (`$where`, `$limit=50000`, `$offset`). Discover datasets via `{domain}/api/catalog/v1?q=...`.
- **EU**: data.europa.eu (CKAN API), ECB Data Portal (`data-api.ecb.europa.eu` — SDMX), Eurostat API (keyless JSON).
- **CKAN portals** (many national): `/api/3/action/package_search?q=...`.
- Regulator registers (licenses, authorizations) are usually searchable HTML backed by a JSON search endpoint — check the network tab (§7).

## 5. Web archives & page-history reconstruction

Reconstruct time series from pages that only ever showed a "current" number:
- **CDX API**: `web.archive.org/cdx/search/cdx?url={URL}&output=json&from=2024&to=2026&filter=statuscode:200&collapse=timestamp:6` — lists all snapshots (collapse `:6` = monthly, `:8` = daily).
- Fetch each snapshot at `web.archive.org/web/{timestamp}/{URL}`, extract the figure, build the series.
- Works for: stated user counts, "assets on platform," fee schedules, product lists (detect launch dates by first appearance), leaderboards.
- Also archives *API responses* sometimes — try the API URL in CDX.

## 6. GraphQL endpoints

Modern frontends often speak GraphQL at `/graphql` or `/api/graphql`.
- Introspection probe: `curl -s -X POST {url} -H "Content-Type: application/json" -d '{"query":"{__schema{queryType{fields{name}}}}"}'` — if enabled, this lists every queryable field (the whole hidden API surface).
- If introspection is disabled, read the site's JS bundle for `query {...}` strings and replay them.

## 7. Browser network reconnaissance

When endpoints aren't guessable:
1. Open the target page with browser tools; let the chart/table render.
2. Read the network requests (XHR/fetch); find the call that returns the data (filter by JSON content-type, look for the numbers you can see on screen).
3. Copy the URL + params; replay with curl. Then push the params: longer date ranges, larger limits, other instrument IDs from the enumeration endpoint.
4. If requests carry a session token in headers, the endpoint is account-gated → Rung 6 handoff (never extract or reuse credentials/tokens; describe to the user exactly which response to export instead).

## 8. Cross-check recipes

Before presenting scraped data, reconcile one anchor point:
- Sum your daily series to a month and compare to an officially reported monthly figure. State the ratio and whether the wedge is explainable (scope: subset of products; basis: single- vs double-counted; unit: base vs quote currency).
- If nothing official exists, sanity-bound it: compare against a comparable venue, or check internal consistency (volume ≥ 0, OI ≤ cumulative volume, prices within the day's high/low).
- Record in the output: source endpoint, pull timestamp (UTC), derivation formula, and the cross-check result.
