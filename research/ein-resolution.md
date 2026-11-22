# Resolving EINs from the IRS 990-N extract

11,229 organisations in this archive had no EIN, so no filing could be looked up for them and no Part III
text could exist. The 1,554,852-row Form 990-N extract carries EIN **and** the organisation's name and
address, so an EIN can be recovered by matching the registry against our own records.

## Matching rule

Name normalised (case, punctuation, legal suffixes removed), then:

1. **tier 1** — normalised name identical **and** city **and** state identical: **200 organisations**;
2. **tier 2** — normalised name identical, 14+ characters, unique in the registry for that state, city
   equal or absent: **28 organisations**;
3. **tier 3** — normalised name identical, **20+ characters and unique in the registry nationally**:
   166 candidates, 155 written (11 skipped because that EIN was already claimed by another org record,
   which would have broken the one-EIN-one-organisation invariant `audit.py` enforces).

Every resolution is recorded in `org_ein_resolved` with the rule that produced it; nothing was matched on
name alone. Tier 1 was applied first and its filings ingested; tier 2 was measured, then applied.

## Result

Running the e-file ingestor over the resolved EINs:

| run | filings parsed | Part III texts | rows after |
|---|---|---|---|
| tier 1 (200 EINs) | 213 | **100** | 3,050 |
| tier 2 (28 EINs) | 133 | **18** | 3,068 |
| tier 3 (155 EINs) | 162 | **36** | 3,380 |

**Part III 2,950 → 3,380.** The remaining organisations without an EIN are overwhelmingly government
entities — cities, counties, housing authorities — which do not file a Form 990 at all.
