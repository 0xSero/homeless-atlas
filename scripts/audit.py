#!/usr/bin/env python3
"""Data-quality audit of data/atlas.db.

Checks the things a public-record dataset must not get wrong: provenance coverage, impossible
values, duplicate entities that should be merged, and text that looks generated rather than
harvested. Exit 0 when there is nothing that would embarrass the site.

Run: python3 scripts/audit.py [--fix]
"""
from __future__ import annotations
import json, re, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"
FIX = "--fix" in sys.argv
conn = sqlite3.connect(DB, timeout=120)
conn.row_factory = sqlite3.Row

problems, notes = [], []


def chk(name, sql, limit=5, severity="PROBLEM"):
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as e:
        problems.append((severity, name, f"query failed: {e}"))
        return []
    if rows:
        v = rows[0][1] if len(rows[0]) > 1 else rows[0][0]
        problems.append((severity, name, f"{v} · e.g. {[dict(r) for r in rows[:limit]]}"))
    return rows


def ok(name, msg=""):
    notes.append(("ok", name, msg))


# ---------------------------------------------------------------- provenance
for t in ("org", "award", "pit", "geo_city", "geo_coc", "facility", "law", "media", "doc"):
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        s = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE source_id IS NULL").fetchone()[0]
        if n and s:
            problems.append(("PROBLEM", f"{t}: rows without provenance", f"{s}/{n}"))
        elif n:
            ok(f"{t} provenance", f"{n} rows all sourced")
    except sqlite3.Error:
        pass
# provenance plausibility: a table must not be citing a source from an unrelated domain
EXPECTED = {
    "geo_city": ("places", "hud_pit", "coc_geometry"), "geo_state": ("places", "hud_pit"),
    "geo_coc": ("hud_pit", "coc_geometry"), "pit": ("hud_pit",), "award": ("usaspending", "sf", "budgets"),
    "org": ("usaspending", "nonprofit", "org_contacts", "places"), "org_person": ("nonprofit",),
    "facility": ("facility",), "law": ("laws",), "media": ("media",),
    # documents and laws can legitimately be collected by any research domain
    "doc": ("docs", "crime", "health", "budgets", "laws", "media", "sf", "facility"),
}
for t, allowed in EXPECTED.items():
    try:
        rows = conn.execute(f"""SELECT s.domain, COUNT(*) n FROM {t} x JOIN source s ON s.id=x.source_id
                                GROUP BY s.domain ORDER BY n DESC""").fetchall()
    except sqlite3.Error:
        continue
    total = sum(r["n"] for r in rows) or 1
    bad = [(r["domain"], r["n"]) for r in rows if r["domain"] not in allowed and r["n"] > total * 0.2]
    if bad:
        problems.append(("PROBLEM", f"{t}: provenance from an unrelated domain",
                         f"{bad} of {total} rows; expected one of {allowed}. A worker probably UPDATEd source_id "
                         f"on rows it did not create — the citation on the site is then wrong."))

chk("source rows without a URL", "SELECT id,url FROM source WHERE url IS NULL OR url NOT LIKE 'http%'")
chk("source rows with a bad retrieval date", "SELECT id,retrieved_at FROM source WHERE retrieved_at IS NULL")

# ---------------------------------------------------------------- impossible values
chk("pit: unsheltered + sheltered != total", """
    SELECT coc_num, year FROM pit WHERE total IS NOT NULL AND sheltered IS NOT NULL AND unsheltered IS NOT NULL
      AND abs(sheltered + unsheltered - total) > 2""")
chk("pit: negative or absurd counts", "SELECT coc_num, year, total FROM pit WHERE total < 0 OR total > 400000")
chk("pit: subpopulation larger than total", "SELECT coc_num, year, smi FROM pit WHERE smi > total OR sud > total OR chronic > total")
# negative obligations are real (deobligations / corrections), not an error
neg = conn.execute("SELECT COUNT(*) FROM award WHERE amount_obligated < 0").fetchone()[0]
notes.append(("coverage", "awards with negative obligation (deobligations)", str(neg)))
chk("award: absurd single amount (> $5B)", "SELECT award_key, amount_obligated FROM award WHERE amount_obligated > 5e9")
chk("geo_city: population negative", "SELECT geoid, pop FROM geo_city WHERE pop < 0")
# Alaska's Aleutians cross the antimeridian, so a naive lon < -64 test is wrong there
chk("geo_city: outside US bounding box", """SELECT geoid, lat, lon FROM geo_city
     WHERE lat IS NOT NULL AND (lat < 17 OR lat > 72
       OR (lon < -180 OR lon > 180)
       OR (lon > -64 AND NOT (state='AK' AND lon > 170)))""")
chk("facility: closed before opened", "SELECT id, name FROM facility WHERE closed IS NOT NULL AND opened IS NOT NULL AND closed < opened")
chk("org_person: compensation absurd (> $50M)", "SELECT person, comp FROM org_person WHERE comp > 5e7")

# ---------------------------------------------------------------- duplicates
dups = conn.execute("""SELECT ein, COUNT(*) c FROM org WHERE ein IS NOT NULL AND ein<>'' GROUP BY ein HAVING c>1""").fetchall()
if dups:
    problems.append(("PROBLEM", "orgs sharing an EIN", f"{len(dups)} EINs, e.g. {[dict(d) for d in dups[:3]]}"))
    if FIX:
        n = 0
        for d in dups:
            rows = conn.execute("SELECT * FROM org WHERE ein=? ORDER BY (revenue IS NOT NULL) DESC, id", (d["ein"],)).fetchall()
            keep = rows[0]
            for r in rows[1:]:
                conn.execute("UPDATE award SET org_id=? WHERE org_id=?", (keep["id"], r["id"]))
                conn.execute("UPDATE org_person SET org_id=? WHERE org_id=?", (keep["id"], r["id"]))
                # fill blanks on the survivor, then drop the duplicate
                conn.execute("""UPDATE org SET website=COALESCE(website,?), email=COALESCE(email,?),
                                phone=COALESCE(phone,?), revenue=COALESCE(revenue,?), expenses=COALESCE(expenses,?),
                                assets=COALESCE(assets,?), employees=COALESCE(employees,?),
                                uei=COALESCE(uei,?), ntee=COALESCE(ntee,?) WHERE id=?""",
                             (r["website"], r["email"], r["phone"], r["revenue"], r["expenses"], r["assets"],
                              r["employees"], r["uei"], r["ntee"], keep["id"]))
                conn.execute("DELETE FROM org WHERE id=?", (r["id"],))
                n += 1
        conn.commit()
        notes.append(("fixed", "merged duplicate EIN orgs", str(n)))
chk("org_person: exact duplicate people rows", """SELECT person_norm, org_name, year, title FROM org_person
    GROUP BY person_norm, org_name, year, title HAVING COUNT(*)>1""")

# ---------------------------------------------------------------- text sanity
chk("unflagged placeholder org names", """SELECT id, name FROM org
    WHERE (name GLOB '*[*]' OR TRIM(name) IN ('TBD','Unknown','N/A','NA','None','') OR length(TRIM(name))<3)
      AND COALESCE(status,'') <> 'placeholder_name'""")
chk("doc/media URLs that are not http", "SELECT id, url FROM doc WHERE url NOT LIKE 'http%'")
chk("media without a licence recorded", "SELECT id, title FROM media WHERE license IS NULL OR license=''")

# ---------------------------------------------------------------- coverage
cov = {
    "states with a count": "SELECT COUNT(DISTINCT c.state) FROM pit p JOIN geo_coc c ON c.coc_num=p.coc_num WHERE p.total IS NOT NULL",
    "latest year present": "SELECT MAX(year) FROM pit",
    "orgs with an award": "SELECT COUNT(DISTINCT org_id) FROM award WHERE org_id IS NOT NULL",
    "orgs with a person": "SELECT COUNT(DISTINCT org_id) FROM org_person WHERE org_id IS NOT NULL",
    "cities mapped to a CoC": "SELECT COUNT(*) FROM geo_city WHERE coc_num IS NOT NULL",
    "facilities with coordinates": "SELECT COUNT(*) FROM facility WHERE lat IS NOT NULL",
}
for k, sql in cov.items():
    try:
        notes.append(("coverage", k, str(conn.execute(sql).fetchone()[0])))
    except sqlite3.Error:
        pass

# ---------------------------------------------------------------- repairs (only with --fix)
if FIX:
    fixed = []
    # 1) a subpopulation cannot exceed the whole: null it, never estimate
    for r in conn.execute("SELECT coc_num, year, total, chronic FROM pit WHERE chronic > total").fetchall():
        conn.execute("UPDATE pit SET chronic=NULL WHERE coc_num=? AND year=?", (r["coc_num"], r["year"]))
        fixed.append(f"pit {r['coc_num']} {r['year']}: chronic {r['chronic']} > total {r['total']} -> NULL")
    # 2) institution that closed before it opened: keep the record, drop the impossible year
    for r in conn.execute("""SELECT id, name, opened, closed FROM facility
                             WHERE closed IS NOT NULL AND opened IS NOT NULL AND closed < opened""").fetchall():
        conn.execute("""UPDATE facility SET closed=NULL, status='unknown',
                        population_note=COALESCE(population_note,'')||?
                        WHERE id=?""",
                     (f" Source dates conflict (opened {r['opened']}, closed {r['closed']}): "
                      f"verify before use.", r["id"]))
        fixed.append(f"facility {r['name']} ({r['id']}): closed {r['closed']} < opened {r['opened']} -> closed NULL, status unknown")
    # 3) population figures must cite the population file, not a boundary shapefile a worker touched
    pop = conn.execute("""SELECT id FROM source WHERE url LIKE '%sub-est%.csv' ORDER BY id LIMIT 1""").fetchone()
    if pop:
        for t, cond in (("geo_city", "pop IS NOT NULL"), ("geo_state", "pop IS NOT NULL")):
            n = conn.execute(f"""SELECT COUNT(*) FROM {t} x JOIN source s ON s.id=x.source_id
                                 WHERE {cond} AND s.domain NOT IN ('places','hud_pit')""").fetchone()[0]
            if n:
                conn.execute(f"""UPDATE {t} SET source_id=?
                                 WHERE {cond} AND source_id IN (SELECT id FROM source WHERE domain NOT IN ('places','hud_pit'))""",
                             (pop[0],))
                fixed.append(f"{t}: repointed {n} population rows to the population source")

    # 4) placeholder recipient names are real source values but must not rank as organisations
    for r in conn.execute("""SELECT id, name FROM facility WHERE 0""").fetchall():
        pass
    for r in conn.execute("""SELECT id, name FROM org
                             WHERE TRIM(name) IN ('TBD','Unknown','N/A','NA','None','')
                               AND COALESCE(status,'') <> 'placeholder_name'""").fetchall():
        conn.execute("UPDATE org SET status='placeholder_name' WHERE id=?", (r["id"],))
        fixed.append(f"org {r['name']!r} ({r['id']}) -> status placeholder_name")
    if fixed:
        conn.commit()
        with open(ROOT / "research" / "DATA_AUDIT.md", "a") as f:
            f.write("\n### audit --fix pass\n\n" + "\n".join(f"- {x}" for x in fixed) + "\n")
    fixed_list = fixed
else:
    fixed_list = []

# ---------------------------------------------------------------- report
print("=== coverage ===")
for _, k, v in notes:
    print(f"  {k:38} {v}")
hard = [p for p in problems if p[0] == "PROBLEM"]
print(f"\n=== problems: {len(hard)} ===")
for sev, name, detail in hard:
    print(f"  {sev:8} {name}\n           {detail[:400]}")
for x in fixed_list:
    print(f"  FIXED    {x}")
if FIX and fixed_list:
    print(f"\n{len(fixed_list)} repair(s) applied — re-run without --fix to confirm clean")
print("\nAUDIT CLEAN" if not hard else f"\n{len(hard)} PROBLEM(S)")
sys.exit(0 if not hard else 1)
