#!/usr/bin/env python3
"""spend_vs_outcome — a domain module that only *derives*; the work lives in scripts/.

This task is a metric builder, not a fetcher: nothing is downloaded here, and every input is a row
another ingestor already wrote and cached (`usaspending` / `award_places` for federal obligation and
place of performance, `spm` for exits to permanent housing, `hud_pit` for the point-in-time total,
`coc_geometry` for the CoC polygons, Census gazetteer rows in `geo_city` for county/place centroids).

The single implementation is `scripts/build_outcome_metrics.py` (`build(conn, ...)`), which can also be
run directly. This wrapper exists so the derivation shows up in `ingest_run`, so
`python3 -m ingest.run spend_vs_outcome` re-derives the two metrics, and so the domain has the name the
contract expects:

    spend_per_exit_to_housing = federal obligated in a CoC's counties/cities / SPM exits to PH
    spend_per_person_placed   = federal obligated / (pit_total(FY) - pit_total(FY+1)), >0 only

Both are written to `metric` with `entity_type='coc'`, `is_est=1` and the formula in the note. The
private tables `spend_vs_outcome_place` (place -> CoC, with the method used) and `spend_vs_outcome_spend`
(attributed obligation per CoC-year) are rebuilt from scratch on every run.

Run: python3 -m ingest.run spend_vs_outcome     (aka python3 scripts/build_outcome_metrics.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

DOMAIN = "spend_vs_outcome"


def run(conn, cfg=None) -> dict:
    from scripts.build_outcome_metrics import SRC_MONEY_URL, SRC_SPM_URL, build  # noqa: E402

    res = build(conn, dry_run=False, log=lambda m: print(f"[{DOMAIN}] {m}"))
    notes = (f"{res['metric_rows']} derived metric rows: {res['cocs_exit']} CoCs with "
             f"spend_per_exit_to_housing, {res['cocs_person']} with spend_per_person_placed; "
             f"{res['coc_years_with_money']} CoC-years of attributed federal obligation. "
             f"Dropped: " + ", ".join(f"{k}={v}" for k, v in sorted(res["dropped"].items())))
    return {"rows": res["metric_rows"], "notes": notes,
            "sources": [(SRC_MONEY_URL, "Public domain (US Government work)"),
                        (SRC_SPM_URL, "US Government work — public domain")]}


def main() -> int:
    from ingest import common

    conn = common.db()
    res = run(conn, {})
    conn.commit()
    conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                 (DOMAIN, common.now(), res["rows"], res["notes"][:2000]))
    conn.commit()
    print(f"[{DOMAIN}] OK rows={res['rows']} {res['notes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
