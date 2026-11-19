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
