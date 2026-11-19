# spend-vs-outcome — join the money to the outcomes
Once `spm` and `subawards` exist, add derived metrics that put spend and outcome side by side per CoC:
  'spend_per_exit_to_housing' = federal obligated (place of performance = CoC state/county)
                                 ÷ SPM exits to permanent housing, and
  'spend_per_person_placed'  = federal obligated ÷ (pit_total − pit_total_next_year), where negative
                                 denominators are dropped, not flipped.
Every figure is_est=1 with the formula in the note. Write scripts/build_outcome_metrics.py (not a domain
ingestor) so it can be recomputed whenever the inputs change. Surface them on /rankings.html as
"Most and least spent per exit" — with an explicit warning that a low number can mean either efficiency
or an accounting artefact, and that CoC spend cannot be separated from city and county spend.
accept: >=200 CoCs with both inputs; the rankings page gains the table with the caveat text.
