# Ingest contract (read before writing a domain ingestor)

One file per domain: `ingest/domains/<domain>.py`. Nothing else may be edited by a worker except
`research/<domain>.md` and `data/raw/<domain>/`.

## Interface

```python
DOMAIN = "hud_pit"          # snake_case, matches file name
def run(conn, cfg) -> dict: # returns {"rows": int, "notes": str, "sources": [(url, license)]}
```

`conn` is a `sqlite3.Connection` to `data/atlas.db` (schema in `ingest/schema.sql`).
`cfg` is a dict from `ingest/domains/<domain>.json` (optional; may be absent).

Helpers available from `ingest/common.py`:

```python
from ingest.common import fetch, fetch_json, fetch_post_json, save_raw, upsert, clean, now
```

- `fetch(url, *, method="GET", data=None, headers=None, force=False) -> bytes`
  caches to `data/raw/<DOMAIN>/<sha1(url)>.bin`, 1 req/s global, 3 retries, records a `source` row.
- `fetch_json(...)`, `fetch_post_json(url, payload)` same but parsed / POST JSON.
- `save_raw(name, bytes) -> Path` for big downloads (Excel/ZIP/CSV) — return the path.
- `upsert(conn, table, row: dict, keys=[...])` insert-or-update on conflict of `keys`.
- `clean(s)` collapse whitespace, strip, None on empty.

## Hard rules
- **Idempotent**: running twice must not duplicate rows. Use `upsert` with a natural key.
- **Never fabricate**: if a field is missing write `None`. Set `is_est=1` on modelled/proxy numbers.
- **Every row carries provenance**: `source_id` = the `source.id` for the URL it came from
  (`fetch` returns bytes and registers the source; use `source_id_for(url)`).
- **Name normalisation**: always also write `name_norm` (uppercase, punctuation stripped, org
  suffixes removed: INC, LLC, CORP, CO, ASSOC, FOUNDATION, THE). Matching between sources keys on
  `name_norm` + state.
- **Rate limit**: 1 req/s per worker. If a source 403s, stop that source, note it, keep the rest.
- **Log**: append one JSON line per source to `logs/ingest-<DOMAIN>.jsonl`
  `{"ts","url","status","bytes"}`.
- Read `docs/SOURCES.md` and add a row there for any source you used that is not listed
  (append only, never rewrite the file).

## Tables you may write

Core (owned by the schema, many domains feed them):
`source, org, org_person, award, pit, geo_state, geo_coc, geo_city, facility, law, media, doc,
metric`

Domain-private tables must be prefixed `<domain>_` and created with `CREATE TABLE IF NOT EXISTS`
inside your `run()`.

## Sanity gate (your work is rejected without this)

```bash
python3 -m ingest.domains.<domain> --selftest   # must print rows + 3 sample rows
python3 -m ingest.domains.<domain>             # real run, prints row counts
```

Spot-check 5 random rows against the live source URL by hand and put the 5 URLs + what you
verified in `research/<domain>.md` under a `## Verified` heading, plus a `## Coverage` heading
stating what fraction of the target universe you captured and what is still missing.
