# county: per-county layer

Ingestor: `ingest/domains/county.py` (DOMAIN `county`). Every metric row is
`entity_type='county'`, `entity_id` = 5-digit county FIPS. Run it with
`python3 -m ingest.domains.county` (all steps), `--step <name>` for one step, `--states PA,NY`
to limit the recipients step, `--selftest` to also print 3 sample rows of `metric` and
`county_coc`. A step that fails is logged and the others still run; the run exits 1 if any
step failed. A second run from cache takes about 6 seconds (recipients for one state included).

## What was captured

| Step | Source | Written |
|---|---|---|
| `acs` | ACS 2020-2024 5-year, table-based summary files, 14 tables | 15 metrics, year 2024, is_est=0 |
| `crosswalk` | B01003 tract population, 2024 Gazetteer tract internal points, `geo_coc.geometry` | `county_coc` (rebuilt each run), `county_coc_count` |
| `pit_by_county` | `pit` table (HUD PIT by CoC) through `county_coc` | `pit_*` (is_est=0) or `pit_total_apportioned` (is_est=1), 2007-2025 |
| `places` | CDC PLACES county data, 2025 release (swc5-untb) | 5 `places_*` metrics, is_est=1 |
| `hpsa` | HRSA mental health HPSA detail file | `mh_hpsa_count`, `mh_hpsa_max_score`, `mh_hpsa_geographic`, year 2026 |
| `federal_awards` | USASpending spending_by_geography, county, place of performance | `federal_homeless_awards` FY2021-FY2025, `federal_homeless_awards_per_capita` |
| `recipients` | USASpending spending_by_category/recipient, one call per county | `county_recipient`, top 15 recipients over FY2021-FY2025 |

### ACS formulas (every column checked against the 2024 file header and `ACS20245YR_Table_Shells.txt`)

- `acs_population` = B01003_E001 (persons)
- `poverty_rate` = B17001_E002 / B17001_E001 * 100
- `median_household_income` = B19013_E001 (usd, 2024 dollars)
- `median_gross_rent` = B25064_E001 (usd)
- `median_home_value` = B25077_E001 (usd)
- `rent_burden_30` = (B25070_E007 + E008 + E009 + E010) / (B25070_E001 - B25070_E011) * 100
- `rent_burden_50` = B25070_E010 / (B25070_E001 - B25070_E011) * 100
- `renter_share` = B25003_E003 / B25003_E001 * 100
- `rental_vacancy_rate` = B25004_E002 / (B25003_E003 + B25004_E002 + B25004_E003) * 100
- `unemployment_rate` = B23025_E005 / B23025_E003 * 100
- `uninsured_rate` = (B27010_E017 + E033 + E050 + E066) / B27010_E001 * 100
- `veteran_share` = B21001_E002 / B21001_E001 * 100
- `overcrowded_share` = (B25014_E005 + E006 + E007 + E011 + E012 + E013) / B25014_E001 * 100
- `public_assistance_share` = B19057_E002 / B19057_E001 * 100
- `no_vehicle_share` = B08201_E002 / B08201_E001 * 100

No formula had to change from the spec. Negative Census annotation codes (for example
-666666666, "not available") are treated as missing, and a ratio with a zero or missing
denominator is not written. `ACS_YEAR` is a module constant so the refresh can bump it.

### Crosswalk and PIT rule

Each 2024 ACS tract (85,382) is placed in the smallest-area CoC whose simplified FY2024
polygon holds its Gazetteer internal point (even-odd rule, holes respected, bbox prefilter,
latitude-band edge index). `county_share` = tract population of the county in that CoC /
county tract population; `coc_share` = the same population / all tract population assigned to
that CoC. Tracts in no polygon go to `coc_num='(none)'` with `coc_share` NULL.

A county gets the HUD count itself (`pit_total`, `pit_sheltered`, `pit_unsheltered`,
`pit_chronic`, `pit_veterans`, `pit_youth`, is_est=0) only when one CoC holds >= 99% of the
county and >= 99% of that CoC lies in the county. Every other county gets
`pit_total_apportioned` = round(sum of CoC total * coc_share), is_est=1, sourced to the
largest contributing CoC's pit row. A county-year is skipped when any contributing CoC has no
count that year (636 county-years), so a missing count never reads as zero.

### Choices made here

- HPSA: only `HPSA Status = Designated` rows count. The 8,267 "Proposed For Withdrawal" rows
  are ignored (not counted separately). `mh_hpsa_count` is written as 0 for counties in
  `geo_city` with no designation, and the note says that 0 is a published absence.
  `mh_hpsa_geographic` is 0 or 1 for the same universe. Year = year the file was retrieved
  (read from the cache metadata, so a rerun from cache keeps it).
- Assistance listing 64.033 (SSVF) was dropped: it is a real listing ($352M in FY2024 by
  `spending_by_category/cfda`), but its awards carry a state place of performance only and the
  county layer returns no rows. The `references/cfda/totals/<num>/` check the spec suggested
  answers HTTP 204 for every listing, so each listing was verified by a non-empty county
  result instead. The 7 listings kept: 14.267, 14.231, 14.276, 64.024, 93.150, 93.623, 14.241.
- `federal_homeless_awards_per_capita` is is_est=1 (repo convention in `health`: a rate we
  compute from two sources is flagged), and uses the ACS 2020-2024 population for every year.
- Recipients: matched to `org` by UEI first, then by `name_norm` plus the county's state
  (the recipient endpoint returns no recipient state). 120 of 180 PA rows matched.
- PLACES: see the open item under Coverage.

## Caveats

- Apportioned PIT values are estimates. HUD does not count by county.
- The crosswalk uses the simplified CoC polygons. Coastlines lose detail: 9.2% of Manhattan's
  tract population (36061) falls in no polygon. Nationally 99.67% of tract population is
  assigned. `coc_share` is relative to assigned population, so a CoC's apportioned county
  values still sum to its full count.
- The FY2024 CoC boundaries are applied to every PIT year back to 2007; earlier years across
  a CoC merger or split are less reliable.
- PLACES values are model-based small-area estimates, not direct survey counts.
- ACS margins of error are not stored. B25077 is top-coded by Census.
- USASpending amounts are net obligations by action date; a deobligation can make a year
  negative. Connecticut appears under its old county codes in USASpending and HRSA but under
  planning regions (091xx) in ACS and `geo_city`.

## Verified

1. Erie County PA population: the cached B01003 file line is
   `0500000US42049|269052|-555555555`, and `acs_population` for 42049 is 269052. The B17001
   line gives E001=255825, E002=38291, and 38291/255825*100 = 14.968 = `poverty_rate`.
   https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/acsdt5y2024-b01003.dat
2. PA-605 is Erie's own CoC: `county_coc` shows 42049 / PA-605 with county_share 1.0 and
   coc_share 1.0, and county `pit_total` 2025 = 457 = `pit.total` for PA-605 2025 (also
   sheltered 410, unsheltered 47, chronic 112, veterans 24, youth 22, all equal).
3. USASpending Erie FY2024: direct POST to
   https://api.usaspending.gov/api/v2/search/spending_by_geography/ with
   `geo_layer_filters:["42049"]` and FY2024 dates returned aggregated_amount 3104068.74,
   equal to `federal_homeless_awards` 42049 / 2024. Los Angeles FY2025 the same way:
   298822397.54 live and in the table.
4. Erie recipients: direct POST to
   https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/ (PA, county 049,
   FY2021-FY2025) returned COUNTY OF ERIE 12876772.80, CITY OF ERIE 2423812.00, VETERANS
   LEADERSHIP PROGRAM OF WESTERN PENNSYLVANIA INC 481834.92, ERIE UNITED METHODIST ALLIANCE
   412521.48, all equal to `county_recipient`. Their sum, 16,194,941.20, equals the sum of Erie's
   five `federal_homeless_awards` rows (16,194,941.20). Org 1340 (COUNTY OF ERIE, Erie PA 16501)
   carries a second county UEI, M5YCQLLWGFB1, which USASpending's recipient search confirms.
5. PLACES Los Angeles: live query
   https://data.cdc.gov/resource/swc5-untb.json?$where=locationid='06037' AND datavaluetypeid='CrdPrv' AND measureid='MHLTH'
   returned 16.2 for 2023, equal to `places_mental_distress` 06037 / 2023. HRSA Erie: the
   retrieved file lists 4 designated MH HPSAs for 42049 (scores 21, 16, 14, 12) and its
   Single County geographic HPSA 742049 is Withdrawn, matching count 4, max score 21,
   geographic 0.

## Coverage

Counties with each metric after the run on 2026-11-20 (universe: 3,144 `geo_city` counties;
ACS and HRSA also carry Puerto Rico, so some counts exceed it):

| Metric | Counties |
|---|---|
| acs_population, poverty_rate, rent_burden_30/50, renter_share, rental_vacancy_rate, unemployment_rate, uninsured_rate, veteran_share, overcrowded_share, public_assistance_share, no_vehicle_share | 3,222 |
| median_household_income | 3,221 |
| median_home_value | 3,217 |
| median_gross_rent | 3,215 |
| county_coc_count | 3,217 (5 counties lie in no CoC polygon: 02105, 12003, 12029, 12125, 40083) |
| pit_total and the other pit_* (county is its own CoC) | 162 |
| pit_total_apportioned | 3,055 |
| places_* (each of 5) | 2,956 |
| mh_hpsa_count, mh_hpsa_geographic | 3,279 |
| mh_hpsa_max_score | 3,116 |
| federal_homeless_awards (any FY2021-FY2025) | 983 (809 to 852 per year) |
| federal_homeless_awards_per_capita | 971 |
| county_recipient | 47 PA counties, 180 rows (the full national pass, about 900 counties, was not run) |

`county_coc`: 3,607 rows; 2025 county PIT values (exact plus apportioned) sum to 744,170 of
the national 745,652 (the gap is CoCs without a polygon, mostly territories).

Missing:
- PLACES 2025 has no rows for these five measures in Pennsylvania (67 counties) or a handful
  of other counties, so Erie has no `places_*`. The 2024 release (fu4u-a9bh) has them (Erie
  MHLTH 17.4, DEPRESSION 23.4, BINGE 18.2, ACCESS2 6.0, DISABILITY 34.1, data year 2022). The
  code supports filling gaps from it (`PLACES_PRIOR_FALLBACK`), but it is off: mixing model
  vintages is the project owner's call.
- SSVF (64.033) county spending, as above.
- The recipients step has only been run for PA.
