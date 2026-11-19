# health — mental illness, substance use and overdose by state and city
Write ingest/domains/health.py. Sources: SAMHSA NSDUH state prevalence tables (public Excel),
CDC WONDER / NCHS drug-overdose death counts by state and county, HUD PIT subpopulation counts
(already in `pit`), and city medical-examiner overdose dashboards where published.
Store metric rows: 'smi_prevalence', 'sud_prevalence', 'overdose_deaths', 'overdose_rate'.
accept: all 50 states + DC for overdose and SMI, >=20 cities for overdose; no number without a source
