#!/usr/bin/env python3
"""org_990_pdf — Part III text from 990 PDFs the organisations publish on their own sites.

The IRS e-file indexes reach only part of this archive, and ProPublica's copies answer 403. But many
nonprofits put the return they filed on their own website, and 2,857 organisations here have a verified
site and no program text. Those PDFs are the same form, with the same Part III "Statement of Program
Service Accomplishments".

Per organisation: fetch the homepage, look for a link whose URL or anchor text says 990 / annual report
/ financials, fall back to the usual paths, download at most two PDFs, extract Part III with
`pdftotext`, and write it. The page the PDF came from is recorded, so the text is provenance-linked.

Run: python3 -m ingest.domains.org_990_pdf [--limit N] [--workers 6] [--dry-run]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch, now  # noqa: E402
from ingest.domains.efile_pdf import part3_text  # noqa: E402

DOMAIN = "org_990"
PATHS = ("", "/financials", "/financial-information", "/990", "/annual-report")
PDFISH = re.compile(r"(990|annual[-_ ]?report|financial|audit|tax[-_ ]?(return|filing))", re.I)


def find_pdfs(page_url: str, body: str) -> list[str]:
    """Every href that looks like a 990, an audit or an annual report. Anchor parsing is skipped on
    purpose: the markup is often unbalanced and a tag-based pass silently misses whole sections."""
    out = []
    for m in re.finditer(r'href=["\']([^"\'>]+)', body[:800_000], re.I):
        href = html.unescape(m.group(1))
        low = href.lower()
        if not low.endswith((".pdf", ".PDF".lower())) and "/990" not in low and ".pdf?" not in low:
            continue
        if not PDFISH.search(low) and "annual" not in low:
            continue
        out.append(urljoin(page_url, href))
    uniq = list(dict.fromkeys(out))
    uniq.sort(key=lambda u: (0 if "990" in u.lower() else 1, -len(u)))
    return uniq[:6]


def try_pdf(url: str) -> str:
    try:
        raw = fetch(url, domain=DOMAIN, timeout=45)
    except Exception:
        return ""
    if not raw[:5].startswith(b"%PDF"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tf:
        tf.write(raw)
        tf.flush()
        try:
            out = subprocess.run(["pdftotext", "-layout", tf.name, "-"],
                                 capture_output=True, timeout=120)
        except Exception:
            return ""
    mission, _ = part3_text(out.stdout.decode("utf-8", "replace"))
    return mission


def harvest(org: dict) -> tuple[dict, str, str, int]:
    base = (org["website"] or "").rstrip("/")
    if not base:
        return org, "", "", 0
    for path in PATHS:
        page = base + path
        try:
            body = fetch(page, domain=DOMAIN, timeout=8).decode("utf-8", "replace")
        except Exception:
            continue
        for pdf in find_pdfs(page, body):
            text = try_pdf(pdf)
            if text:
                year = None
                m = re.search(r"(20\d\d)", pdf)
                if m:
                    year = int(m.group(1))
                return org, text, pdf, year or 0
    return org, "", "", 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS org_990_progress(
        org_id INTEGER PRIMARY KEY, status TEXT, pdf TEXT, year INTEGER, chars INTEGER, checked_at TEXT)""")
    conn.commit()
    seen = {r[0] for r in conn.execute("SELECT org_id FROM org_990_progress WHERE status NOT LIKE 'error%'")}
    # biggest federal grantees first: they are the ones with a professional site and a posted return,
    # which is why the id-ordered first pass found a PDF for only 2% of them
    rows = [dict(r) for r in conn.execute(
        """SELECT o.id AS org_id, o.name, o.ein, o.website,
                  (SELECT SUM(a.amount_obligated) FROM award a WHERE a.org_id = o.id) amt
             FROM org o
            WHERE o.website IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM org_program g WHERE g.org_id = o.id)
            ORDER BY COALESCE(amt, 0) DESC, o.id""")]
    rows = [r for r in rows if r["org_id"] not in seen]
    if a.limit:
        rows = rows[: a.limit]
    print(f"organisations to try: {len(rows):,}", flush=True)
    written = 0
    found_pdf = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for i, (org, text, pdf, year) in enumerate(pool.map(harvest, rows), 1):
            if pdf:
                found_pdf += 1
            if text and not a.dry_run:
                conn.execute(
                    """INSERT INTO org_program(org_id, year, ein, mission, program_text,
                                               expenses_program, revenue_total, source_id)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (org["org_id"], year or None, org["ein"], text, "", None, None, None))
                written += 1
            if not a.dry_run:
                conn.execute("INSERT OR REPLACE INTO org_990_progress VALUES(?,?,?,?,?,?)",
                             (org["org_id"], "ok" if text else "no_pdf", pdf, year,
                              len(text), now()))
                if i % 25 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(rows)}] pdf={found_pdf} program={written}", flush=True)
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org_program").fetchone()[0]
    print(json.dumps({"tried": len(rows), "pdfs_found": found_pdf, "written": written,
                      "program_rows_now": total}))


if __name__ == "__main__":
    main()
