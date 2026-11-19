"""crime -- area-level crime context per city, honestly labelled.

WHY THIS DOMAIN LOOKS THE WAY IT DOES
-------------------------------------
UCR / NIBRS (the FBI's national crime collections) **do not record housing status**.
Nothing in a police incident record says "this person was homeless". So there is no
federal dataset that answers "how much crime do homeless people commit" -- the question
is not measurable from administrative crime data at all. What *is* measurable, per
police agency, is the area-level volume of reported offences.

This ingestor therefore does exactly two things, and never mixes them:

1. `metric` -- area-level rates per city per year:
       metric='violent_crime_rate', 'property_crime_rate', 'theft_rate'
       unit='per_100k', value = reported incidents / city population * 100000
   Every single row carries `note` with the caveat that the underlying records contain
   no homelessness status, so the number is *context about the place*, not about who
   committed the offence. The rate is flagged is_est=1 because the numerator is the
   police agency's jurisdiction and the denominator is the Census place population
   (they are close but not identical), and because counts are crosswalked from each
   portal's own offence labels into three coarse buckets.

2. `doc` -- documented studies / agency reports that *do* attempt to measure
   homeless-attributable or homeless-victim incidents (surveys, audits, hate-crime
   tallies, NCVS-style victimisation studies). These are catalogued, not summed:
   `doc` rows carry title/publisher/author/date/url only.

Sources
-------
* FBI Crime Data Explorer (api.usa.gov) -- ATTEMPTED, BLOCKED. DEMO_KEY is rate limited
  (HTTP 429 OVER_RATE_LIMIT) and the CDE S3 bulk bucket is gone. See BLOCKERS.md.
* City open-data portals: Socrata `/resource/*.json` (SoQL group-by, no key) for
  NYC, LA, Chicago, Dallas, Austin, SF, Seattle, Oakland, Cincinnati, Honolulu,
  Baton Rouge, Norfolk, Colorado Springs, Cambridge, Gainesville, New Orleans, Mesa.
* City/agency ArcGIS FeatureServer `query?groupByFieldsForStatistics=` (no key) for
  Philadelphia, Charlotte, Columbus, Baltimore, Tampa, Detroit, Raleigh, Cleveland,
  Boise, Sacramento, Long Beach, Minneapolis, Denver, Nashville, Boston, Las Vegas,
  Tucson, Washington DC, Houston, Louisville, Albuquerque, San Jose, Indianapolis,
  Durham, San Diego.
* doc rows: Crossref API (keyless) + data.gov CKAN (keyless) + agency report pages
  that resolve.

Everything is fetched through common.fetch (cached in data/raw/crime/, 1 req/s).
Nothing is modelled: if a portal does not publish a number the field stays NULL and
the source is recorded as `failed` in crime_source with the reason.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone

from ingest.common import (clean, fetch, fetch_json, now, save_raw, source_id_for,
                           to_int, upsert)

DOMAIN = "crime"

CAVEAT = ("UCR/NIBRS records carry NO homelessness status: this is area-level reported-crime "
          "context for the city, not crime committed by homeless people. Rate = portal-reported "
          "incidents crosswalked from the portal's own offence labels / Census place population x100k; "
          "agency jurisdiction and city limits differ, so is_est=1.")

BUCKET_NOTE = {
    "violent_crime_rate": "Violent bucket = homicide/murder, rape & sexual assault, robbery, "
                          "aggravated/simple assault, shooting, kidnapping, arson excluded.",
    "property_crime_rate": "Property bucket = burglary, arson, motor-vehicle theft, vandalism, "
                           "fraud/embezzlement/stolen property, other property offences.",
    "theft_rate": "Theft bucket = larceny/theft, shoplifting, theft from vehicle/building.",
}

# --------------------------------------------------------------------------------------
# crosswalk: portal offence label -> bucket. Ordered, first match wins.
# --------------------------------------------------------------------------------------
RULES = [
    ("violent", r"\b(HOMICIDE|MURDER|MANSLAUGHTER|NON-?NEGLIGENT|MURDER ?& ?NONNEG|NEGLIGENT MANSLAUGHTER)\b"),
    ("violent", r"\b(RAPE|SEXUAL ASSAULT|FORCIBLE SEX|SEX OFFENSE.{0,12}FORCIBLE|SODOMY)\b"),
    ("violent", r"\bROBBER"),
    ("violent", r"\b(KIDNAP|ABDUCT)"),
    ("violent", r"\b(AGGRAVATED ASSAULT|AGG ASSAULT|AGGRAVATED BATTERY|AGGRAVATED INJURY)\b"),
    ("violent", r"\b(SHOOTING|SHOTS FIRED|GUNSHOT|DISCHARGE.{0,10}FIREARM)\b"),
    ("violent", r"\b(ASSAULT|BATTERY)\b"),
    ("violent", r"\b(INTIMIDATION|STALKING)\b"),
    ("theft", r"\b(LARCEN|THEFT|SHOPLIFT|PURSE ?SNATCH|PICKPOCKET)"),
    ("property", r"\b(BURGLAR|BREAKING ?& ?ENTERING|BREAKING AND ENTERING)"),
    ("property", r"\b(MOTOR VEHICLE THEFT|AUTO THEFT|VEHICLE THEFT|STOLEN VEHICLE|MVTHEFT|MVT)\b"),
    ("property", r"\b(ARSON)"),
    ("property", r"\b(VANDAL|CRIMINAL MISCHIEF|DAMAGE.{0,12}PROPERTY|DESTRUCTION.{0,12}PROPERTY)"),
    ("property", r"\b(FRAUD|EMBEZZL|FORGERY|COUNTERFEIT|BAD CHECK|IDENTITY THEFT)"),
    ("property", r"\b(STOLEN PROPERTY|RECEIVING STOLEN|EXTORTION)\b"),
    ("property", r"\b(PROPERTY (CRIME|DAMAGE|OFFENSE|DESTRUCTION|LOSS))\b"),
    ("other", r"\b(DRUG|NARCOTIC|LIQUOR|ALCOHOL|WEAPON|TRESPASS|DISORDERLY|VAGRAN|LOITER|"
              "PROSTITUT|GAMBL|OBSCEN|CURFEW|RUNAWAY|TRAFFIC|DUI|FAMILY OFFENSE|JUVENILE|"
              "ANIMAL|FIREWORK|PUBLIC ORDER|OTHER|UNKNOWN|ALL OTHER|SIMPLE)\b"),
]
RULES = [(b, re.compile(p)) for b, p in RULES]


def bucket_for(label: str | None) -> tuple[str | None, str | None]:
    """label -> (bucket, rule-source). None when the label is empty."""
    if label is None:
        return None, None
    s = str(label).upper().strip()
    if not s:
        return None, None
    for b, rx in RULES:
        m = rx.search(s)
        if m:
            return b, m.group(0)
    return "other", "no-rule"


# --------------------------------------------------------------------------------------
# registry. Every url here was fetched at least once and is cached under data/raw/crime/.
# --------------------------------------------------------------------------------------
def S(city, state, host, dsid, title, publisher, note=""):
    return dict(key=f"socrata:{host}/{dsid}", city=city, state=state, kind="socrata",
                host=host, dsid=dsid, title=title, publisher=publisher,
                license="public data (city open-data portal terms; see portal)",
                url=f"https://{host}/resource/{dsid}.json", note=note)


def A(city, state, base, layer, title, publisher, year=None, note=""):
    b = base.rstrip("/")
    url = b if layer is None else f"{b}/{layer}"
    return dict(key=f"arcgis:{url}", city=city, state=state, kind="arcgis",
                base=b, layer=layer, title=title, publisher=publisher,
                license="public data (city ArcGIS open-data terms; see service)",
                url=url, year_fixed=year, note=note)


SOURCES = [
    # ---- Socrata /resource/*.json ----
    S("New York", "NY", "data.cityofnewyork.us", "qgea-i56i",
      "NYPD Complaint Data Historic", "NYC Police Department (via NYC OpenData)",
      "felony/misdemeanour complaint records, 2006-present"),
    S("Los Angeles", "CA", "data.lacity.org", "2nrs-mtv8",
      "Crime Data from 2020 to 2024", "Los Angeles Police Department (via LA Open Data)"),
    S("Los Angeles", "CA", "data.lacity.org", "k7nn-b2ep",
      "LAPD NIBRS Offenses Dataset", "Los Angeles Police Department (via LA Open Data)"),
    S("Chicago", "IL", "data.cityofchicago.org", "ijzp-q8t2",
      "Crimes - 2001 to Present", "Chicago Police Department (via Chicago Data Portal)"),
    S("Dallas", "TX", "www.dallasopendata.com", "qv6i-rri7",
      "Police Incidents", "Dallas Police Department (via Dallas OpenData)"),
    S("Austin", "TX", "datahub.austintexas.gov", "fdj4-gpfu",
      "Crime Reports", "Austin Police Department (via Austin Open Data)"),
    S("San Francisco", "CA", "data.sfgov.org", "wg3w-h783",
      "Police Department Incident Reports: 2018 to Present", "San Francisco Police Department"),
    S("Seattle", "WA", "data.seattle.gov", "tazs-3rd5",
      "SPD Crime Data: 2008-Present", "Seattle Police Department"),
    S("Oakland", "CA", "data.oaklandca.gov", "ppgh-7dqv",
      "CrimeWatch Data", "Oakland Police Department (via Oakland Open Data)"),
    S("Cincinnati", "OH", "data.cincinnati-oh.gov", "t9kw-3jfx",
      "Cincinnati Police Department Reported Crime Dataset", "Cincinnati Police Department"),
    S("Honolulu", "HI", "data.honolulu.gov", "vg88-5rn5",
      "HPD Crime Incidents", "Honolulu Police Department"),
    S("Baton Rouge", "LA", "data.brla.gov", "pbin-pcm7",
      "Baton Rouge Police Crime Incidents", "Baton Rouge Police Department"),
    S("Norfolk", "VA", "data.norfolk.gov", "r7bn-2egr",
      "Police Incident Reports", "Norfolk Police Department"),
    S("Colorado Springs", "CO", "policedata.coloradosprings.gov", "bc88-hemr",
      "Crime Level Data", "Colorado Springs Police Department"),
    S("Cambridge", "MA", "data.cambridgema.gov", "xuad-73uj",
      "Crime Reports", "Cambridge Police Department"),
    S("Gainesville", "FL", "data.cityofgainesville.org", "gvua-xt9q",
      "Crime Responses", "Gainesville Police Department"),
    S("New Orleans", "LA", "data.nola.gov", "c5iy-ew8n",
      "Electronic Police Report 2024", "New Orleans Police Department"),
    S("New Orleans", "LA", "data.nola.gov", "9wdb-bznc",
      "Electronic Police Report 2022", "New Orleans Police Department"),
    S("New Orleans", "LA", "data.nola.gov", "mm32-zkg7",
      "Electronic Police Report 2019", "New Orleans Police Department"),
    S("Mesa", "AZ", "citydata.mesaaz.gov", "37q9-d27y",
      "Crime Reporting Statistics", "Mesa Police Department"),

    # ---- ArcGIS FeatureServer / MapServer query ----
    A("Philadelphia", "PA", "https://services.arcgis.com/fLeGjb7u4uXqeF9q/arcgis/rest/services/INCIDENTS_PART1_PART2/FeatureServer", 0,
      "INCIDENTS_PART1_PART2", "City of Philadelphia (maps.phl.data)"),
    A("Charlotte", "NC", "https://gis.charlottenc.gov/arcgis/rest/services/CMPD/CMPDIncidents/MapServer", 0,
      "CMPD Incidents", "Charlotte-Mecklenburg Police Department"),
    A("Columbus", "OH", "https://services1.arcgis.com/9yy6msODkIBzkUXU/arcgis/rest/services/Police_Incident_Reports/FeatureServer", 0,
      "Police Incident Reports", "City of Columbus (ColumbusOhioGIS)"),
    A("Baltimore", "MD", "https://services1.arcgis.com/UWYHeuuJISiGmgXx/arcgis/rest/services/NIBRS_GroupA_Crime_Data/FeatureServer", 0,
      "NIBRS Group A Crime Data", "Baltimore Police Department"),
    A("Tampa", "FL", "https://services1.arcgis.com/IbNXlmt2RVVRCZ6M/arcgis/rest/services/crimes_public_365days/FeatureServer", 0,
      "crimes_public_365days", "Tampa Police Department"),
    A("Detroit", "MI", "https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/RMS_Crime_Incidents/FeatureServer", 0,
      "RMS Crime Incidents", "Detroit Police Department"),
    A("Raleigh", "NC", "https://services.arcgis.com/v400IkDOw1ad7Yad/arcgis/rest/services/Police_Incidents/FeatureServer", 0,
      "Raleigh Police Incidents (NIBRS)", "Raleigh Police Department (OpenData_ral)"),
    A("Cleveland", "OH", "https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/Crime_Incidents/FeatureServer", 0,
      "Crime Incidents", "Cleveland Division of Police (opendataCLE)"),
    A("Boise", "ID", "https://services1.arcgis.com/WHM6qC35aMtyAAlN/arcgis/rest/services/BPD_Crimes_Public/FeatureServer", 0,
      "BPD Crimes", "Boise Police Department"),
    A("Sacramento", "CA", "https://services5.arcgis.com/54falWtcpty3V47Z/arcgis/rest/services/Police_Crime_3Years/FeatureServer", 0,
      "Police_Crime_3Years", "Sacramento Police Department"),
    A("Long Beach", "CA", "https://services6.arcgis.com/yCArG7wGXGyWLqav/arcgis/rest/services/Police_Crime_Mapping/FeatureServer", 0,
      "Police Crime Mapping", "Long Beach Police Department"),
    A("Minneapolis", "MN", "https://services.arcgis.com/afSMGVsC7QlRK1kZ/arcgis/rest/services/Police_Incidents_Last_2Years/FeatureServer", 0,
      "Police Incidents Last 2 Years", "Minneapolis Police Department"),
    A("Denver", "CO", "https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/arcgis/rest/services/ODC_CRIME_OFFENSES_P/FeatureServer", 324,
      "Crime (offenses)", "City and County of Denver"),
    A("Nashville", "TN", "https://services2.arcgis.com/HdTo6HJqh92wn4D8/arcgis/rest/services/Metro_Nashville_Police_Department_Incidents_view/FeatureServer", 0,
      "Metro Nashville Police Department Incidents", "Nashville Open Data"),
    A("Boston", "MA", "https://services.arcgis.com/sFnw0xNflSi8J0uh/arcgis/rest/services/Boston_Incidents_Public_v2_view/FeatureServer", 0,
      "Boston Crime Incidents v2.2 - All Data View", "Boston Police Department (BPDMaps)"),
    A("Las Vegas", "NV", "https://services.arcgis.com/jjSk6t82vIntwDbs/arcgis/rest/services/Weekly_Public_Crimes/FeatureServer", 0,
      "LVMPD Reported NIBRS Crimes", "Las Vegas Metropolitan Police Department"),
    A("Houston", "TX", "https://services.arcgis.com/NummVBqZSIJKUeVR/arcgis/rest/services/HPD_Crime_Summary/FeatureServer", 0,
      "HPD Crime Summary (2020-2024)", "Houston Police Department (MyCityHouston)"),
    A("Louisville", "KY", "https://services1.arcgis.com/79kfd2K6fskCAkyg/arcgis/rest/services/crime_data_2025/FeatureServer", 0,
      "Louisville Metro KY - Crime Data 2025", "Louisville Metro Police Department", year=2025),
    A("Louisville", "KY", "https://services1.arcgis.com/79kfd2K6fskCAkyg/arcgis/rest/services/crimedata2024/FeatureServer", 0,
      "Louisville Metro KY - Crime Data 2024", "Louisville Metro Police Department", year=2024),
    A("Albuquerque", "NM", "https://coageo.cabq.gov/cabqgeo/rest/services/Incidents/FeatureServer", 0,
      "CAD Incidents", "City of Albuquerque"),
    A("San Jose", "CA", "https://geodev.sanjoseca.gov/server/rest/services/OPN/OPN_OpenDataService/MapServer", 588,
      "Police Calls 2025", "San Jose Police Department", year=2025),
    A("San Jose", "CA", "https://geodev.sanjoseca.gov/server/rest/services/OPN/OPN_OpenDataService/MapServer", 587,
      "Police Calls 2024", "San Jose Police Department", year=2024),
    A("Indianapolis", "IN", "https://gis.indy.gov/server/rest/services/OpenData/OpenData_NonSpatial/MapServer", 8,
      "IMPD UCR Current Year", "Indianapolis Metropolitan Police Department (IndyGIS)"),
    A("Durham", "NC", "https://services2.arcgis.com/G5vR3cOjh6g2Ed8E/arcgis/rest/services/Police/FeatureServer", 0,
      "Durham Police", "Durham Open Data"),
    A("Washington", "DC", "https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/MPD/FeatureServer", 7,
      "Crime Incidents in 2025", "DC Metropolitan Police Department (DCGIS)", year=2025),
    A("Washington", "DC", "https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/MPD/FeatureServer", 6,
      "Crime Incidents in 2024", "DC Metropolitan Police Department (DCGIS)", year=2024),
    A("Washington", "DC", "https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/MPD/FeatureServer", 5,
      "Crime Incidents in 2023", "DC Metropolitan Police Department (DCGIS)", year=2023),
    A("Tucson", "AZ", "https://services3.arcgis.com/9coHY2fvuFjG9HQX/arcgis/rest/services/TPDOpenData_ReportedCrimes_2026/FeatureServer", 0,
      "TPDOpenData_ReportedCrimes_2026", "Tucson Police Department", year=2026),
    A("Tucson", "AZ", "https://services3.arcgis.com/9coHY2fvuFjG9HQX/arcgis/rest/services/TPDOpenData_ReportedCrimes_2025/FeatureServer", 0,
      "TPDOpenData_ReportedCrimes_2025", "Tucson Police Department", year=2025),
    A("Tucson", "AZ", "https://services3.arcgis.com/9coHY2fvuFjG9HQX/arcgis/rest/services/TPDOpenData_ReportedCrimes_2024/FeatureServer", 0,
      "TPDOpenData_ReportedCrimes_2024", "Tucson Police Department", year=2024),
]

# documented attempts that failed (kept in the progress table with the reason)
DOC_QUERIES_CROSSREF = [
    "homelessness crime victimization",
    "homeless encampment crime offences arrests",
    "homeless people victimization violence",
]
CROSSREF = ("https://api.crossref.org/works?query.bibliographic={q}&rows=100"
            "&filter=from-pub-date:2012-01-01&select=DOI,title,author,publisher,"
            "container-title,issued,URL,abstract")

DATAGOV_SEARCH = ("https://catalog.data.gov/api/3/action/package_search?q=homeless%20crime"
                  "&rows=40")
# agency / NGO report pages that measure homeless-attributable or homeless-victim incidents
REPORT_PAGES = [
    ("https://nationalhomeless.org/publication/hate-crimes-violence-against-people-experiencing-homelessness/",
     "National Coalition for the Homeless"),
    ("https://bjs.ojp.gov/library/publications/",
     "Bureau of Justice Statistics"),
]

DATE_HINTS = [
    "incident_occurred_at", "occurreddate", "occurred_on", "occurredon", "occurrencedate",
    "offense_date", "offensedate", "crime_date", "crimedatetime", "report_date",
    "reported_date", "reportdate", "reported_datetime", "date_reported", "dispatch_date_time",
    "reporteddate", "occurreddatetime", "occurrence_date_utc", "first_occurrence_date",
    "incident_occurred", "start_date", "cmplnt_fr_dt", "from_date", "reported_on",
    "offenseyear", "reported_year", "occurred_year", "occurred_yr", "incident_year", "year",
]
CAT_HINTS = [
    "offense_description", "offense_desc", "offensedescription", "offense_category",
    "offensecategory", "highest_nibrs_description", "nibrsdesc", "nibrsoffense",
    "ucrsummarydescription", "ucr_summary_description", "ucr_general", "ucrdesc",
    "crime_category", "crime_type", "crimetype", "crime_description", "primary_type",
    "ofns_desc", "text_general_code", "general_subject", "statute_description",
    "offense_type_id", "offense", "description", "category", "type",
]
YEAR_HINTS = ["offenseyear", "reported_year", "occurred_year", "occurred_yr", "incident_year",
              "year"]
MIN_YEAR, MAX_YEARS = 2015, 8


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _mk_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS crime_source (
      key TEXT PRIMARY KEY, city TEXT, state TEXT, kind TEXT, url TEXT, title TEXT,
      publisher TEXT, status TEXT, rows INTEGER, note TEXT, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS crime_category (
      key TEXT, field TEXT, value TEXT, bucket TEXT, rule TEXT,
      PRIMARY KEY(key, field, value));
    CREATE TABLE IF NOT EXISTS crime_city_year (
      geoid TEXT, year INTEGER, violent INTEGER, property INTEGER, theft INTEGER,
      total INTEGER, source_id INTEGER,
      PRIMARY KEY(geoid, year));
    """)


def _norm(s):
    return re.sub(r"[^A-Z0-9 ]+", " ", (s or "").upper()).strip()


def city_geoid(conn, name, state):
    rows = conn.execute(
        "SELECT geoid, name, pop FROM geo_city WHERE kind='place' AND state=? "
        "ORDER BY pop DESC", (state,)).fetchall()
    want = _norm(name)
    for r in rows:
        if _norm(r["name"]) == want or _norm(r["name"]).startswith(want + " "):
            return r["geoid"], r["pop"]
    for r in rows:
        if want in _norm(r["name"]):
            return r["geoid"], r["pop"]
    return None, None


def _pick(fields, hints, types, prefer_type=None):
    """fields: [(name, type)]. hints ordered by priority. Returns field name or None."""
    low = {f[0].lower(): f for f in fields}
    for h in hints:
        if h in low:
            f = low[h]
            if f[1] in types:
                return f[0]
    for h in hints:
        for f in fields:
            if h in f[0].lower() and f[1] in types:
                return f[0]
    if prefer_type:
        for f in fields:
            if f[1] == prefer_type:
                return f[0]
    return None


# ---------------------------- Socrata ----------------------------
def _socrata_meta(host, dsid):
    url = f"https://{host}/api/views/{dsid}.json"
    d = fetch_json(url, domain=DOMAIN)
    cols = d.get("columns") or []
    fields = []
    for c in cols:
        if c.get("fieldName") and c.get("dataTypeName") != "nested":
            fields.append((c["fieldName"], c.get("dataTypeName") or ""))
    title = clean(d.get("name")) or ""
    lic = clean((d.get("license") or {}).get("name") if isinstance(d.get("license"), dict) else None)
    return fields, title, lic


def _soql_rows(host, dsid, select, where=None, group=None, order=None, limit=50000):
    q = {"$select": select, "$limit": str(limit)}
    if where:
        q["$where"] = where
    if group:
        q["$group"] = group
    if order:
        q["$order"] = order
    qs = "&".join(f"{k}={_urlq(v)}" for k, v in q.items())
    url = f"https://{host}/resource/{dsid}.json?{qs}"
    return fetch_json(url, domain=DOMAIN)


def _urlq(s):
    from urllib.parse import quote
    return quote(str(s), safe="$(),.*:-><=' ")


def _socrata_counts(src):
    host, dsid = src["host"], src["dsid"]
    fields, title, lic = _socrata_meta(host, dsid)
    if not fields:
        raise RuntimeError("no columns in metadata")
    date_f = _pick(fields, DATE_HINTS, {"calendar_date", "text", "number", "date"})
    cat_f = _pick(fields, CAT_HINTS, {"text", "number", "checkbox"})
    if not cat_f:
        raise RuntimeError("no category field")
    # sanity: category must have >1 distinct value
    probe = _soql_rows(host, dsid, f"{cat_f} AS c, count(*) AS n",
                       group=cat_f, order="n DESC", limit=400)
    if len(probe) < 2:
        raise RuntimeError(f"category field {cat_f} has <2 values")
    is_cal = any(f[0] == date_f and f[1] == "calendar_date" for f in fields)
    iso_year = any(f[0] == date_f and f[1] == "number" and "year" in f[0].lower() for f in fields)
    out = {}
    if date_f and is_cal:
        rows = _soql_rows(host, dsid,
                          f"date_trunc_y({date_f}) AS y, {cat_f} AS c, count(*) AS n",
                          group=f"y,{cat_f}", order="y", limit=50000)
        for r in rows:
            y = str(r.get("y") or "")[:4]
            if y.isdigit():
                out.setdefault(int(y), {})[r.get("c")] = to_int(r.get("n")) or 0
    elif date_f:
        for y in range(datetime.now(timezone.utc).year, MIN_YEAR - 1, -1):
            where = f"starts_with({date_f}, '{y}')" if not iso_year else f"{date_f} = {y}"
            try:
                rows = _soql_rows(host, dsid, f"{cat_f} AS c, count(*) AS n",
                                  where=where, group=cat_f, limit=50000)
            except Exception:
                continue
            if rows:
                out[y] = {r.get("c"): to_int(r.get("n")) or 0 for r in rows}
    else:
        rows = _soql_rows(host, dsid, f"{cat_f} AS c, count(*) AS n", group=cat_f, limit=4000)
        out[src.get("year_fixed") or datetime.now(timezone.utc).year] = {
            r.get("c"): to_int(r.get("n")) or 0 for r in rows}
    return out, cat_f, (title or src["title"]), lic, probe


# ---------------------------- ArcGIS ----------------------------
def _agol_url(base, layer, suffix=""):
    u = base.rstrip("/")
    if layer is not None:
        u = f"{u}/{layer}"
    return u + suffix


def _agol_meta(base, layer):
    d = fetch_json(_agol_url(base, layer, "?f=json"), domain=DOMAIN)
    if not d.get("fields") and d.get("layers"):
        layer = d["layers"][0]["id"]
        d = fetch_json(_agol_url(base, layer, "?f=json"), domain=DOMAIN)
    fields = [(f["name"], f.get("type", "")) for f in d.get("fields", [])]
    if not fields:
        raise RuntimeError("no fields in service metadata")
    return fields, layer, clean(d.get("name")) or ""


def _agol_query(base, layer, params):
    q = {"f": "json", "returnGeometry": "false", "outFields": "*"}
    q.update(params)
    qs = "&".join(f"{k}={_urlq(v)}" for k, v in q.items())
    d = fetch_json(_agol_url(base, layer, "/query?" + qs), domain=DOMAIN)
    if "error" in d:
        raise RuntimeError("arcgis error: " + str(d["error"])[:200])
    return d


def _agol_counts(src):
    base = src["base"]
    fields, layer, svcname = _agol_meta(base, src["layer"])
    types = {t for _, t in fields}
    oid = next((n for n, t in fields if t == "esriFieldTypeOID"), None)
    date_f = _pick(fields, DATE_HINTS,
                   {"esriFieldTypeDate", "esriFieldTypeString", "esriFieldTypeInteger"},
                   prefer_type="esriFieldTypeDate")
    year_f = _pick(fields, YEAR_HINTS, {"esriFieldTypeInteger", "esriFieldTypeSmallInteger",
                                        "esriFieldTypeDouble", "esriFieldTypeString"})
    cat_f = _pick(fields, CAT_HINTS, {"esriFieldTypeString", "esriFieldTypeInteger"})
    if not cat_f:
        raise RuntimeError("no category field")
    stat = json.dumps([{"statisticType": "count", "onStatisticField": oid or cat_f,
                        "outStatisticFieldName": "n"}])
    base_params = {"groupByFieldsForStatistics": cat_f, "outStatistics": stat,
                   "orderByFields": "n DESC"}

    def one(where=None):
        p = dict(base_params)
        if where:
            p["where"] = where
        d = _agol_query(base, layer, p)
        feats = d.get("features") or []
        out = {}
        for f in feats:
            a = f.get("attributes") or {}
            out[a.get(cat_f)] = to_int(a.get("n")) or 0
        return out

    # dry-run / probe on the whole service
    probe = one("1=1" if not date_f else None) if False else one()
    if len(probe) < 2:
        raise RuntimeError(f"category field {cat_f} has <2 values")

    out = {}
    fixed = src.get("year_fixed")
    years = [fixed] if fixed else list(range(datetime.now(timezone.utc).year, MIN_YEAR - 1, -1))
    for y in years:
        if y is None:
            continue
        if fixed:
            out[y] = probe
            continue
        got = {}
        if year_f and not any(k in year_f.lower() for k in ("date",)):
            try:
                got = one(f"{year_f} = {y}")
            except Exception:
                got = {}
        if not got:
            for where in (f"{date_f} >= DATE '{y}-01-01' AND {date_f} <= DATE '{y}-12-31'",
                          f"{date_f} >= TIMESTAMP '{y}-01-01 00:00:00' AND "
                          f"{date_f} <= TIMESTAMP '{y}-12-31 23:59:59'",
                          f"{date_f} >= {int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp()*1000)} "
                          f"AND {date_f} < {int(datetime(y+1, 1, 1, tzinfo=timezone.utc).timestamp()*1000)}"):
                try:
                    got = one(where)
                except Exception:
                    got = {}
                if got:
                    break
        if got:
            out[y] = got
    return out, cat_f, svcname or src["title"], None, probe


# --------------------------------------------------------------------------------------
# metric writing
# --------------------------------------------------------------------------------------
def _write_city(conn, conn_meta, geoid, pop, buckets, src_id, src):
    """buckets: {year: {violent: n, property: n, theft: n, other: n}}"""
    rows_metric = 0
    for year in sorted(buckets):
        if year < MIN_YEAR - 6:
            continue
        b = buckets[year]
        tot = sum(b.values())
        if tot <= 0:
            continue
        upsert(conn, "crime_city_year",
               {"geoid": geoid, "year": year, "violent": b.get("violent", 0),
                "property": b.get("property", 0), "theft": b.get("theft", 0),
                "total": tot, "source_id": src_id}, keys=["geoid", "year"])
        if not pop:
            continue
        for metric, key in (("violent_crime_rate", "violent"),
                            ("property_crime_rate", "property"),
                            ("theft_rate", "theft")):
            n = b.get(key, 0)
            rate = round(n / pop * 100000.0, 1) if n else 0.0
            note = (f"{CAVEAT} {BUCKET_NOTE[metric]} Numerator: {n:,} incidents reported in "
                    f"{year} on the {src['publisher']} portal "
                    f"(\"{conn_meta['title']}\"); denominator: Census 2024 place population "
                    f"{pop:,}. Reporting practice and agency jurisdiction change over time, "
                    f"so treat year-to-year moves as reporting artefacts until checked.")
            upsert(conn, "metric",
                   {"entity_type": "city", "entity_id": geoid, "metric": metric,
                    "value": rate, "unit": "per_100k", "year": year, "is_est": 1,
                    "note": note, "source_id": src_id},
                   keys=["entity_type", "entity_id", "metric", "year"])
            rows_metric += 1
    return rows_metric


# --------------------------------------------------------------------------------------
# doc collection
# --------------------------------------------------------------------------------------
def _year_of(v):
    if not v:
        return None
    if isinstance(v, dict):
        dp = (v.get("date-parts") or [[None]])[0]
        if dp and dp[0]:
            return to_int(dp[0])
    if isinstance(v, list) and v and isinstance(v[0], dict):
        dp = (v[0].get("date-parts") or [[None]])[0]
        if dp and dp[0]:
            return to_int(dp[0])
    m = re.search(r"(19|20)\d{2}", str(v))
    return int(m.group(0)) if m else None


def _collect_docs(conn, stats):
    n = 0
    for q in DOC_QUERIES_CROSSREF:
        url = CROSSREF.format(q=q.replace(" ", "%20"))
        try:
            d = fetch_json(url, domain=DOMAIN)
        except Exception as e:
            stats["doc_errors"].append(f"crossref {q}: {str(e)[:120]}")
            continue
        sid = source_id_for(conn, url, DOMAIN, title=f"Crossref search: {q}",
                            publisher="Crossref", license="Crossref metadata - open (CC0)")
        for it in (d.get("message") or {}).get("items", []):
            doi = clean(it.get("DOI"))
            if not doi:
                continue
            title = clean((it.get("title") or [None])[0])
            if not title:
                continue
            authors = it.get("author") or []
            a = None
            if authors:
                a = ", ".join(clean(x.get("family")) or clean(x.get("name")) or ""
                              for x in authors[:3]).strip(", ") or None
            upsert(conn, "doc",
                   {"kind": "study", "title": title,
                    "url": f"https://doi.org/{doi}",
                    "publisher": clean((it.get("container-title") or [None])[0]) or clean(it.get("publisher")),
                    "author": a, "published": str(_year_of(it.get("issued")) or "") or None,
                    "summary": clean((it.get("abstract") or ""))[:1200] or None,
                    "source_id": sid}, keys=["url"])
            n += 1
    try:
        d = fetch_json(DATAGOV_SEARCH, domain=DOMAIN)
        sid = source_id_for(conn, DATAGOV_SEARCH, DOMAIN, title="data.gov CKAN search: homeless crime",
                            publisher="data.gov (GSA)", license="public domain (US Government work)")
        for p in (d.get("result") or {}).get("results", []):
            u = clean(p.get("url") or p.get("name"))
            t = clean(p.get("title"))
            if not t or not u:
                continue
            upsert(conn, "doc",
                   {"kind": "dataset", "title": t, "url": u,
                    "publisher": clean((p.get("organization") or {}).get("title")),
                    "published": clean(p.get("metadata_modified"))[:10],
                    "summary": clean(p.get("notes"))[:1200],
                    "source_id": sid}, keys=["url"])
            n += 1
    except Exception as e:
        stats["doc_errors"].append(f"data.gov: {str(e)[:120]}")
    for url, pub in REPORT_PAGES:
        try:
            b = fetch(url, domain=DOMAIN)
        except Exception as e:
            stats["doc_errors"].append(f"report {url}: {str(e)[:120]}")
            continue
        html = b.decode("latin-1", "ignore")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = clean(re.sub(r"\s+", " ", m.group(1))) if m else None
        if not title:
            continue
        sid = source_id_for(conn, url, DOMAIN, title=title, publisher=pub,
                            license="publisher web page (see page)")
        upsert(conn, "doc",
               {"kind": "report", "title": title, "url": url, "publisher": pub,
                "published": None, "summary": None, "source_id": sid}, keys=["url"])
        n += 1
    return n


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
def run(conn, cfg):
    force = bool((cfg or {}).get("force"))
    _mk_tables(conn)
    conn.commit()
    stats = {"doc_errors": [], "failed": [], "ok": 0, "metric": 0, "doc": 0, "sources": []}
    done = {r["key"]: r for r in conn.execute("SELECT * FROM crime_source")}

    for src in SOURCES:
        prev = done.get(src["key"])
        if prev and prev["status"] == "ok" and not force:
            stats["ok"] += 1
            continue
        t0 = time.time()
        try:
            if src["kind"] == "socrata":
                per_year, cat_f, title, lic, probe = _socrata_counts(src)
            else:
                per_year, cat_f, title, lic, probe = _agol_counts(src)
            # crosswalk + audit trail
            buckets, mapped, total = {}, 0, 0
            for y, cats in per_year.items():
                b = buckets.setdefault(y, {})
                for val, n in cats.items():
                    bk, rule = bucket_for(val)
                    total += n
                    if bk:
                        mapped += n
                        b[bk] = b.get(bk, 0) + n
                    upsert(conn, "crime_category",
                           {"key": src["key"], "field": cat_f, "value": str(val)[:300],
                            "bucket": bk or "none", "rule": rule}, keys=["key", "field", "value"])
            if not total:
                raise RuntimeError("zero counts returned")
            if mapped / total < 0.60:
                raise RuntimeError(f"crosswalk mapped only {mapped}/{total} incidents")
            if not any(sum(v.values()) for v in buckets.values()):
                raise RuntimeError("no bucketed counts")
            geoid, pop = city_geoid(conn, src["city"], src["state"])
            if not geoid:
                raise RuntimeError(f"city {src['city']}, {src['state']} not in geo_city")
            sid = source_id_for(conn, src["url"], DOMAIN, title=title or src["title"],
                                publisher=src["publisher"],
                                license=lic or src["license"])
            nrows = _write_city(conn, {"title": title or src["title"]}, geoid, pop,
                                buckets, sid, src)
            upsert(conn, "crime_source",
                   {"key": src["key"], "city": src["city"], "state": src["state"],
                    "kind": src["kind"], "url": src["url"], "title": title or src["title"],
                    "publisher": src["publisher"], "status": "ok", "rows": nrows,
                    "note": f"cat_field={cat_f} years={sorted(buckets)} "
                            f"mapped={mapped}/{total} geoid={geoid} pop={pop} "
                            f"{time.time()-t0:.1f}s {src.get('note') or ''}".strip(),
                    "fetched_at": now()}, keys=["key"])
            stats["ok"] += 1
            stats["metric"] += nrows
            stats["sources"].append((src["city"], src["state"], geoid, sorted(buckets), nrows))
            print(f"  ok  {src['city']:<16}{src['state']} {nrows:>4} rows "
                  f"years={sorted(buckets)[:8]} cat={cat_f}", flush=True)
        except Exception as e:
            msg = str(e)[:300]
            upsert(conn, "crime_source",
                   {"key": src["key"], "city": src["city"], "state": src["state"],
                    "kind": src["kind"], "url": src["url"], "title": src["title"],
                    "publisher": src["publisher"], "status": "failed", "rows": 0,
                    "note": msg, "fetched_at": now()}, keys=["key"])
            stats["failed"].append(f"{src['city']},{src['state']}: {msg}")
            print(f"  FAIL {src['city']:<16}{src['state']} {msg[:110]}", flush=True)
        conn.commit()

    if not (cfg or {}).get("skip_docs"):
        try:
            stats["doc"] = _collect_docs(conn, stats)
        except Exception as e:
            stats["doc_errors"].append(f"docs: {str(e)[:200]}")
        conn.commit()

    nsrc = conn.execute("SELECT COUNT(*) FROM source WHERE domain=?", (DOMAIN,)).fetchone()[0]
    ncity = conn.execute("SELECT COUNT(DISTINCT entity_id) FROM metric WHERE metric='violent_crime_rate'").fetchone()[0]
    nmetric = conn.execute("SELECT COUNT(*) FROM metric WHERE metric IN "
                           "('violent_crime_rate','property_crime_rate','theft_rate')").fetchone()[0]
    nnote = conn.execute("SELECT COUNT(*) FROM metric WHERE metric IN "
                         "('violent_crime_rate','property_crime_rate','theft_rate') "
                         "AND note IS NOT NULL AND note LIKE '%NO homelessness status%'").fetchone()[0]
    ndoc = conn.execute("SELECT COUNT(*) FROM doc").fetchone()[0]
    notes = (f"cities_ok={stats['ok']}/{len(SOURCES)} city_metrics={ncity} "
             f"metric_rows={nmetric} metric_rows_with_caveat={nnote} doc_rows={ndoc} "
             f"sources={nsrc}")
    if stats["failed"]:
        notes += " | failed: " + "; ".join(stats["failed"])[:900]
    if stats["doc_errors"]:
        notes += " | doc_errors: " + "; ".join(stats["doc_errors"])[:400]
    return {"rows": stats["metric"] + stats["doc"], "notes": notes,
            "sources": [(s["url"], s["license"]) for s in SOURCES]}


def selftest():
    from ingest.common import db
    conn = db()
    _mk_tables(conn)
    for t in ("crime_source", "crime_category", "crime_city_year"):
        rows = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"[{DOMAIN}] rows in {t}: {rows}")
    print("[crime] metric rows:",
          conn.execute("SELECT COUNT(*) FROM metric WHERE metric IN "
                       "('violent_crime_rate','property_crime_rate','theft_rate')").fetchone()[0])
    print("[crime] doc rows:", conn.execute("SELECT COUNT(*) FROM doc").fetchone()[0])
    print("[crime] cities with a violent_crime_rate:",
          conn.execute("SELECT COUNT(DISTINCT entity_id) FROM metric WHERE "
                       "metric='violent_crime_rate'").fetchone()[0])
    for r in conn.execute("SELECT * FROM metric WHERE metric='violent_crime_rate' "
                          "ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))
    for r in conn.execute("SELECT * FROM crime_source ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        from ingest.common import db, main as common_main
        common_main(DOMAIN, lambda c, cfg: run(c, cfg), selftest_table="crime_source")
