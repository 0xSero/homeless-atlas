# docs — audits, court cases, studies, investigative series
Write ingest/domains/docs.py: HUD OIG + GAO homelessness audits, city controller/auditor reports
(SF, LA, NYC, Seattle, Denver, San Diego, Portland, Boston, DC, Houston), HUD AHAR narrative,
major court decisions (Martin v. Boise, Grants Pass v. Johnson, LA Alliance), and the strongest
investigative series (LA Times, SF Chronicle, ProPublica, NPR, Guardian). Use each site's search/RSS
or the primary PDF. Store in `doc` with publisher, date, place and a 2-sentence sourced summary
that is a paraphrase, never a quote over 25 words.
accept: >=300 docs, >=20 court decisions, every row with a working url (HEAD 200 verified)
