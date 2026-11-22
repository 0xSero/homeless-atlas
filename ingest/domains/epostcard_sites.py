#!/usr/bin/env python3
"""epostcard_sites — the website an organisation itself declares on its IRS 990-N filing.

The IRS publishes every Form 990-N (e-Postcard) with the website the filer entered. It is a government
registry keyed by EIN, which is the one thing every name-guessing source lacks, and it covers exactly the
small filers that make up this archive's gap: 1,176 organisations here have no website and do have a
990-N website on file.

Each URL is normalised, fetched, and only written when the page names the organisation (whole name in the
<title>/<h1>, or its distinctive words together with its city). The registry row is the evidence and the
check is recorded in `epostcard_check`.

Data: data/raw/epostcard/data-download-epostcard.txt (pipe-delimited, field 7 is the website).
Run: python3 -m ingest.domains.epostcard_sites [--limit N] [--dry-run]
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

WORD_STOP = {"the", "of", "and", "for", "inc", "incorporated", "llc", "corp", "co", "services",
             "service", "center", "centre", "community", "housing", "shelter", "home", "homes",
             "family", "families", "program", "programs", "project", "projects", "group", "county",
             "city", "department", "authority", "ministries", "mission", "association", "society",
             "foundation", "council", "agency", "alliance", "network", "partners", "coalition"}


def norm_url(u: str) -> str:
    u = (u or "").strip()
    if not u or " " in u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    m = re.match(r"(https?://)([^/]+)(.*)", u)
    if not m:
        return ""
    return m.group(1) + m.group(2).lower() + m.group(3)


def one(org: dict) -> tuple[dict, str, int]:
    url = norm_url(org["declared"])
    if not url:
        return org, "", 0
    try:
        body = fetch(url, domain="epostcard", timeout=12).decode("utf-8", "replace")
    except Exception:
        return org, url, 0
    title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I) or \
        re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
    title = re.sub(r"[^a-z0-9]", "", (title_m.group(1) if title_m else "").lower())
    low = re.sub(r"[^a-z0-9 ]", " ", body[:400_000].lower())
    flat = low.replace(" ", "")
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", org["name"] or "")
            if len(t) > 3 and t.lower() not in WORD_STOP]
    key = "".join(t.lower() for t in toks)
    city = (org["city"] or "").lower().strip()
    if key and len(key) >= 10 and (key in title or key in flat):
        return org, url, 1
    if toks and all(t.lower() in low for t in toks) and (not city or city in low):
        return org, url, 1
    if toks and city and city in low and sum(1 for t in toks if t.lower() in low) >= max(1, len(toks) // 2):
        return org, url, 2
    return org, url, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS epostcard_check(
        org_id INTEGER PRIMARY KEY, ein TEXT, url TEXT, status INTEGER, matched INTEGER, checked_at TEXT)""")
    conn.commit()
    seen = {r[0] for r in conn.execute("SELECT org_id FROM epostcard_check WHERE matched>0")}
    rows = [dict(r) for r in conn.execute(
        """SELECT o.id AS org_id, o.name, o.ein, o.city, o.state, s.url AS declared, s.year
             FROM org o JOIN org_990n_site s ON s.ein = o.ein
            WHERE o.website IS NULL
            ORDER BY s.year DESC""")]
    rows = [r for r in rows if r["org_id"] not in seen]
    if a.limit:
        rows = rows[: a.limit]
    print(f"organisations with an IRS-declared website: {len(rows):,}", flush=True)
    written = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for i, (org, url, matched) in enumerate(pool.map(one, rows), 1):
            if matched and not a.dry_run:
                conn.execute("UPDATE org SET website=?, updated_at=? WHERE id=?", (url, now(), org["org_id"]))
                written += 1
            if not a.dry_run:
                conn.execute("INSERT OR REPLACE INTO epostcard_check VALUES(?,?,?,?,?,?)",
                             (org["org_id"], org["ein"], url, 200 if matched else 0, matched, now()))
                if i % 50 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(rows)}] written={written}", flush=True)
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0]
    print(json.dumps({"tried": len(rows), "written": written, "websites_total_now": total}))


if __name__ == "__main__":
    main()
