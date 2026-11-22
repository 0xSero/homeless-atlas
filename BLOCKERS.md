# Blockers

Anything that stops a domain, with exactly what unblocks it. Keep this list honest and current.

## Keys that would unlock more (none are strictly required for the keyless core)
- `DATA_GOV_API_KEY` (api.data.gov, free, instant) — SAM.gov entity registrations (UEI -> full registration,
  officer names, address, NAICS) and the FBI Crime Data Explorer at full rate. Without it we degrade to
  USASpending recipients + DEMO_KEY.
- `CENSUS_API_KEY` (api.census.gov, free) — ACS detail tables without hitting the keyless 500/day cap.
- `YOUTUBE_API_KEY` — quota-clean YouTube search. Without it we use `yt-dlp ytsearch`.

## health

**Blocked: SAMHSA NSDUH state prevalence tables — no `smi_prevalence` / `sud_prevalence` rows.**
`www.samhsa.gov` answers HTTP 403 to every request from this host (all user agents tried,
including a browser UA and Googlebot), so the state-level serious-mental-illness and
substance-use-disorder estimates cannot be fetched. Tried and failed on 2026-11-19:

| URL | status |
|---|---|
| `https://www.samhsa.gov/` | 403 |
| `https://www.samhsa.gov/data/` | 403 |
| `https://www.samhsa.gov/data/sites/default/files/reports/rpt42638/NSDUHsaePercents2022/NSDUHsaeExcelPercents2022.xlsx` | 403 |
| `https://pdas.samhsa.gov/saes/state` (NSDUH state-estimates explorer) | 503 |
| `https://store.samhsa.gov/` | 202, empty body |
| `https://library.samhsa.gov/` | reachable, but its search returns no downloadable state-prevalence workbook |

There is **no API key** for this — it is a WAF/IP block, not a rate limit. What unblocks it:
allowlist this host's egress IP, or fetch the current *NSDUH State Prevalence Estimates*
workbook (`NSDUHsaeExcelPercents*.xlsx`, linked from
`https://www.samhsa.gov/data/release/{year}-nsduh-state-prevalence-estimates`) and drop it in
`data/raw/health/`; the ingestor can then parse it into `metric(metric='smi_prevalence'|'sud_prevalence')`
with no other change. Fallbacks checked and unavailable: KFF State Health Facts SMI indicator
(404 under every slug), Internet Archive (offline), Common Crawl index (504).

**Blocked: CDC WONDER (`https://wonder.cdc.gov/`) — HTTP 403.** Used to be the route for
final county-level mortality queries (Multiple Cause of Death, D76/D157). Worked around with the
keyless Socrata mirror of the same NCHS release (`data.cdc.gov/resource/gb4e-yj24`), so
`overdose_deaths` by county is still captured; the only loss is final (non-provisional) county
counts and queries by age/sex/race.

**Not blocked, noted for the next worker:** city medical-examiner dashboards that publish
overdose deaths but expose no keyless tabular API (Chicago/Cook County ME case archive, LA County,
King County, Philadelphia, DC, Boston). San Francisco (DataSF) and Connecticut (data.ct.gov) were
reachable and are ingested.

## Web search for agents (facility/health/laws discovered this the hard way)
DuckDuckGo, Bing, Startpage, Mojeek, Ecosia and public SearxNG instances all challenge automated queries;
`data.cms.gov`, `healthdata.gov`, SAMHSA N-MHSS and asylumprojects.org block automated access. That is why
per-facility budgets and staffing stay thin.

**Available in this environment and not blocked:** `BRAVE_API_KEY` and `EXA_API_KEY` are present as environment
variables. Workers should use those APIs (never print the key) for source discovery instead of scraping a search
engine: Brave `GET https://api.search.brave.com/res/v1/web/search?q=...` with `X-Subscription-Token`,
Exa `POST https://api.exa.ai/search` with `x-api-key`. Read the keys inside the command only.

## laws

Not a key problem: this domain needs no API key. What is recorded here is what a *complete*
legal-landscape sweep would still need, and why each route was closed at the time of writing
(2026-11-19). The [laws ingestor](../ingest/domains/laws.py) ships 1,164 rows on the sources
that do work; details in `research/laws.md` § Coverage.

**1. Statute numbers for statewide camping / sit-lie / panhandling bans.**
NHLC's appendix matrix prints only an "x" per state/category, never a section number, so
those rows carry an appendix pinpoint rather than a statute section. To close this you would
have to read 50 state codes. Blocked without a key:
Tested 2026-11-19 with this project's user agent:

| URL tested | Status | Note |
|---|---|---|
| `https://law.justia.com/codes/alabama/` | 403 | state codes, best single index |
| `https://codes.findlaw.com/al/` | 403 | state codes |
| `https://casetext.com/` | 403 | |
| `https://www.ncsl.org/` | 403 | NCSL state-law database |
| `https://www.lawserver.com/law/state/alabama/al-code` | 307 (redirect not followed) | |
| `https://leginfo.legislature.ca.gov/faces/codes.xhtml` | 403 | California |
| `https://lis.virginia.gov/` | 200 (2.5 kB shell) | reachable, but no stable per-section URL scheme to cite |
| 48 other legislature sites | not attempted | each is a bespoke system; out of scope for a 1 req/s sweep |

What unblocks it: a state-code bulk download or an allowlisted egress IP for Justia/FindLaw,
or hand-curated section numbers per state with a reviewer who reads the code.

**2. NLIHC's State & Local Housing Trust Fund *database*** (would give the enabling statute and
year for every state fund): https://nlihc.org/housing-trust-fund-project/database renders a JS
application; the HTML contains no table and the page's own copy defers to the app
("You can get the most up-to-date counts ... from the State and Local Housing Trust Fund
Database"). Only the 14 states described in prose on `/housing-trust-fund-project` could be
ingested (`funding` rows). Unblocks with: NLIHC publishing the database as CSV/JSON, or a
browser session against the app.

**3. Housing Not Handcuffs "Emergent Threats Map"** (per-state bill tracker, would add 2024-26
status for all 50 states): https://housingnothandcuffs.org/map/ embeds the map client-side; the
data lives in a WordPress custom post type `igmap` which the public REST API returns with empty
`content` (`/wp-json/wp/v2/igmap` lists 3 maps — "Criminalizing Legislation by State",
"Defeated Bills", "Positive Policies" — all `content.rendered == ""`, and their own permalinks
404). Unblocks with: `context=edit` credentials for the site, or the site exposing the map data
as JSON.

**4. Housing First mandates per state.** Neither NLIHC's Housing First page nor 24 CFR Part 578
names state statutes (Part 578 does not contain the phrase "Housing First" at all — checked via
the eCFR API). Only California's mandate is documented (NHLC 2025). Unblocks with: a state-by-state
Housing First statute table, or HUD's CoC NOFO (which does impose the requirement for CoC-funded
projects) as a fetchable document.

**5. Property storage / belongings seizure.** No reachable source tabulates state or city rules on
storage of personal property taken during an encampment sweep. The NHLC publications index lists
report-length PDFs that touch it (`Tent_City_USA_2017.pdf`, `Eviction_Without_Notice.pdf`,
`Public-Property-Public-Need.pdf`) but they were not parsed for a jurisdiction table — they are
advocacy reports, not surveys. The `property` topic in this domain is vehicle habitation only.

**6. Federal court dockets.** `https://www.courtlistener.com/api/rest/v4/search/` answers HTTP 401
without a token (`COURTLISTENER_API_TOKEN`, free signup at https://www.courtlistener.com/sign-in/ ), and
`https://www.pacer.uscourts.gov/cgi-bin/login.pl` returned 404 (docs host is 200; the search
endpoints are paywalled per page). The Grants Pass holding and the Martin v. Boise rule
were therefore taken from the Supreme Court opinion text on Cornell LII (which quotes both),
not from the dockets. Unblocks injunction rows (status=enjoined) per city with a token.

## budgets

**Blocked: California HCD homelessness programme allocations (`hcd.ca.gov`) — HTTP 403.**
`https://www.hcd.ca.gov/grants-and-funding/homeless-housing-assistance-and-prevention` returns
403 with the ingest user agent, so the state's own HHAP round allocations (a per-CoC table) could
not be captured. No key exists for this — it is a WAF/IP rule, same class as the SAMHSA block
above. Unblocks a state-own-money line for the largest state programme: allowlist the egress IP or
drop the allocation table in `data/raw/budgets/` and point a `STATE_OWN`-style spec at it.

**Blocked: six of the ten named city budget portals are not machine-readable from this host.**

| City | Portal tried | Result |
|---|---|---|
| Portland | `data.portlandoregon.gov`, `opendata.portland.gov` | connection reset / host unreachable (every attempt) |
| Houston | `data.houstontx.gov` | HTTP 404 on `/api/views.json`, `/resource/...`, DCAT feed |
| Boston | `data.boston.gov` | HTTP 404 on `/api/views.json`, DCAT feed |
| Denver | `data.denvergov.org` | `/api/views.json` and `/resource/...` return the HTML catalogue page, not JSON |
| San Diego | `data.sandiego.gov` | HTTP 404 on `/api/views.json`, DCAT feed, CKAN API |
| Washington DC | `opendata.dc.gov` (DCAT feed, 1,919 datasets) | no citywide homelessness budget dataset: only "FY2020 Proposed Mayoral Budget Submission", school/DCPS budgets and a 2014-2018 employment-services extract |

Their CoC-level federal money is still captured (`hud_coc_esg_obligated_county_attributed`,
`award_hud_coc_attributed`); what is missing is the city's *own* appropriation. Unblocks per city:
a `CITY_BUDGETS`-style spec against a tabular budget endpoint (Socrata resource, ArcGIS feature
service or CSV) that shows the homelessness department/programme line.

**Note, not a block:** `api.census.gov` government-finance series would give every state's
"housing and community development" expenditure, but keyless calls now return the *Missing Key*
page (see the `CENSUS_API_KEY` entry above); it is also a housing function, not homelessness, so
it was not used as a state-budget proxy.

## uk

**Not blocked, but one host is unusable: `www.nihe.gov.uk` answers `403.2 Forbidden` (15 bytes) to
every path, with a plain UA and with a full browser UA/header set, from this host.** It is the
Northern Ireland Housing Executive, the publisher of the *Northern Ireland Homelessness Bulletin*.
There is **no API key and no signup** involved — it is a WAF/IP block, so nothing can be
"requested"; the only unblock is allowlisting this host's egress IP or fetching the bulletin
workbook by hand into `data/raw/uk/`.

Worked around, not lost: the same bulletin is published by the Northern Ireland **Department for
Communities**, whose tables XLSX is reachable — with two quirks that had to be reverse-engineered:

| URL | plain request | browser UA + `Accept: */*` | + `Accept-Encoding: gzip, deflate` |
|---|---|---|---|
| `https://www.nihe.gov.uk/` | 403 | 403 | 403 |
| `https://www.nihe.gov.uk/Working-With-Us/Research/Homelessness-statistics` | 403 | 403 | 403 |
| `https://www.communities-ni.gov.uk/topics/housing-statistics` | 403 | 403 | **200** |
| `https://www.communities-ni.gov.uk/system/files/2026-06/ni-homelessness-bulletin-oct-mar-2026-tables.xlsx` | 403 | 403 | **200** (1,507,427 B) |

Note that adding `br` to `Accept-Encoding` puts `communities-ni.gov.uk` **back** to 403, so the
ingestor sends exactly `gzip, deflate`. Data captured: NI presentations, acceptances and temporary
accommodation placements by the 11 local government districts plus the NI total, October 2025 –
March 2026.

Also worth recording for the next UK worker: **there is no keyless JSON API on StatsWales any
more** (`statswales.gov.wales/api/v1/...` was retired with the move to `stats.gov.wales`), and
`data.nisra.gov.uk` is a PxStat instance whose REST paths (`/api/v1/...`) all answer 404, so
neither is usable for a scripted pull; StatsWales CSVs come from a form POST instead (see
`research/uk.md`).

## canada

**Blocked: per-community Reaching Home funding allocations are not published in bulk.**
HICC publishes Reaching Home money as *project* grants, not as a per-community allocation table.
The only public machine-readable route is the government-wide proactive-disclosure register
(`https://search.open.canada.ca/grants`, dataset `432527ab-7aac-45b5-81d6-7597107a7013`), whose
bulk CSV (`.../resource/1d15a62f-5656-49ad-8c88-f40ce689d831/download/grants.csv`, 244,809,728 B)
was fetched and found to contain only departments alphabetically A–C (Canadian Heritage …
Canada Water Agency, 236,715 rows) — **no HICC / Infrastructure Canada rows**, so it yields
nothing for this domain; and the per-organization per-fiscal-year files behind the search UI are
not discoverable through the open.canada.ca CKAN API either (`fq=organization:infc` returns 24
datasets, none of them grants). The Reaching Home programme page prints no envelope figure; the
one dollar amount on a public HICC programme page is Budget 2022's $18.1 M over three years for
Action Research on Chronic Homelessness, which is what `metric(reaching_home_action_research_funding)`
holds. What unblocks it: an HICC grants-and-contributions extract filtered to `owner_org=infc`
(drop it in `data/raw/canada/` and point the ingestor at it), or the `grants.csv` bulk file
re-issued with HICC included.

**Blocked at the publisher: `homelessnesslearninghub.ca` → HTTP 403 (Cloudflare interstitial).**
The Government of Canada's Homelessness Learning Hub hosts the *only* national per-community PiT
table ("Everyone Counts 2024: Community Enumeration Results"). Every request from this host gets
the "Just a moment…" challenge page (5,875 B, JS required); no UA/header combination gets past it.
Worked around, not lost: the same page is read from its Internet Archive snapshot
(`web.archive.org/web/2026/https://homelessnesslearninghub.ca/library/resources/everyone-counts-2024-results/`),
which is what the `source` row records — 39 communities with values. Unblocks a direct read (and
the 2025 edition, once published) with: an allowlisted egress IP, or the page saved by hand into
`data/raw/canada/`.

**Blocked at the publisher: `www.homelesshub.ca` → connections aborted.** The Canadian Observatory
on Homelessness community profiles carry one PiT snapshot per community (total, sheltered,
unsheltered, with the count year) for ~40 non-BC communities, which would have added a second
cycle for the big cities. Early in the sweep one profile returned 200 and its table parsed; from
then on every request died with
`ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected(...)))` — an IP-level
block, not a rate limit (1 req/s was honoured). The code path is kept but disabled
(`HH_ENABLED = False` in `ingest/domains/canada.py`) so re-runs do not hammer it. Unblocks with:
an allowlisted egress IP, or the `Everyone Counts` 2025 community table from HICC, which would
supersede it.

**Not blocked, but not swept: per-community counts outside British Columbia.**
Alberta, Saskatchewan, Manitoba, Ontario, the Atlantic provinces and the territories publish each
community's PiT result only inside that community's own PDF report (~40 documents, a different
layout each). HICC aggregates them and never republishes the table. The ingestor captures what is
tabular (the 2024 HLH table, 39 communities; BC, 33 count areas over four cycles; the national
series over five cycles) and leaves the PDF-by-PDF sweep to a worker who can verify each
transcription.

## australia

Five hosts were tried and failed during this sweep. None of them needs a *key* — three are
unreachable from this machine at the network level, one is behind a bot challenge, and one is a
hostname that only resolves inside a partner network. Nothing below is an API-key blocker, so
there is no signup URL to send; each entry says what would actually unblock it.

**1. `www.aihw.gov.au` → Cloudflare JS challenge on every path (this is the big one).**
The Australian Institute of Health and Welfare is the custodian of the Specialist Homelessness
Services Collection (SHSC) and publishes the *Specialist Homelessness Services annual report* data
tables (clients by LGA/SA4/PHN, agency and outlet counts, beds, demographics, unmet need,
housing outcomes). Every request from this host — plain UA, full browser UA, `Accept: */*`,
`Accept-Encoding: gzip, deflate`, `fetch()` and raw `curl` — returns the "Just a moment…" page
(5,572–5,875 B, JS required), including the direct binary paths:

| URL | response |
|---|---|
| `https://www.aihw.gov.au/reports/homelessness-services/shs-annual-report` | 403 + Cloudflare challenge |
| `https://www.aihw.gov.au/reports/homelessness-services/shs-annual-report/contents/data-tables` | 403 + Cloudflare challenge |
| `https://www.aihw.gov.au/getmedia/…/<file>.xlsx` (any direct file path) | 403 + Cloudflare challenge |

*Worked around, not lost*: the Productivity Commission's **Report on Government Services 2026,
Part G section 19 "Homelessness services"** republishes the SHSC-derived per-jurisdiction numbers
and the expenditure series as one keyless CSV, which is where every `shs_*` row in this domain
comes from (tables 19A.1, 19A.11, 19A.21). What that does **not** give is anything below state
level. *Unblocks*: an allowlisted egress IP, a hand-saved copy of one SHS annual report data
workbook in `data/raw/australia/`, or AIHW's own bulk-data platform replacing the public site —
then the LGA/SA4 client counts and agency-level tables become writable with no schema change.

**2. AURIN's AIHW SHSC geography datasets → the WFS host does not resolve.**
`data.gov.au` lists 13 AIHW SHSC datasets aggregated to 2018 LGA / SA4 / SA3 / SA2 / PHN / GCCSA
(class "Total Clients 2014-2019", CC BY 3.0 AU), each with a single resource whose URL is
`https://adp-access.aurin.org.au/public/download/?uri=https://adp-gatekeeper.prod.aurin-prod.cloud.edu.au/geoserver/wfs?...`.
The inner hostname is internal to AURIN:
`curl: (6) Could not resolve host: adp-gatekeeper.prod.aurin-prod.cloud.edu.au` (NXDOMAIN from this
machine, IPv4 and IPv6). The download-manager URL itself answers 200 but only serves an HTML
landing page. *Unblocks*: a resolvable AURIN endpoint (or an AURIN account whose download manager
mints a signed URL), or the same tables from AIHW per (1). These would add `shs_clients` per LGA
for 2014-15 … 2018-19, i.e. a second SHS cycle at LGA level.

**3. `databyregion.abs.gov.au` → no DNS.** ABS "Data by Region" is where LGA/SA-level ERP
population lives; without it the 555 `geo_city` rows here keep `pop = NULL` (nothing is estimated).
`ConnectionError: MaxRetryError … Name or service not known`. *Unblocks*: the ASGS/ERP CSV by hand
(`32180` regional population tables) or a working host.

**4. `www.housing.gov.au` → no DNS**, and **`www.dss.gov.au` → Akamai resets.**
These are the two publishers of the **National Agreement on Social Housing and Homelessness
(NASHH, from 1 July 2024)** and its predecessor the National Housing and Homelessness Agreement —
i.e. the per-state bilateral funding schedules. `www.housing.gov.au` is NXDOMAIN from here
(`curl: (6) Could not resolve host`); `www.dss.gov.au` either fails TLS/HTTP2 mid-stream
(`curl: (92) HTTP/2 stream 0 was not closed cleanly: INTERNAL_ERROR`) or times out with zero bytes
on HTTP/1.1. *Consequence*: `shs_funding` holds RoGS's recurrent government expenditure series
(state/territory governments' own expenditure, real 2024-25 dollars), which RoGS explicitly frames
as the NASHH homelessness component and which matches the "$1.8 billion / $64.05 per person" figure
its chapter text states — but it is **not** the bilateral allocation per state. *Unblocks*: either
host reachable, or the NASHH bilateral schedules as CSV in `data/raw/australia/`.

**Not blocked, deliberately unswept**: ABS cube 5 (SA2/SA3/SA4 homeless counts) and cube 7
(Indigenous status) are fetched and parse cleanly but are not written, because the store interface
for this domain defines only `au_state` and `au_lga` entities; parking SA4 rows in `kind='au_lga'`
would mislabel geography. ABS also publishes no LGA-level estimate for 2006/2011 in this release,
so the LGA layer is two Census years, not four.

## sf

**Blocked: no reachable primary source for the November 2024 ballot measures (`sfelections.org`, `sf.gov`, `ourcityourhome.org`).**
The assignment asks for Measure C 2024 budget tracking next to Prop C 2018. What was tried, on
2026-11-19:

| URL | result |
|---|---|
| `https://www.sfelections.org/` | HTTP 403 (also `sfelections.sfgov.org/`) |
| `https://www.sf.gov/measures-and-ballot-arguments` | HTTP 202 with an empty body (bot challenge) — same for every `sf.gov` page tried, including `/reports`, `/departments/sheriff`, `/new-laguna-honda`, `/departments/department-homelessness-and-supportive-housing` |
| `https://www.sf.gov/sites/default/files/2024-07/LHH%20Community%20Report.pdf` | HTTP 202, empty body |
| `https://ourcityourhome.org/` | DNS/connection failure (host does not resolve from here) — no OCOH committee reports, no Prop C spending plan |
| `https://openbook.sfgov.org/` | connection timeout |
| `https://ballotpedia.org/San_Francisco,_California_measures,_November_2024` | HTTP 202, empty body |
| `http://web.archive.org/cdx/search/cdx?url=www.sfelections.org/results_110524/*` | 200, **zero** snapshots — the archive has nothing for the SF 2024 results pages |
| `https://data.sf.gov/resource/88s2-6ua9.json?$where=year='2024'` | works, but that database stops at the **March 2024** ballot (7 measures); the only 2024 Measure C it has is the March 2024 real-estate-transfer-tax exemption, which is not a homelessness measure |

No API key exists for this — it is a WAF/IP rule plus one dead domain. What unblocks it: an
allowlist for this host's egress IP, or a copy of the November 2024 voter information pamphlet /
OCOH annual report dropped in `data/raw/sf/`. Until then this domain publishes Prop C 2018 and the
`SR Our City Our Home Fund` appropriations (FY2019–FY2028) and **asserts nothing** about Measure C 2024.

**Partial: granular SF program-level budget stops at FY2018.** The Controller's Budget dataset
(`xdgd-c79v`) publishes per-program lines (Laguna Honda `DA5`, SFGH `D1H`/`D1P`/`D5S`, Sheriff
`AFC`, HSH `CSH`/`COT`) only through FY2018; from FY2019 every department reports a single `OPR`
"Operating" program, so later years are captured at department total and at hospital operating-fund
level instead. Unblocks it: a Socrata dataset or CSV of the Annual Appropriation Ordinance detail
for FY2019+ (OpenBook's own exports).

## org_contacts

**Blocked: SAM.gov entity registration as a source of organisation websites (`api.sam.gov`).**
The brief lists the org's SAM registration as one of the three possible origins of a website address.
That data lives behind the SAM Entity Management API, which needs an `api.data.gov` key we do not have
and which is *not* in the keyless set this repo already uses. What was tried on 2026-11-19:

| URL | result |
|---|---|
| `https://api.sam.gov/entity-information/v3/entities?ueiSAM=…` | HTTP 404 with no key, and 404 with `api_key=DEMO_KEY` (DEMO_KEY is only valid for a handful of api.data.gov services) |
| `https://sam.gov/api/prod/sgs/v1/search/?index=ei&q=ABODE+SERVICES` | HTTP 200, but the `ei` index is the **exclusions** list (debarment records), not entity registrations — returned entities are excluded parties, no website field |
| `https://sam.gov/api/prod/sgs/v1/search/?index=entity` / `index=sam` / `index=entity-registration` | HTTP 400 bad request — no public (keyless) entity-registration search endpoint |
| `https://www.sam.gov/` bulk extract (UI download) | login-gated |

**Key needed:** `SAM_API_KEY` (an `api.data.gov` key), free and instant:
<https://api.data.gov/signup/> (then `https://open.gsa.gov/api/entity-api/` for the endpoint contract;
the website is the entity's own URL field in `entityInformation`).
**What it would add:** websites for the ~10,000 `org` rows that have a UEI but no EIN — the
government agencies, housing authorities and CoC lead agencies that never file a Form 990. Until then
those orgs are only covered if they file a 990 or already carry a website in Wikidata.

**Partial: IRS TEOS batch `2026_05A` and friends are Deflate64.** Some published batches are stored
with zip compression method 9 (Deflate64), which Python's `zipfile` cannot inflate
(`NotImplementedError: That compression method is not supported`). Handled, not blocked: those batches
are unpacked with the system `unzip` (which does support method 9) and parsed from disk — see
`_harvest()` in `ingest/domains/org_contacts.py`.

**Not a blocker, recorded so nobody re-tries:** `https://s3.amazonaws.com/irs-form-990/<object_id>_public.xml`
(the AWS Open Data mirror of individual 990 XMLs) lists as **empty** — `list-type=2` returns
`KeyCount 0`, and a known object id answers `NoSuchKey`. Per-filing XML therefore has to come out of the
annual bulk batches, and ProPublica's `download-xml?object_id=…` is HTTP 403 for this host (recorded by
the `nonprofit` worker).

## search-index

**Not a source blocker — a scope blocker.** The item ("split `<letter>` shards into `<letter>-<0..9>`
buckets, keep every fetched file under 250 KB, p95 < 300 ms on 3G") can only be implemented in
`scripts/build_site.py:build_index()`, `site_src/app.js:loadShard()` and `site/data/idx/`, all of which
rule 4 forbids this worker to write. There is no ingest-side hook to use instead: `build_index()` builds
from in-memory DB reads and no other code reads a prepared index (`grep -rn "data/idx|search-index.json"`
→ `scripts/build_site.py`, `scripts/verify.py`, `site_src/app.js` only).

State as measured (details, baselines and design options with sizes in `research/search-index.md`):

- the `<letter>-<0..9>` split already exists (commit `f43045e9b9`); largest shard `c-9.json` is
  423,061 B on disk, 115,001 B brotli on the wire, and **76 of 360** shards exceed 250 KB on disk;
- the client still fetches all ten buckets of a letter, i.e. 4,071,823 B on disk / 1,131,932 B on the
  wire for one keystroke on `c` (byte counts fetched live from `homeless.sybilsolutions.ai`), which is
  ~6 s on Fast 3G and ~23 s on Slow 3G — criterion 3 fails by an order of magnitude;
- "continuum of care" baselines at 141 scored rows (top `CONTINUUM OF CARE INC`); "housing clinic" has
  **4** scored rows, all `person`, because **`Tenderloin Housing Clinic` is not in `org` or `org_person`
  at all** (`like '%TENDERLOIN HOUSING%'` → 0 rows) — the exemplar in the code comment does not exist in
  this atlas, so that acceptance test can only check "unchanged", not "finds the org".

### Unblock

A decision, not a key. One of:

1. grant an exception to edit `scripts/build_site.py:build_index()`, `site_src/app.js:loadShard()` and
   `site/data/idx/` (plus permission to run `scripts/build_site.py` and the crawl gate) — 2-level
   `token → bucket list` manifest, 4-char token prefixes, payload `[type,title,url]` + shared subtitle
   table, files capped ~200 KB; or
2. move search to the Worker (`/api/search` over KV/D1 — `wrangler.jsonc` already deploys
   `scripts/deploy-worker.js`), which is the only option that makes p95 < 300 ms on 3G hold for common
   tokens; or
3. re-scope the item to something a domain worker owns. As of today a domain worker owns nothing here.

Caveat for whoever takes it: no static layout that keeps whole-letter substring matching fits 300 ms on
3G — a keystroke has to reach ≲ 40 KB, and the acceptance wording needs to say whether the 300 ms is
per keystroke or warm-cache.

## 2026-11-21 — org websites / emails cannot reach 5,000 / 2,000 from bulk public data

**Target (goal item 3):** `org.website >= 5000`, `org.email >= 2000`.
**Reached:** 2,023 websites, 832 emails.

Measured yields, so the shortfall is a fact rather than an opinion:

| source | attempted | websites obtained | emails obtained |
|---|---|---|---|
| Form 990 e-file XML (`WebsiteAddressTxt`, Part VI email fields) | 2,782 filings parsed | 10 (0.4%) | 0 |
| IRS BMF | not usable — the file carries no web or email field at all | — | — |
| ProPublica Nonprofit Explorer | their API exposes filings and PDF links, no website/email; `download-xml` returns 403 to non-browser clients | 0 | 0 |
| Brave Search (key present, slim API) | 3 queries spot-checked | top hits are Nextdoor, Reddit, USASpending for a named organisation — needs a per-org query budget | — |

**Brave's quota is the binding constraint, measured:** `x-ratelimit-policy: 1;w=1, 2000;w=2592000`
— one request per second, **2,000 requests per 30 days**, of which 1,911 remained when this was
checked. Measured yield over a 25-organisation trial: **7 websites (28%) and 3 emails (12%)** per
lookup. Spent against the whole remaining quota that is ≈ 535 websites and ≈ 230 emails, i.e. about
2,560 websites and 1,060 emails in total — short of both targets, and it would exhaust the month.

**What would unblock it:** SAM.gov's entity API returns `entityURL` for every registered entity and a
point of contact (free key from api.data.gov, one-time signup, no cost), which covers the
organisations that hold federal awards — the ones that matter most here. The Brave key present in the
environment can top up the rest, but only at 2,000 lookups per month.

**Decision:** leave the columns at their verified values rather than fill them from search results
that cannot be checked; the goal's own rule is that a URL that does not answer is worse than NULL.

## 2026-11-21 — Part III XML: was blocked, then fixed (my extraction code, not the source)

**Status: resolved.** The 990 XML ingest stalled at 1,945 of 5,000 organisations and I first recorded
it here as a source limitation. That was wrong. Three bugs in `ingest/domains/efile.py` were stacked:

1. `unzip -o -j -q <zip> <names…> -d <dir>` — `-d` must come **before** the member list, otherwise
   `unzip` reads the directory as another member name and exits non-zero (which `check=False` hid).
2. `unzip -j` writes the published basename `…_public.xml`, but the parser looked for `<oid>.xml`, so
   every successfully extracted filing was invisible and reported as missing.
3. The index names some batches `2024_TEOS_XML_05a` while the published archive is `05A`.

After the fixes a supervised run parsed 1,201 filings with **0 failures**. Lesson recorded because it
generalises: a script that extracts nothing should raise, not report a count of zero and move on —
`check=False` and a missing rename turned a loud error into a silent "blocked".

## 2026-11-21 — the ingest loop cannot run while the model endpoint is down

The loop launches `pi -p --model homelab/deepseek-v4.1-flash-1017` workers. Twice today a launched
worker sat with an empty log, zero TCP connections and no rows written for ten minutes:

```
$ curl -s -o /dev/null -w '%{http_code}' https://api.homelabai.org/v1/models
000
$ ps -eo etimes,args | grep atlas-subawards
291 timeout 7200 pi -p --model homelab/deepseek-v4.1-flash-1017 ...
```

The subawards item was finished by hand (`ingest/domains/subawards.py`, 2,705 rows) rather than wait
for the 7,200 s worker timeout. The loop is installed as `atlas-loop.service` (systemd user unit,
`Restart=always`) but left **stopped** until `api.homelabai.org` answers or the unit's `MODEL` is
changed to a reachable one — spawning workers that cannot connect just burns two hours each.

### Partial unblock: 544 addresses recovered from pages we had already fetched

The 19 GB page cache under `data/raw/org_contacts/` holds 5,023 pages from the organisations' own
websites. Re-parsing it — no requests, no quota — found a plausible published address for **1,225
organisations**, of which **544 had none on record**: emails 862 → **1,406**.
`scripts/mine_contact_cache.py` records the page each address came from in `org_email_mine`.

Crawling contact pages for the remainder was tried and abandoned: a bounded run over the organisations
that have a website but no address found no usable mailbox in the pages it fetched (these sites hide
contact behind a form, or render it client-side). Mining what was already stored was the only
productive route, and its quota cost was zero.

### The loop now waits for its model instead of lying to itself

The ingest loop died twice on 2026-11-21 because `api.homelabai.org/v1/models` returned nothing while
workers were already launched: each `pi` worker sat with an empty log and zero TCP connections for its
full 7,200 s timeout, then the item looked like a domain that produced no data. Three items were left
stuck `RUNNING`.

`scripts/loop.sh` now checks the endpoint before launching anything and sleeps 300 s between attempts,
logging why. Observed on start:

```
loop start MAX_PAR=1 MODEL=homelab/deepseek-v4.1-flash-1017
model endpoint unreachable for MODEL=homelab/deepseek-v4.1-flash-1017 — waiting 300s (no worker is started)
```

The unit is enabled again: it costs one `curl` every five minutes and resumes by itself the moment the
endpoint answers, which is better than a disabled unit that needs a human to notice.

## 2026-11-21 — Part III has a hard ceiling of 2,903 organisations in this archive

**Target (goal item 4):** 5,000 organisations with a Part III program-service description.
**Reached:** 2,903. The gap is structural, and here is the arithmetic.

Of the 11,404 organisations that carry an EIN:

| step | count |
|---|---|
| EINs in the archive | 11,404 |
| e-filed returns found in the 2019–2026 IRS indexes and parsed | 4,027 |
| of those, filings that actually carry Part III text | **2,903** (74%) |
| EINs with **no e-filed return in any published index year** | 7,191 |

The 7,191 are not a fetching failure. Five sampled at random — Wyoming Homeless Collaborative
(472437635), Safe Haven Associates (911727990), H.E.L.P. (133449085), Promise House (752180083),
Montgomery County Homeless Continuum (760612493) — appear in **neither the 2025 nor the 2026 e-file
index**. Those organisations file the 990-N postcard, which has no Part III, or they have stopped
filing; the remainder of the 74% shortfall are 990-EZ filers, whose form has no program-service
section either.

AWS's `irs-form-990` bucket, which used to serve one XML per filing and would have made a wider sweep
cheap, is now **empty** (`KeyCount 0`). The only published route is the per-year batch ZIPs, which the
ingest uses; it cannot conjure filings that were never submitted electronically.

**A second, smaller ceiling found later:** the 2019–2021 indexes carry no `XML_BATCH_ID` column, only
`OBJECT_ID`. Without the batch name the archive holding a filing cannot be addressed, so **438 filings
belonging to unfetched organisations** (274 from 2021, 164 from 2020) would require downloading whole
year archives — ~24 per year, 300–800 MB each — and scanning them. Recovering all of them at the
observed 74% adds ~325 rows: 2,903 → ~3,230, still short of 5,000.

**What would unblock it:** a different definition of "program description" — the organisations' own
websites (2,092 of which are verified in this archive) — or a paid filing aggregator. Neither is a
Form 990 Part III, so the target as written cannot be met and is left short rather than filled with a
substitute.

## 2026-11-21 — the website gap narrowed to 3,937 of 5,000, by name-matching not by search

Search had a quota; the Common Crawl domain rank list does not. It names every domain the crawl has
seen (134,979,706 of them, 2.5 GB), so an organisation whose name appears in its own domain can be
found without asking anybody. Two passes:

| pass | rule | candidates | verified and written |
|---|---|---|---|
| 1 | exact name key, or 12-char prefix within ±6 | 5,086 | 1,269 |
| 2 | containment anywhere, ±10 length window | 8,049 | 408 |

Every candidate was fetched, and written only when the page named the organisation together with its
city, or all of its distinctive words with its city, or two distinctive words with its state.
**Websites 2,092 → 3,937.** The pages were then mined for contact addresses on the spot: **emails
1,422 → 2,297, which meets the 2,000 target.**

**A precision bug worth recording.** The first run of the city/county pattern generator accepted a
match when the city name appeared on the page, without the state — and put `charleston.org` on
Charleston, West Virginia (that domain mentions California, not West Virginia). Re-checking all 20
accepted rows against the strict rule dropped **8 of 20 (40%)**, including `cityofsanantonio.org` for
San Antonio, whose real site is `sanantonio.gov`. The rule now demands the state; the cost is that
`cityofappleton.org` — a correct site — was dropped too, because its page carries no state text. A
missing website is a smaller error than a wrong one.

**Still short:** 1,063 websites. What is left are government entities whose domains do not contain
their name, and organisations with no site at all. The honest next step is the free SAM.gov entity API
(`entityURL` per registered entity), which resolves addresses by UEI rather than by guessing domains.

### City, county and housing-authority websites, generated then proved

Government entities' domains do not always contain their name, so the Common Crawl match misses them.
Their addresses are predictable instead: `cityof<place>.org`, `<place><st>.gov`, `ci.<place>.<st>.us`,
`co.<place>.<st>.us`, and for housing authorities `hac<town>.org`, `<town>ha.org`, `<town>pha.org`.
The URL is generated, but it is written **only when the page proves it**: HTTP 200, the place name on
the page, *and* the state (abbreviation or full name).

| family | tried | written |
|---|---|---|
| cities, counties, towns, villages, boroughs | 690 | 309 |
| housing authorities | 317 | 29 |
| **total** | **1,007** | **338** |

Websites **3,937 → 4,275**; contact addresses mined from those same cached pages took emails to
**2,382**. Housing authorities yield badly (9%) because the small ones have no site at all.

### Sources tried for the last 380 websites, and what each yielded

| source | mechanism | yield |
|---|---|---|
| Common Crawl domain ranks | name in domain, 3 passes (exact, containment, any 9-letter run) | 1,811 |
| generated city/county patterns | `cityof*`, `<place><st>.gov`, `ci.*`, `co.*` + name/state check | 338 |
| housing authority patterns | `hac<town>.org`, `<town>ha.org`, `<town>pha.org` | 29 |
| OpenStreetMap tags | 6,939 shelter/NGO `website` tags matched by name+city | 5 |
| city + service keyword patterns | `<city>homeless.org`, `<city>rescuemission.org` | 13 |
| Brave web search | 1,200 lookups of the remaining quota | 168 |
| Wikidata by EIN (P1297) | 400 EINs queried for P856 | 1 |
| Wikidata by label | 1,200 long names queried for P856 | 3 |

Every one of those is now spent. The remaining organisations either have no website in any public
corpus, or a domain that shares no token with their name — which is exactly what a registry keyed by
UEI solves and a name guess cannot.

### The last of the Brave quota, spent deliberately

1,200 lookups (the remainder of the 2,000/30-day allowance) over the organisations with the largest
federal dollars and no website: **168 sites and 71 addresses**. Every search result was fetched before
being written. Websites **4,449 → 4,617**; emails **2,429 → 2,509**, now a quarter above their target.
The quota is spent, and the next free window is weeks away.

### Third pass: any 9-letter run of the name, anywhere in the domain

Passes 1 and 2 only matched a name at the start or end of the domain label. Pass 3 indexed every
9-letter run of each distinctive name (an organisation called "Homes for Hope" is findable inside
`siouxlandhomesforhope`). Candidates exploded to 16,590 — most of them nonsense, "Strategic Research
Group" matching `researchgate.net` — so the result rests entirely on the page check: 7,694 candidates
fetched, **134 written (1.7%)**, websites **4,275 → 4,435**.

The tail is now exhausted: 1.7% acceptance means the remaining 18,000 organisations without a website
are mostly organisations with no site at all, or whose domain shares no run of nine letters with their
name. Further passes would cost hours for a handful of rows. **4,435 of 5,000 (89%)**, and the honest
unblock for the rest is SAM.gov's `entityURL`, keyed by UEI rather than guessed from a name.

## 2026-11-21 — ProPublica holds the missing filings and refuses to hand over the PDFs

The 7,191 organisations with no e-filed return still have paper in the world: ProPublica's Nonprofit
Explorer API answers for them, returning filings from 2011 onward that the IRS XML indexes no longer
reach. Three of six sampled organisations had them:

```
911727990 SAFE HAVEN ASSOCIATES           with_data=8   years 2011-2016  pdf=True
752180083 PROMISE HOUSE INC               with_data=12  years 2011-2016  pdf=True
760612493 MONTGOMERY COUNTY HOMELESS COA  with_data=6   years 2011-2018  pdf=True
```

That looked like the unblock for Part III: `pdftotext` the filed PDF, cut the Part III block, write the
program text. `ingest/domains/efile_pdf.py` does exactly that and it works right up to the download.

```
GET https://projects.propublica.org/nonprofits/download-filing?path=10_2019_prefixes_86-92%2F911727990_2
    with a browser user-agent, Referer and Accept: application/pdf
-> HTTP 403, 6,057 bytes, "text/html"   (a bot wall, not a missing file)
```

Fetching past that wall would mean defeating bot protection, which this project does not do. The
ingestor keeps its PDF path and progress table for the day a lawful route exists — an api.data.gov key
for the IRS bulk PDFs, or a ProPublica API that includes document access.

**Part III therefore stays at 2,903 of 5,000**, short by 2,097, with three documented causes: no e-filed
return exists for 7,191 organisations, the 2019–2021 indexes carry no batch id for 438 filings, and the
PDFs that would cover the rest are behind a 403.

## 2026-11-22 — provenance audit: every website and address is now backed by a recorded check

Six mechanisms gathered tonight's 4,599 websites and 2,383 addresses, each writing to its own evidence
table — but 2,025 websites and 826 addresses predated those tables and carried no trace of the fetch
that justified them. An archive that cannot say why it believes a URL is no better than one that
guesses.

`scripts/verify_websites.py` fetched every one of them again and recorded the answer:

| result | count |
|---|---|
| websites re-checked | 2,025 |
| still live | **2,006** |
| dead, cleared from the record | **19** |
| live but the page does not name the organisation (kept, flagged) | 85 |
| addresses re-proved against their own site | 826 checked, **525** confirmed, **301** cleared |
| addresses later restored from the page cache, with the page as evidence | **175** |

Websites **4,618 → 4,599**; addresses **2,509 → 2,383**, still 19% above the 2,000 target.
**Unevidenced now: 0 websites, 4 addresses.** The 85 live-but-unmatched sites are kept because a
client-rendered homepage can hide its own name, and they are documented rather than assumed correct.

### Charity Navigator, measured properly: it covers 2.4% of this population

Re-running the EIN lookup with the organisations ordered by revenue rather than by id settled what the
earlier tail suggested:

| tried | CN had a URL | accepted |
|---|---|---|
| 2,568 | **62 (2.4%)** | **48 (77% of those with a URL)** |

The limit is not verification, it is coverage: Charity Navigator holds a URL for 2.4% of the
organisations in this archive, because the archive is dominated by 990-EZ filers and government entities
that have no profile there. Extrapolated across the 8,777 organisations still without a website, the
whole source is worth ~210 URLs, of which ~160 would pass — taking the total to roughly 4,810, still
short of 5,000, at a cost of about nine hours of fetching. Not run.

### ProPublica's org pages do not carry websites (checked, closed)

The obvious next registry after Charity Navigator. Its robots.txt allows
`/nonprofits/organizations/<ein>`, the pages answer 200 — and they contain no organisation website. Two
EINs whose sites are already known (`133600232` → csh.org, `550784381` → wvceh.org) were checked against
their pages: neither URL appears anywhere in the HTML. Its JSON API has no website field either. A
checked negative, recorded so the next run does not repeat it.

### Tenth source: Charity Navigator, keyed by EIN — 2% overall, and why

Charity Navigator publishes one page per EIN with the organisation's URL embedded as `"url":"https://…"`,
and its robots.txt allows `/ein/` (it disallows exactly two EINs, which the ingestor skips). Unlike the
nine search-and-guess sources, this is a **registry keyed by the identifier we already hold**.

`ingest/domains/cn_sites.py` reads it, then proves the URL two ways: tier 1, the site itself names the
organisation; tier 2, the registry record carries the same name, or the same city and state, and the
domain answers. Both tiers are recorded in `cn_site_check`.

| tried | CN had a URL | accepted |
|---|---|---|
| 2,050 | ~40% of the first 600 | **48** (6 tier 1, 42 tier 2) |

The rate collapsed from 7.7% to 0.1% as the run went past the organisations CN actually covers: its
database is weighted to larger nonprofits, and the archive's long tail is 990-EZ filers with no profile.
**Websites 4,599 → 4,647.** Many of the sites answer a bot wall (HTTP 202 challenge), which is recorded
as liveness but cannot be read; the project does not try to pass it.

### 990 PDFs on the organisations' own websites: 2% yield, not worth the hours

A ninth source for Part III: 2,857 organisations have a verified website and no program text, and many
nonprofits post the return they filed. `ingest/domains/org_990_pdf.py` walks the homepage, a handful of
financial paths and any `href` that names a 990, downloads at most six PDFs, runs `pdftotext`, and cuts
Part III. It works — Downtown Rescue Mission's 2025 filing yields its mission statement verbatim — but
of the first 225 organisations tried, **5 had a PDF that answered (2%)**. At that rate the remaining
2,600 would take five hours and add ~60 rows against a 2,097 gap.

Two bugs worth recording, both of which made a working parse look broken:

* the anchor-based link scan missed whole sections on unbalanced markup — matching `href=` directly
  finds the same PDFs;
* `part3_text` terminated the block at the words "Schedule O", which appear in Part III's own header
  line ("Check if Schedule O contains a response"), so every extraction returned an empty string. The
  terminator is Part IV.

## 2026-11-22 — 732 organisations have no Part III but do state a purpose, and the difference is labelled

1,309 filings in this archive carry no Part III text because they are 990-EZ or 990-N filers, whose form
has no program-service section. Their silence is not total: **964 of them write a primary exempt purpose
into the 990 header** (`PrimaryExemptPurposeTxt`), which is the only prose some of them file —

> "Provide affordable housing for low-incom…" · "To promote awareness of the homelessness in New Hampshire."

`ingest/domains/efile_purpose.py` reads the XML already cached (no requests) and stores **732** of those
statements in their own table, `org_purpose`. They are **not** counted toward the Part III target and are
never called Part III: the org page heads them "What it says it is for" and says in as many words that
this filer has no Part III section, that this is the header field, and that it is not a programme-service
account. Ledger domain `org-purpose`, separate from `part3`.

A one-level `ROOT` miscalculation (`parent.parent` instead of `parent.parent.parent`) made the first run
report zero rows from a directory that does not exist; the fix is in `efile_purpose`, `efile_pdf` and
`org_990_pdf`.

## 2026-11-22 — disk: 19.3 GB of mined pages deleted, evidence pages kept

`data/raw/org_contacts` had grown to 19 GB: 5,023 cached organisation-website pages from the first
contact harvest. Every address they produced is already in `org_email_mine` with the page URL recorded,
so the bytes are a convenience, not the evidence.

| removed | kept | freed |
|---|---|---|
| 3,847 pages that yielded nothing | 1,176 pages cited as email evidence | **19.3 GB** |

Also deleted the 2.5 GB Common Crawl rank list (its candidates are extracted to JSON and were verified
already) and the downloaded 990 PDFs (their Part III text is in `org_program`).

**Disk 35 GB → 53 GB free.** The constraint stops ingest below 20 GB; this buys the next campaign room
without losing a single row or the page behind a single address.

## 2026-11-22 — the last route to the 438 orphaned filings is gone: the IRS retired the batches

Two attempts to recover the filings whose pre-2022 index rows carry no `XML_BATCH_ID`:

* **per-object fetch** — `https://s3.amazonaws.com/irs-form-990/<OBJECT_ID>_public.xml` for a real
  object id out of `index_2021.csv` (`202101629349200830`): **HTTP 404**. The bucket listing was already
  empty; the objects are gone too.
* **whole-year archives** — probing every batch name for 2021 (`01A` … `12B`, 24 requests):

  ```
  01A: 206 (3.5 GB)     01B..12B: 302 redirect — retired
  ```

  One batch survives out of twenty-four. Even a full 3.5 GB download would recover only the handful of
  our 159 filings that happen to sit in it.

Three separate failures on this source (empty listing, 404 objects, retired batches) meet the
objective's own stop condition for one dataset. **Part III stays at 2,942**; the 438 orphaned filings
are not recoverable from the IRS, and ProPublica's copies of the same years answer 403.

## 2026-11-22 — the two remaining gaps and the exact stop condition for each

**Item 3, websites: 4,647 of 5,000.** Blocked on "a required key with no keyless equivalent", which the
objective names as a stop condition. Ten sources were measured to exhaustion (`Sources tried for the last
380 websites`), the last two being registries keyed by EIN rather than guessed from a name. The key is
free, and SAM.gov refuses anonymous use — the endpoint answers without one:

```
GET https://api.sam.gov/entity-information/v3/entities?fmt=json   ->  404  (api_key required)
```

**Item 4, Part III: 2,942 of 5,000.** Blocked on "3 consecutive unreachable-source failures for one
dataset", also a named stop condition:

1. the IRS S3 bucket lists nothing (`KeyCount 0`) and single objects 404;
2. the 2019–2021 indexes carry no batch id, and 23 of 24 batch archives for 2021 are retired (302);
3. ProPublica holds the filings but its download endpoint answers 403 to every honest request.

Both conditions are met and evidenced. No row was invented to close either gap.
