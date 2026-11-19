#!/usr/bin/env python3
"""sf — San Francisco specifics: homelessness money, encampment volume, overdose, institutional capacity.

Everything here comes from a handful of publishers, all fetched through `ingest.common.fetch`
(cached under `data/raw/sf/`) and cited per row with `source_id`:

  * SF Controller's Budget (DataSF `xdgd-c79v`) — every appropriation line for the Department of
    Homelessness and Supportive Housing (`department_code='HOM'`), the Our City Our Home fund created
    by Prop C (Nov 2018), and the DPH programs that pay for Laguna Honda and SF General's psychiatric
    wards. Aggregated to one row per (fiscal year, fund / program) in `metric`.
  * SF Controller's Budget - FTE (DataSF `4zfx-f2ts`) — HSH staffing per fiscal year.
  * SF Controller's Supplier Contracts (DataSF `cqi5-hm2d`) — executed contracts with vendors/grantees
    (prime contractor, agreed amount, term). Written to `award` with award_type='local_contract'.
  * DataSF 311 Cases (`vw6y-z8j6`) — encampment and homeless-concern reports per year.
  * DataSF Law Enforcement Dispatched Calls for Service: Closed (`2zdj-bwza`) — SFPD "HOMELESS
    COMPLAINT" calls per year.
  * DataSF OCME Preliminary Unintentional Drug Overdose Deaths (`jxrr-bmra`) — overdose deaths per year.
  * SF Sheriff's Office 2025 Annual Report (PDF) — capacity and average population of each county jail,
    Jail Health Services mental-health screening/treatment volumes, the ZSFGH jail wards.
  * DataSF San Francisco Ballot Propositions Database (`88s2-6ua9`) — the two measures this file cites:
    Prop C 2018 (Our City Our Home) and the March 2024 Measure C.
  * Wikipedia infoboxes for Laguna Honda and San Francisco General for licensed bed counts (same
    practice as `ingest/domains/facility.py`).

Run: python3 -m ingest.run sf            (or python3 -m ingest.domains.sf --selftest)
Idempotent: every write goes through `common.upsert` on a natural key.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import (  # noqa: E402
    clean, db, fetch, name_norm, now, save_raw, source_id_for, to_int, upsert,
)

DOMAIN = "sf"
RES = "https://data.sf.gov/resource/{}.json"
PDDL = "Open Data Commons Public Domain Dedication and License (PDDL) 1.0"
CITY = "City and County of San Francisco"
ENTITY = "San Francisco"
AGENCY = "City and County of San Francisco"

JAIL_PDF = ("https://sfsheriff.com/sites/default/files/2026-02/"
            "ANNUAL%20REPORT%202025%20UPDATED_compressed.pdf")
JAIL_INDEX = "https://sfsheriff.com/services/jail-services/jails"
WIKI = "https://en.wikipedia.org/w/index.php?title={}&action=raw"


# ---------------------------------------------------------------- plumbing
def socrata(conn, dataset, *, select=None, where=None, group=None, order=None,
            limit=50000, title=None, publisher=CITY, license=PDDL):
    """Query a DataSF resource, cache the response, register the source, return (records, source_id, url)."""
    params = {}
    if select:
        params["$select"] = select
    if where:
        params["$where"] = where
    if group:
        params["$group"] = group
    if order:
        params["$order"] = order
    params["$limit"] = str(limit)
    url = RES.format(dataset) + "?" + urlencode(params, quote_via=quote, safe="")
    body = fetch(url, domain=DOMAIN)
    sid = source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)
    return json.loads(body), sid, url


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def day(v):
    """'2020-07-01T00:00:00.000' -> '2020-07-01'."""
    if not v:
        return None
    return str(v)[:10]


def put_metric(conn, metric, year, value, source_id, *, unit=None, note=None, is_est=0, entity_type="city"):
    if value is None or year is None:
        return 0
    row = {
        "entity_type": entity_type,
        "entity_id": ENTITY,
        "metric": metric,
        "value": float(value),
        "unit": unit,
        "year": int(year),
        "is_est": is_est,
        "note": note,
        "source_id": source_id,
    }
    upsert(conn, "metric", row, keys=["entity_type", "entity_id", "metric", "year"])
    return 1


# ---------------------------------------------------------------- 1. budget
HOM = "department_code='HOM'"

# HSH programs worth their own metric (program codes as published by the Controller)
HOM_PROGRAM_METRICS = {
    "CSH": "hsh_budget_shelter_and_housing",
    "COT": "hsh_budget_outreach_and_prevention",
    "ADMIN": "hsh_budget_administration",
    "CMN": "hsh_budget_admin_and_management",
    "CAP": "hsh_budget_capital",
    "RCVY": "hsh_budget_disaster_recovery",
    "FAY": "hsh_budget_transitional_aged_youth",
    "TECH": "hsh_budget_technology",
}

# DPH programs that are the institutional capacity the brief asks about
DPH_PROGRAM_METRICS = {
    "DA5": "dph_budget_laguna_honda_long_term_care",
    "DBG": "dph_budget_laguna_honda_acute_care",
    "D1H": "dph_budget_sfgh_acute_hospital",
    "D1P": "dph_budget_sfgh_acute_psychiatry",
    "D1F": "dph_budget_sfgh_acute_forensics",
    "D5S": "dph_budget_sfgh_emergency_psychiatric",
    "DLT": "dph_budget_mh_long_term_care",
    "DMM": "dph_budget_mh_community_care",
}


def ingest_budget(conn):
    n = 0
    # 1a. HSH: one row per (fiscal year, fund)
    recs, sid, url = socrata(
        conn, "xdgd-c79v",
        select="fiscal_year,fund_type,fund,fund_category,sum(budget) as amt",
        where=HOM, group="fiscal_year,fund_type,fund,fund_category", order="fiscal_year,fund",
        title="San Francisco Controller budget (Annual Appropriation Ordinance line items)",
    )
    per_year, ocoh, gf, grants = {}, {}, {}, {}
    for r in recs:
        fy = to_int(r.get("fiscal_year"))
        amt = num(r.get("amt")) or 0.0
        if fy is None:
            continue
        per_year[fy] = per_year.get(fy, 0.0) + amt
        fund = (r.get("fund") or "")
        ftype = (r.get("fund_type") or "")
        fcat = (r.get("fund_category") or "")
        if fund.startswith("SR Our City Our Home"):
            ocoh[fy] = ocoh.get(fy, 0.0) + amt
        if ftype == "General Fund":
            gf[fy] = gf.get(fy, 0.0) + amt
        if fcat.startswith("Grants"):
            grants[fy] = grants.get(fy, 0.0) + amt

    for fy, amt in sorted(per_year.items()):
        if amt == 0:
            continue
        n += put_metric(conn, "hsh_budget_total", fy, amt, sid, unit="usd",
                        note="Sum of every budget line, department_code='HOM' (Dept. of Homelessness and "
                             "Supportive Housing), all funds and characters of expenditure")
    for fy, amt in sorted(ocoh.items()):
        if amt == 0:
            continue
        n += put_metric(conn, "hsh_budget_ocoh_prop_c", fy, amt, sid, unit="usd",
                        note="Fund 'SR Our City Our Home Fund' — the dedicated homelessness fund created by "
                             "Prop C (Nov 2018), department_code='HOM'")
    for fy, amt in sorted(gf.items()):
        if amt == 0:
            continue
        n += put_metric(conn, "hsh_budget_general_fund", fy, amt, sid, unit="usd",
                        note="fund_type='General Fund', department_code='HOM'")
    for fy, amt in sorted(grants.items()):
        if amt == 0:
            continue
        n += put_metric(conn, "hsh_budget_grants", fy, amt, sid, unit="usd",
                        note="fund_category starts with 'Grants', department_code='HOM'")

    # 1b. HSH: budget per program
    recs, sid, _ = socrata(
        conn, "xdgd-c79v",
        select="fiscal_year,program_code,sum(budget) as amt",
        where=HOM, group="fiscal_year,program_code", order="fiscal_year,program_code",
        title="San Francisco Controller budget — HSH by program",
    )
    for r in recs:
        fy, pc, amt = to_int(r.get("fiscal_year")), (r.get("program_code") or "").strip(), num(r.get("amt")) or 0.0
        metric = HOM_PROGRAM_METRICS.get(pc)
        if not metric or not amt:
            continue
        n += put_metric(conn, metric, fy, amt, sid, unit="usd",
                        note=f"department_code='HOM', program_code='{pc}'")

    # 1b2. Laguna Honda and SF General operating funds. The Controller's granular program codes end at
    # FY2018 (from FY2019 the same departments report under program 'OPR'), but the enterprise funds that
    # carry each hospital keep reporting through FY2028.
    where = "fund_type in ('Laguna Honda Hospital Funds','General Hospital Medical Center Funds')"
    recs, sid, _ = socrata(
        conn, "xdgd-c79v",
        select="fiscal_year,fund_type,sum(budget) as amt", where=where, group="fiscal_year,fund_type",
        order="fiscal_year,fund_type",
        title="San Francisco Controller budget — Laguna Honda and SF General operating funds",
    )
    for r in recs:
        fy, ftype, amt = to_int(r.get("fiscal_year")), r.get("fund_type"), num(r.get("amt")) or 0.0
        if not amt:
            continue
        if ftype.startswith("Laguna Honda"):
            n += put_metric(conn, "laguna_honda_operating_budget", fy, amt, sid, unit="usd",
                            note="Sum of budget lines with fund_type='Laguna Honda Hospital Funds'")
        else:
            n += put_metric(conn, "sfgh_operating_budget", fy, amt, sid, unit="usd",
                            note="Sum of budget lines with fund_type='General Hospital Medical Center Funds'")

    # 1c. HSH staffing (Budget - FTE dataset): the `budget` column in that resource is FTE count
    recs, sid, _ = socrata(
        conn, "4zfx-f2ts",
        select="fiscal_year,sum(budget) as fte", where=HOM, group="fiscal_year", order="fiscal_year",
        title="San Francisco Controller budget — FTE by department and fiscal year",
    )
    for r in recs:
        fy, fte = to_int(r.get("fiscal_year")), num(r.get("fte"))
        n += put_metric(conn, "hsh_fte", fy, fte, sid, unit="fte",
                        note="Sum of budgeted FTE, department_code='HOM' (Budget - FTE dataset)")

    # 1d. DPH programs: Laguna Honda, SF General (incl. psychiatric wards), mental health
    where = "department_code='DPH' and (program_code in ('DA5','DBG','D1H','D1P','D1F','D5S','DLT','DMM'))"
    recs, sid, _ = socrata(
        conn, "xdgd-c79v",
        select="fiscal_year,program_code,sum(budget) as amt", where=where, group="fiscal_year,program_code",
        order="fiscal_year,program_code",
        title="San Francisco Controller budget — DPH institutional programs (Laguna Honda, SFGH, mental health)",
    )
    for r in recs:
        fy, pc, amt = to_int(r.get("fiscal_year")), (r.get("program_code") or "").strip(), num(r.get("amt")) or 0.0
        metric = DPH_PROGRAM_METRICS.get(pc)
        if not metric or not amt:
            continue
        n += put_metric(conn, metric, fy, amt, sid, unit="usd",
                        note=f"department_code='DPH', program_code='{pc}'")

    # 1e. Sheriff budget (the jails the mental-health numbers below describe), department total and —
    # where the Controller still publishes the program split — the Custody program.
    recs, sid, _ = socrata(
        conn, "xdgd-c79v",
        select="fiscal_year,program_code,sum(budget) as amt", where="department_code='SHF'",
        group="fiscal_year,program_code", order="fiscal_year",
        title="San Francisco Controller budget — Sheriff's Department",
    )
    shf_total, shf_custody = {}, {}
    for r in recs:
        fy, pc, amt = to_int(r.get("fiscal_year")), (r.get("program_code") or ""), num(r.get("amt")) or 0.0
        shf_total[fy] = shf_total.get(fy, 0.0) + amt
        if pc == "AFC":
            shf_custody[fy] = shf_custody.get(fy, 0.0) + amt
    for fy, amt in sorted(shf_total.items()):
        if amt:
            n += put_metric(conn, "sheriff_budget_total", fy, amt, sid, unit="usd",
                            note="All budget lines, department_code='SHF'")
    for fy, amt in sorted(shf_custody.items()):
        if amt:
            n += put_metric(conn, "sheriff_budget_custody", fy, amt, sid, unit="usd",
                            note="department_code='SHF', program_code='AFC' (Custody); the Controller publishes "
                                 "this program split only through FY2018")
    return n


# ---------------------------------------------------------------- 2. contracts
CONTRACT_WHERE = (
    "department_code='HOM' OR upper(scope_of_work) like '%HOMELESS%' "
    "OR upper(contract_title) like '%HOMELESS%' OR upper(scope_of_work) like '%SHELTER%' "
    "OR upper(contract_title) like '%SHELTER%' OR upper(scope_of_work) like '%ENCAMPMENT%' "
    "OR upper(contract_title) like '%ENCAMPMENT%'"
)
CONTRACT_COLS = ("contract_no,contract_title,term_start_date,term_end_date,contract_type,"
                 "purchasing_authority,sole_source_flg,department_code,department,prime_contractor,"
                 "non_profit,scope_of_work,agreed_amt,consumed_amt,pmt_amt,remaining_amt")


def ingest_contracts(conn):
    recs, sid, url = socrata(
        conn, "cqi5-hm2d", select=CONTRACT_COLS, where=CONTRACT_WHERE, order="contract_no",
        title="San Francisco Supplier Contracts (executed expenditure contracts and grants)",
    )
    n = 0
    for r in recs:
        no = clean(r.get("contract_no"))
        vendor = clean(r.get("prime_contractor"))
        if not no or not vendor:
            continue
        title = clean(r.get("contract_title"))
        scope = clean(r.get("scope_of_work"))
        dept = clean(r.get("department"))
        desc = title
        if scope and scope not in (title or ""):
            desc = f"{title}: {scope}" if title else scope
        row = {
            "award_key": f"SF-{no}",
            "award_id": no,
            "recipient_name": vendor,
            "recipient_norm": name_norm(vendor),
            "award_type": "local_contract",
            "agency": AGENCY,
            "subtier": dept,
            "office": clean(r.get("purchasing_authority")),
            "program": title,
            "description": desc,
            "amount_total": num(r.get("agreed_amt")),
            "amount_obligated": num(r.get("consumed_amt")),
            "pop_start": day(r.get("term_start_date")),
            "pop_end": day(r.get("term_end_date")),
            "place_state": "CA",
            "place_city": "San Francisco",
            "place_county": "San Francisco",
            "source_id": sid,
            "is_est": 0,
        }
        upsert(conn, "award", row, keys=["award_key"])
        n += 1
    return n


# ---------------------------------------------------------------- 3. volume counts
def ingest_counts(conn):
    n = 0
    # 3a. 311: encampment + homeless-concern reports per year
    recs, sid, _ = socrata(
        conn, "vw6y-z8j6",
        select="date_extract_y(requested_datetime) as y,service_name,count(*) as c",
        where="upper(service_name) like '%ENCAMPMENT%' or upper(service_name) like '%HOMELESS%'",
        group="y,service_name", order="y", title="San Francisco 311 Cases",
    )
    enc, hc = {}, {}
    for r in recs:
        y, name, c = to_int(r.get("y")), (r.get("service_name") or ""), to_int(r.get("c"))
        if y is None or c is None:
            continue
        if "ENCAMPMENT" in name.upper():
            enc[y] = enc.get(y, 0) + c
        elif "HOMELESS" in name.upper():
            hc[y] = hc.get(y, 0) + c
    for y, c in sorted(enc.items()):
        n += put_metric(conn, "encampment_311_requests", y, c, sid, unit="requests",
                        note="311 cases whose service_name is 'Encampment'/'Encampments' (all subtypes, by "
                             "requested date)")
    for y, c in sorted(hc.items()):
        n += put_metric(conn, "homeless_concerns_311_requests", y, c, sid, unit="requests",
                        note="311 cases whose service_name is 'Homeless Concerns' (by requested date)")

    # 3b. SFPD dispatched calls: HOMELESS COMPLAINT per year
    recs, sid, _ = socrata(
        conn, "2zdj-bwza",
        select="date_extract_y(received_datetime) as y,count(*) as c",
        where="call_type_final_desc='HOMELESS COMPLAINT'", group="y", order="y",
        title="San Francisco law enforcement dispatched calls for service (closed)",
    )
    for r in recs:
        y, c = to_int(r.get("y")), to_int(r.get("c"))
        if y is None or c is None:
            continue
        n += put_metric(conn, "homeless_complaint_police_calls", y, c, sid, unit="calls",
                        note="CAD calls whose final call type is 'HOMELESS COMPLAINT' (dataset coverage "
                             "starts in 2015; a handful of 2014 rows exist)")
    # 3c. OCME: unintentional drug overdose deaths per year
    recs, sid, _ = socrata(
        conn, "jxrr-bmra",
        select="year,sum(total_deaths) as d,max(data_as_of) as asof", group="year", order="year",
        title="SF Office of the Chief Medical Examiner — preliminary unintentional drug overdose deaths",
    )
    for r in recs:
        y, d = to_int(r.get("year")), num(r.get("d"))
        asof = day(r.get("asof"))
        note = "Sum of monthly OCME preliminary unintentional fatal drug overdoses"
        if asof:
            note += f" (data_as_of {asof})"
        if y == max((to_int(x.get("year")) or 0) for x in recs):
            note += "; latest year is incomplete"
        n += put_metric(conn, "overdose_deaths", y, d, sid, unit="deaths", note=note)
    return n


# ---------------------------------------------------------------- 4. institutions
def _pdf_text(conn, url, name, title, publisher, license):
    body = fetch(url, domain=DOMAIN)
    p = save_raw(DOMAIN, name, body)
    sid = source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)
    txt_path = Path(p).with_suffix(".txt")
    try:
        subprocess.run(["pdftotext", "-enc", "UTF-8", str(p), str(txt_path)],
                       check=True, capture_output=True, timeout=300)
        return txt_path.read_text(errors="replace"), sid
    except Exception as e:  # pdftotext missing or PDF unreadable
        print(f"[sf] pdftotext failed for {url}: {e!r}", flush=True)
        return None, sid


def _wiki_raw(conn, page, title):
    url = WIKI.format(quote(page.replace(" ", "_"), safe=""))
    body = fetch(url, domain=DOMAIN).decode("utf-8", "replace")
    sid = source_id_for(conn, url, DOMAIN, title=title, publisher="Wikipedia (English)",
                        license="CC BY-SA 4.0")
    return body, sid


def _int(v):
    return to_int(re.sub(r"[^0-9]", "", v)) if v else None


def latest_metric(conn, metric):
    """(year, value) of the newest year for one of this domain's San Francisco metrics."""
    r = conn.execute(
        "SELECT year, value FROM metric WHERE entity_type='city' AND entity_id=? AND metric=? "
        "ORDER BY year DESC LIMIT 1", (ENTITY, metric)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def ingest_facilities(conn):
    n = 0
    text, jail_sid = _pdf_text(
        conn, JAIL_PDF, "sfso-annual-report-2025.pdf",
        "San Francisco Sheriff's Office — 2025 Annual Report",
        "San Francisco Sheriff's Office", "public record (published PDF)")
    # source page for the jails we name
    try:
        fetch(JAIL_INDEX, domain=DOMAIN)
    except Exception:
        pass
    jails_sid = source_id_for(conn, JAIL_INDEX, DOMAIN, title="San Francisco Sheriff's Office — Jails",
                              publisher="San Francisco Sheriff's Office", license="public record")
    annex_sid = source_id_for(
        conn, "https://sfsheriff.com/services/jail-services/jails/county-jail-2-annex",
        DOMAIN, title="San Francisco Sheriff's Office — County Jail #2 Annex",
        publisher="San Francisco Sheriff's Office", license="public record")
    try:
        fetch("https://sfsheriff.com/services/jail-services/jails/county-jail-2-annex", domain=DOMAIN)
    except Exception:
        pass

    facilities = []
    if text:
        # "COUNTY JAIL #2 - HOUSING\n425 7th Street | Opened: 1994 | Capacity: 392 | Average Jail Population: 361"
        blocks = re.findall(
            r"COUNTY JAIL #(\d) - ([A-Za-z ]+?)\n+([^\n]*?)Capacity: (\d+)\s*\|\s*Average Jail Population: (\d+)",
            text)
        per_fac = {}
        for num_, kind, line, cap, pop in blocks:
            addr = clean(line.split("|")[0])
            opened = re.search(r"Opened:\s*(\d{4})", line)
            per_fac[num_] = (kind.strip().title(), addr, to_int(opened.group(1)) if opened else None,
                             to_int(cap), to_int(pop))
        names = {"1": ("County Jail #1 - Intake and Release", "San Francisco"),
                 "2": ("County Jail #2 - Housing", "San Francisco"),
                 "3": ("County Jail #3 - Housing", "San Bruno")}
        for num_, (kind, addr, opened, cap, pop) in sorted(per_fac.items()):
            name, city = names.get(num_, (f"County Jail #{num_}", "San Francisco"))
            facilities.append({
                "name": name, "name_norm": name_norm(name), "kind": "jail", "status": "open",
                "opened": opened, "beds": cap, "operator": "San Francisco Sheriff's Office",
                "owner": CITY, "state": "CA", "city": city, "address": addr,
                "population_note": f"Capacity {cap}, average jail population {pop} (2025 Annual Report)",
                "source_id": jail_sid,
            })
        # Jail Health Services volumes. The PDF lays this stat grid out as all nine numbers, then all
        # nine labels in the same order, so the mapping is positional — checked against the labels.
        start = text.find("Jail Health Services")
        end = text.find("Individuals in SFSO custody", start + 1)
        block = text[start:end] if start >= 0 and end > start else ""
        labels = [
            ("Triage visits", "jail_health_triage_visits", "triage visits"),
            ("Nursing Visits", "jail_health_nursing_visits", "nursing visits"),
            ("Dental Visits", "jail_health_dental_visits", "dental visits"),
            ("MD/NP Visits", "jail_health_md_np_visits", "MD/NP visits (incl. psychiatry)"),
            ("STD & HIV Tests", "jail_health_std_hiv_tests", "STD and HIV tests"),
            ("Initial Mental Health", "jail_initial_mental_health_evaluations", "initial mental health evaluations"),
            ("Ongoing Mental Health", "jail_mental_health_treatment_visits", "ongoing mental health treatment visits"),
            ("Court-ordered Reports", "jail_court_ordered_reports", "court-ordered reports"),
            ("Release Planning", "jail_release_planning_cases", "release planning case management"),
        ]
        vals = [to_int(v.replace(",", "")) for v in re.findall(r"(?m)^([\d,]+)$", block)]
        if len(vals) == len(labels) and all(lbl.split()[0] in block for lbl, _, _ in labels):
            for val, (_, metric, label) in zip(vals, labels):
                n += put_metric(conn, metric, 2025, val, jail_sid, unit="visits",
                                note=f"Jail Health Services (DPH staff in the county jails): {label}, "
                                     f"2025 Annual Report")
        # ZSFGH jail wards 7D/7L: average monthly appointments
        m = re.search(r"([\d,]+)\nAverage Monthly\s*\nAppointments", text)
        if m:
            n += put_metric(conn, "sfgh_jail_ward_monthly_appointments", 2025, _int(m.group(1)), jail_sid,
                            unit="appointments/month",
                            note="Zuckerberg SF General Hospital jail wards 7D/7L — average monthly appointments")
        if len(per_fac) >= 3:
            cap_sum = sum(v[3] for v in per_fac.values())
            pop_sum = sum(v[4] for v in per_fac.values())
            n += put_metric(conn, "county_jail_beds", 2025, cap_sum, jail_sid, unit="beds", is_est=1,
                            note="Derived: sum of County Jail #1 (%d) + #2 (%d) + #3 (%d) capacity per the 2025 "
                                 "Annual Report" % tuple(per_fac[k][3] for k in sorted(per_fac)))
            n += put_metric(conn, "county_jail_average_daily_population", 2025, pop_sum, jail_sid, unit="people",
                            is_est=1,
                            note="Derived: sum of the average jail populations reported per facility (2025 "
                                 "Annual Report)")

    # closed intake annex, from the Sheriff's jail pages
    facilities.append({
        "name": "County Jail #2 Annex", "name_norm": name_norm("County Jail #2 Annex"), "kind": "jail",
        "status": "closed", "closed": 2020, "beds": None, "operator": "San Francisco Sheriff's Office",
        "owner": CITY, "state": "CA", "city": "San Francisco", "address": "850 Bryant Street, 7th Floor",
        "population_note": "Formerly County Jail #4; closed 5 September 2020 and no longer houses "
                           "incarcerated people (per Sheriff's Office page)",
        "source_id": annex_sid,
    })

    # Laguna Honda: bed count from the Wikipedia article text, budget from the Controller's DA5 program
    wiki, wiki_sid = _wiki_raw(conn, "Laguna Honda Hospital", "Laguna Honda Hospital (Wikipedia)")
    beds = None
    m = re.search(r"([\d,]+)\s*bed", wiki)
    if m:
        beds = _int(m.group(1))
    lhh_fy, lhh_val = latest_metric(conn, "laguna_honda_operating_budget")
    facilities.append({
        "name": "Laguna Honda Hospital and Rehabilitation Center", "name_norm": name_norm("Laguna Honda Hospital"),
        "kind": "other", "status": "open", "opened": 1866, "beds": beds,
        "operator": "San Francisco Department of Public Health", "owner": CITY,
        "state": "CA", "city": "San Francisco",
        "budget": int(lhh_val) if lhh_val else None,
        "population_note": "Publicly funded long-term acute care hospital / skilled nursing facility; "
                           "13 neighborhoods of roughly 60 residents each"
                           + (f"; budget = FY{lhh_fy} appropriation of the Laguna Honda Hospital Funds"
                              if lhh_val else ""),
        "source_id": wiki_sid,
    })
    n += put_metric(conn, "laguna_honda_beds", 2025, beds, wiki_sid, unit="beds",
                    note="Beds stated in the Wikipedia article for Laguna Honda Hospital")

    # San Francisco General: infobox profiles general acute care / acute psychiatric / SNF beds
    wiki, wiki_sid = _wiki_raw(conn, "San Francisco General Hospital",
                               "San Francisco General Hospital (Wikipedia)")
    def infobox_num(pat):
        m = re.search(pat, wiki)
        return _int(m.group(1)) if m else None
    gac = infobox_num(r"beds\s*=\s*([\d,]+)\s*General Acute Care")
    psych = infobox_num(r"([\d,]+)\s*Acute Psychiatric")
    snf_mh = infobox_num(r"([\d,]+)\s*Skilled Nursing Mental Health")
    sfgh_fy, sfgh_val = latest_metric(conn, "sfgh_operating_budget")
    facilities.append({
        "name": "Zuckerberg San Francisco General Hospital", "name_norm": name_norm("Zuckerberg San Francisco General Hospital"),
        "kind": "other", "status": "open", "beds": gac,
        "operator": "San Francisco Department of Public Health", "owner": CITY,
        "state": "CA", "city": "San Francisco", "address": "1001 Potrero Ave",
        "budget": int(sfgh_val) if sfgh_val else None,
        "population_note": f"Licensed beds: {gac} general acute care, {psych} acute psychiatric, {snf_mh} "
                           f"skilled nursing mental health; also houses the Sheriff's jail wards 7D/7L"
                           + (f"; budget = FY{sfgh_fy} appropriation of the General Hospital Medical Center Funds"
                              if sfgh_val else ""),
        "source_id": wiki_sid,
    })
    n += put_metric(conn, "sfgh_beds", 2025, gac, wiki_sid, unit="beds",
                    note="General acute care beds from the hospital infobox")
    n += put_metric(conn, "sfgh_acute_psych_beds", 2025, psych, wiki_sid, unit="beds",
                    note="Acute psychiatric beds from the hospital infobox")
    n += put_metric(conn, "sfgh_skilled_nursing_mental_health_beds", 2025, snf_mh, wiki_sid, unit="beds",
                    note="Skilled nursing mental health beds from the hospital infobox")

    for f in facilities:
        upsert(conn, "facility", f, keys=["name_norm", "state", "city"])
        n += 1
    return n


# ---------------------------------------------------------------- 5. documents + measures
DOCS = [
    ("dataset", "San Francisco Controller — Budget (Annual Appropriation Ordinance line items)",
     RES.format("xdgd-c79v"), CITY, "All budget line items 2010-2028 by department, program, fund, object",
     PDDL),
    ("dataset", "San Francisco Controller — Supplier Contracts",
     RES.format("cqi5-hm2d"), CITY, "Executed contracts with vendors, agreed amounts and terms", PDDL),
    ("dataset", "San Francisco 311 Cases", RES.format("vw6y-z8j6"), CITY,
     "Every 311 case with service name/subtype, address, district and dates", PDDL),
    ("dataset", "San Francisco law enforcement dispatched calls for service (closed)",
     RES.format("2zdj-bwza"), CITY, "CAD calls with final call type and disposition", PDDL),
    ("dataset", "SF Office of the Chief Medical Examiner — preliminary unintentional drug overdose deaths",
     RES.format("jxrr-bmra"), CITY, "Monthly preliminary unintentional fatal overdose counts", None),
    ("dataset", "San Francisco Ballot Propositions Database (historical ballot propositions)",
     RES.format("88s2-6ua9"), "San Francisco Public Library / City and County of San Francisco",
     "Measure letter, title, description, pass/fail and vote percentages since 1911", PDDL),
    ("report", "San Francisco Sheriff's Office — 2025 Annual Report", JAIL_PDF,
     "San Francisco Sheriff's Office",
     "Per-jail capacity and average population, Jail Health Services mental-health volumes, custody programs",
     "public record"),
]


def ingest_docs(conn):
    n = 0
    for kind, title, url, publisher, summary, license in DOCS:
        try:
            fetch(url, domain=DOMAIN)
        except Exception as e:
            print(f"[sf] could not fetch {url}: {e!r}", flush=True)
            continue
        sid = source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)
        upsert(conn, "doc", {
            "kind": kind, "title": title, "url": url, "publisher": publisher,
            "place": "San Francisco", "state": "CA", "summary": summary, "source_id": sid,
        }, keys=["url"])
        n += 1

    # the two ballot measures, each fetched by its own query so the URL is the evidence
    for where, kind in [
        ("year='2018' and propletter='C' and upper(proptitle) like '%HOMELESS%'", "article"),
        ("year='2024' and propletter='C'", "article"),
    ]:
        recs, sid, url = socrata(
            conn, "88s2-6ua9",
            select="propletter,proptitle,year,date,kind,description,pass_fail,percent_vote_yes",
            where=where, limit=10,
            title="San Francisco Ballot Propositions Database", publisher=DOCS[5][3],
        )
        for r in recs:
            title = f"San Francisco {r.get('date','')[:4]} Measure {r.get('propletter')}: {clean(r.get('proptitle'))}"
            summary = clean(r.get("description"))
            pct = num(r.get("percent_vote_yes"))
            if pct is not None:
                summary = (summary or "") + f" Result: {r.get('pass_fail')} with {pct*100:.1f}% yes."
            upsert(conn, "doc", {
                "kind": kind, "title": title[:300], "url": url, "publisher": DOCS[5][3],
                "published": day(r.get("date")), "place": "San Francisco", "state": "CA",
                "summary": summary, "source_id": sid,
            }, keys=["url"])
            n += 1
    return n


# ---------------------------------------------------------------- entrypoint
def run(conn, cfg=None):
    notes = []
    contracts = ingest_contracts(conn)
    conn.commit()
    notes.append(f"contracts={contracts}")

    metrics = ingest_budget(conn)
    conn.commit()
    notes.append(f"budget_metrics={metrics}")

    counts = ingest_counts(conn)
    conn.commit()
    notes.append(f"count_metrics={counts}")

    fac = ingest_facilities(conn)
    conn.commit()
    notes.append(f"facility_metrics+rows={fac}")

    docs = ingest_docs(conn)
    conn.commit()
    notes.append(f"docs={docs}")

    total = contracts + metrics + counts + fac + docs
    return {"rows": total, "notes": " | ".join(notes),
            "sources": [(RES.format("xdgd-c79v"), PDDL), (RES.format("cqi5-hm2d"), PDDL),
                        (RES.format("vw6y-z8j6"), PDDL), (RES.format("2zdj-bwza"), PDDL),
                        (RES.format("jxrr-bmra"), None), (RES.format("88s2-6ua9"), PDDL),
                        (JAIL_PDF, "public record")]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    conn = db()
    try:
        res = run(conn, {})
        conn.commit()
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                     (DOMAIN, now(), res["rows"], res["notes"]))
        conn.commit()
        print(f"[{DOMAIN}] rows={res['rows']} {res['notes']}")
        if args.selftest:
            for q in [
                "SELECT award_id, recipient_name, amount_total, pop_start, pop_end FROM award "
                "WHERE award_type='local_contract' ORDER BY amount_total DESC LIMIT 3",
                "SELECT metric, year, value, unit FROM metric WHERE entity_id='San Francisco' "
                "AND metric LIKE 'hsh_budget%' ORDER BY year DESC LIMIT 3",
                "SELECT name, kind, beds, status FROM facility WHERE state='CA' AND city IN "
                "('San Francisco','San Bruno') AND name LIKE '%Jail%' LIMIT 3",
            ]:
                for r in conn.execute(q):
                    print("   ", dict(r))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
