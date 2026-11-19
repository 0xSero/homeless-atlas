# laws — the legal landscape per state and per large city

Ingestor: `ingest/domains/laws.py` · table: `law` · run with `python3 -m ingest.run laws`
(idempotent: upsert on `jurisdiction, citation, title`; resumable: per-task progress in
`laws_progress`, pass `{"force": 1}` to redo everything).

## What is stored

One row per (jurisdiction, provision). `jurisdiction` is `US`, a two-letter state/DC
abbreviation, or `"<City>, <ST>"` for the municipal survey rows. `topic` uses the schema
vocabulary (`camping_ban, sit_lie, property, drug, mental_health_hold, encampment_clearance,
funding, housing_first, other`). `status` is `enacted | enjoined | repealed | proposed`, or
`NULL` where the source does not state it. `citation` is the statute section when the source
prints one, otherwise a pinpoint locator into the table the value was read from. `url` +
`source_id` point at the cached response.

## Sources (all cached under `data/raw/laws/`, 1 req/s via `common.fetch`)

| Source | Gives | Rows | Licence / terms |
|---|---|---|---|
| Treatment Advocacy Center (TAC) state pages, 50 states + DC — `tac.org/map_directory/<state>/` | per-state statute text and section numbers: emergency evaluation, inpatient commitment, outpatient/AOT commitment, TAC's recommended amendments | 200 | statutory text is public record; page reproduced with attribution (no explicit licence published, robots.txt allows) |
| NHLC, *Housing Not Handcuffs 2021: State Law Supplement*, appendix matrix | 51 jurisdictions × 12 statewide restriction categories | 146 | public advocacy report, cited and linked |
| NHLC, *Housing Not Handcuffs* (2019), Appendix A "Prohibited Conduct Chart" (2016 data) | 187 surveyed cities × 11 municipal ordinance categories | 735 | as above |
| NHLC, *One year since Grants Pass* (May 2025 update) | 2024-25 statewide bill status per state, CA Housing First mandate + AB 20, federal Housing Not Handcuffs Act | 48 | as above |
| NLIHC, *Successfully Fighting Against State and Local Homelessness Criminalization Bills* | named bills with numbers: GA SB 62, KS HB 2430, MO HB 1606 (struck down) / SB 1336, IN & IA bills | 15 | quoted with attribution |
| NLIHC Housing Trust Fund Project page (`nlihc.org/housing-trust-fund-project`) | the three kinds of state legislation that support local housing trust funds, with statutes (A.R.S. 11-381, RCW 84.52.105, Act 137, Vt. Stat. Ann. tit. 18, SB1533, H3509, …) | 16 | quoted with attribution |
| Cornell LII: *City of Grants Pass v. Johnson* (No. 23-175), 24 CFR 578.7, 42 U.S.C. § 11302 | federal frame: Eighth Amendment holding + the Ninth Circuit rule it displaced, CoC written standards, the statutory definition of "homeless individual" | 3 | public record |
| NLIHC "Housing First" issue page | the only sourced statement about Housing First adoption (context row, no status) | 1 | quoted with attribution |

## Parsing notes and guards

* **Both criminalization matrices are checksum-gated.** The 2021 state appendix and the 2019
  city chart are parsed by clustering the character offset of every printed `x` into columns
  (11-12 columns), then reconciling each column against the report's own printed Total row.
  If the parse does not reproduce `1/3/4/15/6/4/16/24/36/6/24/7` (state) or
  `34/50/61/93/88/73/60/100/50/114/12` (city) exactly, **no rows are written** and the run
  reports the mismatch. Both pass: 146 of 146 state marks, 735 of 735 city marks, 51 and 184
  jurisdictions.
* **TAC headings are classified defensively.** Several states title the middle section
  "Inpatient **and** outpatient commitment law"; matching `outpatient` first silently
  attributed the inpatient statute to the AOT row (caught on WA/WI/WV/WY in an earlier build).
  `inpatient` is now tested before `outpatient`. 5 states (SC, SD, VT, WI, WV) publish no
  separate outpatient/AOT section at all — they get no AOT row rather than a guess.
* **Statute citations are recovered from several formatting styles** (they are printed
  verbatim by TAC, nothing is normalised upward): `ALA. CODE § 22-52-91(a)`,
  `N.Y. MENTAL HYG. LAW § 9.27`, `DEL. CODE ANN. tit. 16 § 5001(8)`,
  `MASS. GEN. LAWS ANN. ch. 123 § 12(a)`, `VT. STAT. ANN. tit. 18 § 7504(a)`,
  `S.D. CODIFIED LAWS § 27A-10-1`, and Illinois' `405 ILL. COMP. STAT. 5/3-701(a)` which has
  no section sign at all. Codes are trimmed against a token whitelist so prose such as
  "Criteria for Inpatient Commitment" does not leak into the citation field.
* **A wrong assumption was caught and fixed:** the first build cited 24 CFR 578.7 as the
  federal *Housing First* mandate. Part 578 does not contain the phrase at all (the section
  text and the whole part were fetched through the eCFR API and grepped: 0 hits). The row now
  states what § 578.7 actually requires (CoC written standards) and notes that the Housing
  First requirement arrives through HUD's annual CoC NOFO, which is not reproduced here.
  Same discipline removed a hard-coded `1987` year on the § 11302 row (no source-credit line
  on the page → `year = NULL`).
* Years are only written when the fetched text supports them (enactment verbs near a year,
  and only when the paragraph names a single such year).

## Result

```
law rows:      1164
jurisdictions: 236   (51 states + DC + US, and 184 cities)
(jurisdiction, topic) cells: 814
jurisdictions with >=5 rows:   119
jurisdictions with >=5 distinct topics: 39
```

| topic | rows | jurisdictions |
|---|---|---|
| other (loitering/vagrancy, AOT, general measures) | 311 | 189 |
| camping_ban | 271 | 168 |
| panhandling | 237 | 181 |
| mental_health_hold | 102 | 51 |
| sit_lie | 94 | 94 |
| property (vehicle habitation) | 77 | 77 |
| encampment_clearance | 36 | 21 |
| drug (substance use as a commitment basis) | 17 | 17 |
| funding (state housing trust funds) | 16 | 14 |
| housing_first | 3 | 2 |

Status split: enacted 1061, proposed 89, enjoined 2 (MO HB 1606 struck down; the Missouri
Cicero statute struck down), NULL 12 (sources that list a bill without saying whether it
passed). Every row carries a citation and a URL; 0 rows have a null/dangling `source_id`.

## Verified

Eight rows re-checked **live** (each URL re-fetched with the disk cache bypassed, then the
stored value searched for in the fresh response). Machine output: `logs/laws-verify.log`.

| # | Row in `law` | Live URL re-fetched | What was confirmed |
|---|---|---|---|
| 1 | AL / mental_health_hold — `ALA. CODE § 22-52-91(a)` | https://www.tac.org/map_directory/alabama/ | the citation string and the quoted statutory text are on the page (282 kB response) |
| 2 | WY / mental_health_hold — `WYO. STAT. ANN. § 25-10-109(a)` | https://www.tac.org/map_directory/wyoming/ | citation present verbatim, in the "Emergency evaluation" accordion |
| 3 | CA / other — `CALIF. WELF. & INST. CODE § 5346(b)` | https://www.tac.org/map_directory/california/ | citation present verbatim in the AOT section |
| 4 | US / camping_ban — Grants Pass | https://www.law.cornell.edu/supremecourt/text/23-175 | the page prints "No. 23–175. Argued April 22, 2024—Decided June 28, 2024" (the row's citation and year) |
| 5 | AZ / funding — `A.R.S. 11-381` | https://nlihc.org/housing-trust-fund-project | the statute and the 2007 enactment sentence are on the page |
| 6 | GA / encampment_clearance — `GA SB 62` | https://nlihc.org/sites/default/files/Successfully-Fighting-Against-State-and-Local-Homelessness-Criminalization-Bills.pdf | "ensured that SB 62 was stripped of its most harmful provisions before it was enacted" |
| 7 | AL statewide matrix rows (6 of them) | https://homelesslaw.org/wp-content/uploads/2022/02/2021-HNH-State-Crim-Supplement.pdf | fresh download + re-extract: the Alabama line carries 6 marks, matching the 6 stored rows |
| 8 | Anchorage, AK city rows (3) | https://homelesslaw.org/wp-content/uploads/2018/10/Housing-Not-Handcuffs.pdf | fresh download + re-extract: the Anchorage line carries 3 marks, matching the 3 stored rows |

## Coverage

**Captured.** All 51 jurisdictions (50 states + DC) get a real per-state research pass: a TAC
page fetch that yields the state's own emergency-evaluation and commitment statutes with
section numbers (102 rows, 51 jurisdictions) plus that state's row in NHLC's statewide
criminalization matrix (146 marks = 146 of 146 printed marks) and, where applicable, its
2024-25 legislative status and its housing-trust-fund legislation. 184 of the 187 cities in
NHLC's surveyed chart carry at least one ordinance row (735 of 735 printed marks). Every
state-level jurisdiction has ≥5 rows (min 5, max 15); 119 jurisdictions overall have ≥5 rows.

**Not captured, and why.**

1. **Statute numbers for camping / sit-lie / panhandling.** NHLC's appendix prints only an
   "x" per cell — no section numbers — so those rows cite the appendix cell
   (`HNH 2021 State Law Supplement, Appendix column 4 (Camping in public — particular public
   places)`) and the PDF URL, and the summary says the section number is not printed by the
   source. Verifying them would mean fetching 50 state codes; Justia, FindLaw, Casetext and
   NCSL all answer 403 from this host, and state legislature sites use 50 different systems.
   This is the single biggest gap in the domain.
2. **Housing First mandates.** Only California is documented (from the 2025 NHLC update, which
   records the state mandate and the defeated AB 20 repeal). NLIHC's Housing First page is
   generic — it names no state statutes — and Part 578 of the CFR does not contain the
   phrase. There is no keyless per-state list of Housing First mandates in the sources
   reachable here, so 49 states + DC have no `housing_first` row rather than a guessed one.
3. **<5 distinct topics for 27 state-level jurisdictions** (NY, OH, UT 2; AL, AR, AK, DC, MD,
   MI, NV, RI, VA, VT, WY 3; CO, CT, DE, IL, KY, LA, MA, ME, ND, NE, NM, SD 4). These are
   states whose NHLC matrix row is blank in the camping/sit-lie/vehicle columns and whose TAC
   statute text does not mention substance use — i.e. the remaining categories have **no**
   statewide statute to cite. Reaching "5 topics for 50 states" would require inventing rows
   for categories the source shows are empty, so it was not done. Aggregate coverage is
   814 (jurisdiction, topic) cells over 236 jurisdictions and 10 topics.
4. **Property storage / belongings seizure.** No reachable source tabulates it per state; the
   `property` topic here is vehicle habitation only (4 statewide rows, 73 cities).
5. **2024-25 legislative status for ~25 states.** The 2025 NHLC update names states
   selectively (21 get a row); states it does not mention have no status row.
6. **Sources found but unusable** (recorded in `BLOCKERS.md`): the NLIHC State & Local Housing
   Trust Fund *database* is a JS application with no table in the HTML; the "Emergent Threats
   Map" data lives in a WordPress custom post type (`igmap`) that the public REST API exposes
   with empty content; the 2025 State & Local Housing Trust Fund *report* PDF is narrative
   with no per-state table; ACLU's post-Grants-Pass ordinance tracker (cited by NHLC) is not
   fetched here.
