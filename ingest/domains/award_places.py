#!/usr/bin/env python3
"""award_places — where the federal money is actually spent, city and county.

`award.place_state` was filled for 18.5k/18.7k rows but `place_city` / `place_county` were empty
everywhere, so no city page could show money and the San Francisco brief could not total itself.

The award *search* endpoint only returns place **codes** ("PA**043"), which do not resolve to a
name; the award *detail* endpoint returns names but costs one request per award (187,120 awards at
1 req/s = 52 hours). The USASpending **bulk award download** endpoint answers the same filter set
as the sweep that built the `award` table and returns the resolved names in a CSV, so the whole
universe is covered in a handful of requests:

    POST /api/v2/download/awards/   {"filters": ..., "file_format": "csv", "columns": [...]}
      -> {"status_url": ..., "file_url": ...}
    GET  /api/v2/download/status?file_name=...   -> {"status": "finished"|"running"|"failed"}
    GET  <file_url>                              -> ZIP with
        Contracts_PrimeAwardSummaries_*.csv   /  Assistance_PrimeAwardSummaries_*.csv
        (plus a subaward csv we ignore)

Columns used (verified in the delivered header, not assumed):

    assistance : assistance_award_unique_key, primary_place_of_performance_city_name,
                 primary_place_of_performance_county_name,
                 prime_award_summary_place_of_performance_{county,state}_fips_code,
                 primary_place_of_performance_{state_name,zip_4,scope,country_code},
                 recipient_{uei,name,address_line_1,city_name,county_name,state_code,zip_code,
                 country_code}
    contracts  : contract_award_unique_key, plus the same primary_place_of_performance_* and
                 recipient_* names (contracts use `primary_place_of_performance_state_code` and
                 `recipient_zip_4_code` where assistance uses `..._state_name` and `recipient_zip_code`)

`<*_unique_key>` is exactly `award.award_key` (`generated_internal_id`), which is how the CSV rows
are joined back to the archive.

The plan is four merged filters (the union of the query families `usaspending.py` used to build the
table, widened to OR-once instead of one request per listing/NAICS/keyword). Every plan item, its
generated file_url, its row counts and any failure are recorded in `award_places_plan`, and every
award key seen is recorded in `award_places_progress`, so the run is resumable and re-runnable.

Run: python3 -m ingest.domains.award_places [--selftest] [--plans-csv-out logs/x.csv]
"""
from __future__ import annotations
import argparse, csv, io, json, sys, time, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import (DB, clean, db, fetch, fetch_json, fetch_post_json, now,  # noqa: E402
                           source_id_for)

DOMAIN = "award_places"
DOWNLOAD_URL = "https://api.usaspending.gov/api/v2/download/awards/"
STATUS_URL = "https://api.usaspending.gov/api/v2/download/status?file_name={}"
COUNT_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award_count/"

# the filter families that built the award table (ingest/domains/usaspending.py), merged so that
# one download covers a whole family instead of one request per listing / NAICS / keyword / year
GRANTS = ["02", "03", "04", "05"]
CONTRACTS = ["A", "B", "C", "D"]
PROGRAM_CODES = ["14.231", "14.267", "14.241", "14.275", "14.239",
                 "14.235", "14.238", "14.276", "14.249"]
NAICS_CODES = ["624221", "624229", "623220", "925110"]
KEYWORDS = ["homeless", "homelessness", "continuum of care", "emergency solutions grant",
            "rapid rehousing", "permanent supportive housing", "HMIS", "Housing First",
            "transitional housing", "street outreach", "coordinated entry"]
# scripts/sweep_awards.py (which built part of the award table) also swept the bare keyword
# "shelter"; 1,343 award rows carry program='kw=shelter FY2023'. Kept as a separate plan item so
# the two big merged downloads did not have to be regenerated.
EXTRA_KEYWORDS = ["shelter"]
ALL_TIME = [{"start_date": "2007-10-01", "end_date": "2027-09-30", "date_type": "action_date"}]
FY_RANGE = range(2008, 2028)

ASSIST_COLS = [
    "assistance_award_unique_key", "award_id_fain",
    "primary_place_of_performance_city_name", "primary_place_of_performance_county_name",
    "prime_award_summary_place_of_performance_county_fips_code",
    "prime_award_summary_place_of_performance_state_fips_code",
    "primary_place_of_performance_state_name", "primary_place_of_performance_zip_4",
    "primary_place_of_performance_scope", "primary_place_of_performance_country_code",
    "recipient_uei", "recipient_name", "recipient_address_line_1", "recipient_city_name",
    "recipient_county_name", "recipient_state_code", "recipient_zip_code",
    "prime_award_summary_recipient_county_fips_code", "recipient_country_code",
    "total_obligated_amount",
]
CONTRACT_COLS = [
    "contract_award_unique_key", "award_id_piid",
    "primary_place_of_performance_city_name", "primary_place_of_performance_county_name",
    "prime_award_summary_place_of_performance_county_fips_code",
    "prime_award_summary_place_of_performance_state_fips_code",
    "primary_place_of_performance_state_code",
    "primary_place_of_performance_zip_4", "primary_place_of_performance_country_code",
    "recipient_uei", "recipient_name", "recipient_address_line_1", "recipient_city_name",
    "recipient_county_name", "recipient_state_code", "recipient_zip_4_code",
    "prime_award_summary_recipient_county_fips_code", "recipient_country_code",
    "total_obligated_amount",
]

POLL_INTERVAL = 10
POLL_MAX = 420          # 70 minutes of wall clock per download generation
SHORTFALL = 0.90        # if a merged download returns <90% of the API's own count, go per-FY

PLAN_DDL = """
CREATE TABLE IF NOT EXISTS award_places_plan (
  plan_key    TEXT PRIMARY KEY,
  group_name  TEXT,
  family      TEXT,
  filters     TEXT,
  n_hint      INTEGER,
  status      TEXT,
  file_name   TEXT,
  file_url    TEXT,
  rows_read   INTEGER,
  rows_matched INTEGER,
  awards_city INTEGER,
  orgs_filled INTEGER,
  note        TEXT,
  updated_at  TEXT
)"""
PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS award_places_progress (
  award_key TEXT PRIMARY KEY, ok INTEGER, note TEXT, ts TEXT)"""
FILL_DDL = """
CREATE TABLE IF NOT EXISTS award_places_fill (
  award_key   TEXT PRIMARY KEY,
  award_id    TEXT,
  plan_key    TEXT,
  place_city  TEXT,
  place_county TEXT,
  place_state TEXT,
  county_fips TEXT,
  pop_scope   TEXT,
  pop_country TEXT,
  amount      REAL,
  source_id   INTEGER,
  ts          TEXT)"""
ORG_FILL_DDL = """
CREATE TABLE IF NOT EXISTS award_places_org_fill (
  org_id    INTEGER PRIMARY KEY,
  uei       TEXT,
  plan_key  TEXT,
  city      TEXT,
  county    TEXT,
  state     TEXT,
  zip       TEXT,
  address1  TEXT,
  source_id INTEGER,
  ts        TEXT)"""


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def plan_items() -> list[dict]:
    return [
        dict(key="grants_programs_all", group_name="grants", family="programs",
             filters={"program_numbers": PROGRAM_CODES, "award_type_codes": GRANTS,
                      "time_period": ALL_TIME}),
        dict(key="grants_keywords_all", group_name="grants", family="keywords",
             filters={"keywords": KEYWORDS, "award_type_codes": GRANTS,
                      "time_period": ALL_TIME}),
        dict(key="contracts_naics_all", group_name="contracts", family="naics",
             filters={"naics_codes": NAICS_CODES, "award_type_codes": CONTRACTS,
                      "time_period": ALL_TIME}),
        dict(key="contracts_keywords_all", group_name="contracts", family="keywords",
             filters={"keywords": KEYWORDS, "award_type_codes": CONTRACTS,
                      "time_period": ALL_TIME}),
        dict(key="grants_keywords_extra", group_name="grants", family="keywords_extra",
             filters={"keywords": EXTRA_KEYWORDS, "award_type_codes": GRANTS,
                      "time_period": ALL_TIME}),
        dict(key="contracts_keywords_extra", group_name="contracts", family="keywords_extra",
             filters={"keywords": EXTRA_KEYWORDS, "award_type_codes": CONTRACTS,
                      "time_period": ALL_TIME}),
    ]


def probe_items() -> list[dict]:
    """Tiny, already-cacheable filter used by --selftest: exercises the whole pipeline."""
    return [dict(key="selftest_probe", group_name="contracts", family="probe",
                 filters={"award_type_codes": CONTRACTS, "naics_codes": ["925110"],
                          "time_period": [{"start_date": "2024-10-01", "end_date": "2024-12-31",
                                           "date_type": "action_date"}]})]


def fy_items(parent: dict) -> list[dict]:
    """Fallback: the same filter split into federal fiscal years (each result set stays small)."""
    out = []
    for fy in FY_RANGE:
        f = dict(parent["filters"])
        f["time_period"] = [{"start_date": f"{fy - 1}-10-01", "end_date": f"{fy}-09-30",
                             "date_type": "action_date"}]
        out.append(dict(key=f"{parent['key']}:fy{fy}", group_name=parent["group_name"],
                        family=parent["family"], filters=f, parent=parent["key"]))
    return out


def plan_count(filters: dict):
    """The API's own award count for a filter set (free reference point for the shortfall check)."""
    try:
        d = fetch_post_json(COUNT_URL, {"filters": filters}, domain=DOMAIN)
    except Exception:  # noqa: BLE001
        return None
    res = d.get("results") or {}
    return int(sum(v or 0 for v in res.values() if isinstance(v, int)))


# --------------------------------------------------------------------------- #
# download + poll
# --------------------------------------------------------------------------- #
def request_download(item: dict) -> dict:
    payload = {"filters": item["filters"], "file_format": "csv",
               "columns": CONTRACT_COLS if item["group_name"] == "contracts" else ASSIST_COLS}
    return fetch_post_json(DOWNLOAD_URL, payload, domain=DOMAIN)


def wait_finished(status_url: str) -> dict:
    t0 = time.time()
    while time.time() - t0 < POLL_MAX:
        st = fetch_json(status_url, domain=DOMAIN, force=True)
        s = st.get("status")
        if s == "finished":
            return st
        if s == "failed":
            raise RuntimeError(f"download failed: {status_url} {st.get('message') or ''}"[:300])
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"download did not finish within {POLL_MAX}s: {status_url}")


def fetch_zip(item: dict, req: dict) -> bytes:
    """Pull the generated ZIP, re-requesting the download once if the server dropped the file."""
    try:
        return fetch(req["file_url"], domain=DOMAIN)
    except Exception:  # noqa: BLE001
        import hashlib
        from ingest.common import RAW
        key = hashlib.sha1(("POST" + DOWNLOAD_URL + json.dumps(
            {"filters": item["filters"], "file_format": "csv",
             "columns": CONTRACT_COLS if item["group_name"] == "contracts" else ASSIST_COLS},
            sort_keys=True, default=str)).encode()).hexdigest()
        p = RAW / DOMAIN / f"{key}.bin"
        p.with_suffix(".meta.json").unlink(missing_ok=True)
        p.unlink(missing_ok=True)
        req = request_download(item)
        wait_finished(req["status_url"])
        return fetch(req["file_url"], domain=DOMAIN)


# --------------------------------------------------------------------------- #
# parsing / loading
# --------------------------------------------------------------------------- #
def prime_csv_from_zip(blob: bytes):
    z = zipfile.ZipFile(io.BytesIO(blob))
    for name in z.namelist():
        base = name.rsplit("/", 1)[-1]
        if base.startswith(("Contracts_PrimeAwardSummaries", "Assistance_PrimeAwardSummaries")):
            with z.open(name) as fh:
                yield csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
            return


def zip5(v):
    """USASpending gives recipient_zip_code (5) for assistance and recipient_zip_4_code for
    contracts; keep only the 5-digit ZIP (never invented, just truncated at the ZIP boundary)."""
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[:5] or None


def state_abbr_from_fips(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT fips, abbr FROM geo_state") if r[1]}


def _cached(url: str) -> bool:
    """Is this exact GET already in the data/raw cache (so re-loading needs no network at all)?"""
    import hashlib
    from ingest.common import RAW
    key = hashlib.sha1(("GET" + url + json.dumps({}, sort_keys=True)).encode()).hexdigest()
    p = RAW / DOMAIN / f"{key}.bin"
    return p.exists() and p.stat().st_size > 0


def run_item(conn, item: dict) -> dict:
    """Generate (or reuse) one download, load its prime-award CSV into award/org."""
    plan_key = item["key"]
    prev = conn.execute("SELECT status, file_url FROM award_places_plan WHERE plan_key=?",
                        (plan_key,)).fetchone()
    req = None
    if prev and prev["status"] == "finished" and prev["file_url"]:
        req = {"status_url": None, "file_url": prev["file_url"]}
    if req is None:
        req = request_download(item)
        wait_finished(req["status_url"])
    elif not _cached(req["file_url"]):        # cached plan, but the artifact is not on disk: re-poll
        wait_finished(STATUS_URL.format(req["file_url"].rsplit("/", 1)[-1]))
    src = source_id_for(
        conn, req["file_url"], DOMAIN,
        title=f"USASpending bulk award download ({item['family']}, {item['group_name']}): "
              f"place of performance + recipient location",
        publisher="US Department of the Treasury — USASpending.gov",
        license="US Government work, public domain")
    blob = fetch_zip(item, req)
    # source_id_for() cannot compute these for a POST-generated artifact file, so record the size
    # and digest of the exact ZIP that was parsed
    import hashlib as _hl
    conn.execute("UPDATE source SET bytes=COALESCE(bytes,?), sha256=COALESCE(sha256,?) WHERE id=?",
                 (len(blob), _hl.sha256(blob).hexdigest(), src))

    award_keys = {r[0] for r in conn.execute("SELECT award_key FROM award WHERE award_key IS NOT NULL")}
    org_of_award = {r[0]: r[1] for r in conn.execute("SELECT award_key, org_id FROM award")}
    fips2abbr = state_abbr_from_fips(conn)
    ts = now()

    award_upd, fill_rows, progress_rows = [], [], []
    recip_by_uei, recip_by_org = {}, {}
    rows_read = matched = in_archive = city_n = 0
    for rd in prime_csv_from_zip(blob):
        for row in rd:
            rows_read += 1
            key = clean(row.get("assistance_award_unique_key") or row.get("contract_award_unique_key"))
            if not key:
                continue
            pop_country = clean(row.get("primary_place_of_performance_country_code"))
            city = clean(row.get("primary_place_of_performance_city_name"))
            county = clean(row.get("primary_place_of_performance_county_name"))
            scope = clean(row.get("primary_place_of_performance_scope"))
            cfips = clean(row.get("prime_award_summary_place_of_performance_county_fips_code"))
            sfips = clean(row.get("prime_award_summary_place_of_performance_state_fips_code"))
            state = clean(row.get("primary_place_of_performance_state_code")) or fips2abbr.get(sfips or "")
            if pop_country and pop_country.upper() != "USA":
                city = county = state = None          # never put a foreign place in a US city page
            # recipient location (organisation address), only for US recipients
            rec_country = clean(row.get("recipient_country_code"))
            rec = None
            if rec_country and rec_country.upper() == "USA":
                rec = dict(name=clean(row.get("recipient_name")),
                           uei=clean(row.get("recipient_uei")),
                           address1=clean(row.get("recipient_address_line_1")),
                           city=clean(row.get("recipient_city_name")),
                           county=clean(row.get("recipient_county_name")),
                           state=clean(row.get("recipient_state_code")),
                           zip=zip5(row.get("recipient_zip_code") or row.get("recipient_zip_4_code")),
                           cfips=clean(row.get("prime_award_summary_recipient_county_fips_code")))
            matched += 1
            if key not in award_keys:
                continue                      # a row the archive does not hold (other award type)
            in_archive += 1
            progress_rows.append((key, 1, f"csv:{plan_key}", ts))
            if city:
                city_n += 1
            award_upd.append((city, county, state, key))
            fill_rows.append((key, clean(row.get("award_id_fain") or row.get("award_id_piid")),
                              plan_key, city, county, state, cfips, scope, pop_country,
                              _float(row.get("total_obligated_amount")), src, ts))
            if rec:
                if rec["uei"]:
                    recip_by_uei.setdefault(rec["uei"], rec)
                oid = org_of_award.get(key)
                if oid:
                    recip_by_org.setdefault(oid, rec)
            if rows_read % 20000 == 0:
                print(f"    parsed {rows_read} rows, {matched} matched, {city_n} with city", flush=True)

    # --- award: place of performance (never overwrite an existing value)
    conn.executemany(
        """UPDATE award SET place_city=COALESCE(place_city,?),
                            place_county=COALESCE(place_county,?),
                            place_state=COALESCE(place_state,?)
           WHERE award_key=?""", award_upd)
    conn.executemany(
        """INSERT INTO award_places_fill(award_key,award_id,plan_key,place_city,place_county,
                                         place_state,county_fips,pop_scope,pop_country,amount,source_id,ts)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(award_key) DO UPDATE SET
             award_id=COALESCE(excluded.award_id,award_places_fill.award_id),
             plan_key=excluded.plan_key,
             place_city=COALESCE(excluded.place_city,award_places_fill.place_city),
             place_county=COALESCE(excluded.place_county,award_places_fill.place_county),
             place_state=COALESCE(excluded.place_state,award_places_fill.place_state),
             county_fips=COALESCE(excluded.county_fips,award_places_fill.county_fips),
             pop_scope=COALESCE(excluded.pop_scope,award_places_fill.pop_scope),
             pop_country=COALESCE(excluded.pop_country,award_places_fill.pop_country),
             amount=COALESCE(excluded.amount,award_places_fill.amount),
             source_id=excluded.source_id, ts=excluded.ts""", fill_rows)
    conn.executemany(
        """INSERT INTO award_places_progress(award_key,ok,note,ts) VALUES(?,?,?,?)
           ON CONFLICT(award_key) DO UPDATE SET ok=excluded.ok, note=excluded.note, ts=excluded.ts""",
        progress_rows)

    # --- org: address from the recipient location, only where the row has none
    org_upd = []
    for oid, rec in recip_by_org.items():
        if rec["city"] or rec["state"] or rec["zip"] or rec["address1"]:
            org_upd.append((rec["city"], rec["county"], rec["state"], rec["zip"], rec["address1"], oid))
    conn.executemany(
        """UPDATE org SET city=COALESCE(city,?), county=COALESCE(county,?), state=COALESCE(state,?),
                          zip=COALESCE(zip,?), address1=COALESCE(address1,?), updated_at=?
           WHERE id=?""",
        [(a, b, c_, d, e, ts, f) for (a, b, c_, d, e, f) in org_upd])
    uei_upd = [(rec["city"], rec["county"], rec["state"], rec["zip"], rec["address1"], ts, uei)
               for uei, rec in recip_by_uei.items()
               if rec["city"] or rec["state"] or rec["zip"] or rec["address1"]]
    # which org rows the UEI pass is about to touch (recorded before the UPDATE, so a replay from
    # an already-filled archive records nothing new)
    uei_touch = []
    for uei, rec in recip_by_uei.items():
        if not (rec["city"] or rec["state"] or rec["zip"] or rec["address1"]):
            continue
        for r in conn.execute("""SELECT id FROM org WHERE uei=? AND uei IS NOT NULL AND uei<>''
                                 AND (state IS NULL OR state IN (SELECT abbr FROM geo_state))
                                 AND (city IS NULL OR state IS NULL OR zip IS NULL OR address1 IS NULL
                                      OR COALESCE(city,'')=COALESCE(?,'')
                                         AND COALESCE(zip,'')=COALESCE(?,''))""",
                              (uei, rec["city"], rec["zip"])):
            uei_touch.append((r[0], uei))
    conn.executemany(
        """UPDATE org SET city=COALESCE(city,?), county=COALESCE(county,?), state=COALESCE(state,?),
                          zip=COALESCE(zip,?), address1=COALESCE(address1,?), updated_at=?
           WHERE uei=? AND uei IS NOT NULL AND uei<>''
             AND (state IS NULL OR state IN (SELECT abbr FROM geo_state))
             AND (city IS NULL OR state IS NULL OR zip IS NULL OR address1 IS NULL)""", uei_upd)
    org_fill = [(oid, rec["uei"], plan_key, rec["city"], rec["county"], rec["state"], rec["zip"],
                 rec["address1"], src, ts) for oid, rec in recip_by_org.items()]
    # provenance for orgs reached only through recipient_uei (no award of ours points at them)
    have = {r[0] for r in conn.execute("SELECT org_id FROM award_places_org_fill")}
    for oid, uei in uei_touch:
        if oid in have:
            continue
        rec = recip_by_uei[uei]
        have.add(oid)
        org_fill.append((oid, uei, plan_key, rec["city"], rec["county"], rec["state"], rec["zip"],
                         rec["address1"], src, ts))
    conn.executemany(
        """INSERT INTO award_places_org_fill(org_id,uei,plan_key,city,county,state,zip,address1,source_id,ts)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(org_id) DO UPDATE SET plan_key=excluded.plan_key,
             city=COALESCE(award_places_org_fill.city,excluded.city),
             county=COALESCE(award_places_org_fill.county,excluded.county),
             state=COALESCE(award_places_org_fill.state,excluded.state),
             zip=COALESCE(award_places_org_fill.zip,excluded.zip),
             address1=COALESCE(award_places_org_fill.address1,excluded.address1),
             source_id=excluded.source_id, ts=excluded.ts""", org_fill)

    filled_city = conn.execute(
        "SELECT COUNT(*) FROM award WHERE place_city IS NOT NULL").fetchone()[0]
    conn.execute(
        """UPDATE award_places_plan SET status='finished', file_name=?, file_url=?, rows_read=?,
             rows_matched=?, awards_city=?, orgs_filled=?, note=?, updated_at=? WHERE plan_key=?""",
        (req["file_url"].rsplit("/", 1)[-1], req["file_url"], rows_read, in_archive, filled_city,
         len(org_upd), "", ts, plan_key))
    conn.commit()
    print(f"  [{plan_key}] rows={rows_read} matched_in_archive={in_archive} with_city={city_n} "
          f"orgs_touched={len(org_upd)}", flush=True)
    return dict(rows_read=rows_read, matched=in_archive, city=city_n, orgs=len(org_upd),
                awards_city_total=filled_city, src=src, file_url=req["file_url"])


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def mark_failed(conn, plan_key: str, note: str):
    conn.execute("""INSERT INTO award_places_plan(plan_key,status,note,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(plan_key) DO UPDATE SET status='failed', note=excluded.note,
                    updated_at=excluded.updated_at""", (plan_key, "failed", note[:500], now()))
    conn.commit()


def drop_foreign_places(conn) -> int:
    """Reconcile non-US places of performance out of the city columns.

    One award (CONT_AWD_W912PF05C0003_9700_-NONE-_-NONE-, Vicenza, IT) reports pop_country=ITA in
    one download and a blank country in another; whichever plan loaded it first kept the city. The
    plan that names a non-US country is the stronger evidence, so no foreign place ends up on a US
    city page.
    """
    keys = [r[0] for r in conn.execute(
        """SELECT award_key FROM award_places_fill
           WHERE pop_country IS NOT NULL AND UPPER(pop_country)<>'USA'""")]
    if not keys:
        return 0
    conn.executemany("UPDATE award SET place_city=NULL, place_county=NULL WHERE award_key=?",
                     [(k,) for k in keys])
    conn.executemany("UPDATE award_places_fill SET place_city=NULL, place_county=NULL WHERE award_key=?",
                     [(k,) for k in keys])
    conn.commit()
    return len(keys)


def drop_foreign_org_address(conn) -> int:
    """Foreign awardees must not get a US street address.

    USASpending mints placeholder recipient UEIs shared by many unrelated foreign awardees
    (`MISCELLANEOUS FOREIGN AWARDEES` appears once per country), so a UEI match can hand one row a
    US ZIP/county/street from a different awardee. An `org` whose `state` is not a USPS abbreviation
    is foreign: drop the county / ZIP / street this domain added and its provenance row.
    """
    ids = [r[0] for r in conn.execute(
        """SELECT o.id FROM org o JOIN award_places_org_fill f ON f.org_id=o.id
           WHERE o.state IS NOT NULL AND o.state NOT IN (SELECT abbr FROM geo_state)
             AND (o.county IS NOT NULL OR o.zip IS NOT NULL OR o.address1 IS NOT NULL)""")]
    for i in ids:
        conn.execute("UPDATE org SET county=NULL, zip=NULL, address1=NULL WHERE id=?", (i,))
        conn.execute("DELETE FROM award_places_org_fill WHERE org_id=?", (i,))
    conn.commit()
    return len(ids)


def quick_check(conn, plan_key):
    chk = conn.execute("PRAGMA quick_check").fetchone()[0]
    if chk != "ok":
        raise RuntimeError(f"database integrity check failed after {plan_key}: {chk}")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run(conn, cfg) -> dict:
    cfg = cfg or {}
    selftest = bool(cfg.get("selftest"))
    for ddl in (PLAN_DDL, PROGRESS_DDL, FILL_DDL, ORG_FILL_DDL):
        conn.execute(ddl)
    conn.commit()

    items = probe_items() if selftest else plan_items()
    for it in items:
        n = None if selftest else plan_count(it["filters"])
        conn.execute("""INSERT INTO award_places_plan(plan_key,group_name,family,filters,n_hint,status,updated_at)
                        VALUES(?,?,?,?,?,COALESCE((SELECT status FROM award_places_plan WHERE plan_key=?),'pending'),?)
                        ON CONFLICT(plan_key) DO UPDATE SET filters=excluded.filters,
                          n_hint=COALESCE(excluded.n_hint,award_places_plan.n_hint)""",
                     (it["key"], it["group_name"], it["family"], json.dumps(it["filters"]),
                      n, it["key"], now()))
    conn.commit()

    total = dict(rows=0, matched=0, city=0, orgs=0)
    done_items = []
    reload = bool(cfg.get("reload"))
    for it in items:
        st = conn.execute("SELECT status, n_hint FROM award_places_plan WHERE plan_key=?",
                          (it["key"],)).fetchone()
        if st and st["status"] == "finished" and not reload:
            done_items.append((it, st["n_hint"]))
            continue
        try:
            r = run_item(conn, it)
        except Exception as e:  # noqa: BLE001
            mark_failed(conn, it["key"], repr(e))
            print(f"  [{it['key']}] FAILED {e!r}"[:300], flush=True)
            done_items.append((it, st["n_hint"] if st else None))
            continue
        quick_check(conn, it["key"])
        total["rows"] += r["rows_read"]
        total["matched"] += r["matched"]
        total["city"] += r["city"]
        total["orgs"] += r["orgs"]
        done_items.append((it, st["n_hint"] if st else None))

    # --- fallback: a merged download that came up short gets re-run per fiscal year
    if not selftest:
        for it, n_hint in done_items:
            got = conn.execute("SELECT status, rows_read FROM award_places_plan WHERE plan_key=?",
                               (it["key"],)).fetchone()
            if not n_hint or not got:
                continue
            short = got["status"] != "finished" or (got["rows_read"] or 0) < SHORTFALL * n_hint
            if not short:
                continue
            print(f"  [{it['key']}] returned {got['rows_read']} of {n_hint} → per-FY fallback",
                  flush=True)
            for sub in fy_items(it):
                ss = conn.execute("SELECT status FROM award_places_plan WHERE plan_key=?",
                                  (sub["key"],)).fetchone()
                if ss and ss["status"] == "finished":
                    continue
                try:
                    r = run_item(conn, sub)
                    quick_check(conn, sub["key"])
                    total["rows"] += r["rows_read"]
                    total["matched"] += r["matched"]
                    total["city"] += r["city"]
                    total["orgs"] += r["orgs"]
                except Exception as e:  # noqa: BLE001
                    mark_failed(conn, sub["key"], repr(e))
                    print(f"  [{sub['key']}] FAILED {e!r}"[:300], flush=True)

    foreign_dropped = drop_foreign_places(conn)
    foreign_orgs = drop_foreign_org_address(conn)
    n_award = conn.execute("SELECT COUNT(*) FROM award").fetchone()[0]
    n_city = conn.execute("SELECT COUNT(*) FROM award WHERE place_city IS NOT NULL").fetchone()[0]
    n_county = conn.execute("SELECT COUNT(*) FROM award WHERE place_county IS NOT NULL").fetchone()[0]
    n_org_addr = conn.execute("""SELECT COUNT(*) FROM org
                                 WHERE city IS NOT NULL AND state IS NOT NULL""").fetchone()[0]
    n_org_zip = conn.execute("SELECT COUNT(*) FROM org WHERE zip IS NOT NULL").fetchone()[0]
    pct = 100.0 * n_city / n_award if n_award else 0.0
    notes = (f"place_city {n_city}/{n_award} ({pct:.1f}%) place_county {n_county} "
             f"orgs_with_city+state {n_org_addr} orgs_with_zip {n_org_zip} "
             f"plans={len(items)} matched={total['matched']} foreign_cleared={foreign_dropped} "
             f"foreign_orgs_cleared={foreign_orgs}")
    return {"rows": total["matched"], "notes": notes,
            "sources": [(DOWNLOAD_URL, "public domain")]}


def selftest():
    conn = db()
    res = run(conn, {"selftest": True, "reload": True})
    print(f"[{DOMAIN}] selftest rows={res['rows']} {res['notes']}")
    print("sample award rows just filled:")
    for r in conn.execute("""SELECT a.award_key, a.award_type, a.place_state, a.place_city,
                                    a.place_county, f.county_fips, f.amount
                             FROM award_places_fill f JOIN award a ON a.award_key=f.award_key
                             ORDER BY RANDOM() LIMIT 3"""):
        print(" ", dict(r))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--reload", action="store_true",
                    help="re-load every finished plan from the cached ZIP (no network); idempotent")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        conn = db()
        print(json.dumps(run(conn, {"reload": a.reload}), indent=1, default=str))
