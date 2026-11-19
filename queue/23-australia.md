# australia — Australia: ABS census homelessness estimates and SHS/SHA funding
Write ingest/domains/australia.py. Sources: ABS "Estimating homelessness: Census" (public tables by
state/SA4/LGA), AIHW Specialist Homelessness Services (SHS) data tables, and the National Housing and
Homelessness Agreement / SHS funding by state.
Store: geo_city rows kind='au_lga' or 'au_state', metrics entity_type='au_state'/'au_lga'
(metric='census_homeless_estimate', 'shs_clients', 'shs_funding'), national row entity_type='australia'.
accept: >=8 states/territories with census estimates, >=2 census years, research/australia.md with Verified.
