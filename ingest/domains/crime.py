"""crime — area-level crime context for the largest US cities, honestly labelled.

WHAT THIS IS NOT
----------------
UCR/NIBRS and local incident systems record *offences and their location*, never the housing
status or homelessness of a suspect. Nothing in this domain can tell you that a crime was
committed by a homeless person, and no row here claims that. What is stored:

  * `metric` rows, entity_type='city', entity_id=<place GEOID>, for
    violent_crime_rate / property_crime_rate / theft_rate / crime_incidents_total,
    per 100,000 city residents per year, computed from *reported incidents* published by the
    city's own police open-data feed. Every row carries NOTE_CAVEAT plus the exact category
    labels that were counted, so the arithmetic is auditable.
  * `doc` rows for studies/reports that *do* try to measure homeless-attributable incidents or
    victimisation of homeless people (sourced from Crossref / OpenAlex metadata only — title,
    publisher, year, DOI; no numbers are copied).

SOURCES (all keyless; FBI CDE is attempted first and is currently blocked, see BLOCKERS.md)
  1. FBI Crime Data Explorer (api.usa.gov/crime/fbi/cde) with DEMO_KEY / DATA_GOV_API_KEY.
     Every call is attempted once per run; a 429/403 is recorded in `crime_progress`, never
     silently swallowed.
  2. City open-data portals — Socrata SoQL (`/resource/<id>.json`, `$group`/`$select`) and
     ArcGIS FeatureServer (`/query?returnCountOnly` / `outStatistics`), one city at a time.
  3. Crossref (api.crossref.org) + OpenAlex (api.openalex.org) for the `doc` table.

Every counts value is produced server-side by the portal from its own published rows and the raw
response is cached under data/raw/crime/. Category→bucket assignment (violent/property/theft) is
this ingestor's own mapping, so those metric rows are written is_est=1 and the mapping is spelled
out in the note.

Resumable: `crime_progress` records the per-city resolution so a re-run does not re-fetch.
"""
from __future__ import annotations

import json
import re
import urllib.parse

from ingest.common import (clean, db, fetch, name_norm, now, source_id_for, to_int, upsert)

DOMAIN = "crime"

# ---------------------------------------------------------------------------------------------
# the caveat that must sit on every metric row
# ---------------------------------------------------------------------------------------------
NOTE_CAVEAT = ("AREA-LEVEL CONTEXT ONLY. UCR/NIBRS and city incident systems do not record "
               "housing status, so this is NOT a count of crimes committed by homeless people "
               "and must not be read that way.")

CDE_KEY = "DEMO_KEY"          # FBI CDE demo key; frequently rate-limited (429) — see BLOCKERS.md
CDE_SIGNUP = "https://api.data.gov/signup/"
CDE = "https://api.usa.gov/crime/fbi/cde"

# ---------------------------------------------------------------------------------------------
# curated: which portal serves which city.  This is knowledge of *where the feed lives*, not of
# any data value; every entry is verified live before a single number is stored.  A city may
# have several candidates; the first one that resolves wins.
# ("socrata", domain, [hint dataset ids]) or ("arcgis", search-phrase, None)
# ---------------------------------------------------------------------------------------------
PORTALS = {
    "New York|NY":        [("socrata", "data.cityofnewyork.us", ["q6ts-7cfw", "5uac-w243"]), ("arcgis", "New York NYPD crime complaints", None)],
    "Los Angeles|CA":     [("socrata", "data.lacity.org", []), ("arcgis", "Los Angeles crime data 2020 to present", None)],
    "Chicago|IL":         [("socrata", "data.cityofchicago.org", ["ijzp-q8t2"])],
    "Houston|TX":         [("socrata", "data.houstontx.gov", []), ("arcgis", "Houston police crime incidents", None)],
    "Phoenix|AZ":         [("socrata", "phoenixopendata.com", []), ("arcgis", "Phoenix police crime incidents", None)],
    "Philadelphia|PA":    [("socrata", "data.phila.gov", []), ("socrata", "opendataphilly.org", []),
                           ("arcgis", "Philadelphia police crime incidents", None)],
    "San Antonio|TX":     [("socrata", "data.sanantonio.gov", []), ("arcgis", "San Antonio police crime incidents", None)],
    "San Diego|CA":       [("socrata", "data.sandiego.gov", []), ("arcgis", "San Diego police crime incidents", None)],
    "Dallas|TX":          [("socrata", "www.dallasopendata.com", ["qv6i-rri7"]), ("arcgis", "Dallas police incidents", None)],
    "Jacksonville|FL":    [("arcgis", "Jacksonville sheriff crime incidents", None)],
    "Fort Worth|TX":      [("arcgis", "Fort Worth police crime incidents", None)],
    "San Jose|CA":        [("socrata", "data.sanjoseca.gov", []), ("arcgis", "San Jose police crime incidents", None)],
    "Austin|TX":          [("socrata", "data.austintexas.gov", ["fdj4-gpfu", "7g7q-vg9i"]), ("arcgis", "Austin police crime reports", None)],
    "Charlotte|NC":       [("socrata", "data.charlottenc.gov", []), ("arcgis", "Charlotte police crime incidents", None)],
    "Columbus|OH":        [("socrata", "opendata.columbus.gov", []), ("arcgis", "Columbus police crime incidents", None)],
    "Indianapolis|IN":    [("socrata", "data.indy.gov", []), ("arcgis", "Indianapolis IMPD crime incidents", None)],
    "San Francisco|CA":   [("socrata", "data.sfgov.org", ["wg3w-h783"])],
    "Seattle|WA":         [("socrata", "data.seattle.gov", ["tazs-3rd5"])],
    "Denver|CO":          [("socrata", "data.denvergov.org", []), ("arcgis", "Denver police crime offenses", None)],
    "Oklahoma City|OK":   [("socrata", "data.okc.gov", []), ("arcgis", "Oklahoma City police crime incidents", None)],
    "Nashville-Davidson|TN": [("socrata", "data.nashville.gov", []), ("arcgis", "Metro Nashville police crime incidents", None)],
    "Washington|DC":      [("socrata", "opendata.dc.gov", []), ("arcgis", "DC police crime incidents", None)],
    "El Paso|TX":         [("socrata", "data.elpasotexas.gov", []), ("arcgis", "El Paso police crime incidents", None)],
    "Las Vegas|NV":       [("arcgis", "Las Vegas Metropolitan Police crime incidents", None)],
    "Boston|MA":          [("socrata", "data.boston.gov", []), ("arcgis", "Boston police crime incident reports", None)],
    "Detroit|MI":         [("arcgis", "Detroit police crime incidents", None)],
    "Louisville/Jefferson County|KY": [("socrata", "data.louisvilleky.gov", []), ("arcgis", "Louisville police crime incidents", None)],
    "Portland|OR":        [("arcgis", "Portland police crime incidents", None)],
    "Memphis|TN":         [("arcgis", "Memphis police crime incidents", None)],
    "Baltimore|MD":       [("socrata", "data.baltimorecity.gov", []), ("arcgis", "Baltimore police crime incidents", None)],
    "Milwaukee|WI":       [("socrata", "data.milwaukee.gov", []), ("arcgis", "Milwaukee police crime incidents", None)],
    "Albuquerque|NM":     [("socrata", "data.cabq.gov", []), ("arcgis", "Albuquerque police crime incidents", None)],
    "Tucson|AZ":          [("socrata", "data.tucsonaz.gov", []), ("arcgis", "Tucson police crime incidents", None)],
    "Fresno|CA":          [("arcgis", "Fresno police crime incidents", None)],
    "Sacramento|CA":      [("socrata", "data.cityofsacramento.org", []), ("arcgis", "Sacramento police crime incidents", None)],
    "Atlanta|GA":         [("arcgis", "Atlanta police crime incidents", None)],
    "Mesa|AZ":            [("socrata", "data.mesaaz.gov", []), ("arcgis", "Mesa police crime incidents", None)],
    "Kansas City|MO":     [("socrata", "data.kcmo.org", []), ("arcgis", "Kansas City police crime incidents", None)],
    "Raleigh|NC":         [("socrata", "data.raleighnc.gov", []), ("arcgis", "Raleigh police crime incidents", None)],
    "Colorado Springs|CO": [("arcgis", "Colorado Springs police crime incidents", None)],
    "Omaha|NE":           [("arcgis", "Omaha police crime incidents", None)],
    "Miami|FL":           [("arcgis", "Miami police crime incidents", None)],
    "Virginia Beach|VA":  [("arcgis", "Virginia Beach police crime incidents", None)],
    "Long Beach|CA":      [("socrata", "data.longbeach.gov", []), ("arcgis", "Long Beach police crime incidents", None)],
    "Oakland|CA":         [("socrata", "data.oaklandca.gov", []), ("arcgis", "Oakland police crime incidents", None)],
    "Minneapolis|MN":     [("socrata", "opendata.minneapolismn.gov", []), ("arcgis", "Minneapolis police crime incidents", None)],
    "Bakersfield|CA":     [("arcgis", "Bakersfield police crime incidents", None)],
    "Tulsa|OK":           [("arcgis", "Tulsa police crime incidents", None)],
    "Tampa|FL":           [("socrata", "data.tampagov.net", []), ("arcgis", "Tampa police crime incidents", None)],
    "Arlington|TX":       [("socrata", "data.arlingtontx.gov", []), ("arcgis", "Arlington Texas police crime incidents", None)],
    "Aurora|CO":          [("arcgis", "Aurora Colorado police crime incidents", None)],
    "Wichita|KS":         [("arcgis", "Wichita police crime incidents", None)],
    "Cleveland|OH":       [("socrata", "data.clevelandohio.gov", []), ("arcgis", "Cleveland police crime incidents", None)],
    "New Orleans|LA":     [("socrata", "data.nola.gov", []), ("arcgis", "New Orleans police crime incidents", None)],
    "Henderson|NV":       [("arcgis", "Henderson Nevada police crime incidents", None)],
    "Urban Honolulu|HI":  [("socrata", "data.honolulu.gov", []), ("arcgis", "Honolulu police crime incidents", None)],
    "Anaheim|CA":         [("arcgis", "Anaheim police crime incidents", None)],
    "Orlando|FL":         [("socrata", "data.cityoforlando.net", []), ("arcgis", "Orlando police crime incidents", None)],
    "Lexington-Fayette|KY": [("arcgis", "Lexington Kentucky police crime incidents", None)],
}

TARGET_CITIES = 60        # top-60 US places by population = the universe we try to cover

# category → bucket mapping applied to the *labels the portal publishes*
VIOLENT = ("homicide", "murder", "manslaughter", "rape", "sexual assault", "sexual violence",
           "sexual abuse", "sodomy", "robbery", "carjack", "assault", "battery", "shoot", "shot",
           "gun", "firearm", "weapon", "kidnap", "abduct", "human traffic", "intimidat")
PROPERTY = ("burglary", "burglar", "larceny", "theft", "shoplift", "stolen", "motor vehicle",
            "vehicle theft", "auto theft", "vandal", "criminal damage", "damage to property",
            "arson", "fraud", "forgery", "embezzl", "extortion", "bribery", "counterfeit",
            "receiving stolen", "bad check", "identity theft")
THEFT = ("larceny", "theft", "shoplift", "purse-snatch", "purse snatch", "pickpocket",
         "stolen property")
# rows that are not reported offences at all — excluded from every bucket and from the total
NON_CRIME = ("case closure", "fire report", "courtesy report", "civil sidewalk", "lost property",
             "non-criminal", "noncriminal", "non criminal", "traffic", "accident", "crash",
             "parking", "citation", "abandoned", "animal", "alarm", "noise", "welfare check",
             "calls for service", "cad", "dispatch", "medical", "mental health")

DATE_HINTS = ("incident_date", "occurred", "offense_date", "report_date", "date_time", "datetime",
              "timestamp", "date", "when", "created")
CAT_HINTS = ("nibrs_crime_against_category", "crime_against", "ucr_category", "offense_category",
             "incident_category", "primary_type", "offense_type", "offense_sub_category",
             "crime_type", "offence", "offense", "offence_type", "crime", "category", "type",
             "nature", "call_type", "description")

REJECT_TITLE = ("calls for service", "911", "cad ", "dispatch", "response times", "arrest",
                "traffic", "accident", "crash", "citation", "parking", "budget", "salary",
                "map", "story", "dashboard")
ACCEPT_TITLE = ("crime", "offense", "offence", "incident", "complaint", "theft")


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _try(url: str, **kw):
    """fetch() that returns None instead of raising (source failures are recorded, not fatal)."""
    try:
        return fetch(url, domain=DOMAIN, retries=2, timeout=kw.pop("timeout", 90), **kw)
    except Exception:
        return None


def _try_json(url: str, **kw):
    b = _try(url, **kw)
    if b is None:
        return None
    try:
        return json.loads(b)
    except Exception:
        return None


def _ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS crime_progress(
        city_key TEXT PRIMARY KEY, geoid TEXT, city TEXT, state TEXT,
        engine TEXT, endpoint TEXT, dataset TEXT, status TEXT, detail TEXT,
        metric_rows INTEGER DEFAULT 0, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS crime_year_counts(
        geoid TEXT, year INTEGER, dataset TEXT, label TEXT, bucket TEXT, n INTEGER,
        source_id INTEGER, PRIMARY KEY(geoid, year, dataset, label))""")


def _progress(conn, row):
    row = dict(row)
    row["updated_at"] = now()
    upsert(conn, "crime_progress", row, keys=["city_key"])


def _iso_year(v):
    if not v:
        return None
    m = re.match(r"(\d{4})-", str(v))
    if m:
        return int(m.group(1))
    return None


def _pick_date_col(row: dict):
    """first column in a sample record that looks like a usable date/datetime column."""
    for hint in DATE_HINTS:
        for k, v in row.items():
            if hint in k.lower() and _iso_year(v):
                return k
    for k, v in row.items():
        if _iso_year(v):
            return k
    return None


def _rank_cat_cols(cols, row: dict, date_col):
    """candidate offence-label columns, best first (exact UCR/NIBRS-ish names win)."""
    lowered = {c.lower(): c for c in cols}
    out = []
    for hint in CAT_HINTS:
        for lc, c in lowered.items():
            if hint == lc and c != date_col and isinstance(row.get(c), str) and c not in out:
                out.append(c)
    for hint in CAT_HINTS:
        for lc, c in lowered.items():
            if hint in lc and c != date_col and isinstance(row.get(c), str) and c not in out:
                out.append(c)
    for c in cols:
        if (c != date_col and c not in out and isinstance(row.get(c), str)
                and 0 < len(row.get(c) or "") < 60):
            out.append(c)
    return out


def _bucket(label: str):
    l = (label or "").lower()
    if not l or any(k in l for k in NON_CRIME):
        return None
    if any(k in l for k in VIOLENT):
        return "violent"
    if any(k in l for k in PROPERTY):
        return "property"
    return None


def _is_theft(label: str):
    l = (label or "").lower()
    return any(k in l for k in THEFT)


def _note(dataset_name, url, labels_by_bucket, pop, pop_year):
    lab = "; ".join(f"{b}={sorted(set(v))[:12]}" for b, v in labels_by_bucket.items() if v)
    return (f"{NOTE_CAVEAT} Source: '{dataset_name}' at {url}. Value = offences the portal "
            f"returns for this calendar year divided by {pop:,} city residents (Census "
            f"vintage {pop_year}, geo_city) x 100,000. is_est=1 because the offence-label -> "
            f"UCR-style bucket mapping is this ingestor's, not the publisher's. Labels counted: "
            f"{lab or 'none'}.")


# ---------------------------------------------------------------------------------------------
# engine 1 — FBI Crime Data Explorer (blocked on DEMO_KEY today; attempted every run)
# ---------------------------------------------------------------------------------------------
def cde_attempt(conn, cfg):
    key = (cfg or {}).get("cde_key") or CDE_KEY
    url = f"{CDE}/summarized/state/CA/violent-crime/2022/2022?API_KEY={urllib.parse.quote(key)}"
    body = _try(url, retries=1, timeout=45)
    if body is None:
        _progress(conn, {"city_key": "__cde__", "engine": "fbi_cde", "endpoint": url,
                         "status": "blocked",
                         "detail": f"api.usa.gov refused key {key} (HTTP 429/403). Sign up for a "
                                   f"free key at {CDE_SIGNUP} and set DATA_GOV_API_KEY / "
                                   f"cfg.cde_key. See BLOCKERS.md."})
        return 0, "cde=blocked"
    try:
        j = json.loads(body)
    except Exception:
        return 0, "cde=unparsable"
    _progress(conn, {"city_key": "__cde__", "engine": "fbi_cde", "endpoint": url,
                     "status": "ok", "detail": f"key {key} accepted"})
    return 0, f"cde=reachable({type(j).__name__})"


# ---------------------------------------------------------------------------------------------
# engine 2 — Socrata
# ---------------------------------------------------------------------------------------------
def _socrata_candidates(dom: str, city: str, hints):
    """dataset ids to try: curated hints first, then a domain-scoped catalogue search."""
    ids = list(hints or [])
    for q in ("crime", "police incidents", "offenses"):
        u = ("https://api.us.socrata.com/api/catalog/v1?"
             + urllib.parse.urlencode({"domains": dom, "q": q, "limit": 25}))
        j = _try_json(u)
        if not j:
            continue
        for r in j.get("results", []):
            rid = r.get("resource", {}).get("id")
            name = (r.get("resource", {}).get("name") or "")
            meta_dom = (r.get("metadata", {}) or {}).get("domain") or ""
            if meta_dom != dom or not rid or rid in ids:
                continue
            nl = name.lower()
            if any(k in nl for k in REJECT_TITLE):
                continue
            if not any(k in nl for k in ACCEPT_TITLE):
                continue
            ids.append(rid)
    return ids[:8]


ID_COL = re.compile(r"(^|_)(id|no|num|number|case|key|uuid|guid|dr_?no|incident_id|report_number)(_|$)")


def _socrata_city(conn, conn_city, dom: str, hints, pop, pop_year):
    """Probe up to N datasets on one portal, keep the one covering most incidents.

    Return (status, dataset_name, base_url, rows_written, detail).
    """
    geoid, city, state = conn_city
    cityname = city.split("-")[0].split("/")[0].split(" (")[0].strip()
    cur_year = int(now()[:4])
    best = None            # (score, rid, title, base, dcol, ccol, by_year{sid:...}, years)
    tried = 0
    for rid in _socrata_candidates(dom, cityname, hints)[:5]:
        tried += 1
        base = f"https://{dom}/resource/{rid}"
        sample = _try_json(f"{base}.json?$limit=1")
        if not isinstance(sample, list) or not sample:
            continue
        cols = list(sample[0].keys())
        dcol = _pick_date_col(sample[0])
        if not dcol:
            continue
        ccols = [c for c in _rank_cat_cols(cols, sample[0], dcol) if not ID_COL.search(c.lower())]
        if not ccols:
            continue
        ccol = ccols[0]
        # one query gives year x offence-label counts for the whole dataset
        q = (f"{base}.json?$select=date_extract_y({dcol}) as y,{ccol} as lab,count(*) as n"
             f"&$group=y,lab&$limit=50000")
        j = _try_json(q)
        if not isinstance(j, list) or not j:
            continue
        by_year = {}
        for rec in j:
            y = to_int(rec.get("y"))
            if not y:
                continue
            by_year.setdefault(y, []).append((str(rec.get("lab") or ""), to_int(rec.get("n")) or 0))
        max_year = max(by_year) if by_year else None
        if not max_year:
            continue
        years = [max_year - 1, max_year - 2] if max_year >= cur_year else [max_year, max_year - 1]
        years = [y for y in years if y >= 2015 and y in by_year]
        if not years:
            continue
        score = sum(n for _, n in by_year[max(years)])
        if best is None or score > best[0]:
            best = (score, rid, base, dcol, ccol, by_year, years, ccols)
    if best is None or best[0] < 200:
        return ("no_dataset", dom, None, 0,
                f"portal reachable, no crime dataset with usable date+offence columns "
                f"({tried} probed)")
    score, rid, base, dcol, ccol, by_year, years, ccols = best
    meta = _try_json(f"https://api.us.socrata.com/api/catalog/v1?domains={dom}&q={rid}&limit=5")
    title = None
    if meta:
        for r in meta.get("results", []):
            if r.get("resource", {}).get("id") == rid:
                title = clean(r["resource"].get("name"))
                break
    title = title or rid
    sid = source_id_for(conn, base + ".json", DOMAIN,
                        title=f"{title} ({city}, {state}) open-data incidents",
                        publisher=f"{city} open-data portal ({dom})",
                        license="city open-data terms; see the portal")
    written = 0
    for y in years:
        totals = {"violent": 0, "property": 0, "theft": 0, "all": 0}
        labels = {"violent": [], "property": [], "theft": []}
        for lab, n in by_year.get(y, []):
            totals["all"] += n
            b = _bucket(lab)
            if b:
                totals[b] += n
                labels[b].append(lab)
                if _is_theft(lab):
                    totals["theft"] += n
                    labels["theft"].append(lab)
            upsert(conn, "crime_year_counts",
                   {"geoid": geoid, "year": y, "dataset": f"{dom}/{rid}", "label": lab,
                    "bucket": b, "n": n, "source_id": sid},
                   keys=["geoid", "year", "dataset", "label"])
        note = _note(title, base, labels, pop, pop_year)
        for metric, count in (("violent_crime_rate", totals["violent"]),
                              ("property_crime_rate", totals["property"]),
                              ("theft_rate", totals["theft"])):
            if not count:
                continue
            upsert(conn, "metric",
                   {"entity_type": "city", "entity_id": geoid, "metric": metric,
                    "value": round(count / pop * 100000, 1), "unit": "per_100k",
                    "year": y, "is_est": 1, "note": note, "source_id": sid},
                   keys=["entity_type", "entity_id", "metric", "year"])
            written += 1
        upsert(conn, "metric",
               {"entity_type": "city", "entity_id": geoid, "metric": "crime_incidents_total",
                "value": float(totals["all"]), "unit": "incidents", "year": y, "is_est": 0,
                "note": f"{NOTE_CAVEAT} Every row the dataset '{title}' returns dated in {y} "
                        f"(offences plus any non-offence record types it publishes). "
                        f"Source: {base}", "source_id": sid},
               keys=["entity_type", "entity_id", "metric", "year"])
        written += 1
    return ("ok", f"{dom}/{rid}", base + ".json", written,
            f"{title}; date={dcol}; cat={ccol}; years={years}; max_year={max(by_year)}")


# ---------------------------------------------------------------------------------------------
# engine 3 — ArcGIS FeatureServer
# ---------------------------------------------------------------------------------------------
def _arcgis_city(conn, conn_city, phrase, pop, pop_year):
    geoid, city, state = conn_city
    cityname = city.split("-")[0].split("/")[0].strip().lower()
    q = urllib.parse.quote(f'{phrase} type:"Feature Service"')
    j = _try_json(f"https://www.arcgis.com/sharing/rest/search?q={q}&f=json&num=25")
    if not j or not j.get("results"):
        return ("no_dataset", "arcgis", None, 0, "no ArcGIS feature service matched")
    cands = []
    for r in j["results"]:
        title = (r.get("title") or "")
        blob = (title + " " + (r.get("snippet") or "") + " " + (r.get("description") or "")).lower()
        tl = title.lower()
        if cityname not in blob and state.lower() not in blob:
            continue
        if any(k in tl for k in REJECT_TITLE):
            continue
        if not any(k in tl for k in ACCEPT_TITLE):
            continue
        if not r.get("url"):
            continue
        cands.append(r)
    for r in cands[:5]:
        svc = r["url"].rstrip("/")
        if svc.endswith("/FeatureServer") or "/FeatureServer" in svc or "/MapServer" in svc:
            layer_url = svc + "/0" if not re.search(r"/\d+$", svc) else svc
        else:
            continue
        lj = _try_json(f"{layer_url}?f=json")
        if not lj or "fields" not in lj:
            continue
        fields = {f["name"]: f for f in lj["fields"]}
        dfield = None
        for hint in DATE_HINTS:
            for n, f in fields.items():
                if hint in n.lower() and f.get("type") == "esriFieldTypeDate":
                    dfield = n
                    break
            if dfield:
                break
        if not dfield:
            continue
        cfield = None
        for hint in CAT_HINTS:
            for n, f in fields.items():
                if hint == n.lower() and f.get("type") == "esriFieldTypeString":
                    cfield = n
                    break
            if cfield:
                break
        if not cfield:
            for hint in CAT_HINTS:
                for n, f in fields.items():
                    if hint in n.lower() and f.get("type") == "esriFieldTypeString":
                        cfield = n
                        break
                if cfield:
                    break
        if not cfield:
            continue
        oid = lj.get("objectIdField") or lj.get("fields")[0]["name"]
        # latest date present
        last = _try_json(f"{layer_url}/query?where=1%3D1&outFields={dfield}"
                         f"&orderByFields={dfield}+DESC&resultRecordCount=1"
                         f"&returnGeometry=false&f=json")
        max_year = None
        try:
            v = last["features"][0]["attributes"][dfield]
            if isinstance(v, (int, float)):
                import datetime as _dt
                max_year = _dt.datetime.utcfromtimestamp(v / 1000).year
        except Exception:
            pass
        if not max_year:
            continue
        cur_year = int(now()[:4])
        years = [max_year - 1, max_year - 2] if max_year >= cur_year else [max_year, max_year - 1]
        title = clean(r.get("title")) or svc
        sid = source_id_for(conn, svc, DOMAIN,
                            title=f"{title} ({city}, {state}) feature service",
                            publisher=f"{clean(r.get('owner')) or 'ArcGIS Online'} feature service",
                            license="publisher open-data terms; see item page")
        written = 0
        any_year = False
        for y in years:
            if y < 2015:
                continue
            w = urllib.parse.quote(f"{dfield} >= DATE '{y}-01-01' AND {dfield} < DATE '{y + 1}-01-01'")
            stats = urllib.parse.quote(json.dumps(
                [{"statisticType": "count", "onStatisticField": oid,
                  "outStatisticFieldName": "n"}]))
            qq = (f"{layer_url}/query?where={w}&groupByFieldsForStatistics={cfield}"
                  f"&outStatistics={stats}&returnGeometry=false&f=json")
            jj = _try_json(qq)
            if not jj or "features" not in jj:
                continue
            any_year = True
            totals = {"violent": 0, "property": 0, "theft": 0, "all": 0}
            labels = {"violent": [], "property": [], "theft": []}
            for ft in jj["features"]:
                a = ft.get("attributes", {})
                lab = str(a.get(cfield) or "")
                n = to_int(a.get("n")) or 0
                totals["all"] += n
                b = _bucket(lab)
                if b:
                    totals[b] += n
                    labels[b].append(lab)
                    if _is_theft(lab):
                        totals["theft"] += n
                        labels["theft"].append(lab)
                upsert(conn, "crime_year_counts",
                       {"geoid": geoid, "year": y, "dataset": svc, "label": lab, "bucket": b,
                        "n": n, "source_id": sid},
                       keys=["geoid", "year", "dataset", "label"])
            note = _note(title, svc, labels, pop, pop_year)
            for metric, count in (("violent_crime_rate", totals["violent"]),
                                  ("property_crime_rate", totals["property"]),
                                  ("theft_rate", totals["theft"])):
                if not count:
                    continue
                upsert(conn, "metric",
                       {"entity_type": "city", "entity_id": geoid, "metric": metric,
                        "value": round(count / pop * 100000, 1), "unit": "per_100k", "year": y,
                        "is_est": 1, "note": note, "source_id": sid},
                       keys=["entity_type", "entity_id", "metric", "year"])
                written += 1
            upsert(conn, "metric",
                   {"entity_type": "city", "entity_id": geoid, "metric": "crime_incidents_total",
                    "value": float(totals["all"]), "unit": "incidents", "year": y, "is_est": 0,
                    "note": f"{NOTE_CAVEAT} All records in feature service '{title}' dated {y}. "
                            f"Source: {svc}", "source_id": sid},
                   keys=["entity_type", "entity_id", "metric", "year"])
            written += 1
        if any_year and written:
            return ("ok", svc, svc, written, f"{title}; date={dfield}; cat={cfield}; max_year={max_year}")
    return ("no_dataset", "arcgis", None, 0, "no ArcGIS layer produced usable counts")


# ---------------------------------------------------------------------------------------------
# doc — studies/reports that attempt to measure homeless-attributable incidents or victimisation
# ---------------------------------------------------------------------------------------------
CROSSREF_QUERIES = [
    "homelessness crime victimization",
    "homeless people arrests police contact",
    "criminalization of homelessness offenses",
    "homelessness and crime rates city",
    "victimization of homeless persons",
    "homeless encampment crime displacement",
    "unsheltered homelessness public order offenses",
    "homelessness arrests cost jail",
]
OPENALEX_QUERIES = [
    "homelessness crime",
    "homeless victimization",
    "homeless arrests",
]
DOC_OK = re.compile(r"homeless", re.I)
DOC_TOPIC = re.compile(r"crim|arrest|offen|offenc|victimi|polic|incarcerat|jail|citation|theft", re.I)


def _docs(conn):
    rows = 0
    for q in CROSSREF_QUERIES:
        u = ("https://api.crossref.org/works?"
             + urllib.parse.urlencode({"query.bibliographic": q, "rows": 20,
                                       "select": "DOI,title,publisher,issued,URL,type,author,container-title"}))
        j = _try_json(u)
        if not j:
            continue
        sid = source_id_for(conn, "https://api.crossref.org/works?query.bibliographic="
                                 + urllib.parse.quote(q) + "&rows=20", DOMAIN,
                            title=f"Crossref search: {q}", publisher="Crossref",
                            license="Crossref metadata is CC0 / open; abstracts remain publisher copyright")
        for it in (j.get("message", {}) or {}).get("items", []):
            title = clean((it.get("title") or [None])[0])
            if not title or not DOC_OK.search(title) or not DOC_TOPIC.search(title):
                continue
            doi = it.get("DOI")
            if not doi:
                continue
            url = clean(it.get("URL")) or f"https://doi.org/{doi}"
            authors = it.get("author") or []
            author = None
            if authors:
                a0 = authors[0]
                author = clean(" ".join(x for x in (a0.get("given"), a0.get("family")) if x) or a0.get("name"))
            year = None
            try:
                year = it["issued"]["date-parts"][0][0]
            except Exception:
                pass
            cont = clean((it.get("container-title") or [None])[0])
            upsert(conn, "doc",
                   {"kind": "study", "title": title, "url": url,
                    "publisher": clean(it.get("publisher")) or cont, "author": author,
                    "published": str(year) if year else None,
                    "summary": ("Peer-reviewed study/report measuring homelessness and offending, "
                                "arrest or victimisation (Crossref DOI " + doi + "). Metadata only; "
                                "no figure is reproduced here. Per UCR/NIBRS these measures come "
                                "from studies or agency records, not from offence-based crime data."),
                    "source_id": sid},
                   keys=["url"])
            rows += 1
    for q in OPENALEX_QUERIES:
        u = "https://api.openalex.org/works?" + urllib.parse.urlencode(
            {"search": q, "per-page": 25, "filter": "type:report|type:article"})
        j = _try_json(u)
        if not j:
            continue
        sid = source_id_for(conn, "https://api.openalex.org/works?search=" + urllib.parse.quote(q)
                            + "&per-page=25", DOMAIN, title=f"OpenAlex search: {q}",
                            publisher="OpenAlex", license="OpenAlex metadata CC0")
        for it in j.get("results", []):
            title = clean(it.get("title"))
            if not title or not DOC_OK.search(title) or not DOC_TOPIC.search(title):
                continue
            url = clean(it.get("doi")) or clean((it.get("primary_location") or {}).get("landing_page_url"))
            if not url:
                continue
            pub = ((it.get("primary_location") or {}).get("source") or {}).get("display_name")
            upsert(conn, "doc",
                   {"kind": "report" if it.get("type") == "report" else "study", "title": title,
                    "url": url, "publisher": clean(pub) or "OpenAlex record",
                    "author": clean((it.get("authorships") or [{}])[0].get("author", {}).get("display_name")),
                    "published": str(it.get("publication_year")) if it.get("publication_year") else None,
                    "summary": ("Study/report on homelessness and offending, arrest or victimisation "
                                "(OpenAlex record " + str(it.get("id")) + "). Metadata only; no "
                                "figure reproduced."),
                    "source_id": sid},
                   keys=["url"])
            rows += 1
    return rows


# ---------------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------------
def run(conn, cfg):
    _ensure_tables(conn)
    cfg = cfg or {}
    force = bool(cfg.get("force"))
    limit = int(cfg.get("limit") or TARGET_CITIES)

    cde_rows, cde_note = cde_attempt(conn, cfg)

    cities = conn.execute(
        """SELECT geoid, name, state, pop FROM geo_city
           WHERE kind='place' AND pop IS NOT NULL AND pop > 0 AND geoid NOT LIKE '15%'
           ORDER BY pop DESC LIMIT ?""", (limit,)).fetchall()

    done = {}
    if not force:
        for r in conn.execute("SELECT city_key, status FROM crime_progress"):
            done[r["city_key"]] = r["status"]

    ok_cities = 0
    metric_rows = 0
    doc_rows = 0
    missing = []
    for c in cities:
        geoid, name, state, pop = c["geoid"], c["name"], c["state"], c["pop"]
        city_key = f"{name}|{state}"
        if done.get(city_key) == "ok":
            ok_cities += 1
            continue
        cands = PORTALS.get(city_key)
        if cands is None:
            # fall back to prefix match (names in PORTALS were trimmed of suffixes)
            for k, v in PORTALS.items():
                if k.split("|")[0].split(" ")[0].lower() == name.split(" ")[0].lower() and k.endswith("|" + state):
                    cands = v
                    break
        if not cands:
            _progress(conn, {"city_key": city_key, "geoid": geoid, "city": name, "state": state,
                             "engine": None, "status": "no_portal",
                             "detail": "no open-data crime feed known for this city; see research/crime.md"})
            missing.append(f"{name}, {state}")
            conn.commit()
            continue
        status, endpoint, dataset_url, written, detail = ("no_dataset", None, None, 0, "no candidate resolved")
        for spec in cands:
            if spec[0] == "socrata":
                status, endpoint, dataset_url, written, detail = _socrata_city(
                    conn, (geoid, name, state), spec[1], spec[2], pop, 2024)
            else:
                status, endpoint, dataset_url, written, detail = _arcgis_city(
                    conn, (geoid, name, state), spec[1], pop, 2024)
            if status == "ok":
                break
            conn.commit()
        _progress(conn, {"city_key": city_key, "geoid": geoid, "city": name, "state": state,
                         "engine": spec[0], "endpoint": endpoint, "dataset": dataset_url,
                         "status": status, "detail": detail[:900], "metric_rows": written})
        conn.commit()
        metric_rows += written
        if status == "ok":
            ok_cities += 1
        else:
            missing.append(f"{name}, {state}")

    doc_rows = _docs(conn)
    conn.commit()

    notes = (f"cities_ok={ok_cities}/{len(cities)} metric_rows={metric_rows} docs={doc_rows} "
             f"{cde_note} missing={len(missing)}")
    sources = [
        (f"{CDE}/summarized/state/{{state}}/{{offense}}/{{since}}/{{until}}?API_KEY={CDE_KEY}",
         "public domain; api.data.gov key (DEMO_KEY rate-limited)"),
        ("https://api.us.socrata.com/api/catalog/v1", "Socrata Discovery API — dataset metadata"),
        ("https://www.arcgis.com/sharing/rest/search", "ArcGIS Online item search — metadata"),
        ("https://api.crossref.org/works", "Crossref metadata, CC0"),
        ("https://api.openalex.org/works", "OpenAlex metadata, CC0"),
    ]
    for spec in PORTALS.values():
        for s in spec:
            if s[0] == "socrata":
                sources.append((f"https://{s[1]}/resource/<id>.json", "city open-data terms"))
    return {"rows": metric_rows + doc_rows, "notes": notes, "sources": sources}


# ---------------------------------------------------------------------------------------------
def selftest():
    conn = db()
    _ensure_tables(conn)
    res = run(conn, {"limit": 4})
    conn.commit()
    print(res["notes"])
    print("progress:", [dict(r) for r in conn.execute(
        "SELECT city_key,status,engine,detail FROM crime_progress ORDER BY city_key LIMIT 8")])
    for r in conn.execute("SELECT * FROM metric WHERE metric LIKE '%crime%' ORDER BY RANDOM() LIMIT 3"):
        d = dict(r)
        d["note"] = (d.get("note") or "")[:160] + " ..."
        print(d)
    for r in conn.execute("SELECT title,url,publisher,published FROM doc ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    else:
        conn = db()
        print(run(conn, {})["notes"])
        conn.commit()
