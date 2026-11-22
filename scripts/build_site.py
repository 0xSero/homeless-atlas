#!/usr/bin/env python3
"""Generate the Homeless Atlas static site from data/atlas.db -> site/.

No build step, no server, no framework. Every page is self-contained HTML that links
/assets/atlas.css + /assets/app.js. Maps use MapLibre GL JS from a CDN with a keyless
CARTO basemap. Search is a sharded JSON index read by the client.

Run: python3 scripts/build_site.py [--fast]
"""
from __future__ import annotations
import hashlib, json, math, os, re, shutil, sqlite3, sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"
SITE = ROOT / "site"
STAGING = ROOT / "site.staging"
SRC = ROOT / "site_src"
GEO = ROOT / "data" / "geo"
FAST = "--fast" in sys.argv
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d")

CITY_PAGE_MIN_POP = 25_000
PERSON_OK = set()   # slugs that actually have a page
ORG_URL = {}        # org id -> the URL its page was actually written to
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
    if abs(v) < 1:
        return f"{int(round(v)):,}" if v == int(v) else f"{v:,.2f}"
    return f"{int(round(v)):,}"

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

def cite(source_id, dash="—"):
    """Inline link to the source row a figure came from."""
    if source_id is None:
        return dash
    r = one(CONN, "SELECT url, publisher, title FROM source WHERE id=?", (source_id,), {}) or {}
    url = r.get("url") or ""
    if not url.startswith("http"):
        return dash
    label = (r.get("publisher") or r.get("title") or "source")
    label = str(label).split("—")[0].strip()[:40]
    return f'<a href="{esc(url)}" rel="noopener">{esc(label)}</a>'


def yr(v, dash="—"):
    """Years print as 2025, never 2,025."""
    if v is None or v == "":
        return dash
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return str(v)


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
    ("/states.html", "States"), ("/cities.html", "Cities"), ("/orgs.html", "Orgs"),
    ("/people.html", "Officers"), ("/facilities.html", "Facilities"), ("/budgets.html", "Money"),
    ("/country/us.html", "Countries"), ("/rankings.html", "Rankings"), ("/moved.html", "What moved"), ("/costs.html", "Costs"), ("/laws.html", "Laws"), ("/media.html", "Media"), ("/docs.html", "Docs"),
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
<script>try{{var t=localStorage.getItem("atlas-theme");if(t==="dark")document.documentElement.dataset.theme="dark";}}catch(e){{}}</script>
<script src="{rel}assets/app.js"></script>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect rx='22' width='100' height='100' fill='%230b0e13'/><circle cx='50' cy='50' r='24' fill='%23ff7a45'/></svg>">
{head}
</head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="top"><div class="wrap">
  <div class="topbar">
    <a class="brand" href="{rel}index.html"><span class="dot"></span>Homeless&nbsp;Atlas</a>
    <form class="searchbox" role="search" onsubmit="return goSearch(event)">
      <span class="ico">⌕</span>
      <input type="search" id="gq" placeholder="Search counts, orgs, people, laws…" aria-label="Search" autocomplete="off">
      <div class="ac" role="listbox"></div>
    </form>
    <button class="themebtn" id="themebtn" aria-label="Switch between light and dark" title="Light / dark">◐</button>
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
<script>
function goSearch(e){{e.preventDefault();var v=document.getElementById('gq').value.trim();if(v)location.href='{rel}search.html?q='+encodeURIComponent(v);return false;}}
(function(){{
  var b=document.getElementById('themebtn');
  if(!b)return;
  b.addEventListener('click',function(){{
    var on=document.documentElement.dataset.theme==='dark';
    if(on){{delete document.documentElement.dataset.theme;}}else{{document.documentElement.dataset.theme='dark';}}
    try{{localStorage.setItem('atlas-theme',on?'light':'dark');}}catch(e){{}}
    /* maps choose their basemap and colour ramp at load, so reload to match */
    if(document.getElementById('map')||document.querySelector('.map'))location.reload();
  }});
}})();
</script>
{head and ''}
</body></html>"""

def spark_svg(series, key="total", w=100, h=34, color="var(--ember)"):
    """SVG sparkline from [{year,total},...], with the first and last value labelled.
    Returns '' when there is nothing to draw."""
    pts = [r.get(key) for r in series]
    clean = [p for p in pts if p is not None]
    if len(clean) < 3 or (max(clean) or 0) <= 0:
        return ""
    lo, mx = min(clean), max(clean)
    rng = (mx - lo) or 1
    xy = " ".join(f"{i/(len(pts)-1)*w:.1f},{h-2-(((p if p is not None else lo)-lo)/rng*(h-4)):.1f}"
                  for i, p in enumerate(pts))
    y0, y1 = series[0].get("year"), series[-1].get("year")
    v0, v1 = series[0].get(key), series[-1].get(key)
    return (f'<figure class="sparkfig"><svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'style="width:100%;height:{h + 14}px" role="img" '
            f'aria-label="trend {y0} to {y1}">'
            f'<polyline points="{xy}" fill="none" stroke="{color}" stroke-width="1.8" '
            f'vector-effect="non-scaling-stroke"/></svg>'
            f'<figcaption><span>{esc(str(y0))} · <b>{num(v0)}</b></span>'
            f'<span>{esc(str(y1))} · <b>{num(v1)}</b></span></figcaption></figure>')


def trend(series, key="total"):
    """(first, latest, absolute change, percent change, years) or Nones."""
    vals = [(r.get("year"), r.get(key)) for r in series if r.get(key) is not None]
    if len(vals) < 2:
        return None
    (y0, v0), (y1, v1) = vals[0], vals[-1]
    if not v0:
        return None
    return {"y0": y0, "v0": v0, "y1": y1, "v1": v1, "abs": v1 - v0, "pct": (v1 - v0) / v0 * 100}


def trend_badge(t):
    if not t:
        return '<span class="dim">no series</span>'
    up = t["pct"] > 0.5
    down = t["pct"] < -0.5
    arrow = "▲" if up else ("▼" if down else "■")
    colour = "var(--red)" if up else ("var(--teal)" if down else "var(--fg2)")
    return (f'<span class="mono" style="color:{colour}">{arrow} {t["pct"]:+.1f}%</span> '
            f'<span class="dim" style="font-size:.8em">{t["y0"]}→{t["y1"]}</span>')


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
        # markdown links must be consumed before bare URLs, or the URL inside the
        # parentheses gets linkified first and the [label](url) form never matches
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" rel="noopener">\1</a>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*", r"<em>\1</em>", s)

        def _bare(m):
            url, trail = m.group(1), ""
            while url and url[-1] in ".,;:":
                trail, url = url[-1] + trail, url[:-1]
            return f'<a href="{url}" rel="noopener">{url}</a>{trail}'

        s = re.sub(r'(?<!["\'=>])(https?://[^\s<)\]]+)', _bare, s)
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
        # HUD publishes SMI/SUD per CoC only in PDFs; the national series lives in metric
        d["nat_smi"] = (one(conn, """SELECT value, year FROM metric WHERE entity_type='us' AND metric='pit_smi'
                                     ORDER BY year DESC LIMIT 1""", (), {}) or {}).get("value")
        d["nat_sud"] = (one(conn, """SELECT value, year FROM metric WHERE entity_type='us' AND metric='pit_sud'
                                     ORDER BY year DESC LIMIT 1""", (), {}) or {}).get("value")
        d["nat_chronic"] = (one(conn, """SELECT value, year FROM metric WHERE entity_type='us' AND metric='pit_chronic'
                                     ORDER BY year DESC LIMIT 1""", (), {}) or {}).get("value")
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
    d["top_orgs"] = q(conn, """SELECT o.id, o.name, o.state, o.city, o.ein, o.revenue, o.expenses, o.website, o.email,
                                      COUNT(a.id) n_awards, SUM(a.amount_obligated) amt, COUNT(p.id) people
                               FROM org o
                               LEFT JOIN award a ON a.org_id=o.id
                               LEFT JOIN org_person p ON p.org_id=o.id
                               GROUP BY o.id
                               ORDER BY COALESCE(amt,0)+COALESCE(o.revenue,0) DESC LIMIT 40000""")
    d["people_all"] = q(conn, """SELECT p.id, p.org_id, p.org_name, p.person, p.person_norm, p.title, p.comp, p.year,
                                        o.state, o.website
                                 FROM org_person p LEFT JOIN org o ON o.id=p.org_id
                                 WHERE p.person IS NOT NULL
                                 ORDER BY COALESCE(p.comp,0) DESC, p.person LIMIT 200000""")
    d["people_top"] = d["people_all"][:PERSON_PAGE_MAX]
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
            feats.append({"type": "Feature", "id": abbr, "properties": p2, "geometry": f.get("geometry")})
    (SITE / "data" / "map-states.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    # per-year rates so the map can be scrubbed through time (growth and fall, state by state)
    yrs = [r["year"] for r in q(CONN, "SELECT DISTINCT year FROM pit WHERE total IS NOT NULL ORDER BY year")]
    by_year = {}
    for r in q(CONN, """SELECT c.state abbr, p.year, SUM(p.total) total
                        FROM pit p JOIN geo_coc c ON c.coc_num=p.coc_num
                        WHERE c.state IS NOT NULL AND p.total IS NOT NULL GROUP BY c.state, p.year"""):
        st_ = next((x for x in d["states"] if x["abbr"] == r["abbr"]), None)
        pop = (st_ or {}).get("pop")
        if pop:
            by_year.setdefault(str(r["year"]), {})[r["abbr"]] = round(r["total"] / pop * 10000, 3)
    # country layer: the US is populated from HUD; the other three are declared and marked as pending
    # until their ingestors land, so the map never invents a number
    countries = []
    us_row = one(CONN, """SELECT value, year FROM metric WHERE entity_type='us' AND metric='pit_total'
                          ORDER BY year DESC LIMIT 1""", (), {}) or {}
    uk_row = one(CONN, """SELECT value, year FROM metric WHERE entity_type='uk' AND metric='rough_sleeping'
                          AND note LIKE '%England%' ORDER BY year DESC LIMIT 1""", (), {}) or {}
    uk_wales = one(CONN, """SELECT value, year FROM metric WHERE entity_type='uk' AND metric='rough_sleeping'
                            AND note LIKE '%Wales%' ORDER BY year DESC LIMIT 1""", (), {}) or {}
    ca_row = one(CONN, """SELECT value, year FROM metric WHERE entity_type='canada' AND metric='pit_total'
                          ORDER BY year DESC LIMIT 1""", (), {}) or {}
    au_row = one(CONN, """SELECT value, year FROM metric WHERE entity_type='australia'
                          AND metric='census_homeless_estimate' ORDER BY year DESC LIMIT 1""", (), {}) or {}
    for code, name, lat, lon, row, label in (
            ("US", "United States", 39.5, -98.35, us_row, "point-in-time count"),
            ("CA", "Canada", 56.1, -106.3, ca_row, "point-in-time count"),
            ("GB", "United Kingdom", 54.2, -2.9, uk_row, "England rough sleeping snapshot"),
            ("AU", "Australia", -25.3, 133.8, au_row, "census estimate")):
        countries.append({"code": code, "name": name, "lat": lat, "lon": lon,
                          "value": row.get("value"), "year": row.get("year"), "label": label,
                          "pending": row.get("value") is None})
    (SITE / "data" / "map-countries.json").write_text(json.dumps(
        {"countries": countries, "uk_wales": uk_wales}))

    (SITE / "data" / "map-years.json").write_text(json.dumps(
        {"years": yrs, "rates": by_year, "unit": "homeless per 10,000 residents (HUD PIT / Census)"}))

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
    seen = set()

    def add(t, title, sub, url):
        # the same person can appear several times (one row per tax year); keep the first, which is
        # the highest-compensation row because people_all is ordered by compensation
        key = (t, url)
        if key in seen:
            return
        seen.add(key)
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
    for p in d["people_all"]:
        sl = person_slug(p)
        url = f"/person/{sl}.html" if sl in PERSON_OK else ORG_URL.get(p.get("org_id"), "/people.html")
        add("person", p["person"], f"{(p.get('title') or '').strip()} — {p.get('org_name') or ''}", url)
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

    # Shard by the first letter of EVERY significant word in the title and subtitle, so a
    # substring search like "housing clinic" finds "Tenderloin Housing Clinic" — sharding by the
    # first character of the title alone silently missed every multi-word query.
    shards = defaultdict(list)
    for row in idx:
        keys = set()
        for field in (row[1], row[2]):
            for tok in re.findall(r"[a-z0-9]+", (field or "").lower()):
                if len(tok) >= 2 or tok.isdigit():
                    keys.add(tok[0])
        if not keys:
            keys.add("_")
        for k in keys:
            bucket = int(hashlib.md5((row[1] or "").encode()).hexdigest(), 16) % 10
            shards[f"{k}-{bucket}"].append(row)
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
    """One page per person *per organisation*.

    Including the tax year produced three near-identical pages for one officer; including only the
    name would merge different people who share a name. Person + organisation is the right key.
    """
    h = hashlib.md5(((p.get("person_norm") or "") + "|" + (p.get("org_name") or "")).encode()).hexdigest()[:7]
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
        kpi(num(d.get("nat_smi")), "seriously mentally ill adults", "HUD count, same night, national", "d"),
        kpi(num(d.get("nat_sud")), "adults with substance use disorder", "HUD count, same night, national"),
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
        line = spark_svg([dict(r) for r in series], h=86)
        if line:
            spark = (f'<div class="panel"><h3>National count, {series[0]["year"]}–{series[-1]["year"]}</h3>'
                     f'{line}<div class="legend"><span class="dim">each point is the CoC-summed count for '
                     f'that January</span></div></div>')
    body = banner("Public record · United States",
                  "Every count, contract, organization and institution in American homelessness",
                  "HUD point-in-time counts by Continuum of Care, federal awards down to the recipient, "
                  "IRS filings, closed and open institutions, and the law in each jurisdiction — searchable.") + f"""
<div class="wrap">
  <div class="kpis">{kpis}</div>
  <div class="grid side">
    <div>
      <h2>Homelessness per 10,000 residents, by state</h2>
      <div class="map" id="map"></div>
      <div class="legend">
        <span><i style="background:#EFE6D6"></i>0–2</span><span><i style="background:#D2A576"></i>2–8</span>
        <span><i style="background:#BD8250"></i>8–20</span><span><i style="background:#8A3A1E"></i>20–45</span>
        <span><i style="background:#6A2410"></i>45+</span>
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
          <li><a href="/report/san-francisco.html">San Francisco brief</a> · <a href="/report/los-angeles.html">Los Angeles</a> · <a href="/report/new-york.html">New York</a></li>
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
<script src="/assets/maplibre-gl.js"></script>
<link href="/assets/maplibre-gl.css" rel="stylesheet">
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
  <div class="layertoggle" id="ctry" style="margin:14px 0">
    <button data-c="US" class="on">United States</button>
    <button data-c="CA">Canada</button>
    <button data-c="GB">United Kingdom</button>
    <button data-c="AU">Australia</button>
    <button data-c="world">World view</button>
  </div>
  <div class="panel" id="ctrybox" style="margin:0 0 14px;padding:12px 14px"></div>
  <div class="panel" style="margin:16px 0;padding:12px 14px">
    <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <label for="yr" style="font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--fg2)">Year</label>
      <input id="yr" type="range" min="2007" max="2025" step="1" value="2025" style="flex:1;min-width:220px;accent-color:var(--ember)">
      <output id="yrout" class="mono" style="font-size:1.05rem;color:var(--amber)">2025</output>
    </div>
    <div class="legend" id="yrlegend" style="margin-top:8px"><span class="dim">Scrub through the HUD counts: the colour of each state is its homeless rate that January.</span></div>
  </div>
  <div class="layertoggle" id="lt">
    <button data-l="choro" class="on">State heat</button>
    <button data-l="coc" class="on">CoC areas</button>
    <button data-l="cities">Cities</button>
    <button data-l="fac">Institutions</button>
  </div>
  <div class="map tall" id="map"></div>
  <div class="legend">
    <span><i style="background:#EFE6D6"></i>low</span><span><i style="background:#6A2410"></i>high</span>
    <span><i style="background:#2ee6c5"></i>institution open</span><span><i style="background:#ff5d5d"></i>closed</span>
    <span class="dim">Data: HUD PIT/AHAR, Census, Wikidata. Click a feature.</span>
  </div>
</div>
<script src="/assets/maplibre-gl.js"></script>
<link href="/assets/maplibre-gl.css" rel="stylesheet">
<script>
var m=atlasMap.base('map',{zoom:3.2});
window.__atlasMap=m;
var COUNTRIES=null;
function countryBox(code){
  if(!COUNTRIES){return;}
  var box=document.getElementById('ctrybox'); if(!box) return;
  var c=COUNTRIES.countries.filter(function(x){return x.code===code;})[0];
  if(code==='world'){
    box.innerHTML='<b>World view</b><br><span class="dim">'+COUNTRIES.countries.map(function(x){
      return x.name+': '+(x.pending?'not ingested yet':(x.value||0).toLocaleString()+' ('+x.label+', '+x.year+')');
    }).join(' · ')+'</span>';
    m.flyTo({center:[-20,25],zoom:1.2});
    return;
  }
  if(!c){box.innerHTML='';return;}
  if(c.pending){
    box.innerHTML='<b>'+c.name+'</b> — <span class="dim">no '+c.label+' ingested yet. The queue item for this country '+
      'is open; nothing is drawn rather than showing an empty or estimated map.</span>';
    m.flyTo({center:[c.lon,c.lat],zoom:3});
    return;
  }
  box.innerHTML='<b>'+c.name+'</b> — '+(c.value||0).toLocaleString()+' people ('+c.label+', '+c.year+')'+
    (c.code==='GB'&&COUNTRIES.uk_wales&&COUNTRIES.uk_wales.value
      ? ' · <span class="dim">Wales monthly rough sleeping: '+(COUNTRIES.uk_wales.value||0).toLocaleString()+
        ' ('+COUNTRIES.uk_wales.year+'). England and Wales publish different measures and are never added together here.</span>'
      : '');
  m.flyTo({center:[c.lon,c.lat],zoom:3.2});
}
var loaded={};
function loadLayer(name){
  if(loaded[name]) return loaded[name];
  if(name==='choro') loaded[name]=fetch('/data/map-states.geojson').then(r=>r.json()).then(g=>{atlasMap.choro(m,g,'st');});
  if(name==='coc') loaded[name]=fetch('/data/map-cocs.geojson').then(r=>r.json()).then(g=>{
     m.addSource('coc',{type:'geojson',data:g});
     m.addLayer({id:'coc-l',type:'line',source:'coc',paint:{'line-color':'#7fc4ff','line-width':0.6,'line-opacity':.28}});
     var pop=new maplibregl.Popup({maxWidth:'300px'});
     m.on('click','coc-l',e=>{var p=e.features[0].properties;pop.setLngLat(e.lngLat).setHTML(p.__html||p.name).addTo(m);});
  });
  if(name==='cities') loaded[name]=fetch('/data/map-places.geojson').then(r=>r.json()).then(g=>atlasMap.pops(m,g,'cities','rate','name'));
  if(name==='fac') loaded[name]=fetch('/data/map-facilities.geojson').then(r=>r.json()).then(g=>{
     m.addSource('fac',{type:'geojson',data:g});
     m.addLayer({id:'fac',type:'circle',source:'fac',paint:{
       'circle-radius':['interpolate',['linear'],['sqrt',['coalesce',['get','beds'],4]],1,3,20,12],
       'circle-color':['match',['get','status'],'closed','#A63A1B','open','#2C6B62','#8F6B0E'],
       'circle-opacity':.88,'circle-stroke-color':'#FCFBF7','circle-stroke-width':.7}});
     var pop=new maplibregl.Popup({maxWidth:'300px'});
     m.on('click','fac',e=>{var p=e.features[0].properties;pop.setLngLat(e.features[0].geometry.coordinates).setHTML(p.__html||p.name).addTo(m);});
  });
  return loaded[name];
}
function colorExpr(){ return ['interpolate',['linear'],['coalesce',['feature-state','rate'],['get','rate'],0],
    0,'#EFE6D6',3,'#E4CDA9',8,'#D2A576',15,'#BD8250',28,'#A75832',45,'#8A3A1E',70,'#6A2410']; }
var YR=null, current=null;
function apply(y){
  if(!YR || !m.getSource('st')) return;
  var rates=YR.rates[String(y)]||{};
  Object.keys(rates).forEach(function(ab){ m.setFeatureState({source:'st',id:ab},{rate:rates[ab]}); });
  if(m.getLayer('st-fill')){
    m.setPaintProperty('st-fill','fill-color',colorExpr());
    m.setPaintProperty('st-fill','fill-opacity',0.92);
  }
  current=String(y);
  window.__atlasYear=current; window.__atlasRates=rates;
  var out=document.getElementById('yrout'); if(out) out.textContent=current;
  var lg=document.getElementById('yrlegend');
  if(lg) lg.innerHTML='<span class="dim">'+current+' · '+Object.keys(rates).length+' states reporting · deep red = 70 or more per 10,000</span>';
}
m.on('load',function(){
  loadLayer('coc'); loadLayer('fac').then(function(){
    if(m.getLayer('fac')) m.setLayoutProperty('fac','visibility','none');
  });
  loadLayer('choro').then(function(){
    return fetch('/data/map-years.json').then(r=>r.json());
  }).then(function(d){
    YR=d;
    var inp=document.getElementById('yr');
    var ys=d.years.filter(Boolean);
    inp.min=Math.min.apply(null,ys); inp.max=Math.max.apply(null,ys); inp.value=inp.max;
    apply(inp.value);
    inp.addEventListener('input',function(){ apply(inp.value); });
  });
});
fetch('/data/map-countries.json').then(function(r){return r.json();}).then(function(d){
  COUNTRIES=d;
  document.querySelectorAll('#ctry button').forEach(function(b){
    b.addEventListener('click',function(){
      document.querySelectorAll('#ctry button').forEach(function(x){x.classList.remove('on');});
      b.classList.add('on');
      countryBox(b.dataset.c);
    });
  });
  countryBox('US');
});
document.querySelectorAll('#lt button').forEach(function(b){
  b.addEventListener('click',function(){
    var l=b.dataset.l,on=b.classList.toggle('on');
    loadLayer(l).then(function(){
      var ids={choro:['st-fill','st-line'],coc:['coc-l'],cities:['cities'],fac:['fac']}[l]||[];
      ids.forEach(function(id){ if(m.getLayer(id)) m.setLayoutProperty(id,'visibility',on?'visible':'none'); });
      if(l==='choro'&&on) apply(current||inp_max());
    });
  });
});
function inp_max(){ return document.getElementById('yr').max; }
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
                     (f'<a href="{ORG_URL[p["org_id"]]}">{esc(p.get("org_name") or "")}</a>'
                      if ORG_URL.get(p.get("org_id")) else esc(p.get("org_name") or "")), "", p.get("org_name") or "",
                     esc(p.get("state") or ""), "", p.get("state") or "",
                     num(p.get("comp")), "r", p.get("comp") or 0, yr(p.get("year")), "r", p.get("year") or 0])
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
    for f in facs:
        rows.append([f'<a href="/facility/{f["id"]}.html">{esc(f["name"])}</a>', "", f["name"],
                     esc((f.get("kind") or "").replace("_", " ")), "", f.get("kind") or "",
                     f'<span class="tag {esc(f.get("status") or "")}">{esc(f.get("status") or "?")}</span>', "", f.get("status") or "",
                     esc(f.get("city") or ""), "", f.get("city") or "", esc(f.get("state") or ""), "", f.get("state") or "",
                     num(f.get("beds")), "r", f.get("beds") or 0, yr(f.get("opened")), "r", f.get("opened") or 0,
                     yr(f.get("closed")), "r", f.get("closed") or 0])
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
  <span><i style="background:#8F6B0E"></i>other/unknown</span></div>
  <h2>All institutions</h2>
  {table([("Institution",""),("Kind",""),("Status",""),("City",""),("State",""),("Beds","r"),("Opened","r"),("Closed","r")], rows)}
</div>
<script src="/assets/maplibre-gl.js"></script>
<link href="/assets/maplibre-gl.css" rel="stylesheet">
<script>
var m=atlasMap.base('map',{{zoom:3.1}});
m.on('load',function(){{
 fetch('/data/map-facilities.geojson').then(r=>r.json()).then(function(g){{
   m.addSource('fac',{{type:'geojson',data:g}});
   m.addLayer({{id:'fac',type:'circle',source:'fac',paint:{{
     'circle-radius':['interpolate',['linear'],['sqrt',['coalesce',['get','beds'],4]],1,3,20,13],
     'circle-color':['match',['get','status'],'closed','#A63A1B','open','#2C6B62','#8F6B0E'],
     'circle-opacity':.88,'circle-stroke-color':'#FCFBF7','circle-stroke-width':.7}}}});
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


def page_rankings(d, manifest_total):
    """Leaderboards that are actually comparable.

    HUD publishes counts per Continuum of Care, never per city limit, and a CoC's number is inherited by
    every city inside it — so ranking cities by count produces dozens of ties (every Los Angeles County
    city "had" 67,777 homeless people in 2025). We therefore rank CoCs for counts and rates, and rank
    cities only where a city genuinely publishes its own figure: budgets and overdose deaths.
    """
    # ---- CoCs by count and rate (published numbers)
    coc_rows = []
    for c in d["cocs"]:
        if not c.get("pit_total"):
            continue
        pop = c.get("pop")
        coc_rows.append({"coc": c["coc_num"], "name": c.get("name") or "", "state": c.get("state") or "",
                         "total": c["pit_total"], "year": c.get("pit_year"),
                         "pop": pop, "rate": (c["pit_total"] / pop * 10000) if pop else None})
    by_count = sorted(coc_rows, key=lambda r: -r["total"])[:100]
    by_rate = sorted([r for r in coc_rows if r["rate"]], key=lambda r: -r["rate"])[:100]

    def coc_cell(r):
        return f'<a href="coc/{esc(r["coc"])}.html">{esc(r["coc"])}</a> {esc(r["name"])[:60]}'

    crow = [[coc_cell(r), "", r["coc"], esc(r["state"]), "", r["state"],
             num(r["total"]), "r", r["total"], yr(r["year"]), "", r["year"] or 0] for r in by_count]
    rrow = [[coc_cell(r), "", r["coc"], esc(r["state"]), "", r["state"],
             num(r["pop"]), "r", r["pop"] or 0, f"{r['rate']:.1f}", "r", r["rate"]] for r in by_rate]

    # ---- cities with their own published money and harm figures (entity_id is the place GEOID)
    def city_metric(metric, limit=60):
        rows = q(CONN, """SELECT m.entity_id geoid, m.value, m.year, m.is_est, m.note,
                                 g.name, g.state, g.pop
                          FROM metric m LEFT JOIN geo_city g ON g.geoid = m.entity_id
                          WHERE m.entity_type='city' AND m.metric=? AND m.value IS NOT NULL
                          ORDER BY m.value DESC LIMIT ?""", (metric, limit))
        return rows

    bud = city_metric("city_homelessness_budget")
    od = city_metric("overdose_deaths")

    def ccell(r):
        if r.get("name"):
            return f'<a href="city/{esc(r["geoid"])}.html">{esc(r["name"])}, {esc(r["state"] or "")}</a>'
        return esc(str(r["geoid"]))

    brow = [[ccell(r), "", str(r["geoid"]), num(r["pop"]), "r", r["pop"] or 0,
             money(r["value"]), "r", r["value"], yr(r["year"]), "", r["year"] or 0] for r in bud]
    orow = [[ccell(r), "", str(r["geoid"]), num(r["pop"]), "r", r["pop"] or 0,
             num(r["value"]), "r", r["value"], yr(r["year"]), "", r["year"] or 0] for r in od]

    # spend per homeless person: city budget ÷ the count of the CoC that plans it (derived)
    spend = []
    for r in bud:
        coc = next((c for c in d["cocs"] if c.get("coc_num") and r.get("state")
                    and (c["coc_num"] or "").startswith(r["state"])
                    and (c.get("name") or "").lower().startswith((r.get("name") or "x").lower()[:6])), None)
        if coc and coc.get("pit_total"):
            spend.append({"name": r.get("name"), "state": r.get("state"), "geoid": r["geoid"],
                          "budget": r["value"], "count": coc["pit_total"], "coc": coc["coc_num"],
                          "per": r["value"] / coc["pit_total"]})
    spend.sort(key=lambda x: -x["per"])
    srow = [[f'<a href="city/{esc(x["geoid"])}.html">{esc(x["name"])}, {esc(x["state"] or "")}</a>', "", x["name"],
             money(x["budget"]), "r", x["budget"], num(x["count"]), "r", x["count"],
             money(x["per"]) + "†", "r", x["per"]] for x in spend]

    body = banner("Rankings", "Ranked, with the caveats built in",
                  "Counts are ranked by Continuum of Care because that is how HUD publishes them; cities are ranked "
                  "only where a city publishes its own money or death figures. Every derived column carries a †.") + f"""
<div class="wrap">
  <h2>Continuums of Care with the most people homeless</h2>
  {table([("CoC",""),("State",""),("Homeless","r"),("Year","")], crow,
         note="Published HUD point-in-time totals. A CoC is the regional planning body, so this is the only "
              "geography for which an official count exists.")}

  <h2>Highest rate per 10,000 residents</h2>
  {table([("CoC",""),("State",""),("Population","r"),("Per 10,000†","r")], rrow,
         note="† CoC count ÷ CoC population × 10,000. CoC population is a Census county aggregate and is missing "
              "for 29 CoCs that split a county, which is why they do not appear here.")}

  <h2>Cities with the largest homelessness budgets</h2>
  {table([("City",""),("Population","r"),("Budget","r"),("Year","")], brow,
         note="From each city's own adopted budget. This is city money, not the federal awards shown on Money.")}

  <h2>City spend per homeless resident</h2>
  {table([("City",""),("Budget","r"),("Homeless (CoC)","r"),("$ per person†","r")], srow,
         note="† city budget ÷ the point-in-time count of the CoC that plans that city. It mixes a city's money with a "
              "regional count and is therefore an order-of-magnitude comparison, not an accounting figure.")}

  <h2>Overdose deaths by city</h2>
  {table([("City",""),("Population","r"),("Deaths","r"),("Year","")], orow,
         note="Where a city or county publishes medical-examiner counts. Cause of death, not an attribution to "
              "homeless people.")}

{_spend_block()}
</div>"""
    return shell("", "Rankings", "rankings.html", body, manifest_total=manifest_total)


def _spend_block():
    """Federal dollars next to outcomes, per CoC — the caveats are the point, not a footnote.

    Both series come from scripts/build_outcome_metrics.py (metric rows written with is_est=1) and
    spend_vs_outcome_spend (the attributed federal obligation per CoC-year).
    """
    def series(metric, denom_metric, min_denom):
        latest = one(CONN, """SELECT MAX(year) y FROM metric WHERE entity_type='coc' AND metric=?""",
                     (metric,), default={}) or {}
        y = latest.get("y")
        rows = q(CONN, """SELECT m.entity_id coc, m.value per, m.year, m.note, g.name, g.state,
                                 s.obligated, s.awards, d.value denom
                          FROM metric m
                          JOIN geo_coc g ON g.coc_num = m.entity_id
                          LEFT JOIN spend_vs_outcome_spend s ON s.coc_num = m.entity_id AND s.fy = m.year
                          LEFT JOIN metric d ON d.entity_type='coc' AND d.entity_id = m.entity_id
                               AND d.metric = ? AND d.year = m.year
                          WHERE m.entity_type='coc' AND m.metric = ? AND m.value IS NOT NULL
                            AND m.year = ? AND COALESCE(d.value, 0) >= ?""",
                   (denom_metric, metric, y, min_denom))
        return y, rows

    def rows_for(rs, denom_head):
        out = []
        for r in rs:
            coc = f'<a href="coc/{esc(r["coc"])}.html">{esc(r["coc"])}</a> {esc((r.get("name") or "")[:54])}'
            out.append([coc, "", r["coc"], esc(r.get("state") or ""), "", r.get("state") or "",
                        money(r.get("obligated")), "r", r.get("obligated") or 0,
                        num(r.get("denom")), "r", r.get("denom") or 0,
                        money(r["per"]) + est(True), "r", r["per"], yr(r["year"]), "", r["year"] or 0])
        return out

    y, sp = series("spend_per_exit_to_housing", "spm_exit_to_ph_count", 50)
    if not sp:
        return ""
    sp.sort(key=lambda r: r["per"])
    head = [("CoC", ""), ("State", ""), ("Federal $", "r"), ("Exits to housing", "r"),
            ("$ per exit†", "r"), ("Year", "r")]
    lo, hi = rows_for(sp[:25], "Exits"), rows_for(sp[-25:][::-1], "Exits")
    n_all = len(sp)

    py, pp = series("spend_per_person_placed", "spend_per_person_placed", 1)
    pp = [r for r in pp if r.get("denom")]
    head_person = [("CoC", ""), ("State", ""), ("Federal $", "r"), ("Net decline", "r"),
                   ("$ per net person†", "r"), ("Year", "r")]
    py_all = rows_for(sorted(pp, key=lambda r: r["per"])[:15], "Net decline") if pp else []
    py_hi = rows_for(sorted(pp, key=lambda r: r["per"])[-15:][::-1], "Net decline") if pp else []

    block = f"""
  <h2>Most and least spent per exit to housing</h2>
  <p class="dim" style="max-width:70ch">Federal dollars obligated against the exits to permanent housing that HUD's
  System Performance Measures record for the same CoC and fiscal year, FY{esc(y)}. {n_all} CoCs have both
  inputs and at least 50 exits. <strong>A low number is not evidence of efficiency.</strong> It can equally be an
  accounting artefact: dollars booked to a neighbouring county, awards dated in a different fiscal year, a prime
  award whose subawards carry the service money, or a place of performance recorded at a headquarters address.
  <strong>CoC spend cannot be separated from city and county spend</strong> — the same county's police, hospital and
  general-fund money is invisible here, and a CoC is a planning body, not a spending unit. This table is a
  question to ask a CoC, not a verdict on it.</p>
  {table(head, lo, note="Least federal dollars per exit to permanent housing, FY" + esc(y) + ". † federal obligation with place of performance inside the CoC (USASpending award records, award type grant/contract, subawards excluded) ÷ SPM exits to permanent housing. FY is the fiscal year of the award's base obligation date.")}
  <h3>The other end of the same ranking</h3>
  {table(head, hi, note="Most federal dollars per exit, same definition. A high figure here usually means the county hosts a federal grantee whose money is booked locally, not that the CoC is profligate.")}

  {('<h2>Most and least spent per net person placed</h2>'
    '<p class="dim" style="max-width:70ch">Federal obligation ÷ the net fall in the point-in-time count between '
    'FY' + esc(py) + ' and FY' + esc(int(py) + 1) + '. That denominator is a net change in a census-style count, '
    'not a count of placements: people arriving into homelessness are netted off it, and any CoC whose count rose '
    'is dropped rather than given a negative cost. Far fewer CoCs qualify than in the table above, so read this as '
    'a cross-check, not a league table.</p>'
    + table(head_person, py_all,
            note="Cheapest per net person placed. A low figure here is as much about inflow as about placement: a CoC whose count fell sharply looks cheap whether or not it housed anybody.")
    + table(head_person, py_hi, note="Most expensive per net person placed.")) if py_all else ""}
"""
    return block


COUNTRIES = [("us", "United States", "US", "point-in-time count"),
             ("uk", "United Kingdom", "GB", "rough sleeping snapshot"),
             ("canada", "Canada", "CA", "point-in-time count"),
             ("australia", "Australia", "AU", "census estimate")]


def page_country(country, manifest_total):
    """A country page driven entirely by whatever metrics that country's ingestor produced."""
    etype, name, code, label = country
    nation = q(CONN, """SELECT entity_id, metric, value, unit, year, is_est, note, source_id FROM metric
                        WHERE entity_type=? ORDER BY entity_id, year DESC, metric""", (etype,))
    sub_types = {"uk": ("uk_la",), "canada": ("ca_city",), "australia": ("au_state", "au_lga")}.get(etype, ())
    subs = []
    for st in (etype,):
        pass
    for st in sub_types:
        subs += q(CONN, """SELECT entity_type, entity_id, metric, value, unit, year, is_est, source_id
                           FROM metric WHERE entity_type=? AND value IS NOT NULL
                           ORDER BY year DESC, value DESC LIMIT 400""", (st,))
    if not nation and not subs:
        body = banner(name, f"{name} — not yet ingested",
                      "Nothing is shown rather than an empty or estimated figure. The queue item for this "
                      "country is open; this page fills in when its ingestor lands.",
                      crumbs=f'<a href="/index.html">Atlas</a> / {name}', small=True) + \
            '<div class="wrap wrap-narrow" style="margin-top:20px"><p class="dim">' \
            'See <a href="/method.html">Method</a> for how the US figures are produced, and ' \
            '<a href="/sources.html">Sources</a> for the datasets already in use.</p></div>'
        return shell("../", name, "", body, manifest_total=manifest_total)

    kpis = ""
    if nation:
        latest = {}
        for r in nation:
            latest.setdefault(r["metric"], r)
        top = [("rough_sleeping", "people rough sleeping"), ("pit_total", "people homeless"),
               ("census_homeless_estimate", "people estimated homeless"),
               ("statutory_homeless_households", "households owed a duty"),
               ("shs_clients", "specialist homelessness clients")]
        if etype == "uk":
            # England snapshot and the Wales monthly count are different measures; never mix them
            eng = one(CONN, """SELECT value, year FROM metric WHERE entity_type='uk' AND metric='rough_sleeping'
                               AND note LIKE '%England%' ORDER BY year DESC LIMIT 1""", (), {}) or {}
            if eng.get("value"):
                kpis += kpi(num(eng["value"]), "people rough sleeping (England snapshot)", f"{eng['year']}", "")
        for key, lbl in top:
            if key in latest and not (etype == "uk" and key == "rough_sleeping"):
                note = (latest[key].get("note") or "").split("(")[0].strip()
                cap = note[:60] if note else f"{latest[key]['year']}"
                kpis += kpi(num(latest[key]["value"]) + ("†" if latest[key]["is_est"] else ""), lbl, cap, "")
    nrow = [[esc(str(r.get("entity_id") or "")), "", str(r.get("entity_id") or ""),
             esc((r["metric"] or "").replace("_", " ")), "", r["metric"] or "",
             num(r["value"]) + ("†" if r["is_est"] else ""), "r", r["value"] or 0,
             esc(r.get("unit") or ""), "", r["unit"] or "", yr(r["year"]), "", r["year"] or 0,
             cite(r.get("source_id"), "—"), "", ""] for r in nation[:120]]
    srow = [[esc(r["entity_type"]), "", r["entity_type"], esc(str(r["entity_id"])), "", str(r["entity_id"]),
             esc((r["metric"] or "").replace("_", " ")), "", r["metric"] or "",
             num(r["value"]) + ("†" if r["is_est"] else ""), "r", r["value"] or 0,
             yr(r["year"]), "", r["year"] or 0] for r in subs[:400]]
    # the page file is keyed by the metric entity type (us/uk/canada/australia), not the ISO code
    others = "".join(f'<a class="card" href="/country/{e2}.html"><h3>{n}</h3><div class="m">{lbl}</div></a>'
                     for e2, n, c, lbl in COUNTRIES if e2 != etype)
    body = banner(name, f"{name}", f"Every figure on this page comes from {name}'s own statistics agency or "
                  f"funding department, retrieved by an ingestor and cited per row.",
                  crumbs=f'<a href="/index.html">Atlas</a> / {name}', small=True) + f"""
<div class="wrap">
  <div class="kpis">{kpis or '<div class="kpi"><div class="v">—</div><div class="l">no national figure yet</div></div>'}</div>
  <h2>National series</h2>
  {table([("Nation / geography",""),("Measure",""),("Value","r"),("Unit",""),("Year",""),("Source","")], nrow,
         note="† derived. National series are as published by the country's statistics agency or funding department.")}
  <h2>Subnational detail</h2>
  {table([("Geography level",""),("Area",""),("Measure",""),("Value","r"),("Year","r")], srow,
         note="Local-authority, community or state rows, exactly as the source publishes them.")}
  <h2>Other countries</h2>
  <div class="cards">{"".join(others)}</div>
  </div>
</div>"""
    return shell("../", name, "", body, manifest_total=manifest_total)


def page_costs(d, manifest_total):
    """What institutional and housing capacity actually costs, every figure sourced or marked derived."""
    facs = d["facilities"]
    metrics = q(CONN, """SELECT entity_type, entity_id, metric, value, year, is_est, note, source_id
                         FROM metric WHERE metric IN ('cost_per_bed_year','facility_annual_budget',
                         'hud_homeless_obligated_per_homeless','overdose_deaths','overdose_rate')
                         ORDER BY metric, value DESC""")
    by_metric = defaultdict(list)
    for m in metrics:
        by_metric[m["metric"]].append(m)

    # per-bed cost, published or derived from budget / beds
    costed = []
    for f in facs:
        cpb = f.get("annual_cost_per_bed")
        derived = False
        if cpb is None and f.get("budget") and f.get("beds"):
            cpb = f["budget"] / f["beds"]
            derived = True
        if cpb:
            costed.append({"f": f, "cpb": cpb, "derived": derived})
    for m in by_metric["cost_per_bed_year"]:
        if m["entity_type"] == "facility":
            for c in costed:
                if str(c["f"]["id"]) == str(m["entity_id"]) and m["value"]:
                    c["cpb"] = m["value"]
                    c["derived"] = bool(m.get("is_est"))
    costed.sort(key=lambda x: -x["cpb"])
    crows = []
    for c in costed[:40]:
        f = c["f"]
        crows.append([
            f'<a href="facility/{f["id"]}.html">{esc(f["name"])}</a>', "", f["name"],
            esc((f.get("kind") or "").replace("_", " ")), "", f.get("kind") or "",
            esc(f.get("state") or ""), "", f.get("state") or "",
            num(f.get("beds")), "r", f.get("beds") or 0,
            money(c["cpb"]) + ("†" if c["derived"] else ""), "r", c["cpb"],
            num(f.get("staff")), "r", f.get("staff") or 0,
        ])
    med = None
    vals = sorted(c["cpb"] for c in costed)
    if vals:
        med = vals[len(vals) // 2]

    # closed vs open beds by state
    st = defaultdict(lambda: {"closed": 0, "open": 0, "cclosed": 0, "copen": 0})
    for f in facs:
        s_ = (f.get("state") or "?")
        if f.get("status") == "closed":
            st[s_]["closed"] += f.get("beds") or 0
            st[s_]["cclosed"] += 1
        elif f.get("status") == "open":
            st[s_]["open"] += f.get("beds") or 0
            st[s_]["copen"] += 1
    srows = []
    for s_, v in sorted(st.items(), key=lambda kv: -(kv[1]["closed"])):
        if not (v["closed"] or v["open"]):
            continue
        est = v["closed"] * (med or 0)
        srows.append([
            f'<a href="state/{esc(s_)}.html">{esc(s_)}</a>', "", s_,
            num(v["open"]), "r", v["open"], num(v["closed"]), "r", v["closed"],
            num(v["cclosed"]), "r", v["cclosed"],
            (money(est) + "†") if est else "—", "r", est,
        ])

    ph = by_metric["hud_homeless_obligated_per_homeless"]
    prow = [[f'<a href="state/{esc(m["entity_id"])}.html">{esc(m["entity_id"])}</a>', "", m["entity_id"],
             f'${m["value"]:,.0f}', "r", m["value"] or 0, yr(m.get("year")), "r", m.get("year") or 0]
            for m in ph[:55]]
    od = [m for m in by_metric["overdose_rate"]]

    body = banner("Money", "What it costs — and what rebuilding capacity would cost",
                  "Published per-bed costs where they exist, derived figures marked † with their formula, and the "
                  "closed capacity each state would have to rebuild.") + f"""
<div class="wrap">
  <div class="kpis">
    {kpi(num(len(d["facilities"])), "institutions recorded")}
    {kpi(num(sum(1 for f in facs if f.get("status") == "closed")), "closed", "hospitals, prisons, jails", "b")}
    {kpi(num(sum(f.get("beds") or 0 for f in facs if f.get("status") == "closed")), "beds in closed institutions", "", "c")}
    {kpi(money(med) + "†" if med else "—", "median cost per bed-year", "median of sourced facility costs", "d")}
  </div>

  <h2>Published cost per bed-year</h2>
  <p class="muted">Where an operator publishes a budget and a licensed-bed count, that is a sourced cost. Where we divide a
  published budget by published beds, the figure carries a † and is a division, not a published number.</p>
  {table([("Institution",""),("Kind",""),("State",""),("Beds","r"),("Cost per bed-year","r"),("Staff","r")], crows,
         note="† = budget ÷ beds. Blank staff means the operator does not publish a staffing figure.")}

  <h2>Open versus closed capacity, by state</h2>
  {table([("State",""),("Beds open","r"),("Beds closed","r"),("Closed institutions","r"),("Annual cost to reopen†","r")], srows,
         note="† reopen cost = closed beds × the median sourced cost per bed-year above. It is an order-of-magnitude "
              "figure for comparison, not a budget: it excludes capital repair, licensing and workforce costs.")}

  <h2>Federal homelessness money per homeless person, by state</h2>
  {table([("State",""),("$ per homeless person†","r"),("Year","r")], prow,
         note="† obligated federal awards whose place of performance is the state, divided by the latest point-in-time count. "
              "Obligated is not spent; place of performance is not where the money lands.")}

  <h2>Staffing</h2>
  <p>The authoritative staffing answer is a ratio, and almost no operator publishes one. Where a facility publishes both
  staff and beds we show both columns above so the ratio can be read directly. For planning, the defensible published
  benchmarks are 1.0–2.5 direct-care FTE per psychiatric bed depending on acuity; anything in that range is a planning
  assumption, not a measurement, and this project does not store it as data.</p>

  <h2>Comparison: harm versus spend</h2>
  <p>Overdose deaths and homeless counts sit side by side on state pages; the overdose series is in <code>metric</code>
  ({num(len(by_metric['overdose_deaths']))} rows). Neither dataset attributes a death to homelessness, and we do not
  compute a ratio that implies it.</p>
</div>"""
    return shell("", "Costs", "costs.html", body, manifest_total=manifest_total)


def page_laws(d, manifest_total):
    rows = []
    for l in d["laws"][:4000]:
        anchor = slug((l.get("jurisdiction") or "") + "-" + (l.get("citation") or l.get("title") or ""))
        rows.append([f'<span id="{anchor}">{esc(l.get("jurisdiction") or "")}</span>', "", l.get("jurisdiction") or "",
                     esc((l.get("topic") or "").replace("_", " ")), "", l.get("topic") or "",
                     f'<a href="{esc(l.get("url") or "#")}" rel="noopener">{esc(l.get("title") or l.get("citation") or "")}</a>', "",
                     l.get("title") or "", esc(l.get("status") or ""), "", l.get("status") or "",
                     yr(l.get("year")), "r", l.get("year") or 0])
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
<li><b>"In its own words" covers about a third of the organisations.</b> The text comes from Part III of a
Form 990, which only 990 and 990-EZ filers have — and of the 11,631 organisations here with an EIN, 4,364
have an e-filed return in a year the IRS still publishes, and 76% of those returns carry a Part III
section. The rest file the 990-N postcard, which has no description field at all. Where a 990-N or 990-EZ
filer states a purpose in its header, that is shown separately and labelled as what it is: "What it says
it is for", not a programme account.</li>
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
                     (f'<a href="{esc(u)}" rel="noopener">{esc((s.get("title") or u)[:90])}</a>'
                      if u.startswith("http") else esc((s.get("title") or u)[:90])), "", s.get("title") or u,
                     esc(s.get("publisher") or ""), "", s.get("publisher") or "",
                     esc(s.get("license") or ""), "", s.get("license") or "",
                     esc((s.get("retrieved_at") or "")[:10]), "", s.get("retrieved_at") or ""])
    body = banner("Sources", f"{num(len(d['sources']))} registered sources", "Every source used, with its licence and the date we retrieved it.") + f"""
<div class="wrap"><div style="margin-top:22px">
{table([("Domain",""),("Source",""),("Publisher",""),("Licence",""),("Retrieved","")], rows,
       note="Public data used under its own terms. HUD, Census, USASpending and Wikidata data are public domain or CC0; ProPublica Nonprofit Explorer is used under its terms; Wikipedia/Wikimedia content is CC BY-SA and attributed at the source link.")}
</div></div>"""
    return shell("", "Sources", "sources.html", body, manifest_total=manifest_total)


REPORT_META = {
    "san-francisco": ("San Francisco", "The flagship case study: counts, contracts, organizations, institutions, law and cost."),
    "los-angeles": ("Los Angeles", "The largest unsheltered population in the country, and the county that holds it."),
    "new-york": ("New York", "The largest sheltered system in the country, built on a right to shelter."),
}


def page_report(d, slug, manifest_total):
    title, blurb = REPORT_META[slug]
    src = ROOT / "reports" / f"{slug}.md"
    others = " · ".join(f'<a href="/report/{k}.html">{v[0]}</a>' for k, v in REPORT_META.items() if k != slug)
    footer = f'<p class="dim" style="margin-top:34px;font-size:.82rem">Other briefs: {others}</p>'
    if src.exists():
        text = re.sub(r"\A\s*#\s+.*\n", "", src.read_text(), count=1)
        body = banner("Deep brief", title, blurb, crumbs="Report") + \
            f'<div class="wrap wrap-narrow" style="margin-top:22px">{md(text)}</div>' + footer
    else:
        body = banner("Deep brief", title, blurb, crumbs="Report") + (
            f'<div class="wrap wrap-narrow"><p class="dim">Generated from the database by '
            f'<code>scripts/metro_report.py {slug}</code>. It has not run yet.</p></div>' + footer)
    return shell("../", title, "", body, manifest_total=manifest_total)


def page_report_sf(d, manifest_total):
    return page_report(d, "san-francisco", manifest_total)


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
        line = spark_svg([dict(r) for r in series], h=120)
        if line:
            spark = (f'<div class="panel"><h3>Homeless count {series[0]["year"]}–{series[-1]["year"]}</h3>'
                     f'{line}</div>')
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


def coc_outcomes(coc_num):
    """Outcome measures for one CoC, if the SPM ingest has produced any."""
    rows = q(CONN, """SELECT metric, value, unit, year, is_est, note, source_id FROM metric
                      WHERE entity_type='coc' AND entity_id=? AND (metric LIKE 'spm%' OR metric LIKE '%exit%'
                        OR metric LIKE '%return%') AND value IS NOT NULL
                      ORDER BY metric, year DESC""", (coc_num,))
    if not rows:
        return ""
    body = "".join(f'<tr><td class="num">{num(r["value"])}{"†" if r["is_est"] else ""}</td>'
                   f'<td>{esc((r["metric"] or "").replace("_", " "))}</td><td>{yr(r["year"])}</td>'
                   f'<td>{esc(r.get("note") or "")[:120]}</td><td>{cite(r.get("source_id"))}</td></tr>'
                   for r in rows[:40])
    return (f'<h2>Does it work here? System performance</h2><div class="tablewrap"><table class="sortable">'
            f'<thead><tr><th class="r">Value</th><th>Measure</th><th>Year</th><th>Note</th><th>Source</th>'
            f'</tr></thead><tbody>{body}</tbody></table></div>'
            f'<p class="dim" style="font-size:.8rem">HUD System Performance Measures: how long people stay '
            f'homeless, how many exit to permanent housing, and how many come back. † marks a figure we derived.</p>')


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
    outcomes_block = coc_outcomes(num_)
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
  <div class="grid two" style="margin-top:16px">
    <div class="panel"><h3>Total homeless, {series[0]["year"] if series else ""}–{series[-1]["year"] if series else ""}</h3>
      {spark_svg(series)}<div class="legend">{trend_badge(trend(series))}
      <span class="dim">{num(series[0]["total"]) if series else "—"} → {num(series[-1]["total"]) if series else "—"}</span></div></div>
    <div class="panel"><h3>Unsheltered share over time</h3>
      {spark_svg([{"year": r["year"], "total": (r["unsheltered"] or 0)} for r in series], color="var(--teal)")}
      <div class="legend"><span class="dim">latest unsheltered: {num((series[-1]["unsheltered"] if series else None))}</span></div></div>
  </div>
  <h2>Every reported year</h2>
  {table([("Year",""),("Total","r"),("Sheltered","r"),("Unsheltered","r"),("Chronic","r"),("SMI","r"),("SUD","r"),("Veterans","r")], srow,
         note="Blank cells are years or subpopulations HUD did not publish for this CoC. SMI and SUD are blank for every "
              "CoC because HUD publishes those two only in per-CoC PDF reports and nationally; the national figures are in "
              "the headline numbers. We never interpolate inside this table.")}
  {outcomes_block}
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
    cseries = q(CONN, """SELECT year, total, sheltered, unsheltered FROM pit
                         WHERE coc_num=? ORDER BY year""", (c.get("coc_num"),)) if c.get("coc_num") else []
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
  <div class="panel" style="margin:16px 0"><h3>Trend for the CoC that plans this city
    {trend_badge(trend(cseries))}</h3>
    {spark_svg(cseries)}
    <div class="legend"><span class="dim">{num(cseries[0]["total"]) if cseries else "no series"} in {cseries[0]["year"] if cseries else "—"}
    → {num(cseries[-1]["total"]) if cseries else "—"} in {cseries[-1]["year"] if cseries else "—"}</span></div>
  </div>
  <p class="dim" style="font-size:.84rem">HUD counts homelessness by Continuum of Care, not by city boundary. The city figure above is
  the CoC figure and includes surrounding areas. That limitation is in the source, not in this page.</p>
  <h2>Organizations based in {esc(c["name"])}</h2>
  {table([("Organization",""),("Federal $","r"),("Revenue","r")], orow)}
  <h2>Institutions</h2>
  {table([("Institution",""),("Status",""),("Beds","r")], frow)}
</div>"""
    return shell("../", f'{c["name"]}, {c.get("state") or ""}', "", body, manifest_total=manifest_total)


AGENCY_SHORT = {
    "Department of Health and Human Services": "HHS",
    "Department of Housing and Urban Development": "HUD",
    "Department of Veterans Affairs": "VA",
    "Department of Agriculture": "USDA",
    "Department of Labor": "DOL",
    "Department of Education": "ED",
    "Department of Justice": "DOJ",
    "Department of Commerce": "DOC",
    "Department of the Treasury": "Treasury",
    "Department of Homeland Security": "DHS",
    "Department of Transportation": "DOT",
    "Department of the Interior": "DOI",
    "Department of Energy": "DOE",
    "Social Security Administration": "SSA",
    "Small Business Administration": "SBA",
    "Corporation for National and Community Service": "AmeriCorps",
}


def agency(name):
    """Long federal department names shrink to the acronym everyone uses in tables."""
    n = (name or "").strip()
    return AGENCY_SHORT.get(n, n)


PROFILE_LABEL = {
    "profile_score": "public-record profile",
    "officer_turnover": "officer turnover (latest pair)",
    "cost_per_officer": "expenses per named officer †",
    "officer_dropout_flag": "roster gap in the filings",
    "profile_disclosure": "disclosure / 40",
    "profile_solvency": "solvency / 25",
    "profile_dependency": "CoC dependency / 20",
    "profile_pay": "executive pay / 15",
}


def org_profile(org_id):
    """The public-record profile: filing completeness and financial shape, never an outcome claim."""
    rows = q(CONN, """SELECT metric, value, note, year FROM metric
                       WHERE entity_type='org' AND entity_id=?
                         AND (metric LIKE 'profile_%' OR metric IN ('officer_turnover','cost_per_officer',
                                                                    'officer_dropout_flag'))
                       ORDER BY CASE metric WHEN 'profile_score' THEN 0
                                            WHEN 'profile_disclosure' THEN 1
                                            WHEN 'profile_solvency' THEN 2
                                            WHEN 'profile_dependency' THEN 3
                                            WHEN 'profile_pay' THEN 4
                                            WHEN 'officer_turnover' THEN 5
                                            WHEN 'cost_per_officer' THEN 6
                                            ELSE 7 END, year DESC""",
             (str(org_id),))
    if not rows:
        return ""
    score = next((r for r in rows if r["metric"] == "profile_score"), None)
    if not score:
        return ""
    def fmt(r):
        m = r["metric"]
        if m == "officer_turnover":
            return pct((r["value"] or 0) * 100)
        if m == "cost_per_officer":
            return money(r["value"])
        if m == "officer_dropout_flag":
            return "yes"
        return f'{r["value"]:g}'

    seen = set()
    bars = ""
    for r in rows:
        if r["metric"] == "profile_score" or r["metric"] in seen:
            continue
        seen.add(r["metric"])
        bars += (f'<tr><td>{esc(PROFILE_LABEL.get(r["metric"], r["metric"]))}</td>'
                 f'<td class="r">{fmt(r)}</td></tr>')
    return (f'<div class="panel"><h3>Public-record profile</h3>'
            f'<p class="num" style="font-family:var(--serif);font-size:2rem;margin:0 0 .1em">'
            f'{score["value"]:g}<span class="dim" style="font-size:.9rem"> / 100</span></p>'
            f'<div class="tablewrap" style="border-top:1px solid var(--rule)"><table>'
            f'<tbody>{bars}</tbody></table></div>'
            f'<p class="dim" style="font-size:.76rem;margin:.6em 0 0">This scores filing completeness and '
            f'financial shape only. No public dataset reports whether this organisation housed anyone: HUD '
            f'publishes outcomes per Continuum of Care, never per provider. Do not read a high score as '
            f'effectiveness.</p></div>')


def org_program_block(org_id):
    """What the organisation says it does, verbatim from Part III of its own Form 990."""
    r = q(CONN, """SELECT year, mission, program_text, expenses_program, revenue_total, source_id
                     FROM org_program WHERE org_id=? ORDER BY year DESC LIMIT 1""", (org_id,))
    if not r:
        # no Part III: fall back to the purpose the organisation states in the 990 header, labelled
        # as exactly that, because it is not the same section and must not be presented as it
        pr = q(CONN, "SELECT text, year FROM org_purpose WHERE org_id=? LIMIT 1", (org_id,))
        if not pr:
            return ""
        prow = pr[0]
        return ('<h2>What it says it is for</h2>'
                '<p class="dim" style="font-size:.78rem;margin:0 0 .8em">'
                f'The primary exempt purpose from the {esc(str(prow.get("year") or ""))} Form 990 header. '
                'This filer has no Part III program-service section, so this is the only description it '
                'files; it is quoted verbatim and is not a programme-service account.</p>'
                f'<p style="margin:.2em 0 .6em">{esc((prow.get("text") or "")[:900])}</p>')
    row = r[0]
    mission = esc((row.get("mission") or "")[:900])
    prog = esc((row.get("program_text") or "")[:1600])
    share = ""
    if row.get("expenses_program") and row.get("revenue_total"):
        try:
            share = f' · {row["expenses_program"] / row["revenue_total"] * 100:.0f}% of revenue went to programme services'
        except ZeroDivisionError:
            share = ""
    body = ""
    if mission:
        body += f'<p style="margin:.2em 0 .6em">{mission}</p>'
    if prog:
        body += f'<p class="dim" style="margin:0 0 .6em;font-size:.9rem">{prog}</p>'
    if not body:
        return ""
    return (f'<h2>In its own words{share}</h2>'
            f'<p class="dim" style="font-size:.78rem;margin:0 0 .8em">Part III of the {esc(str(row.get("year") or ""))} '
            f'Form 990, verbatim {cite(row.get("source_id"))}. Filers write these themselves; nothing here is edited.</p>'
            f'{body}')


def page_org(d, o, manifest_total):
    awards = q(CONN, """SELECT * FROM award WHERE org_id=? ORDER BY amount_obligated DESC NULLS LAST LIMIT 500""", (o["id"],))
    people = q(CONN, """SELECT * FROM org_person WHERE org_id=? ORDER BY COALESCE(comp,0) DESC LIMIT 200""", (o["id"],))
    def _aid(a):
        # a subaward is the second hop of the same federal dollar; mark it rather than letting it read
        # like a prime award this organisation won directly
        if a.get("award_type") == "subaward":
            return f'<span class="dim" title="subaward of a federal prime award">↳ </span>{esc((a.get("award_id") or "")[:40])}'
        return esc((a.get("award_id") or "")[:40])
    aw = [[_aid(a), "", a.get("award_id") or "",
           esc(agency(a.get("agency"))), "", a.get("agency") or "",
           esc((a.get("description") or "")[:120]), "", a.get("description") or "",
           money(a.get("amount_obligated")), "r", a.get("amount_obligated") or 0,
           str(a.get("pop_start") or "")[:4], "", a.get("pop_start") or ""] for a in awards]
    prow = [[(f'<a href="/person/{person_slug(p)}.html">{esc(p["person"])}</a>' if person_slug(p) in PERSON_OK
              else esc(p["person"])), "", p["person"],
             esc(p.get("title") or ""), "", p.get("title") or "", num(p.get("comp")), "r", p.get("comp") or 0,
             yr(p.get("year")), "r", p.get("year") or 0] for p in people]
    tot = sum(a.get("amount_obligated") or 0 for a in awards)
    bits = []
    if o.get("city") or o.get("state"):
        bits.append(", ".join(x for x in [o.get("city"), o.get("state")] if x))
    if tot:
        bits.append(f"${tot/1e6:,.2f}M in mapped federal awards across {len(awards)} grant{'s' if len(awards) != 1 else ''}")
    if o.get("revenue"):
        bits.append(f"reported ${o['revenue']/1e6:,.2f}M revenue on its latest Form 990")
    if people:
        top = people[0]
        bits.append(f"{num(len(people))} named officers, the best paid being {esc(top['person']).title()} ({esc((top.get('title') or 'officer')).title()})")
    body = banner((o.get("org_type") or "organization").replace("_", " "), esc(o.get("name")),
                  " · ".join(bits) + ".",
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
      {org_profile(o["id"])}
      <h2>Awards</h2>
      <p class="dim" style="font-size:.78rem;margin:-.3em 0 .8em">Federal obligations recorded against this
      organisation. A row marked ↳ is a <b>subaward</b> — money passed down from a prime recipient — so it is
      the second hop of the same federal dollar, and the prime award is counted once, elsewhere.</p>
      {table([("Award",""),("Agency",""),("Description",""),("Obligated","r"),("Year","r")], aw)}
      {org_program_block(o["id"])}
    </div>
    <div>
      <div class="panel"><h3>Facts</h3><dl class="facts">
        <dt>Type</dt><dd>{esc((o.get('org_type') or 'unknown'))}</dd>
        <dt>EIN</dt><dd>{esc(o.get('ein') or '—')}</dd>
        <dt>UEI</dt><dd>{esc(o.get('uei') or '—')}</dd>
        <dt>Address</dt><dd>{esc(' '.join(x for x in [o.get('address1'), o.get('city'), o.get('state'), o.get('zip')] if x) or '—')}</dd>
        <dt>Website</dt><dd>{(f'<a href="{esc(o["website"])}" rel="noopener">{esc(o["website"])}</a>' if o.get('website') else '—')}</dd>
        <dt>Email</dt><dd>{(f'<a href="mailto:{esc(o["email"])}">{esc(o["email"])}</a>' if o.get('email') else '—')}</dd>
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
             yr(x.get("year")), "r", x.get("year") or 0] for x in others]
    body = banner("published role", esc(p["person"]), esc(p.get("title") or ""),
                  crumbs='<a href="/index.html">Atlas</a> / <a href="/people.html">People</a>', small=True) + f"""
<div class="wrap wrap-narrow">
  <div class="panel"><dl class="facts">
    <dt>Organization</dt><dd>{f'<a href="{ORG_URL[p["org_id"]]}">{esc(p.get("org_name") or "")}</a>' if ORG_URL.get(p.get("org_id")) else esc(p.get('org_name') or '')}</dd>
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
BASELINE = ROOT / "data" / "baseline-counts.json"


def previous_counts() -> dict:
    """High-water mark per table: the largest count ever published.

    A table can legitimately shrink (a re-ingest that removes duplicates), but dropping below half
    of its high-water mark almost always means an ingestor is mid-transaction with its rows invisible.
    Publishing that would silently delete a whole section of the site, so we refuse instead.
    """
    out = {}
    mf = SITE / "data" / "manifest.json"
    if mf.exists():
        try:
            out = dict(json.loads(mf.read_text()).get("rows", {}))
        except Exception:
            out = {}
    if BASELINE.exists():
        try:
            for k, v in json.loads(BASELINE.read_text()).items():
                out[k] = max(out.get(k, 0), v)
        except Exception:
            pass
    return out


def save_baseline(counts: dict):
    cur = {}
    if BASELINE.exists():
        try:
            cur = json.loads(BASELINE.read_text())
        except Exception:
            cur = {}
    for k, v in counts.items():
        cur[k] = max(cur.get(k, 0), v)
    BASELINE.write_text(json.dumps(cur, indent=1, sort_keys=True))


def _build_lock():
    """Only one build may run at a time: two builds share site.staging and interleave into garbage."""
    import fcntl
    fh = open(ROOT / "logs" / "build.lock", "a+")
    for _ in range(600):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            time.sleep(2)
    raise SystemExit("another build holds the lock; refusing to interleave")


HOMELESS_PROGRAMS = ["Continuum of Care Program", "Emergency Solutions Grant Program",
                     "Supportive Housing Program", "Shelter Plus Care",
                     "VA Supportive Services for Veteran Families Program",
                     "VA Homeless Providers Grant and Per Diem Program",
                     "Homeless Veterans’ Reintegration Program",
                     "Youth Homelessness Demonstration Program"]


def coc_winners():
    """CoCs whose count fell from 2015 to 2025, with the organisations that received the most
    federal homelessness money in each. This is co-location in a region, not attribution."""
    rows = q(CONN, """SELECT coc_num, year, total FROM pit
                      WHERE year BETWEEN 2015 AND 2025 AND total IS NOT NULL AND year != 2021""")
    by = {}
    for r in rows:
        by.setdefault(r["coc_num"], {})[r["year"]] = r["total"]
    meta = {r["coc_num"]: r for r in q(CONN, "SELECT coc_num,name,state FROM geo_coc")}
    placeholders = ",".join("?" * len(HOMELESS_PROGRAMS))
    recips = q(CONN, f"""SELECT g.coc_num AS coc, a.org_id AS oid, o.name, SUM(a.amount_obligated) AS amt, COUNT(*) AS n
                         FROM award a JOIN org o ON o.id=a.org_id
                         JOIN geo_city g ON lower(g.name)=lower(a.place_city) AND g.state=a.place_state
                              AND g.coc_num IS NOT NULL
                         WHERE a.amount_obligated > 0 AND a.program IN ({placeholders})
                         GROUP BY g.coc_num, a.org_id""", HOMELESS_PROGRAMS)
    by_coc = {}
    for r in recips:
        by_coc.setdefault(r["coc"], []).append(r)

    out = []
    for coc, ys in by.items():
        if not (ys.get(2015) and ys.get(2025)) or ys[2015] < 300:
            continue
        years = sorted(ys)
        seq = [ys[y] for y in years]
        downs = sum(1 for a_, b_ in zip(seq, seq[1:]) if b_ < a_)
        net = (ys[2025] - ys[2015]) / ys[2015] * 100
        m = meta.get(coc, {})
        top = sorted(by_coc.get(coc, []), key=lambda r: -(r["amt"] or 0))[:3]
        out.append({"coc": coc, "name": m.get("name") or coc, "state": m.get("state") or "",
                    "y0": ys[2015], "y1": ys[2025], "net": net, "downs": downs,
                    "of": len(seq) - 1, "top": top})
    out.sort(key=lambda r: r["net"])
    return out


def page_moved(d, manifest_total):
    rows = coc_winners()
    fell = [r for r in rows if r["net"] < 0]
    rose = [r for r in rows if r["net"] >= 0]

    def cell_orgs(recs):
        if not recs:
            return '<span class="dim">no mapped award</span>'
        return " · ".join(f'<a href="/org/{r["oid"]}-{slug(r["name"])}.html">{esc(r["name"][:46])}</a>'
                          f'<span class="dim"> ${(r["amt"] or 0)/1e6:,.1f}M</span>' for r in recs)

    body_rows = [[f'<a href="/coc/{esc(r["coc"])}.html">{esc(r["coc"])}</a>', "", r["coc"],
                  esc(r["state"]), "", r["state"],
                  num(r["y0"]), "r", r["y0"], num(r["y1"]), "r", r["y1"],
                  f'{r["net"]:+.1f}%', "r", r["net"],
                  f'{r["downs"]}/{r["of"]}', "r", r["downs"],
                  cell_orgs(r["top"]), "", " ".join(x["name"] for x in r["top"])] for r in fell]

    body = banner("Analysis", "Where the count fell, and who was paid there",
                  f"Of the {len(rows)} Continuums of Care that counted at least 300 people in both 2015 and 2025, "
                  f"{len(fell)} counted fewer people in 2025 and {len(rose)} counted more. "
                  f"This page lists the falls next to the organisations that received the most federal "
                  f"homelessness money in that same region.") + f"""
<div class="wrap">
  <div class="panel" style="margin-top:18px">
    <h3>What this can and cannot say</h3>
    <p style="margin-bottom:.5em">It can say: in this region the one-night count fell, and these organisations
    received these federal homelessness grants there. The money is {('Continuum of Care, Emergency Solutions Grant, '
    'Supportive Housing, Shelter Plus Care, VA SSVF and Grant-and-Per-Diem, HVRP and YHDP')} awards whose
    place of performance is a city that maps to the CoC.</p>
    <p style="margin-bottom:.5em">It cannot say that any organisation caused the fall. No public dataset links a
    provider to an outcome: HUD publishes System Performance Measures per Continuum of Care, not per agency, and
    HMIS records are confidential. A count can also fall because the count changed, because people were moved on,
    or because they left the area.</p>
    <p class="dim" style="margin:0;font-size:.82rem">Two more limits: a grantee working statewide is credited to the
    city where the money was recorded, and only federal homelessness programmes are counted — state and city money,
    which is large in California, Houston and New York, is not in this table.</p>
  </div>
  <h2>Continuums of Care where the count fell, 2015 → 2025</h2>
  <p class="dim" style="font-size:.84rem">Sorted by the size of the fall. "Down years" counts the years in which the
  count was lower than the year before, out of the nine year-pairs present (2021 is omitted: the count was partial).</p>
  {table([("CoC","nosort"),("State","nosort"),("2015","r"),("2025","r"),("Change","r"),("Down yrs","r"),
          ("Largest federal homelessness grants in the region","nosort")], body_rows)}
  <p class="dim" style="font-size:.8rem">Counts: HUD point-in-time counts. Money: USASpending awards for the eight
  federal homelessness programmes, attributed to a CoC through the place-of-performance city.</p>
</div>"""
    return shell("", "Where the count fell", "", body, desc="CoCs whose homeless count fell 2015-2025, next to the organisations that received federal homelessness money there.", manifest_total=manifest_total)


def page_404(d, manifest_total):
    body = banner("404", "That page is not in the atlas",
                  "The address is wrong, or the record it pointed at was withdrawn after a source check failed.") + """
<div class="wrap">
  <p>Nothing here. Try the search box above, or start from one of these:</p>
  <div class="cards" style="margin-top:18px">
    <div class="card"><h3><a href="/map.html">The map</a></h3><div class="m">states, CoCs, cities, institutions</div></div>
    <div class="card"><h3><a href="/orgs.html">Organizations</a></h3><div class="m">ranked by public money</div></div>
    <div class="card"><h3><a href="/states.html">States</a></h3><div class="m">counts, rates and federal awards</div></div>
    <div class="card"><h3><a href="/facilities.html">Institutions</a></h3><div class="m">psychiatric hospitals, prisons, jails</div></div>
    <div class="card"><h3><a href="/report/san-francisco.html">San Francisco brief</a></h3><div class="m">the flagship case study</div></div>
    <div class="card"><h3><a href="/search.html">Search</a></h3><div class="m">every record in the archive</div></div>
  </div>
</div>"""
    # served at whatever path was requested, so every link must be root-absolute
    return shell("", "Not found", "", body, manifest_total=manifest_total)


def main():
    global CONN, SITE, PERSON_OK, ORG_URL
    _lock = _build_lock()
    if "--out" in sys.argv:
        SITE = Path(sys.argv[sys.argv.index("--out") + 1]).resolve()
        print(f"building into {SITE}")
    if not DB.exists():
        print("no database"); return 1
    # read-write handle: a read-only connection cannot see rows still in the WAL
    CONN = sqlite3.connect(DB, timeout=180)
    CONN.row_factory = sqlite3.Row
    prev = previous_counts()
    d = load(CONN)
    counts = d["meta"]["counts"]
    shrunk = {t: (prev[t], counts.get(t, 0)) for t in prev
              if prev[t] >= 100 and counts.get(t, 0) < prev[t] * 0.5}
    if shrunk and "--force" not in sys.argv:
        print("REFUSING TO BUILD — these tables are less than half their last published size:")
        for t, (a, b) in shrunk.items():
            print(f"  {t:12} {a:,} -> {b:,}")
        print("That usually means an ingestor is mid-transaction. Re-run with --force if the drop is real.")
        return 2
    if "--out" in sys.argv:
        SITE.mkdir(parents=True, exist_ok=True)
    if "--out" not in sys.argv:
        # write into STAGING and swap at the end (see the end of main)
        if STAGING.exists():
            shutil.rmtree(STAGING)
        SITE = STAGING
        SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "assets").mkdir(exist_ok=True)
    for f in ("atlas.css", "app.js"):
        shutil.copy(SRC / f, SITE / "assets" / f)
    for f in ("maplibre-gl.js", "maplibre-gl.css"):   # vendored, so the map has no CDN dependency
        shutil.copy(SRC / "vendor" / f, SITE / "assets" / f)

    PERSON_OK = {person_slug(p) for p in d["people_top"][:PERSON_PAGE_MAX]}
    ORG_URL = {o["id"]: f"/org/{o['id']}-{slug(o['name'])}.html" for o in d["top_orgs"][:ORG_PAGE_MAX]}
    geo_counts = build_exports(d, CONN)
    total, manifest = build_index(d)
    (SITE / "data" / "manifest.json").write_text(json.dumps({"total": total, "at": NOW, "rows": counts}))
    save_baseline(counts)
    write("index.html", page_index(d, total))
    write("map.html", page_map(d, total))
    write("states.html", page_states(d, total))
    write("cities.html", page_cities(d, total))
    write("orgs.html", page_orgs(d, total))
    write("org/index.html", page_org_index(d, total))
    write("people.html", page_people(d, total))
    write("facilities.html", page_facilities(d, total))
    write("budgets.html", page_budgets(d, total))
    for _c in COUNTRIES:
        write(f"country/{_c[0]}.html", page_country(_c, total))
    write("rankings.html", page_rankings(d, total))
    write("costs.html", page_costs(d, total))
    write("laws.html", page_laws(d, total))
    write("media.html", page_media(d, total))
    write("docs.html", page_docs(d, total))
    write("search.html", page_search(d, total))
    write("about.html", page_about(d, total))
    write("404.html", page_404(d, total))
    write("moved.html", page_moved(d, total))
    write("method.html", page_method(d, total))
    write("sources.html", page_sources(d, total))
    write("report/san-francisco.html", page_report(d, "san-francisco", total))
    write("report/los-angeles.html", page_report(d, "los-angeles", total))
    write("report/new-york.html", page_report(d, "new-york", total))

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
    if "--out" not in sys.argv:
        # atomic swap: a reader never sees a half-written site
        final = ROOT / "site"
        prev = ROOT / "site.prev"
        if prev.exists():
            shutil.rmtree(prev)
        if final.exists():
            final.rename(prev)
        STAGING.rename(final)
        if prev.exists():
            shutil.rmtree(prev)
        print(f"published {final} atomically ({n + 16} pages)")
    print(json.dumps({"pages": n + 16, "search_index": total, "geo": geo_counts,
                      "rows": d["meta"]["counts"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
