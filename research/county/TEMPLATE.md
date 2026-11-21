---
# Copy this file to research/county/<ST>/<5-digit FIPS>-<slug>.md, e.g. research/county/PA/42049-erie.md
# Then run: python3 -m ingest.domains.local_sources --county <FIPS>
county_fips: "00000"          # quoted, always: unquoted 01001 is read by YAML as the number 513
county: Example County        # as the Census Bureau names it
state: XX                     # 2-letter postal code; must match the folder name
coc: [XX-000]                 # the HUD Continuum(s) of Care covering the county
updated: 2026-01-01           # the day you last edited this file

sources:
  - url: https://example.gov/report.pdf
    kind: report              # report | audit | study | article | dataset | court | program | plan | org
    title: Exact title as published
    publisher: Example County Department of Human Services
    published: 2026-06-01     # optional
    claims:                   # optional: numbers the county page may show
      - metric: overdose_deaths      # snake_case; stored as local_overdose_deaths
        value: 116
        year: 2023
        unit: persons
        quote: "116 overdose deaths"  # short and exact; must appear in the document with the value
      # Three claim names feed the WTF street flyer (/flyers.html) when the county is its own CoC:
      #   pit_total, pit_unsheltered  a local point-in-time count newer than HUD's (one night in January)
      #   coc_award_total             HUD's Continuum of Care award total for the CoC, from HUD's report

responsible:                  # optional: the public offices that answer for the figures
  - for: [pit_count, coc_funding]    # pit_count, coc_funding, county_budget, shelter_beds, overdose,
                                     # housing_authority, behavioral_health, encampment_policy,
                                     # records (the office's open records officer: used as the
                                     # addressee of the ready records-request letters),
                                     # call (the one number printed on the street flyer; usually
                                     # the elected official who oversees the money)
    office: Example County Department of Human Services
    role: Lead applicant, XX-000 Continuum of Care
    name: Jane Doe                   # optional: the official holding the role, as published
    title: Director                  # optional
    phone: "555-555-0100"            # the office line exactly as published on `url`
    email: office@example.gov        # the official address exactly as published on `url`
    address: "1 Main St, Example, XX 00000"
    url: https://example.gov/contact # the official page that publishes these details
    elected: false                   # true for elected officials

help:                         # optional: service lines for printable street flyers
  - name: Example Mission men's shelter
    what: Emergency shelter for men, meals   # short, as the provider describes it
    phone: "555-555-0101"                    # as published on url
    address: "2 Main St, Example, XX 00000"  # as published on url
    hours: "24/7"                            # optional, as published
    url: https://example.org/shelter
---

## Summary

Three to five sentences: the size of the problem here, the trend, and the one or two facts a
reader should leave with. Every sentence cites a source as [1], [2] (the order of `sources`).

## The count

Point-in-time counts for the CoC over time, sheltered and unsheltered, and what the local
report says about how the count was done.

## Who is homeless here

Families, youth, veterans, chronically homeless, age, race, and where people sleep, as the
published sources report them. Groups only; never a named or pictured individual.

## Mental health and addiction

Overdose deaths, treatment capacity, crisis services, shortage designations, and what the
county's own behavioral health reports say.

## Housing and money

Rents, vacancy, voucher wait lists, eviction filings, and the money: federal awards, the county
and city budget lines for homelessness, and what they buy.

## Shelters and providers

Who runs shelter, outreach and housing programs, how many beds, and what the inventory shows.

## Local government and law

Ordinances on camping and sitting, encampment policy, court cases, and who decides what.

## What is changing

New programs, closures, lawsuits, ballot measures, and plans, with dates.

## Gaps in the record

What a reader would want to know that no public source answers, and who could publish it.

## Sources

1. Publisher, *Title*, date. URL
