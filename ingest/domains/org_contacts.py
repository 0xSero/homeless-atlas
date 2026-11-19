"""org_contacts — the organisation's own published website, general e-mail and phone.

For every organisation already in `org`, discover the site the organisation itself publishes, then
read the contact details that the organisation itself publishes on that site.

Where the website address comes from (both organisation-published, never guessed):

  1. **IRS Form 990 e-file XML** (TEOS bulk batches, `apps.irs.gov/pub/epostcard/990/xml/<year>/`).
     The main return of every 990 / 990-EZ / 990-PF carries `<WebsiteAddressTxt>` and the header
     carries the filer's `<PhoneNum>`.  Each batch is a ~110 MB zip of ~17 000 filings; we only keep
     the filings whose EIN is one of ours.  Every batch is cached under `data/raw/org_contacts/`.
  2. **`org.website`** already in the database (Wikidata P856, written by the `nonprofit` ingestor) —
     re-crawled here so that every value gets a page-level provenance row of its own.

  SAM.gov entity registration (the website of a UEI-only organisation) needs an api.data.gov key and
  is recorded in BLOCKERS.md; nothing here is guessed from a name or a domain pattern.

What we then do: `robots.txt` (honoured, cached per host), the homepage, and — only when the homepage
publishes no general e-mail — one contact page linked from it. 1 request/second through
`ingest.common.fetch`; every response cached under `data/raw/org_contacts/`.

Only organisation-level contact data is kept:
  * **website** — the homepage must answer HTTP 200, be HTML, not be a parked/for-sale page, and must
    mention a distinctive token of the organisation's own name (or carry it in the host name);
  * **email** — only role mailboxes (`info@`, `contact@`, `office@`, `intake@`, …), never a mailbox
    whose local part is a person (the `org_person` roster is used to reject those), never a hosting
    provider's address (`sentry@…`, `wixpress.com`, …);
  * **phone** — a `tel:` link the site publishes; `fax`/`cell`/`mobile` links are rejected. A phone the
    organisation filed as its own filer phone on the 990 is used only when the site crawl found none.

Per-field provenance lives in `org_contacts_site` (`website_source_id`, `email_source_id`,
`phone_source_id` → `source.id` → the exact page that published the value). The columns on `org` are
filled only where they are empty, and `org.source_id` is never rewritten (it belongs to the ingestor
that created the row; a blanket re-point once made the site cite a boundary shapefile for population
figures — see docs/INGEST_CONTRACT.md).

Resumable by design: one row per downloaded batch in `org_contacts_zip`, one row per candidate site in
`org_contacts_site`, and every crawled site appended to `data/raw/org_contacts/site_results.jsonl`,
so a killed run loses at most the site in flight.

    python3 -m ingest.run org_contacts
    ORG_CONTACTS_BUDGET=1200 python3 -m ingest.run org_contacts     # crawl for 20 minutes then stop
    python3 -m ingest.domains.org_contacts --selftest
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import urllib.parse
import urllib.robotparser
import zipfile

from ingest import common
from ingest.common import clean, fetch, name_norm, now, source_id_for, upsert

DOMAIN = "org_contacts"

# ---------------------------------------------------------------- knobs (env, all optional)
YEARS = [y for y in os.environ.get("ORG_CONTACTS_YEARS", "2026,2025,2024,2023").split(",") if y.strip()]
BATCHES = int(os.environ.get("ORG_CONTACTS_BATCHES", "12"))       # 01A .. NNA per year
HARVEST_BUDGET = int(os.environ.get("ORG_CONTACTS_HARVEST", "2400"))  # seconds for the zip phase
BUDGET = int(os.environ.get("ORG_CONTACTS_BUDGET", "1500"))       # seconds for the crawl phase
MAX_SITES = int(os.environ.get("ORG_CONTACTS_MAX_SITES", "0"))    # 0 = no cap
WORKERS = int(os.environ.get("ORG_CONTACTS_WORKERS", "0")) or min(16, os.cpu_count() or 4)
TIER2_EMAIL = os.environ.get("ORG_CONTACTS_EMAIL_TIER2", "0") == "1"
PAGE_TIMEOUT = int(os.environ.get("ORG_CONTACTS_TIMEOUT", "25"))

TEOS = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{year}_TEOS_XML_{batch:02d}A.zip"
JOURNAL = common.RAW / DOMAIN / "site_results.jsonl"
ROBOTS_CACHE = common.RAW / DOMAIN / "robots.json"

IRSTITLE = "IRS Form 990 e-file XML (TEOS bulk batch {year}-{batch:02d}A)"
IRSLIC = "US Government work — public domain (IRS e-file returns as published)"

# ---------------------------------------------------------------- DDL
DDL = """
CREATE TABLE IF NOT EXISTS org_contacts_zip(
  url TEXT PRIMARY KEY, year INTEGER, batch INTEGER, done INTEGER DEFAULT 0,
  filings INTEGER, ours INTEGER, sites INTEGER, note TEXT, source_id INTEGER, ts TEXT);

CREATE TABLE IF NOT EXISTS org_contacts_filing(
  ein TEXT NOT NULL, object_id TEXT NOT NULL, tax_period TEXT, form TEXT, org_id INTEGER,
  filer_name TEXT, state TEXT, website_raw TEXT, website TEXT, phone TEXT,
  zip_url TEXT, source_id INTEGER, ts TEXT, PRIMARY KEY(ein, object_id));
CREATE INDEX IF NOT EXISTS ix_ocfil_ein ON org_contacts_filing(ein);
CREATE INDEX IF NOT EXISTS ix_ocfil_org ON org_contacts_filing(org_id);

CREATE TABLE IF NOT EXISTS org_contacts_site(
  org_id INTEGER PRIMARY KEY,
  org_name TEXT, ein TEXT,
  candidate_url TEXT, candidate_host TEXT, candidate_kind TEXT, candidate_source_id INTEGER,
  priority REAL,
  phase TEXT DEFAULT 'seed',            -- seed | done | blocked_robots | dead
  robots TEXT, http_status INTEGER, final_url TEXT, page_title TEXT, name_match TEXT,
  verified INTEGER DEFAULT 0,
  website TEXT, website_source_id INTEGER,
  email TEXT, email_source_id INTEGER, email_page TEXT,
  phone TEXT, phone_source_id INTEGER, phone_page TEXT,
  pages INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, note TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_ocsite_phase ON org_contacts_site(phase);
"""


# ================================================================ small helpers
def _sid(conn, url, *, title=None, publisher=None, license=None):
    """source.id for `url`, with bytes/sha256 taken from the fetch() cache when present."""
    sid = source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)
    try:
        key = hashlib.sha1(("GET" + url + json.dumps({}, sort_keys=True)).encode()).hexdigest()
        p = common.RAW / DOMAIN / f"{key}.bin"
        if p.exists():
            b = p.read_bytes()
            conn.execute("UPDATE source SET bytes=?, sha256=? WHERE id=? AND (bytes IS NULL OR sha256 IS NULL)",
                         (len(b), hashlib.sha256(b).hexdigest(), sid))
    except Exception:
        pass
    return sid


def _get(url, *, retries=2, timeout=None, headers=None):
    """fetch() that returns (bytes|None, status:int, err:str|None) instead of raising."""
    try:
        b = fetch(url, domain=DOMAIN, timeout=timeout or PAGE_TIMEOUT, retries=retries, headers=headers)
        return b, 200, None
    except Exception as e:  # noqa: BLE001 - a dead site is data, not a crash
        m = re.search(r"HTTP (\d{3})", str(e))
        return None, int(m.group(1)) if m else 0, re.sub(r"\s+", " ", str(e))[:180]


BAD_HOSTS = {
    "facebook.com", "m.facebook.com", "fb.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "guidestar.org", "candid.org", "charitynavigator.org",
    "propublica.org", "wikipedia.org", "amazon.com", "smile.amazon.com", "paypal.com", "paypal.me",
    "venmo.com", "gofundme.com", "godaddy.com", "sedo.com", "afternic.com", "hugedomains.com",
    "squareup.com", "squarespace.com", "wordpress.com", "blogspot.com", "change.org", "yelp.com",
    "bbb.org", "mapquest.com", "google.com", "goo.gl", "bit.ly", "t.co", "tinyurl.com", "zillow.com",
    "indeed.com", "glassdoor.com", "networkforgood.com", "mightycause.com", "classy.org",
}
PARKED = re.compile(r"(domain (?:name )?(?:is|may be) for sale|buy this domain|this domain is parked|"
                    r"parkingcrew|sedoparking|hugedomains|afternic|domain for sale|account suspended|"
                    r"website is coming soon|under construction)", re.I)
JUNK_LOCAL = {"example", "yourname", "youremail", "email", "user", "username", "name", "yourname.com",
              "sentry", "webmaster", "postmaster", "hostmaster", "abuse", "noreply", "no-reply",
              "donotreply", "test", "admin@localhost"}
JUNK_DOMAIN = {"example.com", "example.org", "domain.com", "yourdomain.com", "email.com", "sentry.io",
               "wixpress.com", "wix.com", "squarespace.com", "godaddy.com", "test.com", "localhost",
               "sentry-next.wixpress.com", "wordpress.com", "shopify.com", "site.com", "mywebsite.com",
               "company.com", "emailservice.com", "constantcontact.com", "mailchimp.com", "hubspot.com",
               "w3.org", "schema.org", "companyname.com", "gmail.com.com", "yourcompany.com"}
GENERIC_LOCAL = {
    "info", "information", "contact", "contacts", "contactus", "contact-us", "office", "offices",
    "admin", "administration", "hello", "hi", "help", "helpdesk", "support", "inquiries", "inquiry",
    "enquiries", "enquiry", "questions", "mail", "email", "general", "reception", "frontdesk",
    "front-desk", "front.desk", "main", "team", "staff", "shelter", "shelters", "intake", "services",
    "service", "programs", "program", "communications", "development", "volunteer", "volunteers",
    "donate", "donations", "giving", "outreach", "events", "media", "news", "gethelp", "get-help",
    "referrals", "referral", "reachus", "reach-us", "ask", "website", "web", "connect", "clients",
    "community", "shelterinfo", "hq", "mainoffice", "main-office", "officemanager", "resources",
}
SOCIAL_RE = re.compile(r"(facebook|twitter|instagram|linkedin|youtube|tiktok)\.com", re.I)
RE_MAILTO = re.compile(r"mailto:([^\"'<>\s\)]+)", re.I)
RE_TEL = re.compile(r"tel:([+0-9()\-.\s]{7,24})", re.I)
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,12}")
RE_PHONE_TXT = re.compile(r"(?:phone|call us|call|tel|telephone|office)\s*(?:at|:|is)?\s*"
                          r"(\(?\d{3}\)?[\s.\-]{0,2}\d{3}[\s.\-]{0,2}\d{4})", re.I)
STOP_TOK = {"THE", "AND", "FOR", "OF", "INC", "LLC", "INCORPORATED", "CORP", "CORPORATION", "ASSOC",
            "ASSOCIATION", "FOUNDATION", "SERVICE", "SERVICES", "PROGRAM", "PROGRAMS", "PROJECT",
            "PROJECTS", "COUNTY", "CITY", "AREA", "COMPANY", "ORGANIZATION", "MINISTRIES", "NONPROFIT",
            "UNITED", "AMERICA", "AMERICAN", "STATE", "NATIONAL", "REGIONAL"}

GENERIC_TOK = {"HOME", "HOMES", "HOUSE", "HOUSING", "STREET", "CARE", "CARES", "HELP", "HOPE",
               "LIFE", "NEW", "FIRST", "GOOD", "SAFE", "SHELTER", "PEOPLE", "COMMUNITY", "FAMILY",
               "FAMILIES", "YOUTH", "WOMEN", "WOMENS", "MEN", "MENS", "CHILD", "CHILDREN", "FOOD",
               "HEALTH", "SOCIAL", "SUPPORT", "SERVING", "SHARE", "TRANSITIONAL", "TEMPORARY",
               "EMERGENCY", "CRISIS", "RESCUE", "MISSION", "MINISTRY", "MISSIONS", "HOUSED",
               "HOMELESS", "HOMELESSNESS", "SHELTERS", "RECOVERY", "CENTER", "CENTRE", "CAMP",
               "RANCH", "FARM", "ADULT", "SENIOR", "LEGAL", "MEDICAL", "CLINIC", "CENTRAL", "NORTH",
               "SOUTH", "EAST", "WEST", "GREATER", "METRO", "VALLEY", "RIVER", "LAKE", "MOUNTAIN"}


def norm_site(raw):
    """Normalise a published website string into an http(s) URL we may fetch, or None.

    Only repairs whitespace/scheme noise ('HTTP://WWW.X.ORG', 'https: www.x.org'); never invents a
    host: a string without a dotted host is dropped, not guessed at.
    """
    s = clean(raw)
    if not s:
        return None
    s = s.strip().strip(".,;:'\"")
    s = re.sub(r"\s+", "", s)
    low = s.lower().strip(".")
    if low in {"n/a", "na", "n.a.", "none", "nil", "no", "no.", "-", "--", "0", "x", "tbd", "unknown",
               "www", "http://", "https://", "ww", "n/a.", "same", "none.", "doesnotapply",
               "notapplicable", "notavailable", "no website", "n/a-", "na.", "n.a"}:
        return None
    s = re.sub(r"^(https?):/*", lambda m: m.group(1).lower() + "://", s)
    if not re.match(r"^https?://", s, re.I):
        s = "http://" + s
    try:
        p = urllib.parse.urlsplit(s)
        host = (p.hostname or "").lower().strip(".")
    except Exception:
        return None
    if not host or "." not in host or " " in host or "_" in host:
        return None
    tld = host.rsplit(".", 1)[1]
    if not re.fullmatch(r"[a-z]{2,6}", tld):
        return None
    bare = host[4:] if host.startswith("www.") else host
    if bare in BAD_HOSTS or any(bare.endswith("." + b) for b in BAD_HOSTS):
        return None
    if bare.count(".") == 0:
        return None
    path = (p.path or "").rstrip("/")
    if path.lower() in ("/index.html", "/index.htm", "/index.php", "/home"):
        path = ""
    return f"https://{host}{path}"


def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10 or d[0] in "01":
        return None
    if d in ("5555555555", "1234567890", "0000000000") or d[:3] == "555" or d[3:6] == "555":
        return None
    if len(set(d)) <= 2:
        return None
    return f"{d[:3]}-{d[3:6]}-{d[6:]}"


def norm_email(addr):
    a = re.split(r"[?&]", (addr or ""))[0]
    a = urllib.parse.unquote(a.strip().strip(".,;:'\"()<>")).lower()
    if a.count("@") != 1:
        return None
    loc, dom = a.split("@")
    if not loc or not dom or "." not in dom:
        return None
    if not re.fullmatch(r"[a-z0-9._%+\-]+", loc) or not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,12}", dom):
        return None
    if dom in JUNK_DOMAIN or any(dom.endswith("." + d) for d in JUNK_DOMAIN):
        return None
    if loc in JUNK_LOCAL or re.search(r"\.(png|jpg|jpeg|gif|webp|svg|css|js)$", a):
        return None
    if len(loc) > 40 or len(a) > 90:
        return None
    if re.search(r"^(u003e|u003c)|sentry|2x", loc):
        return None
    return a


# ================================================================ phase 1: TEOS harvest
_ZIPB = b""
_ZF = [None, None]  # (pid, ZipFile) — one ZipFile per worker process, not one per filing


def _cache_path(url):
    key = hashlib.sha1(("GET" + url + json.dumps({}, sort_keys=True)).encode()).hexdigest()
    return common.RAW / DOMAIN / f"{key}.bin"


def _parse_file(path):
    """Same parse as _parse_member() but for a filing already extracted to disk (deflate64 batches)."""
    try:
        raw = open(path, "rb").read()
    except Exception:
        return None
    return _parse_bytes(raw, os.path.basename(path))


def _zipf():
    pid = os.getpid()
    if _ZF[0] != pid or _ZF[1] is None:
        _ZF[0], _ZF[1] = pid, zipfile.ZipFile(io.BytesIO(_ZIPB))
    return _ZF[1]


def _parse_member(name):
    """Regex-parse one filing inside the batch zip. Returns a dict or None."""
    try:
        raw = _zipf().read(name)
    except Exception:
        return None
    return _parse_bytes(raw, name)


def _parse_bytes(raw, name):
    m = re.search(rb"<Filer>(.*?)</Filer>", raw, re.S)
    if not m:
        return None
    filer = m.group(1)

    def g(pat, block=filer):
        mm = re.search(pat, block, re.S)
        return mm.group(1).decode("utf-8", "replace").strip() if mm else None

    ein = g(rb"<EIN>(\d{9})</EIN>")
    if not ein:
        return None
    hdr = re.search(rb"<ReturnHeader.*?</ReturnHeader>", raw, re.S)
    hdr = hdr.group(0) if hdr else b""
    web = None
    wm = re.search(rb"<WebsiteAddressTxt>(.{0,300}?)</WebsiteAddressTxt>", raw, re.S)
    if wm:
        web = re.sub(r"\s+", " ", wm.group(1).decode("utf-8", "replace")).strip()
    phone = None
    pm = re.search(rb"<PhoneNum>(\d{10})</PhoneNum>", filer)
    if pm:
        phone = pm.group(1).decode()
    obj = os.path.basename(name)
    obj = obj[:-len("_public.xml")] if obj.endswith("_public.xml") else obj.rsplit(".", 1)[0]
    return {
        "ein": ein,
        "object_id": obj,
        "tax_period": (re.search(rb"<TaxPeriodEndDt>(\d{4}-\d{2}-\d{2})</TaxPeriodEndDt>", hdr).group(1).decode()
                       if re.search(rb"<TaxPeriodEndDt>", hdr) else None),
        "form": (re.search(rb"<ReturnTypeCd>([A-Za-z0-9]+)</ReturnTypeCd>", hdr).group(1).decode()
                 if re.search(rb"<ReturnTypeCd>", hdr) else None),
        "filer_name": g(rb"<BusinessNameLine1Txt>(.*?)</BusinessNameLine1Txt>"),
        "state": g(rb"<StateAbbreviationCd>(..)</StateAbbreviationCd>"),
        "website_raw": web,
        "phone": phone,
    }


def _years_batches():
    out = []
    for y in YEARS:
        for b in range(1, BATCHES + 1):
            out.append((int(y), b))
    return out


def _harvest(conn, t0):
    """Download the TEOS batches (newest first) and keep the filings of our EINs."""
    ours = {r[0]: r[1] for r in conn.execute("SELECT ein, id FROM org WHERE ein IS NOT NULL")}
    ours = {str(k).replace("-", "").zfill(9): v for k, v in ours.items()}
    if not ours:
        return {"zips": 0, "filings": 0, "note": "no EINs in org"}

    done = {r[0] for r in conn.execute("SELECT url FROM org_contacts_zip WHERE done=1")}
    todo = [(y, b, TEOS.format(year=y, batch=b)) for y, b in _years_batches()]
    # newest first: a 2026 filing is the freshest thing an org has published
    todo.sort(key=lambda t: (-t[0], -t[1]))
    todo = [t for t in todo if t[2] not in done]

    stats = {"zips": 0, "filings": 0, "kept": 0, "skipped": 0}
    import multiprocessing as mp
    for year, batch, url in todo:
        if time.time() - t0 > HARVEST_BUDGET:
            stats["skipped"] += len(todo) - stats["zips"]
            break
        blob, status, err = _get(url, retries=2, timeout=900)
        _ZF[1] = None  # every batch is a new zip: drop the worker-side handle before refilling
        if blob is None:
            conn.execute("INSERT INTO org_contacts_zip(url,year,batch,done,note,ts) VALUES(?,?,?,2,?,?) "
                         "ON CONFLICT(url) DO UPDATE SET note=excluded.note, ts=excluded.ts",
                         (url, year, batch, f"fetch failed: {err}", now()))
            conn.commit()
            stats["skipped"] += 1
            continue
        sid = _sid(conn, url, title=IRSTITLE.format(year=year, batch=batch),
                   publisher="Internal Revenue Service", license=IRSLIC)
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            names = [n for n in zf.namelist() if n.endswith(".xml")]
            methods = {i.compress_type for i in zf.infolist()}
        except Exception as e:  # noqa: BLE001
            conn.execute("INSERT INTO org_contacts_zip(url,year,batch,done,note,source_id,ts) VALUES(?,?,?,2,?,?,?) "
                         "ON CONFLICT(url) DO UPDATE SET done=2, note=excluded.note, ts=excluded.ts",
                         (url, year, batch, f"bad zip: {e}"[:180], sid, now()))
            conn.commit()
            continue
        del zf
        # some batches are stored with Deflate64 (method 9), which Python's zipfile cannot inflate:
        # those are unpacked once with the system `unzip` and parsed from disk instead
        deflate64 = bool(methods - {0, 8, 12, 14})
        kept = []
        if deflate64:
            import subprocess, shutil, tempfile
            tmpd = tempfile.mkdtemp(dir=str(common.RAW / DOMAIN), prefix="x64-")
            try:
                src = _cache_path(url)
                subprocess.run(["unzip", "-qq", "-o", str(src), "-d", tmpd], check=True,
                               timeout=1800)
                files = []
                for root, _dirs, fs in os.walk(tmpd):
                    for f in fs:
                        if f.endswith(".xml"):
                            files.append(os.path.join(root, f))
                ctx = mp.get_context("fork")
                with ctx.Pool(WORKERS) as pool:
                    for r in pool.imap_unordered(_parse_file, files, chunksize=200):
                        if r and r["ein"] in ours:
                            kept.append(r)
                names = files
            finally:
                shutil.rmtree(tmpd, ignore_errors=True)
        else:
            global _ZIPB
            _ZIPB = blob
            _ZF[1] = None
            if WORKERS <= 1:
                for n in names:
                    r = _parse_member(n)
                    if r and r["ein"] in ours:
                        kept.append(r)
            else:
                ctx = mp.get_context("fork")
                with ctx.Pool(WORKERS) as pool:
                    for r in pool.imap_unordered(_parse_member, names, chunksize=200):
                        if r and r["ein"] in ours:
                            kept.append(r)
            _ZIPB = b""
        for r in kept:
            r["org_id"] = ours.get(r["ein"])
            r["website"] = norm_site(r["website_raw"])
            r["zip_url"] = url
            r["source_id"] = sid
            r["ts"] = now()
            upsert(conn, "org_contacts_filing", r,
                   keys=["ein", "object_id"])
        with_site = len({r["ein"] for r in kept if r["website"]})
        conn.execute("""INSERT INTO org_contacts_zip(url,year,batch,done,filings,ours,sites,source_id,ts,note)
                        VALUES(?,?,?,1,?,?,?,?,?,?)
                        ON CONFLICT(url) DO UPDATE SET done=1, filings=excluded.filings, ours=excluded.ours,
                          sites=excluded.sites, source_id=excluded.source_id, ts=excluded.ts, note=excluded.note""",
                     (url, year, batch, len(names), len(kept), with_site, sid, now(),
                      "deflate64 (method 9): unpacked with the system unzip and parsed from disk"
                      if deflate64 else None))
        conn.commit()
        stats["zips"] += 1
        stats["filings"] += len(names)
        stats["kept"] += len(kept)
        print(f"[{DOMAIN}] batch {year}-{batch:02d}A: {len(names)} filings, {len(kept)} ours "
              f"({with_site} with a website)", flush=True)
    return stats


# ================================================================ phase 2: candidates
def _seed(conn):
    """Turn harvested website addresses (and existing org.website) into crawl candidates."""
    have = {r[0] for r in conn.execute("SELECT org_id FROM org_contacts_site")}
    rows = conn.execute("""SELECT f.org_id, f.ein, f.website, f.phone, f.tax_period,
                                  o.name AS org_name, COALESCE(o.revenue, 0) AS revenue
                           FROM org_contacts_filing f JOIN org o ON o.id=f.org_id
                           WHERE f.org_id IS NOT NULL ORDER BY f.tax_period""").fetchall()
    best = {}
    for r in rows:
        if not r["website"]:
            continue
        cur = best.get(r["org_id"])
        if cur is None or (r["tax_period"] or "") >= (cur["tax_period"] or ""):
            best[r["org_id"]] = dict(r)

    adds = 0
    for org_id, r in best.items():
        if org_id in have:
            continue
        upsert(conn, "org_contacts_site",
               {"org_id": org_id, "org_name": r["org_name"], "ein": r["ein"],
                "candidate_url": r["website"], "candidate_host": urllib.parse.urlsplit(r["website"]).hostname,
                "candidate_kind": "form990", "candidate_source_id": None,
                "priority": float(r["revenue"] or 0), "phase": "seed", "updated_at": now(),
                "note": f"website as filed on Form 990 for period {r['tax_period']}"},
               keys=["org_id"])
        adds += 1
        have.add(org_id)

    # organisations that already carry a website in `org` (Wikidata) get a provenance row too
    for r in conn.execute("""SELECT id, name, ein, website, COALESCE(revenue,0) rev FROM org
                             WHERE website IS NOT NULL AND TRIM(website)<>''""").fetchall():
        if r["id"] in have:
            continue
        u = norm_site(r["website"])
        if not u:
            continue
        upsert(conn, "org_contacts_site",
               {"org_id": r["id"], "org_name": r["name"], "ein": r["ein"], "candidate_url": u,
                "candidate_host": urllib.parse.urlsplit(u).hostname, "candidate_kind": "org.website",
                "priority": float(r["rev"] or 0), "phase": "seed", "updated_at": now(),
                "note": "website already recorded on the org row (Wikidata P856); re-verified here"},
               keys=["org_id"])
        adds += 1
        have.add(r["id"])
    conn.commit()
    return adds


# ================================================================ phase 3: crawl
_robots_mem = {}
_robots_dirty = [False]


def _load_robots():
    if ROBOTS_CACHE.exists():
        try:
            _robots_mem.update(json.loads(ROBOTS_CACHE.read_text()))
        except Exception:
            pass


def _save_robots():
    if not _robots_dirty[0]:
        return
    try:
        ROBOTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ROBOTS_CACHE.write_text(json.dumps(_robots_mem))
        _robots_dirty[0] = False
    except Exception:
        pass


def _robots_allows(host, url):
    """returns (allowed: bool, note: str) — robots.txt honoured per host, cached."""
    key = host or ""
    if key in _robots_mem:
        rec = _robots_mem[key]
    else:
        rec = None
        blob, status, err = _get(f"https://{host}/robots.txt", retries=1, timeout=20)
        if status == 0:  # https unreachable at all: try the plain-http robots before giving up
            blob, status, err = _get(f"http://{host}/robots.txt", retries=1, timeout=20)
        if status == 200 and blob is not None:
            text = blob.decode("utf-8", "replace")
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(text.splitlines())
            try:
                allowed = rp.can_fetch(common.UA, url)
            except Exception:
                allowed = True
            rec = {"status": 200, "allowed": bool(allowed)}
        elif status in (401, 403):
            rec = {"status": status, "allowed": False}
        else:  # no robots.txt (404) or unreachable: nothing is disallowed
            rec = {"status": status, "allowed": True}
        _robots_mem[key] = rec
        _robots_dirty[0] = True
    return bool(rec.get("allowed", True)), f"robots {rec.get('status')}"


def _text_of(html_bytes):
    """title + visible text; lxml when available, html.parser as the fallback."""
    try:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html_bytes, "lxml")
        except Exception:
            soup = BeautifulSoup(html_bytes, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for bad in soup(["script", "style", "noscript"]):
            bad.decompose()
        return soup, title, soup.get_text(" ", strip=True)
    except Exception:
        return None, "", ""


def _contact_links(soup, base):
    out = []
    if soup is None:
        return out
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        label = (a.get_text(" ", strip=True) or "") + " " + href
        if re.search(r"contact|get in touch|reach us", label, re.I):
            u = urllib.parse.urljoin(base, href)
            sp = urllib.parse.urlsplit(u)
            if sp.scheme in ("http", "https") and sp.hostname:
                out.append(u.split("#")[0])
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:2]


def _name_match(name, title, text, host):
    """Prove the page belongs to this organisation. Host carrying the full name is strongest."""
    nn = name_norm(name)
    bare = (host or "").lower().replace("-", "").replace(".", "")
    full = nn.replace(" ", "").lower()
    if len(full) >= 6 and full in bare:
        return "host:fullname"
    toks = [t for t in nn.split() if len(t) >= 4 and t not in STOP_TOK]
    hay = ((title or "") + " " + (text or "")[:400000]).lower()
    strong = [t for t in toks if t not in GENERIC_TOK]
    hit_text = [t for t in strong if t.lower() in hay]
    if hit_text:
        return f"text:{hit_text[0]}"
    hit_host = [t for t in strong if len(t) >= 4 and t.lower() in bare]
    if hit_host:
        return f"host:{hit_host[0]}"
    weak = [t for t in toks if t in GENERIC_TOK and t.lower() in hay]
    if len(weak) >= 2:
        return "text:" + "+".join(weak[:2]) + " (generic)"
    return None


def _person_tokens(conn, org_id):
    out = set()
    for r in conn.execute("SELECT person_norm FROM org_person WHERE org_id=?", (org_id,)):
        for t in (r[0] or "").split():
            if len(t) >= 3:
                out.add(t.lower())
    return out


def _pick_email(emails_by_page, person_tokens):
    """emails_by_page: [(url, [addr...])] in page order -> (addr, url) or (None, None)."""
    counts, first_page = {}, {}
    for url, addrs in emails_by_page:
        for a in addrs:
            a = norm_email(a)
            if not a:
                continue
            loc = a.split("@")[0]
            ok = loc in GENERIC_LOCAL or loc.replace("-", "").replace(".", "") in GENERIC_LOCAL
            if not ok and TIER2_EMAIL:
                toks = {t for t in re.split(r"[._\-0-9]+", loc) if len(t) >= 3}
                ok = (len(toks) >= 2 or len(loc) >= 8) and not (toks & person_tokens)
            if not ok:
                continue
            counts[a] = counts.get(a, 0) + 1
            first_page.setdefault(a, url)
    if not counts:
        return None, None
    best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best, first_page[best]


def _pick_phone(phones_by_page):
    counts, first_page = {}, {}
    for url, nums in phones_by_page:
        for n in nums:
            if not n:
                continue
            counts[n] = counts.get(n, 0) + 1
            first_page.setdefault(n, url)
    if not counts:
        return None, None
    best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best, first_page[best]


def _page_links(html_bytes, soup):
    html = html_bytes.decode("utf-8", "replace")
    emails, phones = [], []
    for m in RE_MAILTO.finditer(html):
        emails.append(m.group(1))
    for m in RE_TEL.finditer(html):
        if _tel_is_fax(html, m.start()):
            continue
        p = norm_phone(m.group(1))
        if p:
            phones.append(p)
    text = ""
    if soup is not None:
        text = soup.get_text(" ", strip=True)
    if text and len(emails) < 1:
        for m in RE_EMAIL.finditer(text):
            emails.append(m.group(0))
    if text:
        for m in RE_PHONE_TXT.finditer(text[:120000]):
            start = max(0, m.start() - 40)
            ctx = text[start:m.start() + len(m.group(0))]
            if re.search(r"fax|cell|mobile|text only", ctx, re.I):
                continue
            p = norm_phone(m.group(1))
            if p:
                phones.append(p)
    return emails, phones


def _tel_is_fax(html, pos):
    ctx = html[max(0, pos - 140):pos].lower()
    return bool(re.search(r"fax", ctx))


def _crawl_site(conn, s, person_tokens):
    url = s["candidate_url"]
    host = s["candidate_host"]
    res = {"org_id": s["org_id"], "org_name": s["org_name"], "ein": s["ein"],
           "candidate_url": url, "candidate_host": host, "candidate_kind": s["candidate_kind"],
           "priority": s["priority"], "attempts": (s["attempts"] or 0) + 1, "updated_at": now(),
           "verified": 0, "pages": 0, "phase": "done", "website": None, "email": None, "phone": None}

    allowed, rnote = _robots_allows(host, url)
    res["robots"] = rnote
    if not allowed:
        res["phase"] = "blocked_robots"
        res["note"] = f"{rnote}: crawling disallowed for {url}"
        return res

    blob, status, err = _get(url, retries=2)
    used_url = url
    if blob is None and status in (0, 301, 302, 400, 404, 403, 410, 500, 502, 503):
        # a filed address is often scheme- or www-wrong: try the other scheme / bare host, once
        alt = []
        sp = urllib.parse.urlsplit(url)
        if sp.scheme == "https":
            alt.append(urllib.parse.urlunsplit(("http", sp.netloc, sp.path, "", "")))
        if sp.netloc.startswith("www."):
            alt.append(urllib.parse.urlunsplit((sp.scheme, sp.netloc[4:], sp.path, "", "")))
        else:
            alt.append(urllib.parse.urlunsplit((sp.scheme, "www." + sp.netloc, sp.path, "", "")))
        for a in alt:
            b2, st2, e2 = _get(a, retries=1)
            if b2 is not None:
                blob, status, used_url = b2, 200, a
                break
    res["http_status"] = status
    res["final_url"] = used_url
    if blob is None:
        res["phase"] = "dead"
        res["note"] = f"no response ({status}): {err}"
        return res

    soup, title, text = _text_of(blob)
    res["page_title"] = clean(title)[:300] if title else None
    if not text or len(text) < 80:
        res["note"] = "page has no readable text"
        return res
    if PARKED.search(text[:4000]):
        res["note"] = "parked / for-sale / placeholder page — not treated as the org's website"
        return res

    nm = _name_match(s["org_name"], title, text, host)
    res["name_match"] = nm
    emails_pages, phones_pages = [], []
    em, ph = _page_links(blob, soup)
    emails_pages.append((used_url, em))
    phones_pages.append((used_url, ph))
    res["pages"] = 1

    email, email_url = _pick_email(emails_pages, person_tokens)
    if not email:
        for c in _contact_links(soup, used_url):
            if urllib.parse.urlsplit(c).hostname != host and \
                    not (urllib.parse.urlsplit(c).hostname or "").endswith(host):
                continue
            cb, cs, ce = _get(c, retries=1)
            if cb is None:
                continue
            csoup, ctitle, ctext = _text_of(cb)
            if not ctext or len(ctext) < 80:
                continue
            cem, cph = _page_links(cb, csoup)
            res["pages"] += 1
            emails_pages.append((c, cem))
            phones_pages.append((c, cph))
            if not nm:
                nm2 = _name_match(s["org_name"], ctitle or "", ctext or "", host)
                if nm2:
                    nm = nm2
                    res["name_match"] = nm
            if _pick_email(emails_pages, person_tokens)[0]:
                break
    email, email_url = _pick_email(emails_pages, person_tokens)
    phone, phone_url = _pick_phone(phones_pages)
    if not phone:
        # the phone the organisation filed as its own on the 990 (never a cell/mobile field)
        f = conn.execute("""SELECT phone, zip_url FROM org_contacts_filing
                            WHERE org_id=? AND phone IS NOT NULL AND phone<>''
                            ORDER BY tax_period DESC LIMIT 1""", (s["org_id"],)).fetchone()
        if f:
            p = norm_phone(f["phone"])
            if p:
                phone, phone_url = p, f["zip_url"]
                res["phone_kind"] = "form990-filer"

    if nm:
        res["verified"] = 1
        res["website"] = used_url
        res["note"] = f"verified by {nm}" + ("" if used_url == url else f" (filed as {url})")
    else:
        res["note"] = "HTTP 200 but no name match — not recorded as the org's website"
    res["email"] = email
    res["email_page"] = email_url
    res["phone"] = phone
    res["phone_page"] = phone_url
    if res.get("phone_kind") == "form990-filer" and phone:
        res["note"] = (res.get("note") or "") + "; phone taken from the filer block of the org's own Form 990"
    return res


def _journal_append(rec):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _journal_load():
    out = {}
    if JOURNAL.exists():
        for line in JOURNAL.read_text().splitlines():
            try:
                r = json.loads(line)
                out[r["org_id"]] = r
            except Exception:
                continue
    return out


def _replay(conn, j):
    """Land the journal in org_contacts_site (idempotent; called before every crawl)."""
    n = 0
    for org_id, r in j.items():
        cur = conn.execute("SELECT phase, updated_at FROM org_contacts_site WHERE org_id=?",
                           (org_id,)).fetchone()
        if cur and cur["phase"] not in ("seed", None) and (cur["updated_at"] or "") >= (r.get("updated_at") or ""):
            continue
        upsert(conn, "org_contacts_site", {
            "org_id": org_id, "org_name": r.get("org_name"), "ein": r.get("ein"),
            "candidate_url": r.get("candidate_url"), "candidate_host": r.get("candidate_host"),
            "candidate_kind": r.get("candidate_kind"), "priority": r.get("priority"),
            "phase": r.get("phase") or "done", "robots": r.get("robots"),
            "http_status": r.get("http_status"), "final_url": r.get("final_url"),
            "page_title": r.get("page_title"), "name_match": r.get("name_match"),
            "verified": r.get("verified") or 0, "website": r.get("website"),
            "email": r.get("email"), "email_page": r.get("email_page"),
            "phone": r.get("phone"), "phone_page": r.get("phone_page"),
            "pages": r.get("pages"), "attempts": r.get("attempts"), "note": r.get("note"),
            "updated_at": r.get("updated_at"),
        }, keys=["org_id"])
        n += 1
    return n


def _crawl(conn, t0):
    _load_robots()
    j = _journal_load()
    todo = conn.execute("""
        SELECT s.* FROM org_contacts_site s JOIN org o ON o.id=s.org_id
        WHERE s.phase IN ('seed','dead') AND s.candidate_url IS NOT NULL
          AND COALESCE(s.attempts,0) < 2
        ORDER BY s.priority DESC, s.org_id""").fetchall()
    todo = [dict(r) for r in todo if r["org_id"] not in j]
    if MAX_SITES:
        todo = todo[:MAX_SITES]
    budget_end = time.time() + BUDGET
    persons = {}
    n = 0
    for s in todo:
        if time.time() > budget_end:
            break
        if s["org_id"] not in persons:
            persons[s["org_id"]] = _person_tokens(conn, s["org_id"])
        if (s.get("attempts") or 0) >= 1:
            # a filed address that did not answer: try the next address the org filed in a later/
            # earlier year before giving up on the organisation
            alt = conn.execute("""SELECT website FROM org_contacts_filing
                                  WHERE org_id=? AND website IS NOT NULL AND website<>?
                                  ORDER BY tax_period DESC LIMIT 1""",
                               (s["org_id"], s["candidate_url"])).fetchone()
            if alt and alt[0]:
                s = dict(s)
                s["candidate_url"] = alt[0]
                s["candidate_host"] = urllib.parse.urlsplit(alt[0]).hostname
        try:
            rec = _crawl_site(conn, s, persons[s["org_id"]])
        except Exception as e:  # noqa: BLE001 - one bad site must not kill the sweep
            rec = {"org_id": s["org_id"], "org_name": s["org_name"], "ein": s["ein"],
                   "candidate_url": s["candidate_url"], "candidate_host": s["candidate_host"],
                   "candidate_kind": s["candidate_kind"], "priority": s["priority"],
                   "attempts": (s["attempts"] or 0) + 1, "updated_at": now(), "phase": "dead",
                   "verified": 0, "note": f"crawler error: {e}"[:200]}
        _journal_append(rec)
        j[s["org_id"]] = rec
        n += 1
        if n % 25 == 0:
            _save_robots()
            vs = sum(1 for r in j.values() if r.get("verified"))
            print(f"[{DOMAIN}] crawled {n}/{len(todo)} sites this run; journal: "
                  f"{len(j)} orgs, {vs} verified, {sum(1 for r in j.values() if r.get('email'))} emails",
                  flush=True)
    _save_robots()
    return n


# ================================================================ phase 4: write org rows
def _write(conn):
    """Land verified values on `org` (fill blanks only) and set page-level provenance."""
    out = {"website": 0, "email": 0, "phone": 0}
    rows = conn.execute("""SELECT * FROM org_contacts_site
                           WHERE verified=1 OR email IS NOT NULL OR phone IS NOT NULL""").fetchall()
    for r in rows:
        org = conn.execute("SELECT id, website, email, phone, source_id FROM org WHERE id=?",
                           (r["org_id"],)).fetchone()
        if not org:
            continue
        web = r["website"] if r["verified"] else None
        if web:
            u = norm_site(web)
            if u:
                web = u
        wsid = _sid(conn, r["final_url"] or r["candidate_url"], title=clean(r["page_title"]) or None,
                    publisher=r["org_name"], license="published by the organisation itself")
        esid = None
        if r["email"]:
            esid = _sid(conn, r["email_page"] or r["final_url"],
                        title=clean(r["page_title"]) or None, publisher=r["org_name"],
                        license="published by the organisation itself")
        psid = None
        if r["phone"] and r["phone_page"]:
            psid = _sid(conn, r["phone_page"], publisher=r["org_name"],
                        license="published by the organisation itself")
        cur = conn.execute("SELECT website, email, phone FROM org WHERE id=?", (r["org_id"],)).fetchone()
        if web and not clean(cur["website"]):
            conn.execute("UPDATE org SET website=?, updated_at=? WHERE id=?", (web, now(), r["org_id"]))
            out["website"] += 1
        if r["email"] and not clean(cur["email"]):
            conn.execute("UPDATE org SET email=?, updated_at=? WHERE id=?", (r["email"], now(), r["org_id"]))
            out["email"] += 1
        if r["phone"] and not clean(cur["phone"]):
            conn.execute("UPDATE org SET phone=?, updated_at=? WHERE id=?", (r["phone"], now(), r["org_id"]))
            out["phone"] += 1
        conn.execute("UPDATE org SET source_id=? WHERE id=? AND source_id IS NULL", (wsid, r["org_id"]))
        conn.execute("""UPDATE org_contacts_site SET website_source_id=COALESCE(website_source_id,?),
                        email_source_id=COALESCE(email_source_id,?), phone_source_id=COALESCE(phone_source_id,?),
                        phone=COALESCE(phone,?) WHERE org_id=?""",
                     (wsid, esid, psid, None, r["org_id"]))
    conn.commit()
    return out


# ================================================================ entry points
def run(conn, cfg):
    conn.executescript(DDL)
    conn.commit()
    t0 = time.time()
    h = _harvest(conn, t0)
    seeded = _seed(conn)
    replayed = _replay(conn, _journal_load())
    conn.commit()
    # the crawl is long and network-bound: do not hold the archive write lock for it
    for fh in getattr(common, "_LOCK_HELD", []):
        try:
            fh.close()
        except Exception:
            pass
    common._LOCK_HELD.clear()
    try:
        crawled = _crawl(conn, t0)
    finally:
        common._acquire_write_lock()
    replayed += _replay(conn, _journal_load())
    wrote = _write(conn)
    conn.execute("INSERT OR REPLACE INTO org_contacts_zip(url,year,batch,done,note,ts) "
                 "VALUES('run-summary',?,?,1,?,?)",
                 (int(time.time()), 0, json.dumps({"harvest": h, "seeded": seeded, "crawled": crawled,
                                                   "write": wrote})[:400], now()))
    conn.commit()
    tot = conn.execute("SELECT COUNT(*) FROM org_contacts_site WHERE verified=1").fetchone()[0]
    em = conn.execute("SELECT COUNT(*) FROM org_contacts_site WHERE email IS NOT NULL").fetchone()[0]
    ph = conn.execute("SELECT COUNT(*) FROM org_contacts_site WHERE phone IS NOT NULL").fetchone()[0]
    notes = (f"harvest {h}; {seeded} new candidates; {crawled} crawled this run; "
             f"verified websites {tot}, emails {em}, phones {ph}; org columns written {wrote}")
    sources = [
        ("https://apps.irs.gov/pub/epostcard/990/xml/", "US Government work — public domain"),
    ]
    return {"rows": tot, "notes": notes, "sources": sources}


def selftest(conn):
    conn.executescript(DDL)
    n = conn.execute("SELECT COUNT(*) FROM org_contacts_site").fetchone()[0]
    v = conn.execute("SELECT COUNT(*) FROM org_contacts_site WHERE verified=1").fetchone()[0]
    e = conn.execute("SELECT COUNT(*) FROM org_contacts_site WHERE email IS NOT NULL").fetchone()[0]
    p = conn.execute("SELECT COUNT(*) FROM org_contacts_site WHERE phone IS NOT NULL").fetchone()[0]
    print(f"[{DOMAIN}] candidates={n} verified_websites={v} emails={e} phones={p}")
    for r in conn.execute("""SELECT s.org_id, s.org_name, s.candidate_url, s.final_url, s.verified,
                                    s.email, s.phone, s.note, s.website_source_id, s.email_source_id,
                                    s.phone_source_id
                             FROM org_contacts_site s WHERE s.verified=1 ORDER BY RANDOM() LIMIT 3"""):
        print(dict(r))
    for r in conn.execute("""SELECT s.org_id, s.org_name, s.email, s.email_page, s.email_source_id
                             FROM org_contacts_site s WHERE s.email IS NOT NULL ORDER BY RANDOM() LIMIT 3"""):
        print(dict(r))


if __name__ == "__main__":
    import sys
    conn = common.db()
    if "--selftest" in sys.argv:
        selftest(conn)
    else:
        print(run(conn, {}))
