#!/usr/bin/env python3
"""org_gov_sites — find the websites of government entities by pattern, then prove each one.

1,057 cities, counties, towns and housing authorities in the archive have no website. Their sites are
predictable — cityofcharleston.com, charlestonwv.gov, ci.appleton.wi.us — so the candidate can be
generated rather than searched for. A generated URL is only written when the page proves it: the
response is 200 **and** the page names the entity (its distinctive words) together with its state.

Run: python3 scripts/org_gov_sites.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch, now  # noqa: E402

GOV_RE = re.compile(r"^(CITY|COUNTY|TOWN|VILLAGE|BOROUGH|PARISH) OF ", re.I)
WORD_STOP = {"city", "county", "town", "village", "borough", "parish", "of", "the", "and", "housing",
             "authority", "department", "office", "police", "sheriff", "public", "schools", "school",
             "district", "board", "commission", "services", "service", "agency", "council", "court",
             "government", "county-wide"}


def entity_place(name: str) -> tuple[str, str]:
    """('CITY OF CHARLESTON', 'WV') -> ('charleston', 'kind')."""
    m = GOV_RE.match(name or "")
    if not m:
        return "", ""
    kind = m.group(1).lower()
    rest = (name or "")[m.end():]
    rest = re.split(r"\b(HOUSING|POLICE|DEPARTMENT|OFFICE|SHERIFF|PUBLIC|SCHOOL)\b", rest, flags=re.I)[0]
    return rest.strip().lower(), kind


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def candidates(place: str, kind: str, state: str) -> list[str]:
    p = slug(place)
    if not p or len(p) < 3:
        return []
    st = (state or "").lower()
    out = [
        f"https://www.cityof{p}.gov", f"https://www.cityof{p}.org", f"https://www.cityof{p}.com",
        f"https://{p}{st}.gov", f"https://www.{p}{st}.gov", f"https://www.{p}.gov",
        f"https://www.{p}.org", f"https://www.{p}.us",
        f"https://www.ci.{p}.{st}.us", f"https://www.co.{p}.{st}.us", f"https://{p}.gov",
    ]
    out += [f"https://www.{p}{st}.org", f"https://www.{p}-{st}.gov", f"https://www.{p}city.gov",
            f"https://www.{p}county.org", f"https://www.{p}-{st}.org"]
    if kind == "county":
        out = [f"https://www.{p}county{st}.gov", f"https://www.{p}county.gov",
               f"https://www.co.{p}.{st}.us", f"https://{p}county{st}.gov"] + out
    return [c for c in out if ".." not in c]


STATES = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
          "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
          "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
          "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
          "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
          "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
          "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
          "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
          "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
          "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
          "VA": "virginia", "WA": "washington", "WV": "west virginia", "WI": "wisconsin",
          "WY": "wyoming", "DC": "district of columbia", "PR": "puerto rico"}


def pha_candidates(name: str, city: str, state: str) -> list[str]:
    m = re.search(r"(.*?)\s+HOUSING AUTHORITY|HOUSING AUTHORITY\s+OF\s+(.*)", name or "", re.I)
    town = (m.group(1) or m.group(2) or "").strip() if m else ""
    town = re.sub(r"^(CITY OF|COUNTY OF|TOWN OF|THE)\s+", "", town, flags=re.I)
    town = re.sub(r"\s+(COUNTY|CITY|WEST|VIRGINIA|NORTH|SOUTH|EAST|WEST)$", "", town, flags=re.I).strip()
    if not town and city:
        town = city
    p = slug(town)
    if not p or len(p) < 3:
        return []
    st = (state or "").lower()
    return [f"https://www.hac{p}.org", f"https://www.{p}ha.org", f"https://www.{p}housing.org",
            f"https://www.{p}housingauthority.org", f"https://www.{p}pha.org",
            f"https://www.{p}ha.us", f"https://www.hac{p}.us", f"https://www.{p}housingauthority.com",
            f"https://www.{p}ha.gov", f"https://www.{p}.gov"]


def check_pha(org: dict) -> tuple[dict, str, int]:
    town = ""
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", org["name"] or "")
            if len(t) > 3 and t.lower() not in WORD_STOP]
    st = (org["state"] or "").upper()
    full = STATES.get(st, "")
    for url in pha_candidates(org["name"], org.get("city") or "", org["state"]):
        try:
            body = fetch(url, domain="org_gov", timeout=6).decode("utf-8", "replace")
        except Exception:
            continue
        low = body[:400_000].lower()
        state_ok = (bool(st) and re.search(r"\b" + re.escape(st.lower()) + r"\b", low) is not None) \
                   or (bool(full) and full in low)
        authority_ok = "housing authority" in low or "public housing" in low
        tokens_ok = sum(1 for t in toks if t.lower() in low) >= min(2, len(toks))
        if state_ok and authority_ok and tokens_ok:
            return org, url, 1
    return org, "", 0


def check(org: dict) -> tuple[dict, str, int]:
    place, kind = entity_place(org["name"])
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", org["name"] or "") if len(t) > 3 and t.lower() not in WORD_STOP]
    st = (org["state"] or "").lower().strip()
    for url in candidates(place, kind, org["state"]):
        try:
            body = fetch(url, domain="org_gov", timeout=6).decode("utf-8", "replace")
        except Exception:
            continue
        low = body[:400_000].lower()
        flat = re.sub(r"[^a-z0-9]", "", low)
        name_ok = slug(place) and slug(place) in flat
        full = STATES.get((org["state"] or "").upper(), "")
        state_ok = (bool(st) and re.search(r"\b" + re.escape(st) + r"\b", low) is not None) or \
                   (bool(full) and full in low)
        tokens_ok = all(t.lower() in low for t in toks) if toks else False
        # the state must be on the page: "Charleston" is a city name in eight states, and a
        # name-only match put Charleston SC's site on Charleston WV's record during the first run
        if (name_ok and state_ok) or (tokens_ok and state_ok):
            return org, url, 1
    return org, "", 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pha", action="store_true", help="housing authorities instead of cities/counties")
    ap.add_argument("--recheck", action="store_true", help="re-apply the strict rule to accepted rows")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS org_gov_site(
        org_id INTEGER PRIMARY KEY, url TEXT, matched INTEGER, checked_at TEXT)""")
    conn.commit()
    if a.recheck:
        rows = [dict(r) for r in conn.execute(
            """SELECT o.id, o.name, o.city, o.state, g.url FROM org_gov_site g
                 JOIN org o ON o.id = g.org_id
                WHERE g.matched = 1 AND o.website = g.url""")]
        print(f"rechecking {len(rows)} accepted sites", flush=True)
        dropped = 0
        for r in rows:
            try:
                body = fetch(r["url"], domain="org_gov", timeout=10).decode("utf-8", "replace")
            except Exception:
                body = ""
            low = body[:400_000].lower()
            st = (r["state"] or "").upper()
            full = STATES.get(st, "")
            state_ok = (bool(st) and re.search(r"\b" + re.escape(st.lower()) + r"\b", low) is not None) \
                       or (bool(full) and full in low)
            if not state_ok:
                if not a.dry_run:
                    conn.execute("UPDATE org SET website=NULL, updated_at=? WHERE id=?", (now(), r["id"]))
                    conn.execute("UPDATE org_gov_site SET matched=0 WHERE org_id=?", (r["id"],))
                dropped += 1
                print(f"  dropped {r['url']} for {r['name']} ({st})", flush=True)
        if not a.dry_run:
            conn.commit()
        print(json.dumps({"rechecked": len(rows), "dropped": dropped}))
        return
    seen = {r[0] for r in conn.execute("SELECT org_id FROM org_gov_site")}
    if a.pha:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, name, city, state FROM org
                WHERE website IS NULL AND name LIKE '%HOUSING AUTHORITY%' ORDER BY id""")]
    else:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, name, city, state FROM org
                WHERE website IS NULL AND NOT name LIKE '%HOUSING AUTHORITY%'
                  AND (name LIKE 'CITY OF %' OR name LIKE 'COUNTY OF %' OR name LIKE 'TOWN OF %'
                       OR name LIKE 'VILLAGE OF %' OR name LIKE 'BOROUGH OF %' OR name LIKE 'PARISH OF %')
                ORDER BY id""")]
    rows = [r for r in rows if r["id"] not in seen]
    if a.limit:
        rows = rows[: a.limit]
    print(f"to try: {len(rows):,}", flush=True)
    written = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for i, (org, url, matched) in enumerate(pool.map(check_pha if a.pha else check, rows), 1):
            if matched and not a.dry_run:
                conn.execute("UPDATE org SET website=?, updated_at=? WHERE id=?", (url, now(), org["id"]))
                written += 1
            if not a.dry_run:
                conn.execute("INSERT OR REPLACE INTO org_gov_site VALUES(?,?,?,?)",
                             (org["id"], url, matched, now()))
                if i % 50 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(rows)}] written={written}", flush=True)
    if not a.dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0]
    print(json.dumps({"tried": len(rows), "written": written, "websites_total_now": total}))


if __name__ == "__main__":
    main()
