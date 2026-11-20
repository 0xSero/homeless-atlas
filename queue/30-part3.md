# part3 — what each organisation says it does, verbatim from its own Form 990
Part III of the 990 is the mission and the program-service descriptions. It is the only place an
organisation states in its own words what the money bought, and the XML already downloaded for the
contacts work contains it.

Add a table `org_program(org_id, year, ein, mission, program_text, revenue_total, expenses_program,
source_id, UNIQUE(org_id, year))` via the usual migration in ingest/schema.sql, and a parser in
ingest/domains/program.py that reads the cached XML under data/raw/org_contacts/ (no new fetching).

Never paraphrase: store the filed text verbatim, truncated only at 4,000 characters with an explicit
"…(truncated)" marker. Expenses attributable to program services is `TotalProgramServiceExpensesAmt`
where present — that is the number that lets the site show what share of spend is program rather than
administration.

accept: >=5000 orgs with a program row, >=3000 with program-service expenses, research/program.md
        quoting three filings verbatim next to their XML URL so the extraction can be spot-checked.
