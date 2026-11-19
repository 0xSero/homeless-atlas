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

import hashlib
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
MAX_PAGES = 500          # hard cap per query (the API's own window is 50,000 rows)
SPLIT_THRESHOLD = 19000  # families above this are swept one fiscal year at a time
FY_RANGE = range(2008, 2028)   # federal fiscal years with data (API starts 2007-10-01)
ALL_TIME_END = "2027-09-30"
LOCK_RETRIES = 10        # concurrent writers: other domain agents hold write locks
LOCK_SLEEP = 30          # seconds between lock retries
BUSY_TIMEOUT = 600000    # 10 min sqlite busy timeout (several agents share this DB)
DESC_MAX = 4000          # description is truncated to this many chars

_PREFIX_LOADED: set = set()

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
def families() -> list[dict]:
    """Query families, most precise first (assistance listings, then NAICS, then
    keyword sweeps), so a partial run still holds the unambiguous awards."""
    fam: list[dict] = []
    for p in PROGRAM_CODES:
        fam.append({
            "key": f"program:{p}:grants",
            "label": f"grants assistance listing {p}",
            "filters": {"program_numbers": [p], "award_type_codes": GRANTS},
            "group": "grants",
        })
    for n in NAICS_CODES:
        fam.append({
            "key": f"naics:{n}:contracts",
            "label": f"contracts NAICS {n}",
            "filters": {"naics_codes": [n], "award_type_codes": CONTRACTS},
            "group": "contracts",
        })
    for kw in KEYWORDS:
        fam.append({
            "key": f"kw:{kw}:grants",
            "label": f"grants keyword '{kw}'",
            "filters": {"keywords": [kw], "award_type_codes": GRANTS},
            "group": "grants",
        })
    for kw in KEYWORDS:
        fam.append({
            "key": f"kw:{kw}:contracts",
            "label": f"contracts keyword '{kw}'",
            "filters": {"keywords": [kw], "award_type_codes": CONTRACTS},
            "group": "contracts",
        })
    return fam


def family_count(filt: dict):
    """Award count for a filter set, or None if the API refused it."""
    try:
        d = fetch_post_json(COUNT_URL, {"filters": filt}, domain=DOMAIN)
    except Exception:  # noqa: BLE001
        return None
    res = d.get("results") or {}
    return int(sum(v or 0 for v in res.values() if isinstance(v, int)))


def _all_time(filt):
    """The API's own default window (data starts 2007-10-01), stated explicitly."""
    f = dict(filt)
    f["time_period"] = [{"start_date": "2007-10-01", "end_date": ALL_TIME_END,
                         "date_type": "action_date"}]
    return f


def _fy_window(filt, fy):
    """One federal fiscal year of the same filter set.

    The search endpoint stops serving pages at a result-window limit and the
    default ordering slices awards by age, so a single all-time query silently
    loses whole year cohorts. Splitting per fiscal year keeps every query small.
    """
    f = dict(filt)
    f["time_period"] = [{"start_date": f"{fy - 1}-10-01", "end_date": f"{fy}-09-30",
                         "date_type": "action_date"}]
    return f


def build_queries(selftest: bool = False) -> list[dict]:
    """Expand families into concrete queries, per fiscal year where a family is
    too large for one result window."""
    qs: list[dict] = []
    for f in families():
        n = family_count(f["filters"])
        if selftest and f["key"] not in ("kw:homeless:grants", "naics:624221:contracts"):
            continue
        if n and n > SPLIT_THRESHOLD:
            for fy in FY_RANGE:
                qs.append({
                    "key": f"{f['key']}:fy{fy}",
                    "label": f"{f['label']} FY{fy}",
                    "filters": _fy_window(f["filters"], fy),
                    "group": f["group"],
                    "n_hint": None,
                    "family": f["key"],
                    "family_total": n,
                })
        else:
            qs.append({
                "key": f["key"],
                "label": f["label"],
                "filters": _all_time(f["filters"]),
                "group": f["group"],
                "n_hint": n,
                "family": f["key"],
                "family_total": n,
            })
    if selftest:
        qs = qs[:2]
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


def _listing(prefix, out: dict) -> None:
    """Load every assistance listing title for one 2-digit agency prefix."""
    for page in range(1, 6):
        url = f"{API}/api/v2/references/assistance_listing/{prefix}/?limit=100&page={page}"
        try:
            payload = json.loads(common.fetch(url, domain=DOMAIN))
        except Exception:  # noqa: BLE001
            return
        res = payload.get("results") or []
        for r in res:
            if r.get("code") and r["code"] not in out:
                out[r["code"]] = clean(r.get("description"))
        if len(res) < 100:
            return


def program_names() -> dict:
    """Assistance listing code -> title, read from the live reference endpoint.

    HUD (14.xxx) is loaded up front; other agencies are loaded lazily when an
    award carries one of their CFDA numbers.
    """
    out: dict = {}
    _listing("14", out)
    return out


def program_for(cfda, pmap: dict):
    """Title for a CFDA number, fetching that agency's listing on first use."""
    if not cfda:
        return None
    if cfda in pmap:
        return pmap[cfda]
    prefix = cfda.split(".")[0]
    if prefix not in _PREFIX_LOADED:
        _PREFIX_LOADED.add(prefix)
        _listing(prefix, pmap)
    return pmap.get(cfda)


def _cache_path(payload):
    """The on-disk fetch-cache file that ingest.common.fetch would use."""
    data = json.dumps(payload)
    key = hashlib.sha1(("POST" + SEARCH_URL + json.dumps(data, sort_keys=True, default=str)).encode()).hexdigest()
    return common.RAW / DOMAIN / f"{key}.bin"


def _cached(payload):
    p = _cache_path(payload)
    return p.exists() and p.stat().st_size > 0


def _write(conn, sql, params=()):
    """Execute + commit, riding out write locks held by other domain agents."""
    for attempt in range(LOCK_RETRIES):
        try:
            conn.execute(sql, params)
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(LOCK_SLEEP)


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
    budget = float(cfg.get("budget_s") or 0)   # 0 = run to completion
    cache_only = bool(cfg.get("cache_only"))   # load phase: never touch the network
    t0 = time.monotonic()
    out_of_time = False

    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT}")
    _write(conn, PROGRESS_DDL)

    pmap = program_names()
    src_id = None
    for attempt in range(LOCK_RETRIES):
        try:
            src_id = source_id_for(conn, SEARCH_URL, DOMAIN,
                                   title="USASpending.gov API v2 spending_by_award search",
                                   publisher="US Treasury / Bureau of the Fiscal Service",
                                   license=LICENSE)
            conn.commit()
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(LOCK_SLEEP)

    queries = build_queries(selftest)
    seen: set[str] = set()
    org_cache: dict[tuple, int] = {}
    notes: list[str] = []
    capped, zero, failed = [], [], []

    for q in queries:
        if budget and time.monotonic() - t0 > budget:
            out_of_time = True
            break
        n = q.get("n_hint")
        if n == 0:
            zero.append(q["key"])
            _write(conn,
                """INSERT INTO usaspending_progress(query_key,label,n_awards,pages_total,pages_done,done,note,updated_at)
                   VALUES(?,?,?,?,0,1,'zero results',?)
                   ON CONFLICT(query_key) DO UPDATE SET n_awards=excluded.n_awards,done=1,
                     note='zero results',updated_at=excluded.updated_at""",
                (q["key"], q["label"], 0, 0, now()))
            continue

        if n is None:
            pages_total = max_pages
            capped_note = None
        else:
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

        _write(conn,
            """INSERT INTO usaspending_progress(query_key,label,n_awards,pages_total,pages_done,done,note,updated_at)
               VALUES(?,?,?,?,?,0,?,?)
               ON CONFLICT(query_key) DO UPDATE SET n_awards=excluded.n_awards,
                 pages_total=excluded.pages_total,note=excluded.note,updated_at=excluded.updated_at""",
            (q["key"], q["label"], n, pages_total, start_page - 1, capped_note, now()))

        # --- paginate ------------------------------------------------------ #
        skipped = False
        page = start_page
        while page <= pages_total:
            if budget and time.monotonic() - t0 > budget:
                out_of_time = True
                notes.append(f"{q['key']}: paused at page {page} (time budget)")
                break
            payload = {
                "filters": q["filters"],
                "fields": FIELDS,
                "page": page,
                "limit": LIMIT,
            }
            if cache_only and not _cached(payload):
                notes.append(f"{q['key']}: page {page} not warmed yet, skipped")
                skipped = True
                break
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
                    time.sleep(LOCK_SLEEP)
            else:
                failed.append(f"{q['key']} p{page}: database locked, gave up")
                break
            page += 1

        if n is None and page > pages_total:
            capped_note = capped_note or f"page cap {max_pages} hit"
            capped.append(q["key"])
        if skipped:
            # leave the query incomplete so a later run picks it up again
            _write(conn,
                """UPDATE usaspending_progress SET done=0,pages_done=?,updated_at=? WHERE query_key=?""",
                (min(page - 1, pages_total), now(), q["key"]))
            continue
        if out_of_time:
            _write(conn,
                """UPDATE usaspending_progress SET pages_done=?,updated_at=? WHERE query_key=?""",
                (min(page - 1, pages_total), now(), q["key"]))
            break
        _write(conn,
            """UPDATE usaspending_progress SET done=1,pages_done=?,note=?,updated_at=?
               WHERE query_key=?""",
            (min(page - 1, pages_total), capped_note or "", now(), q["key"]))

    # another agent's `usaspending_sweep` ingestor wrote query labels into
    # award.program on rows we now own; upsert's COALESCE cannot clear a column,
    # so blank them explicitly (only rows carrying our source_id).
    if not cache_only:
        _write(conn,
               """UPDATE award SET program=NULL WHERE source_id=? AND program LIKE 'grants kw=%'""",
               (src_id,))
        # normalise empty strings left on shared rows (upsert drops our NULLs, so
        # COALESCE keeps whatever a previous writer put in the column)
        for col in ("award_id", "recipient_name", "recipient_norm", "recipient_uei", "agency",
                    "subtier", "office", "cfda", "psc", "naics", "program", "description",
                    "pop_start", "pop_end", "fy", "place_state", "place_city", "place_county"):
            _write(conn, f"UPDATE award SET {col}=NULL WHERE source_id=? AND trim({col})=''", (src_id,))
        for col in ("city", "state", "zip", "uei", "county", "address1"):
            _write(conn, f"UPDATE org SET {col}=NULL WHERE source_id=? AND trim({col})=''", (src_id,))

    awards_total = conn.execute("SELECT COUNT(*) FROM award").fetchone()[0]
    orgs_total = conn.execute("SELECT COUNT(*) FROM org").fetchone()[0]

    notes_out = [
        f"queries={len(queries)} awarded_rows_seen={len(seen)}",
        f"db award={awards_total} org={orgs_total}",
        f"capped={len(capped)}:{','.join(capped)}" if capped else "capped=0",
        f"zero={len(zero)}:{','.join(zero)}" if zero else "zero=0",
        f"failed={len(failed)}:{';'.join(failed[:6])}" if failed else "failed=0",
        f"program_names={len(pmap)}",
        "OUT_OF_TIME: rerun to resume" if out_of_time else "complete",
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
        "program": program_for(cfda, pmap),
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


def warm_cache(max_pages=None, verbose=True, budget_s=0):
    """Phase A: pull every page of every query into the on-disk fetch cache.

    Touches no database table, so it is not slowed down by the other domain
    agents writing to atlas.db; `run()` then replays the same requests from
    cache and only has to do the SQL. Resumable by construction: cached pages
    cost ~1 ms to re-request.
    """
    max_pages = int(max_pages or MAX_PAGES)
    queries = build_queries()
    pages_done = 0
    short = 0
    t0 = time.monotonic()
    for i, q in enumerate(queries, 1):
        if budget_s and time.monotonic() - t0 > budget_s:
            print(f"[warm] budget reached after {pages_done} pages, {i - 1}/{len(queries)} queries; rerun to resume",
                  flush=True)
            return pages_done
        pages = max_pages if q["n_hint"] is None else min(math.ceil(q["n_hint"] / LIMIT), max_pages)
        got = 0
        for page in range(1, pages + 1):
            if budget_s and time.monotonic() - t0 > budget_s:
                print(f"[warm] budget reached at {q['key']} p{page} ({pages_done} pages, "
                      f"{i - 1}/{len(queries)} queries); rerun to resume", flush=True)
                return pages_done
            payload = {"filters": q["filters"], "fields": FIELDS, "page": page, "limit": LIMIT}
            try:
                d = fetch_post_json(SEARCH_URL, payload, domain=DOMAIN)
            except Exception as e:  # noqa: BLE001
                if "maximum result limit" not in str(e):
                    print(f"[warm] {q['key']} p{page}: {str(e)[:160]}", flush=True)
                break
            if not (d.get("results") or []):
                break
            got += 1
            pages_done += 1
            if verbose and pages_done % 50 == 0:
                print(f"[warm] {pages_done} pages cached (query {i}/{len(queries)}: {q['key']} p{page}/{pages})",
                      flush=True)
        if got < pages:
            short += 1
        print(f"[warm] {q['key']}: {got}/{pages} pages", flush=True)
    print(f"[warm] done: {pages_done} pages cached, {short} queries short of their page budget", flush=True)
    return pages_done


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def _selftest():
    conn = common.db()
    # re-ingest the two sample queries from scratch so the gate is meaningful
    conn.execute("DELETE FROM usaspending_progress WHERE query_key IN "
                 "('kw:homeless:grants:fy2008', 'naics:624221:contracts')")
    conn.commit()
    res = run(conn, {"selftest": True, "max_pages": 1})
    # do not leave the one-page sample cursors behind: a real run must redo these
    conn.execute("DELETE FROM usaspending_progress WHERE query_key IN "
                 "('kw:homeless:grants:fy2008', 'naics:624221:contracts')")
    conn.commit()
    print(f"[{DOMAIN}] selftest rows={res['rows']} notes={res['notes']}")
    common.selftest_report(conn, DOMAIN, "award", "award_key IS NOT NULL", 3)
    common.selftest_report(conn, DOMAIN, "org", "uei IS NOT NULL", 3)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--load" in sys.argv:
        b = 0
        for a in sys.argv[1:]:
            if a.startswith("--budget="):
                b = int(a.split("=", 1)[1])
        common.main(DOMAIN, lambda c, cfg: run(c, {"cache_only": True, "budget_s": b}), "award")
    elif "--warm" in sys.argv:
        mp, bs = None, 0
        for a in sys.argv[1:]:
            if a.startswith("--max-pages="):
                mp = int(a.split("=", 1)[1])
            if a.startswith("--budget="):
                bs = int(a.split("=", 1)[1])
        warm_cache(mp, budget_s=bs)
    else:
        common.main(DOMAIN, run, "award")
