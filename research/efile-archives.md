# Opening the archives the indexes cannot address

From 2019 to 2023 the IRS index has no `XML_BATCH_ID` column, so `ingest/domains/efile.py` cannot say which
archive holds a filing and skips those years entirely — 1,485 filings belonging to this archive. That left
Part III at 3,068 of the 5,000 target.

The archives are still published one per batch and each is small (118–235 MB, 21,000 filings). A batch can
simply be opened and searched for the object ids we want.

## What survives

Probing every batch name for 2019–2023:

| year | batches still published | everything else |
|---|---|---|
| 2023 | 01A … 12A | 13A+ → 302 |
| 2022 | 01A, 02A | 03A+ → 302 |
| 2021 | 01A | 02A+ → 302 |
| 2020 | none | 01A → 302 |
| 2019 | none | 01A → 302 |

## Result

`ingest/domains/efile_scan.py` walks the surviving batches, extracts only the filings whose object id is in
our index rows, parses Part III with the same extractor the XML ingestor uses, and deletes each archive
(including `fetch()`'s cached copy — 100–235 MB per batch otherwise).

**Part III 3,068 → 3,344** (+276), with roughly 40 texts per surviving batch.

## Why it stops here

The remaining ~15 batches of 2023, 31 of 2022 and 23 of 2021 are retired at the source, and 2019–2020 have
none left at all. There is no naming trick: `302` is a redirect to the IRS 404 page, not a rate limit.

`audit.py` flagged one duplicate `(org_id, year)` insert during the run — two filings of the same year for
one organisation — so the insert is now `INSERT OR IGNORE` and only counts rows it actually wrote.
