# uk — United Kingdom: rough sleeping and statutory homelessness

Domain owner: `ingest/domains/uk.py` (`DOMAIN = "uk"`). One reader per administration, because
England, Scotland, Wales and Northern Ireland publish on four unrelated systems with different
definitions. Nothing here is merged into the US tables' semantics: UK rows are quarantined in
`geo_city(kind='uk_la')` and in `metric(entity_type='uk_la' | 'uk')`.

Run: `python3 -m ingest.run uk` → 6,686 rows in **2.1 s** (9 sources, ~10.5 MB of payloads).
Idempotent: a second run wrote the identical 6,325 `metric` rows and 361 `geo_city` rows
(upsert on `(entity_type, entity_id, metric, year)` / `geoid`). Selftest:
`python3 -m ingest.domains.uk --selftest`.

## What is stored

| metric | entity_type | entity_id | years | publisher / table |
|---|---|---|---|---|
| `rough_sleeping` | `uk_la` | `UK-<ONS code>` | 2010–2025 | MHCLG rough sleeping snapshot in England, autumn 2025, Table 1 (single-night count/estimate, all 296 English LADs) |
| `rough_sleeping` | `uk` | `England` | 2010–2025 | same table, published England row (`E92000001`) |
| `rough_sleeping` | `uk_la` / `uk` | `UK-W…` / `Wales` | 2023–2026 | StatsWales *Rough sleepers by local authority* (monthly snapshot of individuals; latest month of each year kept) |
| `statutory_homeless_households` | `uk_la` | `UK-E…` | 2026 (Q1) | MHCLG detailed LA tables Jan–Mar 2026, Table A1: households assessed as owed a prevention or relief duty — 288 of 296 LADs have a value |
| `statutory_homeless_households` | `uk` | `England` | 2019–2025 (FY) | MHCLG England time series Table A1, financial-year totals |
| `statutory_homeless_households` | `uk_la` / `uk` | `UK-S…` / `Scotland` | 2021–2025 (FY) | Scottish Government HL1 Table 12: applications assessed as homeless **or threatened** with homelessness |
| `statutory_homeless_households` | `uk_la` / `uk` | `UK-W…` / `Wales` | 2016–2026 (FY) | StatsWales Section 75: households found eligible, unintentionally homeless and in priority need |
| `statutory_homeless_households` | `uk_la` / `uk` | `UK-N…` / `Northern Ireland` | 2026 | DfC bulletin Table 2.3: households **accepted** as homeless, Oct 2025–Mar 2026 |
| `temp_accommodation_households` | `uk_la` | `UK-E…` | 2026 | MHCLG detailed LA tables Table TA1, households in TA at 31 Mar 2026 — 284 of 296 LADs |
| `temp_accommodation_households` | `uk` | `England` | 1998–2026 | MHCLG England time series Table TA1 (one value per year: the latest quarter published for that year) |
| `temp_accommodation_households` | `uk_la` / `uk` | `UK-S…` / `Scotland` | 2021–2025 | Scottish HL2 Table 27, households in TA at 31 March |
| `homeless_applications` | `uk_la` / `uk` | `UK-S…` / `Scotland` | 2021–2025 (FY) | Scottish HL1 Table 1 (applications) — Scotland has no other count published under this name |
| `homeless_presentations` | `uk_la` / `uk` | `UK-N…` / `Northern Ireland` | 2026 | DfC Table 1.3, households presenting as homeless |
| `temp_accommodation_people` | `uk_la` / `uk` | `UK-W…` / `Wales` | 2023–2026 | StatsWales: homeless **individuals** temporarily accommodated at the end of the month (Wales publishes people here, not households — so it is not filed under `temp_accommodation_households`) |
| `temp_accommodation_placements` | `uk_la` / `uk` | `UK-N…` / `Northern Ireland` | 2026 | DfC Table 3.2A, TA **placements** in the half-year (NI publishes flows, not a stock — not filed as households) |

`geo_city`: 361 rows, `kind='uk_la'`, `geoid='UK-<ONS code>'`, `state` = England (296) /
Scotland (32) / Wales (22) / Northern Ireland (11). Names and codes come from the ONS
*Local Authority Districts (April 2025) Names and Codes in the UK* service; `pop`, `lat`, `lon`
are deliberately NULL (that code list carries no population or coordinates, and nothing here is
estimated).

### The four "statutory homelessness" definitions are not the same number
They are stored under one metric name **only** because they are the closest equivalent in each
legal system, and every row's `note` carries the definition:

- England — households assessed as **owed a prevention or relief duty** (HRA 2017).
- Scotland — applications **assessed as homeless or threatened with homelessness** (HL1).
- Wales — households found **eligible, unintentionally homeless and in priority need** (s75).
- Northern Ireland — households **accepted as homeless** (full duty).

Cross-nation comparisons must use the `note`, not the metric name alone.

### Sources (all keyless, all OGL v3.0, every response cached in `data/raw/uk/`)

| what | URL | bytes / sha256 |
|---|---|---|
| Rough sleeping snapshot in England: autumn 2025 – tables (ODS) | https://assets.publishing.service.gov.uk/media/699daac73e672177d0bc759b/Rough_sleeping_snapshot_in_England__autumn_2025.ods (index: https://www.gov.uk/government/statistical-data-sets/tables-on-rough-sleeping) | 364,483 B, `7b7ed536d0d5…` |
| Statutory homelessness England-level time series (ODS) | https://assets.publishing.service.gov.uk/media/6a82eee63bd75b81e2329a7b/Statutory_Homelessness_England_Time_Series_202603.ods (index: https://www.gov.uk/government/statistical-data-sets/live-tables-on-homelessness) | 281,848 B, `27d993342341…` |
| Statutory homelessness detailed LA tables, January–March 2026 (ODS) | https://assets.publishing.service.gov.uk/media/6a82eeb83bd75b81e2329a7a/Statutory_Homelessness_Detailed_Local_Authority_Data_202603.ods | 1,180,681 B, `f77eebd76145…` |
| ONS Local Authority Districts (April 2025) names + codes (ArcGIS query, JSON) | https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/LAD_APR_2025_UK_NC_v2/FeatureServer/0/query?where=1%3D1&outFields=LAD25CD,LAD25NM&returnGeometry=false&f=json&resultRecordCount=2000 | 23,355 B, 361 districts |
| Homelessness in Scotland 2024-25 — Main tables (XLSX) | https://www.gov.scot/publications/homelessness-in-scotland-2024-25/ (attachment `Main tables_Homelessness in Scotland 2024-25.xlsx`) | 8,285,936 B, `af4f9a253850…` |
| StatsWales *Rough sleepers by local authority* | dataset `116be6b0-97bb-4abc-8cfb-13ff4b5d5d7e`, page https://stats.gov.wales/en-GB/116be6b0-97bb-4abc-8cfb-13ff4b5d5d7e/start | CSV via `POST /en-GB/<id>/download` (`view_type=unfiltered&format=csv&view_choice=raw`) |
| StatsWales *Homeless individuals temporarily accommodated at the end of the period by local authority and accommodation type* | dataset `35713e0f-e755-4444-ab14-915d8d9432e1` | 1,493,603 B, `5deba22a0145…` |
| StatsWales *Households for which assistance has been provided by outcome…* | dataset `e856209f-09d4-4e98-824c-19c101813f67` | 526,375 B, `61a690ec60bd…` |
| NI Homelessness Bulletin, October 2025 – March 2026: tables (XLSX) | https://www.communities-ni.gov.uk/system/files/2026-06/ni-homelessness-bulletin-oct-mar-2026-tables.xlsx (page: https://www.communities-ni.gov.uk/publications/northern-ireland-homelessness-bulletin-october-2025-march-2026) | 1,507,427 B, `8aba899f94c4…` |

Operational notes learned the hard way, saved for the next worker:

- **No `odfpy` in this image and `pip` is broken**, so `uk.py` carries a ~45-line streaming ODS
  reader (`ods_sheets`, `zipfile` + `xml.etree.iterparse`). It handles `number-rows-repeated`,
  `number-columns-repeated`, `office:value` and text in `text:p`. Two traps: (1) `iterparse`
  fires the `text:p` end event *before* the cell's, so cell text must be captured as those events
  arrive — clearing the element on every end event silently blanks every string cell; (2) a
  31 MB `content.xml` (the LA workbook) parses in ~0.2 s when rows are cleared as they complete.
- **`communities-ni.gov.uk` and `nihe.gov.uk` reject the project user-agent / brotli**:
  `nihe.gov.uk` answers `403.2 Forbidden` to everything (blocker below); `communities-ni.gov.uk`
  returns 403 unless the request carries a browser UA **and** `Accept-Encoding: gzip, deflate`
  (adding `br` to that list flipped it back to 403). The bulletin tables are fetched with those
  headers.
- **StatsWales is now a Next.js app** (`stats.gov.wales`); there is no public JSON API. The
  dataset page embeds the full filter vocabulary, and the CSV comes back from
  `POST /en-GB/<dataset-id>/download` with `selected_filter_options=[]` (an empty selection =
  the whole dataset) — no CSRF token or session needed.
- `common.source_id_for` looks for `data/raw/uk/<sha1(url)>.bin`, but `fetch()` keys its cache by
  `sha1(method+url+form-body)`, so it never finds the payload and leaves `source.bytes`/`sha256`
  NULL. `stamp_source()` in `uk.py` writes size + sha256 onto the source rows this domain created
  (verified: 9/9 sources stamped).
- Publisher quirks handled: MHCLG writes `[x]`/`[z]`/`[c]`/`[n]` placeholders for missing,
  not-applicable, suppressed and "LA created by reorganisation" (`_num()` → NULL, never 0);
  Scottish worksheets stack two tables per sheet separated by a blank row (table 1b is
  *percentages* — reading past the blank row silently overwrote real counts with `1`); NI
  worksheets put the year and period label two rows above the data with the "per 1,000" rate
  column interleaved, so the latest *count* column has to be picked by descriptor, not by position.

## Verified

Five rows re-checked against the live publishers on 2026-11-19 (each source re-fetched with
`force=True`, i.e. bypassing the cache; the England rows were also read out of the raw
`content.xml` row XML and out of the publisher's own HTML release page, not through this
domain's parser):

| # | row in `metric` | DB value | live re-check | result |
|---|---|---|---|---|
| 1 | `uk_la` / `UK-E08000025` (Birmingham) / `rough_sleeping` / 2025 | 68 | raw `<table:table-row>` for "Birmingham" in the autumn 2025 ODS: `… 52, 17, 31, 39, 36, 59, 68` (2010→2025), last cell 68 | PASS |
| 2 | `uk` / `England` / `rough_sleeping` / 2025 | 4793 | gov.uk release page text: "number of people estimated to be sleeping rough on a single night in autumn 2025 is **4,793**. This is a new record high, exceeding the previous peak of 4,751 in 2017" (https://www.gov.uk/government/statistics/rough-sleeping-snapshot-in-england-autumn-2025/rough-sleeping-snapshot-in-england-autumn-2025) | PASS |
| 3 | `uk` / `England` / `temp_accommodation_households` / 2026 | 135580 | England time-series workbook, row `202603` ("31 March 2026"): `135580, 86460, 177530, …`; the same string appears in the January–March 2026 detailed LA workbook | PASS |
| 4 | `uk_la` / `UK-S12000036` (City of Edinburgh) / `temp_accommodation_households` / 2025 | 4358 | Scottish main tables, worksheet T27, row "Edinburgh": `2824, 3316, 3560, 3817, 4358` (2021→2025); Scotland row `13753 … 17240` matches | PASS |
| 5 | `uk_la` / `UK-W06000015` (Cardiff) / `rough_sleeping` / 2026 | 44 | StatsWales CSV re-downloaded: `44, Number of rough sleepers, 31/08/2026, Cardiff`; Wales row `164` at the same period | PASS |
| 6 | `uk_la` / `UK-N09000003` (Belfast) / `statutory_homeless_households` / 2026 | 1464 | NI bulletin Table 2.3 re-parsed: Belfast row `… 1823, 1464` (last column = Oct–Mar 2025/26); NI row `… 5813, 5039` | PASS |

## Coverage

**Counts written: 6,686 rows — 361 `geo_city`, 6,325 `metric`; 9 `source` rows (all stamped with
size + sha256); 16 HTTP requests, all HTTP 200 at 1 req/s.**

| universe | captured | notes |
|---|---|---|
| English local authority districts, autumn 2025 rough sleeping | **296 / 296 = 100%** | England has 296 LADs in the ONS April 2025 code list (the ONS April 2023 list also has 296, checked), and the MHCLG snapshot table publishes exactly those 296 rows — so 296 is the complete current universe, not a sample. The brief's "≥300 English local authorities" target is above the number of districts England has had since at least April 2021; no row is missing and nothing was padded to reach 300. |
| English LAD-years of rough sleeping | **4,736 / 4,736 = 100%** | 296 LADs × 16 autumns (2010–2025) |
| England national rough sleeping years | **16 / 16** | 2010–2025, from the publisher's own England row |
| England households owed a prevention/relief duty, per LA, 2026 Q1 | **288 / 296 = 97%** | the 8 gaps are `[x]`/`[c]` in the published workbook (LA did not return / suppressed) — kept as NULL, not zero |
| England households in TA at 31 Mar 2026, per LA | **284 / 296 = 96%** | same, e.g. Ashford and Adur are `[x]`/`[c]` in the source |
| England national statutory series | **7 FY (2019–2025)** | the A1 time series only starts at April 2018 because the HRA 2017 duty replaced the old acceptances series |
| England national TA series | **29 years (1998–2026)** | TA1 goes back to 31 March 1998 |
| Scottish councils | **32 / 32 = 100%**, 5 FYs (2021–2025) × 3 metrics = 480 + 15 national | |
| Welsh unitary authorities | **22 / 22 = 100%**; rough sleeping 4 years (2023–2026), TA individuals 4 years, s75 11 years (2016–2026, 239/242 cells populated) | |
| Northern Ireland districts | **11 / 11 = 100%**, 3 metrics, one reporting period (Oct 2025–Mar 2026) | |
| UK-wide aggregate | **not written** | no publisher puts out a UK rough sleeping or homelessness total across the four administrations; summing them would be an invention, so `entity_type='uk'` holds one row set per nation instead |

**Still missing / next steps**

1. **England per-LA history beyond the latest quarter.** The metric key is
   `(entity_type, entity_id, metric, year)`, so only one value per LA-year fits. Earlier quarters
   live in ~40 separate workbooks listed on `live-tables-on-homelessness` (2018 Q1 → 2026 Q1);
   ingesting them needs a quarterly dimension (a `uk_` private table keyed
   `(ons_code, metric, period)`, or a period code in `note` + a schema change) — flag for the
   schema owner rather than silently overwriting a quarter with another quarter.
2. **Wales quarter/month granularity** — same reason; the latest month of each year is kept.
3. **England regions** (`E12…`) are in the rough sleeping table and the LA workbooks but are not
   stored as entities; Scotland/Wales/NI sub-national regions likewise. Only LADs and the four
   nations are in the store.
4. **NI is a single half-year.** Older bulletins (Jan–Jun 2021 → Oct 2024–Mar 2025) are on the
   same index page as separate XLSX files; the parser is generic over sheet name (`1_3`, `2_3`,
   `3_2A`), so a loop over the ten publication URLs would add ~10 periods without new parsing
   logic — not done here to keep the sweep inside one minute.
5. **Scotland has no rough sleeping count** and NI has no rough sleeping count at district level;
   Wales has a monthly LA snapshot instead of an autumn count. No series was synthesised to fill
   those.
6. **`geo_city.pop` is NULL for every UK LA** — population denominators (NRS/NISRA/ONS mid-year
   estimates) are a separate source; a per-1,000 rate would currently have to be computed from
   data this domain does not have, so none is written.
7. **England rough sleeping before 2010** exists in the autumn statistics but only as London-only
   counts prior to 2010; the ODS in the live tables starts at 2010 and that is where this
   ingestor starts.

### ⚠ One thing the site builder needs to know

`scripts/build_site.py:456` picks the country dot with
`SELECT value, year FROM metric WHERE entity_type='uk' AND metric='rough_sleeping' ORDER BY year DESC LIMIT 1`
and labels it *United Kingdom*. That row set is now four nations, not one: this domain stores
England (2010–2025) and Wales (2023–2026); neither the Scottish main tables nor the NI bulletin
contains a rough-sleeping count at all (checked their table-of-contents sheets), so no Scottish or
NI row exists to compete.
Because Wales publishes a month as late as August 2026, that query now returns **Wales 164 (2026)**
rather than **England 4,793 (2025)** — the tie is broken arbitrarily by SQLite. The data is right;
the query needs `AND entity_id='England'` (or a proper UK-wide series, which no publisher
produces). The site was deliberately **not** edited from here — flagging it to the owner instead of
silently bending the rows to fit one query.
