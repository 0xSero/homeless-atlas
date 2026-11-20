# turnover — leadership churn, which is measurable and published
Officer rosters exist for 48,844 people across FY2013–2026. Turnover is the one organisational
behaviour the filings support without inference: who was named in year N and was not in year N+1.

Write scripts/build_turnover.py (a metric builder, not a fetcher): for every org with >=2 years of
officer rows, compute
  'officer_turnover'      = 1 - |roster(N) ∩ roster(N+1)| / |roster(N)|, year = N+1
  'cost_per_officer'      = expenses(N+1) / |roster(N+1)|, is_est=1 with the formula in the note
  'officer_dropout_flag'  = 1 when a whole roster disappears in one year (a filing gap, not churn) —
                            flagged rather than scored, because a missing 990 is not a resignation.
Guard: only count a year-pair when both filings exist for that EIN, so a gap never reads as churn.
accept: >=1000 orgs with a turnover metric, the flag fires on >=1 known filing-gap case, and
        research/turnover.md explains the guard with two worked examples.
