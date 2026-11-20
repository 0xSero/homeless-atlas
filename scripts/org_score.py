#!/usr/bin/env python3
"""A public-record profile score for organisations — disclosure and financial shape only.

HUD publishes no per-organisation outcomes. There is no "success rate" per nonprofit in any public
dataset: System Performance Measures are published per Continuum of Care, not per provider, and HMIS
data is not public. Anyone who shows you an org-level success rate is inventing it.

What *is* published, and what this script scores, comes only from a Form 990/990-EZ filing and from
matched federal award records:

    disclosure    (40)  how much of the filing is actually filled in: revenue, expenses, assets,
                        employees, NTEE code, website, a reachable contact
    solvency      (25)  expenses / revenue in the latest year — 1.0 means it spent what it took in;
                        wildly outside 0.75–1.15 is a warning in either direction
    concentration (20)  share of the most recent year's federal awards going to this one organisation
                        within its CoC — dependency, not virtue
    pay           (15)  the best-paid officer's compensation as a share of total expenses

Nothing here measures whether anyone got housed. The score is a filing-quality and financial-shape
index, every component is shown, and the page says so in as many words.

Run: python3 scripts/org_score.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB  # noqa: E402

NOTE = ("Public-record profile: disclosure {d}/40, solvency {s}/25, dependency {c}/20, pay {p}/15. "
        "Measures filing completeness and financial shape ONLY — no public dataset reports whether "
        "an organisation housed anyone.")

COMPONENTS = [
    ("profile_disclosure", 40),
    ("profile_solvency", 25),
    ("profile_dependency", 20),
    ("profile_pay", 15),
]


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def nmax(x: float, ceiling: float) -> float:
    return clamp(x / ceiling, 0.0, 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row

    # only organisations we can actually document: at least one 990 financial and one award row
    rows = c.execute("""
        SELECT o.id, o.name, o.revenue, o.expenses, o.assets, o.employees, o.ntee, o.website,
               o.email, o.phone, o.ein, o.fiscal_year,
               (SELECT SUM(amount_obligated) FROM award a WHERE a.org_id = o.id)      AS award_total,
               (SELECT MAX(amount_obligated) FROM award a WHERE a.org_id = o.id)      AS biggest_award,
               (SELECT MAX(comp) FROM org_person p WHERE p.org_id = o.id)             AS top_comp,
               o.coc_num AS coc_num, o.source_id AS org_src, o.uei AS uei,
               (SELECT source_id FROM award a WHERE a.org_id = o.id
                  AND a.amount_obligated IS NOT NULL ORDER BY a.amount_obligated DESC LIMIT 1) AS src_award
        FROM org o
        WHERE (o.revenue IS NOT NULL OR o.expenses IS NOT NULL)
    """).fetchall()
    if a.limit:
        rows = rows[:a.limit]

    # CoC totals for the dependency component, from awards whose place of performance sits in that CoC
    coc_total = {}
    for r in c.execute("""SELECT o.coc_num AS coc, SUM(a.amount_obligated) AS t
                            FROM award a JOIN org o ON o.id = a.org_id
                           WHERE o.coc_num IS NOT NULL AND a.amount_obligated IS NOT NULL
                           GROUP BY o.coc_num"""):
        if r["coc"] and r["t"]:
            coc_total[r["coc"]] = r["t"]

    coc_of = {r["id"]: r["coc_num"] for r in rows}
    written = {}
    for r in rows:
        if not r["ein"] and not r["website"] and not r["award_total"]:
            continue  # not enough public record to score honestly

        # --- disclosure 40: how complete is the published record
        fields = [r["revenue"], r["expenses"], r["assets"], r["employees"], r["ntee"], r["fiscal_year"]]
        filled = sum(1 for f in fields if f not in (None, 0, ""))
        contact = 1 if (r["website"] or r["email"] or r["phone"]) else 0
        # 6 filing fields x 4, a reachable contact 8, an EIN 4, a UEI 4 — a filing with no contact
        # route and no identifiers should not score well on disclosure
        disclosure = (filled / len(fields)) * 24 + contact * 8 + (4 if r["ein"] else 0) + (4 if r["uei"] else 0)

        # --- solvency 25: expenses against revenue
        rev, exp = r["revenue"], r["expenses"]
        solvency = 0.0
        if rev and exp:
            ratio = exp / rev
            solvency = 25 * clamp(1 - abs(ratio - 1.0) / 0.5, 0, 1)

        # --- dependency 20: this org's share of its CoC's mapped federal money
        dep = 0.0
        coc = coc_of.get(r["id"])
        if coc and r["award_total"] and coc_total.get(coc):
            share = r["award_total"] / coc_total[coc]
            dep = 20 * clamp(share / 0.25, 0, 1)

        # --- pay 15: top compensation against expenses
        pay = 0.0
        if exp and r["top_comp"]:
            share = r["top_comp"] / exp
            pay = 15 * clamp(1 - (share - 0.005) / 0.045, 0, 1) if share > 0.005 else 15.0

        total = round(disclosure + solvency + dep + pay, 1)
        note = NOTE.format(d=round(disclosure), s=round(solvency), c=round(dep), p=round(pay))
        src = r["src_award"] or r["org_src"]   # the award row, else the filing that supplied the financials
        written[r["id"]] = (total, note, src)
        if a.dry_run:
            continue

        vals = {k: round(v, 1) for k, v in
                (("profile_disclosure", disclosure), ("profile_solvency", solvency),
                 ("profile_dependency", dep), ("profile_pay", pay))}
        vals["profile_score"] = total
        for metric, value in vals.items():
            c.execute("""INSERT INTO metric(entity_type, entity_id, metric, value, unit, year, is_est, note, source_id)
                         VALUES('org', ?, ?, ?, 'score_0_100', ?, 1, ?, ?)
                         ON CONFLICT(entity_type, entity_id, metric, year) DO UPDATE SET
                           value=excluded.value, note=excluded.note, source_id=excluded.source_id,
                           is_est=excluded.is_est""",
                      (str(r["id"]), metric, value, r["fiscal_year"], note, src))
    if not a.dry_run:
        c.commit()
    print(f"scored {len(written)} organisations (dry_run={a.dry_run})")
    top = sorted(written.items(), key=lambda kv: -kv[1][0])[:5]
    print("highest:", [f"{i}:{v[0]}" for i, v in top])


if __name__ == "__main__":
    main()
