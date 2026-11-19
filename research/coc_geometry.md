# coc_geometry — CoC, state and place polygons for the maps

Domain owner: `ingest/domains/coc_geometry.py`. Writes `geo_coc.geometry`, `geo_state`,
`geo_city` (kind='place') and the two map exports `data/export/coc.geojson`,
`data/export/state.geojson`. Domain-private bookkeeping tables: `coc_geometry_page`,
`coc_geometry_progress` (resumability).

No shapely, no GEOS, no pyshp: rings are simplified with a pure-python Douglas-Peucker
(`_dp_keep`) and shapefiles are read with a small struct-based `.shp`/`.dbf` reader.

## Sources (all keyless, all public domain, all fetched through `common.fetch` -> `data/raw/coc_geometry/`)

| # | URL | What it gives | Notes |
|---|---|---|---|
| 1 | `https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/CoC_Geo_Type/FeatureServer/0/query` | HUD CoC boundary polygons, `f=geojson`, 25 features/page, `geometryPrecision=5`, `maxAllowableOffset=0.0005` (~55 m server-side transfer reduction) | 387 features; layer name `CoC_Geo_type`; description says "current for FY2024"; `copyrightText` = HUD; `licenseInfo` is **null** in the layer metadata, so we record "public domain (US Government work)" and repeat HUD's no-warranty disclaimer |
| 2 | same service `?where=1&returnCountOnly=true` | feature count (387) | the figure quoted in the run notes |
| 3 | `https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip` | 2023 cartographic boundary, state (3,249,332 B, Last-Modified 2024-04-16) | 56 features (50 states, DC, PR, VI, GU, AS, MP) |
| 4 | `https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_place_500k.zip` | 2023 cartographic boundary, place (22,856,787 B) | attributes only here — the 500k file carries no centroid fields |
| 5 | `https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_place_national.zip` | authoritative internal point (INTPTLAT/INTPTLONG) + land area per place (1,220,953 B) | used for `geo_city.lat/lon` |
| 6 | `https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv` | PEP vintage 2024 population, SUMLEV 040 (states) and 162/170 (incorporated places / MCDs) (7,087,080 B, Last-Modified 2025-05-15) | CDPs have no row here -> `pop` stays NULL, never estimated |

Live re-check on 2026-11-19 (all HTTP 200): service metadata (9,892 B), feature count
(`{"count":387}`), and HEAD on the four Census files (sizes above match the cached copies).

## Method

1. Page the HUD feature service (25 features/request, `orderByFields=OBJECTID_1`), cache every
   page, and keep a named copy `data/raw/coc_geometry/coc_page_NNNN.json` per page.
2. Simplify each ring with Douglas-Peucker on equirectangular-scaled coordinates
   (`x * cos(mean lat)`), so the tolerance is roughly metric. Tolerance is escalated along
   `TOL_LADDER = [0.002, 0.003, 0.004, 0.006, 0.008, ...]` until the estimated CoC export fits
   `COC_EXPORT_BUDGET = 8 MB`. Chosen: **tol = 0.002 deg (~220 m), 214,700 points kept** from the
   671,125 points the service delivers across the 16 pages. Coordinates are rounded to 4 dp for the
   export and stored the same way in `geo_coc.geometry`.
3. `geo_coc.lat/lon` = area centroid of the largest outer ring (derived here, not published —
   `is_est` is not set because the schema has no flag on `geo_coc`; the derivation is documented in
   the module docstring).
4. States: `.shp` rings grouped into Polygon/MultiPolygon by containment, simplified at the same
   tolerance; `land_sqmi` = ALAND / 2,589,988.110336; `pop` from PEP SUMLEV 040.
5. Places: attributes from the cartographic place file, internal points from the gazetteer,
   population from PEP, and `coc_num` assigned by point-in-polygon against the **simplified** CoC
   rings (smallest containing CoC wins, holes respected, bbox pre-filter).
6. Every write goes through `common.upsert` on the natural key (`coc_num` / `fips` / `geoid`);
   a rerun re-uses the disk cache and does not duplicate rows (checked: see Verified).

## Verified

Five rows re-checked against the live source on 2026-11-19 (responses saved as
`data/raw/coc_geometry/verify_*.json`), comparing name/state/geometry/centroid to the DB:

1. `AK-500` — live `.../query?where=COCNUM='AK-500'&outFields=COCNUM,COCNAME,ST_1,STATE_NAME,Geo_Type,HudNum&returnGeometry=true&f=geojson`
   -> "Anchorage CoC", ST_1=AK, Geo_Type="Other Urban CoCs", centroid (61.14966, -149.10639);
   DB "Anchorage CoC"/AK, (61.14969, -149.10652). Match.
2. `CA-600` — same query for `CA-600` -> "Los Angeles City & County CoC", CA, "Major Cities",
   centroid (34.36908, -118.21773); DB (34.36882, -118.21792); the `coc.geojson` feature carries
   the same 498-point MultiPolygon as the DB row. Match.
3. `NY-600` — "New York City CoC", NY, "Major Cities", centroid (40.68959, -73.87183);
   DB (40.68927, -73.87162). Match.
4. `PR-503` — "South-Southeast Puerto Rico CoC", PR, centroid (18.16842, -66.48990);
   DB (18.16828, -66.48952), 26,035 B of stored geometry. Match.
5. `geo_state` fips `50` (Vermont) — live `cb_2023_us_state_500k.zip` re-downloaded and reparsed:
   NAME=Vermont, STUSPS=VT, ALAND/2589988.11 = 9,217.3 sq mi, centroid (44.07520, -72.66262);
   DB land_sqmi=9217.3, (44.0752, -72.66262), pop 648,493. Match.
6. `geo_city` `0820000` (Denver) — live gazetteer row: `Denver city`, CO, INTPTLAT 39.76185,
   INTPTLONG -104.881105; live `sub-est2024.csv` SUMLEV 162 / STATE 08 / PLACE 20000:
   POPESTIMATE2024 = 729,019. DB: name Denver, CO, (39.76185, -104.881105), pop 729,019,
   coc_num CO-503. Match.

Also re-checked that the 27 CoCs in `geo_coc` without geometry really have no polygon in the
FY2024 layer: `where=COCNUM='NY-516'`, `'MD-508'`, `'TX-625'` each returned HTTP 200 with
`features: []`, so the NULL geometry is a property of the published layer, not a bug here.

Idempotency: two consecutive `python3 -m ingest.run coc_geometry` runs left
`geo_coc` 414 / `geo_state` 56 / `geo_city` 56,819 rows unchanged.

Export gate (re-measured after the run):
`data/export/coc.geojson` 4,204,831 B / 387 features, `data/export/state.geojson` 1,387,139 B /
56 features — **5,591,970 B total (5.6 MB) < 12 MB**, both load with `json.load` and both are
`FeatureCollection`s of Polygon/MultiPolygon with every ring closed and >= 4 points (checked: 0
unclosed or degenerate rings in either file).

## Coverage

- **CoC boundaries: 387 of 387 features in HUD's FY2024 `CoC_Geo_type` layer — 100% of the
  published boundary universe.** 387 rows in `geo_coc` have non-NULL `geometry` and non-NULL
  `lat/lon` (acceptance threshold was >= 380). All 56 state/territory prefixes are represented,
  including PR (2), VI (1), GU (1), AS (1), MP (1).
- `geo_coc` holds 414 rows: the 387 polygons plus **27 CoCs that exist in HUD's CoC/PIT universe
  but have no polygon in this layer** — AR-502/504/506/509/510/512, CA-528, MA-501/508/510/514/517/519,
  MD-500/502/507/508/509/510/512, MI-513, NY-504/506/516/607/609, TX-625. Those keep HUD's other
  attributes and a NULL geometry, which is the honest representation (verified live, see above).
- `geo_state`: 56 of 56 Census state-equivalents. `pop` for 51 (PEP sub-est2024 SUMLEV 040 has no
  rows for PR/VI/GU/AS/MP); `lat/lon` for all 56.
- `geo_city`: **32,608 places** written by this domain (the 2023 cartographic place file).
  32,329 have a gazetteer internal point, 19,471 have a PEP 2024 population, and **32,241 (98.9%)
  were assigned a CoC** by point-in-polygon. Missing: 13,137 places with no PEP row (CDPs and
  other unincorporated places are not in the sub-county estimates -> `pop` NULL, not modelled) and
  367 places that fall in no CoC polygon (offshore/water CDPs, places split across a CoC boundary
  whose point lands in a gap). Counties are not written here — `places`/`hud_pit` own `kind='county'`.
- Not attempted (out of scope for this domain, and not needed for the map):
  HUD's CoC↔county crosswalk, block-level or tract geometry, zip boundaries,
  historical (pre-FY2024) CoC boundary vintages.

## Operational note for the orchestrator

`hud_pit` also writes `geo_coc.geometry` (a coarser copy paged from
`.../Continuum_of_Care_Grantee_Areas/FeatureServer/0` with `geometryPrecision=3`,
`maxAllowableOffset=0.005`). Both domains target the same column, so **whichever ingestor runs
last wins** — after a `hud_pit` run the stored rings are ~500 m and 3 dp, after a `coc_geometry`
run they are ~220 m and 4 dp. Both are valid GeoJSON and both keep 387 non-NULL geometries; if the
map looks blobby, re-run `coc_geometry` last. The exports in `data/export/` are written only by
`coc_geometry`, so they are unaffected by that race.
