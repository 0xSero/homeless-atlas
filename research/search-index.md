# search-index — make the index cheap on a phone

Verdict: **BLOCKED on scope, not on data.** Everything this item asks for lives in two files this
worker is forbidden to touch (`scripts/build_site.py`, `site_src/app.js`) and in the generated tree
`site/`. The measurements below are real and reproducible; the change itself has to be made by the
build owner or a worker with an explicit exception (see `BLOCKERS.md` § search-index).

## What the item asks for

1. Split each letter shard into `<letter>-<0..9>` buckets hashed by title, with per-bucket counts in
   `manifest.json`, and load only the buckets the client needs (or a 2-level `token -> bucket list`).
2. Accept: no file the client fetches exceeds 250 KB; "continuum of care" and "housing clinic" return
   the same top hits; `search.html` p95 query latency < 300 ms on a simulated 3G throttle.

## Current state (measured, not assumed)

Point 1 was already done, in commit `f43045e9b9` ("search: bucketed shards"), which edited exactly the
two files this worker may not edit:

- `scripts/build_site.py:570 build_index()` — shards by the first letter of **every** token of
  title+subtitle, then `bucket = md5(title) % 10`, writing `site/data/idx/<letter>-<bucket>.json` and
  `site/data/idx/manifest.json` = `{"shards": {"c-4": 3363, ...}, "total": 64772}`.
- `site_src/app.js:7-31 loadShard()` — on each new leading letter the client fetches **all ten**
  `letter-0..9` files in parallel, concatenates them, and caches the letter in memory.

Sizes on disk (`site/data/idx`, 360 shard files + manifest, 50,008,374 bytes total):

| | value |
|---|---|
| files over 250 KB on disk | **76 of 360** |
| largest file | `c-9.json` **423,061 B** (`c-8` 416,954, `f-9` 412,688) |
| bytes pulled by one keystroke on a new letter | letter `c`: **4,071,823 B** (10 files); `f` 3,941,401; `r` 3,606,477; `p` 3,195,717 |

Bytes actually on the wire (Cloudflare brotli-encodes these; `Content-Encoding: br`), fetched live from
`https://homeless.sybilsolutions.ai/data/idx/…`:

| file | disk | wire (br) |
|---|---|---|
| `c-9.json` | 423,061 | 115,001 |
| `c-8.json` | 416,954 | 113,797 |
| `f-9.json` | 412,688 | 115,861 |
| `c-1.json` | 410,317 | 114,338 |
| `f-1.json` | 401,425 | 113,332 |
| **letter `c`, all 10 files** | **4,071,823** | **1,131,932** |

So criterion 1 holds only because of transport compression (ratio ≈ 0.28); on disk it does not, and a
`wrangler` setting that disables compression, or any client that ignores `Content-Encoding`, takes the
full 423 KB. Criterion 3 fails outright: one keystroke on `c`/`f`/`r`/`p` is 1.0–1.2 MB br. At
"Fast 3G" (1.6 Mbps ≈ 200 KB/s, 150 ms RTT) that is ≈ 6 s; at "Slow 3G" (400 kbps, 400 ms RTT) ≈ 23 s.
Arithmetic from the measured byte counts, not a benchmark — but one to two orders of magnitude clear
of the 300 ms target, so no measurement subtlety can save it.

### Repo state while measuring

`git status` was clean for `scripts/`/`site/` when this worker started, and became dirty *during* the
session without this worker writing anything there: `scripts/build_site.py` (+`page_404`),
`scripts/deploy-worker.js`, and a regenerated `site/data/idx/manifest.json` at 09:35:59 (key order only —
same 360 shards, same counts, same file sizes, so the numbers above are unaffected). Something else in
the loop is rebuilding the site right now. That is exactly the interleaving the write rules exist to
prevent, and it is the second reason not to hand-edit `site/data/idx` from a domain worker: it was
rewritten under me mid-measurement.

## Baseline for the two acceptance queries

Replayed `app.js`'s `score()` over the real shards (read-only; script output in
`logs/search-index-measure.json`). `shardKey(q)` uses the first letter of the query, so both queries
scan one letter's ten buckets:

- **"continuum of care"** → letter `c`, 34,566 rows scanned, 141 rows score > 0, top:
  `CONTINUUM OF CARE INC` (1479), `Continuum Of Care Ministry` (1474), `CONTINUUM OF CARE FOR LGBTQ
  YOUTH` (1467), `CONTINUUM OF CARE NETWORK NWI INC` (1467), `Continuum Of Care Collaborative Inc`
  (1465), doc `CONTINUUM OF CARE HOMELESS COUNT & SURVEY REPORT` (1452).
- **"housing clinic"** → letter `h`, 22,914 rows scanned, **4** rows score > 0, all `person` rows at the
  floor score 200: `Deborah Jones`, `Dwayne D Poole-seymour` (`Clinical Coordinator — ACACIA NETWORK
  HOUSING INC`), `Thidar Moldin`, `Mary Ann Stott`.

**The org the code comment cites, "Tenderloin Housing Clinic", is not in this atlas.** Verified:

```
select count(*) from org        where name like '%TENDERLOIN HOUSING%'  -> 0
select count(*) from org_person where org_name like '%TENDERLOIN%HOUSING%' -> 0
select count(*) from org        where name like '%CLINIC%' and name like '%HOUSING%' -> 0
```

(the 22,180 `org` rows are all in the index; the nonprofit domain simply has no such organisation.)
So "same top hits" is checkable — it is the 4 person rows above — but the behaviour the comment
promises ("housing clinic" → Tenderloin Housing Clinic) cannot be delivered by re-bucketing. That is a
**coverage** gap in `nonprofit`/`org_contacts`, not a search-index problem.

## Why nothing was changed

- The generator is `scripts/build_site.py:570`; the client is `site_src/app.js` (copied to
  `site/assets/app.js` by `scripts/build_site.py:1972`); the artifacts are `site/data/idx/*.json`.
  All three are excluded by rule 4 ("write ONLY inside `ingest/domains/<domain>.py`,
  `data/raw/<domain>/`, `research/<domain>.md`, `docs/SOURCES.md`, `logs/`").
- There is no ingest-side hook to use instead: `build_index()` builds the index from the in-memory
  `d` dict of DB reads and has no file input, and no other code reads a prepared index
  (`grep -rn "data/idx\|search-index.json"` → only `scripts/build_site.py`, `scripts/verify.py`,
  `site_src/app.js`). An `ingest/domains/search_index.py` would write shards nothing serves, and the
  next `build_site.py` run wipes `site/data/idx` anyway.
- Rebuilding also takes `logs/build.lock` (`scripts/build_site.py:1911`) and rewrites the whole site;
  that is the build owner's job, not a per-domain ingestor's.
- Ran the contract's sanity command anyway to record what happens: `python3 -m ingest.run search-index`
  → `ModuleNotFoundError: No module named 'ingest.domains.search-index'`, and no
  `ingest/domains/search_index.py` exists. Row counts: **none** — nothing was written to `atlas.db`
  (`ingest_run` untouched by this worker), no new `source` row, no `docs/SOURCES.md` row, because no
  new source was used: only the project's own `site/data/idx` artifacts were read (cached in
  `data/raw/search_index/`, logged in `logs/ingest-search_index.jsonl`, measurements in
  `logs/search-index-measure.json`).

## What a fix needs, with measured numbers

Options, all sized from the same 64,772-row index (`site/data/search-index.json`, capped at 60,000
items in that file — the 360 shards hold the full 64,772):

| design | size | verdict |
|---|---|---|
| token postings keyed by 4-char prefix, payload `[type,title,sub,url]` | 14,457 prefixes; **22 prefixes still > 250 KB singly** (`fede` 2,866,314 B / 22,201 rows, `rev` 2,865,383, `inc` 2,031,387); `fede` is 776,278 B gzipped | still seconds on 3G for common tokens |
| one client-cached index, `[type,title,sub,url]` | 6,307,094 B raw / 1,608,727 B gzip | ≥ 6 files of 250 KB, ~8 s on Fast 3G before first result |
| one client-cached index, `[type,title,url]` (drop `sub`) | 4,102,454 B raw / 1,110,920 B gzip | cheaper, but changes `score()`'s 600/200 tiers → top hits change |
| titles only | 1,387,438 B raw / 452,002 B gzip | smallest, loses all subtitle matching |

Per-query fetch must be ≲ 40 KB to land under 300 ms on 3G, and no static-file layout that keeps
substring matching over the whole letter can do that: the 300 ms budget does not fit the row volume.
Recommendation, in order:

1. **Search server-side.** `wrangler.jsonc` already points at `scripts/deploy-worker.js` with `site/` as
   static assets, so a `/api/search` route over a compact postings table (KV or D1) returns ~2–8 KB per
   keystroke and makes p95 < 300 ms achievable regardless of index size. Needs edits to
   `scripts/deploy-worker.js` + `wrangler.jsonc` + a deploy — infra scope, out of this item.
2. **If it must stay static:** two-level manifest (`token → [bucket files]`, counts per bucket), 4-char
   token prefixes, payload `[type,title,url]` plus a shared subtitle table, files capped at ~200 KB, and
   the client fetches only the buckets its tokens name. That satisfies criteria 1 and 2 and gets the
   median query under 300 ms, but common tokens (`of`, `care`, `housing`) will still miss 300 ms on 3G.
   Then re-word criterion 3 to "p95 after first keystroke with the letter cached", or accept option 1.

## Unblock

One of:

- grant a worker an exception to edit `scripts/build_site.py:build_index()`, `site_src/app.js:loadShard()`
  and `site/data/idx/` (and to run `scripts/build_site.py` / the crawl gate), **or**
- decide that search moves to the Worker (`/api/search`), in which case this item belongs to the deploy
  task, **or**
- re-scope the item to what a domain worker can do alone (nothing, as of today).
