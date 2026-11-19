# subawards — the second hop: who the prime recipients pass the money to
`award` shows who holds federal money; subawards show what they then hand on. For a nonprofit atlas this
is the difference between "X got $40M" and "X passed $31M to 60 local providers".

Write ingest/domains/subawards.py using USASpending's subaward endpoints:
POST /api/v2/search/spending_by_award/ with award_type_codes ["02","03","04","05"] to find prime
awards (already in hand), then the subaward search endpoint (/api/v2/search/spending_by_subaward/) per
prime award, or the bulk subaward download for scale. Store in `award` with award_type='subaward'
and a `program` label like 'subaward of <prime award id>', keeping the prime's award_id in description.
accept: >=2,000 subawards with recipient name, amount and prime award id; >=100 distinct subrecipients
        appearing as org rows; research/subawards.md documents coverage of the prime set.
