#!/usr/bin/env python3
"""org_web / part3 — one pass over the IRS e-file XML that fills three gaps at once.

From a single filing XML per organisation we take:

    WebsiteAddressTxt                -> org.website   (written only after the URL answers)
    an email address in the filing   -> org.email
    Part III mission + programme text -> org_program  (verbatim, for the "what they say they do" layer)
    TotalProgramServiceExpensesAmt   -> org_program.expenses_program (what share of spend is programme)

The index CSVs list every e-filed return by year. We filter to the EINs this archive already holds,
take the most recent tax period per EIN, and fetch only those XMLs — one request per organisation
instead of one per filing.

Every response is cached under data/raw/efile/, a website is written only after a 200/301/302 (403/429
count as live-but-blocked), and the run is resumable: an EIN that has been processed is recorded in
`efile_progress`.

Run: python3 -m ingest.domains.efile [--years 2026 2025 2024 2023] [--limit EINs] [--selftest]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ingest.common import DB, fetch, name_norm, now, source_id_for  # noqa: E402

DOMAIN = "efile"
INDEX_URL = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv"
XML_URL = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{oid}_public.xml"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")

FIELDS = ("WebsiteAddressTxt", "MissionDesc", "DescriptionProgramSrvcDescr",
          "TotalProgramServiceExpensesAmt", "ActivityOrMissionDesc", "EmailAddress", "Email",
          "WebsiteAddress", "TotalRevenueColumn", "TotalExpensesColumn", "Organization501c3Indicator")

SCHEMA = """
CREATE TABLE IF NOT EXISTS org_program(
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL,
  year INTEGER,
  ein TEXT,
  mission TEXT,
  program_text TEXT,
  expenses_program REAL,
  revenue_total REAL,
  source_id INTEGER,
  UNIQUE(org_id, year)
);
CREATE TABLE IF NOT EXISTS efile_progress(
  ein TEXT PRIMARY KEY,
  year INTEGER, object_id TEXT, status TEXT, fields TEXT, checked_at TEXT
);
"""


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml(raw: bytes) -> dict:
    """Pull the fields we want out of an e-file XML without assuming a namespace or a schema version."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    found: dict[str, list[str]] = {}
    for el in root.iter():
        tag = strip_ns(el.tag)
        if tag in FIELDS and (el.text or "").strip():
            found.setdefault(tag, []).append(el.text.strip())
    return found


def clean_text(s: str, limit: int = 4000) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + " …(truncated)"


def live(url: str) -> int:
    """0 means no answer; 403/429 mean a bot wall, not a dead site."""
    for ua in ("homeless-atlas/1.0", UA):
        try:
            r = fetch(url, domain="org_web_check", headers={"User-Agent": ua}, timeout=25)
            return 200 if r else 0
        except Exception:
            continue
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="*", default=[2026, 2025, 2024, 2023])
    ap.add_argument("--limit", type=int, default=None, help="stop after N EINs (for a smoke test)")
    ap.add_argument("--no-verify", action="store_true", help="skip the live-URL check (debug only)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.executescript(SCHEMA)
    conn.commit()
    src = source_id_for(conn, "https://www.irs.gov/charities-non-profits/form-990-series-downloads", DOMAIN,
                        title="IRS Form 990 e-file XML", publisher="Internal Revenue Service")

    want = {r[0]: r[1] for r in conn.execute(
        "SELECT ein, id FROM org WHERE ein IS NOT NULL AND ein != ''").fetchall()}
    done = {r[0] for r in conn.execute("SELECT ein FROM efile_progress WHERE status='ok'").fetchall()}
    print(f"organisations with an EIN: {len(want):,}  already processed: {len(done):,}")

    # The IRS publishes the XML in per-year batch ZIPs, not as individual files, so the index tells us
    # which batch holds each organisation's latest return and we download only those batches.
    latest: dict[str, tuple[str, str, str]] = {}          # ein -> (tax_period, batch, object_id)
    for year in a.years:
        url = INDEX_URL.format(year=year)
        try:
            body = fetch(url, domain=DOMAIN, timeout=600)
        except Exception as e:
            print(f"  index {year}: {type(e).__name__}")
            continue
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))))
        print(f"  index {year}: {len(rows):,} filings")
        for r in rows:
            ein = (r.get("EIN") or "").strip()
            if ein not in want or ein in done:
                continue
            period = (r.get("TAX_PERIOD") or "").strip()
            batch = (r.get("XML_BATCH_ID") or "").strip()
            oid = (r.get("OBJECT_ID") or "").strip()
            if not (batch and oid):
                continue
            prev = latest.get(ein)
            if prev is None or period > prev[0]:
                latest[ein] = (period, batch, oid)
    print(f"latest filings to fetch: {len(latest):,}")

    # one zip per batch, entries extracted only for the object ids we asked for
    needed: dict[str, set[str]] = {}
    for ein, (period, batch, oid) in latest.items():
        needed.setdefault(batch, set()).add(oid)
    print(f"batches to download: {len(needed)}")
    xml_dir = Path("data/raw") / DOMAIN / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    oid_to_ein = {oid: ein for ein, (period, batch, oid) in latest.items()}
    for batch, oids in needed.items():
        # the index writes some batches with a lowercase suffix (05a) while the published file is 05A
        zbatch = batch.upper()
        zpath = Path("data/raw") / DOMAIN / f"{zbatch}.zip"
        if not zpath.exists():
            zurl = f"https://apps.irs.gov/pub/epostcard/990/xml/{zbatch[:4]}/{zbatch}.zip"
            try:
                data = fetch(zurl, domain=DOMAIN, timeout=1800)
            except Exception as e:
                print(f"  {batch}: {type(e).__name__}")
                continue
            if not data[:2] == b"PK":            # an HTML error page saved as a zip is not a zip
                print(f"  {batch}: not a zip ({len(data)} bytes)")
                continue
            zpath.write_bytes(data)
        # Python's zipfile raises NotImplementedError on some of the IRS archives, so shell out to
        # unzip, which handles every compression method they publish.
        try:
            import subprocess
            listing = subprocess.run(["unzip", "-Z1", str(zpath)], capture_output=True, text=True, timeout=600)
            entries = [n for n in listing.stdout.splitlines() if n.strip()]
            wanted = [n for n in entries if Path(n).stem.replace("_public", "") in oids]
            if not entries:
                print(f"  {batch}: zip listing empty (stderr={listing.stderr.strip()[:120]}, "
                      f"size={zpath.stat().st_size})")
            elif not wanted:
                sample = entries[:2]
                print(f"  {batch}: {len(entries)} entries, none matched {len(oids)} oids; "
                      f"e.g. {sample} vs oid sample {list(oids)[:2]}")
            for i in range(0, len(wanted), 200):
                # -d must precede the member list, or unzip reads the directory as a member name
                r = subprocess.run(["unzip", "-o", "-j", "-q", str(zpath), "-d", str(xml_dir),
                                    *wanted[i:i + 200]], capture_output=True, text=True, timeout=900)
                if r.returncode:
                    print(f"    unzip rc={r.returncode} err={r.stderr.strip()[:160]}")
            # unzip -j keeps the published basename (…_public.xml); the parser looks for <oid>.xml
            for n in wanted:
                base = Path(n).name
                if base.endswith("_public.xml"):
                    src_p = xml_dir / base
                    dst_p = xml_dir / (base[: -len("_public.xml")] + ".xml")
                    if src_p.exists() and not dst_p.exists():
                        src_p.rename(dst_p)
        except Exception as e:
            print(f"  {batch}: unzip {type(e).__name__}")
        finally:
            zpath.unlink(missing_ok=True)   # 100 MB per batch; keep only the extracted filings
            # the fetch cache also keeps a copy of every archive; drop it too or the disk fills
            import hashlib
            ck = hashlib.sha1(("GET" + zurl + "{}").encode()).hexdigest()
            for suffix in (".bin", ".meta.json"):
                (Path("data/raw") / DOMAIN / f"{ck}{suffix}").unlink(missing_ok=True)
        print(f"  {batch}: {len(oids)} wanted, {sum(1 for o in oids if (xml_dir / f'{o}.xml').exists())} extracted",
              flush=True)

    if a.limit:
        latest = dict(list(latest.items())[: a.limit])

    stats = {"xml": 0, "no_fields": 0, "website": 0, "email": 0, "program": 0, "fail": 0}
    for i, (ein, (period, batch, oid)) in enumerate(sorted(latest.items()), 1):
        fpath = xml_dir / f"{oid}.xml"
        try:
            raw = fpath.read_bytes()
        except Exception as e:
            conn.execute("INSERT OR REPLACE INTO efile_progress VALUES(?,?,?,?,?,?)",
                         (ein, int(period[:4]) if period[:4].isdigit() else None, oid,
                          f"error:{type(e).__name__}", "", now()))
            conn.commit()
            stats["fail"] += 1
            continue
        stats["xml"] += 1
        f = parse_xml(raw)
        if not f:
            conn.execute("INSERT OR REPLACE INTO efile_progress VALUES(?,?,?,?,?,?)",
                         (ein, int(period[:4]) if period[:4].isdigit() else None, oid, "no_fields", "", now()))
            conn.commit()
            stats["no_fields"] += 1
            continue

        org_id = want[ein]
        web = next((v[0] for k in ("WebsiteAddressTxt", "WebsiteAddress") if (v := f.get(k))), "")
        email = next((v[0] for k in ("EmailAddress", "Email") if (v := f.get(k))), "")
        mission = clean_text((f.get("MissionDesc") or f.get("ActivityOrMissionDesc") or [""])[0])
        progtext = clean_text(" ".join(f.get("DescriptionProgramSrvcDescr") or []))
        exp_prog = (f.get("TotalProgramServiceExpensesAmt") or [None])[0]
        rev = (f.get("TotalRevenueColumn") or [None])[0]

        sets, vals = [], []
        if web and not a.no_verify and re.match(r"^https?://", web):
            if live(web):
                sets.append("website=?"); vals.append(web); stats["website"] += 1
        if email and re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", email, re.I):
            sets.append("email=?"); vals.append(email); stats["email"] += 1
        if sets:
            sets.append("updated_at=?"); vals.append(now())
            conn.execute(f"UPDATE org SET {', '.join(sets)} WHERE id=?", (*vals, org_id))

        if mission or progtext:
            conn.execute("""INSERT INTO org_program(org_id, year, ein, mission, program_text,
                                       expenses_program, revenue_total, source_id)
                            VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(org_id, year) DO UPDATE SET
                              mission=excluded.mission, program_text=excluded.program_text,
                              expenses_program=excluded.expenses_program,
                              revenue_total=excluded.revenue_total, source_id=excluded.source_id""",
                         (org_id, int(period[:4]) if period[:4].isdigit() else None, ein,
                          mission, progtext,
                          float(exp_prog) if exp_prog and re.match(r"^-?[\d.]+$", exp_prog) else None,
                          float(rev) if rev and re.match(r"^-?[\d.]+$", rev) else None, src))
            stats["program"] += 1

        # "ok" means the filing was read and produced something. A filing with no Part III text is
        # marked no_fields, not ok, so a later sweep retries it instead of skipping it forever.
        status = "ok" if (mission or progtext) else "no_fields"
        conn.execute("INSERT OR REPLACE INTO efile_progress VALUES(?,?,?,?,?,?)",
                     (ein, int(period[:4]) if period[:4].isdigit() else None, oid, status,
                      ",".join(sorted(f)), now()))
        if i % 25 == 0:
            conn.commit()
            print(f"  [{i}/{len(latest)}] xml={stats['xml']} web={stats['website']} "
                  f"mail={stats['email']} prog={stats['program']} fail={stats['fail']}", flush=True)
    conn.commit()
    print(json.dumps(stats))
    print("totals:",
          conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0], "websites,",
          conn.execute("SELECT COUNT(*) FROM org WHERE email IS NOT NULL").fetchone()[0], "emails,",
          conn.execute("SELECT COUNT(*) FROM org_program").fetchone()[0], "program rows")


if __name__ == "__main__":
    main()
