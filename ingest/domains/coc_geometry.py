"""coc_geometry — CoC and state/city polygons for the maps.

Sources (all keyless, all public domain / HUD public data):

1. HUD CoC GIS boundaries — HUD's own ArcGIS feature service
   `services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/CoC_Geo_Type/FeatureServer/0`
   (HUD org, FY2024 CoC geography, one polygon feature per CoC, 387 CoCs incl. PR/VI/GU/AS/MP).
   Fetched as GeoJSON in pages of 25 features. The service is asked for
   `geometryPrecision=5` and `maxAllowableOffset=0.0005` (~55 m) so the transfer stays
   reasonable; the coarse geometry is then simplified *again* here with a pure-python
   Douglas-Peucker (`_dp_keep`, no shapely/GEOS anywhere) down to the tolerance that fits
   the export budget.
2. Census cartographic boundary files (500k, 2023 vintage):
     cb_2023_us_state_500k.zip  -> geo_state + data/export/state.geojson
     cb_2023_us_place_500k.zip  -> geo_city (kind='place')
   Shapefile(.shp/.dbf) parsing is done with the small reader below — no pyshp/fiona.
3. Census 2023 gazetteer (place national) -> authoritative internal point (INTPTLAT/LON)
   for places, since the 500k cartographic files carry no centroid attributes.
4. Census PEP city/town estimates vintage 2024 (sub-est2024.csv) -> population for
   states (SUMLEV 040) and incorporated places (SUMLEV 162).

What is written:
  * geo_coc.geometry  = simplified GeoJSON geometry as JSON text (Polygon/MultiPolygon)
  * geo_coc.lat/lon   = area centroid of the largest ring of that CoC (derived, not published)
  * geo_state          = 56 Census state/territory rows
  * geo_city           = every place in the 2023 cartographic place file (~32k),
                         with pop/state/lat/lon filled where the matched sources publish them
                         and `coc_num` assigned by point-in-polygon against the CoC rings
                         (smallest-area containing CoC wins).
  * data/export/state.geojson, data/export/coc.geojson (both < 12 MB combined)

Resumable: page progress lives in `coc_geometry_page` / `coc_geometry_progress`, and every
HTTP response is disk-cached by ingest.common.fetch, so a rerun replays from cache.
"""
from __future__ import annotations

import io
import json
import math
import sqlite3
import struct
import sys
import time
import zipfile
from pathlib import Path

from ingest import common
from ingest.common import clean, fetch, fetch_json, now, save_raw, source_id_for, upsert

DOMAIN = "coc_geometry"

ROOT = common.ROOT
EXPORT = ROOT / "data" / "export"

# ---- HUD CoC boundaries ----------------------------------------------------
COC_SVC = ("https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
           "CoC_Geo_Type/FeatureServer/0")
COC_LAYER_META = COC_SVC + "?f=json"
COC_QUERY = COC_SVC + "/query"
COC_OUT_FIELDS = "COCNUM,COCNAME,ST_1,STATE_NAME,Geo_Type,HudNum"
PAGE = 25                      # features per request (~2-3 MB of GeoJSON)
MAX_ALLOWABLE_OFFSET = 0.0005  # degrees; server-side transfer reduction (~55 m)
GEOM_PRECISION = 5             # decimals kept by the service

HUD_TITLE = "HUD CoC (Continuum of Care) geographic boundaries, FY2024 (CoC_Geo_Type)"
HUD_PUBLISHER = "U.S. Department of Housing and Urban Development (HUD)"
HUD_LICENSE = ("Public domain (US Government work). HUD disclaimer: no warranty of spatial "
               "or attribute accuracy; see the layer licenseInfo")

# ---- Census ----------------------------------------------------------------
CENSUS_PUB = "U.S. Census Bureau"
CENSUS_LIC = "Public domain (US Government work)"
CB_STATE_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip"
CB_PLACE_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_place_500k.zip"
GAZ_PLACE_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
                 "2023_Gazetteer/2023_Gaz_place_national.zip")
PEP_URL = ("https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/"
           "cities/totals/sub-est2024.csv")

# ---- budget ----------------------------------------------------------------
# exports must be < 12 MB total; leave headroom
COC_EXPORT_BUDGET = 8_000_000
STATE_EXPORT_BUDGET = 1_800_000
TOL_LADDER = [0.004, 0.006, 0.008, 0.012, 0.016, 0.024, 0.032, 0.05, 0.08]

SQ_M_PER_SQ_MI = 2589988.110336


# --------------------------------------------------------------------------- #
# write helpers — other workers share data/atlas.db, so retry on SQLITE_BUSY
# --------------------------------------------------------------------------- #
def _retry(fn, attempts=40):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = e
            time.sleep(min(5.0, 0.5 + 0.5 * i))
    raise last


def _upsert(conn, table, row, keys):
    _retry(lambda: upsert(conn, table, row, keys))


def _commit(conn):
    _retry(lambda: conn.commit())


def _exec(conn, sql, params=()):
    return _retry(lambda: conn.execute(sql, params))


def _source_id(conn, url, *a, **kw):
    """source_id_for() writes a row; other workers share the DB, so retry on locks."""
    return _retry(lambda: source_id_for(conn, url, DOMAIN, *a, **kw))


# --------------------------------------------------------------------------- #
# pure-python geometry helpers (Douglas-Peucker etc.)
# --------------------------------------------------------------------------- #
def _dp_keep(pts, tol):
    """Douglas-Peucker on a list of (x, y). Returns list of indices to keep."""
    n = len(pts)
    if n <= 2:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        x0, y0 = pts[i]
        x1, y1 = pts[j]
        dx = x1 - x0
        dy = y1 - y0
        den = dx * dx + dy * dy
        maxd = -1.0
        idx = -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if den == 0.0:
                ex = px - x0
                ey = py - y0
            else:
                t = ((px - x0) * dx + (py - y0) * dy) / den
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                ex = px - (x0 + t * dx)
                ey = py - (y0 + t * dy)
            d = ex * ex + ey * ey
            if d > maxd:
                maxd = d
                idx = k
        if maxd > tol2 and idx > 0:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [i for i in range(n) if keep[i]]


def _simplify_ring(ring, tol):
    """Simplify one closed ring. Returns a closed ring or None if it degenerated."""
    n = len(ring)
    if n < 4:
        return None
    closed = ring[0] == ring[-1]
    pts = ring[:-1] if closed else list(ring)
    if len(pts) < 3:
        return None
    # equirectangular scaling so the tolerance means metres-ish everywhere
    lat0 = sum(p[1] for p in pts) / len(pts)
    kx = max(0.05, math.cos(math.radians(lat0)))
    scaled = [(p[0] * kx, p[1]) for p in pts]
    idx = _dp_keep(scaled, tol)
    if len(idx) < 3:
        return None
    out = [pts[i] for i in idx]
    # reject slivers whose whole extent is below the tolerance
    xs = [p[0] for p in out]
    ys = [p[1] for p in out]
    if (max(xs) - min(xs)) < tol and (max(ys) - min(ys)) < tol:
        return None
    out.append(out[0])
    return out


def _simplify_polygon(rings, tol):
    """rings = [outer, hole, hole...] -> simplified rings or None."""
    if not rings:
        return None
    outer = _simplify_ring(rings[0], tol)
    if not outer:
        return None
    out = [outer]
    for h in rings[1:]:
        hs = _simplify_ring(h, tol)
        if hs:
            out.append(hs)
    return out


def _simplify_geometry(geom, tol):
    """Simplify a GeoJSON Polygon/MultiPolygon. Returns a GeoJSON geometry or None."""
    if not geom:
        return None
    t = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return None
    if t == "Polygon":
        p = _simplify_polygon(coords, tol)
        return {"type": "Polygon", "coordinates": p} if p else None
    if t == "MultiPolygon":
        polys = []
        for poly in coords:
            sp = _simplify_polygon(poly, tol)
            if sp:
                polys.append(sp)
        if not polys:
            return None
        if len(polys) == 1:
            return {"type": "Polygon", "coordinates": polys[0]}
        return {"type": "MultiPolygon", "coordinates": polys}
    return None


def _count_points(geom):
    if not geom:
        return 0
    t = geom.get("type")
    c = geom.get("coordinates") or []
    if t == "Polygon":
        return sum(len(r) for r in c)
    if t == "MultiPolygon":
        return sum(len(r) for p in c for r in p)
    return 0


def _signed_area(ring):
    a = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        a += x0 * y1 - x1 * y0
    return a * 0.5


def _centroid(ring):
    """Area centroid of a closed ring; falls back to the mean of the vertices."""
    a = 0.0
    cx = 0.0
    cy = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-14:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    a *= 0.5
    return (cx / (6.0 * a), cy / (6.0 * a))


def _pip(x, y, ring):
    """Ray casting point-in-ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def _bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _outer_rings(geom):
    if not geom:
        return []
    if geom.get("type") == "Polygon":
        return [geom["coordinates"][0]] if geom.get("coordinates") else []
    if geom.get("type") == "MultiPolygon":
        return [p[0] for p in geom["coordinates"] if p]
    return []


def _center_largest(geom):
    """(lat, lon) centroid of the largest outer ring of a GeoJSON polygon geometry."""
    best = None
    for r in _outer_rings(geom):
        a = abs(_signed_area(r))
        if best is None or a > best[0]:
            best = (a, r)
    if not best:
        return (None, None)
    lon, lat = _centroid(best[1])
    return (round(lat, 5), round(lon, 5))


def _round_geom(geom, nd=4):
    """Round coordinates for export size; keeps GeoJSON-ness."""
    if not geom:
        return None
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Polygon":
        return {"type": "Polygon", "coordinates": [[[round(x, nd), round(y, nd)] for x, y in r] for r in c]}
    if t == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[[[round(x, nd), round(y, nd)] for x, y in r] for r in p] for p in c]}
    return geom


# --------------------------------------------------------------------------- #
# shapefile (.shp / .dbf) readers — no external deps
# --------------------------------------------------------------------------- #
def _read_dbf(path):
    b = Path(path).read_bytes()
    nrec = struct.unpack("<I", b[4:8])[0]
    hlen = struct.unpack("<H", b[8:10])[0]
    rlen = struct.unpack("<H", b[10:12])[0]
    fields = []
    off = 32
    while off < hlen and b[off] != 0x0D:
        name = b[off:off + 11].split(b"\x00")[0].decode("latin-1")
        ftype = chr(b[off + 11])
        flen = b[off + 16]
        fdec = b[off + 17]
        fields.append((name, ftype, flen, fdec))
        off += 32
    rows = []
    pos = hlen
    for _ in range(nrec):
        rec = b[pos:pos + rlen]
        pos += rlen
        if not rec:
            break
        if rec[:1] == b"*":
            continue
        p = 1
        row = {}
        for name, ftype, flen, fdec in fields:
            raw = rec[p:p + flen]
            p += flen
            v = raw.decode("latin-1").strip()
            if ftype in ("N", "F"):
                if v in ("", "-"):
                    v = None
                else:
                    try:
                        v = float(v) if fdec else int(v)
                    except ValueError:
                        v = None
            else:
                v = v or None
            row[name] = v
        rows.append(row)
    return rows


def _read_shp_polygons(path):
    """Yield parts (list of rings) for each shape record, None for null shapes."""
    b = Path(path).read_bytes()
    out = []
    pos = 100
    n = len(b)
    while pos + 8 <= n:
        _recnum, clen = struct.unpack(">ii", b[pos:pos + 8])
        pos += 8
        content = b[pos:pos + clen * 2]
        pos += clen * 2
        if len(content) < 4:
            out.append(None)
            continue
        st = struct.unpack("<i", content[:4])[0]
        if st == 0:
            out.append(None)
            continue
        nparts, npoints = struct.unpack("<ii", content[36:44])
        parts = list(struct.unpack("<%di" % nparts, content[44:44 + 4 * nparts]))
        off = 44 + 4 * nparts
        flat = struct.unpack("<%dd" % (2 * npoints), content[off:off + 16 * npoints])
        rings = []
        for i, start in enumerate(parts):
            end = parts[i + 1] if i + 1 < nparts else npoints
            rings.append([(flat[2 * k], flat[2 * k + 1]) for k in range(start, end)])
        out.append(rings)
    return out


def _rings_to_geometry(rings):
    """Group shapefile rings into Polygon/MultiPolygon using containment (orientation agnostic)."""
    if not rings:
        return None
    areas = [abs(_signed_area(r)) for r in rings]
    order = sorted(range(len(rings)), key=lambda i: -areas[i])
    polys = []          # (ring_index, [hole indices])
    holes = []          # (ring_index, parent ring_index)
    for i in order:
        pt = rings[i][0]
        parent = None
        for j in order:
            if j == i or areas[j] <= areas[i]:
                continue
            if _pip(pt[0], pt[1], rings[j]):
                parent = j
                break
        if parent is None:
            polys.append([i, []])
        else:
            holes.append((i, parent))
    for h, parent in holes:
        for p in polys:
            if p[0] == parent:
                p[1].append(h)
    built = [([rings[o]] + [rings[h] for h in hs]) for o, hs in polys]
    if len(built) == 1:
        return {"type": "Polygon", "coordinates": built[0]}
    return {"type": "MultiPolygon", "coordinates": built}


# --------------------------------------------------------------------------- #
# progress bookkeeping (resumability)
# --------------------------------------------------------------------------- #
def _ensure(conn):
    _exec(conn, """CREATE TABLE IF NOT EXISTS coc_geometry_page(
        page_offset INTEGER PRIMARY KEY,
        n_feat INTEGER,
        n_points INTEGER,
        url TEXT,
        done_at TEXT)""")
    _exec(conn, """CREATE TABLE IF NOT EXISTS coc_geometry_progress(
        step TEXT PRIMARY KEY,
        value TEXT,
        done_at TEXT)""")
    _commit(conn)


def _mark(conn, step, value=""):
    _exec(conn, """INSERT INTO coc_geometry_progress(step,value,done_at) VALUES(?,?,?)
                    ON CONFLICT(step) DO UPDATE SET value=excluded.value, done_at=excluded.done_at""",
          (step, str(value), now()))
    _commit(conn)


def _done(conn, step):
    r = conn.execute("SELECT value FROM coc_geometry_progress WHERE step=?", (step,)).fetchone()
    return r[0] if r else None


# --------------------------------------------------------------------------- #
# step 1 — HUD CoC boundaries
# --------------------------------------------------------------------------- #
def _coc_total(conn):
    url = (COC_QUERY + "?where=1%3D1&returnCountOnly=true&f=json")
    j = fetch_json(url, domain=DOMAIN)
    return int(j["count"]), url


def _coc_page_url(offset):
    return (COC_QUERY +
            "?where=1%3D1"
            "&outFields=" + COC_OUT_FIELDS +
            "&returnGeometry=true&f=geojson"
            "&geometryPrecision=" + str(GEOM_PRECISION) +
            "&maxAllowableOffset=" + str(MAX_ALLOWABLE_OFFSET) +
            "&orderByFields=OBJECTID_1"
            "&resultOffset=%d&resultRecordCount=%d" % (offset, PAGE))


def _fetch_coc(conn, max_pages=None):
    """Download every CoC page (cached on disk + named copies in data/raw/coc_geometry/)."""
    total, count_url = _coc_total(conn)
    print(f"[{DOMAIN}] HUD service reports {total} CoC features")
    feats = []
    pages = 0
    for offset in range(0, total, PAGE):
        if max_pages is not None and pages >= max_pages:
            break
        url = _coc_page_url(offset)
        raw = fetch(url, domain=DOMAIN)
        j = json.loads(raw)
        page_feats = j.get("features") or []
        save_raw(DOMAIN, f"coc_page_{offset:04d}.json", raw)
        npts = sum(_count_points(f.get("geometry")) for f in page_feats)
        _exec(conn, """INSERT INTO coc_geometry_page(page_offset,n_feat,n_points,url,done_at)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(page_offset) DO UPDATE SET n_feat=excluded.n_feat,
                          n_points=excluded.n_points, url=excluded.url, done_at=excluded.done_at""",
              (offset, len(page_feats), npts, url, now()))
        _commit(conn)
        src = _source_id(conn, url, title=f"{HUD_TITLE} — features {offset}-{offset + PAGE - 1}",
                            publisher=HUD_PUBLISHER, license=HUD_LICENSE)
        for f in page_feats:
            feats.append((f.get("properties") or {}, f.get("geometry"), src))
        pages += 1
        print(f"[{DOMAIN}] page offset={offset} feats={len(page_feats)} points={npts}")
    _mark(conn, "coc_pages", pages)
    return feats, total, count_url


def _simplify_cocs(feats, tol):
    out = []
    npts = 0
    for props, geom, src in feats:
        sg = _simplify_geometry(geom, tol)
        if not sg:
            continue
        npts += _count_points(sg)
        out.append({"props": props, "geom": sg, "src": src})
    return out, npts


def _pick_tolerance(feats):
    """Escalate the Douglas-Peucker tolerance until the CoC export fits its budget."""
    chosen = None
    for tol in TOL_LADDER:
        simplified, npts = _simplify_cocs(feats, tol)
        est = npts * 21  # ~bytes per rounded coordinate pair in json.dumps output
        print(f"[{DOMAIN}] tol={tol} kept_points={npts} est_export={est/1e6:.2f}MB")
        chosen = (tol, simplified, npts)
        if est <= COC_EXPORT_BUDGET:
            break
    return chosen


# --------------------------------------------------------------------------- #
# step 2 — Census states
# --------------------------------------------------------------------------- #
def _zip_parts(blob, prefix):
    """Extract .shp/.dbf bytes from an in-memory zip."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    out = {}
    for name in z.namelist():
        low = name.lower()
        if low.endswith(".shp") and prefix in low:
            out["shp"] = z.read(name)
        elif low.endswith(".dbf") and prefix in low:
            out["dbf"] = z.read(name)
    return out


def _write_tmp(tmpdir, name, data):
    p = Path(tmpdir) / name
    p.write_bytes(data)
    return p


def _census_states(conn, tol):
    raw = fetch(CB_STATE_URL, domain=DOMAIN)
    save_raw(DOMAIN, "cb_2023_us_state_500k.zip", raw)
    parts = _zip_parts(raw, "state")
    tmp = ROOT / "data" / "raw" / DOMAIN / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    shp = _write_tmp(tmp, "states.shp", parts["shp"])
    dbf = _write_tmp(tmp, "states.dbf", parts["dbf"])
    attrs = _read_dbf(dbf)
    shapes = _read_shp_polygons(shp)
    src_state = _source_id(conn, CB_STATE_URL,
                              title="2023 Cartographic Boundary File, State (cb_2023_us_state_500k)",
                              publisher=CENSUS_PUB, license=CENSUS_LIC)

    pop = _pep_population(conn)
    rows = []
    feats = []
    for a, rings in zip(attrs, shapes):
        fips = str(a.get("STATEFP") or "").zfill(2)
        abbr = clean(a.get("STUSPS"))
        name = clean(a.get("NAME"))
        if not fips or not name:
            continue
        geom = _rings_to_geometry(rings) if rings else None
        sg = _simplify_geometry(geom, tol) if geom else None
        lat, lon = _center_largest(geom) if geom else (None, None)
        aland = a.get("ALAND")
        land_sqmi = round(aland / SQ_M_PER_SQ_MI, 1) if isinstance(aland, (int, float)) else None
        _upsert(conn, "geo_state", {
            "fips": fips, "abbr": abbr, "name": name,
            "pop": pop["state"].get(fips),
            "lat": lat, "lon": lon, "land_sqmi": land_sqmi,
        }, keys=["fips"])
        rows.append((fips, abbr, name))
        if sg:
            feats.append({"type": "Feature", "properties": {
                "fips": fips, "abbr": abbr, "name": name,
                "pop": pop["state"].get(fips), "land_sqmi": land_sqmi},
                "geometry": _round_geom(sg)})
    _commit(conn)
    return feats, src_state, rows


# --------------------------------------------------------------------------- #
# step 3 — population (PEP vintage 2024)
# --------------------------------------------------------------------------- #
_PEP_CACHE = {}


def _pep_population(conn):
    """{state fips: pop} and {5-digit place geoid: pop} from sub-est2024.csv."""
    if "pop" in _PEP_CACHE:
        return _PEP_CACHE["pop"]
    raw = fetch(PEP_URL, domain=DOMAIN)
    save_raw(DOMAIN, "sub-est2024.csv", raw)
    src = _source_id(conn, PEP_URL,
                        title="Annual Estimates of the Resident Population for Incorporated Places "
                              "and Minor Civil Divisions, 2020-2024 (sub-est2024)",
                        publisher=CENSUS_PUB, license=CENSUS_LIC)
    import csv
    text = raw.decode("latin-1")
    states, places = {}, {}
    for row in csv.DictReader(io.StringIO(text)):
        lev = row.get("SUMLEV")
        try:
            val = int(row.get("POPESTIMATE2024") or "")
        except ValueError:
            continue
        st = (row.get("STATE") or "").zfill(2)
        if lev == "040":
            states[st] = val
        elif lev in ("162", "170"):
            pl = (row.get("PLACE") or "").zfill(5)
            if pl != "00000":
                places[st + pl] = val
    pop = {"state": states, "place": places, "source_id": src}
    _PEP_CACHE["pop"] = pop
    return pop


# --------------------------------------------------------------------------- #
# step 4 — Census places -> geo_city
# --------------------------------------------------------------------------- #
def _gazetteer_points(conn):
    raw = fetch(GAZ_PLACE_URL, domain=DOMAIN)
    save_raw(DOMAIN, "2023_Gaz_place_national.zip", raw)
    z = zipfile.ZipFile(io.BytesIO(raw))
    txt = z.read(z.namelist()[0]).decode("latin-1")
    src = _source_id(conn, GAZ_PLACE_URL,
                        title="2023 Gazetteer Files, National Places (2023_Gaz_place_national)",
                        publisher=CENSUS_PUB, license=CENSUS_LIC)
    out = {}
    lines = txt.splitlines()
    if not lines:
        return out, src
    hdr = lines[0].split("\t")
    idx = {h.strip(): i for i, h in enumerate(hdr)}
    for ln in lines[1:]:
        f = [x.strip() for x in ln.split("\t")]
        if len(f) < len(hdr) - 1:
            continue
        geoid = f[idx["GEOID"]]
        try:
            lat = float(f[idx["INTPTLAT"]])
            lon = float(f[idx["INTPTLONG"]])
        except (ValueError, KeyError):
            continue
        out[geoid] = (lat, lon, f[idx["USPS"]], f[idx["ALAND_SQMI"]])
    return out, src


def _census_places(conn, coc_geoms):
    raw = fetch(CB_PLACE_URL, domain=DOMAIN)
    save_raw(DOMAIN, "cb_2023_us_place_500k.zip", raw)
    parts = _zip_parts(raw, "place")
    tmp = ROOT / "data" / "raw" / DOMAIN / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    dbf = _write_tmp(tmp, "places.dbf", parts["dbf"])
    attrs = _read_dbf(dbf)
    src_place = _source_id(conn, CB_PLACE_URL,
                              title="2023 Cartographic Boundary File, Place (cb_2023_us_place_500k)",
                              publisher=CENSUS_PUB, license=CENSUS_LIC)
    gaz, _src_gaz = _gazetteer_points(conn)
    pop = _pep_population(conn)

    fips2abbr = {r["fips"]: r["abbr"] for r in conn.execute(
        "SELECT fips, abbr FROM geo_state WHERE abbr IS NOT NULL")}

    # point-in-polygon index over the simplified CoC rings (smallest area wins)
    coc_index = []
    for coc_num, geom in coc_geoms.items():
        if not geom:
            continue
        polys = []
        total_area = 0.0
        for poly in ([geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]):
            outer = poly[0]
            b = _bbox(outer)
            a = abs(_signed_area(outer))
            total_area += a
            polys.append((b, outer, [r for r in poly[1:]]))
        if not polys:
            continue
        xs = [p[0][0] for p in polys] + [p[0][2] for p in polys]
        ys = [p[0][1] for p in polys] + [p[0][3] for p in polys]
        coc_index.append((total_area, coc_num, (min(xs), min(ys), max(xs), max(ys)), polys))

    def match_coc(lon, lat):
        best = None
        for area, coc_num, box, polys in coc_index:
            if not (box[0] <= lon <= box[2] and box[1] <= lat <= box[3]):
                continue
            if best is not None and area >= best[0]:
                continue
            for b, outer, holes in polys:
                if not (b[0] <= lon <= b[2] and b[1] <= lat <= b[3]):
                    continue
                if not _pip(lon, lat, outer):
                    continue
                if any(_pip(lon, lat, h) for h in holes):
                    continue
                best = (area, coc_num)
                break
        return best[1] if best else None

    n = 0
    n_pop = 0
    n_pt = 0
    n_coc = 0
    for a in attrs:
        geoid = str(a.get("GEOID") or "")
        if len(geoid) != 7:
            continue
        name = clean(a.get("NAME"))
        st = fips2abbr.get(str(a.get("STATEFP") or "").zfill(2))
        g = gaz.get(geoid)
        lat = g[0] if g else None
        lon = g[1] if g else None
        p = pop["place"].get(geoid)
        coc_num = match_coc(lon, lat) if lat is not None else None
        _upsert(conn, "geo_city", {
            "geoid": geoid, "kind": "place", "name": name, "state": st,
            "pop": p, "lat": lat, "lon": lon, "coc_num": coc_num,
            "source_id": src_place,
        }, keys=["geoid"])
        n += 1
        n_pop += 1 if p is not None else 0
        n_pt += 1 if lat is not None else 0
        n_coc += 1 if coc_num else 0
    _commit(conn)
    print(f"[{DOMAIN}] places={n} with_pop={n_pop} with_point={n_pt} with_coc={n_coc}")
    return {"places": n, "with_pop": n_pop, "with_point": n_pt, "with_coc": n_coc,
            "source_id": src_place}


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run(conn, cfg):
    cfg = cfg or {}
    _exec(conn, "PRAGMA busy_timeout=600000")
    _ensure(conn)
    EXPORT.mkdir(parents=True, exist_ok=True)
    max_pages = cfg.get("max_pages")

    # ---- CoCs -------------------------------------------------------------
    feats, total, count_url = _fetch_coc(conn, max_pages=max_pages)
    if max_pages:
        print(f"[{DOMAIN}] selftest mode: {len(feats)} features")
    tol, simplified, npts = _pick_tolerance(feats)
    print(f"[{DOMAIN}] CoC simplification tolerance={tol} points={npts}")

    coc_geoms = {}
    coc_rows = 0
    # ---- CoC export (filesystem only; written before any DB write so a locked
    #      shared DB cannot leave the exports stale) --------------------------
    coc_feats = []
    for rec in simplified:
        props = rec["props"]
        coc_num = clean(props.get("COCNUM"))
        if not coc_num:
            continue
        coc_feats.append({"type": "Feature", "properties": {
            "coc_num": coc_num,
            "name": clean(props.get("COCNAME")),
            "state": coc_num.split("-")[0],
            "geo_type": clean(props.get("Geo_Type")),
        }, "geometry": _round_geom(rec["geom"])})
    # a --selftest run only has PAGE features; never let it clobber the real export
    coc_path = EXPORT / "coc.geojson" if not max_pages else (
        ROOT / "data" / "raw" / DOMAIN / "_tmp" / "coc.selftest.geojson")
    coc_text = json.dumps({"type": "FeatureCollection",
                           "name": "HUD CoC boundaries FY2024 (simplified)",
                           "features": coc_feats}, separators=(",", ":"))
    coc_path.parent.mkdir(parents=True, exist_ok=True)
    coc_path.write_text(coc_text)
    print(f"[{DOMAIN}] wrote {coc_path} features={len(coc_feats)} {len(coc_text)/1e6:.2f}MB")

    for rec in simplified:
        props = rec["props"]
        coc_num = clean(props.get("COCNUM"))
        if not coc_num:
            continue
        geom = rec["geom"]
        lat, lon = _center_largest(geom)
        state = (coc_num.split("-")[0] or None)
        _upsert(conn, "geo_coc", {
            "coc_num": coc_num,
            "name": clean(props.get("COCNAME")),
            "state": state,
            "geometry": json.dumps(_round_geom(geom), separators=(",", ":")),
            "lat": lat,
            "lon": lon,
            "source_id": rec["src"],
        }, keys=["coc_num"])
        coc_geoms[coc_num] = geom
        coc_rows += 1
    _commit(conn)
    _mark(conn, "cocs", coc_rows)
    print(f"[{DOMAIN}] geo_coc rows written: {coc_rows} (service total {total})")

    # ---- states -----------------------------------------------------------
    state_feats, _src_state, state_rows = _census_states(conn, tol)
    state_path = EXPORT / "state.geojson" if not max_pages else (
        ROOT / "data" / "raw" / DOMAIN / "_tmp" / "state.selftest.geojson")
    state_text = json.dumps({"type": "FeatureCollection",
                             "name": "US states, Census 2023 cartographic boundaries (simplified)",
                             "features": state_feats}, separators=(",", ":"))
    state_path.write_text(state_text)
    total_bytes = len(coc_text) + len(state_text)
    print(f"[{DOMAIN}] exports: coc={len(coc_text)/1e6:.2f}MB state={len(state_text)/1e6:.2f}MB "
          f"total={total_bytes/1e6:.2f}MB")

    # ---- places -----------------------------------------------------------
    city_stats = {"places": 0}
    if not max_pages:
        city_stats = _census_places(conn, coc_geoms)
    _mark(conn, "exports", total_bytes)

    notes = (f"coc_rows={coc_rows}/{total} tol={tol} coc_pts={npts} "
             f"export_coc={len(coc_text)}B export_state={len(state_text)}B "
             f"states={len(state_rows)} places={city_stats.get('places')} "
             f"places_with_pop={city_stats.get('with_pop')} "
             f"places_with_coc={city_stats.get('with_coc')}")

    # sanity: the export must be valid JSON and inside the size budget
    json.loads(coc_path.read_text())
    json.loads(state_path.read_text())
    if not max_pages and total_bytes > 12_000_000:
        raise RuntimeError(f"export budget blown: {total_bytes} bytes")
    nonnull = conn.execute("SELECT COUNT(*) FROM geo_coc WHERE geometry IS NOT NULL").fetchone()[0]
    if not max_pages and nonnull < 380:
        raise RuntimeError(f"only {nonnull} CoCs have geometry")

    return {"rows": coc_rows, "notes": notes}


# --------------------------------------------------------------------------- #
def _selftest():
    conn = common.db()
    res = run(conn, {"max_pages": 1})
    print(f"[{DOMAIN}] selftest rows={res['rows']} notes={res['notes']}")
    common.selftest_report(conn, DOMAIN, "geo_coc", "geometry IS NOT NULL", 3)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        common.main(DOMAIN, run, "geo_coc")
