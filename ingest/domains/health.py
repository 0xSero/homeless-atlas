"""health -- mental illness, substance use and drug-overdose mortality by state, county and city.

What this domain owns (all rows go to the generic `metric` table)
----------------------------------------------------------------
`overdose_deaths`   count of drug-overdose deaths
`overdose_rate`     deaths per 100,000 (age-adjusted where the publisher gives it, else crude)
`depression_prevalence`, `binge_drinking_prevalence`   BRFSS state prevalence (substitutes below)

Everything is a *published* number: if a publisher does not print a cell it stays NULL, and
anything we compute (a crude rate from a published count + Census population, a metropolitan
city total summed from its counties) is flagged `is_est=1` with the derivation in `note`.

Sources (all keyless, every response cached under data/raw/health/)
-------------------------------------------------------------------
NCHS state, final     https://data.cdc.gov/resource/xbxb-epbu.json
                      Drug Poisoning Mortality by State, 1999-2016, deaths + age-adjusted rate.
NCHS VSRR, state      https://data.cdc.gov/resource/xkb8-kh2a.json
                      Provisional 12-month-ending overdose deaths, 2015-now.
NCHS VSRR, county     https://data.cdc.gov/resource/gb4e-yj24.json
                      Provisional monthly overdose deaths by county, 2020-now (1-9 suppressed).
SF DPH                https://data.sfgov.org/resource/jxrr-bmra.json
                      Preliminary unintentional overdose deaths by month.
CT OCME               https://data.ct.gov/resource/rybz-nyjw.json
                      Row-level accidental drug-related deaths with town of residence.
CDC BRFSS             https://data.cdc.gov/resource/dttw-5yxu.json
                      State adult prevalence (depression, binge drinking), 2024.

Blocked (see BLOCKERS.md `## health`): SAMHSA NSDUH state prevalence tables (samhsa.gov returns
403 to every path and user agent from this host, PDAS 503) and CDC WONDER (403). That is why
`smi_prevalence` and `sud_prevalence` are *not* written: the only publisher of those two
estimates by state is SAMHSA. Nothing is filled in from a proxy under those metric names.

Requires only `requests` (already used by ingest.common).
"""
from __future__ import annotations

import json
import re
import sys
import traceback
import urllib.parse
from datetime import datetime, timezone

from ingest.common import (clean, db, fetch, name_norm, now, save_raw, source_id_for,
                           to_int, upsert)

DOMAIN = "health"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

CDC_LIC = "Public Domain U.S. Government (https://www.usa.gov/government-works)"
CT_LIC = "Public Domain (data.ct.gov dataset metadata)"
SF_LIC = "DataSF open data portal; no licence field in dataset metadata"

# --- datasource registry ------------------------------------------------------
DS = {
    "nchs_state": dict(host="data.cdc.gov", id="xbxb-epbu",
                       title="NCHS - Drug Poisoning Mortality by State: United States",
                       publisher="CDC / National Center for Health Statistics", license=CDC_LIC),
    "vsrr_state": dict(host="data.cdc.gov", id="xkb8-kh2a",
                       title="VSRR Provisional Drug Overdose Death Counts",
                       publisher="CDC / National Center for Health Statistics", license=CDC_LIC),
    "vsrr_county": dict(host="data.cdc.gov", id="gb4e-yj24",
                        title="VSRR Provisional County-Level Drug Overdose Death Counts",
                        publisher="CDC / National Center for Health Statistics", license=CDC_LIC),
    "sf_dph": dict(host="data.sfgov.org", id="jxrr-bmra",
                   title="Preliminary Unintentional Drug Overdose Deaths",
                   publisher="San Francisco Department of Public Health (DataSF)", license=SF_LIC),
    "ct_ocme": dict(host="data.ct.gov", id="rybz-nyjw",
                    title="Accidental Drug Related Deaths 2012-2024",
                    publisher="Connecticut Office of the Chief Medical Examiner", license=CT_LIC),
    "brfss": dict(host="data.cdc.gov", id="dttw-5yxu",
                  title="Behavioral Risk Factor Surveillance System (BRFSS) Prevalence Data",
                  publisher="CDC (BRFSS)", license="Public Domain (data.cdc.gov dataset metadata)"),
}

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}

# independent-city / county suffix stripping for coextensive city<->county matching
COUNTY_SUFFIX = re.compile(r"\s+(county|parish|borough|census area|municipio|city)$", re.I)
CITY_SUFFIX = re.compile(r"\s+(city|town|village|borough|cdp|municipality)$", re.I)


# --------------------------------------------------------------------- helpers
def _socrata(key: str, params: dict, *, page: int = 50000, max_pages: int = 40) -> list[dict]:
    """GET a Socrata resource, paged, cached through common.fetch (1 req/s + data/raw/health/)."""
    d = DS[key]
    rows: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        q = dict(params)
        q["$limit"] = page
        q["$offset"] = offset
        url = f"https://{d['host']}/resource/{d['id']}.json?" + urllib.parse.urlencode(
            sorted(q.items(), key=lambda kv: kv[0]))
        body = fetch(url, domain=DOMAIN, headers={"User-Agent": UA, "Accept": "application/json"},
                     timeout=300)
        batch = json.loads(body)
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def _sid(conn, key: str) -> int:
    d = DS[key]
    url = f"https://{d['host']}/resource/{d['id']}.json"
    return source_id_for(conn, url, DOMAIN, title=d["title"], publisher=d["publisher"],
                         license=d["license"])


def _f(v):
    """string|number -> float|None (Socrata ships everything as text)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _metric(conn, etype, eid, metric, value, unit, year, sid, is_est=0, note=None) -> int:
    if eid is None or year is None or value is None:
        return 0
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    try:
        year = int(year)
    except (TypeError, ValueError):
        return 0
    upsert(conn, "metric",
           {"entity_type": etype, "entity_id": str(eid), "metric": metric, "value": float(value),
            "unit": unit, "year": year, "is_est": int(is_est), "note": clean(note), "source_id": sid},
           keys=["entity_type", "entity_id", "metric", "year"])
    return 1


def _rate(deaths, pop):
    if deaths is None or not pop:
        return None
    return round(100000.0 * deaths / pop, 1)


def _state_maps(conn):
    """-> ({NAME_UPPER: fips}, {abbr: fips}, {fips: pop}, {abbr: pop})"""
    by_name, by_abbr, pop_fips, pop_abbr = {}, {}, {}, {}
    for r in conn.execute("SELECT fips, abbr, name, pop FROM geo_state"):
        if r["name"]:
            by_name[name_norm(r["name"])] = r["fips"]
        if r["abbr"]:
            by_abbr[r["abbr"].upper()] = r["fips"]
            if r["pop"]:
                pop_abbr[r["abbr"].upper()] = r["pop"]
        pop_fips[r["fips"]] = r["pop"]
    return by_name, by_abbr, pop_fips, pop_abbr


def _county_pops(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT geoid, pop FROM geo_city WHERE kind='county'")}


def _progress(conn, step, rows, ok, note=""):
    conn.execute("""CREATE TABLE IF NOT EXISTS health_progress(
                       step TEXT PRIMARY KEY, ts TEXT, rows INTEGER, ok INTEGER, note TEXT)""")
    conn.execute("""INSERT INTO health_progress(step,ts,rows,ok,note) VALUES(?,?,?,?,?)
                    ON CONFLICT(step) DO UPDATE SET ts=excluded.ts, rows=excluded.rows,
                      ok=excluded.ok, note=excluded.note""",
                 (step, now(), rows, int(ok), (note or "")[:900]))


# ------------------------------------------------------------------ 1. NCHS final state series
def step_nchs_state(conn) -> tuple[int, str]:
    rows = _socrata("nchs_state", {
        "$where": ("sex='Both Sexes' AND age_group='All Ages' "
                   "AND race_and_hispanic_origin='All Races-All Origins'"),
        "$order": "state,year", "$limit": 50000})
    sid = _sid(conn, "nchs_state")
    by_name, by_abbr, _, _ = _state_maps(conn)
    n = 0
    years = set()
    for r in rows:
        state = clean(r.get("state"))
        if not state:
            continue
        if state.lower() == "united states":
            etype, eid = "us", "US"
        else:
            fips = by_name.get(name_norm(state)) or by_abbr.get(state.upper())
            if not fips:
                continue
            etype, eid = "state", fips
        year = to_int(r.get("year"))
        years.add(year)
        deaths = _f(r.get("deaths"))
        n += _metric(conn, etype, eid, "overdose_deaths", deaths, "deaths", year, sid,
                     note="NCHS final multiple-cause mortality, drug poisoning "
                          "(ICD-10 X40-X44, X60-X64, X85, Y10-Y14), both sexes, all ages")
        rate = _f(r.get("age_adjusted_rate"))
        n += _metric(conn, etype, eid, "overdose_rate", rate, "per 100k (age-adjusted)", year, sid,
                     note="NCHS age-adjusted drug-poisoning death rate, both sexes, all ages")
    return n, f"nchs_state rows={len(rows)} years={min(years) if years else None}-{max(years) if years else None}"


# --------------------------------------------------- 2. VSRR provisional state series (recent)
def step_vsrr_state(conn) -> tuple[int, str]:
    rows = _socrata("vsrr_state", {
        "$where": "indicator='Number of Drug Overdose Deaths' AND period='12 month-ending'",
        "$order": "state,year,month", "$limit": 50000})
    sid = _sid(conn, "vsrr_state")
    by_name, by_abbr, _, pop_abbr = _state_maps(conn)
    # latest period available per (state, year); we only write years after the final series ends
    latest: dict[tuple[str, int], dict] = {}
    for r in rows:
        st = clean(r.get("state")) or ""
        year = to_int(r.get("year"))
        mi = MONTHS.get(clean(r.get("month")) or "", 0)
        if year is None or mi == 0:
            continue
        k = (st.upper(), year)
        if k not in latest or MONTHS[clean(latest[k]["month"])] < mi:
            latest[k] = r
    n, states, skipped = 0, set(), 0
    for (st, year), r in latest.items():
        if year <= 2016:          # overlaps the final NCHS series -> keep the final one
            skipped += 1
            continue
        if st == "US":
            etype, eid, pop = "us", "US", None
        else:
            fips = by_abbr.get(st) or by_name.get(name_norm(clean(r.get("state_name")) or ""))
            if not fips:
                continue
            etype, eid, pop = "state", fips, pop_abbr.get(st)
        month = clean(r.get("month")) or ""
        period = f"12 months ending {month} {year}"
        reported = _f(r.get("data_value"))
        predicted = _f(r.get("predicted_value"))
        if reported is not None:
            n += _metric(conn, etype, eid, "overdose_deaths", reported, "deaths", year, sid, is_est=1,
                         note=f"NCHS VSRR provisional, {period} (reported, not final)")
            val, est_note = reported, None
        elif predicted is not None:
            n += _metric(conn, etype, eid, "overdose_deaths", predicted, "deaths", year, sid, is_est=1,
                         note=f"NCHS VSRR provisional, {period}; predicted count "
                              f"(reporting-delay adjusted) because the reported cell was not published")
            val, est_note = predicted, "predicted"
        else:
            continue
        if pop:
            n += _metric(conn, etype, eid, "overdose_rate", _rate(val, pop),
                         "per 100k (crude)", year, sid, is_est=1,
                         note=("crude rate computed from the published provisional count divided by the "
                               f"Census 2024 state population in geo_state ({pop:,})"
                               + ("; count is NCHS predicted value" if est_note else "")
                               + f"; {period}"))
        states.add(st)
    return n, f"vsrr_state periods={len(rows)} jurisdictions={len(states)} skipped_pre2017={skipped}"


# ------------------------------------------------------- 3. VSRR provisional county counts
def step_vsrr_county(conn, years=("2023", "2024")) -> tuple[int, str]:
    """NCHS publishes county cells as *12-month-ending* rolling counts, one row per month.

    The annual figure for a county-year is therefore the last month-ending value published for
    that year (December, or the latest month with an unsuppressed cell), not a sum of months.
    """
    county_pop = _county_pops(conn)
    known = {r[0] for r in conn.execute("SELECT geoid FROM geo_city WHERE kind='county'")}
    sid = _sid(conn, "vsrr_county")
    rows = _socrata("vsrr_county", {
        "$select": "fips,st_abbrev,countyname,year,month,monthendingdate,provisional_drug_overdose",
        "$where": "year in ('" + "','".join(years) + "')",
        "$order": "fips,year,month", "$limit": 50000})
    best: dict[tuple[str, int], tuple[int, float, str]] = {}
    for r in rows:
        fips = (clean(r.get("fips")) or "").zfill(5)
        if fips not in known:
            continue
        y = to_int(r.get("year"))
        m = to_int(r.get("month"))
        v = _f(r.get("provisional_drug_overdose"))
        if y is None or m is None or v is None:
            continue
        k = (fips, y)
        if k not in best or m > best[k][0]:
            best[k] = (m, v, clean(r.get("monthendingdate")) or "")
    n, counties = 0, set()
    for (fips, y), (m, v, _asof) in sorted(best.items()):
        note = (f"NCHS VSRR provisional 12-month-ending count for the 12 months ending month {m:02d} "
                f"of {y} (the last month of {y} with an unsuppressed cell); counties with 1-9 deaths "
                "in a month are suppressed by NCHS")
        n += _metric(conn, "city", fips, "overdose_deaths", v, "deaths", y, sid, is_est=1, note=note)
        if county_pop.get(fips):
            n += _metric(conn, "city", fips, "overdose_rate", _rate(v, county_pop[fips]),
                         "per 100k (crude)", y, sid, is_est=1,
                         note="crude rate from the provisional 12-month-ending county count above "
                              "divided by the Census 2024 county population in geo_city")
        counties.add(fips)
    return n, f"vsrr_county years={','.join(years)} county_cells={len(rows)} counties={len(counties)}"


# --------------------------------------------------------------- 4. San Francisco (city DPH)
def step_sf(conn) -> tuple[int, str]:
    """SF DPH republishes the same month several times as the OCME revises it: one row per
    (year, month) per release. Keep the row with the latest `data_as_of` before summing."""
    rows = _socrata("sf_dph", {"$order": "year,month", "$limit": 20000})
    sid = _sid(conn, "sf_dph")
    latest: dict[tuple[int, str], tuple[str, float]] = {}
    for r in rows:
        y = to_int(r.get("year"))
        mon = clean(r.get("month")) or ""
        v = _f(r.get("total_deaths"))
        if y is None or not mon or v is None:
            continue
        as_of = clean(r.get("data_as_of")) or ""
        k = (y, mon)
        if k not in latest or as_of > latest[k][0]:
            latest[k] = (as_of, v)
    per_year: dict[int, list[tuple[str, float]]] = {}
    for (y, mon), (_as_of, v) in latest.items():
        per_year.setdefault(y, []).append((mon, v))
    pop = conn.execute("SELECT pop FROM geo_city WHERE geoid='0667000'").fetchone()
    pop = pop[0] if pop else None
    n = 0
    for y, months in sorted(per_year.items()):
        total = round(sum(v for _m, v in months))
        n += _metric(conn, "city", "0667000", "overdose_deaths", total, "deaths", y, sid, is_est=1,
                     note=f"San Francisco DPH preliminary unintentional drug overdose deaths, sum of the "
                          f"{len(months)} monthly counts published for {y} (latest revision of each month)")
        if pop:
            n += _metric(conn, "city", "0667000", "overdose_rate", _rate(total, pop),
                         "per 100k (crude)", y, sid, is_est=1,
                         note=f"crude rate from the SF DPH monthly counts divided by the Census 2024 "
                              f"city population in geo_city ({pop:,})")
    return n, f"sf_dph rows={len(rows)} distinct_months={len(latest)} years={len(per_year)}"


# ----------------------------------------------------------- 5. Connecticut towns (city rows)
def step_ct(conn) -> tuple[int, str]:
    rows = _socrata("ct_ocme", {
        "$select": "residencecity,date_trunc_ym(date) AS ym,count(*) AS deaths",
        "$group": "residencecity,ym", "$order": "residencecity,ym", "$limit": 50000})
    sid = _sid(conn, "ct_ocme")
    # CT municipality lookup: Census county subdivisions ("town") first, then places
    lookup: dict[str, tuple[str, int | None]] = {}
    for r in conn.execute("SELECT geoid, kind, name, pop FROM geo_city WHERE state='CT'"):
        base = CITY_SUFFIX.sub("", r["name"] or "")
        k = name_norm(base)
        if not k:
            continue
        if k not in lookup or r["kind"] == "town":
            lookup[k] = (r["geoid"], r["pop"])
    per: dict[tuple[str, int], float] = {}
    unmatched = set()
    for r in rows:
        town = clean(r.get("residencecity"))
        if not town:
            continue
        k = name_norm(town)
        if k not in lookup:
            unmatched.add(town)
            continue
        y = to_int((r.get("ym") or "")[:4])
        v = _f(r.get("deaths"))
        if y is None or v is None:
            continue
        key = (lookup[k][0], y)
        per[key] = per.get(key, 0.0) + v
    pop_by_geoid = {g: p for g, p in lookup.values()}
    n = 0
    for (geoid, y), v in sorted(per.items()):
        pop = pop_by_geoid.get(geoid)
        n += _metric(conn, "city", geoid, "overdose_deaths", round(v), "deaths", y, sid,
                     note="counted from the CT Office of the Chief Medical Examiner row-level "
                          "accidental drug-related death records, by town of residence")
        if pop:
            n += _metric(conn, "city", geoid, "overdose_rate", _rate(v, pop), "per 100k (crude)", y, sid,
                         is_est=1,
                         note=f"crude rate from the CT OCME town count divided by the Census 2024 "
                              f"population of the municipality in geo_city ({pop:,})")
    return n, (f"ct_ocme rows={len(rows)} municipalities={len(per)} "
               f"unmatched_residence_names={len(unmatched)}")


# ------------------------------- 6. cities that ARE a county (coextensive) + New York City
NYC_COUNTIES = ("36005", "36047", "36061", "36081", "36085")


def step_city_from_county(conn, year: int = 2024) -> tuple[int, str]:
    """County-level counts -> city rows for city-county equivalents, plus the NYC five-county sum.

    A city that already has a count from a city-level publisher (e.g. San Francisco DPH,
    CT OCME) is left alone: the county value must not overwrite a more specific one.
    """
    rows = conn.execute("""SELECT entity_id, value, note, is_est, source_id FROM metric
                           WHERE entity_type='city' AND metric='overdose_deaths' AND year=?
                             AND length(entity_id)=5""", (year,)).fetchall()
    counties = {r[0]: r for r in rows}
    csrc = conn.execute("""SELECT source_id FROM metric WHERE entity_type='city'
                          AND metric='overdose_deaths' AND year=? AND length(entity_id)=5
                          LIMIT 1""", (year,)).fetchone()
    county_sid = csrc[0] if csrc else None

    def _has_other_source(geoid: str) -> bool:
        r = conn.execute("""SELECT source_id FROM metric WHERE entity_type='city' AND entity_id=?
                            AND metric='overdose_deaths' AND year=?""", (geoid, year)).fetchone()
        return bool(r and r[0] != county_sid)

    places: dict[tuple[str, str], list] = {}
    for p in conn.execute("""SELECT geoid, name, state, pop FROM geo_city
                             WHERE kind IN ('place','city') AND pop IS NOT NULL"""):
        places.setdefault((p["state"] or "", name_norm(CITY_SUFFIX.sub("", p["name"] or ""))),
                          []).append(p)
    n, matched, skipped = 0, [], 0
    for c in conn.execute("""SELECT geoid, name, state, pop FROM geo_city
                             WHERE kind='county' AND pop IS NOT NULL"""):
        crow = counties.get(c["geoid"])
        if not crow:
            continue
        base = name_norm(COUNTY_SUFFIX.sub("", c["name"] or ""))
        for p in places.get((c["state"] or "", base), []):
            if not p["pop"] or abs(p["pop"] - c["pop"]) > max(1, 0.005 * c["pop"]):
                continue
            if _has_other_source(p["geoid"]):
                skipped += 1
                continue
            n += _metric(conn, "city", p["geoid"], "overdose_deaths", crow[1], "deaths", year,
                         crow[4], is_est=1,
                         note=f"city and county are coextensive (same Census population); value is the "
                              f"county-level VSRR provisional count for {c['name']}, {c['state']}")
            matched.append(p["name"])
    # New York City = the five counties it is coextensive with
    nyc = [counties[f] for f in NYC_COUNTIES if f in counties]
    if len(nyc) == len(NYC_COUNTIES):
        total = sum(r[1] for r in nyc)
        pop = conn.execute("SELECT pop FROM geo_city WHERE geoid='3651000'").fetchone()
        pop = pop[0] if pop else None
        n += _metric(conn, "city", "3651000", "overdose_deaths", round(total), "deaths", year,
                     nyc[0][4], is_est=1,
                     note="sum of the VSRR provisional counts for the five counties New York City is "
                          "coextensive with (Bronx 36005, Kings 36047, New York 36061, Queens 36081, "
                          "Richmond 36085); suppressed 1-9 cells excluded")
        if pop:
            n += _metric(conn, "city", "3651000", "overdose_rate", _rate(total, pop),
                         "per 100k (crude)", year, nyc[0][4], is_est=1,
                         note=f"crude rate from the five-county sum divided by the Census 2024 NYC "
                              f"population in geo_city ({pop:,})")
    return n, (f"coextensive_cities={len(matched)} skipped_had_city_source={skipped} "
               f"nyc={'yes' if len(nyc) == len(NYC_COUNTIES) else 'no'}")


# ------------------------------- 7. BRFSS state prevalence (mental illness / substance use proxy)
def step_brfss_state(conn) -> tuple[int, str]:
    """Adults told they have depression, and binge drinkers -- published BRFSS state prevalence.

    These are NOT `smi_prevalence` / `sud_prevalence`: the SMI and SUD estimates by state are
    only published by SAMHSA, which is blocked from this host (BLOCKERS.md `## health`).
    """
    sid = _sid(conn, "brfss")
    _, by_abbr, _, _ = _state_maps(conn)
    n, years, meas = 0, set(), 0
    specs = [("Depression", "form of depression", "depression_prevalence",
              "adults ever told they have a form of depression"),
             ("Binge Drinking", "binge drinkers", "binge_drinking_prevalence",
              "adult binge drinkers (5+/4+ drinks on one occasion)")]
    for topic, needle, metric, label in specs:
        rows = _socrata("brfss", {
            "$where": (f"break_out='Overall' AND topic='{topic}' AND response='Yes' "
                       "AND data_value_type='Crude Prevalence'"),
            "$order": "year", "$limit": 50000})
        if not rows:
            continue
        # newest published year per state (a few states have no cell in the newest release)
        best: dict[str, tuple[int, float]] = {}
        for r in rows:
            if needle not in (clean(r.get("question")) or "").lower():
                continue
            abbr = (clean(r.get("locationabbr")) or "").upper()
            y = to_int(r.get("year"))
            v = _f(r.get("data_value"))
            if not by_abbr.get(abbr) or y is None or v is None:
                continue
            if abbr not in best or y > best[abbr][0]:
                best[abbr] = (y, v)
        for abbr, (y, v) in best.items():
            years.add(y)
            meas += 1
            n += _metric(conn, "state", by_abbr[abbr], metric, v,
                         "percent of adults (crude prevalence)", y, sid, is_est=0,
                         note=f"BRFSS state prevalence: {label}, {y} (self-reported survey, "
                              "crude prevalence, overall breakout); newest year this state published")
    return n, f"brfss states={meas} years={sorted(years)}"


# ------------------------------------------------------------------------- driver
STEPS = [
    ("nchs_state", step_nchs_state),
    ("vsrr_state", step_vsrr_state),
    ("vsrr_county", step_vsrr_county),
    ("sf_dph", step_sf),
    ("ct_ocme", step_ct),
    ("city_from_county", step_city_from_county),
    ("brfss_state", step_brfss_state),
]


def run(conn, cfg):
    conn.execute("""CREATE TABLE IF NOT EXISTS health_progress(
                       step TEXT PRIMARY KEY, ts TEXT, rows INTEGER, ok INTEGER, note TEXT)""")
    total, detail, errs = 0, {}, []
    for name, fn in STEPS:
        try:
            n, note = fn(conn, **((cfg or {}).get(f"{name}_kw") or {}))
            conn.commit()
            total += n
            detail[name] = {"rows": n, "note": note}
            _progress(conn, name, n, 1, note)
        except Exception as e:  # keep going: one dead source must not kill the domain
            conn.rollback()
            errs.append(f"{name}: {e!r}")
            detail[name] = {"rows": 0, "note": f"FAILED {e!r}"}
            _progress(conn, name, 0, 0, repr(e))
            print(f"[health] step {name} FAILED: {e!r}", file=sys.stderr)
            traceback.print_exc(limit=2)

    # what did we end up with? (counted from the table, not from this run's writes)
    cov = {}
    for m in ("overdose_deaths", "overdose_rate", "depression_prevalence",
              "binge_drinking_prevalence", "smi_prevalence", "sud_prevalence"):
        for et in ("us", "state", "city"):
            k = conn.execute("""SELECT COUNT(*) FROM metric WHERE metric=? AND entity_type=?""",
                             (m, et)).fetchone()[0]
            if k:
                cov[f"{m}:{et}"] = k
    notes = ("; ".join(f"{k}={v['rows']}" for k, v in detail.items())
             + (" | ERRORS: " + " || ".join(errs) if errs else "")
             + " | coverage " + json.dumps(cov, sort_keys=True))
    sources = [(f"https://{d['host']}/resource/{d['id']}.json", d["license"]) for d in DS.values()]
    return {"rows": total, "notes": notes[:3000], "detail": detail, "coverage": cov,
            "sources": sources}


# ------------------------------------------------------------------------ selftest
def _selftest():
    conn = db()
    res = run(conn, {})
    conn.commit()
    print(json.dumps({"rows": res["rows"], "coverage": res["coverage"]}, indent=1))
    print("--- sample metric rows ---")
    for r in conn.execute("""SELECT entity_type, entity_id, metric, value, unit, year, is_est, note
                             FROM metric WHERE metric IN ('overdose_deaths','overdose_rate',
                               'depression_prevalence','binge_drinking_prevalence')
                             ORDER BY RANDOM() LIMIT 3"""):
        print(dict(r))
    print("--- state coverage ---")
    for r in conn.execute("""SELECT metric, COUNT(DISTINCT entity_id) n FROM metric
                             WHERE entity_type IN ('state','us') GROUP BY metric ORDER BY metric"""):
        print(dict(r))
    print("--- city coverage ---")
    for r in conn.execute("""SELECT metric, COUNT(DISTINCT entity_id) n FROM metric
                             WHERE entity_type='city' GROUP BY metric ORDER BY metric"""):
        print(dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        conn = db()
        res = run(conn, {})
        conn.commit()
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                     (DOMAIN, now(), res["rows"], res["notes"][:2000]))
        conn.commit()
        print(json.dumps({k: res[k] for k in ("rows", "coverage", "detail")}, indent=1))
