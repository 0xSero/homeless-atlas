"""facility domain — US institutions: state psychiatric hospitals (open, closed, repurposed),
state and federal prisons, large jails, with capacity, operator, opening/closing years and
published operating economics.

Sources (all keyless, cached under data/raw/facility/):
  * HIFLD Hospitals + HIFLD Prisons point layers (ArcGIS FeatureServer) — current inventory
    with bed/capacity, owner, status, coordinates and data vintage per record.
  * Wikidata SPARQL — inception (P571), dissolved (P576), capacity (P1083), employees (P1128),
    coordinates (P625), admin territory (P131), operator (P137), owner (P127), budget (P2769).
  * Wikipedia MediaWiki API — per-state institution categories (fills what Wikidata lacks,
    including defunct institutions), article infoboxes (beds/opened/closed/operator/budget),
    and the "List of <state> state prisons" tables (capacity + opened/closed per facility).
  * LAO (California Legislative Analyst's Office) budget analyses — state hospital system
    operating expenditures and CDCR expenditures, used for derived cost-per-bed economics.

Nothing is estimated silently: derived numbers go to `metric` with is_est=1 and the formula
in `note`; unknown fields stay NULL.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import urllib.parse

from ingest.common import (
    clean,
    fetch,
    fetch_json,
    name_norm,
    now,
    save_raw,
    source_id_for,
    upsert,
)

DOMAIN = "facility"

# --------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------

STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Puerto Rico": "PR",
}
# Wikipedia category naming differs slightly from the plain state name
WIKI_STATE_NAMES = [s for s in STATE_ABBR if s != "District of Columbia"] + ["Washington, D.C."]
WIKI_STATE_NAMES = ["Georgia (U.S. state)" if s == "Georgia" else s for s in WIKI_STATE_NAMES]

WD_SPARQL = "https://query.wikidata.org/sparql"

HIFLD_HOSP = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
              "Hospitals/FeatureServer/0")
HIFLD_PRISON = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
                "prison/FeatureServer/0")

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_PRISON_LISTS = "Lists of United States state prisons"

PSY_CLASSES = ["Q210999", "Q7603700", "Q1516931", "Q2539416"]
PRISON_CLASSES = ["Q40357", "Q115300823", "Q133247580", "Q344836", "Q386432",
                  "Q1006048", "Q61016951", "Q124375691", "Q124375810", "Q594128", "Q6014145"]
JAIL_CLASSES = ["Q12858028", "Q131290639", "Q114713792", "Q20919300"]

LAO_DSH = "https://lao.ca.gov/Publications/Report/4154"   # 2020-21 Budget: DSH analysis
LAO_CDCR = "https://lao.ca.gov/Publications/Report/4145"  # State Correctional Spending...

PSYCH_TITLE_RE = re.compile(
    r"(state hospital|asylum|psychiatric|mental|insane|developmental center|state school|"
    r"sanitari|state colony|state training school|psychopathic)", re.I)

_ACRONYM_OK = {"FCI", "USP", "FDC", "FPC", "FMC", "MCC", "MCF", "MDC", "CI", "SP", "CF",
               "RCC", "DRC", "ICE", "BOP", "CDF", "CIM", "CIF", "VSP", "SCC", "WCC", "SAP",
               "CIW", "HDSP", "CMC", "CAC", "SAC", "CSP", "CTF", "SQF", "ASP", "CEN", "FSP",
               "ISP", "KVI", "MSP", "NSP", "OSP", "QIP", "RJD", "SRJ", "TSP", "WSP", "JCC",
               "MR", "USA", "US", "MC", "NW", "NE", "SW", "SE"}
_STOP_LOWER = {"AT", "OF", "THE", "IN", "ON", "AND", "FOR", "TO", "A", "AN"}


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------

def _num(v):
    """HIFLD uses -999 / 'NOT AVAILABLE' for missing values."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return None if n <= -900 else (n or None)
    s = str(v).strip()
    if not s or s.upper() == "NOT AVAILABLE":
        return None
    try:
        n = int(float(re.sub(r"[^0-9.\-]", "", s) or "0"))
    except ValueError:
        return None
    return None if n <= -900 else (n or None)


def _year(v):
    if v is None:
        return None
    m = re.search(r"\b(1[6-9]\d\d|20[0-4]\d)\b", str(v))
    return int(m.group(1)) if m else None


def _money(v):
    """'$12.3 million' / '$1,234,567' -> int dollars (None when not parseable)."""
    if not v:
        return None
    s = str(v)
    m = re.search(r"\$\s*([\d.,]+)\s*(billion|million|thousand)?", s, re.I)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    mult = {"billion": 1e9, "million": 1e6, "thousand": 1e3}.get(unit, 1)
    val = int(round(n * mult))
    return val if val >= 1000 else None


def smart_title(s: str) -> str:
    out = []
    for i, tok in enumerate((s or "").split()):
        # hyphenated names: "STATE-HOSPITAL" -> "State-Hospital"
        if "-" in tok and not re.search(r"\d-\d", tok):
            left, _, right = tok.partition("-")
            tok = f"{smart_title(left)} - {smart_title(right)}"
            out.append(tok)
            continue
        core = tok.strip(".,()[]")
        if core and core.isupper() and len(core) <= 4 and core in _ACRONYM_OK:
            out.append(tok)
        elif core and core.upper() in _STOP_LOWER and i > 0:
            out.append(core.lower())
        elif tok.isupper():
            out.append(tok.capitalize())
        else:
            out.append(tok)
    return " ".join(out)


_WIKI_STATE_FIXES = {
    "Washington, D.C.": "District of Columbia",
    "Washington DC": "District of Columbia",
    "Georgia (U.S. state)": "Georgia",
}


def _norm_state_name(label: str) -> str:
    label = re.sub(r"\s+", " ", label or "").strip()
    label = label.replace("(U.S. state)", "").strip()
    label = re.sub(r"^State of ", "", label)
    return _WIKI_STATE_FIXES.get(label, label)


def _state_from_list_title(title: str):
    """'List of Georgia state prisons' -> 'GA'."""
    t = re.sub(r"^List of ", "", title or "")
    for suf in (" state correctional facilities", " state prisons", " prisons and jails",
                " state penitentiary", " prisons", " state correctional institution",
                " state hospital", " state hospitals"):
        if t.lower().endswith(suf):
            t = t[: -len(suf)]
            break
    t = t.replace("U.S. ", "").replace("United States ", "").strip()
    return STATE_ABBR.get(_norm_state_name(t))


def _state_from_label(label: str | None):
    if not label:
        return None
    label = re.sub(r"\s+", " ", label).strip()
    if _norm_state_name(label) in STATE_ABBR:
        return STATE_ABBR[_norm_state_name(label)]
    # "List of Georgia state prisons", "Georgia state prisons"
    m = re.match(r"^(?:List of )?(.+?) (?:state )?(?:prisons|psychiatric hospitals)$", label)
    if m and _norm_state_name(m.group(1)) in STATE_ABBR:
        return STATE_ABBR[_norm_state_name(m.group(1))]
    # longest state names first so "West Virginia" wins over "Virginia"
    for nm in sorted(STATE_ABBR, key=len, reverse=True):
        if re.search(r"(^|[,\s])" + re.escape(nm) + r"$", label) or label.endswith(", " + nm):
            return STATE_ABBR[nm]
    for nm in sorted(STATE_ABBR, key=len, reverse=True):
        if nm in ("Washington", "New York", "Georgia", "Kansas", "Mississippi", "Missouri"):
            continue  # ambiguous as bare substrings (cities named after states)
        if re.search(r"\b" + re.escape(nm) + r"\b", label):
            return STATE_ABBR[nm]
    return None


def _us_coords(lat, lon):
    """Reject coordinates outside US states/territories (bad infobox/coord parses)."""
    if lat is None or lon is None:
        return None, None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    boxes = [
        (24.0, 50.0, -125.5, -66.0),      # contiguous US
        (51.0, 72.0, -180.0, -129.0),     # Alaska
        (18.5, 22.5, -161.0, -154.0),     # Hawaii
        (17.5, 18.6, -68.0, -65.0),       # Puerto Rico / USVI
        (13.0, 21.0, 144.0, 146.2),       # Guam / CNMI
        (-15.0, -13.0, -171.5, -170.0),   # American Samoa
    ]
    for la0, la1, lo0, lo1 in boxes:
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return lat, lon
    return None, None


def _km(a, b, c, d):
    return math.hypot((a - c) * 111.0, (b - d) * 111.0 * math.cos(math.radians(a)))


def _strip_refs(wt: str) -> str:
    wt = re.sub(r"<ref[^>]*?/>", "", wt, flags=re.I)
    wt = re.sub(r"<ref[^>]*>.*?</ref>", "", wt, flags=re.S | re.I)
    wt = re.sub(r"<!--.*?-->", "", wt, flags=re.S)
    return wt


def _clean_wiki_value(v: str | None) -> str | None:
    if not v:
        return None
    v = _strip_refs(v)
    v = re.sub(r"\{\{[^{}]*\}\}", " ", v)
    v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"'''?|<[^>]+>", "", v)
    v = re.sub(r"\s+", " ", v).strip(" ,;:|")
    return clean(v)


# --------------------------------------------------------------------------------------
# HIFLD layers
# --------------------------------------------------------------------------------------

def _arcgis(conn, base, where, order_field, tag, page=1000):
    rows = []
    offset = 0
    while True:
        q = {
            "f": "json", "where": where, "outFields": "*", "returnGeometry": "true",
            "outSR": "4326", "resultOffset": str(offset), "resultRecordCount": str(page),
            "orderByFields": order_field,
        }
        url = f"{base}/query?" + urllib.parse.urlencode(sorted(q.items()))
        j = fetch_json(url, domain=DOMAIN)
        feats = j.get("features") or []
        if not feats:
            break
        save_raw(DOMAIN, f"{tag}_p{offset}.json", json.dumps(feats).encode())
        rows.extend(feats)
        if len(feats) < page:
            break
        offset += page
    return rows


def hifld_records(conn):
    """Current-inventory institutions from the two HIFLD layers."""
    recs = []
    sid_hosp = source_id_for(conn, HIFLD_HOSP, DOMAIN,
                             title="HIFLD Hospitals (ArcGIS FeatureServer)",
                             publisher="DHS Homeland Infrastructure Foundation-Level Data",
                             license="US Government open data (public domain)")
    sid_pris = source_id_for(conn, HIFLD_PRISON, DOMAIN,
                             title="HIFLD Prisons (ArcGIS FeatureServer)",
                             publisher="DHS Homeland Infrastructure Foundation-Level Data",
                             license="US Government open data (public domain)")

    hosp = _arcgis(conn, HIFLD_HOSP, "TYPE='PSYCHIATRIC' AND OWNER LIKE 'GOVERNMENT%'",
                   "FID", "hifld_hosp")
    for f in hosp:
        a = f.get("attributes") or {}
        g = f.get("geometry") or {}
        lat = g.get("y") if g else None
        lon = g.get("x") if g else None
        owner = clean(a.get("OWNER"))
        kind = "state_psychiatric" if (owner or "").upper() == "GOVERNMENT - STATE" else "psych_unit"
        if re.search(r"university|ucla|uc |veterans|va ", (a.get("NAME") or ""), re.I):
            kind = "psych_unit"
        status = {"OPEN": "open", "CLOSED": "closed"}.get((a.get("STATUS") or "").upper(), "unknown")
        vintage = _year(a.get("VAL_DATE")) or _year(a.get("SOURCEDATE"))
        notes = [f"HIFLD Hospitals layer (record vintage {vintage or 'n/a'}); owner {owner or 'n/a'}"]
        if (a.get("STATUS") or "").upper() not in ("OPEN", "CLOSED"):
            notes.append(f"HIFLD status field '{a.get('STATUS')}'")
        if owner and owner.upper() != "GOVERNMENT - STATE":
            notes.append("not a state-operated hospital (federal/local government owner)")
        recs.append({
            "name": smart_title(clean(a.get("NAME")) or ""),
            "kind": kind, "status": status,
            "beds": _num(a.get("BEDS")), "beds_year": vintage,
            "staff": _num(a.get("TTL_STAFF")), "staff_year": vintage,
            "state": (a.get("STATE") or "").strip() or None,
            "city": smart_title(clean(a.get("CITY")) or "") or None,
            "address": clean(a.get("ADDRESS")), "lat": lat, "lon": lon,
            "operator": None, "owner": owner, "opened": None, "closed": None,
            "budget": None, "budget_year": None,
            "notes": notes, "source_id": sid_hosp, "is_est": 0,
            "provenance": "hifld_hospitals",
        })

    pris = _arcgis(
        conn, HIFLD_PRISON,
        "((TYPE IN ('STATE','FEDERAL')) OR (TYPE IN ('COUNTY','LOCAL') AND CAPACITY>=250)) "
        "AND SECURELVL<>'JUVENILE'",
        "OBJECTID_1", "hifld_prison")
    for f in pris:
        a = f.get("attributes") or {}
        g = f.get("geometry") or {}
        lat = g.get("y") if g else None
        lon = g.get("x") if g else None
        typ = (a.get("TYPE") or "").upper()
        kind = "jail" if typ in ("COUNTY", "LOCAL") else "prison"
        if re.search(r"psychiatric|mental health|state hospital", (a.get("NAME") or ""), re.I):
            kind = "state_psychiatric"
        status = {"OPEN": "open", "CLOSED": "closed"}.get((a.get("STATUS") or "").upper(), "unknown")
        vintage = _year(a.get("VAL_DATE")) or _year(a.get("SOURCEDATE"))
        cap = _num(a.get("CAPACITY"))
        pop = _num(a.get("POPULATION"))
        notes = [f"HIFLD Prisons layer (record vintage {vintage or 'n/a'}); type {typ or 'n/a'}, "
                 f"security {a.get('SECURELVL') or 'n/a'}"]
        if pop:
            notes.append(f"reported population/custody count {pop} (same vintage)")
        if a.get("STATUS") and (a.get("STATUS") or "").upper() not in ("OPEN", "CLOSED"):
            notes.append(f"HIFLD status field '{a.get('STATUS')}'")
        recs.append({
            "name": smart_title(clean(a.get("NAME")) or ""),
            "kind": kind, "status": status,
            "beds": cap, "beds_year": vintage if cap else None,
            "capacity_est": None,
            "staff": None, "staff_year": None,
            "state": (a.get("STATE") or "").strip() or None,
            "city": smart_title(clean(a.get("CITY")) or "") or None,
            "address": clean(a.get("ADDRESS")), "lat": lat, "lon": lon,
            "operator": None, "owner": None, "opened": None, "closed": None,
            "budget": None, "budget_year": None,
            "notes": notes, "source_id": sid_pris, "is_est": 0,
            "provenance": "hifld_prisons",
        })
    return recs


# --------------------------------------------------------------------------------------
# Wikidata
# --------------------------------------------------------------------------------------

def _sparql(query):
    url = WD_SPARQL
    body = urllib.parse.urlencode({"query": query, "format": "json"}).encode()
    raw = fetch(url, method="POST", data=body, domain=DOMAIN,
                headers={"Accept": "application/sparql-results+json",
                         "Content-Type": "application/x-www-form-urlencoded"},
                timeout=300)
    return json.loads(raw)


def _wd_rows(query):
    j = _sparql(query)
    out = []
    for b in j.get("results", {}).get("bindings", []):
        out.append({k: v.get("value") for k, v in b.items()})
    return out


def _state_class_qids(conn):
    rows = _wd_rows("SELECT ?s WHERE { ?s wdt:P31 wd:Q35657 . }")
    qids = [r["s"].rsplit("/", 1)[-1] for r in rows]
    qids.append("Q61")  # District of Columbia
    return qids


def wikidata_records(conn):
    """US institutions of the psychiatric / prison / jail classes, with dates and capacity."""
    states = _state_class_qids(conn)
    sv = " ".join(f"wd:{q}" for q in states)

    def run(classes, kind_default):
        vals = " ".join(f"wd:{q}" for q in classes)
        query = f"""
SELECT ?i ?label ?walabel
       (SAMPLE(?typeLabel) AS ?type)
       (MIN(?inception) AS ?incep) (MIN(?dissolved) AS ?diss)
       (SAMPLE(?cap) AS ?cap) (SAMPLE(?coords) AS ?coord)
       (SAMPLE(?opLabel) AS ?op) (SAMPLE(?ownerLabel) AS ?owner)
       (SAMPLE(?adminLabel) AS ?admin) (SAMPLE(?emp) AS ?emp) (SAMPLE(?bud) AS ?bud)
WHERE {{
  VALUES ?t {{ {vals} }}
  ?i wdt:P31 ?t .
  {{ ?i wdt:P17 wd:Q30 . }}
  UNION {{ ?i wdt:P131 ?st . VALUES ?st {{ {sv} }} }}
  UNION {{ ?i wdt:P131 ?adm0 . ?adm0 wdt:P131 ?st . VALUES ?st {{ {sv} }} }}
  UNION {{ ?i wdt:P131 ?adm0 . ?adm0 wdt:P131 ?adm1 . ?adm1 wdt:P131 ?st . VALUES ?st {{ {sv} }} }}
  ?i rdfs:label ?label . FILTER(lang(?label)="en")
  OPTIONAL {{ ?i wdt:P31 ?x . ?x rdfs:label ?typeLabel . FILTER(lang(?typeLabel)="en") }}
  OPTIONAL {{ ?i wdt:P571 ?inception }}
  OPTIONAL {{ ?i wdt:P576 ?dissolved }}
  OPTIONAL {{ ?i wdt:P1083 ?cap }}
  OPTIONAL {{ ?i wdt:P625 ?coords }}
  OPTIONAL {{ ?i wdt:P131 ?adm . ?adm rdfs:label ?adminLabel . FILTER(lang(?adminLabel)="en") }}
  OPTIONAL {{ ?i wdt:P137 ?op . ?op rdfs:label ?opLabel . FILTER(lang(?opLabel)="en") }}
  OPTIONAL {{ ?i wdt:P127 ?own . ?own rdfs:label ?ownerLabel . FILTER(lang(?ownerLabel)="en") }}
  OPTIONAL {{ ?i wdt:P1128 ?emp }}
  OPTIONAL {{ ?i wdt:P2769 ?bud }}
}} GROUP BY ?i ?label ?walabel
"""
        return _wd_rows(query)

    records = []

    def collect(classes, kind_default, provenance, kind_map):
        for r in run(classes, kind_default):
            qid = r["i"].rsplit("/", 1)[-1]
            label = clean(r.get("label"))
            if not label:
                continue
            typ = (r.get("type") or "").lower()
            kind = kind_default
            for needle, k in kind_map.items():
                if needle in typ:
                    kind = k
                    break
            if re.search(r"\bjail\b", label, re.I):
                kind = "jail"
            elif re.search(r"\bpsychiatric hospital\b", typ, re.I) and kind == "other":
                kind = "state_psychiatric"
            state = _state_from_label(r.get("admin")) or _state_from_label(r.get("walabel"))
            lat = lon = None
            coord = r.get("coord") or ""
            m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", coord)
            if m:
                lat, lon = _us_coords(m.group(2), m.group(1))
            opened = _year(r.get("incep"))
            closed = _year(r.get("diss"))
            status = "closed" if closed else ("open" if opened else "unknown")
            notes = [f"Wikidata {qid}" + (f"; instance of {r.get('type')}" if r.get("type") else "")]
            if closed:
                notes.append(f"dissolved/closed date {r.get('diss')[:10]}")
            if opened:
                notes.append(f"inception {r.get('incep')[:10]}")
            cap = None
            if r.get("cap"):
                try:
                    cap = int(float(r["cap"]))
                except ValueError:
                    cap = None
            records.append({
                "name": label, "kind": kind, "status": status,
                "opened": opened, "closed": closed,
                "beds": cap, "beds_year": None,
                "staff": int(float(r["emp"])) if (r.get("emp") or "").replace(".", "").isdigit() else None,
                "staff_year": None,
                "budget": int(float(r["bud"])) if (r.get("bud") or "").replace(".", "").isdigit() else None,
                "budget_year": None,
                "state": state, "city": clean(r.get("admin")),
                "address": None, "lat": lat, "lon": lon,
                "operator": clean(r.get("op")), "owner": clean(r.get("owner")),
                "notes": notes, "source_id": None, "is_est": 0,
                "provenance": provenance, "qid": qid,
            })

    collect(PSY_CLASSES, "state_psychiatric", "wikidata_psych",
            {"lunatic asylum": "state_psychiatric", "forensic": "psych_unit",
             "state hospital": "state_psychiatric", "psychiatric hospital": "state_psychiatric"})
    collect(PRISON_CLASSES, "prison", "wikidata_prison",
            {"jail": "jail", "detention": "jail", "prison": "prison", "penitentiary": "prison"})
    collect(JAIL_CLASSES, "jail", "wikidata_jail", {"jail": "jail", "prison": "prison"})
    return records


# --------------------------------------------------------------------------------------
# Wikipedia
# --------------------------------------------------------------------------------------

def _wiki(params):
    q = {"format": "json", "formatversion": "2"}
    q.update(params)
    url = WIKI_API + "?" + urllib.parse.urlencode(sorted(q.items()))
    return fetch_json(url, domain=DOMAIN)


def wiki_categories(conn):
    """Harvest institution article titles + hints from per-state Wikipedia categories."""
    patterns = [
        ("Category:Psychiatric hospitals in {s}", "state_psychiatric", False),
        ("Category:Prisons in {s}", "prison", False),
        ("Category:Jails in {s}", "jail", False),
        ("Category:Defunct prisons in {s}", "prison", True),
        ("Category:Defunct hospitals in {s}", None, True),  # filtered by title below
    ]
    hints = {}

    def add(title, kind, state, defunct):
        if not title or title.startswith("Category:") or title.startswith("List of"):
            return
        if title not in hints:
            hints[title] = {"kind": kind, "state": state, "defunct": defunct}

    for sname in WIKI_STATE_NAMES:
        state = _state_from_label(sname)
        if not state:
            continue
        for pat, kind, defunct in patterns:
            cat = pat.format(s=sname)
            j = _wiki({"action": "query", "list": "categorymembers", "cmtitle": cat,
                       "cmlimit": "500", "cmtype": "page|subcat"})
            members = (j.get("query") or {}).get("categorymembers") or []
            for m in members:
                t = m.get("title") or ""
                if t.startswith("Category:"):
                    # one level of descent (e.g. "Defunct prisons in X" -> county subcats)
                    j2 = _wiki({"action": "query", "list": "categorymembers", "cmtitle": t,
                                "cmlimit": "500", "cmtype": "page"})
                    for m2 in (j2.get("query") or {}).get("categorymembers") or []:
                        t2 = m2.get("title") or ""
                        if kind is None and not PSYCH_TITLE_RE.search(t2):
                            continue
                        add(t2, kind or "state_psychiatric", state, defunct)
                    continue
                if kind is None and not PSYCH_TITLE_RE.search(t):
                    continue
                add(t, kind or "state_psychiatric", state, defunct)

    # curated pan-US categories for historic asylums
    for cat, kind, defunct in [("Category:Kirkbride Plan hospitals", "state_psychiatric", True)]:
        j = _wiki({"action": "query", "list": "categorymembers", "cmtitle": cat,
                   "cmlimit": "500", "cmtype": "page"})
        for m in (j.get("query") or {}).get("categorymembers") or []:
            add(m.get("title"), kind, None, defunct)
    return hints


def wiki_pages(conn, titles):
    """Fetch wikitext + coordinates for article titles (50 per request)."""
    pages = {}
    titles = sorted(t for t in titles if t and ":" not in t.split("(")[0])
    for i in range(0, len(titles), 40):
        batch = titles[i:i + 40]
        j = _wiki({"action": "query", "prop": "revisions|coordinates|categories",
                   "rvprop": "content", "cllimit": "100",
                   "rvslots": "main", "titles": "|".join(batch), "redirects": "1"})
        q = j.get("query") or {}
        norm = {}
        for n in q.get("normalized") or []:
            norm[n.get("to")] = n.get("from")
        redir = {}
        for rd in q.get("redirects") or []:
            redir[rd.get("to")] = rd.get("from")
        for p in q.get("pages") or []:
            title = p.get("title")
            if not title or p.get("missing"):
                continue
            revs = p.get("revisions") or []
            wt = None
            if revs:
                slots = revs[0].get("slots") or {}
                wt = (slots.get("main") or {}).get("content")
            coords = p.get("coordinates") or []
            pages[title] = {
                "title": title,
                "wikitext": wt,
                "lat": coords[0].get("lat") if coords else None,
                "lon": coords[0].get("lon") if coords else None,
                "cats": [c.get("title") for c in (p.get("categories") or [])],
                "aliases": [a for a in (norm.get(title), redir.get(title)) if a],
            }
    return pages


def parse_infobox(wt):
    if not wt:
        return {}
    m = re.search(r"\{\{\s*Infobox[^\n]*\n", wt, re.I)
    if not m:
        return {}
    body = wt[m.end():]
    out = {}
    depth = 0
    for line in body.split("\n"):
        if line.startswith("}}") and depth <= 0:
            break
        mm = re.match(r"\s*\|\s*([\w_ \-]+?)\s*=\s*(.*)$", line)
        if mm:
            out[mm.group(1).strip().lower().replace(" ", "_")] = mm.group(2).strip()
        if "{{" in line:
            depth += line.count("{{") - line.count("}}")
        if depth < 0:
            break
    return out


def _first(ib, keys):
    for k in keys:
        if ib.get(k):
            return ib[k]
    return None


def record_from_page(conn, page, hint):
    wt = page.get("wikitext") or ""
    ib = parse_infobox(wt)
    title = page["title"]
    name = re.sub(r"\s*\((?:[^)]*)\)\s*$", "", title)
    state = (hint or {}).get("state") or _state_from_label(title)
    kind = (hint or {}).get("kind")
    if not kind:
        kind = "state_psychiatric" if PSYCH_TITLE_RE.search(title) else "other"

    opened = _year(_first(ib, ["founded", "opened", "established", "opening_date",
                               "opened_date", "construction_began"]))
    closed = _year(_first(ib, ["closed", "closing_date", "closed_date", "dissolved"]))
    beds = _num(_first(ib, ["beds", "beds_current", "capacity", "bed_count",
                            "number_of_beds"]))
    operator = _clean_wiki_value(_first(ib, ["operator", "managed_by", "management",
                                            "operated_by", "oversight"]))
    owner = _clean_wiki_value(_first(ib, ["owner", "ownership", "parent"]))
    staff = _num(_first(ib, ["employees", "staff", "number_of_employees"]))
    budget = _money(_first(ib, ["budget", "annual_budget", "operating_budget"]))
    budget_year = _year(_first(ib, ["budget_year", "fiscal_year"])) or closed

    notes = [f"Wikipedia article '{title}'"]
    if budget is None:
        # look for a sourced annual budget / cost sentence in the article body
        txt = _strip_refs(wt)
        for m in re.finditer(r"[^.\n]{0,200}\$[\d.,]+\s*(?:million|billion)?[^.\n]{0,200}\.", txt):
            s = re.sub(r"\s+", " ", m.group(0))
            if re.search(r"budget|operat|annual(ly)? cost|per patient|per year|spend", s, re.I):
                b = _money(s)
                if b:
                    budget = b
                    budget_year = _year(s) or budget_year
                    notes.append(f"budget figure from article text: \"{s[:220]}\"")
                    break

    if not state:
        for cat in page.get("cats") or []:
            c = re.sub(r"^Category:", "", cat)
            m = re.search(r"\bin ([A-Z][\w .,()'\-]+)$", c)
            if m:
                state = _state_from_label(m.group(1))
                if state:
                    break

    lat, lon = _us_coords(page.get("lat"), page.get("lon"))
    if lat is None:
        cm = re.search(r"\{\{\s*coord\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)", wt, re.I)
        if cm:
            lat, lon = _us_coords(cm.group(1), cm.group(2))

    status = None
    if closed:
        status = "closed"
    elif (hint or {}).get("defunct"):
        status = "closed"
        notes.append("listed in a Wikipedia 'Defunct ...' category")

    return {
        "name": clean(name), "kind": kind, "status": status,
        "opened": opened, "closed": closed, "beds": beds, "beds_year": None,
        "capacity_est": None, "staff": staff, "staff_year": None,
        "state": state, "city": _clean_wiki_value(_first(ib, ["location_city", "city"])),
        "address": _clean_wiki_value(_first(ib, ["location", "address"])),
        "lat": lat, "lon": lon, "operator": operator, "owner": owner,
        "budget": budget, "budget_year": budget_year,
        "notes": notes, "source_id": None, "is_est": 0, "provenance": "wikipedia_page",
        "wiki_title": title,
    }


def wiki_prison_lists(conn):
    """Capacity / opening / closing years from the per-state 'List of ... prisons' tables."""
    import io

    import pandas as pd

    recs = []
    j = _wiki({"action": "parse", "page": WIKI_PRISON_LISTS, "prop": "wikitext"})
    wt = (j.get("parse") or {}).get("wikitext") or ""
    titles = re.findall(r"\[\[(List of [^\]|#]+)", wt)
    titles = [t for t in titles if re.search(r"prison|correctional", t, re.I)]
    titles += ["List of U.S. federal prisons"]
    seen = set()
    for title in titles:
        if title in seen:
            continue
        seen.add(title)
        page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        try:
            html = fetch(page_url, domain=DOMAIN).decode("utf-8", "replace")
            tables = pd.read_html(io.StringIO(html))
        except Exception:
            continue
        state = _state_from_list_title(title)
        if not state and "federal" in title.lower():
            state = "US"
        sid = source_id_for(conn, page_url, DOMAIN, title=title, publisher="Wikipedia",
                            license="CC BY-SA 4.0")
        for t in tables:
            if t.shape[0] < 2 or t.shape[1] < 2:
                continue
            cols = [re.sub(r"\[.*?\]", "", str(c)).strip() for c in t.columns]
            ci = {c.lower(): i for i, c in enumerate(cols)}

            def find(*pats):
                for pat in pats:
                    for c, i in ci.items():
                        if re.search(pat, c):
                            return i
                return None

            i_name = find(r"^prison", r"^facility", r"^name", r"institution", r"^location name")
            i_cap = find(r"capacity", r"^beds", r"design capacity", r"inmates")
            i_open = find(r"^opened", r"^established", r"year opened", r"^open")
            i_closed = find(r"^closed", r"^closure", r"date closed")
            i_city = find(r"^city", r"^location", r"^town", r"^address")
            i_county = find(r"^county")
            if i_city is None and i_county is not None:
                i_county_only = i_county
            else:
                i_county_only = None
            if i_name is None or i_cap is None:
                continue
            for _, row in t.iterrows():
                raw_name = row.iloc[i_name]
                if not isinstance(raw_name, str) or len(raw_name.strip()) < 4:
                    continue
                nm = re.sub(r"\[.*?\]", "", raw_name).strip()
                if re.match(r"(?i)^(prison|facility|name|total|region)", nm):
                    continue
                cap = _num(row.iloc[i_cap])
                opened = _year(row.iloc[i_open]) if i_open is not None else None
                closed = _year(row.iloc[i_closed]) if i_closed is not None else None
                city = None
                if i_city is not None and isinstance(row.iloc[i_city], str):
                    city = re.sub(r"\[.*?\]", "", row.iloc[i_city]).strip()
                roster = (i_closed is None) and not re.search(r"closed", nm, re.I)
                notes = [f"Wikipedia table '{title}'"]
                if roster and not closed:
                    notes.append("appears on the current state-prison roster in the table")
                if i_county_only is not None and isinstance(row.iloc[i_county_only], str):
                    cty = re.sub(r"\[.*?\]", "", row.iloc[i_county_only]).strip()
                    if cty and cty.lower() != "nan":
                        notes.append(f"county: {cty}")
                if closed:
                    notes.append(f"table lists closure year {closed}")
                recs.append({
                    "name": clean(nm), "kind": "prison",
                    "status": "closed" if closed else ("open" if roster else None),
                    "opened": opened, "closed": closed,
                    "beds": cap, "beds_year": None, "capacity_est": None,
                    "staff": None, "staff_year": None,
                    "state": state, "city": clean(city), "address": None,
                    "lat": None, "lon": None, "operator": None, "owner": None,
                    "budget": None, "budget_year": None,
                    "notes": notes, "source_id": sid, "is_est": 0,
                    "provenance": "wikipedia_prison_list",
                })
    return recs


# --------------------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------------------

FILL_FIELDS = ["opened", "closed", "beds", "beds_year", "staff", "staff_year", "capacity_est",
               "operator", "owner", "budget", "budget_year", "address", "lat", "lon",
               "city", "state"]


def _key(rec):
    return (name_norm(rec.get("name")), (rec.get("state") or "").upper(),
            name_norm(rec.get("city")))


def _similar(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _merge_into(cur, rec):
    """Fill gaps in `cur` from `rec`; keep the best-sourced status."""
    for f in FILL_FIELDS:
        if cur.get(f) in (None, "") and rec.get(f) not in (None, ""):
            cur[f] = rec[f]
    if rec.get("closed") and not cur.get("closed"):
        cur["closed"] = rec["closed"]
    if rec.get("status") == "closed" and cur.get("status") in (None, "unknown"):
        cur["status"] = "closed"
    if rec.get("status") == "closed" and cur.get("status") == "open" and rec.get("closed"):
        cur["status"] = "partial"
        cur.setdefault("notes", []).append(
            "current-inventory source lists the site as open but another source records "
            f"the institution closing in {rec['closed']} (site possibly repurposed)")
    for n in rec.get("notes") or []:
        if n not in cur.setdefault("notes", []):
            cur["notes"].append(n)
    cur["is_est"] = max(cur.get("is_est") or 0, rec.get("is_est") or 0)
    if cur.get("source_id") is None and rec.get("source_id") is not None:
        cur["source_id"] = rec["source_id"]
    if rec.get("wiki_title"):
        cur.setdefault("wiki_titles", []).append(rec["wiki_title"])


def consolidate(merger):
    """Final pass: one row per (name_norm, state). HIFLD records carry the current status,
    bed count and coordinates, so they are preferred as the base record."""
    groups = {}
    for rec in merger.recs.values():
        if not rec.get("name"):
            continue
        groups.setdefault((name_norm(rec["name"]), (rec.get("state") or "")), []).append(rec)
    out = {}
    merged = 0
    for gk, recs in groups.items():
        if len(recs) == 1:
            out[gk] = recs[0]
            continue
        recs.sort(key=lambda r: (0 if (r.get("provenance") or "").startswith("hifld") else 1,
                                 r.get("status") not in ("open", "closed", "partial")))
        base = recs[0]
        for other in recs[1:]:
            _merge_into(base, other)
        if base.get("status") == "unknown":
            for other in recs[1:]:
                if other.get("status") in ("open", "closed", "partial"):
                    base["status"] = other["status"]
                    break
        out[gk] = base
        merged += len(recs) - 1
    merger.recs = {k: v for k, v in out.items()}
    return merged


class Merger:
    def __init__(self):
        self.recs: dict[tuple, dict] = {}
        self.grid: dict[tuple, list] = {}

    def _grid_keys(self, rec):
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None:
            return []
        out = []
        for dlat in (-0.05, 0.0, 0.05):
            for dlon in (-0.05, 0.0, 0.05):
                out.append((rec.get("state"),
                            round(math.floor((lat + dlat) / 0.05)),
                            round(math.floor((lon + dlon) / 0.05))))
        return out

    def _register_grid(self, rec, key):
        for g in self._grid_keys(rec):
            self.grid.setdefault(g, []).append(key)

    def match(self, rec):
        k = _key(rec)
        if k in self.recs:
            return k
        if k[0]:
            # exactly one existing facility with the same normalised name in the same state:
            # cities routinely differ between sources (county vs city vs mailing address)
            cands = [kk for kk in self.recs if kk[0] == k[0] and kk[1] == k[1]]
            if not cands and k[1]:
                cands = [kk for kk in self.recs if kk[0] == k[0] and not kk[1]]
            if len(cands) == 1:
                return cands[0]
        # geographic proximity + name similarity
        if rec.get("lat") is not None and rec.get("lon") is not None:
            for g in self._grid_keys(rec):
                for kk in self.grid.get(g, []):
                    other = self.recs.get(kk)
                    if not other or rec.get("state") != other.get("state"):
                        continue
                    d = _km(rec["lat"], rec["lon"], other["lat"], other["lon"])
                    if d > 1.2:
                        continue
                    if d <= 0.25 or _similar(k[0], kk[0]) >= 0.55:
                        return kk
        return None

    def add(self, rec):
        if not rec.get("name") or len(rec["name"]) < 3:
            return None
        k = self.match(rec)
        if k is None:
            k = _key(rec)
            if k in self.recs:  # identical key, different object
                k = (k[0], k[1], k[2])
            rec.setdefault("notes", [])
            self.recs[k] = rec
            self._register_grid(rec, k)
            return k
        cur = self.recs[k]
        _merge_into(cur, rec)
        return k


# --------------------------------------------------------------------------------------
# economics
# --------------------------------------------------------------------------------------

def _page_text(url):
    import html as _html
    raw = fetch(url, domain=DOMAIN).decode("utf-8", "replace")
    raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
    txt = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", txt)


def economics(conn):
    """Published operating-economics figures from primary budget documents.

    Returns a list of dicts: {metric, value, unit, year, is_est, note, source_url, selector}
    where selector picks which facilities the figure applies to.
    """
    facts = []

    # ---- California state hospitals: LAO analysis of the 2020-21 DSH budget -------------
    try:
        t = _page_text(LAO_DSH)
        flat = re.sub(r"\s", "", t)
        m = re.search(r"totalexpendituresofabout\$([\d.]+)billion", flat)
        year_m = re.search(r"for(\w{3,8})operationsin(20\d\d)", flat)
        if m:
            total = float(m.group(1)) * 1e9
            facts.append({
                "metric": "_dsh_total", "value": total, "year": 2021,
                "source_url": LAO_DSH,
                "quote": "Governor's budget proposes total expenditures of about $2.3 billion "
                         "($2.1 billion from the General Fund) for DSH operations in 2020-21.",
                "note": "California Department of State Hospitals operational expenditures, "
                        "FY2020-21 (LAO analysis of the 2020-21 budget).",
            })
    except Exception:
        pass

    # ---- California prisons: LAO CDCR spending + inmate population ----------------------
    try:
        t = _page_text(LAO_CDCR)
        flat = re.sub(r"\s", "", t)
        m = re.search(r"fromabout\$9\.7billionin2010‑11toanestimated\$([\d.]+)billionin2019‑20", flat)
        if not m:
            m = re.search(r"toanestimated\$([\d.]+)billionin2019", flat)
        pop = re.search(r"inmatepopulationdeclinedfromabout\s*162,?400\s*to\s*([\d,]+)", flat)
        pop2 = re.search(r"declinedfromabout162,?400to([\d,]+)", flat)
        inmates = None
        for mm in (pop, pop2):
            if mm:
                inmates = int(re.sub(r"[^\d]", "", mm.group(1)))
                break
        if m and inmates:
            facts.append({
                "metric": "_cdcr", "value": float(m.group(1)) * 1e9, "year": 2020,
                "inmates": inmates, "source_url": LAO_CDCR,
                "quote": f"CDCR expenditures estimated at ${m.group(1)} billion in 2019-20; "
                         f"inmate population about {inmates:,} (June 2019).",
                "note": "California Department of Corrections and Rehabilitation expenditures "
                        "FY2019-20 (LAO) and June 2019 inmate population (LAO).",
            })
    except Exception:
        pass

    return facts


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------

def _find_existing(conn, name_n, state, city):
    row = conn.execute(
        "SELECT id FROM facility WHERE name_norm=? AND state IS ? AND city IS ? LIMIT 1",
        (name_n, state, city)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT id FROM facility WHERE name_norm=? AND state IS ? AND (city IS NULL OR city='') "
        "LIMIT 1", (name_n, state)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT id FROM facility WHERE name_norm=? AND state IS ? LIMIT 1",
        (name_n, state)).fetchone()
    return row[0] if row else None


def _write_facility(conn, rec):
    name = clean(rec.get("name"))
    if not name:
        return None
    nn = name_norm(name)
    state = (rec.get("state") or None)
    city = clean(rec.get("city")) or None
    status = rec.get("status") or "unknown"
    if rec.get("closed") and status == "unknown":
        status = "closed"
    if status not in ("open", "closed", "partial", "unknown"):
        status = "unknown"
    row = {
        "name": name, "name_norm": nn, "kind": rec.get("kind") or "other", "status": status,
        "opened": rec.get("opened"), "closed": rec.get("closed"),
        "beds": rec.get("beds"), "capacity_est": rec.get("capacity_est"),
        "operator": clean(rec.get("operator")), "owner": clean(rec.get("owner")),
        "state": state, "city": city, "address": clean(rec.get("address")),
        "lat": rec.get("lat"), "lon": rec.get("lon"),
        "budget": rec.get("budget"), "staff": rec.get("staff"),
        "annual_cost_per_bed": None,
        "population_note": "; ".join(dict.fromkeys(rec.get("notes") or []))[:1200] or None,
        "source_id": rec.get("source_id"), "is_est": int(rec.get("is_est") or 0),
    }
    rid = _find_existing(conn, nn, state, city)
    if rid:
        sets = ", ".join(
            (f"{k}=CASE WHEN ? IN ('open','closed','partial') OR status IS NULL OR status='unknown' "
             f"THEN ? ELSE status END" if k == "status" else f"{k}=COALESCE(?, {k})")
            for k in row if k not in ("name", "name_norm"))
        params = []
        for k, v in row.items():
            if k in ("name", "name_norm"):
                continue
            params.append(v)
            if k == "status":
                params.append(v)
        conn.execute(f"UPDATE facility SET {sets} WHERE id=?", params + [rid])
        return rid
    cur = conn.execute(
        "INSERT INTO facility(name,name_norm,kind,status,opened,closed,beds,capacity_est,"
        "operator,owner,state,city,address,lat,lon,budget,staff,annual_cost_per_bed,"
        "population_note,source_id,is_est) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["name"], row["name_norm"], row["kind"], row["status"], row["opened"],
         row["closed"], row["beds"], row["capacity_est"], row["operator"], row["owner"],
         row["state"], row["city"], row["address"], row["lat"], row["lon"], row["budget"],
         row["staff"], row["annual_cost_per_bed"], row["population_note"],
         row["source_id"], row["is_est"]))
    return cur.lastrowid


def _metric(conn, fid, metric, value, year, unit=None, is_est=0, note=None, source_id=None):
    if value is None or fid is None:
        return
    if year is None:
        year = 0
    conn.execute(
        "INSERT INTO metric(entity_type,entity_id,metric,value,unit,year,is_est,note,source_id) "
        "VALUES('facility',?,?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id,metric,year) "
        "DO UPDATE SET value=excluded.value, unit=COALESCE(excluded.unit,metric.unit), "
        "is_est=excluded.is_est, note=COALESCE(excluded.note,metric.note), "
        "source_id=COALESCE(excluded.source_id,metric.source_id)",
        (str(fid), metric, float(value), unit, int(year), int(is_est), note, source_id))


# --------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------

def run(conn, cfg):
    cfg = cfg or {}
    notes = []
    merger = Merger()

    # 1. current inventory ---------------------------------------------------------------
    hifld = hifld_records(conn)
    for rec in hifld:
        merger.add(rec)
    notes.append(f"hifld={len(hifld)}")

    # 2. Wikipedia prison list tables (capacity + opening/closing years) ------------------
    lists = wiki_prison_lists(conn)
    for rec in lists:
        merger.add(rec)
    notes.append(f"wiki_lists={len(lists)}")

    # 3. Wikidata items ------------------------------------------------------------------
    wd = wikidata_records(conn)
    wd_url = "https://query.wikidata.org/sparql"
    sid_wd = source_id_for(conn, wd_url, DOMAIN, title="Wikidata Query Service (SPARQL)",
                           publisher="Wikidata", license="CC0 1.0")
    for rec in wd:
        rec["source_id"] = sid_wd
        merger.add(rec)
    notes.append(f"wikidata={len(wd)}")

    # 4. Wikipedia articles (categories + infoboxes) -------------------------------------
    hints = wiki_categories(conn)
    pages = wiki_pages(conn, list(hints))
    wp = 0
    for title, page in pages.items():
        hint = hints.get(title)
        aliases = [a for a in ([title] + (page.get("aliases") or [])) if a]
        rec = record_from_page(conn, page, hint)
        if hint and hint.get("state"):
            rec["state"] = hint["state"]
        if hint and hint.get("kind"):
            rec["kind"] = hint["kind"]
        if not rec.get("state"):
            for al in aliases:
                s = _state_from_label(al)
                if s:
                    rec["state"] = s
                    break
        page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        if rec.get("beds") or rec.get("opened") or rec.get("closed") or rec.get("budget"):
            rec["source_id"] = source_id_for(conn, page_url, DOMAIN, title=title,
                                             publisher="Wikipedia", license="CC BY-SA 4.0")
        else:
            sid = source_id_for(conn, WIKI_API, DOMAIN,
                                title="Wikipedia API (category members)",
                                publisher="Wikipedia", license="CC BY-SA 4.0")
            rec["source_id"] = sid
        if merger.add(rec) is not None:
            wp += 1
    notes.append(f"wiki_pages={len(pages)}/{len(hints)}")

    # 5. deduplicate by name within state (safety net) -----------------------------------
    seen_names = {}
    for k in list(merger.recs):
        rec = merger.recs[k]
        nk = (name_norm(rec.get("name")), (rec.get("state") or ""))
        if not nk[0]:
            continue
        if nk in seen_names:
            other = merger.recs.get(seen_names[nk])
            if other is not None and other is not rec:
                for f in FILL_FIELDS:
                    if other.get(f) in (None, "") and rec.get(f) not in (None, ""):
                        other[f] = rec[f]
                continue
        seen_names[nk] = k

    # 5b. backfill missing states from nearby facilities with a known state ---------------
    refs = [(r["lat"], r["lon"], r["state"]) for r in merger.recs.values()
            if r.get("state") and r.get("lat") is not None and r.get("lon") is not None]
    filled = 0
    if refs:
        rg = {}
        for la, lo, st in refs:
            rg.setdefault((round(math.floor(la / 0.5)), round(math.floor(lo / 0.5))), []).append(
                (la, lo, st))
        for rec in merger.recs.values():
            if rec.get("state") or rec.get("lat") is None or rec.get("lon") is None:
                continue
            la, lo = rec["lat"], rec["lon"]
            cands = []
            for dla in (-1, 0, 1):
                for dlo in (-1, 0, 1):
                    cands.extend(rg.get((round(math.floor(la / 0.5)) + dla,
                                         round(math.floor(lo / 0.5)) + dlo), []))
            best = min(cands, key=lambda x: _km(la, lo, x[0], x[1]), default=None)
            if best and _km(la, lo, best[0], best[1]) <= 80:
                rec["state"] = best[2]
                rec.setdefault("notes", []).append(
                    f"state inferred from coordinates (nearest mapped facility is in {best[2]}, "
                    f"{_km(la, lo, best[0], best[1]):.0f} km away)")
                filled += 1
    notes.append(f"states_backfilled={filled}")
    collapsed = consolidate(merger)
    notes.append(f"merged_duplicate_records={collapsed}")

    # 5c. scope check: psychiatric facilities with no government operator evidence --------
    for rec in merger.recs.values():
        if rec.get("kind") != "state_psychiatric":
            continue
        op = f"{rec.get('operator') or ''} {rec.get('owner') or ''}".strip()
        opl = op.lower()
        gov = bool(re.search(r"state|county|city|federal|govern|department|commonwealth|"
                             r"district|authority|university|veterans|health system|hospital "
                             r"district", opl))
        if op and not gov and rec.get("status") != "closed" and not rec.get("closed"):
            # affirmative evidence of a non-government operator: not a state hospital
            rec["kind"] = "other"
            rec.setdefault("notes", []).append(
                f"operator/owner recorded as '{op}' (not a government entity) and the facility is "
                "not recorded as closed: classified 'other', not a state psychiatric hospital")
        elif not op and not re.search(r"\bstate\b", rec.get("name") or "", re.I):
            rec.setdefault("notes", []).append(
                "type classified from the Wikidata class / Wikipedia category; the sources record "
                "no operator or owner for this facility")

    # 6. write facilities ----------------------------------------------------------------
    ids = {}
    rows = 0
    for k, rec in merger.recs.items():
        fid = _write_facility(conn, rec)
        if fid:
            ids[k] = fid
            rows += 1
    rows = len(set(ids.values()))
    notes.append(f"facilities={rows}")

    # 7. metrics -------------------------------------------------------------------------
    def by_kind_state(kind=None, state=None):
        for k, rec in merger.recs.items():
            if kind and rec.get("kind") != kind:
                continue
            if state and rec.get("state") != state:
                continue
            if k in ids:
                yield k, rec

    n_beds = n_staff = n_budget = n_cost = 0
    for k, rec in list(merger.recs.items()):
        fid = ids.get(k)
        if not fid:
            continue
        if rec.get("beds"):
            _metric(conn, fid, "facility_beds", rec["beds"], rec.get("beds_year") or 0,
                    unit="beds", is_est=int(rec.get("is_est") or 0),
                    note="bed/capacity count as reported by the cited inventory source",
                    source_id=rec.get("source_id"))
            n_beds += 1
        if rec.get("staff"):
            _metric(conn, fid, "facility_staff", rec["staff"], rec.get("staff_year") or 0,
                    unit="staff", note="staffing count as reported by the cited source",
                    source_id=rec.get("source_id"))
            n_staff += 1
        if rec.get("budget"):
            _metric(conn, fid, "facility_annual_budget", rec["budget"],
                    rec.get("budget_year") or 0, unit="USD/year",
                    note="annual operating budget as reported; see population_note",
                    source_id=rec.get("source_id"))
            n_budget += 1
            if rec.get("beds"):
                cpb = int(round(rec["budget"] / rec["beds"]))
                _metric(conn, fid, "cost_per_bed_year", cpb, rec.get("budget_year") or 0,
                        unit="USD/bed/year", is_est=1,
                        note=f"derived: annual budget ${rec['budget']:,} / {rec['beds']} beds "
                             f"= ${cpb:,} per bed-year (is_est=1 means derived by division)",
                        source_id=rec.get("source_id"))
                n_cost += 1

    # 8. published operating economics (state systems) -----------------------------------
    facts = economics(conn)
    econ_notes = []
    for f in facts:
        if f["metric"] == "_dsh_total":
            ca = [r for k, r in by_kind_state("state_psychiatric", "CA")]
            beds = sum(r["beds"] or 0 for r in ca)
            if beds:
                per = int(round(f["value"] / beds))
                sid = source_id_for(conn, f["source_url"], DOMAIN,
                                    title="LAO: The 2020-21 Budget: Analysis of the Department "
                                          "of State Hospitals Budget",
                                    publisher="California Legislative Analyst's Office",
                                    license="Public record (CA state government)")
                for k, r in by_kind_state("state_psychiatric", "CA"):
                    _metric(conn, ids[k], "cost_per_bed_year", per, f["year"],
                            unit="USD/bed/year", is_est=1,
                            note=(f"derived: DSH operational expenditures ${f['value']:,.0f} "
                                  f"FY2020-21 divided by {beds} beds reported for California state "
                                  f"psychiatric hospitals = ${per:,} per bed-year. Statewide "
                                  "system average, not this facility's own cost. "
                                  f"Quote: {f['quote']}"),
                            source_id=sid)
                    n_cost += 1
                econ_notes.append(f"CA state hospital system: ${per:,}/bed-year (derived, {beds} beds)")

        elif f["metric"] == "_cdcr":
            ca_prisons = [r for k, r in by_kind_state("prison", "CA")]
            beds = sum(r["beds"] or 0 for r in ca_prisons)
            denom = f.get("inmates") or beds
            if denom:
                per = int(round(f["value"] / denom))
                sid = source_id_for(conn, f["source_url"], DOMAIN,
                                    title="LAO: State Correctional Spending Increased Despite "
                                          "Significant Population Reductions",
                                    publisher="California Legislative Analyst's Office",
                                    license="Public record (CA state government)")
                for k, r in by_kind_state("prison", "CA"):
                    _metric(conn, ids[k], "cost_per_bed_year", per, f["year"],
                            unit="USD/bed/year", is_est=1,
                            note=(f"derived: CDCR expenditures ${f['value']:,.0f} FY2019-20 divided "
                                  f"by {denom:,} inmates (June 2019) = ${per:,} per inmate-year, "
                                  "used as a proxy for cost per occupied prison bed. Statewide "
                                  "system average, not this facility's own cost. "
                                  f"Quote: {f['quote']}"),
                            source_id=sid)
                    n_cost += 1
                econ_notes.append(f"CA prison system: ${per:,}/inmate-year (derived)")

    notes.extend(econ_notes)
    notes.append(f"metrics: beds={n_beds} staff={n_staff} budget={n_budget} cost_per_bed={n_cost}")

    # 9. state coverage snapshot for the research note -----------------------------------
    cov = conn.execute(
        "SELECT state, COUNT(*) FROM facility WHERE state IS NOT NULL GROUP BY state "
        "ORDER BY state").fetchall()
    notes.append(f"states_covered={len(cov)}")

    return {
        "rows": rows,
        "notes": " | ".join(notes)[:1800],
        "sources": [
            (HIFLD_HOSP, "US Government open data (public domain)"),
            (HIFLD_PRISON, "US Government open data (public domain)"),
            ("https://query.wikidata.org/sparql", "CC0 1.0"),
            (WIKI_API, "CC BY-SA 4.0"),
            (LAO_DSH, "public record (CA Legislative Analyst's Office)"),
            (LAO_CDCR, "public record (CA Legislative Analyst's Office)"),
        ],
    }


if __name__ == "__main__":
    import sys

    from ingest import common

    if "--selftest" in sys.argv:
        conn = common.db()
        res = run(conn, {})
        conn.commit()
        print(res["rows"], res["notes"])
        common.selftest_report(conn, DOMAIN, "facility")
    else:
        common.main(DOMAIN, run, "facility")
