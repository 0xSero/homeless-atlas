---
name: add-county
description: Add or update one U.S. county in the Homeless Atlas (research/county/<ST>/<FIPS>-<slug>.md) and open its pull request, or install the optional background service that keeps a county's facts checked. Use when asked to add a county, contribute local homelessness data, fix a county page, or run the atlas contributor service.
---

# Add a county

One county, one file, one pull request. A machine re-checks every number, phone number and link
in the file, so the work is finding public sources and copying them exactly.

## Steps

1. **Find the FIPS.** Five digits, state then county (Erie County, PA is `42049`). Look it up on
   the county's atlas page (`https://homeless.sybilsolutions.ai/county/<FIPS>.html`) or in the
   Census gazetteer. Done when you have five digits and the county name matches.
2. **Start the file.**
   ```bash
   python3 scripts/county.py new <FIPS>
   ```
   It writes `research/county/<ST>/<FIPS>-<slug>.md` with the county, its Continuum of Care and
   the offices the atlas already knows. Done when the file exists and its front matter parses.
3. **Collect sources.** Search official sources first: the county, the city, the housing
   authority, the Continuum of Care lead, the state, audits, budgets, plans. Then reputable local
   news. Add each to `sources:`. For a number you want on the county page, add a `claims:` entry
   whose `quote` is copied exactly from the document and contains the value. Done when each
   number in your narrative has a source and each claim has a quote.
4. **Name who is responsible.** Under `responsible:`, add the offices that answer for each figure,
   with the official page that publishes their office phone, email and address. Add each agency's
   published open records officer with `for: [records]`; the atlas addresses its ready records
   requests to them. Done when every entry has a `url` that shows its contact details.
5. **Add help lines.** Under `help:`, add shelters, crisis lines and outreach exactly as each
   provider lists them on its own page. These go on the printable street flyer. Done when every
   phone appears on its `url`.
6. **Write the narrative** under the template headings. Every sentence cites `[n]`, the order of
   `sources`. Done when no heading is empty; write "No public source found" where that is true.
7. **Check.**
   ```bash
   python3 scripts/county.py check <FIPS>
   ```
   Fix or remove each line that is not `verified`. Done when it exits 0.
8. **Preview.** `python3 scripts/county.py preview <FIPS>` and open the two paths it prints (the
   county page and the flyer). Done when your sources, contacts and help lines appear.
9. **Open the pull request.**
   ```bash
   python3 scripts/county.py pr <FIPS> --dry-run   # read the commands and the PR body
   python3 scripts/county.py pr <FIPS>
   ```
   It commits only your county file on a fresh branch, pushes to your fork and opens the PR with
   the check results in the body. Done when the PR URL prints. CI re-runs the check on the PR.

## Rules

- **Public** sources only: readable by anyone without an account. Cite a free syndicated copy of
  a paywalled article, never an archive of one.
- **Exact** quotes. Copy them; the checker normalizes quotes, dashes and thousands separators.
- **Official** office contacts only: office lines, office email, office street address, as the
  office publishes them. Never a personal cell number, a home address or a personal account.
- **Groups**, never individuals: no person experiencing homelessness is named, pictured or
  described in a way that identifies them.
- **Measures** stay distinct. HUD's point-in-time count is one night in January. A local figure
  that adds people in housing programs, or counts a whole year, is a different measure: name it
  as such in the narrative.
- Crime and health figures are area-level context only.

## Keep it live (optional)

The contributor service re-checks your counties on a schedule and proposes a draft pull request
when a source moves or a contact changes. It runs only an unmodified copy of the upstream code,
in its own folder, capped at 1 GB of memory.

```bash
python3 scripts/county.py service install --dry-run   # read every file it would write
python3 scripts/county.py service install
python3 scripts/county.py service status
python3 scripts/county.py service uninstall --yes     # removes all of it
```

What it can touch and why it is safe: `docs/CONTRIBUTOR_SERVICE.md`.

## When the checker says

| Result | Meaning | Do |
|---|---|---|
| `quote_missing` | The quote is not in the fetched text | Copy it again from the document; check the PDF has a text layer |
| `value_missing` | The quote is there, the value is not | Put the number inside the quote |
| `gone` | 404 and no archive copy | Find the document's new address, or drop it |
| archive-only | The publisher refused automated reads; read from the Internet Archive | Fine for documents; contacts must verify on the live page |
| phone or email not ok | Not on the live `url` | Use the page that actually publishes it |
