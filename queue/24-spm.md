# spm — HUD System Performance Measures: does the money change outcomes?
This is the outcome layer the atlas is missing. HUD publishes System Performance Measures (SPM) per CoC:
length of time homeless, exits to permanent housing, returns to homelessness within 6/12/24 months,
first-time homelessness rate, and the number of people who leave to *homeless* destinations.

Write ingest/domains/spm.py.
Sources (verify live): HUD Exchange "System Performance Measures" reports and the machine-readable SPM
files (hudexchange.info spm data archive), plus the CoC-level SPM tables inside the AHAR appendices.
Store in `metric`: entity_type='coc', entity_id=coc_num, year, metric names such as
'spm_avg_days_homeless', 'spm_exit_to_ph_rate', 'spm_return_6mo_rate', 'spm_return_12mo_rate',
'spm_first_time_rate', 'spm_exit_to_homeless_count', unit, source_id.
Also compute (is_est=1, formula in note): 'spm_returns_per_100_exits'.
accept: >=300 CoCs with at least one SPM year, >=2 years where published, research/spm.md with
        Verified (5 CoC-years re-read from the source) and Coverage (which measures HUD stopped publishing).
