"""usaspending — federal contracts, grants and cooperative agreements related to homelessness.

Keyless. Uses the USASpending.gov v2 search API:
  POST /api/v2/search/spending_by_award_count/   -> size a query
  POST /api/v2/search/spending_by_award/         -> page results (limit 100)

Query families
  * keyword sweeps ("homeless", "continuum of care", ...) over grants (02-05)
    and contracts (A-D) separately (the API refuses mixed award-type groups)
  * contracts by NAICS 624221/624229/623220/925110
  * grants by Assistance Listing (CFDA) program number, incl. the historical
    HUD homeless programs (14.235 SHP, 14.238 SPC); program *names* are read
    from /api/v2/references/assistance_listing/14/ at run time, never hardcoded

Awards are keyed by `generated_internal_id` -> award.award_key (UNIQUE).
Orgs are keyed by (name_norm, state); UEI/city/zip come from the recipient
block of the search response.

Resumable: per-query page cursor lives in `usaspending_progress`, and every
response is disk-cached by ingest.common.fetch, so a rerun replays from cache.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import time

from ingest import common
from ingest.common import clean, fetch_post_json, name_norm, now, source_id_for, upsert

DOMAIN = "usaspending"
API = "https://api.usaspending.gov"
SEARCH_URL = API + "/api/v2/search/spending_by_award/"
COUNT_URL = API + "/api/v2/search/spending_by_award_count/"
ASSIST_LIST_URL = API + "/api/v2/references/assistance_listing/14/"
LICENSE = "Public domain (US Government work)"

GRANTS = ["02", "03", "04", "05"]      # block/formula/project/cooperative agreement
CONTRACTS = ["A", "B", "C", "D"]       # BPA call, purchase order, delivery order, definitive contract

KEYWORDS = [
    "homeless",
    "homelessness",
    "continuum of care",
    "emergency solutions grant",
    "rapid rehousing",
    "permanent supportive housing",
    "HMIS",
    "Housing First",
    "transitional housing",
    "street outreach",
    "coordinated entry",
]

NAICS_CODES = ["624221", "624229", "623220", "925110"]

# Assistance Listing numbers for HUD homeless / homelessness-adjacent programs.
# Codes only — names are resolved from the live assistance-listing endpoint.
PROGRAM_CODES = [
    "14.231",  # Emergency Solutions Grant Program
    "14.267",  # Continuum of Care Program
    "14.241",  # HOPWA
    "14.275",  # Housing Trust Fund
    "14.239",  # HOME Investment Partnerships Program
    "14.235",  # Supportive Housing Program (pre-2012)
    "14.238",  # Shelter Plus Care (pre-2012)
    "14.276",  # Youth Homelessness Demonstration Program
    "14.249",  # Section 8 Moderate Rehab SRO
]

FIELDS = [
    "Award ID",
    "generated_internal_id",
    "Recipient Name",
    "Recipient UEI",
    "Recipient Location",
    "Award Amount",
    "Potential Award Amount",
    "Total Obligation",
    "Total Outlays",
    "Description",
    "Start Date",
    "End Date",
    "Base Obligation Date",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Awarding Office",
    "Funding Agency",
    "Funding Sub Agency",
    "CFDA Number",
    "NAICS",
    "PSC",
    "Award Type",
    "Contract Award Type",
    "Place of Performance State Code",
    "Place of Performance City Code",
    "Place of Performance County Code",
    "Place of Performance Zip5",
]

LIMIT = 100
MAX_PAGES = 200          # hard cap per query
LOCK_RETRIES = 8         # concurrent writers: other domain agents hold write locks
DESC_MAX = 4000          # description is truncated to this many chars

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS usaspending_progress (
  query_key   TEXT PRIMARY KEY,
  label       TEXT,
  n_awards    INTEGER,
  pages_total INTEGER,
  pages_done  INTEGER DEFAULT 0,
  done        INTEGER DEFAULT 0,
  note        TEXT,
  updated_at  TEXT
)
"""


# --------------------------------------------------------------------------- #
# query plan
# --------------------------------------------------------------------------- #
def build_queries(selftest: bool = False) -> list[dict]:
    """Most precise families first (assistance listings, then NAICS), so a partial
    run still holds the unambiguous homeless-programme awards."""
    qs: list[dict] = []
    for p in PROGRAM_CODES:
        qs.append({
            "key": f"program:{p}:grants",
            "label": f"grants assistance listing {p}",
            "filters": {"program_numbers": [p], "award_type_codes": GRANTS},
            "group": "grants",
        })
    for n in NAICS_CODES:
        qs.append({
            "key": f"naics:{n}:contracts",
            "label": f"contracts NAICS {n}",
            "filters": {"naics_codes": [n], "award_type_codes": CONTRACTS},
            "group": "contracts",
        })
    for kw in KEYWORDS:
        qs.append({
            "key": f"kw:{kw}:grants",
            "label": f"grants keyword '{kw}'",
            "filters": {"keywords": [kw], "award_type_codes": GRANTS},
            "group": "grants",
        })
    for kw in KEYWORDS:
        qs.append({
            "key": f"kw:{kw}:contracts",
            "label": f"contracts keyword '{kw}'",
            "filters": {"keywords": [kw], "award_type_codes": CONTRACTS},
            "group": "contracts",
        })
    if selftest:
        qs = [q for q in qs if q["key"] in ("kw:homeless:grants", "naics:624221:contracts")]
    return qs


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ffy(ds):
    """Federal fiscal year (Oct-Sep) of an ISO date string."""
    if not ds or len(str(ds)) < 7:
        return None
    try:
        y, m = int(str(ds)[:4]), int(str(ds)[5:7])
    except ValueError:
        return None
    return str(y + 1 if m >= 10 else y)


def _org_type(name):
    """Only unambiguous, name-derived types; anything else stays NULL."""
    n = (name or "").upper()
    if not n:
        return None
    if "HOUSING AUTHORITY" in n:
        return "housing_authority"
    if re.match(r"^(CITY|COUNTY|TOWN|TOWNSHIP|VILLAGE|BOROUGH|PARISH) OF\b", n):
        return "government"
    if re.match(r"^(STATE OF|COMMONWEALTH OF|STATE DEPT|STATE DEPARTMENT)\b", n):
        return "government"
    return None


def program_names() -> dict:
    """Assistance listing code -> description, from the live reference endpoint."""
    out, page = {}, 1
    while page <= 10:
        url = f"{ASSIST_LIST_URL}?limit=100&page={page}"
        try:
            payload = json.loads(common.fetch(url, domain=DOMAIN))
        except Exception:
            break
        res = payload.get("results") or []
        for r in res:
            if r.get("code"):
                out[r["code"]] = clean(r.get("description"))
        if len(res) < 100:
            break
        page += 1
    return out


def _money(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _code(field):
    if isinstance(field, dict):
        return clean(field.get("code"))
    return None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def run(conn, cfg):
    cfg = cfg or {}
    selftest = bool(cfg.get("selftest"))
    max_pages = int(cfg.get("max_pages") or (1 if selftest else MAX_PAGES))

    conn.execute("PRAGMA busy_timeout = 120000")
    conn.execute(PROGRESS_DDL)
    conn.commit()

    pmap = program_names()
    src_id = source_id_for(conn, SEARCH_URL, DOMAIN,
                           title="USASpending.gov API v2 spending_by_award search",
                           publisher="US Treasury / Bureau of the Fiscal Service",
                           license=LICENSE)
    conn.commit()

    queries = build_queries(selftest)
    seen: set[str] = set()
    org_cache: dict[tuple, int] = {}
    notes: list[str] = []
    capped, zero, failed = [], [], []

    for q in queries:
        # --- size the query (cheap, cached) -------------------------------- #
        try:
            cnt = fetch_post_json(COUNT_URL, {"filters": q["filters"]}, domain=DOMAIN)
            res = cnt.get("results") or {}
            n = int(sum(v or 0 for v in res.values() if isinstance(v, int)))
        except Exception as e:  # noqa: BLE001
            failed.append(f"{q['key']} (count: {str(e)[:120]})")
            continue
        if n == 0:
            zero.append(q["key"])
            conn.execute(
                """INSERT INTO usaspending_progress(query_key,label,n_awards,pages_total,pages_done,done,note,updated_at)
                   VALUES(?,?,?,?,0,1,'zero results',?)
                   ON CONFLICT(query_key) DO UPDATE SET n_awards=excluded.n_awards,done=1,
                     note='zero results',updated_at=excluded.updated_at""",
                (q["key"], q["label"], 0, 0, now()))
            conn.commit()
            continue

        pages_needed = math.ceil(n / LIMIT)
        pages_total = min(pages_needed, max_pages)
        capped_note = None
        if pages_needed > pages_total:
            capped_note = f"page cap {max_pages} hit ({pages_needed} pages needed, {n} awards)"
            capped.append(q["key"])

        row = conn.execute("SELECT pages_done,done FROM usaspending_progress WHERE query_key=?",
                           (q["key"],)).fetchone()
        start_page = 1
        if row:
            if row["done"]:
                notes.append(f"{q['key']}: already complete ({row['pages_done']} pages)")
                continue
            start_page = int(row["pages_done"] or 0) + 1
            if row["pages_done"]:
                notes.append(f"{q['key']}: resuming at page {start_page}")

        conn.execute(
            """INSERT INTO usaspending_progress(query_key,label,n_awards,pages_total,pages_done,done,note,updated_at)
               VALUES(?,?,?,?,?,0,?,?)
               ON CONFLICT(query_key) DO UPDATE SET n_awards=excluded.n_awards,
                 pages_total=excluded.pages_total,note=excluded.note,updated_at=excluded.updated_at""",
            (q["key"], q["label"], n, pages_total, start_page - 1, capped_note, now()))
        conn.commit()

        # --- paginate ------------------------------------------------------ #
        page = start_page
        while page <= pages_total:
            payload = {
                "filters": q["filters"],
                "fields": FIELDS,
                "page": page,
                "limit": LIMIT,
            }
            try:
                d = fetch_post_json(SEARCH_URL, payload, domain=DOMAIN)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "maximum result limit" in msg:
                    capped_note = capped_note or "api max result limit"
                    break
                failed.append(f"{q['key']} p{page}: {msg[:160]}")
                break

            results = d.get("results") or []
            if not results:
                break

            # other agents write to this DB too; retry the page as a whole on
            # lock contention, restoring the in-memory dedupe state first
            for attempt in range(LOCK_RETRIES):
                snap_seen, snap_org = set(seen), dict(org_cache)
                try:
                    with conn:
                        for r in results:
                            _write_award(conn, r, q["group"], src_id, pmap, seen, org_cache)
                        conn.execute(
                            """UPDATE usaspending_progress SET pages_done=?,updated_at=? WHERE query_key=?""",
                            (page, now(), q["key"]))
                    break
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e) and "busy" not in str(e):
                        raise
                    seen.clear()
                    seen.update(snap_seen)
                    org_cache.clear()
                    org_cache.update(snap_org)
                    notes.append(f"{q['key']} p{page}: locked, retry {attempt + 1}/{LOCK_RETRIES}")
                    time.sleep(20)
            else:
                failed.append(f"{q['key']} p{page}: database locked, gave up")
                break
            page += 1

        with conn:
            conn.execute(
                """UPDATE usaspending_progress SET done=1,pages_done=?,note=?,updated_at=?
                   WHERE query_key=?""",
                (min(page - 1, pages_total), capped_note or "", now(), q["key"]))

    awards_total = conn.execute("SELECT COUNT(*) FROM award").fetchone()[0]
    orgs_total = conn.execute("SELECT COUNT(*) FROM org").fetchone()[0]

    notes_out = [
        f"queries={len(queries)} awarded_rows_seen={len(seen)}",
        f"db award={awards_total} org={orgs_total}",
        f"capped={len(capped)}:{','.join(capped)}" if capped else "capped=0",
        f"zero={len(zero)}:{','.join(zero)}" if zero else "zero=0",
        f"failed={len(failed)}:{';'.join(failed[:6])}" if failed else "failed=0",
        f"program_names={len(pmap)}",
    ] + notes[-8:]

    return {
        "rows": len(seen),
        "notes": " | ".join(notes_out),
        "sources": [(SEARCH_URL, LICENSE), (API + "/api/v2/search/spending_by_award_count/", LICENSE),
                    (ASSIST_LIST_URL, LICENSE)],
    }


def _write_award(conn, r, group, src_id, pmap, seen: set, org_cache: dict) -> None:
    key = clean(r.get("generated_internal_id"))
    if not key:
        return
    if key in seen:
        return
    seen.add(key)

    rname = clean(r.get("Recipient Name"))
    loc = r.get("Recipient Location") or {}
    state = clean(loc.get("state_code"))
    if not state:
        ctry = clean(loc.get("location_country_code"))
        state = "US" if (ctry in (None, "USA")) else ctry
    norm = name_norm(rname)

    org_id = None
    if rname and norm:
        ok = (norm, state)
        org_id = org_cache.get(ok)
        if org_id is None:
            upsert(conn, "org", {
                "name": rname,
                "name_norm": norm,
                "uei": clean(r.get("Recipient UEI")),
                "org_type": _org_type(rname),
                "address1": clean(loc.get("address_line1")),
                "city": clean(loc.get("city_name")),
                "state": state,
                "zip": clean(loc.get("zip5")),
                "county": clean(loc.get("county_name")),
                "status": "unknown",
                "source_id": src_id,
                "updated_at": now(),
            }, keys=["name_norm", "state"])
            row = conn.execute("SELECT id FROM org WHERE name_norm=? AND state=?", ok).fetchone()
            org_id = row["id"] if row else None
            org_cache[ok] = org_id

    cfda = clean(r.get("CFDA Number"))
    award_amount = _money(r.get("Award Amount"))
    potential = _money(r.get("Potential Award Amount"))
    obligated = _money(r.get("Total Obligation"))
    start = clean(r.get("Start Date"))
    base_obl = clean(r.get("Base Obligation Date"))

    upsert(conn, "award", {
        "award_key": key,
        "award_id": clean(r.get("Award ID")),
        "recipient_name": rname,
        "recipient_norm": norm or None,
        "recipient_uei": clean(r.get("Recipient UEI")),
        "org_id": org_id,
        "award_type": "contract" if group == "contracts" else "grant",
        "agency": clean(r.get("Awarding Agency")),
        "subtier": clean(r.get("Awarding Sub Agency")),
        "office": clean(r.get("Awarding Office")),
        "cfda": cfda,
        "psc": _code(r.get("PSC")),
        "naics": _code(r.get("NAICS")),
        "program": pmap.get(cfda) if cfda else None,
        "description": (clean(r.get("Description")) or "")[:DESC_MAX] or None,
        "amount_obligated": obligated if obligated is not None else award_amount,
        "amount_total": potential if potential is not None else award_amount,
        "pop_start": start,
        "pop_end": clean(r.get("End Date")),
        "fy": _ffy(base_obl or start),
        "place_state": clean(r.get("Place of Performance State Code")),
        # the search payload carries only a geocode for place of performance
        # (and grants mask it as e.g. "HI*****"); no city name is returned.
        "place_city": None,
        "place_county": None,
        "source_id": src_id,
        "is_est": 0,
    }, keys=["award_key"])


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def _selftest():
    conn = common.db()
    res = run(conn, {"selftest": True, "max_pages": 1})
    conn.commit()
    print(f"[{DOMAIN}] selftest rows={res['rows']} notes={res['notes']}")
    common.selftest_report(conn, DOMAIN, "award", "award_key IS NOT NULL", 3)
    common.selftest_report(conn, DOMAIN, "org", "uei IS NOT NULL", 3)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        common.main(DOMAIN, run, "award")
