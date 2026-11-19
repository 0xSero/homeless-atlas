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
