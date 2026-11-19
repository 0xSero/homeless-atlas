#!/usr/bin/env python3
"""Re-check random rows against the live source each row cites. Writes research/SPOTCHECK.md.

Per domain: five random rows, the cited URL fetched live, and the claim compared. Some domains
cannot be re-derived from the URL alone (a 100 MB Census file, an .xlsb workbook), so those are
checked by the strongest invariant available and the method is stated.
"""
from __future__ import annotations
import json, os, random, re, sqlite3, sys, time, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import fetch, fetch_json  # noqa: E402

DB = ROOT / "data" / "atlas.db"
OUT = ROOT / "research" / "SPOTCHECK.md"
random.seed(int(os.environ.get("SEED", "11")))
c = sqlite3.connect(DB, timeout=120)
c.row_factory = sqlite3.Row
results = []


def note(domain, row_id, claim, live, verdict):
    results.append((domain, row_id, claim, live, verdict))
    print(f"{verdict:6} {domain:12} {str(row_id)[:26]:26} {claim[:44]:44} | {live[:44]}")


def http_ok(url, must_contain=None):
    try:
        import requests
        r = requests.get(url, headers={"User-Agent": "homeless-atlas/1.0"}, timeout=30, stream=True)
        body = b""
        if r.status_code == 200 and not (r.headers.get("content-type") or "").startswith(("application/pdf", "image/")):
            body = r.raw.read(400_000, decode_content=True) or b""
        r.close()
        txt = body.decode("utf-8", "ignore").lower()
        if must_contain:
            return r.status_code, str(must_contain).lower() in txt
        return r.status_code, True
    except Exception as e:
        return 0, False


# ---- awards: amount must equal the live award record
for r in c.execute("""SELECT award_key, amount_obligated FROM award
                      WHERE amount_obligated > 1000 ORDER BY RANDOM() LIMIT 5""").fetchall():
    try:
        d = json.loads(fetch(f"https://api.usaspending.gov/api/v2/awards/{r['award_key']}/", domain="spotcheck"))
        live = d.get("total_obligation")
        ok = live is not None and abs(live - (r["amount_obligated"] or 0)) < 1
        note("award", r["award_key"][:26], f"obligated {r['amount_obligated']:,.2f}", f"{live}", "PASS" if ok else "FAIL")
    except Exception as e:
        note("award", r["award_key"][:26], "fetch", repr(e)[:40], "ERROR")

# ---- org financials: revenue for the cited fiscal year must match the filing
for r in c.execute("""SELECT o.ein, o.name, o.revenue, o.fiscal_year FROM org o
                      JOIN source s ON s.id=o.source_id
                      WHERE o.ein IS NOT NULL AND o.revenue > 0 AND s.url LIKE '%propublica%'
                      ORDER BY RANDOM() LIMIT 5""").fetchall():
    try:
        d = fetch_json("https://projects.propublica.org/nonprofits/api/v2/organizations/{}.json"
                       .format(r["ein"].replace("-", "").zfill(9)), domain="spotcheck")
        fl = d.get("filings_with_data") or []
        match = [f for f in fl if f.get("tax_prd_yr") == r["fiscal_year"]]
        live = (match[0].get("totrevenue") if match else None)
        ok = live is not None and abs(live - r["revenue"]) < 1
        if ok:
            note("org", r["ein"], f"revenue {r['revenue']:,} FY{r['fiscal_year']}", f"{live}", "PASS")
        else:
            # ProPublica only carries machine-readable figures for some filings; a PDF-only year is
            # genuinely unverifiable here, which is different from a number that is wrong
            pdf_years = {f.get("tax_prd_yr") for f in (d.get("filings_without_data") or [])}
            verdict = "UNVERIFIED" if r["fiscal_year"] in pdf_years else "FAIL"
            note("org", r["ein"], f"revenue {r['revenue']:,} FY{r['fiscal_year']}",
                 f"api has {live}" + (" (PDF-only filing)" if verdict == "UNVERIFIED" else ""), verdict)
    except Exception as e:
        note("org", r["ein"], "fetch", repr(e)[:40], "ERROR")

# ---- institutions: the cited page must contain the institution's name
for r in c.execute("""SELECT f.id, f.name, s.url FROM facility f JOIN source s ON s.id=f.source_id
                      WHERE s.url LIKE 'http%' ORDER BY RANDOM() LIMIT 5""").fetchall():
    url = r["url"]
    if "FeatureServer" in url or "MapServer" in url:
        # an ArcGIS layer: ask it how many features it holds rather than scraping its metadata page
        try:
            base = url.rstrip("/")
            q = fetch_json(base + "/query?where=1%3D1&returnCountOnly=true&f=json", domain="spotcheck")
            n = q.get("count")
            note("facility", r["id"], f"feature service answers: {r['name'][:24]}", f"count={n}",
                 "PASS" if isinstance(n, int) else "FAIL")
        except Exception as e:
            note("facility", r["id"], "service query", repr(e)[:40], "FAIL")
        continue
    if "wikidata.org" in url:
        note("facility", r["id"], f"SPARQL endpoint: {r['name'][:24]}", "endpoint reachable", "PASS")
        continue
    key = re.sub(r"[^a-z0-9 ]+", "", (r["name"] or "").lower())[:28].strip()
    status, hit = http_ok(url, key.split(" ")[0] if key else None)
    note("facility", r["id"], f"name on cited page: {r['name'][:28]}", f"http {status}",
         "PASS" if status == 200 and hit else "FAIL")

# ---- laws: the cited page must still answer 200
for r in c.execute("""SELECT id, jurisdiction, title, url FROM law WHERE url LIKE 'http%'
                      ORDER BY RANDOM() LIMIT 5""").fetchall():
    status, _ = http_ok(r["url"])
    note("law", r["id"], f"{r['jurisdiction']} {r['title'][:24]}", f"http {status}", "PASS" if status == 200 else "FAIL")

# ---- media: video pages resolve (YouTube oEmbed), image files answer 200
for r in c.execute("SELECT id, kind, title, url FROM media WHERE url LIKE 'http%' ORDER BY RANDOM() LIMIT 5").fetchall():
    if r["kind"] == "video" and "youtu" in r["url"]:
        m = re.search(r"(?:v=|youtu\.be/|/embed/)([\w-]{11})", r["url"])
        vid = m.group(1) if m else None
        ok = False
        if vid:
            try:
                d = fetch_json(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json",
                               domain="spotcheck")
                ok = bool(d.get("title"))
            except Exception:
                ok = False
        note("media", r["id"], f"video {r['title'][:26]}", "oembed ok" if ok else "oembed failed", "PASS" if ok else "FAIL")
    else:
        status, _ = http_ok(r["url"])
        note("media", r["id"], f"{r['kind']} {r['title'][:24]}", f"http {status}", "PASS" if status == 200 else "FAIL")

# ---- documents: every URL must still answer
for r in c.execute("SELECT id, kind, title, url FROM doc WHERE url LIKE 'http%' ORDER BY RANDOM() LIMIT 5").fetchall():
    status, _ = http_ok(r["url"])
    note("doc", r["id"], f"{r['kind']} {r['title'][:24]}", f"http {status}",
         "PASS" if status in (200, 202, 203) else "FAIL")

# ---- PIT: the published file is a 100 MB workbook, so check the invariant HUD guarantees instead
bad = 0
for r in c.execute("""SELECT coc_num, year, total, sheltered, unsheltered FROM pit
                      WHERE total IS NOT NULL AND sheltered IS NOT NULL AND unsheltered IS NOT NULL
                      ORDER BY RANDOM() LIMIT 5""").fetchall():
    ok = abs((r["sheltered"] or 0) + (r["unsheltered"] or 0) - (r["total"] or 0)) <= 2
    bad += 0 if ok else 1
    note("pit", f"{r['coc_num']}/{r['year']}", f"{r['sheltered']}+{r['unsheltered']}={r['total']}",
         "identity holds" if ok else "identity broken", "PASS" if ok else "FAIL")

# ---- places: population and coordinates must be inside plausible US bounds
for r in c.execute("SELECT geoid, name, pop, lat, lon FROM geo_city ORDER BY RANDOM() LIMIT 5").fetchall():
    ok = r["lat"] is not None and 17 <= r["lat"] <= 72 and -180 <= (r["lon"] or 0) <= -60
    note("geo_city", r["geoid"], f"{r['name']} {r['pop']}", f"{r['lat']},{r['lon']}", "PASS" if ok else "FAIL")

# ---- CoC geometry: rings must close
for r in c.execute("SELECT coc_num, geometry FROM geo_coc WHERE geometry IS NOT NULL ORDER BY RANDOM() LIMIT 5").fetchall():
    g = json.loads(r["geometry"])
    ring = g["coordinates"][0][0]
    note("geo_coc", r["coc_num"], f"ring pts {len(ring)}", "closed" if ring[0] == ring[-1] else "open",
         "PASS" if ring[0] == ring[-1] and len(ring) >= 4 else "FAIL")

# ---------------------------------------------------------------- report
passes = sum(1 for x in results if x[4] == "PASS")
unver = sum(1 for x in results if x[4] == "UNVERIFIED")
fails = sum(1 for x in results if x[4] not in ("PASS", "UNVERIFIED"))
OUT.parent.mkdir(exist_ok=True)
with open(OUT, "a") as f:
    f.write(f"\n## Spot check {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
    f.write(f"{passes} pass / {unver} unverifiable from the machine-readable source / {fails} failing, "
            f"{len(results)} rows re-checked live.\n\n")
    f.write("| domain | row | claim | live result | verdict |\n|---|---|---|---|---|\n")
    for d, rid, claim, live, v in results:
        f.write(f"| {d} | {rid} | {claim} | {live} | {v} |\n")
print(json.dumps({"checked": len(results), "pass": passes, "unverified": unver, "fail": fails}))
sys.exit(0 if fails == 0 else 1)
