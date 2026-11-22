#!/usr/bin/env python3
"""efile_pdf — Part III program text from scanned 990 PDFs, for organisations the XML indexes lost.

The IRS e-file XML indexes only reach back so far, and 7,191 organisations in this archive have no
e-filed return in any index year. ProPublica's Nonprofit Explorer mirrors the filings the IRS does not
still serve as XML — including 2011–2018 — and exposes a `pdf_url` per filing. Those PDFs carry the
same Part III, "Statement of Program Service Accomplishments", that the XML parse reads.

Per organisation: ask the API for its filings, take the newest 990 that has a PDF, extract the text with
`pdftotext`, cut the Part III block, and write it. 1 req/s, every response cached under
data/raw/efile_pdf/, progress recorded in efile_pdf_progress so a re-run resumes.

Run: python3 -m ingest.domains.efile_pdf [--limit N] [--no-verify]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import db, fetch, now  # noqa: E402

DOMAIN = "efile_pdf"
API = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
UA = {"User-Agent": "homeless-atlas/1.0 (public-record research)"}


def part3_text(txt: str) -> tuple[str, str]:
    """Return (mission, programs) from a 990 PDF's text, or ('', '') when Part III is absent."""
    flat = re.sub(r"[ \t]+", " ", txt)
    m = re.search(r"Part\s*III\b.*?Statement of Program Service Accomplishments", flat, re.I | re.S)
    start = m.end() if m else None
    if start is None:
        m2 = re.search(r"Statement of Program Service Accomplishments", flat, re.I)
        if not m2:
            return "", ""
        start = m2.end()
    # "Schedule O" appears in the header line of Part III itself ("Check if Schedule O contains a
    # response"), so it cannot be the terminator: Part IV is.
    end = re.search(r"Part\s*IV\b", flat[start:])
    block = flat[start: start + (end.start() if end else 12000)]
    mm = re.search(r"Briefly describe the organization", block, re.I)
    if mm:
        block = block[mm.start():]
    block = re.sub(r"\n{2,}", "\n", block)
    lines = []
    for line in block.split("\n"):
        s = line.strip()
        if not s or re.fullmatch(r"[\d\W_]+", s):
            continue
        if re.search(r"(Check this box|Form 990 \(|Page \d|SEE ADDITIONAL|^\d{1,3}\.?$"
                     r"|Did the organization|If \"Yes,\"|~~~)", s, re.I):
            continue
        lines.append(s)
    body = " ".join(lines)
    body = re.sub(r"\(Code:.*?\)\s*\(Expenses:.*?\)", " ", body)
    body = re.sub(r"\(Code:\s*[A-Z0-9 ,]*\)", " ", body)
    body = re.sub(r"\(Expenses:\s*\$?[\d,]+\.?\)", " ", body)
    body = re.sub(r"\s{2,}", " ", body).strip(" .-")
    if len(body) < 80:
        return "", ""
    return body[:6000], ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args()
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS efile_pdf_progress(
        ein TEXT PRIMARY KEY, status TEXT, year INTEGER, pdf TEXT, chars INTEGER, checked_at TEXT)""")
    conn.commit()
    # error rows are retried: a failed download must never look like a completed check
    done = {r[0] for r in conn.execute(
        "SELECT ein FROM efile_pdf_progress WHERE status NOT LIKE 'error%'")}
    have = {r[0] for r in conn.execute("SELECT DISTINCT ein FROM org_program")}
    rows = [r for r in conn.execute(
        """SELECT o.id AS org_id, o.ein, o.name FROM org o
            WHERE o.ein IS NOT NULL AND o.ein != '' ORDER BY o.id""")
        if r["ein"] not in done and r["ein"] not in have]
    if a.limit:
        rows = rows[: a.limit]
    print(f"organisations to try: {len(rows):,}", flush=True)

    stats = {"api": 0, "pdf": 0, "program": 0, "no_filings": 0, "no_part3": 0, "fail": 0}
    for i, r in enumerate(rows, 1):
        ein = r["ein"]
        year, pdf, mission, status = None, "", "", "no_filings"
        try:
            body = fetch(API.format(ein=ein), domain=DOMAIN, headers=UA, timeout=40)
            stats["api"] += 1
            d = json.loads(body)
            filings = [f for f in (d.get("filings_with_data") or []) if f.get("pdf_url")]
            filings.sort(key=lambda f: (f.get("tax_prd_yr") or 0), reverse=True)
            if filings:
                f = filings[0]
                year, pdf = f.get("tax_prd_yr"), f["pdf_url"]
                raw = fetch(pdf, domain=DOMAIN, headers=UA, timeout=90)
                stats["pdf"] += 1
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tf:
                    tf.write(raw)
                    tf.flush()
                    out = subprocess.run(["pdftotext", "-layout", tf.name, "-"],
                                         capture_output=True, timeout=120)
                mission, _ = part3_text(out.stdout.decode("utf-8", "replace"))
                if mission:
                    status = "ok"
                    stats["program"] += 1
                    conn.execute(
                        """INSERT INTO org_program(org_id, year, ein, mission, program_text,
                                                   expenses_program, revenue_total, source_id)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (r["org_id"], year, ein, mission, "", None, None, None))
                else:
                    status = "no_part3"
                    stats["no_part3"] += 1
        except Exception as e:
            status = f"error:{type(e).__name__}"
            stats["fail"] += 1
        conn.execute("INSERT OR REPLACE INTO efile_pdf_progress VALUES(?,?,?,?,?,?)",
                     (ein, status, year, pdf, len(mission), now()))
        if i % 25 == 0:
            conn.commit()
            print(f"  [{i}/{len(rows)}] program={stats['program']} pdf={stats['pdf']}", flush=True)
        time.sleep(1)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org_program").fetchone()[0]
    print(json.dumps({**stats, "orgs_attempted": len(rows), "program_rows_now": total}))


if __name__ == "__main__":
    main()
