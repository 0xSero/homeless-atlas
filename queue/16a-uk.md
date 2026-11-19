# uk — United Kingdom: rough sleeping and statutory homelessness
Write ingest/domains/uk.py. Sources (all keyless, verify live URLs):
- DLUHC / gov.uk "Rough sleeping snapshot in England" (autumn count, per local authority, 2010→latest) — CSV/XLSX on gov.uk.
- Statutory homelessness: "Live tables on homelessness" (MHCLG) — households owed a duty, temporary accommodation, per local authority, quarterly.
- Scotland: Scottish Government homelessness statistics (HL1/HL2). Wales: statswales.gov.wales. Northern Ireland: NI Housing Executive.
Store: geo_city rows for UK local authorities (kind='uk_la', geoid='UK-<ONS code>'), counts in `metric` with
entity_type='uk_la' (metrics: rough_sleeping, statutory_homeless_households, temp_accommodation_households),
national totals as entity_type='uk'. Do NOT merge UK rows into the US tables' semantics — keep them separate.
accept: >=300 English local authorities with rough sleeping for the newest year, >=5 years of national totals,
        Scotland/Wales/NI present, research/uk.md with Verified + Coverage.
