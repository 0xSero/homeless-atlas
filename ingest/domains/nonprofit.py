"""nonprofit — every US nonprofit that works on homelessness.

Sources (all keyless):
  * IRS Exempt Organizations Business Master File (EO BMF) CSV splits eo1..eo4
    -> the organisation universe: name, address, city, state, zip, NTEE code,
       subsection, ruling date, BMF status.
  * ProPublica Nonprofit Explorer API v2 search
       https://projects.propublica.org/nonprofits/api/v2/search.json?q=...&page=N
    -> name-keyword sweeps (the `state[id]` and `ntee[id]` filters are inert /
       broken on the live v2 API, see research/nonprofit.md).
  * ProPublica Nonprofit Explorer filing page
       https://projects.propublica.org/nonprofits/organizations/{ein}
    -> per-fiscal-year extracted financials (Total revenue / expenses /
       assets / net assets) and the Form 990 Part VII Section A table
       (officers, directors, key employees: name, title, reportable comp).
       The matching ``download-xml?object_id=`` endpoint is behind a bot wall
       (HTTP 403 "Security Check"), so the XML is of no use here; the filing
       page renders the same Part I / Part VII values.
  * Wikidata (P1297 EIN -> P856 official website, P1329 phone) for a minority
    of orgs, exact-key join only, gap-fill only.

Phases (each resumable through the nonprofit_progress table):
  bmf -> search -> pages (financials + people) -> wikidata

Reads/writes only: org, org_person, source, ingest_run, nonprofit_* tables.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse

from ingest.common import (clean, db, fetch, main, name_norm, now, save_raw,
                           selftest_report, source_id_for, to_int, upsert)

DOMAIN = "nonprofit"

BMF_URLS = [f"https://www.irs.gov/pub/irs-soi/eo{i}.csv" for i in (1, 2, 3, 4)]
PP_BASE = "https://projects.propublica.org/nonprofits"
SEARCH_URL = PP_BASE + "/api/v2/search.json"
ORG_URL = PP_BASE + "/organizations/{ein}"
WDQS = "https://query.wikidata.org/sparql"

# --- universe definition -------------------------------------------------
# IRS NTEE codes that *are* homelessness / temporary shelter services.
# L41 = homeless shelters/temporary housing, L40 = temporary housing,
# P85 = homeless persons centres & services.
HOMELESS_NTEE3 = {"L41", "L40", "P85"}
# Animal shelters are also named "... Shelter"; D* is the animal NTEE group.
EXCLUDE_NTEE1 = {"D"}
# Terms that unambiguously mean homelessness work -> accepted on the name alone.
STRICT_NAME_RE = re.compile(
    r"\bHOMELESS|\bUNHOUSED|\bRE-?HOUS\w*|\bHOUSING FIRST|\bSUPPORTIVE\s+HOUS\w*|"
    r"\bCONTINUUM OF CARE|\bSTREET OUTREACH|\bRESCUE MISSION|\bWARMING CENT\w*|"
    r"\bBRIDGE HOUS\w*|\bEMERGENCY HOUS\w*|\bHOMELESS PREVENTION|\bHMIS\b|"
    r"\bTRANSITIONAL\s+HOUS\w*|\bTRANSITIONAL\s+LIVING",
    re.I,
)
# "... Shelter" / "Safe Haven" are ambiguous (animal shelters, insurers, DV and
# mental-health programs) -> only with a human-services / housing-ish NTEE or
# no NTEE at all.
WEAK_NAME_RE = re.compile(r"\bSHELTER|\bSAFE HAVEN", re.I)
# NTEE major groups where "shelter"/"safe haven" names are plausible
WEAK_NTEE_ALLOW = {"", "L", "P", "X", "O", "S", "K", "F"}
NONHOMELESS_NAME_RE = re.compile(
    r"ANIMAL|HUMANE|PAWS|WILDLIFE|VETERINAR|SPAY|NEUTER|AVIAN|BIRD|KITT|K9|"
    r"PET |PETS\b|EQUINE|HORSE|FELINE|CANINE|WILDLIFE|AQUARIUM|ZOO\b",
    re.I,
)

# 12 required sweeps first, then documented variants.
QUERIES = [
    "homeless",
    "homelessness",
    "continuum of care",
    "shelter",
    "housing first",
    "transitional housing",
    "rapid rehousing",
    "supportive housing",
    "food and shelter",
    "street outreach",
    "HMIS",
    "homeless shelter",
    "emergency shelter",
    "emergency housing",
    "safe haven",
    "rescue mission",
    "warming center",
    "day center",
    "soup kitchen",
    "unhoused",
    "homeless prevention",
    "bridge housing",
    "permanent supportive housing",
]

# org columns this domain may write
ORG_COLS = [
    "name", "name_norm", "ein", "org_type", "website", "email", "phone", "address1",
    "city", "state", "zip", "county", "ntee", "founded", "status", "revenue",
    "expenses", "assets", "employees", "fiscal_year", "is_est", "source_id", "updated_at",
]
FIN_COLS = {"revenue", "expenses", "assets", "employees", "fiscal_year"}
IDENTITY_COLS = {"name", "name_norm", "state", "ein"}

_ROLE_OFFICER = re.compile(
    r"\b(CHIEF|CEO|CFO|COO|CAO|CTO|CIO|PRESIDENT|VICE\s*PRES\w*|SECRETARY|TREASURER|"
    r"COMPTROLLER|CONTROLLER)\b|EXEC(UTIVE)?\.?\s*DIRECTOR|\bOFFICER\b",
    re.I,
)
_ROLE_BOARD = re.compile(r"TRUSTEE|BOARD|CHAIR", re.I)
_ROLE_DIRECTOR_EXACT = re.compile(r"^DIRECTOR\b(?!\s+OF)", re.I)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _retry(fn, *a, **kw):
    """Retry a DB write while another worker holds the write lock.

    Other domain workers keep long transactions open while they talk to their
    APIs; ``conn`` already has a 120 s busy timeout, this adds a longer, jittered
    wait instead of aborting the whole run.
    """
    delay = 1.0
    for _ in range(20):
        try:
            return fn(*a, **kw)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            time.sleep(delay)
            delay = min(delay * 1.7, 20.0)
    return fn(*a, **kw)


def _commit(conn):
    _retry(conn.commit)


def _sid(conn, url, blob=None, title=None, publisher=None, license=None):
    """source.id for a URL, plus real bytes/sha256 of the fetched body."""
    sid = _retry(source_id_for, conn, url, DOMAIN, title=title, publisher=publisher,
                 license=license)
    if blob:
        _retry(conn.execute,
               "UPDATE source SET bytes=?, sha256=?, retrieved_at=? WHERE id=?",
               (len(blob), hashlib.sha256(blob).hexdigest(), now(), sid))
    return sid


def _money(s):
    """'$1,003,736' -> 1003736 ; '($1,234)' -> -1234 ; '--' -> None."""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in ("-", "--", "—"):
        return None
    neg = s.startswith("(") or s.startswith("-$")
    v = to_int(s)
    if v is None:
        return None
    return -abs(v) if neg else v


def _ein9(v) -> str | None:
    if v is None:
        return None
    d = re.sub(r"[^0-9]", "", str(v))
    if not d:
        return None
    return d.zfill(9)


def _ein_fmt(ein: str) -> str:
    return f"{ein[:2]}-{ein[2:]}" if ein and len(ein) == 9 else ein


def person_norm(s: str | None) -> str | None:
    if not s:
        return None
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\b(JR|SR|II|III|IV|V|MD|PHD|CPA|ESQ)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def role_class(title: str | None) -> str | None:
    """Classify a published Form 990 Part VII title. Heuristic, never invents."""
    if not title:
        return None
    t = title.strip()
    if not t:
        return None
    if _ROLE_OFFICER.search(t):
        return "officer"
    if _ROLE_BOARD.search(t):
        return "director"
    if _ROLE_DIRECTOR_EXACT.search(t):
        return "director"
    return "key_employee"


def _is_homeless_org(name, ntee) -> bool:
    code = (ntee or "").strip().upper()
    if code[:1] in EXCLUDE_NTEE1:
        return False
    if code[:3] in HOMELESS_NTEE3:
        return True
    if NONHOMELESS_NAME_RE.search(name or ""):
        return False
    if STRICT_NAME_RE.search(name or ""):
        return True
    if WEAK_NAME_RE.search(name or ""):
        return code[:1] in WEAK_NTEE_ALLOW
    return False


# --------------------------------------------------------------------------
# tables + progress
# --------------------------------------------------------------------------
def _ensure_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nonprofit_candidate (
          ein         TEXT PRIMARY KEY,
          name        TEXT,
          city        TEXT,
          state       TEXT,
          zip         TEXT,
          address1    TEXT,
          ntee        TEXT,
          subsection  TEXT,
          ruling      TEXT,
          bmf_status  TEXT,
          bmf_income  INTEGER,
          src         TEXT,
          seen_at     TEXT,
          page_done   INTEGER DEFAULT 0,
          page_ts     TEXT,
          page_note   TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_np_cand_done ON nonprofit_candidate(page_done, bmf_income);
        CREATE TABLE IF NOT EXISTS nonprofit_progress (
          phase TEXT NOT NULL,
          key   TEXT NOT NULL,
          done  INTEGER DEFAULT 0,
          ts    TEXT,
          note  TEXT,
          PRIMARY KEY(phase, key)
        );
        """
    )
    _commit(conn)


def _p_done(conn, phase, key) -> bool:
    r = conn.execute(
        "SELECT done FROM nonprofit_progress WHERE phase=? AND key=?", (phase, key)
    ).fetchone()
    return bool(r and r[0])


def _p_mark(conn, phase, key, note="", done=1):
    _retry(_p_mark_1, conn, phase, key, note, done)


def _p_mark_1(conn, phase, key, note="", done=1):
    conn.execute(
        """INSERT INTO nonprofit_progress(phase,key,done,ts,note) VALUES(?,?,?,?,?)
           ON CONFLICT(phase,key) DO UPDATE SET done=excluded.done, ts=excluded.ts,
             note=COALESCE(excluded.note, nonprofit_progress.note)""",
        (phase, key, done, now(), note),
    )


def _cand_upsert(conn, row: dict):
    _retry(_cand_upsert_1, conn, row)


def _cand_upsert_1(conn, row: dict):
    row = {k: v for k, v in row.items() if v is not None}
    if not row.get("ein"):
        return
    row.setdefault("seen_at", now())
    cols = list(row)
    sets = ",".join(f"{c}=COALESCE(excluded.{c},{'nonprofit_candidate'}.{c})" for c in cols if c != "ein")
    conn.execute(
        f"INSERT INTO nonprofit_candidate({','.join(cols)}) VALUES({','.join('?'*len(cols))}) "
        f"ON CONFLICT(ein) DO UPDATE SET {sets or 'ein=ein'}",
        [row[c] for c in cols],
    )


# --------------------------------------------------------------------------
# org merge (never clobber non-null, never write NULL over a value)
# --------------------------------------------------------------------------
def _merge_org(conn, row: dict) -> tuple[int, bool]:
    row = {k: v for k, v in row.items() if k in ORG_COLS and v is not None}
    row["updated_at"] = now()
    ein = row.get("ein")
    nn = row.get("name_norm")
    st = row.get("state")

    cur = None
    if ein:
        cur = conn.execute("SELECT * FROM org WHERE ein=?", (ein,)).fetchone()
    if cur is None and nn:
        cur = conn.execute(
            "SELECT * FROM org WHERE name_norm=? AND state IS ?", (nn, st)
        ).fetchone()

    if cur is None:
        if not row.get("name"):
            row["name"] = nn or ein or "UNKNOWN"
        cols = list(row)
        try:
            cur_id = conn.execute(
                f"INSERT INTO org({','.join(cols)}) VALUES({','.join('?'*len(cols))})",
                [row[c] for c in cols],
            ).lastrowid
            return int(cur_id), True
        except Exception:
            # another worker created the same (name_norm,state) row in between
            cur = conn.execute(
                "SELECT * FROM org WHERE name_norm=? AND state IS ?", (nn, st)
            ).fetchone()
            if cur is None:
                raise

    newfy = row.get("fiscal_year")
    oldfy = cur["fiscal_year"]
    sets, vals = [], []
    for c, v in row.items():
        if c == "id" or v is None:
            continue
        old = cur[c] if c in cur.keys() else None
        if c in FIN_COLS:
            # only move financials forward in time
            if newfy is None or oldfy is None or int(newfy) >= int(oldfy):
                if c == "fiscal_year" and old is not None and int(v) < int(old):
                    continue
                sets.append(f"{c}=?"); vals.append(v)
        elif c in IDENTITY_COLS:
            if old is None:  # never rename / re-key another domain's row
                sets.append(f"{c}=?"); vals.append(v)
        elif old is None:
            sets.append(f"{c}=?"); vals.append(v)
    if sets:
        sets.append("updated_at=?")
        vals.append(now())
        vals.append(cur["id"])
        conn.execute(f"UPDATE org SET {','.join(sets)} WHERE id=?", vals)
        return int(cur["id"]), False
    return int(cur["id"]), False


def _add_person(conn, org_id, org_name, person, title, comp, year, role, sid):
    pn = person_norm(person)
    if not pn or not org_name:
        return 0
    row = conn.execute(
        """SELECT id FROM org_person
           WHERE person_norm=? AND org_name=? AND year IS ? AND title IS ?""",
        (pn, org_name, year, title),
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE org_person SET comp=COALESCE(comp,?), org_id=COALESCE(org_id,?),
                   role_class=COALESCE(role_class,?), source_id=COALESCE(source_id,?)
               WHERE id=?""",
            (comp, org_id, role, sid, row["id"]),
        )
        return 0
    conn.execute(
        """INSERT INTO org_person(org_id,org_name,person,person_norm,title,comp,year,role_class,source_id)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (org_id, org_name, clean(person), pn, clean(title), comp, year, role, sid),
    )
    return 1


# --------------------------------------------------------------------------
# phase 1: IRS EO BMF -> the universe
# --------------------------------------------------------------------------
def phase_bmf(conn, cfg):
    if _p_done(conn, "bmf", "all"):
        return 0
    n = 0
    for url in BMF_URLS:
        blob = fetch(url, domain=DOMAIN)
        sid = _sid(conn, url, blob, title=f"IRS Exempt Organizations Business Master File ({url.rsplit('/',1)[-1]})",
                   publisher="Internal Revenue Service", license="public domain (US federal government)")
        rr = csv.DictReader(io.StringIO(blob.decode("latin-1")))
        for r in rr:
            name = clean(r.get("NAME"))
            if not name:
                continue
            ntee = clean(r.get("NTEE_CD"))
            if not _is_homeless_org(name, ntee):
                continue
            ein = _ein9(r.get("EIN"))
            if not ein:
                continue
            state = clean(r.get("STATE"))
            try:
                inc = int(r.get("INCOME_AMT") or 0)
            except ValueError:
                inc = 0
            _cand_upsert(conn, {
                "ein": ein, "name": name, "city": clean(r.get("CITY")), "state": state,
                "zip": clean(r.get("ZIP")), "address1": clean(r.get("STREET")),
                "ntee": ntee, "subsection": clean(r.get("SUBSECTION")),
                "ruling": clean(r.get("RULING")), "bmf_status": clean(r.get("STATUS")),
                "bmf_income": inc, "src": "bmf",
            })
            n += 1
            if n % 500 == 0:
                _commit(conn)
        _p_mark(conn, "bmf", url.rsplit("/", 1)[-1], f"sid={sid}")
        _commit(conn)
    _p_mark(conn, "bmf", "all", f"{n} homeless-relevant BMF rows")
    _commit(conn)
    return n


# --------------------------------------------------------------------------
# phase 2: ProPublica search sweeps
# --------------------------------------------------------------------------
def phase_search(conn, cfg):
    queries = cfg.get("queries") or QUERIES
    max_pages_per_q = int(cfg.get("search_pages_per_query", 0)) or None
    added = 0
    pages = 0
    for q in queries:
        # page 0 always (cached after first run) so we learn num_pages
        first_url = SEARCH_URL + "?" + urllib.parse.urlencode({"q": q, "page": 0})
        try:
            first = json.loads(fetch(first_url, domain=DOMAIN))
        except Exception as e:  # source down / 404
            _p_mark(conn, "search", f"{q}|0", f"ERR {e}"[:300])
            _commit(conn)
            continue
        npages = int(first.get("num_pages") or 0)
        if max_pages_per_q:
            npages = min(npages, max_pages_per_q)
        sid = _sid(conn, first_url, None, title=f"ProPublica Nonprofit Explorer search q={q!r}",
                   publisher="ProPublica", license="public data (IRS BMF + Form 990)")
        _p_mark(conn, "search", f"{q}|0", f"sid={sid}")
        for page in range(npages):
            key = f"{q}|{page}"
            if _p_done(conn, "search", key) and page > 0:
                continue
            if page == 0:
                d = first
            else:
                url = SEARCH_URL + "?" + urllib.parse.urlencode({"q": q, "page": page})
                try:
                    d = json.loads(fetch(url, domain=DOMAIN))
                except Exception as e:
                    _p_mark(conn, "search", key, f"ERR {e}"[:300])
                    _commit(conn)
                    break
            pages += 1
            for o in d.get("organizations") or []:
                name = clean(o.get("name"))
                ntee = clean(o.get("ntee_code"))
                ein = _ein9(o.get("ein"))
                if not name or not ein or not _is_homeless_org(name, ntee):
                    continue
                _cand_upsert(conn, {
                    "ein": ein, "name": name, "city": clean(o.get("city")),
                    "state": clean(o.get("state")), "ntee": ntee,
                    "subsection": str(o.get("subseccd")) if o.get("subseccd") is not None else None,
                    "src": "search",
                })
                added += 1
            _p_mark(conn, "search", key, f"n={len(d.get('organizations') or [])}")
        _commit(conn)
    _commit(conn)
    return added, pages


# --------------------------------------------------------------------------
# phase 3: materialise org rows from candidates (breadth first, no network)
# --------------------------------------------------------------------------
def phase_orgs(conn, cfg):
    rows = conn.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM org o WHERE o.ein=c.ein) AS has_org
           FROM nonprofit_candidate c"""
    ).fetchall()
    n_new = 0
    for c in rows:
        ntee = c["ntee"]
        row = {
            "name": c["name"], "name_norm": name_norm(c["name"]), "ein": c["ein"],
            "org_type": "nonprofit", "address1": c["address1"], "city": c["city"],
            "state": c["state"], "zip": c["zip"], "ntee": ntee,
            "status": "active" if (c["bmf_status"] or "") == "01" else None,
            "source_id": None,
        }
        if not row["name_norm"]:
            continue
        url = (BMF_URLS[0] if c["src"] == "bmf" else
               SEARCH_URL + "?" + urllib.parse.urlencode({"q": "homeless", "page": 0}))
        row["source_id"] = _sid(conn, url, None, publisher="IRS/ProPublica")
        _, created = _merge_org(conn, row)
        n_new += 1 if created else 0
    _commit(conn)
    return n_new


# --------------------------------------------------------------------------
# phase 4: filing pages -> financials + people
# --------------------------------------------------------------------------
def _parse_filing_page(blob):
    """Extract org metadata, per-year financials and Part VII people from a
    ProPublica filing page. Tolerant: every field may be None."""
    from bs4 import BeautifulSoup

    out = {"meta": {}, "filings": [], "people": []}
    soup = BeautifulSoup(blob, "lxml")

    h1 = soup.select_one("h1")
    if h1:
        out["meta"]["name"] = clean(h1.get_text(" ", strip=True))
    meta = soup.select_one(".basic-org-metadata")
    if meta:
        txt = clean(meta.get_text(" ", strip=True)) or ""
        m = re.search(r"EIN:\s*([0-9\-]+)", txt)
        if m:
            out["meta"]["ein"] = _ein9(m.group(1))
        m = re.match(r"^(.+?),\s*([A-Z]{2})\b", txt)
        if m:
            out["meta"]["city"] = clean(m.group(1))
            out["meta"]["state"] = clean(m.group(2))
        m = re.search(r"Tax exemption issued:\s*([A-Za-z]+\.?)\s*(\d{4})", txt)
        if m:
            out["meta"]["ruling_year"] = int(m.group(2))

    # JSON-LD dataset of the newest filing carries address/zip/NTEE
    for sc in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            d = json.loads(sc.string or "{}")
        except Exception:
            continue
        tbl = (d.get("mainEntity") or {}).get("csvw:tableSchema") or {}
        for col in tbl.get("csvw:columns") or []:
            nm = (col.get("csvw:name") or "").strip()
            cells = col.get("csvw:cells") or [{}]
            val = cells[0].get("csvw:value")
            if val in (None, ""):
                continue
            if nm == "Organization address":
                out["meta"].setdefault("address1", clean(val))
            elif nm == "Organization city":
                out["meta"].setdefault("city", clean(val))
            elif nm == "Organization state":
                out["meta"].setdefault("state", clean(val))
            elif nm == "Organization zip code":
                out["meta"].setdefault("zip", clean(val))
            elif nm == "Organization NTEE Code":
                out["meta"].setdefault("ntee", clean(val))

    for sec in soup.select("section.single-filing-period"):
        yl = sec.select_one(".year-label")
        year = None
        if yl:
            t = (yl.get_text(strip=True) or "").strip()
            if t.isdigit():
                year = int(t)
        if year is None:
            continue
        f = {"year": year}
        rev = sec.select_one(".row-revenue__number")
        if rev:
            f["revenue"] = _money(rev.get_text(strip=True))
        for item in sec.select(".row-summary__item"):
            hed = item.select_one(".row-summary__hed")
            num = item.select_one(".row-summary__number")
            if not hed or not num:
                continue
            label = clean(hed.get_text(" ", strip=True)) or ""
            val = _money(num.get_text(strip=True))
            if label.lower() == "expenses":
                f["expenses"] = val
            elif label.lower() == "net assets":
                f["net_assets"] = val
            elif label.lower() == "net income":
                f["net_income"] = val
        # details table ("Total Assets", "Total Liabilities", revenue lines)
        for tr in sec.select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            label = clean(tds[0].get_text(" ", strip=True)) or ""
            lv = label.lower()
            if lv == "total assets":
                f["assets"] = _money(tds[1].get_text(strip=True))
            elif lv == "total liabilities":
                f["liabilities"] = _money(tds[1].get_text(strip=True))
        npeople = 0
        for tr in sec.select("tr.employee-row"):
            tds = tr.find_all("td")
            if not tds:
                continue
            cell = tds[0]
            span = cell.find("span")
            title = None
            if span:
                title = clean(span.get_text(" ", strip=True))
                if title:
                    title = title.strip()
                    if title.startswith("(") and title.endswith(")"):
                        title = title[1:-1].strip()
            span.extract() if span else None
            name = clean(cell.get_text(" ", strip=True))
            if name:
                name = re.sub(r"\s*[-–]\s*$", "", name).strip()
            comp = _money(tds[1].get_text(strip=True)) if len(tds) > 1 else None
            if not name or len(name) < 3:
                continue
            out["people"].append({"year": year, "person": name, "title": title, "comp": comp})
            npeople += 1
        f["n_people"] = npeople
        if any(k in f for k in ("revenue", "expenses", "assets", "net_assets")) or npeople:
            out["filings"].append(f)
    return out


def _write_page(conn, cand, parsed, sid, keep_years):
    """All DB writes for one filing page (retried as a unit on lock contention)."""
    ein = cand["ein"]
    m = parsed["meta"]
    name = m.get("name") or cand["name"]
    org_row = {
        "name": name, "name_norm": name_norm(name), "ein": ein, "org_type": "nonprofit",
        "address1": m.get("address1") or cand["address1"],
        "city": m.get("city") or cand["city"], "state": m.get("state") or cand["state"],
        "zip": m.get("zip") or cand["zip"], "ntee": m.get("ntee") or cand["ntee"],
        "status": "active" if (cand["bmf_status"] or "") == "01" else None,
        "source_id": sid,
    }
    filings = [f for f in parsed["filings"] if f.get("revenue") is not None]
    if filings:
        lat = max(filings, key=lambda f: f["year"])
        org_row["revenue"] = lat.get("revenue")
        org_row["expenses"] = lat.get("expenses")
        org_row["assets"] = lat.get("assets") if lat.get("assets") is not None else lat.get("net_assets")
        org_row["fiscal_year"] = lat["year"]
    org_id, _ = _merge_org(conn, org_row)
    canonical_name = conn.execute("SELECT name FROM org WHERE id=?", (org_id,)).fetchone()["name"]

    years_with_people = sorted({p["year"] for p in parsed["people"]}, reverse=True)[:keep_years]
    keepset = set(years_with_people)
    added = 0
    for p in parsed["people"]:
        if p["year"] not in keepset:
            continue
        added += _add_person(conn, org_id, canonical_name, p["person"], p["title"],
                             p["comp"], p["year"], role_class(p["title"]), sid)
    if not parsed["filings"] and not parsed["people"]:
        note, empty = "no-form-990-data (pdf-only or no filings)", True
    else:
        note, empty = f"filings={len(parsed['filings'])} people={len(parsed['people'])}", False
    conn.execute(
        "UPDATE nonprofit_candidate SET page_done=1, page_ts=?, page_note=? WHERE ein=?",
        (now(), note[:200], ein),
    )
    return added, empty


def phase_pages(conn, cfg):
    max_pages = int(cfg.get("max_pages", os.environ.get("NONPROFIT_MAX_PAGES", "2000")))
    budget = float(cfg.get("budget_seconds", os.environ.get("NONPROFIT_BUDGET", "1700")))
    keep_years = int(cfg.get("person_years", os.environ.get("NONPROFIT_PERSON_YEARS", "3")))
    t0 = time.time()
    done = persons = 0
    empty = 0
    errors = 0
    rows = conn.execute(
        """SELECT * FROM nonprofit_candidate WHERE page_done=0
           ORDER BY COALESCE(bmf_income,0) DESC, ein"""
    ).fetchall()
    for c in rows:
        if done >= max_pages or (time.time() - t0) > budget:
            break
        ein = c["ein"]
        url = ORG_URL.format(ein=ein)
        try:
            blob = fetch(url, domain=DOMAIN)
        except Exception as e:
            note = str(e)[:200]
            _retry(conn.execute,
                   "UPDATE nonprofit_candidate SET page_done=2, page_ts=?, page_note=? WHERE ein=?",
                   (now(), note, ein))
            _commit(conn)
            errors += 1
            continue
        try:
            parsed = _parse_filing_page(blob)
        except Exception as e:
            _retry(conn.execute,
                   "UPDATE nonprofit_candidate SET page_done=3, page_ts=?, page_note=? WHERE ein=?",
                   (now(), f"parse {e}"[:200], ein))
            _commit(conn)
            errors += 1
            continue
        sid = _sid(conn, url, blob, title=f"Form 990 filing summary for EIN {ein}",
                   publisher="ProPublica Nonprofit Explorer / IRS",
                   license="public data (IRS Form 990)")
        added, was_empty = _retry(_write_page, conn, c, parsed, sid, keep_years)
        _commit(conn)
        persons += added
        empty += 1 if was_empty else 0
        done += 1
        if done % 25 == 0:
            print(f"[{DOMAIN}] pages={done} persons={persons} "
                  f"({round(time.time()-t0)}s)", flush=True)
    _commit(conn)
    return {"pages": done, "persons": persons, "empty": empty, "errors": errors,
            "seconds": round(time.time() - t0, 1)}


# --------------------------------------------------------------------------
# phase 5: Wikidata gap-fill (website / phone, exact EIN join)
# --------------------------------------------------------------------------
def phase_wikidata(conn, cfg):
    if cfg.get("wiki") is False or os.environ.get("NONPROFIT_NO_WIKI"):
        return 0
    batch = 120
    rows = conn.execute(
        """SELECT c.ein, o.name FROM nonprofit_candidate c
           JOIN org o ON o.ein=c.ein
           WHERE o.website IS NULL AND length(c.ein)=9"""
    ).fetchall()
    todo = []
    for r in rows:
        key = f"wd|{r['ein']}"
        if _p_done(conn, "wikidata", key):
            continue
        todo.append(r)
    filled = 0
    for i in range(0, min(len(todo), batch * 60), batch):
        chunk = todo[i:i + batch]
        vals = " ".join(f'"{_ein_fmt(r["ein"])}"' for r in chunk)
        q = ("SELECT ?ein (SAMPLE(?site) AS ?s) (SAMPLE(?ph) AS ?p) WHERE { VALUES ?ein { "
             + vals + " } ?org wdt:P1297 ?ein . OPTIONAL { ?org wdt:P856 ?site } "
             "OPTIONAL { ?org wdt:P1329 ?ph } } GROUP BY ?ein")
        try:
            blob = fetch(WDQS, method="POST",
                         data=urllib.parse.urlencode({"query": q, "format": "json"}),
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  "Accept": "application/sparql-results+json"},
                         domain=DOMAIN, timeout=180)
            d = json.loads(blob)
        except Exception as e:
            print(f"[{DOMAIN}] wikidata batch failed: {e}"[:200])
            break
        sid = _sid(conn, WDQS, None, title="Wikidata Query Service (EIN -> official website)",
                   publisher="Wikidata", license="CC0")
        for b in d.get("results", {}).get("bindings", []):
            ein = _ein9(b.get("ein", {}).get("value"))
            site = clean(b.get("s", {}).get("value"))
            phone = clean(b.get("p", {}).get("value"))
            if not ein or not (site or phone):
                continue
            sets, vals = [], []
            # only ever fill a NULL (never overwrite a published value elsewhere)
            sets.append("website=COALESCE(website,?)"); vals.append(site)
            sets.append("phone=COALESCE(phone,?)"); vals.append(phone)
            vals.append(ein)
            cur = conn.execute(
                f"UPDATE org SET {','.join(sets)} WHERE ein=?", vals)
            if cur.rowcount:
                filled += cur.rowcount
        for r in chunk:
            _p_mark(conn, "wikidata", f"wd|{r['ein']}", f"batch {i//batch}")
        _commit(conn)
    return filled


# --------------------------------------------------------------------------
# run()
# --------------------------------------------------------------------------
def run(conn, cfg):
    cfg = cfg or {}
    cfg.setdefault("domain", DOMAIN)
    t0 = time.time()
    _ensure_tables(conn)
    _commit(conn)

    n_bmf = phase_bmf(conn, cfg)
    _commit(conn)
    n_search, s_pages = phase_search(conn, cfg)
    _commit(conn)
    n_orgs = phase_orgs(conn, cfg)
    _commit(conn)
    page_stats = phase_pages(conn, cfg)
    _commit(conn)
    n_wiki = phase_wikidata(conn, cfg)
    _commit(conn)

    remaining = conn.execute(
        "SELECT COUNT(*) FROM nonprofit_candidate WHERE page_done=0").fetchone()[0]
    orgs = conn.execute("SELECT COUNT(*) FROM org WHERE org_type='nonprofit'").fetchone()[0]
    with_fin = conn.execute(
        "SELECT COUNT(*) FROM org WHERE org_type='nonprofit' AND revenue IS NOT NULL").fetchone()[0]
    persons = conn.execute("SELECT COUNT(*) FROM org_person").fetchone()[0]
    notes = (f"bmf_candidates={n_bmf} search_orgs={n_search} search_pages={s_pages} "
             f"pages={page_stats['pages']} persons+={page_stats['persons']} "
             f"pdf_only={page_stats['empty']} page_err={page_stats['errors']} "
             f"wiki_filled={n_wiki} queued_pages={remaining} "
             f"org_nonprofit_rows={orgs} orgs_with_financials={with_fin} org_person={persons} "
             f"seconds={round(time.time()-t0,1)}")
    print(f"[{DOMAIN}] {notes}")
    return {
        "rows": page_stats["persons"] + page_stats["pages"],
        "notes": notes,
        "sources": [(u, "public domain (US federal government)") for u in BMF_URLS]
                   + [(SEARCH_URL, "public data (IRS BMF + Form 990)"),
                      (PP_BASE + "/organizations/", "public data (IRS Form 990)"),
                      (WDQS, "CC0")],
    }


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        conn = db()
        res = run(conn, {"queries": ["homeless"], "max_pages": 6, "budget_seconds": 240,
                         "wiki": False})
        _commit(conn)
        print(res["notes"])
        selftest_report(conn, DOMAIN, "org", where="org_type='nonprofit' AND ein IS NOT NULL", n=3)
        selftest_report(conn, DOMAIN, "org_person", where="1=1", n=3)
    else:
        main(DOMAIN, run, "org")
