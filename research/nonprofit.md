# Nonprofit domain — the organisations and the people who run them

Scope: every US tax-exempt organisation that works on homelessness — shelters, transitional and
supportive housing providers, Continuums of Care, outreach and rescue missions — with its
financials, location and the officers/directors/key employees who run it (IRS Form 990, Part VII
Section A).

Ingestor: `ingest/domains/nonprofit.py` (`DOMAIN="nonprofit"`, `run(conn, cfg)`).
Raw responses and the fetch journal: `data/raw/nonprofit/`.

## Where the data comes from

| Layer | Source | What it gives |
|---|---|---|
| Universe | IRS Exempt Organizations Business Master File, `eo1..eo4.csv` (4 files, 1,984,891 rows) | name, EIN, street/city/state/zip, NTEE code, subsection, ruling date, BMF status |
| Universe + names | ProPublica Nonprofit Explorer search API v2, 23 name queries | orgs the BMF NTEE filter would miss (search is name-keyword based) |
| Financials + people | ProPublica Nonprofit Explorer filing page `/nonprofits/organizations/{ein}` | per fiscal year: total revenue, expenses, total/net assets; Form 990 Part VII table: person, title, reportable compensation |
| Website / phone | Wikidata SPARQL, `P1297` (EIN) → `P856`, `P1329` | published official website / phone for a minority of orgs |

### Universe definition (documented, not hand-picked)

An organisation is in scope if its published IRS NTEE code is `L41*` (homeless/temporary shelter),
`L40*` (temporary housing) or `P85*` (homeless persons centres & services) — 8,383 candidates — **or** its registered name
contains an unambiguous homelessness term (`HOMELESS`, `UNHOUSED`, `REHOUSING`, `HOUSING FIRST`,
`SUPPORTIVE HOUSING`, `TRANSITIONAL HOUSING/LIVING`, `CONTINUUM OF CARE`, `STREET OUTREACH`,
`RESCUE MISSION`, `WARMING CENTER`, `BRIDGE/EMERGENCY HOUSING`, `HMIS`). Names matching only
`SHELTER` or `SAFE HAVEN` are accepted only for NTEE groups L/P/X/O/S/K/F (i.e. not for animal,
education, health or medical groups). Animal-shelter NTEE group `D*` and animal-shelter names
(`ANIMAL`, `HUMANE`, `PAWS`, `VETERINAR`, …) are always rejected.

Known imprecision, deliberately kept rather than guessed at: NTEE `L40` ("temporary housing") also
covers non-homelessness temporary housing (a few university dormitory corporations, camps). They are
left in; naming them is more honest than silently dropping them, and the NTEE code is published data.

### Things that did not work (verified live, 2026-11-19)

* `state[id]` on the v2 search API is inert — `q=homeless&state[id]=0`, `=6`, `=51`, `=52` all
  returned the identical 1,709 nationwide records. The 60-state sweep described in the brief is
  therefore impossible; the nationwide sweep already contains every state, and `state` is taken from
  each organisation record (53 states/territories present in the output).
* `ntee[id]=…` returns HTTP 500 for every value tried (`L41`, `P85`, `13`, `185`); `c_code[id]` works
  but is capped at 10,000 results and cannot be combined usefully. So NTEE filtering is done locally
  on the BMF file instead.
* `https://projects.propublica.org/nonprofits/download-xml?object_id=…` (the Form 990 XML) answers
  HTTP 403 "Security Check" to non-browser clients, with or without a Referer/session. The filing PDFs
  (`/nonprofits/display_990/…`) are behind Cloudflare ("Just a moment…", HTTP 403) as well. The
  rendered filing page carries the same Part I / Part VII values and is what this ingestor parses.
* Search results carry no `filings_with_data` array (the brief's assumption); results are
  25 orgs/page with ein, name, city, state, ntee_code, subseccd only. Financial detail needs the
  per-organisation call, which is 1 request per org — hence the journal design below.

### Engineering notes (why the data survives)

* 1 request/second via `common.fetch`; every response cached under `data/raw/nonprofit/`; every row
  carries `source_id` of the URL it came from (org financials cite the filing page, persons cite the
  filing page, addresses cite the BMF file).
* Fetching and writing are decoupled: each filing page is parsed into `data/raw/nonprofit/pages.jsonl`
  and only then flushed into `org`/`org_person` (`pages_flushed.txt` records what has landed). Other
  workers hold long write transactions, and the database was rolled back once during this run — the
  journal is what made a full replay possible without re-fetching a single page.
* `org` merges never overwrite a non-null value with NULL, never rewrite another domain's
  `name`/`name_norm`/`state`/`ein`, and only move financials forward in time (`fiscal_year` must be
  >= the stored year). When a row's financials are (re)written, its `source_id` is re-pointed at the
  filing page they came from — otherwise a "fix the citation" pass in another worker rewrites the
  number from an older extract and the site would cite the BMF registration file for a Form 990
  figure. Persons are deduplicated on `(person_norm, org_id, year, title)` with a NULL-safe match and
  an `org_name` fallback (an org can be renamed between runs, which would otherwise duplicate its
  whole roster); rows carrying the same person twice are collapsed on every run.
* `role_class` is derived from the published title: `officer` (CEO/CFO/COO/CIO, president, vice
  president, secretary, treasurer, executive director, officer), `director` (director, trustee,
  board, chair), `key_employee` (everything else with a title), `None` when the filing records no
  title. Titles are stored exactly as filed, including notations like `(thru 04/24)`.

## Verified

Five organisations re-checked by hand on 2026-11-19 against the live source (a fresh, cache-bypassed
request for each, compared with the stored row). "Page" = `https://projects.propublica.org/nonprofits/organizations/{ein}`
(FY column = the fiscal year stored in `org.fiscal_year`); "API" = `.../api/v2/organizations/{ein}.json`
(independent endpoint, BMF/IRS extract values `revenue_amount` / `asset_amount`).
Transcript: `logs/nonprofit-verify.log`.

| EIN | Organisation | Stored | Live page (same FY) | API record | Officer re-checked |
|---|---|---|---|---|---|
| 94-3087060 | Abode Services (CA) | rev 158,389,185, exp 150,365,574, assets 74,106,426, fy2025 | revenue $158,389,185, expenses $150,365,574 | revenue_amount 158,389,185, asset_amount 74,106,426 | Vivian Wan, Chief Executive Officer, $323,238 (fy2025 table) — stored identically; fy2023's CEO Louis Chicoine $300,007 also stored for 2023 |
| 95-3950196 | People Assisting The Homeless (CA) | rev 175,138,803, exp 170,072,825, fy2025 | revenue $175,138,803, expenses $170,072,825 | revenue_amount 175,138,803, asset_amount 66,376,077 | Jennifer Hark Dietz, Chief Executive Officer, $311,188 — stored identically |
| 30-0104507 | Ali Forney Center (NY) | rev 29,485,367, exp 27,183,672, fy2024 | revenue $29,485,367, expenses $27,183,672 | revenue_amount 29,485,367, asset_amount 32,346,486 | Zachary Bar Cohen, Deputy Executive Director Of Devt, $255,390 — stored identically |
| 90-0924350 | Our Brothers Keeper Shelter (MI) | rev 906,531, exp 561,879, fy2024 | revenue $906,531, expenses $561,879 | revenue_amount 906,531, asset_amount 762,766 | Jeffrey Auguste, Executive Director, $40,985 — stored identically |
| 46-1235885 | Monroe County Homeless Services Continuum-of-care (FL) | rev 900,790, exp 873,194, fy2024 | revenue $900,790, expenses $873,194 | revenue_amount 900,790, asset_amount 460,330 | Mark Lenkner, Executive Director (thru 04/24), $35,831 — stored identically |

Notes on the checks: `revenue`/`expenses`/`assets` and the three top-compensated Part VII rows
(person, title, reportable compensation) were compared field by field and matched exactly in all five
cases. The API's `revenue_amount` is the most recent return's income as carried in the BMF extract, so
it agrees with the page's newest fiscal year in all five; note that the API's own `filings_with_data`
time series lags (TY2023 at the time of writing) while the filing page renders FY2025 returns, which
is why the page, not the API series, is the source for `revenue`/`expenses`/`fiscal_year`.

Two direct-primary-document checks were attempted and are blocked at the publisher: the Form 990 XML
endpoint (`download-xml?object_id=…`, HTTP 403 "Security Check") and the IRS PDF of the return
(`display_990/…`, Cloudflare interstitial). No value in this domain therefore rests on a PDF that
could not be re-read; every number traces to the rendered filing page recorded in `source`.

## Coverage

* **Organisations: 11,404** `org` rows with `org_type='nonprofit'` across **53** states/territories
  (target 4,000+). 10,925 carry an NTEE code, 10,529 a ZIP. 10,083 came from the BMF file, 1,401 from
  the search sweeps (and were not already in the BMF set); the union is 11,484 candidates, of which 11,404 become distinct `org` rows — two EINs with the
  same normalised name in the same state collapse onto one row (the `org` key is `name_norm`+`state`).
* **Financials: 4,626** orgs have revenue/expenses/assets/fiscal_year from a Form 990 (target was
  every org row where the source publishes it — 9,448 organisations were page-fetched, the other
  4,542 have no e-filed Form 990 at all; see below). Combined latest-year revenue **$20.63B**.
  Negative-revenue rows (990 total revenue below zero in a loss year, e.g. an endowment drawdown) are
  stored as filed.
* **People: 106,890** `org_person` rows (target 10,000+) covering **4,581** organisations and
  **48,837** distinct people, fiscal years 2013-2026. By role: 56,034 director, 37,494 officer,
  12,953 key employee, 409 untitled (the filing recorded a person without a title). 11,594 rows carry
  positive reportable compensation totalling **$1.155B**; the largest single figure is $1,434,419
  (Boston Rescue Mission, 2023).
* **Website: 221** orgs (1.9%), **phone: 0**. The IRS BMF and the Form 990 extracts do not publish
  either; the only keyless join is Wikidata's EIN property, which covers a small share of small
  nonprofits. Nothing is inferred from a name or domain guess.
* **Requests: 13,990** HTTP calls logged in `logs/ingest-nonprofit.jsonl` (13,702 × HTTP 200,
  273 × 404, 15 × 400/403): 4 BMF files, 23 search queries over 292 result pages, 9,539 filing-page
  records (9,448 organisations), 2 Wikidata passes.

### What is still missing, and why

* **1,941 organisations are registered but not yet page-fetched** (`nonprofit_candidate.page_done=0`)
  — the tail of the BMF by last reported income/assets. Yield falls off with depth but not to zero:
  the last 1,693 pages returned 1,239 person rows and 238 orgs with financials, and 276 orgs with no
  990 data. Resume with `python3 -m ingest.run nonprofit` (journal + progress tables make it additive,
  nothing is re-fetched; 1 req/s ≈ 28 min per 1,700 orgs). Knobs: `NONPROFIT_MAX_PAGES`,
  `NONPROFIT_BUDGET` (seconds), `NONPROFIT_PERSON_YEARS`, `NONPROFIT_NO_WIKI=1`.
* **PDF-only and pre-2013 filings have no officer data.** 4,542 fetched orgs returned no Part VII
  table and no extracted financials (`page_note = 'no-form-990-data …'`): they file the 990-N
  e-postcard, file on paper (PDFs are Cloudflare-blocked), or have stopped filing. Those orgs still
  have their `org` row, address and NTEE code. 95 candidates failed outright (HTTP 404 on the
  ProPublica page = EIN not in their index, or a parse failure) and are marked `page_done=2`.
* **Employees head-count is not captured (`org.employees` stays NULL).** "Total employees" lives in
  Form 990 Part I line 5, which only the bot-blocked XML/PDF exposes; the ProPublica page does not
  render it. Deriving it from salaries or officer counts would be invention.
* **Officer history is capped at the 3 most recent filing years per org** (configurable:
  `NONPROFIT_PERSON_YEARS`), to keep `org_person` a usable size. The pages carry up to ~13 years.
* **Orgs whose names and NTEE codes both miss the filter are not in the universe** — e.g. a general
  human-services agency that runs a shelter but is coded `P20` and named "Community Action Agency".
  They surface only when their name contains a homelessness term. Domestic-violence shelters coded
  `I70`/`P43` are largely excluded on the same rule (I70 is "abuse prevention", not homelessness).
* **Canadian/Australian/UK charities, for-profit providers and public housing authorities** are out
  of scope for this domain (`org_type` stays `nonprofit`).
