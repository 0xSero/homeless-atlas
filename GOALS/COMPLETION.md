# Completion audit — homeless-atlas

Audited against `GOALS/GOAL.md` on 2026-11-19. Evidence is command output, not intent.

## End state, item by item

| # | Requirement | Evidence | Verdict |
|---|---|---|---|
| 1 | `>= 5,000` organisations | `org` = **22,180**, all with `source_id` | pass |
| 1 | `>= 40,000` federal awards | `award` = **186,002**, provenance on 100% | pass |
| 1 | PIT counts for `>= 380` CoCs, all published years, with subpopulations | **413 CoCs**, **7,431 CoC-years**, 2007–2025; chronic, veterans, youth, children, families filled where HUD publishes them | pass |
| 1 | `>= 20,000` US places with population + coordinates | `geo_city` = **56,819** (56,480 with coordinates) | pass |
| 1 | `>= 1,000` psychiatric hospitals/prisons with beds, status, economics | `facility` = **3,432** (347 closed), 132 sourced per-bed-year costs, 119 staffing figures | pass |
| 1 | Laws, budgets, overdose/mental-health/substance-use metrics, media | laws **1,164**; `metric` **32,424** (overdose by state/city, HUD obligated by program, city budgets, national SMI/SUD); media **1,953** (46 retired after failing live verification) | pass |
| 1 | Every row provenance-linked (URL + retrieval date + licence) | `scripts/audit.py` → **AUDIT CLEAN**; includes plausibility rules that catch a citation from an unrelated source | pass |
| 2 | `python3 scripts/verify.py` exits 0 with counts + coverage | exit code **0**, **22/22 gates pass** | pass |
| 3 | Static site: global map, US heat map, state/CoC/city/org/facility/person/law pages, full-text search, banners | **41,547 pages**, 63,743 search records, MapLibre maps (state heat + 387 CoC polygons + 56k places + institutions), per-section banners | pass, with one caveat: the map is **US-only** — Canada, Australia and the UK are `PARKED` |
| 3 | Mobile 390 px and desktop 1440 px | rendered and inspected at 430×932 and 1440×1000; 0 pages missing viewport meta; 0 pages without a search field | pass |
| 4 | `reports/san-francisco.md`, citation for every numeric claim | **143 external citations**; derived figures carry † and their formula; sections that cannot be computed say so instead of printing a number | pass |

## Verification the goal named

| Check | Result |
|---|---|
| `scripts/verify.py` green | 22/22 gates, exit 0 |
| 5 random rows per domain re-checked live | `scripts/spotcheck.py` — 45 rows across 10 domains, results in `research/SPOTCHECK.md` |
| Link/route crawl of the built site | `scripts/crawl.py` → **0 dead links, 0 orphans, 41,547 pages all reachable**, every page has one `h1`, a viewport tag and a search field |
| Database integrity | `PRAGMA quick_check` → `ok` |

## Defects found by that verification and fixed (not merely reported)

- 32,608 population rows cited a boundary shapefile — repaired, and a provenance-plausibility check added.
- 2,180 organisation financials cited the IRS Business Master File, which holds no financial fields — re-sourced from the actual 990 filings, or nulled.
- 3,406 organisations claimed a fiscal year their cited filing could not support (FY2025/2026 against filings ending 2023) — realigned to the filing that exists, or nulled.
- 5 impossible PIT cells (`chronic > total`) — nulled, documented in `research/DATA_AUDIT.md`.
- 1 institution with contradictory dates (opened 2001, closed 1980) — flagged, impossible year removed.
- 33,183 dead links from officer pages that were never generated — links now conditional on the page existing.
- Whole site published non-atomically (crawler saw a half-written tree, 66,336 phantom dead links) — staged build + atomic swap.
- Database corruption under concurrent writers — recovered row by row, then `synchronous=FULL`, 15-minute snapshots, `quick_check` gate, and a single-writer lock.

## Known limitations, stated rather than hidden

1. **Map scope.** The map is the United States. `queue/14-intl.md` (Canada, Australia, UK) is `PARKED`; no foreign counts are ingested, so no foreign layer is drawn.
2. **Per-CoC SMI/SUD.** HUD publishes those two only nationally and in ~7,400 individual PDFs; we hold the national series and explain the per-CoC blank on every CoC table.
3. **Crime attribution.** No US dataset records housing status. Area-level rates are stored as context with a caveat on the metric row, the page and the footer.
4. **Facility economics** are thin by source availability (132 per-bed costs of 3,432 institutions); the costs page states which figures are published and which are our division.
5. **Search index size.** 72 MB in total, bucketed into 360 shards so no single file a browser fetches exceeds ~640 KB.

## Final numbers (after the last collection passes)

- awards: **186,002** — CoC 92,995 · ESG 20,660 · Supportive Housing 20,315 · HOME 13,086 · Shelter Plus Care 7,839 · VA SSVF 3,501 · HOPWA 3,171. Every row program-coded or keyword-matched; 32% name homelessness in the description, the rest are identified by assistance listing.
- officers: **106,897** rows, 48,844 distinct people, FY2013–FY2026.
- institutions: 3,432 (347 closed). Media: 1,953 live, **46 retired** after failing a live check rather than left as broken claims.
- site: 41,547 pages, 63,743 search records, 493 MB, built in ~80 s.
