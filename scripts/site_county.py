"""County pages for the Homeless Atlas site.

Called from build_site.main() after the CoC and city pages. Writes:

  county/<fips>.html        one page per county: the count, the money, who answers for it
  county/<fips>-print.html  the WTF street flyer (scripts/flyer.py), when every number on it traces to a record
  api/flyer/<fips>.json     that flyer's county file, in the kit's format, for scripts/flyer.py --from
  counties.html             every county in one sortable table
  freshness.html            when each upstream source last changed and when we last checked

Everything here reads tables that ingest domains write: metric (entity_type 'county', plus the
county overdose rows that health.py files under entity_type 'city' with a 5-digit FIPS),
county_coc, county_recipient (county), officials_* (officials), local_sources_* (local_sources),
and research/county/<ST>/<fips>-<slug>.md. Any of those may be missing in a partial database;
the page then leaves that section out instead of printing an empty table.

Page design rules: the site's own parts only (banner, kpi row, ruled tables, dl.facts). One
plain "Answerable" line per section names the office responsible and its published number.
No new colours, badges or boxes.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import flyer  # scripts/flyer.py: the WTF street flyer
import site_records  # scripts/site_records.py: records request letters and law guides

B: Any = None  # the build_site module, set by build()
NA = ""  # the site's empty-cell mark, taken from B.num(None) in _bind()

STATE_NAMES = {}

# ------------------------------------------------------------------ metric catalogue
# (metric, label, format, higher_is_worse)
HOUSING = [
    ("median_gross_rent", "Median gross rent, monthly", "usd", True),
    ("median_household_income", "Median household income", "usd", False),
    ("rent_burden_30", "Renters paying 30% or more of income", "pct", True),
    ("rent_burden_50", "Renters paying 50% or more of income", "pct", True),
    ("rental_vacancy_rate", "Rental vacancy rate", "pct", False),
    ("poverty_rate", "People below the poverty line", "pct", True),
    ("unemployment_rate", "Unemployment rate", "pct", True),
    ("overcrowded_share", "Homes with more than one person per room", "pct", True),
    ("public_assistance_share", "Households with public assistance income", "pct", True),
]
HEALTH = [
    ("overdose_deaths", "Drug overdose deaths", "n", True),
    ("overdose_rate", "Overdose deaths per 100,000", "r", True),
    ("places_mental_distress", "Adults with frequent mental distress", "pct", True),
    ("places_depression", "Adults ever told they have depression", "pct", True),
    ("places_binge", "Adults who binge drink", "pct", True),
    ("uninsured_rate", "People without health insurance", "pct", True),
    ("mh_hpsa_max_score", "Mental health shortage score (0 to 25)", "n", True),
    ("veteran_share", "Veterans among adults", "pct", False),
]
COMPARE = [m for m, *_ in HOUSING + HEALTH]

LEVEL_LABEL = {
    "federal_senate": "U.S. Senate", "federal_house": "U.S. House",
    "state_upper": "State Senate", "state_lower": "State House",
    "pha": "Housing authority", "coc_lead": "Continuum of Care lead",
}
LEVEL_ORDER = ["local", "coc_lead", "pha", "state_lower", "state_upper", "federal_house", "federal_senate"]


# ------------------------------------------------------------------ small helpers
def fmt(v, kind):
    if v is None:
        return NA
    if kind == "usd":
        return f"${float(v):,.0f}"
    if kind == "pct":
        return B.pct(v)
    if kind == "r":
        return B.rate(v)
    return B.num(v)


def tel(phone):
    """Published office number as a tap-to-call link. Digits only in the href."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return B.esc(phone)
    shown = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return f'<a href="tel:+1{digits}">{shown}</a>'


def mailto(email):
    if not email or "@" not in str(email):
        return ""
    return f'<a href="mailto:{B.esc(email)}">{B.esc(email)}</a>'


def table_exists(name):
    return bool(B.q(B.CONN, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def front_matter(path: Path):
    """(front matter dict, markdown body) for a research file. Empty dict when there is none."""
    import yaml
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    try:
        return (yaml.safe_load(m.group(1)) or {}), m.group(2)
    except Exception:
        return {}, m.group(2)


def research_files():
    root = B.ROOT / "research" / "county"
    out = {}
    if root.exists():
        for p in sorted(root.glob("*/[0-9]*.md")):
            fips = p.name.split("-", 1)[0]
            if re.fullmatch(r"\d{5}", fips):
                out[fips] = p
    return out


# ------------------------------------------------------------------ data load
def load_counties():
    """Every county, its metrics by name (each a list sorted newest first), and peer values."""
    for r in B.q(B.CONN, "SELECT abbr, name FROM geo_state"):
        STATE_NAMES[r["abbr"]] = r["name"]
    counties = B.q(B.CONN, """SELECT geoid fips, name, state, pop, lat, lon FROM geo_city
                              WHERE kind='county' AND length(geoid)=5 ORDER BY state, name""")
    byfips = {c["fips"]: c for c in counties}
    metrics = defaultdict(lambda: defaultdict(list))
    rows = B.q(B.CONN, """SELECT entity_type, entity_id, metric, value, unit, year, is_est, note, source_id
                          FROM metric WHERE (entity_type='county')
                             OR (entity_type='city' AND length(entity_id)=5
                                 AND metric IN ('overdose_deaths','overdose_rate'))""")
    for r in rows:
        if r["entity_id"] in byfips and r["value"] is not None:
            metrics[r["entity_id"]][r["metric"]].append(r)
    for m in metrics.values():
        for k in m:
            m[k].sort(key=lambda r: (r["year"] or 0), reverse=True)
    # latest value of each comparable metric, nationally and per state, for "compared with" columns
    peers = {k: {"US": []} for k in COMPARE}
    for fips, m in metrics.items():
        st = byfips[fips]["state"]
        for k in COMPARE:
            if m.get(k):
                v = m[k][0]["value"]
                peers[k]["US"].append(v)
                peers[k].setdefault(st, []).append(v)
    for k in peers:
        for g in peers[k]:
            peers[k][g].sort()
    return counties, metrics, peers


def median(vals):
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def share_below(vals, v):
    """Percent of peer counties with a lower value."""
    if not vals or v is None:
        return None
    import bisect
    return bisect.bisect_left(vals, v) / len(vals) * 100


def latest(m, key):
    return (m.get(key) or [None])[0]


def count_series(m):
    """The county's homeless count by year: exact if the county is its own CoC, else apportioned."""
    if m.get("pit_total"):
        return sorted(m["pit_total"], key=lambda r: r["year"]), False
    return sorted(m.get("pit_total_apportioned") or [], key=lambda r: r["year"]), True


# ------------------------------------------------------------------ who answers
KEEP_UPPER = {"AIDS", "HIV", "HUD", "LLC", "NAACP", "USA", "YMCA", "YWCA", "II", "III", "IV", "PA", "NY", "NJ", "DC"}
SMALL = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "the", "to"}


def proper(name):
    """'COUNTY OF ERIE' -> 'County of Erie'. Names already in mixed case are left exactly as published."""
    if not name or not name.isupper():
        return name
    out = []
    for i, w in enumerate(name.split()):
        core = w.strip(".,()&")
        if core in KEEP_UPPER:
            out.append(w)
        elif i and w.lower() in SMALL:
            out.append(w.lower())
        else:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(out)


def responsible(fips):
    """People and offices responsible for this county, best first.

    Each row: level, name, role, phone, email, address, url, for (set of figure keys), share,
    checked (True when local_sources found the number on the official page), source.
    """
    out = []
    if table_exists("local_sources_responsible"):
        for r in B.q(B.CONN, "SELECT * FROM local_sources_responsible WHERE county_fips=?", (fips,)):
            name = " · ".join(x for x in (r.get("name"), r.get("office")) if x) or r.get("office")
            out.append({"level": "local", "name": name, "role": r.get("title") or r.get("role"),
                        "phone": r.get("phone"), "email": r.get("email"), "address": r.get("address"),
                        "url": r.get("url"), "share": None, "elected": bool(r.get("elected")),
                        "for": set((r.get("for_keys") or "").replace(",", " ").split()),
                        "checked": bool(r.get("phone_ok"))})
    if table_exists("officials_county"):
        for r in B.q(B.CONN, """SELECT oc.level, oc.share, o.name, o.kind, o.phone, o.email, o.address, o.url,
                                       o.exec_name, o.exec_phone, o.exec_email
                                FROM officials_county oc JOIN officials_office o ON o.id=oc.person_or_office_id
                                WHERE oc.county_fips=?""", (fips,)):
            role = "Executive director: " + r["exec_name"] if r.get("exec_name") else None
            out.append({"level": r.get("kind") or r["level"], "name": proper(r["name"]), "role": role,
                        "phone": r.get("exec_phone") or r.get("phone"), "email": r.get("exec_email") or r.get("email"),
                        "address": r.get("address"), "url": r.get("url"), "share": None, "elected": False,
                        "for": {"housing_authority"} if (r.get("kind") == "pha") else {"pit_count", "coc_funding"},
                        "checked": False})
        for r in B.q(B.CONN, """SELECT oc.level, oc.share, p.name, p.party, p.title, p.body, p.district,
                                       p.phone, p.email, p.address, p.url, p.contact_form
                                FROM officials_county oc JOIN officials_person p ON p.id=oc.person_or_office_id
                                WHERE oc.county_fips=?""", (fips,)):
            party = f" ({r['party'][:1]})" if r.get("party") else ""
            district = f", district {r['district']}" if r.get("district") else ""
            out.append({"level": r["level"], "name": f"{r['name']}{party}",
                        "role": f"{LEVEL_LABEL.get(r['level'], r['level'])}{district}",
                        "phone": r.get("phone"), "email": r.get("email"), "address": r.get("address"),
                        "url": r.get("url") or r.get("contact_form"), "share": r.get("share"), "elected": True,
                        "for": {"federal_money"} if r["level"].startswith("federal") else {"state_money"},
                        "checked": False})
    rank = {k: i for i, k in enumerate(LEVEL_ORDER)}
    out.sort(key=lambda r: (rank.get(r["level"], 99), -(r["share"] or 0), r["name"] or ""))
    return out


def answerable(people, keys, fallback_levels=()):
    """One quiet line naming who answers for a section, with a number to call."""
    pick = [p for p in people if p["for"] & set(keys) and p.get("phone")]
    if not pick:
        pick = [p for p in people if p["level"] in fallback_levels and p.get("phone")][:2]
    if not pick:
        return ""
    parts = [f'{B.esc(p["name"])} {tel(p["phone"])}' for p in pick[:3]]
    return f'<p class="dim" style="font-size:.84rem">Answerable: {" · ".join(parts)}</p>'


# ------------------------------------------------------------------ the county page
def page_county(c, m, peers, research, total):
    fips, name, st = c["fips"], c["name"], c["state"]
    stname = STATE_NAMES.get(st, st)
    series, apportioned = count_series(m)
    cur = series[-1] if series else None
    t = B.trend(series, "value") if series else None
    cocs = B.q(B.CONN, """SELECT x.coc_num, x.county_share, x.coc_share, g.name FROM county_coc x
                          LEFT JOIN geo_coc g ON g.coc_num=x.coc_num
                          WHERE x.county_fips=? AND x.coc_num<>'(none)' ORDER BY x.county_share DESC""", (fips,)) \
        if table_exists("county_coc") else []
    people = responsible(fips)
    used_sources = set()

    def src(r):
        if r and r.get("source_id"):
            used_sources.add(r["source_id"])
        return r

    # ---- headline
    if cur:
        src(cur)
        dag = B.est(apportioned)
        h1 = (f'{B.num(cur["value"])}{dag} people homeless in {B.esc(name)}' if not apportioned else
              f'About {B.num(cur["value"])}{dag} people homeless in {B.esc(name)}')
        how = (f'HUD point-in-time count, January {cur["year"]}, '
               f'<a href="/coc/{B.esc(cocs[0]["coc_num"])}.html">{B.esc(cocs[0]["coc_num"])}</a>, whose boundary is this county.'
               if (not apportioned and cocs) else
               "HUD counts by Continuum of Care, not by county. This is the county's population share of "
               + ", ".join(f'<a href="/coc/{B.esc(x["coc_num"])}.html">{B.esc(x["coc_num"])}</a>' for x in cocs[:4])
               + f', January {cur["year"]}. Treat it as an estimate.')
    else:
        h1, how = f'{B.esc(name)}, {B.esc(stname)}', "No HUD count could be matched to this county."
    pop = latest(m, "acs_population")
    sub = (f'{how} Population {B.num(pop["value"] if pop else c.get("pop"))}. '
           + (f'<a href="/county/{fips}-print.html">Street flyer</a>' if fips in FLYERS else ""))

    # ---- kpis
    awards = sorted(m.get("federal_homeless_awards") or [], key=lambda r: r["year"])
    aw_last = awards[-1] if awards else None
    od = latest(m, "overdose_deaths")
    rb = latest(m, "rent_burden_50")
    per_person = (aw_last["value"] / cur["value"]) if (aw_last and cur and cur["value"]) else None
    k1_note = B.trend_badge(t) if t else ""
    kpis = "".join([
        B.kpi(B.num(cur["value"]) + B.est(apportioned) if cur else NA, f"homeless, January {cur['year']}" if cur else "homeless", k1_note),
        B.kpi(B.money(aw_last["value"]) if aw_last else NA, f"federal homelessness awards, FY{aw_last['year']}" if aw_last else "federal homelessness awards",
              (f"{B.money(per_person)} per person counted" + B.est(True)) if per_person else ""),
        B.kpi(B.num(od["value"]) if od else NA, f"overdose deaths, {od['year']}" if od else "overdose deaths",
              (f'{B.rate(odr["value"])} per 100,000' if (odr := latest(m, "overdose_rate")) else "")),
        B.kpi(B.pct(rb["value"]) if rb else NA, "renters paying half their income or more",
              f"ACS {rb['year'] - 4}-{rb['year']}" if rb else ""),
    ])
    for r in (aw_last, od, rb):
        src(r)

    # ---- funding and outcome
    count_by_year = {r["year"]: r for r in series}
    fy_rows = []
    for a in awards:
        src(a)
        pc = count_by_year.get(a["year"])
        prev = count_by_year.get(a["year"] - 1)
        per = (a["value"] / pc["value"]) if (pc and pc["value"]) else None
        chg = (pc["value"] - prev["value"]) if (pc and prev and pc["value"] is not None and prev["value"] is not None) else None
        fy_rows.append([
            f"FY{a['year']}", "", a["year"],
            B.money(a["value"]), "r", a["value"],
            (B.num(pc["value"]) + B.est(apportioned)) if pc else NA, "r", (pc or {}).get("value") or 0,
            (B.money(per) + B.est(True)) if per else NA, "r", per or 0,
            (f"{chg:+,.0f}" if chg is not None else NA), "r", chg or 0,
        ])
    funding = ""
    if fy_rows:
        funding = (
            "<h2>Funding and outcome</h2>"
            + B.table([("Fiscal year", ""), ("Federal homelessness awards", "r"), ("People counted in January", "r"),
                       ("Dollars per person counted", "r"), ("Change in count", "r")], fy_rows,
                      note="Awards are obligations performed in this county under the Continuum of Care, Emergency "
                           "Solutions Grant, Youth Homelessness Demonstration, VA Grant and Per Diem, PATH, Basic Center "
                           "and HOPWA programs (USASpending). The January count falls inside the same federal fiscal year. "
                           "Dollars per person counted is a ratio we compute, not a cost per person served.")
            + answerable(people, {"pit_count", "coc_funding"}, ("coc_lead", "federal_house")))

    # ---- where the money went
    money_html = ""
    if table_exists("county_recipient"):
        rec = B.q(B.CONN, """SELECT r.*, o.website, o.phone, o.name org_name FROM county_recipient r
                             LEFT JOIN org o ON o.id=r.org_id WHERE r.county_fips=? ORDER BY r.rank""", (fips,))
        rows = []
        for r in rec:
            src(r)
            officer = None
            if r.get("org_id"):
                officer = B.one(B.CONN, """SELECT person, title, comp, year FROM org_person WHERE org_id=?
                                           ORDER BY year DESC, COALESCE(comp,0) DESC LIMIT 1""", (r["org_id"],))
            url = B.ORG_URL.get(r.get("org_id"))
            label = B.esc(proper(r["recipient_name"]))
            rows.append([
                f'<a href="{url}">{label}</a>' if url else label, "", r["recipient_name"],
                B.money(r["amount"]), "r", r["amount"] or 0,
                (f'{B.esc(officer["person"])}, {B.esc(officer["title"] or "")} ({officer["year"]})' if officer else NA), "", "",
                tel(r.get("phone")) or (f'<a href="{B.esc(r["website"])}" rel="noopener">website</a>' if r.get("website") else NA), "", "",
            ])
        if rows:
            period = rec[0]["period"].replace("FY", "FY ").replace("-", " to ")
            cols = [("Recipient", ""), (period, "r"), ("Top officer on its latest Form 990", ""), ("Contact", "")]
            # drop a column no recipient here has data for, rather than print a column of blanks
            keep = [i for i in range(len(cols)) if i < 2 or any(r[i * 3] != NA for r in rows)]
            cols = [cols[i] for i in keep]
            rows = [sum((r[i * 3:i * 3 + 3] for i in keep), []) for r in rows]
            money_html = ("<h2>Where the money went</h2>"
                          + B.table(cols, rows,
                                    note="Recipients of the same federal homelessness programs for work performed in this county "
                                         "(USASpending)." + (" Officers are as named in the organization's own IRS Form 990."
                                                             if 2 in keep else "")))

    # ---- who is responsible
    resp_html = ""
    if people:
        rows = []
        for p in people:
            level = "Local" if p["level"] == "local" else (LEVEL_LABEL.get(p["level"]) or p["level"])
            if p.get("share") is not None and p["level"] in ("federal_house", "state_upper", "state_lower") and p["share"] < 0.995:
                level += f" ({p['share'] * 100:.0f}% of county)"
            nm = f'<a href="{B.esc(p["url"])}" rel="noopener">{B.esc(p["name"])}</a>' if p.get("url") else B.esc(p["name"])
            rows.append([nm, "", p["name"], B.esc(p.get("role") or ""), "", p.get("role") or "",
                         B.esc(level), "", level, tel(p.get("phone")) or NA, "", "",
                         mailto(p.get("email")) or NA, "", "", B.esc(p.get("address") or ""), "", ""])
        resp_html = ("<h2>Who is responsible</h2>"
                     + B.table([("Office or official", ""), ("Role", ""), ("Level", ""), ("Phone", ""), ("Email", ""), ("Office address", "")],
                               rows, note="Official office contacts as each office or official publishes them: local offices from "
                                          "this county's research file (checked against the linked page), housing authorities from HUD, "
                                          "legislators from Open States and the congress-legislators project. No personal numbers or "
                                          "home addresses are collected."))

    # ---- housing pressure and health, compared with the state and the country
    def compare_table(spec, title, note, keys, fallback):
        rows = []
        for key, label, kind, worse in spec:
            r = latest(m, key)
            if not r:
                continue
            src(r)
            st_med = median(peers[key].get(st, []))
            us_med = median(peers[key]["US"])
            rank = share_below(peers[key]["US"], r["value"])
            if rank is not None and not worse:
                rank = 100 - rank
            rows.append([B.esc(label), "", label,
                         fmt(r["value"], kind) + B.est(bool(r["is_est"])), "r", r["value"],
                         fmt(st_med, kind), "r", st_med or 0, fmt(us_med, kind), "r", us_med or 0,
                         (f"worse than {rank:.0f}% of counties" if rank is not None else NA), "", rank or 0,
                         B.yr(r["year"]), "r", r["year"] or 0, B.cite(r.get("source_id")), "", ""])
        if not rows:
            return ""
        return (f"<h2>{title}</h2>"
                + B.table([("Measure", ""), ("This county", "r"), (f"{B.esc(stname)} median", "r"), ("U.S. median", "r"),
                           ("Rank", ""), ("Year", "r"), ("Source", "")], rows, note=note)
                + answerable(people, keys, fallback))

    housing = compare_table(HOUSING, "Housing pressure",
                            "American Community Survey 2020 to 2024 five-year estimates for every county. Medians are across "
                            "counties, not people. Margins of error are published by the Census Bureau and not shown here.",
                            {"county_budget", "housing_authority"}, ("pha", "state_lower"))
    health = compare_table(HEALTH, "Mental health and addiction",
                           "Overdose deaths: CDC NCHS provisional county counts. Adult health measures: CDC PLACES model-based "
                           "estimates (†). Shortage score: HRSA mental health professional shortage designations; higher is worse.",
                           {"behavioral_health", "overdose"}, ("state_lower", "state_upper"))

    # ---- local research
    local = ""
    p = research.get(fips)
    if p:
        fm, body = front_matter(p)
        checks = B.q(B.CONN, "SELECT url, status, via, archived_at FROM local_sources_check WHERE county_fips=?", (fips,)) \
            if table_exists("local_sources_check") else []
        archived = {r["url"]: r for r in checks if (r.get("via") or "").startswith("wayback")}
        rel = p.relative_to(B.ROOT)
        local = (f'<h2>Local research</h2><div class="prose">{B.md(body)}</div>'
                 + (f'<p class="dim" style="font-size:.8rem">{len(archived)} of {len(checks)} sources were read from an '
                    f'Internet Archive copy because the publisher refused automated requests.</p>' if archived else "")
                 + f'<p class="dim" style="font-size:.8rem">Updated {B.esc(str(fm.get("updated", "")))} · '
                   f'<a href="https://github.com/0xSero/homeless-atlas/blob/master/{B.esc(str(rel))}">edit this research</a></p>')
    else:
        local = ('<h2>Local research</h2><p class="dim">No one has written the local record for this county yet. '
                 '<a href="https://github.com/0xSero/homeless-atlas/blob/master/research/county/README.md">Add it</a>: '
                 'one file, one pull request.</p>')

    # ---- CoC context for apportioned counties
    coc_html = ""
    if apportioned and cocs:
        rows = [[f'<a href="/coc/{B.esc(x["coc_num"])}.html">{B.esc(x["coc_num"])}</a>', "", x["coc_num"],
                 B.esc(x.get("name") or ""), "", x.get("name") or "",
                 B.pct((x["county_share"] or 0) * 100), "r", x["county_share"] or 0,
                 B.pct((x["coc_share"] or 0) * 100), "r", x["coc_share"] or 0] for x in cocs]
        coc_html = ("<h2>Continuums of Care</h2>"
                    + B.table([("CoC", ""), ("Name", ""), ("Share of this county", "r"), ("This county's share of the CoC", "r")], rows,
                              note="Shares of population, from 2020 to 2024 ACS tract populations placed in HUD FY2024 CoC boundaries."))

    seen, items = set(), []
    for sid in used_sources:
        r = source_row(sid)
        if not r or not str(r.get("url") or "").startswith("http"):
            continue
        key = (r.get("publisher") or "", r.get("title") or r["url"])
        if key in seen:
            continue
        seen.add(key)
        items.append((key, f'{B.esc(key[0])}{". " if key[0] else ""}<a href="{B.esc(r["url"])}" rel="noopener">{B.esc(key[1])}</a>'))
    srcs = "".join(f"<li>{h}</li>" for _, h in sorted(items))
    body = B.banner(f"County · {stname}", h1, sub,
                    crumbs=f'<a href="/index.html">Atlas</a> / <a href="/counties.html">Counties</a> / '
                           f'<a href="/state/{B.esc(st)}.html">{B.esc(stname)}</a> / {B.esc(name)}', small=True) + f"""
<div class="wrap">
  <div class="kpis">{kpis}</div>
  {funding}
  {money_html}
  {resp_html}
  {site_records.county_section_for(B, c)}
  {housing}
  {health}
  {coc_html}
  {local}
  {f'<h2>Sources</h2><ul style="font-size:.86rem">{srcs}</ul>' if srcs else ''}
  {cite_block(c, stname)}
</div>"""
    rec = county_record(c, m)
    RECORDS[fips] = rec
    head = (f'<link rel="alternate" type="application/json" href="/api/county/{fips}.json">' + json_ld(c, stname, rec))
    facts = [f"{B.num(cur['value'])}{' (estimated)' if apportioned else ''} people counted homeless in January {cur['year']}"] if cur else []
    if aw_last:
        facts.append(f"{B.money(aw_last['value'])} in federal homelessness awards in FY{aw_last['year']}")
    desc = (f"Homelessness in {name}, {stname}: " + ("; ".join(facts) + ". " if facts else "")
            + "Who received the money, the officials responsible and their published numbers, with every source linked.")
    return B.shell("../", f"Homelessness in {name}, {stname}", "counties", body, head=head, manifest_total=total, desc=desc)


# ------------------------------------------------------------------ data for agents and citations
_SRC: dict = {}
LABELS = {k: lab for k, lab, *_ in HOUSING + HEALTH}
LABELS.update({
    "pit_total": "People counted homeless (HUD point-in-time)", "pit_total_apportioned": "People counted homeless, county share of CoC count",
    "pit_sheltered": "Sheltered", "pit_unsheltered": "Unsheltered", "pit_chronic": "Chronically homeless",
    "pit_veterans": "Veterans counted homeless", "pit_youth": "Unaccompanied youth counted homeless",
    "federal_homeless_awards": "Federal homelessness awards (obligations)", "acs_population": "Population",
})


def source_row(sid):
    if sid is None:
        return None
    if sid not in _SRC:
        r = B.one(B.CONN, "SELECT url, publisher, title, license, retrieved_at FROM source WHERE id=?", (sid,), {}) or {}
        _SRC[sid] = {k: r.get(k) for k in ("url", "publisher", "title", "license", "retrieved_at")} if r.get("url") else None
    return _SRC[sid]


def county_record(c, m):
    """Every figure on the county page as data: value, year, whether it is an estimate, and its source."""
    figures = []
    for key in sorted(m):
        rows = m[key]
        if not rows:
            continue
        for r in rows if key in ("pit_total", "pit_total_apportioned", "federal_homeless_awards") else rows[:1]:
            figures.append({"metric": key, "label": LABELS.get(key, key.replace("_", " ")), "value": r["value"],
                            "year": r["year"], "is_estimate": bool(r.get("is_est")), "source": source_row(r.get("source_id"))})
    return {"county_fips": c["fips"], "county": c["name"], "state": c["state"],
            "url": f"{B.BASE_URL}/county/{c['fips']}.html", "generated": B.NOW,
            "notes": "is_estimate marks figures the atlas derived (for example a county's population share of its "
                     "Continuum of Care count); every other figure is as published by the source.",
            "figures": figures}


def json_ld(c, stname, rec):
    years = sorted({f["year"] for f in rec["figures"] if isinstance(f.get("year"), int)})
    d = {"@context": "https://schema.org", "@type": "Dataset",
         "name": f"Homelessness in {c['name']}, {stname}: counts, federal funding and officials",
         "description": (f"County-level public-record data on homelessness in {c['name']}, {stname}: HUD point-in-time counts, "
                         "federal homelessness awards and recipients from USASpending, housing cost and health measures "
                         "from the Census Bureau and CDC, and the published office contacts of the officials responsible. "
                         "Each figure carries its year, source and whether it is an estimate."),
         "url": rec["url"], "isAccessibleForFree": True, "dateModified": B.NOW,
         "creator": {"@type": "Organization", "name": "Homeless Atlas", "url": B.BASE_URL + "/"},
         "spatialCoverage": {"@type": "Place", "name": f"{c['name']}, {stname}",
                             **({"geo": {"@type": "GeoCoordinates", "latitude": c["lat"], "longitude": c["lon"]}}
                                if c.get("lat") is not None and c.get("lon") is not None else {})},
         "variableMeasured": sorted({f["label"] for f in rec["figures"]})[:40],
         "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                           "contentUrl": f"{B.BASE_URL}/api/county/{c['fips']}.json"}],
         "isBasedOn": sorted({f["source"]["url"] for f in rec["figures"] if f.get("source")})[:30]}
    if years:
        d["temporalCoverage"] = f"{years[0]}/{years[-1]}" if years[0] != years[-1] else str(years[0])
    return '<script type="application/ld+json">' + json.dumps(d).replace("</", "<\\/") + "</script>"


def cite_block(c, stname):
    title = f"Homelessness in {c['name']}, {stname}"
    url = f"{B.BASE_URL}/county/{c['fips']}.html"
    y, mo, d = B.NOW.split("-")
    month = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"][int(mo) - 1]
    apa = f"Homeless Atlas. ({y}, {month} {int(d)}). <i>{B.esc(title)}</i> [Data set]. {url}"
    bib = (f"@misc{{homelessatlas_{c['fips']},\n  author = {{{{Homeless Atlas}}}},\n  title = {{{title}}},\n"
           f"  year = {{{y}}},\n  howpublished = {{\\url{{{url}}}}},\n  note = {{Data set, updated {B.NOW}. Figures from HUD, "
           f"USASpending, the U.S. Census Bureau and CDC; see the sources listed on the page}}\n}}")
    return (f'<details class="cite"><summary>Cite this page</summary>'
            f'<p><b>APA 7</b><br>{apa}</p>'
            f'<p><b>BibTeX</b></p><pre style="white-space:pre-wrap;font-size:.8rem">{B.esc(bib)}</pre>'
            f'<p class="dim" style="font-size:.8rem">Cite the original source for any single figure; each one is linked '
            f'in its table. Data as JSON: <a href="/api/county/{c["fips"]}.json">/api/county/{c["fips"]}.json</a>.</p></details>')


RECORDS: dict = {}
FLYERS: set = set()  # counties whose flyer passed every check this build


# ------------------------------------------------------------------ index and freshness
def page_counties(counties, metrics, research, total):
    rows = []
    for c in counties:
        m = metrics.get(c["fips"], {})
        series, apportioned = count_series(m)
        cur = series[-1] if series else None
        pop = (latest(m, "acs_population") or {}).get("value") or c.get("pop")
        per10k = (cur["value"] / pop * 10000) if (cur and pop) else None
        aw = sum(r["value"] or 0 for r in (m.get("federal_homeless_awards") or []))
        od = latest(m, "overdose_rate")
        rb = latest(m, "rent_burden_50")
        rows.append([
            f'<a href="/county/{c["fips"]}.html">{B.esc(c["name"])}</a>', "", c["name"],
            f'<a href="/state/{B.esc(c["state"])}.html">{B.esc(c["state"])}</a>', "", c["state"],
            (B.num(cur["value"]) + B.est(apportioned)) if cur else NA, "r", (cur or {}).get("value") or 0,
            (B.rate(per10k) + B.est(apportioned)) if per10k else NA, "r", per10k or 0,
            B.money(aw) if aw else NA, "r", aw,
            B.pct(rb["value"]) if rb else NA, "r", (rb or {}).get("value") or 0,
            B.rate(od["value"]) if od else NA, "r", (od or {}).get("value") or 0,
            "yes" if c["fips"] in research else "", "", 1 if c["fips"] in research else 0,
        ])
    rows.sort(key=lambda r: -(r[8] or 0))
    n_local = sum(1 for c in counties if c["fips"] in research)
    body = B.banner("Counties", "Every county", f"{B.num(len(counties))} counties and county equivalents. "
                    f"{n_local} have a local research file so far. Click a column to sort.") + f"""
<div class="wrap">
  {B.table([("County", ""), ("State", ""), ("Homeless", "r"), ("Per 10,000", "r"), ("Federal awards FY2021 to FY2025", "r"),
            ("Renters paying half their income", "r"), ("Overdose deaths per 100,000", "r"), ("Local research", "")], rows,
           note="† Most counties share a Continuum of Care, so their count is the county's population share of the CoC count. "
                "Counties that are their own CoC show HUD's published count.")}
</div>"""
    return B.shell("", "Counties", "counties", body, manifest_total=total)


def page_freshness(total):
    p = B.ROOT / "data" / "freshness.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    rows = []
    for dom, d in sorted((data.get("domains") or {}).items()):
        for name, pr in sorted((d.get("probes") or {}).items()):
            fp = pr.get("fingerprint") or {}
            changed = fp.get("last_modified") or fp.get("rowsUpdatedAt") or fp.get("lastEditDate") or fp.get("date") or ""
            rows.append([B.esc(dom), "", dom, f'<a href="{B.esc(pr.get("url") or "")}" rel="noopener">{B.esc(name)}</a>', "", name,
                         B.esc(str(changed)), "", str(changed), B.esc(str(pr.get("accepted_at") or "")), "", str(pr.get("accepted_at") or ""),
                         B.esc(str(d.get("last_ingest") or "")), "", str(d.get("last_ingest") or "")])
    body = B.banner("Method", "Data freshness", f"When each upstream source last changed and when the atlas last took it in. "
                    f"Checked {B.esc(str(data.get('checked_at', '')))}.", small=True) + f"""
<div class="wrap">
  {B.table([("Domain", ""), ("Source", ""), ("Upstream changed", ""), ("Accepted", ""), ("Last ingest", "")], rows,
           note="scripts/refresh.py compares each source's published timestamp or file signature with the last accepted one.")}
</div>"""
    return B.shell("", "Data freshness", "", body, manifest_total=total)


def page_flyers(total, n_counties):
    """/flyers.html: what the street flyer is, how to post it, and where each number on it comes from."""
    ex = "42049" if "42049" in FLYERS else (sorted(FLYERS)[0] if FLYERS else "")
    li = lambda items: "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"
    sources = [
        ["The dollar figure", "Federal homelessness grants performed in the area, latest fiscal year (USASpending.gov). "
         "A county's own research file can put HUD's Continuum of Care award total here instead."],
        ["The counts and chart", "HUD point-in-time counts, one night each January. A research file can add a newer local count."],
        ["The housed-people line", "HUD Housing Inventory Count: permanent supportive, rapid rehousing and other permanent "
         "housing beds, rounded down. Without it, the flyer says that people in housing programs are not in the count."],
        ["The years of pay", "Census ACS five-year median household income for the county. The sources line shows the division."],
        ["The phone number", "The county research file's verified office line, chosen for who answers for the money. "
         "Otherwise the county's U.S. Representative's office when one district holds the county, "
         "or the U.S. House switchboard (202-224-3121, from house.gov) when it is split."],
        ["The shelter line", "The research file's first verified shelter or help line, or 211."],
        ["The QR code", "The county's atlas page: the records behind each number, who answers for them, and ready "
         "public records request letters."],
    ]
    rows = [[f"<b>{a}</b>", "", a, b, "", b] for a, b in sources]
    refs = [
        ('van Zomeren, Postmes and Spears (2008), <i>Psychological Bulletin</i> 134(4). Injustice, efficacy and identity in '
         'collective action.', "https://dare.uva.nl/record/1/298688"),
        ('"Mobilize Is a Verb" (2024), <i>Personality and Social Psychology Bulletin</i>. Verbs and concrete language in '
         'calls to action.', "https://journals.sagepub.com/doi/10.1177/01461672241238418"),
        ("Small, Loewenstein and Slovic (2007). Sympathy and callousness: identifiable and statistical victims.",
         "https://www.andrew.cmu.edu/user/gl20/GeorgeLoewenstein/Papers_files/pdf/SympathyCallousness.pdf"),
        ("Nickerson and Rogers (2010), <i>Psychological Science</i> 21(2). Do you have a voting plan?",
         "https://www.psychologicalscience.org/journals/psychological-science/0956797609359326/"),
        ("Congressional Management Foundation (2017). Citizen-Centric Advocacy.",
         "https://www.congressfoundation.org/revitalizing-congress/communicating-with-congress/citizen-centric-advocacy-2017"),
        ("New York City plain language standard.", "https://blueprint.cityofnewyork.us/content/plain-language-and-readability/"),
    ]
    body = B.banner("Act", "WTF: Where's The Funds?",
                    f"A one-page, black-and-white flyer for {B.num(len(FLYERS))} of {B.num(n_counties)} counties. It puts the "
                    "homelessness money next to what people can see on the street, and gives them one phone number to call. "
                    "Every number on it traces to a public record.", small=True) + f"""
<div class="wrap">
  <h2>Get your county's flyer</h2>
  <p>Open your county from <a href="/counties.html">Counties</a> and choose <b>Street flyer</b>, then Print.
  {f'Example: <a href="/county/{ex}-print.html">{B.esc(flyer_example_name(ex))}</a>.' if ex else ""}
  A county has no flyer until every line on it traces to a record; adding the county's research file fills most gaps.</p>

  <h2>Before you print</h2>
  {li(["Call the phone number yourself. A person or an office should answer.",
       "Call the shelter line. It should work.",
       "Scan the QR code from a printed copy. It should open the county's page.",
       "Check each number against the source named at the bottom of the flyer.",
       "Hand it to someone who knows nothing about this. In ten seconds they should tell you what it asks them to do.",
       "Tape one up, cross the street, and check that you can still read WTF and the dollar figure."])}

  <h2>Printing and posting</h2>
  {li(["<b>Print it letter size, black only.</b> Use Print on the flyer page, or the PDF from <code>scripts/flyer.py</code>.",
       "<b>Laser printer or copy shop.</b> The black bands empty an inkjet fast and smear in rain.",
       "<b>Heavier paper outdoors.</b> Cardstock, with clear packing tape over the face if rain is coming.",
       "<b>Eye level, where people wait.</b> Bus stops, laundromats, community boards. People read while they stand still.",
       "<b>Post where it's allowed.</b> Public boards, and businesses or churches that say yes. A flyer torn down for "
       "breaking a rule helps nobody.",
       "<b>Reprint when the numbers change.</b> New counts come out each spring; the flyer page updates with them."])}

  <h2>Never</h2>
  {li(["Aim it at homeless people. The target is the money and the offices that answer for it.",
       "Add a face or a name. Offices, phone numbers and dollar figures do the work.",
       "Write “stolen,” “fraud” or “corrupt” without a public finding. The flyer asks where the money went.",
       "Print a number you can't trace, or round one up.",
       "Remove the shelter line or the line about people already housed. One helps the reader who needs it most; "
       "the other is the fact the first official on the phone will raise.",
       "Add a second big thing: no extra headline, logo, box or slogan."])}

  <h2>Where each number comes from</h2>
  <p>HUD counts people by Continuum of Care; federal grants are placed by county. A flyer sets them side by side only when
  both cover the same people, to within 2%. When a county shares its Continuum of Care with neighbors, the flyer covers
  that whole area and says so (“Butler and 16 nearby counties”).</p>
  {B.table([("Line", ""), ("Source and rule", "")], rows,
           note="The county file behind each flyer is at /api/flyer/&lt;FIPS&gt;.json, in the flyer kit's format.")}

  <h2>Make or change one yourself</h2>
  <p><code>python3 scripts/flyer.py &lt;FIPS&gt;</code> writes the PDF and PNG from the atlas database, or from the live
  county file when there is no database. Edit the JSON and run <code>python3 scripts/flyer.py --from file.json</code> to
  print a version with a local phone number; the script refuses placeholder text and lines too long to read from the curb.
  To put a local count, the County Executive's line or a shelter number on every copy, add them to the county's
  research file (<a href="https://github.com/0xSero/homeless-atlas/blob/master/.agents/skills/add-county/SKILL.md">how</a>).</p>

  <h2>Why it reads the way it does</h2>
  <p>Short words, one person before any statistic, one action with the exact words to say, and the fact that cuts against
  the argument printed up front. The research behind those rules:</p>
  <ol>{"".join(f'<li>{t} <a href="{u}" rel="noopener">Source</a></li>' for t, u in refs)}</ol>
  <p>The fifth source studies Congress; applying it to county offices is a judgment. Design and wording standard: the
  WTF: Where's The Funds? flyer kit.</p>
</div>"""
    return B.shell("", "WTF street flyers", "", body, manifest_total=total,
                   desc="Printable black-and-white flyers that set each county's federal homelessness money beside its "
                        "homeless count, with one phone number to call. Every number traces to a public record.")


def flyer_example_name(fips):
    r = B.q(B.CONN, "SELECT name, state FROM geo_city WHERE kind='county' AND geoid=?", (fips,))
    return f'{r[0]["name"]}, {r[0]["state"]}' if r else fips


# ------------------------------------------------------------------ entry point
def _bind(build_site_module):
    global B, NA
    B = build_site_module
    NA = B.num(None)
    site_records.B = build_site_module


def build(build_site_module, total):
    """Write every county page. Returns the number of pages written."""
    _bind(build_site_module)
    counties, metrics, peers = load_counties()
    if not counties:
        return 0
    research = research_files()
    n = 0
    B.write("counties.html", page_counties(counties, metrics, research, total)); n += 1
    fresh = page_freshness(total)
    if fresh:
        B.write("freshness.html", fresh); n += 1
    for c in counties:
        m = metrics.get(c["fips"], {})
        d, problems = flyer.flyer_data(B.CONN, c["fips"])
        if d and not problems and not flyer.broken_rules(d):
            FLYERS.add(c["fips"])
            B.write(f"county/{c['fips']}-print.html", flyer.build_html(d, site=True)); n += 1
            B.write(f"api/flyer/{c['fips']}.json", json.dumps(d, indent=1)); n += 1
        B.write(f"county/{c['fips']}.html", page_county(c, m, peers, research, total)); n += 1
        B.write(f"api/county/{c['fips']}.json", json.dumps(RECORDS.pop(c["fips"]), indent=1, default=str)); n += 1
    B.write("flyers.html", page_flyers(total, len(counties))); n += 1
    n += site_records.build(B, counties, STATE_NAMES, total)
    return n


def state_counties(build_site_module, abbr):
    """The county table on a state page, largest count first. Empty string when there is no data."""
    _bind(build_site_module)
    rows = B.q(B.CONN, """SELECT g.geoid fips, g.name,
                            (SELECT value FROM metric m WHERE m.entity_type='county' AND m.entity_id=g.geoid
                               AND m.metric='pit_total' ORDER BY year DESC LIMIT 1) exact,
                            (SELECT value FROM metric m WHERE m.entity_type='county' AND m.entity_id=g.geoid
                               AND m.metric='pit_total_apportioned' ORDER BY year DESC LIMIT 1) appor,
                            (SELECT value FROM metric m WHERE m.entity_type='county' AND m.entity_id=g.geoid
                               AND m.metric='rent_burden_50' ORDER BY year DESC LIMIT 1) rb
                          FROM geo_city g WHERE g.kind='county' AND g.state=? AND length(g.geoid)=5""", (abbr,))
    if not rows:
        return ""
    out = []
    for r in rows:
        v = r["exact"] if r["exact"] is not None else r["appor"]
        out.append([f'<a href="/county/{r["fips"]}.html">{B.esc(r["name"])}</a>', "", r["name"],
                    (B.num(v) + B.est(r["exact"] is None)) if v is not None else NA, "r", v or 0,
                    B.pct(r["rb"]) if r["rb"] is not None else NA, "r", r["rb"] or 0])
    out.sort(key=lambda x: -(x[5] or 0))
    return ("<h2>Counties</h2>" + B.table([("County", ""), ("Homeless", "r"), ("Renters paying half their income", "r")], out,
                                          note="† Population share of the Continuum of Care count; HUD does not count by county."))


def search_records(build_site_module):
    """[type, title, sub, url] rows for the search index."""
    _bind(build_site_module)
    out = []
    for r in B.q(B.CONN, "SELECT geoid, name, state FROM geo_city WHERE kind='county' AND length(geoid)=5"):
        out.append(["county", f'{r["name"]}, {r["state"]}', "County", f'/county/{r["geoid"]}.html'])
    if site_records.available():
        for r in B.q(B.CONN, "SELECT r.jurisdiction st, s.name FROM records_law r LEFT JOIN geo_state s ON s.abbr=r.jurisdiction"):
            label = "Federal FOIA requests" if r["st"] == "US" else f'Public records requests in {r["name"] or r["st"]}'
            out.append(["records", label, "How to ask, deadlines, appeals", f'/records/{r["st"]}.html'])
    return out
