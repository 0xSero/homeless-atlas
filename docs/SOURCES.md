# Sources

One row per data source used by an ingestor. `license` is what the publisher states;
`rate` is the politeness limit the ingestor enforces. Append only — never rewrite.

| domain | source | url | publisher | license | rate | notes |
|---|---|---|---|---|---|---|
| nonprofit | IRS Exempt Organizations Business Master File (EO BMF) splits | https://www.irs.gov/pub/irs-soi/eo1.csv (also eo2, eo3, eo4) | Internal Revenue Service | public domain (US federal government) | 4 files, 1 req/s | Universe of tax-exempt orgs: name, address, city/state/zip, NTEE code, subsection, ruling date, BMF status. ~2.0M rows, filtered to homelessness/shelter NTEE codes. |
| nonprofit | ProPublica Nonprofit Explorer API v2 — search | https://projects.propublica.org/nonprofits/api/v2/search.json | ProPublica | public data (IRS BMF + Form 990 extracts); API free, no key | 1 req/s | Name-keyword sweeps (homeless, shelter, housing first, ...). 25 orgs/page, max 400 pages per query; `state[id]` and `ntee[id]` filters are inert/500 on live v2. |
| nonprofit | ProPublica Nonprofit Explorer — filing page | https://projects.propublica.org/nonprofits/organizations/{ein} | ProPublica | public data (IRS Form 990) | 1 req/s | Per fiscal-year extracted financials (revenue, expenses, assets, net assets) and the Form 990 Part VII Section A officer/director table (name, title, reportable comp). The `download-xml?object_id=` endpoint returns 403 "Security Check" to non-browser clients, so the rendered filing page is used instead. |
| nonprofit | Wikidata Query Service (SPARQL) — EIN -> official website / phone | https://query.wikidata.org/sparql | Wikidata / Wikimedia Foundation | CC0 | 1 req/s, batched 120 EINs | Exact-key join P1297 (IRS EIN) -> P856 (official website), P1329 (phone). Gap-fill only; never overwrites a value from another source. |
