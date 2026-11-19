#!/usr/bin/env python3
"""Sweep USASpending for homelessness awards, year by year, and upsert them into `award`.

Why this exists: the domain ingestor's sweeps stopped at ~2017 (our table had 5,190 awards in FY2010
and 14 in FY2024 while a single FY2023-24 query returns 3,469 keyword grants and 12,662 grants under
assistance listings 14.231/14.267). This does a year-by-year, page-by-page sweep with its own resume
ledger so nothing is lost and nothing duplicates.

Run: python3 scripts/sweep_awards.py --from-fy 2018 --to-fy 2026 [--kinds grants contracts]
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch_post_json, name_norm, now, source_id_for  # noqa: E402

URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
COUNT = "https://api.usaspending.gov/api/v2/search/spending_by_award_count/"
DOMAIN = "usaspending_sweep"
FIELDS = ["Award ID", "Recipient Name", "Recipient UEI", "Award Amount", "Total Outlays",
          "Start Date", "End Date", "Awarding Agency", "Awarding Sub Agency", "Description",
          "CFDA Number", "NAICS", "PSC", "Place of Performance State Code", "Recipient Location State Code"]
GRANT_CODES = ["02", "03", "04", "05"]
CONTRACT_CODES = ["A", "B", "C", "D"]
PROGRAMS = ["14.231", "14.267", "14.239", "14.275"]
KEYWORDS = ["homeless", "homelessness", "continuum of care", "emergency solutions grant", "rapid rehousing",
            "permanent supportive housing", "HMIS", "housing first", "transitional housing",
            "street outreach", "coordinated entry", "shelter"]
NAICS = ["624221", "624229", "623220", "925110"]


def flat(v):
    """The API returns some fields as dicts or lists (CFDA Numbers, NAICS); store a scalar."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        for k in ("cfda_number", "code", "naics", "psc", "description"):
            if k in v:
                v = v[k]
                break
        else:
            v = None
    return v


def connect():
    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS award_sweep_progress(
                   query_key TEXT PRIMARY KEY, label TEXT, kind TEXT, fy TEXT,
                   total INTEGER, pages_done INTEGER, done INTEGER, note TEXT, updated_at TEXT)""")
    c.commit()
    return c


def fy_period(fy: int):
    return {"start_date": f"{fy-1}-10-01", "end_date": f"{fy}-09-30", "date_type": "action_date"}


def filters_for(kind, fy, program=None, keyword=None, naics=None):
    f = {"award_type_codes": GRANT_CODES if kind == "grants" else CONTRACT_CODES,
         "time_period": [fy_period(fy)]}
    if program:
        f["program_numbers"] = program if isinstance(program, list) else [program]
    if keyword:
        f["keywords"] = [keyword]
    if naics:
        f["naics_codes"] = {"require": [naics]}
    return f


def count_awards(kind, fy, **kw):
    d = fetch_post_json(COUNT, {"filters": filters_for(kind, fy, **kw)}, domain=DOMAIN)
    r = d.get("results", {})
    return r.get("grants" if kind == "grants" else "contracts", 0) + r.get("idv", 0) + r.get("other", 0)


def upsert_page(c, rows, kind, fy, label, src):
    n = 0
    for r in rows:
        key = r.get("generated_internal_id")
        if not key:
            continue
        name = (r.get("Recipient Name") or "").strip()
        state = r.get("Place of Performance State Code") or r.get("Recipient Location State Code")
        uei = r.get("Recipient UEI")
        org_id = None
        if name and state:
            nn = name_norm(name)
            c.execute("""INSERT INTO org(name, name_norm, state, uei, org_type, source_id, updated_at)
                         VALUES(?,?,?,?,'unknown',?,?)
                         ON CONFLICT(name_norm, state) DO UPDATE SET
                           uei=COALESCE(org.uei, excluded.uei), source_id=COALESCE(org.source_id, excluded.source_id),
                           updated_at=excluded.updated_at""",
                      (name, nn, state, uei, src, now()))
            row = c.execute("SELECT id FROM org WHERE name_norm=? AND state=?", (nn, state)).fetchone()
            org_id = row[0] if row else None
        c.execute("""INSERT INTO award(award_key, award_id, recipient_name, recipient_norm, recipient_uei, org_id,
                       award_type, agency, subtier, cfda, naics, psc, program, description, amount_obligated,
                       amount_total, pop_start, pop_end, fy, place_state, source_id)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(award_key) DO UPDATE SET
                       amount_obligated=COALESCE(excluded.amount_obligated, award.amount_obligated),
                       org_id=COALESCE(award.org_id, excluded.org_id),
                       cfda=COALESCE(award.cfda, excluded.cfda),
                       program=COALESCE(award.program, excluded.program),
                       place_state=COALESCE(award.place_state, excluded.place_state),
                       source_id=COALESCE(award.source_id, excluded.source_id)""",
                  (key, r.get("Award ID"), name, name_norm(name), uei, org_id,
                   "grant" if kind == "grants" else "contract",
                   r.get("Awarding Agency"), r.get("Awarding Sub Agency"),
                   flat(r.get("CFDA Number") or r.get("CFDA Numbers")),
                   flat(r.get("NAICS")), flat(r.get("PSC")), label,
                   (r.get("Description") or "")[:600], r.get("Award Amount"), r.get("Total Outlays"),
                   (r.get("Start Date") or "")[:10], (r.get("End Date") or "")[:10], str(fy),
                   state, src))
        n += 1
    return n


def sweep(c, from_fy, to_fy, kinds):
    src = source_id_for(c, URL, DOMAIN, title="USASpending spending_by_award sweep",
                        publisher="US Treasury / USASpending.gov", license="public domain")
    c.commit()
    jobs = []
    for fy in range(from_fy, to_fy + 1):
        # one combined assistance-listing sweep per year: far fewer requests, same coverage
        jobs.append(("grants", fy, {"program": PROGRAMS}, f"grants programs FY{fy}"))
        for kw in KEYWORDS:
            for kind in kinds:
                jobs.append((kind, fy, {"keyword": kw}, f"{kind} kw={kw} FY{fy}"))
        for naics in NAICS:
            jobs.append(("contracts", fy, {"naics": naics}, f"contracts naics={naics} FY{fy}"))
    print(f"{len(jobs)} jobs", flush=True)
    total_new = 0
    for kind, fy, kw, label in jobs:
        qkey = f"{label}"
        row = c.execute("SELECT done, pages_done FROM award_sweep_progress WHERE query_key=?", (qkey,)).fetchone()
        if row and row["done"]:
            continue
        try:
            total = count_awards(kind, fy, **kw)
        except Exception as e:
            print(f"count failed {label}: {e}", flush=True)
            continue
        if not total:
            c.execute("""INSERT OR REPLACE INTO award_sweep_progress VALUES(?,?,?,?,?,?,?,?,?)""",
                      (qkey, label, kind, str(fy), 0, 0, 1, "zero results", now()))
            c.commit()
            continue
        page = (row["pages_done"] if row else 0) + 1
        pages = (total + 99) // 100
        while page <= pages:
            payload = {"filters": filters_for(kind, fy, **kw), "fields": FIELDS, "limit": 100,
                       "page": page, "sort": "Award Amount", "order": "desc"}
            try:
                d = fetch_post_json(URL, payload, domain=DOMAIN)
            except Exception as e:
                print(f"page failed {label} p{page}: {e}", flush=True)
                time.sleep(5)
                break
            rows = d.get("results") or []
            if not rows:
                break
            total_new += upsert_page(c, rows, kind, fy, label, src)
            c.commit()
            page += 1
            if page % 20 == 0:
                print(f"  {label} {page-1}/{pages}", flush=True)
        c.execute("""INSERT OR REPLACE INTO award_sweep_progress VALUES(?,?,?,?,?,?,?,?,?)""",
                  (qkey, label, kind, str(fy), total, page - 1, 1 if page > pages else 0,
                   f"{total} awards", now()))
        c.commit()
        got = c.execute("SELECT COUNT(*) FROM award").fetchone()[0]
        print(f"[{label}] total={total} -> table now {got}", flush=True)
    print(json.dumps({"upserted": total_new,
                      "award_rows": c.execute("SELECT COUNT(*) FROM award").fetchone()[0]}))
    return total_new


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-fy", type=int, default=2018)
    ap.add_argument("--to-fy", type=int, default=2026)
    ap.add_argument("--kinds", nargs="*", default=["grants", "contracts"])
    a = ap.parse_args()
    sweep(connect(), a.from_fy, a.to_fy, a.kinds)
