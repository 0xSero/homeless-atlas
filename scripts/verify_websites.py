#!/usr/bin/env python3
"""verify_websites — re-prove every website on an organisation record that has no check row.

The archive now carries 4,618 organisation websites and 2,509 contact addresses, gathered by six
different mechanisms. Each mechanism recorded its own evidence table, but 2,025 websites and 830
addresses predate those tables — they came from the first ingests and carry no trace of the fetch that
justified them.

This walks those records, fetches each URL once, and records what came back: HTTP status, whether the
page still mentions the organisation, and when it was checked. A URL that no longer answers is cleared
from the record — an archive that keeps a dead link is worse than one that admits it has none — and the
attempt stays in `org_web_check` as the evidence for that decision.

Run: python3 scripts/verify_websites.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch, now  # noqa: E402

WORD_STOP = {"the", "of", "and", "for", "inc", "incorporated", "llc", "corp", "co", "services",
             "service", "center", "centre", "community", "housing", "shelter", "home", "homes",
             "family", "families", "program", "programs", "project", "projects", "group", "county",
             "city", "department", "authority", "ministries", "mission", "association"}


def check(org: dict) -> tuple[dict, int, int]:
    toks = [t.lower() for t in re.split(r"[^A-Za-z0-9]+", org["name"] or "")
            if len(t) > 3 and t.lower() not in WORD_STOP]
    try:
        body = fetch(org["website"], domain="web_check", timeout=12).decode("utf-8", "replace")
        status = 200
    except Exception as e:
        msg = str(e)
        status = 404 if "404" in msg else 0
        return org, status, 0
    low = body[:400_000].lower()
    flat = re.sub(r"[^a-z0-9]", "", low)
    name_key = "".join(toks)
    matched = 0
    if name_key and len(name_key) >= 8 and name_key in flat:
        matched = 1
    elif toks and sum(1 for t in toks if t in low) >= max(1, len(toks) // 2):
        matched = 1
    return org, status, matched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--emails", action="store_true",
                    help="instead of websites, re-prove contact addresses that carry no evidence")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS org_web_check(
        org_id INTEGER PRIMARY KEY, url TEXT, status INTEGER, matched INTEGER, checked_at TEXT)""")
    conn.commit()
    if a.emails:
        rows = [dict(r) for r in conn.execute(
            """SELECT o.id, o.name, o.website, o.email FROM org o
                WHERE o.email IS NOT NULL AND o.website IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM org_email_mine m WHERE m.org_id=o.id)
                  AND NOT EXISTS (SELECT 1 FROM org_site_check s WHERE s.org_id=o.id AND s.email IS NOT NULL)""")]
        if a.limit:
            rows = rows[: a.limit]
        print(f"addresses to re-prove: {len(rows):,}", flush=True)
        kept = cleared = 0

        def one(r):
            try:
                body = fetch(r["website"], domain="web_check", timeout=12).decode("utf-8", "replace")
            except Exception:
                try:
                    body = fetch(r["website"].rstrip("/") + "/contact", domain="web_check",
                                 timeout=12).decode("utf-8", "replace")
                except Exception:
                    return r, False
            return r, r["email"].lower() in body[:500_000].lower()

        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, (r, ok) in enumerate(pool.map(one, rows), 1):
                if ok:
                    kept += 1
                else:
                    cleared += 1
                if not a.dry_run:
                    if ok:
                        conn.execute("INSERT OR REPLACE INTO org_email_mine VALUES(?,?,?,?,?)",
                                     (r["id"], r["email"], "", r["website"], now()))
                    else:
                        conn.execute("UPDATE org SET email=NULL, updated_at=? WHERE id=?",
                                     (now(), r["id"]))
                    if i % 50 == 0:
                        conn.commit()
        if not a.dry_run:
            conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM org WHERE email IS NOT NULL").fetchone()[0]
        print(json.dumps({"checked": len(rows), "confirmed": kept, "cleared": cleared,
                          "emails_total_now": total}))
        return
    seen = {r[0] for r in conn.execute("SELECT org_id FROM org_web_check")}
    rows = [dict(r) for r in conn.execute(
        """SELECT o.id, o.name, o.website FROM org o
            WHERE o.website IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM org_domain_mine m WHERE m.org_id=o.id AND m.matched=1)
              AND NOT EXISTS (SELECT 1 FROM org_site_check s WHERE s.org_id=o.id AND s.accepted=1)
              AND NOT EXISTS (SELECT 1 FROM org_gov_site g WHERE g.org_id=o.id AND g.matched=1)""")]
    rows = [r for r in rows if r["id"] not in seen]
    if a.limit:
        rows = rows[: a.limit]
    print(f"to re-check: {len(rows):,}", flush=True)
    dead = 0
    unmatched = 0
    live = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, (org, status, matched) in enumerate(pool.map(check, rows), 1):
            if status == 200:
                live += 1
            else:
                dead += 1
            if status == 200 and not matched:
                unmatched += 1
            if not a.dry_run:
                if status != 200:
                    conn.execute("UPDATE org SET website=NULL, updated_at=? WHERE id=?",
                                 (now(), org["id"]))
                conn.execute("INSERT OR REPLACE INTO org_web_check VALUES(?,?,?,?,?)",
                             (org["id"], org["website"], status, matched, now()))
                if i % 50 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(rows)}] live={live} dead={dead}", flush=True)
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0]
    print(json.dumps({"checked": len(rows), "live": live, "dead": dead,
                      "live_but_name_unmatched": unmatched, "websites_total_now": total}))


if __name__ == "__main__":
    main()
