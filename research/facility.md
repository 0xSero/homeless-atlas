# facility — US institutions (state psychiatric hospitals, prisons, large jails)

Domain ingestor: `ingest/domains/facility.py` (`DOMAIN="facility"`). Re-runnable; every fetch is
cached under `data/raw/facility/`, every row carries `source_id`, and derived numbers are written
with `is_est=1` plus the formula in `metric.note`.

## Run

```bash
python3 -m ingest.run facility
# [facility] OK rows=3426 ... facilities=3426 | metrics: beds=2612 staff=120 budget=14 cost_per_bed=137
```

`facility` table: **3,432 rows**, all touched by the current run (`facility_seen` ledger = 3,432),
0 duplicate `(name_norm, state)` groups, 3,161 with coordinates, 2,609 with a bed/capacity count,
1,127 with an opening year, 167 with a closing year, 14 with an annual budget, 119 with a staffing
count, 132 with an annual cost per bed.

| kind | rows | open | closed | partial | unknown |
|---|---|---|---|---|---|
| prison | 1,987 | 1,471 | 243 | 38 | 235 |
| jail | 1,020 | 794 | 31 | 0 | 195 |
| state_psychiatric | 350 | 164 | 67 | 9 | 110 |
| psych_unit | 37 | 34 | 3 | 0 | 0 |
| other | 38 | 22 | 3 | 1 | 12 |
| **total** | **3,432** | **2,485** | **347** | **48** | **552** |

Metrics written: `facility_beds` 2,618 · `facility_staff` 121 · `facility_annual_budget` 14 ·
`cost_per_bed_year` 137 (2 of them directly reported by a source, the rest derived and flagged
`is_est=1`).

## Verified

Five rows re-checked against the live source at the time of writing (query URLs given so each can be
re-run):

1. **Napa State Hospital (CA, state_psychiatric)** — HIFLD Hospitals layer, `NAME LIKE '%NAPA%'`:
   `BEDS=1418`, `STATUS=OPEN`, `OWNER=GOVERNMENT - STATE`, `VAL_DATE=2014-02-10`. Row matches
   (`beds=1418`, `status=open`, owner `GOVERNMENT - STATE`).
   <https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Hospitals/FeatureServer/0/query?where=NAME+LIKE+%27%25NAPA%25%27&outFields=*&f=json>
2. **USP Leavenworth (KS, prison)** — HIFLD Prisons layer, `NAME LIKE '%LEAVENWORTH%'`:
   `CAPACITY=1134`, `TYPE=FEDERAL`, `STATUS=OPEN`. Row matches (`beds=1134`, `status=open`).
   <https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/prison/FeatureServer/0/query?where=NAME+LIKE+%27%25LEAVENWORTH%25%27&outFields=*&f=json>
3. **Danvers State Hospital (MA, closed asylum)** — Wikipedia infobox (`action=query&prop=revisions`,
   page `Danvers State Hospital`): `| opened = 1878`, `| closed = 1992`. Row matches
   (`opened=1878`, `closed=1992`, `status=closed`).
   <https://en.wikipedia.org/w/index.php?title=Danvers_State_Hospital&action=raw>
4. **San Quentin State Prison (CA, prison)** — `List of California state prisons` table
   (design capacity 3,082; article `San Quentin Rehabilitation Center` gives capacity 3,084; HIFLD
   prisons record 2017 reports custody population 3,989). Row stores the table value `beds=3082`,
   `opened=1852`, `operator=California Department of Corrections and Rehabilitation`.
   <https://en.wikipedia.org/wiki/List_of_California_state_prisons>
5. **Marlboro Psychiatric Hospital (NJ, closed state hospital)** — Wikipedia article text: *“in 1995,
   the hospital served an average of 780 adults per day with a staff of 1,157 employees and a total
   budget of $55.5 million (fiscal year 1995).”* Row stores `budget=55,500,000`, `staff=1157`,
   `annual_cost_per_bed=71,154` = budget ÷ 780 average daily residents (`is_est=1`, formula in
   `metric.note`).
   <https://en.wikipedia.org/wiki/Marlboro_Psychiatric_Hospital>

Discrepancy found while verifying: **Men's Central Jail (Los Angeles)** was written with
`beds=5276` (Wikidata P1083) while the live HIFLD record reports `CAPACITY=6750`. Both are sourced;
the two records did not merge because one arrived without a state and the other with `CA`, and the
write order let the lower-priority record land last. Not fabricated, but the HIFLD figure is the one
this atlas should prefer — flagged as a known merge-order defect (see Blockers).

## Coverage

* **54 jurisdictions**: all 50 states, DC, PR, plus GU and MP records carried in HIFLD. No state has
  zero facility rows.
* **Psychiatric institutions**: 350 `state_psychiatric` (164 open, 67 closed with a sourced closure
  year, 9 `partial` = HIFLD lists the site as open but another source records the institution
  closing — i.e. closed-and-repurposed) + 37 `psych_unit` (federal/local government psychiatric
  hospitals). Closed stock includes the classic asylum cohort (Danvers 1878–1992, Camarillo
  1936–1997, Manteno 1930–1985, Mayview 1893–2008, Marlboro 1931–1998, Topeka 1900–2010 …).
* **Prisons**: 1,987 — all state and federal adult institutions in the HIFLD inventory (2,234 state
  + 255 federal in the layer, minus juvenile, minus merges) plus institutions that only appear in the
  per-state `List of … state prisons` tables and in Wikidata.
* **Jails**: 1,020 — county/local jails with reported capacity ≥ 250 plus county jails that only
  appear in Wikipedia categories. Jails below the capacity threshold are intentionally excluded
  ("large jail" scope).
* **Fields**: coordinates 92%, beds 76%, opening year 33%, closing year 5%. Budget/staff/cost per bed
  are the thinnest fields — see below.
* **Still missing**:
  * Per-facility budgets and staffing for most institutions. State DOC / DMH budget documents are the
    only per-facility source and none of the state portals were discoverable without a web search
    engine (see Blockers); Wikipedia article text supplied the 14 budgets and 119 staff counts.
  * Facility-level census/custody counts are stored in `population_note` text (from the HIFLD
    `POPULATION` field) rather than a numeric column, because the schema has no such column.
  * Jails smaller than 250 beds; youth/jvenile institutions (deliberately typed `other`); county
    hospitals without a psychiatric designation.
  * Private psychiatric hospitals are included only when they are government-owned or closed
    (`kind=other` with the operator quoted otherwise) — the domain is public capacity, not the whole
    inpatient market.

## Economics

What a reopened asylum bed costs per year, from sources that are fetchable and re-checkable:

| figure | value | year | basis | source |
|---|---|---|---|---|
| California state hospital system (DSH) | **$340,993 per bed-year** | FY2020-21 | derived: $2.3 B DSH operational expenditures ÷ 6,745 beds of the five open DSH state hospitals | LAO, *The 2020-21 Budget: Analysis of the Department of State Hospitals Budget* — <https://lao.ca.gov/Publications/Report/4154> |
| California prison system (CDCR) | **$105,976 per inmate-year** | FY2019-20 | derived: $13.3 B CDCR expenditures ÷ 125,500 inmates (June 2019) | LAO, *State Correctional Spending Increased Despite Significant Population Reductions* — <https://lao.ca.gov/Publications/Report/4145> |
| Marlboro Psychiatric Hospital, NJ | **$71,154 per resident-year** | 1995 | derived: $55.5 M budget ÷ 780 average daily residents | Wikipedia article quoting the 1995 operating figures — <https://en.wikipedia.org/wiki/Marlboro_Psychiatric_Hospital> |
| Kankakee State Hospital, IL | **$175,844 per patient-year** | 2010 | directly reported (not derived) | Wikipedia article text; `metric.is_est=0` |
| Lebanon Correctional Institution, OH | **$19,867 per inmate-year** | 2005–07 | directly reported (“annual cost per inmate of $19,867.31”) | Wikipedia article text; `metric.is_est=0` |

Reading these together: a **civil state hospital bed costs roughly $150 k–$350 k per year** to
operate at current US staffing and acuity, an order of magnitude above the **$20 k–$110 k** a prison
or jail bed costs. The California figure is the cleanest current benchmark because both the numerator
(system operating budget) and the denominator (open DSH beds) come from the same budget cycle; the
Illinois figure is a single facility's reported cost; the New Jersey figure is the best-documented
example of what a large asylum actually spent per resident before closure, useful as a floor for
“bring the old building back into service” arithmetic.

Caveats that matter for costing out capacity:

* Per-bed cost is dominated by staffing ratios, not by the building. The DV/forensic mix at DSH
  (~90% forensic patients, LAO) is why California sits at the top of the range.
* `annual_cost_per_bed` in `facility` is `is_est=1` for every row that was derived (division of a
  budget by beds, or of the state total by the state bed count); the formula and the source sentence
  are in `population_note` and `metric.note`. Two rows (`Kankakee`, `Lebanon`) carry directly
  reported per-patient costs and are `is_est=0`.
* System-wide averages were applied per facility only where the source is a state-system total; the
  note on those rows says explicitly “statewide system average, not this facility's own cost”.

## Sources used

* **HIFLD Hospitals** (ArcGIS FeatureServer, `TYPE='PSYCHIATRIC' AND OWNER LIKE 'GOVERNMENT%'`) —
  current inventory, beds, owner, status, coordinates, per-record vintage (2014–2020).
* **HIFLD Prisons** (ArcGIS FeatureServer, state/federal adult + county/local with capacity ≥ 250) —
  capacity, custody count, security level, status, coordinates, per-record vintage (2016–2018).
* **Wikidata SPARQL** — psychiatric hospital / state hospital / lunatic asylum / prison / jail
  classes restricted to the US; P571 inception, P576 dissolved, P1083 capacity, P1128 employees,
  P2769 budget, P625 coordinates, P131 admin territory, P137 operator, P127 owner.
* **Wikipedia MediaWiki API** — per-state institution categories (including “Defunct …” subcats),
  article infoboxes and prose (beds, opened/closed, operator, budget, staffing), and the per-state
  `List of … state prisons` tables.
* **California Legislative Analyst's Office** — DSH and CDCR budget analyses (the two derived
  system-wide per-bed figures).

## Blockers / known defects

1. **No general web search available from this environment.** DuckDuckGo, Bing, Startpage, Mojeek,
   Ecosia and public SearxNG instances all return challenge pages or empty results, and the CMS
   `data.cms.gov` / `healthdata.gov` Socrata endpoints block machine access. That is why per-facility
   budgets/staffing are thin: state budget documents and HCRIS cost reports exist but cannot be
   discovered or fetched without a search surface. `nri-inc.org` sets `ai-input`/`ai-train`
   content-signal reservations in `robots.txt`, so NASMHPD-NRI facility cost tables were not scraped.
2. **`asylumprojects.org` returns HTTP 403** to non-browser clients — historic asylum bed counts from
   that source are not in the DB.
3. **SAMHSA N-MHSS/N-SUMHSS public-use files return HTTP 403**, so the national facility frame for
   state psychiatric hospitals is HIFLD + Wikidata + Wikipedia rather than the survey frame.
4. **Shared-DB corruption incident (2026-11-19 09:12 UTC).** `PRAGMA quick_check` on
   `data/atlas.db` returned page-level errors (`invalid page number`, `2nd reference to page`) in the
   `metric` b-tree; ids ≥ 60,000 were unreadable (29,501 of the older metric rows were still
   readable). A subsequent facility run rewrote the affected pages and `quick_check` now returns
   `ok`, but the event shows the DB is being written by several processes concurrently. Recommend
   serialising writers, and keeping the `sqlite3` CLI available for `.recover` (it is not installed
   in this environment).
5. **Merge-order defect**: when one source record arrives without a `state` and another with one, the
   two are not merged and the later write can land the lower-priority values (Men's Central Jail:
   Wikidata 5,276 beds over HIFLD 6,750). Fix: resolve state before merging and re-run `consolidate()`
   after the geographic backfill.
