# coc-geometry — CoC and state polygons for the maps
Write ingest/domains/coc_geometry.py: download the HUD CoC GIS boundary file (verify the live URL),
simplify rings with a pure-python Douglas-Peucker (no shapely), store GeoJSON rings into
geo_coc.geometry, and also emit data/export/state.geojson + data/export/coc.geojson.
Also ingest Census cartographic boundary state + place files for the city layer.
accept: geometry non-null for >=380 CoCs, export files < 12 MB total, GeoJSON validates with json.load
