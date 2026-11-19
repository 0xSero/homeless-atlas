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


## Status

`python3 scripts/verify.py` → **ALL GATES PASS**. Latest build: 36,691 pages, 58,245 search
records, 0 dead internal links, 0 orphan pages, database `integrity_check: ok`.

| what | rows | source |
|---|---|---|
| organisations | 15,100+ | USASpending recipients, IRS filings, census |
| named officers/directors | 82,000+ | IRS Form 990 Part VII (published roles only) |
| federal awards | 41,500+ | USASpending.gov (contracts + grants, all years) |
| CoC point-in-time counts | 7,431 CoC-years, 413 CoCs, 2007-2025 | HUD PIT/AHAR |
| places with population + coordinates | 56,819 | Census population estimates + gazetteer |
| institutions (hospitals, prisons, jails) | 3,432 | Wikidata, BJS, state agencies, LAO/TAC |
| law rows | 1,164 | state legislatures, NLIHC, TAC, journalist databases |
| videos + images | 1,999 | yt-dlp search, Wikimedia Commons |
| audits, court decisions, studies | 375 | search-API discovery, every URL verified 200 |
| registered sources | 5,400+ | every row in every table points at one |

## Operational lessons (why the scripts look the way they do)

- **SQLite WAL allows exactly one writer.** Ten concurrent ingestors starved each other: 2 rows in
  15 minutes. One writer at a time does ~8,900 rows in 4 minutes. `common.db()` takes an exclusive
  flock for the life of an ingest process; `scripts/sweep_awards.py` and friends are run one at a
  time. Concurrency here is a bug, not a feature.
- **The database got corrupted once** (invalid page numbers across the metric b-tree) under that
  concurrent-write load. It was recovered row by row from the damaged image; `synchronous=FULL`,
  15-minute `VACUUM INTO` snapshots (`scripts/backup_db.sh`) and a `quick_check` gate in the loop
  exist because of it.
- **A number whose citation does not contain the number is worse than no number.** Two provenances
  had to be repaired: 32,608 population rows citing a boundary shapefile, and 2,180 org financials
  citing the IRS Business Master File (no financial fields). `scripts/audit.py` now checks that
  every table's provenance comes from a domain that could plausibly contain its fields.
- **The site is published atomically** (`site.staging` → swap) and refuses to publish a section that
  shrank by more than half, which is what an ingestor mid-transaction looks like from outside.

## Rules
Public data only. Organisation-level and published professional-role information only.
No number without a source row. Estimates are flagged `is_est=1` and render with a dagger.
