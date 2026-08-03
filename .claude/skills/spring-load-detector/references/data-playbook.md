# Data Playbook — where every input lives and how to fetch it

All endpoints below were verified working in this environment (July 2026). EDGAR requests need a
`User-Agent` header with a name and email or the SEC blocks you.

## 1. Resolve the company → CIK

- If you have a ticker: `https://www.sec.gov/files/company_tickers.json` (WebFetch or curl) maps
  ticker → CIK, or take the `cik` field from any api-ninjas result.
- CIK must be zero-padded to 10 digits for the submissions API.

## 2. The master filing index (event calendar + Form 4 discovery) — one call

```
curl -s -H "User-Agent: <name> <email>" https://data.sec.gov/submissions/CIK##########.json
```

`filings.recent` is a column-oriented dict with parallel arrays: `form`, `filingDate`,
`accessionNumber`, `items`, `primaryDocument`, `acceptanceDateTime`. ~1000 most recent filings;
older ones are in the `filings.files` continuation files listed in the same response.

This single call gives you BOTH:
- **The MNPI event calendar** — every 8-K with its item numbers. The ones that matter:
  - `2.02` = results announcement (earnings press release) — this is the true earnings date,
    usually days before the 10-Q/10-K
  - `1.01` = material definitive agreement (M&A, big contracts)
  - `5.02` = officer/director changes AND comp arrangements (grants often disclosed here)
  - `7.01`/`8.01` = Reg FD / other events (guidance, product news)
  - `2.01` = completed acquisition/disposition
  Also note 10-K/10-Q filing dates, and DEFM14A/S-4 (deal proxies).
- **Form 4 index** — every `form == "4"` row with accession number and filing date.

## 3. Form 4 detail (the authoritative grant record)

Build the folder URL from the accession number (strip dashes):
```
https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/index.json   → lists files
https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/form4.xml    → usually this name; confirm via index.json
```

The XML contains what the api-ninjas summary does NOT:
- `<transactionDate>` — the actual grant date (filing date can lag up to 2 business days, and
  late filings lag more — a late Form 4 for a grant is itself a minor flag)
- `<transactionCode>` — `A` = grant/award. Also watch `D` (disposition to issuer), `M` (exercise),
  `F` (tax withholding), `G` (gift), `J` (other — read the footnote, J hides bodies)
- `<transactionShares>`, `<transactionPricePerShare>` — for options, price-per-share on code-A rows
  is the strike
- `<derivativeTable>` vs `<nonDerivativeTable>` — options/SARs vs stock/RSUs
- `<exerciseDate>`, `<expirationDate>`, `<underlyingSecurity>`
- `<footnote>` blocks — vesting schedules, approval details, plan names, "granted pursuant to..."
  language. ALWAYS read the footnotes; this is where vesting mechanics and odd terms live.
- `<periodOfReport>` — earliest transaction date in the filing.

## 4. Bulk scan of insider activity (discovery layer)

`mcp__api-ninjas__get_insider_transactions` (ticker, start_date, end_date, insider_name, limit).
Returns insider name/position, transaction code/name, shares, value, and the `sec_filing_url` +
`accession_number` for drill-down.

**Limitations found in testing — do not trust it beyond discovery:**
- Returns `filing_date` only, NOT the transaction/grant date → never use its dates for timing
  analysis; always drill into the Form 4 XML.
- The `transaction_code` / `transaction_type` filters did not filter reliably in testing — fetch
  broadly and filter code `A` rows yourself.

Use it to quickly enumerate which insiders filed Form 4s in a window, then fetch the XML for each
relevant accession from EDGAR.

## 5. Price history (the price path around the grant)

`mcp__api-ninjas__get_stock_price_historical` — period `"1d"`, **start/end must be Unix
timestamps** (YYYY-MM-DD errors out despite what the docs say). Returns daily OHLCV.
Fetch grant date −90 calendar days to +90 (or to today if sooner). If the window is recent and
+90d hasn't elapsed, say so in the output — the post-grant return is still developing.
Alternative: the `massive` MCP server (search_endpoints → aggregates) if api-ninjas fails.

## 6. Earnings dates

- Past announcement dates: 8-K Item 2.02 filings from the submissions JSON (step 2) — use
  `acceptanceDateTime` to know if it hit before market open or after close.
- Next scheduled: `mcp__api-ninjas__get_earnings_calendar` or Quartr `list_events`.
- Reported financials + periodic filing dates: `mcp__api-ninjas__get_earnings`.

## 7. Proxy statements (grant policy + history + Item 402(x) table)

From the submissions JSON, find `DEF 14A` rows, fetch the primary document. Search inside it for:
- "Grants of Plan-Based Awards" table (grant date AND committee approval date if different)
- Item 402(x) disclosure: the company's option-grant timing policy and the table of awards granted
  close in time to MNPI releases (check `references/taxonomy.md` for how to read it)
- CD&A language about grant timing practices ("annual grants are made at the February meeting...")
  — this establishes the company's OWN stated cadence, which you then hold them to.

## 8. EDGAR full-text search and other verified endpoints

| Purpose | Endpoint |
|---|---|
| Full-text search API (JSON) | `https://efts.sec.gov/LATEST/search-index?q=%22spring-loaded%22&forms=8-K&startdt=2024-01-01&enddt=2024-12-31` (params: `q`, `forms`, `startdt`, `enddt`; UI at sec.gov/edgar/search/; covers 2001+) |
| Browse a company's Form 4s | `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=##########&type=4&owner=include&count=40` |
| Browse by individual insider (reporting-owner CIK) | same URL with `action=getowner&CIK=<owner CIK>` — use to check whether an executive has a timing pattern across MULTIPLE companies |
| Bulk insider datasets (quarterly, structured) | `https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets` |

Rate limit ≤10 req/sec on SEC endpoints.

## 9. News/catalyst check beyond filings

WebSearch: `"<company>" news <grant month year>` and `"<company>" announcement <date range>` —
looking for FDA decisions, contract awards, analyst days, conference presentations, guidance
raises that are NOT 8-K'd (or 8-K'd late). Cross-check the grant date against anything found.

## Assembly order (fastest path)

1. Submissions JSON → event calendar + Form 4 list (one call)
2. Form 4 XMLs for the grant(s) under review + all code-A grants for the past 3–5 years for the
   same executives (this is the cadence baseline — batch the fetches)
3. Price history around the grant(s)
4. Latest DEF 14A → stated grant policy + 402(x) table
5. Targeted WebSearch for non-filing catalysts near the grant date
