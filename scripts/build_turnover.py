#!/usr/bin/env python3
"""Officer turnover — the one organisational behaviour the Form 990 officer rosters can support.

For each organisation with a roster in consecutive years, this measures how much of the roster
changed. It is a metric builder, not a fetcher: it reads org_person and writes `metric` rows.

Definition, stated exactly because a metric without its formula is a rumour:

    turn(N → N+1) = 1 - |roster(N) ∩ roster(N+1)| / |roster(N)|

Rules that stop a missing filing from reading as a resignation:

  * a year-pair is scored only when both years have at least one named officer;
  * a year-pair where the whole roster disappears (zero rows in N+1) is *flagged*, never scored —
    that is a filing gap, not churn;
  * an organisation whose roster is a single person both years scores 0, not 1 — one name cannot
    be a "turnover rate" on its own, so those pairs are recorded with the flag set instead.

Derived values carry is_est=1 and the formula in the note.

Run: python3 scripts/build_turnover.py [--dry-run] [--orgs N]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB  # noqa: E402

NOTE_TURN = ("1 - |roster(N) ∩ roster(N+1)| / |roster(N)| over named officers in Form 990 Part VII. "
             "Scores leadership change, not performance; a name can leave for retirement as easily as "
             "for resignation.")
NOTE_COST = ("expenses(N+1) / number of named officers in N+1, from the same filings. A crude span-of-control "
             "proxy, not a salary.")
NOTE_FLAG = ("The roster vanished between the two years while the organisation kept filing — treated as a "
             "filing gap and flagged instead of scored.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--orgs", type=int, default=None)
    a = ap.parse_args()

    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row

    orgs = c.execute("""SELECT org_id, year, person_norm, source_id
                          FROM org_person
                         WHERE org_id IS NOT NULL AND year IS NOT NULL AND person_norm IS NOT NULL""").fetchall()
    rosters: dict[int, dict[int, set]] = {}
    src: dict[tuple[int, int], int] = {}
    for r in orgs:
        rosters.setdefault(r["org_id"], {}).setdefault(r["year"], set()).add(r["person_norm"])
        src[(r["org_id"], r["year"])] = r["source_id"]      # last one wins; all rows share the filing
    if a.orgs:
        rosters = dict(list(rosters.items())[: a.orgs])

    scored = flagged = skipped = 0
    rows = []
    for org_id, years in rosters.items():
        ys = sorted(years)
        # a gap inside an organisation's own span is a missing filing, not a resignation: flag it
        for y in range(ys[0], ys[-1]):
            if y in years and (y + 1) not in years:
                flagged += 1
                rows.append((org_id, y + 1, "officer_dropout_flag", 1, "flag", NOTE_FLAG,
                             src.get((org_id, y))))
        for n, n1 in zip(ys, ys[1:]):
            if n1 != n + 1:                                  # only consecutive years
                continue
            rn, rn1 = years[n], years[n1]
            if not rn or not rn1:
                flagged += 1
                rows.append((org_id, n1, "officer_dropout_flag", 1, "flag", NOTE_FLAG, src.get((org_id, n1))))
                continue
            if len(rn) < 2:                                  # a single name is not a rate
                skipped += 1
                continue
            keep = len(rn & rn1) / len(rn)
            turn = 1 - keep
            rows.append((org_id, n1, "officer_turnover", round(turn, 4), "ratio", NOTE_TURN, src.get((org_id, n1))))
            scored += 1
            exp = c.execute("SELECT expenses FROM org WHERE id=?", (org_id,)).fetchone()
            if exp and exp[0]:
                rows.append((org_id, n1, "cost_per_officer", round(exp[0] / len(rn1), 2), "USD",
                             NOTE_COST, src.get((org_id, n1))))

    if not a.dry_run:
        for org_id, year, metric, value, unit, note, source_id in rows:
            if source_id is None:
                continue
            c.execute("""INSERT INTO metric(entity_type, entity_id, metric, value, unit, year, is_est, note, source_id)
                         VALUES('org', ?, ?, ?, ?, ?, 1, ?, ?)
                         ON CONFLICT(entity_type, entity_id, metric, year) DO UPDATE SET
                           value=excluded.value, unit=excluded.unit, is_est=1,
                           note=excluded.note, source_id=excluded.source_id""",
                      (str(org_id), metric, value, unit, year, note, source_id))
        c.commit()

    orgs_scored = len({r[0] for r in rows if r[2] == "officer_turnover"})
    print(f"year-pairs scored: {scored}  flagged as filing gaps: {flagged}  "
          f"skipped (single-name rosters): {skipped}")
    print(f"organisations with a turnover metric: {orgs_scored}  (dry_run={a.dry_run}, rows={len(rows)})")


if __name__ == "__main__":
    main()
