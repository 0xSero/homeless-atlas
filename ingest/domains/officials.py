"""officials: per-county public offices and elected officials over homelessness policy and money.

For every county (geo_city kind='county', 5-digit FIPS) this lists the people and offices a
resident would contact about homelessness policy and funding, with their OFFICIAL published
contact details only.

Sources (all keyless; every response cached by ingest.common.fetch under data/raw/officials/)
------------------------------------------------------------------------------------------
1. U.S. Congress: unitedstates/congress-legislators `legislators-current.json` (CC0).
2. State legislators: Open States bulk people CSV per state
   `https://data.openstates.org/people/current/<st>.csv` (CC0, openstates/people).
3. District to county: Census 2020 relationship files (rel2020/cd-sld), 119th Congress and 2024
   state legislative upper/lower districts to 2020 counties. Share = AREALAND_PART /
   AREALAND_COUNTY_20 (the district's share of the county's land area).
   Connecticut: geo_city uses the 2022 planning regions (09110..09190) but the 2020 county
   files use the old CT counties, so CT is built from the town-level (cousub) relationship files
   and each town is assigned to its planning region with the Census 2023 Gazetteer (cousub
   FIPS codes did not change in 2022, only the county part of the GEOID did).
4. HUD Public Housing Authorities ArcGIS layer (office contact, executive director phone and
   email). County = STATE2KX + CURCNTY when that is a known county; CT PHAs go through their
   town (CURCOSUB) to the planning region; otherwise nearest county internal point in the same
   state (method='nearest_county_point').

Privacy: public officials in their official capacity only. Open States birth_date, gender,
biography, image and every social-media column are dropped on read and never stored. No home
addresses or personal numbers are stored; the sources publish office/capitol/district-office
contacts only.

Tables
------
officials_person  one row per elected official (federal_house, federal_senate, state_upper,
                  state_lower). phone/address = capitol or D.C. office; district_phone /
                  district_address = district office (Open States) where published.
officials_office  one row per office (kind='pha'); exec_* = HUD-published executive director
                  phone/email (the layer carries no executive director name).
officials_county  county -> person/office, with share of county land area for districts
                  (1.0 for senators, NULL for PHAs).

Idempotent: every run deletes and rewrites the rows of the states it processes.
Usage: python3 -m ingest.domains.officials [--states PA,NY] [--refresh] [--selftest]
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sys
import time
import urllib.parse

from ingest import common
from ingest.common import clean, fetch, now, source_id_for, upsert

DOMAIN = "officials"

CONGRESS_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
CONGRESS_LIC = ("CC0 1.0 public domain dedication (unitedstates/congress-legislators); "
                "facts from house.gov / senate.gov")
OS_URL = "https://data.openstates.org/people/current/{st}.csv"
OS_LIC = "CC0 1.0 public domain dedication (Open States people data, github.com/openstates/people)"
REL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/cd-sld/"
REL_COUNTY = {
    "cd": REL + "tab20_cd11920_county20_natl.txt",
    "upper": REL + "tab20_sldu202420_county20_natl.txt",
    "lower": REL + "tab20_sldl202420_county20_natl.txt",
}
REL_CT_COUSUB = {
    "cd": REL + "tab20_cd11920_cousub20_st09.txt",
    "upper": REL + "tab20_sldu202420_cousub20_st09.txt",
    "lower": REL + "tab20_sldl202420_cousub20_st09.txt",
}
GAZ_CT = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/"
          "2023_gaz_cousubs_09.txt")
CENSUS_PUB = "U.S. Census Bureau"
CENSUS_LIC = "Public domain (US Government work)"
PHA_LAYER = ("https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
             "Public_Housing_Authorities/FeatureServer/0")
PHA_LIC = "Public domain (US Government work), HUD Public Housing Authorities open data layer"
PHA_FIELDS = ("OBJECTID,PARTICIPANT_CODE,FORMAL_PARTICIPANT_NAME,HA_PHN_NUM,HA_EMAIL_ADDR_TEXT,"
              "EXEC_DIR_PHONE,EXEC_DIR_EMAIL,STD_ADDR,STD_CITY,STD_ST,STD_ZIP5,STATE2KX,CURCNTY,"
              "CURCOSUB,LAT,LON")
PHA_PAGE = 1000

PERSON_LEVELS = ("federal_house", "federal_senate", "state_upper", "state_lower")
# Open States columns we keep. Everything else (birth_date, gender, biography, image, twitter,
# youtube, instagram, facebook, ...) is dropped on read.
OS_KEEP = ("id", "name", "current_party", "current_chamber", "current_district", "email",
           "capitol_address", "capitol_voice", "district_address", "district_voice", "links")
LOWER_TITLE = {"ca": "Assembly Member", "nv": "Assembly Member", "ny": "Assembly Member",
               "wi": "Assembly Member", "nj": "Assembly Member", "md": "Delegate",
               "va": "Delegate", "wv": "Delegate"}
ORDINALS = {"first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th",
            "sixth": "6th", "seventh": "7th", "eighth": "8th", "ninth": "9th", "tenth": "10th",
            "eleventh": "11th", "twelfth": "12th"}
STOP = {"state", "senate", "house", "district", "districts", "legislative", "subdistrict",
        "senatorial", "assembly", "and", "of", "the", "ward"}


# --------------------------------------------------------------------------- helpers
def _ensure(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS officials_person (
      id TEXT PRIMARY KEY, level TEXT, name TEXT, party TEXT, title TEXT, body TEXT,
      district TEXT, state TEXT, phone TEXT, email TEXT, address TEXT, url TEXT,
      contact_form TEXT, district_phone TEXT, district_address TEXT, source_id INTEGER,
      updated_at TEXT);
    CREATE TABLE IF NOT EXISTS officials_office (
      id TEXT PRIMARY KEY, kind TEXT, name TEXT, phone TEXT, email TEXT, address TEXT, url TEXT,
      exec_name TEXT, exec_phone TEXT, exec_email TEXT, state TEXT, county_fips TEXT,
      method TEXT, source_id INTEGER);
    CREATE TABLE IF NOT EXISTS officials_county (
      county_fips TEXT, person_or_office_id TEXT, level TEXT, share REAL,
      PRIMARY KEY(county_fips, person_or_office_id));
    CREATE INDEX IF NOT EXISTS officials_county_id ON officials_county(person_or_office_id);
    CREATE INDEX IF NOT EXISTS officials_person_state ON officials_person(state, level);
    CREATE INDEX IF NOT EXISTS officials_office_state ON officials_office(state, kind);
    """)


def _get(url, force=False):
    return fetch(url, domain=DOMAIN, force=force)


def _sid(conn, url, title, publisher, license):
    """Register a source row and fill bytes/sha256 from the fetch cache file."""
    sid = source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)
    key = hashlib.sha1(("GET" + url + json.dumps({}, sort_keys=True)).encode()).hexdigest()
    p = common.RAW / DOMAIN / f"{key}.bin"
    if p.exists():
        b = p.read_bytes()
        conn.execute("UPDATE source SET bytes=?, sha256=? WHERE id=?",
                     (len(b), hashlib.sha256(b).hexdigest(), sid))
    return sid


def _phone(s):
    s = clean(s)
    if not s:
        return None
    d = re.sub(r"\D", "", str(s))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10 and d != "0000000000":
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return s


def _email(s):
    s = clean(s)
    if not isinstance(s, str) or "@" not in s:
        return None
    return s.lower() if s.upper() == s else s


def _pipe_rows(blob):
    text = blob.decode("utf-8-sig")
    rdr = csv.DictReader(io.StringIO(text), delimiter="|")
    for r in rdr:
        yield {k.strip(): (v or "").strip() for k, v in r.items() if k}


def _dkey(s):
    """Normalise a district label so Census names and Open States labels compare equal."""
    s = (s or "").lower().replace("&", " and ")
    toks = []
    for t in re.split(r"[^a-z0-9]+", s):
        if not t or t in STOP:
            continue
        t = ORDINALS.get(t, t)
        t = re.sub(r"^0+(?=[0-9a-z])", "", t)
        toks.append(t)
    # order kept: "Middlesex and Worcester" and "Worcester and Middlesex" are different MA
    # districts; joined without spaces so "Chittenden Southeast" == "Chittenden South East"
    return "".join(toks)


def _first_official_link(links):
    parts = [p.strip() for p in (links or "").split(";") if p.strip()]
    for p in parts:
        host = urllib.parse.urlparse(p).netloc.lower()
        if host.endswith(".gov") or host.endswith(".us") or ".state." in host or "legis" in host:
            return p
    return parts[0] if parts else None


def _haversine(a, b, c, d):
    r = math.pi / 180
    x = (math.sin((c - a) * r / 2) ** 2 + math.cos(a * r) * math.cos(c * r)
         * math.sin((d - b) * r / 2) ** 2)
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(x)))


# --------------------------------------------------------------------------- geography
def _counties(conn):
    out = {}
    for r in conn.execute("SELECT geoid, name, state, lat, lon FROM geo_city WHERE kind='county'"):
        out[r[0]] = {"name": r[1], "state": r[2], "lat": r[3], "lon": r[4]}
    return out


def _state_maps(conn, counties):
    fips2ab, ab2fips = {}, {}
    used = {c[:2] for c in counties}
    for r in conn.execute("SELECT fips, abbr FROM geo_state"):
        if r[0] in used:
            fips2ab[r[0]] = r[1]
            ab2fips[r[1]] = r[0]
    return fips2ab, ab2fips


def _ct_towns(force):
    """Town (cousub FIPS, 5 digits) -> 2022 planning region county FIPS."""
    town = {}
    text = _get(GAZ_CT, force).decode("utf-8-sig")
    rdr = csv.DictReader(io.StringIO(text), delimiter="\t")
    for r in rdr:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        g = r.get("GEOID", "")
        if len(g) == 10:
            town[g[5:]] = g[:5]
    return town


def _district_shares(conn, kind, states, counties, ct_town, force):
    """Return {district_geoid: {"name", "counties": {fips: share}}} for one district kind."""
    tag = {"cd": "CD119", "upper": "SLDU2024", "lower": "SLDL2024"}[kind]
    gcol, ncol = f"GEOID_{tag}_20", f"NAMELSAD_{tag}_20"
    out = {}
    for r in _pipe_rows(_get(REL_COUNTY[kind], force)):
        dg, cf = r[gcol], r["GEOID_COUNTY_20"]
        if dg[:2] not in states or dg[:2] == "09" or "ZZ" in dg or cf not in counties:
            continue
        land, part = float(r["AREALAND_COUNTY_20"] or 0), float(r["AREALAND_PART"] or 0)
        if land <= 0 or part <= 0:
            continue
        d = out.setdefault(dg, {"name": r[ncol], "counties": {}})
        d["counties"][cf] = round(part / land, 4)
    if "09" in states and ct_town:
        region_land, parts, names, seen = {}, {}, {}, set()
        for r in _pipe_rows(_get(REL_CT_COUSUB[kind], force)):
            dg, cs = r[gcol], r["GEOID_COUSUB_20"]
            region = ct_town.get(cs[5:])
            if "ZZ" in dg or not region or region not in counties:
                continue
            if cs not in seen:
                seen.add(cs)
                region_land[region] = region_land.get(region, 0) + float(r["AREALAND_COUSUB_20"] or 0)
            part = float(r["AREALAND_PART"] or 0)
            if part > 0:
                names[dg] = r[ncol]
                parts[(dg, region)] = parts.get((dg, region), 0) + part
        for (dg, region), part in parts.items():
            if region_land.get(region, 0) > 0:
                d = out.setdefault(dg, {"name": names[dg], "counties": {}})
                d["counties"][region] = round(part / region_land[region], 4)
    return out


# --------------------------------------------------------------------------- writers
def _clear(conn, levels, ab_list, fips_list, office=False):
    q = ",".join("?" * len(levels))
    qa = ",".join("?" * len(ab_list))
    qf = ",".join("?" * len(fips_list))
    if office:
        conn.execute(f"DELETE FROM officials_office WHERE kind IN ({q}) AND state IN ({qa})",
                     (*levels, *ab_list))
    else:
        conn.execute(f"DELETE FROM officials_person WHERE level IN ({q}) AND state IN ({qa})",
                     (*levels, *ab_list))
    conn.execute(f"DELETE FROM officials_county WHERE level IN ({q}) "
                 f"AND substr(county_fips,1,2) IN ({qf})", (*levels, *fips_list))


def _link(conn, fips, pid, level, share):
    upsert(conn, "officials_county", {"county_fips": fips, "person_or_office_id": pid,
                                      "level": level, "share": share},
           ["county_fips", "person_or_office_id"])


def _federal(conn, states, fips2ab, ab2fips, counties, cds, force, stats):
    data = json.loads(_get(CONGRESS_URL, force))
    sid = _sid(conn, CONGRESS_URL, "Current members of the U.S. Congress (congress-legislators)",
               "unitedstates/congress-legislators project", CONGRESS_LIC)
    abs_ = [fips2ab[s] for s in states]
    _clear(conn, ["federal_house", "federal_senate"], abs_, list(states))
    by_state_county = {}
    for c in counties:
        by_state_county.setdefault(c[:2], []).append(c)
    cd_index = {}
    for dg, d in cds.items():
        code = dg[2:]
        n = 0 if code in ("00", "98") else int(code)
        cd_index[(dg[:2], n)] = d
    matched_cd = set()
    for p in data:
        t = p["terms"][-1]
        st = t["state"]
        if st not in ab2fips or ab2fips[st] not in states:
            continue
        sf = ab2fips[st]
        nm = p["name"]
        name = nm.get("official_full") or " ".join(x for x in (nm.get("first"), nm.get("last")) if x)
        pid = "bioguide:" + p["id"]["bioguide"]
        if t["type"] == "sen":
            level, title, body = "federal_senate", "U.S. Senator", "U.S. Senate"
            district = f"class {t.get('class')}" if t.get("class") else None
        else:
            level, body = "federal_house", "U.S. House of Representatives"
            title = "Delegate" if st == "DC" else "U.S. Representative"
            district = "At-Large" if t.get("district") == 0 else str(t.get("district"))
        upsert(conn, "officials_person", {
            "id": pid, "level": level, "name": clean(name), "party": clean(t.get("party")),
            "title": title, "body": body, "district": district, "state": st,
            "phone": _phone(t.get("phone")), "email": None, "address": clean(t.get("address")),
            "url": clean(t.get("url")), "contact_form": clean(t.get("contact_form")),
            "district_phone": None, "district_address": None, "source_id": sid,
            "updated_at": now()}, ["id"])
        stats[level] = stats.get(level, 0) + 1
        if level == "federal_senate":
            for c in by_state_county.get(sf, []):
                _link(conn, c, pid, level, 1.0)
        else:
            d = cd_index.get((sf, int(t.get("district") or 0)))
            if not d:
                stats.setdefault("unmatched_house", []).append(f"{st}-{t.get('district')}")
                continue
            matched_cd.add((sf, int(t.get("district") or 0)))
            for c, sh in d["counties"].items():
                _link(conn, c, pid, level, sh)
    vac = sorted(f"{fips2ab[k[0]]}-{k[1]}" for k in cd_index if k not in matched_cd)
    if vac:
        stats["house_districts_without_member"] = vac


def _state_leg(conn, states, fips2ab, counties, sld, force, stats):
    abs_ = [fips2ab[s] for s in states]
    _clear(conn, ["state_upper", "state_lower"], abs_, list(states))
    # index Census districts per (state, chamber) by name key and by code key
    idx = {}
    for chamber, dd in sld.items():
        for dg, d in dd.items():
            k = (dg[:2], chamber)
            m = idx.setdefault(k, {})
            m.setdefault(("n", _dkey(d["name"])), dg)
            m.setdefault(("c", _dkey(dg[2:])), dg)
    used = set()
    for sf in sorted(states):
        st = fips2ab[sf]
        url = OS_URL.format(st=st.lower())
        try:
            blob = _get(url, force)
        except Exception as e:  # noqa: BLE001
            stats.setdefault("openstates_failed", []).append(f"{st}: {str(e)[:120]}")
            continue
        sid = _sid(conn, url, f"Open States current legislators, {st}", "Open States (Plural Policy)",
                   OS_LIC)
        rows = csv.DictReader(io.StringIO(blob.decode("utf-8-sig")))
        state_counties = [c for c in counties if c[:2] == sf]
        for raw in rows:
            r = {k: (raw.get(k) or "").strip() for k in OS_KEEP}   # drops birth_date, gender, ...
            chamber = r["current_chamber"]
            if chamber not in ("upper", "lower", "legislature"):
                continue
            level = "state_lower" if chamber == "lower" else "state_upper"
            if st == "DC":
                title, body = "Councilmember", "Council of the District of Columbia"
            elif chamber == "legislature":
                title, body = "State Senator", f"{st} Legislature (unicameral)"
            elif chamber == "upper":
                title, body = "State Senator", f"{st} Legislature, upper chamber"
            else:
                title = LOWER_TITLE.get(st.lower(), "State Representative")
                body = f"{st} Legislature, lower chamber"
            pid = r["id"]
            cap_addr = clean(r["capitol_address"])
            cap_ph = _phone(r["capitol_voice"])
            upsert(conn, "officials_person", {
                "id": pid, "level": level, "name": clean(r["name"]),
                "party": clean(r["current_party"]), "title": title, "body": body,
                "district": clean(r["current_district"]), "state": st,
                "phone": cap_ph, "email": _email(r["email"]), "address": cap_addr,
                "url": _first_official_link(r["links"]), "contact_form": None,
                "district_phone": _phone(r["district_voice"]),
                "district_address": clean(r["district_address"]),
                "source_id": sid, "updated_at": now()}, ["id"])
            stats[level] = stats.get(level, 0) + 1
            dist = r["current_district"]
            if dist.lower() in ("at-large", "at large", "chairman"):
                for c in state_counties:
                    _link(conn, c, pid, level, 1.0)
                continue
            ch = "lower" if chamber == "lower" else "upper"
            m = idx.get((sf, ch), {})
            dg = m.get(("n", _dkey(dist))) or m.get(("c", _dkey(dist)))
            seat = re.fullmatch(r"(\d+)[A-Za-z]", dist)
            if not dg and seat:
                # e.g. Idaho "16B": seat B of whole district 16 (Census has no A/B areas there)
                dg = m.get(("c", _dkey(seat.group(1))))
            if not dg and st == "NH" and ch == "lower":
                # NH floterial districts (e.g. "Rockingham 33") overlay base districts inside one
                # county and are not published as Census areas: link to the named county with an
                # unknown share rather than guess the land area.
                cname = re.sub(r"\s*\d+$", "", dist).strip().lower()
                hit = [c for c in state_counties
                       if counties[c]["name"].lower() == f"{cname} county"]
                if len(hit) == 1:
                    _link(conn, hit[0], pid, level, None)
                    stats["nh_floterial_linked"] = stats.get("nh_floterial_linked", 0) + 1
                    continue
            if not dg:
                stats.setdefault("unmatched_state_district", []).append(f"{st} {ch} {dist!r}")
                continue
            used.add((ch, dg))
            for c, sh in sld[ch][dg]["counties"].items():
                _link(conn, c, pid, level, sh)
    empty = sorted(f"{fips2ab[dg[:2]]} {ch} {dd[dg]['name']}" for ch, dd in sld.items() for dg in dd
                   if (ch, dg) not in used and dg[:2] in states and fips2ab[dg[:2]] != "NH")
    if empty:
        stats["state_districts_without_member"] = empty


def _pha(conn, states, fips2ab, ab2fips, counties, ct_town, force, stats):
    abs_ = [fips2ab[s] for s in states]
    _clear(conn, ["pha"], abs_, list(states), office=True)
    sid = _sid(conn, PHA_LAYER, "HUD Public Housing Authorities (ArcGIS FeatureServer layer 0)",
               "U.S. Department of Housing and Urban Development (HUD)", PHA_LIC)
    if set(states) >= set(fips2ab):
        where = "1=1"
    else:
        where = "STD_ST IN (" + ",".join(f"'{a}'" for a in sorted(abs_)) + ")"
    by_state = {}
    for c, v in counties.items():
        by_state.setdefault(v["state"], []).append((c, v["lat"], v["lon"]))
    feats, off = [], 0
    while True:
        q = urllib.parse.urlencode({"where": where, "outFields": PHA_FIELDS,
                                    "returnGeometry": "false", "orderByFields": "OBJECTID",
                                    "resultOffset": off, "resultRecordCount": PHA_PAGE, "f": "json"})
        d = json.loads(_get(PHA_LAYER + "/query?" + q, force))
        if "error" in d:
            raise RuntimeError(f"PHA layer error: {d['error']}")
        page = [f["attributes"] for f in d.get("features", [])]
        feats += page
        if not page or (not d.get("exceededTransferLimit") and len(page) < PHA_PAGE):
            break
        off += len(page)
    for a in feats:
        st = clean(a.get("STD_ST"))
        if st not in ab2fips or ab2fips[st] not in states:
            continue
        code = clean(a.get("PARTICIPANT_CODE"))
        if not code:
            continue
        fips, method = None, None
        sfx, cc = clean(a.get("STATE2KX")), clean(a.get("CURCNTY"))
        if sfx and cc and f"{sfx}{cc}" in counties:
            fips, method = f"{sfx}{cc}", "county_fips_field"
        elif st == "CT" and clean(a.get("CURCOSUB")) and ct_town.get(clean(a.get("CURCOSUB"))):
            fips, method = ct_town[clean(a.get("CURCOSUB"))], "cousub_to_planning_region"
        elif a.get("LAT") and a.get("LON") and by_state.get(st):
            la, lo = float(a["LAT"]), float(a["LON"])
            best = min((_haversine(la, lo, y, x), c) for c, y, x in by_state[st]
                       if y is not None and x is not None)
            fips, method = best[1], "nearest_county_point"
        stats["pha_method_" + str(method)] = stats.get("pha_method_" + str(method), 0) + 1
        street = clean(a.get("STD_ADDR"))
        city = clean(a.get("STD_CITY"))
        zp = clean(a.get("STD_ZIP5"))
        addr = ", ".join(str(x) for x in (street, city, " ".join(str(y) for y in (st, zp) if y)) if x) or None
        oid = "pha:" + str(code)
        upsert(conn, "officials_office", {
            "id": oid, "kind": "pha", "name": clean(a.get("FORMAL_PARTICIPANT_NAME")),
            "phone": _phone(a.get("HA_PHN_NUM")), "email": _email(a.get("HA_EMAIL_ADDR_TEXT")),
            "address": addr, "url": None, "exec_name": None,
            "exec_phone": _phone(a.get("EXEC_DIR_PHONE")),
            "exec_email": _email(a.get("EXEC_DIR_EMAIL")), "state": st, "county_fips": fips,
            "method": method, "source_id": sid}, ["id"])
        stats["pha"] = stats.get("pha", 0) + 1
        if fips:
            _link(conn, fips, oid, "pha", None)


# --------------------------------------------------------------------------- run
def run(conn, cfg):
    t0 = time.time()
    _ensure(conn)
    force = bool(cfg.get("refresh"))
    counties = _counties(conn)
    if not counties:
        raise RuntimeError("geo_city has no county rows; run the county/coc_geometry domains first")
    fips2ab, ab2fips = _state_maps(conn, counties)
    want = cfg.get("states")
    if want:
        bad = [s for s in want if s.upper() not in ab2fips]
        if bad:
            raise RuntimeError(f"unknown states: {bad}")
        states = {ab2fips[s.upper()] for s in want}
    else:
        states = set(fips2ab)
    rel_sids = [_sid(conn, u, f"Census 2020 relationship file {u.rsplit('/', 1)[1]}", CENSUS_PUB,
                     CENSUS_LIC) for u in REL_COUNTY.values() if _get(u, force)]
    ct_town = {}
    if "09" in states:
        ct_town = _ct_towns(force)
        _sid(conn, GAZ_CT, "Census 2023 Gazetteer, county subdivisions, Connecticut", CENSUS_PUB,
             CENSUS_LIC)
        for u in REL_CT_COUSUB.values():
            _get(u, force)
            _sid(conn, u, f"Census 2020 relationship file {u.rsplit('/', 1)[1]}", CENSUS_PUB,
                 CENSUS_LIC)
    cds = _district_shares(conn, "cd", states, counties, ct_town, force)
    sld = {ch: _district_shares(conn, ch, states, counties, ct_town, force)
           for ch in ("upper", "lower")}
    stats = {}
    _federal(conn, states, fips2ab, ab2fips, counties, cds, force, stats)
    conn.commit()
    _state_leg(conn, states, fips2ab, counties, sld, force, stats)
    conn.commit()
    try:
        _pha(conn, states, fips2ab, ab2fips, counties, ct_town, force, stats)
    except Exception as e:  # noqa: BLE001  a 403/outage on one source must not sink the rest
        stats["pha_failed"] = str(e)[:300]
    conn.commit()

    qf = ",".join("?" * len(states))
    rows = conn.execute(f"SELECT COUNT(*) FROM officials_county WHERE substr(county_fips,1,2) "
                        f"IN ({qf})", tuple(states)).fetchone()[0]
    nocov = conn.execute(
        f"SELECT COUNT(*) FROM geo_city g WHERE kind='county' AND substr(geoid,1,2) IN ({qf}) AND "
        f"NOT EXISTS (SELECT 1 FROM officials_county oc WHERE oc.county_fips=g.geoid AND "
        f"oc.level='state_lower')", tuple(states)).fetchone()[0]
    stats["counties_without_state_lower"] = nocov
    counts = {k: v for k, v in stats.items() if isinstance(v, int)}
    lists = {k: v for k, v in stats.items() if not isinstance(v, int)}
    notes = f"states={len(states)} links={rows} {json.dumps(counts, sort_keys=True)}"
    for k, v in lists.items():
        notes += f" {k}={len(v) if isinstance(v, list) else v}"
        if isinstance(v, list):
            notes += ":" + ";".join(v[:12])
    notes += f" rel_sources={len(rel_sids)} {time.time()-t0:.0f}s"
    return {"rows": rows, "notes": notes,
            "sources": [(CONGRESS_URL, CONGRESS_LIC), (OS_URL, OS_LIC),
                        *[(u, CENSUS_LIC) for u in REL_COUNTY.values()], (PHA_LAYER, PHA_LIC)]}


def _args():
    cfg = {}
    a = sys.argv[1:]
    if "--states" in a:
        i = a.index("--states")
        cfg["states"] = [s.strip().upper() for s in a[i + 1].split(",") if s.strip()]
    if "--refresh" in a:
        cfg["refresh"] = True
    return cfg


def _counts(conn):
    for r in conn.execute("SELECT level, COUNT(*), COUNT(DISTINCT county_fips) FROM officials_county "
                          "GROUP BY level ORDER BY level"):
        print(f"[{DOMAIN}] officials_county level={r[0]} links={r[1]} counties={r[2]}")
    for r in conn.execute("SELECT level, COUNT(*) FROM officials_person GROUP BY level ORDER BY level"):
        print(f"[{DOMAIN}] officials_person level={r[0]} people={r[1]}")
    for r in conn.execute("SELECT kind, method, COUNT(*) FROM officials_office GROUP BY 1,2 ORDER BY 1,2"):
        print(f"[{DOMAIN}] officials_office kind={r[0]} method={r[1]} offices={r[2]}")


def _selftest():
    cfg = _args()
    cfg.setdefault("states", ["PA"])
    conn = common.db()
    res = run(conn, cfg)
    conn.commit()
    print(f"[{DOMAIN}] selftest rows={res['rows']} notes={res['notes']}")
    _counts(conn)
    common.selftest_report(conn, DOMAIN, "officials_person", "state='PA'", 3)
    common.selftest_report(conn, DOMAIN, "officials_office", "state='PA'", 3)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _cfg = _args()
        common.main(DOMAIN, lambda conn, cfg: run(conn, _cfg), None)
        import sqlite3
        _counts(sqlite3.connect(common.DB))
