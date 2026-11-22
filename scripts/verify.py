#!/usr/bin/env python3
"""Completion gate for homeless-atlas. Exit 0 only when every gate passes.

Usage: python3 scripts/verify.py [--json]
"""
from __future__ import annotations
import json, sqlite3, subprocess, sys, os, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"

# gate: (table, sql, target, label)
GATES = [
    ("org",            "SELECT COUNT(*) FROM org",                                              5000,  "organizations"),
    ("org(sourced)",   "SELECT COUNT(*) FROM org WHERE source_id IS NOT NULL",                  4500,  "orgs with provenance"),
    ("award",          "SELECT COUNT(*) FROM award",                                           40000,  "federal awards"),
    ("award(sourced)", "SELECT COUNT(*) FROM award WHERE source_id IS NOT NULL",               39000,  "awards with provenance"),
    ("org_person",     "SELECT COUNT(*) FROM org_person",                                      10000,  "named people (990 officers)"),
    ("pit rows",       "SELECT COUNT(*) FROM pit WHERE total IS NOT NULL",                      2000,  "CoC-year PIT rows"),
    ("pit CoCs",       "SELECT COUNT(DISTINCT coc_num) FROM pit",                                380,  "CoCs with counts"),
    ("geo_city",       "SELECT COUNT(*) FROM geo_city",                                        20000,  "US places"),
    ("geo_city(coord)","SELECT COUNT(*) FROM geo_city WHERE lat IS NOT NULL",                  19000,  "places with coords"),
    ("geo_state",      "SELECT COUNT(*) FROM geo_state",                                          51,  "states + DC"),
    ("geo_coc",        "SELECT COUNT(*) FROM geo_coc",                                           380,  "CoC records"),
    ("geo_coc",        "SELECT COUNT(*) FROM geo_coc WHERE geometry IS NOT NULL",                380,  "CoC polygons"),
    ("facility",       "SELECT COUNT(*) FROM facility",                                         1200,  "institutions"),
    ("facility closed","SELECT COUNT(*) FROM facility WHERE status='closed'",                     200,  "closed institutions"),
    ("law",            "SELECT COUNT(*) FROM law",                                               250,  "law rows"),
    ("metric",         "SELECT COUNT(*) FROM metric",                                           5000,  "context metrics"),
    ("media",          "SELECT COUNT(*) FROM media",                                             800,  "videos + images"),
    ("doc",            "SELECT COUNT(*) FROM doc",                                               300,  "audits/studies/court"),
    ("source",         "SELECT COUNT(*) FROM source",                                            200,  "registered sources"),
    # ---- the current goal's own targets, so a green run actually covers them -------------------
    ("metric",         "SELECT COUNT(DISTINCT entity_id) FROM metric WHERE metric LIKE 'spm%'",   300,  "CoCs with HUD system performance measures"),
    ("award",          "SELECT COUNT(*) FROM award WHERE award_type='subaward'",                 2000,  "subaward rows linked to primes"),
    ("org",            "SELECT COUNT(*) FROM org WHERE website IS NOT NULL",                     5000,  "org websites"),
    ("org",            "SELECT COUNT(*) FROM org WHERE email IS NOT NULL",                       2000,  "org contact addresses"),
    ("metric",         "SELECT COUNT(DISTINCT entity_id) FROM metric WHERE metric='officer_turnover'", 1000, "orgs with an officer-turnover metric"),
    ("org_purpose",    "SELECT COUNT(*) FROM org_purpose",                                        700,  "990 header statements of purpose"),
]

# precision gates: these must come out zero, however large the counts above get
ZERO_GATES = [
    ("websites without a recorded check",
     """SELECT COUNT(*) FROM org o WHERE o.website IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM org_domain_mine m WHERE m.org_id=o.id AND m.matched=1)
          AND NOT EXISTS (SELECT 1 FROM org_site_check s WHERE s.org_id=o.id AND s.accepted=1)
          AND NOT EXISTS (SELECT 1 FROM org_gov_site g WHERE g.org_id=o.id AND g.matched=1)
          AND NOT EXISTS (SELECT 1 FROM org_web_check w WHERE w.org_id=o.id AND w.status=200)
          AND NOT EXISTS (SELECT 1 FROM cn_site_check c WHERE c.org_id=o.id AND c.matched>0)
          AND NOT EXISTS (SELECT 1 FROM link_graph_check l WHERE l.org_id=o.id AND l.matched>0)
          AND NOT EXISTS (SELECT 1 FROM acronym_check a WHERE a.org_id=o.id AND a.matched>0)
          AND NOT EXISTS (SELECT 1 FROM epostcard_check e WHERE e.org_id=o.id AND e.matched>0)"""),
    ("emails without a recorded check",
     """SELECT COUNT(*) FROM org o WHERE o.email IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM org_email_mine m WHERE m.org_id=o.id)
          AND NOT EXISTS (SELECT 1 FROM org_site_check s WHERE s.org_id=o.id AND s.email IS NOT NULL)"""),
    ("subawards with no prime award in the table",
     """SELECT COUNT(*) FROM award s WHERE s.award_type='subaward'
          AND NOT EXISTS (SELECT 1 FROM award p WHERE p.award_type!='subaward'
                            AND s.award_key LIKE 'SUB_' || p.award_key || '_%')"""),
    ("derived metrics with no note",
     "SELECT COUNT(*) FROM metric WHERE is_est=1 AND (note IS NULL OR note='')"),
]

SITE_ROUTES = [
    "index.html", "map.html", "search.html", "states.html", "cities.html", "orgs.html",
    "people.html", "facilities.html", "laws.html", "budgets.html", "costs.html", "rankings.html", "media.html", "docs.html",
    "about.html", "method.html", "sources.html", "report/san-francisco.html",
    "data/search-index.json", "data/map-places.geojson", "data/map-cocs.geojson",
]


def main() -> int:
    as_json = "--json" in sys.argv
    rows, fails = [], 0
    if not DB.exists():
        print("FATAL: data/atlas.db missing")
        return 2
    conn = sqlite3.connect(DB, timeout=120)   # read-write: a read-only handle cannot see the WAL
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, sql, target, label in GATES:
        tname = table.split("(")[0].split()[0]
        if tname not in tables:
            rows.append({"gate": label, "value": 0, "target": target, "ok": False, "note": "table missing"})
            fails += 1
            continue
        try:
            v = conn.execute(sql).fetchone()[0] or 0
        except Exception as e:
            v = 0
        ok = v >= target
        fails += 0 if ok else 1
        rows.append({"gate": label, "value": v, "target": target, "ok": ok})

    # precision gates: a count can be met with bad rows, these must be zero
    for label, sql in ZERO_GATES:
        try:
            v = conn.execute(sql).fetchone()[0] or 0
        except Exception:
            v = -1                       # a gate that cannot run has not passed
        ok = v == 0
        fails += 0 if ok else 1
        rows.append({"gate": label, "value": v, "target": 0, "ok": ok})

    # site gates
    site = ROOT / "site"
    site_missing = [r for r in SITE_ROUTES if not (site / r).exists()]
    if site_missing:
        fails += 1
    rows.append({"gate": "site routes", "value": len(SITE_ROUTES) - len(site_missing),
                 "target": len(SITE_ROUTES), "ok": not site_missing,
                 "note": ",".join(site_missing[:6])})
    if (site / "data" / "search-index.json").exists():
        try:
            idx = json.loads((site / "data" / "search-index.json").read_text())
            n_entities = len(idx.get("items", idx) if isinstance(idx, dict) else idx)
        except Exception:
            n_entities = 0
        ok = n_entities >= 10000
        fails += 0 if ok else 1
        rows.append({"gate": "search index entities", "value": n_entities, "target": 10000, "ok": ok})

    # the SF report gate: no uncited numeric paragraphs is checked by a linter
    rep = ROOT / "reports" / "san-francisco.md"
    if rep.exists():
        txt = rep.read_text()
        cites = len(re.findall(r"\]\(http", txt))
        ok = cites >= 120 and len(txt) > 20000
        fails += 0 if ok else 1
        rows.append({"gate": "SF report citations", "value": cites, "target": 120, "ok": ok})
    else:
        fails += 1
        rows.append({"gate": "SF report citations", "value": 0, "target": 120, "ok": False, "note": "missing"})

    if as_json:
        print(json.dumps({"ok": fails == 0, "fails": fails, "gates": rows}, indent=1))
    else:
        w = max(len(r["gate"]) for r in rows) + 2
        for r in rows:
            mark = "PASS" if r["ok"] else "FAIL"
            print(f"{mark}  {r['gate']:<{w}} {r['value']:>9,} / {r['target']:>9,}" + (f"  [{r.get('note','')}]" if r.get("note") else ""))
        print(f"\n{'ALL GATES PASS' if fails == 0 else f'{fails} GATE(S) FAILING'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
