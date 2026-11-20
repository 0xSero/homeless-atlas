# Sources and licences

Every dataset this project uses, what it gives us, its terms, and the ingestor that fetches it.
Workers: append a row when you use a new source. Never delete a row.

## Counts (who and how many)

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| HUD Point-in-Time (PIT) count, by CoC, 2007– | total, sheltered, unsheltered, subpopulations | US Government work, public domain | hudexchange.info (PIT/HIC data since 2007), huduser.gov AHAR | `hud_pit`, `pit_sub` |
| HUD Housing Inventory Count (HIC) | beds/units by program type per CoC | public domain | hudexchange.info | `hic` |
| Census Population Estimates (vintage 2024, sub-county) | population denominators for rates | public domain | www2.census.gov/programs-surveys/popest/ | `places`, `hud_pit` |
| Census Gazetteer (2023) | place/county/cousub internal points, land area | public domain | www2.census.gov/geo/docs/maps-data/data/gazetteer/ | `places` |
| Census TIGER / cartographic boundaries, HUD CoC GIS | map polygons | public domain | census.gov, hudexchange.info | `coc_geometry` |
| HUD CoC boundary feature service (`CoC_Geo_Type`, FY2024) + Census 2023 cartographic state/place 500k, 2023 gazetteer, PEP sub-est2024 | CoC polygons, state polygons, place points/population for the city layer | US Government work, public domain; the ArcGIS layer sets no `licenseInfo`, HUD's no-warranty disclaimer applies | services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/CoC_Geo_Type/FeatureServer/0, www2.census.gov/geo/tiger/GENZ2023/shp/, www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/, www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv | `coc_geometry` |
| HUD USER AHAR Part 1 data files: HIC by CoC workbook `2007-2025-HIC-Counts-by-CoC.xlsx` (2,598,803 B, one sheet per year) and the project-level inventory file `2025-HIC-Counts-by-State.csv` (11,334,278 B, 30,012 project-inventory rows) | year-round beds per program type (ES, TH, SH, RRH, PSH, OPH, demonstration), DV beds, dedicated youth/veteran/chronic beds, ES seasonal and overflow beds, family units, project counts | US Government work, public domain (HUD USER may serve these files to a browser user-agent only) | https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-HIC-Counts-by-CoC.xlsx, https://www.huduser.gov/portal/sites/default/files/xls/2025-HIC-Counts-by-State.csv (index page https://www.huduser.gov/portal/datasets/ahar/2025-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html) | `hic` |
| United Kingdom (four administrations): MHCLG rough sleeping snapshot autumn 2025 tables; MHCLG statutory homelessness England-level time series + January–March 2026 detailed LA tables; ONS LAD April 2025 names and codes; Scottish Government *Homelessness in Scotland 2024-25* main tables (HL1/HL2); StatsWales rough sleepers, homeless individuals temporarily accommodated, and Section 75 outcome datasets; DfC Northern Ireland Homelessness Bulletin Oct 2025–Mar 2026 tables | people sleeping rough per English LA 2010–2025 and the England total; households owed a prevention/relief duty and households in temporary accommodation (England, per LA for 2026 Q1, national series 1998–2026); per-council Scottish applications/assessments/households in TA 2020-21 to 2024-25; per-unitary-authority Welsh rough sleeper counts, individuals in TA and s75 acceptances; per-district NI presentations, acceptances and TA placements; the ONS local authority code list that `geo_city(kind='uk_la', geoid='UK-<ONS code>')` is built from | Open Government Licence v3.0 (all six publishers); ONS Open Geography Portal services are OGL; StatsWales data are OGL | https://www.gov.uk/government/statistical-data-sets/tables-on-rough-sleeping, https://www.gov.uk/government/statistical-data-sets/live-tables-on-homelessness, https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/LAD_APR_2025_UK_NC_v2/FeatureServer/0/query, https://www.gov.scot/publications/homelessness-in-scotland-2024-25/, https://stats.gov.wales/en-GB/topic/62/homelessness (CSV via `POST /en-GB/<dataset-id>/download`), https://www.communities-ni.gov.uk/articles/northern-ireland-homelessness-bulletin | `uk` |

| Australia — ABS *Estimating homelessness: Census, 2021* (cat. no. 2049.0), data cube 1 (tables 1.1, 1.3, 1.5: homeless persons and rate per 10,000 by state/territory of **usual residence**, 2006/2011/2016/2021) and data cube 6 (tables 6.1, 6.2: homeless operational groups and 'All homeless persons' by **place of enumeration**, Local Government Area, 2021 and 2016) | people experiencing homelessness per state/territory for four Censuses and per LGA for two; the ABS-published rate per 10,000 | Creative Commons Attribution 4.0 International (ABS copyright and Creative Commons statement; Coat of Arms, ABS logo and Census branding excepted) | https://www.abs.gov.au/statistics/people/housing/estimating-homelessness-census/2021 (cubes `20490do001_2021.xlsx`, `20490do006_2021.xlsx`; 8 cubes published, these two used) | `australia` |
| Australia — Productivity Commission, *Report on Government Services 2026*, Part G Housing and homelessness, Section 19 Homelessness services: machine-readable dataset (tables 19A.1, 19A.11, 19A.21) | SHS clients per jurisdiction and year (AIHW Specialist Homelessness Services Collection), recurrent government expenditure on homelessness services in real 2024-25 dollars, expenditure per person and cost per client (the NASHH homelessness component), and average daily unassisted requests for accommodation | Creative Commons Attribution 4.0 International (Productivity Commission copyright statement; attribute as 'Source: Productivity Commission') | https://assets.pc.gov.au/2026-01/rogs-2026-partg-section19-homelessness-services-dataset.csv?VersionId=oou7gCfLtIUjLTYmqVgZgevEBezJo44g (chapter page https://www.pc.gov.au/ongoing/report-on-government-services/housing-homelessness/homelessness-services) | `australia` |
| Australia — AIHW *Specialist Homelessness Services annual report* data tables (`www.aihw.gov.au`, incl. `/getmedia/…` binary paths) | **not usable**: every path answers a Cloudflare "Just a moment…" JS challenge (403) to this host, so the SHS client counts by LGA/SA4/PHN, agency/bed tables and demographic breakdowns are unreachable — see BLOCKERS.md | n/a — blocked at the publisher; RoGS s19 is used as the keyless SHSC-derived substitute | https://www.aihw.gov.au/reports/homelessness-services/shs-annual-report | `australia` (documented blocker) |

## Money (who gets paid)

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| USASpending.gov API v2 | every federal contract, grant, cooperative agreement, IDV: recipient, amount, agency, CFDA, place | US Government work, public domain; API is free and keyless | api.usaspending.gov | `usaspending` |
| SAM.gov Entity Management API | UEI → registration, address, NAICS, officers (needs `DATA_GOV_API_KEY`) | public domain, rate-limited by key | api.sam.gov | — (blocked, see BLOCKERS.md) |
| City open-data portals (Socrata) | local homelessness contracts and budgets | varies per portal, usually open | e.g. data.sfgov.org | `sf`, `budgets` |
| State budget documents | state homelessness line items | public record | state legislature / budget sites | `budgets` |

## Organizations and people

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| IRS Form 990 (via ProPublica Nonprofit Explorer) | EIN, financials, officers, titles, compensation, website | IRS filings are public records; Nonprofit Explorer API free for non-commercial use, attribute ProPublica | projects.propublica.org/nonprofits/api/v2 | `nonprofit` |
| IRS Exempt Organizations Business Master File | the full universe of registered nonprofits by NTEE | public domain | irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract | `nonprofit` |
| Organization websites | published general contact email/phone | the organisation's own site; robots.txt respected, 1 req/s | — | `org_contacts` |

## Institutions

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| Wikidata (SPARQL) | psychiatric hospitals, prisons, opening/closing dates, capacity, coordinates | CC0 | query.wikidata.org/sparql | `facility` |
| Wikipedia / MediaWiki API | institution lists, historical detail | CC BY-SA 4.0 — attributed by linking to the article | en.wikipedia.org/w/api.php | `facility` |
| BJS correctional facility census | prison/jail capacity and custody | public domain | bjs.ojp.gov | `facility` |
| State health/hospital agencies | licensed beds, budgets, staff | public record | per state | `facility` |
| Treatment Advocacy Center / NASMHPD reports | per-patient state hospital costs | cited, not reproduced in bulk | — | `facility` |

## Health, crime, harm

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| SAMHSA NSDUH state estimates | serious mental illness and substance use prevalence by state | public domain | samhsa.gov/data | `health` |
| CDC WONDER / NCHS | overdose deaths by state and county | public domain | wonder.cdc.gov | `health` |
| FBI Crime Data Explorer | area-level crime counts and rates | public domain; needs api.usa.gov key (`DEMO_KEY` works at low rate) | api.usa.gov/crime/fbi/cde | `crime` |
| City police/open-data portals | incident-level and 311 data for large cities | varies, usually open | Socrata | `crime` |
| NCHS Drug Poisoning Mortality by State | drug-overdose deaths and age-adjusted rate by state, 1999–2016 (final) | Public Domain U.S. Government | data.cdc.gov/resource/xbxb-epbu | `health` |
| NCHS VSRR Provisional Drug Overdose Death Counts | provisional 12-month-ending overdose deaths by state, 2015–now | Public Domain U.S. Government | data.cdc.gov/resource/xkb8-kh2a | `health` |
| NCHS VSRR Provisional County-Level Drug Overdose Death Counts | provisional 12-month-ending overdose deaths by county, 2020–now | Public Domain U.S. Government | data.cdc.gov/resource/gb4e-yj24 | `health` |
| CDC BRFSS Prevalence Data | state adult prevalence (depression, binge drinking) | Public Domain U.S. Government | data.cdc.gov/resource/dttw-5yxu | `health` |
| SF DPH / OCME via DataSF | preliminary unintentional overdose deaths by month, San Francisco | DataSF open data portal; no licence field in dataset metadata | data.sfgov.org/resource/jxrr-bmra | `health` |
| CT Office of the Chief Medical Examiner via data.ct.gov | row-level accidental drug-related deaths with town of residence | Public Domain (dataset metadata) | data.ct.gov/resource/rybz-nyjw | `health` |

## Law

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| State legislature sites | statute text and citations | public record | per state | `laws` |
| NLIHC state reports, NCH civil commitment report | curated summaries with citations | cited, not copied | nlihc.org, treatmentadvocacycenter.org | `laws` |
| CourtListener / Court records | injunctions, appellate decisions (Martin v. Boise, Grants Pass v. Johnson) | public record | courtlistener.com API | `laws` |

## Media

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| YouTube (via `yt-dlp` search) | public videos about a place, with uploader and duration | metadata only, links out; no rehosting | yt-dlp ytsearch | `media` |
| Wikimedia Commons API | openly licensed photographs | per-file licence read from `imageinfo` extmetadata | commons.wikimedia.org/w/api.php | `media` |

## Rules

1. Public data only. No scraping behind a login, a paywall or a CAPTCHA.
2. One request per second per worker; cache every response under `data/raw/<domain>/`.
3. Record URL, retrieval time, byte size and SHA-256 in the `source` table for every source used.
4. Organisation-level contact information only. Published professional roles (Form 990 officers) are fine;
   home addresses, personal mobile numbers and personal social accounts are not.
5. Attribution stays on the page: the site links every figure to the `source` row it came from.

## hud_pit — exact endpoints used (verified live 2026-11-19, HTTP 200)

Appended by the `hud_pit` worker. The generic rows above name these families; below are the
concrete URLs that actually serve the files today, plus two dead ends, so the next worker does not
have to rediscover them. All are US Government works / public domain.

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| HUD 2007–2025 PIT Estimates by CoC (xlsb, one sheet per year) | `pit.*` for 19 years × ~390 CoCs | US Gov work, public domain | https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-PIT-Counts-by-CoC.xlsb (index: https://www.huduser.gov/portal/datasets/ahar/2024-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html) | `hud_pit` |
| HUD CoC Grantee Areas feature service (387 CoC polygons + `COCNUM`/`COCNAME`/`STUSAB`) | `geo_coc.geometry`, CoC universe, city→CoC assignment | US Gov work, public domain | https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/Continuum_of_Care_Grantee_Areas/FeatureServer/0/query (found via https://hudgis-hud.opendata.arcgis.com/api/search/v1/collections/all/items?q=Continuum%20of%20Care) | `hud_pit` |
| Census vintage-2024 sub-county estimates (`sub-est2024.csv`, SUMLEV 040/050/162) | state, county and place populations (2024) | public domain | https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv | `places`, `hud_pit` |
| Census 2024 Gazetteer (`2024_Gaz_state/counties/place_national.zip`) | internal points (lat/lon) and land area for states, counties, places | public domain | https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_{state,counties,place}_national.zip | `hud_pit` |
| HUD CoC Homeless Populations and Subpopulations reports, national scope (PDF, 2007–2025) | national totals incl. SMI / chronic substance abuse / HIV / DV / households — the only place HUD publishes those aggregates | US Gov work, public domain | https://files.hudexchange.info/reports/published/CoC_PopSub_NatlTerrDC_{year}.pdf (index: https://www.hudexchange.info/programs/coc/coc-homeless-populations-and-subpopulations-reports/) | `hud_pit` |

Dead ends (do not retry):
* `https://www.hudexchange.info/resource/3031/pit-and-hic-data-since-2007/` and `/resource/5713/coc-gis-toolkit/` → 404; the CoC spreadsheets now live on huduser.gov under `/portal/sites/default/files/xls/`.
* `hudexchange.info` serves **404 to the default ingest User-Agent** — send a browser UA.
* `api.census.gov` returns a *Missing Key* page for keyless ACS queries, so tract-level population
  is not obtainable without a key (no tract allocation was attempted in `hud_pit`).

## coc_geometry — exact endpoints used (verified live 2026-11-19, HTTP 200)

Appended by the `coc_geometry` worker. All five are US Government works / public domain; none needs a
key. Every response is cached under `data/raw/coc_geometry/` and registered in `source`.

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| HUD CoC boundary polygons, FY2024 (`CoC_Geo_type`) | `geo_coc.geometry`, `lat`/`lon`, `data/export/coc.geojson` | US Gov work, public domain (layer `licenseInfo` is null; HUD `copyrightText` applies) | https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/CoC_Geo_Type/FeatureServer/0/query (16 pages of 25, `f=geojson&geometryPrecision=5&maxAllowableOffset=0.0005&orderByFields=OBJECTID_1`; count via `&returnCountOnly=true` -> 387) | `coc_geometry` |
| Census 2023 cartographic boundary, state (500k) | `geo_state`, `data/export/state.geojson` | public domain | https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip (3,249,332 B, 56 features) | `coc_geometry` |
| Census 2023 cartographic boundary, place (500k) | `geo_city` (kind='place') attributes | public domain | https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_place_500k.zip (22,856,787 B, no centroid fields) | `coc_geometry` |
| Census 2023 Gazetteer, national places | `geo_city.lat/lon` (INTPTLAT/INTPTLONG) | public domain | https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_place_national.zip (1,220,953 B) | `coc_geometry` |
| Census PEP vintage 2024, sub-county totals | `geo_state.pop`, `geo_city.pop` (SUMLEV 040 / 162 / 170) | public domain | https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv (7,087,080 B) | `coc_geometry` |

Notes: the `CoC_Geo_Type` layer publishes boundaries for **387** CoCs only; CoC numbers that HUD lists
elsewhere but has no FY2024 polygon for (`where=COCNUM='NY-516'` etc.) return `features: []`, so their
`geo_coc.geometry` is NULL by construction, not by failure. Simplification is pure-python
Douglas-Peucker at 0.002 deg (~220 m); no shapely/GEOS/pyshp dependency anywhere.

### facility domain — sources added by the facility ingestor

| Source | Used for | License | Endpoint | Domain |
|---|---|---|---|---|
| HIFLD Hospitals (FeatureServer) | current psychiatric-hospital inventory: name, address, city/state, bed count, owner, operating status, coordinates, per-record data vintage | US Government open data (public domain) | `services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Hospitals/FeatureServer/0` | `facility` |
| HIFLD Prisons (FeatureServer) | current prison/jail inventory: capacity, custody count, security level, facility type, operating status, coordinates, per-record data vintage | US Government open data (public domain) | `services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/prison/FeatureServer/0` | `facility` |
| California Legislative Analyst's Office — *2020-21 Budget: Analysis of the Department of State Hospitals Budget* | DSH operational expenditures FY2020-21 ($2.3 B) used to derive cost per state-hospital bed-year | public record, CA state government | <https://lao.ca.gov/Publications/Report/4154> | `facility` |
| California Legislative Analyst's Office — *State Correctional Spending Increased Despite Significant Population Reductions* | CDCR expenditures FY2019-20 ($13.3 B) and June 2019 inmate population (125,500) used to derive cost per inmate-year | public record, CA state government | <https://lao.ca.gov/Publications/Report/4145> | `facility` |
| Treatment Advocacy Center — *Going, Going, Gone* and *Prevention Over Punishment* (state hospital bed data, 2016–2024) | reference only: cross-checking state psychiatric bed counts and the direction of bed trends; the per-state PDF pages are image-based and were not machine-extracted | TAC publication terms (cited, not redistributed) | <https://www.tac.org/wp-content/uploads/2023/11/Going-Going-Gone.pdf> | `facility` |

Sources attempted and rejected for the facility domain (recorded so nobody re-tries blindly):
`asylumprojects.org` (HTTP 403 to non-browser clients), SAMHSA N-MHSS/N-SUMHSS public-use files
(HTTP 403), `data.cms.gov` HCRIS (HTTP 403, Akamai), `catalog.data.gov` CKAN (404),
`nri-inc.org` (robots.txt asserts `ai-input`/`ai-train` content-signal reservations).

## laws — exact endpoints used (verified live 2026-11-19, HTTP 200)

Appended by the `laws` worker. The three rows in the Law table above are the generic
description; these are the specific documents whose text the `law` rows are built from
(every response cached under `data/raw/laws/`, 1 req/s).

| Source | Gives | Licence / terms | Endpoint | Ingestor |
|---|---|---|---|---|
| Treatment Advocacy Center state pages (50 states + DC) | each state's emergency-evaluation, inpatient-commitment and outpatient/AOT statute text with section numbers, plus TAC's recommended amendments | statutory text is public record; pages cited and linked, not reproduced in bulk | https://www.tac.org/map_directory/<state>/ (e.g. `/alabama/`, `/district-of-columbia/`) | `laws` |
| National Homelessness Law Center, *Housing Not Handcuffs 2021: State Law Supplement* | 51 jurisdictions x 12 statewide restriction categories (sleeping/camping, sit-lie, vehicle lodging, loitering/vagrancy, roadway, panhandling) | public advocacy report, cited and linked | https://homelesslaw.org/wp-content/uploads/2022/02/2021-HNH-State-Crim-Supplement.pdf | `laws` |
| National Homelessness Law Center, *Housing Not Handcuffs* (2019), Appendix A | 187 surveyed cities x 11 municipal ordinance categories (2016 chart) | public advocacy report, cited and linked | https://homelesslaw.org/wp-content/uploads/2018/10/Housing-Not-Handcuffs.pdf | `laws` |
| National Homelessness Law Center / Housing Not Handcuffs, *One year since Grants Pass* (May 2025) | 2024-25 statewide bill status, CA Housing First mandate + AB 20, federal Housing Not Handcuffs Act | public advocacy report, quoted with attribution | https://housingnothandcuffs.org/wp-content/uploads/2025/06/Cicero-Homeless-Criminalization-Summary-May-2025.pdf | `laws` |
| NLIHC, *Successfully Fighting Against State and Local Homelessness Criminalization Bills* | named state bills with numbers (GA SB 62, KS HB 2430, MO HB 1606, MO SB 1336, IN/IA bills) and the states where criminalization bills failed | public advocacy report, quoted with attribution | https://nlihc.org/sites/default/files/Successfully-Fighting-Against-State-and-Local-Homelessness-Criminalization-Bills.pdf | `laws` |
| NLIHC Housing Trust Fund Project page | the state legislation that enables local housing trust funds, with statutes (A.R.S. 11-381, RCW 84.52.105, Act 137, SB1533, H3509, P.L. 2009 ch. 123, ...) | public research page, quoted with attribution | https://nlihc.org/housing-trust-fund-project | `laws` |
| Cornell LII — U.S. Supreme Court + CFR + U.S. Code | *City of Grants Pass v. Johnson* (No. 23-175) opinion text; 24 CFR 578.7 (CoC written standards); 42 U.S.C. § 11302 (definition of homeless individual) | public record; LII terms permit linking | https://www.law.cornell.edu/supremecourt/text/23-175, /cfr/text/24/578.7, /uscode/text/42/11302 | `laws` |
| NLIHC "Housing First" issue page | the adoption of Housing First by federal agencies and by state/local officials (context row; names no state statutes) | public advocacy page, quoted with attribution | https://nlihc.org/explore-issues/housing-programs/housing-first | `laws` |
| eCFR versioner API | used as a *check*, not a source of rows: verifies that 24 CFR Part 578 contains no "Housing First" language | US Government work, public domain | https://www.ecfr.gov/api/versioner/v1/full/<date>/title-24.xml?part=578 | `laws` |
| ProPublica Nonprofit Explorer — search API v2 + filing pages | nonprofit name sweeps (25 orgs/page); per fiscal year total revenue, expenses, assets; Form 990 Part VII Section A officers/directors/key employees with titles and reportable compensation | public data (IRS BMF + Form 990 extracts); free API, no key, 1 req/s honoured; attribute ProPublica | projects.propublica.org/nonprofits/api/v2/search.json, projects.propublica.org/nonprofits/organizations/{ein} | `nonprofit` |
| Wikidata Query Service — EIN join | official website (P856) and phone (P1329) for nonprofits, joined on IRS EIN (P1297); gap-fill only, never overwritten | CC0 | query.wikidata.org/sparql | `nonprofit` |
| ProPublica Form 990 XML + filing PDF endpoints | **not usable**: `download-xml?object_id=…` answers HTTP 403 "Security Check" and `display_990/…` sits behind a Cloudflare interstitial, so 990 XML and paper PDFs are unreachable from this harness | n/a — blocked at the publisher | projects.propublica.org/nonprofits/download-xml, /nonprofits/display_990/… | `nonprofit` (documented blocker) |
| Canada — HICC "Everyone Counts" nationally coordinated Point-in-Time Counts (2016, 2018, 2020-2022, 2024 and 2025 national reports; Annex A community list of the 2024 report; the community-level enumeration results table HICC publishes on the Homelessness Learning Hub, reachable only through its Internet Archive snapshot because the Learning Hub 403s this host) | national PiT enumeration per cycle (total, shelters, unsheltered, transitional, encampments, institutional), and per-community 2024 enumeration (unsheltered / sheltered / transitional) for 39 communities | Open Government Licence - Canada for the HICC-hosted pages; the Learning Hub page is a Government of Canada product, read via the Internet Archive snapshot (web.archive.org) | housing-infrastructure.canada.ca/homelessness-sans-abri/reports-rapports/ (pit-counts-dp-eng, pit-counts-dp-2018-highlights-eng, pit-counts-dp-2020-2022-results-resultats-eng, pit-counts-dp-2024-highlights-p1-eng, everyone-counts-2025-tout-monde-compte-eng, alt-format/pdf/…/pit-counts-dp-2024-highlights-p1-en.pdf), web.archive.org/web/2026/https://homelessnesslearninghub.ca/library/resources/everyone-counts-2024-results/ | `canada` |
| Canada — Homelessness Services Association of BC / BC Housing / Ministry of Housing, *Report on Homeless Counts in B.C.* (2020/21, 2023, 2024-2025 editions, incl. the 2018 per-community column of the 2020/21 report; tables 3.1, 2.2, 3) | sheltered / unsheltered / total PiT count per BC count area, 2018, 2020/21, 2023, 2024/25 — 33 count areas, the only Canadian jurisdiction publishing per-community PiT results as a table | Public provincial statistics reports; the publisher states no explicit licence (recorded as such rather than guessed) | hsa-bc.ca/_Library/2020_HC/2020-21-BC-Homeless-Counts.pdf, _Library/2023_HC/2023-BC-Homeless-Counts.pdf, _Library/2024_25_HC/2024-2025_Report_on_Homeless_Counts_in_BC.pdf | `canada` |
| Canada — Statistics Canada table 98-10-0252-01, "Shelter-cost-to-income ratio by tenure: Canada, provinces and territories, census metropolitan areas and census agglomerations" (2021 Census), full-table CSV zip | private households by shelter-cost-to-income ratio: Canada and 13 provinces/territories, plus the derived 30%-or-more share | Open Government Licence - Canada (Statistics Canada open licence) | www150.statcan.gc.ca/n1/tbl/csv/98100252-eng.zip | `canada` |
| Canada — HICC Reaching Home programme information page | the one published federal dollar figure for a homelessness initiative reachable in bulk (Budget 2022 Action Research on Chronic Homelessness, $18.1 M over three years); per-community Reaching Home allocations are **not** published in bulk — see BLOCKERS.md | Open Government Licence - Canada | housing-infrastructure.canada.ca/homelessness-sans-abri/prog-info-eng.html | `canada` |
| San Francisco (City and County) — SF Controller **Budget** (`xdgd-c79v`) and **Budget - FTE** (`4zfx-f2ts`), SF Controller **Supplier Contracts** (`cqi5-hm2d`), **311 Cases** (`vw6y-z8j6`), **Law Enforcement Dispatched Calls for Service: Closed** (`2zdj-bwza`), SF OCME **Preliminary Unintentional Drug Overdose Deaths** (`jxrr-bmra`), SF Public Library **Ballot Propositions Database** (`88s2-6ua9`) on DataSF; **San Francisco Sheriff's Office 2025 Annual Report** (PDF); Wikipedia infobox/article text for Laguna Honda and SF General licensed beds | every HSH/DHS appropriation line FY2017–FY2028 (department total, *SR Our City Our Home Fund* Prop C money, General Fund, grants, program split to FY2018, FTE), 1,118 executed homelessness contracts with vendor/amount/term, encampment 311 volume 2008–2026, SFPD *HOMELESS COMPLAINT* calls 2015–2026, SF overdose deaths 2020–2026, per-jail capacity and average population, Jail Health Services mental-health volumes, jail and hospital bed counts | DataSF datasets: Open Data Commons PDDL 1.0 (OCME dataset declares no licence, recorded as none); Sheriff annual report: public record; Wikipedia: CC BY-SA 4.0 | `data.sf.gov/resource/{xdgd-c79v,4zfx-f2ts,cqi5-hm2d,vw6y-z8j6,2zdj-bwza,jxrr-bmra,88s2-6ua9}.json` (host `data.sfgov.org` now 301-redirects to `data.sf.gov`), `sfsheriff.com/sites/default/files/2026-02/ANNUAL REPORT 2025 UPDATED_compressed.pdf`, `en.wikipedia.org/w/index.php?action=raw` | `sf` |

### org-contacts domain — sources added by the `org_contacts` ingestor

| Source | Used for | Licence | Endpoint | Domain |
|---|---|---|---|---|
| IRS Form 990 e-file XML, TEOS bulk batches (`<WebsiteAddressTxt>` + filer `<PhoneNum>`) | the website address each organisation published on its own annual return, and the 990 filer phone used only when the site crawl found none | US Government work — public domain | `apps.irs.gov/pub/epostcard/990/xml/<year>/<year>_TEOS_XML_<NN>A.zip` (12 batches per filing year, ~17k-84k filings per batch) | `org_contacts` |
| The organisations' own websites (`robots.txt`, homepage, one linked contact page) | verification that the address belongs to the org (HTTP 200 + name match), and the general `mailto:`/`tel:` the org publishes | published by each organisation; cited and linked, not reproduced | each org's own host, e.g. `https://www.pinestreetinn.org`, `https://www.pinestreetinn.org/robots.txt` | `org_contacts` |
| Wikidata P856 (already on `org.website` from the `nonprofit` worker) | re-verified here so that every published website also gets a page-level provenance row | CC0 | `query.wikidata.org` (value re-checked against the live site) | `org_contacts` |
| SAM.gov Entity Management API | **not usable without a key**: would give the website of the ~10,000 UEI-only orgs (government, housing authorities, CoC lead agencies) that file no 990. See BLOCKERS.md | n/a — key required (`api.data.gov` key, <https://api.data.gov/signup/>) | `api.sam.gov/entity-information/v3/entities` | `org_contacts` (documented blocker) |

### award-places domain — source added by the `award_places` ingestor

| Source | Used for | Licence | Endpoint | Domain |
|---|---|---|---|---|
| USASpending.gov **bulk award download** — `POST /api/v2/download/awards/` generating `PrimeAwardSummariesAndSubawards_*.zip` on `files.usaspending.gov`, polled via `/api/v2/download/status/`, requested with a `columns` subset; plus `GET /api/v2/awards/<generated_internal_id>/` for the five live verification rows | the **resolved** place of performance (`primary_place_of_performance_city_name` / `_county_name`, county FIPS, state, ZIP, `pop_scope`, country) and the recipient location (`recipient_address_line_1`, city, county, state, ZIP, UEI) for the awards already in the archive — the fields the award search endpoint returns only as USASpending-internal codes (`PA**043`), so that city pages can show money; six generated downloads cover the whole 187,120-row award table | US Government work, public domain; API is free and keyless | `api.usaspending.gov/api/v2/download/awards/`, `api.usaspending.gov/api/v2/download/status?file_name=…`, `files.usaspending.gov/generated_downloads/…zip`, `api.usaspending.gov/api/v2/awards/<generated_internal_id>/` | `award_places` |
