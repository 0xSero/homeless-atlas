# county: one page of published measures per U.S. county
Build `ingest/domains/county.py` (DOMAIN `county`): ACS 2020-2024 5-year housing, poverty and
health-access measures from the keyless table-based summary files; a tract-population-weighted
county to CoC crosswalk (`county_coc`); HUD PIT by county (the count itself where the county
is its own CoC, a flagged population-apportioned estimate elsewhere); CDC PLACES 2025 mental
health and substance measures; HRSA mental health shortage areas; USASpending homelessness
awards by county and fiscal year; and the top recipients per county (`county_recipient`).
accept: >=3,100 counties with poverty_rate; Erie County PA (42049) has pit_total 2025 = 457
        with is_est=0 and county_coc PA-605 at >=0.99 both ways; Los Angeles (06037) has
        several CoCs and pit_total_apportioned only; a second run changes no row counts;
        research/county.md has `## Verified` (5 live spot-checks) and `## Coverage`.
