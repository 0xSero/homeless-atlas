# Blockers

Anything that stops a domain, with exactly what unblocks it. Keep this list honest and current.

## Keys that would unlock more (none are strictly required for the keyless core)
- `DATA_GOV_API_KEY` (api.data.gov, free, instant) — SAM.gov entity registrations (UEI -> full registration,
  officer names, address, NAICS) and the FBI Crime Data Explorer at full rate. Without it we degrade to
  USASpending recipients + DEMO_KEY.
- `CENSUS_API_KEY` (api.census.gov, free) — ACS detail tables without hitting the keyless 500/day cap.
- `YOUTUBE_API_KEY` — quota-clean YouTube search. Without it we use `yt-dlp ytsearch`.
