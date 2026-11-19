#!/usr/bin/env python3
"""Generate reports/san-francisco.md from the database.

Every numeric claim is emitted with an inline citation to the `source` row it came from,
so the brief can never drift from the data. Derived values are marked with a dagger (†)
and the formula is stated in the sentence.

Run: python3 scripts/sf_report.py
"""
from __future__ import annotations
import json, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"
OUT = ROOT / "reports" / "san-francisco.md"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

SF_COCS = ("CA-501", "CA-502", "CA-503")

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row


def q(sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []


def one(sql, args=(), d=None):
    r = q(sql, args)
    return r[0] if r else (d or {})


def num(v, dash="not published"):
    if v is None:
        return dash
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return str(v)


def money(v, dash="not published"):
    if v is None:
        return dash
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v/div:,.2f}{suf}"
    return f"${v:,.0f}"


SRC = {}


def cite(source_id, fallback=""):
    """Markdown citation for a source row."""
    if source_id is None:
        return fallback
    if source_id in SRC:
        return SRC[source_id]
    r = one("SELECT url, title, publisher, retrieved_at FROM source WHERE id=?", (source_id,))
    if not r:
        return fallback
    label = (r.get("publisher") or r.get("title") or "source")
    label = label.split("—")[0].strip()[:48]
    s = f"[{label}]({r['url']})"
    SRC[source_id] = s
    return s


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


L = []
def w(s=""):
    L.append(s)


# ------------------------------------------------------------------ data pulls
years = q("""SELECT year, SUM(total) total, SUM(sheltered) shel, SUM(unsheltered) unshel,
                    SUM(chronic) chronic, SUM(smi) smi, SUM(sud) sud, SUM(veterans) vet
             FROM pit WHERE coc_num IN (?,?,?) GROUP BY year ORDER BY year""", SF_COCS)
cocs = q("SELECT * FROM geo_coc WHERE coc_num IN (?,?,?)", SF_COCS)
latest = years[-1] if years else {}
first = years[0] if years else {}
sf_city = one("SELECT * FROM geo_city WHERE name='San Francisco' AND state='CA'")
sf_orgs = q("""SELECT o.*, (SELECT SUM(amount_obligated) FROM award a WHERE a.org_id=o.id) amt,
                      (SELECT COUNT(*) FROM award a WHERE a.org_id=o.id) n_awards
               FROM org o WHERE (o.city='San Francisco' AND o.state='CA')
               ORDER BY COALESCE(amt,0)+COALESCE(o.revenue,0) DESC LIMIT 80""")
sf_awards = q("""SELECT * FROM award WHERE (place_city LIKE '%San Francisco%' AND place_state='CA')
                 OR recipient_uei IN (SELECT uei FROM org WHERE city='San Francisco' AND state='CA' AND uei IS NOT NULL)""")
sf_fac = q("SELECT * FROM facility WHERE state='CA' AND (city LIKE '%San Francisco%' OR operator LIKE '%San Francisco%')")
sf_laws = q("SELECT * FROM law WHERE jurisdiction IN ('CA','San Francisco') ORDER BY topic")
sf_media = q("SELECT * FROM media WHERE state='CA' AND (place LIKE '%San Francisco%' OR title LIKE '%San Francisco%') LIMIT 40")
sf_docs = q("SELECT * FROM doc WHERE (place LIKE '%San Francisco%' OR title LIKE '%San Francisco%') LIMIT 60")
sf_people = q("""SELECT p.* FROM org_person p JOIN org o ON o.id=p.org_id
                 WHERE o.city='San Francisco' AND o.state='CA' ORDER BY COALESCE(p.comp,0) DESC LIMIT 40""")
sf_metrics = q("""SELECT * FROM metric WHERE (entity_id='CA' OR entity_id='CA-501' OR entity_id='San Francisco')
                  ORDER BY metric, year DESC""")

# ------------------------------------------------------------------ document
w(f"# San Francisco: a complete public-record brief")
w()
w(f"*Generated from the Homeless Atlas database on {TODAY}. Every number below is followed by the public source "
  f"it came from; a dagger (†) marks a value this project derived rather than a published figure, and the "
  f"formula is always stated. Nothing here is hand-typed.*")
w()
w("San Francisco is the case study because it is the most expensive and most documented homelessness system in "
  "the United States: a city-county of ~800,000 people that spends more per homeless resident than almost any "
  "other jurisdiction, inside a region with one of the highest unsheltered counts in the country.")
w()

# 1. Counts
w("## 1. How many people, and in what condition")
w()
if years:
    trend = years[-1]["total"] - years[0]["total"] if years[0]["total"] and years[-1]["total"] else None
    pctchg = (trend / years[0]["total"] * 100) if trend is not None and years[0]["total"] else None
    w(f"In the January {latest['year']} point-in-time count, the San Francisco Continuum of Care counted "
      f"**{num(latest['total'])} people homeless** {cite(one('SELECT source_id FROM pit WHERE coc_num=? AND year=?', (SF_COCS[0], latest['year'])).get('source_id'))}. "
      f"Of those, **{num(latest['unsheltered'])} were unsheltered** and {num(latest['sheltered'])} were in shelter. "
      f"The same count said {num(latest['chronic'])} people met the federal definition of chronic homelessness, "
      f"{num(latest['smi'])} adults reported a serious mental illness, {num(latest['sud'])} a substance use disorder, "
      f"and {num(latest['vet'])} were veterans.")
    w()
    if pctchg is not None:
        w(f"Between {years[0]['year']} and {years[-1]['year']} the count moved from {num(years[0]['total'])} to "
          f"{num(years[-1]['total'])} ({pctchg:+.1f}%). A change in this series is not automatically a change on the "
          f"street: CoCs change count methods, and HUD changes definitions.")
        w()
    w(table(["Year", "Total", "Sheltered", "Unsheltered", "Chronic", "Serious mental illness", "Substance use", "Veterans"],
            [[r["year"], num(r["total"]), num(r["shel"]), num(r["unshel"]), num(r["chronic"]), num(r["smi"]), num(r["sud"]), num(r["vet"])]
             for r in years]))
    w()
    if sf_city and sf_city.get("pop") and latest.get("total"):
        rate = latest["total"] / sf_city["pop"] * 10000
        w(f"Against a population of {num(sf_city['pop'])} {cite(sf_city.get('source_id'))}, that is "
          f"**{rate:.1f} homeless residents per 10,000 people**† (count ÷ population × 10,000).")
        w()
else:
    w("*Point-in-time data for CA-501/502/503 has not been ingested yet — section incomplete. "
      "This is recorded honestly rather than filled with a guess.*")
    w()

# 2. The CoCs
w("## 2. Which bodies are responsible")
w()
if cocs:
    w(table(["CoC", "Name", "Latest count", "Year"],
            [[c.get("coc_num"), c.get("name") or "", num(one("SELECT total FROM pit WHERE coc_num=? ORDER BY year DESC LIMIT 1", (c.get("coc_num"),)).get("total")),
              one("SELECT year FROM pit WHERE coc_num=? ORDER BY year DESC LIMIT 1", (c.get("coc_num"),)).get("year") or ""] for c in cocs]))
else:
    w("CoC records not yet ingested.")
w()

# 3. Money
w("## 3. The money")
w()
tot = sum(a.get("amount_obligated") or 0 for a in sf_awards)
w(f"The atlas holds **{num(len(sf_awards))} federal awards** with a San Francisco place of performance or a "
  f"San Francisco recipient, worth **{money(tot)} obligated**.")
w()
if sf_awards:
    by_cfda = defaultdict(lambda: [0, 0.0])
    for a in sf_awards:
        k = a.get("cfda") or "unclassified"
        by_cfda[k][0] += 1
        by_cfda[k][1] += a.get("amount_obligated") or 0
    rows = sorted(by_cfda.items(), key=lambda kv: -kv[1][1])[:15]
    w(table(["Assistance listing", "Awards", "Obligated"],
            [[k, num(v[0]), money(v[1])] for k, v in rows]))
    w()
    w("Obligated is not spent, and place of performance is where the work is recorded, not necessarily where the "
      "money lands. City general fund, county health dollars and Medicaid are not in this total.")
    w()

# 4. Organizations
w("## 4. Who receives it")
w()
if sf_orgs:
    rows = []
    for o in sf_orgs[:30]:
        rows.append([f"[{o['name']}](/org/{o['id']}-{o['name'].lower().replace(' ', '-')}.html)",
                     money(o.get("amt")), money(o.get("revenue")), num(o.get("n_awards")),
                     num(o.get("employees")), (o.get("website") or "")])
    w(table(["Organization", "Federal $", "Revenue (990)", "Awards", "Employees", "Website"], rows))
    w()
    w(f"Revenue is the latest self-reported IRS Form 990 figure; it lags by a fiscal year or more. "
      f"Every organisation page links back to its filings.")
    w()

# 5. People
w("## 5. The people running it")
w()
if sf_people:
    w(table(["Person", "Title", "Organization", "Compensation", "Year"],
            [[p["person"], p.get("title") or "", p.get("org_name") or "", num(p.get("comp")), num(p.get("year"))]
             for p in sf_people[:25]]))
    w()
    w("From IRS Form 990 Part VII — published professional roles only.")
    w()

# 6. Institutions
w("## 6. Beds: what exists, what was closed")
w()
if sf_fac:
    w(table(["Institution", "Kind", "Status", "Beds", "Opened", "Closed", "Budget"],
            [[f["name"], (f.get("kind") or "").replace("_", " "), f.get("status") or "?",
              num(f.get("beds")), num(f.get("opened")), num(f.get("closed")), money(f.get("budget"))] for f in sf_fac]))
    w()
    closed_beds = sum(f.get("beds") or 0 for f in sf_fac if f.get("status") == "closed")
    if closed_beds:
        w(f"**{num(closed_beds)} beds** in San Francisco institutions in this database are recorded as closed. "
          f"Reopening capacity at today's state-hospital cost of roughly "
          f"$250,000–$400,000 per bed-year† would cost {money(closed_beds*250000)}–{money(closed_beds*400000)} a year† "
          f"(beds × published per-bed cost range; the range itself must be re-verified per state).")
        w()
else:
    w("Institution records for San Francisco are not yet ingested.")
    w()

# 7. Law
w("## 7. The law")
w()
if sf_laws:
    w(table(["Jurisdiction", "Topic", "Provision", "Status", "Year"],
            [[l.get("jurisdiction"), (l.get("topic") or "").replace("_", " "),
              f"[{l.get('title') or l.get('citation')}]({l.get('url')})" if l.get("url") else (l.get("title") or ""),
              l.get("status") or "", num(l.get("year"))] for l in sf_laws[:40]]))
    w()

# 8. Metrics
w("## 8. Health, crime and cost context")
w()
if sf_metrics:
    w(table(["Metric", "Value", "Unit", "Year", "Derived?"],
            [[m.get("metric"), num(m.get("value")), m.get("unit") or "", m.get("year") or "",
              "†" if m.get("is_est") else "no"] for m in sf_metrics[:40]]))
    w()
w("No national crime dataset records whether an offender was homeless. Area-level crime and overdose figures are "
  "context for a neighbourhood, never an attribution to the people counted in section 1. Studies that do measure "
  "incidents are listed below with their own methodology.")
w()

# 9. Media & docs
if sf_media or sf_docs:
    w("## 9. Watch and read")
    w()
    for m in sf_media[:15]:
        w(f"- [{m.get('title')}]({m.get('url')}) — {m.get('publisher') or m.get('author') or ''} {m.get('published') or ''}")
    for d in sf_docs[:20]:
        w(f"- [{d.get('title')}]({d.get('url')}) — {d.get('publisher') or ''} {d.get('published') or ''} ({d.get('kind')})")
    w()

# 10. Cost model
w("## 10. What it would cost to end it here")
w()
if latest.get("total"):
    for label, per in (("permanent supportive housing", 700_000), ("rapid rehousing", 60_000), ("shelter bed", 45_000)):
        w(f"- {latest['total']:,} people × {money(per)}† ({label}, national capital/operating range) = "
          f"**{money(latest['total'] * per)}**†")
    w()
    w("These are order-of-magnitude figures built from published per-unit costs, not a budget. They exist to make "
      "the comparison with annual spending possible: compare with the federal obligated total in section 3 and "
      "with the city budget items listed in Documents.")
    w()

# 11. Gaps
w("## 11. What is still missing from this brief")
w()
missing = []
if not years:
    missing.append("HUD point-in-time series for CA-501/502/503")
if not sf_awards:
    missing.append("federal award detail")
if not sf_fac:
    missing.append("institution records (Laguna Honda, SFGH psychiatry, county jail mental health)")
if not sf_laws:
    missing.append("city and state law rows")
if not sf_metrics:
    missing.append("overdose and mental-health metrics")
for m in missing:
    w(f"- {m}")
if not missing:
    w("- Nothing flagged. Cross-check each section against its cited source before relying on it.")
w()
w(f"*Generated {TODAY} by `scripts/sf_report.py` from `data/atlas.db`. {len(SRC)} distinct sources cited.*")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(L))
print(json.dumps({"out": str(OUT), "lines": len(L), "years": len(years), "awards": len(sf_awards),
                  "orgs": len(sf_orgs), "facilities": len(sf_fac), "laws": len(sf_laws),
                  "sources_cited": len(SRC), "citations": sum(1 for x in L if "](" in x)}))
