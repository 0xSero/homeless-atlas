#!/usr/bin/env python3
"""efile_purpose — the organisation's own statement of purpose from the 990 header.

1,309 filings in this archive carry no Part III text: they are 990-EZ or 990-N filers, whose form has no
program-service section. But 964 of them do carry `PrimaryExemptPurposeTxt` — the "primary exempt
purpose" the organisation writes into the 990 header, which is the same kind of statement ("Serenity
Living Transitional Home provides housing for homeless girls, workforce training, community support")
and is often the only prose it files at all.

This is **not** Part III and is never counted as such: it is stored in its own table, labelled on the org
page as the 990 header field, and given its own ledger domain. Reading the XML that is already cached
costs no requests.

Run: python3 -m ingest.domains.efile_purpose [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import db, now  # noqa: E402


def clean(text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", text)
    t = t.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS org_purpose(
        org_id INTEGER PRIMARY KEY, ein TEXT, year INTEGER, text TEXT, source TEXT, written_at TEXT)""")
    conn.commit()
    need = {}
    for r in conn.execute("""SELECT o.id, o.ein FROM org o
                              WHERE o.ein IS NOT NULL AND o.ein != ''
                                AND NOT EXISTS (SELECT 1 FROM org_program g WHERE g.org_id = o.id)"""):
        need[r["ein"]] = r["id"]
    have = {r[0] for r in conn.execute("SELECT ein FROM org_purpose")}
    print(f"organisations without Part III text: {len(need):,}", flush=True)

    written = 0
    for f in glob.glob(str(ROOT / "data/raw/efile/**/*.xml"), recursive=True):
        try:
            body = open(f, errors="replace").read()
        except Exception:
            continue
        m = re.search(r"<EIN>(\d{9})</EIN>", body)
        if not m or m.group(1) not in need or m.group(1) in have:
            continue
        t = re.search(r"<PrimaryExemptPurposeTxt>(.*?)</PrimaryExemptPurposeTxt>", body, re.S)
        if not t:
            continue
        text = clean(t.group(1))
        if len(text) < 40:
            continue
        yr = re.search(r"<TaxPeriodEndDt>(\d{4})", body) or re.search(r"<TaxYr>(\d{4})</TaxYr>", body)
        year = int(yr.group(1)) if yr else None
        if not a.dry_run:
            conn.execute("INSERT OR REPLACE INTO org_purpose VALUES(?,?,?,?,?,?)",
                         (need[m.group(1)], m.group(1), year, text[:6000],
                          "PrimaryExemptPurposeTxt, Form 990 header", now()))
        have.add(m.group(1))
        written += 1
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org_purpose").fetchone()[0]
    print(json.dumps({"written": written, "rows_now": total, "dry_run": a.dry_run}))


if __name__ == "__main__":
    main()
