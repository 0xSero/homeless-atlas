# health — mental illness, substance use, overdose

Domain owner: `ingest/domains/health.py`. Everything lands in the generic `metric` table
(`entity_type` = `us` | `state` | `city`, `entity_id` = 2-digit state FIPS / 5-digit county
GEOID / 7-digit place GEOID / `US`), one row per (entity, metric, year), each carrying the
`source_id` of the exact dataset URL it came from.

## What was captured

| metric | geography | years | publisher |
|---|---|---|---|
| `overdose_deaths` | 50 states + DC + US | 1999–2016 | NCHS Drug Poisoning Mortality by State (final multiple-cause mortality) |
| `overdose_deaths` | 50 states + DC + US (+ PR, GU) | 2017–2026 | NCHS VSRR provisional, 12-month-ending |
| `overdose_rate` | 50 states + DC + US | 1999–2016 | NCHS **age-adjusted** rate as published |
| `overdose_rate` | 50 states + DC + US | 2017–2026 | crude rate = published provisional count ÷ Census 2024 state population (`is_est=1`) |
| `overdose_deaths` / `overdose_rate` | 2,151 counties (2024) / 2,162 (2023) | 2023–2024 | NCHS VSRR Provisional County-Level counts |
| `overdose_deaths` / `overdose_rate` | San Francisco | 2020–2026 | SF DPH / Office of the Chief Medical Examiner (via DataSF) |
| `overdose_deaths` / `overdose_rate` | 125 Connecticut municipalities (2024), 13 years each where present | 2012–2024 | CT Office of the Chief Medical Examiner (row-level death records) |
| `overdose_deaths` | 32 city-county equivalents (2024) + New York City | 2024 | derived from the NCHS county counts, `is_est=1`, derivation in `note` |
| `depression_prevalence` | 50 states + DC (+ PR, GU, VI) | 2024–2025 | CDC BRFSS, adults ever told they have a form of depression |
| `binge_drinking_prevalence` | 50 states + DC (+ PR, GU, VI) | 2024–2025 | CDC BRFSS, adult binge drinkers |

Row counts on a full run: **15,021 metric rows**, 6 `source` rows, ~23 s warm / ~90 s cold
(the first run pulls ~11 MB of county cells; everything after that is served from
`data/raw/health/`).

**Important reading note on the county cells.** The NCHS county product publishes *12-month
ending* rolling counts, one row per county-month (verified: Kent County DE 2024 reads 102 in
January and 76 in December, which is an annual total, not a monthly one). The annual figure is
therefore the **last month with an unsuppressed cell in that year**, not a sum of months.
Summing the twelve monthly rows multiplies the true annual figure by ~12 — that mistake was
made and corrected in the first version of this file.

**San Francisco via DataSF** republishes the same month several times as OCME revises it; the
ingestor keeps the row with the newest `data_as_of` per (year, month) before summing the 12
months. Verified: 2024 = 635 (12 distinct months).

## What is NOT here, and why (also in BLOCKERS.md `## health`)

`smi_prevalence` (serious mental illness) and `sud_prevalence` (substance use disorder) are
**not written**. The only publisher of those estimates by state is SAMHSA's National Survey on
Drug Use and Health state prevalence tables, and `www.samhsa.gov` answers **HTTP 403** to every
path, host variant and user agent tried from this machine:

- `https://www.samhsa.gov/` → 403
- `https://www.samhsa.gov/data/` → 403
- `https://www.samhsa.gov/data/sites/default/files/reports/rpt42638/NSDUHsaePercents2022/NSDUHsaeExcelPercents2022.xlsx` → 403
- `https://pdas.samhsa.gov/saes/state` → 503 (pdas = the NSDUH state-estimates explorer)
- `https://store.samhsa.gov/` → 202 with an empty body

CDC WONDER (`https://wonder.cdc.gov/`, the query engine normally used for county mortality
counts) is also 403 from here, which is why the county layer uses the keyless Socrata mirror of
the same NCHS VSRR release instead.

Rather than relabel a proxy as SMI, the domain publishes the two BRFSS measures above under
their own metric names. KFF's State Health Facts no longer carries an SMI indicator (checked
2026-11-19: `adults-with-serious-mental-illness*` → 404 under every slug tried), and the
Internet Archive and Common Crawl index (for an archived copy of the SAMHSA workbook) were both
unreachable when this ran.

## Verified

Five (eight) rows re-fetched live from the source, not from the cache, on 2026-11-19:

1. `https://data.cdc.gov/resource/xkb8-kh2a.json?state=US&year=2023&month=December` — "Number of
   Drug Overdose Deaths" = **106,881**; DB `metric(us, US, overdose_deaths, 2023)` = 106881.0 ✓
2. `https://data.cdc.gov/resource/xkb8-kh2a.json?state=CA&year=2024&month=December` = **9,687**;
   DB `metric(state, 06, overdose_deaths, 2024)` = 9687.0 ✓
3. `https://data.cdc.gov/resource/gb4e-yj24.json?fips=6037&year=2024&month=12` — Los Angeles
   County = **1,829**; DB `metric(city, 06037, overdose_deaths, 2024)` = 1829.0 ✓
   (note: the county file publishes county FIPS without the leading zero; the ingestor zero-pads
   to 5 digits so it joins `geo_city`)
4. `https://data.sfgov.org/resource/jxrr-bmra.json?year=2024` — 12 monthly rows sum to **635**;
   DB `metric(city, 0667000, overdose_deaths, 2024)` = 635.0 ✓
5. `https://data.ct.gov/resource/rybz-nyjw.json` — `count(*)` where `residencecity='HARTFORD'`
   and date in 2024 = **100**; DB `metric(city, 0911037070, overdose_deaths, 2024)` = 100.0 ✓
6. New York City 2024: Bronx 682 + Kings 511 + New York 412 + Queens 359 + Richmond 96 =
   **2,060**; DB `metric(city, 3651000, overdose_deaths, 2024)` = 2060.0 ✓
7. `https://data.cdc.gov/resource/dttw-5yxu.json` — Alabama, 2025, Depression/Yes/Crude
   Prevalence = **25.06 %**; DB `metric(state, 01, depression_prevalence, 2025)` = 25.06 ✓
8. `https://data.cdc.gov/resource/xbxb-epbu.json` — California 2016, both sexes / all ages:
   deaths **4,654**, age-adjusted rate **11.2218**; DB = 4654.0 and 11.2218 ✓

Idempotency: ran the domain twice back to back — health-sourced `metric` rows 15,021 before and
15,021 after, 6 `source` rows, no duplicates.

## Coverage

Target universe for this assignment: 50 states + DC for overdose and SMI; ≥20 cities for
overdose; SMI/SUD prevalence by state.

- **Overdose, states: 51/51** — all 50 states + DC carry `overdose_deaths` and `overdose_rate`
  for 1999–2016 (NCHS final) and 2017–2026 (NCHS VSRR provisional). 100 %.
- **SMI, states: 0/51 — blocked.** See BLOCKERS.md. `sud_prevalence` likewise 0/51.
  Substitute state-level measures captured instead: `depression_prevalence` and
  `binge_drinking_prevalence` for 51/51 states + DC (54 jurisdictions incl. PR/GU/VI).
- **Overdose, cities: 159 non-county city entities in 2024 (248 across all years)** — well past
  the 20-city bar. Composition: 125 Connecticut municipalities (town-level OCME data), San
  Francisco (city DPH), New York City (five-county sum), and 32 city-county equivalents
  (Philadelphia, Baltimore, Denver, St. Louis, Richmond, Alexandria, Chesapeake, Carson,
  Danville, Colonial Heights, …) where city and county are the same geography.
- **Overdose, counties: 2,151 of 3,144 US counties for 2024** (68 %) and 2,162 for 2023. The
  remainder are missing because NCHS suppresses every 1-9 cell, so counties with very few
  deaths publish no usable cell at all; nothing is imputed.
- **Overdose rates for cities outside the county-equivalent group are not written**: a city that
  shares a county with other places (Chicago in Cook, Phoenix in Maricopa) would need a
  place-within-county allocation, and that would be a model, not a published number. The county
  value is available on the county page instead.

Still missing / next steps:
1. SAMHSA NSDUH state tables (`smi_prevalence`, `sud_prevalence`, and drug-specific SUD) — needs
   samhsa.gov to stop 403-ing this host, or the workbook dropped in by hand.
2. City medical-examiner counts for the big cities with their own dashboards but no keyless API
   (Chicago/Cook ME case archive, Los Angeles County, King County, Philadelphia, DC, Boston) —
   each needs a bespoke scrape; only SF and CT were reachable as clean tabular APIs here.
3. Finer county years (2020–2022 are in the same NCHS file and could be added by widening
   `step_vsrr_county`'s `years` argument).
