# Homeless Atlas: notes for agents

Public-record atlas of homelessness. Start with `README.md`, then `GOALS/GOAL.md` (the contract)
and `docs/INGEST_CONTRACT.md` (how a data domain is added).

## Skills

- **County** work (add a county, fix a county page, run the contributor service):
  `.agents/skills/add-county/SKILL.md`.
- **Records** requests (FOIA, Right-to-Know, open records; follow-ups and appeals):
  `.agents/skills/file-records-request/SKILL.md`.

## Rules

- Public data only. No scraping behind logins, CAPTCHAs or paywalls.
- Never invent a number, a URL, a quote or a contact. Every figure carries a `source_id`; derived
  figures set `is_est` and render with a dagger.
- People: published professional roles and official office contacts only. No home addresses,
  no personal phone numbers, and no individual experiencing homelessness is identified.
- Crime and health figures are area-level context, never attributed to homeless people.
- Records letters are templates, not legal advice. Only facts verified against the official
  text appear in them.
- SQLite allows one writer: `ingest/common.db()` holds the lock. Run ingestors one at a time.
- One domain or one site change per commit. One county per pull request.

## Checks

- `python3 scripts/verify.py`: the completion gate for the dataset and site.
- `python3 scripts/county.py check <FIPS>`: one county file.
- `python3 scripts/refresh.py --check`: which upstream sources changed.
- `python3 scripts/build_site.py --fast --force --out /tmp/atlas-site`: a local preview; never
  deploy a `--force` build.
