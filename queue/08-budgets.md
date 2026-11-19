# budgets — what each level of government spends on homelessness
Write ingest/domains/budgets.py: HUD CoC + ESG award totals per state/CoC (from `award`), state
homelessness budget lines (NASHPH/NLIHC/CSH state profiles + state budget documents), city
homelessness budgets (SF, LA, NYC, Seattle, Portland, Denver, San Diego, Houston, Boston, DC...),
and per-capita / per-homeless-person spend as derived metrics with is_est=1 + formula in note.
accept: >=30 states with a sourced state-level figure, >=10 city budgets, per-homeless spend metric
