# GOAL — Homeless Atlas

**Objective.** Build `homeless-atlas`: the most complete public-record atlas of homelessness in the US (then Canada, Australia, UK) — every government-funded homelessness organization and contract, every CoC/city homeless count and trend, every program/budget, every closed and open psychiatric hospital and prison with operating economics, the laws, the measured sub-populations (mental illness, substance use, chronic, veteran, youth), crime and overdose context, media, and a searchable wiki site with maps that works on mobile and desktop. US first and deepest; San Francisco is the flagship deep report.

**Verified by**
1. `python3 scripts/verify.py` exits 0 and prints per-domain row counts, coverage (states/CoCs/cities covered), and page counts.
2. `sqlite3 data/atlas.db` counts: `org` >= 5,000; `award` >= 40,000 homelessness-related federal awards; `pit` covers >= 380 CoCs × all years 2007→latest; `geo_city` >= 20,000 places; `facility` >= 1,000 institutions; every row carries a `source_id`.
3. Every entity in the DB has a reachable HTML page; `site/search-index.json` contains every entity; every page has a working search box + breadcrumb; pages render at 390px and 1440px widths.
4. `reports/san-francisco.md` exists with citations to primary sources for every numeric claim.
5. Zero fabricated values: every number traces to a `source` row with URL + retrieval date; estimated/proxy numbers are labelled `est` in the DB and rendered with a dagger + footnote.

**Constraints**
- Public data only. Org-level and published-role professional information only (Form 990 officer names/titles, published org emails). No home addresses, no personal cell numbers, no scraping behind logins/CAPTCHAs/paywalls.
- Never invent a number or a URL. If a source is unavailable, record it in `BLOCKERS.md` and continue.
- Respect source terms: USASpending/ProPublica/Wikidata/Census APIs at polite rates (<= 1 req/s, exponential backoff, cache every response under `data/raw/`).
- Licenses recorded per source in `docs/SOURCES.md`; attribution rendered on the site.
- Microcommit: one domain or one site change per commit.
- Parallel agents write only inside their own namespace (`ingest/domains/<domain>.py`, `data/raw/<domain>/`, `research/<domain>.md`) — never each other's files, never `site/` (only I own the site generator).

**Boundaries / scope stops**
- Do NOT dox individuals: nothing that identifies a private person's location or contact beyond what a public filing publishes.
- No framing of homeless people as a criminal class: crime data is presented as area-level context with explicit caveats that homelessness-attributed crime is not recorded as such in UCR/NIBRS.
- Not a legal-advice product; state law pages are summaries with citations.

**Iteration policy**
Between cycles: read `ledger.jsonl` tail + `QUEUE.md`, pick the highest-value unfinished item whose sources are reachable, run up to 4 parallel workers on disjoint domains, verify row counts + spot-check 5 random rows against the live source, commit, log a ledger row (with verdict KEEP/REJECT/PARKED), rebuild the site, then queue the next item. If a domain is exhausted (no new URLs, counts plateau), mark it DONE with evidence and move on. The loop stops only when `QUEUE.md` has no OPEN/DONE-less items and `verify.py` passes — or on an unrecoverable blocker.

**Blocked stop condition**
Stop and report (do not idle, do not fake progress) if: (a) a required API key is missing and no keyless equivalent exists (record exact key + signup URL in `BLOCKERS.md`), (b) rate limits/site policy make a domain unreachable after 3 retries across 2 cycles, (c) disk < 20 GB. Then write what unblocks it and continue every other reachable domain.
