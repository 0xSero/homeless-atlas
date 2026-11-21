# student-homelessness: McKinney-Vento homeless students by school district, rolled up to county
Public schools count every enrolled student who is homeless under the McKinney-Vento Act,
which reaches families the PIT count misses (doubled-up, motels). Ingest EDFacts file FS118
(data group 655, "Homeless Students Enrolled") by local education agency (LEA) and school
year, then map each LEA to a county with the NCES Common Core of Data (CCD) LEA directory
(LEAID to county FIPS). Write metric rows entity_type='county' (`homeless_students`, sum of
LEAs whose CCD county is that county, is_est=0, with the LEA count in the note) and keep the
LEA-level rows in a `student_homelessness_lea` table. Keep the EDFacts suppression flags as
NULL, never 0.
Status of the inputs: on 2026-11-20 the ed.gov data-file pages (www2.ed.gov EDFacts data
files index) answered HTTP 403 to automated fetches, so the direct file URLs for FS118 and the
CCD LEA directory are still to be found (try the ED Data Express download API, the
data.ed.gov CKAN catalog, and the NCES CCD data file pages).
accept: >=2,500 counties with homeless_students for the latest school year; spot-check 5 LEAs
        against the published file in research/student-homelessness.md.
