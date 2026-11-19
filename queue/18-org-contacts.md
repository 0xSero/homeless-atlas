# org-contacts — published website, email and phone for each organization
Write ingest/domains/org_contacts.py: for orgs already in the database, find the organisation's own published
website (from Form 990 XML <WebsiteAddressTxt>, ProPublica profile, or the org's SAM registration), and the
public general contact email/phone that the organisation itself publishes on its site (mailto: on the homepage
or /contact). Only org-level contact, never an individual's personal address or mobile. Respect robots.txt,
1 req/s, cache every page under data/raw/org_contacts/.
Store in org.website, org.email, org.phone with source_id pointing at the page that published it.
accept: >=2,000 orgs with a verified website, >=800 with a public org email, every value traceable to a URL.
