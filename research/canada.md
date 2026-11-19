# canada — Canada: Everyone Counts point-in-time counts, census shelter costs, federal money

Domain owner: `ingest/domains/canada.py` (`DOMAIN = "canada"`). Three publishers with three
unrelated systems, so the module has one reader per system and never mixes their semantics.

Run: `python3 -m ingest.run canada` → **606 rows in ~11 s** (12 sources, 116 MB of payloads
cached under `data/raw/canada/`). A second run writes the identical rows (upsert on `geoid` for
`geo_city`, on `(entity_type, entity_id, metric, year)` for `metric`) — 606 rows again.
Selftest: `python3 -m ingest.domains.canada --selftest`.

## What is stored

| metric | entity_type | entity_id | years | publisher / table |
|---|---|---|---|---|
| `pit_total`, `pit_sheltered`, `pit_unsheltered` | `ca_city` | `CA-<prov>-<slug>` | 2018, 2021, 2023, 2024, 2025 | see the three community sources below |
| `pit_total`, `pit_sheltered`, `pit_unsheltered`, `pit_transitional` | `canada` | `Canada` | 2016, 2018, 2022, 2024, 2025 | HICC Everyone Counts national reports |
| `pit_total_panel72` | `canada` | `Canada` | 2022, 2024, 2025 | HICC 2025 report, the 72-community restated panel |
| `pit_encampments`, `pit_institutional` | `canada` | `Canada` | 2024, 2025 | HICC 2024 / 2025 reports |
| `households_total_2021census`, `households_paying_30pct_plus_shelter_cost`, `share_paying_30pct_plus_shelter_cost` | `canada` | `Canada`, `Canada:<Province>` | 2021 | StatCan table 98-10-0252-01 (2021 Census) |
| `reaching_home_action_research_funding` | `canada` | `Canada` | 2022 | HICC Reaching Home programme page (Budget 2022 money) |

`geo_city`: **101 rows**, `kind='ca_city'`, `geoid='CA-<prov>-<slug>'`, `state` = province /
territory. 74 come from Annex A of the HICC 2024 report (the participating communities), the
British Columbia count areas add the rest. `pop`, `lat`, `lon` are NULL — no source fetched here
publishes a population or a coordinate for these count areas, and nothing is estimated.
Each row keeps the name its publisher printed, so where two publishers name the same count area
differently there are two `geoid`s rather than a merged one: `Metro Vancouver` (HICC Annex A) vs
`Greater Vancouver` (BC reports), `Victoria` (Annex A) vs `Greater Victoria` (BC),
`Charlottetown` + `Summerside` (Annex A, two communities) vs `Charlottetown – Summerside` (the
single combined count area HICC publishes in its 2024 results table). The BC reports' own
"+/− change from previous count" column is what proves two of those names are one count area (see
below), but neither publisher uses one name for the others, and inventing one would be a
fabricated label — so a consumer joining on `name_norm` must apply that mapping itself.

Counts in `metric`: 431 `ca_city` rows (146 `pit_total` values across 67 communities) and 67
`canada`-level rows.

### The three community-level sources are not interchangeable

1. **HICC "Everyone Counts 2024: Community Enumeration Results"** (Homelessness Learning Hub
   page, table "Number of people who experienced homelessness on the night of the count") — the
   only national *table* of per-community PiT results HICC publishes. 39 of the 40 listed
   communities have values (Toronto's row is published as "–"); the page itself says it covers
   "only locations that are common to all participating communities" and only communities that
   chose to publish, so it is **not** the 74-community enumeration total. Stored as
   `pit_total` = the sum of the three published components (col. "All Unsheltered Locations",
   "Sheltered Locations", "Provisionally Accommodated in Transitional Housing"), `pit_sheltered`
   = the shelters column, `pit_unsheltered`, and `pit_transitional`, each with that caveat in
   `note`.
   *Caveat on access*: `homelessnesslearninghub.ca` answers HTTP 403 (Cloudflare) to this host, so
   the page is read from the Internet Archive snapshot
   `https://web.archive.org/web/2026/https://homelessnesslearninghub.ca/library/resources/everyone-counts-2024-results/`
   — that URL is what the `source` row records.

2. **Homelessness Services Association of BC / BC Housing / Ministry of Housing, "Report on
   Homeless Counts in B.C."** — the only Canadian jurisdiction that prints per-community PiT
   results as a table, for three cycles, and (in the 2020/21 report) the 2018 total per
   community. 2018: 23 communities, 2020/21: 25, 2023: 27, 2024/25: 32.
   The count-area names change between vintages and the module normalises only where the report
   itself proves the identity, using its own "change from previous count" column:

   | printed 2020/21 | printed 2023/24 | proof |
   |---|---|---|
   | Duncan 129 | Cowichan Valley 230, "+101 from previous count" | 230 − 101 = 129 |
   | Metro Vancouver 3,634 | Greater Vancouver 4,821, "+1,187" | 4,821 − 1,187 = 3,634 |
   | Parksville/Qualicum 87 | Parksville/Qualicum Beach 103, "+16" | 103 − 16 = 87 |

   Cycles are stored under the cycle's final year: the "2020/21" count → `year=2021`, the
   "2024-25" count → `year=2025`, each with the cycle named in `note`.

3. **HICC national reports** — national enumeration per cycle. 2016 and 2018 are the
   full-participation figures; 2022 is the 72-community figure the 2020-2022 report prints and
   that the 2025 report restates; 2024 is the sum of the three components the 2024 report prints
   for its 74 communities ("nearly 60,000"); 2025 is the sum of the components the 2025 report
   prints for its 75 communities ("over 65,000"). Because the participation set changes between
   cycles, the 72-community panel series (`pit_total_panel72` + the 2022/2024/2025
   `pit_sheltered`/`pit_unsheltered`/`pit_transitional` rows) is stored alongside: 40,713 →
   66,494 → 64,740, which is the only apples-to-apples national trend. Every row's `note` says
   which basis it is on.

### Statistics Canada

Table **98-10-0252-01** "Shelter-cost-to-income ratio by tenure: Canada, provinces and
territories, census metropolitan areas and census agglomerations" (2021 Census) is published as a
48 MB zip holding a 1.37 GB CSV. The ingestor streams the CSV out of the cached zip and keeps only
the `Number of private households` rows (the file also carries confidence-interval rows with the
same labels — those are skipped explicitly), for Canada and the 13 provinces/territories.
`share_paying_30pct_plus_shelter_cost` is the ratio of the two published counts, flagged in
`note`. Canada: 14,744,570 households, 3,074,715 spending 30%+ of income on shelter (20.85%).

Shelter-cost **spending** (`shelter_cost_30pct_plus` per community / CMA) is deliberately not
written: the CMA/CA member names in that table (`Vancouver (CMA), B.C.`) do not match the count
areas here (`Greater Vancouver`), and a name-matching join would be a guess.

## Sources (all keyless; every response cached in `data/raw/canada/`)

| what | URL | bytes |
|---|---|---|
| HICC, Everyone Counts 2016 highlights | https://housing-infrastructure.canada.ca/homelessness-sans-abri/reports-rapports/pit-counts-dp-eng.html | 68,602 |
| HICC, Everyone Counts 2018 highlights | …/reports-rapports/pit-counts-dp-2018-highlights-eng.html | 49,957 |
| HICC, Everyone Counts 2020-2022 results | …/reports-rapports/pit-counts-dp-2020-2022-results-resultats-eng.html | 133,616 |
| HICC, Everyone Counts 2024 highlights part 1 (HTML + PDF) | …/reports-rapports/pit-counts-dp-2024-highlights-p1-eng.html, …/alt-format/pdf/…/pit-counts-dp-2024-highlights-p1-en.pdf | 40,056 + 327,783 |
| HICC, Everyone Counts 2025 results | …/reports-rapports/everyone-counts-2025-tout-monde-compte-eng.html | 76,528 |
| HICC, Reaching Home programme information | …/homelessness-sans-abri/prog-info-eng.html | 23,496 |
| HICC community enumeration results (HLH page, Internet Archive snapshot) | https://web.archive.org/web/2026/https://homelessnesslearninghub.ca/library/resources/everyone-counts-2024-results/ | 182,267 (the snapshot the crawler serves varies slightly between requests) |
| HSABC/BC Housing, 2020/21 Report on Homeless Counts in B.C. | https://hsa-bc.ca/_Library/2020_HC/2020-21-BC-Homeless-Counts.pdf | 2,758,555 |
| HSABC/BC Housing, 2023 Report on Homeless Counts in B.C. | https://hsa-bc.ca/_Library/2023_HC/2023-BC-Homeless-Counts.pdf | 6,140,772 |
| HSABC/BC Housing, 2024-2025 Report on Homeless Counts in B.C. | https://hsa-bc.ca/_Library/2024_25_HC/2024-2025_Report_on_Homeless_Counts_in_BC.pdf | 15,648,978 |
| Statistics Canada, table 98-10-0252-01 (full-table CSV zip) | https://www150.statcan.gc.ca/n1/tbl/csv/98100252-eng.zip | 48,589,259 |

### Two transport quirks worth recording

* `housing-infrastructure.canada.ca` serves an **incomplete TLS chain** — the leaf
  `secure.infc.gc.ca` is issued by `Entrust OV TLS Issuing RSA CA 2`, which the server does not
  send, so every request fails `CERTIFICATE_VERIFY_FAILED` with a stock CA bundle (OpenSSL:
  `unable to verify the first certificate`). `_gc_ca_bundle()` fetches that intermediate from the
  CA's own `http://crt.sectigo.com/EntrustOVTLSIssuingRSACA2.crt`, appends it to the certifi roots
  in `data/raw/canada/ca_bundle_gc.pem`, and points `REQUESTS_CA_BUNDLE` at it for the ingest
  process only. Nothing outside `data/raw/canada/` is touched.
* The same host times out intermittently (it took two attempts per page in this sweep); `fetch`
  retries with backoff.
* There is **no machine-readable per-community PiT dataset on open.canada.ca**: org `infc` holds
  24 datasets, none of them PiT results, and a CKAN `package_search` for "everyone counts",
  "point-in-time", "Reaching Home" returns nothing relevant. The per-community layer exists only
  as the HLH table and as individual community PDFs.

## Verified

Five values re-read **live** on 2026-11-19 (fresh HTTP GET, not the cache) and compared with the
rows this ingestor wrote:

| # | Source re-read live | Value checked | Live | `metric` row | Match |
|---|---|---|---|---|---|
| 1 | HICC, Everyone Counts 2025 (`everyone-counts-2025-tout-monde-compte-eng.html`) | 72-community panel 2025 total / shelters / unsheltered; 2023-24 panel total | 64,740 / 36,576 / 17,583; 66,494 | `Canada` `pit_total_panel72` 2025 = 64,740; `pit_sheltered` 2025 = 36,576; `pit_unsheltered` 2025 = 17,583; `pit_total_panel72` 2024 = 66,494 | ✔ |
| 2 | HSABC, *2023 Report on Homeless Counts in B.C.* (table 2.2, row "Greater Vancouver") | sheltered / unsheltered / total | 3,360 / 1,461 / 4,821 | `CA-BC-greater-vancouver` 2023 = 3,360 / 1,461 / 4,821 | ✔ |
| 3 | HSABC, *2024-2025 Report on Homeless Counts in B.C.* (table 3) | Vernon, Nelson, Princeton (shel./unshel./total) | 114/85/199, 26/56/82, 0/20/20 | identical in `pit_sheltered` / `pit_unsheltered` / `pit_total` for year 2025 | ✔ |
| 4 | HICC community enumeration results table (HLH snapshot) | Calgary, Regina, Halifax, Iqaluit, Abbotsford (unsheltered / sheltered / transitional) | 804/1540/193, 543/186/83, 151/838/0, 5/68/6, 215/187/10 | `pit_unsheltered` / `pit_sheltered` / `pit_transitional` = the same three numbers; `pit_total` = their sum (2,537 / 812 / 989 / 79 / 412) | ✔ |
| 5 | Statistics Canada table 98-10-0252-01 — zip re-downloaded live (48,589,259 B, byte-for-byte the cached size) and the CSV re-read | Canada, tenure = total, "Number of private households" | total 14,744,570; ≥30% of income on shelter 3,074,715 | `households_total_2021census` = 14,744,570; `households_paying_30pct_plus_shelter_cost` = 3,074,715; share 20.85% | ✔ |

While verifying #4 the live table also showed Toronto as `–`/`–`/`–`, which is why Toronto has a
`geo_city` row (from Annex A) but no 2024 count.

## Coverage

**Per-community PIT counts: 67 communities, in 10 provinces/territories**, of which **32 have two
or more count cycles and 25 have three or more** (British Columbia count areas).

| province | communities with a count | cycles |
|---|---|---|
| British Columbia | 33 | 2018, 2020/21, 2023, 2024/25 |
| Ontario | 16 | 2024 |
| Alberta | 6 | 2024 |
| New Brunswick | 3 | 2024 |
| Manitoba | 3 | 2024 |
| Nova Scotia | 2 | 2024 |
| Saskatchewan | 1 | 2024 |
| Prince Edward Island | 1 | 2024 |
| Nunavut | 1 | 2024 |
| Newfoundland and Labrador | 1 | 2024 |

Fraction of the universe captured: HICC says **75 communities** enumerated in 2025, **74** in 2024
(Annex A, 60 of them Reaching Home Designated Communities) and 72 comparable in 2020-2022; Reaching
Home funds **64 Designated Communities** plus 30 Indigenous Homelessness communities, 3 territorial
capitals and rural/remote allocations. So:

* community *identifiers*: 101 `ca_city` rows vs 74 in the 2024 Annex A → the 2024 participating
  universe is fully enumerated as places, and British Columbia count areas that Annex A does not
  name are added;
* community *counts, 2024 cycle*: 39 of the 74 (53%) — the HLH table only covers communities that
  published their enumeration publicly; the other 35 are in local PDFs only;
* community *counts for more than one cycle*: 32 of 67 (48%), all in BC;
* national series: complete for five cycles (2016, 2018, 2020-22, 2024, 2025).

**What is missing, and why**

* Quebec: did not participate in the 2024 enumeration at all (13 counts in 2022 per the HICC 2024
  report), and its 2020-2022 results "do not include unsheltered homelessness (in either PiT
  count)" — so no Quebec community rows, by design rather than oversight.
* Ontario/Saskatchewan/Alberta 2023 and 2025 community-level numbers: published only inside each
  community's own PDF report; HICC aggregates them and never republishes a table. Parsing ~40
  independent PDFs with a different layout each was out of scope for this sweep and would risk
  transcription errors.
* Per-community **Reaching Home dollar allocations**: not reachable in bulk (see BLOCKERS.md). The
  only money captured is the Budget 2022 Action Research on Chronic Homelessness line ($18.1 M over
  three years) printed on the programme page.
* Census **shelter cost per community / CMA**: table 98-10-0252 has it, but its geography names
  (`Vancouver (CMA), B.C.`) do not join to the count areas; a name-matched join would be a guess.
  Only Canada + provinces are stored.
* **Hidden homelessness**: HICC prints hidden-homelessness findings from the PiT survey
  (couch-surfing, "staying with others") only as percentages by region/overnight location in the
  2020-2022 results report; StatCan's own hidden-homelessness work is published as articles, not
  tables. No hidden-homelessness number is stored rather than store a percentage as a count.
* 2025 enumerations of the 75 communities: HICC has published the national figures only; the
  community table for 2025 was not published at the time of this sweep (the HLH page is current
  as of 4 November 2025 and covers 2024).

## Blockers

`www.homelesshub.ca` (Canadian Observatory on Homelessness) began aborting connections from this
host during the sweep — its per-community profiles were to add ~40 one-cycle counts outside BC.
`homelessnesslearninghub.ca` refuses this host outright (HTTP 403, Cloudflare). Both are recorded
in `BLOCKERS.md` under `## canada`.
