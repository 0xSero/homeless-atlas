---
name: file-records-request
description: Prepare a public records request (state open records law or federal FOIA) for homelessness contracts, payments and outcome reports in any U.S. county, using the Homeless Atlas's ready letters. Use when someone wants to file a FOIA, Right-to-Know or open records request, ask a county or housing authority for contracts, or follow up on or appeal a request that went unanswered.
---

# File a records request

The atlas publishes ready letters for every county, with each legal fact checked against the
official text of the law. Your job is to fill in the requester's details, show them the letter,
and let them send it. You prepare; the person sends.

## Steps

1. **Find the county FIPS** (five digits; Erie County, PA is `42049`). Done when the county
   name on `https://homeless.sybilsolutions.ai/county/<FIPS>.html` matches.
2. **Fetch the letters.**
   `https://homeless.sybilsolutions.ai/api/records/<FIPS>.json` lists `requests[]`, each with
   `agency`, `law`, `records`, `subject`, `body`, `deadline`, `delivery`, `cautions`,
   `followup` and `appeal`. Done when you have picked the request the person wants; ask them if
   it is unclear.
3. **Read the cautions to the person.** Some states let only their own residents use their law;
   the caution says what to do instead (often the federal FOIA letter). Done when the person
   qualifies for the letter you chose.
4. **Fill the placeholders** `{{requester_name}}`, `{{requester_address}}`,
   `{{requester_email}}` and `{{date}}` with what the person gives you. Change nothing else
   unless they ask. Done when no `{{` remains.
5. **Hand it over.** Show the subject, the recipient from `delivery`, and the letter. The person
   sends it from their own email, the agency portal, or by mail. Done when they have it. For a
   person on a phone, the county page `county/<FIPS>-records.html` does the same in one screen.
6. **Track the deadline.** `deadline` gives the response time under that law. Business days skip
   weekends and holidays. Tell the person the date. If it passes with no answer, use `followup`;
   if the agency denies the request or stays silent, use `appeal`, which names where to file
   and by when.

## Rules

- **Template, not legal advice.** Say so once when you hand over a letter.
- **Person sends.** Never send a request, sign in to a portal or submit a form for someone.
- **Facts from the file.** Never add a deadline, fee rule or appeal route that is not in the
  JSON; a missing field means it could not be verified, so leave it out.
- **Their details stay with them.** Use the requester's details only to fill the letter.

State-by-state law summaries, with quotes: `https://homeless.sybilsolutions.ai/records.html` and
`/api/records-laws.json`.
