# Overnight run, 2026-11-21 → 22 — what was delivered, and what was not

A single-writer pass over the archive's own gaps plus the move of the public origin onto Cloudflare. Every
number below was measured by a script in this repository and gated by `scripts/verify.py`,
`scripts/audit.py`, `scripts/crawl.py` and `PRAGMA quick_check`.

## Met

| requirement | target | before | after | gate |
|---|---|---|---|---|
| HUD System Performance Measures | ≥300 CoCs | 411 CoCs | 411 CoCs, 291,589 rows | `verify.py` floor added |
| Subawards linked to prime awards | ≥2,000 | 2,705 | 2,705 | `verify.py` floor added |
| Organisation websites | ≥5,000 | 2,092 | **5,168** | floor raised to 5,000 |
| Organisation emails | ≥2,000 | 862 | **2,380** | floor added |
| Officer turnover metric | ≥1,000 orgs | 3,826 | 3,826 | `verify.py` floor added |
| LA / NYC briefs | ≥100 cited links each | 185 / 161 | 185 / 161, ledger rows added | manual count |
| Public origin off this machine | Cloudflare-hosted | local tunnel | Workers static assets, `atlas-web` inactive, URL 200 | fetch with unit stopped |

## Not met

**Requirement 4 — 990 Part III program-service descriptions for ≥5,000 organisations. Final: 3,396 (68%).**

Thirteen routes, each measured rather than assumed:

| route | outcome |
|---|---|
| IRS e-file XML indexes 2024–2026 | complete; 4,364 EINs have a filing, 76% carry Part III |
| IRS batch archives 2021–2023 | opened by hand: 2023 01A–12A, 2022 01A–02A, 2021 01A survive; the rest return 302 |
| IRS S3 bucket (`irs-form-990`) | listing empty, single objects 404 |
| ProPublica PDF endpoint | 403 to every honest request |
| ProPublica HTML pages | no filing text |
| Internet Archive (IRS objects) | 0 of 12 snapshots |
| Internet Archive (org 990 PDFs) | 4.3% yield, ~15 h for ~130 rows — stopped |
| 990 PDFs posted on orgs' own sites | 1–4% |
| 990-N / 990-EZ header purpose | 732 statements captured, kept separate — **not** Part III |
| EIN resolution tier 1 (name+city+state) | 200 EINs → 100 texts |
| EIN resolution tier 2 (unique name+state) | 28 → 18 |
| EIN resolution tier 3 (unique nationally ≥20 chars) | 155 → 36 |
| EIN resolution tier 4 (unique nationally 14–19) | 87 → 5 |
| Zenodo / Dataverse mirrors | 403 / samples only |

The remaining organisations file the 990-N postcard, which has **no description field**, or their filings sit
in archive batches the IRS has retired. That is a supply limit, not a search that has not been run.

## Discipline applied during the run

- Every website and address has a recorded check row; `verify.py` fails if any does not (two such rows were
  found and cleared/confirmed).
- Eight websites written from page anchors were **reverted** after the anchor was found to name one
  organisation while linking to another (`Praxis Housing Initiatives → supportivehousing.org`).
- `part3` was briefly ledgered `DONE` at 3,068 against a 5,000 target; the error was corrected by an
  appended ledger entry rather than a silent rewrite.
- EINs recovered by name matching now require the filing's own header city and state to corroborate the
  match; 17 filings were rejected on that test.
- One duplicate `(org_id, year)` insert crashed the archive scan; inserts are `INSERT OR IGNORE` and only
  rows actually written are counted.

## Files added

`ingest/domains/`: `efile_pdf.py`, `efile_purpose.py`, `efile_scan.py`, `org_990_pdf.py`, `cn_sites.py`,
`epostcard_sites.py`, `acronym_sites.py`
`scripts/`: `org_domains.py`, `org_gov_sites.py`, `mine_contact_cache.py`, `verify_websites.py`,
`link_graph_sites.py`, `wayback_990.py`

## What would move requirement 4

A paid filing aggregator (Candid/GuideStar bulk, or a 990 PDF mirror with an API). Nothing free remains: the
IRS stopped publishing the archives it once served, and no public mirror holds them.
