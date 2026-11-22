#!/usr/bin/env python3
"""wayback_990 — the 990 PDFs an organisation published and later took down.

The self-posted-990 harvest reads only what is on the site today. Organisations redesign, move to a new
CMS and drop the `financials` page, and the Internet Archive often still holds the PDF. This asks the
Wayback CDX index for archived URLs on each organisation's own domain whose path mentions 990, fetches the
snapshot with the `id_` modifier (which returns the original bytes, not the Archive's wrapper), and cuts
Part III exactly as the other PDF paths do.

A snapshot is evidence in its own right and the archive URL is recorded, so a row can be traced to the
capture it came from.

Run: python3 scripts/wayback_990.py [--limit N] [--workers 4] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, now  # noqa: E402
from ingest.domains.efile_pdf import part3_text  # noqa: E402

UA = {"User-Agent": "homeless-atlas/1.0 (public-record research; archive.org)"}
CDX = ("http://web.archive.org/cdx/search/cdx?url={host}&matchType=domain&output=json"
       "&filter=original:.*990.*\\.pdf&filter=statuscode:200&collapse=urlkey&limit=4")


def cdx(host: str) -> list[tuple[str, str]]:
    """[(timestamp, original_url)] for archived 990 PDFs on this host."""
    for attempt in range(2):
        try:
            raw = urllib.request.urlopen(CDX.format(host=urllib.parse.quote(host)), timeout=45).read()
            rows = json.loads(raw) if raw.strip() else []
            return [(r[1], r[2]) for r in rows[1:]] if len(rows) > 1 else []
        except Exception:
            time.sleep(3)
    return []


def fetch_pdf(ts: str, url: str) -> str:
    snap = f"https://web.archive.org/web/{ts}id_/{url}"
    try:
        raw = urllib.request.urlopen(urllib.request.Request(snap, headers=UA), timeout=60).read()
    except Exception:
        return ""
    if not raw[:5].startswith(b"%PDF"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tf:
        tf.write(raw)
        tf.flush()
        try:
            out = subprocess.run(["pdftotext", "-layout", tf.name, "-"], capture_output=True, timeout=120)
        except Exception:
            return ""
    mission, _ = part3_text(out.stdout.decode("utf-8", "replace"))
    return mission


def one(org: dict) -> tuple[dict, str, str]:
    host = urllib.parse.urlparse(org["website"] or "").netloc
    if not host:
        return org, "", ""
    for ts, url in cdx(host):
        text = fetch_pdf(ts, url)
        if text:
            year = None
            m = re.search(r"(20\d\d)", url)
            if m:
                year = int(m.group(1))
            return org, text, f"https://web.archive.org/web/{ts}/{url}#{year or ''}"
    return org, "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS wayback_check(
        org_id INTEGER PRIMARY KEY, snapshot TEXT, chars INTEGER, checked_at TEXT)""")
    conn.commit()
    seen = {r[0] for r in conn.execute("SELECT org_id FROM wayback_check")}
    rows = [dict(r) for r in conn.execute(
        """SELECT o.id AS org_id, o.name, o.ein, o.website FROM org o
            WHERE o.website IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM org_program g WHERE g.org_id = o.id)""")]
    rows = [r for r in rows if r["org_id"] not in seen]
    if a.limit:
        rows = rows[: a.limit]
    print(f"organisations to check in the archive: {len(rows):,}", flush=True)
    written = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for i, (org, text, snapshot) in enumerate(pool.map(one, rows), 1):
            if text and not a.dry_run:
                year = None
                m = re.search(r"#(20\d\d)", snapshot)
                if m:
                    year = int(m.group(1))
                conn.execute(
                    """INSERT OR IGNORE INTO org_program(org_id, year, ein, mission, program_text,
                                                         expenses_program, revenue_total, source_id)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (org["org_id"], year, org["ein"], text, "", None, None, None))
                written += conn.total_changes and 1 or 0
            if not a.dry_run:
                conn.execute("INSERT OR REPLACE INTO wayback_check VALUES(?,?,?,?)",
                             (org["org_id"], snapshot, len(text), now()))
                if i % 25 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(rows)}] found={written}", flush=True)
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org_program").fetchone()[0]
    print(json.dumps({"checked": len(rows), "written": written, "program_rows_now": total}))


if __name__ == "__main__":
    main()
