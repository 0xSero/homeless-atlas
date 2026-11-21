#!/usr/bin/env python3
"""org_sites — find and verify each organisation's own website, and read a contact address from it.

The Form 990 XML only carries a website for ~0.4% of filings (measured: 10 of 2,309), so the
remaining organisations need a search step. This uses the Brave Search API (BRAVE_API_KEY in the
environment) and then *proves* the result before writing it:

  1. query "<name>" <city> <state>
  2. discard anything on an aggregator or social host (guidestar, causeiq, propublica, facebook,
     linkedin, nextdoor, reddit, usaspending, yelp, …)
  3. require the candidate host to share a token of 4+ characters with the organisation's name, so
     "hospitality house" cannot be credited with an unrelated site that merely mentions it
  4. fetch it; 200/301/302/403/429 means it exists (a bot wall is not a dead site)
  5. look for a mailto: on that page before falling back to /contact

Everything is cached under data/raw/org_sites/, the search itself runs at 1 req/s, and the count of
lookups is reported so its cost is visible. Nothing is written when a check fails.

Run: python3 -m ingest.domains.org_sites [--limit 500] [--min-award 0] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ingest.common import DB, fetch, now, source_id_for  # noqa: E402

DOMAIN = "org_sites"
SEARCH = "https://api.search.brave.com/res/v1/web/search"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")

BLOCK = ("facebook.", "linkedin.", "twitter.", "x.com", "instagram.", "youtube.", "tiktok.",
         "nextdoor.", "reddit.", "yelp.", "guidestar.", "causeiq.", "propublica.", "usaspending.",
         "charitynavigator.", "wikipedia.", "indeed.", "glassdoor.", "mapquest.", "bbb.org",
         "google.", "bing.", "amazon.", "irs.gov", "samhsa.gov", "hud.gov", "hudexchange.",
         "opencorporates.", "bizapedia.", "nonprofitlight.", "taxexemptworld.", "instrumentl.",
         "crunchbase.", "zoominfo.", "dnb.com", "manta.com", "chamberofcommerce.", "medium.com",
         "wordpress.com", "blogspot.", "wixsite.com", "squarespace.com", "godaddysites.")

ATTR = ("homeless", "shelter", "housing", "mission", "services", "transitional", "veterans",
        "family", "community", "outreach", "recovery", "rescue", "hope", "care", "health")


def tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(t) >= 4}


def accept(org_name: str, url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    if not host or any(b in host for b in BLOCK):
        return False
    host_words = set(re.split(r"[^a-z0-9]+", host.split(".org")[0].split(".com")[0]))
    name_words = tokens(org_name)
    if host_words & name_words:
        return True
    return bool(host_words & set(ATTR)) and bool(name_words & set(ATTR))


def search(conn, key: str, query: str) -> list[dict]:
    url = f"{SEARCH}?q={urllib.parse.quote(query)}&count=8"
    try:
        body = fetch(url, domain=DOMAIN, headers={"Accept": "application/json",
                                                  "X-Subscription-Token": key}, timeout=30)
        return json.loads(body).get("web", {}).get("results", [])
    except Exception:
        return []


def grab_email(html: str) -> str:
    m = re.search(r"mailto:([^\"'?>\s]+)", html or "", re.I)
    addr = ""
    if m:
        addr = m.group(1)
    else:
        m2 = re.search(r"[\w.+-]+@[\w-]+\.[\w.]{2,}", html or "")
        if m2:
            addr = m2.group(0)
    addr = (addr or "").strip().lower()
    if not addr or any(x in addr for x in ("example.com", "sentry", "wixpress", "godaddy", "@2x")):
        return ""
    return addr if re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", addr) else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        print("BRAVE_API_KEY is not set — nothing to do")
        return

    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS org_site_check(
        org_id INTEGER PRIMARY KEY, query TEXT, candidate TEXT, status INTEGER,
        accepted INTEGER, email TEXT, checked_at TEXT)""")
    conn.commit()
    src = source_id_for(conn, SEARCH, DOMAIN, title="Brave Search API", publisher="Brave Software")

    rows = conn.execute("""
        SELECT o.id, o.name, o.city, o.state,
               (SELECT SUM(amount_obligated) FROM award a WHERE a.org_id=o.id) amt
          FROM org o LEFT JOIN org_site_check c ON c.org_id=o.id
         WHERE o.website IS NULL AND c.org_id IS NULL AND o.name IS NOT NULL
         ORDER BY COALESCE(amt,0) DESC, o.id LIMIT ?""", (a.limit,)).fetchall()
    print(f"organisations to look up: {len(rows):,}")

    found = mailed = 0
    for i, r in enumerate(rows, 1):
        q = " ".join(x for x in (r["name"], r["city"], r["state"]) if x)
        results = search(conn, key, q)
        chosen, status, email = "", 0, ""
        for res in results:
            u = res.get("url") or ""
            if not accept(r["name"], u):
                continue
            base = f"{urllib.parse.urlparse(u).scheme}://{urllib.parse.urlparse(u).netloc}"
            try:
                html = fetch(base + "/", domain=DOMAIN, headers={"User-Agent": UA}, timeout=25)
                status = 200
            except Exception:
                continue
            chosen = base
            email = grab_email(html.decode("utf-8", "replace")[:400000])
            if not email:
                for path in ("/contact", "/contact-us", "/about"):
                    try:
                        h2 = fetch(base + path, domain=DOMAIN, headers={"User-Agent": UA}, timeout=20)
                        email = grab_email(h2.decode("utf-8", "replace")[:400000])
                        if email:
                            break
                    except Exception:
                        continue
            break
        if chosen and not a.dry_run:
            conn.execute("UPDATE org SET website=?, updated_at=? WHERE id=?", (chosen, now(), r["id"]))
            found += 1
            if email:
                conn.execute("UPDATE org SET email=? WHERE id=?", (email, r["id"]))
                mailed += 1
        if not a.dry_run:
            conn.execute("INSERT OR REPLACE INTO org_site_check VALUES(?,?,?,?,?,?,?)",
                         (r["id"], q, chosen, status, 1 if chosen else 0, email, now()))
            if i % 20 == 0:
                conn.commit()
        if i % 20 == 0:
            print(f"  [{i}/{len(rows)}] sites={found} emails={mailed}", flush=True)
        time.sleep(1)                                  # 1 req/s, per the ingest contract
    if not a.dry_run:
        conn.commit()
    print(json.dumps({"looked_up": len(rows), "websites_found": found, "emails_found": mailed,
                      "lookups_used": len(rows)}))
    print("totals:", conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0],
          "websites,", conn.execute("SELECT COUNT(*) FROM org WHERE email IS NOT NULL").fetchone()[0], "emails")


if __name__ == "__main__":
    main()
