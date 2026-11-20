# org-web — websites and contact addresses for more of the 22,180 organisations
The contacts harvest stopped at 2,032 websites and 832 emails because it only walked the 2017–2026
IRS e-file index batches. The target is 5,000 websites and 2,000 emails, and the same XML carries
both fields.

Extend ingest/domains/org_contacts.py to walk the full e-file index (2011 onward), preferring one
filing per organisation per year, and read from each XML: WebsiteAddressTxt, the Part VII mailing
address block if it holds an email, and the "email address" element some filers use in Part VI.
Match filings to org rows on EIN first, normalised name+state second; never create an org row for a
filing that cannot be matched with a published identifier.

A website is written only after a GET answers 200/301/302 — 403/429 mean the host blocks bots and
count as live, everything else is discarded. Keep the check history in org_website_check.

accept: org.website >= 5000, org.email >= 2000, every written URL returning a non-error status,
        research/org-contacts.md updated with the batch list and the per-year hit rate.
