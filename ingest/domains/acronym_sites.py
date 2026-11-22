#!/usr/bin/env python3
"""acronym_sites — guess an organisation's domain from its initials, then make the site prove it.

The anchor channel showed that the domains no name-match can reach are the acronyms: Texas Homeless
Network → thn.org, Central Florida Commission on Homelessness → cfch.org, National Urban League → nul.org.
Those come from initialisms that share no run of letters with the full name, so every name-based source
walks past them.

This generates the initials for organisations that still have no website, tries .org/.com/.net, and
writes nothing unless the page's own <title> or <h1> carries the organisation's name. A guessed URL that
is not proven is exactly the kind of invention this archive forbids, so the title check is not optional.

Run: python3 scripts/acronym_sites.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch, now  # noqa: E402

STOP = {"of", "the", "and", "for", "inc", "incorporated", "llc", "co", "corp", "a", "an", "in", "on",
        "at", "to", "county", "city", "program", "services", "service", "department", "us", "usa"}
TLDS = (".org", ".com", ".net")


def initials(name: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name or "") if w and w.lower() not in STOP]
    if len(words) < 3:
        return ""
    ac = "".join(w[0].lower() for w in words)
    if not (3 <= len(ac) <= 6) or not ac.isalpha():
        return ""
    return ac


def one(org: dict) -> tuple[dict, str, int]:
    ac = initials(org["name"])
    if not ac:
        return org, "", 0
    key = re.sub(r"[^a-z0-9]", "", (org["name"] or "").lower())
    for tld in TLDS:
        url = f"https://{ac}{tld}"
        try:
            body = fetch(url, domain="acronym", timeout=10).decode("utf-8", "replace")
        except Exception:
            continue
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I) or \
            re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
        title = re.sub(r"[^a-z0-9]", "", (m.group(1) if m else "").lower())
        if key and len(key) >= 12 and key in title:
            return org, url, 1
        # initials plus a city is not proof: "cwd" + Wisconsin passed that test and gave a commercial
        # site to a state department. Only the site naming the whole organisation counts.
    return org, "", 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS acronym_check(
        org_id INTEGER PRIMARY KEY, url TEXT, matched INTEGER, checked_at TEXT)""")
    conn.commit()
    seen = {r[0] for r in conn.execute("SELECT org_id FROM acronym_check")}
    rows = [dict(r) for r in conn.execute(
        """SELECT id AS org_id, name, city, state FROM org
            WHERE website IS NULL AND name IS NOT NULL ORDER BY id""")]
    rows = [r for r in rows if r["org_id"] not in seen and initials(r["name"])]
    if a.limit:
        rows = rows[: a.limit]
    print(f"organisations with a usable acronym: {len(rows):,}", flush=True)
    written = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for i, (org, url, matched) in enumerate(pool.map(one, rows), 1):
            if matched and not a.dry_run:
                conn.execute("UPDATE org SET website=?, updated_at=? WHERE id=?", (url, now(), org["org_id"]))
                written += 1
            if not a.dry_run:
                conn.execute("INSERT OR REPLACE INTO acronym_check VALUES(?,?,?,?)",
                             (org["org_id"], url, matched, now()))
                if i % 50 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(rows)}] written={written}", flush=True)
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0]
    print(json.dumps({"tried": len(rows), "written": written, "websites_total_now": total}))


if __name__ == "__main__":
    main()
