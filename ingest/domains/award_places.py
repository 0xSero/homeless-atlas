#!/usr/bin/env python3
"""Fill award place-of-performance city/county, and org addresses, from the USASpending award detail API.

`place_city` and `place_county` were empty for every award, so no city page could show money and the
San Francisco brief could not total its own contracts. The search endpoint only returns place codes;
the award detail endpoint returns names:

    place_of_performance: {state_code, county_name, city_name, zip4}
    recipient.location:   {city_name, county_name, state_code, zip}

Awards are processed largest-first (that is the money that matters on a city page) and every award
key is recorded in `award_places_progress`, so the run is resumable and idempotent.

Run: python3 -m ingest.domains.award_places [--limit N] [--min-amount 0]
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch, now, source_id_for, upsert  # noqa: E402

DOMAIN = "award_places"
API = "https://api.usaspending.gov/api/v2/awards/{}/"


def conn_rw():
    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS award_places_progress(
                   award_key TEXT PRIMARY KEY, ok INTEGER, note TEXT, ts TEXT)""")
    c.commit()
    return c


def run(limit=None, min_amount=0, sleep=1.0):
    c = conn_rw()
    src = source_id_for(c, API.format("{award_key}"), DOMAIN,
                        title="USASpending award detail (place of performance, recipient location)",
                        publisher="US Treasury / USASpending.gov", license="public domain")
    c.commit()
    q = """SELECT a.award_key, a.amount_obligated, a.org_id, a.place_city, a.place_county
           FROM award a LEFT JOIN award_places_progress p ON p.award_key = a.award_key
           WHERE p.award_key IS NULL AND a.award_key IS NOT NULL
             AND COALESCE(a.amount_obligated,0) >= ?
           ORDER BY COALESCE(a.amount_obligated,0) DESC"""
    todo = c.execute(q, (min_amount,)).fetchall()
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} awards to enrich", flush=True)
    done = ok = with_city = orgs_filled = 0
    for row in todo:
        key = row["award_key"]
        try:
            payload = json.loads(fetch(API.format(key), domain=DOMAIN))
        except Exception as e:
            c.execute("INSERT OR REPLACE INTO award_places_progress VALUES(?,0,?,?)",
                      (key, repr(e)[:200], now()))
            c.commit()
            done += 1
            continue
        pop = payload.get("place_of_performance") or {}
        rec_loc = (payload.get("recipient") or {}).get("location") or {}
        city = pop.get("city_name") or rec_loc.get("city_name")
        county = pop.get("county_name") or rec_loc.get("county_name")
        state = pop.get("state_code") or rec_loc.get("state_code")
        if city or county or state:
            c.execute("""UPDATE award SET place_city=COALESCE(?,place_city),
                         place_county=COALESCE(?,place_county),
                         place_state=COALESCE(?,place_state), source_id=COALESCE(source_id,?)
                         WHERE award_key=?""", (city, county, state, src, key))
            if city:
                with_city += 1
        if row["org_id"] and (rec_loc.get("city_name") or rec_loc.get("zip")):
            cur = c.execute("SELECT city,state,zip,address1 FROM org WHERE id=?", (row["org_id"],)).fetchone()
            if cur and not (cur["city"] and cur["state"]):
                c.execute("""UPDATE org SET city=COALESCE(city,?), state=COALESCE(state,?),
                             zip=COALESCE(zip,?), address1=COALESCE(address1,?) WHERE id=?""",
                          (rec_loc.get("city_name"), rec_loc.get("state_code"),
                           (rec_loc.get("zip") or "")[:10] or None, rec_loc.get("address_line1"), row["org_id"]))
                orgs_filled += 1
        c.execute("INSERT OR REPLACE INTO award_places_progress VALUES(?,1,?,?)", (key, city or "", now()))
        done += 1
        ok += 1
        if done % 50 == 0:
            c.commit()
            print(f"  {done}/{len(todo)} · city filled {with_city} · orgs {orgs_filled}", flush=True)
        if done % 500 == 0:
            chk = c.execute("PRAGMA quick_check").fetchone()[0]
            if chk != "ok":
                print(f"ABORT: database integrity check failed: {chk}", flush=True)
                c.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,0)",
                          (DOMAIN, now(), ok, f"aborted on integrity failure at {done}"))
                c.commit()
                raise SystemExit(2)
        time.sleep(sleep)
    c.commit()
    c.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
              (DOMAIN, now(), ok, f"enriched={ok} city={with_city} orgs={orgs_filled} attempted={done}"))
    c.commit()
    total_city = c.execute("SELECT COUNT(*) FROM award WHERE place_city IS NOT NULL").fetchone()[0]
    print(json.dumps({"attempted": done, "ok": ok, "with_city": with_city,
                      "orgs_filled": orgs_filled, "awards_with_city_total": total_city}))
    return {"rows": ok, "notes": f"city={with_city} orgs={orgs_filled}"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--min-amount", type=float, default=0)
    a = ap.parse_args()
    run(limit=a.limit, min_amount=a.min_amount)
