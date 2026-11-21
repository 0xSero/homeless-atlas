"""county: a per-county layer of housing, poverty, health-access, homelessness-count and
federal-award measures, keyed by 5-digit county FIPS (entity_type='county').

Sources (all keyless, every response cached by ingest.common.fetch under data/raw/county/)
-----------------------------------------------------------------------------------------
1. American Community Survey 2020-2024 5-year, Census table-based summary files
   www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/
   acsdt5y2024-<table>.dat (pipe-delimited, one file per table, every summary level).
   The Census API now demands a key, so the static files are used instead. Only county rows
   (GEO_ID 0500000US<fips>) are kept, plus tract rows (1400000US<geoid>) of B01003.
2. Census 2024 Gazetteer, census tracts (2024_Gaz_tracts_national.zip): tract internal points.
3. HUD CoC boundaries already in geo_coc.geometry (FY2024, simplified GeoJSON) and HUD PIT
   counts already in the pit table (written by coc_geometry and hud_pit).
4. CDC PLACES county data, 2025 release (Socrata swc5-untb on data.cdc.gov).
5. HRSA mental health HPSA detail file (data.hrsa.gov BCD_HPSA_FCT_DET_MH.csv).
6. USASpending.gov spending_by_geography (county, place of performance), filtered to the
   federal assistance listings that fund homelessness programs.
7. USASpending.gov spending_by_category/recipient, same listings, one call per county that
   had any of that spending: the top recipients over FY2021-FY2025.

What is written
---------------
metric rows, entity_type='county', entity_id=5-digit FIPS:
  acs_population, poverty_rate, median_household_income, median_gross_rent, median_home_value,
  rent_burden_30, rent_burden_50, renter_share, rental_vacancy_rate, unemployment_rate,
  uninsured_rate, veteran_share, overcrowded_share, public_assistance_share, no_vehicle_share
      (year=ACS_YEAR, is_est=0; ratios are computed from published counts, formula in note)
  county_coc_count                     CoCs touching the county (is_est=1, from simplified polygons)
  pit_total, pit_sheltered, pit_unsheltered, pit_chronic, pit_veterans, pit_youth
      only where one CoC and the county are the same area (is_est=0, the HUD count itself)
  pit_total_apportioned                everywhere else (is_est=1, population-weighted share)
  places_mental_distress, places_depression, places_binge, places_uninsured_18_64,
  places_disability                    (is_est=1, PLACES values are model-based estimates)
  mh_hpsa_count, mh_hpsa_max_score, mh_hpsa_geographic
  federal_homeless_awards, federal_homeless_awards_per_capita   (year = federal fiscal year)
county_coc table: county to CoC crosswalk weighted by tract population (rebuilt every run).
county_recipient table: top 15 recipients per county over FY2021-FY2025, matched to org by UEI,
  then by name_norm plus the county's state (the API returns no recipient state); rows for a
  county are deleted and rewritten whenever that county is refetched. `--states PA,NY` limits
  this step to some states (about 900 counties nationally, one request per second).

Caveats
-------
* ACS margins of error are not stored. Negative Census annotation codes (e.g. -666666666,
  "estimate not available") are treated as missing; a ratio with a zero or missing
  denominator is not written.
* The crosswalk assigns each tract wholly to the CoC holding its internal point, using the
  simplified FY2024 CoC polygons; it is an approximation of the real boundary.
* HUD does not count homelessness by county. pit_total_apportioned is an estimate, and the
  FY2024 boundaries are applied to every PIT year, so earlier years across a CoC merger or
  split are less reliable.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import time
import traceback
import urllib.parse
import zipfile
from datetime import datetime, timezone

from ingest import common
from ingest.common import clean, db, fetch, name_norm, now, source_id_for, upsert

DOMAIN = "county"

# ---- ACS ----------------------------------------------------------------------------------
ACS_YEAR = 2024  # bump when the next 5-year table-based summary file is published
ACS_BASE = "https://www2.census.gov/programs-surveys/acs/summary_file"
CENSUS_PUB = "U.S. Census Bureau"
CENSUS_LIC = "Public domain (U.S. Government work)"


def acs_url(table: str) -> str:
    return (f"{ACS_BASE}/{ACS_YEAR}/table-based-SF/data/5YRData/"
            f"acsdt5y{ACS_YEAR}-{table.lower()}.dat")


def _acs_label() -> str:
    return f"ACS {ACS_YEAR - 4}-{ACS_YEAR} 5-year"


# metric, unit, primary table, numerator columns, denominator plus, denominator minus
# A metric with no denominator is the published value itself. Columns were checked against
# the header of each 2024 file and the ACS20245YR table shells.
ACS_METRICS = [
    ("acs_population", "persons", "B01003", ["B01003_E001"], None, None),
    ("poverty_rate", "pct", "B17001", ["B17001_E002"], ["B17001_E001"], []),
    ("median_household_income", "usd", "B19013", ["B19013_E001"], None, None),
    ("median_gross_rent", "usd", "B25064", ["B25064_E001"], None, None),
    ("median_home_value", "usd", "B25077", ["B25077_E001"], None, None),
    ("rent_burden_30", "pct", "B25070",
     ["B25070_E007", "B25070_E008", "B25070_E009", "B25070_E010"],
     ["B25070_E001"], ["B25070_E011"]),
    ("rent_burden_50", "pct", "B25070", ["B25070_E010"], ["B25070_E001"], ["B25070_E011"]),
    ("renter_share", "pct", "B25003", ["B25003_E003"], ["B25003_E001"], []),
    ("rental_vacancy_rate", "pct", "B25004", ["B25004_E002"],
     ["B25003_E003", "B25004_E002", "B25004_E003"], []),
    ("unemployment_rate", "pct", "B23025", ["B23025_E005"], ["B23025_E003"], []),
    ("uninsured_rate", "pct", "B27010",
     ["B27010_E017", "B27010_E033", "B27010_E050", "B27010_E066"], ["B27010_E001"], []),
    ("veteran_share", "pct", "B21001", ["B21001_E002"], ["B21001_E001"], []),
    ("overcrowded_share", "pct", "B25014",
     ["B25014_E005", "B25014_E006", "B25014_E007", "B25014_E011", "B25014_E012",
      "B25014_E013"], ["B25014_E001"], []),
    ("public_assistance_share", "pct", "B19057", ["B19057_E002"], ["B19057_E001"], []),
    ("no_vehicle_share", "pct", "B08201", ["B08201_E002"], ["B08201_E001"], []),
]

TABLE_TITLES = {
    "B01003": "Total Population",
    "B17001": "Poverty Status in the Past 12 Months by Sex by Age",
    "B19013": "Median Household Income in the Past 12 Months",
    "B25064": "Median Gross Rent (Dollars)",
    "B25077": "Median Value (Dollars)",
    "B25070": "Gross Rent as a Percentage of Household Income in the Past 12 Months",
    "B25003": "Tenure",
    "B25004": "Vacancy Status",
    "B23025": "Employment Status for the Population 16 Years and Over",
    "B27010": "Types of Health Insurance Coverage by Age",
    "B21001": "Sex by Age by Veteran Status for the Civilian Population 18 Years and Over",
    "B25014": "Tenure by Occupants per Room",
    "B19057": "Public Assistance Income in the Past 12 Months for Households",
    "B08201": "Household Size by Vehicles Available",
}

# ---- crosswalk ----------------------------------------------------------------------------
GAZ_TRACTS_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
                  "2024_Gaz_tracts_national.zip")
COC_GEOMETRY_YEAR = 2024
XWALK_METHOD = f"tract-intpt-pip-acs{ACS_YEAR}"
NONE_COC = "(none)"
SAME_AREA = 0.99

PIT_FIELDS = [("pit_total", "total"), ("pit_sheltered", "sheltered"),
              ("pit_unsheltered", "unsheltered"), ("pit_chronic", "chronic"),
              ("pit_veterans", "veterans"), ("pit_youth", "youth")]

# ---- PLACES -------------------------------------------------------------------------------
PLACES_HOST = "https://data.cdc.gov/resource"
PLACES_ID = "swc5-untb"  # County Data, 2025 release
PLACES_PRIOR_ID = "fu4u-a9bh"  # County Data, 2024 release
# The 2025 release carries no rows for these measures in Pennsylvania (and some other
# counties). When True, a county-measure missing from the 2025 release is taken from the 2024
# release instead (its own source row and its own data year). Off by default: mixing two model
# vintages is a methodology choice for the project owner.
PLACES_PRIOR_FALLBACK = True
PLACES_MEASURES = {
    "MHLTH": "places_mental_distress",
    "DEPRESSION": "places_depression",
    "BINGE": "places_binge",
    "ACCESS2": "places_uninsured_18_64",
    "DISABILITY": "places_disability",
}
PLACES_LABELS = {
    "MHLTH": "frequent mental distress (14+ bad mental health days in 30), adults",
    "DEPRESSION": "ever told they have depression, adults",
    "BINGE": "binge drinking, adults",
    "ACCESS2": "no health insurance, adults 18-64",
    "DISABILITY": "any disability, adults",
}
CDC_LIC = "Public domain (CDC)"
PAGE = 50000

# ---- HRSA ---------------------------------------------------------------------------------
HPSA_URL = "https://data.hrsa.gov/DataDownload/DD_Files/BCD_HPSA_FCT_DET_MH.csv"
HRSA_PUB = "Health Resources and Services Administration (HRSA)"

# ---- USASpending --------------------------------------------------------------------------
USAS_GEO_URL = "https://api.usaspending.gov/api/v2/search/spending_by_geography/"
USAS_PUB = "U.S. Department of the Treasury, USAspending.gov"
USAS_LIC = "Public domain (U.S. Government work)"
HOMELESS_PROGRAMS = {
    "14.267": "Continuum of Care",
    "14.231": "Emergency Solutions Grant",
    "14.276": "Youth Homelessness Demonstration",
    "64.024": "VA Homeless Providers Grant and Per Diem",
    "64.033": "Supportive Services for Veteran Families",
    "93.150": "PATH",
    "93.623": "Basic Center (runaway and homeless youth)",
    "14.241": "HOPWA",
}
FISCAL_YEARS = range(2021, 2026)
_PROGRAMS_OK: list[str] = []  # listings that returned county spending in step 6

USAS_RECIPIENT_URL = "https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/"
RECIPIENT_LIMIT = 15


# =========================================================================== helpers
def _cache_file(url, method="GET", data=None):
    """The cache path common.fetch() uses for this request (same key derivation)."""
    key = hashlib.sha1((method + url + json.dumps(data or {}, sort_keys=True,
                                                 default=str)).encode()).hexdigest()
    p = common.RAW / DOMAIN / f"{key}.bin"
    return p if p.exists() and p.stat().st_size else None


def _retrieved_at(url, method="GET", data=None) -> datetime:
    """When the cached copy was fetched (meta file), else now."""
    p = _cache_file(url, method, data)
    if p is not None:
        meta = p.with_suffix(".meta.json")
        try:
            return datetime.fromisoformat(json.loads(meta.read_text())["at"])
        except Exception:  # noqa
            pass
    return datetime.now(timezone.utc)


def _sid(conn, url, *, method="GET", data=None, **kw) -> int:
    """source_id_for(), plus bytes/sha256 from the real fetch() cache file (different key)."""
    sid = source_id_for(conn, url, DOMAIN, **kw)
    p = _cache_file(url, method, data)
    if p is not None:
        row = conn.execute("SELECT bytes, sha256 FROM source WHERE id=?", (sid,)).fetchone()
        if row[0] is None or row[1] is None:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            conn.execute("UPDATE source SET bytes=?, sha256=? WHERE id=?",
                         (p.stat().st_size, h.hexdigest(), sid))
    return sid


def _metric(conn, eid, metric, value, unit, year, sid, is_est=0, note=None) -> int:
    if eid is None or value is None or year is None:
        return 0
    upsert(conn, "metric",
           {"entity_type": "county", "entity_id": str(eid), "metric": metric,
            "value": float(value), "unit": unit, "year": int(year), "is_est": int(is_est),
            "note": clean(note), "source_id": sid},
           keys=["entity_type", "entity_id", "metric", "year"])
    return 1


def _num(s):
    """ACS cell -> float, or None for blanks and negative Census annotation codes."""
    if s is None:
        return None
    s = s.strip()
    if not s or s.lower() in ("null", "n", "(x)", "-"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if v < 0 else v


def _county_fips(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT geoid FROM geo_city WHERE kind='county'")}


# =========================================================================== step 1: ACS
_ACS_CACHE: dict = {}


def _acs_table(table: str, *, tracts=False):
    """Fetch (cached) one table file; return (header set, {fips: {col: value}}, {tract: {..}})."""
    memo = (table, tracts)
    if memo in _ACS_CACHE:
        return _ACS_CACHE[memo]
    raw = fetch(acs_url(table), domain=DOMAIN, timeout=600)
    buf = io.BytesIO(raw)
    header = buf.readline().decode().strip().split("|")
    idx = {c: i for i, c in enumerate(header)}
    counties, tract_rows = {}, {}
    for line in buf:
        if line.startswith(b"0500000US"):
            target, key = counties, line[9:14].decode()
        elif tracts and line.startswith(b"1400000US"):
            target, key = tract_rows, line[9:20].decode()
        else:
            continue
        parts = line.decode().rstrip("\r\n").split("|")
        target[key] = {c: _num(parts[i]) for c, i in idx.items() if c != "GEO_ID"
                       and "_E" in c and i < len(parts)}
    del raw, buf
    out = (set(header), counties, tract_rows)
    _ACS_CACHE[memo] = out
    return out


def _acs_tables_needed():
    t = []
    for _m, _u, primary, num, dp, dm in ACS_METRICS:
        for c in [*num, *(dp or []), *(dm or [])]:
            tab = c.split("_")[0]
            if tab not in t:
                t.append(tab)
        if primary not in t:
            t.append(primary)
    return t


def _formula(num, dp, dm) -> str:
    n = " + ".join(num)
    if dp is None:
        return n
    d = " + ".join(dp) + "".join(f" - {c}" for c in dm)
    nn = f"({n})" if len(num) > 1 else n
    dd = f"({d})" if len(dp) + len(dm) > 1 else d
    return f"{nn} / {dd} * 100"


def step_acs(conn, cfg=None) -> tuple[int, str]:
    tables = _acs_tables_needed()
    data, sids = {}, {}
    for t in tables:
        header, counties, _ = _acs_table(t, tracts=(t == "B01003"))
        data[t] = (header, counties)
        sids[t] = _sid(conn, acs_url(t), title=f"{_acs_label()} table {t}: {TABLE_TITLES.get(t, t)}"
                       f" (table-based summary file)", publisher=CENSUS_PUB, license=CENSUS_LIC)
    missing_cols = []
    for m, _u, _p, num, dp, dm in ACS_METRICS:
        for c in [*num, *(dp or []), *(dm or [])]:
            if c not in data[c.split("_")[0]][0]:
                missing_cols.append(f"{m}:{c}")
    if missing_cols:
        raise RuntimeError(f"ACS columns not in file header: {missing_cols}")

    n, per = 0, {}
    all_fips = set()
    for t in tables:
        all_fips |= set(data[t][1])
    for m, unit, primary, num, dp, dm in ACS_METRICS:
        tabs = sorted({c.split("_")[0] for c in [*num, *(dp or []), *(dm or [])]})
        formula = _formula(num, dp, dm)
        note = f"{_acs_label()}, table {', '.join(tabs)}; {formula}"
        k = 0
        for fips in all_fips:
            def g(c):
                return data[c.split("_")[0]][1].get(fips, {}).get(c)
            nv = [g(c) for c in num]
            if any(v is None for v in nv):
                continue
            if dp is None:
                val = nv[0]
            else:
                dvals = [g(c) for c in dp] + [g(c) for c in dm]
                if any(v is None for v in dvals):
                    continue
                den = sum(g(c) for c in dp) - sum(g(c) for c in dm)
                if den <= 0:
                    continue
                val = round(sum(nv) / den * 100, 3)
            k += _metric(conn, fips, m, val, unit, ACS_YEAR, sids[primary], 0, note)
        per[m] = k
        n += k
    return n, f"{len(all_fips)} county rows in {len(tables)} tables; " + ", ".join(
        f"{m}={v}" for m, v in per.items())


# =========================================================================== step 2: crosswalk
class _Ring:
    """One polygon ring with its edges bucketed into latitude bands for fast ray casting."""
    __slots__ = ("ymin", "ymax", "h", "k", "bands")

    def __init__(self, ring):
        edges = []
        n = len(ring)
        for i in range(n):
            x0, y0 = ring[i - 1][0], ring[i - 1][1]
            x1, y1 = ring[i][0], ring[i][1]
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
        ys = [p[1] for p in ring] or [0.0]
        self.ymin, self.ymax = min(ys), max(ys)
        self.k = max(1, len(edges) // 4)
        self.h = (self.ymax - self.ymin) / self.k or 1.0
        self.bands = [[] for _ in range(self.k)]
        for e in edges:
            lo = int((min(e[1], e[3]) - self.ymin) / self.h)
            hi = int((max(e[1], e[3]) - self.ymin) / self.h)
            for b in range(max(0, lo), min(self.k - 1, hi) + 1):
                self.bands[b].append(e)

    def crossings(self, x, y) -> int:
        """Parity (0/1) of ray crossings to the right of (x, y)."""
        if y < self.ymin or y > self.ymax:
            return 0
        b = min(self.k - 1, int((y - self.ymin) / self.h))
        c = 0
        for x0, y0, x1, y1 in self.bands[b]:
            if (y0 > y) != (y1 > y):
                if x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                    c ^= 1
        return c


def _signed_area(ring):
    a = 0.0
    for i in range(len(ring) - 1):
        a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return a * 0.5


def _coc_index(conn):
    """[(area, coc_num, bbox, [(bbox, [_Ring outer, _Ring hole, ...])])] sorted by area."""
    idx = []
    for coc_num, gtxt in conn.execute(
            "SELECT coc_num, geometry FROM geo_coc WHERE geometry IS NOT NULL AND geometry<>''"):
        try:
            g = json.loads(gtxt)
        except ValueError:
            continue
        if g.get("type") == "Polygon":
            polys = [g["coordinates"]]
        elif g.get("type") == "MultiPolygon":
            polys = g["coordinates"]
        else:
            continue
        parts, area = [], 0.0
        for poly in polys:
            if not poly or len(poly[0]) < 4:
                continue
            outer = poly[0]
            xs = [p[0] for p in outer]
            ys = [p[1] for p in outer]
            area += abs(_signed_area(outer))  # outer-ring area, the coc_geometry rule
            parts.append(((min(xs), min(ys), max(xs), max(ys)), [_Ring(r) for r in poly]))
        if not parts:
            continue
        box = (min(p[0][0] for p in parts), min(p[0][1] for p in parts),
               max(p[0][2] for p in parts), max(p[0][3] for p in parts))
        idx.append((area, coc_num, box, parts))
    idx.sort(key=lambda t: (t[0], t[1]))
    return idx


def _locate(idx, lon, lat):
    """Smallest-area CoC whose polygon holds the point (even-odd rule, holes respected)."""
    for _area, coc_num, box, parts in idx:  # ascending area: first hit is the smallest
        if not (box[0] <= lon <= box[2] and box[1] <= lat <= box[3]):
            continue
        for b, rings in parts:
            if not (b[0] <= lon <= b[2] and b[1] <= lat <= b[3]):
                continue
            c = 0
            for r in rings:
                c ^= r.crossings(lon, lat)
            if c:
                return coc_num
    return None


def _gaz_points():
    raw = fetch(GAZ_TRACTS_URL, domain=DOMAIN, timeout=600)
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
    text = z.read(name).decode("utf-8", "replace")
    rd = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(rd)]
    gi, la, lo = header.index("GEOID"), header.index("INTPTLAT"), header.index("INTPTLONG")
    pts = {}
    for row in rd:
        if len(row) <= max(gi, la, lo):
            continue
        try:
            pts[row[gi].strip()] = (float(row[lo]), float(row[la]))
        except ValueError:
            continue
    return pts


def step_crosswalk(conn, cfg=None) -> tuple[int, str]:
    conn.execute("""CREATE TABLE IF NOT EXISTS county_coc(
                      county_fips TEXT, coc_num TEXT, pop_in INTEGER, county_share REAL,
                      coc_share REAL, tracts INTEGER, method TEXT, source_id INTEGER,
                      PRIMARY KEY(county_fips, coc_num))""")
    _, _, tract_pop = _acs_table("B01003", tracts=True)
    pts = _gaz_points()
    sid = _sid(conn, GAZ_TRACTS_URL, title="Census 2024 Gazetteer, census tracts (internal points)",
               publisher=CENSUS_PUB, license=CENSUS_LIC)
    _sid(conn, acs_url("B01003"), title=f"{_acs_label()} table B01003: Total Population "
         f"(table-based summary file)", publisher=CENSUS_PUB, license=CENSUS_LIC)
    idx = _coc_index(conn)
    if not idx:
        raise RuntimeError("geo_coc has no geometry; run coc_geometry first")

    agg: dict[tuple[str, str], list] = {}
    no_point = 0
    t0 = time.time()
    for geoid, vals in tract_pop.items():
        pop = vals.get("B01003_E001") or 0
        pt = pts.get(geoid)
        if pt is None:
            no_point += 1
            continue
        coc = _locate(idx, pt[0], pt[1]) or NONE_COC
        a = agg.setdefault((geoid[:5], coc), [0, 0])
        a[0] += int(pop)
        a[1] += 1
    county_tot, coc_tot = {}, {}
    for (cf, coc), (p, _t) in agg.items():
        county_tot[cf] = county_tot.get(cf, 0) + p
        coc_tot[coc] = coc_tot.get(coc, 0) + p

    conn.execute("DELETE FROM county_coc")
    for (cf, coc), (p, t) in sorted(agg.items()):
        conn.execute("""INSERT INTO county_coc(county_fips, coc_num, pop_in, county_share,
                          coc_share, tracts, method, source_id) VALUES(?,?,?,?,?,?,?,?)""",
                     (cf, coc, p, (p / county_tot[cf]) if county_tot[cf] else None,
                      (p / coc_tot[coc]) if coc != NONE_COC and coc_tot[coc] else None,
                      t, XWALK_METHOD, sid))

    # number of real CoCs touching each county
    real = {}
    for (cf, coc), (p, t) in agg.items():
        if coc != NONE_COC:
            real.setdefault(cf, set()).add(coc)
    note = (f"CoCs whose FY{COC_GEOMETRY_YEAR} boundary (simplified) holds the internal point of "
            f"at least one of this county's census tracts; derived, not published")
    for cf, s in real.items():
        _metric(conn, cf, "county_coc_count", len(s), "CoCs", COC_GEOMETRY_YEAR, sid, 1, note)

    nat = sum(county_tot.values())
    assigned = nat - coc_tot.get(NONE_COC, 0)
    msg = (f"{len(agg)} county-CoC rows; {len(real)} counties with >=1 CoC of "
           f"{len(county_tot)} with tracts; {len(tract_pop)} tracts, {no_point} without a "
           f"gazetteer point; {assigned / nat * 100:.2f}% of national tract population "
           f"assigned to a CoC; {len(idx)} CoC polygons; pip {time.time() - t0:.1f}s")
    return len(agg), msg


# =========================================================================== step 3: PIT
def step_pit_by_county(conn, cfg=None) -> tuple[int, str]:
    xw = {}
    for cf, coc, cs, ks in conn.execute(
            "SELECT county_fips, coc_num, county_share, coc_share FROM county_coc"):
        if coc != NONE_COC:
            xw.setdefault(cf, []).append((coc, cs or 0.0, ks or 0.0))
    if not xw:
        raise RuntimeError("county_coc is empty; run the crosswalk step first")
    cols = ", ".join(f for _m, f in PIT_FIELDS)
    pit = {}
    for r in conn.execute(f"SELECT coc_num, year, {cols}, is_est, source_id FROM pit"):
        pit[(r[0], r[1])] = r
    years = sorted({y for _c, y in pit})

    names = [m for m, _f in PIT_FIELDS] + ["pit_total_apportioned"]
    conn.execute(f"DELETE FROM metric WHERE entity_type='county' AND metric IN "
                 f"({','.join('?' * len(names))})", names)
    n = same = appo = skipped = 0
    for cf, cocs in xw.items():
        big = [c for c in cocs if c[1] >= SAME_AREA]
        if len(big) == 1 and big[0][2] >= SAME_AREA:
            coc = big[0][0]
            same += 1
            note = f"HUD PIT count for {coc}, whose boundary is this county"
            for y in years:
                r = pit.get((coc, y))
                if r is None:
                    continue
                est = 1 if r["is_est"] else 0
                for m, f in PIT_FIELDS:
                    n += _metric(conn, cf, m, r[f], "persons", y, r["source_id"], est, note)
            continue
        appo += 1
        cocs = sorted(cocs, key=lambda c: c[0])
        for y in years:
            rows = [(c, pit.get((c[0], y))) for c in cocs if c[2] > 0]
            if not rows or any(r is None or r["total"] is None for _c, r in rows):
                skipped += 1  # a contributing CoC has no count this year: write nothing
                continue
            parts = [(c[0], r["total"] * c[2], r["source_id"]) for c, r in rows if r is not None]
            val = round(sum(p[1] for p in parts))
            lead = max(parts, key=lambda p: p[1])
            note = (f"population-apportioned share of {', '.join(p[0] for p in parts)}; "
                    f"HUD does not count by county")
            n += _metric(conn, cf, "pit_total_apportioned", val, "persons", y, lead[2], 1, note)
    return n, (f"{same} counties are their own CoC (pit_* is_est=0), {appo} apportioned; "
               f"{skipped} county-years skipped for a missing CoC count; years {years[0]}-{years[-1]}")


# =========================================================================== step 4: PLACES
def _places_rows(dataset: str) -> list[dict]:
    where = ("measureid in('" + "','".join(PLACES_MEASURES) + "') AND datavaluetypeid='CrdPrv'")
    rows, offset = [], 0
    while True:
        q = [("$select", "year,locationid,measureid,data_value,datavaluetypeid"),
             ("$where", where), ("$limit", str(PAGE))]
        if offset:
            q.append(("$offset", str(offset)))
        q.append(("$order", "locationid,measureid,year"))
        url = f"{PLACES_HOST}/{dataset}.json?" + urllib.parse.urlencode(
            q, quote_via=urllib.parse.quote)
        batch = json.loads(fetch(url, domain=DOMAIN, headers={"Accept": "application/json"},
                                 timeout=300))
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE


def step_places(conn, cfg=None) -> tuple[int, str]:
    sid = _sid(conn, f"{PLACES_HOST}/{PLACES_ID}.json",
               title="CDC PLACES: Local Data for Better Health, County Data, 2025 release "
                     "(attribution: CDC PLACES)",
               publisher="Centers for Disease Control and Prevention", license=CDC_LIC)
    n, have, per = 0, set(), {}
    for r in _places_rows(PLACES_ID):
        fips, mid = (r.get("locationid") or "").strip(), r.get("measureid")
        if len(fips) != 5 or mid not in PLACES_MEASURES or r.get("data_value") in (None, ""):
            continue
        m = PLACES_MEASURES[mid]
        note = (f"CDC PLACES 2025 release, {mid}: {PLACES_LABELS[mid]}, crude prevalence; "
                f"model-based small-area estimate from BRFSS {r.get('year')}, not a direct survey count")
        k = _metric(conn, fips, m, float(r["data_value"]), "pct", r.get("year"), sid, 1, note)
        n += k
        per[m] = per.get(m, 0) + k
        have.add((fips, mid))
    msg = ", ".join(f"{m}={v}" for m, v in sorted(per.items()))
    if PLACES_PRIOR_FALLBACK:
        psid = _sid(conn, f"{PLACES_HOST}/{PLACES_PRIOR_ID}.json",
                    title="CDC PLACES: Local Data for Better Health, County Data, 2024 release "
                          "(attribution: CDC PLACES)",
                    publisher="Centers for Disease Control and Prevention", license=CDC_LIC)
        k = 0
        for r in _places_rows(PLACES_PRIOR_ID):
            fips, mid = (r.get("locationid") or "").strip(), r.get("measureid")
            if (len(fips) != 5 or mid not in PLACES_MEASURES or (fips, mid) in have
                    or r.get("data_value") in (None, "")):
                continue
            note = (f"CDC PLACES 2024 release (county absent from the 2025 release), {mid}: "
                    f"{PLACES_LABELS[mid]}, crude prevalence; model-based small-area estimate "
                    f"from BRFSS {r.get('year')}")
            k += _metric(conn, fips, PLACES_MEASURES[mid], float(r["data_value"]), "pct",
                         r.get("year"), psid, 1, note)
        n += k
        msg += f"; {k} rows from the 2024 release fallback"
    return n, msg


# =========================================================================== step 5: HPSA
def step_hpsa(conn, cfg=None) -> tuple[int, str]:
    raw = fetch(HPSA_URL, domain=DOMAIN, timeout=600)
    year = _retrieved_at(HPSA_URL).year
    sid = _sid(conn, HPSA_URL, title="HRSA Health Professional Shortage Areas, mental health, "
               "detail file (BCD_HPSA_FCT_DET_MH)", publisher=HRSA_PUB, license=CENSUS_LIC)
    rd = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace")))
    hpsas: dict[str, dict[str, float | None]] = {}
    geo: set[str] = set()
    proposed = 0
    for r in rd:
        if (r.get("HPSA Discipline Class") or "").strip() != "Mental Health":
            continue
        status = (r.get("HPSA Status") or "").strip()
        if status == "Proposed For Withdrawal":
            proposed += 1
        if status != "Designated":
            continue
        fips = (r.get("Common State County FIPS Code") or "").strip()
        hid = (r.get("HPSA ID") or "").strip()
        if len(fips) != 5 or not hid:
            continue
        try:
            score = float(r.get("HPSA Score") or "")
        except ValueError:
            score = None
        hpsas.setdefault(fips, {})[hid] = score
        dtype = (r.get("Designation Type") or "").strip()
        comp = (r.get("HPSA Component Type Description") or "").strip()
        if dtype in ("Geographic HPSA", "High Needs Geographic HPSA") and comp == "Single County":
            geo.add(fips)

    base = (f"HRSA mental health HPSA file as retrieved {year}; status 'Designated' only "
            f"('Proposed For Withdrawal' and 'Withdrawn' are excluded)")
    n = 0
    universe = _county_fips(conn) | set(hpsas)
    for fips in sorted(universe):
        ids = hpsas.get(fips, {})
        if ids:
            n += _metric(conn, fips, "mh_hpsa_count", len(ids), "HPSAs", year, sid, 0,
                         f"{base}; distinct HPSA IDs of any designation type touching the county")
        else:
            n += _metric(conn, fips, "mh_hpsa_count", 0, "HPSAs", year, sid, 0,
                         f"{base}; 0 = no designated mental health HPSA lists this county in "
                         f"the published file")
        scores = [s for s in ids.values() if s is not None]
        if scores:
            n += _metric(conn, fips, "mh_hpsa_max_score", max(scores), "score 0-25", year, sid, 0,
                         f"{base}; highest HPSA score among them, scale 0-25, higher means a "
                         f"worse shortage")
        n += _metric(conn, fips, "mh_hpsa_geographic", 1 if fips in geo else 0, "flag", year, sid,
                     0, f"{base}; 1 = a designated Geographic or High Needs Geographic HPSA "
                        f"covers this whole county (Single County component), else 0")
    return n, (f"{len(hpsas)} counties with a designated MH HPSA, {len(geo)} whole-county "
               f"geographic; {proposed} 'Proposed For Withdrawal' rows ignored; year={year}")


# =========================================================================== step 6: awards
def _usas(payload) -> list[dict]:
    data = json.dumps(payload, sort_keys=True)
    body = fetch(USAS_GEO_URL, method="POST", data=data, domain=DOMAIN,
                 headers={"Content-Type": "application/json"}, timeout=300)
    d = json.loads(body)
    pm = d.get("page_metadata") or {}
    if pm.get("hasNext") or pm.get("has_next_page"):
        raise RuntimeError("spending_by_geography paginated unexpectedly; handle pages")
    return d.get("results") or []


def _geo_payload(programs, start, end):
    return {"scope": "place_of_performance", "geo_layer": "county",
            "filters": {"program_numbers": list(programs),
                        "time_period": [{"start_date": start, "end_date": end}]}}


def step_federal_awards(conn, cfg=None) -> tuple[int, str]:
    sid = _sid(conn, USAS_GEO_URL, title="USAspending.gov spending_by_geography, county, place "
               "of performance, homelessness assistance listings", publisher=USAS_PUB,
               license=USAS_LIC)
    first, last = min(FISCAL_YEARS), max(FISCAL_YEARS)
    programs, dropped = [], []
    for p in HOMELESS_PROGRAMS:  # an assistance listing that returns nothing is dropped
        res = _usas(_geo_payload([p], f"{first - 1}-10-01", f"{last}-09-30"))
        (programs if any(r.get("aggregated_amount") for r in res) else dropped).append(p)
    _PROGRAMS_OK[:] = programs
    if not programs:
        raise RuntimeError("no assistance listing returned spending")
    pops = {r[0]: r[1] for r in conn.execute(
        """SELECT entity_id, value FROM metric WHERE entity_type='county'
           AND metric='acs_population' AND year=?""", (ACS_YEAR,))}
    plist = ", ".join(programs)
    n, per_year = 0, {}
    for fy in FISCAL_YEARS:
        start, end = f"{fy - 1}-10-01", f"{fy}-09-30"
        note = (f"USAspending.gov obligations with action dates in FY{fy} ({start} to {end}), "
                f"place of performance in this county, assistance listings {plist}")
        k = 0
        for r in _usas(_geo_payload(programs, start, end)):
            fips = (r.get("shape_code") or "").strip()
            amt = r.get("aggregated_amount")
            if len(fips) != 5 or not fips.isdigit() or amt is None:
                continue
            k += _metric(conn, fips, "federal_homeless_awards", round(amt, 2), "usd", fy, sid, 0,
                         note)
            pop = pops.get(fips)
            if pop:
                n += _metric(conn, fips, "federal_homeless_awards_per_capita",
                             round(amt / pop, 4), "usd per person", fy, sid, 1,
                             f"derived: FY{fy} federal_homeless_awards / {_acs_label()} "
                             f"population (B01003)")
        per_year[fy] = k
        n += k
    return n, (f"programs {plist}" + (f"; dropped (no county-level spending) {', '.join(dropped)}" if dropped
                                      else "") + "; counties per FY " + json.dumps(per_year))


# =========================================================================== step 7: recipients
def _programs_with_spending() -> list[str]:
    """The step 6 listing set (re-derived from cache when this step runs alone)."""
    if _PROGRAMS_OK:
        return list(_PROGRAMS_OK)
    first, last = min(FISCAL_YEARS), max(FISCAL_YEARS)
    out = [p for p in HOMELESS_PROGRAMS
           if any(r.get("aggregated_amount") for r in
                  _usas(_geo_payload([p], f"{first - 1}-10-01", f"{last}-09-30")))]
    _PROGRAMS_OK[:] = out
    return out


def _org_matcher(conn):
    by_uei, by_name = {}, {}
    for oid, uei, nn, st in conn.execute("SELECT id, uei, name_norm, state FROM org"):
        if uei:
            by_uei.setdefault(uei.strip().upper(), oid)
        if nn and st:
            by_name.setdefault((nn, st), oid)

    def match(uei, name, state):
        if uei and uei.strip().upper() in by_uei:
            return by_uei[uei.strip().upper()]
        nn = name_norm(name)
        return by_name.get((nn, state)) if nn else None
    return match


def step_recipients(conn, cfg=None) -> tuple[int, str]:
    """Top recipients of the step 6 assistance listings, by county of place of performance."""
    conn.execute("""CREATE TABLE IF NOT EXISTS county_recipient(
                      county_fips TEXT, period TEXT, rank INTEGER, recipient_name TEXT,
                      recipient_uei TEXT, recipient_id TEXT, amount REAL, org_id INTEGER,
                      source_id INTEGER, PRIMARY KEY(county_fips, period, rank))""")
    first, last = min(FISCAL_YEARS), max(FISCAL_YEARS)
    period = f"FY{first}-FY{last}"
    start, end = f"{first - 1}-10-01", f"{last}-09-30"
    programs = _programs_with_spending()
    st_abbr = {r[0]: r[1] for r in conn.execute(
        "SELECT fips, abbr FROM geo_state WHERE abbr IS NOT NULL")}
    want = {s.strip().upper() for s in ((cfg or {}).get("states") or []) if s.strip()}
    counties = [r[0] for r in conn.execute(
        """SELECT entity_id FROM metric WHERE entity_type='county'
           AND metric='federal_homeless_awards' AND year BETWEEN ? AND ?
           GROUP BY entity_id HAVING SUM(value) > 0 ORDER BY entity_id""", (first, last))]
    if not counties:
        raise RuntimeError("no federal_homeless_awards rows; run the federal_awards step first")
    sid = _sid(conn, USAS_RECIPIENT_URL, title="USAspending.gov spending_by_category/recipient, "
               "place of performance county, homelessness assistance listings",
               publisher=USAS_PUB, license=USAS_LIC)
    match = _org_matcher(conn)
    n = done = matched = 0
    skipped_state = []
    for fips in counties:
        st = st_abbr.get(fips[:2])
        if st is None:
            skipped_state.append(fips)
            continue
        if want and st not in want:
            continue
        payload = {"filters": {"program_numbers": programs,
                               "place_of_performance_locations": [
                                   {"country": "USA", "state": st, "county": fips[2:]}],
                               "time_period": [{"start_date": start, "end_date": end}]},
                   "limit": RECIPIENT_LIMIT, "page": 1}
        body = fetch(USAS_RECIPIENT_URL, method="POST", data=json.dumps(payload, sort_keys=True),
                     domain=DOMAIN, headers={"Content-Type": "application/json"}, timeout=300)
        res = json.loads(body).get("results") or []
        conn.execute("DELETE FROM county_recipient WHERE county_fips=? AND period=?",
                     (fips, period))
        for rank, r in enumerate(res, 1):
            oid = match(r.get("uei"), r.get("name"), st)
            matched += 1 if oid else 0
            conn.execute("""INSERT INTO county_recipient(county_fips, period, rank, recipient_name,
                              recipient_uei, recipient_id, amount, org_id, source_id)
                            VALUES(?,?,?,?,?,?,?,?,?)""",
                         (fips, period, rank, clean(r.get("name")), clean(r.get("uei")),
                          clean(r.get("recipient_id")), r.get("amount"), oid, sid))
            n += 1
        done += 1
        if done % 50 == 0:
            conn.commit()
    return n, (f"{done} counties queried ({'states ' + ','.join(sorted(want)) if want else 'all'}), "
               f"top {RECIPIENT_LIMIT} recipients over {period}; {matched} of {n} matched to org"
               + (f"; no state code for {len(skipped_state)}" if skipped_state else ""))


# =========================================================================== run
STEPS = [
    ("acs", step_acs),
    ("crosswalk", step_crosswalk),
    ("pit_by_county", step_pit_by_county),
    ("places", step_places),
    ("hpsa", step_hpsa),
    ("federal_awards", step_federal_awards),
    ("recipients", step_recipients),
]


def run(conn, cfg):
    want = (cfg or {}).get("steps")
    total, detail, errs = 0, {}, []
    for name, fn in STEPS:
        if want and name not in want:
            continue
        t0 = time.time()
        try:
            n, note = fn(conn, cfg or {})
            conn.commit()
            total += n
            detail[name] = {"rows": n, "note": note, "secs": round(time.time() - t0, 1)}
        except Exception as e:  # one dead source must not stop the others
            conn.rollback()
            errs.append(f"{name}: {e!r}")
            detail[name] = {"rows": 0, "note": f"FAILED {e!r}", "secs": round(time.time() - t0, 1)}
            print(f"[{DOMAIN}] step {name} FAILED: {e!r}", file=sys.stderr)
            traceback.print_exc(limit=3)
    cov = {r[0]: r[1] for r in conn.execute(
        """SELECT metric, COUNT(DISTINCT entity_id) FROM metric WHERE entity_type='county'
           GROUP BY metric ORDER BY metric""")}
    notes = ("; ".join(f"{k}={v['rows']}" for k, v in detail.items())
             + (" | ERRORS: " + " || ".join(errs) if errs else ""))
    sources = [(acs_url(t), CENSUS_LIC) for t in _acs_tables_needed()] + [
        (GAZ_TRACTS_URL, CENSUS_LIC), (f"{PLACES_HOST}/{PLACES_ID}.json", CDC_LIC),
        (HPSA_URL, CENSUS_LIC), (USAS_GEO_URL, USAS_LIC), (USAS_RECIPIENT_URL, USAS_LIC)]
    return {"rows": total, "notes": notes[:3000], "detail": detail, "coverage": cov,
            "errors": errs, "sources": sources}


def _main(argv):
    steps = None
    if "--step" in argv:
        i = argv.index("--step")
        if i + 1 >= len(argv) or argv[i + 1] not in dict(STEPS):
            sys.exit(f"--step needs one of: {', '.join(n for n, _ in STEPS)}")
        steps = [argv[i + 1]]
    cfg = {"steps": steps} if steps else {}
    if "--states" in argv:
        i = argv.index("--states")
        if i + 1 >= len(argv):
            sys.exit("--states needs a list such as PA,NY")
        cfg["states"] = argv[i + 1].split(",")
    conn = db()
    t0 = time.time()
    res = run(conn, cfg)
    conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,?)",
                 (DOMAIN, now(), res["rows"], res["notes"][:2000], 0 if res["errors"] else 1))
    conn.commit()
    for name, d in res["detail"].items():
        print(f"[{DOMAIN}] {name}: rows={d['rows']} ({d['secs']}s) {d['note']}")
    print(f"[{DOMAIN}] {'OK' if not res['errors'] else 'PARTIAL'} rows={res['rows']} "
          f"{time.time() - t0:.1f}s")
    print(f"[{DOMAIN}] county coverage by metric: {json.dumps(res['coverage'])}")
    if "--selftest" in argv:
        n = conn.execute("SELECT COUNT(*) FROM metric WHERE entity_type='county'").fetchone()[0]
        print(f"[{DOMAIN}] rows in metric (entity_type='county'): {n}")
        for r in conn.execute("""SELECT * FROM metric WHERE entity_type='county'
                                 ORDER BY RANDOM() LIMIT 3"""):
            print(dict(r))
        common.selftest_report(conn, DOMAIN, "county_coc")
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
