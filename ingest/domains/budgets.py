"""budgets — what each level of government spends on homelessness.

Four layers, every number traceable to a cached response under `data/raw/budgets/`:

1. **Federal dollars per state, two independent ways**
   a. `award` — the rows the `usaspending` domain already ingested from the
      USASpending.gov v2 search API for HUD's homelessness Assistance Listings
      (14.231 ESG, 14.235 SHP, 14.267 CoC, 14.276 YHDP, 14.241 HOPWA) are
      aggregated by (place_state, fiscal year, program).
   b. USASpending `/api/v2/search/spending_by_geography/`, scope
      place_of_performance, geo_layer=state, one call per program per FY — the
      cross-check on (a) for the two most recent complete fiscal years.

2. **Federal dollars per CoC**
   a. county-level spending_by_geography rows are attributed to the CoC whose
      name/city list contains the county name (same state, unique match only);
   b. `award` rows are attributed to a CoC when the recipient name contains the
      name of a city that `geo_city` assigns to that CoC.
   Both are modelled (`is_est=1`) and both carry the formula in `note`.

3. **State's own money** — appropriation rows in a state budget dataset whose
   programme/unit/sub-programme name contains "homeless" (Maryland operating
   budget now; the loader is generic, see STATE_OWN).

4. **City / county budgets** — each jurisdiction's own open-data budget or
   expenditure table, filtered to the published lines that name homelessness
   (see CITY_BUDGETS). One row per published line in `budgets_city_line`, one
   rolled-up metric per jurisdiction-year.

Derived metrics (`is_est=1`, formula in `note`): spend per resident and spend
per person counted homeless (HUD PIT, latest year for that CoC/state).

Interface: `def run(conn, cfg) -> dict`
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.parse

from ingest.common import (
    RAW,
    clean,
    fetch_json,
    fetch_post_json,
    now,
    source_id_for,
    upsert,
)

DOMAIN = "budgets"

USA_GEO = "https://api.usaspending.gov/api/v2/search/spending_by_geography/"
USA_AWARD = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
PUB_TREASURY = "U.S. Department of the Treasury, Bureau of the Fiscal Service (USASpending.gov)"
LIC_FED = "US Government work, public domain"

# Assistance Listing (CFDA) -> (metric stem, program name)
AWARD_PROGRAMS = {
    "14.231": ("esg", "Emergency Solutions Grant (ESG)"),
    "14.235": ("shp", "Supportive Housing Program (legacy CoC program, pre-2012)"),
    "14.267": ("coc", "Continuum of Care (CoC)"),
    "14.276": ("yhdp", "Youth Homelessness Demonstration Program (YHDP)"),
    "14.241": ("hopwa", "Housing Opportunities for Persons With AIDS (HOPWA)"),
}

# fiscal years pulled from the geography API (start, end)
FYS = [
    (2024, "2023-10-01", "2024-09-30"),
    (2023, "2022-10-01", "2023-09-30"),
]

# ---------------------------------------------------------------------------
# Jurisdiction budgets from each government's own open-data budget table.
#   where      SoQL filter over the published line labels/columns
#   amount     money column in that table
#   group      columns that identify a line (kept in budgets_city_line)
#   year       fiscal year written to metric.year (or fy_col = derive it)
#   basis      plain-English description of what the filter selects
# ---------------------------------------------------------------------------
CITY_BUDGETS = [
    dict(
        city="San Francisco", state="CA", kind="place", host="data.sfgov.org", ds="xdgd-c79v",
        where="fiscal_year='2025' AND revenue_or_spending='Spending' AND lower(department) like '%homeless%'",
        amount="budget", group=["department"], year=2025,
        basis="adopted budget, all funds: every department line whose name contains 'Homeless'",
    ),
    dict(
        city="New York", state="NY", kind="place", host="data.cityofnewyork.us", ds="mwzb-yiwb",
        where="fiscal_year='2025' AND agency_name='DEPARTMENT OF HOMELESS SERVICES'",
        amount="adopted_budget_amount", group=["unit_appropriation_name"], year=2025,
        dedupe_col="publication_date",
        basis="adopted expense budget of the Department of Homeless Services, newest publication for the FY",
    ),
    dict(
        city="Los Angeles", state="CA", kind="place", host="data.lacity.org", ds="98ve-cuf5",
        where="fiscal_year='2025'", amount="pstng_am", group=["dept_nm"], year=2025,
        metric="city_homelessness_expenditure",
        basis="payments posted in the city's Homelessness Expense Tracker (expenditure actually recorded, not appropriation)",
    ),
    dict(
        city="Seattle", state="WA", kind="place", host="data.seattle.gov", ds="8u2j-imqx",
        where="fiscal_year='2025' AND lower(program) like '%homeless%'",
        amount="approved_amount", group=["department", "program"], year=2025,
        basis="approved operating budget of city programmes whose name contains 'Homeless'",
    ),
    dict(
        city="Chicago", state="IL", kind="place", host="data.cityofchicago.org", ds="x394-e874",
        where="lower(appropriation_account_description) like '%homeless%'",
        amount="_ordinance_amount_", group=["department_description", "appropriation_account_description"],
        basis="budget-ordinance appropriations whose appropriation account description contains 'Homeless'",
    ),
    dict(
        city="Austin", state="TX", kind="place", host="data.austintexas.gov", ds="yeeq-kk6v",
        where="fy='2025' AND lower(dept_nm) like '%homeless%'", amount="bud", group=["dept_nm", "div_nm"],
        year=2025,
        basis="FY2025 adopted budget of every department whose name contains 'Homeless'",
    ),
    dict(
        city="Mesa", state="AZ", kind="place", host="citydata.mesaaz.gov", ds="29q3-4ewh",
        where="lower(core_business_process_name) like '%homeless%' OR lower(activity_name) like '%homeless%'",
        amount="adopted_budget", group=["department_name", "core_business_process_name", "activity_name"],
        fy_col="fiscal_year", nonzero=True,
        basis="adopted budget of budget lines whose core business process or activity name contains 'Homeless' (latest FY with a non-zero amount)",
    ),
    dict(
        city="Richmond", state="VA", kind="place", host="data.richmondgov.com", ds="7nru-hsrx",
        where="lower(service_descr) like '%homeless%'", amount="fy16_adopted",
        group=["agency_descr", "service_descr"], year=2016,
        basis="FY2016 adopted General Fund budget lines whose service description contains 'Homeless'",
    ),
    dict(
        city="San Mateo County", state="CA", kind="county", host="data.smcgov.org", ds="8w4u-sjiw",
        where="1=1", amount="appropriations", group=["orginization_description", "account_description"],
        year=2014,
        basis="every line of the county's published list of FY2013/14 budget items related to homelessness",
    ),
    dict(
        city="Montgomery County", state="MD", kind="county", host="data.montgomerycountymd.gov", ds="e543-hana",
        where="lower(account_name) like '%homeless%'", amount="approved_amount",
        group=["department", "program", "account_name"], fy_col="fiscal_year",
        basis="approved operating-budget lines whose account name contains 'Homeless' (latest FY present in the dataset)",
    ),
]

# State's own appropriations, from the state's budget dataset.
STATE_OWN = [
    dict(
        state="MD", host="opendata.maryland.gov", ds="yu65-jmmv",
        where=("lower(program_name) like '%homeless%' OR lower(unit_name) like '%homeless%' "
               "OR lower(subprogram_name) like '%homeless%'"),
        amount="budget", group=["unit_name", "program_name", "subprogram_name"],
        fy_col="fiscal_year", fy_kind="Budget - Actual",
        basis=("Maryland operating budget rows whose programme, unit or sub-programme name contains 'Homeless', "
               "using the 'Budget - Actual' figures of the latest fiscal year present"),
    ),
]

COUNTY_CUT = re.compile(
    r"\s+(COUNTY|PARISH|BOROUGH|CENSUS AREA|MUNICIPALITY|CITY AND BOROUGH|ISLAND)$", re.I)

BUSY_TRIES = 40
WRITE_COUNT = [0]
CITY_MIN_POP = 40000


# ---------------------------------------------------------------------------
# sqlite helpers: other domain workers hold write locks on atlas.db
# ---------------------------------------------------------------------------
def _retry(fn, *a, **kw):
    last = None
    for i in range(BUSY_TRIES):
        try:
            return fn(*a, **kw)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            last = e
            time.sleep(min(2 + i, 15))
    raise last


def _x(conn, sql, params=()):
    return _retry(conn.execute, sql, params)


def _up(conn, table, row, keys):
    return _retry(upsert, conn, table, row, keys)


def _commit(conn):
    return _retry(conn.commit)


def _ensure(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS budgets_city_line (
          entity    TEXT NOT NULL,
          entity_id TEXT,
          state     TEXT NOT NULL,
          fy        TEXT NOT NULL,
          label     TEXT NOT NULL,
          amount    REAL,
          dataset   TEXT,
          source_id INTEGER,
          PRIMARY KEY (entity, state, fy, label)
        );
        CREATE TABLE IF NOT EXISTS budgets_state_line (
          state     TEXT NOT NULL,
          fy        TEXT NOT NULL,
          label     TEXT NOT NULL,
          amount    REAL,
          dataset   TEXT,
          source_id INTEGER,
          PRIMARY KEY (state, fy, label)
        );
        CREATE TABLE IF NOT EXISTS budgets_progress (
          step      TEXT PRIMARY KEY,
          done_at   TEXT,
          detail    TEXT
        );
        """
    )
    # This domain's private tables were created once without the keys these
    # loaders upsert on (ON CONFLICT needs a matching unique index). Bring them
    # into the shape this file expects, keeping whatever an earlier run wrote.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(budgets_city_line)")]
    if "entity" not in cols:
        conn.executescript("ALTER TABLE budgets_city_line RENAME TO budgets_city_line_legacy;")
        conn.executescript(
            """CREATE TABLE IF NOT EXISTS budgets_city_line (
                 entity    TEXT NOT NULL, entity_id TEXT, state TEXT NOT NULL,
                 fy        TEXT NOT NULL, label TEXT NOT NULL, amount REAL,
                 dataset   TEXT, source_id INTEGER,
                 PRIMARY KEY (entity, state, fy, label));"""
        )
    conn.execute("DELETE FROM budgets_progress WHERE rowid NOT IN "
                 "(SELECT MIN(rowid) FROM budgets_progress GROUP BY step)")
    for sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_budgets_progress ON budgets_progress(step)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_budgets_state_line ON budgets_state_line(state,fy,label)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_budgets_city_line ON budgets_city_line(entity,state,fy,label)",
    ):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            print(f"[budgets] index not created ({e}): {sql}")
    conn.commit()


def _mark(conn, step, detail=""):
    _x(conn, "INSERT INTO budgets_progress(step,done_at,detail) VALUES(?,?,?) "
          "ON CONFLICT(step) DO UPDATE SET done_at=excluded.done_at, detail=excluded.detail",
       (step, now(), str(detail)[:500]))
    _commit(conn)


def _source(conn, url, *, title, publisher, license, method="GET", payload=None):
    sid = source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)
    key = hashlib.sha1((method + url + json.dumps(payload or {}, sort_keys=True, default=str)).encode()).hexdigest()
    p = RAW / DOMAIN / f"{key}.bin"
    if p.exists():
        b = p.read_bytes()
        _x(conn, "UPDATE source SET bytes=?, sha256=?, retrieved_at=? WHERE id=?",
           (len(b), hashlib.sha256(b).hexdigest(), now(), sid))
    return sid


def _metric(conn, entity_type, entity_id, metric, value, unit, year, source_id,
            is_est=0, note=None):
    if value is None or entity_id in (None, ""):
        return
    WRITE_COUNT[0] += 1
    _up(conn, "metric", {
        "entity_type": entity_type, "entity_id": str(entity_id), "metric": metric,
        "value": float(value), "unit": unit, "year": int(year) if year else None,
        "is_est": is_est, "note": note, "source_id": source_id,
    }, ["entity_type", "entity_id", "metric", "year"])


# ---------------------------------------------------------------------------
# Socrata helpers (all cached through common.fetch -> data/raw/budgets/)
# ---------------------------------------------------------------------------
def _soql(host, ds, params):
    u = f"https://{host}/resource/{ds}.json?" + urllib.parse.urlencode(params)
    return u, fetch_json(u, domain=DOMAIN, retries=2, timeout=240)


def _dataset_meta(host, ds):
    """Official dataset title/description from the portal API (for source rows)."""
    u = f"https://{host}/api/views/{ds}.json"
    try:
        j = fetch_json(u, domain=DOMAIN, retries=2, timeout=90)
        return u, clean(j.get("name")), clean(j.get("description"))
    except Exception:
        return u, None, None


def _money(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fy_int(v):
    try:
        return int(str(v)[:4])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1a. federal obligations per state, from the `award` table
# ---------------------------------------------------------------------------
def _state_from_award(conn, stats):
    sid = _source(
        conn, USA_AWARD,
        title="USASpending.gov API v2 spending_by_award — HUD homelessness Assistance Listings "
              "14.231/14.235/14.267/14.276/14.241 (rows aggregated from this atlas's `award` table)",
        publisher=PUB_TREASURY, license=LIC_FED, method="POST")

    cf = ",".join("'%s'" % c for c in AWARD_PROGRAMS)
    rows = conn.execute(
        f"""SELECT UPPER(place_state) st, fy, cfda AS cfda,
                   SUM(COALESCE(amount_obligated, amount_total)) amt, COUNT(*) n
              FROM award
             WHERE cfda IN ({cf}) AND place_state IS NOT NULL AND length(place_state)=2
               AND fy GLOB '[0-9][0-9][0-9][0-9]'
             GROUP BY 1,2,3""").fetchall()

    per_state_fy = {}
    for r in rows:
        st, fy, cfda, amt, n = r["st"], int(r["fy"]), r["cfda"], r["amt"], r["n"]
        if not st or amt is None:
            continue
        stem, pname = AWARD_PROGRAMS[cfda]
        per_state_fy.setdefault((st, fy), {"total": 0.0, "n": 0})["total"] += float(amt)
        per_state_fy[(st, fy)]["n"] += n
        _metric(conn, "state", st, f"award_hud_{stem}_obligated", amt, "USD", fy, sid,
                note=f"{pname}: obligations recorded in the atlas `award` table with place of "
                     f"performance in {st} for federal fiscal year {fy} "
                     f"({n:,} award rows; USASpending assistance listing {cfda})")

    for (st, fy), d in sorted(per_state_fy.items()):
        _metric(conn, "state", st, "award_hud_homeless_obligated", d["total"], "USD", fy, sid,
                note=f"formula: sum of HUD ESG + SHP + CoC + YHDP + HOPWA obligations in the atlas "
                     f"`award` table whose place of performance is {st}, federal FY{fy} "
                     f"({d['n']:,} award rows)")

    # derived: latest fiscal year with a figure per state
    latest = {}
    for (st, fy), d in per_state_fy.items():
        if d["total"] > 0 and (st not in latest or fy > latest[st][0]):
            latest[st] = (fy, d["total"])
    pop = {r["abbr"]: r["pop"] for r in conn.execute("SELECT abbr,pop FROM geo_state")}
    pit = _state_pit(conn)
    for st, (fy, total) in sorted(latest.items()):
        if pop.get(st):
            _metric(conn, "state", st, "award_hud_homeless_per_capita", total / pop[st],
                    "USD/person", fy, sid, is_est=1,
                    note=f"formula: award-table HUD homeless obligations for {st} in FY{fy} "
                         f"(${total:,.0f}) / {pop[st]:,} residents (Census population in geo_state)")
        py, ptot = pit.get(st, (None, None))
        if ptot:
            _metric(conn, "state", st, "award_hud_homeless_per_homeless", total / ptot,
                    "USD/person", fy, sid, is_est=1,
                    note=f"formula: award-table HUD homeless obligations for {st} in FY{fy} "
                         f"(${total:,.0f}) / {ptot:,} people counted homeless in the state in {py} "
                         f"(sum of the state's CoC PIT totals)")
    _commit(conn)
    stats["award_states"] = len(latest)
    stats["award_rows"] = len(rows)
    return latest


def _state_pit(conn):
    """state abbr -> (year, people counted homeless) from `pit` joined to geo_coc."""
    out = {}
    for r in conn.execute(
            """SELECT g.state AS st, p.year AS y, SUM(p.total) AS tot
                 FROM pit p JOIN geo_coc g ON g.coc_num = p.coc_num
                WHERE p.total IS NOT NULL
                GROUP BY g.state, p.year"""):
        st, y, tot = r["st"], r["y"], r["tot"]
        if not st or not tot:
            continue
        if st not in out or y > out[st][0]:
            out[st] = (y, int(tot))
    return out


# ---------------------------------------------------------------------------
# 1b. federal obligations per state, from the USASpending geography API
# ---------------------------------------------------------------------------
def _geo(programs, start, end, layer, timeout=240):
    payload = {
        "scope": "place_of_performance",
        "geo_layer": layer,
        "filters": {"program_numbers": list(programs),
                    "time_period": [{"start_date": start, "end_date": end}]},
        "subawards": False,
    }
    return fetch_post_json(USA_GEO, payload, domain=DOMAIN, retries=3, timeout=timeout)


def _state_geo(conn, stats):
    sid = _source(
        conn, USA_GEO,
        title="USASpending.gov API v2 spending_by_geography, place of performance, geo_layer=state "
              "(HUD homeless assistance programs 14.231/14.267/14.276/14.241, FY2023-FY2024)",
        publisher=PUB_TREASURY, license=LIC_FED, method="POST")

    per_state = {}
    pops = {}
    for fy, start, end in FYS:
        for cfda, (stem, pname) in AWARD_PROGRAMS.items():
            if cfda not in ("14.231", "14.267", "14.276", "14.241"):
                continue
            try:
                j = _geo([cfda], start, end, "state")
            except Exception as e:
                stats["notes"].append(f"geo FY{fy} {cfda}: {repr(e)[:110]}")
                continue
            for r in j.get("results", []):
                abbr = clean(r.get("shape_code"))
                amt = r.get("aggregated_amount")
                if not abbr or amt is None or len(abbr) != 2:
                    continue
                pops[abbr] = r.get("population") or pops.get(abbr)
                per_state.setdefault((fy, abbr), {})[stem] = float(amt)
                _metric(conn, "state", abbr, f"hud_{stem}_obligated", amt, "USD", fy, sid,
                        note=f"{pname}: federal obligations in FY{fy} with place of performance in "
                             f"{abbr} (USASpending spending_by_geography, geo_layer=state)")
            stats["geo_calls"] += 1
    _commit(conn)

    pit = _state_pit(conn)
    n = 0
    for (fy, abbr), d in sorted(per_state.items()):
        total = sum(d.values())
        _metric(conn, "state", abbr, "hud_homeless_obligated_total", total, "USD", fy, sid,
                note=f"formula: sum of HUD ESG + CoC + YHDP + HOPWA obligations for {abbr} in FY{fy} "
                     f"from USASpending spending_by_geography (place of performance = {abbr})")
        n += 1
        pop = pops.get(abbr)
        if pop:
            _metric(conn, "state", abbr, "hud_homeless_obligated_per_capita", total / float(pop),
                    "USD/person", fy, sid, is_est=1,
                    note=f"formula: HUD homeless-assistance obligations FY{fy} (${total:,.0f}) / "
                         f"{int(pop):,} residents (population field returned by the geography API)")
        py, ptot = pit.get(abbr, (None, None))
        if ptot:
            _metric(conn, "state", abbr, "hud_homeless_obligated_per_homeless", total / ptot,
                    "USD/person", fy, sid, is_est=1,
                    note=f"formula: HUD homeless-assistance obligations FY{fy} (${total:,.0f}) / "
                         f"{ptot:,} people counted homeless in {abbr} in {py} (sum of CoC PIT totals)")
    _commit(conn)
    stats["geo_state_rows"] = n
    return n


# ---------------------------------------------------------------------------
# 2a. CoC money via county-level geography rows
# ---------------------------------------------------------------------------
def _coc_index(conn):
    index = {}
    for r in conn.execute("SELECT coc_num, name, state, cities FROM geo_coc"):
        st = (r["state"] or "").upper()
        names = set()
        for blob in (r["name"], r["cities"]):
            if not blob:
                continue
            for tok in re.split(r"[,/;&]| and ", blob):
                tok = re.sub(r"\bCoC\b", "", tok, flags=re.I)
                tok = re.sub(r"\b(city|county|counties|balance of state|continuum of care)\b", "",
                             tok, flags=re.I)
                tok = re.sub(r"[^A-Za-z ]+", " ", tok)
                tok = re.sub(r"\s+", " ", tok).strip().upper()
                if len(tok) > 2:
                    names.add(tok)
        index.setdefault(st, []).append((r["coc_num"], (r["name"] or "").upper(), names))
    return index


def _pit_latest(conn):
    out = {}
    for r in conn.execute("SELECT coc_num, year, total FROM pit WHERE total IS NOT NULL AND total > 0"):
        c = r["coc_num"]
        if c not in out or r["year"] > out[c][0]:
            out[c] = (r["year"], r["total"])
    return out


def _coc_from_county(conn, stats):
    index = _coc_index(conn)
    counties = {r["geoid"]: (r["name"], (r["state"] or "").upper())
                for r in conn.execute("SELECT geoid,name,state FROM geo_city WHERE kind='county'")}

    fy, start, end = FYS[0]
    agg, matched, seen = {}, 0, 0
    got = {}
    for cfda in ("14.267", "14.231"):
        try:
            got[cfda] = _geo([cfda], start, end, "county")
        except Exception as e:
            stats["notes"].append(f"county geography {cfda}: {repr(e)[:110]}")
            continue
        stats["geo_calls"] += 1

    for cfda, j in got.items():
        for r in j.get("results", []):
            fips = clean(r.get("shape_code"))
            amt = r.get("aggregated_amount")
            if not fips or amt is None:
                continue
            seen += 1
            cname, st = counties.get(fips, (None, None))
            if not st:
                continue
            base = COUNTY_CUT.sub("", (cname or "").upper()).strip()
            if not base:
                continue
            hits = {cn for cn, nm, toks in index.get(st, []) if base in toks or base in nm}
            if len(hits) != 1:
                continue
            matched += 1
            coc = hits.pop()
            agg.setdefault(coc, {}).setdefault(cfda, 0.0)
            agg[coc][cfda] += float(amt)

    sid = _source(
        conn, USA_GEO,
        title="USASpending.gov API v2 spending_by_geography, place of performance, geo_layer=county "
              "(HUD CoC 14.267 + ESG 14.231, FY2024), attributed to CoCs",
        publisher=PUB_TREASURY, license=LIC_FED, method="POST")
    pit = _pit_latest(conn)

    n = 0
    for coc, d in sorted(agg.items()):
        total = sum(d.values())
        _metric(conn, "coc", coc, "hud_coc_esg_obligated_county_attributed", total, "USD", fy, sid,
                is_est=1,
                note=f"formula: sum of HUD CoC {_fmt(d.get('14.267'))} + ESG {_fmt(d.get('14.231'))} "
                     f"county-level obligations in FY{fy} whose county was attributed to this CoC by "
                     f"county-name matching against the CoC name/city list (same state, unique match "
                     f"only); counties that matched no single CoC are excluded")
        n += 1
        py, ptot = pit.get(coc, (None, None))
        if ptot:
            _metric(conn, "coc", coc, "hud_coc_esg_obligated_per_homeless", total / ptot,
                    "USD/person", fy, sid, is_est=1,
                    note=f"formula: county-attributed HUD CoC+ESG obligations FY{fy} (${total:,.0f}) / "
                         f"{ptot:,} people in this CoC's {py} PIT count")
    _commit(conn)
    stats["coc_county_rows"] = n
    stats["coc_county_match"] = (matched, seen)
    return n


# ---------------------------------------------------------------------------
# 2b. CoC money via award recipients
# ---------------------------------------------------------------------------
def _locality_index(conn):
    """(state, UPPER name, min name length) -> (coc_num, geoid, name) for populated places/counties."""
    idx = {}
    for r in conn.execute(
            "SELECT geoid,kind,name,pop,coc_num FROM geo_city "
            "WHERE coc_num IS NOT NULL AND pop IS NOT NULL AND pop > ? "
            "AND kind IN ('place','county')", (CITY_MIN_POP,)):
        nm = (r["name"] or "").upper()
        if len(nm) < 6:
            continue
        idx.setdefault((r["state"] or "").upper(), []).append((nm, r["coc_num"], r["geoid"], r["pop"] or 0))
    return idx


def _coc_from_award(conn, stats):
    idx = _locality_index(conn)
    sid = _source(
        conn, USA_AWARD,
        title="USASpending.gov API v2 spending_by_award — HUD ESG/SHP/CoC awards from the `award` table, "
              "attributed to CoCs by recipient name",
        publisher=PUB_TREASURY, license=LIC_FED, method="POST")

    per = {}   # (coc, fy) -> amount
    for r in conn.execute(
            """SELECT UPPER(COALESCE(recipient_name,'')) rn, UPPER(place_state) st, fy,
                      COALESCE(amount_obligated, amount_total) amt
                 FROM award
                WHERE cfda IN ('14.231','14.235','14.267') AND place_state IS NOT NULL
                  AND fy GLOB '[0-9][0-9][0-9][0-9]' AND CAST(fy AS INTEGER) >= 2010""").fetchall():
        rn, st, fy, amt = r["rn"], r["st"], int(r["fy"]), r["amt"]
        if not rn or not st or amt is None:
            continue
        best = None
        for nm, coc, geoid, pop in idx.get(st, ()):
            if nm in rn and (best is None or len(nm) > len(best[0])):
                best = (nm, coc)
        if not best:
            continue
        key = (best[1], fy)
        per[key] = per.get(key, 0.0) + float(amt)

    pit = _pit_latest(conn)
    for (coc, fy), total in sorted(per.items()):
        _metric(conn, "coc", coc, "award_hud_coc_attributed", total, "USD", fy, sid, is_est=1,
                note="formula: sum of HUD ESG (14.231) + SHP (14.235) + CoC (14.267) award rows in the "
                     "atlas `award` table for this fiscal year whose recipient name contains the name "
                     "of a city/county that geo_city assigns to this CoC; recipient-name attribution, "
                     "not place of performance, so multi-city recipients are assigned to the "
                     f"longest matching place name")
        py, ptot = pit.get(coc, (None, None))
        if ptot and fy == max(k[1] for k in per if k[0] == coc):
            _metric(conn, "coc", coc, "award_hud_coc_attributed_per_homeless", total / ptot,
                    "USD/person", fy, sid, is_est=1,
                    note=f"formula: award-attributed HUD CoC-program dollars FY{fy} (${total:,.0f}) / "
                         f"{ptot:,} people in this CoC's {py} PIT count")
    _commit(conn)
    stats["coc_award_rows"] = len(per)
    return len(per)


def _fmt(v):
    return "n/a" if v is None else f"${v:,.0f}"


# ---------------------------------------------------------------------------
# 3. state's own appropriations
# ---------------------------------------------------------------------------
def _state_lines(spec):
    """HTTP phase for a state budget dataset: returns (url, fy, total, by_fy). No DB access."""
    st = spec["state"]
    sel = [spec["fy_col"]]
    grp = [spec["fy_col"]]
    if spec.get("fy_kind"):
        sel.append("type")
        grp.append("type")
    sel.append(f"sum({spec['amount']})")
    url, rows = _soql(spec["host"], spec["ds"], {
        "$select": ",".join(sel), "$where": spec["where"],
        "$group": ",".join(grp), "$limit": 5000,
    })
    if not rows:
        return url, None, None, {}
    amt_key = [k for k in rows[0] if k.startswith("sum_")][0]
    by_fy = {}
    for r in rows:
        if spec.get("fy_kind") and str(r.get("type")) != spec["fy_kind"]:
            continue
        fy = _fy_int(r.get(spec["fy_col"]))
        amt = _money(r.get(amt_key))
        if fy and amt is not None:
            by_fy[fy] = by_fy.get(fy, 0.0) + amt
    if not by_fy:
        return url, None, None, {}
    fy = max(by_fy)
    return url, fy, by_fy[fy], by_fy


def _state_own(conn, stats):
    for spec in STATE_OWN:
        st = spec["state"]
        sel = [spec["fy_col"]]
        grp = [spec["fy_col"]]
        if spec.get("fy_kind"):
            sel.append("type")
            grp.append("type")
        sel.append(f"sum({spec['amount']})")
        try:
            url, fy, total, by_fy = _state_lines(spec)
        except Exception as e:
            stats["notes"].append(f"state {st}: {repr(e)[:130]}")
            continue
        if fy is None:
            stats["notes"].append(f"state {st}: no rows of type {spec.get('fy_kind')}")
            continue

        meta_url, meta_title, _ = _dataset_meta(spec["host"], spec["ds"])
        sid = _source(conn, url,
                      title=f"{meta_title or 'State budget dataset'} ({spec['host']}/{spec['ds']}): "
                            f"rows for {st} homelessness programmes",
                      publisher=f"State of {st} open data portal",
                      license="State open data portal terms (public record)")
        _up(conn, "budgets_state_line", {
            "state": st, "fy": str(fy), "label": spec["basis"][:300],
            "amount": total, "dataset": f"{spec['host']}/{spec['ds']}", "source_id": sid,
        }, ["state", "fy", "label"])

        _metric(conn, "state", st, "state_homelessness_appropriation", total, "USD", fy, sid,
                note=f"{st} state budget: {spec['basis']}; filter [{spec['where']}] on "
                     f"{spec['host']}/{spec['ds']}, latest fiscal year present = {fy}")
        pop = conn.execute("SELECT pop FROM geo_state WHERE abbr=?", (st,)).fetchone()
        if pop and pop["pop"]:
            _metric(conn, "state", st, "state_homelessness_appropriation_per_capita",
                    total / pop["pop"], "USD/person", fy, sid, is_est=1,
                    note=f"formula: {st} state homelessness appropriation FY{fy} (${total:,.0f}) / "
                         f"{pop['pop']:,} residents (Census population in geo_state)")
        py, ptot = _state_pit(conn).get(st, (None, None))
        if ptot:
            _metric(conn, "state", st, "state_homelessness_appropriation_per_homeless",
                    total / ptot, "USD/person", fy, sid, is_est=1,
                    note=f"formula: {st} state homelessness appropriation FY{fy} (${total:,.0f}) / "
                         f"{ptot:,} people counted homeless in {st} in {py} (sum of CoC PIT totals)")
        _commit(conn)
        stats["state_own"].append(f"{st} FY{fy} ${total:,.0f}")


# ---------------------------------------------------------------------------
# 4. city / county budgets
# ---------------------------------------------------------------------------
def _jurisdiction(conn, spec):
    kind = spec.get("kind", "place")
    r = conn.execute(
        "SELECT geoid,pop,coc_num,name FROM geo_city WHERE name=? AND state=? AND kind=? "
        "ORDER BY pop DESC LIMIT 1", (spec["city"], spec["state"], kind)).fetchone()
    return r


def _city_lines(spec):
    """HTTP phase for one jurisdiction budget: (url, rows, amt_key, where, newest, year).

    Pure fetch (no DB), so `--warm` can populate data/raw/budgets/ before the write lock
    is free. Returns None when the table has no matching lines.
    """
    amt = spec["amount"]

    # 1. which fiscal year: fixed in the spec, or the latest present in the table
    year = spec.get("year")
    if spec.get("fy_col") and not year:
        _, fyrows = _soql(spec["host"], spec["ds"], {
            "$select": f"{spec['fy_col']},sum({amt})", "$where": spec["where"],
            "$group": spec["fy_col"], "$limit": 5000})
        key = [k for k in fyrows[0] if k.startswith("sum_")][0] if fyrows else None
        cands = {_fy_int(r.get(spec["fy_col"])): _money(r.get(key)) for r in fyrows}
        cands = {y: v for y, v in cands.items() if y and v is not None}
        if spec.get("nonzero"):
            cands = {y: v for y, v in cands.items() if v > 0}
        if not cands:
            return None
        year = max(cands)

    where = spec["where"]
    if spec.get("fy_col") and spec.get("year") is None:
        where = f"({where}) AND {spec['fy_col']}='{year}'"

    # 2. tables that publish the same fiscal year several times: keep the newest publication
    newest = None
    if spec.get("dedupe_col"):
        _, drows = _soql(spec["host"], spec["ds"], {
            "$select": f"{spec['dedupe_col']},sum({amt})", "$where": where,
            "$group": spec["dedupe_col"], "$limit": 5000})
        pubs = sorted(str(r.get(spec["dedupe_col"])) for r in drows if r.get(spec["dedupe_col"]))
        newest = pubs[-1]
        where = f"({where}) AND {spec['dedupe_col']}='{newest}'"

    url, rows = _soql(spec["host"], spec["ds"], {
        "$select": ",".join(spec["group"] + [f"sum({amt})"]),
        "$where": where, "$group": ",".join(spec["group"]), "$limit": 5000})
    if not rows:
        return None
    amt_key = [k for k in rows[0] if k.startswith("sum_")][0]
    return url, rows, amt_key, where, newest, year


def _city_budgets(conn, stats):
    for spec in CITY_BUDGETS:
        city, state = spec["city"], spec["state"]
        metric = spec.get("metric", "city_homelessness_budget")
        etype = "county" if spec.get("kind") == "county" else "city"
        try:
            got = _city_lines(spec)
        except Exception as e:
            stats["notes"].append(f"{city}: {repr(e)[:130]}")
            continue
        if not got:
            stats["notes"].append(f"{city}: query returned no lines")
            continue
        url, rows, amt_key, where, newest, year = got

        meta_url, meta_title, _ = _dataset_meta(spec["host"], spec["ds"])
        if year is None and meta_title:
            # no fiscal-year column in the table: the FY is in the dataset title
            m = re.search(r"\b(19|20)\d{2}\b", meta_title)
            year = int(m.group(0)) if m else None
        sid = _source(conn, url,
                      title=f"{meta_title or city + ' budget dataset'} ({spec['host']}/{spec['ds']}) — "
                            f"homelessness lines used for {city}, {state}",
                      publisher=f"City of {city} open data portal",
                      license="City open data portal terms (public record)")

        j = _jurisdiction(conn, spec)
        eid = j["geoid"] if j else city
        total = 0.0
        kept = 0
        for r in rows:
            v = _money(r.get(amt_key))
            if v is None:
                continue
            label = " | ".join(str(r.get(c) or "") for c in spec["group"]).strip(" |") or spec["basis"]
            total += v
            kept += 1
            _up(conn, "budgets_city_line", {
                "entity": city, "entity_id": eid, "state": state, "fy": str(year or ""),
                "label": label[:300], "amount": v,
                "dataset": f"{spec['host']}/{spec['ds']}", "source_id": sid,
            }, ["entity", "state", "fy", "label"])

        _metric(conn, etype, eid, metric, total, "USD", year, sid,
                note=f"{city}, {state}: {spec['basis']}; filter [{where}] on "
                     f"{spec['host']}/{spec['ds']}; {kept} published line(s) summed"
                     + (f"; newest {spec['dedupe_col']} {newest} used" if newest else ""))
        if j and j["pop"]:
            _metric(conn, etype, eid, f"{metric}_per_capita", total / j["pop"], "USD/person",
                    year, sid, is_est=1,
                    note=f"formula: {metric} ({city}, ${total:,.0f}) / {j['pop']:,} residents "
                         f"(Census population in geo_city)")
        if j and j["coc_num"]:
            pr = conn.execute(
                "SELECT year,total FROM pit WHERE coc_num=? AND total IS NOT NULL AND total>0 "
                "ORDER BY year DESC LIMIT 1", (j["coc_num"],)).fetchone()
            if pr:
                _metric(conn, etype, eid, f"{metric}_per_homeless", total / pr["total"], "USD/person",
                        year, sid, is_est=1,
                        note=f"formula: {metric} ({city}, ${total:,.0f}) / HUD PIT {pr['year']} count "
                             f"for CoC {j['coc_num']} ({pr['total']:,} people)")
        _commit(conn)
        stats["cities"].append(f"{city} {year} ${total:,.0f} ({kept} lines)")


def warm():
    """Populate data/raw/budgets/ for every URL the real run needs (no DB, no write lock)."""
    for fy, start, end in FYS:
        for cfda in ("14.231", "14.267", "14.276", "14.241"):
            for layer in ("state", "county"):
                if layer == "county" and cfda not in ("14.231", "14.267"):
                    continue
                try:
                    _geo([cfda], start, end, layer)
                except Exception as e:
                    print(f"geo {layer} {cfda} FY{fy}: {repr(e)[:120]}")
    for spec in STATE_OWN:
        try:
            _dataset_meta(spec["host"], spec["ds"])
            print("state", spec["state"], _state_lines(spec)[1:3])
        except Exception as e:
            print("state", spec["state"], repr(e)[:120])
    for spec in CITY_BUDGETS:
        try:
            _dataset_meta(spec["host"], spec["ds"])
            got = _city_lines(spec)
            print("city", spec["city"], (got[5], round(sum(_money(r.get(got[2])) or 0 for r in got[1]))) if got else None)
        except Exception as e:
            print("city", spec["city"], repr(e)[:120])


# ---------------------------------------------------------------------------
def run(conn, cfg=None):
    cfg = cfg or {}
    t0 = time.time()
    _ensure(conn)
    # rows this domain wrote on an earlier (possibly interrupted) run are rebuilt
    _x(conn, r"DELETE FROM metric WHERE metric LIKE 'hud\_%' ESCAPE '\' "
             r"OR metric LIKE 'award\_hud\_%' ESCAPE '\' "
             r"OR metric LIKE 'city\_homelessness\_%' ESCAPE '\' "
             r"OR metric LIKE 'county\_homelessness\_%' ESCAPE '\' "
             r"OR metric LIKE 'state\_homelessness\_%' ESCAPE '\'")
    _commit(conn)

    stats = dict(notes=[], geo_calls=0, geo_state_rows=0, award_states=0, award_rows=0,
                 coc_county_rows=0, coc_county_match=(0, 0), coc_award_rows=0,
                 state_own=[], cities=[])

    latest = _state_from_award(conn, stats)
    _mark(conn, "state_from_award", f"{stats['award_states']} states, {len(latest)} state-year totals")

    _state_geo(conn, stats)
    _mark(conn, "state_geo", f"{stats['geo_state_rows']} state-year totals from geography API")

    _coc_from_county(conn, stats)
    _mark(conn, "coc_from_county",
          f"{stats['coc_county_rows']} CoCs, matched {stats['coc_county_match'][0]}/{stats['coc_county_match'][1]} county rows")

    _coc_from_award(conn, stats)
    _mark(conn, "coc_from_award", f"{stats['coc_award_rows']} CoC-fiscal-year totals")

    _state_own(conn, stats)
    _mark(conn, "state_own", "; ".join(stats["state_own"]) or "none")

    _city_budgets(conn, stats)
    _mark(conn, "city_budgets", f"{len(stats['cities'])} jurisdictions")

    _commit(conn)
    geo_states = conn.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM metric WHERE metric='hud_homeless_obligated_total'"
    ).fetchone()[0]
    city_n = conn.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM metric WHERE metric LIKE 'city_homelessness%' "
        "OR metric LIKE 'county_homelessness%'").fetchone()[0]
    notes = (f"states with federal figure: award-table={stats['award_states']}, "
             f"geography-api={geo_states}; jurisdiction budgets={city_n}; "
             f"coc: county-attributed={stats['coc_county_rows']} "
             f"(matched {stats['coc_county_match'][0]}/{stats['coc_county_match'][1]} county rows), "
             f"award-attributed={stats['coc_award_rows']}; "
             f"state own budgets: {'; '.join(stats['state_own']) or 'none'}; "
             f"city/county budgets: {len(stats['cities'])} ({'; '.join(stats['cities'])}); "
             f"calls: geo={stats['geo_calls']}; "
             + ("; ".join(stats["notes"])[:700] if stats["notes"] else "no source failures"))
    return {"rows": WRITE_COUNT[0], "notes": notes,
            "sources": [(USA_GEO, LIC_FED), (USA_AWARD, LIC_FED)]}


def selftest():
    from ingest.common import db
    conn = db()
    print("[budgets] metric rows by name:")
    for r in conn.execute("""SELECT metric, COUNT(*) n, COUNT(DISTINCT entity_id) ids,
                                    ROUND(SUM(value)) total, MIN(year) y0, MAX(year) y1
                               FROM metric
                              WHERE metric LIKE 'hud%' OR metric LIKE 'award_hud%'
                                 OR metric LIKE 'city_homelessness%' OR metric LIKE 'county_homelessness%'
                                 OR metric LIKE 'state_homelessness%'
                              GROUP BY metric ORDER BY n DESC"""):
        print("   ", dict(r))
    print("[budgets] 3 sample state rows:")
    for r in conn.execute("""SELECT * FROM metric WHERE entity_type='state'
                              AND (metric LIKE 'award_hud%' OR metric LIKE 'state_homelessness%')
                              ORDER BY RANDOM() LIMIT 3"""):
        print("   ", dict(r))
    print("[budgets] 3 sample city rows:")
    for r in conn.execute("""SELECT * FROM metric WHERE metric LIKE 'city_homelessness%'
                                OR metric LIKE 'county_homelessness%' ORDER BY RANDOM() LIMIT 3"""):
        print("   ", dict(r))
    print("[budgets] city line detail:")
    for r in conn.execute("SELECT entity, fy, label, amount FROM budgets_city_line ORDER BY entity LIMIT 12"):
        print("   ", dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--warm" in sys.argv:
        warm()
    else:
        from ingest.common import main
        main(DOMAIN, run)
