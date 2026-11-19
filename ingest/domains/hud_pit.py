"""hud_pit -- HUD Point-in-Time (PIT) homeless counts by Continuum of Care, 2007-2025.

What this domain owns
---------------------
* `pit`      one row per (coc_num, year) for every CoC-year HUD actually published.
* `geo_coc`  the CoC universe (number, name, state, population, marker, geometry).
* `geo_city` the ~1,000 largest US places with population, lat/lon and their CoC.
* `geo_state` state population / land area / centroid (population denominators).
* `metric`   national (entity_type='us') PIT series, incl. the national subpopulation
             totals HUD publishes in its PDF reports (SMI/SUD/HIV/DV are *not*
             published per CoC for any year -- see research/hud_pit.md).

Live sources (verified 2026-11-19, see docs/SOURCES.md)
-------------------------------------------------------
PIT workbook     https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-PIT-Counts-by-CoC.xlsb
CoC boundaries   https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/Continuum_of_Care_Grantee_Areas/FeatureServer/0
Census pops      https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv
Census gazetteer https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_{place,counties,state}_national.zip
National PopSub  https://files.hudexchange.info/reports/published/CoC_PopSub_NatlTerrDC_{year}.pdf

Note: hudexchange.info returns 404 to the default ingest User-Agent; a browser UA is
used for every request (same public files, just not blocked).

Requires `pyxlsb` (pure-python xlsb reader; HUD ships the PIT workbook as .xlsb since
2024). Install with:
    pip install --user pyxlsb
If pyxlsb is missing the domain still writes the Census/geo/metric parts from cached
raw files, and reports the gap in its notes.

Nothing here is modelled: if HUD does not publish a cell it stays NULL.
"""
from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path

from ingest.common import RAW, clean, fetch, save_raw, source_id_for, to_int, upsert

DOMAIN = "hud_pit"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

URL_PIT_XLSB = ("https://www.huduser.gov/portal/sites/default/files/xls/"
                "2007-2025-PIT-Counts-by-CoC.xlsb")
URL_ARC_BASE = ("https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
                "Continuum_of_Care_Grantee_Areas/FeatureServer/0/query")
URL_SUBEST = ("https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/"
              "cities/totals/sub-est2024.csv")
URL_GAZ = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
           "2024_Gaz_{kind}_national.zip")
URL_POPSUB_NAT = ("https://files.hudexchange.info/reports/published/"
                  "CoC_PopSub_NatlTerrDC_{year}.pdf")

CENSUS_LICENSE = "public domain (US Census Bureau)"
HUD_LICENSE = "public domain (US Government work)"

COC_RX = re.compile(r"^[A-Z]{2}-\d{3}$")          # excludes the MO-604a duplicate
NON_CITY_SUFFIX = re.compile(
    r"\s+(city|town|village|borough|municipality|CDP|urban county|"
    r"city and borough|consolidated government|metropolitan government|"
    r"metro government|unified government)\s*$", re.I)

# pit column <- candidate header names in the HUD workbook (first match wins)
PIT_COLS = {
    "total": ["Overall Homeless"],
    "sheltered": ["Sheltered Total Homeless"],
    "unsheltered": ["Unsheltered Homeless"],
    "chronic": ["Overall Chronically Homeless", "Overall Chronically Homeless Individuals"],
    "veterans": ["Overall Homeless Veterans"],
    "youth": ["Overall Homeless Unaccompanied Youth (Under 25)"],
    "children": ["Overall Homeless - Under 18"],
    "families": ["Overall Homeless People in Families"],
}
# published, but not the semantic equivalent of pit.households (which means *all*
# homeless households): only family households exist in the CoC workbook -> metric.
FAMILY_HH_COL = "Overall Homeless Family Households"
# HUD never published these per CoC in any machine-readable product -> pit.* stays NULL
NEVER_PER_COC = ("smi", "sud", "hiv", "dv")


# --------------------------------------------------------------------------- util
def _num(v):
    """Excel cell -> int|None. Blank stays blank, 0 stays 0."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    return to_int(v)


def _get(url, name=None, **kw):
    b = fetch(url, domain=DOMAIN, headers={"User-Agent": UA}, **kw)
    if name:
        save_raw(DOMAIN, name, b)
    return b


# ----------------------------------------------------------------- PIT workbook
def read_pit_workbook(path: Path):
    """-> (rows{(coc,year): dict}, coc_names{coc: name}, notes[list])"""
    try:
        from pyxlsb import open_workbook
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "pyxlsb is required to read HUD's .xlsb PIT workbook "
            "(pip install --user pyxlsb)") from e

    rows, names, notes = {}, {}, []
    skipped_dupe = 0
    with open_workbook(str(path)) as wb:
        years = sorted(int(s) for s in wb.sheets if s.isdigit() and len(s) == 4)
        for year in years:
            with wb.get_sheet(str(year)) as sh:
                it = sh.rows()
                hdr = [c.v for c in next(it)]
                idx = {str(h).strip(): i for i, h in enumerate(hdr) if h is not None}
                if "CoC Number" not in idx:
                    notes.append(f"{year}: no CoC Number column, skipped")
                    continue
                ci, ni = idx["CoC Number"], idx.get("CoC Name")
                wanted = {}
                for key, cands in PIT_COLS.items():
                    for cand in cands:
                        if cand in idx:
                            wanted[key] = idx[cand]
                            break
                if FAMILY_HH_COL in idx:
                    wanted["family_households"] = idx[FAMILY_HH_COL]
                n = 0
                for r in it:
                    if not r:
                        continue
                    cell = r[ci].v if ci < len(r) else None
                    coc = str(cell).strip() if isinstance(cell, str) else None
                    if not coc or not COC_RX.match(coc):
                        if coc and re.match(r"^[A-Z]{2}-\d{3}[a-z]$", coc):
                            skipped_dupe += 1       # MO-604a footnote duplicate
                        continue
                    rec = {}
                    for key, i in wanted.items():
                        rec[key] = _num(r[i].v) if i < len(r) else None
                    if (coc, year) in rows:
                        notes.append(f"{year}: duplicate row for {coc}, kept first")
                        continue
                    rows[(coc, year)] = rec
                    if ni is not None and ni < len(r) and coc not in names:
                        nm = clean(r[ni].v)
                        if nm:
                            names[coc] = nm
                    n += 1
                if n == 0:
                    notes.append(f"{year}: no CoC rows parsed")
    if skipped_dupe:
        notes.append(f"excluded {skipped_dupe} 'NN-###x' footnote-duplicate row(s) "
                     "(e.g. MO-604a repeats MO-604 totals)")
    return rows, names, notes


def write_pit(conn, rows, src):
    n = 0
    for (coc, year), rec in rows.items():
        row = {"coc_num": coc, "year": year, "is_est": 0, "source_id": src}
        for k in ("total", "sheltered", "unsheltered", "chronic", "veterans",
                  "youth", "children", "families"):
            row[k] = rec.get(k)
        # households / smi / sud / hiv / dv are never published per CoC -> NULL
        upsert(conn, "pit", row, ["coc_num", "year"])
        n += 1
    return n


# ------------------------------------------------------------------- geometry
def read_coc_geometry(conn, force=False):
    """Page through HUD's CoC grantee-area service; return {coc: feature dict}."""
    feats, offset, page = {}, 0, 40
    while True:
        url = (f"{URL_ARC_BASE}?where=1%3D1&outFields=COCNUM,COCNAME,STUSAB"
               f"&outSR=4326&geometryPrecision=3&maxAllowableOffset=0.005"
               f"&resultOffset={offset}&resultRecordCount={page}&f=geojson")
        raw = _get(url, name=f"coc_boundaries_{offset:04d}.geojson", force=force)
        d = json.loads(raw)
        fs = d.get("features") or []
        for f in fs:
            p = f.get("properties") or {}
            coc = (p.get("COCNUM") or "").strip()
            if not coc or not COC_RX.match(coc):
                continue
            feats[coc] = {
                "name": clean(p.get("COCNAME")),
                "state": clean(p.get("STUSAB")),
                "geom": f.get("geometry"),
            }
        if len(fs) < page:
            break
        offset += page
        if offset > 1200:
            break
    return feats


class _Poly:
    """CoC polygon: exterior rings + holes as numpy arrays, with a bbox."""

    __slots__ = ("rings", "holes", "bbox")

    def __init__(self, geom):
        import numpy as np
        self.rings, self.holes = [], []
        if not geom:
            self.bbox = None
            return

        def add(poly_coords):
            if not poly_coords:
                return
            for i, ring in enumerate(poly_coords):
                arr = np.asarray(ring, dtype="float64")[:, :2]
                if len(arr) < 4:
                    continue
                (self.rings if i == 0 else self.holes).append(arr)

        gt = geom.get("type")
        if gt == "Polygon":
            add(geom.get("coordinates"))
        elif gt == "MultiPolygon":
            for pc in geom.get("coordinates") or []:
                add(pc)
        if not self.rings:
            self.bbox = None
            return
        xs = np.concatenate([r[:, 0] for r in self.rings])
        ys = np.concatenate([r[:, 1] for r in self.rings])
        self.bbox = (xs.min(), ys.min(), xs.max(), ys.max())

    def contains(self, x, y):
        import numpy as np
        if self.bbox is None:
            return False
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False

        def in_ring(ring):
            xa, ya = ring[:, 0], ring[:, 1]
            xb, yb = np.roll(xa, -1), np.roll(ya, -1)
            straddle = (ya > y) != (yb > y)
            if not straddle.any():
                return False
            with np.errstate(divide="ignore", invalid="ignore"):
                xint = xa + (y - ya) * (xb - xa) / (yb - ya)
            return bool((np.count_nonzero(straddle & (x < xint))) % 2)

        if not any(in_ring(r) for r in self.rings):
            return False
        return not any(in_ring(h) for h in self.holes)


def assign_coc(points, polys):
    """points: list of (key, lon, lat) -> {key: coc_num} (first containing CoC)."""
    out = {}
    order = list(polys.items())
    for key, lon, lat in points:
        for coc, poly in order:
            if poly.contains(lon, lat):
                out[key] = coc
                break
    return out


# --------------------------------------------------------------------- Census
def read_census():
    """-> states[], counties[], places[] from vintage-2024 estimates + 2024 gazetteer."""
    sub = _get(URL_SUBEST, name="sub-est2024.csv").decode("latin-1")
    rows = list(csv.DictReader(io.StringIO(sub)))

    def pop(r):
        return to_int(r.get("POPESTIMATE2024"))

    states, counties, places = {}, {}, []
    fips_abbr = {}

    def rows_of(blob):
        # gazetteer headers carry trailing spaces; strip every key
        out = []
        for d in csv.DictReader(io.StringIO(blob.decode("latin-1")), delimiter="\t"):
            out.append({(k or "").strip(): (v.strip() if isinstance(v, str) else v)
                        for k, v in d.items()})
        return out
    # USPS <-> FIPS from the state gazetteer (authoritative pairing)
    with zipfile.ZipFile(io.BytesIO(_get(URL_GAZ.format(kind="state"),
                                         name="2024_Gaz_state_national.zip"))) as z:
        name = z.namelist()[0]
        gaz_states = rows_of(z.read(name))

    for g in gaz_states:
        fips_abbr[g["GEOID"]] = g["USPS"]
        states[g["GEOID"]] = {
            "fips": g["GEOID"], "abbr": g["USPS"], "name": clean(g["NAME"]),
            "pop": None, "land_sqmi": float(g["ALAND_SQMI"]),
            "lat": float(g["INTPTLAT"]), "lon": float(g["INTPTLONG"]),
        }
    for r in rows:
        if r["SUMLEV"] != "040":
            continue
        st = states.get(r["STATE"])
        if st is not None:
            st["pop"] = pop(r)
            st["name"] = clean(r["STNAME"]) or st["name"]

    with zipfile.ZipFile(io.BytesIO(_get(URL_GAZ.format(kind="counties"),
                                         name="2024_Gaz_counties_national.zip"))) as z:
        name = z.namelist()[0]
        for g in rows_of(z.read(name)):
            counties[g["GEOID"]] = {
                "geoid": g["GEOID"], "name": clean(g["NAME"]), "state": g["USPS"],
                "pop": None, "lat": float(g["INTPTLAT"]), "lon": float(g["INTPTLONG"]),
            }
    for r in rows:
        if r["SUMLEV"] != "050":
            continue
        geoid = r["STATE"] + r["COUNTY"]
        c = counties.get(geoid)
        if c is not None:
            c["pop"] = pop(r)

    with zipfile.ZipFile(io.BytesIO(_get(URL_GAZ.format(kind="place"),
                                         name="2024_Gaz_place_national.zip"))) as z:
        name = z.namelist()[0]
        gaz_places = {g["GEOID"]: g for g in rows_of(z.read(name))}

    for r in rows:
        # 162 = whole place. 157 in this vintage = place-part-by-county, which
        # would triple-count multi-county cities (New York, Chicago, ...).
        if r["SUMLEV"] != "162":
            continue
        p = pop(r)
        if not p:
            continue
        geoid = r["STATE"] + r["PLACE"]
        g = gaz_places.get(geoid)
        nm = clean(r["NAME"]) or ""
        nm = re.sub(r"\s*\(balance\)\s*$", "", nm, flags=re.I)
        nm = NON_CITY_SUFFIX.sub("", nm).strip() or nm
        places.append({
            "geoid": geoid, "kind": "place", "name": nm,
            "state": fips_abbr.get(r["STATE"]),
            "pop": p,
            "lat": float(g["INTPTLAT"]) if g else None,
            "lon": float(g["INTPTLONG"]) if g else None,
        })
    places.sort(key=lambda x: x["pop"], reverse=True)
    return list(states.values()), list(counties.values()), places


# --------------------------------------------------------- national PDF totals
def national_pdf_totals(year: int):
    """Parse HUD's national 'Homeless Populations and Subpopulations' PDF (2007-2014).

    Returns {} if the year has no report or pdftotext is unavailable.
    """
    url = URL_POPSUB_NAT.format(year=year)
    try:
        b = _get(url, name=f"CoC_PopSub_NatlTerrDC_{year}.pdf")
    except Exception:
        return {}
    p = RAW / DOMAIN / f"CoC_PopSub_NatlTerrDC_{year}.pdf"
    try:
        txt = subprocess.run(["pdftotext", "-layout", str(p), "-"],
                             capture_output=True, text=True, timeout=120).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return {}
    if not txt:
        return {}
    # HUD changed the wording between the 2007-2012 and 2013+ report layouts.
    want = [
        (("Total Homeless Persons in Households", "Total Homeless Persons"),
         "pit_total_hud_published"),
        (("Total Homeless Households", "Total Households"), "pit_households"),
        (("Chronically Homeless",), "pit_chronic"),
        (("Severely Mentally Ill",), "pit_smi"),
        (("Chronic Substance Abuse",), "pit_sud"),
        (("Veterans",), "pit_veterans"),
        (("Persons with HIV/AIDS", "HIV/AIDS"), "pit_hiv"),
        (("Victims of Domestic Violence",), "pit_dv"),
    ]
    out = {}
    for line in txt.splitlines():
        s = line.strip()
        for labels, metric in want:
            if metric in out:
                continue
            for label in labels:
                if not s.startswith(label):
                    continue
                rest = s[len(label):]
                if metric == "pit_chronic" and re.match(r"\s+[A-Za-z]", rest):
                    break                     # 'Chronically Homeless Individuals' etc.
                nums = re.findall(r"[\d,]{1,12}", rest)
                nums = [int(x.replace(",", "")) for x in nums if x.strip(",")]
                if len(nums) >= 2:
                    out[metric] = nums[-1]    # last column is always 'Total'
                break
    return out


# --------------------------------------------------------------------- driver
def run(conn, cfg):
    cfg = cfg or {}
    force = bool(cfg.get("force"))
    notes, written = [], {}
    sources = []

    # 1. PIT counts per CoC-year -------------------------------------------------
    xlsb = RAW / DOMAIN / "2007-2025-PIT-Counts-by-CoC.xlsb"
    if force or not xlsb.exists():
        save_raw(DOMAIN, xlsb.name, _get(URL_PIT_XLSB))
    src_pit = source_id_for(conn, URL_PIT_XLSB, DOMAIN,
                            title="2007-2025 Point-in-Time Estimates by CoC (HUD)",
                            publisher="HUD", license=HUD_LICENSE)
    sources.append((URL_PIT_XLSB, HUD_LICENSE))
    pit_rows, coc_names, pnotes = read_pit_workbook(xlsb)
    notes.extend(pnotes)
    written["pit"] = write_pit(conn, pit_rows, src_pit)

    # 2. CoC universe + boundaries ----------------------------------------------
    coc_geo = read_coc_geometry(conn, force=force)
    src_coc = source_id_for(conn, URL_ARC_BASE, DOMAIN,
                            title="HUD Continuum of Care (CoC) Grantee Areas (Feature Service)",
                            publisher="HUD", license=HUD_LICENSE)
    sources.append((URL_ARC_BASE, HUD_LICENSE))
    polys = {}
    for coc, f in coc_geo.items():
        p = _Poly(f["geom"])
        if p.bbox is not None:
            polys[coc] = p
    notes.append(f"CoC boundaries: {len(coc_geo)} features, {len(polys)} with usable polygons")

    # 3. Census denominators ----------------------------------------------------
    states, counties, places = read_census()
    src_census = source_id_for(conn, URL_SUBEST, DOMAIN,
                               title="Population Estimates, July 1 2024 (sub-county, vintage 2024)",
                               publisher="US Census Bureau", license=CENSUS_LICENSE)
    for u in (URL_SUBEST, URL_GAZ.format(kind="place"),
              URL_GAZ.format(kind="counties"), URL_GAZ.format(kind="state")):
        sources.append((u, CENSUS_LICENSE))

    # counties -> CoC (county internal point inside the CoC polygon)
    county_pts = [(c["geoid"], c["lon"], c["lat"]) for c in counties
                  if c["lat"] is not None]
    county_coc = assign_coc(county_pts, polys)
    cpop, csum, claws = {}, {}, {}
    for c in counties:
        coc = county_coc.get(c["geoid"])
        if not coc or c["pop"] is None:
            continue
        cpop[coc] = cpop.get(coc, 0) + c["pop"]
        csum[coc] = csum.get(coc, 0) + 1
        w = cpop[coc]
        lat = c["lat"] * c["pop"]
        lon = c["lon"] * c["pop"]
        prev = claws.get(coc, (0.0, 0.0))
        claws[coc] = (prev[0] + lat, prev[1] + lon)
    for coc, (lat, lon) in list(claws.items()):
        p = cpop.get(coc) or 0
        claws[coc] = (lat / p, lon / p) if p else (None, None)
    assigned_county_pop = sum(cpop.values())
    notes.append(f"counties mapped to CoCs: {len(county_coc)}/{len(counties)}; "
                 f"sum pop {assigned_county_pop:,}")

    # 4. geo_state --------------------------------------------------------------
    n = 0
    for st in states:
        if not st["fips"]:
            continue
        upsert(conn, "geo_state", {"fips": st["fips"], "abbr": st["abbr"],
                                   "name": st["name"], "pop": st["pop"],
                                   "lat": st["lat"], "lon": st["lon"],
                                   "land_sqmi": st["land_sqmi"]}, ["fips"])
        n += 1
    written["geo_state"] = n

    # 5. geo_city (largest 1,000 places) ---------------------------------------
    top_places = places[:1000]
    place_pts = [(p["geoid"], p["lon"], p["lat"]) for p in top_places
                 if p["lon"] is not None and p["lat"] is not None]
    place_coc = assign_coc(place_pts, polys)
    cities_by_coc = {}
    n = 0
    for p in top_places:
        coc = place_coc.get(p["geoid"])
        upsert(conn, "geo_city", {"geoid": p["geoid"], "kind": p["kind"],
                                  "name": p["name"], "state": p["state"],
                                  "pop": p["pop"], "lat": p["lat"], "lon": p["lon"],
                                  "coc_num": coc, "source_id": src_census},
               ["geoid"])
        n += 1
        if coc:
            cities_by_coc.setdefault(coc, []).append(p)
    written["geo_city"] = n
    for lst in cities_by_coc.values():
        lst.sort(key=lambda x: x["pop"], reverse=True)
    notes.append(f"places mapped to a CoC: {len(place_coc)}/{len(top_places)}")

    # 6. geo_coc ---------------------------------------------------------------
    universe = set(coc_names) | set(coc_geo)
    n = 0
    for coc in sorted(universe):
        g = coc_geo.get(coc, {})
        name = coc_names.get(coc) or g.get("name")
        state = g.get("state") or coc.split("-")[0]
        cities = cities_by_coc.get(coc) or []
        city_txt = ", ".join(c["name"] for c in cities[:3]) or None
        if not city_txt and name:
            # fall back to the locality the CoC name starts with ("Anchorage CoC")
            head = re.split(r"[/,]", re.sub(r"\bCoC\b|\bContinuum of Care\b", "", name))[0]
            head = re.sub(r"\bCounties?\b|\bBalance of State\b|\bCity & County\b", "",
                          head).strip(" -&")
            city_txt = clean(head) or None
        lat, lon = claws.get(coc, (None, None))
        if lat is None and cities:
            lat, lon = cities[0]["lat"], cities[0]["lon"]
        geom = coc_geo.get(coc, {}).get("geom")
        upsert(conn, "geo_coc", {
            "coc_num": coc, "name": name, "state": state, "cities": city_txt,
            "pop": cpop.get(coc), "lat": lat, "lon": lon,
            "geometry": json.dumps(geom, separators=(",", ":")) if geom else None,
            "source_id": src_coc,
        }, ["coc_num"])
        n += 1
    written["geo_coc"] = n

    # 7. national metrics -------------------------------------------------------
    years = sorted({y for (_, y) in pit_rows})
    def tot(year, key):
        vals = [r.get(key) for (c, y), r in pit_rows.items() if y == year and r.get(key) is not None]
        return sum(vals) if vals else None

    n = 0
    newest = years[-1] if years else None
    for y in years:
        series = {
            "pit_total": tot(y, "total"),
            "pit_sheltered": tot(y, "sheltered"),
            "pit_unsheltered": tot(y, "unsheltered"),
            "pit_chronic": tot(y, "chronic"),
            "pit_veterans": tot(y, "veterans"),
            "pit_youth_under25": tot(y, "youth"),
            "pit_children": tot(y, "children"),
            "pit_families": tot(y, "families"),
            "pit_family_households": tot(y, "family_households"),
        }
        for metric, val in series.items():
            if val is None:
                continue
            upsert(conn, "metric", {
                "entity_type": "us", "entity_id": "US", "metric": metric,
                "value": float(val), "unit": "persons", "year": y, "is_est": 0,
                "note": "sum of HUD-published CoC counts for that year",
                "source_id": src_pit,
            }, ["entity_type", "entity_id", "metric", "year"])
            n += 1

    # HUD's own national report totals (2007-2014); NULl after HUD dropped
    # SMI/SUD/HIV/DV from the PIT instrument.
    src_pdf = source_id_for(conn, URL_POPSUB_NAT.format(year=2007), DOMAIN,
                            title="HUD CoC Homeless Populations and Subpopulations Reports (national PDFs)",
                            publisher="HUD", license=HUD_LICENSE)
    sources.append((URL_POPSUB_NAT.format(year=2007), HUD_LICENSE))
    pdf_years = []
    for y in range(2007, (max(years) if years else 2025) + 1):
        vals = national_pdf_totals(y)
        if not vals:
            continue
        pdf_years.append(y)
        for metric, val in vals.items():
            upsert(conn, "metric", {
                "entity_type": "us", "entity_id": "US", "metric": metric,
                "value": float(val), "unit": "persons" if "household" not in metric else "households",
                "year": y, "is_est": 0,
                "note": "HUD national PopSub report, 'Total' column",
                "source_id": src_pdf,
            }, ["entity_type", "entity_id", "metric", "year"])
            n += 1
    if pdf_years:
        notes.append(f"national PopSub PDFs parsed for {pdf_years[0]}-{pdf_years[-1]}")

    # 8. city-level value for the newest year (inherited from the city's CoC) ---
    if newest:
        m = 0
        for p in top_places:
            coc = place_coc.get(p["geoid"])
            if not coc:
                continue
            rec = pit_rows.get((coc, newest), {})
            for metric, key in (("pit_total", "total"), ("pit_sheltered", "sheltered"),
                                ("pit_unsheltered", "unsheltered")):
                val = rec.get(key)
                if val is None:
                    continue
                upsert(conn, "metric", {
                    "entity_type": "city", "entity_id": p["geoid"], "metric": metric,
                    "value": float(val), "unit": "persons", "year": newest, "is_est": 1,
                    "note": f"inherited from CoC {coc} ({newest}); city not counted separately",
                    "source_id": src_pit,
                }, ["entity_type", "entity_id", "metric", "year"])
                m += 1
        notes.append(f"city metrics for {newest}: {m} rows (is_est=1, CoC-inherited)")
    written["metric"] = n + (m if newest else 0)

    total = sum(written.values())
    return {"rows": total, "notes": "; ".join(notes[:12]), "sources": sources,
            "detail": written}


# ------------------------------------------------------------------- selftest
def _selftest():
    from ingest.common import db as _db
    conn = _db()
    for t, q in (("pit", "SELECT COUNT(*) FROM pit"),
                 ("geo_coc", "SELECT COUNT(*) FROM geo_coc"),
                 ("geo_city", "SELECT COUNT(*) FROM geo_city"),
                 ("geo_state", "SELECT COUNT(*) FROM geo_state"),
                 ("pit years", "SELECT MIN(year)||'-'||MAX(year) FROM pit"),
                 ("pit cocs", "SELECT COUNT(DISTINCT coc_num) FROM pit")):
        print(f"  {t}: {conn.execute(q).fetchone()[0]}")
    for r in conn.execute("SELECT * FROM pit ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))
    for r in conn.execute("SELECT coc_num,name,state,cities,pop,lat,lon FROM geo_coc "
                          "ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        from ingest.common import db, now as _now
        conn = db()
        res = run(conn, {})
        conn.commit()
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                     (DOMAIN, _now(), res["rows"], res["notes"][:2000]))
        conn.commit()
        print(json.dumps({k: res[k] for k in ("rows", "notes", "detail")}, indent=1))
