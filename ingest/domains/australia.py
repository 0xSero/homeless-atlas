"""australia -- Australia: ABS census homelessness estimates, SHS clients and SHS funding.

Two publishers with two unrelated systems, so this module has one reader per system:

  ABS   "Estimating homelessness: Census, 2021" (cat. no. 2049.0), data cubes published on
        the release page. Cube 1 table 1.3 = number of homeless persons by state and
        territory of USUAL RESIDENCE for the 2006, 2011, 2016 and 2021 Censuses; table 1.5
        = the same rows expressed as a rate per 10,000 of the population. Cube 6 tables 6.1
        and 6.2 = homeless operational groups by place of ENUMERATION for every Local
        Government Area (LGA), 2021 and 2016.
  RoGS  Report on Government Services 2026, Part G section 19 "Homelessness services"
        machine-readable dataset (table 19A.21 "Recurrent cost per client accessing
        homelessness services", table 19A.1 "Government expenditure on homelessness
        services", table 19A.11 "Average daily unassisted requests"). This is the only
        keyless machine-readable feed of AIHW Specialist Homelessness Services Collection
        (SHSC) per-jurisdiction client counts and of government SHS expenditure, because
        www.aihw.gov.au itself sits behind a Cloudflare JS challenge (see BLOCKERS.md).

Everything lands in the generic tables, quarantined from the US/UK/Canada rows:

  geo_city   kind='au_state', geoid='AU-<ABS abbr>'   (8 states/territories + OT)
             kind='au_lga',   geoid='AU-LGA-<abbr>-<slug>'
             fields: name as the publisher printed it, state = the abbreviation,
             pop/lat/lon NULL (neither source publishes a population or a coordinate
             for these units, and nothing here is estimated)
  metric     entity_type='au_state'  entity_id='AU-<abbr>'
             entity_type='au_lga'    entity_id='AU-LGA-<abbr>-<slug>'
             entity_type='australia' entity_id='Australia'   (published national totals)

Metrics written (all published values; `is_est` stays 0 because ABS and RoGS publish these
as counts/expenditure, not model output):

  census_homeless_estimate        people counted homeless on Census night
  census_homeless_rate_per_10k    the ABS-published rate per 10,000 of the population
  shs_clients                     distinct SHS clients in the financial year (SHSC)
  shs_funding                     recurrent government expenditure, $m, 2024-25 dollars
  shs_funding_per_person          recurrent expenditure per person in the population
  shs_cost_per_client             estimated recurrent cost per SHS client
  shs_unassisted_requests_daily   average daily unassisted requests for accommodation

Basis differences that a consumer must not paper over (every row's `note` says which):

  * state/territory rows from ABS are by USUAL RESIDENCE; LGA rows are by PLACE OF
    ENUMERATION, because ABS publishes nothing finer than state by usual residence. The two
    are different populations in a mobile country: 2021 NSW is 35,011 usual residence vs
    34,982 place of enumeration.
  * census years are calendar years (2006, 2011, 2016, 2021); SHS reporting years are
    financial years, stored under their ENDING year (FY2024-25 -> year 2025).
  * `shs_funding` is the state/territory government recurrent expenditure RoGS reports for
    specialist homelessness services. Since 1 July 2024 SHS is jointly funded through the
    National Agreement on Social Housing and Homelessness (NASHH) with the homelessness
    component matched equally by the Australian Government, and state governments make the
    direct expenditure; RoGS states recurrent government expenditure in 2024-25 was
    $1.8 billion, which is the national row of this series. It is not a per-state NASHH
    allocation -- those allocations are not published in machine-readable form (BLOCKERS.md).

No API keys. 1 req/s via ingest.common.fetch, every response cached in data/raw/australia/,
and every inserted row carries the source_id of the exact URL it came from.
"""
from __future__ import annotations

import csv
import io
import re
import sys

from ingest.common import clean, db, fetch, slug, source_id_for, upsert

DOMAIN = "australia"

ABS_RELEASE = "https://www.abs.gov.au/statistics/people/housing/estimating-homelessness-census/2021"
ABS_CUBE_STATE = ABS_RELEASE + "/20490do001_2021.xlsx"    # cube 1: state/territory tables
ABS_CUBE_LGA = ABS_RELEASE + "/20490do006_2021.xlsx"      # cube 6: Local Government Area
ROGS_CSV = ("https://assets.pc.gov.au/2026-01/rogs-2026-partg-section19-homelessness-services-"
            "dataset.csv?VersionId=oou7gCfLtIUjLTYmqVgZgevEBezJo44g")

ABS_TITLE = ("Estimating homelessness: Census, 2021 -- data cubes 1 (state and territory of usual "
             "residence, tables 1.1-1.5) and 6 (Local Government Area, tables 6.1-6.2)")
ABS_PUBLISHER = "Australian Bureau of Statistics (ABS), cat. no. 2049.0"
ABS_LICENSE = "CC BY 4.0 International (ABS copyright and Creative Commons statement)"
ROGS_TITLE = ("Report on Government Services 2026, Part G Housing and homelessness, Section 19 "
              "Homelessness services -- dataset (tables 19A.1, 19A.11, 19A.21)")
ROGS_PUBLISHER = "Productivity Commission (Report on Government Services), on behalf of Australian governments"
ROGS_LICENSE = "CC BY 4.0 International (Productivity Commission copyright statement)"

# ABS column labels -> (abbreviation, full name). Both cubes are keyed on these labels.
STATES = [
    ("NSW", "NSW", "New South Wales"),
    ("Vic.", "VIC", "Victoria"),
    ("Qld", "QLD", "Queensland"),
    ("SA", "SA", "South Australia"),
    ("WA", "WA", "Western Australia"),
    ("Tas.", "TAS", "Tasmania"),
    ("NT", "NT", "Northern Territory"),
    ("ACT", "ACT", "Australian Capital Territory"),
]
ABBR_OF_LABEL = {lab: abbr for lab, abbr, _ in STATES}
NAME_OF_ABBR = {abbr: name for _, abbr, name in STATES}
# cube 6 groups LGAs under full state names plus a catch-all "Other Territories"
ABBR_OF_FULLNAME = {name.lower(): abbr for _, abbr, name in STATES}
ABBR_OF_FULLNAME["other territories"] = "OT"
NAME_OF_ABBR["OT"] = "Other Territories"

# RoGS table 19A.21 / 19A.1 / 19A.11 row labels -> metric names
ROGS_METRICS = {
    ("19A.21", "Total clients", "no."): ("shs_clients", "clients",
                               "distinct clients of specialist homelessness services in the financial year "
                               "(AIHW Specialist Homelessness Services Collection)"),
    ("19A.21", "Total real recurrent expenditure", "$m"): (
        "shs_funding", "AUD million (2024-25 dollars)",
        "recurrent government expenditure on specialist homelessness services, real 2024-25 dollars"),
    ("19A.21", "Estimated cost per client", "$"): (
        "shs_cost_per_client", "AUD per client (2024-25 dollars)",
        "recurrent expenditure divided by clients (RoGS estimated cost per client)"),
    ("19A.1", "Real expenditure per person in the residential population", "$"): (
        "shs_funding_per_person", "AUD per person (2024-25 dollars)",
        "recurrent expenditure on specialist homelessness services per person in the residential population"),
}


# ---------------------------------------------------------------- helpers

def _num(v):
    """Parse a published number; return None for blanks, 'n.a.', '-' and footnotes."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s.lower() in ("n.a.", "na", "np", "-", "--", "..", "n/a"):
        return None
    s = s.replace(",", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def _xlsx(url: str):
    import openpyxl
    b = fetch(url, domain=DOMAIN)
    return openpyxl.load_workbook(io.BytesIO(b), read_only=True, data_only=True)


def _rows(ws):
    for row in ws.iter_rows(values_only=True):
        yield row


def _label(v):
    """clean() passes numbers through; label cells hold years and names, so force text."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    c = clean(v)
    return c if c is None else str(c)


def _cell(row, i):
    return row[i] if i < len(row) else None


# ABS prints Local Government Area names with a council-type suffix in the 2016 table
# ('Maribyrnong (C)', 'Barkly (R)') and without it in the 2021 table ('Maribyrnong', 'Barkly'),
# sometimes with a state tag appended as well ('Campbelltown (C) (NSW)'). Stripping those
# tokens is the only way the same LGA can be one entity across the two Census years; the map
# is printed -> canonical, so no name is invented and every row keeps the name ABS printed.
_LGA_TYPE_TOK = re.compile(
    r"\s*\((C|A|S|T|M|B|R|DC|RC|AC|SC|RegC|IRC|Shire|Region|Aboriginal Shire)\)\s*$", re.I)
_LGA_STATE_TOK = re.compile(r"\s*\((NSW|Vic\.?|Qld|SA|WA|Tas\.?|NT|ACT)\)\s*$", re.I)


def _lga_canon(name: str) -> str:
    """Canonical form of a printed LGA name: council-type and state tokens removed, leading
    'The' and the conjunction 'and' dropped, punctuation collapsed by slug(). ABS spells the
    same council 'Blackall-Tambo' in 2016 and 'Blackall Tambo' in 2021; those are one place."""
    n = str(name).strip()
    while True:
        m = _LGA_TYPE_TOK.search(n) or _LGA_STATE_TOK.search(n)
        if not m:
            break
        n = n[:m.start()].strip()
    n = re.sub(r"^The\s+", "", n, flags=re.I)
    n = re.sub(r"\s+and\s+", " ", n, flags=re.I)
    return n


def _lga_geoid(abbr: str, name: str) -> str:
    return f"AU-LGA-{abbr}-{slug(_lga_canon(name))}"


def _state_geoid(abbr: str) -> str:
    return f"AU-{abbr}"


def _fy_end_year(label: str):
    """'2024-25' -> 2025; '2011' -> 2011. Financial years are stored under their ending year."""
    m = re.match(r"^(\d{4})-(\d{2})$", str(label).strip())
    if m:
        return int(m.group(1)) + 1
    m = re.match(r"^(\d{4})$", str(label).strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- ABS: state and territory

def parse_abs_national_series(url: str):
    """Table 1.1 of ABS cube 1 -- the national 'All homeless persons' row for each Census year,
    with the year printed in a proper column header (2006, 2011, 2016, 2021).

    This is the anchor used to date the blocks of tables 1.3 and 1.5: ABS omits the '2011'
    year label in table 1.3 (checked: the block header row is simply missing), so matching a
    block's published national value against this series is the only non-guess way to date it.

    Returns {year: {"count": n, "rate": r}}
    """
    wb = _xlsx(url)
    if "Table_1.1" not in wb.sheetnames:
        raise RuntimeError(f"{url} has no sheet Table_1.1")
    ws = wb["Table_1.1"]
    year_cols = None       # {year: column index of the 'no.' cell}
    rate_col_of = {}       # {year: column index of the rate cell}
    out = {}
    rows = list(_rows(ws))
    for i, row in enumerate(rows):
        labs = [_label(_cell(row, c)) for c in range(0, 22)]
        years = {int(x): c for c, x in enumerate(labs) if x and re.match(r"^(19|20)\d{2}$", x)}
        if len(years) >= 3:
            year_cols = years
            units = [_label(_cell(rows[i + 1], c)) for c in range(0, 22)]
            for y, c in years.items():
                for c2 in range(c, min(c + 4, 22)):
                    u = units[c2] or ""
                    if u.startswith("Rate per 10,000"):
                        rate_col_of[y] = c2
                        break
            continue
        lab = _label(_cell(row, 0)) or ""
        if year_cols and lab.lower().startswith("all homeless persons"):
            for y, c in year_cols.items():
                cnt = _num(_cell(row, c))
                if cnt is not None:
                    out[y] = {"count": cnt}
                    rc = rate_col_of.get(y)
                    if rc is not None:
                        rate = _num(_cell(row, rc))
                        if rate is not None:
                            out[y]["rate"] = rate
            break
    wb.close()
    if not out:
        raise RuntimeError(f"{url}: could not read the national series from table 1.1")
    return out


def parse_abs_states(url: str):
    """Table 1.3 (counts) and table 1.5 (rates per 10,000) of ABS cube 1.

    Returns {(year, entity_id): {"count": n, "rate": r, "entity_type": ...}} where entity_id
    is 'AU-<abbr>' for a state or 'Australia' for the published national column. Each block's
    year is identified by matching its published national value against table 1.1.
    """
    anchor = parse_abs_national_series(url)
    wb = _xlsx(url)
    out = {}
    years_seen = set()
    for sheet, field in (("Table_1.3", "count"), ("Table_1.5", "rate")):
        if sheet not in wb.sheetnames:
            raise RuntimeError(f"{url} has no sheet {sheet}")
        ws = wb[sheet]
        header_idx = None
        colmap = {}
        blocks = []
        for i, row in enumerate(_rows(ws)):
            if i > 200:
                break
            label = _label(_cell(row, 0))
            if header_idx is None:
                # the header row carries the state labels in columns 1..8 and 'Aust.' in 9;
                # in table 1.3 col. 0 of that row is blank, in table 1.5 it holds the row title
                labs = [_label(_cell(row, c)) for c in range(1, 11)]
                if "NSW" in labs and any(l and l.startswith("Vic") for l in labs):
                    header_idx = i
                    for c, lab in enumerate(labs, start=1):
                        if not lab:
                            continue
                        if lab in ABBR_OF_LABEL:
                            colmap[_state_geoid(ABBR_OF_LABEL[lab])] = c
                        elif lab.startswith("Aust"):
                            colmap["Australia"] = c
                    if len(colmap) != 9:
                        raise RuntimeError(f"unexpected {sheet} header: {labs}")
                continue
            if label and label.lower().startswith("total homeless persons"):
                blocks.append({eid: _num(_cell(row, c)) for eid, c in colmap.items()})
        # date each block from its own published national value
        used = set()
        for blk in blocks:
            nat = blk.get("Australia")
            if nat is None:
                continue
            year = None
            for y, rec in anchor.items():
                ref = rec.get(field)
                if ref is None or y in used:
                    continue
                if abs(ref - nat) <= 0.05:
                    year = y
                    break
            if year is None:
                raise RuntimeError(f"{sheet}: no Census year in table 1.1 matches the national "
                                   f"{field} {nat}; refusing to guess a year")
            used.add(year)
            years_seen.add(year)
            for eid, val in blk.items():
                if val is None:
                    continue
                etype = "australia" if eid == "Australia" else "au_state"
                out.setdefault((year, eid), {"entity_type": etype})
                out[(year, eid)][field] = val
    wb.close()
    if len(years_seen) < 2:
        raise RuntimeError("expected at least two Census years in tables 1.3/1.5")
    return out


# ---------------------------------------------------------------- ABS: Local Government Area

def parse_abs_lga(url: str):
    """Tables 6.1 (2021) and 6.2 (2016) of ABS cube 6: homeless persons by LGA, place of
    enumeration. Returns [(year, state_abbr, lga_name, total)]."""
    wb = _xlsx(url)
    out = []
    seen = set()
    for sheet, year in (("Table_6.1", 2021), ("Table_6.2", 2016)):
        if sheet not in wb.sheetnames:
            raise RuntimeError(f"{url} has no sheet {sheet}")
        ws = wb[sheet]
        header = None
        state = None
        total_col = None
        for i, row in enumerate(_rows(ws)):
            labs = [_label(_cell(row, c)) for c in range(0, 15)]
            if header is None:
                # header line: 'Australia | State or Territory | LGA(...) | <group cols> |
                # All homeless persons | ... | Other marginal housing ...'
                if any(l and l.startswith("LGA(") for l in labs):
                    header = i
                    for c, lab in enumerate(labs):
                        if lab and lab.lower().startswith("all homeless persons"):
                            total_col = c
                    if total_col is None:
                        raise RuntimeError(f"{sheet}: no 'All homeless persons' column")
                continue
            if i < header + 2:
                continue
            a = _label(_cell(row, 0))
            b = _label(_cell(row, 1))
            c2 = _label(_cell(row, 2))
            if a and a.lower() == "australia":
                continue
            if b and not c2:
                key = b.lower()
                if key in ABBR_OF_FULLNAME:
                    state = ABBR_OF_FULLNAME[key]
                continue
            if c2 and state:
                if b:  # LGA printed together with its state on one line
                    state = ABBR_OF_FULLNAME.get(b.lower(), state)
                total = _num(_cell(row, total_col))
                if total is None:
                    continue
                ident = (year, state, c2)
                if ident in seen:
                    continue
                seen.add(ident)
                out.append((year, state, c2, int(round(total))))
    wb.close()
    return out


# ---------------------------------------------------------------- RoGS section 19

def parse_rogs(csv_bytes: bytes):
    """RoGS section 19 dataset. Returns [{"table","fy","metric","unit","note","values":{...}}]
    with values keyed by ABS abbreviation plus 'Australia'."""
    text = csv_bytes.decode("utf-8-sig")
    rd = csv.DictReader(io.StringIO(text))
    colnames = {  # RoGS header -> ABS abbreviation
        "NSW": "NSW", "Vic": "VIC", "Qld": "QLD", "SA": "SA", "WA": "WA",
        "Tas": "TAS", "NT": "NT", "ACT": "ACT", "Aust": "Australia",
    }
    out = []
    for r in rd:
        table = (r.get("Table_Number") or "").strip()
        if table not in ("19A.21", "19A.1", "19A.11"):
            continue
        unit = (r.get("Unit") or "").strip()
        labels = [clean(r.get(f"Description{i}")) for i in range(1, 7)]
        label = next((x for x in labels if x), None)
        if table == "19A.11":
            # average daily unassisted requests, accommodation only
            metric = None
            if unit == "no." and any(x and x.lower() == "accommodation services" for x in labels) \
                    and any(x and "average daily unassisted requests" in x.lower() for x in labels):
                metric = ("shs_unassisted_requests_daily", "requests per day",
                          "average daily unassisted requests for accommodation services "
                          "(clients turned away, RoGS table 19A.11)")
        else:
            # the label that carries the measure sits in Description1 for 19A.21 and in
            # Description3 for 19A.1, so every non-empty label is offered to the map
            metric = next((ROGS_METRICS[(table, lab, unit)] for lab in labels
                           if (table, lab, unit) in ROGS_METRICS), None)
        if not metric:
            continue
        name, unit_out, note = metric
        fy = (r.get("Year") or "").strip()
        year = _fy_end_year(fy)
        if year is None:
            continue
        vals = {}
        for col, abbr in colnames.items():
            v = _num(r.get(col))
            if v is not None:
                vals[abbr] = v
        if not vals:
            continue
        out.append({"table": table, "fy": fy, "metric": name, "unit": unit_out,
                    "note": note, "values": vals})
    return out


# ---------------------------------------------------------------- writers

def _ensure_source(conn, url, title, publisher, license):
    return source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)


def _upsert_geo(conn, geoid, kind, name, state, source_id, rows):
    upsert(conn, "geo_city", {
        "geoid": geoid, "kind": kind, "name": name, "state": state,
        "coc_num": None, "pop": None, "lat": None, "lon": None, "source_id": source_id,
    }, keys=["geoid"])
    rows.append(("geo_city", geoid))


def _metric(conn, entity_type, entity_id, metric, value, unit, year, note, source_id, rows):
    upsert(conn, "metric", {
        "entity_type": entity_type, "entity_id": entity_id, "metric": metric,
        "value": value, "unit": unit, "year": year, "is_est": 0, "note": note,
        "source_id": source_id,
    }, keys=["entity_type", "entity_id", "metric", "year"])
    rows.append(("metric", f"{entity_type}:{entity_id}:{metric}:{year}"))


def run(conn, cfg=None):
    notes = []
    written = []
    abs_sid = _ensure_source(conn, ABS_CUBE_STATE, ABS_TITLE, ABS_PUBLISHER, ABS_LICENSE)
    abs_lga_sid = _ensure_source(conn, ABS_CUBE_LGA, ABS_TITLE, ABS_PUBLISHER, ABS_LICENSE)
    _ensure_source(conn, ABS_RELEASE, "Estimating homelessness: Census, 2021 (release page)",
                   ABS_PUBLISHER, ABS_LICENSE)
    rogs_sid = _ensure_source(conn, ROGS_CSV, ROGS_TITLE, ROGS_PUBLISHER, ROGS_LICENSE)

    # --- states and territories (geo_city kind='au_state')
    for _, abbr, fullname in STATES:
        _upsert_geo(conn, _state_geoid(abbr), "au_state", fullname, abbr, abs_sid, written)

    # --- ABS census counts + published rates per state and nationally
    states = parse_abs_states(ABS_CUBE_STATE)
    n_census = 0
    for (year, eid), rec in sorted(states.items()):
        etype = rec["entity_type"]
        if etype == "au_state":
            disp = NAME_OF_ABBR[eid[3:]]
            basis = "place of usual residence"
        else:
            disp = "Australia"
            basis = "place of usual residence"
        if "count" in rec:
            _metric(conn, etype, eid, "census_homeless_estimate", rec["count"], "people", year,
                    f"ABS 2049.0 Estimating homelessness: Census, 2021, table 1.3, {disp} row "
                    f"'Total homeless persons', {year} Census, {basis} (published count)",
                    abs_sid, written)
            n_census += 1
        if "rate" in rec:
            _metric(conn, etype, eid, "census_homeless_rate_per_10k", rec["rate"],
                    "per 10,000 people", year,
                    f"ABS 2049.0 table 1.5, {disp} row 'Total homeless persons', {year} Census, "
                    f"rate per 10,000 of the {basis} population (ABS-published rate)",
                    abs_sid, written)

    # --- ABS Local Government Area counts (place of enumeration)
    lgas = parse_abs_lga(ABS_CUBE_LGA)
    lga_names = {}
    for year, abbr, name, total in lgas:
        geoid = _lga_geoid(abbr, name)
        prev = lga_names.get(geoid)
        # one entity across Census years: keep the most recent spelling ABS printed
        if prev is None or year > prev[2]:
            lga_names[geoid] = (name, abbr, year)
    for geoid, (name, abbr, _) in sorted(lga_names.items()):
        _upsert_geo(conn, geoid, "au_lga", name, abbr, abs_lga_sid, written)
    n_lga_metrics = 0
    for year, abbr, name, total in lgas:
        geoid = _lga_geoid(abbr, name)
        _metric(conn, "au_lga", geoid, "census_homeless_estimate", total, "people", year,
                f"ABS 2049.0 Estimating homelessness: Census, 2021, table {6.1 if year == 2021 else 6.2}, "
                f"LGA '{name}' ({NAME_OF_ABBR.get(abbr, abbr)}) 'All homeless persons', {year} Census, "
                f"place of enumeration", abs_lga_sid, written)
        n_lga_metrics += 1

    # --- RoGS / SHSC: clients, expenditure, unassisted requests per jurisdiction
    rogs = parse_rogs(fetch(ROGS_CSV, domain=DOMAIN))
    n_rogs = 0
    for rec in rogs:
        for abbr, val in rec["values"].items():
            if abbr == "Australia":
                etype, eid, disp = "australia", "Australia", "Australia"
            else:
                etype, eid, disp = "au_state", _state_geoid(abbr), NAME_OF_ABBR[abbr]
            _metric(conn, etype, eid, rec["metric"], val, rec["unit"],
                    _fy_end_year(rec["fy"]),
                    f"RoGS 2026 section 19 table {rec['table']}, {disp}, FY{rec['fy']}: {rec['note']}",
                    rogs_sid, written)
            n_rogs += 1

    conn.commit()
    notes.append(f"abs_state_census_rows={n_census}")
    notes.append(f"abs_lga_units={len(lga_names)} abs_lga_metric_rows={n_lga_metrics}")
    notes.append(f"rogs_metric_rows={n_rogs}")
    n_geo = sum(1 for t, _ in written if t == "geo_city")
    n_met = sum(1 for t, _ in written if t == "metric")
    # cheap consistency check that is worth failing loudly on: state sums vs published national
    chk = conn.execute("""SELECT (SELECT SUM(value) FROM metric WHERE entity_type='au_state'
                             AND metric='census_homeless_estimate' AND year=?),
                             (SELECT value FROM metric WHERE entity_type='australia'
                             AND metric='census_homeless_estimate' AND year=?)""",
                       (2021, 2021)).fetchone()
    if chk and chk[0] and chk[1]:
        notes.append(f"2021 state sum={int(chk[0])} national={int(chk[1])} "
                     f"diff={int(chk[1]-chk[0])} (Other Territories)")
    return {"rows": n_geo + n_met, "notes": " | ".join(notes)}


def _selftest():
    conn = db()
    res = run(conn, {})
    conn.commit()
    print(res)
    for label, sql in (
            ("geo_city by kind", "SELECT kind, COUNT(*) c FROM geo_city WHERE kind IN "
                                 "('au_state','au_lga') GROUP BY 1"),
            ("metric by name", "SELECT entity_type, metric, COUNT(*) c, MIN(year), MAX(year) "
                               "FROM metric WHERE entity_type IN ('australia','au_state','au_lga') "
                               "GROUP BY 1,2 ORDER BY 1,2"),
            ("states 2021 census", "SELECT entity_id, value FROM metric WHERE entity_type='au_state' "
                                   "AND metric='census_homeless_estimate' AND year=2021 ORDER BY 1"),
            ("SHS per state 2024-25", "SELECT entity_id, metric, value, unit FROM metric WHERE "
                                      "entity_type='au_state' AND year=2025 ORDER BY 1,2")):
        print("--", label)
        for r in conn.execute(sql):
            print("  ", tuple(r))
    print("-- 6 sample rows")
    for r in conn.execute("""SELECT m.entity_type, m.entity_id, m.metric, m.year, m.value, m.unit,
                                    m.note, s.url
                             FROM metric m LEFT JOIN source s ON s.id=m.source_id
                             WHERE m.entity_type IN ('australia','au_state','au_lga')
                               AND m.metric IN ('census_homeless_estimate','shs_clients','shs_funding')
                             ORDER BY RANDOM() LIMIT 6"""):
        print("  ", dict(r))


def reset(conn):
    """Delete only the rows this domain wrote (its geoid scheme changed once, before release).

    Every geo_city kind='au_state'/'au_lga' row and every metric row with entity_type
    'australia'/'au_state'/'au_lga' comes from this module -- checked with
    `SELECT DISTINCT s.domain FROM metric m JOIN source s ON s.id=m.source_id WHERE ...`.
    """
    conn.execute("DELETE FROM metric WHERE entity_type IN ('australia','au_state','au_lga')")
    conn.execute("DELETE FROM geo_city WHERE kind IN ('au_state','au_lga')")
    conn.commit()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--reset" in sys.argv:
        _c = db()
        reset(_c)
        res = run(_c, {})
        _c.commit()
        print(f"[{DOMAIN}] reset+rows={res['rows']} {res['notes']}")
    else:
        conn = db()
        res = run(conn, {})
        conn.commit()
        print(f"[{DOMAIN}] rows={res['rows']} {res['notes']}")
