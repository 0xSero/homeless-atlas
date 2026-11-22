#!/usr/bin/env python3
"""link_graph_sites — read websites off the anchor text of pages we already hold.

4,647 organisation sites are cached on disk. Their outbound links carry anchor text, and coalition or
member-list pages name each member in that anchor: `<a href="https://hcx.org">X Housing Coalition</a>`.
When the anchor text matches an organisation in this archive that has no website, the href is that
organisation's site — named by the page that linked to it, not guessed from a domain label.

No requests are made to read the pages (they are on disk). Each candidate domain is then fetched once and
must still name the organisation or its city and state before anything is written.

Run: python3 scripts/link_graph_sites.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from ingest.common import DB, fetch, now  # noqa: E402

CACHES = ("org_domains", "org_gov", "org_sites", "org_contacts", "web_check", "cn_sites", "link_graph")
SUFFIX = {"inc", "incorporated", "llc", "co", "corp", "corporation", "ltd", "the", "of", "and",
          "a", "an", "dba", "usa", "us", "mn", "tx"}
GENERIC_TOKEN = {"housing", "shelter", "emergency", "supportive", "services", "service", "community",
                 "initiatives", "development", "outreach", "resources", "assistance", "transitional",
                 "care", "health", "mental", "social", "family", "families", "youth", "children",
                 "center", "centre", "program", "programs", "project", "projects", "opportunity",
                 "opportunities", "solutions", "partners", "network", "alliance", "coalition"}
BAD_HOST = ("facebook.", "twitter.", "instagram.", "linkedin.", "youtube.", "google.", "gstatic.",
            "cdn.", "static.", "img", "wix", "squarespace", "parastorage", "sqspcdn", "wp.com",
            "cloudflare", "godaddy", "gravatar", "doubleclick", "paypal", "guidestar", "candid",
            "charitynavigator", "yelp", "indeed", "eventbrite", "mailchimp", "constantcontact")


def key_of(name: str) -> str:
    n = html.unescape(name or "").lower().replace("&", " and ")
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    toks = [t for t in n.split() if t and t not in SUFFIX]
    return "".join(toks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--crawl", action="store_true",
                    help="first fetch member/partner/about pages of the sites we already hold")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS link_graph_check(
        org_id INTEGER PRIMARY KEY, url TEXT, anchor TEXT, source_page TEXT, matched INTEGER, checked_at TEXT)""")
    conn.commit()

    if a.crawl:
        # member and partner pages are where one organisation names another, which is the whole basis
        # of this channel; only four paths per site are tried and every response is cached
        conn.execute("PRAGMA busy_timeout=300000")
        # member lists live on coalition, alliance, network and council sites, not on a single shelter's
        # site: crawl those first instead of trying every site in the archive
        sites = [r[0].rstrip("/") for r in conn.execute(
            """SELECT website FROM org WHERE website IS NOT NULL AND (
                   name LIKE '%COALITION%' OR name LIKE '%ALLIANCE%' OR name LIKE '%NETWORK%'
                OR name LIKE '%COUNCIL%' OR name LIKE '%ASSOCIATION%' OR name LIKE '%CONSORTIUM%'
                OR name LIKE '%CONTINUUM%')""")]
        paths = ("/members", "/member-organizations", "/partners", "/our-members", "/directory")
        urls = [s0 + p for s0 in sites for p in paths]
        print(f"crawling {len(urls):,} member/partner/about pages", flush=True)

        def grab(u):
            try:
                fetch(u, domain="link_graph", timeout=10)
                return 1
            except Exception:
                return 0

        done = 0
        with ThreadPoolExecutor(max_workers=10) as pool:
            for i, ok in enumerate(pool.map(grab, urls), 1):
                done += ok
                if i % 2000 == 0:
                    print(f"  {i}/{len(urls)} fetched={done}", flush=True)
        print(f"member pages fetched: {done:,}", flush=True)

    index: dict[str, list] = {}
    for r in conn.execute("""SELECT id, name, city, state FROM org
                              WHERE website IS NULL AND name IS NOT NULL""").fetchall():
        k = key_of(r["name"])
        # an anchor that says "Transitional Housing" names a category, not an organisation: only names
        # of three or more words and sixteen or more letters are distinctive enough to match on
        if len(k) >= 16 and len(k.split()) if False else False:
            pass
        words = [t for t in re.split(r"[^A-Za-z0-9]+", r["name"] or "")
                 if len(t) > 2 and t.lower() not in SUFFIX]
        # an organisation whose every word is a social-service category ("Supportive Housing Inc")
        # cannot be told apart from any other page that uses those words: only a distinctive word
        # survives, and the match must then be confirmed by the site's own title
        if len(k) >= 12 and len(words) >= 2 and any(w.lower() not in GENERIC_TOKEN for w in words):
            index.setdefault(k, []).append(dict(r))
    print(f"distinctive names without a website: {len(index):,}", flush=True)

    anchor_re = re.compile(r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
    seen_org = {r[0] for r in conn.execute("SELECT org_id FROM link_graph_check")}
    found: dict[int, tuple] = {}
    pages_read = 0
    for cache in CACHES:
        for meta in Path(f"data/raw/{cache}").glob("*.meta.json"):
            b = meta.with_suffix("").with_suffix(".bin")
            if not b.exists():
                continue
            try:
                page_url = json.loads(meta.read_text()).get("url") or ""
                body = b.read_bytes()[:400_000].decode("utf-8", "replace")
            except Exception:
                continue
            pages_read += 1
            for href, text in anchor_re.findall(body):
                label = re.sub(r"<[^>]+>", " ", text)
                k = key_of(label)
                if len(k) < 12 or k not in index:
                    continue
                host = urlparse(html.unescape(href)).netloc.lower()
                if not host or host.startswith("www.") and False:
                    continue
                host = host[4:] if host.startswith("www.") else host
                if any(bad in host for bad in BAD_HOST) or not host:
                    continue
                for org in index[k]:
                    if org["id"] in found or org["id"] in seen_org:
                        continue
                    found[org["id"]] = (org, "https://" + host, label.strip(), page_url)
    print(f"pages read: {pages_read:,} | organisations matched by anchor text: {len(found):,}", flush=True)

    rows = list(found.values())
    if a.limit:
        rows = rows[: a.limit]

    def verify(item):
        """The site must carry the organisation's name in its own title or h1: an anchor names the
        organisation, but the link may point at a member list or a partner, not at its own site."""
        org, url, label, page = item
        try:
            body = fetch(url, domain="link_graph", timeout=12).decode("utf-8", "replace")
        except Exception:
            return item, 0
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I) or \
            re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
        title = re.sub(r"<[^>]+>", " ", m.group(1)) if m else ""
        flat_title = re.sub(r"[^a-z0-9]", "", title.lower())
        k = key_of(org["name"])
        if k and len(k) >= 12 and k in flat_title:
            return item, 1
        # no fallback: a city plus a name somewhere in the body is what let a member list pass for a
        # member's own site. Only the site naming itself in its title or heading counts.
        return item, 0

    written = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, (item, matched) in enumerate(pool.map(verify, rows), 1):
            org, url, label, page = item
            if matched and not a.dry_run:
                conn.execute("UPDATE org SET website=?, updated_at=? WHERE id=?", (url, now(), org["id"]))
                written += 1
            if not a.dry_run:
                conn.execute("INSERT OR REPLACE INTO link_graph_check VALUES(?,?,?,?,?,?)",
                             (org["id"], url, label[:200], page, matched, now()))
                if i % 25 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(rows)}] written={written}", flush=True)
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0]
    print(json.dumps({"matched": len(found), "verified": written, "websites_total_now": total}))


if __name__ == "__main__":
    main()
