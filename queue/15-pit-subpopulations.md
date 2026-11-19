PARKED 2026-11-19 — HUD publishes SMI/SUD/HIV/DV per CoC **only** in ~7,400 individual PDF reports with no stable URLs; the machine-readable PIT workbooks carry them nationally only. National values (all 19 years) are already in `metric` as `pit_smi`/`pit_sud`/`pit_hiv`/`pit_dv` from the `hud_pit` ingest. Re-open only if HUD ships a per-CoC machine-readable subpopulation file, or if someone budgets for PDF extraction with OCR.

# pit-subpopulations — who the counted people are: SMI, substance use, chronic, veterans, youth, families
STATUS: the `pit` table has total/sheltered/unsheltered for 2007-2025 but smi/sud/chronic/veterans/youth are EMPTY.
Write ingest/domains/pit_sub.py and fill those columns for every CoC-year where HUD actually publishes them.
Sources: HUD's PIT subpopulation workbooks (HUD Exchange "PIT and HIC data since 2007" archives), the AHAR
Part 2 CoC-level tables, and the "2007-2024 PIT Count by Subpopulation" files. Set pit.smi/sud/chronic/...
by UPDATE on (coc_num, year); never overwrite an existing non-null value; never interpolate.
accept: smi filled for >=300 CoC-years, sud likewise, chronic for >=3,000 CoC-years; research/pit_sub.md
        states exactly which years HUD publishes each subpopulation and which are simply not published.
