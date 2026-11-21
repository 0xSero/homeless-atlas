#!/usr/bin/env python3
"""subawards — the second hop: what prime recipients hand on to local providers.

`award` records who holds federal money. A CoC grant to a county is only half the story: the county
then issues subawards to the shelters that actually run the projects. USASpending publishes those
sub-recipient records per prime award:

    POST /api/v2/subawards/  {"award_id": "<award.award_key>", "page": N, "limit": 100}
      -> {"results": [{subaward_number, recipient_name, recipient_uei, amount, action_date,
                       description, subaward_amount, sub_place_of_performance{...}}]}

Each response is cached under data/raw/subawards/<award_key>.p<N>.json and the fact that a prime
award has been walked is recorded in `subaward_progress`, so the sweep resumes instead of refetching.

Rows land in `award` with award_type='subaward' and award_key 'SUB_<prime key>_<subaward number>',
so the prime survives in the key and every subaward is traceable to the row it came from. Recipients
are matched to existing organisations on normalised name; unmatched ones get an org row with only
the published name and the source — no invented EIN, address or website.

Run: python3 -m ingest.domains.subawards [--limit-primes N] [--min-prime 250000] [--selftest]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ingest.common import db, fetch_post_json, name_norm, now, source_id_for  # noqa: E402

DOMAIN = "subawards"
API = "https://api.usaspending.gov/api/v2/subawards/"
API_DOC = "https://api.usaspending.gov/docs/endpoints"
HOMELESS = ("Continuum of Care Program", "Emergency Solutions Grant Program", "Supportive Housing Program",
            "Shelter Plus Care", "VA Supportive Services for Veteran Families Program",
            "VA Homeless Providers Grant and Per Diem Program", "Homeless Veterans’ Reintegration Program",
            "Youth Homelessness Demonstration Program")


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS subaward_progress(
        award_key TEXT PRIMARY KEY,
        pages INTEGER, rows INTEGER, status TEXT, checked_at TEXT)""")
    conn.commit()


def fetch_page(award_key: str, page: int):
    payload = {"award_id": award_key, "page": page, "limit": 100}
    return fetch_post_json(API, payload, domain=DOMAIN)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-primes", type=int, default=600)
    ap.add_argument("--min-prime", type=float, default=250_000)
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    conn = db()
    ensure_tables(conn)
    src = source_id_for(conn, API_DOC, DOMAIN,
                        title="USASpending subaward records", publisher="USAspending.gov")

    marks = ",".join("?" * len(HOMELESS))
    primes = conn.execute(f"""
        SELECT a.award_key, a.award_id, a.recipient_name, a.amount_obligated, a.place_state, a.place_city
          FROM award a
          LEFT JOIN subaward_progress p ON p.award_key = a.award_key
         WHERE a.program IN ({marks}) AND a.amount_obligated >= ?
           AND a.award_type != 'subaward' AND p.award_key IS NULL
         ORDER BY a.amount_obligated DESC LIMIT ?""",
        (*HOMELESS, a.min_prime, a.limit_primes)).fetchall()

    print(f"prime awards to walk: {len(primes)}")
    added = updated = primes_done = 0
    for i, pr in enumerate(primes, 1):
        key = pr["award_key"]
        total_rows, pages = 0, 0
        try:
            for page in range(1, a.pages + 1):
                d = fetch_page(key, page)
                results = d.get("results") or []
                pages = page
                for s in results:
                    num = (s.get("subaward_number") or "").strip()
                    name = (s.get("recipient_name") or "").strip()
                    if not num or not name:
                        continue
                    sub_key = f"SUB_{key}_{num}"[:180]
                    norm = name_norm(name)
                    org = conn.execute("SELECT id FROM org WHERE name_norm=? LIMIT 1", (norm,)).fetchone()
                    org_id = org[0] if org else None
                    if org_id is None:
                        cur = conn.execute("""INSERT OR IGNORE INTO org(name, name_norm, org_type, state,
                                                            source_id, updated_at)
                                              VALUES(?, ?, 'unknown', ?, ?, ?)""",
                                           (name, norm, pr["place_state"], src, now()))
                        org_id = cur.lastrowid if cur.lastrowid else \
                            conn.execute("SELECT id FROM org WHERE name_norm=? LIMIT 1", (norm,)).fetchone()[0]
                    exists = conn.execute("SELECT 1 FROM award WHERE award_key=?", (sub_key,)).fetchone()
                    conn.execute("""INSERT INTO award(award_key, award_id, recipient_name, recipient_norm,
                            recipient_uei, org_id, award_type, agency, subtier, program, description,
                            amount_obligated, amount_total, pop_start, fy, place_state, place_city, source_id)
                        VALUES(?,?,?,?,?,?,'subaward',?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(award_key) DO UPDATE SET
                            amount_obligated=excluded.amount_obligated,
                            description=excluded.description,
                            org_id=excluded.org_id,
                            source_id=excluded.source_id""",
                        (sub_key, num, name, norm, s.get("recipient_uei"), org_id,
                         None, None, f"subaward of {pr['award_id']}", (s.get("description") or "")[:400],
                         s.get("amount") or s.get("subaward_amount"), s.get("subaward_amount"),
                         (s.get("action_date") or "")[:10], (s.get("action_date") or "")[:4],
                         pr["place_state"], pr["place_city"], src))
                    updated += 1 if exists else 0
                    added += 0 if exists else 1
                    total_rows += 1
                if len(results) < 100:
                    break
                time.sleep(1)                     # 1 req/s, per the ingest contract
        except Exception as e:                     # a failed prime is recorded, not retried forever
            conn.execute("INSERT OR REPLACE INTO subaward_progress VALUES(?,?,?,?,?)",
                         (key, pages, total_rows, f"error:{type(e).__name__}", now()))
            conn.commit()
            print(f"  {key}: {type(e).__name__} {e}")
            continue
        conn.execute("INSERT OR REPLACE INTO subaward_progress VALUES(?,?,?,?,?)",
                     (key, pages, total_rows, "ok", now()))
        conn.commit()
        primes_done += 1
        if i % 25 == 0 or total_rows:
            print(f"  [{i}/{len(primes)}] {key}: {total_rows} subawards (new {added})", flush=True)

    n_subs = conn.execute("SELECT COUNT(*) FROM award WHERE award_type='subaward'").fetchone()[0]
    n_orgs = conn.execute("""SELECT COUNT(DISTINCT org_id) FROM award
                              WHERE award_type='subaward' AND org_id IS NOT NULL""").fetchone()[0]
    print(json.dumps({"primes_walked": primes_done, "subawards_new": added,
                      "subawards_total": n_subs, "subrecipient_orgs": n_orgs}))


if __name__ == "__main__":
    main()
