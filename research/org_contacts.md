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
