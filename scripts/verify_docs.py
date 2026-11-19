#!/usr/bin/env python3
"""Re-check every document URL and drop the ones that no longer serve a document.

A 403/404 for both a plain client and a browser-like client means the page is gone or blocked; the
atlas should not present it as a document. Reachable rows are left untouched.

Run: python3 scripts/verify_docs.py [--limit N] [--dry-run]
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, now  # noqa: E402

BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/126.0 Safari/537.36")


def status(url, ua):
    import requests
    try:
        r = requests.get(url, headers={"User-Agent": ua, "Accept": "text/html,application/pdf,*/*"},
                         timeout=25, stream=True, allow_redirects=True)
        code = r.status_code
        r.close()
        return code
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT id, title, url FROM doc WHERE url LIKE 'http%'").fetchall()
    if a.limit:
        rows = rows[:a.limit]
    gone = kept = blocked = 0
    for i, r in enumerate(rows, 1):
        code = status(r["url"], "homeless-atlas/1.0")
        if code in (401, 403, 429):
            # the publisher blocks automated clients (GAO, HUD and most city sites do). A browser
            # opens these fine, so they are kept — deleting them would throw away good documents.
            blocked += 1
            kept += 1
        elif code in (404, 410, 0):
            code2 = status(r["url"], BROWSER)
            if code2 in (404, 410, 0):
                gone += 1
                if not a.dry_run:
                    c.execute("DELETE FROM doc WHERE id=?", (r["id"],))
                    c.commit()
            else:
                kept += 1
        else:
            kept += 1
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} kept={kept} dropped={gone}", flush=True)
        time.sleep(0.5)
    c.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
              ("docs_verify", now(), kept, f"kept={kept} blocked_to_bots={blocked} dropped={gone}"))
    c.commit()
    print(json.dumps({"checked": len(rows), "kept": kept, "blocked_to_automation": blocked,
                      "dropped": gone, "dry_run": a.dry_run}))


if __name__ == "__main__":
    main()
