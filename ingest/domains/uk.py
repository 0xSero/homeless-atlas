"""uk -- United Kingdom: rough sleeping and statutory homelessness.

Four administrations publish on four different systems, so this module has one small
reader per publisher and never mixes their semantics:

  England   MHCLG "Rough sleeping snapshot in England" (autumn 2025 tables) -- per local
            authority 2010-2025 plus the published England total.
  England   MHCLG "Statutory homelessness" live tables -- England-level time series
            (Table A1 households owed a duty, Table TA1 households in temporary
            accommodation since 1998) and the latest quarterly detailed local authority
            workbook (Table A1 / Table TA1 by local authority).
  Scotland  Scottish Government "Homelessness in Scotland 2024-25" main tables --
            HL1 applications (T1), households assessed as homeless or threatened (T12),
            households in temporary accommodation at 31 March (T27), all by council.
  Wales     StatsWales -- rough sleepers by local authority (monthly), homeless
            individuals temporarily accommodated by local authority (monthly) and
            households found eligible/unintentionally homeless/in priority need
            (Section 75) by local authority (annual).
  N. Ireland DfC "Northern Ireland Homelessness Bulletin" tables -- presentations,
            acceptances and temporary accommodation placements by local government
            district.
  All UK    ONS "Local Authority Districts (April 2025) Names and Codes in the UK" --
            the code list that geo_city(kind='uk_la', geoid='UK-<ONS code>') is built
            from and that the Scottish/Welsh/NI names are matched against.

Everything lands in the generic tables, quarantined from the US rows:

  geo_city   kind='uk_la', geoid='UK-<ONS code>', state=England|Scotland|Wales|Northern Ireland
  metric     entity_type='uk_la' (entity_id = 'UK-<ONS code>')  sub-national figures
             entity_type='uk'    (entity_id = England|Scotland|Wales|Northern Ireland)
                                  published national totals

Metrics written (all published values, nothing modelled; `is_est` stays 0):
  rough_sleeping                 people counted sleeping rough on the snapshot night
  statutory_homeless_households  households owed/assessed a statutory homelessness duty
  temp_accommodation_households  households in temporary accommodation
Plus three series that only exist in this shape for some administrations, written under
their own names rather than mislabelled:
  homeless_applications          Scotland HL1 applications (Scotland only)
  homeless_presentations         Northern Ireland presentations (NI only)
  temp_accommodation_people      Wales: homeless individuals temporarily accommodated
  temp_accommodation_placements  Northern Ireland: TA placements in the half-year

The metric table is keyed (entity_type, entity_id, metric, year), so one value per
administration/LA/year is stored: where a publisher is monthly or quarterly the latest
published period of that year is kept and `note` names the exact period.

No API keys, 1 req/s (ingest.common.fetch), every response cached in data/raw/uk/ and
every inserted row carries the source_id of the exact URL it came from.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

from ingest.common import (clean, db, fetch, fetch_json, save_raw, source_id_for,
                           to_int, upsert)

DOMAIN = "uk"

OGL = "Open Government Licence v3.0 (https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)"

# --- source URLs (all verified live 2026-11-19) -------------------------------
MHCLG = "Ministry of Housing, Communities and Local Government"
SG = "The Scottish Government"
WG = "Welsh Government (StatsWales)"
DfC = "Northern Ireland Department for Communities"
ONS = "Office for National Statistics (Open Geography Portal)"

RS_URL = ("https://assets.publishing.service.gov.uk/media/699daac73e672177d0bc759b/"
          "Rough_sleeping_snapshot_in_England__autumn_2025.ods")
RS_TITLE = "Rough sleeping snapshot in England: autumn 2025 - tables"
RS_PAGE = "https://www.gov.uk/government/statistical-data-sets/tables-on-rough-sleeping"

ENG_TS_URL = ("https://assets.publishing.service.gov.uk/media/6a82eee63bd75b81e2329a7b/"
              "Statutory_Homelessness_England_Time_Series_202603.ods")
ENG_TS_TITLE = "Statutory homelessness England level time series live tables (to January to March 2026)"
ENG_TS_PAGE = "https://www.gov.uk/government/statistical-data-sets/live-tables-on-homelessness"

ENG_LA_URL = ("https://assets.publishing.service.gov.uk/media/6a82eeb83bd75b81e2329a7a/"
              "Statutory_Homelessness_Detailed_Local_Authority_Data_202603.ods")
ENG_LA_TITLE = "Statutory homelessness detailed local authority level tables: January to March 2026"

LAD_URL = ("https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
           "LAD_APR_2025_UK_NC_v2/FeatureServer/0/query"
           "?where=1%3D1&outFields=LAD25CD,LAD25NM&returnGeometry=false&f=json"
           "&resultRecordCount=2000")
LAD_TITLE = "Local Authority Districts (April 2025) Names and Codes in the UK"

SCT_URL = ("https://www.gov.scot/binaries/content/documents/govscot/publications/statistics/"
           "2025/09/homelessness-in-scotland-2024-25/documents/"
           "main-tables_homelessness-in-scotland-2024-25/"
           "main-tables_homelessness-in-scotland-2024-25/govscot%3Adocument/"
           "Main%2Btables_Homelessness%2Bin%2BScotland%2B2024-25.xlsx")
SCT_TITLE = "Main tables, Homelessness in Scotland 2024-25 (HL1/HL2)"
SCT_PAGE = "https://www.gov.scot/publications/homelessness-in-scotland-2024-25/"

SW = "https://stats.gov.wales/en-GB"
WLS_ROUGH_ID = "116be6b0-97bb-4abc-8cfb-13ff4b5d5d7e"
WLS_TA_ID = "35713e0f-e755-4444-ab14-915d8d9432e1"
WLS_OUTCOME_ID = "e856209f-09d4-4e98-824c-19c101813f67"

NI_URL = ("https://www.communities-ni.gov.uk/system/files/2026-06/"
          "ni-homelessness-bulletin-oct-mar-2026-tables.xlsx")
NI_TITLE = "Northern Ireland Homelessness Bulletin, October 2025 - March 2026: tables"
NI_PAGE = ("https://www.communities-ni.gov.uk/publications/"
           "northern-ireland-homelessness-bulletin-october-2025-march-2026")

# hosts behind a WAF that rejects the project UA
BROWSER = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
           "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}

NATIONS = {"England": "E", "Scotland": "S", "Wales": "W", "Northern Ireland": "N"}

# Scottish / Welsh publisher spellings that differ from the ONS name
NAME_ALIAS = {
    "Eilean Siar": "Na h-Eileanan Siar",
    "Orkney": "Orkney Islands",
    "Shetland": "Shetland Islands",
}


# --------------------------------------------------------------------------- #
# ODS (OpenDocument spreadsheet) reader -- stdlib only, no odfpy in this image
# --------------------------------------------------------------------------- #
_T = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_OFF = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TABLE, _ROW, _CELL, _COVERED = _T + "table", _T + "table-row", _T + "table-cell", _T + "covered-table-cell"
_NAME, _ROWS_REP, _COLS_REP = _T + "name", _T + "number-rows-repeated", _T + "number-columns-repeated"


def ods_sheets(content: bytes, wanted: set[str], max_rep: int = 250) -> dict:
    """Stream content.xml and return {sheet_name: [[cell, ...], ...]} for `wanted`.

    Cells carry either office:value (numbers) or text in text:p; both are captured here
    because text:p children are the first elements iterparse would clear.
    """
    out: dict[str, list] = {}
    cur = None
    rows = row = cell = None
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        with z.open("content.xml") as fh:
            for ev, el in ET.iterparse(fh, events=("start", "end")):
                tag = el.tag
                if ev == "start":
                    if tag == _TABLE:
                        nm = el.get(_NAME)
                        if nm in wanted and nm not in out:
                            cur, rows = nm, []
                        else:
                            cur = rows = None
                    elif cur is not None and tag == _ROW:
                        row = []
                    elif cur is not None and tag in (_CELL, _COVERED):
                        cell = {"v": el.get(_OFF + "value"), "t": [],
                                "n": min(int(el.get(_COLS_REP, 1)), max_rep)}
                    continue
                # end events
                if cell is not None and tag.endswith("}p"):
                    txt = "".join(el.itertext())
                    if txt.strip():
                        cell["t"].append(txt)
                elif cell is not None and tag in (_CELL, _COVERED):
                    if tag == _COVERED and cell["v"] is None:
                        val = ""
                    elif cell["v"] is not None:
                        val = cell["v"]
                    else:
                        val = clean(" ".join(cell["t"])) or ""
                    if row is not None:
                        row.extend([val] * cell["n"])
                    cell = None
                elif cur is not None and tag == _ROW:
                    rep = min(int(el.get(_ROWS_REP, 1)), max_rep)
                    while row and row[-1] == "":
                        row.pop()
                    for _ in range(rep):
                        rows.append(list(row))
                    row = cell = None
                    el.clear()
                elif cur is not None and tag == _TABLE:
                    while rows and not rows[-1]:
                        rows.pop()
                    out[cur] = rows
                    cur = rows = row = cell = None
                    el.clear()
    return out


def _find_row(rows, *needles, start=0):
    for i in range(start, len(rows)):
        line = " | ".join(str(x or "") for x in rows[i][:8]).lower()
        if all(n.lower() in line for n in needles):
            return i
    return None


def _col(header, *needles, exclude=()):
    for i, h in enumerate(header):
        hl = str(h or "").lower()
        if all(n.lower() in hl for n in needles) and not any(x.lower() in hl for x in exclude):
            return i
    return None


def _num(v):
    """Published cell -> int/None. '[x]', '[z]', '[c]', '[n]', '*' mean not available."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.startswith("["):
        return None
    if "*" in s or s.lower() in ("nan", "-"):
        return None
    return to_int(s)


def _year_of(label):
    """'2024-25' -> 2025, '2025/26' -> 2026, '2021' -> 2021, '31/08/2026' -> 2026."""
    s = str(label or "")
    m = re.match(r"\s*(\d{4})\s*[-/_]\s*(\d{2})\s*$", s)
    if m:
        return int(m.group(1)[:2] + m.group(2))
    m = re.search(r"(\d{4})\s*$", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{4})", s)
    return int(m.group(1)) if m else None


def _name_key(s: str) -> str:
    s = (s or "").lower().replace("&", " and ").replace("-", " ").replace(",", " ")
    s = re.sub(r"\b(city of|london borough of|royal borough of|city|council|county)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# --------------------------------------------------------------------------- #
# writers
# --------------------------------------------------------------------------- #
def stamp_source(conn, url, content) -> None:
    """Record size + sha256 on the source row for the exact payload we parsed.

    common.source_id_for looks the cache up by sha1(url), but fetch() keys GET payloads by
    sha1(method+url+"{}") and POST payloads by their form body, so it never finds the file
    and leaves bytes/sha256 NULL. We know the payload, so we write it ourselves.
    """
    conn.execute("UPDATE source SET bytes=?, sha256=? WHERE domain=? AND url=?",
                 (len(content), hashlib.sha256(content).hexdigest(), DOMAIN, url))


def put_metric(conn, etype, eid, metric, year, value, unit, note, source_id) -> int:
    if value is None or year is None or eid is None:
        return 0
    v = float(value)
    v = int(v) if v.is_integer() else v
    upsert(conn, "metric", {"entity_type": etype, "entity_id": eid, "metric": metric,
                            "value": v, "unit": unit, "year": int(year), "is_est": 0,
                            "note": note, "source_id": source_id},
           keys=["entity_type", "entity_id", "metric", "year"])
    return 1


def put_la(conn, code, name, nation, source_id) -> int:
    upsert(conn, "geo_city", {"geoid": f"UK-{code}", "kind": "uk_la", "name": name,
                              "state": nation, "source_id": source_id},
           keys=["geoid"])
    return 1


# --------------------------------------------------------------------------- #
# 1. ONS local authority code list  ->  the geo_city universe + name lookup
# --------------------------------------------------------------------------- #
NATION_OF = {"E": "England", "S": "Scotland", "W": "Wales", "N": "Northern Ireland"}


def ons_lads(conn):
    raw = fetch(LAD_URL, domain=DOMAIN)
    data = json.loads(raw)
    src = source_id_for(conn, LAD_URL, DOMAIN, title=LAD_TITLE, publisher=ONS, license=OGL)
    stamp_source(conn, LAD_URL, raw)
    by_code, by_name, rows = {}, {}, 0
    for f in data.get("features", []):
        a = f["attributes"]
        code, name = a.get("LAD25CD"), clean(a.get("LAD25NM"))
        if not code or not name:
            continue
        nation = NATION_OF.get(code[0])
        if not nation:
            continue
        by_code[code] = (name, nation)
        by_name[_name_key(name)] = code
        rows += put_la(conn, code, name, nation, src)
    return by_code, by_name, rows, src


# --------------------------------------------------------------------------- #
# 2. England -- rough sleeping snapshot, per local authority, 2010-2025
# --------------------------------------------------------------------------- #
def rough_sleeping(conn, src, lads):
    content = fetch(RS_URL, domain=DOMAIN)
    stamp_source(conn, RS_URL, content)
    sheets = ods_sheets(content, {"Table_1_Total"})
    rows = sheets["Table_1_Total"]
    h = _find_row(rows, "Local Authority Code", "Local Authority Name")
    if h is None:
        raise RuntimeError("rough sleeping: header row not found")
    header = rows[h]
    years = {}
    for i, cell in enumerate(header):
        if re.fullmatch(r"(19|20)\d{2}", (cell or "").strip()):
            years[i] = int(cell)
    n = 0
    for r in rows[h + 1:]:
        if len(r) < 4:
            continue
        code, name, rcode, rname = r[0], clean(r[1]), r[2], r[3]
        vals = {yi: _num(r[yi]) if yi < len(r) else None for yi in years}
        if rcode == "E92000001":                       # published England total
            for yi, y in years.items():
                n += put_metric(conn, "uk", "England", "rough_sleeping", y, vals[yi],
                                "people",
                                "Rough sleeping snapshot in England, autumn %d "
                                "(single-night count/estimate)" % y, src)
            continue
        if not re.fullmatch(r"E0[6789]\d{6}", code or ""):
            continue                                    # regions ('E12...') and blanks
        for yi, y in years.items():
            n += put_metric(conn, "uk_la", f"UK-{code}", "rough_sleeping", y, vals[yi],
                            "people",
                            "Rough sleeping snapshot in England, autumn %d "
                            "(single-night count/estimate)" % y, src)
    return n, len(years)


# --------------------------------------------------------------------------- #
# 3. England -- statutory homelessness, England-level time series
# --------------------------------------------------------------------------- #
def england_time_series(conn):
    content = fetch(ENG_TS_URL, domain=DOMAIN)
    src = source_id_for(conn, ENG_TS_URL, DOMAIN, title=ENG_TS_TITLE, publisher=MHCLG, license=OGL)
    stamp_source(conn, ENG_TS_URL, content)
    sh = ods_sheets(content, {"A1", "TA1"})
    n = 0

    rows = sh["A1"]
    h = _find_row(rows, "Time period", "Time period code")
    header, code_i = rows[h], 1
    owed_i = _col(header, "owed a prevention or relief duty", "total")
    status_i = _col(header, "data status")
    for r in rows[h + 1:]:
        if len(r) <= max(code_i, owed_i or 0):
            continue
        code = (r[code_i] or "").strip()
        if not re.fullmatch(r"\d{4}_\d{2}", code):
            continue                                    # financial-year totals only
        v = _num(r[owed_i])
        st = (r[status_i] or "").strip() if status_i is not None else ""
        n += put_metric(conn, "uk", "England", "statutory_homeless_households",
                        _year_of(code), v, "households",
                        "England, financial year %s-%s, households assessed as owed a "
                        "prevention or relief duty%s" % (code[:4], code[5:], " (provisional)" if "p" in st else ""),
                        src)

    rows = sh["TA1"]
    h = _find_row(rows, "Time period", "Time period code")
    header, code_i = rows[h], 1
    ta_i = _col(header, "households in ta", "total", exclude=("with children", "per thousand"))
    best = {}
    for r in rows[h + 1:]:
        if len(r) <= max(code_i, ta_i or 0):
            continue
        code = (r[code_i] or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        v = _num(r[ta_i])
        if v is None:
            continue
        y = int(code[:4])
        if y not in best or code > best[y][0]:
            best[y] = (code, v)
    for y, (code, v) in best.items():
        n += put_metric(conn, "uk", "England", "temp_accommodation_households", y, v,
                        "households",
                        "England, households in temporary accommodation at the end of %s-%s"
                        % (code[:4], code[4:6]), src)
    return n, src


# --------------------------------------------------------------------------- #
# 4. England -- latest quarterly detailed local authority workbook
# --------------------------------------------------------------------------- #
def england_la_period(conn, lads):
    content = fetch(ENG_LA_URL, domain=DOMAIN)
    src = source_id_for(conn, ENG_LA_URL, DOMAIN, title=ENG_LA_TITLE, publisher=MHCLG, license=OGL)
    stamp_source(conn, ENG_LA_URL, content)
    sh = ods_sheets(content, {"A1", "TA1"})
    n = 0
    for sheet, metric, unit, note in (
            ("A1", "statutory_homeless_households", "households",
             "England, quarter January to March 2026, households assessed as owed a "
             "prevention or relief duty"),
            ("TA1", "temp_accommodation_households", "households",
             "England, households in temporary accommodation at 31 March 2026")):
        rows = sh[sheet]
        h = _find_row(rows, "Organisation Identification Code")
        header = rows[h]
        if sheet == "A1":
            vi = _col(header, "owed a prevention or relief duty", "total")
        else:
            vi = _col(header, "households in ta", "total",
                      exclude=("with children", "per thousand"))
        year = _year_of(rows[0][0])
        for r in rows[h + 1:]:
            if len(r) <= max(1, vi) or not re.fullmatch(r"E0[6789]\d{6}", (r[0] or "").strip()):
                continue
            n += put_metric(conn, "uk_la", f"UK-{r[0].strip()}", metric, year,
                            _num(r[vi]), unit, note, src)
    return n, src


# --------------------------------------------------------------------------- #
# 5. Scotland -- HL1 applications, assessments, temporary accommodation
# --------------------------------------------------------------------------- #
def _xlsx_sheets(content: bytes, wanted) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out = {}
    for name in wanted:
        out[name] = [list(r) for r in wb[name].iter_rows(values_only=True)]
    wb.close()
    return out


def _scotland_table(rows, metric, unit, note, source_id, conn, lads, by_name, national):
    """Long-form: header row whose first cell is 'Local Authority' + one column per year."""
    h = None
    for i, r in enumerate(rows):
        if r and str(r[0] or "").strip().lower() == "local authority":
            h = i
            break
    if h is None:
        return 0
    header = rows[h]
    cols = {}
    for i, cell in enumerate(header):
        s = str(cell or "").strip()
        if re.fullmatch(r"(19|20)\d{2}(\s*[-/]\s*\d{2})?", s):
            cols[i] = _year_of(s)
    n = 0
    started = False
    for r in rows[h + 1:]:
        if not r or not clean(r[0]):
            if started:          # worksheets stack table a on table b, separated by a blank row
                break
            continue
        started = True
        name = clean(r[0])
        if name.lower() in ("scotland", "all scotland"):
            for i, y in cols.items():
                n += put_metric(conn, "uk", national, metric, y,
                                _num(r[i]) if i < len(r) else None, unit, note, source_id)
            continue
        if name[0].isdigit() or name.startswith("Table"):
            continue
        code = by_name.get(_name_key(NAME_ALIAS.get(name, name)))
        if not code:
            continue
        for i, y in cols.items():
            n += put_metric(conn, "uk_la", f"UK-{code}", metric, y,
                            _num(r[i]) if i < len(r) else None, unit, note, source_id)
    return n


def scotland(conn, lads):
    content = fetch(SCT_URL, domain=DOMAIN)
    save_raw(DOMAIN, "homelessness_in_scotland_2024-25_main_tables.xlsx", content)
    src = source_id_for(conn, SCT_URL, DOMAIN, title=SCT_TITLE, publisher=SG, license=OGL)
    stamp_source(conn, SCT_URL, content)
    sh = _xlsx_sheets(content, {"T1", "T12", "T27"})
    by_name = {_name_key(n): c for c, (n, _nat) in lads.items()}
    for code, (name, _n) in lads.items():
        if code[0] == "S":
            by_name.setdefault(_name_key(name), code)
    n = 0
    n += _scotland_table(sh["T1"], "homeless_applications", "applications",
                         "Scotland HL1: homelessness applications by local authority",
                         src, conn, lads, by_name, "Scotland")
    n += _scotland_table(sh["T12"], "statutory_homeless_households", "households",
                         "Scotland HL1: applications assessed as homeless or threatened "
                         "with homelessness (statutory duty)",
                         src, conn, lads, by_name, "Scotland")
    n += _scotland_table(sh["T27"], "temp_accommodation_households", "households",
                         "Scotland HL2: households in temporary accommodation at 31 March",
                         src, conn, lads, by_name, "Scotland")
    return n, src


# --------------------------------------------------------------------------- #
# 6. Wales -- StatsWales CSV downloads (unfiltered, whole dataset)
# --------------------------------------------------------------------------- #
def _wales_csv(dataset_id, title):
    payload = {"view_type": "unfiltered", "format": "csv", "view_choice": "raw",
               "extended": "no", "download_language": "en-GB",
               "selected_filter_options": "[]"}
    url = f"{SW}/{dataset_id}/download"
    content = fetch(url, method="POST", data=payload, domain=DOMAIN, headers=BROWSER)
    if not content.lstrip().startswith(b"Data values"):
        raise RuntimeError(f"StatsWales {dataset_id}: unexpected download payload")
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    return rows, url, content


def wales(conn, lads):
    by_name = {_name_key(n): c for c, (n, _nat) in lads.items()}
    n = 0
    sources = []

    def wrow(area):
        if area == "Wales":
            return ("uk", "Wales")
        code = by_name.get(_name_key(area))
        return ("uk_la", f"UK-{code}") if code else (None, None)

    # 6a rough sleepers, monthly
    rows, url, content = _wales_csv(WLS_ROUGH_ID, "Rough sleepers by local authority")
    src = source_id_for(conn, url, DOMAIN,
                        title="Rough sleepers by local authority (StatsWales dataset "
                              f"{WLS_ROUGH_ID})", publisher=WG, license=OGL)
    stamp_source(conn, url, content)
    sources.append(src)
    best = {}
    for r in rows:
        if (r.get("Data description") or "") != "Number of rough sleepers":
            continue
        per = (r.get("Period") or "").strip()
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", per)
        if not m:
            continue
        key = (r["Area"], m.group(3))
        if key not in best or per > best[key][0]:
            best[key] = (per, _num(r["Data values"]))
    for (area, yr), (per, v) in best.items():
        et, eid = wrow(area)
        n += put_metric(conn, et, eid, "rough_sleeping", int(yr), v, "people",
                        "Wales, individuals sleeping rough on the last day of the month, "
                        "period ending %s" % per, src)

    # 6b homeless individuals temporarily accommodated, monthly
    rows, url, content = _wales_csv(WLS_TA_ID, "Homeless individuals temporarily accommodated")
    src = source_id_for(conn, url, DOMAIN,
                        title="Homeless individuals temporarily accommodated at the end of "
                              f"the period by local authority (StatsWales dataset {WLS_TA_ID})",
                        publisher=WG, license=OGL)
    stamp_source(conn, url, content)
    sources.append(src)
    best = {}
    for r in rows:
        if (r.get("Accommodation type") or "") != "Total":
            continue
        if "temporarily accommodated" not in (r.get("Data description") or "").lower():
            continue
        per = (r.get("Period") or "").strip()
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", per)
        if not m:
            continue
        key = (r["Area"], m.group(3))
        if key not in best or per > best[key][0]:
            best[key] = (per, _num(r["Data values"]))
    for (area, yr), (per, v) in best.items():
        et, eid = wrow(area)
        n += put_metric(conn, et, eid, "temp_accommodation_people", int(yr), v, "people",
                        "Wales, homeless individuals temporarily accommodated at the end of "
                        "the month, period ending %s, all accommodation types" % per, src)

    # 6c households found eligible, unintentionally homeless, in priority need (s75)
    rows, url, content = _wales_csv(WLS_OUTCOME_ID, "Households for which assistance has been provided by outcome")
    src = source_id_for(conn, url, DOMAIN,
                        title="Households for which assistance has been provided by outcome and "
                              f"household type (StatsWales dataset {WLS_OUTCOME_ID})",
                        publisher=WG, license=OGL)
    stamp_source(conn, url, content)
    sources.append(src)
    for r in rows:
        if (r.get("Data description") or "") != "Number":
            continue
        if (r.get("Outcomes") or "") != ("Eligible, unintentionally homeless and in priority "
                                         "need (Section 75)"):
            continue
        et, eid = wrow(r["Area"])
        n += put_metric(conn, et, eid, "statutory_homeless_households",
                        _year_of(r.get("Period")), _num(r["Data values"]), "households",
                        "Wales, households found eligible, unintentionally homeless and in "
                        "priority need (s75) during %s" % r.get("Period"), src)
    return n, sources


# --------------------------------------------------------------------------- #
# 7. Northern Ireland -- homelessness bulletin tables
# --------------------------------------------------------------------------- #
def northern_ireland(conn, lads):
    content = fetch(NI_URL, domain=DOMAIN, headers=BROWSER)
    save_raw(DOMAIN, "ni_homelessness_bulletin_oct2025_mar2026_tables.xlsx", content)
    src = source_id_for(conn, NI_URL, DOMAIN, title=NI_TITLE, publisher=DfC, license=OGL)
    stamp_source(conn, NI_URL, content)
    sh = _xlsx_sheets(content, {"1_3", "2_3", "3_2A"})
    by_name = {_name_key(n): c for c, (n, _nat) in lads.items() if c[0] == "N"}
    n = 0
    for sheet, metric, unit, mnote in (
            ("2_3", "statutory_homeless_households", "households",
             "Northern Ireland, households accepted as homeless"),
            ("1_3", "homeless_presentations", "households",
             "Northern Ireland, households presenting as homeless"),
            ("3_2A", "temp_accommodation_placements", "placements",
             "Northern Ireland, placements in temporary accommodation")):
        rows = sh[sheet]
        hdr = next((i for i in range(0, 6)
                    if rows[i] and str(rows[i][0] or "").lower().startswith("local government district")),
                   None)
        ni_row = next((r for r in rows if str(r[0] or "").strip() == "Northern Ireland"), None)
        if hdr is None or ni_row is None:
            continue
        # latest published period = last column carrying a count (skip rates / percentages)
        last = None
        for j in range(len(ni_row) - 1, 0, -1):
            if not isinstance(ni_row[j], (int, float)):
                continue
            desc = str(rows[hdr][j] or "") if j < len(rows[hdr]) else ""
            if "per 1,000" in desc.lower() or "%" in desc:
                continue
            last = j
            break
        if last is None:
            continue
        year_label = next((rows[i][last] for i in (1, 2)
                           if last < len(rows[i]) and re.search(r"\d{4}", str(rows[i][last] or ""))),
                          None)
        period = clean(rows[2][last]) if last < len(rows[2]) else None
        year = _year_of(year_label)
        note = "%s, %s %s" % (mnote, period or "", year_label or "")
        for r in rows:
            nm = clean(r[0] or "")
            if not nm or len(r) <= last:
                continue
            v = _num(r[last])
            if nm == "Northern Ireland":
                n += put_metric(conn, "uk", "Northern Ireland", metric, year, v, unit, note, src)
                continue
            code = by_name.get(_name_key(nm))
            if code:
                n += put_metric(conn, "uk_la", f"UK-{code}", metric, year, v, unit, note, src)
    return n, src


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run(conn, cfg=None):
    cfg = cfg or {}
    notes = []
    total = 0

    lads, by_name, n_la, _src_lad = ons_lads(conn)
    total += n_la
    notes.append(f"ons_lads={len(lads)}")

    n, yrs = rough_sleeping(conn, source_id_for(
        conn, RS_URL, DOMAIN, title=RS_TITLE, publisher=MHCLG, license=OGL), lads)
    total += n
    notes.append(f"england_rough_sleeping={n} ({yrs} years)")

    n, _ = england_time_series(conn)
    total += n
    notes.append(f"england_national_series={n}")

    n, _ = england_la_period(conn, lads)
    total += n
    notes.append(f"england_la_latest_quarter={n}")

    n, _ = scotland(conn, lads)
    total += n
    notes.append(f"scotland={n}")

    n, _ = wales(conn, lads)
    total += n
    notes.append(f"wales={n}")

    n, _ = northern_ireland(conn, lads)
    total += n
    notes.append(f"northern_ireland={n}")

    conn.commit()
    return {"rows": total, "notes": " | ".join(notes)}


def _selftest():
    conn = db()
    res = run(conn, {})
    conn.commit()
    print(res)
    for label, sql in (
            ("geo_city uk_la", "SELECT COUNT(*) c FROM geo_city WHERE kind='uk_la'"),
            ("metric by type", "SELECT entity_type, COUNT(*) c, COUNT(DISTINCT metric) m FROM metric "
                               "WHERE entity_type IN ('uk','uk_la') GROUP BY 1"),
            ("metric by name", "SELECT metric, entity_type, COUNT(*) c, MIN(year), MAX(year) FROM metric "
                               "WHERE entity_type IN ('uk','uk_la') GROUP BY 1,2 ORDER BY 1"),
            ("national totals", "SELECT entity_id, metric, year, value FROM metric WHERE entity_type='uk' "
                                "AND metric='rough_sleeping' ORDER BY entity_id, year DESC LIMIT 12")):
        print("--", label)
        for r in conn.execute(sql):
            print("  ", tuple(r))
    print("-- 3 sample uk_la rows")
    for r in conn.execute("""SELECT c.geoid, c.name, c.state, m.metric, m.year, m.value, m.note, s.url
                             FROM metric m JOIN geo_city c ON c.geoid=m.entity_id
                             LEFT JOIN source s ON s.id=m.source_id
                             WHERE m.entity_type='uk_la' ORDER BY RANDOM() LIMIT 3"""):
        print("  ", dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        conn = db()
        res = run(conn, {})
        conn.commit()
        print(f"[{DOMAIN}] rows={res['rows']} {res['notes']}")
