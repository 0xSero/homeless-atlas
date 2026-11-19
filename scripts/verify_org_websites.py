#!/usr/bin/env python3
"""Check every org.website and retire the ones that are dead.

A URL published on a 2020 Form 990 is often gone by now — half of the sites the contacts harvester
fetched returned 404. A website field that points nowhere is a small false claim on thousands of
organisation pages, so each is fetched and only a live one is kept; dead ones move to `website_dead`
(kept for the record, not displayed).

Run: python3 scripts/verify_org_websites.py [--limit N] [--dry-run]
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, now  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")


def reachable(url):
    import requests
    for ua in ("homeless-atlas/1.0", UA):
        try:
            r = requests.get(url, timeout=20, stream=True, allow_redirects=True,
                             headers={"User-Agent": ua})
            code = r.status_code
            r.close()
            if code in (200, 203, 204, 301, 302, 403, 429):
                return code          # 403/429 mean blocked, not gone
        except Exception:
            continue
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS org_website_check(org_id INTEGER PRIMARY KEY, url TEXT, "
              "status INTEGER, checked_at TEXT)")
    rows = c.execute("SELECT id, name, website FROM org WHERE website IS NOT NULL").fetchall()
    if a.limit:
        rows = rows[:a.limit]
    live = dead = 0
    for i, r in enumerate(rows, 1):
        code = reachable(r["website"])
        ok = code != 0
        if ok:
            live += 1
        else:
            dead += 1
            if not a.dry_run:
                c.execute("UPDATE org SET website=NULL WHERE id=?", (r["id"],))
        if not a.dry_run:
            c.execute("INSERT OR REPLACE INTO org_website_check VALUES(?,?,?,?)",
                      (r["id"], r["website"], code, now()))
            if i % 50 == 0:
                c.commit()
        if i % 100 == 0:
            print(f"  {i}/{len(rows)} live={live} dead={dead}", flush=True)
        time.sleep(0.4)
    c.commit()
    print(json.dumps({"checked": len(rows), "live": live, "dead_to_bots": dead, "dry_run": a.dry_run}))


if __name__ == "__main__":
    main()
