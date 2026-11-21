# officials: who answers for homelessness policy and money in each county

Domain file: `ingest/domains/officials.py`. Tables: `officials_person`, `officials_office`,
`officials_county`. Run: `python3 -m ingest.domains.officials [--states PA] [--refresh]`,
`--selftest` (PA only).

## Sources and licences

| Source | URL | Licence |
| --- | --- | --- |
| Current members of Congress | https://unitedstates.github.io/congress-legislators/legislators-current.json | CC0 1.0 (unitedstates/congress-legislators) |
| State legislators, one CSV per state | https://data.openstates.org/people/current/<st>.csv | CC0 1.0: the openstates/people README says the data "is in the public domain in the United States with all copyright waived via a CC0 dedication"; LICENSE file is CC0 1.0 Universal |
| 119th Congress districts to 2020 counties | https://www2.census.gov/geo/docs/maps-data/data/rel2020/cd-sld/tab20_cd11920_county20_natl.txt | Public domain (Census) |
| 2024 state senate districts to counties | .../cd-sld/tab20_sldu202420_county20_natl.txt | Public domain (Census) |
| 2024 state house districts to counties | .../cd-sld/tab20_sldl202420_county20_natl.txt | Public domain (Census) |
| CT town-level versions of the three files | .../cd-sld/tab20_{cd11920,sldu202420,sldl202420}_cousub20_st09.txt | Public domain (Census) |
| CT towns to 2022 planning regions | https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_gaz_cousubs_09.txt | Public domain (Census) |
| HUD Public Housing Authorities layer | https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/Public_Housing_Authorities/FeatureServer/0 | Public domain (HUD) |

Relationship file headers checked, not assumed: `GEOID_<TAG>_20`, `NAMELSAD_<TAG>_20`,
`GEOID_COUNTY_20`, `AREALAND_COUNTY_20`, `AREALAND_PART` (UTF-8 BOM on the first column).
`share = AREALAND_PART / AREALAND_COUNTY_20`, rounded to 4 places; districts with zero land
overlap (water only) and the `ZZZ` "not defined" rows are dropped.

## Method notes

- District matching: Census district names and Open States labels are normalised to an
  order-kept token string (`5th Bristol`, `Belknap 3`, `Chittenden-Southeast`, `44B`). Order
  matters: "Middlesex and Worcester" and "Worcester and Middlesex" are two MA senate districts.
- Idaho elects seats A and B per whole district: `16B` falls back to district 16.
- New Hampshire floterial House districts (55 members, e.g. "Rockingham 33") are not Census
  areas. They are linked to the county they are named for with `share` NULL.
- Connecticut: geo_city uses the 2022 planning regions; the 2020 county files use the old
  counties. CT shares are built from town pieces summed into planning regions (shares per
  region sum to 1.0 for each level).
- Nebraska (unicameral) and the DC Council are stored as `state_upper`. DC at-large members and
  the chair link to 11001 with share 1.0. Senators link to every county in their state with 1.0.
- PHA county: `STATE2KX + CURCNTY` (3,696 of 3,697). Its office location only, not its service
  area. The layer carries no executive director name, so `exec_name` is NULL; `exec_phone` and
  `exec_email` are HUD's published executive director contact fields.
- Privacy: Open States `birth_date`, `gender`, `biography`, `image` and all social columns are
  dropped at read. Only capitol/D.C. office and district office contacts are stored.

## Coverage

Full run 2026-11-21 (51 jurisdictions: 50 states + DC; 3,144 counties):

| Level | People/offices | Counties linked |
| --- | --- | --- |
| federal_house | 434 | 3,117 |
| federal_senate | 100 | 3,143 (all but DC, which has none) |
| state_upper | 1,974 | 3,123 |
| state_lower | 5,362 | 3,030 |
| pha | 3,697 offices | 2,013 |

28,208 county links in total. Gaps:
- U.S. House: no member in the source for FL-20 and TX-23 (vacant per the source), so the 27
  TX-23 counties have no House row.
- State legislatures: 47 Census districts have no member in Open States (vacancies or lag),
  leaving 20 counties without a lower-house member and 21 without an upper-house member, apart
  from Nebraska and DC, which have no lower house. Maine's two non-voting tribal
  representatives have no district geography and are stored unlinked.
- PHA: 79 territory PHAs (PR, GU, VI, MP) are outside geo_city. `pha:NJ114` (Middlesex County,
  NJ) has no county code and no coordinates, so it has no county link.
- CoC lead contacts: not ingested. HUD Exchange publishes them at
  https://www.hudexchange.info/grantees/contacts/ (server-rendered rows with "Collaborative
  Applicant" name, address, phone, email, and an xls export via `?export=1&params=<json>`), but
  the site answers HTTP 404 to every non-browser User-Agent, including this project's UA. It was
  not fetched with a spoofed browser UA. The per-county research files carry CoC contacts.
- DC Council rows come from Open States `dc.csv` and were not spot-checked; that file may lag
  council changes.

## Verified

Checked 2026-11-21 against the live official pages:

1. https://www.house.gov/representatives : PA 16th is Mike Kelly (R), (202) 225-5406; matches
   the 42049 federal_house row (share 1.0). https://kelly.house.gov/ shows 1707 Longworth,
   (202) 225-5406, matching `address`/`phone`.
2. https://www.senate.gov/states/PA/intro.htm : John Fetterman (D), 142 Russell, (202) 224-4254;
   David McCormick (R), 702 Hart, (202) 224-6324. Both match the 42049 federal_senate rows.
3. https://www.palegis.us/senate/members : Daniel Laughlin, District 49, and Scott E.
   Hutchinson, District 21 ("Erie (part)"), are the two senators listing Erie; the DB gives 49
   (share 0.902) and 21 (share 0.098) for 42049.
4. https://www.palegis.us/house/members : the members listing "Erie (part)" are Harkins 1,
   Merski 2, Bizzarro 3, Banta 4 and Roae 6 (Crawford and Erie); the DB's 42049 state_lower
   rows are exactly these five.
5. https://www.hace.org/ : Housing Authority of the City of Erie, 606 Holland Street, Erie PA
   16501, phone (814) 452-2425; matches `pha:PA013` (phone, address). HUD's executive director
   phone in the same record is 814-452-4225 and is stored as published.
6. Extra, CT town crosswalk: https://www.house.gov/representatives lists CT-2 Courtney
   (202) 225-2076, CT-3 DeLauro (202) 225-3661, CT-5 Hayes (202) 225-4476; these are the three
   House rows for South Central CT planning region 09170 (shares 0.098, 0.837, 0.065).
