# crime — area-level crime context, honestly labelled
Write ingest/domains/crime.py using the FBI Crime Data Explorer (api.usa.gov key: try DEMO_KEY first,
record a BLOCKER if refused) plus city open-data portals (Socrata /resource/*.json, no key) for the
50 largest cities. IMPORTANT: UCR/NIBRS do not record homelessness status — never claim a crime was
committed by a homeless person. Store area-level totals/rates in `metric` (metric='violent_crime_rate',
'property_crime_rate', 'theft_rate', year) with a note field stating the caveat, and store every
documented study or agency report that does measure homeless-attributable incidents in `doc`.
accept: >=50 cities with a documented source, every metric row carries the caveat note
