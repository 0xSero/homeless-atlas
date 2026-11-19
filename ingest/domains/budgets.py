"""budgets — what each level of government spends on homelessness.

Federal level
  * HUD McKinney-Vento program obligations per state, from the USASpending.gov
    `spending_by_geography` API (CFDA 14.231 ESG, 14.267 CoC, 14.276 YHDP,
    14.241 HOPWA), scope = place_of_performance, geo_layer = state, one call
    per program per fiscal year.
  * the same, at geo_layer = county, attributed to the CoC that covers the
    county (county name -> geo_coc.name / geo_coc.cities, same state only).
  * derived metrics: obligations per resident and per person counted homeless
    (HUD PIT, latest year per state) with is_est=1 and the formula in `note`.

City level
  * city open-data budget/appropriation datasets (Socrata SoQL) for the city
    homelessness department / program line. One row per dataset line in the
    private table budgets_city_line, one rolled-up metric per city-year.
  * derived: per resident and per homeless person, is_est=1 + formula.

Every number here comes from a response cached under data/raw/budgets/.
Nothing is typed in by hand: the city configs name a dataset, a label filter and
an amount column, and the amounts are whatever the API returns.

Interface: `def run(conn, cfg) -> dict`
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
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

USASPENDING_GEO = "https://api.usaspending.gov/api/v2/search/spending_by_geography/"

# CFDA -> (metric stem, program name). Program numbers as published in the
# USASpending program_numbers filter (they are the CFDA numbers).
PROGRAMS = {
    "14.231": ("hud_esg", "Emergency Solutions Grant (ESG)"),
    "14.267": ("hud_coc", "Continuum of Care (CoC)"),
    "14.276": ("hud_yhdp", "Youth Homelessness Demonstration Program (YHDP)"),
    "14.241": ("hud_hopwa", "Housing Opportunities for Persons with AIDS (HOPWA)"),
}

# federal fiscal years to pull: (label used in metric.year, start, end)
FYS = [
    (2024, "2023-10-01", "2024-09-30"),
    (2023, "2022-10-01", "2023-09-30"),
]

# ---------------------------------------------------------------------------
# City homelessness budget lines, from each city's own open-data budget table.
#   host, dataset id, label filter (SoQL $where), amount column, metric basis,
#   human page, licence note.
# The label filters are keyword matches on the published line-item labels, so
# they are reproducible from the same dataset by anyone.
# ---------------------------------------------------------------------------
CITY_BUDGETS = [
    dict(
        city="San Francisco", state="CA", host="data.sfgov.org", ds="xdgd-c79v",
        where="fiscal_year='2025' AND revenue_or_spending='Spending' "
              "AND lower(department) like '%homelessness%'",
        amount_col="budget", group_cols=["department"], fy="2025",
        basis="adopted budget, all funds, department line (sum of object-level rows)",
        page="https://data.sfgov.org/d/xdgd-c79v",
    ),
    dict(
        city="New York", state="NY", host="data.cityofnewyork.us", ds="mwzb-yiwb",
        where="fiscal_year='2025' AND agency_name='DEPARTMENT OF HOMELESS SERVICES'",
        amount_col="adopted_budget_amount", group_cols=["publication_date"], fy="2025",
        dedupe_newest="publication_date",
        basis="adopted expense budget for the Department of Homeless Services (latest publication for the FY)",
        page="https://data.cityofnewyork.us/d/mwzb-yiwb",
    ),
    dict(
        city="Los Angeles", state="CA", host="data.lacity.org", ds="98ve-cuf5",
        where="fiscal_year='2025'",
        amount_col="pstng_am", group_cols=[], fy="2025",
        basis="actual payments recorded in the city's Homelessness Expense Tracker (expenditure, not appropriation)",
        metric="city_homelessness_expenditure",
        page="https://data.lacity.org/d/98ve-cuf5",
    ),
    dict(
        city="Seattle", state="WA", host="data.seattle.gov", ds="8u2j-imqx",
        where="fiscal_year='2025' AND lower(program) like '%homeless%'",
        amount_col="approved_amount", group_cols=["program"], fy="2025",
        basis="approved operating budget of Human Services Department programs whose name contains 'homeless'",
        page="https://data.seattle.gov/d/8u2j-imqx",
    ),
    dict(
        city="Chicago", state="IL", host="data.cityofchicago.org", ds="x394-e874",
        where="lower(appropriation_account_description) like '%homeless%'", fy="2024",
        amount_col="_ordinance_amount_", group_cols=["department_description", "appropriation_account_description"],
        basis="2024 budget ordinance appropriations whose account description contains 'homeless' (Department of Family and Support Services)",
        page="https://data.cityofchicago.org/d/x394-e874",
    ),
    dict(
        city="Austin", state="TX", host="data.austintexas.gov", ds="yeeq-kk6v",
        where="fy='2025' AND lower(dept_nm) like '%homeless%'",
        amount_col="bud", group_cols=["dept_nm", "div_nm"], fy="2025",
        basis="FY2025 budget for the Homeless Strategies and Operations department",
        page="https://data.austintexas.gov/d/yeeq-kk6v",
    ),
    dict(
        city="Honolulu", state="HI", host="data.honolulu.gov", ds="4mj2-5un8",
        where="lower(department_name) like '%homeless%' OR lower(division_name) like '%homeless%'",
        amount_col="fy_2025_adopted_budget", group_cols=["department_name", "division_name"], fy="2025",
        basis="FY2025 adopted budget lines whose department or division name contains 'homeless'",
        page="https://data.honolulu.gov/d/4mj2-5un8",
    ),
    dict(
        city="San Mateo County", state="CA", host="data.smcgov.org", ds="8w4u-sjiw",
        where="1=1", amount_col="appropriations", group_cols=["orginization_description"], fy="2014",
        entity_type="county", metric="county_homelessness_budget",
        basis="county budget items the county tagged as related to homelessness (FY2013/14), summed by organisation",
        page="https://data.smcgov.org/d/8w4u-sjiw",
    ),
]

# CoC money attribution: county geo_layer rows are matched to a CoC by county
# name appearing in the CoC name or the CoC city list, in the same state.
COUNTY_CUT = re.compile(
    r"\s+(COUNTY|PARISH|BOROUGH|CENSUS AREA|MUNICIPALITY|CITY AND BOROUGH|ISLAND)$", re.I)


BUSY_TRIES = 40


def _x(conn, sql, params=()):
    """Execute a write with retry: other domain workers hold locks on atlas.db."""
    import sqlite3 as _s
    import time as _t
    last = None
    for i in range(BUSY_TRIES):
        try:
            return conn.execute(sql, params)
        except _s.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            last = e
            _t.sleep(min(2 + i, 15))
    raise last


def _up(conn, table, row, keys):
    import sqlite3 as _s
    import time as _t
    last = None
    for i in range(BUSY_TRIES):
        try:
            return upsert(conn, table, row, keys)
        except _s.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            last = e
            _t.sleep(min(2 + i, 15))
    raise last


def _commit(conn):
    import sqlite3 as _s
    import time as _t
    last = None
    for i in range(BUSY_TRIES):
        try:
            return conn.commit()
        except _s.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            last = e
            _t.sleep(min(2 + i, 15))
    raise last


def _q(host, ds, soql, timeout=180):
    """Socrata SoQL GET, cached by common.fetch under data/raw/budgets/."""
    url = f"https://{host}/resource/{ds}.json?" + urllib.parse.urlencode(soql)
    return url, fetch_json(url, domain=DOMAIN, retries=2, timeout=timeout)


def _geo(programs, start, end, layer, timeout=240):
    payload = {
        "scope": "place_of_performance",
        "geo_layer": layer,
        "filters": {"program_numbers": list(programs),
                    "time_period": [{"start_date": start, "end_date": end}]},
        "subawards": False,
    }
    return fetch_post_json(USASPENDING_GEO, payload, domain=DOMAIN, retries=3, timeout=timeout)


def _ensure(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS budgets_city_line (
          city      TEXT NOT NULL,
          state     TEXT NOT NULL,
          fy        TEXT NOT NULL,
          label     TEXT NOT NULL,
          amount    REAL,
          dataset   TEXT,
          source_id INTEGER,
          PRIMARY KEY (city, state, fy, label)
        );
        CREATE TABLE IF NOT EXISTS budgets_progress (
          step      TEXT PRIMARY KEY,
          done_at   TEXT,
          detail    TEXT
        );
        """
    )


def _done(conn, step):
    return conn.execute("SELECT 1 FROM budgets_progress WHERE step=?", (step,)).fetchone() is not None


def _mark(conn, step, detail=""):
    _x(conn, "INSERT INTO budgets_progress(step,done_at,detail) VALUES(?,?,?) "
          "ON CONFLICT(step) DO UPDATE SET done_at=excluded.done_at, detail=excluded.detail",
       (step, now(), str(detail)[:400]))
    _commit(conn)


def _source(conn, url, *, title, publisher, license, method="GET", payload=None):
    """Register the source row and fill in bytes/sha256 from the fetch cache."""
    sid = source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)
    key = hashlib.sha1((method + url + json.dumps(payload or {}, sort_keys=True, default=str)).encode()).hexdigest()
    p = RAW / DOMAIN / f"{key}.bin"
    if p.exists():
        b = p.read_bytes()
        _x(conn, "UPDATE source SET bytes=?, sha256=?, retrieved_at=? WHERE id=?",
           (len(b), hashlib.sha256(b).hexdigest(), now(), sid))
    return sid


WROTE = [0]  # metric rows written by this process (for the run report)


def _metric(conn, entity_type, entity_id, metric, value, unit, year, source_id,
            is_est=0, note=None, counter=None):
    if value is None or entity_id is None:
        return
    WROTE[0] += 1
    _up(conn, "metric", {
        "entity_type": entity_type, "entity_id": str(entity_id), "metric": metric,
        "value": float(value), "unit": unit, "year": int(year) if year else None,
        "is_est": is_est, "note": note, "source_id": source_id,
    }, ["entity_type", "entity_id", "metric", "year"])


# ---------------------------------------------------------------------------
# 1. federal per-state obligations
# ---------------------------------------------------------------------------
def _state_federal(conn, stats):
    sid = _source(
        conn, USASPENDING_GEO,
        title="USASpending.gov API v2 spending_by_geography (HUD homeless assistance programs, CFDA 14.231/14.267/14.276/14.241)",
        publisher="U.S. Department of the Treasury, Bureau of the Fiscal Service",
        license="US Government work, public domain", method="POST")

    per_state = {}   # (fy, abbr) -> {metric: value}
    pops = {}
    for fy, start, end in FYS:
        for cfda, (stem, pname) in PROGRAMS.items():
            try:
                j = _geo([cfda], start, end, "state")
            except Exception as e:  # one program failing must not kill the sweep
                stats["notes"].append(f"federal FY{fy} {cfda}: {repr(e)[:120]}")
                continue
            n = 0
            for r in j.get("results", []):
                abbr = clean(r.get("shape_code"))
                amt = r.get("aggregated_amount")
                if not abbr or amt is None or len(abbr) != 2:
                    continue
                n += 1
                pops[abbr] = r.get("population") or pops.get(abbr)
                per_state.setdefault((fy, abbr), {})[stem] = float(amt)
                _metric(conn, "state", abbr, f"{stem}_obligated", amt, "USD", fy, sid,
                        note=f"{pname}, federal obligations recorded in FY{fy} (USASpending place-of-performance)")
            stats["federal_state_calls"] += 1
            stats["federal_state_rows"] += n

    _commit(conn)

    # state totals + derived per-capita / per-homeless
    pit = _state_pit(conn)
    n_tot = 0
    for (fy, abbr), d in sorted(per_state.items()):
        total = sum(d.values())
        _metric(conn, "state", abbr, "hud_homeless_obligated_total", total, "USD", fy, sid,
                note="formula: sum of HUD ESG + CoC + YHDP + HOPWA obligations for the state in FY%d "
                     "from USASpending spending_by_geography (place of performance)" % fy)
        n_tot += 1
        pop = pops.get(abbr)
        if pop:
            _metric(conn, "state", abbr, "hud_homeless_obligated_per_capita",
                    total / pop, "USD/person", fy, sid, is_est=1,
                    note="formula: HUD homeless-assistance obligations FY%d / resident population "
                         "(%s, population field returned by USASpending geography API)" % (fy, f"{pop:,}"))
        py, ptot = pit.get(abbr, (None, None))
        if ptot:
            _metric(conn, "state", abbr, "hud_homeless_obligated_per_homeless",
                    total / ptot, "USD/person", fy, sid, is_est=1,
                    note="formula: HUD homeless-assistance obligations FY%d / HUD PIT count for the "
                         "state in %s (%s people, sum of the state's CoC PIT totals)" % (fy, py, f"{ptot:,}"))
    _commit(conn)
    stats["state_metric_rows"] += n_tot
    return sid


def _state_pit(conn):
    """state abbr -> (year, total people homeless) from the pit table, latest year."""
    out = {}
    rows = conn.execute(
        """SELECT g.state AS st, p.year AS y, SUM(p.total) AS tot
             FROM pit p JOIN geo_coc g ON g.coc_num = p.coc_num
            WHERE p.total IS NOT NULL
            GROUP BY g.state, p.year""")
    for r in rows:
        st, y, tot = r["st"], r["y"], r["tot"]
        if not st or not tot:
            continue
        if st not in out or y > out[st][0]:
            out[st] = (y, int(tot))
    return out


# ---------------------------------------------------------------------------
# 2. county -> CoC attribution of HUD CoC + ESG money
# ---------------------------------------------------------------------------
def _coc_from_county(conn, stats):
    index = {}
    for r in conn.execute("SELECT coc_num, name, state, cities FROM geo_coc"):
        st = (r["state"] or "").upper()
        names = set()
        for blob in (r["name"], r["cities"]):
            if not blob:
                continue
            for tok in re.split(r"[,/;&]| and ", blob):
                tok = re.sub(r"\bCoC\b", "", tok, flags=re.I)
                tok = re.sub(r"\b(city|county|counties|balance of state|continuum of care)\b", "", tok, flags=re.I)
                tok = re.sub(r"[^A-Za-z ]+", " ", tok)
                tok = re.sub(r"\s+", " ", tok).strip().upper()
                if len(tok) > 2:
                    names.add(tok)
        index.setdefault(st, []).append((r["coc_num"], r["name"] or "", names))

    fy, start, end = FYS[0]
    agg = {}
    matched = seen = 0
    geo_by_prog = {}
    for cfda in ("14.267", "14.231"):
        try:
            geo_by_prog[cfda] = _geo([cfda], start, end, "county")
        except Exception as e:
            stats["notes"].append(f"county geography {cfda}: {repr(e)[:120]}")
            continue
        stats["federal_county_calls"] += 1

    counties = {r["geoid"]: (r["name"], (r["state"] or "").upper())
                for r in conn.execute("SELECT geoid,name,state FROM geo_city WHERE kind='county'")}

    for cfda, j in geo_by_prog.items():
        for r in j.get("results", []):
            fips = clean(r.get("shape_code"))
            amt = r.get("aggregated_amount")
            if not fips or amt is None:
                continue
            seen += 1
            cname, st = counties.get(fips, (r.get("display_name") or "", ""))
            if not st:
                continue
            base = COUNTY_CUT.sub("", (cname or "").upper()).strip()
            if not base:
                continue
            hits = {cn for cn, nm, toks in index.get(st, [])
                    if base in toks or base in nm.upper()}
            if len(hits) != 1:
                continue
            matched += 1
            coc = hits.pop()
            agg.setdefault(coc, {}).setdefault(cfda, 0.0)
            agg[coc][cfda] += float(amt)

    sid = _source(
        conn, USASPENDING_GEO,
        title="USASpending.gov API v2 spending_by_geography, county layer (HUD CoC + ESG, FY2024)",
        publisher="U.S. Department of the Treasury, Bureau of the Fiscal Service",
        license="US Government work, public domain", method="POST")

    pit = {}
    for r in conn.execute("SELECT coc_num, year, total FROM pit WHERE total IS NOT NULL AND total > 0"):
        if r["coc_num"] not in pit or r["year"] > pit[r["coc_num"]][0]:
            pit[r["coc_num"]] = (r["year"], r["total"])

    n = 0
    for coc, d in sorted(agg.items()):
        total = sum(d.values())
        _metric(conn, "coc", coc, "hud_coc_esg_obligated_county_attributed", total, "USD", fy, sid,
                is_est=1,
                note="formula: sum of HUD CoC (14.267, %s) + ESG (14.231, %s) county-level obligations "
                     "whose county was attributed to this CoC by county name matching the CoC name/city "
                     "list (same state); county rows that matched no single CoC are excluded"
                     % (_fmt(d.get("14.267")), _fmt(d.get("14.231"))))
        n += 1
        py, ptot = pit.get(coc, (None, None))
        if ptot:
            _metric(conn, "coc", coc, "hud_coc_esg_obligated_per_homeless", total / ptot, "USD/person",
                    fy, sid, is_est=1,
                    note="formula: county-attributed HUD CoC+ESG obligations FY%d / HUD PIT count for the "
                         "CoC in %s (%s people)" % (fy, py, f"{ptot:,}"))
    _commit(conn)
    stats["coc_rows"] = n
    stats["coc_county_match"] = (matched, seen)
    return n


def _fmt(v):
    return "n/a" if v is None else f"${v:,.0f}"


# ---------------------------------------------------------------------------
# 3. city budgets from city open data
# ---------------------------------------------------------------------------
def _city_budgets(conn, stats):
    for spec in CITY_BUDGETS:
        city, state = spec["city"], spec["state"]
        metric = spec.get("metric", "city_homelessness_budget")
        etype = spec.get("entity_type", "city")
        kind = "county" if etype == "county" else "place"
        sel = list(spec["group_cols"]) + [f"sum({spec['amount_col']})"]
        soql = {
            "$select": ",".join(sel),
            "$where": spec["where"],
            "$group": ",".join(spec["group_cols"]) if spec["group_cols"] else None,
            "$limit": 5000,
        }
        soql = {k: v for k, v in soql.items() if v is not None}
        try:
            url, rows = _q(spec["host"], spec["ds"], soql)
        except Exception as e:
            stats["notes"].append(f"city {city}: {repr(e)[:150]}")
            continue
        if not rows:
            stats["notes"].append(f"city {city}: query returned 0 rows ({spec['ds']})")
            continue
        amt_key = [k for k in rows[0] if k.startswith("sum_")][0]
        sid = _source(
            conn, url,
            title=f"{city} homelessness budget lines ({spec['page']})",
            publisher=f"City of {city} open data portal",
            license="City open data portal (public record); see portal terms")
        fy_label = spec.get("fy") or "latest"

        total = 0.0
        kept = 0
        for r in rows:
            amt = r.get(amt_key)
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                continue
            label = " | ".join(str(r.get(c) or "") for c in spec["group_cols"]).strip(" |") or spec["basis"]
            if spec.get("dedupe_newest"):
                pass
            if amt == 0 and spec.get("dedupe_newest"):
                continue
            total += amt
            kept += 1
            _up(conn, "budgets_city_line", {
                "city": city, "state": state, "fy": fy_label, "label": label[:300],
                "amount": amt, "dataset": f"{spec['host']}/{spec['ds']}", "source_id": sid,
            }, ["city", "state", "fy", "label"])

        if spec.get("dedupe_newest"):
            # NYC publishes the same FY several times (one row per publication);
            # keep only the newest publication for the FY.
            rows = sorted(rows, key=lambda r: str(r.get(spec["dedupe_newest"]) or ""))
            newest = str(rows[-1].get(spec["dedupe_newest"])) if rows else ""
            total = 0.0
            kept = 0
            for r in rows:
                if str(r.get(spec["dedupe_newest"])) != newest:
                    continue
                try:
                    amt = float(r.get(amt_key))
                except (TypeError, ValueError):
                    continue
                total += amt
                kept += 1
            _x(conn, "DELETE FROM budgets_city_line WHERE city=? AND state=? AND fy=?",
               (city, state, fy_label))
            _up(conn, "budgets_city_line", {
                "city": city, "state": state, "fy": fy_label,
                "label": f"all agencies matching filter (publication {newest})",
                "amount": total, "dataset": f"{spec['host']}/{spec['ds']}", "source_id": sid,
            }, ["city", "state", "fy", "label"])
            note_extra = f"; newest publication_date {newest} used"
        else:
            note_extra = ""

        year = int(fy_label) if str(fy_label).isdigit() else None
        _metric(conn, etype, city, metric, total, "USD", year, sid,
                note=f"{city}: {spec['basis']}; filter [{spec['where']}]; {kept} published line(s) summed{note_extra}")

        pop_row = conn.execute(
            "SELECT geoid, pop, coc_num FROM geo_city WHERE name=? AND state=? AND kind=? "
            "ORDER BY pop DESC LIMIT 1", (city, state, kind)).fetchone()
        eid = pop_row["geoid"] if pop_row and pop_row["geoid"] else city
        if pop_row and pop_row["pop"]:
            _metric(conn, etype, eid, f"{metric}_per_capita", total / pop_row["pop"], "USD/person",
                    year, sid, is_est=1,
                    note="formula: %s / %s residents (Census place population in geo_city)" % (metric, f"{pop_row['pop']:,}"))
        if pop_row and pop_row["coc_num"]:
            pr = conn.execute(
                "SELECT year,total FROM pit WHERE coc_num=? AND total IS NOT NULL AND total>0 "
                "ORDER BY year DESC LIMIT 1", (pop_row["coc_num"],)).fetchone()
            if pr:
                _metric(conn, etype, eid, f"{metric}_per_homeless", total / pr["total"], "USD/person",
                        year, sid, is_est=1,
                        note="formula: %s / HUD PIT %d count for CoC %s (%s people)"
                             % (metric, pr["year"], pop_row["coc_num"], f"{pr['total']:,}"))
        _commit(conn)
        stats["city_rows"] += 1
        stats["cities"].append(city)


# ---------------------------------------------------------------------------
# 4. HUD new awards in FY2024 to local governments (cities / counties).
#    The `award` table in this atlas has place_city empty for these CDFAs, so
#    the recipient name is matched against geo_city instead; only the largest
#    page of awards per program is read, so this is a floor, not a total.
# ---------------------------------------------------------------------------
SPENDING_BY_AWARD = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
LOCAL_GOV = re.compile(r"\b(CITY|COUNTY|BOROUGH|MUNICIPAL|GOVERNMENT|TOWN|VILLAGE|PARISH)\b")


def _city_awards(conn, stats):
    fy, start, end = FYS[0]
    per = {}   # (etype, canonical name, state) -> amount
    sid = None
    for cfda in ("14.231", "14.267"):
        payload = {
            "filters": {"program_numbers": [cfda],
                        "time_period": [{"start_date": start, "end_date": end,
                                         "date_type": "new_awards_only"}],
                        "award_type_codes": ["02", "03", "04", "05"]},
            "fields": ["Award ID", "Recipient Name", "Award Amount", "Description",
                       "Start Date", "Place of Performance State Code"],
            "page": 1, "limit": 100, "sort": "Award Amount", "order": "desc", "subawards": False,
        }
        try:
            j = fetch_post_json(SPENDING_BY_AWARD, payload, domain=DOMAIN, retries=3, timeout=240)
        except Exception as e:
            stats["notes"].append(f"local-government awards {cfda}: {repr(e)[:140]}")
            continue
        sid = _source(conn, SPENDING_BY_AWARD,
                      title="USASpending.gov API v2 spending_by_award (HUD ESG + CoC new awards, FY2024, local-government recipients)",
                      publisher="U.S. Department of the Treasury, Bureau of the Fiscal Service",
                      license="US Government work, public domain", method="POST")
        stats["award_calls"] += 1
        for r in j.get("results", []):
            st = clean(r.get("Place of Performance State Code"))
            rn = (clean(r.get("Recipient Name")) or "").upper()
            amt = r.get("Award Amount")
            if not st or not rn or amt is None or not LOCAL_GOV.search(rn):
                continue
            hit = _match_locality(conn, rn, st)
            if not hit:
                continue
            key = (hit[0], hit[1], hit[2], st)
            per[key] = per.get(key, 0.0) + float(amt)

    n = 0
    for (etype, name, geoid, st), total in sorted(per.items(), key=lambda kv: -kv[1]):
        eid = geoid or name
        _metric(conn, etype, eid, "hud_new_awards_fy2024", total, "USD", fy, sid,
                note="formula: sum of HUD ESG (14.231) + CoC (14.267) awards first created in FY%d whose "
                     "recipient name is this local government (%s, %s), top 100 awards by amount per program "
                     "(USASpending spending_by_award); matched on recipient name, not place of performance"
                     % (fy, name, st))
        n += 1
        row = conn.execute(
            "SELECT pop, coc_num FROM geo_city WHERE geoid=? ORDER BY pop DESC LIMIT 1", (eid,)).fetchone()
        if row and row["pop"]:
            _metric(conn, etype, eid, "hud_new_awards_fy2024_per_capita", total / row["pop"], "USD/person",
                    fy, sid, is_est=1,
                    note="formula: HUD ESG+CoC FY%d new awards to this local government / %s residents "
                         "(Census place/county population in geo_city)" % (fy, f"{row['pop']:,}"))
        if row and row["coc_num"]:
            pr = conn.execute("SELECT year,total FROM pit WHERE coc_num=? AND total IS NOT NULL AND total>0 "
                              "ORDER BY year DESC LIMIT 1", (row["coc_num"],)).fetchone()
            if pr:
                _metric(conn, etype, eid, "hud_new_awards_fy2024_per_homeless", total / pr["total"],
                        "USD/person", fy, sid, is_est=1,
                        note="formula: HUD ESG+CoC FY%d new awards to this local government / HUD PIT %d count "
                             "for CoC %s (%s people)" % (fy, pr["year"], row["coc_num"], f"{pr['total']:,}"))
    _commit(conn)
    stats["local_gov_rows"] = n
    return n


def _match_locality(conn, recipient_upper, state):
    """Longest geo_city name (place or county) that appears in the recipient name."""
    best = None
    for r in conn.execute(
            "SELECT geoid,kind,name,pop FROM geo_city WHERE state=? AND kind IN ('place','county') "
            "AND pop IS NOT NULL AND pop > 40000", (state,)):
        nm = (r["name"] or "").upper()
        if len(nm) < 5:
            continue
        if re.search(r"(^|[^A-Z])" + re.escape(nm) + r"([^A-Z]|$)", recipient_upper):
            if best is None or (r["pop"] or 0) > best[3]:
                best = ("county" if r["kind"] == "county" else "city", r["name"], r["geoid"], r["pop"] or 0)
    return (best[0], best[1], best[2]) if best else None


# ---------------------------------------------------------------------------
def run(conn, cfg=None):
    cfg = cfg or {}
    _ensure(conn)
    _x(conn, "DELETE FROM metric WHERE (metric LIKE 'city\\_homelessness%' ESCAPE '\\' "
             "OR metric LIKE 'county\\_homelessness%' ESCAPE '\\' "
             "OR metric LIKE 'hud\\_new\\_awards%' ESCAPE '\\') AND entity_id NOT GLOB '[0-9]*'")
    _commit(conn)
    stats = dict(notes=[], federal_state_calls=0, federal_state_rows=0, state_metric_rows=0,
                 federal_county_calls=0, coc_rows=0, coc_county_match=(0, 0), city_rows=0, cities=[],
                 award_calls=0, local_gov_rows=0)

    _state_federal(conn, stats)
    _mark(conn, "state_federal", f"{stats['state_metric_rows']} state-year totals")

    _coc_from_county(conn, stats)
    _mark(conn, "coc_county", f"{stats['coc_rows']} CoCs, matched {stats['coc_county_match'][0]}/{stats['coc_county_match'][1]} county rows")

    _city_budgets(conn, stats)
    _mark(conn, "city_budgets", f"{stats['city_rows']} cities: {', '.join(stats['cities'])}")

    _city_awards(conn, stats)
    _mark(conn, "city_awards", f"{stats['local_gov_rows']} local governments")

    _commit(conn)
    rows = WROTE[0]
    notes = ("state=federal obligations per state/FY (HUD ESG/CoC/YHDP/HOPWA via USASpending), "
             f"coc={stats['coc_rows']} (county-attributed, matched {stats['coc_county_match'][0]}/"
             f"{stats['coc_county_match'][1]} county rows), city={stats['city_rows']} "
             f"({', '.join(stats['cities'])}), local_gov_awards={stats['local_gov_rows']}; "
             f"calls: state={stats['federal_state_calls']} county={stats['federal_county_calls']} "
             f"awards={stats['award_calls']}; "
             + ("; ".join(stats["notes"])[:600] if stats["notes"] else "no source failures"))
    return {"rows": rows, "notes": notes,
            "sources": [(USASPENDING_GEO, "US Government work, public domain")]}


def selftest():
    from ingest.common import db
    conn = db()
    print("[budgets] metric rows by metric (top 20):")
    for r in conn.execute("""SELECT metric, COUNT(*) n, ROUND(SUM(value)) total, MIN(year) y0, MAX(year) y1
                               FROM metric WHERE metric LIKE 'hud%' OR metric LIKE 'city%'
                              GROUP BY metric ORDER BY n DESC LIMIT 20"""):
        print("   ", dict(r))
    print("[budgets] sample state rows:")
    for r in conn.execute("""SELECT * FROM metric WHERE entity_type='state' AND metric LIKE 'hud%'
                             ORDER BY RANDOM() LIMIT 3"""):
        print("   ", dict(r))
    print("[budgets] sample city rows:")
    for r in conn.execute("""SELECT * FROM metric WHERE metric LIKE 'city_homelessness%'
                             OR metric LIKE 'county_homelessness%' OR metric LIKE 'hud_new_awards%'
                             ORDER BY RANDOM() LIMIT 3"""):
        print("   ", dict(r))
    print("[budgets] city line detail:")
    for r in conn.execute("SELECT city, fy, label, amount FROM budgets_city_line ORDER BY city, amount DESC LIMIT 8"):
        print("   ", dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        from ingest.common import main
        main(DOMAIN, run)
