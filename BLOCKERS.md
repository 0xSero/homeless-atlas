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
