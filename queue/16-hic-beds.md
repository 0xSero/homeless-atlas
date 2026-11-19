# hic-beds — how much shelter and housing capacity exists in each CoC
Write ingest/domains/hic.py from HUD's Housing Inventory Count (HIC) files: beds and units by program type
(ES, TH, PSH, RRH, OPH, DV, youth) per CoC per year. Store as metric rows:
entity_type='coc', entity_id=coc_num, metric='hic_beds_<program>', year, value, source_id.
accept: >=380 CoCs for the newest year, >=5 program types, and a derived beds-per-homeless-person metric.
