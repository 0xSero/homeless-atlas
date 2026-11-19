# sf — San Francisco money, encampment volume, overdose and institutional capacity

Domain owner: `ingest/domains/sf.py` (`DOMAIN="sf"`). One run makes ~25 cached requests
(`data/raw/sf/`, plus the 13.5 MB Sheriff annual report PDF and its `pdftotext` output), takes
~2 minutes warm, and writes:

| table | rows | what |
|---|---|---|
| `award` (`award_type='local_contract'`) | 1,118 | every executed Supplier Contract of the Dept. of Homelessness and Supportive Housing plus a homelessness/shelter/encampment keyword sweep of other departments: vendor, agreed amount, payments, term |
| `metric` (`entity_type='city'`, `entity_id='San Francisco'`) | 281 | HSH and DPH/Sheriff budget lines per fiscal year, HSH FTE, 311 encampment volume, SFPD "homeless complaint" calls, OCME overdose deaths, jail capacity/population, Jail Health Services volumes, hospital bed counts |
| `facility` | 6 | County Jail #1/#2/#3, County Jail #2 Annex (closed), Laguna Honda, Zuckerberg SF General |
| `doc` | 9 | the six datasets used, the Sheriff's 2025 annual report, and the two ballot measures |
| `source` | 25 | one per distinct URL fetched |

Everything is idempotent (`common.upsert` on `award_key`, `(entity_type, entity_id, metric, year)`,
`(name_norm, state, city)`, `doc.url`): a second run leaves the counts identical.

## The money

**Budget (SF Controller, `xdgd-c79v`).** The Controller's budget dataset carries 4,036 appropriation
lines for `department_code='HOM'` (Dept. of Homelessness and Supportive Housing) across FY2017–FY2028,
and they reconcile to the department's published appropriations. Series written (USD):

| metric | years | FY2024 | FY2025 | FY2026 |
|---|---|---|---|---|
| `hsh_budget_total` | 2017–2028 | 1,079,884,797 | 1,330,187,958 | 1,232,399,581 |
| `hsh_budget_ocoh_prop_c` (fund *SR Our City Our Home Fund*) | 2020–2028 | 469,182,300 | 724,684,150 | 696,274,722 |
| `hsh_budget_general_fund` | 2017–2028 | | | |
| `hsh_budget_grants` | 2017–2028 | | | |
| `hsh_fte` (Budget - FTE, `4zfx-f2ts`) | 2017–2028 | 292 | 278 | 270 |

FY2017 = 434.8 M and FY2021 = 1.43 B: the department roughly tripled in four years, most of the
increase arriving through the Prop C fund.

**Prop C 2018 ("Our City Our Home") and Measure C 2024.** The ballot-proposition database
(`88s2-6ua9`, SF Public Library) records *2018 Measure C — "Additional Business Taxes to Fund
Homeless Services"*, passed 2018-11-06 with 61.3 % yes, described by the publisher as taxes of
"0.175% to 0.69% on gross receipts over $50 million" plus "1.5% on certain administrative offices'
payroll expense", "raising an estimated $250-300 million" a year with no expiration date. That is the
measure whose revenue the *SR Our City Our Home Fund* rows above track.

The same database's newest year is 2024, and its only **2024 Measure C** is the March 2024
"Real Estate Transfer Tax Exemption and Office Space Allocation" (passed, 52.8 % yes) — a
commercial-to-residential conversion tax exemption, **not** a homelessness-funding measure. The
November 2024 ballot is not in that dataset and `sfelections.org` 403s this host (see BLOCKERS.md
`## sf`), so **no 2024 homelessness measure is asserted here**; the rows for it stay out rather
than being guessed.

**Contracts (SF Controller, `cqi5-hm2d`).** 1,118 distinct contracts, $5.89 B of agreed value,
term starts 1999–2026 and ends 2017–2050. One row carries the source's open-ended sentinel
`term_end_date = 2200-12-31`; that single `pop_end` is stored as NULL rather than as a fake date
(the ingestor scrubs the sentinel on every run, and says so in code). `amount_total` = `agreed_amt`, `amount_obligated` = `consumed_amt` (what the
City has actually paid/consumed so far), so the two columns answer different questions and should not
be added together. Largest counterparties by agreed value:

| vendor | contracts | agreed value |
|---|---|---|
| Episcopal Community Services of SF | 82 | 640.2 M |
| Tenderloin Housing Clinic | 35 | 578.8 M |
| Five Keys Schools and Programs | 29 | 403.1 M |
| St Vincent de Paul Society of SF | 15 | 206.2 M |
| Larkin Street Youth Services | 46 | 204.0 M |
| Compass Family Services | 30 | 196.2 M |
| Chicago Title Company | 5 | 188.5 M (master-lease property acquisition) |
| Catholic Charities | 36 | 174.2 M |

**DPH institutional budgets** (`metric`, FY2010–FY2018 for program codes, FY2010–FY2028 for the
hospital operating funds — from FY2019 the Controller stops publishing the program split and reports
one `OPR` "Operating" line per department, so program-level detail ends at FY2018):

- `laguna_honda_operating_budget` 2010–2028 — FY2026 = 604,545,529 (fund name changes from
  *Laguna Honda Operating Fund* to *LHH-Operating Fund* in FY2019)
- `sfgh_operating_budget` 2010–2028 — FY2026 = 2,836,578,488 (fund *General Hopsital Operating Fund*
  [sic] → *SFGH-Operating Fund*)
- `dph_budget_laguna_honda_long_term_care`, `dph_budget_sfgh_acute_hospital`,
  `dph_budget_sfgh_acute_psychiatry`, `dph_budget_sfgh_acute_forensics`,
  `dph_budget_sfgh_emergency_psychiatric`, `dph_budget_mh_long_term_care`,
  `dph_budget_mh_community_care`, `dph_budget_laguna_honda_acute_care` — FY2010–FY2018
- `sheriff_budget_total` 2010–2028, `sheriff_budget_custody` (program `AFC`) 2010–2018

## The street data

**311 encampment reports** (`vw6y-z8j6`, service names *Encampment* / *Encampments*): 1,495 in 2008 →
69,691 in 2019 → 26,682 in 2021 → 45,107 in 2025 → 34,980 in 2026 (year to date). The 2026 drop is
the year not being over, not a policy change. *Homeless Concerns* (`homeless_concerns_311_requests`)
is a separate service name that effectively stops in 2018 (126 cases in 2019, under 200 a year after),
so its series is short and should not be read as a trend.

**SFPD "HOMELESS COMPLAINT"** (`2zdj-bwza`, `homeless_complaint_police_calls`): 53,136 (2015) →
60,405 (2017) → 16,791 (2020) → 9,920 (2022) → 11,259 (2025) → 8,353 (2026 YTD). The 2015–2017
figures are the peak of the pre-pandemic encampment complaint wave; volume has been flat near
10,000/year since 2021.

**OCME overdose deaths** (`jxrr-bmra`, `overdose_deaths`, 2020–2026): 725, 641, 647, **810** (2023),
635, 626, 399 (2026 partial; `data_as_of` 2026-10-26). The 2026 row carries "latest year is
incomplete" in its note. These reconcile exactly with the `health` domain's SF series, which stores
the same numbers under the place GEOID `0667000`; this domain stores them under
`entity_id='San Francisco'` because the brief joins on the city name.

## The institutions

| facility | kind | beds | note |
|---|---|---|---|
| County Jail #1 – Intake and Release | jail | 298 | opened 1994, 425 7th St; average population 33 |
| County Jail #2 – Housing | jail | 392 | opened 1994, 425 7th St; average population 361; "medical and psychiatric observation housing" |
| County Jail #3 – Housing | jail | 1,140 | opened 2005, 1 Moreland Dr, San Bruno; average population 871 |
| County Jail #2 Annex | jail | — | formerly CJ#4, closed 5 Sep 2020 |
| Laguna Honda Hospital and Rehabilitation Center | other (long-term care / SNF) | 780 | opened 1866, DPH; FY2026 operating fund 604.5 M |
| Zuckerberg San Francisco General Hospital | other | 403 general acute | 22 acute psychiatric + 59 skilled-nursing mental health beds in the same infobox; FY2026 operating fund 2.84 B; houses jail wards 7D/7L |

Jail mental-health capacity is reported as **service volume**, because the Sheriff publishes no
psychiatric bed count: 3,680 initial mental health evaluations and 19,075 ongoing mental health
treatment visits in 2025, against 14,276 triage visits, 20,087 MD/NP visits (incl. psychiatry),
876 dental visits, 6,525 STD/HIV tests, 2,743 court-ordered reports and 6,647 release-planning cases
(`jail_*` metrics). ZSFGH's jail wards 7D/7L averaged 433 appointments a month
(`sfgh_jail_ward_monthly_appointments`). Derived totals: `county_jail_beds` = 1,830 and
`county_jail_average_daily_population` = 1,265, both `is_est=1` with the arithmetic in `metric.note`.

## Verified

Seven checks against the live publisher after the ingest run (query URLs given so each can be
re-run), including the two numbers that come out of a PDF rather than a JSON API:

1. **Contract `1000017241`, Tenderloin Housing Clinic — "Master Lease Hotels-CNC & NCNC"**.
   `GET /resource/cqi5-hm2d.json?contract_no=1000017241` returns
   `prime_contractor=TENDERLOIN HOUSING CLINIC INC`, `agreed_amt=303609319`, term
   `2020-07-01` → `2027-12-31`, dept `HOM Homelessness Services`. `award` row matches all four.
   <https://data.sf.gov/resource/cqi5-hm2d.json?contract_no=1000017241>
2. **`hsh_budget_total` FY2026 = 1,232,399,581**.
   `GET /resource/xdgd-c79v.json?$select=sum(budget)&$where=department_code='HOM' and fiscal_year='2026'`
   returns `1232399581`. Metric row matches exactly.
   <https://data.sf.gov/resource/xdgd-c79v.json?%24select=sum(budget)%20as%20amt&%24where=department_code%3D%27HOM%27%20and%20fiscal_year%3D%272026%27>
3. **`hsh_budget_ocoh_prop_c` FY2025 = 724,684,150**.
   Same dataset, `$where=department_code='HOM' and fiscal_year='2025' and fund='SR Our City Our Home Fund'`
   returns `724684150`; metric row matches.
   <https://data.sf.gov/resource/xdgd-c79v.json?%24where=department_code%3D%27HOM%27%20and%20fiscal_year%3D%272025%27%20and%20fund%3D%27SR%20Our%20City%20Our%20Home%20Fund%27>
4. **`overdose_deaths` 2023 = 810** and `data_as_of` 2026-10-26.
   `GET /resource/jxrr-bmra.json?$select=sum(total_deaths),max(data_as_of)&$where=year='2023'` returns
   `810.0` / `2026-10-26T11:15:03.760`; metric `value=810` and the note carries that date.
   <https://data.sf.gov/resource/jxrr-bmra.json?%24where=year%3D%272023%27>
5. **`encampment_311_requests` 2025 = 45,107**. `GET /resource/vw6y-z8j6.json?$select=count(*)&$where=date_extract_y(requested_datetime)=2025 and upper(service_name) like '%ENCAMPMENT%'`
   returns `45107`; metric row matches. Same check on SFPD (`homeless_complaint_police_calls` 2024)
   returned `10798`, matching the stored row.
   <https://data.sf.gov/resource/vw6y-z8j6.json?%24where=date_extract_y(requested_datetime)%3D2025%20and%20upper(service_name)%20like%20%27%25ENCAMPMENT%25%27>
6. **County Jail #3 capacity 1,140 / average population 871** — read from the cached PDF text
   (`data/raw/sf/sfso-annual-report-2025.txt`, section "COUNTY JAIL #3 - HOUSING":
   `1 Moreland Dr | Opened: 2005 | Capacity: 1140 | Average Jail Population: 871`).
   **Discrepancy, recorded not hidden:** the Sheriff's own jail page for County Jail #3 says the
   facility houses "up to 768 people", and the HIFLD-derived row already in `facility`
   ("San Francisco County Jail #5", 768 beds) agrees with the web page. The `sf` row stores the
   annual report's 1,140 and says so in `population_note`.
   <https://sfsheriff.com/sites/default/files/2026-02/ANNUAL%20REPORT%202025%20UPDATED_compressed.pdf>
7. **Laguna Honda = 780 beds** — the Wikipedia article body says "a nonprofit, publicly funded, 780
   bed long-term acute care hospital"; `facility.beds=780` and `laguna_honda_beds=780`.
   <https://en.wikipedia.org/w/index.php?title=Laguna_Honda_Hospital&action=raw>

## Coverage

**Contracts: ~97 % of the HSH universe, ~0 % of other departments' behavioural-health buying.**
The Supplier Contracts dataset holds 50,195 rows citywide; 1,067 of them are the Dept. of
Homelessness and Supportive Housing's (that is *all* of HSH: the filter takes every HOM row), and the
homelessness/shelter/encampment keyword sweep adds 53 rows from 11 other departments. What is missing
on purpose: DPH's ~92 contracts that mention "SRO", "mental health" or "treatment" (~$1.27 B agreed
value) and PUC/Airport "outreach" hits are a different subject (healthcare procurement and utility
outreach), so they were left out rather than mislabelled as homelessness contracts. Also outside this
dataset entirely: the ~$1.0 B of HSH contracts that have not been executed (pending/proposed) and
grant agreements executed by the Mayor's Office of Housing.

**Budget: complete for HSH, partial for DPH/Sheriff.** Every HOM line FY2017–FY2028 is captured
(department total, Prop C fund, General Fund, grants, 8 program codes, FTE). DPH and Sheriff
program-level detail stops at FY2018 because the Controller's own dataset stops publishing it;
later years are covered at department total (Sheriff) and hospital-fund level (Laguna Honda, SFGH).
Measure C 2024's specific provisions are **not** captured (see BLOCKERS.md `## sf`).

**Encampment/street data: complete for 311 and SFPD from their dataset start.** The 311 series covers
every case with service name *Encampment*/*Encampments* (504,061 cases lifetime) and the SFPD series
every call with final type *HOMELESS COMPLAINT* (327,033 calls). Not captured: DPW/PUC street-cleaning
work orders tied to encampment clean-ups, and the *Tenderloin Tent Count* (DataSF `ubjr-y9qw`,
archived 2025) which would add a physical count per date.

**Overdose: all 7 years the OCME publishes** (2020–2026, latest partial). Pre-2020 SF overdose
deaths are not published by OCME as a machine-readable series; the `health` domain's NCHS county
series covers 2023–2024 only for SF, so a pre-2020 SF series does not exist in this archive.

**Facilities: 4 of the institutions the brief needs, 2 partial.** Jails: all three open county jails
plus the closed CJ#2 Annex (CJ#3 Annex is documented but has no published capacity, so no row).
Hospitals: Laguna Honda (beds + budget), ZSFGH (beds + psych/SNF split + budget). Missing: a
per-ward psychiatric bed count for ZSFGH (only the 22+59 licensed split), the jail psychiatric
housing bed count (the Sheriff publishes service volume instead), and shelter/detox inventory, which
belongs to the `hic`/`facility` domains.

**Duplicates to be aware of.** Two HIFLD-sourced rows already existed for these jails under other
names ("San Francisco County Jail #2", 392 beds — same facility as *County Jail #2 - Housing*;
"San Francisco County Jail #5", 768 beds — the same building as *County Jail #3 - Housing*). The `sf`
rows are named as the Sheriff's own report names them, and the HIFLD rows were **not** re-pointed at
this domain's `source_id`, per the contract rule that a domain never rewrites provenance of rows it
did not create. A later de-duplication pass should merge them on `(name_norm, state, city)` or on
address.
