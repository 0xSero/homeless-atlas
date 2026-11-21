#!/usr/bin/env python3
"""spend-vs-outcome — put federal money and CoC outcomes side by side.

This is a **metric builder, not a fetcher**. It touches no network: every input already exists in
`data/atlas.db` and was fetched by a domain ingestor (and cached under `data/raw/<domain>/`):

| input | table / metric | written by |
|---|---|---|
| federal obligations + place of performance | `award.amount_obligated`, `place_state`, `place_county`, `place_city`, `fy` | `usaspending`, `award_places`, `sf` |
| county and place centroids | `geo_city` (`kind='county'`, `kind='place'`) | `places` / `hud_pit` gazetteer work |
| CoC polygons | `geo_coc.geometry` | `coc_geometry` (HUD CoC grantee areas) |
| exits to permanent housing | `metric` `spm_exit_to_ph_count` (`entity_type='coc'`) | `spm` |
| point-in-time total | `pit.total` | `hud_pit` |

It writes two derived metrics per CoC-year, both `is_est=1` with the formula in `metric.note`:

    spend_per_exit_to_housing = federal obligated (place of performance inside the CoC)
                                / SPM exits to permanent housing
    spend_per_person_placed   = federal obligated (place of performance inside the CoC)
                                / (pit_total(Y) - pit_total(Y+1))

Rules the code enforces, because each one is a way such a ratio usually lies:

1. **Geography is resolved, not assumed.** An award is attributed to a CoC only through its place of
   performance: county name -> county internal point -> point-in-polygon against HUD's CoC grantee
   areas. A named city decides when it resolves, because USASpending's record carries the city *and*
   the county of the same place and a city polygon is always the more specific body. A county name
   that no longer exists (Connecticut's 2022 planning regions) or a bare name shared by a county and
   an independent city (Baltimore, St. Louis, the Virginia cities) is resolved by suffix or by the
   CITY/COUNTY word in the award's own string. Unmatched places are counted and their dollars are
   **left out of every CoC**, never spread over the ones that did match.
2. **Overlapping polygons go to the most specific body.** HUD draws each grantee area as its own
   feature, so a Balance-of-State polygon geometrically contains every city CoC inside it; a point is
   assigned to the smallest polygon that contains it. The method used for every single resolution is
   stored in `spend_vs_outcome_place.method`, so any attribution can be audited with one query.
3. **A negative denominator is dropped, not flipped.** If a CoC's PIT count rose, it placed nobody
   net; `spend_per_person_placed` is then absent rather than a negative "cost".
4. **Zero and missing denominators are dropped.** No division by zero, no `NULL -> 0`.
5. **FY2020 pairs carry a warning.** HUD's January 2021 point-in-time count was not a normal count
   (nationally 54,504 unsheltered people against 226,080 the year before), so a 2020->2021 net change
   is largely a reporting artefact. Those rows are kept, because dropping them would hide the effect,
   but the note says so.
6. **Subawards are excluded from the numerator.** `award` holds prime awards and their subawards
   (`award_type='subaward'`), and a subaward is part of its prime's obligation — adding both would
   double count.
7. **These are not budgets.** The numerator is federal obligation whose place of performance is in
   the CoC, from the archive's homelessness-filtered award universe. ESG, state and local money,
   philanthropy, Medicaid, and anything city or county spends are missing; a CoC is a planning body,
   not a spending unit. A low ratio can be efficiency or an accounting artefact (money booked to
   another county, awards dated in a different fiscal year, a prime award whose subawards carry the
   real service dollars). The site says this next to the table.

Run: python3 scripts/build_outcome_metrics.py [--dry-run] [--report] [--verbose]
     (equivalently `python3 -m ingest.run spend_vs_outcome`, which also records ingest_run)
Re-runnable at any time. The metric rows and both private tables are replaced wholesale inside one
transaction, so a rule change cannot leave a stale CoC-year behind.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, source_id_for, upsert  # noqa: E402

DOMAIN = "spend_vs_outcome"

METRIC_EXIT = "spend_per_exit_to_housing"
METRIC_PERSON = "spend_per_person_placed"

# the two published sources the numerators/denominators come from, registered so every derived row
# can cite the response it was computed from
SRC_MONEY_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
SRC_SPM_URL = "https://files.hudexchange.info/resources/documents/System-Performance-Measures-Data.xlsx"
SRC_PIT_URL = "https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-PIT-Counts-by-CoC.xlsb"

# county / place name suffixes that differ between the Census names in geo_city and the
# USASpending place-of-performance strings ("LOS ANGELES" vs "Los Angeles County")
SUFFIXES = ("PLANNING REGION", "CENSUS AREA", "CITY AND BOROUGH", "CITY AND COUNTY", "BOROUGH",
            "PARISH", "COUNTY", "MUNICIPALITY", "MUNICIPIO", "ISLAND", "TOWNSHIP", "CITY",
            "TOWN", "VILLAGE", "CDP", "BALANCE", "UT")


def norm_place(s: str | None) -> str:
    """Uppercase, apostrophes/dots dropped (not turned into a space), other punctuation -> space."""
    if not s:
        return ""
    s = str(s).upper().replace("&", " AND ")
    s = re.sub(r"['\u2019`\.]", "", s)
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def base_place(s: str | None) -> str:
    """norm_place minus trailing type words, applied symmetrically to both sides of the join."""
    k = norm_place(s)
    changed = True
    while changed:
        changed = False
        for suf in SUFFIXES:
            if k.endswith(" " + suf):
                k = k[: -(len(suf) + 1)].strip()
                changed = True
    return k


# ---------------------------------------------------------------- geometry
def load_coc_polygons(conn):
    """[(coc_num, state, [(ring, minx, miny, maxx, maxy), ...]), ...] from geo_coc.geometry."""
    out = []
    for r in conn.execute("SELECT coc_num, state, geometry FROM geo_coc WHERE geometry IS NOT NULL"):
        try:
            g = json.loads(r["geometry"])
        except (TypeError, ValueError):
            continue
        polys = g.get("coordinates") or []
        polys = polys if g.get("type") == "MultiPolygon" else [polys]
        rings = []
        for poly in polys:
            for ring in poly:
                if len(ring) < 4:
                    continue
                xs = [p[0] for p in ring]
                ys = [p[1] for p in ring]
                rings.append((ring, min(xs), min(ys), max(xs), max(ys)))
        if rings:
            # crude size, used only to rank overlapping polygons by specificity
            area = sum((maxx - minx) * (maxy - miny) for _, minx, miny, maxx, maxy in rings)
            out.append((r["coc_num"], r["state"], rings, area))
    return out


def point_in_ring(ring, x, y) -> bool:
    """Even-odd ray cast."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def cocs_at(lon, lat, state, polygons) -> list[tuple[str, float]]:
    """[(coc_num, polygon_size), ...] for every CoC area containing the point."""
    hits = []
    for num, st, rings, area in polygons:
        if st != state:
            continue
        for ring, minx, miny, maxx, maxy in rings:
            if not (minx <= lon <= maxx and miny <= lat <= maxy):
                continue
            if point_in_ring(ring, lon, lat):
                hits.append((num, area))
                break
    return hits


def build_place_crosswalk(conn, polygons, log):
    """Resolve every county and place centroid to at most one CoC. Writes spend_vs_outcome_place."""
    # these two tables are wholly derived: clearing them keeps a rule change from leaving stale
    # CoC-years behind (upsert alone never removes a row a new rule no longer produces)
    conn.execute("DELETE FROM spend_vs_outcome_place")
    conn.execute("""CREATE TABLE IF NOT EXISTS spend_vs_outcome_place (
                      state TEXT NOT NULL, place_kind TEXT NOT NULL, place_key TEXT NOT NULL,
                      geoid TEXT, name TEXT, lon REAL, lat REAL, coc_num TEXT, method TEXT,
                      candidates TEXT, PRIMARY KEY(state, place_kind, place_key))""")
    rows, stats = [], collections.Counter()
    for g in conn.execute("""SELECT geoid, kind, name, state, pop, lat, lon FROM geo_city
                              WHERE kind IN ('county','place') AND lat IS NOT NULL AND lon IS NOT NULL"""):
        kind = g["kind"]
        key = base_place(g["name"])
        if not key:
            continue
        hits = cocs_at(g["lon"], g["lat"], g["state"], polygons)
        if not hits:
            method, coc = "no_coc_polygon", None
            stats[f"{kind}_unmapped"] += 1
        elif len(hits) == 1:
            method, coc = "centroid_in_polygon", hits[0][0]
            stats[f"{kind}_mapped"] += 1
        else:
            # HUD draws each grantee area as its own feature, so a Balance-of-State polygon
            # geometrically contains every city CoC inside it. Most specific polygon wins.
            coc = min(hits, key=lambda h: h[1])[0]
            method = "nested_smallest_polygon"
            stats[f"{kind}_nested"] += 1
        rows.append({"state": g["state"], "place_kind": kind, "place_key": key, "geoid": g["geoid"],
                     "name": g["name"], "lon": g["lon"], "lat": g["lat"], "coc_num": coc,
                     "method": method,
                     "candidates": ",".join(sorted(h[0] for h in hits)) or None})
    for r in rows:
        upsert(conn, "spend_vs_outcome_place", r, keys=["state", "place_kind", "place_key"])
    log(f"crosswalk: {stats['county_mapped']:,} counties -> CoC, {stats['county_unmapped']:,} with no CoC, "
        f"{stats['county_nested']:,} nested (smallest polygon wins); "
        f"{stats['place_mapped']:,} places -> CoC, {stats['place_unmapped']:,} with no CoC, "
        f"{stats['place_nested']:,} nested")
    return {r["geoid"]: r["coc_num"] for r in rows if r["geoid"]}, stats


# ---------------------------------------------------------------- money
def attribute_awards(conn, log):
    """Sum federal obligation per (CoC, FY) from place of performance. Writes spend_vs_outcome_spend."""
    place = collections.defaultdict(set)          # (state, place name) -> {(geoid, coc_num), ...}
    for r in conn.execute("""SELECT state, place_kind, place_key, geoid, coc_num FROM spend_vs_outcome_place
                              WHERE coc_num IS NOT NULL AND place_kind='place'"""):
        place[(r["state"], r["place_key"])].add((r["geoid"], r["coc_num"]))

    # a base key that maps to two counties in one state (Baltimore, St. Louis, the Virginia
    # independent cities) can only be resolved when the award says CITY or COUNTY
    ambiguous = collections.defaultdict(list)
    for r in conn.execute("""SELECT state, place_kind, place_key, geoid, name, coc_num, candidates
                               FROM spend_vs_outcome_place WHERE place_kind='county'"""):
        ambiguous[(r["state"], r["place_key"])].append(r)

    def resolve_county(state, raw):
        """-> (coc_num, county_geoid, method, allowed_cocs). allowed_cocs is set when the county
        centroid falls inside more than one CoC polygon, so a named city can decide."""
        k = base_place(raw)
        if not k:
            return None, None, "no_county_name", None
        cands = ambiguous.get((state, k)) or []
        if len(cands) == 1:
            row = cands[0]
            allowed = set((row["candidates"] or row["coc_num"] or "").split(",")) - {""}
            return row["coc_num"], row["geoid"], "county", (allowed if len(allowed) > 1 else None)
        if not cands:
            return None, None, "county_not_found", None
        hint = "CITY" if re.search(r"\bCITY\b", norm_place(raw)) else (
            "COUNTY" if re.search(r"\bCOUNTY\b", norm_place(raw)) else "")
        if hint:
            exact = [c for c in cands if hint in norm_place(c["name"])]
            if len(exact) == 1:
                allowed = set((exact[0]["candidates"] or "").split(",")) - {""}
                return exact[0]["coc_num"], exact[0]["geoid"], "county_name_hint", (
                    allowed if len(allowed) > 1 else None)
        # no hint: the county-shaped row is the conservative reading of a bare county name
        cty = [c for c in cands if re.search(r"\bCOUNTY\b|\bPARISH\b|\bBOROUGH\b",
                                            norm_place(c["name"]))]
        if len(cty) == 1:
            allowed = set((cty[0]["candidates"] or "").split(",")) - {""}
            return cty[0]["coc_num"], cty[0]["geoid"], "county_bare_ambiguous_county", (
                allowed if len(allowed) > 1 else None)
        return None, None, "county_ambiguous", None

    def resolve_place(state, raw, allowed=None):
        """-> (coc_num, place_geoid, method). A name shared by two places in one state is only
        accepted when they agree on the CoC (or when the county already narrowed it down)."""
        k = base_place(raw)
        if not k:
            return None, None, "no_city_name"
        hits = place.get((state, k)) or set()
        if not hits:
            return None, None, "city_not_found"
        cocs = {h[1] for h in hits}
        if len(cocs) == 1:
            geoid, coc = next(iter(hits))
            return coc, geoid, "city"
        if allowed:
            narrowed = {h[1] for h in hits} & allowed
            if len(narrowed) == 1:
                geoid, coc = next(h for h in hits if h[1] in narrowed)
                return coc, geoid, "city_narrowed_by_county"
        return None, None, "city_ambiguous"

    agg = collections.defaultdict(lambda: {"obligated": 0.0, "awards": 0,
                                           "from_county": 0, "from_city": 0})
    method_totals = collections.Counter()
    method_dollars = collections.Counter()
    for a in conn.execute("""SELECT place_state, place_county, place_city, fy, amount_obligated
                               FROM award
                              WHERE amount_obligated IS NOT NULL AND fy IS NOT NULL
                                AND award_type != 'subaward'"""):
        st = a["place_state"]
        amt = a["amount_obligated"]
        try:
            fy = int(a["fy"])
        except (TypeError, ValueError):
            method_totals["fy_unparseable"] += 1
            method_dollars["fy_unparseable"] += amt or 0
            continue
        coc, method, allowed = (None, "no_place", None)
        if st:
            if a["place_county"]:
                coc, _geoid, method, allowed = resolve_county(st, a["place_county"])
            if a["place_city"]:
                # USASpending's place of performance carries the city and the county of the same
                # record, and a city polygon is always more specific than its county polygon, so
                # the named city decides when it resolves to a CoC.
                c2, _pgeoid, m2 = resolve_place(st, a["place_city"], allowed)
                if c2:
                    method = m2 if coc is None else "city_in_county"
                    coc = c2
        method_totals[method] += 1
        method_dollars[method] += amt
        if coc is None:
            continue
        d = agg[(coc, fy)]
        d["obligated"] += amt
        d["awards"] += 1
        d["from_city" if method.startswith("city") else "from_county"] += 1

    conn.execute("DELETE FROM spend_vs_outcome_spend")
    conn.execute("""CREATE TABLE IF NOT EXISTS spend_vs_outcome_spend (
                      coc_num TEXT NOT NULL, fy INTEGER NOT NULL, obligated REAL, awards INTEGER,
                      from_county INTEGER, from_city INTEGER, PRIMARY KEY(coc_num, fy))""")
    for (coc, fy), d in agg.items():
        upsert(conn, "spend_vs_outcome_spend",
               {"coc_num": coc, "fy": fy, "obligated": round(d["obligated"], 2), "awards": d["awards"],
                "from_county": d["from_county"], "from_city": d["from_city"]},
               keys=["coc_num", "fy"])
    total = sum(method_dollars.values())
    attributed = sum(d["obligated"] for d in agg.values())
    log(f"awards attributed: {attributed / 1e9:,.2f} B of {total / 1e9:,.2f} B obligated "
        f"({attributed / total * 100 if total else 0:.1f} %) over {len(agg):,} CoC-years")
    for m, n in method_totals.most_common():
        log(f"   {m:28} {n:8,} awards  {method_dollars[m] / 1e9:8.3f} B")
    return agg, {"attributed": attributed, "total": total,
                 "methods": {k: [method_totals[k], method_dollars[k]] for k in method_totals}}


# ---------------------------------------------------------------- ratios
def build_metrics(conn, log, dry_run=False):
    money = {(r["coc_num"], r["fy"]): r["obligated"]
             for r in conn.execute("SELECT coc_num, fy, obligated FROM spend_vs_outcome_spend")}
    exits = {(r["entity_id"], r["year"]): r["value"]
             for r in conn.execute("""SELECT entity_id, year, value FROM metric
                                       WHERE entity_type='coc' AND metric='spm_exit_to_ph_count'
                                         AND value IS NOT NULL""")}
    pit = {(r["coc_num"], r["year"]): r["total"]
           for r in conn.execute("SELECT coc_num, year, total FROM pit WHERE total IS NOT NULL")}

    sid_money = source_id_for(conn, SRC_MONEY_URL, "usaspending",
                              title="USASpending.gov API v2 spending_by_award search",
                              publisher="US Treasury / Bureau of the Fiscal Service",
                              license="Public domain (US Government work)")
    sid_spm = source_id_for(conn, SRC_SPM_URL, "spm",
                            title="CoC System Performance Measures Data Since FY 2015",
                            publisher="U.S. Department of Housing and Urban Development, HUD Exchange",
                            license="US Government work — public domain")
    sid_pit = source_id_for(conn, SRC_PIT_URL, "hud_pit",
                            title="2007-2025 Point-in-Time Estimates by CoC (HUD)",
                            publisher="HUD", license="public domain (US Government work)")

    rows = []
    dropped = collections.Counter()
    for (coc, fy), oblig in sorted(money.items()):
        if fy < 2015 or fy > 2025:          # outside the SPM / PIT vintages in the archive
            continue
        ex = exits.get((coc, fy))
        if ex is None:
            dropped["no_spm_exits"] += 1
        elif ex <= 0:
            dropped["zero_exits"] += 1
        elif not oblig or oblig <= 0:
            dropped["no_money"] += 1
        else:
            val = oblig / ex
            note = (f"derived: federal obligation obligated in FY{fy}, place of performance inside this CoC "
                    f"({oblig:,.0f} USD; USASpending award table, source {sid_money}) / "
                    f"SPM exits to permanent housing FY{fy} ({int(ex):,} exits; HUD SPM workbook, "
                    f"source {sid_spm}) = {val:,.0f} USD per exit. Not a CoC budget: federal awards only, "
                    f"awarded not spent, and not separable from city/county spend in the same place.")
            rows.append(("coc", coc, METRIC_EXIT, round(val, 2), "USD", fy, note, sid_money))

        p0, p1 = pit.get((coc, fy)), pit.get((coc, fy + 1))
        if p0 is None or p1 is None:
            dropped["no_pit_pair"] += 1
            continue
        net = p0 - p1
        if net <= 0:
            dropped["pit_rose_or_flat"] += 1          # negative denominator: dropped, never flipped
            continue
        if not oblig or oblig <= 0:
            dropped["no_money_net"] += 1
            continue
        val = oblig / net
        warn = ""
        if fy == 2020:
            # HUD's January 2021 count was not a normal count: nationally 54,504 unsheltered people
            # were reported against 226,080 a year earlier, so this net change is mostly an artefact.
            warn = (" WARNING: the FY2021 PIT count was not a normal count (most CoCs counted sheltered "
                    "people only), so this FY2020 net is mostly a reporting artefact, not placements.")
        note = (f"derived: federal obligation obligated in FY{fy}, place of performance inside this CoC "
                f"({oblig:,.0f} USD; USASpending award table, source {sid_money}) / net decline in the "
                f"point-in-time count FY{fy}->FY{fy + 1} ({p0:,} - {p1:,} = {net:,} people; HUD PIT, "
                f"source {sid_pit}) = {val:,.0f} USD per net person placed. A census-style net change, not "
                f"a count of placements: inflow into homelessness is netted off, and a CoC whose count rose "
                f"has no value here.{warn}")
        rows.append(("coc", coc, METRIC_PERSON, round(val, 2), "USD", fy, note, sid_pit))

    if not dry_run:
        # same argument as the private tables: a CoC-year that stops producing a value must stop
        # having one, so the derived series is replaced wholesale inside this transaction
        conn.execute("DELETE FROM metric WHERE metric IN (?,?)", (METRIC_EXIT, METRIC_PERSON))
        for et, eid, metric, value, unit, year, note, sid in rows:
            upsert(conn, "metric",
                   {"entity_type": et, "entity_id": eid, "metric": metric, "value": value, "unit": unit,
                    "year": year, "is_est": 1, "note": note, "source_id": sid},
                   keys=["entity_type", "entity_id", "metric", "year"])

    by_year = collections.Counter((r[2], r[5]) for r in rows)
    log(f"metric rows written: {len(rows):,} (dry_run={dry_run})")
    for metric in (METRIC_EXIT, METRIC_PERSON):
        ys = sorted(y for (m, y) in by_year if m == metric)
        cols = " ".join(f"{y}:{by_year[(metric, y)]}" for y in ys)
        n_coc = len({r[1] for r in rows if r[2] == metric})
        log(f"  {metric:26} CoCs {n_coc:4d}  rows/year {cols}")
    for k, n in sorted(dropped.items()):
        log(f"  dropped {k:20} {n:,}")
    return rows, dropped


# ---------------------------------------------------------------- report
def report(conn, year=None, n=12):
    y = year or conn.execute("""SELECT MAX(year) FROM metric WHERE entity_type='coc'
                                 AND metric=? AND value IS NOT NULL""", (METRIC_EXIT,)).fetchone()[0]
    if y is None:
        print("no metric rows yet")
        return
    base = """SELECT m.entity_id coc, g.name, g.state, m.value per, x.value exits,
                     s.obligated, m.year
                FROM metric m
                JOIN geo_coc g ON g.coc_num = m.entity_id
                LEFT JOIN metric x ON x.entity_type='coc' AND x.entity_id=m.entity_id
                     AND x.metric='spm_exit_to_ph_count' AND x.year=m.year
                LEFT JOIN spend_vs_outcome_spend s ON s.coc_num=m.entity_id AND s.fy=m.year
               WHERE m.entity_type='coc' AND m.metric=? AND m.year=? AND m.value IS NOT NULL
                 AND COALESCE(x.value,0) >= 50"""
    rows = [dict(r) for r in conn.execute(base, (METRIC_EXIT, y))]
    rows.sort(key=lambda r: r["per"])
    print(f"\n{y}: {len(rows)} CoCs with both inputs and >=50 exits to permanent housing")
    for label, sub in (("cheapest", rows[:n]), ("most expensive", rows[-n:][::-1])):
        print(f"\n  {label} per exit")
        for r in sub:
            print(f"   {r['coc']:7} {r['state']} {r['name'][:44]:44} "
                  f"{r['obligated'] / 1e6:9,.1f} M / {int(r['exits']):6,} exits = ${r['per']:,.0f}")


def build(conn, *, dry_run=False, log=print) -> dict:
    """Run the whole derivation against an open connection. Returns counts.

    Called by `python3 scripts/build_outcome_metrics.py` and by the thin domain module
    `ingest/domains/spend_vs_outcome.py`, so `python3 -m ingest.run spend_vs_outcome` works too.
    """
    polys = load_coc_polygons(conn)
    log(f"{len(polys)} CoC polygons loaded ({sum(len(p[2]) for p in polys):,} rings)")
    build_place_crosswalk(conn, polys, log)
    agg, attribution = attribute_awards(conn, log)
    rows, dropped = build_metrics(conn, log, dry_run=dry_run)
    n_exit = sum(1 for r in rows if r[2] == METRIC_EXIT)
    n_person = sum(1 for r in rows if r[2] == METRIC_PERSON)
    return {"metric_rows": len(rows), "exit_rows": n_exit, "person_rows": n_person,
            "cocs_exit": len({r[1] for r in rows if r[2] == METRIC_EXIT}),
            "cocs_person": len({r[1] for r in rows if r[2] == METRIC_PERSON}),
            "coc_years_with_money": len(agg), "attribution": attribution,
            "dropped": dict(dropped)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--report", action="store_true", help="print the newest year's table after building")
    a = ap.parse_args()

    t0 = time.time()

    def log(msg):
        print(f"[{DOMAIN}] {msg}")

    if not DB.exists():
        print(f"no database at {DB}")
        return 1
    conn = sqlite3.connect(DB, timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=300000")

    res = build(conn, dry_run=a.dry_run, log=log)
    if not a.dry_run:
        conn.commit()
    if a.report:
        report(conn)
    log(f"done in {time.time() - t0:.1f}s")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
