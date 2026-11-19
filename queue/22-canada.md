# canada — Canada: point-in-time counts and federal funding
Write ingest/domains/canada.py. Sources: Infrastructure Canada / Reaching Home "Everyone Counts" PIT counts
(public dataset), Statistics Canada census shelter costs/hidden homelessness indicators, and Reaching Home
community funding allocations (public). Multi-year PIT data by community.
Store: geo_city rows kind='ca_city', counts in `metric` entity_type='ca_city' (metric='pit_total',
'pit_unsheltered', 'pit_sheltered'), national rows entity_type='canada'. Provenance on every row.
accept: >=40 Canadian communities with a PIT count, >=2 count cycles where published, research/canada.md.
