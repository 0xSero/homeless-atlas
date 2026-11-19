# Blockers

Anything that stops a domain, with exactly what unblocks it. Keep this list honest and current.

## Keys that would unlock more (none are strictly required for the keyless core)
- `DATA_GOV_API_KEY` (api.data.gov, free, instant) — SAM.gov entity registrations (UEI -> full registration,
  officer names, address, NAICS) and the FBI Crime Data Explorer at full rate. Without it we degrade to
  USASpending recipients + DEMO_KEY.
- `CENSUS_API_KEY` (api.census.gov, free) — ACS detail tables without hitting the keyless 500/day cap.
- `YOUTUBE_API_KEY` — quota-clean YouTube search. Without it we use `yt-dlp ytsearch`.

## health

**Blocked: SAMHSA NSDUH state prevalence tables — no `smi_prevalence` / `sud_prevalence` rows.**
`www.samhsa.gov` answers HTTP 403 to every request from this host (all user agents tried,
including a browser UA and Googlebot), so the state-level serious-mental-illness and
substance-use-disorder estimates cannot be fetched. Tried and failed on 2026-11-19:

| URL | status |
|---|---|
| `https://www.samhsa.gov/` | 403 |
| `https://www.samhsa.gov/data/` | 403 |
| `https://www.samhsa.gov/data/sites/default/files/reports/rpt42638/NSDUHsaePercents2022/NSDUHsaeExcelPercents2022.xlsx` | 403 |
| `https://pdas.samhsa.gov/saes/state` (NSDUH state-estimates explorer) | 503 |
| `https://store.samhsa.gov/` | 202, empty body |
| `https://library.samhsa.gov/` | reachable, but its search returns no downloadable state-prevalence workbook |

There is **no API key** for this — it is a WAF/IP block, not a rate limit. What unblocks it:
allowlist this host's egress IP, or fetch the current *NSDUH State Prevalence Estimates*
workbook (`NSDUHsaeExcelPercents*.xlsx`, linked from
`https://www.samhsa.gov/data/release/{year}-nsduh-state-prevalence-estimates`) and drop it in
`data/raw/health/`; the ingestor can then parse it into `metric(metric='smi_prevalence'|'sud_prevalence')`
with no other change. Fallbacks checked and unavailable: KFF State Health Facts SMI indicator
(404 under every slug), Internet Archive (offline), Common Crawl index (504).

**Blocked: CDC WONDER (`https://wonder.cdc.gov/`) — HTTP 403.** Used to be the route for
final county-level mortality queries (Multiple Cause of Death, D76/D157). Worked around with the
keyless Socrata mirror of the same NCHS release (`data.cdc.gov/resource/gb4e-yj24`), so
`overdose_deaths` by county is still captured; the only loss is final (non-provisional) county
counts and queries by age/sex/race.

**Not blocked, noted for the next worker:** city medical-examiner dashboards that publish
overdose deaths but expose no keyless tabular API (Chicago/Cook County ME case archive, LA County,
King County, Philadelphia, DC, Boston). San Francisco (DataSF) and Connecticut (data.ct.gov) were
reachable and are ingested.
