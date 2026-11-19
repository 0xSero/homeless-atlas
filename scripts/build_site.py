#!/usr/bin/env python3
"""Generate the Homeless Atlas static site from data/atlas.db -> site/.

No build step, no server, no framework. Every page is self-contained HTML that links
/assets/atlas.css + /assets/app.js. Maps use MapLibre GL JS from a CDN with a keyless
CARTO basemap. Search is a sharded JSON index read by the client.

Run: python3 scripts/build_site.py [--fast]
"""
from __future__ import annotations
import hashlib, json, math, os, re, shutil, sqlite3, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"
SITE = ROOT / "site"
SRC = ROOT / "site_src"
GEO = ROOT / "data" / "geo"
FAST = "--fast" in sys.argv
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d")

CITY_PAGE_MIN_POP = 25_000
ORG_PAGE_MAX = 30_000
PERSON_PAGE_MAX = 25_000

# ---------------------------------------------------------------- utilities
def esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def num(v, dash="—"):
    if v is None:
        return dash
    try:
        v = float(v)
    except (TypeError, ValueError):
        return esc(v)
    return f"{int(round(v)):,}" if abs(v) >= 1 else f"{v:,.2f}"

def money(v, dash="—"):
    if v is None:
        return dash
    try:
        v = float(v)
    except (TypeError, ValueError):
        return esc(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v/div:,.2f}{suf}"
    return f"${v:,.0f}"

def rate(v, d=1, dash="—"):
    if v is None:
        return dash
    try:
        return f"{float(v):.{d}f}"
    except (TypeError, ValueError):
        return esc(v)


def pct(v, d=1, dash="—"):
    if v is None:
        return dash
    try:
        return f"{float(v):.{d}f}%"
    except (TypeError, ValueError):
        return esc(v)

def slug(s, maxlen=70):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:maxlen] or "x").rstrip("-")

def est(dag=False):
    return '<span class="dag" title="Derived or modelled value, not a published figure.">†</span>' if dag else ""

def q(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error as e:
        if os.environ.get("ATLAS_DEBUG"):
            print("SQL FAIL:", e, "\n ", sql[:200], file=sys.stderr)
        return []

def one(conn, sql, args=(), default=None):
    r = q(conn, sql, args)
    return r[0] if r else default

NAV = [
    ("/index.html", "Overview"), ("/map.html", "Map"), ("/search.html", "Search"),
    ("/states.html", "States"), ("/cities.html", "Cities"), ("/orgs.html", "Organizations"),
    ("/people.html", "People"), ("/facilities.html", "Institutions"), ("/budgets.html", "Money"),
    ("/laws.html", "Laws"), ("/media.html", "Media"), ("/docs.html", "Docs"),
]

# ---------------------------------------------------------------- shell
def shell(rel: str, title: str, section: str, body: str, head: str = "", desc: str = "",
          manifest_total: int = 0):
    def navlink(u, n):
        on = " class='on'" if (section and u.endswith(section + '.html')) else ""
        return f'<a href="{rel}{u.lstrip("/")}"{on}>{n}</a>'
    nav = "".join(navlink(u, n) for u, n in NAV)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Homeless Atlas</title>
<meta name="description" content="{esc(desc or 'Public-record atlas of homelessness in the United States: counts, contracts, organizations, institutions, laws, money.')}">
<meta property="og:title" content="{esc(title)} · Homeless Atlas">
<meta property="og:type" content="website">
<link rel="stylesheet" href="{rel}assets/atlas.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect rx='22' width='100' height='100' fill='%230b0e13'/><circle cx='50' cy='50' r='24' fill='%23ff7a45'/></svg>">
{head}
</head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="top"><div class="wrap">
  <div class="topbar">
    <a class="brand" href="{rel}index.html"><span class="dot"></span>Homeless&nbsp;Atlas</a>
    <form class="searchbox" role="search" onsubmit="return goSearch(event)">
      <span class="ico">⌕</span>
      <input type="search" id="gq" placeholder="Search 100,000+ records — city, CoC, org, person, law…" aria-label="Search" autocomplete="off">
      <div class="ac" role="listbox"></div>
    </form>
    <button class="navbtn" aria-label="Menu">☰ Menu</button>
  </div>
  <nav class="main">{nav}
    <a href="{rel}about.html">About</a>
    <a href="{rel}method.html">Method</a>
    <a href="{rel}sources.html">Sources</a>
  </nav>
</div></header>
<main id="main">{body}</main>
<footer class="site"><div class="wrap">
  <div class="row">
    <div><strong>Homeless Atlas</strong> · public-record research · generated {NOW} · {num(manifest_total)} indexed records</div>
    <div><a href="{rel}method.html">Method &amp; caveats</a> · <a href="{rel}sources.html">Sources &amp; licences</a> · <a href="{rel}about.html">About</a></div>
  </div>
  <p class="dim" style="margin-top:14px">Every figure links to a public source. Anything marked † is derived, not published.
  Crime and health figures are area-level context — no dataset attributes an offence to a homeless person, and we never claim otherwise.</p>
</div></footer>
<script src="{rel}assets/app.js"></script>
<script>
function goSearch(e){{e.preventDefault();var v=document.getElementById('gq').value.trim();if(v)location.href='{rel}search.html?q='+encodeURIComponent(v);return false;}}
</script>
{head and ''}
</body></html>"""

def banner(kicker, h1, sub, rel="", small=False, crumbs=""):
    return f"""<section class="banner{' small' if small else ''}"><div class="wrap"><div class="inner">
  {f'<div class="crumbs">{crumbs}</div>' if crumbs else ''}
  <div class="kicker">{esc(kicker)}</div>
  <h1>{h1}</h1>
  {f'<p class="sub">{sub}</p>' if sub else ''}
</div></div></section>"""

def write(path: str, html: str):
    p = SITE / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")

def kpi(v, label, note="", cls=""):
    return f'<div class="kpi {cls}"><div class="v">{v}</div><div class="l">{esc(label)}</div>' + (f'<div class="n">{note}</div>' if note else "") + "</div>"

def _cells(row):
    """Accept either a list of (value, class, sortvalue) triples or a flat list of them."""
    if row and isinstance(row[0], (list, tuple)):
        return list(row)
    return [tuple(row[i:i + 3]) for i in range(0, len(row), 3)]


def table(headers, rows, cls="sortable", note=""):
    th = "".join(f'<th class="{c}">{h}</th>' for h, c in headers)
    tr = "".join("<tr>" + "".join(f'<td class="{c}" data-v="{esc(dv if dv is not None else v)}">{v}</td>'
                                   for v, c, dv in _cells(row)) + "</tr>" for row in rows)
    return (f'<div class="tablewrap"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'
            + (f'<p class="dim" style="font-size:.78rem">{note}</p>' if note else ""))

# ---------------------------------------------------------------- minimal markdown
def md(text: str) -> str:
    lines, out, in_list, in_tbl, in_code = text.split("\n"), [], False, False, False
    def inline(s):
        s = esc(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" rel="noopener">\1</a>', s)
        s = re.sub(r"(?<!\()(https?://[^\s<)]+)", r'<a href="\1" rel="noopener">\1</a>', s)
        return s
    for ln in lines:
        if ln.strip().startswith("```"):
            out.append("<pre><code>" if not in_code else "</code></pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(esc(ln))
            continue
        l = ln.rstrip()
        if re.match(r"^\s*\|.*\|\s*$", l):
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if re.match(r"^[\s|:\-]+$", l):
                continue
            if not in_tbl:
                out.append("<div class='tablewrap'><table class='sortable'><thead><tr>" +
                           "".join(f"<th>{inline(c)}</th>" for c in cells) + "</tr></thead><tbody>")
                in_tbl = True
            else:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        elif in_tbl:
            out.append("</tbody></table></div>")
            in_tbl = False
        m = re.match(r"^(#{1,6})\s+(.*)$", l)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
            continue
        if re.match(r"^\s*[-*+]\s+", l) or re.match(r"^\s*\d+\.\s+", l):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item_txt = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", l)
            out.append("<li>" + inline(item_txt) + "</li>")
            continue
        elif in_list:
            out.append("</ul>")
            in_list = False
        if l.startswith(">"):
            out.append(f"<blockquote>{inline(l.lstrip('> '))}</blockquote>")
        elif l.strip() in ("---", "***"):
            out.append("<hr>")
        elif l.strip():
            out.append(f"<p>{inline(l)}</p>")
    if in_list:
        out.append("</ul>")
    if in_tbl:
        out.append("</tbody></table></div>")
    return "\n".join(out)

# ---------------------------------------------------------------- data load
def load(conn):
    d = {}
    d["meta"] = {"generated": NOW,
                 "counts": {t: (one(conn, f"SELECT COUNT(*) c FROM {t}") or {}).get("c", 0)
                            for t in ("org", "org_person", "award", "pit", "geo_city", "geo_coc",
                                      "geo_state", "facility", "law", "media", "doc", "metric", "source")}}
    d["years"] = [r["year"] for r in q(conn, "SELECT DISTINCT year FROM pit WHERE total IS NOT NULL ORDER BY year")]
    if d["years"]:
        y = d["years"][-1]
        d["latest"] = y
        d["nat"] = one(conn, """SELECT SUM(total) total, SUM(sheltered) shel, SUM(unsheltered) unshel,
                                SUM(chronic) chronic, SUM(smi) smi, SUM(sud) sud, SUM(veterans) vet,
                                SUM(youth) youth, COUNT(DISTINCT coc_num) cocs
                                FROM pit WHERE year=?""", (y,), {}) or {}
        d["nat_prev"] = one(conn, "SELECT SUM(total) total FROM pit WHERE year=?", (y - 1,), {}) or {}
        d["nat_series"] = q(conn, """SELECT year, SUM(total) total, SUM(sheltered) shel, SUM(unsheltered) unshel
                                     FROM pit WHERE total IS NOT NULL GROUP BY year ORDER BY year""")
    else:
        d["latest"], d["nat"], d["nat_prev"], d["nat_series"] = None, {}, {}, []

    # state rows
    st = {r["abbr"]: r for r in q(conn, "SELECT * FROM geo_state")}
    pit_by_state = q(conn, """SELECT c.state abbr, p.year, SUM(p.total) total, SUM(p.sheltered) shel,
                              SUM(p.unsheltered) unshel, SUM(p.chronic) chronic, COUNT(DISTINCT p.coc_num) cocs
                              FROM pit p JOIN geo_coc c ON c.coc_num=p.coc_num
                              WHERE c.state IS NOT NULL GROUP BY c.state, p.year""")
    latest_by_state = {}
    if d["latest"]:
        for r in pit_by_state:
            if r["year"] == d["latest"]:
                latest_by_state[r["abbr"]] = r
    mon = {r["place_state"]: r for r in q(conn, """SELECT place_state, SUM(amount_obligated) amt, COUNT(*) n
                                                  FROM award WHERE place_state IS NOT NULL GROUP BY place_state""")}
    orgs_by_state = {r["state"]: r for r in q(conn, "SELECT state, COUNT(*) n, SUM(revenue) rev FROM org WHERE state IS NOT NULL GROUP BY state")}
    d["states"] = []
    for abbr, row in st.items():
        p = latest_by_state.get(abbr, {})
        pop = row.get("pop")
        total = p.get("total")
        d["states"].append({
            "abbr": abbr, "name": row.get("name") or abbr, "pop": pop, "lat": row.get("lat"), "lon": row.get("lon"),
            "pit": total, "shel": p.get("shel"), "unshel": p.get("unshel"), "chronic": p.get("chronic"),
            "cocs": p.get("cocs"), "rate": (total / pop * 10000) if (total and pop) else None,
            "unshel_share": (p.get("unshel") / total * 100) if (total and p.get("unshel") is not None) else None,
            "award": (mon.get(abbr) or {}).get("amt"), "awards": (mon.get(abbr) or {}).get("n"),
            "orgs": (orgs_by_state.get(abbr) or {}).get("n"), "revenue": (orgs_by_state.get(abbr) or {}).get("rev"),
        })
    d["states"].sort(key=lambda r: (-(r["pit"] or 0), r["name"]))

    d["cocs"] = q(conn, """SELECT c.*, (SELECT total FROM pit p WHERE p.coc_num=c.coc_num AND p.total IS NOT NULL
                            ORDER BY year DESC LIMIT 1) pit_total,
                           (SELECT year FROM pit p WHERE p.coc_num=c.coc_num AND p.total IS NOT NULL
                            ORDER BY year DESC LIMIT 1) pit_year,
                           (SELECT total FROM pit p WHERE p.coc_num=c.coc_num ORDER BY year ASC LIMIT 1) pit_first
                           FROM geo_coc c ORDER BY pit_total DESC NULLS LAST""")
    d["award_by_cfda"] = q(conn, """SELECT COALESCE(cfda,'(none)') cfda, program, COUNT(*) n, SUM(amount_obligated) amt
                                    FROM award GROUP BY cfda ORDER BY amt DESC NULLS LAST LIMIT 40""")
    d["award_by_agency"] = q(conn, """SELECT COALESCE(agency,'(unknown)') agency, COUNT(*) n, SUM(amount_obligated) amt
                                      FROM award GROUP BY agency ORDER BY amt DESC NULLS LAST LIMIT 30""")
    d["award_total"] = one(conn, "SELECT SUM(amount_obligated) amt, COUNT(*) n, COUNT(DISTINCT org_id) orgs FROM award", (), {}) or {}
    d["top_orgs"] = q(conn, """SELECT o.id, o.name, o.state, o.city, o.ein, o.revenue, o.expenses, o.website,
                                      COUNT(a.id) n_awards, SUM(a.amount_obligated) amt, COUNT(p.id) people
                               FROM org o
                               LEFT JOIN award a ON a.org_id=o.id
                               LEFT JOIN org_person p ON p.org_id=o.id
                               GROUP BY o.id
                               ORDER BY COALESCE(amt,0)+COALESCE(o.revenue,0) DESC LIMIT 40000""")
    d["people_top"] = q(conn, """SELECT p.*, o.state, o.website FROM org_person p LEFT JOIN org o ON o.id=p.org_id
                                 WHERE p.person IS NOT NULL
                                 ORDER BY COALESCE(p.comp,0) DESC, p.person LIMIT ?""", (PERSON_PAGE_MAX,))
    d["facilities"] = q(conn, """SELECT * FROM facility ORDER BY (status='open') DESC, beds DESC NULLS LAST""")
    d["laws"] = q(conn, "SELECT * FROM law ORDER BY jurisdiction, topic")
    d["media"] = q(conn, "SELECT * FROM media ORDER BY COALESCE(published,'') DESC LIMIT 5000")
    d["docs"] = q(conn, "SELECT * FROM doc ORDER BY COALESCE(published,'') DESC LIMIT 5000")
    d["cities"] = q(conn, """SELECT * FROM geo_city WHERE pop IS NULL OR pop>=? ORDER BY pop DESC NULLS LAST LIMIT 40000""",
                    (CITY_PAGE_MIN_POP,))
    d["sources"] = q(conn, "SELECT domain, url, title, publisher, license, retrieved_at, bytes FROM source ORDER BY domain, url")
    d["runs"] = q(conn, "SELECT domain, ts, rows, ok FROM ingest_run ORDER BY id DESC LIMIT 60")
    d["metrics"] = q(conn, """SELECT entity_type, entity_id, metric, value, unit, year, is_est
                              FROM metric WHERE metric IN ('smi_prevalence','sud_prevalence','overdose_deaths',
                              'overdose_rate','violent_crime_rate','property_crime_rate','theft_rate',
                              'state_homeless_budget','city_homeless_budget','spend_per_homeless')
                              ORDER BY metric, year DESC""")
    return d

# ---------------------------------------------------------------- geo helpers
STATES_URLS = [
    "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json",
    "https://raw.githubusercontent.com/python-visualization/folium-example-data/main/us_states.json",
]

def state_geojson(conn):
    GEO.mkdir(parents=True, exist_ok=True)
    cache = GEO / "us-states.geojson"
    if cache.exists() and cache.stat().st_size > 1000:
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    import urllib.request
    for u in STATES_URLS:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "homeless-atlas/1.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                gj = json.loads(r.read().decode())
            cache.write_text(json.dumps(gj))
            return gj
        except Exception as e:
            print("states geojson fetch failed", u, e, file=sys.stderr)
    return None

ABBR2NAME = {}


def build_exports(d, conn):
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "data").mkdir(exist_ok=True)
    feats = []
    by_abbr = {s["abbr"]: s for s in d["states"]}
    gj = state_geojson(conn)
    if gj:
        for f in gj.get("features", []):
            p = f.get("properties") or {}
            name = p.get("name") or p.get("NAME") or ""
            abbr = p.get("abbr") or p.get("STUSPS") or next((a for a, s in by_abbr.items() if s["name"] == name), None)
            if not abbr:
                continue
            s = by_abbr.get(abbr.lstrip("-").upper()) or by_abbr.get(abbr)
            rate = (s or {}).get("rate")
            p2 = {"name": name, "abbr": abbr, "rate": round(rate, 3) if rate else 0,
                  "pit": (s or {}).get("pit"), "unshel": (s or {}).get("unshel"),
                  "pop": (s or {}).get("pop")}
            rate_txt = f"{rate:.2f}" if rate else "no count yet"
            p2["__html"] = (f'<b>{esc(name)}</b><br>homeless per 10k: <b>{rate_txt}</b><br>'
                            f'count: <b>{num((s or {}).get("pit"))}</b><br>'
                            f'unsheltered: <b>{num((s or {}).get("unshel"))}</b><br>'
                            f'<a href="/state/{esc(abbr)}.html">state page →</a>') if s else f"<b>{esc(name)}</b>"
            feats.append({"type": "Feature", "properties": p2, "geometry": f.get("geometry")})
    (SITE / "data" / "map-states.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    # city points
    pts = []
    for c in d["cities"][:12000]:
        if c.get("lat") is None or c.get("lon") is None:
            continue
        pts.append({"type": "Feature", "properties": {
            "name": c["name"], "state": c["state"], "url": f"/city/{c['geoid']}.html",
            "v": c.get("pop") or 0, "rate": None,
            "__html": f'<b>{esc(c["name"])}, {esc(c["state"] or "")}</b><br>population: <b>{num(c.get("pop"))}</b><br>'
                      f'<a href="/city/{c["geoid"]}.html">city page →</a>'},
            "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]}})
    (SITE / "data" / "map-places.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": pts}))

    # coc polygons
    cfeats = []
    for c in d["cocs"]:
        g = c.get("geometry")
        if not g:
            continue
        try:
            geom = json.loads(g) if isinstance(g, str) else g
        except Exception:
            continue
        cfeats.append({"type": "Feature", "properties": {
            "name": c["name"], "coc": c["coc_num"], "pit": c.get("pit_total"),
            "rate": 0,
            "__html": f'<b>{esc(c["coc_num"])}</b> {esc(c["name"])}<br>latest PIT: <b>{num(c.get("pit_total"))}</b> ({c.get("pit_year")})<br>'
                      f'<a href="/coc/{c["coc_num"]}.html">CoC page →</a>'},
            "geometry": geom})
    (SITE / "data" / "map-cocs.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": cfeats}))

    # facilities
    ffeats = []
    for f in d["facilities"]:
        if f.get("lat") is None or f.get("lon") is None:
            continue
        ffeats.append({"type": "Feature", "properties": {
            "name": f["name"], "kind": f.get("kind"), "status": f.get("status"), "beds": f.get("beds"),
            "__html": f'<b>{esc(f["name"])}</b><br>{esc((f.get("kind") or "").replace("_"," "))} · '
                      f'<span class="tag {esc(f.get("status") or "")}">{esc(f.get("status") or "?")}</span><br>'
                      f'beds: <b>{num(f.get("beds"))}</b> · opened {num(f.get("opened"),"?")} · closed {num(f.get("closed"),"?")}<br>'
                      f'<a href="/facility/{f["id"]}.html">institution page →</a>'},
            "geometry": {"type": "Point", "coordinates": [f["lon"], f["lat"]]}})
    (SITE / "data" / "map-facilities.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": ffeats}))
    return {"states": len(feats), "places": len(pts), "cocs": len(cfeats), "facilities": len(ffeats)}


# ---------------------------------------------------------------- search index
def build_index(d):
    idx = []
    def add(t, title, sub, url):
        idx.append([t, title or "", sub or "", url])

    for s in d["states"]:
        add("state", s["name"], f"{num(s['pit'])} homeless · {rate(s['rate'])} per 10k", f"/state/{s['abbr']}.html")
    for c in d["cocs"]:
        add("coc", f"{c['coc_num']} {c.get('name') or ''}", f"CoC · {num(c.get('pit_total'))} homeless ({(c.get('pit_year') or '—')})", f"/coc/{c['coc_num']}.html")
    for c in d["cities"]:
        add("city", f"{c['name']}, {c.get('state') or ''}", f"population {num(c.get('pop'))}", f"/city/{c['geoid']}.html")
    for o in d["top_orgs"]:
        add("org", o["name"], f"{o.get('city') or ''} {o.get('state') or ''} · {money(o.get('amt'))} federal · rev {money(o.get('revenue'))}",
            f"/org/{o['id']}-{slug(o['name'])}.html")
    for p in d["people_top"]:
        add("person", p["person"], f"{(p.get('title') or '').strip()} — {p.get('org_name') or ''}", f"/person/{person_slug(p)}.html")
    for f in d["facilities"]:
        add("facility", f["name"], f"{(f.get('kind') or '').replace('_',' ')} · {f.get('status') or '?'} · {f.get('city') or ''} {f.get('state') or ''}",
            f"/facility/{f['id']}.html")
    for l in d["laws"]:
        add("law", l.get("title") or l.get("citation") or "law", f"{l.get('jurisdiction')} · {(l.get('topic') or '').replace('_',' ')}",
            f"/laws.html#{slug((l.get('jurisdiction') or '') + '-' + (l.get('citation') or l.get('title') or ''))}")
    for m in d["media"]:
        add("media", m.get("title") or "media", f"{m.get('kind')} · {m.get('publisher') or ''} {m.get('state') or ''}", m.get("url") or "/media.html")
    for x in d["docs"]:
        add("doc", x.get("title") or "document", f"{x.get('kind')} · {x.get('publisher') or ''} {x.get('published') or ''}", x.get("url") or "/docs.html")
    for u, n in NAV + [("/about.html", "About"), ("/method.html", "Method and caveats"), ("/sources.html", "Sources and licences"),
                       ("/report/san-francisco.html", "San Francisco brief")]:
        add("page", n, "section", u)

    # shard by first alnum char of title
    shards = defaultdict(list)
    for row in idx:
        k = re.sub(r"[^a-z0-9]", "", row[1].lower())[:1] or "_"
        shards[k].append(row)
    out = SITE / "data" / "idx"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for k, rows in shards.items():
        (out / f"{k}.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
        manifest[k] = len(rows)
    (out / "manifest.json").write_text(json.dumps({"shards": manifest, "total": len(idx)}))
    (SITE / "data" / "search-index.json").write_text(
        json.dumps({"total": len(idx), "types": dict(Counter(r[0] for r in idx)),
                    "items": idx if len(idx) <= 60000 else idx[:60000]},
                   ensure_ascii=False, separators=(",", ":")))
    return len(idx), manifest


def person_slug(p):
    h = hashlib.md5(((p.get("person_norm") or "") + (p.get("org_name") or "") + str(p.get("year") or "")).encode()).hexdigest()[:7]
    return f"{slug(p.get('person') or 'person', 40)}-{h}"

# ---------------------------------------------------------------- pages
def page_index(d, manifest_total):
    n, p = d["nat"], d["nat_prev"]
    delta = None
    if n.get("total") and p.get("total"):
        delta = (n["total"] - p["total"]) / p["total"] * 100
    a = d["award_total"]
    kpis = "".join([
        kpi(num(n.get("total")), f"people homeless (Jan {d['latest'] or '—'})",
            f"{pct(delta)} vs previous year" if delta is not None else "US, HUD point-in-time"),
        kpi(pct(n["unshel"] / n["total"] * 100) if (n.get("unshel") and n.get("total")) else "—",
            "unsheltered share", "on the street, not in shelter", "b"),
        kpi(money(a.get("amt")), "federal money mapped", f"{num(a.get('n'))} awards → {num(a.get('orgs'))} orgs", "c"),
        kpi(num(n.get("smi")), "severely mentally ill", "counted in the same PIT", "d"),
        kpi(num(n.get("sud")), "substance use disorder", "counted in the same PIT"),
        kpi(num(d["meta"]["counts"]["org"]), "organizations", "nonprofits, agencies, housing authorities"),
        kpi(num(d["meta"]["counts"]["org_person"]), "named people", "officers and directors from public filings"),
        kpi(num(d["meta"]["counts"]["facility"]), "institutions", "psychiatric hospitals, prisons, jails"),
    ])
    top_states = [s for s in d["states"] if s["pit"]][:15]
    rows = [[f'<a href="/state/{s["abbr"]}.html">{esc(s["name"])}</a>', "", s["name"],
             num(s["pit"]), "r", s["pit"] or 0, rate(s["rate"], 2), "r", s["rate"] or 0,
             pct(s["unshel_share"]), "r", s["unshel_share"] or 0, money(s["award"]), "r", s["award"] or 0] for s in top_states]
    top_orgs = d["top_orgs"][:20]
    orows = [[f'<a href="/org/{o["id"]}-{slug(o["name"])}.html">{esc(o["name"])[:80]}</a>', "", o["name"],
              esc(o.get("state") or ""), "", o.get("state") or "",
              money(o.get("amt")), "r", o.get("amt") or 0, money(o.get("revenue")), "r", o.get("revenue") or 0,
              num(o.get("n_awards")), "r", o.get("n_awards") or 0] for o in top_orgs]
    series = d["nat_series"]
    spark = ""
    if len(series) > 2:
        mx = max((r["total"] or 0) for r in series) or 1
        pts = " ".join(f'{i/(len(series)-1)*100:.1f},{40-(r["total"] or 0)/mx*36:.1f}' for i, r in enumerate(series))
        spark = (f'<div class="panel"><h3>National count, {series[0]["year"]}–{series[-1]["year"]}</h3>'
                 f'<svg viewBox="0 0 100 42" preserveAspectRatio="none" style="width:100%;height:110px">'
                 f'<polyline points="{pts}" fill="none" stroke="var(--ember)" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>'
                 f'<div class="legend"><span>{num(series[0]["total"])} in {series[0]["year"]}</span>'
                 f'<span>→ {num(series[-1]["total"])} in {series[-1]["year"]}</span></div></div>')
    body = banner("Public record · United States",
                  "Every count, contract, organization and institution<br>in American homelessness",
                  "HUD point-in-time counts by Continuum of Care, federal awards down to the recipient, "
                  "IRS filings, closed and open institutions, and the law in each jurisdiction — searchable.") + f"""
<div class="wrap">
  <div class="kpis">{kpis}</div>
  <div class="grid side">
    <div>
      <h2>Homelessness per 10,000 residents, by state</h2>
      <div class="map" id="map"></div>
      <div class="legend">
        <span><i style="background:#22303f"></i>0–2</span><span><i style="background:#6b2f3a"></i>2–8</span>
        <span><i style="background:#a83c33"></i>8–20</span><span><i style="background:#e2603a"></i>20–45</span>
        <span><i style="background:#ffc857"></i>45+</span>
        <span class="dim">Click a state. Rate = latest PIT count ÷ population × 10,000.</span>
      </div>
    </div>
    <div>
      {spark}
      <div class="panel" style="margin-top:16px">
        <h3>Start here</h3>
        <ul style="margin:0;padding-left:18px;font-size:.92rem">
          <li><a href="/map.html">The full map</a> — states, CoCs, cities, institutions</li>
          <li><a href="/orgs.html">Organizations</a> ranked by public money</li>
          <li><a href="/budgets.html">Where the money goes</a></li>
          <li><a href="/facilities.html">Asylums and prisons</a>, open and closed</li>
          <li><a href="/report/san-francisco.html">San Francisco brief</a></li>
          <li><a href="/method.html">How every number is produced</a></li>
        </ul>
      </div>
    </div>
  </div>
  <h2>States ranked by homeless population</h2>
  {table([("State",""),("Homeless","r"),("Per 10k","r"),("Unsheltered","r"),("Federal $","r")], rows)}
  <h2>Organizations holding the most public money</h2>
  {table([("Organization",""),("State",""),("Federal awards","r"),("Revenue (990)","r"),("Awards","r")], orows)}
  <h2>What is in the database</h2>
  <div class="cards">
    <div class="card"><h3>{num(d['meta']['counts']['award'])}</h3><div class="m">federal awards</div></div>
    <div class="card"><h3>{num(d['meta']['counts']['pit'])}</h3><div class="m">CoC-year count rows</div></div>
    <div class="card"><h3>{num(d['meta']['counts']['geo_city'])}</h3><div class="m">places</div></div>
    <div class="card"><h3>{num(d['meta']['counts']['law'])}</h3><div class="m">law rows</div></div>
    <div class="card"><h3>{num(d['meta']['counts']['media'])}</h3><div class="m">videos &amp; images</div></div>
    <div class="card"><h3>{num(d['meta']['counts']['source'])}</h3><div class="m">registered sources</div></div>
  </div>
</div>
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script>
fetch('/data/map-states.geojson').then(r=>r.json()).then(gj=>{{
  var m=atlasMap.base('map',{{zoom:3.0}});
  m.on('load',function(){{ atlasMap.choro(m,gj,'st'); }});
}}).catch(function(){{document.getElementById('map').innerHTML='<p style="padding:20px" class="dim">Map data not built yet.</p>';}});
</script>"""
    return shell("", "Overview", "", body, desc="Atlas of US homelessness: counts, contracts, organizations, institutions, laws.", manifest_total=manifest_total)


def page_map(d, manifest_total):
    body = banner("Map", "One map, every layer", "State heat map by homeless rate, Continuum of Care boundaries, "
                   "city populations and institutions. Toggle layers; click anything for the record.") + """
<div class="wrap">
  <div class="layertoggle" id="lt">
    <button data-l="choro" class="on">State heat</button>
    <button data-l="coc" class="on">CoC areas</button>
    <button data-l="cities">Cities</button>
    <button data-l="fac" class="on">Institutions</button>
  </div>
  <div class="map tall" id="map"></div>
  <div class="legend">
    <span><i style="background:#22303f"></i>low</span><span><i style="background:#a83c33"></i>high</span>
    <span><i style="background:#2ee6c5"></i>institution open</span><span><i style="background:#ff5d5d"></i>closed</span>
    <span class="dim">Data: HUD PIT/AHAR, Census, Wikidata. Click a feature.</span>
  </div>
</div>
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script>
var m=atlasMap.base('map',{zoom:3.2});
var loaded={};
function loadLayer(name){
  if(loaded[name])return;
  loaded[name]=1;
  if(name==='choro') fetch('/data/map-states.geojson').then(r=>r.json()).then(g=>atlasMap.choro(m,g,'st'));
  if(name==='coc') fetch('/data/map-cocs.geojson').then(r=>r.json()).then(g=>{
     m.addSource('coc',{type:'geojson',data:g});
     m.addLayer({id:'coc-l',type:'line',source:'coc',paint:{'line-color':'#5aa9ff','line-width':1,'line-opacity':.55}});
     var pop=new maplibregl.Popup({maxWidth:'300px'});
     m.on('click','coc-l',e=>{var p=e.features[0].properties;pop.setLngLat(e.lngLat).setHTML(p.__html||p.name).addTo(m);});
  });
  if(name==='cities') fetch('/data/map-places.geojson').then(r=>r.json()).then(g=>atlasMap.pops(m,g,'cities','rate','name'));
  if(name==='fac') fetch('/data/map-facilities.geojson').then(r=>r.json()).then(g=>{
     m.addSource('fac',{type:'geojson',data:g});
     m.addLayer({id:'fac',type:'circle',source:'fac',paint:{
       'circle-radius':['interpolate',['linear'],['sqrt',['coalesce',['get','beds'],4]],1,3,20,12],
       'circle-color':['match',['get','status'],'closed','#ff5d5d','open','#2ee6c5','#ffc857'],
       'circle-opacity':.85,'circle-stroke-color':'#0b0e13','circle-stroke-width':.6}});
     var pop=new maplibregl.Popup({maxWidth:'300px'});
     m.on('click','fac',e=>{var p=e.features[0].properties;pop.setLngLat(e.features[0].geometry.coordinates).setHTML(p.__html||p.name).addTo(m);});
  });
}
m.on('load',function(){loadLayer('choro');loadLayer('coc');loadLayer('fac');});
document.querySelectorAll('#lt button').forEach(function(b){
  b.addEventListener('click',function(){
    var l=b.dataset.l,on=b.classList.toggle('on');
    loadLayer(l);
    var ids={choro:['st-fill','st-line'],coc:['coc-l'],cities:['cities'],fac:['fac']}[l]||[];
    ids.forEach(function(id){ if(m.getLayer(id)) m.setLayoutProperty(id,'visibility',on?'visible':'none'); });
  });
});
</script>"""
    return shell("", "Map", "map.html", body, manifest_total=manifest_total)


def page_states(d, manifest_total):
    rows = []
    for s in d["states"]:
        if not (s["pit"] or s["pop"]):
            continue
        rows.append([f'<a href="/state/{s["abbr"]}.html">{esc(s["name"])}</a>', "", s["name"],
                     num(s["pop"]), "r", s["pop"] or 0, num(s["pit"]), "r", s["pit"] or 0,
                     pct(s["rate"], 2), "r", s["rate"] or 0, pct(s["unshel_share"]), "r", s["unshel_share"] or 0,
                     money(s["award"]), "r", s["award"] or 0, num(s["orgs"]), "r", s["orgs"] or 0])
    body = banner("States", f"{len(rows)} states and territories", "Ranked by homeless population. Rate is per 10,000 residents; "
                   "federal money is obligated awards whose place of performance is in that state.") + f"""
<div class="wrap"><div style="margin-top:22px">
{table([("State",""),("Population","r"),("Homeless","r"),("Per 10k","r"),("Unsheltered","r"),("Federal $","r"),("Orgs","r")], rows,
       note="County/CoC level detail is on each state page. Counts are HUD point-in-time, one night in January.")}
</div></div>"""
    return shell("", "States", "states.html", body, manifest_total=manifest_total)


def page_cities(d, manifest_total):
    rows = []
    for c in d["cities"][:3000]:
        if not c.get("pop"):
            continue
        rows.append([f'<a href="/city/{c["geoid"]}.html">{esc(c["name"])}</a>', "", c["name"],
                     esc(c.get("state") or ""), "", c.get("state") or "",
                     num(c.get("pop")), "r", c.get("pop") or 0,
                     esc(c.get("coc_num") or "—"), "", c.get("coc_num") or "",
                     f"{round(c['lat'],3) if c.get('lat') else ''}, {round(c['lon'],3) if c.get('lon') else ''}", "r",
                     c.get("lat") or 0])
    body = banner("Cities", "Places, populations and their Continuum of Care",
                  "Every US place above 25,000 people, with the CoC that plans its homelessness response. "
                  "Counts live on the CoC page because HUD counts by CoC, not by city limit.") + f"""
<div class="wrap"><div style="margin-top:22px">
{table([("City",""),("State",""),("Population","r"),("CoC",""),("Coordinates","r")], rows)}
</div></div>"""
    return shell("", "Cities", "cities.html", body, manifest_total=manifest_total)


def page_orgs(d, manifest_total):
    rows = []
    for o in d["top_orgs"][:2500]:
        rows.append([f'<a href="/org/{o["id"]}-{slug(o["name"])}.html">{esc(o["name"])}</a>', "", o["name"],
                     esc(o.get("state") or ""), "", o.get("state") or "",
                     money(o.get("amt")), "r", o.get("amt") or 0, money(o.get("revenue")), "r", o.get("revenue") or 0,
                     num(o.get("n_awards")), "r", o.get("n_awards") or 0, num(o.get("people")), "r", o.get("people") or 0])
    body = banner("Organizations", f"{num(d['meta']['counts']['org'])} organizations",
                  "Nonprofits, housing authorities and agencies that receive public money to address homelessness, "
                  "ranked by federal awards plus reported revenue.") + f"""
<div class="wrap"><div style="margin-top:22px">
<p class="muted"><a href="/org/index.html"><b>Browse all organizations A–Z →</b></a> · Filter: <input id="f" placeholder="type a name, state or EIN" style="background:var(--bg2);border:1px solid var(--line2);border-radius:9px;color:var(--fg);padding:7px 11px;font:inherit;min-width:260px"></p>
{table([("Organization",""),("State",""),("Federal $","r"),("Revenue","r"),("Awards","r"),("People","r")], rows,
       note="Federal $ is obligated award value from USASpending. Revenue is the latest IRS Form 990 figure where filed.")}
</div></div>
<script>
var f=document.getElementById('f');
f.addEventListener('input',function(){{
  var v=f.value.toLowerCase();
  document.querySelectorAll('table.sortable tbody tr').forEach(function(tr){{
    tr.style.display = tr.textContent.toLowerCase().indexOf(v)>=0 ? '' : 'none';
  }});
}});
</script>"""
    return shell("", "Organizations", "orgs.html", body, manifest_total=manifest_total)


def page_org_index(d, manifest_total):
    groups = defaultdict(list)
    for o in d["top_orgs"]:
        groups[(o["name"] or "?")[0].upper()].append(o)
    out = []
    letters = "".join(f'<a class="tag" href="#L{k}">{k}</a>' for k in sorted(groups))
    out.append(f'<p>{letters}</p>')
    for k in sorted(groups):
        rows = []
        for o in sorted(groups[k], key=lambda x: (x["name"] or "").lower()):
            rows.append([f'<a href="../org/{o["id"]}-{slug(o["name"])}.html">{esc(o["name"])}</a>', "", o["name"],
                         esc(o.get("city") or ""), "", o.get("city") or "", esc(o.get("state") or ""), "",
                         o.get("state") or "", money(o.get("amt")), "r", o.get("amt") or 0])
        out.append(f'<h2 id="L{k}">{k} <span class="dim" style="font-size:.6em">{len(rows)}</span></h2>')
        out.append(table([("Organization",""),("City",""),("State",""),("Federal $","r")], rows))
    body = banner("Directory", f"All {num(len(d['top_orgs']))} organizations, A–Z",
                  "The complete alphabetical index. Use search for anything faster — press / anywhere on this site.",
                  crumbs='<a href="/index.html">Atlas</a> / Directory', small=True) + \
        f'<div class="wrap" style="margin-top:18px">{"".join(out)}</div>'
    return shell("../", "Organization directory", "", body, manifest_total=manifest_total)


def page_people(d, manifest_total):
    rows = []
    for p in d["people_top"][:4000]:
        rows.append([f'<a href="/person/{person_slug(p)}.html">{esc(p["person"])}</a>', "", p["person"],
                     esc(p.get("title") or ""), "", p.get("title") or "",
                     f'<a href="/org/{p["org_id"]}-{slug(p.get("org_name") or "")}.html">{esc(p.get("org_name") or "")}</a>' if p.get("org_id") else esc(p.get("org_name") or ""), "", p.get("org_name") or "",
                     esc(p.get("state") or ""), "", p.get("state") or "",
                     num(p.get("comp")), "r", p.get("comp") or 0, num(p.get("year")), "r", p.get("year") or 0])
    body = banner("People", "Who runs the organizations",
                  "Officers, directors and key employees exactly as published in IRS Form 990 filings. "
                  "Professional roles only — no personal contact details are collected.") + f"""
<div class="wrap"><div style="margin-top:22px">
{table([("Person",""),("Title",""),("Organization",""),("State",""),("Compensation","r"),("Year","r")], rows)}</div></div>"""
    return shell("", "People", "people.html", body, manifest_total=manifest_total)


def page_facilities(d, manifest_total):
    facs = d["facilities"]
    kinds = Counter((f.get("kind") or "other") for f in facs)
    st = Counter((f.get("status") or "unknown") for f in facs)
    beds_open = sum(f.get("beds") or 0 for f in facs if (f.get("status") == "open"))
    beds_closed = sum(f.get("beds") or 0 for f in facs if (f.get("status") == "closed"))
    rows = []
    for f in facs[:3000]:
        rows.append([f'<a href="/facility/{f["id"]}.html">{esc(f["name"])}</a>', "", f["name"],
                     esc((f.get("kind") or "").replace("_", " ")), "", f.get("kind") or "",
                     f'<span class="tag {esc(f.get("status") or "")}">{esc(f.get("status") or "?")}</span>', "", f.get("status") or "",
                     esc(f.get("city") or ""), "", f.get("city") or "", esc(f.get("state") or ""), "", f.get("state") or "",
                     num(f.get("beds")), "r", f.get("beds") or 0, num(f.get("opened")), "r", f.get("opened") or 0,
                     num(f.get("closed")), "r", f.get("closed") or 0])
    body = banner("Institutions", "Asylums, hospitals and prisons — open and closed",
                  "State psychiatric hospitals, psychiatric units, prisons and jails, with capacity, operator and "
                  "operating economics where they are published.") + f"""
<div class="wrap">
  <div class="kpis">
    {kpi(num(len(facs)), "institutions recorded")}
    {kpi(num(st.get('closed',0)), "closed", "deinstitutionalisation, counted", "b")}
    {kpi(num(beds_closed), "beds in closed institutions", "capacity that once existed", "c")}
    {kpi(num(beds_open), "beds open today", "", "d")}
  </div>
  <div class="map" id="map"></div>
  <div class="legend"><span><i style="background:#2ee6c5"></i>open</span><span><i style="background:#ff5d5d"></i>closed</span>
  <span><i style="background:#ffc857"></i>other/unknown</span></div>
  <h2>All institutions</h2>
  {table([("Institution",""),("Kind",""),("Status",""),("City",""),("State",""),("Beds","r"),("Opened","r"),("Closed","r")], rows)}
</div>
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script>
var m=atlasMap.base('map',{{zoom:3.1}});
m.on('load',function(){{
 fetch('/data/map-facilities.geojson').then(r=>r.json()).then(function(g){{
   m.addSource('fac',{{type:'geojson',data:g}});
   m.addLayer({{id:'fac',type:'circle',source:'fac',paint:{{
     'circle-radius':['interpolate',['linear'],['sqrt',['coalesce',['get','beds'],4]],1,3,20,13],
     'circle-color':['match',['get','status'],'closed','#ff5d5d','open','#2ee6c5','#ffc857'],
     'circle-opacity':.85,'circle-stroke-color':'#0b0e13','circle-stroke-width':.6}}}});
   var pop=new maplibregl.Popup({{maxWidth:'300px'}});
   m.on('click','fac',function(e){{var p=e.features[0].properties;pop.setLngLat(e.features[0].geometry.coordinates).setHTML(p.__html||p.name).addTo(m);}});
 }});
}});
</script>"""
    return shell("", "Institutions", "facilities.html", body, manifest_total=manifest_total)


def page_budgets(d, manifest_total):
    cf = d["award_by_cfda"]
    ag = d["award_by_agency"]
    crow = [[esc(r.get("cfda") or ""), "", r.get("cfda") or "",
             esc((r.get("program") or "")[:70]), "", r.get("program") or "",
             num(r.get("n")), "r", r.get("n") or 0, money(r.get("amt")), "r", r.get("amt") or 0] for r in cf]
    arow = [[esc(r.get("agency") or ""), "", r.get("agency") or "", num(r.get("n")), "r", r.get("n") or 0,
             money(r.get("amt")), "r", r.get("amt") or 0] for r in ag]
    srow = [[f'<a href="/state/{s["abbr"]}.html">{esc(s["name"])}</a>', "", s["name"],
             num(s["pit"]), "r", s["pit"] or 0, money(s["award"]), "r", s["award"] or 0,
             f'${(s["award"]/s["pit"]):,.0f}' if (s["award"] and s["pit"]) else "—", "r", (s["award"] / s["pit"]) if (s["award"] and s["pit"]) else 0]
            for s in d["states"] if s.get("award") or s.get("pit")]
    mrows = [[esc(m.get("entity_type") or ""), "", m.get("entity_type") or "", esc(str(m.get("entity_id") or "")), "",
              str(m.get("entity_id") or ""), esc((m.get("metric") or "").replace("_", " ")), "", m.get("metric") or "",
              num(m.get("value")), "r", m.get("value") or 0, esc(m.get("unit") or ""), "", m.get("unit") or "",
              str(m.get("year") or ""), "", m.get("year") or 0] for m in d["metrics"][:1500]]
    body = banner("Money", "Follow the money", "Federal awards by program and agency, and per-homeless-person spend by state. "
                  "Award values are obligated amounts from USASpending, deduplicated by award key.") + f"""
<div class="wrap">
  <h2>Federal programs funding homelessness</h2>
  {table([("Assistance listing",""),("Program",""),("Awards","r"),("Obligated","r")], crow)}
  <h2>By awarding agency</h2>
  {table([("Agency",""),("Awards","r"),("Obligated","r")], arow)}
  <h2>Federal dollars per homeless person, by state</h2>
  {table([("State",""),("Homeless","r"),("Federal $","r"),("$ per person","r")], srow,
         note="† per-person figure is derived (obligated awards ÷ latest PIT count), not a published number. Award place-of-performance is not the same as where the money is spent.")}
  <h2>Other public money and area metrics</h2>
  {table([("Scope",""),("Entity",""),("Metric",""),("Value","r"),("Unit",""),("Year","r")], mrows)}
</div>"""
    return shell("", "Money", "budgets.html", body, manifest_total=manifest_total)


def page_laws(d, manifest_total):
    rows = []
    for l in d["laws"][:4000]:
        anchor = slug((l.get("jurisdiction") or "") + "-" + (l.get("citation") or l.get("title") or ""))
        rows.append([f'<span id="{anchor}">{esc(l.get("jurisdiction") or "")}</span>', "", l.get("jurisdiction") or "",
                     esc((l.get("topic") or "").replace("_", " ")), "", l.get("topic") or "",
                     f'<a href="{esc(l.get("url") or "#")}" rel="noopener">{esc(l.get("title") or l.get("citation") or "")}</a>', "",
                     l.get("title") or "", esc(l.get("status") or ""), "", l.get("status") or "",
                     num(l.get("year")), "r", l.get("year") or 0])
    body = banner("Law", "What the law actually says", "Camping and sit-lie rules, property and encampment clearance, "
                  "involuntary psychiatric hold statutes, civil commitment, and Housing First mandates — with citations.") + f"""
<div class="wrap"><div style="margin-top:22px">
{table([("Jurisdiction",""),("Topic",""),("Provision",""),("Status",""),("Year","r")], rows)}</div></div>"""
    return shell("", "Laws", "laws.html", body, manifest_total=manifest_total)


def page_media(d, manifest_total):
    vids = [m for m in d["media"] if m.get("kind") == "video"]
    imgs = [m for m in d["media"] if m.get("kind") == "image"]
    def vcard(m):
        vid = ""
        u = m.get("url") or ""
        mm = re.search(r"(?:v=|youtu\.be/|/embed/)([\w-]{11})", u)
        if mm:
            vid = mm.group(1)
        thumb = m.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "")
        return (f'<a class="card" href="{esc(u)}" rel="noopener" style="display:block">'
                + (f'<img src="{esc(thumb)}" alt="" loading="lazy" style="border-radius:8px;aspect-ratio:16/9;object-fit:cover;width:100%">' if thumb else "")
                + f'<h3 style="margin-top:9px">{esc((m.get("title") or "")[:110])}</h3>'
                f'<div class="m">{esc(m.get("publisher") or m.get("author") or "")} · {esc(m.get("published") or "")}'
                f'{(" · " + esc(m["state"])) if m.get("state") else ""}</div></a>')
    def icard(m):
        return (f'<a class="card" href="{esc(m.get("url") or "#")}" rel="noopener" style="display:block">'
                f'<img src="{esc(m.get("thumbnail") or m.get("url") or "")}" alt="" loading="lazy" style="border-radius:8px;aspect-ratio:4/3;object-fit:cover;width:100%">'
                f'<div class="m" style="margin-top:8px">{esc((m.get("title") or "")[:90])}<br>{esc(m.get("license") or "licence unknown")}</div></a>')
    body = banner("Media", "What it looks like", "Public videos and openly licensed images, filed by place. "
                  "Videos link to their publisher; nothing is rehosted.") + f"""
<div class="wrap">
  <h2>Videos <span class="dim" style="font-size:.7em">{num(len(vids))}</span></h2>
  <div class="cards">{''.join(vcard(m) for m in vids[:60])}</div>
  <h2>Images <span class="dim" style="font-size:.7em">{num(len(imgs))}</span></h2>
  <div class="cards">{''.join(icard(m) for m in imgs[:60])}</div>
</div>"""
    return shell("", "Media", "media.html", body, manifest_total=manifest_total)


def page_docs(d, manifest_total):
    rows = [[esc((x.get("kind") or "")), "", x.get("kind") or "",
             f'<a href="{esc(x.get("url") or "#")}" rel="noopener">{esc((x.get("title") or "")[:110])}</a>', "", x.get("title") or "",
             esc(x.get("publisher") or ""), "", x.get("publisher") or "", esc(x.get("published") or ""), "", x.get("published") or "",
             esc(x.get("state") or ""), "", x.get("state") or ""] for x in d["docs"][:3000]]
    body = banner("Documents", "Audits, court decisions, studies", "The paper trail: inspector-general audits, GAO reports, "
                  "city controller reviews, federal court decisions and the strongest investigative series.") + f"""
<div class="wrap"><div style="margin-top:22px">
{table([("Kind",""),("Document",""),("Publisher",""),("Date",""),("State","")], rows)}</div></div>"""
    return shell("", "Documents", "docs.html", body, manifest_total=manifest_total)


def page_search(d, manifest_total):
    body = banner("Search", "Search everything", "Every state, CoC, city, organization, person, institution, law, video and document in the atlas.") + """
<div class="wrap wrap-narrow">
  <div class="searchbar"><input id="q" placeholder="Try: San Francisco, Continuum of Care, Laguna Honda, camping ban…" aria-label="Search query" autocomplete="off"></div>
  <div class="filters">
    <button data-type="" class="on">All</button><button data-type="org">Organizations</button>
    <button data-type="city">Cities</button><button data-type="coc">CoCs</button><button data-type="state">States</button>
    <button data-type="person">People</button><button data-type="facility">Institutions</button>
    <button data-type="law">Laws</button><button data-type="media">Media</button><button data-type="doc">Docs</button>
  </div>
  <div id="results"><p class="dim">Type at least two characters.</p></div>
</div>"""
    return shell("", "Search", "search.html", body, manifest_total=manifest_total)


def page_about(d, manifest_total):
    c = d["meta"]["counts"]
    body = banner("About", "What this is", "A public-record atlas, built from primary sources, of how the United States "
                  "counts, funds, houses, punishes and treats homeless people.") + f"""
<div class="wrap wrap-narrow">
<h2>What is here</h2>
<p>Homeless Atlas joins the datasets that normally live apart:</p>
<ul>
<li><b>{num(c['pit'])} Point-in-Time count rows</b> — HUD's January street and shelter counts, by Continuum of Care, by year.</li>
<li><b>{num(c['award'])} federal awards</b> — every contract, grant and cooperative agreement whose purpose is homelessness, with recipient.</li>
<li><b>{num(c['org'])} organizations</b> and <b>{num(c['org_person'])} named officers</b> — from USASpending, IRS Form 990 filings and SAM registrations.</li>
<li><b>{num(c['facility'])} institutions</b> — state psychiatric hospitals, psychiatric units, prisons and jails, open and closed, with beds and, where published, budgets.</li>
<li><b>{num(c['law'])} law rows</b>, <b>{num(c['media'])} media items</b>, <b>{num(c['doc'])} documents</b>.</li>
</ul>
<h2>What this is not</h2>
<p>It is not a list of homeless people. No individual experiencing homelessness is identified anywhere in this dataset.
It is not a claim that homeless people are criminals: no national crime dataset records housing status, and any page that
puts crime numbers next to homeless counts says so explicitly. It is not legal advice.</p>
<h2>Why</h2>
<p>Because the money is public and the outcome is visible on the street. If a city spends $100,000 per person housed while
the count rises, that is a fact that should be one search away, with a citation — not a press release.</p>
<h2>Corrections</h2>
<p>Every row carries its source and retrieval date. If a number here disagrees with its source, the source wins — open an issue
with the row's source URL.</p>
</div>"""
    return shell("", "About", "about.html", body, manifest_total=manifest_total)


def page_method(d, manifest_total):
    body = banner("Method", "How every number is produced", "Definitions, joins, and the places where the data lies to you.") + """
<div class="wrap wrap-narrow">
<h2>Definitions</h2>
<dl class="facts">
<dt>PIT count</dt><dd>HUD's Point-in-Time count: people in shelters and on the street on one night in the last ten days of January.
It is a census of visible homelessness, not an estimate of everyone who is homeless during a year.</dd>
<dt>CoC</dt><dd>Continuum of Care: the HUD-designated regional body that plans and receives homelessness funding. There are ~400.
Counts are published by CoC, never by city limit — that is why city numbers here point to their CoC.</dd>
<dt>Unsheltered</dt><dd>People counted outdoors, in vehicles, or in places not meant for habitation. The least reliable and most
policy-sensitive number in the whole system.</dd>
<dt>Chronically homeless</dt><dd>HUD definition: a disabling condition plus either 12 months continuous homelessness or four episodes in three years.</dd>
<dt>Federal award value</dt><dd>Obligated amount from USASpending. Obligated ≠ spent, and place of performance ≠ where services happen.
Contracts and grants are counted separately and deduplicated by generated award key.</dd>
<dt>† derived</dt><dd>Anything marked † is computed by us (a rate, a per-person figure, a per-bed cost). It is never presented as published.</dd>
</dl>
<h2>Known biases and traps</h2>
<ul>
<li><b>Count methods differ by CoC.</b> Some do a full census, some sample, some use HMIS extrapolation. Year-over-year change in a
single CoC can be a method change, not a trend.</li>
<li><b>Federal money is not the whole budget.</b> City general funds, county health dollars, Medicaid and philanthropy are mostly outside
USASpending. Where city or state budgets are included they are labelled.</li>
<li><b>Nonprofit revenue is self-reported</b> and lagged: a Form 990 covers a fiscal year that may be 18 months old when filed.</li>
<li><b>Institutional capacity</b> figures come from many different agencies with different definitions of a "bed". Where a state
publishes licensed beds we use that; where it publishes staffed beds we say so.</li>
<li><b>Crime.</b> UCR/NIBRS do not record housing status. Area rates are context only. Studies that do measure incidents are listed
in Documents, with their own methodology.</li>
<li><b>Substance use and mental illness.</b> PIT subpopulation counts are CoC-reported, self-reported or observed, and systematically
undercount. State prevalence estimates (NSDUH) are modelled survey estimates, not counts of homeless people.</li>
</ul>
<h2>Pipeline</h2>
<p>Each source has an ingestor in <code>ingest/domains/</code> that caches the raw response under <code>data/raw/</code>, records the URL,
retrieval time, byte size and SHA-256 in the <code>source</code> table, and writes rows that reference it. The site is generated from
the database by <code>scripts/build_site.py</code>. Nothing on this site is hand-typed.</p>
</div>"""
    return shell("", "Method", "method.html", body, manifest_total=manifest_total)


def page_sources(d, manifest_total):
    rows = []
    for s in d["sources"][:4000]:
        u = s.get("url") or ""
        rows.append([esc(s.get("domain") or ""), "", s.get("domain") or "",
                     f'<a href="{esc(u)}" rel="noopener">{esc((s.get("title") or u)[:90])}</a>', "", s.get("title") or u,
                     esc(s.get("publisher") or ""), "", s.get("publisher") or "",
                     esc(s.get("license") or ""), "", s.get("license") or "",
                     esc((s.get("retrieved_at") or "")[:10]), "", s.get("retrieved_at") or ""])
    body = banner("Sources", f"{num(len(d['sources']))} registered sources", "Every source used, with its licence and the date we retrieved it.") + f"""
<div class="wrap"><div style="margin-top:22px">
{table([("Domain",""),("Source",""),("Publisher",""),("Licence",""),("Retrieved","")], rows,
       note="Public data used under its own terms. HUD, Census, USASpending and Wikidata data are public domain or CC0; ProPublica Nonprofit Explorer is used under its terms; Wikipedia/Wikimedia content is CC BY-SA and attributed at the source link.")}
</div></div>"""
    return shell("", "Sources", "sources.html", body, manifest_total=manifest_total)


def page_report_sf(d, manifest_total):
    src = ROOT / "reports" / "san-francisco.md"
    if src.exists():
        text = src.read_text()
        text = re.sub(r"\A\s*#\s+.*\n", "", text, count=1)
        body = banner("Deep brief", "San Francisco", "The flagship case study: counts, contracts, organizations, institutions, law and cost.",
                      crumbs="Report") + \
            f'<div class="wrap wrap-narrow" style="margin-top:22px">{md(text)}</div>'
    else:
        body = banner("Deep brief", "San Francisco", "Not written yet.") + (
            '<div class="wrap wrap-narrow"><p class="dim">The San Francisco brief is generated from the database by '
            '<code>scripts/sf_report.py</code>. It has not run yet.</p></div>')
    return shell("../", "San Francisco", "", body, manifest_total=manifest_total)


# ---------- entity pages
def page_state(d, s, manifest_total):
    abbr = s["abbr"]
    cocs = [c for c in d["cocs"] if (c.get("state") or "") == abbr]
    facs = [f for f in d["facilities"] if (f.get("state") or "") == abbr]
    laws = [l for l in d["laws"] if (l.get("jurisdiction") or "") == abbr]
    orgs = q(CONN, """SELECT o.id,o.name,o.city,o.revenue,(SELECT SUM(amount_obligated) FROM award a WHERE a.org_id=o.id) amt
                      FROM org o WHERE o.state=? ORDER BY COALESCE(amt,0)+COALESCE(o.revenue,0) DESC LIMIT 60""", (abbr,))
    series = q(CONN, """SELECT p.year, SUM(p.total) total, SUM(p.sheltered) shel, SUM(p.unsheltered) unshel
                        FROM pit p JOIN geo_coc c ON c.coc_num=p.coc_num WHERE c.state=? GROUP BY p.year ORDER BY p.year""", (abbr,))
    spark = ""
    if len(series) > 2:
        mx = max((r["total"] or 0) for r in series) or 1
        pts = " ".join(f'{i/(len(series)-1)*100:.1f},{44-(r["total"] or 0)/mx*40:.1f}' for i, r in enumerate(series))
        spark = (f'<div class="panel"><h3>Homeless count {series[0]["year"]}–{series[-1]["year"]}</h3>'
                 f'<svg viewBox="0 0 100 46" preserveAspectRatio="none" style="width:100%;height:120px">'
                 f'<polyline points="{pts}" fill="none" stroke="var(--ember)" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg></div>')
    crow = [[f'<a href="/coc/{esc(c["coc_num"])}.html">{esc(c["coc_num"])}</a>', "", c["coc_num"],
             esc(c.get("name") or ""), "", c.get("name") or "", num(c.get("pit_total")), "r", c.get("pit_total") or 0,
             str(c.get("pit_year") or ""), "", c.get("pit_year") or 0] for c in cocs]
    orow = [[f'<a href="/org/{o["id"]}-{slug(o["name"])}.html">{esc(o["name"])}</a>', "", o["name"], esc(o.get("city") or ""), "",
             o.get("city") or "", money(o.get("amt")), "r", o.get("amt") or 0, money(o.get("revenue")), "r", o.get("revenue") or 0] for o in orgs]
    frow = [[f'<a href="/facility/{f["id"]}.html">{esc(f["name"])}</a>', "", f["name"],
             f'<span class="tag {esc(f.get("status") or "")}">{esc(f.get("status") or "?")}</span>', "", f.get("status") or "",
             esc(f.get("city") or ""), "", f.get("city") or "", num(f.get("beds")), "r", f.get("beds") or 0] for f in facs[:200]]
    lrow = [[esc((l.get("topic") or "").replace("_", " ")), "", l.get("topic") or "",
             f'<a href="{esc(l.get("url") or "#")}" rel="noopener">{esc(l.get("title") or l.get("citation") or "")}</a>', "",
             l.get("title") or "", esc(l.get("status") or ""), "", l.get("status") or ""] for l in laws[:200]]
    body = banner(s["name"], f'{num(s["pit"])} people homeless · {rate(s["rate"], 1)} per 10,000',
                  f'Population {num(s["pop"])} · unsheltered share {pct(s["unshel_share"])} · '
                  f'{money(s["award"])} in federal awards · {num(s["orgs"])} organizations',
                  crumbs=f'<a href="/index.html">Atlas</a> / <a href="/states.html">States</a> / {esc(s["name"])}', small=True) + f"""
<div class="wrap">
  <div class="grid side" style="margin-top:18px">
    <div>{spark}</div>
    <div class="panel"><h3>Key numbers</h3><dl class="facts">
      <dt>Homeless (PIT {d['latest'] or ''})</dt><dd>{num(s['pit'])}</dd>
      <dt>Sheltered</dt><dd>{num(s['shel'])}</dd>
      <dt>Unsheltered</dt><dd>{num(s['unshel'])}</dd>
      <dt>Chronically homeless</dt><dd>{num(s['chronic'])}</dd>
      <dt>CoCs</dt><dd>{num(s['cocs'])}</dd>
      <dt>Federal awards</dt><dd>{num(s['awards'])} · {money(s['award'])}</dd>
      <dt>Nonprofit revenue</dt><dd>{money(s['revenue'])}</dd>
    </dl></div>
  </div>
  <h2>Continuums of Care</h2>
  {table([("CoC",""),("Name",""),("Latest count","r"),("Year","r")], crow)}
  <h2>Organizations</h2>
  {table([("Organization",""),("City",""),("Federal $","r"),("Revenue","r")], orow)}
  <h2>Institutions</h2>
  {table([("Institution",""),("Status",""),("City",""),("Beds","r")], frow)}
  <h2>Law</h2>
  {table([("Topic",""),("Provision",""),("Status","")], lrow)}
</div>"""
    return shell("../", f'{s["name"]} — homelessness', "", body, manifest_total=manifest_total)


def page_coc(d, c, manifest_total):
    num_ = c["coc_num"]
    series = q(CONN, """SELECT * FROM pit WHERE coc_num=? ORDER BY year""", (num_,))
    sub = q(CONN, """SELECT * FROM pit WHERE coc_num=? ORDER BY year DESC LIMIT 1""", (num_,))
    orgs = q(CONN, """SELECT o.id,o.name,o.city,o.revenue,(SELECT SUM(amount_obligated) FROM award a WHERE a.org_id=o.id) amt
                      FROM org o WHERE o.coc_num=? OR (o.state=? AND o.city IS NOT NULL) ORDER BY COALESCE(amt,0) DESC LIMIT 60""",
              (num_, (num_ or "")[:2]))
    srow = [[str(r["year"]), "", r["year"], num(r["total"]), "r", r["total"] or 0, num(r["sheltered"]), "r", r["sheltered"] or 0,
             num(r["unsheltered"]), "r", r["unsheltered"] or 0, num(r["chronic"]), "r", r["chronic"] or 0,
             num(r["smi"]), "r", r["smi"] or 0, num(r["sud"]), "r", r["sud"] or 0, num(r["veterans"]), "r", r["veterans"] or 0]
            for r in series]
    orow = [[f'<a href="/org/{o["id"]}-{slug(o["name"])}.html">{esc(o["name"])}</a>', "", o["name"],
             esc(o.get("city") or ""), "", o.get("city") or "", money(o.get("amt")), "r", o.get("amt") or 0,
             money(o.get("revenue")), "r", o.get("revenue") or 0] for o in orgs]
    cur = sub[0] if sub else {}
    body = banner(f'CoC {num_}', esc(c.get("name") or num_),
                  f'Latest count {num(c.get("pit_total"))} ({c.get("pit_year") or "—"}) · {esc(c.get("cities") or "")}',
                  crumbs=f'<a href="/index.html">Atlas</a> / <a href="/states.html">States</a> / '
                         f'<a href="/state/{esc((num_ or "")[:2])}.html">{esc((num_ or "")[:2])}</a> / {esc(num_)}', small=True) + f"""
<div class="wrap">
  <div class="kpis">
    {kpi(num(cur.get('total')), f"homeless ({cur.get('year') or '—'})")}
    {kpi(num(cur.get('sheltered')), "sheltered", "", "b")}
    {kpi(num(cur.get('unsheltered')), "unsheltered", "street count", "d")}
    {kpi(num(cur.get('chronic')), "chronically homeless", "", "c")}
    {kpi(num(cur.get('smi')), "severely mentally ill", "", "")}
    {kpi(num(cur.get('sud')), "substance use disorder", "", "")}
  </div>
  <h2>Every reported year</h2>
  {table([("Year",""),("Total","r"),("Sheltered","r"),("Unsheltered","r"),("Chronic","r"),("SMI","r"),("SUD","r"),("Veterans","r")], srow,
         note="Blank cells are years or subpopulations HUD did not publish for this CoC. We never interpolate inside this table.")}
  <h2>Organizations associated with this CoC</h2>
  {table([("Organization",""),("City",""),("Federal $","r"),("Revenue","r")], orow)}
</div>"""
    return shell("../", f'{num_} {c.get("name") or ""}', "", body, manifest_total=manifest_total)


def page_city(d, c, manifest_total):
    coc = next((x for x in d["cocs"] if x["coc_num"] == c.get("coc_num")), None)
    st = next((x for x in d["states"] if x["abbr"] == c.get("state")), None)
    orgs = q(CONN, """SELECT o.id,o.name,o.revenue,(SELECT SUM(amount_obligated) FROM award a WHERE a.org_id=o.id) amt
                      FROM org o WHERE o.city=? AND o.state=? ORDER BY COALESCE(amt,0)+COALESCE(o.revenue,0) DESC LIMIT 80""",
              (c["name"], c.get("state")))
    facs = q(CONN, "SELECT * FROM facility WHERE city=? AND state=? LIMIT 80", (c["name"], c.get("state")))
    orow = [[f'<a href="/org/{o["id"]}-{slug(o["name"])}.html">{esc(o["name"])}</a>', "", o["name"],
             money(o.get("amt")), "r", o.get("amt") or 0, money(o.get("revenue")), "r", o.get("revenue") or 0] for o in orgs]
    frow = [[f'<a href="/facility/{f["id"]}.html">{esc(f["name"])}</a>', "", f["name"],
             f'<span class="tag {esc(f.get("status") or "")}">{esc(f.get("status") or "?")}</span>', "", f.get("status") or "",
             num(f.get("beds")), "r", f.get("beds") or 0] for f in facs]
    body = banner(f'{c["name"]}, {c.get("state") or ""}', f'Population {num(c.get("pop"))}',
                  (f'Planned by <a href="/coc/{esc(coc["coc_num"])}.html">CoC {esc(coc["coc_num"])} — {esc(coc.get("name") or "")}</a>'
                   if coc else "No Continuum of Care matched to this place."),
                  crumbs=f'<a href="/index.html">Atlas</a> / <a href="/cities.html">Cities</a> / {esc(c["name"])}', small=True) + f"""
<div class="wrap">
  <div class="kpis">
    {kpi(num(c.get('pop')), "population")}
    {kpi(num((coc or {}).get('pit_total')), "CoC homeless count", f"{(coc or {}).get('pit_year') or '—'}", "b")}
    {kpi(num(len(orgs)), "organizations based here", "", "c")}
    {kpi(num(len(facs)), "institutions here", "", "d")}
  </div>
  <p class="dim" style="font-size:.84rem">HUD counts homelessness by Continuum of Care, not by city boundary. The city figure above is
  the CoC figure and includes surrounding areas. That limitation is in the source, not in this page.</p>
  <h2>Organizations based in {esc(c["name"])}</h2>
  {table([("Organization",""),("Federal $","r"),("Revenue","r")], orow)}
  <h2>Institutions</h2>
  {table([("Institution",""),("Status",""),("Beds","r")], frow)}
</div>"""
    return shell("../", f'{c["name"]}, {c.get("state") or ""}', "", body, manifest_total=manifest_total)


def page_org(d, o, manifest_total):
    awards = q(CONN, """SELECT * FROM award WHERE org_id=? ORDER BY amount_obligated DESC NULLS LAST LIMIT 500""", (o["id"],))
    people = q(CONN, """SELECT * FROM org_person WHERE org_id=? ORDER BY COALESCE(comp,0) DESC LIMIT 200""", (o["id"],))
    aw = [[esc((a.get("award_id") or "")[:40]), "", a.get("award_id") or "",
           esc((a.get("agency") or "")), "", a.get("agency") or "",
           esc((a.get("description") or "")[:120]), "", a.get("description") or "",
           money(a.get("amount_obligated")), "r", a.get("amount_obligated") or 0,
           str(a.get("pop_start") or "")[:10], "", a.get("pop_start") or ""] for a in awards]
    prow = [[f'<a href="/person/{person_slug(p)}.html">{esc(p["person"])}</a>', "", p["person"],
             esc(p.get("title") or ""), "", p.get("title") or "", num(p.get("comp")), "r", p.get("comp") or 0,
             num(p.get("year")), "r", p.get("year") or 0] for p in people]
    tot = sum(a.get("amount_obligated") or 0 for a in awards)
    body = banner((o.get("org_type") or "organization").replace("_", " "), esc(o.get("name")),
                  f'{esc(o.get("city") or "")} {esc(o.get("state") or "")}'
                  + (f' · EIN {esc(o["ein"])}' if o.get("ein") else "")
                  + (f' · UEI {esc(o["uei"])}' if o.get("uei") else ""),
                  crumbs=f'<a href="/index.html">Atlas</a> / <a href="/orgs.html">Organizations</a> / {esc((o.get("name") or "")[:60])}', small=True) + f"""
<div class="wrap">
  <div class="kpis">
    {kpi(money(o.get('revenue')), "revenue (990)")}
    {kpi(money(o.get('expenses')), "expenses (990)", "", "b")}
    {kpi(money(tot), f"federal awards ({len(awards)})", "", "c")}
    {kpi(num(o.get('employees')), "employees reported", "", "d")}
  </div>
  <div class="grid side">
    <div>
      <h2>Awards</h2>
      {table([("Award",""),("Agency",""),("Description",""),("Obligated","r"),("Start","")], aw)}
    </div>
    <div>
      <div class="panel"><h3>Facts</h3><dl class="facts">
        <dt>Type</dt><dd>{esc((o.get('org_type') or 'unknown'))}</dd>
        <dt>EIN</dt><dd>{esc(o.get('ein') or '—')}</dd>
        <dt>UEI</dt><dd>{esc(o.get('uei') or '—')}</dd>
        <dt>Address</dt><dd>{esc(' '.join(x for x in [o.get('address1'), o.get('city'), o.get('state'), o.get('zip')] if x) or '—')}</dd>
        <dt>Website</dt><dd>{(f'<a href="{esc(o["website"])}" rel="noopener">{esc(o["website"])}</a>' if o.get('website') else '—')}</dd>
        <dt>Email</dt><dd>{esc(o.get('email') or '—')}</dd>
        <dt>Phone</dt><dd>{esc(o.get('phone') or '—')}</dd>
        <dt>Founded</dt><dd>{num(o.get('founded'))}</dd>
        <dt>NTEE</dt><dd>{esc(o.get('ntee') or '—')}</dd>
        <dt>Fiscal year</dt><dd>{num(o.get('fiscal_year'))}</dd>
      </dl></div>
      <h2>People</h2>
      {table([("Person",""),("Title",""),("Comp","r"),("Year","r")], prow)}
    </div>
  </div>
</div>"""
    return shell("../", o.get("name") or "organization", "", body, manifest_total=manifest_total)


def page_person(d, p, manifest_total):
    others = q(CONN, """SELECT * FROM org_person WHERE person_norm=? AND org_name<>? LIMIT 50""",
               (p.get("person_norm"), p.get("org_name")))
    orow = [[esc(x.get("org_name") or ""), "", x.get("org_name") or "", esc(x.get("title") or ""), "", x.get("title") or "",
             num(x.get("year")), "r", x.get("year") or 0] for x in others]
    body = banner("published role", esc(p["person"]), esc(p.get("title") or ""),
                  crumbs='<a href="/index.html">Atlas</a> / <a href="/people.html">People</a>', small=True) + f"""
<div class="wrap wrap-narrow">
  <div class="panel"><dl class="facts">
    <dt>Organization</dt><dd>{f'<a href="/org/{p["org_id"]}-{slug(p.get("org_name") or "")}.html">{esc(p.get("org_name") or "")}</a>' if p.get("org_id") else esc(p.get('org_name') or '')}</dd>
    <dt>Role</dt><dd>{esc(p.get('title') or '—')}</dd>
    <dt>Compensation reported</dt><dd>{num(p.get('comp'))}</dd>
    <dt>Tax year</dt><dd>{num(p.get('year'))}</dd>
    <dt>Source</dt><dd>IRS Form 990, Part VII (public filing)</dd>
  </dl></div>
  <p class="dim" style="font-size:.84rem">Only what the public filing states: name, title and reportable compensation.
  No contact details for individuals are collected or published.</p>
  <h2>Other boards and roles</h2>
  {table([("Organization",""),("Title",""),("Year","r")], orow)}
</div>"""
    return shell("../", p["person"], "", body, manifest_total=manifest_total)


def page_facility(d, f, manifest_total):
    body = banner((f.get("kind") or "institution").replace("_", " "), esc(f["name"]),
                  f'{esc(f.get("city") or "")} {esc(f.get("state") or "")} · '
                  f'<span class="tag {esc(f.get("status") or "")}">{esc(f.get("status") or "?")}</span>',
                  crumbs=f'<a href="/index.html">Atlas</a> / <a href="/facilities.html">Institutions</a> / {esc((f.get("name") or "")[:50])}', small=True) + f"""
<div class="wrap wrap-narrow">
  <div class="kpis">
    {kpi(num(f.get('beds')), "beds")}
    {kpi(num(f.get('opened')), "opened")}
    {kpi(num(f.get('closed')), "closed", "", "b")}
    {kpi(money(f.get('budget')), "annual budget", f"{(money(f.get('annual_cost_per_bed')) + ' per bed') if f.get('annual_cost_per_bed') else ''}", "c")}
    {kpi(num(f.get('staff')), "staff", "", "d")}
  </div>
  <div class="panel"><dl class="facts">
    <dt>Kind</dt><dd>{esc((f.get('kind') or 'unknown').replace('_',' '))}</dd>
    <dt>Status</dt><dd>{esc(f.get('status') or 'unknown')}</dd>
    <dt>Operator</dt><dd>{esc(f.get('operator') or '—')}</dd>
    <dt>Owner</dt><dd>{esc(f.get('owner') or '—')}</dd>
    <dt>Address</dt><dd>{esc(f.get('address') or '—')}</dd>
    <dt>Coordinates</dt><dd>{(f"{f['lat']:.4f}, {f['lon']:.4f}" if f.get('lat') else '—')}</dd>
    <dt>Cost per bed-year</dt><dd>{money(f.get('annual_cost_per_bed'))}{est(True) if f.get('annual_cost_per_bed') and f.get('is_est') else ''}</dd>
    <dt>Population note</dt><dd>{esc(f.get('population_note') or '—')}</dd>
  </dl></div>
  <p><a href="/facilities.html">← all institutions</a></p>
</div>"""
    return shell("../", f["name"], "", body, manifest_total=manifest_total)

# ---------------------------------------------------------------- main
def main():
    global CONN
    if not DB.exists():
        print("no database"); return 1
    CONN = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    CONN.row_factory = sqlite3.Row
    d = load(CONN)
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "assets").mkdir(exist_ok=True)
    for f in ("atlas.css", "app.js"):
        shutil.copy(SRC / f, SITE / "assets" / f)

    counts = build_exports(d, CONN)
    total, manifest = build_index(d)
    (SITE / "data" / "manifest.json").write_text(json.dumps({"total": total, "at": NOW}))

    write("index.html", page_index(d, total))
    write("map.html", page_map(d, total))
    write("states.html", page_states(d, total))
    write("cities.html", page_cities(d, total))
    write("orgs.html", page_orgs(d, total))
    write("org/index.html", page_org_index(d, total))
    write("people.html", page_people(d, total))
    write("facilities.html", page_facilities(d, total))
    write("budgets.html", page_budgets(d, total))
    write("laws.html", page_laws(d, total))
    write("media.html", page_media(d, total))
    write("docs.html", page_docs(d, total))
    write("search.html", page_search(d, total))
    write("about.html", page_about(d, total))
    write("method.html", page_method(d, total))
    write("sources.html", page_sources(d, total))
    write("report/san-francisco.html", page_report_sf(d, total))

    n = 0
    for s in d["states"]:
        if s["abbr"]:
            write(f"state/{s['abbr']}.html", page_state(d, s, total)); n += 1
    for c in d["cocs"][:1200]:
        if c.get("coc_num"):
            write(f"coc/{c['coc_num']}.html", page_coc(d, c, total)); n += 1
    if not FAST:
        for c in d["cities"][:3000]:
            write(f"city/{c['geoid']}.html", page_city(d, c, total)); n += 1
        for o in d["top_orgs"][:ORG_PAGE_MAX]:
            write(f"org/{o['id']}-{slug(o['name'])}.html", page_org(d, o, total)); n += 1
        for p in d["people_top"][:PERSON_PAGE_MAX]:
            write(f"person/{person_slug(p)}.html", page_person(d, p, total)); n += 1
        for f in d["facilities"]:
            write(f"facility/{f['id']}.html", page_facility(d, f, total)); n += 1
    # favicon + robots
    write("robots.txt", "User-agent: *\nAllow: /\n")
    print(json.dumps({"pages": n + 16, "search_index": total, "geo": counts,
                      "rows": d["meta"]["counts"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
