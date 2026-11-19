# hud_pit — HUD Point-in-Time counts by Continuum of Care

Domain owner: `ingest/domains/hud_pit.py` (`DOMAIN="hud_pit"`).
Tables written: `pit`, `geo_coc`, `geo_city`, `geo_state`, `metric`.

Run: `python3 -m ingest.run hud_pit` (idempotent; re-runs upsert on the natural key).
Extra dependency: `pyxlsb` (HUD ships the PIT workbook as `.xlsb`). Installed here from the
PyPI wheel into `~/.local/lib/python3.10/site-packages` because `pip` itself is broken in this
environment; a plain `pip install --user pyxlsb` is equivalent.

## Working download URLs (all re-verified live 2026-11-19, HTTP 200)

| What | URL | Notes |
|---|---|---|
| **PIT counts by CoC, 2007–2025** (primary) | `https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-PIT-Counts-by-CoC.xlsb` | 8.9 MB, one sheet per year, wide format. Found from the HUD USER AHAR data page (`https://www.huduser.gov/portal/datasets/ahar/2024-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html`, link text "Point-in-Time (PIT) and Housing Inventory Count (HIC) data files"). The 2007–2024 file is the same product one vintage older. |
| CoC geography | `https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/Continuum_of_Care_Grantee_Areas/FeatureServer/0/query` | HUD's hosted CoC grantee-area layer (387 CoCs, `COCNUM`/`COCNAME`/`STUSAB` + polygons). Discovered via `https://hudgis-hud.opendata.arcgis.com/api/search/v1/collections/all/items?q=Continuum%20of%20Care`. |
| Census population denominators | `https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv` | Vintage-2024 sub-county estimates: state (`SUMLEV=040`), county (`050`), whole place (`162`). |
| Census gazetteer (lat/lon) | `https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_{state,counties,place}_national.zip` | Internal points + land area. |
| National subpopulation reports | `https://files.hudexchange.info/reports/published/CoC_PopSub_NatlTerrDC_{year}.pdf` (2007–2025) | National totals only (see below). Index page: `https://www.hudexchange.info/programs/coc/coc-homeless-populations-and-subpopulations-reports/`. |

Dead ends worth recording (so nobody repeats them):
* `hudexchange.info/resource/3031/pit-and-hic-data-since-2007/` and `/resource/5713/coc-gis-toolkit/`
  now 404 — the CoC Excel files moved to HUD USER (`/portal/sites/default/files/xls/…`).
* `hudexchange.info` returns **404 for the default ingest User-Agent**; every request in this
  domain sends a browser UA (same public files, just not blocked by the edge rule).
* `api.census.gov` now returns a *Missing Key* page for keyless ACS queries, so no tract-level
  population was used (see "CoC population" below).
* HUD's "PIT Counts by State" companion workbook exists (`2007-2025-PIT-Counts-by-State.xlsb`) but
  was not needed: state totals are recomputed from the CoC file and cross-checked instead.

## Verified

Five CoC-year values re-read **at cell level from the downloaded workbook**
(`data/raw/hud_pit/2007-2025-PIT-Counts-by-CoC.xlsb`, sheet = year, row numbers 1-based) and
compared to the rows this ingestor wrote into `pit`:

| # | CoC | Year | Sheet row | Field | File value | `pit` row | Match |
|---|---|---|---|---|---|---|---|
| 1 | NY-600 New York City CoC | 2025 | 273 | overall / sheltered / unsheltered | 125,683 / 121,010 / 4,673 | 125683 / 121010 / 4673 | ✔ |
|   |  |  |  | chronic / veterans / youth<25 / children<18 / families | 3,605 / 697 / 5,233 / 40,586 / 75,735 | identical | ✔ |
| 2 | CA-600 Los Angeles City & County CoC | 2024 | 52 | overall / sheltered / unsheltered | 71,201 / 21,692 / 49,509 | 71201 / 21692 / 49509 | ✔ |
| 3 | TX-607 Texas Balance of State CoC | 2020 | 352 | overall / sheltered / unsheltered / chronic | 9,198 / 3,433 / 5,765 / 1,178 | 9198 / 3433 / 5765 / 1178 | ✔ |
| 4 | WA-500 Seattle/King County CoC | 2015 | 382 | overall / sheltered / unsheltered | 10,122 / 6,319 / 3,803 | 10122 / 6319 / 3803 | ✔ |
| 5 | AK-500 Anchorage CoC | 2007 | 2 | overall / sheltered / unsheltered / chronic (individuals) | 974 / 842 / 132 / 224 | 974 / 842 / 132 / 224 | ✔ (veterans/youth/children NULL — not in the 2007 workbook) |

Whole-table checks that also passed:

* `total = sheltered + unsheltered` for **all 7,431** `pit` rows (0 exceptions), and no row has a
  NULL in those three columns.
* Summing `pit.total` by year reproduces HUD's own published national total
  (`metric` `pit_total_hud_published`, parsed from the national PopSub PDF) **exactly for 16 of 19
  years**; 2013 −1,404, 2017 −2,746, 2021 −27 (the CoC workbook and HUD's PDF disagree slightly in
  those years — HUD data revisions; the workbook value is what `pit` holds).
  2025: 745,652 = 745,652 ✔. 2013: 590,364 vs 591,768 (PDF) — see note above.

## Coverage

**Years: 2007–2025 (19 years, every year HUD has published).** Workbook sheet years verified:
2007, 2008, …, 2025 — no gaps.

**CoC-years: 7,431.**

| year | CoCs | year | CoCs | year | CoCs |
|---|---|---|---|---|---|
| 2007 | 394 | 2014 | 391 | 2021 | 387 |
| 2008 | 390 | 2015 | 395 | 2022 | 387 |
| 2009 | 391 | 2016 | 394 | 2023 | 385 |
| 2010 | 391 | 2017 | 395 | 2024 | 385 |
| 2011 | 392 | 2018 | 396 | 2025 | 386 |
| 2012 | 391 | 2019 | 397 | | |
| 2013 | 392 | 2020 | 392 | | |

**CoCs: 413 distinct CoCs with at least one PIT row**; `geo_coc` holds 414 rows:

* 387 CoCs exist in HUD's current CoC geography layer (FY2024/final CoC boundaries).
* 27 are historical CoCs that no longer have a published boundary (mostly Maryland, Massachusetts
  and Arkansas CoCs consolidated into a Balance-of-State / city CoC): AR-502, AR-504, AR-506,
  AR-509, AR-510, AR-512, CA-528, MA-501, MA-508, MA-510, MA-514, MA-517, MA-519, MD-500, MD-502,
  MD-507, MD-508, MD-509, MD-510, MD-512, MI-513, NY-504, NY-506, NY-516, NY-607, NY-609, TX-625.
  They keep their `pit` history but have `geometry = NULL` and `pop = NULL`.
* AS-500 (American Samoa) is in the CoC universe but has **no PIT count in any year** of the
  workbook — it has a `geo_coc` row and no `pit` rows.
* CoCs dropped/added across years are real HUD universe changes (e.g. 27 CoCs present in 2019 have
  no 2025 row; AL-508, CO-505, MD-514, OR-504, TX-625 appear only later).

**Cities: the 1,000 largest US places** (Census vintage-2024 place estimates, whole-place rows),
each with population, internal-point lat/lon and `coc_num` (1,000/1,000 assigned).
`geo_city` also holds rows written by the concurrent `places` domain; this domain upserts its
1,000 rows on `geoid` and leaves the rest untouched.

**States: 52 rows** (50 states + DC + PR from the gazetteer), 51 with a 2024 population.
Territories other than PR (GU, VI, AS, MP) exist in `geo_state` from the gazetteer with
`pop = NULL` because the vintage-2024 estimates file covers only the 50 states + DC.

### What is missing, and why

1. **`pit.smi`, `pit.sud`, `pit.hiv`, `pit.dv` are NULL for every CoC-year — HUD does not publish
   these per CoC in any machine-readable file.** They appear only as *aggregates* in the
   "CoC Homeless Populations and Subpopulations" reports (national + state PDFs, 2007–2025;
   a per-CoC scoped PDF exists behind the report builder's JS form, but no stable direct file URL
   could be verified, and harvesting ~390 CoCs × 19 years ≈ 7,400 PDFs was out of scope for this
   run). The **national** per-year totals *are* ingested: `metric` `pit_smi`, `pit_sud`, `pit_hiv`,
   `pit_dv` (entity_type `us`, 2007–2025, source = the PDF). Anyone extending this domain should
   parse the per-CoC PDFs and fill `pit.smi/sud/hiv/dv` with `is_est=0`.
2. **`pit.households` is NULL — the CoC workbook publishes only *family* households**, not all
   homeless households. Writing family households into `households` would conflate ~71k with ~563k,
   so the published series lives in `metric` `pit_family_households` (per CoC sum, `is_est=0`) and
   the national all-household total is `metric` `pit_households` (from the PDFs, 2007–2025).
3. **`geo_coc.pop` is NULL for 56 of the 387 current CoCs.** HUD publishes no CoC population, so
   population is the sum of **county** populations whose Census internal point falls inside the CoC
   polygon. That is exact for CoCs that are whole counties or whole states, but a CoC that splits a
   county with a sibling CoC (Chicago vs Cook County, Atlanta vs Fulton/DeKalb, Detroit, Indianapolis,
   Albuquerque, Raleigh/Wake, Madison/Dane, every Puerto Rico and territory CoC …) gets `NULL`
   instead of a modelled share. 331 CoCs do have a population and they sum to 340,110,988, i.e. the
   whole US population is accounted for exactly once.
4. **No sub-county (tract) allocation.** The obvious fix — allocate county population to CoCs by
   tract — needs tract population, and `api.census.gov` now rejects keyless requests
   (`/data/missing_key.html`); the remaining keyless options (per-state PL 94-171 files, TIGER_DP
   shapefiles) were judged too heavy for this pass and would in any case be a *model*, not a
   published number.
5. `pit` has **no rows for CoC-years HUD never published** (e.g. AS-500, and any CoC before it
   existed). Nothing is interpolated anywhere in `pit`; the only `is_est=1` rows in this domain are
   `metric` rows of `entity_type='city'` (see below).

## Column mapping (only what the workbook really contains)

| `pit` column | Workbook header used | Years present |
|---|---|---|
| `total` | `Overall Homeless` | 2007–2025 |
| `sheltered` | `Sheltered Total Homeless` | 2007–2025 |
| `unsheltered` | `Unsheltered Homeless` | 2007–2025 |
| `chronic` | `Overall Chronically Homeless` (2011+); `Overall Chronically Homeless Individuals` (2007–2010, individuals only) | 2007–2025 |
| `veterans` | `Overall Homeless Veterans` | **2011–2025** (HUD did not publish 2010 veterans in the PIT report at all — the 2010 PDF says it would appear in the AHAR veterans supplement) |
| `youth` | `Overall Homeless Unaccompanied Youth (Under 25)` | **2015–2025** |
| `children` | `Overall Homeless - Under 18` | **2013–2025** |
| `families` | `Overall Homeless People in Families` | 2007–2025 |
| `households`, `smi`, `sud`, `hiv`, `dv` | — | never per CoC → NULL (see above) |

Footnote rows in the workbook are handled explicitly: HUD's cross-state CoC
`MO-604a` ("the sum of the point-in-time estimates the CoC separately reported for the portions of
its territory in MO and in KS", with no plain `MO-604` row) is normalised to `MO-604`, and the
`Total` label row plus blank rows are skipped. Without that, 2025 would come out 2,654 persons
short of HUD's published national total.

## Method notes

* **PIT parse**: `pyxlsb`, one sheet per year, header-name lookup (HUD renames and reorders columns
  every few years, and the 2025 sheet has 1,019 columns).
* **CoC boundary fetch**: paged 40 features per request (`resultOffset`), `outSR=4326`,
  `geometryPrecision=3`, `maxAllowableOffset=0.005` (~550 m). Full resolution blows past a 40 MB
  transfer cap; simplified rings total 3.0 MB of `geo_coc.geometry` (stored as GeoJSON).
* **City/county → CoC**: numpy ray-casting point-in-polygon on the Census internal point. Census
  internal points for San Francisco, Boston and a handful of coastal counties sit in open water, so a
  nearest-vertex fallback (only within 0.25°) is used; that took county assignment from 3,210/3,222
  to 3,222/3,222 and places from 992/1,000 to 1,000/1,000.
* **geo_coc.lat/lon**: population-weighted centroid of the counties assigned to the CoC (falls back
  to the largest assigned city's internal point). `geo_coc.cities`: the three largest assigned cities
  by population; when a CoC has no top-1,000 city, the leading locality of the CoC name is used.
* **metric (us)**: `pit_*` series are sums of the ingested CoC rows (`is_est=0`, note explains the
  basis); `pit_total_hud_published`, `pit_households`, `pit_smi`, `pit_sud`, `pit_hiv`, `pit_dv`
  come from HUD's national PDFs (`pdftotext -layout`, last numeric column = "Total").
  A separate set of `pit_veterans` values also exists in the PDFs for 2007–2010 where the CoC
  workbook has nothing.
* **metric (city, newest year)**: `pit_total` / `pit_sheltered` / `pit_unsheltered` for each of the
  1,000 cities, **inherited from the city's CoC** with `is_est=1` and a note naming the CoC — the
  heat map needs a value per city and HUD does not count cities separately. 3,000 rows.
* **Concurrency**: the DB is shared with another ingest worker; `run()` retries on
  `sqlite3.OperationalError: database is locked`.

## Reproduction

```bash
cd /home/ser/projects/homeless-atlas
python3 -m ingest.run hud_pit
python3 -m ingest.domains.hud_pit --selftest    # row counts + random rows
```
