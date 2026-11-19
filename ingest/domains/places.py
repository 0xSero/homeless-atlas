"""places — the full Census universe of US places, counties and states with population and coordinates.

Sources (all public domain, keyless):
  * Census Bureau Population Estimates vintage 2024, sub-county totals (sub-est2024.csv)
  * Census Gazetteer 2023 place + county national files (internal point lat/lon, land area)

Writes: geo_state, geo_city (kind=place|cdp|county, plus state rows are NOT written here).
"""
from __future__ import annotations
import csv, io, zipfile
from pathlib import Path

from ingest.common import (clean, db, fetch, name_norm, now, save_raw, source_id_for,
                           to_int, upsert)

DOMAIN = "places"
POPEST = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv"
GAZ_PLACE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_place_national.zip"
GAZ_COUNTY = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_counties_national.zip"
GAZ_COUSUB = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_cousubs_national.zip"

STATE_FIPS = {
    "01": ("AL", "Alabama"), "02": ("AK", "Alaska"), "04": ("AZ", "Arizona"), "05": ("AR", "Arkansas"),
    "06": ("CA", "California"), "08": ("CO", "Colorado"), "09": ("CT", "Connecticut"), "10": ("DE", "Delaware"),
    "11": ("DC", "District of Columbia"), "12": ("FL", "Florida"), "13": ("GA", "Georgia"), "15": ("HI", "Hawaii"),
    "16": ("ID", "Idaho"), "17": ("IL", "Illinois"), "18": ("IN", "Indiana"), "19": ("IA", "Iowa"),
    "20": ("KS", "Kansas"), "21": ("KY", "Kentucky"), "22": ("LA", "Louisiana"), "23": ("ME", "Maine"),
    "24": ("MD", "Maryland"), "25": ("MA", "Massachusetts"), "26": ("MI", "Michigan"), "27": ("MN", "Minnesota"),
    "28": ("MS", "Mississippi"), "29": ("MO", "Missouri"), "30": ("MT", "Montana"), "31": ("NE", "Nebraska"),
    "32": ("NV", "Nevada"), "33": ("NH", "New Hampshire"), "34": ("NJ", "New Jersey"), "35": ("NM", "New Mexico"),
    "36": ("NY", "New York"), "37": ("NC", "North Carolina"), "38": ("ND", "North Dakota"), "39": ("OH", "Ohio"),
    "40": ("OK", "Oklahoma"), "41": ("OR", "Oregon"), "42": ("PA", "Pennsylvania"), "44": ("RI", "Rhode Island"),
    "45": ("SC", "South Carolina"), "46": ("SD", "South Dakota"), "47": ("TN", "Tennessee"), "48": ("TX", "Texas"),
    "49": ("UT", "Utah"), "50": ("VT", "Vermont"), "51": ("VA", "Virginia"), "53": ("WA", "Washington"),
    "54": ("WV", "West Virginia"), "55": ("WI", "Wisconsin"), "56": ("WY", "Wyoming"), "72": ("PR", "Puerto Rico"),
}


def _gazetteer(url: str, prefix: str) -> dict:
    """{geoid: (lat, lon, aland)} from a Census gazetteer zip."""
    zbytes = fetch(url, domain=DOMAIN)
    out = {}
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
        text = z.read(name).decode("latin-1")
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        gid = row.get("GEOID") or ""
        if not gid:
            continue
        try:
            lat = float(row.get("INTPTLAT") or "nan")
            lon = float(row.get("INTPTLONG") or "nan")
        except ValueError:
            continue
        out[gid] = (lat, lon, to_int(row.get("ALAND")))
    return out


def run(conn, cfg):
    pop_bytes = fetch(POPEST, domain=DOMAIN)
    src_pop = source_id_for(conn, POPEST, DOMAIN, title="Population Estimates vintage 2024, sub-county",
                            publisher="US Census Bureau", license="public domain")
    src_gp = source_id_for(conn, GAZ_PLACE, DOMAIN, title="2023 Gazetteer place national",
                           publisher="US Census Bureau", license="public domain")
    src_gc = source_id_for(conn, GAZ_COUNTY, DOMAIN, title="2023 Gazetteer county national",
                           publisher="US Census Bureau", license="public domain")

    places = _gazetteer(GAZ_PLACE, "place")
    counties = _gazetteer(GAZ_COUNTY, "county")
    try:
        cousubs = _gazetteer(GAZ_COUSUB, "cousub")
    except Exception:
        cousubs = {}

    rows_place = rows_county = rows_state = 0
    state_area = {}
    state_pop = {}

    text = pop_bytes.decode("latin-1")
    for row in csv.DictReader(io.StringIO(text)):
        lev = (row.get("SUMLEV") or "").strip()
        st = (row.get("STATE") or "").strip()
        stname = clean(row.get("STNAME")) or ""
        if lev == "040":
            abbr, name = STATE_FIPS.get(st, (None, stname))
            if not abbr:
                continue
            pop = to_int(row.get("POPESTIMATE2024"))
            state_pop[st] = pop
            upsert(conn, "geo_state", {"fips": st, "abbr": abbr, "name": name, "pop": pop, "source_id": src_pop},
                   keys=["fips"])
            rows_state += 1
            continue
        if lev not in ("162", "061", "050"):
            continue
        name = clean(row.get("NAME")) or ""
        if not name:
            continue
        pop = to_int(row.get("POPESTIMATE2024"))
        if lev == "050":
            geoid = f"{st}{(row.get('COUNTY') or '').strip()}"
            lat, lon, aland = counties.get(geoid, (None, None, None))
            kind = "county"
        elif lev == "061":
            # minor civil division: the New England / Midwest town, a real populated place
            geoid = f"{st}{(row.get('COUNTY') or '').strip()}{(row.get('COUSUB') or '').strip()}"
            lat, lon, aland = cousubs.get(geoid, (None, None, None))
            kind = "town"
        else:
            # 162 = whole incorporated place (157 is a place-part and would double count)
            place = (row.get("PLACE") or "").strip()
            if place == "00000":
                continue
            geoid = f"{st}{place}"
            lat, lon, aland = places.get(geoid, (None, None, None))
            kind = "place"
        abbr = STATE_FIPS.get(st, (None, None))[0]
        if kind != "county" and aland:
            a = state_area.setdefault(st, [0, 0.0, 0.0])
            a[0] += aland
            if lat is not None:
                a[1] += lat * aland
                a[2] += lon * aland
        upsert(conn, "geo_city",
               {"geoid": geoid, "kind": kind, "name": name, "state": abbr, "pop": pop,
                "lat": round(lat, 5) if lat is not None else None,
                "lon": round(lon, 5) if lon is not None else None, "source_id": src_pop},
               keys=["geoid"])
        if kind == "county":
            rows_county += 1
        else:
            rows_place += 1

    # state centroid = population-weighted mean of its counties' internal points (derived; used only as a fallback marker)
    for r in conn.execute("""SELECT state, SUM(lat*pop)*1.0/SUM(pop) la, SUM(lon*pop)*1.0/SUM(pop) lo
                             FROM geo_city WHERE kind='county' AND lat IS NOT NULL AND lon IS NOT NULL AND pop>0
                             GROUP BY state"""):
        conn.execute("UPDATE geo_state SET lat=?, lon=? WHERE abbr=? AND (lat IS NULL OR lon IS NULL)",
                     (round(r[1], 4), round(r[2], 4), r[0]))

    conn.execute("UPDATE geo_state SET source_id=? WHERE source_id IS NULL", (src_pop,))
    notes = (f"states={rows_state} places={rows_place} counties={rows_county} "
             f"gaz_places={len(places)} gaz_counties={len(counties)}")
    return {"rows": rows_state + rows_place + rows_county, "notes": notes,
            "sources": [(POPEST, "public domain"), (GAZ_PLACE, "public domain"), (GAZ_COUNTY, "public domain")]}


def selftest():
    conn = db()
    res = run(conn, {})
    conn.commit()
    print(res)
    for r in conn.execute("SELECT geoid,name,state,kind,pop,lat,lon FROM geo_city WHERE pop>20000 ORDER BY RANDOM() LIMIT 5"):
        print(dict(r))
    for r in conn.execute("SELECT abbr,name,pop,lat,lon FROM geo_state ORDER BY name LIMIT 5"):
        print(dict(r))


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    else:
        conn = db()
        print(run(conn, {}))
        conn.commit()
