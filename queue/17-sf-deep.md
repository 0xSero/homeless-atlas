# sf-deep — San Francisco specifics that the flagship brief needs
Write ingest/domains/sf.py: (1) SF Controller/Board of Supervisors budget line items for homelessness
(HSH department, Prop C 2018 "Our City Our Home", Measure C 2024); (2) SF's largest homelessness contracts
from SF's contract data (sfgov.org open data / Socrata) with vendor, amount, term; (3) SFPD/311 encampment
and street-related service data; (4) SF medical examiner overdose deaths by year; (5) Laguna Honda,
SF General Hospital psychiatric services, and county jail mental-health capacity.
Store contracts in `award` (award_type='local_contract', agency='City and County of San Francisco'),
budgets and counts in `metric` with entity_id='San Francisco', facilities in `facility`, and the rest in `doc`.
accept: >=100 local contracts with vendor+amount, >=5 budget line items, >=3 facilities updated,
        overdose series >=5 years, every row sourced.
