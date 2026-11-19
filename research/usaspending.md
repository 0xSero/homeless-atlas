# usaspending — federal money aimed at homelessness

Every federal contract, grant and cooperative agreement that USASpending.gov associates with
homelessness, pulled from the public v2 API (no key, no quotas, 1 req/s).

- **Ingestor**: `ingest/domains/usaspending.py` (`DOMAIN = "usaspending"`)
- **Endpoints** (all `https://api.usaspending.gov`):
  - `POST /api/v2/search/spending_by_award_count/` — sizes a filter set
  - `POST /api/v2/search/spending_by_award/` — pages results, `limit=100`
  - `GET /api/v2/references/assistance_listing/{2-digit}/` — CFDA titles (never hardcoded)
  - `GET /api/v2/awards/{generated_internal_id}/` — per-award detail, used for the spot checks below
- **Tables**: `award` (keyed on `generated_internal_id` → `award_key`), `org` (keyed on
  `name_norm, state`), plus the domain-private cursor table `usaspending_progress`
- **Run**: `cd /home/ser/projects/homeless-atlas && python3 -m ingest.run usaspending`
  (`--warm` only fills the HTTP cache, `--load` replays the cache into SQL without touching the
  network; both resume where they stopped)

## What was pulled

Query families (33), expanded to **130 queries**, 4,079 pages, all cached under
`data/raw/usaspending/`:

| family | filter | notes |
|---|---|---|
| 9 assistance listings | `program_numbers` 14.231, 14.267, 14.241, 14.275, 14.239, 14.235, 14.238, 14.276, 14.249 | grants (02–05) |
| 4 NAICS | 624221, 624229, 623220, 925110 | contracts (A–D) |
| 11 keywords × grants | homeless, homelessness, continuum of care, emergency solutions grant, rapid rehousing, permanent supportive housing, HMIS, Housing First, transitional housing, street outreach, coordinated entry | grants (02–05) |
| 11 keywords × contracts | same keywords | contracts (A–D) |

Notes on the filter shapes, verified live rather than assumed:

- `award_type_codes` groups cannot be mixed: contracts are `A,B,C,D`, grants are `02,03,04,05`
  (the API answers HTTP 422 "'award_type_codes' must only contain types from one group" otherwise).
- Omitting `time_period` is **not** a 12-month default: `keywords=["homeless"], 02-05` returns
  62,607 awards with no window and the same 62,607 for an explicit 2007-10-01…2026-09-30 window.
- The search endpoint stops serving pages at a result window (~18k–50k rows: page 1000 with
  `limit=100` returns "over the maximum result limit 50000"), and its default ordering slices
  awards by age — a single all-time query therefore silently loses whole year cohorts. Families
  over 19,000 awards are therefore swept **one fiscal year at a time** (`date_type=action_date`,
  FY2008–FY2027 = 20 windows), which is why every year from 2008 on is populated below.
- `program_numbers=["14.231"]` returns awards whose *other* listing is 14.235 (an award can carry
  several listings and the row only shows the primary one). Per-program counts in the `award`
  table are therefore *primary-listing attribution*, not a statement about which funding streams an
  award touches — dedupe is by `generated_internal_id`, so no award is lost or double counted.

## Verified

Five awards drawn at random from the finished table on 2026-11-19, each re-fetched from the live
award-detail endpoint (`GET /api/v2/awards/<generated_internal_id>/`) and compared field by field.
Everything matched; `amount_obligated` on the database side came from `Award Amount` in the search
payload, which equals `total_obligation` in the detail payload for all five.

| URL re-checked | award ID | recipient (UEI) | amount | CFDA | period start | state |
|---|---|---|---|---|---|---|
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_MA0146L1T081508_086/` | MA0146L1T081508 | CITY OF LOWELL (V7J3DKVP8A66) | 169,754.00 | 14.267 | 2016-07-01 | MA |
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_MA01B70-0034_086/` | MA01B70-0034 | BOSTON, CITY OF (LJDLU7EA4SK1) | 39,231.00 | 14.231 | 2007-10-01 | MA |
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_NJ0180L2F161810_086/` | NJ0180L2F161810 | NEW JERSEY HOUSING AND MORTGAGE FINANCE AGENCY (MLMTNTWMGL13) | 6,648.00 | 14.267 | 2019-11-01 | NJ |
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_OH0169L5E071604_086/` | OH0169L5E071604 | LICKING METROPOLITAN HOUSING AUTHORITY (KS6GM27QPFN1) | 46,973.00 | 14.267 | 2017-12-01 | OH |
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_TX0113B6T010801_086/` | TX0113B6T010801 | TARRANT COUNTY (DBH1UNN8U5J3) | 1,103,295.00 | 14.231 | 2008-10-01 | TX |

Checked per row: award ID, recipient name, recipient UEI, total obligation, CFDA number, period of
performance start and place-of-performance state. The `program` title is not in the detail payload
(its `cfda_info[].cfda_program_title` comes back empty); it is read from
`/api/v2/references/assistance_listing/14/`, where 14.267 = "Continuum of Care Program" and
14.231 = "Emergency Solutions Grant Program".

Independent presence check (page 1 and page 3 of four query families, every returned
`generated_internal_id` looked up in `award`): 100/100 present for each of
`keywords=["permanent supportive housing"]` (grants), `keywords=["HMIS"]` (contracts),
`naics_codes=["624229"]` (contracts), `program_numbers=["14.238"]` (grants).

## Coverage

**184,659 awards** (10,303 contracts, 174,356 grants), **$115.95 bn** obligated,
**10,876 org rows** created by this ingestor (one per distinct `name_norm`+state recipient;
10,464 distinct normalised recipient names), 56 place-of-performance states/territories,
place of performance present on 182,853 rows (99.0%), recipient UEI on 179,464 (97.2%).

Awards by primary assistance listing (all rows also carry `cfda`, `agency`, `subtier`, `psc`,
`naics`, `description`, `pop_start`, `pop_end`, FFY):

| program | awards |
|---|---|
| Continuum of Care Program (14.267) | 92,995 |
| Emergency Solutions Grant Program (14.231) | 20,660 |
| Supportive Housing Program (14.235) | 20,315 |
| Home Investment Partnerships Program (14.239) | 13,086 |
| Shelter Plus Care (14.238) | 7,839 |
| VA Supportive Services for Veteran Families | 3,501 |
| Housing Opportunities for Persons with AIDS (14.241) | 3,171 |
| VA Homeless Providers Grant and Per Diem | 2,839 |
| Homeless Veterans' Reintegration Program | 1,424 |
| Transitional Housing Assistance (FVPSA) | 868 |

159,807 awards (86.5%) sit on one of the nine HUD homeless/housing listings swept by number;
59,698 have "homeless" in the award description and 10,171 in the recipient name. The remainder is
the honest cost of keyword and NAICS sweeps (e.g. NAICS 624221 "Temporary Shelters" also matches
tent and trailer rentals to the Army) — they are kept rather than filtered so a downstream user can
decide, and `cfda`/`program` make the precise subset easy to select.

Awards by federal fiscal year of the base obligation date (fallback: period start). This is now
flat, which is the point: the same sweep run as a single all-time query per family returned 2006:
741 … 2017: 133, 2018: 15, 2019: 5 because the result window only served the oldest slice.

| FY | awards | FY | awards | FY | awards |
|---|---|---|---|---|---|
| 2006 | 1,719 | 2014 | 9,661 | 2022 | 9,145 |
| 2007 | 2,517 | 2015 | 10,070 | 2023 | 10,133 |
| 2008 | 8,383 | 2016 | 8,969 | 2024 | 9,315 |
| 2009 | 9,506 | 2017 | 9,991 | 2025 | 8,280 |
| 2010 | 11,041 | 2018 | 10,237 | 2026 | 4,341 |
| 2011 | 10,945 | 2019 | 9,517 | 2027 | 85 |
| 2012 | 9,909 | 2020 | 9,652 | pre-2006 | 120 |
| 2013 | 10,583 | 2021 | 10,540 | | |

Universe captured, where the API can be asked directly (all-time counts vs rows held):

| filter | API total | rows held | share |
|---|---|---|---|
| 14.267 CoC | 92,995 | 92,995 | 100% |
| 14.241 HOPWA | 3,172 | 3,171 | 100% |
| 14.275 Housing Trust Fund | 533 | 533 | 100% |
| 14.276 YHDP | 825 | 825 | 100% |
| 14.249 SRO | 383 | 383 | 100% |
| 14.239 HOME | 13,094 | 13,086 | 99.9% |
| 14.238 Shelter Plus Care | 7,881 | 7,839 | 99.5% |
| NAICS 624221 / 624229 / 623220 / 925110 | 2,738 / 2,463 / 2,758 / 316 | same | 100% |
| 14.235 Supportive Housing | 21,424 | 20,315 (primary listing) | see note |
| 14.231 ESG | 26,409 | 20,660 (primary listing) | see note |

The two "see note" rows are the multi-listing effect described above, not missing awards: the
14.231 filter also returns 14.235 rows and vice versa, and the sampled-page check found every
returned award in the table.

**Still missing / deliberately out of scope**

- `place_city` and `place_county` are NULL everywhere. The search payload returns only *codes*
  (`Place of Performance City Code`, and grants mask it, e.g. `HI*****`); the city name exists only
  on the per-award detail endpoint, which would mean one request per award (~50 h at 1 req/s).
  `place_state` and `Pop Zip5` are populated. Filling this is the single biggest improvement left.
- `description` is truncated at 4,000 characters (282 rows hit the cut); the full text is on the
  per-award detail endpoint.
- `fy` is the federal fiscal year (Oct–Sep) of `Base Obligation Date`, falling back to the period
  start — derived, never taken from a published field. `is_est` stays 0: every number, ID and
  date is a published value, and unknown fields are NULL rather than modelled.
- Total outlays are fetched but not stored: the schema has `amount_obligated` and `amount_total`
  only, and outlays are not obligations. `amount_total` = `Potential Award Amount` where the API
  supplies it, otherwise the same award amount.
- IDVs (`IDV_A…`), loans and direct payments were not swept — only contracts (A–D) and grants
  (02–05). Sub-awards are a separate endpoint and are not part of this table.
- 2,763 rows carry negative obligations (HUD de-obligations/recissions on old ESG awards). They are
  stored as published, not clipped.
- The as-of date is the ingestion date; USASpending revises past years, so a later rerun can shift
  totals. Rerunning is idempotent on `award_key`.
- Other agents share `atlas.db`: rows written here were later normalised (blank `program` labels
  and empty-string dates that a concurrent `usaspending_sweep` writer had left on shared
  `award_key`s are cleared on every non-cache-only run).
