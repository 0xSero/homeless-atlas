#!/usr/bin/env python3
"""mine_contact_cache — recover published contact addresses from pages already crawled.

The contacts harvest cached 5,023 fetched pages (19 GB) under data/raw/org_contacts/. Those pages are
the organisations' own websites, which is exactly where a public contact address lives. Parsing the
cache costs no requests, so this closes part of the email gap without touching the Brave quota.

Rules:
  * only pages whose host matches an organisation's recorded website host are used;
  * the address must look like a real mailbox and must not be a tracker/asset artefact
    (sentry, wixpress, godaddy, @2x, image filenames);
  * preference order info@ > contact@ > hello@ > office@ > anything else on the same domain;
  * an existing org.email is never overwritten, and every write records the page it came from.

Run: python3 scripts/mine_contact_cache.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, now  # noqa: E402

CACHE = ROOT / "data" / "raw" / "org_contacts"
BAD = ("sentry", "wixpress", "godaddy", "example.com", "domain.com", "yourdomain", "@2x",
       ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", "sentry.io", "cloudflare",
       "wordpress.org", "squarespace", "wix.com", "no-reply", "noreply", "donotreply")
PREF = ("info@", "contact@", "hello@", "office@", "admin@", "help@", "reception@")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def host_of(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def plausible(addr: str) -> bool:
    a = addr.lower()
    if any(b in a for b in BAD):
        return False
    if len(a) > 64 or a.count("@") != 1:
        return False
    local, _, domain = a.partition("@")
    if not local or "." not in domain or domain.endswith((".png", ".jpg", ".js", ".css")):
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dir", action="append", default=None,
                    help="cache directory to mine (repeatable); default data/raw/org_contacts")
    a = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS org_email_mine(
        org_id INTEGER PRIMARY KEY, email TEXT, host TEXT, page TEXT, checked_at TEXT)""")
    conn.commit()

    by_host: dict[str, int] = {}
    for r in conn.execute("SELECT id, website FROM org WHERE website IS NOT NULL"):
        h = host_of(r["website"] or "")
        if h:
            by_host[h] = r["id"]
    print(f"organisations with a website host: {len(by_host):,}")

    found: dict[int, Counter] = defaultdict(Counter)
    page_of: dict[int, str] = {}
    dirs = [Path(d) for d in (a.dir or [str(CACHE)])]
    files = sorted(f for d in dirs for f in d.glob("*.meta.json"))
    if a.limit:
        files = files[: a.limit]
    for meta_path in files:
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        url = meta.get("url") or ""
        if "robots.txt" in url or meta.get("status") != 200:
            continue
        org_id = by_host.get(host_of(url))
        if org_id is None:
            continue
        bin_path = meta_path.with_suffix("").with_suffix(".bin")
        if not bin_path.exists():
            continue
        try:
            text = bin_path.read_bytes()[:600_000].decode("utf-8", "replace")
        except Exception:
            continue
        for addr in set(EMAIL_RE.findall(text)):
            if plausible(addr):
                found[org_id][addr.lower()] += 1
                page_of[org_id] = url
    print(f"organisations with at least one plausible address in the cache: {len(found):,}")

    written = 0
    for org_id, counter in found.items():
        row = conn.execute("SELECT email, website FROM org WHERE id=?", (org_id,)).fetchone()
        if row and row["email"]:
            continue
        host = host_of(row["website"] or "") if row else ""
        ranked = sorted(counter.items(), key=lambda kv: (
            0 if any(kv[0].startswith(p) for p in PREF) else 1,
            0 if kv[0].endswith("@" + host) else 1,
            -kv[1]))
        email = ranked[0][0]
        if not a.dry_run:
            conn.execute("UPDATE org SET email=?, updated_at=? WHERE id=?", (email, now(), org_id))
            conn.execute("INSERT OR REPLACE INTO org_email_mine VALUES(?,?,?,?,?)",
                         (org_id, email, host, page_of.get(org_id, ""), now()))
            written += 1
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org WHERE email IS NOT NULL").fetchone()[0]
    print(json.dumps({"orgs_matched": len(found), "emails_written": written,
                      "emails_total_now": total, "dry_run": a.dry_run}))


if __name__ == "__main__":
    main()
