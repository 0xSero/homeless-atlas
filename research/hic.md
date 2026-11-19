# hic — HUD Housing Inventory Count: shelter and housing capacity per CoC

Domain owner: `ingest/domains/hic.py` (`DOMAIN="hic"`).
Tables written: `metric` (only — `entity_type='coc'` and `'us'`), plus `source` rows for the three
URLs it reads. No other table is touched, and no `source_id` is set on rows this domain did not
create.

Run: `python3 -m ingest.run hic` — 2.5 s, idempotent (re-run produced the identical 133,005 rows).

This is the capacity half of the PIT/HIC pair: `hud_pit` gives *how many people are homeless* per
CoC-year, `hic` gives *how many beds exist* per CoC-year, by program type.

## Sources (both live, re-verified 2026-11-19, HTTP 200)

| What | URL | Size | Notes |
|---|---|---|---|
| **HIC counts by CoC, 2007–2025** (primary) | `https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-HIC-Counts-by-CoC.xlsx` | 2,598,803 B | `.xlsx` (not `.xlsb` — the HIC file is Excel 2007+ while the PIT file is xlsb). 19 year sheets + a `Revisions` sheet. 36–77 columns per year. |
| **HIC project-level inventory, 2025** | `https://www.huduser.gov/portal/sites/default/files/xls/2025-HIC-Counts-by-State.csv` | 11,334,278 B | One row per HUD-listed project × inventory period × bed type: 30,012 rows, 386 CoCs, 92 columns. Used for the project count and for verification (below). |
| Index page (link discovery) | `https://www.huduser.gov/portal/datasets/ahar/2025-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html` | 72,984 B | Lists all four 2007–2025 AHAR data files. The older `/2024-…` page still serves `2007-2024-HIC-Counts-by-CoC.xlsx` if a historical snapshot is ever needed. |

Dead ends, so nobody repeats them:

* `https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-HIC-Counts-by-CoC.xlsb` → **404**
  (the HIC file is only published as `.xlsx`; the `.xlsb` name only exists for PIT).
  `…/2007-2024-HIC-Counts-by-CoC.xlsb` → 404 as well.
* `hudexchange.info` 404s the default ingest user-agent; every request here sends a browser UA
  (same public files, edge rule only) — same finding as `research/hud_pit.md`.
* `openpyxl` is required (already installed, 3.1.5). `pyxlsb` is **not** needed for this domain.

## Layout of the workbook (this is the part that breaks naive parsers)

* The header row is row **1 in the 2025 sheet** and row **2 in 2007–2024** (those sheets carry a
  section-label row above the header). The parser finds the first row whose first cell is
  `CoC`/`CoC Number`.
* HUD renames columns every few years and reorders blocks; every column is therefore resolved by a
  *normalised header string* with an ordered candidate list (`COLS` in the module), e.g.
  ES year-round beds is `Total Year-Round Beds (ES)` (2017–2025), `Total Year-Round ES Beds`
  (2007–2016) or `Total Emergency Shelter (ES) Beds (excluding seasonal+O/F)` (2011–2013 — the same
  year-round figure, since seasonal and overflow beds are not year-round).
* `Total Year-Round Beds (ES,TH,SH)` in **2013** is really `(ES,TH,RRH,SH)` — the per-CoC totals are
  therefore *computed from the per-program columns*, never taken from the group column.
* One footnote row (`MO-604a`, HUD's cross-state CoC) is normalised to `MO-604`; the `Total` label
  row and blank rows are skipped.

## Metrics written

All rows are `entity_type='coc'`, `entity_id=<CoC number>`, `year=<HIC year>`, plus a national
`entity_type='us'` / `entity_id='US'` sum per metric per year (`is_est=0`, note says it is a sum).

| metric | what | years |
|---|---|---|
| `hic_beds_es` / `_th` / `_sh` / `_psh` / `_rrh` / `_oph` | published year-round beds per program | 2007– / 2007– / **2008–** / 2007– / **2013–** / **2014–** |
| `hic_beds_dem` | HUD "demonstration" projects (folded into OPH afterwards) | 2014–2016 only |
| `hic_beds_total` | sum of the programs above | 2007–2025 |
| `hic_beds_dv` | domestic-violence beds = published program total − published non-DV total | 2007–2025 |
| `hic_beds_youth` / `hic_beds_veteran` | dedicated youth / veteran beds (ES,TH,SH) | **2017–2025 only** |
| `hic_beds_chronic_psh` | dedicated chronically homeless PSH beds | 2007–2013, 2017–2025 (HUD's 2014–2016 sheets have no such column) |
| `hic_beds_seasonal_es` / `hic_beds_overflow_es` | ES seasonal / overflow beds | 2007–2025 |
| `hic_units_family_es/_th/_sh/_rrh/_psh/_oph` | published units **for households with children only** | 2007– (SH/RRH/OPH later; SH only 2014–) |
| `hic_beds_per_homeless` | **derived**: `hic_beds_total` ÷ `pit.total`, same CoC-year | 2007–2025, 7,418 CoC-years |
| `hic_projects` | HUD-listed residential project-inventory records in the 2025 file | 2025 only |

`is_est=0` for a single published cell, for plain sums of published cells, and for `hic_beds_dv`
(exact partition of two published cells, re-verified at project level — see Verified);
`is_est=1` only for `hic_beds_per_homeless`, which divides by the PIT count.

### Why the units series is called `hic_units_family_*`

HUD's HIC publishes **no all-household unit count**: the only unit columns are
`Total Units for Households with Children (<program>)`. Putting those in a metric called
`hic_units_es` would read as "all ES units" and understate reality by an unknown factor, so the
name says what it is. (Same trap `hud_pit` documented for `pit.households`.)

### How DV beds are derived (and why that is exact)

HIC lists, per program, a **total** and a **non-DV** year-round bed count; the difference is the
domestic-violence inventory. That is checked, not assumed: in the 2025 project-level inventory file
the rows with `Target Population = 'DV'` sum **exactly** to that difference for all 386 CoCs and for
every program type (386/386 for ES, TH, SH, RRH, PSH and OPH; 386/386 in total). Note the
`Victim Service Provider` flag is *not* the DV marker (it means "does not report to HMIS"): using it
gives only 216/386 agreements.

## Two things that cost time (worth knowing)

1. **`Inventory Type` in the project-level file must be filtered to `C`.** The file carries two
   inventory periods per project — `C` (29,374 rows) and `U` (638 rows) — and the `U` rows are not
   what HUD counts. Summing everything gives ES 411,308 beds nationally instead of 402,719 and
   disagrees with the workbook for 65 CoCs; filtering to `C` reproduces the workbook's per-CoC,
   per-program totals **exactly** for all 386 CoCs.
2. **Cells are integers, `0`, `'.'`, or empty.** `common.to_int` maps `'.'`/empty to `None`; `0` is
   kept as a real published zero (a CoC with no safe-haven beds legitimately has `0`).

## National picture the ingested series show

| year | ES | TH | PSH | all programs (`hic_beds_total`) | DV | PIT homeless | beds per homeless person |
|---|---|---|---|---|---|---|---|
| 2007 | 211,451 | 211,205 | 188,636 | 611,292 | 58,638 | 647,258 | 0.94 |
| 2013 | 238,708 | 185,332 | 284,298 | 730,376 | 56,693 | 590,364 | 1.24 |
| 2019 | 291,837 | 95,446 | 369,293 | 911,657 | 57,796 | 567,715 | 1.61 |
| 2025 | 402,719 | 83,957 | 408,167 | 1,202,224 | 68,090 | 745,652 | **1.61** |

(2025 national ratios: `hic_beds_total` 1,202,224 ÷ `pit.total` 745,652 = 1.61.) The CoC-level
ratio in 2025 ranges from 0.15 to 7.49, mean 1.88 — i.e. the *mix* matters far more than the
national average: some CoCs have nearly eight beds per homeless person, others almost none.

## Verified

Five rows re-read **at cell level from a fresh HTTP re-fetch** of the live workbook
(`fetch(..., force=True)`, `data/raw/hic/2007-2025-HIC-Counts-by-CoC.xlsx`, sheet = year, A1
coordinates 1-based) and compared to the `metric` rows this ingestor wrote:

| # | CoC | Year | metric | Cell | File value | `metric` row | Match |
|---|---|---|---|---|---|---|---|
| 1 | MA-505 | 2020 | `hic_beds_sh` | `H154` | 12 | 12 | ✔ |
| 2 | MN-501 | 2015 | `hic_units_family_es` | `S214` | 84 | 84 | ✔ |
| 3 | TX-500 | 2022 | `hic_beds_es` | `F342` | 2,315 | 2,315 | ✔ |
| 4 | IA-501 | 2013 | `hic_beds_dv` | `F116` − `O116` = 1,053 − 684 | 601 | 601 | ✔ |
| 5 | PA-603 | 2020 | `hic_beds_per_homeless` | 343 (`hic_beds_total`) ÷ 101 (`pit.total`) | 3.396 | 3.396 | ✔ |
|   | PA-603 | 2025 | `hic_beds_psh` | `BE319` = 160, and 160 by summing the live project-level CSV (PSH, `Inventory Type='C'`) | 160 | 160 | ✔ |

URLs used for those checks:
`https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-HIC-Counts-by-CoC.xlsx` (all cells)
and `https://www.huduser.gov/portal/sites/default/files/xls/2025-HIC-Counts-by-State.csv` (row 6).

Whole-file checks that also passed (printed in the run notes every time):

* 2025 workbook vs the independent project-level inventory file, CoCs agreeing **exactly**:
  ES 386/386, TH 386/386, SH 386/386, RRH 386/386, PSH 386/386, OPH 386/386, DV 386/386.
* `hic_beds_total` equals the sum of the program columns for every row by construction, and the
  national sums of `hic_beds_es` … `hic_beds_oph` in 2025 (402,719 / 83,957 / 2,309 / 160,505 /
  408,167 / 144,567) match the per-program totals recomputed from the project file.
* Re-running the ingestor leaves the table at 133,005 rows (no duplicates, `metric`'s
  `UNIQUE(entity_type, entity_id, metric, year)` does its job).
* Every `hic_beds_dv` cell is ≥ 0 (a negative difference would mean a non-DV count above the total;
  none occurs, and the code would drop such a cell to the run notes instead of writing it).

## Coverage

**Universe: HUD's CoC list.** 387 CoCs exist in the CoC boundary layer (`geo_coc`, from
`coc_geometry`); **386 of them have HIC rows for 2025** (`hud_pit` also has 386 for 2025 — the two
domains agree CoC-for-CoC, zero difference). The one exception is **AS-500 American Samoa**, which
has a CoC and no published HIC in any year (same as its missing PIT count).

| | count |
|---|---|
| years | 19 (2007–2025, every year HUD published) |
| CoC-year rows (`hic_beds_es`) | **7,818** |
| distinct CoCs over all years | **484** (413 have a `geo_coc` row; 71 are historical CoC codes HUD has since consolidated, e.g. AR/MD/MA/NY codes that appear only in the 2007–2012 sheets) |
| CoCs in the newest year | **386** (accept threshold was ≥380) |
| program types with bed series | 11 (ES, TH, SH, PSH, RRH, OPH, DEM, DV, youth, veteran, chronic-PSH) + seasonal/overflow + 6 unit series |
| `hic_beds_per_homeless` rows | 7,418 of 7,818 CoC-years (**94.9 %**) |
| `hic_projects` | 386 CoCs / 29,374 project-inventory records (2025) |

### What is missing, and why

1. **400 CoC-years have beds but no ratio** because that CoC has no PIT count that year (HIC and PIT
   always publish the *same* CoC list, but a handful of historical CoCs appear in one file only).
   Breakdown by year: 2007:68, 2008:59, 2009:59, 2010:56, 2011:41, 2012:37, 2013:29, 2014:24,
   2015:12, 2016:9, 2017:4, 2018:1, 2021:1 — all small/historical CoCs, nothing interpolated.
2. **No all-household unit counts exist in HIC at all** (see above), so `hic_units_family_*` is the
   only honest unit series. Total beds are the right capacity denominator, units are not published.
3. **Dedicated veteran/youth beds only from 2017** — HUD's 2007–2016 HIC sheets have no dedicated
   veteran/youth columns, so `hic_beds_youth` / `hic_beds_veteran` are NULL (not 0) for 2007–2016.
   `hic_beds_chronic_psh` is NULL for 2014–2016 for the same reason (column absent).
4. **Safe Haven (SH) does not exist before 2008** in HUD's file, and **RRH/OPH** appear only from
   2013/2014 — those NULLs are HUD's universe changing, not a gap in this parse.
5. **HMIS participation rates** (`HMIS Participation Rate…` columns, and `Total HMIS Year-Round
   Beds`) were deliberately not ingested: they are a data-quality measure, not capacity, and would
   triple the row count. They are in the same workbook if a future domain wants
   `hic_hmis_participation`.
6. **Per-CoC DV split by program type** is derivable (published total − published non-DV, per
   program) but was shipped as a single `hic_beds_dv` per CoC-year to keep the metric table
   proportionate; the per-program computation is in the module if a consumer needs it.
7. **Only the 2025 project-level inventory** (`2025-HIC-Counts-by-State.csv`) is ingested. The same
   file exists for earlier years on the corresponding AHAR pages — that would give DV beds and
   project counts for every year, and is the obvious next extension.
8. **State-level HIC rollups are not written**; the per-CoC rows make them a `GROUP BY` away (and
   `entity_type='us'` national sums are already there).

## Reproduction

```bash
cd /home/ser/projects/homeless-atlas
python3 -m ingest.run hic                 # 2.5 s, 133,005 metric rows
python3 -m ingest.domains.hic --selftest   # per-metric counts + 3 random rows
```
