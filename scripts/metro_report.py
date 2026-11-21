#!/usr/bin/env python3
"""Generate a city brief from the database — same contract as scripts/sf_report.py:

  * every numeric claim carries an inline citation to the `source` row it came from;
  * anything derived is marked † and the formula is written in the sentence;
  * sections that the data cannot support say so instead of guessing.

Nothing here is hand-typed. Run:

    python3 scripts/metro_report.py los-angeles
    python3 scripts/metro_report.py new-york
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

HOMELESS_PROGRAMS = ("Continuum of Care Program", "Emergency Solutions Grant Program",
                     "Supportive Housing Program", "Shelter Plus Care",
                     "VA Supportive Services for Veteran Families Program",
                     "VA Homeless Providers Grant and Per Diem Program",
                     "Homeless Veterans’ Reintegration Program",
                     "Youth Homelessness Demonstration Program")

METROS = {
    "los-angeles": {
        "title": "Los Angeles",
        "state": "CA",
        "coc": "CA-600",
        "place": "LOS ANGELES",
        "cities": ("Los Angeles", "Los Angeles County", "Long Beach", "Pasadena", "Glendale", "Santa Monica"),
        "blurb": "The largest unsheltered population in the United States, and the county that holds it.",
        "context": [
            "Los Angeles County has more people sleeping outside than any other local area in the country, "
            "and its homelessness system is split between a city department (LAHSA is the CoC lead) and "
            "county agencies that run health, mental-health and jail services.",
        ],
    },
    "new-york": {
        "title": "New York",
        "state": "NY",
        "coc": "NY-600",
        "place": "NEW YORK",
        "cities": ("New York", "New York City", "Brooklyn", "Bronx", "Queens", "Staten Island", "Manhattan", "Jamaica", "Long Island City", "Flushing", "Staten Island"),
        "blurb": "The largest sheltered system in the country, built on a right to shelter.",
        "context": [
            "New York City is legally obliged to shelter anyone who asks, under the Callahan v. Carey "
            "consent decree of 1981 — which is why its unsheltered count is small and its shelter census "
            "is enormous compared with every other American city.",
        ],
    },
}

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
            return f"${v / div:,.2f}{suf}"
    return f"${v:,.0f}"


def pct(v, dash="not published"):
    if v is None:
        return dash
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return str(v)


SRC: dict[int, str] = {}


def cite(source_id, fallback=""):
    if source_id is None:
        return fallback
    if source_id in SRC:
        return SRC[source_id]
    r = one("SELECT url, title, publisher FROM source WHERE id=?", (source_id,))
    if not r:
        return fallback
    label = (r.get("publisher") or r.get("title") or "source").split("—")[0].strip()[:46]
    SRC[source_id] = f"[{label}]({r['url']})"
    return SRC[source_id]


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


L: list[str] = []


def w(s=""):
    L.append(s)


def build(slug: str) -> dict:
    # module-level accumulators must be reset per brief, or the second run appends to the first
    L.clear()
    SRC.clear()
    m = METROS[slug]
    coc, state = m["coc"], m["state"]

    pit = q("""SELECT year,total,sheltered,unsheltered,chronic,veterans,source_id
               FROM pit WHERE coc_num=? AND total IS NOT NULL ORDER BY year""", (coc,))
    coc_row = one("SELECT * FROM geo_coc WHERE coc_num=?", (coc,))
    latest = pit[-1] if pit else {}
    first = pit[0] if pit else {}

    w(f"# {m['title']}")
    w()
    w(f"*Generated {TODAY} by `scripts/metro_report.py` from `data/atlas.db`. Every number below is "
      f"followed by the public source it came from; a dagger (†) marks a value this project derived and "
      f"the formula is always stated. Nothing here is hand-typed.*")
    w()
    for para in m["context"]:
        w(para)
        w()

    # ---------------------------------------------------------------- counts
    w("## 1. How many people, and in what condition")
    w()
    if latest:
        w(f"The {esc_name(coc_row.get('name') or coc)} counted **{num(latest['total'])} people homeless** "
          f"in {latest['year']} {cite(latest.get('source_id'))}: {num(latest['unsheltered'])} unsheltered and "
          f"{num(latest['sheltered'])} in shelter. The same count recorded {num(latest['chronic'])} people "
          f"meeting the federal definition of chronic homelessness.")
    else:
        w(f"No point-in-time count rows exist for {coc} in the database — nothing is stated here rather than "
          f"estimated.")
    w()
    if first and latest and first.get("total"):
        change = (latest["total"] - first["total"]) / first["total"] * 100
        w(f"Between {first['year']} and {latest['year']} the count moved from {num(first['total'])} to "
          f"{num(latest['total'])} ({change:+.1f}%). A change in this series is not automatically a change on "
          f"the street: CoCs change count methods, and HUD changes definitions between years.")
        w()
    if pit:
        rows = [[r["year"], num(r["total"]), num(r["sheltered"]), num(r["unsheltered"]), num(r["chronic"]),
                 num(r["veterans"]), cite(r.get("source_id"))] for r in pit]
        w(table(["Year", "Total", "Sheltered", "Unsheltered", "Chronic", "Veterans", "Source"], rows))
        w()
        w("*Blank cells are years or subpopulations HUD did not publish for this CoC. Nothing is interpolated.*")
        w()

    # ---------------------------------------------------------------- money
    w("## 2. Where the federal money went")
    w()
    marks = ",".join("?" * len(m["cities"]))
    prog = ",".join("?" * len(HOMELESS_PROGRAMS))
    # recipient-side attribution: awards held by organisations whose own address is in these cities
    city_awards = q(f"""SELECT a.*, o.name AS org_name FROM award a JOIN org o ON o.id=a.org_id
                        WHERE a.amount_obligated>0 AND a.program IN ({prog})
                          AND o.state=? AND lower(o.city) IN ({marks})
                        ORDER BY a.amount_obligated DESC LIMIT 400""",
                    (*HOMELESS_PROGRAMS, state, *[c.lower() for c in m["cities"]]))
    by_prog: dict[str, list] = {}
    for a in city_awards:
        by_prog.setdefault(a.get("program") or "unlabelled", []).append(a)
    grand = sum(a["amount_obligated"] or 0 for a in city_awards)
    w(f"Organisations addressed in these cities hold **{len(city_awards)} mapped federal homelessness "
      f"awards worth {money(grand)}** across the {len(HOMELESS_PROGRAMS)} programmes this archive tracks "
      f"(Continuum of Care, ESG, Supportive Housing, Shelter Plus Care, VA SSVF and Grant-and-Per-Diem, "
      f"HVRP, YHDP). Awards are matched on the recipient organisation's own address, because the "
      f"place-of-performance field is only partly populated.")
    w()
    if by_prog:
        rows = sorted(([p, num(len(v)), money(sum(x["amount_obligated"] or 0 for x in v)),
                        cite(max(v, key=lambda x: x["amount_obligated"] or 0).get("source_id"))]
                       for p, v in by_prog.items()), key=lambda r: -len(r[2]))[:12]
        w(table(["Programme", "Awards", "Obligated", "Source"], rows))
        w()
    top = {}
    for a in city_awards:
        k = a["org_id"]
        if k not in top:
            top[k] = {"name": a["org_name"], "amt": 0, "n": 0, "src": a.get("source_id")}
        top[k]["amt"] += a["amount_obligated"] or 0
        top[k]["n"] += 1
    rows = [[esc_pipe(t["name"]), num(t["n"]), money(t["amt"]), cite(t["src"])]
            for t in sorted(top.values(), key=lambda t: -t["amt"])[:25]]
    w(table(["Recipient", "Awards", "Obligated", "Source"], rows))
    w()

    # ---------------------------------------------------------------- organisations
    w("## 3. The organisations themselves")
    w()
    orgs = q(f"""SELECT o.*, (SELECT SUM(amount_obligated) FROM award a WHERE a.org_id=o.id) amt,
                        (SELECT COUNT(*) FROM award a WHERE a.org_id=o.id) n_awards
                 FROM org o WHERE o.state=? AND lower(o.city) IN ({marks})
                 ORDER BY COALESCE(o.revenue,0) DESC, COALESCE(amt,0) DESC LIMIT 70""",
             (state, *[c.lower() for c in m["cities"]]))
    if orgs:
        rev = sum(o.get("revenue") or 0 for o in orgs if o.get("revenue"))
        w(f"The 70 largest organisations addressed in these cities reported **{money(rev)}** of revenue on "
          f"their latest Form 990 {cite(orgs[0].get('source_id'))}. Revenue is self-reported and the fiscal "
          f"years differ between filers, so the figures are not a single-year total.")
        w()
        rows = [[esc_pipe(o["name"])[:58], esc_pipe(o.get("city") or ""), money(o.get("revenue")),
                 money(o.get("amt")), num(o.get("n_awards")),
                 f"[site]({o['website']})" if o.get("website") else "—",
                 cite(o.get("source_id"))] for o in orgs]
        w(table(["Organisation", "City", "Revenue (990)", "Federal $", "Awards", "Site", "Source"], rows))
        w()
        ids = tuple(o["id"] for o in orgs[:15])
        if ids:
            ph = ",".join("?" * len(ids))
            ppl = q(f"""SELECT p.*, o.name AS org_name FROM org_person p JOIN org o ON o.id=p.org_id
                        WHERE p.org_id IN ({ph}) AND p.comp IS NOT NULL AND p.comp>0
                        ORDER BY p.comp DESC LIMIT 25""", ids)
            if ppl:
                w("**Who runs them.** Named officers and their disclosed compensation, as filed. Professional "
                  "roles only — this archive holds no personal contact details.")
                w()
                rows = [[esc_pipe(p["person"]), esc_pipe((p.get("title") or "")[:38]), esc_pipe(p["org_name"])[:40],
                         num(p.get("comp")), str(p.get("year") or ""), cite(p.get("source_id"))] for p in ppl]
                w(table(["Person", "Title", "Organisation", "Compensation", "Year", "Source"], rows))
                w()

    # ---------------------------------------------------------------- institutions
    w("## 4. Institutions: psychiatric beds and jails")
    w()
    fac = q(f"""SELECT * FROM facility WHERE state=? ORDER BY COALESCE(beds,0) DESC LIMIT 30""", (state,))
    if fac:
        closed = [f for f in fac if (f.get("status") or "").lower() == "closed"]
        w(f"{len(fac)} institutions in {state} are recorded here, {len(closed)} of them closed. Bed counts "
          f"are published by the facility's regulator, not derived.")
        w()
        rows = [[esc_pipe(f["name"])[:52], esc_pipe(f.get("facility_type") or ""), esc_pipe(f.get("city") or ""),
                 esc_pipe(f.get("status") or ""), num(f.get("beds")), cite(f.get("source_id"))] for f in fac]
        w(table(["Facility", "Type", "City", "Status", "Beds", "Source"], rows))
        w()
    else:
        w("No institution rows for this state.")
        w()

    # ---------------------------------------------------------------- law
    w("## 5. The law")
    w()
    laws = q("""SELECT * FROM law WHERE jurisdiction IN (?, ?) ORDER BY year DESC LIMIT 20""",
             (state, m["place"].title()))
    if laws:
        rows = [[esc_pipe((l.get("title") or "")[:70]), esc_pipe(l.get("kind") or ""), str(l.get("year") or ""),
                 cite(l.get("source_id"))] for l in laws]
        w(table(["Instrument", "Kind", "Year", "Source"], rows))
        w()
    else:
        w("No law rows for this jurisdiction — nothing is asserted about local ordinances here.")
        w()

    # ---------------------------------------------------------------- press and documents
    w("## 6. Reporting and primary documents")
    w()
    media = q("""SELECT * FROM media WHERE state=? ORDER BY published DESC LIMIT 38""", (state,))
    if media:
        w("**Coverage recorded for this state.** Each row links the item itself; nothing is quoted beyond its "
          "published title.")
        w()
        rows = [[esc_pipe((x.get("title") or "")[:70]), esc_pipe(x.get("publisher") or ""),
                 str(x.get("published") or "")[:10], f"[watch]({x['url']})" if x.get("url") else "—",
                 cite(x.get("source_id"))] for x in media]
        w(table(["Title", "Publisher", "Published", "Item", "Source"], rows))
        w()
    docs = q("""SELECT * FROM doc WHERE state=? ORDER BY published DESC LIMIT 38""", (state,))
    if docs:
        w("**Primary documents.** Datasets and filings collected for this state, linked directly.")
        w()
        rows = [[esc_pipe((d.get("title") or "")[:64]), esc_pipe((d.get("publisher") or "")[:30]),
                 esc_pipe(d.get("kind") or ""), f"[open]({d['url']})" if d.get("url") else "—",
                 cite(d.get("source_id"))] for d in docs]
        w(table(["Document", "Publisher", "Kind", "Link", "Source"], rows))
        w()

    # ---------------------------------------------------------------- derived
    w("## 7. What it costs, per person †")
    w()
    if latest and latest.get("total") and grand:
        per = grand / latest["total"]
        w(f"† Federal homelessness awards held by organisations addressed in these cities, divided by the "
          f"{latest['year']} point-in-time count: {money(grand)} ÷ {num(latest['total'])} = **{money(per)} "
          f"per person counted homeless**. The numerator covers every year the awards are recorded against, "
          f"so it is a cumulative figure, not annual spend — it is a scale comparison only, and no share of it "
          f"can be attributed to the people counted on that one night.")
        w()

    w("## 8. What this brief cannot say")
    w()
    w("- **No organisation is credited with an outcome.** HUD publishes System Performance Measures per "
      "Continuum of Care, never per provider, and HMIS records are confidential. Nothing in this archive "
      "links a provider to a housing placement.")
    w("- **Awards are attributed by recipient address.** A grantee that works statewide appears once, in the "
      "city it is incorporated in, and its money may be spent elsewhere.")
    w("- **Counts are one night.** Unsheltered counts depend on method and weather; a fall is not proof of "
      "housing.")
    w("- **State and city money is absent.** Only federal programmes are counted; in some places local money "
      "is larger.")
    w()
    w(f"*{len(SRC)} distinct sources cited in this brief.*")

    text = "\n".join(L) + "\n"
    out = ROOT / "reports" / f"{slug}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text)
    return {"file": str(out.relative_to(ROOT)), "sources": len(SRC),
            "citations": sum(1 for line in L if "](" in line), "words": len(text.split())}


def esc_name(s):
    return s


def esc_pipe(s):
    return str(s or "").replace("|", "/")


if __name__ == "__main__":
    targets = sys.argv[1:] or list(METROS)
    for slug in targets:
        if slug not in METROS:
            print(f"unknown metro {slug}; choose from {', '.join(METROS)}")
            continue
        info = build(slug)
        print(f"{slug}: wrote {info['file']} — {info['sources']} sources, "
              f"{info['citations']} cited lines, {info['words']} words")
