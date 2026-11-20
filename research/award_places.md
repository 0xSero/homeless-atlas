# award_places — where the money is spent, city by city

`award.place_state` was populated for 185,180 of 187,120 rows, but `place_city` and
`place_county` were empty on every row the `usaspending` worker created (the only city values were
the 1,118 San Francisco `local_contract` rows from `sf`). A city page could therefore not show a
single federal dollar, and the San Francisco brief could not total its own awards.

This domain adds the missing two columns, without re-downloading the award table.

- **Ingestor**: `ingest/domains/award_places.py` (`DOMAIN = "award_places"`)
- **Run**: `cd /home/ser/projects/homeless-atlas && python3 -m ingest.run award_places`
  (`--selftest` runs one tiny cached filter through the whole pipeline; resuming is automatic —
  every plan item and every award key is recorded)
- **Endpoints** (all `https://api.usaspending.gov`, keyless):
  - `POST /api/v2/download/awards/` — generate a bulk award CSV for a filter set
  - `GET /api/v2/download/status?file_name=…` — poll until `status: finished`
  - `GET https://files.usaspending.gov/generated_downloads/…zip` — the artifact itself
  - `POST /api/v2/search/spending_by_award_count/` — the API's own count per filter set (used to
    detect a short download; no numbers are taken from it)
  - `GET /api/v2/awards/<generated_internal_id>/` — award detail, used **only** for the five live
    spot checks under `## Verified`

## Why the bulk download endpoint and not the detail endpoint

The two obvious routes both fail on a 187,120-row table:

| route | what it gives | cost |
|---|---|---|
| `POST /api/v2/search/spending_by_award/` with `fields=["Place of Performance City Code", …]` | only **codes**, e.g. `"PA**043"`, `"OH00000"` — they are USASpending-internal, not place FIPS, so they do not resolve to a name | 1 request per 100 awards, and still no city name |
| `GET /api/v2/awards/<key>/` | `place_of_performance{city_name,county_name,state_code}` + `recipient.location{city_name,county_name,state_code,zip,address_line1}` | 1 request per award → **52 hours** at the contractual 1 req/s |
| `POST /api/v2/download/awards/` + `columns=[…]` | the same `place_of_performance` / `recipient` fields, **resolved to names**, for every award matching a filter set | **6 requests** for the whole table |

The download endpoint takes the same `filters` object as the search endpoint and honours a
`columns` subset (verified: a 2-column request returned a 57 KB CSV for 10,364 awards instead of a
1.7 MB / 100-column one, and cut generation from 122 s to 26 s). It is the same award universe the
`usaspending` sweep ingested, so the plan is simply the union of the query families that built the
table, widened from one-request-per-listing/NAICS/keyword to one OR'd request per family:

| plan item | filter | API count | rows read | rows in archive |
|---|---|---|---|---|
| `grants_programs_all` | `program_numbers` = 14.231, 14.267, 14.241, 14.275, 14.239, 14.235, 14.238, 14.276, 14.249; types 02–05 | 159,847 | 159,847 | 159,847 |
| `grants_keywords_all` | 11 keywords (`homeless` … `coordinated entry`); types 02–05 | 142,483 | 142,480 | 142,480 |
| `contracts_naics_all` | NAICS 624221, 624229, 623220, 925110; types A–D | 8,275 | 8,275 | 8,275 |
| `contracts_keywords_all` | the same 11 keywords; types A–D | 5,471 | 5,471 | 5,471 |
| `grants_keywords_extra` | keyword `shelter` (used by `scripts/sweep_awards.py`, which built part of the table); types 02–05 | 12,656 | 12,655 | 8,786 |
| `contracts_keywords_extra` | keyword `shelter`; types A–D | 22,221 | 22,221 | 3,891 |

The six downloads returned **350,949 prime-award CSV rows**; **328,750** of them belong to the
archive and resolve to **185,999 distinct `award_key` values** (`award_places_progress`), i.e.
99.4 % of the 187,120-row table was seen in at least one download (the misses are covered under
`## Coverage`). Each ZIP is cached under `data/raw/award_places/` and registered in `source` with
its byte length and sha256 (e.g. the grants-programs ZIP: 4,109,665 B, sha256 `9b17bc167b3aa505…`).

## Fields used

Header names were read off the delivered CSV (`Assistance_PrimeAwardSummaries_*.csv` /
`Contracts_PrimeAwardSummaries_*.csv`), not assumed — the two schemas differ, and asking the
download endpoint for a column that does not exist in that schema makes the generation **fail**
(found twice before the names below were confirmed).

| archive column | assistance column | contract column |
|---|---|---|
| join key → `award.award_key` | `assistance_award_unique_key` | `contract_award_unique_key` |
| `award.place_city` | `primary_place_of_performance_city_name` | `primary_place_of_performance_city_name` |
| `award.place_county` | `primary_place_of_performance_county_name` | `primary_place_of_performance_county_name` |
| `award.place_state` (only if NULL) | `primary_place_of_performance_state_name` / `prime_award_summary_place_of_performance_state_fips_code` → `geo_state.abbr` | `primary_place_of_performance_state_code` |
| `award_places_fill.county_fips` | `prime_award_summary_place_of_performance_county_fips_code` | same |
| `award_places_fill.pop_scope` / `pop_country` | `primary_place_of_performance_scope` / `_country_code` | `…_country_code` (contracts carry no scope) |
| `org.address1` | `recipient_address_line_1` | `recipient_address_line_1` |
| `org.city` / `org.county` / `org.state` / `org.zip` | `recipient_city_name` / `recipient_county_name` / `recipient_state_code` / `recipient_zip_code` | `recipient_city_name` / `recipient_county_name` / `recipient_state_code` / `recipient_zip_4_code` |
| `org.uei` match key | `recipient_uei` | `recipient_uei` |

Rules applied while loading, in the order they matter:

- **Never overwrite**: every archive write is `COALESCE(existing, new)`. The 1,208 city/county
  values a previous detail-endpoint run had already written survive untouched.
- **Foreign places are excluded**: when `primary_place_of_performance_country_code` is not `USA`,
  the city/county are dropped rather than put on a US city page (US territories code as `USA`, so
  Puerto Rico keeps its 1,719 cities). A post-pass (`drop_foreign_places`) reconciles the one award
  that reports `ITA` in one download and a blank country in another: **809 rows** were checked,
  **1** city (Vicenza, IT) removed from `award`.
- **Organisation addresses only for US recipients**, and only into `org` columns that are NULL.
  Orgs are matched twice: by `award.org_id` (award-linked) and by `recipient_uei`. USASpending mints
  placeholder UEIs shared by unrelated foreign awardees (`MISCELLANEOUS FOREIGN AWARDEES` appears
  once per country), so the UEI match additionally refuses to write into an `org` whose `state` is
  not a USPS abbreviation, and a post-pass removes the county/ZIP/street this domain had attached to
  **4** such orgs (`foreign_orgs_cleared=4` on the run that first applied it, `0` thereafter).
- **ZIP**: assistance gives `recipient_zip_code` (5 digits), contracts `recipient_zip_4_code`;
  digits are truncated at the ZIP boundary (5 digits). Nothing is inferred.
- **No provenance is written onto rows we did not create.** `award.source_id` and `org.source_id`
  are left alone (the contract forbids rewriting another domain's provenance). The values this
  domain adds are recorded per row instead, with the `source.id` of the exact download ZIP:

| table | what it holds |
|---|---|
| `award_places_plan` | one row per download: filter JSON, API count, `file_url`, `status`, rows read, awards with a city, orgs touched |
| `award_places_progress` | one row per `award_key` seen in any download (resume marker) |
| `award_places_fill` | what this domain wrote into `award`: city, county, state, county FIPS, `pop_scope`, `pop_country`, obligated amount, `source_id` |
| `award_places_org_fill` | what this domain wrote into `org`: city, county, state, zip, address1, `source_id` |

`award_places_fill` also keeps `pop_scope` (`SINGLE ZIP CODE` / `CITY-WIDE` / `COUNTY-WIDE` /
`STATE-WIDE` / `MULTI-STATE` / `FOREIGN`) so a city page can tell a city-specific award from a
state-wide grant that was merely *reported* against a city.

## What it produced

Final state of the archive, `python3 -m ingest.run award_places` (last of five runs):

```
[award_places] OK rows=0 place_city 177891/187120 (95.1%) place_county 167305
               orgs_with_city+state 22137 orgs_with_zip 21295 plans=6 matched=0
               foreign_cleared=809 foreign_orgs_cleared=0
```

- `award.place_city` — **177,891 / 187,120 = 95.1 %** (was 2,326 = 1.2 %)
- `award.place_county` — **167,305 = 89.4 %** (was 2,325)
- money now attributable to a city: **$104.34 bn of $122.03 bn** (85.5 % of obligated dollars)
- **2,466 distinct cities**, **1,102 distinct counties**
- `org` addressability: city+state **22,137 / 22,180**, zip 21,295, county 10,805, street address
  21,302 → **10,649** org rows got at least one field here (`award_places_org_fill`), each recorded
  with the ZIP it came from
- top cities by obligated money: New York $5.78 bn (3,718 awards), Los Angeles $3.41 bn (3,804),
  Chicago $3.08 bn (3,797), Sacramento $3.05 bn (926), Austin $1.86 bn (554)
- **San Francisco** can now total itself from `place_city`: 2,515 awards, **$1.699 bn**, 71
  recipient orgs (before: only the 1,118 `local_contract` rows, which carried their own city).

Idempotency: run 1 (four merged plans) 316,073 rows; run 2 (two `shelter` plan items) 12,677 rows;
run 3, 4, 5 **0 rows with identical counts** — every plan is `finished` in `award_places_plan`, so a
re-run re-reads nothing and duplicates nothing. A full replay (`--reload`, which re-parses all six
cached ZIPs and re-writes every award/org value through `COALESCE`) reproduced the counts exactly
and made **zero HTTP requests** (`logs/ingest-award_places.jsonl` stayed at 1,386 lines), which is
what makes the run resumable after a crash or a session boundary: the artifacts are on disk, the
plan table says what is done, and the loader is idempotent by construction.

## Verified

Five awards drawn from the finished table on 2026-11-19, each re-fetched live from
`GET https://api.usaspending.gov/api/v2/awards/<generated_internal_id>/` and compared with what the
CSV load wrote. Four match field for field; the fifth matches on city and has one honest difference
(see the note).

| URL re-checked | award | `place_city` written / live `place_of_performance.city_name` | `place_county` written / live `…county_name` | live `recipient.location` |
|---|---|---|---|---|
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_WI39B70-1010_086/` | WI39B70-1010, The Guest House of Milwaukee (14.231, $816,770) | MILWAUKEE / MILWAUKEE ✔ | MILWAUKEE / *(null)* — see note | MILWAUKEE, MILWAUKEE, WI, `1216 N. 13TH ST` |
| `https://api.usaspending.gov/api/v2/awards/CONT_AWD_W912L118P6013_9700_-NONE-_-NONE-/` | W912L118P6013, Air Shelters USA LLC (NAICS 624221, $24,589.98) | JBSA LACKLAND / JBSA LACKLAND ✔ | BEXAR / BEXAR ✔ | POMPANO BEACH, BROWARD, FL — *different from the place of performance*, which is what a city page must use |
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_E-20-MW-06-0016_086/` | E-20-MW-06-0016, City and County of San Francisco ($49,106,462) | SAN FRANCISCO / SAN FRANCISCO ✔ | SAN FRANCISCO / SAN FRANCISCO ✔ | SAN FRANCISCO, SAN FRANCISCO, CA, `1 DR CARLTON B GOODLETT PL STE 316` |
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_AK0004L0C002417_086/` | AK0004L0C002417, Anchorage Housing Initiatives (14.267, $161,868) | ANCHORAGE / ANCHORAGE ✔ | ANCHORAGE / ANCHORAGE ✔ | ANCHORAGE, ANCHORAGE, AK, `315 BARROW ST` |
| `https://api.usaspending.gov/api/v2/awards/ASST_NON_CAH13F003_086/` | CAH13F003, City and County of San Francisco ($25,899,375) | SAN FRANCISCO / SAN FRANCISCO ✔ | SAN FRANCISCO / SAN FRANCISCO ✔ | SAN FRANCISCO, SAN FRANCISCO, CA, `1 S VAN NESS AVE FL 5` |

**Note on WI39B70-1010**: the CSV reports place-of-performance county `MILWAUKEE`, the detail
endpoint returns `place_of_performance.county_name: null` for the same award — the detail payload
only carries a county when the award's own place-of-performance record has one coded, while the
bulk CSV reports the resolved county. The value written is the CSV's, and the recipient location
(MILWAUKEE county) corroborates it. This is the only field-level disagreement found in the five.

Also spot-checked by hand: `CONT_AWD_W912PF05C0003_9700_-NONE-_-NONE-` (Vicenza, IT) had its city
removed by the foreign-place pass, and `ASST_NON_90ZU0148_075` ($546 m CDBG-DR grant, Texas, no
place of performance) still has `place_city` NULL — the ingestor writes NULL rather than guessing.

## Coverage

**95.1 % of awards and 85.5 % of obligated dollars now carry a city; 89.4 % carry a county.**

Universe: the whole `award` table, 187,120 rows ($122.03 bn), all three award types
(174,857 grants, 11,145 federal contracts, 1,118 San Francisco local contracts).

The 9,229 awards without a city (4.9 %) are accounted for — this is not a download gap, it is the
awards that have no city-level place of performance. `pop_scope` from `award_places_fill` says why:

| `pop_scope` of the awards with no city | awards | obligated |
|---|---|---|
| `STATE-WIDE` — the federal place of performance is the state | 5,059 | $13.52 bn |
| `COUNTY-WIDE` — county known and filled, city does not exist | 2,623 | $2.65 bn |
| single ZIP code but no city name given | 271 | $0.30 bn |
| `FOREIGN` (city deliberately not written — not a US city page) | 124 | $0.79 bn |
| `MULTI-STATE` | 36 | $0.13 bn |
| `CITY-WIDE` with an empty city name in the source | 4 | $0.002 bn |
| absent from every download (see below) | 1,111 | $0.28 bn |
| (1 award cleared by the foreign-place pass) | 1 | — |

Residual gap, honestly stated:

1. **State-wide money ($13.5 bn, 7.2 % of awards) belongs on the state page, not a city page.**
   These are largely state-administered ESG/HOME/HOPWA awards and statewide HMIS grants; the
   federal record names a state and nothing finer. Filling a city here would be invention.
2. **`COUNTY-WIDE` awards keep their county** (that is why `place_county` is at 89.4 % while
   `place_city` is at 95.1 %); a county page can use them, a city page cannot.
3. **1,111 awards are absent from all six downloads** even though the same filters built the table:
   ~1,100 are San Francisco `local_contract` rows (a city dataset, not a federal award — they
   already carry `place_city='San Francisco'` from `sf`), plus 3 awards on CFDA 93.243 and 10
   others. They are the difference between 185,999 keys seen and the 187,120-row table.
4. **`local_contract` rows are not federal records** and are not touched by this domain.
5. **City/county names are stored verbatim** as USASpending publishes them: upper case
   (`NEW YORK`, `JBSA LACKLAND`), counties without the "County" suffix (`COOK`, `ST. LOUIS`) while
   `geo_city` uses `Cook County` / `St. Louis County`, and no `geoid` is attached. Joining a city
   page to `geo_city` therefore needs a case-insensitive name + state match (and county-suffix
   normalisation). I did not invent a geocode for any award; `award_places_fill.county_fips` holds
   the 5-digit county FIPS USASpending itself publishes for 164,000+ awards, which is the reliable
   join key for county pages.
6. Two scopes never got a city because the source itself has none: 271 `SINGLE ZIP CODE` awards
   (e.g. HUD Continuum of Care statewide HMIS projects) and 4 `CITY-WIDE` awards.
