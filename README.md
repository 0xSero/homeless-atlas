# Homeless Atlas

Public-record atlas of homelessness in the United States (then Canada, Australia, UK):
every government-funded organisation and contract, every Continuum of Care count and trend,
programs and budgets, closed and open psychiatric hospitals and prisons with their economics,
the laws, the measured sub-populations, area-level crime and overdose context, and the media —
organised as a searchable, map-driven wiki.

**Start here:** `GOALS/GOAL.md` (the contract), `docs/INGEST_CONTRACT.md` (how a domain is added),
`docs/SOURCES.md` (every source + license), `LEDGER.md` (what has been done, with verdicts).

## Layout
```
ingest/          schema.sql, common.py (fetch/cache/upsert), domains/<domain>.py, run.py
queue/           one file per work item + queue/state/<domain>.status
scripts/         loop.sh (worker pool), build_site.py, verify.py, ledger.py
data/atlas.db    the dataset (raw responses cached under data/raw/<domain>/)
site/            generated static wiki (no build step, no server)
research/        per-domain notes: what was captured, what is missing, what was verified
reports/         long-form briefs (san-francisco.md first)
```

## Commands
```bash
python3 -m ingest.run hud_pit usaspending nonprofit facility   # run ingestors
python3 -m ingest.domains.hud_pit --selftest                   # one domain, samples
python3 scripts/verify.py                                      # the completion gate
python3 scripts/build_site.py                                  # regenerate site/
MAX_PAR=4 ./scripts/loop.sh                                    # the loop (tmux + pi workers)
touch STOP                                                     # stop the loop cleanly
```

## Rules
Public data only. Organisation-level and published professional-role information only.
No number without a source row. Estimates are flagged `is_est=1` and render with a dagger.
