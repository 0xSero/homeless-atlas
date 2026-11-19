# award-places — where the money is spent, city by city
The `award` table has place_state for 18,659 of 18,864 rows but **place_city and place_county are empty for every row**,
so no city page can show money and the San Francisco brief cannot compute its own award total.

Write ingest/domains/award_places.py that updates existing award rows (do NOT re-download everything):
- USASpending /api/v2/search/spending_by_award/ accepts `fields` including
  "Place of Performance City Code", "Place of Performance County Code", "Place of Performance State Code",
  "Recipient Location City"/"County Code"/"Zip", and the award detail endpoint
  /api/v2/awards/<generated_internal_id>/ returns place_of_performance{country_name,state_code,county_name,city_name,zip}
  plus recipient{location{address_line1,city_name,county_name,state_code,zip}}.
- Fill award.place_city / place_county from the detail endpoint for awards missing them, batched and cached.
- Also fill org.city / org.state / org.zip from recipient.location where the org row has no address
  (this is what lets city pages rank organisations by money).
accept: place_city non-null for >=60% of awards, org address non-null for >=3,000 orgs,
        research/award_places.md documents the fields used and the residual gap.
