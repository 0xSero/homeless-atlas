# Ledger — homeless-atlas

| ts | domain | verdict | rows | evidence | note |
|---|---|---|---|---|---|
| 2026-11-19T07:27:53+00:00 | usaspending | REJECT | 0 | logs/workers/usaspending.log | attempt 1 failed rc=1 |
| 2026-11-19T07:27:53+00:00 | hud_pit | REJECT | 0 | logs/workers/hud_pit.log | attempt 1 failed rc=1 |
| 2026-11-19T07:27:53+00:00 | nonprofit | REJECT | 0 | logs/workers/nonprofit.log | attempt 1 failed rc=1 |
| 2026-11-19T07:27:54+00:00 | facility | REJECT | 0 | logs/workers/facility.log | attempt 1 failed rc=1 |
| 2026-11-19T07:59:42+00:00 | hud_pit | DONE | 7431 | research/hud_pit.md | 2007-2025, 413 CoCs, national total matches HUD in 16/19 years, smi/sud national-only by design |
| 2026-11-19T08:00:57+00:00 | coc-geometry | DONE | 0 | research/coc-geometry.md + ingest/domains/coc-geometry.py | worker exit 0 |
| 2026-11-19T08:03:33+00:00 | health | DONE | 15021 | research/health.md + ingest/domains/health.py | worker exit 0 |
| 2026-11-19T08:04:43+00:00 | crime | REJECT | 0 | logs/workers/crime.log | attempt 2 failed rc=143 |
| 2026-11-19T08:16:26+00:00 | crime | PARKED | 0 | logs/workers/crime.log | failed 3 attempts, needs review |
| 2026-11-19T08:16:35+00:00 | budgets | REJECT | 1433 | logs/workers/budgets.log | attempt 1 failed rc=143 |
| 2026-11-19T08:16:44+00:00 | laws | REJECT | 0 | logs/workers/laws.log | attempt 1 failed rc=143 |
| 2026-11-19T08:16:52+00:00 | media | REJECT | 0 | logs/workers/media.log | attempt 1 failed rc=143 |
| 2026-11-19T08:26:35+00:00 | facility | DONE | 3432 | research/facility.md | 3,432 institutions, 54 states, 132 per-bed cost figures, commit-safe writer |
| 2026-11-19T09:55:18+00:00 | laws | DONE | 0 | research/laws.md | gate passing at final verify |
| 2026-11-19T09:55:18+00:00 | media | DONE | 0 | research/media.md | gate passing at final verify |
| 2026-11-19T09:55:18+00:00 | docs | DONE | 0 | research/docs.md | gate passing at final verify |
| 2026-11-19T09:55:18+00:00 | budgets | DONE | 0 | research/budgets.md | gate passing at final verify |
| 2026-11-19T09:55:18+00:00 | docs_collect | DONE | 375 | scripts/collect_docs.py | audits/court/studies via search API, URL-verified |
| 2026-11-19T09:55:18+00:00 | usaspending | DONE | 41554 | scripts/sweep_awards.py | year-by-year sweep; FY2024 14->8460, provenance 100% |
| 2026-11-19T12:40:46+00:00 | hic-beds | DONE | 0 | research/hic-beds.md + ingest/domains/hic-beds.py | worker exit 0 |
| 2026-11-19T12:51:49+00:00 | uk | DONE | 6686 | research/uk.md + ingest/domains/uk.py | worker exit 0 |
| 2026-11-19T13:33:08+00:00 | canada | DONE | 606 | research/canada.md + ingest/domains/canada.py | worker exit 0 |
