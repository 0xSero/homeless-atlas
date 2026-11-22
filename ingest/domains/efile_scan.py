#!/usr/bin/env python3
"""efile_scan — read Part III out of the year archives the indexes cannot address.

From 2019 to 2023 the IRS index has no `XML_BATCH_ID`, so `ingest/domains/efile.py` cannot say which
archive holds a given filing and skips it. The archives themselves are still published, one per batch, and
a batch is only ~120 MB and lists 21,000 files: cheap enough to open and look inside for the object ids we
want.

Per year: read the index, collect the object ids of filings whose EIN still has no program text, then walk
every batch archive `{year}_TEOS_XML_{NN}{A|B}.zip`, extract only those entries, parse Part III with the
same extractor the XML ingestor uses, and delete the archive. Progress is recorded so a re-run resumes.

Run: python3 -m ingest.domains.efile_scan --years 2023 2022 [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ingest.common import db, fetch, now  # noqa: E402

BASE = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{year}_TEOS_XML_{batch}.zip"
CACHE = ROOT / "data" / "raw" / "efile_scan"


def part3_from_xml(text: str) -> tuple[str, str]:
    """(mission, programs) from a 990 XML, mirroring what efile.py reads."""
    m = re.search(r"<MissionDesc>(.*?)</MissionDesc>", text, re.S)
    d = re.search(r"<DescriptionProgramSrvcAccomplishment(?:Txt|Grp)?>(.*?)</", text, re.S)
    clean = lambda s: re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()
    mission = clean(m.group(1)) if m else ""
    prog = clean(d.group(1)) if d else ""
    return mission, prog


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2023, 2022])
    ap.add_argument("--max-batch", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS efile_scan_progress(
        year INTEGER, batch TEXT, files INTEGER, hits INTEGER, checked_at TEXT,
        PRIMARY KEY(year, batch))""")
    conn.commit()
    CACHE.mkdir(parents=True, exist_ok=True)
    done_batches = set(conn.execute("SELECT year||'/'||batch FROM efile_scan_progress"))
    total_written = 0
    for year in a.years:
        # which of our filings are still missing, and what are their object ids
        idx = fetch(f"https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv",
                    domain="efile", timeout=300)
        ours = {r[0] for r in conn.execute(
            """SELECT o.ein FROM org o WHERE o.ein IS NOT NULL AND o.ein != ''
                 AND NOT EXISTS (SELECT 1 FROM org_program g WHERE g.org_id = o.id)""")}
        want: dict[str, tuple] = {}
        for r in csv.DictReader(io.StringIO(idx.decode("utf-8", "replace"))):
            if r["EIN"] in ours and r.get("OBJECT_ID"):
                want[r["OBJECT_ID"]] = (r["EIN"], r.get("TAX_PERIOD", ""), r.get("TAXPAYER_NAME", ""))
        print(f"{year}: {len(want)} filings to find for {len({v[0] for v in want.values()})} organisations",
              flush=True)
        if not want:
            continue
        misses = 0
        for n in range(1, a.max_batch + 1):
            for suffix in ("A",):          # the B halves have never existed in any published year
                batch = f"{n:02d}{suffix}"
                if f"{year}/{batch}" in done_batches:
                    continue
                url = BASE.format(year=year, batch=batch)
                zp = CACHE / f"{year}_{batch}.zip"
                try:
                    raw = fetch(url, domain="efile_scan", timeout=60)   # a missing batch redirects fast
                except Exception:
                    misses += 1
                    if misses >= 3:
                        print(f"  {year}: three batches in a row unavailable, year is finished", flush=True)
                        break
                    continue
                misses = 0
                if len(raw) < 10_000:
                    continue
                zp.write_bytes(raw)
                hits = 0
                try:
                    with zipfile.ZipFile(zp) as z:
                        for name in z.namelist():
                            m = re.search(r"(\d{18})_public\.xml$", name)
                            if not m or m.group(1) not in want:
                                continue
                            body = z.read(name).decode("utf-8", "replace")
                            ein, period, tname = want[m.group(1)]
                            mission, prog = part3_from_xml(body)
                            if not (mission or prog):
                                continue
                            yr = int(period[:4]) if period[:4].isdigit() else year
                            row = conn.execute(
                                """SELECT id FROM org WHERE ein=? LIMIT 1""", (ein,)).fetchone()
                            if not row:
                                continue
                            cur = conn.execute(
                                """INSERT OR IGNORE INTO org_program(org_id, year, ein, mission,
                                       program_text, expenses_program, revenue_total, source_id)
                                   VALUES(?,?,?,?,?,?,?,?)""",
                                (row[0], yr, ein, mission[:6000], prog[:6000], None, None, None))
                            hits += cur.rowcount
                finally:
                    zp.unlink(missing_ok=True)
                    # fetch() also caches every archive under data/raw/<domain>/; it is 100-250 MB a
                    # copy and the XML inside is already parsed, so drop those too
                    for blob in CACHE.glob("*.bin"):
                        blob.unlink(missing_ok=True)
                    for blob in CACHE.glob("*.meta.json"):
                        blob.unlink(missing_ok=True)
                conn.execute("INSERT OR REPLACE INTO efile_scan_progress VALUES(?,?,?,?,?)",
                             (year, batch, len(want), hits, now()))
                conn.commit()
                total_written += hits
                print(f"  {year} {batch}: {hits} part III texts (running total {total_written})",
                      flush=True)
            if n >= a.max_batch:
                break
    total = conn.execute("SELECT COUNT(*) FROM org_program").fetchone()[0]
    print(json.dumps({"written": total_written, "program_rows_now": total}))


if __name__ == "__main__":
    main()
