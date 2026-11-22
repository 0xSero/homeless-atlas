# 32 — org websites via SAM.gov entity API (needs a free key)

**Status:** DELEGATE (needs `SAM_API_KEY`; no keyless equivalent left)

## Why this item exists

The website target was 5,000 organisations of 22,633. It stands at **4,599** after nine keyless sources
were measured to exhaustion (`BLOCKERS.md`, "Sources tried for the last 380 websites"):

| source | yield |
|---|---|
| Common Crawl domain ranks, 3 passes | 1,811 |
| generated city/county patterns | 338 |
| Brave web search, whole 2,000/30d quota | 168 |
| housing authority patterns | 29 |
| city + service keyword patterns | 13 |
| OpenStreetMap `website` tags | 5 |
| Wikidata by label | 3 |
| Wikidata by EIN | 1 |

Every one of those infers a website from a *name*. The remaining 18,000 organisations have no site in
any public corpus that publishes names next to domain names.

## What unblocks it

SAM.gov's entity API returns, for every registered entity, `entityURL` and a point of contact:

```
GET https://api.sam.gov/entity-information/v3/entities
      ?api_key=$SAM_API_KEY&ueiSAM=<uei>&samRegistered=Yes&fmt=json
```

Join on UEI where we have it (the subaward and award ingests carry recipient UEIs), and on EIN/CAGE
where we don't. A free key is issued at https://api.data.gov/signup/ (2,000 requests/day, no cost).

## Acceptance

- websites ≥ 5,000, every URL fetched and matched to its organisation by name **and** state;
- an evidence row per write (`org_domain_mine` or a new `org_sam_check`);
- `verify.py` exit 0, `audit.py` AUDIT CLEAN, `crawl.py` SITE OK, `quick_check` ok;
- a ledger row naming the count and the evidence file.
