#!/usr/bin/env python3
"""Repair organisation financials that cite a source which contains no financials.

1,697 organisations carry `revenue`/`expenses` with `source_id` pointing at the IRS Business Master File
(`eo1.csv`) — a registration file that has no revenue fields at all. The numbers came from somewhere
else (most likely a Form 990 or the Nonprofit Explorer page), so the citation on the site is wrong.

This script, for every such organisation:
  * asks ProPublica's Nonprofit Explorer for the organisation's filings;
  * if a filing with figures exists, writes the latest year's revenue/expenses and re-points
    `source_id` at the filing that actually contains them;
  * if no filing with figures exists, NULLs the financials rather than keep an uncitable number.

Run: python3 scripts/repair_org_financials.py [--limit N] [--dry-run]
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch_json, now, source_id_for  # noqa: E402

DOMAIN = "nonprofit"
API = "https://projects.propublica.org/nonprofits/api/v2/organizations/{}.json"


def connect():
    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row
    return c


def targets(c, limit=None):
    q = """SELECT o.id, o.name, o.ein, o.revenue, o.expenses, o.fiscal_year, s.url
           FROM org o JOIN source s ON s.id = o.source_id
           WHERE o.revenue IS NOT NULL AND s.url LIKE '%irs.gov/pub/irs-soi/eo%'
           ORDER BY o.revenue DESC"""
    rows = c.execute(q).fetchall()
    return rows[:limit] if limit else rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    c = connect()
    todo = targets(c, a.limit)
    print(f"{len(todo)} organisations cite a file with no financials", flush=True)
    fixed = nulled = 0
    for i, row in enumerate(todo, 1):
        ein = (row["ein"] or "").replace("-", "").zfill(9)
        if not ein.strip("0"):
            continue
        url = API.format(ein)
        try:
            d = fetch_json(url, domain=DOMAIN)
        except Exception as e:
            print(f"  {row['name'][:40]}: fetch failed {e}", flush=True)
            continue
        # ProPublica's filing objects carry totrevenue and totfuncexpns (total functional expenses);
        # "totrevexp" does not exist in this payload, which is why expenses were coming back empty
        filings = [f for f in (d.get("filings_with_data") or [])
                   if f.get("totrevenue") is not None or f.get("totfuncexpns") is not None]
        if filings:
            latest = max(filings, key=lambda f: f.get("tax_prd_yr") or 0)
            rev = latest.get("totrevenue")
            exp = latest.get("totfuncexpns", latest.get("totrevexp"))
            assets = latest.get("totassetsend")
            yr = latest.get("tax_prd_yr")
            if rev is None and exp is None:
                filings = []
        if filings:
            if not a.dry_run:
                src = source_id_for(c, url, DOMAIN,
                                    title=f"IRS Form 990 filings for EIN {ein} (Nonprofit Explorer)",
                                    publisher="ProPublica Nonprofit Explorer / IRS",
                                    license="public record")
                c.execute("""UPDATE org SET revenue=?, expenses=?, assets=COALESCE(?,assets),
                             fiscal_year=?, source_id=? WHERE id=?""",
                          (rev, exp, assets, yr, src, row["id"]))
                c.commit()
            fixed += 1
        else:
            if not a.dry_run:
                c.execute("UPDATE org SET revenue=NULL, expenses=NULL, fiscal_year=NULL WHERE id=?", (row["id"],))
                c.commit()
            nulled += 1
        if i % 50 == 0:
            print(f"  {i}/{len(todo)} re-sourced {fixed} · nulled {nulled}", flush=True)
        time.sleep(1.0)
    c.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
              ("org_financials_repair", now(), fixed,
               f"re-sourced={fixed} nulled={nulled} examined={len(todo)}{' DRY' if a.dry_run else ''}"))
    c.commit()
    print(json.dumps({"examined": len(todo), "re_sourced": fixed, "nulled": nulled, "dry_run": a.dry_run}))
    with open(ROOT / "research" / "DATA_AUDIT.md", "a") as f:
        f.write(f"\n### Organisation financials re-sourced ({now()[:10]})\n\n"
                f"- {len(todo)} organisations had revenue/expenses citing `eo1.csv` (the IRS Business Master File, "
                f"which contains no financial fields).\n"
                f"- {fixed} were re-sourced from their actual Form 990 filing on Nonprofit Explorer.\n"
                f"- {nulled} had no filing with figures and were set to NULL rather than keep an uncitable number.\n")


if __name__ == "__main__":
    main()
