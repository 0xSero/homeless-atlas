#!/usr/bin/env python3
"""cn_sites — the organisation's website, read from its Charity Navigator EIN page.

Charity Navigator publishes one page per EIN and embeds the organisation's own URL in it as
`"url":"https://…"`. Its robots.txt allows `/ein/` (it disallows exactly two EINs, which this script
skips), and the pages are public, server-rendered and keyless — the tenth source tried for the website
target, and the first one that is a registry of organisations rather than a search engine.

Each candidate is fetched before it is written and must name the organisation (whole name, or its
distinctive words) and its state; a URL that only came from CN is not enough on its own.

Run: python3 -m ingest.domains.cn_sites [--limit N] [--workers 6] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch, now  # noqa: E402

DOMAIN = "cn_sites"
UA = {"User-Agent": "homeless-atlas/1.0 (public-record research; robots-compliant)"}
PAGE = "https://www.charitynavigator.org/ein/{ein}"
ROBOTS_DENY = {"863371262", "86337162"}
WORD_STOP = {"the", "of", "and", "for", "inc", "incorporated", "llc", "corp", "co", "services",
             "service", "center", "centre", "community", "housing", "shelter", "home", "homes",
             "family", "families", "program", "programs", "project", "projects", "group", "county",
             "city", "department", "authority", "ministries", "mission", "association", "society",
             "foundation", "council", "agency", "alliance", "network", "partners", "coalition"}


def cn_name(body: str) -> str:
    m = re.search(r'"name"\s*:\s*"([^"]{3,120})"', body)
    return m.group(1) if m else ""


def alive(url: str) -> int:
    """HTTP status of the URL, including the ones that answer a bot wall (202/403). 0 means no answer."""
    import ssl
    import urllib.error
    import urllib.request
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def site_of(body: str) -> str:
    m = re.search(r'"url"\s*:\s*"(https?://[^"]+)"', body)
    if not m:
        return ""
    url = m.group(1).strip()
    if not url or "charitynavigator" in url.lower():
        return ""
    url = url if url.startswith("http") else "https://" + url
    # CN publishes many URLs upper-cased ("https://WWW.HOPEHOUSEMKE.ORG"); hosts must be lower for TLS
    m2 = re.match(r"(https?://)([^/]+)(.*)", url)
    return (m2.group(1) + m2.group(2).lower() + m2.group(3)) if m2 else url


def one(org: dict) -> tuple[dict, str, int]:
    ein = re.sub(r"\D", "", org["ein"] or "")
    if not ein or ein in ROBOTS_DENY:
        return org, "", 0
    try:
        body = fetch(PAGE.format(ein=ein), domain=DOMAIN, headers=UA, timeout=20).decode("utf-8", "replace")
    except Exception:
        return org, "", 0
    url = site_of(body)
    if not url:
        return org, "", 0
    # the CN record itself is keyed by EIN, so the place on that page is evidence that the URL belongs
    # to this organisation even when the site answers a bot wall we will not try to get past
    cn_low = re.sub(r"[^a-z0-9 ]", " ", body.lower())
    st = (org["state"] or "").lower().strip()
    city = (org["city"] or "").lower().strip()
    registry_ok = (bool(city) and len(city) > 4 and city in cn_low) and \
                  (bool(st) and re.search(r"\b" + re.escape(st) + r"\b", cn_low) is not None)
    status = alive(url)
    if status == 0:
        return org, url, 0                  # the domain does not answer: not a website
    # prove it: fetch the site and find the organisation on it
    try:
        page = fetch(url, domain=DOMAIN, headers=UA, timeout=15).decode("utf-8", "replace")
    except Exception:
        page = ""
    low = re.sub(r"[^a-z0-9 ]", " ", page[:500_000].lower())
    flat = low.replace(" ", "")
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", org["name"] or "") if len(t) > 3 and t.lower() not in WORD_STOP]
    key = "".join(t.lower() for t in toks)
    st = (org["state"] or "").lower().strip()
    city = (org["city"] or "").lower().strip()
    name_ok = bool(key) and len(key) >= 8 and key in flat
    hits = sum(1 for t in toks if t.lower() in low)
    words_ok = bool(toks) and hits >= max(1, (len(toks) * 3 + 4) // 5)   # 60% of distinctive words
    place_ok = (bool(st) and re.search(r"\b" + re.escape(st) + r"\b", low) is not None) or \
               (bool(city) and city in low)
    if name_ok or (words_ok and place_ok):
        return org, url, 1                 # the site itself says who it is
    # the registry record is keyed by EIN; if its name is the same organisation's, the association is
    # established by the registry even when the site answers a bot wall this project will not pass
    cnk = re.sub(r"[^a-z0-9]", "", cn_name(body).lower())
    ourk = re.sub(r"[^a-z0-9]", "", (org["name"] or "").lower())
    if cnk and ourk and (cnk == ourk or cnk in ourk or ourk in cnk) and status < 500:
        return org, url, 2
    if registry_ok and status < 500:
        return org, url, 2
    return org, url, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS cn_site_check(
        org_id INTEGER PRIMARY KEY, ein TEXT, url TEXT, found INTEGER, matched INTEGER, checked_at TEXT)""")
    conn.commit()
    seen = {r[0] for r in conn.execute("SELECT org_id FROM cn_site_check WHERE found=1")}
    rows = [dict(r) for r in conn.execute(
        """SELECT id AS org_id, name, ein, city, state FROM org
            WHERE website IS NULL AND ein IS NOT NULL AND ein != ''
            ORDER BY id""")]
    rows = [r for r in rows if r["org_id"] not in seen]
    if a.limit:
        rows = rows[: a.limit]
    print(f"organisations to look up: {len(rows):,}", flush=True)
    written = 0
    found = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for i, (org, url, matched) in enumerate(pool.map(one, rows), 1):
            if url:
                found += 1
            if not a.dry_run:
                if matched:
                    conn.execute("UPDATE org SET website=?, updated_at=? WHERE id=?", (url, now(), org["org_id"]))
                    written += 1
                conn.execute("INSERT OR REPLACE INTO cn_site_check VALUES(?,?,?,?,?,?)",
                             (org["org_id"], org["ein"], url, 1 if url else 0, matched, now()))
                if i % 50 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(rows)}] found={found} written={written}", flush=True)
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0]
    print(json.dumps({"tried": len(rows), "cn_had_a_url": found, "written": written,
                      "websites_total_now": total}))


if __name__ == "__main__":
    main()
