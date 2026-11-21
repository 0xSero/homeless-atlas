# spm — HUD System Performance Measures per CoC, FY2015–FY2024

Domain owner: `ingest/domains/spm.py` (`DOMAIN="spm"`).
Tables written: `metric` only (`entity_type='coc'`), plus `source` rows for the two URLs it reads and a
private `spm_progress` table (per-year row counts, for observability — not required, the whole sweep
is one download plus 10 sheets and takes **5 s**).
Run: `python3 -m ingest.run spm` (idempotent: re-run produced byte-identical counts, value sum and
note length — 291,589 rows both times). Selftest: `python3 -m ingest.domains.spm --selftest`.

This is the outcome layer the atlas was missing: for every CoC-year it holds length of time
homeless, exits to permanent housing, returns to homelessness at 6/12/24 months, first-time
homelessness, income growth, PH retention, street-outreach outcomes, **and the FY CoC Program award
amount for that same CoC-year**, so money and outcome can be put side by side.

## Source (one file, re-verified live 2026-11-20, HTTP 200)

| What | URL | Size / hash | Notes |
|---|---|---|---|
| **CoC System Performance Measures Data Since FY 2015** (primary, and the only machine-readable SPM product HUD publishes per CoC) | `https://files.hudexchange.info/resources/documents/System-Performance-Measures-Data.xlsx` | 2,530,373 B<br>`sha256 82796eb0686e3fc53d64ebd6ca4d9a4b2629f313591b7d1ae8d3d3aab43c61af` | 10 sheets named `2015`…`2024`, one row per CoC, 46→96 columns per sheet. Cached at `data/raw/spm/`. Served to a browser UA only (see dead ends). |
| Resource page (discovery + provenance) | `https://www.hudexchange.info/resource/5691/system-performance-measures-data-since-fy-2015/` | 29,468 B | "Date Published: **January 2026**". Its description says "since FY 2019", but the workbook actually carries **FY2015–FY2024** — the page text is wrong, the file is authoritative. |
| Programme page (context, not parsed) | `https://www.hudexchange.info/programs/coc/system-performance-measures/` | 41,191 B | Confirms the workbook is the "raw data (for local analysis purposes)" companion to the published SPM report and the Tableau dashboard. |

Licence recorded on every `source` row: *US Government work — public domain* (HUD Exchange; the file
carries no separate licence statement). Publisher: *U.S. Department of Housing and Urban Development,
HUD Exchange*.

Found via Brave search (`BRAVE_API_KEY` is present in this environment) — the old SPM URLs are gone,
see Dead ends.

## Layout of the workbook — this is what breaks a naive parser

* **Sheet = fiscal year.** `2015`, `2016`, …, `2024`. 403/401/395/393/394/388/388/387/385/386 CoC
  rows respectively.
* **Two header rows in 2015–2023, one in 2024.** In 2019 and 2020 the award column lives in row 2
  while every other name is in row 1 — the module reads *both* rows and splits each column into
  (group label, real name). In 2024 all group labels are gone; only the column names remain.
* **Column count jumps**: 46 (2015–17), 81 (2018), 96 (2019–24). The growth is new measures, not
  renames only (see Coverage).
* **HUD renames columns between vintages** (resolved by normalised text, never by position):
  | 2015 | 2016+ |
  |---|---|
  | `Percent with Successful Exit` | `Percent with Successful  ES, TH, SH, PH-RRH Exit` (note the double space) |
  | `Percent with Successful Retention or Exit` | `Percent with Successful PH Retention or Exit` |
  | `Total Persons Exiting Street Outreach` | `Total Persons Exiting Street Outreach (SO)` |
  | `Total Persons Exited to Temporary Housing` / `… to Permanent Housing` | `Total Persons Exited SO to Temporary Destinations` / `… SO to Permanent Destinations` |
  | `Total Persons Exiting PH (but not including PH-RRH)` | `Total Persons Exiting PH or Remaining in PH at end of reporting period (measure excludes PH-RRH)` |
  | `… Residing in PH for 6mos or more or exiting to Permanent Housing` | `Total Persons Exited PH to permanent destinations or Remained in PH for 6+ mos (…)` |
* **Six identical "Total Persons Returns in N mths" blocks from 2018** (per exiting programme plus
  the all-programme block) — identical header text, so they are resolved *positionally* off the
  anchor `Total Persons Exited (SO|ES|TH|SH|PH|SO+ES+TH+SH+PH)` and the six following columns are
  verified to be `returns 6 / 12 / 24 / pct 6 / pct 12 / pct 24` before they are trusted. FY2015–17
  have only one block, labelled `Total Persons Exited (SO+ES+TH+SH+PH)`.
* **Group labels lie.** FY2015–18 label the first-time-homelessness block `SPM 5`; FY2019+ label the
  HMIS count `SPM 3`. Nothing is mapped by group label — only by column text — so the metric names in
  `metric` describe the quantity, not HUD's measure number of the year.
* **`NA` is a string, not blank**: 12,198 cells `NA`, 113 `N/A`, 2 `#N/A`; the 2015/2016 award column
  also contains `*` (MO-604M, "application not submitted"). All become absent rows, never 0.
* **Negative sentinels**: exactly 4 cells in ten years are HUD's "no data" marker — `-1`
  (NC-516 FY2015 average/median days, NY-511 FY2016 exit universe) and `-534` (NY-511 FY2016 exit
  rate). They are dropped as not-published (and named in the ingest note) rather than stored, so no
  aggregation is poisoned.
* **CoC codes are as published per year.** `MO-604M` (FY2015–22) is physically the same CoC as
  `MO-604` (FY2023–) after HUD's renumbering, and `MO-604K` (Kansas City/Wyandotte County) exists in
  FY2015–16 and again in FY2024. No remapping is done on purpose: remapping would silently move
  published numbers from one entity to another.
* FY2024's sheet is **sorted by award size**, not by CoC code, and one row (`MP-500`) publishes `NA`
  for average days in 2015.

## Metrics written (93 names; 87 published columns + 6 derived)

Entity: `entity_type='coc'`, `entity_id=<HUD CoC number>`, `year=<FY>`. `unit` is `persons`, `ratio`
(published fractions, e.g. 0.377 = 37.7 %), `days`, `beds`, `USD`, `per 100 exits`.
Every row's `note` names the exact source column it came from (`SPM FY2024 workbook, column "…"`,
whitespace-collapsed from the cell). 42 measure columns in FY2015–17, 72 in FY2018, 87 in FY2019–24;
**every metric column in every sheet resolves** (the resolver is asserted non-empty in the code and
prints nothing unmapped — see `_selftest`).

| family | metrics |
|---|---|
| SPM 1 length of time homeless | `spm_avg_days_homeless`, `spm_median_days_homeless` (ES+SH+TH), `spm_avg_days_es_sh`, `spm_median_days_es_sh` |
| bed coverage (the denominator HUD prints beside SPM 1) | `spm_beds_es_th_non_dv`, `spm_beds_hmis_es_th_non_dv`, `spm_bed_coverage_rate` |
| SPM 2 returns | `spm_exits_count`, `spm_return_{6,12,24}mo_count`, `spm_return_{6,12,24}mo_rate`, and the same count/rate per programme `_so _es _th _sh _ph` (2018+) |
| SPM 3 first-time homelessness / universe | `spm_first_time_count`, `spm_first_time_incl_ph_count`, `spm_hmis_count` |
| SPM 4 income growth | `spm_income_{stayers,leavers}_count`, `spm_income_{stayers,leavers}_{earned,other,total}_{count,rate}` |
| SPM 7 exits / retention | `spm_exits_es_th_sh_rrh_count`, `spm_exit_to_ph_count`, `spm_exit_to_ph_rate`, `spm_ph_retention_count`, `spm_ph_retention_success_count`, `spm_ph_retention_rate`, `spm_so_exits_count`, `spm_so_exit_temp_count`, `spm_so_exit_perm_count`, `spm_so_exit_perm_rate` |
| HMIS data quality (2019+) | `spm_hmis_dq_exits_{so,es,th,rrh,psh}`, `spm_hmis_dq_unknown_count_*`, `spm_hmis_dq_unknown_rate_*` |
| money | `spm_coc_award_amount` (USD, FY CoC Program award for that CoC-year) |
| derived, `is_est=1`, formula in `note` | `spm_returns_per_100_exits`, `spm_first_time_rate`, `spm_exit_not_ph_count`, `spm_exit_not_ph_rate`, `spm_award_per_exit`, `spm_award_per_exit_to_ph` |

23,284 rows are `is_est=1`; the other 268,305 are single published cells.

## Verified

**Five CoC-years re-read live** at cell level from the downloaded workbook (read-only `openpyxl`,
sheet = FY, row numbers 1-based Excel rows) and compared against the `metric` rows this ingestor
wrote. The source URL is the same for all five (HUD publishes SPM as one workbook, not per CoC —
`https://files.hudexchange.info/resources/documents/System-Performance-Measures-Data.xlsx`), and it
was re-fetched with `force=True` on 2026-11-20 (HTTP 200, byte-identical to the cached copy):

| # | CoC | FY | Sheet row | Columns re-read (1-based) | File value | `metric` value | Match |
|---|---|---|---|---|---|---|---|
| 1 | **NY-600** New York City CoC | 2024 | 388 | 4 award, 11 ES-SH-TH avg days, 12 ES-SH-TH median, 48 exits, 49 returns 6 m, 50 returns 12 m, 52 return rate 6 m, 70 first-time, 55 HMIS count, 77 exits to PH, 78 exit-to-PH rate | 173,695,665 / 287.5 / 229 / 28,320 / 1,947 / 2,698 / 0.06875 / 90,441 / 195,192 / 38,753 / 0.3772205620394615 | identical | ✔ |
| 2 | **NY-600** | 2015 | 291 | 4, 11, 13 exits (SO+ES+TH+SH+PH), 14 returns 6 m, 35 first-time, 42 exits to PH, 43 exit rate | 121,965,844 / 261 / 25,748 / 4,154 / 60,770 / 48,650 / 0.4358069370789738 | identical | ✔ |
| 3 | **TX-607** Texas Balance of State CoC | 2020 | 349 | 4, 11, 48, 49, 52, 55 HMIS count, 77, 78 | 10,470,238 / 63 / 4,930 / 354 / 0.0718052738336714 / 9,293 / 2,673 / 0.29591497841248754 | identical | ✔ |
| 4 | **WA-500** Seattle/King County CoC | 2017 | 384 | 4, 11, 13, 14, 17 return rate 6 m, 43 exit rate | 34,134,466 / 145 / 4,741 / 289 / 0.060957603881037756 / 0.3188238906801781 | identical | ✔ |
| 5 | **AK-500** Anchorage CoC | 2019 | 3 | 4, 11, 48, 49, 52, 77, 78 | 3,982,876 / 83 / 839 / 107 / 0.12753277711561384 / 641 / 0.1726831896551724 | identical | ✔ |

Whole-table checks that also passed (all reproducible with `sqlite3` against `data/atlas.db`):

* **The cached copy is the live copy**: a forced re-download on 2026-11-20 returned a byte-identical
  file (same 2,530,373 bytes, same sha256), so every number above is from a response that is still
  what HUD serves.
* **Provenance**: all 291,589 rows carry `source_id` 13218 (the workbook URL); `source.bytes` and
  `source.sha256` are filled for it. Rows this domain did not create were not touched.
* **Returns are monotone**: 0 CoC-years where returns at 6 months exceed returns at 12 months, and 0
  where 12 exceeds 24 (3,919 CoC-years each).
* **Published percentages reproduce from published counts**: for 3,871 CoC-years,
  `spm_return_6mo_rate = spm_return_6mo_count / spm_exits_count` exactly (≤1e-6) in **every year
  except FY2021**, where 286 of 374 published rates are rounded to two decimals (`0.100000` for
  82/863 = 0.095017, max deviation 0.50 pp). FY2021 percentages in the workbook are rounded; the
  counts are not. `spm_returns_per_100_exits` (derived from counts) is therefore the better number
  for that year.
* **No impossible rows**: 0 CoC-years with exits to permanent housing greater than the exit universe
  (the single such case, NY-511 FY2016, was the `-1` sentinel and is gone).
* **Cross-source**: the workbook's own bed-coverage columns were reconciled against the independent
  HIC file ingested by the `hic` domain for all 3,891 CoC-years present in both: 217 identical to
  `hic_beds_es + hic_beds_th`, 3,556 lower by no more than the CoC's DV-bed count (i.e. exactly the
  non-DV subset HUD claims), 118 inconsistent — 74 of them by more than 5 %+5 beds. The two HUD
  products are *not* the same extract (e.g. CA-600 FY2024: SPM 26,894 non-DV ES+TH beds vs HIC
  26,305 ES+TH; CT-505 FY2015: 2,310 vs 1,919). Whoever uses `spm_beds_*` should know it is HUD's
  SPM-side count, not a copy of the HIC table.
* **Idempotent**: two consecutive runs → 291,589 rows, value sum 25,100,463,112.671, note length sum
  23,484,945 — identical; 0 duplicate `(entity_id, year, metric)` keys.

## Coverage

**Years: 10 — FY2015 … FY2024, every year in the file, no gaps.** FY2024 is the newest SPM vintage
published (resource page dated January 2026); there is no FY2025 sheet yet.

| FY | CoCs | metric rows | measure columns | FY | CoCs | metric rows | measure columns |
|---|---|---|---|---|---|---|---|
| 2015 | 403 | 18,636 | 42 | 2020 | 388 | 34,269 | 87 |
| 2016 | 401 | 18,975 | 42 | 2021 | 388 | 34,389 | 87 |
| 2017 | 395 | 18,684 | 42 | 2022 | 387 | 34,152 | 87 |
| 2018 | 393 | 28,960 | 72 | 2023 | 385 | 34,283 | 87 |
| 2019 | 394 | 34,861 | 87 | 2024 | 386 | 34,380 | 87 |

**CoCs: 411 distinct** CoC numbers with at least one SPM year (target was ≥300); 407 have ≥2 years;
371 have all 10. **3,920 CoC-years**, 291,589 metric rows, 93 metric names.

Against HUD's published CoC universe (387 CoCs in HUD's current grantee-area layer) that is **every
CoC that reported SPM**, plus 24 historical/renumbered codes. 9 of the 411 codes have no `geo_coc`
row: `CA-615, CT-502, IL-509, IN-500, LA-508, MO-604K, MO-604M, NY-502, WA-507` (dropped,
consolidated or renumbered CoCs whose SPM history HUD still publishes; `MO-604M` is the pre-2023
spelling of `MO-604`). They keep their metrics and simply have no geometry/population.

### Which measures HUD stopped publishing

**Within this file, none.** Every measure column present in FY2015 is still present in FY2024 — the
only movement is in the other direction:

* FY2018 **adds** the per-programme SPM 2 returns breakdown (`spm_return_*_{so,es,th,sh,ph}`), so
  those metrics have no FY2015–17 values. HUD has not withdrawn it since.
* FY2019 **adds** the HMIS data-quality block (`spm_hmis_dq_*`, 2019+ only).
* FY2015 ends with **14 unnamed, completely empty "SPM 7" columns** (cols 47–60). They were never
  filled and are gone from FY2016. Column names in the file are consistent with HUD never having
  published anything in that block.
* FY2015–17 publish returns only for the all-programme block; the per-programme splits in those years
  do not exist and there is no way to derive them.

What HUD has **never** published per CoC (so these metrics cannot exist, and no number was invented
to fill them):

1. **A count of the people who leave to *homeless* destinations.** The workbook has the exit
   universe, the exits to permanent housing, the temporary/permanent split for street outreach, and
   the count of exits with *unknown* destination — but no homeless-destination column. The nearest
   published quantity, `spm_exit_not_ph_count` (published exits minus published exits to PH), is
   written as `is_est=1` with the formula in its note and includes institutional, doubled-up, other
   and unknown destinations. It is an **upper bound**, and its note says so instead of relabelling it
   "exits to homelessness".
2. Any destination detail beyond permanent/temporary/unknown (doubled-up, jail, hospital, treatment,
   deceased…), which the AHAR reports summarise nationally only.
3. A full system budget: `spm_coc_award_amount` is the **FY CoC Program award** for that CoC-year
   only. ESG, state/local money, philanthropy, Medicaid and HUD's one-off CoC/CARES awards are not in
   it, so it understates what a CoC spends (and it is the award, not expenditure).
4. Individual-level flows: everything here is an aggregate the CoC submitted through HDX, so no
   person-level returns, no episode lengths, no flows between CoCs.
5. FY2025, and pre-FY2015 history (HUD's raw-data publication starts at FY2015 by design).

The published per-CoC **SPM report PDFs** and the **Tableau** dashboard exist too but carry no extra
machine-readable fields — see Dead ends.

## Money vs outcomes — first look (computed from the rows above, 2026-11-20)

National CoC Program award in the same file vs the median CoC outcome:

| FY | award, all CoCs | median exit-to-PH rate | median returns per 100 exits (6 m) | median avg days homeless |
|---|---|---|---|---|
| 2015 | $1.94 B | 0.445 | 6.75 | 101 |
| 2016 | $1.95 B | 0.445 | 6.94 | 96 |
| 2017 | $1.98 B | 0.446 | 7.09 | 92 |
| 2018 | $2.16 B | 0.455 | 7.13 | 88 |
| 2019 | $2.28 B | 0.445 | 7.23 | 92 |
| 2020 | $2.47 B | 0.422 | 7.23 | 103 |
| 2021 | $2.65 B | 0.399 | 7.41 | 104 |
| 2022 | $2.76 B | 0.366 | 6.10 | 100 |
| 2023 | $3.14 B | 0.372 | 6.64 | 100 |
| 2024 | $3.62 B | 0.371 | 6.65 | 102 |

Award money rose **87 %** over ten years while the median CoC's exit-to-permanent-housing rate fell
from 0.445 to 0.371 and the median time homeless came back up to roughly its 2015 level. That is a
statement about medians, not about causes: the SPM exit universe changed (street outreach was folded
into SPM 2 in 2018, PH-RRH programme changes, and FY2020–21 reporting disruption), and inflation and
wage growth are not deflated here.

Cross-sectionally, spending per exit does not order CoCs by outcome (FY2024, 379 CoCs with both a
positive `spm_award_per_exit` and an exit rate, quartiles of award per exit):

| quartile of award per exit | range | median exit-to-PH rate | median returns per 100 exits |
|---|---|---|---|
| Q1 | $64 – $3,253 | 0.375 | 6.61 |
| Q2 | $3,256 – $5,671 | 0.380 | 7.22 |
| Q3 | $5,718 – $9,953 | 0.334 | 6.63 |
| Q4 | $9,958 – $50,698 | 0.386 | 6.25 |

The most expensive systems per exit are not the ones with the best exit rate, and the cheapest are
not the worst — median exit rate is flat at 0.33–0.39 across a 150× range in dollars per exit. Any
site page that claims "spend more, exit more" is contradicted by this table; the honest framing is
that scale (PIT count, bed count) and the cost of local housing markets drive award per exit more than
performance does.

## Dead ends (so nobody repeats them)

* `https://www.hudexchange.info/programs/coc/coc-performance-measurement/` → **404**.
* `https://www.hudexchange.info/resource/4383/system-performance-measures/` → **404** (guessed old
  resource id; the live ones are 5691 = data, 4483 = programming specs, 5793 = national summary).
* `hudexchange.info` answers **404 to the default ingest user-agent**; every request in this domain
  sends a browser UA (same finding as `research/hud_pit.md`, `research/hic.md`).
* Per-CoC SPM **published reports are PDFs only** and named
  `files.hudexchange.info/reports/published/CoC_Dash_CoC_<COC>-<FY>_<ST>_<FY>.pdf` (CoC Dashboard)
  and `CoC_Perf_State_<ST>_<FY>.pdf` (Performance Profile). ~400 CoCs × 10 years ≈ 4,000 PDFs at
  1 req/s > 1 h, and this environment has **no PDF text extractor** (`pdfplumber`, `pypdf`, `PyPDF2`,
  `fitz` all absent), so those routes were not taken; the state-level Performance Profile reports
  would in any case aggregate away the CoC.
* `public.tableau.com/app/profile/system.performance.measures.hud.public.data/vizzes` (linked from
  the resource page) is the only route to the dashboard; no keyless export endpoint was used.
* The **national** SPM summary (`resource/5793`, "National Summary of Homeless System Performance")
  is a PDF; national totals were therefore not ingested (they are not in the workbook either — the
  workbook has no total row, and this domain deliberately does not sum CoCs into a fake `us` row).
* **AHAR appendices** (the assignment's second suggested source): checked
  `https://www.huduser.gov/portal/datasets/ahar/2025-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html`
  (HTTP 200) — its four data files are PIT by CoC, PIT by State, HIC by CoC, HIC by State, the PIT
  veteran workbooks and the raw HIC CSV, **no SPM tables**; and
  `https://www.hudexchange.info/homelessness-assistance/ahar/` — the CoC-level AHAR products there
  are the *Homeless Populations and Subpopulations* PDFs, which contain population counts, not SPM
  measures. Nothing usable was hidden in an AHAR appendix.

Re-run: `python3 -m ingest.run spm` (no arguments, no key, ~5 s, caches in `data/raw/spm/`,
log in `logs/ingest-spm.jsonl`).
