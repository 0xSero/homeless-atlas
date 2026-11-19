# coc_geometry — CoC, state and place polygons for the maps

Domain owner: `ingest/domains/coc_geometry.py`. Writes `geo_coc.geometry`, `geo_state`,
`geo_city` (kind='place') and the two map exports `data/export/coc.geojson`,
`data/export/state.geojson`. Domain-private tables: `coc_geometry_page`, `coc_geometry_progress`
(resumability) and `coc_geometry_coc` (per-CoC polygon provenance, see the operational note).

No shapely, no GEOS, no pyshp: rings are simplified with a pure-python Douglas-Peucker
(`_dp_keep`) and shapefiles are read with a small struct-based `.shp`/`.dbf` reader.

## Sources (all keyless, all public domain, all fetched through `common.fetch` -> `data/raw/coc_geometry/`)

| # | URL | What it gives | Notes |
|---|---|---|---|
| 1 | `https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/CoC_Geo_Type/FeatureServer/0/query` | HUD CoC boundary polygons, `f=geojson`, 25 features/page, `geometryPrecision=5`, `maxAllowableOffset=0.0005` (~55 m server-side transfer reduction) | 387 features; layer name `CoC_Geo_type`; description says "current for FY2024"; `copyrightText` = HUD; `licenseInfo` is **empty** in the layer metadata, so we record "public domain (US Government work)" and repeat HUD's no-warranty disclaimer |
| 2 | same service `?where=1%3D1&returnCountOnly=true` | feature count (387) | the figure quoted in the run notes |
| 3 | `https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip` | 2023 cartographic boundary, state (3,249,332 B) | 56 features (50 states, DC, PR, VI, GU, AS, MP) |
| 4 | `https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_place_500k.zip` | 2023 cartographic boundary, place (22,856,787 B) | attributes only — the 500k file carries no centroid fields |
| 5 | `https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_place_national.zip` | authoritative internal point (INTPTLAT/INTPTLONG) + land area per place (1,220,953 B) | used for `geo_city.lat/lon` |
| 6 | `https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv` | PEP vintage 2024 population, SUMLEV 040 (states) and 162/170 (incorporated places / MCDs) (7,087,080 B) | CDPs have no row here -> `pop` stays NULL, never estimated |

The five URLs above were re-downloaded live during the delivering run (byte counts above are the
live responses); the feature count came back as `{"count":387}`.

## Method

1. Page the HUD feature service (25 features/request, `orderByFields=OBJECTID_1`), cache every
   page, and keep a named copy `data/raw/coc_geometry/coc_page_NNNN.json` per page (16 pages,
   671,125 points delivered by the service).
2. Simplify each ring with Douglas-Peucker on equirectangular-scaled coordinates
   (`x * cos(mean lat)`), so the tolerance is roughly metric. Tolerance is escalated along
   `TOL_LADDER = [0.002, 0.003, 0.004, 0.006, 0.008, ...]` until the estimated CoC export fits
   `COC_EXPORT_BUDGET = 8 MB`. Chosen: **tol = 0.002 deg (~220 m), 214,700 points kept**.
   Coordinates are rounded to 4 dp for the export and stored the same way in `geo_coc.geometry`.
3. `geo_coc.lat/lon` = area centroid of the largest outer ring (derived here, not published —
   `is_est` is not set because the schema has no flag on `geo_coc`; the derivation is in the
   module docstring).
4. States: `.shp` rings grouped into Polygon/MultiPolygon by containment, simplified at the same
   tolerance; `land_sqmi` = ALAND / 2,589,988.110336; `pop` from PEP SUMLEV 040
   (51 of 56 rows — PEP has no state row for PR/VI/GU/AS/MP, so those stay NULL).
5. Places: attributes from the cartographic place file, internal points from the gazetteer,
   population from PEP, and `coc_num` assigned by point-in-polygon against the **simplified** CoC
   rings (smallest containing CoC wins, holes respected, bbox pre-filter).
6. Every write goes through `common.upsert` on the natural key (`coc_num` / `fips` / `geoid`).
   `geo_coc`, `geo_state` and `geo_city` are all written by more than one domain, so those three go
   through `_put()`, which applies the contract rule *never set `source_id` on a row you did not
   create*: if the existing row already carries a `source_id` belonging to another domain, the key is
   left alone. Within the rows this domain owns, `source_id` points at the source behind the
   published **number** touching that row, not at whichever file happened to supply a join key:
   * `geo_city` with a PEP 2024 population -> `sub-est2024.csv`; a place with no PEP row (CDPs) ->
     the place boundary file, because the row then carries no figure at all, only identity.
   * `geo_state` with a population -> `sub-est2024.csv`; the 5 territories PEP omits -> the state
     boundary file.
   * `geo_coc` -> the boundary service page URL the polygon came from (plus the private
     `coc_geometry_coc` table for per-CoC provenance).
   Composite rows (identity from one file, point from a second, population from a third) cannot be
   expressed in the single `source_id` column; the mapping is spelled out in `docs/SOURCES.md` and
   the concrete per-row choice above.

## Verified

Re-checked against the live sources on 2026-11-19 (this session, responses cached under
`data/raw/coc_geometry/`), by re-querying the endpoint and comparing to the stored row:

1. `NY-600` — `.../CoC_Geo_Type/FeatureServer/0/query?where=COCNUM%3D%27NY-600%27&outFields=COCNUM,COCNAME,ST_1,STATE_NAME,Geo_Type,HudNum&returnGeometry=true&f=geojson`
   -> live `COCNAME='New York City CoC'`, `ST_1=NY`, `HudNum=NY-600`, geometry MultiPolygon;
   DB name `New York City CoC`, state NY, 608 stored points, centroid (40.68927, -73.87162). Match.
2. `CA-600` -> live `'Los Angeles City & County CoC'`, CA, "Major Cities"; DB same name/state,
   498 points, (34.36882, -118.21792). Match.
3. `TX-700` -> live `'Houston, Pasadena, Conroe/Harris, Fort Bend, Montgomery Counties CoC'`, TX;
   DB same, 411 points, (29.90668, -95.50898). Match.
4. `WA-500` -> live `'Seattle/King County CoC'`, WA; DB same, 320 points, (47.49128, -121.79335). Match.
5. `FL-600` -> live `'Miami-Dade County CoC'`, FL; DB same, 907 points, (25.61247, -80.57329). Match.
6. `geo_state` fips `50` (VT) and `08` (CO) — `cb_2023_us_state_500k.zip` re-downloaded (3,249,332 B,
   56 features) and reparsed: VT ALAND/2589988.11 = 9,217.258 sq mi, centroid (44.0752, -72.66262),
   DB `land_sqmi=9217.3`, (44.0752, -72.66262), pop 648,493; CO 103,637.061 sq mi, (38.99855, -105.54781),
   DB `land_sqmi=103637.1`, (38.99855, -105.54781), pop 5,957,493. Match.
7. `geo_city` `0820000` (Denver) — live gazetteer row `Denver city`, CO, INTPTLAT 39.76185,
   INTPTLONG -104.881105; live `sub-est2024.csv` SUMLEV 162 / STATE 08 / PLACE 20000,
   POPESTIMATE2024 = 729,019. DB: name Denver, CO, (39.76185, -104.881105), pop 729,019,
   `coc_num=CO-503` = "Metropolitan Denver CoC". Match.

Also re-checked that CoCs without geometry really have no polygon in the FY2024 layer:
`where=COCNUM='AR-502'`, `'AR-504'`, `'AR-506'`, `'NY-609'`, `'TX-625'` each returned
`{"count":0}` / `features: []` — the NULL geometry is a property of the published layer, not a bug here.

Idempotency: repeated `python3 -m ingest.run coc_geometry` runs leave `geo_coc` 414 rows
(387 with geometry), `coc_geometry_coc` 387, `geo_state` 56, `geo_city` 56,819 rows (3,144 county +
32,656 place + 21,019 town; counties/towns come from other domains) — no duplicate `coc_num`,
`fips` or `geoid` keys.

Export gate (measured after the delivering run):
`data/export/coc.geojson` 4,204,831 B / 387 features, `data/export/state.geojson` 1,387,139 B /
56 features — **5,591,970 B total (5.6 MB) < 12 MB**, both load with `json.load`, both are
`FeatureCollection`s and every feature geometry is a Polygon/MultiPolygon.
DB geometry parses as GeoJSON for all 387 non-NULL `geo_coc.geometry` rows.

## Coverage

- **CoC boundaries: 387 of 387 features in HUD's FY2024 `CoC_Geo_type` layer — 100% of the
  published boundary universe.** 387 rows in `geo_coc` have non-NULL `geometry` and non-NULL
  `lat/lon` (acceptance threshold was >= 380). All 56 state/territory prefixes are represented,
  including PR (2), VI (1), GU (1), AS (1), MP (1).
- `geo_coc` holds 414 rows: the 387 polygons plus **27 CoC numbers that exist in HUD's CoC/PIT
  universe but have no polygon in this layer** — AR-502/504/506/509/510/512, CA-528,
  MA-501/508/510/514/517/519, MD-500/502/507/508/509/510/512, MI-513, NY-504/506/516/607/609,
  TX-625. These are the CoCs that FY2024 boundaries fold into a consolidated "Balance of State"
  CoC (e.g. AR-503, MA-516, MD-514); they keep the other domains' attributes and a NULL geometry,
  which is the honest representation.
- `geo_state`: 56 of 56 Census state-equivalents, all with `source_id`; `pop` for 51, `lat/lon` and
  `land_sqmi` for all 56.
- `geo_city`: **32,608 places** written by this domain (the whole 2023 cartographic place file).
  32,329 have a gazetteer internal point, 19,471 have a PEP 2024 population, and **32,241 (98.9%)
  were assigned a CoC** by point-in-polygon. Missing: 13,137 places with no PEP row (CDPs and other
  unincorporated places are not in the sub-county estimates -> `pop` NULL, not modelled) and 367
  places whose internal point falls in no CoC polygon (offshore/water CDPs, points that land in a
  narrow gap between simplified CoC rings). Counties are not written here — `places`/`hud_pit`
  own `kind='county'`; 48 extra `kind='place'` rows in the shared table (6-digit "Balance of county"
  pseudo-places and a few others) also come from those domains.
- Not attempted (out of scope, not needed for the map): HUD's CoC↔county crosswalk, tract/block
  geometry, ZCTA boundaries, pre-FY2024 CoC boundary vintages, and CoC population (owned by
  `hud_pit`, which writes `geo_coc.pop`/`cities`).

## Operational note for the orchestrator

Three domains write `geo_coc` (`coc_geometry`, `hud_pit`), `geo_state`/`geo_city` (`places`,
`hud_pit`) so **the last writer wins per column**, and all workers share one `data/atlas.db`:

* `hud_pit` also pages CoC polygons from `.../Continuum_of_Care_Grantee_Areas/FeatureServer/0`
  (`geometryPrecision=3`, `maxAllowableOffset=0.005`) into `geo_coc.geometry`, and rewrites
  `source_id`, `name`, `cities`, `pop`, `lat`, `lon`. After a `hud_pit` run the stored rings are
  coarser (~500 m, 3 dp); after a `coc_geometry` run they are ~220 m / 4 dp. Both are valid
  GeoJSON and both leave 387 non-NULL geometries. If the map looks blobby, re-run `coc_geometry`
  last. `data/export/*.geojson` are written only by `coc_geometry`.
* Because `geo_coc.source_id` can be repointed by `hud_pit`, this domain also records polygon
  provenance per CoC in the private table `coc_geometry_coc` (`source_id`, `source_url`, `tol`,
  `points`, `rings`, `parts`) — 387 rows.
* `places`/`hud_pit` also write `geo_state`/`geo_city` rows and re-point `source_id`. With the
  `_put()` ownership guard we no longer overwrite a foreign `source_id`; conversely our rows can be
  re-pointed by them. Either way the values stay consistent because both domains read the same
  Census files — but if a city page cites `places:sub-est2024.csv`, that is the *same* CSV registered
  under another domain, not a different dataset.
* `places`/`hud_pit` rewrite `geo_state.lat/lon/land_sqmi/pop`. Ours are derived from the state
  cartographic polygon itself (largest-ring area centroid), theirs from gazetteer internal points;
  both are legitimate, they differ by a few arc-minutes.
* All DB writes here retry on `SQLITE_BUSY`, and the download/simplify/export path takes no DB
  write lock at all (`_soft()` bookkeeping under a 2 s busy timeout) — with 8 workers running
  concurrently a 10-minute `busy_timeout` per statement made the ingestor look hung, so the
  exports are written to disk **before** the DB phase and progress bookkeeping is best-effort
  (the disk cache under `data/raw/coc_geometry/` is what actually makes reruns cheap).
* `--selftest` runs with `max_pages=1` and therefore writes its 25-feature GeoJSON preview to
  `data/raw/coc_geometry/_tmp/*.selftest.geojson`, never to `data/export/`.
