#!/usr/bin/env python3
"""Align organisation financials with the filing they actually cite.

2,500+ organisations carry a `fiscal_year` of 2025 or 2026 while citing a ProPublica filing that ends
years earlier — a number whose citation cannot contain it. For each such organisation this script:
  * keeps the row only if a filing for that exact year exists in the cited source;
  * otherwise adopts the most recent filing that does exist (year, revenue, expenses, assets);
  * and if there is no filing with figures at all, NULLs the financials.

Run: python3 scripts/repair_org_years.py [--limit N] [--since 2024]
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch_json, now, source_id_for  # noqa: E402

API = "https://projects.propublica.org/nonprofits/api/v2/organizations/{}.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--since", type=int, default=2024)
    a = ap.parse_args()
    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row
    rows = c.execute("""SELECT o.id, o.name, o.ein, o.revenue, o.fiscal_year, s.url
                        FROM org o JOIN source s ON s.id = o.source_id
                        WHERE s.url LIKE '%propublica%organizations%'
                          AND o.fiscal_year >= ? AND o.ein IS NOT NULL
                        ORDER BY o.revenue DESC""", (a.since,)).fetchall()
    if a.limit:
        rows = rows[:a.limit]
    print(f"{len(rows)} organisations to align", flush=True)
    kept = realigned = nulled = 0
    for i, r in enumerate(rows, 1):
        ein = (r["ein"] or "").replace("-", "").zfill(9)
        if not ein.strip("0"):
            continue
        url = API.format(ein)
        try:
            d = fetch_json(url, domain="nonprofit")
        except Exception:
            continue
        filings = [f for f in (d.get("filings_with_data") or [])
                   if f.get("totrevenue") is not None or f.get("totfuncexpns") is not None]
        if not filings:
            c.execute("UPDATE org SET revenue=NULL, expenses=NULL, fiscal_year=NULL WHERE id=?", (r["id"],))
            nulled += 1
        else:
            same = [f for f in filings if f.get("tax_prd_yr") == r["fiscal_year"]]
            if same:
                kept += 1
            else:
                latest = max(filings, key=lambda f: f.get("tax_prd_yr") or 0)
                c.execute("""UPDATE org SET revenue=?, expenses=?, assets=COALESCE(?,assets),
                             fiscal_year=? WHERE id=?""",
                          (latest.get("totrevenue"), latest.get("totfuncexpns"),
                           latest.get("totassetsend"), latest.get("tax_prd_yr"), r["id"]))
                realigned += 1
        c.commit()
        if i % 100 == 0:
            print(f"  {i}/{len(rows)} kept={kept} realigned={realigned} nulled={nulled}", flush=True)
        time.sleep(1.0)
    c.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
              ("org_years_repair", now(), realigned, f"kept={kept} realigned={realigned} nulled={nulled}"))
    c.commit()
    print(json.dumps({"examined": len(rows), "kept": kept, "realigned": realigned, "nulled": nulled}))


if __name__ == "__main__":
    main()
