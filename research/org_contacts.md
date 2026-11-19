# org_contacts — the website, e-mail and phone each organisation publishes itself

Scope: every organisation already in the `org` table (22,180 rows: nonprofits from IRS Form 990 work,
CoC lead agencies, housing authorities, government bodies, plus the facility/health operators). For each
one we want the address of the site the organisation publishes, and the *general* contact channel that
the organisation itself prints on that site — never an individual's mailbox or cell phone.

Ingestor: `ingest/domains/org_contacts.py` (`DOMAIN="org_contacts"`, `run(conn, cfg)`).
Raw responses: `data/raw/org_contacts/`. Fetch journal: `logs/ingest-org_contacts.jsonl`.

## Where the website address comes from

| Layer | Source | What it gives | Coverage |
|---|---|---|---|
| Universe | `org` table (22,180 rows) | name, EIN, UEI, city/state, revenue | — |
| Website address | IRS **Form 990 e-file XML**, TEOS bulk batches — `<WebsiteAddressTxt>` on the main return of 990 / 990-EZ / 990-PF | the web address the organisation fills in itself when it files; plus the filer `<PhoneNum>` and the filer's own address | every org with an EIN and an e-filed return |
| Website address (secondary) | `org.website`, already in the DB from the `nonprofit` worker (Wikidata P856) | a second, independently published address | 221 orgs |
| Verification + contact | the organisation's own site: `robots.txt`, the homepage, and one contact page linked from it | HTTP status, page title, a name match proving the site is the org's; `mailto:` and `tel:` links | every candidate that answers |

Not used, and why: **SAM.gov entity registration** (the brief's third suggested origin) needs an
`api.data.gov` key we do not have — see BLOCKERS.md `## org_contacts` with the key name and signup URL.
**ProPublica's profile page** was checked live and does **not** publish a website: the rendered filing
page for Abode Services (`/nonprofits/organizations/943087060`) contains no website link at all, so it
cannot be the origin of one. Nothing is ever derived from a name guess, a domain pattern, or a search
engine.

### How a website becomes "verified"

A candidate URL is only written to `org.website` when all of the following hold:

1. `robots.txt` for the host allows our agent (`homeless-atlas/1.0`), checked per host and cached;
2. the page answers **HTTP 200** and has a readable body (>80 characters of text);
3. it is **not** a parked / "domain for sale" / "account suspended" / "coming soon" page (regex list);
4. at least one distinctive token of the organisation's own normalised name appears in the page title,
   body or host name — with the generic tokens (`HOME`, `HOPE`, `CARE`, `SHELTER`, `STREET`, …) only
   accepted in pairs, and the full normalised name in the host counting as proof on its own.

Everything else is recorded as *not* theirs, with the reason, in `org_contacts_site.note`. A filed
address that has lost its scheme or `www.` is retried once on the other scheme / the other `www.` form;
that is the only "repair" applied, and the URL actually fetched is what gets stored.

### How e-mail and phone are chosen

* **E-mail**: only role mailboxes from the `mailto:` links (and, when the page has none, from plain
  text) — `info@`, `contact@`, `office@`, `admin@`, `intake@`, `shelter@`, `development@`,
  `communications@`, … (a fixed allow-list). A mailbox whose local part matches a person on that org's
  `org_person` roster is dropped, as are hosting-provider and builder artefacts (`sentry@`,
  `@wixpress.com`, `noreply@`), placeholder addresses (`example.com`, `yourdomain.com`) and asset names.
  The value is taken from the page with the most independent hits, not from the first match.
* **Phone**: `tel:` links only, normalised to `NNN-NNN-NNNN`; a link whose surrounding text says
  `fax`, `cell` or `mobile` is dropped, as are fictional 555 numbers. The contact page is fetched only
  when the homepage publishes no usable e-mail, so a site costs 2-3 requests at most.
* **Fallback phone only**: when the site publishes no `tel:` link, the phone the organisation filed as
  its own filer phone on Form 990 is used, and the row says so
  (`note = "… phone taken from the filer block of the org's own Form 990"`).

### Provenance

`org_contacts_site` is one row per candidate site and carries the page-level provenance:
`website_source_id`, `email_source_id`, `phone_source_id` → `source.id` → the exact URL (and its cached
bytes' sha256) that published the value, plus `email_page` / `phone_page` for the URL itself. The columns
on `org` are filled only where they are empty (`COALESCE`-style: another ingestor's value is never
overwritten), and `org.source_id` is left pointing at whoever created the row — the contract forbids
re-pointing it, because a blanket `UPDATE` once made the site cite a boundary shapefile for population
figures. Value-level traceability therefore lives in `org_contacts_site`, one URL per field.

## Engineering notes

* **Resumable in three layers.** `org_contacts_zip` = one row per downloaded TEOS batch (never fetched
  twice), `org_contacts_filing` = one row per (EIN, object id) kept from those batches,
  `org_contacts_site` = one row per candidate site with its crawl state, and every crawled site is also
  appended to `data/raw/org_contacts/site_results.jsonl` before it is committed, so a killed run loses at
  most the site in flight.
* The crawl is a separate phase from the harvest because it is the slow part: 1 request/second through
  `common.fetch`, ~2.7 requests per site (robots + homepage + optional contact page).
* Some IRS batches are stored with zip compression **method 9 (Deflate64)**, which Python's `zipfile`
  cannot inflate. Those are unpacked once with the system `unzip` and parsed from disk; the batch row
  says so in its note. Batch `2026_05A` (81,770 filings) is one of them and would have been silently
  lost otherwise.
* The zip parse is a regex over the `<Filer>` block and `<ReturnHeader>` only (17-84k filings per batch,
  16 forked workers); the `ReturnData` body is never parsed, which is why a batch takes seconds, not
  minutes.

## Verified

Five organisations re-checked by hand on 2026-11-19 with a **fresh, cache-bypassing** `curl` of the stored
URL (the value stored in `org_contacts_site` is compared with what the live page says today):

| org | stored website (source id) | live page | stored e-mail / phone | live page says |
|---|---|---|---|---|
| 12873 Abode Services | `https://www.abode.org` (source 13121) | HTTP 200, 90,216 bytes | `info@abode.org`, `510-657-7409` | both present verbatim on the homepage |
| 2675 WestHab Inc | `https://www.westhab.org` (11174) | HTTP 200, 138,546 bytes | `mail@westhab.org`, `914-345-2800` | both present; the phone's provenance row is the `/contact` page (source 11021) |
| 2626 Pine Street Inn | `https://www.pinestreetinn.org` (10977) | HTTP 200, 368,538 bytes | `info@pinestreetinn.org`, `617-892-9100` | both present on the homepage |
| 12979 The People Concern | `https://www.thepeopleconcern.org` (13166) | HTTP 200, 388,270 bytes | `events@thepeopleconcern.org` (page 13167), `323-334-9000` (page 10987) | phone on the homepage; the mailbox is on the contact page it was taken from, not the homepage |
| 7608 Hope of the Valley Rescue Mission | `https://www.hopethemission.org` (12211) | HTTP 200, 323,336 bytes | `info@hopethemission.org`, `818-392-0020` | both present (the org now trades as "Hope the Mission"; the 990 still carries the legal name) |

Also checked while building: the field is *not* always on the homepage — The People Concern publishes no
generic `mailto:` on its front page, only on its contact page, which is why the crawl fetches one linked
contact page when the homepage yields nothing. And `org.website` was already non-empty for 209 orgs from
the `nonprofit` worker; those rows keep their original value (the ingestor only fills blanks), but they now
carry a page-level verification row like every other.

## Coverage

* **Candidate websites found: 2,807 organisations** (of 22,180 `org` rows, 11,404 of which have an EIN).
  2,800 have a `<WebsiteAddressTxt>` in one of the Form 990 filings harvested; 112 of the 221 orgs that
  already carried a Wikidata website had no 990 website and were re-verified from that value instead.
  IRS batches harvested: **43**, filing years 2021–2026 (`2022_01A`, `2021_01A` are the big single-file
  annual archives, 433,529 and 589,904 filings; each batch is one `org_contacts_zip` row). 16,561 filings
  of our EINs were kept; the rest of the ~4.4 M filings scanned belong to other organisations.
* **Websites verified and written: 1,990** `org_contacts_site` rows with `verified=1` — **1,823 of them
  filled a previously empty `org.website`** (the other 167 already had a Wikidata address that the crawl
  re-verified). That is 71% of the 2,807 candidates and 87% of the 2,804 that were actually crawled.
  2,032 `org` rows now carry a website; the difference are orgs whose address came from another ingestor
  and where `org.website` was already set.
* **Public general e-mail: 832** orgs, from the `mailto:` links of the page proven to be theirs.
  `info@` is 608 of them (73%); the rest are `contact@`, `office@`, `development@`, `volunteer@`,
  `intake@`, `communications@`, `donations@`, `mail@` and similar role boxes.
* **Phone: 1,938** orgs. 1,141 come from a `tel:` link the site publishes; the other 797 are the filer
  phone on the organisation's own Form 990, and each of those rows says so in its `note`.
* **Requests: 5,941** lines in `logs/ingest-org_contacts.jsonl` (43 bulk batches + robots.txt + pages).
  Every value in the table traces to a `source.id`, i.e. to a cached response under `data/raw/org_contacts/`.
* The run table holds 2,159 `done`, 495 `dead`, 150 `blocked_robots` and 3 not yet crawled (`seed`).
* The crawl runs as shard workers (`ORG_CONTACTS_SHARD=i/n`), each its own `common.fetch` client at
  **1 request/second**; the pool was split so that no host is visited by more than one shard and no host
  receives more than three requests in a run.

### What is still missing, and why

* **~8,600 orgs have no candidate at all.** 7,091 of them have no EIN (government bodies, CoC lead
  agencies, housing authorities, facility operators), so no Form 990 exists to publish a website; the
  SAM.gov entity API that would cover their UEI is blocked (BLOCKERS.md `## org_contacts`, key
  `SAM_API_KEY`, <https://api.data.gov/signup/>). Among the 11,404 EIN'd orgs, only **4,313 appear in any
  harvested filing** at all — the rest file the 990-N e-postcard or on paper, and neither publishes a
  website.
* **1,510 EIN'd orgs appear in a 990 but publish no `WebsiteAddressTxt`** (blank or `N/A` on the return).
  That is the organisation's own omission; nothing is guessed to fill it.
* **814 of the 2,804 crawled candidates did not verify**: 495 never answered (`phase='dead'` — mostly
  domains filed on a 2016-2021 return), 150 disallow crawling in `robots.txt` (`phase='blocked_robots'`,
  mostly a 403 on `robots.txt`), and 169 answered 200 but no page carried the org's name or the page was a
  parked / "domain for sale" placeholder. They are kept, with the reason, in `org_contacts_site` and are
  what a future run retries first (`attempts < 2` → the next address the same org filed is tried).
* **Not every org publishes a general mailbox.** Many sites only show a web form or a staff directory;
  per the brief, no individual's address is taken, so `org.email` stays NULL for them (1,972 of the 2,804
  crawled candidates produced no acceptable role mailbox).
* **Resume**: `python3 -m ingest.run org_contacts` continues where it stopped (batch table + filing table +
  site table + `data/raw/org_contacts/site_results.*.jsonl`). Knobs: `ORG_CONTACTS_YEARS`,
  `ORG_CONTACTS_BATCHES`, `ORG_CONTACTS_HARVEST`, `ORG_CONTACTS_BUDGET`, `ORG_CONTACTS_SHARD`,
  `ORG_CONTACTS_MAX_SITES`, `ORG_CONTACTS_EMAIL_TIER2`, `ORG_CONTACTS_TIMEOUT`.
