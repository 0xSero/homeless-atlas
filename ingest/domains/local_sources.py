"""local_sources -- per-county research files, scraped and re-verified on every run.

Why this exists
---------------
National datasets stop at the county line. What a county's homelessness actually looks like
lives in local reports, audits, plans and news, and a human has to find and read those. That
work is recorded in `research/county/<ST>/<FIPS>-<slug>.md`: YAML front matter listing every
source (and the numeric claims and responsible offices the page may show), then a cited
narrative. This ingestor is the part a machine can do: it re-fetches every cited URL, checks
that each quoted number is really printed in the document, checks that each office's published
contact details are really on the official page it cites, and writes what holds up.

A number whose citation does not contain the number is worse than no number (README), so a
claim reaches the `metric` table only when its exact quote and its value both appear in the
fetched text. Links rot: when a claim that verified before fails now, its metric row stays and
the check tables (and research/county/CHECK.md) show the failure. Rot is information, not a
reason to delete.

Documents come through the fetch ladder (ingest/fetch_ladder.py: direct, retry, browser
headers, publisher alternates, Wayback, a fresh Wayback save, optional render). Statuses per
source: ok (a live rung served it), archived (a Wayback copy; `archived_at` says when),
paywalled (never read around), blocked (every rung failed on a 401/403/429/challenge), gone
(404/410 live and no archive copy), robots_disallowed (robots.txt forbids our UA and the archive
has no copy), text_unavailable (got it, but a PDF with no pdftotext here or no text layer),
error (anything else). `via`, `archived_at` and every attempt are stored per source.
Contact details (responsible offices, help lines) are checked against live copies only: a
phone number on a street flyer has to be current, and an archive copy can be years old.
Claim results: verified, quote_missing, value_missing, unverifiable (no text to check).

Tables (created here):
  local_sources_check        one row per (url, county_fips): status, http, claims verified
  local_sources_claim        one row per claim and its latest result
  local_sources_responsible  one row per responsible office and which contact fields verified
  local_sources_help         one row per service line for street flyers (phone/address verified)
Core tables: `doc` (one row per cited source, keyed on url) and `metric`
(entity_type='county', metric='local_<metric>', verified claims only).

Run:  python3 -m ingest.domains.local_sources [--county 42049] [--selftest]
"""
from __future__ import annotations

import argparse
import json
import datetime as dt
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

import yaml

from ingest import fetch_ladder
from ingest.common import ROOT, clean, now, selftest_report, source_id_for, upsert

DOMAIN = "local_sources"
RESEARCH = ROOT / "research" / "county"
CHECK_MD = RESEARCH / "CHECK.md"
KINDS = {"report", "audit", "study", "article", "dataset", "court", "program", "plan", "org"}
METRIC_RX = re.compile(r"^[a-z][a-z0-9_]*$")
FRONT_RX = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)

DDL = """
CREATE TABLE IF NOT EXISTS local_sources_check (
  url TEXT, county_fips TEXT, status TEXT, http INTEGER, checked_at TEXT, title_found TEXT,
  claims_total INTEGER, claims_verified INTEGER, via TEXT, archived_at TEXT, attempts_json TEXT,
  PRIMARY KEY(url, county_fips));
CREATE TABLE IF NOT EXISTS local_sources_claim (
  county_fips TEXT, url TEXT, metric TEXT, year INTEGER, value REAL, quote TEXT, result TEXT,
  checked_at TEXT, PRIMARY KEY(county_fips, url, metric, year));
CREATE TABLE IF NOT EXISTS local_sources_responsible (
  county_fips TEXT, office TEXT, role TEXT, name TEXT, title TEXT, phone TEXT, email TEXT,
  address TEXT, url TEXT, elected INTEGER, for_keys TEXT, name_ok INTEGER, phone_ok INTEGER,
  email_ok INTEGER, address_ok INTEGER, http INTEGER, checked_at TEXT,
  PRIMARY KEY(county_fips, office, role));
CREATE TABLE IF NOT EXISTS local_sources_help (
  county_fips TEXT, name TEXT, what TEXT, phone TEXT, address TEXT, hours TEXT, url TEXT,
  phone_ok INTEGER, address_ok INTEGER, http INTEGER, checked_at TEXT,
  PRIMARY KEY(county_fips, name));
"""


# --------------------------------------------------------------------------- research files
def research_files(root: Path = RESEARCH) -> list[Path]:
    return sorted(p for p in root.glob("**/[0-9]*.md") if p.is_file())


def _iso(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return clean(v) if v is not None else None


def _str(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def parse_file(path: Path):
    """Return (entry or None, problems). Problems never raise; a bad entry is skipped."""
    probs = []
    rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    m = FRONT_RX.match(path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None, [f"{rel}: no YAML front matter (--- ... ---) at the top"]
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        return None, [f"{rel}: front matter is not valid YAML: {str(e)[:200]}"]
    if not isinstance(meta, dict):
        return None, [f"{rel}: front matter is not a mapping"]

    fips = meta.get("county_fips")
    if not isinstance(fips, str) or not re.fullmatch(r"\d{5}", fips):
        # unquoted 01001 is read by YAML as an octal int, so we insist on a quoted string
        return None, [f"{rel}: county_fips must be a quoted 5-digit string, got {fips!r}"]
    county, state = _str(meta.get("county")), _str(meta.get("state"))
    if not county:
        return None, [f"{rel}: county is required"]
    if not state or not re.fullmatch(r"[A-Z]{2}", state):
        return None, [f"{rel}: state must be a 2-letter postal code, got {meta.get('state')!r}"]
    if path.parent.name != state or not path.name.startswith(fips + "-"):
        probs.append(f"{rel}: file should be research/county/{state}/{fips}-<slug>.md")
    coc = meta.get("coc") or []
    if not isinstance(coc, list):
        probs.append(f"{rel}: coc should be a list like [PA-605]")
        coc = []

    sources, seen_claims = [], set()
    raw_sources = meta.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        return None, probs + [f"{rel}: sources must be a non-empty list"]
    for i, s in enumerate(raw_sources, 1):
        where = f"{rel}: sources[{i}]"
        if not isinstance(s, dict):
            probs.append(f"{where}: not a mapping, skipped")
            continue
        url = _str(s.get("url"))
        if not url or not re.match(r"https?://[^/\s]+", url):
            probs.append(f"{where}: url must be http(s), skipped")
            continue
        kind = _str(s.get("kind"))
        if kind not in KINDS:
            probs.append(f"{where}: kind {kind!r} not one of {sorted(KINDS)}, skipped")
            continue
        title, publisher = _str(s.get("title")), _str(s.get("publisher"))
        if not title or not publisher:
            probs.append(f"{where}: title and publisher are required, skipped")
            continue
        claims = []
        for j, c in enumerate(s.get("claims") or [], 1):
            cw = f"{where}.claims[{j}]"
            if not isinstance(c, dict):
                probs.append(f"{cw}: not a mapping, skipped")
                continue
            metric, value, year, quote = c.get("metric"), c.get("value"), c.get("year"), c.get("quote")
            if not isinstance(metric, str) or not METRIC_RX.match(metric):
                probs.append(f"{cw}: metric must be snake_case, skipped")
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                probs.append(f"{cw}: value must be a number, skipped")
                continue
            if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100:
                probs.append(f"{cw}: year must be a 4-digit integer, skipped")
                continue
            if not isinstance(quote, (str, int, float)) or not str(quote).strip():
                probs.append(f"{cw}: quote is required (short, exact text from the document), skipped")
                continue
            key = (url, metric, year)
            if key in seen_claims:
                probs.append(f"{cw}: duplicate claim {metric}/{year} for this url, skipped")
                continue
            seen_claims.add(key)
            claims.append({"metric": metric, "value": float(value), "year": year,
                           "unit": _str(c.get("unit")), "quote": str(quote).strip()})
        sources.append({"url": url, "kind": kind, "title": title, "publisher": publisher,
                        "published": _iso(s.get("published")), "claims": claims})

    responsible, seen_roles = [], set()
    for i, r in enumerate(meta.get("responsible") or [], 1):
        where = f"{rel}: responsible[{i}]"
        if not isinstance(r, dict):
            probs.append(f"{where}: not a mapping, skipped")
            continue
        office, role, url = _str(r.get("office")), _str(r.get("role")), _str(r.get("url"))
        if not office or not role:
            probs.append(f"{where}: office and role are required, skipped")
            continue
        if not url or not re.match(r"https?://[^/\s]+", url):
            probs.append(f"{where}: url (the official page publishing these details) is required, skipped")
            continue
        if (office, role) in seen_roles:
            probs.append(f"{where}: duplicate office+role, skipped")
            continue
        phone = r.get("phone")
        if phone is not None and not isinstance(phone, str):
            probs.append(f"{where}: quote the phone number as a string")
            phone = str(phone)
        fors = r.get("for") or []
        if isinstance(fors, str):
            fors = [fors]
        seen_roles.add((office, role))
        responsible.append({"office": office, "role": role, "url": url,
                            "name": _str(r.get("name")), "title": _str(r.get("title")),
                            "phone": _str(phone), "email": _str(r.get("email")),
                            "address": _str(r.get("address")),
                            "elected": 1 if r.get("elected") is True else 0,
                            "for_keys": ",".join(str(f) for f in fors) or None})

    helps, seen_help = [], set()
    for i, h in enumerate(meta.get("help") or [], 1):
        where = f"{rel}: help[{i}]"
        if not isinstance(h, dict):
            probs.append(f"{where}: not a mapping, skipped")
            continue
        name, url = _str(h.get("name")), _str(h.get("url"))
        if not name or not url or not re.match(r"https?://[^/\s]+", url):
            probs.append(f"{where}: name and url (the provider's page publishing these details) "
                         "are required, skipped")
            continue
        if name in seen_help:
            probs.append(f"{where}: duplicate name, skipped")
            continue
        phone = h.get("phone")
        if phone is not None and not isinstance(phone, str):
            probs.append(f"{where}: quote the phone number as a string")
            phone = str(phone)
        seen_help.add(name)
        helps.append({"name": name, "url": url, "what": _str(h.get("what")), "phone": _str(phone),
                      "address": _str(h.get("address")), "hours": _str(h.get("hours"))})

    entry = {"path": str(rel), "fips": fips, "county": county, "state": state,
             "coc": [str(c) for c in coc], "updated": _iso(meta.get("updated")),
             "sources": sources, "responsible": responsible, "help": helps}
    return entry, probs


# --------------------------------------------------------------------------- fetching
def get(url: str) -> fetch_ladder.Result:
    """Climb the fetch ladder (ingest/fetch_ladder.py) afresh: re-verification means re-fetching."""
    return fetch_ladder.get(url, domain=DOMAIN, force=True)


# --------------------------------------------------------------------------- text
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "table", "ul", "ol", "header", "footer", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.depth, self.title, self._in_title = [], [], 0, "", False
        self.meta, self._ld, self._in_ld = [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.title:
            self._in_title = True       # the document title only, not an <svg><title>
        a = dict(attrs)
        if tag == "meta" and (a.get("name") or a.get("property") or "").lower() in (
                "description", "og:description", "og:title", "twitter:description"):
            self.meta.append(a.get("content") or "")
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld = True          # publishers declare the article text here on JS-built pages
        if tag in self.SKIP:
            self.depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().startswith(("mailto:", "tel:")):
                # offices often publish an address only as a link target
                self.links.append(href.split(":", 1)[1].split("?", 1)[0])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_ld = False
        if tag in self.SKIP and self.depth:
            self.depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_ld:
            self._ld.append(data)
        if self._in_title:
            self.title += data
        elif not self.depth:
            self.parts.append(data)


def _decode(body: bytes) -> str:
    m = re.search(rb"""charset=["']?([A-Za-z0-9_\-]+)""", body[:4096])
    for enc in ([m.group(1).decode("ascii", "ignore")] if m else []) + ["utf-8"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def extract(body: bytes):
    """Return (text or None, title_found, why_not)."""
    if body.lstrip()[:5] == b"%PDF-":
        exe = shutil.which("pdftotext")
        if not exe:
            return None, None, "text_unavailable (PDF and no pdftotext on this machine)"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "doc.pdf"
            p.write_bytes(body)
            r = subprocess.run([exe, "-q", "-enc", "UTF-8", str(p), "-"], capture_output=True,
                               timeout=180)
            title = None
            info = shutil.which("pdfinfo")
            if info:
                i = subprocess.run([info, str(p)], capture_output=True, timeout=60)
                m = re.search(r"^Title:[ \t]*(\S.*)$", i.stdout.decode("utf-8", "replace"), re.M)
                title = clean(m.group(1)) if m else None
        text = r.stdout.decode("utf-8", "replace")
        return (text if text.strip() else None), title, (None if text.strip() else "PDF has no text layer")
    t = _Text()
    t.feed(_decode(body))
    t.close()
    # the document's own title, description and declared article text count as its text
    extra = [t.title] + t.meta + _ld_text(t._ld)
    text = "".join(t.parts) + "\n" + "\n".join(t.links) + "\n" + "\n".join(x for x in extra if x)
    return text, clean(t.title), None


def _ld_text(blocks) -> list[str]:
    out = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("articleBody", "description", "headline", "text") and isinstance(v, str):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for b in blocks:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return out


_TR: dict[int, str | None] = {ord(c): "'" for c in "\u2018\u2019\u201a\u201b\u2032\u0060\u00b4"}
_TR.update({ord(c): '"' for c in "\u201c\u201d\u201e\u201f\u2033\u00ab\u00bb"})
_TR.update({ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"})
_TR[0x00AD] = None   # soft hyphen


def normalize(s: str) -> str:
    """Lowercase, unify quotes and dashes, drop thousands separators, collapse whitespace."""
    s = unicodedata.normalize("NFKC", s).translate(_TR).lower()
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)
    return re.sub(r"\s+", " ", s).strip()


def value_in(value: float, ntext: str) -> bool:
    if float(value).is_integer():
        pat = rf"{int(value)}(?:\.0+)?(?!\.?\d)"
    else:
        pat = re.escape(repr(float(value)).rstrip("0")) + r"0*(?!\d)"
    return re.search(r"(?<![\d.])" + pat, ntext) is not None


def check_claim(claim, ntext):
    if ntext is None:
        return "unverifiable"
    if normalize(claim["quote"]) not in ntext:
        return "quote_missing"
    if not value_in(claim["value"], ntext):
        return "value_missing"
    return "verified"


_DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"}
PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[\s.\-]*)?\(?(\d{3})\)?[\s.\-]*(\d{3})[\s.\-]*(\d{4})(?!\d)")


def _digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def check_contact(r, ntext):
    """1/0 per published field (None when the field is absent or there is no text)."""
    def ok(field, test):
        if r.get(field) is None or ntext is None:
            return None
        return 1 if test(r[field]) else 0

    def phone_ok(p):
        want = _digits(p)
        return any("".join(m.groups()) == want for m in PHONE_RX.finditer(ntext))

    def address_ok(a):
        words = re.findall(r"[a-z0-9]+", normalize(a))
        if not words or not words[0].isdigit():
            return normalize(a) in ntext
        street = next((w for w in words[1:] if w not in _DIRECTIONS), None)
        if street is None:
            return False
        return re.search(rf"(?<!\d){words[0]}\b[^\d]{{0,25}}?\b{re.escape(street)}\b", ntext) is not None

    return {"name_ok": ok("name", lambda v: normalize(v) in ntext),
            "phone_ok": ok("phone", phone_ok),
            "email_ok": ok("email", lambda v: v.lower() in ntext),
            "address_ok": ok("address", address_ok)}


# --------------------------------------------------------------------------- run
def run(conn: sqlite3.Connection, cfg: dict) -> dict:
    conn.executescript(DDL)
    have = {r[1] for r in conn.execute("PRAGMA table_info(local_sources_check)")}
    for col in ("via", "archived_at", "attempts_json"):       # tables made before the ladder
        if col not in have:
            conn.execute(f"ALTER TABLE local_sources_check ADD COLUMN {col} TEXT")
    only = cfg.get("county")
    entries, problems = [], []
    for p in research_files():
        try:
            e, probs = parse_file(p)
        except Exception as ex:  # noqa: BLE001  one broken file never stops the run
            e, probs = None, [f"{p}: could not be read: {ex!r}"[:300]]
        problems += probs
        if e:
            entries.append(e)
    all_fips = [e["fips"] for e in entries]
    if only:
        entries = [e for e in entries if e["fips"] == str(only)]

    pages: dict[str, tuple] = {}      # url -> (ladder result, normalized text, title, why)

    def page(url):
        if url not in pages:
            res = get(url)
            text = title = why = None
            if res.ok and res.body is not None:
                text, title, why = extract(res.body)
            pages[url] = (res, normalize(text) if text else None, title, why)
        return pages[url]

    def live_text(url):
        res, ntext, _t, _w = page(url)
        return res, (ntext if res.status == "ok" else None)

    rows = 0
    sources_seen = []
    for e in entries:
        for s in e["sources"]:
            res, ntext, title, why = page(s["url"])
            status = "text_unavailable" if res.ok and ntext is None else res.status
            ts = now()
            sid = source_id_for(conn, s["url"], DOMAIN, title=s["title"], publisher=s["publisher"])
            sources_seen.append((s["url"], "public record cited in research/county"))
            upsert(conn, "doc", {"kind": s["kind"], "title": s["title"], "url": s["url"],
                                 "publisher": s["publisher"], "published": s["published"],
                                 "place": e["county"], "state": e["state"], "summary": None,
                                 "source_id": sid}, keys=["url"])
            rows += 1
            verified = 0
            for c in s["claims"]:
                outcome = check_claim(c, ntext)
                upsert(conn, "local_sources_claim",
                       {"county_fips": e["fips"], "url": s["url"], "metric": c["metric"],
                        "year": c["year"], "value": c["value"], "quote": c["quote"],
                        "result": outcome, "checked_at": ts},
                       keys=["county_fips", "url", "metric", "year"])
                rows += 1
                if outcome == "verified":
                    verified += 1
                    upsert(conn, "metric",
                           {"entity_type": "county", "entity_id": e["fips"],
                            "metric": "local_" + c["metric"], "value": c["value"],
                            "unit": c["unit"], "year": c["year"], "is_est": 0,
                            "note": f'quoted: "{c["quote"]}"'[:200], "source_id": sid},
                           keys=["entity_type", "entity_id", "metric", "year"])
                    rows += 1
            conn.execute(
                """INSERT INTO local_sources_check(url,county_fips,status,http,checked_at,title_found,
                     claims_total,claims_verified,via,archived_at,attempts_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url,county_fips) DO UPDATE SET status=excluded.status,
                     http=excluded.http, checked_at=excluded.checked_at,
                     title_found=excluded.title_found, claims_total=excluded.claims_total,
                     claims_verified=excluded.claims_verified, via=excluded.via,
                     archived_at=excluded.archived_at, attempts_json=excluded.attempts_json""",
                (s["url"], e["fips"], status, res.live_http, ts, title, len(s["claims"]), verified,
                 res.via, res.archived_at, json.dumps([list(x) for x in res.attempts])))
            if why and res.ok:
                problems.append(f"{e['path']}: {s['url']}: {why}")
            rows += 1
        for r in e["responsible"]:
            res, ntext = live_text(r["url"])
            code = res.http if res.status == "ok" else res.live_http
            flags = check_contact(r, ntext)
            conn.execute(
                """INSERT INTO local_sources_responsible(county_fips,office,role,name,title,phone,
                     email,address,url,elected,for_keys,name_ok,phone_ok,email_ok,address_ok,http,
                     checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(county_fips,office,role) DO UPDATE SET name=excluded.name,
                     title=excluded.title, phone=excluded.phone, email=excluded.email,
                     address=excluded.address, url=excluded.url, elected=excluded.elected,
                     for_keys=excluded.for_keys, name_ok=excluded.name_ok,
                     phone_ok=excluded.phone_ok, email_ok=excluded.email_ok,
                     address_ok=excluded.address_ok, http=excluded.http,
                     checked_at=excluded.checked_at""",
                (e["fips"], r["office"], r["role"], r["name"], r["title"], r["phone"], r["email"],
                 r["address"], r["url"], r["elected"], r["for_keys"], flags["name_ok"],
                 flags["phone_ok"], flags["email_ok"], flags["address_ok"], code, now()))
            rows += 1
        for h in e["help"]:
            res, ntext = live_text(h["url"])
            code = res.http if res.status == "ok" else res.live_http
            flags = check_contact(h, ntext)
            conn.execute(
                """INSERT INTO local_sources_help(county_fips,name,what,phone,address,hours,url,
                     phone_ok,address_ok,http,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(county_fips,name) DO UPDATE SET what=excluded.what,
                     phone=excluded.phone, address=excluded.address, hours=excluded.hours,
                     url=excluded.url, phone_ok=excluded.phone_ok, address_ok=excluded.address_ok,
                     http=excluded.http, checked_at=excluded.checked_at""",
                (e["fips"], h["name"], h["what"], h["phone"], h["address"], h["hours"], h["url"],
                 flags["phone_ok"], flags["address_ok"], code, now()))
            rows += 1
    conn.commit()

    report = county_report(conn, entries)
    for line in report:
        print(line)
    for p in problems:
        print("PROBLEM", p)
    write_check_md(conn, all_fips, {e["fips"]: e for e in entries}, problems)
    n_claims = sum(len(s["claims"]) for e in entries for s in e["sources"])
    return {"rows": rows,
            "notes": f"counties={len(entries)} sources={sum(len(e['sources']) for e in entries)} "
                     f"claims={n_claims} problems={len(problems)}",
            "sources": sorted(set(sources_seen))}


# --------------------------------------------------------------------------- reporting
def _summary_rows(conn, fips_list):
    out = []
    for f in fips_list:
        st = dict(conn.execute("SELECT status, COUNT(*) FROM local_sources_check WHERE county_fips=? "
                               "GROUP BY status", (f,)).fetchall())
        cv, ct = conn.execute("SELECT COALESCE(SUM(claims_verified),0), COALESCE(SUM(claims_total),0) "
                              "FROM local_sources_check WHERE county_fips=?", (f,)).fetchone()
        offices = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN COALESCE(name_ok,1)=1 AND COALESCE(phone_ok,1)=1 AND "
            "COALESCE(email_ok,1)=1 AND COALESCE(address_ok,1)=1 AND COALESCE(name_ok,phone_ok,"
            "email_ok,address_ok) IS NOT NULL AND http=200 THEN 1 ELSE 0 END) "
            "FROM local_sources_responsible WHERE county_fips=?", (f,)).fetchone()
        helps = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN COALESCE(phone_ok,1)=1 AND COALESCE(address_ok,1)=1 "
            "AND COALESCE(phone_ok,address_ok) IS NOT NULL AND http=200 THEN 1 ELSE 0 END) "
            "FROM local_sources_help WHERE county_fips=?",
            (f,)).fetchone()
        out.append((f, sum(st.values()), st.get("ok", 0), st.get("blocked", 0), st.get("gone", 0),
                    st.get("robots_disallowed", 0), st.get("error", 0) + st.get("text_unavailable", 0),
                    cv, ct, offices[1] or 0, offices[0] or 0, helps[1] or 0, helps[0] or 0,
                    st.get("archived", 0), st.get("paywalled", 0)))
    return out


def county_report(conn, entries):
    lines = ["", "county  name                      sources  ok  archived  paywalled  blocked  gone  "
                 "robots  error  claims ok/total  offices ok/total  help ok/total"]
    for e, r in zip(entries, _summary_rows(conn, [e["fips"] for e in entries])):
        lines.append(f"{r[0]}   {e['county'][:24]:<24}  {r[1]:>7}  {r[2]:>2}  {r[13]:>8}  "
                     f"{r[14]:>9}  {r[3]:>7}  {r[4]:>4}  "
                     f"{r[5]:>6}  {r[6]:>5}  {r[7]:>7}/{r[8]:<7}  {r[9]:>7}/{r[10]:<7}  {r[11]:>4}/{r[12]}")
    return lines


def _yn(v):
    return "-" if v is None else ("yes" if v else "NO")


def _cell(s):
    return str("" if s is None else s).replace("|", "\\|").replace("\n", " ")


def write_check_md(conn, all_fips, entries_by_fips, problems):
    """research/county/CHECK.md: link health for reviewers, rebuilt from the DB on every run."""
    fips_list = sorted(set(all_fips))
    names = {}
    for p in research_files():
        e, _ = parse_file(p)
        if e:
            names[e["fips"]] = f"{e['county']}, {e['state']}"
    L = ["# County research: link and claim check", "",
         "Generated by `python3 -m ingest.domains.local_sources` at " + now() + ". Do not edit by "
         "hand: rerun the ingestor. It re-fetches every source in `research/county/**`, checks "
         "that each quoted number is printed in the document and that each office's contact "
         "details appear on the official page cited for them.", "",
         "## Summary", "",
         "| county | sources | ok | archived | paywalled | blocked | gone | robots | error or no text "
         "| claims verified | offices fully verified | help lines fully verified |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in _summary_rows(conn, fips_list):
        L.append(f"| {r[0]} {_cell(names.get(r[0], ''))} | {r[1]} | {r[2]} | {r[13]} | {r[14]} | "
                 f"{r[3]} | {r[4]} | "
                 f"{r[5]} | {r[6]} | {r[7]}/{r[8]} | {r[9]}/{r[10]} | {r[11]}/{r[12]} |")
    ph = ",".join("?" * len(fips_list)) or "''"
    L += ["", "## Sources", "",
          "`live http` is what the publisher's server answered first; `copy` is where the text we "
          "checked came from; `attempts` is every rung of the fetch ladder in order.", "",
          "| county | status | copy | live http | claims | title found | url | attempts |",
          "|---|---|---|---|---|---|---|---|"]
    for r in conn.execute(f"SELECT county_fips, status, via, archived_at, http, claims_verified, "
                          f"claims_total, title_found, url, attempts_json FROM local_sources_check "
                          f"WHERE county_fips IN ({ph}) ORDER BY county_fips, status, url", fips_list):
        via, arch = r[2], r[3]
        if arch:
            copy = f"Wayback copy, {arch[:10]}" + (" (fresh save)" if via == "wayback_fresh" else "")
        elif via:
            copy = "live" if via == "direct" else f"live ({via})"
        else:
            copy = "none"
        tries = fetch_ladder.attempts_summary(json.loads(r[9] or "[]"))
        L.append(f"| {r[0]} | {r[1]} | {copy} | {_cell(r[4])} | {r[5]}/{r[6]} | "
                 f"{_cell(r[7])[:80]} | {r[8]} | {_cell(tries)} |")
    bad = conn.execute(f"SELECT county_fips, metric, year, value, result, url FROM local_sources_claim "
                       f"WHERE county_fips IN ({ph}) AND result != 'verified' "
                       "ORDER BY county_fips, metric", fips_list).fetchall()
    L += ["", "## Claims not verified", ""]
    if bad:
        L += ["| county | metric | year | value | result | url |", "|---|---|---|---|---|---|"]
        L += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]:g} | {r[4]} | {r[5]} |" for r in bad]
    else:
        L.append("None.")
    L += ["", "## Responsible offices", "",
          "| county | office | role | elected | name | phone | email | address | http | url |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in conn.execute(f"SELECT county_fips, office, role, elected, name_ok, phone_ok, email_ok, "
                          f"address_ok, http, url FROM local_sources_responsible WHERE county_fips "
                          f"IN ({ph}) ORDER BY county_fips, office", fips_list):
        L.append(f"| {r[0]} | {_cell(r[1])} | {_cell(r[2])} | {'yes' if r[3] else 'no'} | "
                 f"{_yn(r[4])} | {_yn(r[5])} | {_yn(r[6])} | {_yn(r[7])} | {_cell(r[8])} | {r[9]} |")
    L += ["", "## Help lines (street flyers)", "",
          "| county | name | phone | address | http | url |", "|---|---|---|---|---|---|"]
    for r in conn.execute(f"SELECT county_fips, name, phone_ok, address_ok, http, url FROM "
                          f"local_sources_help WHERE county_fips IN ({ph}) ORDER BY county_fips, name",
                          fips_list):
        L.append(f"| {r[0]} | {_cell(r[1])} | {_yn(r[2])} | {_yn(r[3])} | {_cell(r[4])} | {r[5]} |")
    L += ["", "## Problems in research files", ""]
    L += [f"- {_cell(p)}" for p in problems] or ["None."]
    CHECK_MD.parent.mkdir(parents=True, exist_ok=True)
    CHECK_MD.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    from ingest.common import DB, main
    ap = argparse.ArgumentParser(description="Re-verify research/county sources and claims.")
    ap.add_argument("--county", help="5-digit FIPS: only this county")
    ap.add_argument("--selftest", action="store_true", help="also print row counts and 3 samples")
    a = ap.parse_args()
    main(DOMAIN, lambda c, _cfg: run(c, {"county": a.county}),
         "local_sources_check" if a.selftest else None)
    if a.selftest:
        ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        selftest_report(ro, DOMAIN, "local_sources_claim")
        selftest_report(ro, DOMAIN, "local_sources_responsible")
        selftest_report(ro, DOMAIN, "local_sources_help")
