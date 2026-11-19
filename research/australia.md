# australia — Australia: ABS census homelessness estimates, SHS clients and SHS funding

Domain owner: `ingest/domains/australia.py` (`DOMAIN = "australia"`). Two publishers with two
unrelated systems, so the module has one reader per publisher and never mixes their semantics.
Australian rows are quarantined in `geo_city(kind='au_state' | 'au_lga')` and in
`metric(entity_type='au_state' | 'au_lga' | 'australia')`.

Run: `python3 -m ingest.run australia` → **1,952 rows** (4 sources, ~1.8 MB of payloads cached
under `data/raw/australia/`; `python3 -m ingest.domains.australia --selftest` re-runs and prints
samples). A second run writes the identical rows (upsert on `geoid` for `geo_city`, on
`(entity_type, entity_id, metric, year)` for `metric`) — 1,952 rows again, no duplicates.
`python3 -m ingest.domains.australia --reset` deletes only this module's rows and rebuilds them
(the LGA identity scheme was corrected before release, so a stale first pass had to be cleared;
`--reset` runs the same fix reproducibly).

Row counts: `geo_city` 563 (555 `au_lga` + 8 `au_state`), `metric` 1,389
(1,092 `au_lga` + 264 `au_state` + 33 `australia`), total 1,952.

## What is stored

| metric | entity_type | entity_id | years | publisher / table |
|---|---|---|---|---|
| `census_homeless_estimate` | `au_state` | `AU-<NSW\|VIC\|QLD\|SA\|WA\|TAS\|NT\|ACT>` | 2006, 2011, 2016, 2021 | ABS 2049.0 *Estimating homelessness: Census, 2021*, table 1.3, row "Total homeless persons" |
| `census_homeless_estimate` | `australia` | `Australia` | 2006, 2011, 2016, 2021 | same table, published `Aust.` column (89,733 → 102,439 → 116,427 → 122,494) |
| `census_homeless_rate_per_10k` | `au_state` / `australia` | as above | 2006, 2011, 2016, 2021 | same cube, table 1.5 (the ABS-published rate per 10,000, not a derived one) |
| `census_homeless_estimate` | `au_lga` | `AU-LGA-<abbr>-<slug>` | 2016, 2021 | same cube, tables 6.2 / 6.1, column "All homeless persons" |
| `shs_clients` | `au_state` / `australia` | `AU-<abbr>` / `Australia` | 2021–2025 (FY) | RoGS 2026 s19, table 19A.21 "Total clients" (AIHW Specialist Homelessness Services Collection) |
| `shs_funding` | `au_state` / `australia` | as above | 2021–2025 (FY) | RoGS 2026 s19, table 19A.21 "Total real recurrent expenditure" (identical to 19A.1's figure; written once, from 19A.21), $m 2024-25 |
| `shs_funding_per_person` | `au_state` / `australia` | as above | 2021–2025 (FY) | RoGS 2026 s19, table 19A.1 "Real expenditure per person in the residential population" (2024-25: NSW 38.83 … NT 329.84, national 64.05) |
| `shs_cost_per_client` | `au_state` / `australia` | as above | 2021–2025 (FY) | RoGS 2026 s19, table 19A.21 "Estimated cost per client" |
| `shs_unassisted_requests_daily` | `au_state` / `australia` | as above | 2021–2025 (FY) | RoGS 2026 s19, table 19A.11, "Accommodation services", average daily unassisted requests |

National series as written:
`census_homeless_estimate` 89,733 (2006) → 102,439 (2011) → 116,427 (2016) → 122,494 (2021);
`shs_clients` 278,275 (FY2020-21) → 272,694 → 273,648 → 280,078 → 288,970 (FY2024-25);
`shs_funding` $1,427.2 m → $1,518.6 m → $1,515.9 m → $1,578.7 m → $1,754.9 m (2024-25 dollars).

`geo_city`: 8 rows `kind='au_state'` (`geoid='AU-<abbr>'`, `state` = the abbreviation, `name` =
the full state/territory name) and 555 rows `kind='au_lga'` (`geoid='AU-LGA-<abbr>-<slug>'`,
`state` = abbreviation, `name` = the name ABS printed). 555 = 537 LGAs that appear in both Census
tables + 9 that only appear in 2016 + 9 that only appear in 2021 (listed below). `pop`, `lat`,
`lon` are NULL on every row: neither source publishes a population or a coordinate for these
units and nothing here is estimated.

84 of the 555 LGAs have a published `0` in at least one Census year (115 of the 1,092 LGA-year
cells are zero: 64 in 2021, 51 in 2016 — very small rural shires) — those zeros are ABS's own
printed value, not a fill. Largest LGA cell is 5,813 (`AU-LGA-QLD-brisbane`, 2016).

## The bases are not the same and the notes say so

1. **Usual residence vs place of enumeration.** The state/territory rows come from ABS table 1.3,
   which is "STATE AND TERRITORY OF **USUAL RESIDENCE**". ABS publishes nothing finer than state
   by usual residence, so the LGA rows (tables 6.1/6.2) are "by place of **ENUMERATION**". The two
   are different populations: 2021 NSW is 35,011 by usual residence but 34,982 by place of
   enumeration; Queensland is 22,428 vs 22,444. Every row's `note` names the basis.
2. **Calendar vs financial years.** Census figures are calendar years (2006/2011/2016/2021); the
   SHS figures are financial years stored under their **ending** year (`FY2024-25` → `year=2025`),
   with the FY label in `note`.
3. **What `shs_funding` is.** It is the recurrent government expenditure on specialist homelessness
   services that RoGS reports for each jurisdiction, in real 2024-25 dollars. Since 1 July 2024 SHS
   is funded under the **National Agreement on Social Housing and Homelessness (NASHH)**, whose
   homelessness component is matched equally by the Australian Government and the states, with the
   states making the direct expenditure (RoGS s19 "Funding"). RoGS states the national figure as
   "$1.8 billion (or $64.05 per person)" in 2024-25 — that is this series' national row
   (1,754.9 $m, 64.05 $/person), and it is **not** a per-state NASHH allocation. No per-state
   bilateral allocation table is published in machine-readable form (see Blockers).

## ABS quirks that cost time (read before touching this module)

- **Table 1.3 is missing its "2011" year-label row.** The blocks in that sheet are 2021, 2016,
  *(no label)*, 2006 — the 2011 block header is simply absent from the file. Dating the blocks by
  position would silently have written 2011's numbers into 2016. `parse_abs_national_series()`
  therefore reads table 1.1 (which *does* carry `2006 | 2011 | 2016 | 2021` as real column
  headers) and each block is dated by matching its own published national value
  (89,733 / 102,439 / 116,427 / 122,494). If a value matches nothing the ingestor raises instead
  of guessing a year.
- **LGA names gain/lose a council-type suffix between vintages.** 2016 prints `Maribyrnong (C)`,
  `Barkly (R)`, `Campbelltown (C) (NSW)`; 2021 prints `Maribyrnong`, `Barkly`, `Campbelltown
  (NSW)`. Only 6 of 547 LGA names match literally across the two tables. The module strips the
  council-type token and a trailing state tag, drops a leading "The" and the conjunction "and",
  then slugs — which is a *spelling* rule, not a guess: it merges `Blackall-Tambo`/`Blackall
  Tambo`, `Orroroo/Carrieton`/`Orroroo Carrieton`, `The Coorong`/`Coorong`,
  `Norwood Payneham and St Peters`/`Norwood Payneham St Peters`, etc. 537 LGAs end up with both
  Census years on one `geoid`; the 18 that do not are genuine renames, mergers and new units and
  are deliberately **not** merged:
  `Botany Bay` + `Rockdale` → `Bayside` (NSW 2016 council merger); `Gundagai` → part of
  `Cootamundra-Gundagai Regional`; `Nambucca` → `Nambucca Valley`; `Western Plains Regional` →
  `Dubbo Regional`; `Mallala` → `Adelaide Plains` (SA rename); `Port Pirie City and Dists` →
  `Port Pirie`; `Anangu Pitjantjatjara` → `Anangu Pitjantjatjara Yunkunytjatjara`; new in 2021:
  `Darwin Waterfront Precinct`, `Christmas Island`, `Cocos Islands`. A consumer joining on
  `name_norm` must apply that mapping itself; the printed name is always kept in `geo_city.name`
  and in each metric row's `note`.
- **ABS's own totals do not reconcile, and we do not "fix" them.** In the LGA tables the 547
  individual 2021 cells sum to 122,545, the nine published state subtotals sum to 122,484, and the
  published national cell is 122,494 (2016: 116,395 / 116,428 / 116,427). Individual LGA cells are
  perturbed by ABS for confidentiality, so no cell is ever recomputed or adjusted here. The same
  is true in table 1.3: the eight state columns sum to 89,736 / 102,436 / 116,367 / 122,487
  against published national 89,733 / 102,439 / 116,427 / 122,494 (Other Territories and
  "state not stated" live only in the Aust. column).
- **RoGS publishes one wide CSV, not ~42 workbooks.** `Table_Number | Year | Measure | … |
  Description1..6 | Unit | NSW | Vic | Qld | WA | SA | Tas | ACT | NT | Aust` (2,618 rows, 19A.1–
  19A.42). Note the jurisdiction column order differs from ABS's (RoGS: NSW, Vic, Qld, **WA**,
  **SA**; ABS: NSW, Vic, Qld, **SA**, **WA**), so the reader keys on the CSV's own header names.
  Where the row's measure label sits **changes between tables** — `Description1` for 19A.21
  (`Total clients`), `Description3` for 19A.1 (`Real expenditure per person in the residential
  population`), because 19A.1 puts the table title in `Description1` — so the reader offers every
  non-empty `Description*` cell to a `(table, label, unit)` map instead of trusting one column.
  The 19A.\*.xlsx is also reachable and is where the human-readable table titles live
  ("Recurrent cost per client accessing homelessness services, 2024-25 dollars") — it was read to
  confirm what 19A.21 means, but rows are written with the CSV's provenance.
- **Two transports, both boring: `abs.gov.au` and `assets.pc.gov.au`.** ABS serves its cubes with
  no cookies and no referer; the RoGS dataset is linked from the chapter page with a
  `?VersionId=…` query string, which is the URL recorded on the `source` row. The bare path also
  answers 200 to a browser UA (tested), so the query string is canonical rather than required —
  both variants serve the same 1,133,782 B file.

## Sources (all keyless; every response cached in `data/raw/australia/`)

| what | URL | bytes / sha256 |
|---|---|---|
| ABS 2049.0 *Estimating homelessness: Census, 2021* — cube 1, state and territory of usual residence, tables 1.1–1.15 (used: **1.1**, **1.3**, **1.5**) | https://www.abs.gov.au/statistics/people/housing/estimating-homelessness-census/2021/20490do001_2021.xlsx | 182,646 B, `226d0ce99062…` |
| ABS 2049.0 — cube 6, Local Government Area, tables 6.1 (2021) and 6.2 (2016) | https://www.abs.gov.au/statistics/people/housing/estimating-homelessness-census/2021/20490do006_2021.xlsx | 124,495 B, `0528a2b2ed19…` |
| ABS release page (index for the eight cubes; also fetched, and the page that names each cube's contents) | https://www.abs.gov.au/statistics/people/housing/estimating-homelessness-census/2021 | 420,989 B, `b476949f6e05…` |
| Productivity Commission, RoGS 2026 Part G section 19 *Homelessness services* — machine-readable dataset (used: tables **19A.1**, **19A.11**, **19A.21**) | https://assets.pc.gov.au/2026-01/rogs-2026-partg-section19-homelessness-services-dataset.csv?VersionId=oou7gCfLtIUjLTYmqVgZgevEBezJo44g | 1,133,782 B, `4cb38419eb58…` |

Also fetched and read (checks, not row sources): RoGS s19 data-tables workbook
`rogs-2026-partg-section19-homelessness-services-data-tables.xlsx` (432,933 B, `8c93012b5ac4…`),
the RoGS s19 chapter page, the ABS media release *Nearly 1 in 200 people homeless on Census night
in 2021* (267,510 B) and the ABS copyright/creative-commons statement. Cubes 2, 3, 4, 5, 7 and 8
(sex/age by state, SA2/SA3/SA4, Indigenous status, remoteness — 85,609–327,822 B) were downloaded
and their sheet structure checked; they are not written because the assignment's store interface
only defines `au_state` and `au_lga` entities, and squeezing SA4 rows into `kind='au_lga'` would
mislabel geography.

Licences: ABS content is **CC BY 4.0 International** (ABS "Copyright and creative commons"
statement, excepting the Coat of Arms/logo/Census branding); Productivity Commission material is
**CC BY 4.0 International** ("Copyright" page). Both recorded on the `source` rows.

## Verified

Five value sets re-read **live** on 2026-11-19 (fresh HTTP GET with `force=True`, not the cache)
and compared with the rows this ingestor wrote.

| # | Source re-read live | Value checked | Live | `metric` row | Match |
|---|---|---|---|---|---|
| 1 | ABS cube 1, table 1.3 (`20490do001_2021.xlsx`) | NSW "Total homeless persons", 2021 and 2016 blocks; published national 2021 | 35,011; 37,715; 122,494 | `AU-NSW` `census_homeless_estimate` 2021 = 35,011 and 2016 = 37,715; `Australia` 2021 = 122,494 | ✔ |
| 2 | ABS cube 1, table 1.5 | NT "Total homeless persons" rate 2021; national rate 2021 | 563.6; 48.2 | `AU-NT` `census_homeless_rate_per_10k` 2021 = 563.6; `Australia` 2021 = 48.2 | ✔ |
| 3 | ABS cube 6, tables 6.1 / 6.2 | LGA cells: `Bayside (NSW)` 2021 = 863; `Albury (C)` 2016 = 124; `Rockdale (C)` 2016 = 672 (and the same rows' other operational-group columns) | 863 / 124 / 672 | `AU-LGA-NSW-bayside` 2021 = 863; `AU-LGA-NSW-albury` 2016 = 124 (2021 = 168); `AU-LGA-NSW-rockdale` 2016 = 672 | ✔ |
| 4 | RoGS 2026 s19 dataset CSV | table 19A.21, FY2024-25: NSW clients / Vic expenditure / national expenditure & clients | 67,824; 526.7 $m; 1,754.9 $m; 288,970 | `AU-NSW` `shs_clients` 2025 = 67,824; `AU-VIC` `shs_funding` 2025 = 526.7; `Australia` `shs_funding` 2025 = 1,754.9 and `shs_clients` 2025 = 288,970 | ✔ |
| 5 | Two *different* URLs, as a cross-check on the series rather than on the same file: ABS media release *Nearly 1 in 200 people homeless on Census night in 2021*, and the RoGS s19 chapter page | "more than 122,000 … increase of 5.2 per cent from 2016 … 48 people per 10,000, compared with 50 … in 2016"; "services were provided to 288,970 people" (2024-25); "Recurrent government expenditure on specialist homelessness services in 2024-25 was $1.8 billion (or $64.05 per person)" | 122,494 = 116,427 × 1.052 ✓; rate 48.2 ≈ 48 ✓ and 2016 49.8 ≈ 50 ✓; 288,970 ✓; 1,754.9 $m = $1.8 b, 64.05 $/person ✓ | `Australia` `census_homeless_estimate` 2021/2016, `census_homeless_rate_per_10k` 2021/2016, `shs_clients` 2025, `shs_funding` 2025 | ✔ |

While verifying #3 the live table also showed that ABS prints `Albury (C)` in 2016 and `Albury` in
2021 — the naming change described above is a property of the source, not of this ingestor.

## Coverage

**Census (ABS 2049.0, 2021 release).**

| geography | published rows | captured | years |
|---|---|---|---|
| State/territory of usual residence | 8 of 8 in each of 4 tables (1.3 counts, 1.5 rates) | 8/8 | 2006, 2011, 2016, 2021 (32 count cells + 32 rate cells) |
| National ("Aust." column) | 1 per table per year | 4 count + 4 rate | same |
| Local Government Area, place of enumeration | 547 (2021), 545 (2016) | 547 + 545 = 1,092 LGA-year cells, 100 % | 2016, 2021 |
| SA2 / SA3 / SA4, remoteness, Indigenous status, sex by age | cubes 5, 7, 8 (not written) | 0 % | — |

So: **every published state/territory cell for four Census years and every published LGA cell for
two Census years is stored**, with the restriction that the LGA layer is only published for 2016
and 2021 (ABS publishes no LGA breakdown for 2006/2011 in this release).

**SHS (RoGS s19, AIHW SHSC-derived).** 8 of 8 jurisdictions + national, 5 financial years
(FY2020-21 … FY2024-25) for `shs_clients`, `shs_funding`, `shs_cost_per_client`,
`shs_funding_per_person` (from 19A.1, same years) and `shs_unassisted_requests_daily`
(accommodation only, 19A.11). That is 40 jurisdiction-year cells per client/funding metric and a
complete national series for the five years RoGS prints.

**What is missing, and why**

- **AIHW's own SHS data tables** (the 30-plus workbooks behind the *Specialist Homelessness
  Services annual report*): `www.aihw.gov.au` answers a Cloudflare JS challenge to every path from
  this host (see Blockers). Consequence: no SHS client counts **by LGA/SA4/PHN**, no agency-level
  or bed/outlet counts, no demographic (age/sex/Indigenous/DV) breakdowns, no unmet-need or
  housing-outcome tables beyond the four indicators RoGS republishes. RoGS section 19 is the
  keyless machine-readable substitute and is where all SHS rows here come from.
- **Per-state NASHH/NHHA bilateral allocations**: published as agreement schedules and annual
  reporting, not as a machine-readable table; the responsible departments' hosts are unreachable
  from here (`www.housing.gov.au` does not resolve; `www.dss.gov.au` times out / resets HTTP/2).
  `shs_funding` therefore holds RoGS's recurrent expenditure series, which RoGS frames as the
  NASHH homelessness component, not the bilateral allocation.
- **Census years 2006/2011 for LGAs**, and **all Census geography below LGA** — not published in
  this release's LGA/state cubes (SA2/SA3/SA4 exist in cube 5 but there is no `au_sa4` entity kind
  in the store interface, so writing them as `au_lga` would be a mislabel).
- **Homelessness Australia / state peaks' funding tables**, **Productivity Commission RoGS years
  before 2020-21** (the published dataset carries 5 years per table only): RoGS 2026 is the one
  edition fetched; earlier editions exist but each has its own asset URLs and none were needed to
  meet the brief.
- **LGA population denominators**: needed for per-capita LGA rates; `abs.gov.au`'s "Data by
  Region" host (`databyregion.abs.gov.au`) does not resolve from this machine, and the census cubes
  used here carry no population column, so `geo_city.pop` stays NULL rather than estimated.

## Blockers

Recorded in `BLOCKERS.md` under `## australia`: AIHW (`www.aihw.gov.au`, Cloudflare),
AURIN's AIHW SHSC LGA datasets (internal gatekeeper hostname does not resolve),
`databyregion.abs.gov.au` (no DNS), `www.housing.gov.au` (no DNS) and `www.dss.gov.au`
(Akamai HTTP/2 resets and timeouts).
