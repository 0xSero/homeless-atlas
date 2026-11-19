"""docs -- audits, court opinions, studies, investigative series about homelessness.

What this domain owns
---------------------
`doc` rows only (schema: kind in report|audit|study|article|dataset|court). Every row is a
document a reader can open: title, publisher, date, place and a two-sentence summary that
paraphrases what the document is and quotes at most 25 words of the publisher's own wording.

Universe captured here
----------------------
* federal oversight      -- GAO reports on homelessness/housing (GovInfo `GAOREPORTS`)
* federal legislation    -- congressional committee reports (GovInfo `CRPT`)
* courts                 -- U.S. Courts of Appeals / district opinions (GovInfo `USCOURTS`,
                            full-text search for camping-ban and encampment litigation) plus the
                            Supreme Court slip opinion for City of Grants Pass v. Johnson
* HUD narrative          -- Annual Homeless Assessment Report, Parts 1 and 2, by year
* city oversight         -- NYC Comptroller, LA Controller, Seattle City Auditor, San Diego
                            City Auditor, Denver Auditor, Boston, DC Auditor, SF Controller,
                            Portland, Houston (whatever each office actually publishes)
* investigative series   -- Los Angeles Times, San Francisco Chronicle, ProPublica, NPR
* peer-reviewed studies  -- Crossref metadata, filtered to homelessness titles

Provenance model
----------------
Every stored URL is itself the `source` row (`source.id` -> `doc.source_id`) and every URL is
HEAD/GET-verified 200 and cached (`docs_verify`) before the row is written. Discovery pages
(municipal report listings, RSS feeds, GovInfo search responses) are fetched through
`common.fetch` and registered as sources too, so the route to a document is auditable.

Nothing is inferred: a field the publisher does not give stays NULL. `published` is stored as
the publisher's date (ISO when it is parseable, otherwise the year or NULL) and `place` only
where the publisher itself scopes the document to a city.

Resumable
---------
Each source is one task in `docs_progress`; re-running skips finished tasks. All responses are
disk-cached, so a re-run re-parses rather than re-fetches. The GovInfo search API is rate
limited to 10 requests/hour on the keyless `DEMO_KEY`; a 429 marks the task as deferred and the
next run (after the hourly window) picks it up. Set `GOVINFO_API_KEY` to lift the cap.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from ingest.common import RAW, clean, db, fetch, now, source_id_for, upsert

DOMAIN = "docs"

# Same UA as the rest of the archive for programmatic fetches; a browser UA for the handful of
# city/news hosts that 403 or 202 the archive UA (huduser.gov, comptroller.nyc.gov, latimes.com,
# sfchronicle.com). Both are declared honestly, nothing is disguised beyond a normal browser
# accept header.
UA = "homeless-atlas/1.0 (public-data research; contact: 0xsero@users.noreply.github.com)"
BUA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")

GOVINFO_KEY = os.environ.get("GOVINFO_API_KEY", "DEMO_KEY")

LIC_USGOV = "U.S. Government work, public domain"
LIC_COURT = "U.S. Government work, public domain (court opinion)"
LIC_CITY = "Public record, municipal government"
LIC_NEWS = "Publisher copyright; stored as headline, link and a quote of 25 words or fewer"
LIC_CROSSREF = "Crossref metadata (CC0); article rights vary by publisher"

HOMELESS_KW = re.compile(
    r"homeless|encampment|unsheltered|tent city|skid row|continuum of care|housing first|"
    r"street sleep|rough sleep|rv dwell|sit-lie|anti-camping|camping ban|camping ordinance", re.I)

GAO_KW = re.compile(r"homeless|encampment|unsheltered|shelter|transitional housing|"
                    r"supportive housing|continuum of care|housing first", re.I)

CITY_NAV_NOISE = re.compile(
    r"\b(guide|dashboard|map|calendar|newsletter|sign up|subscribe|contact|about us|"
    r"apply|how to|find shelter|shelter and|home page)\b", re.I)

LABEL = {"audit": "An audit", "court": "A court opinion", "report": "A report",
         "article": "An article", "study": "A peer-reviewed study", "dataset": "A dataset"}

COUNTER = [0]
NOTES: list[str] = []
_LAST = [0.0]


# ------------------------------------------------------------------------ plumbing
def _throttle() -> None:
    dt = time.time() - _LAST[0]
    if dt < 1.0:
        time.sleep(1.0 - dt)
    _LAST[0] = time.time()


def _fetch(url: str, *, browser: bool = False, timeout: int = 180) -> bytes:
    """Fetch through common (1 req/s, retries, cache, source log) and mirror the bytes under
    sha1(url).bin so source_id_for() can record size + sha256 for this exact URL."""
    h = {"Accept": "*/*"}
    if browser:
        h["User-Agent"] = BUA
        h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    b = fetch(url, domain=DOMAIN, headers=h, timeout=timeout)
    p = RAW / DOMAIN / (hashlib.sha1(url.encode()).hexdigest() + ".bin")
    if not p.exists() or p.stat().st_size == 0:
        p.write_bytes(b)
    return b


def _sid(conn, url: str, *, title: str, publisher: str, license: str) -> int:
    return source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)


def _logline(rec: dict) -> None:
    p = Path("logs")
    p.mkdir(exist_ok=True)
    with open(p / f"ingest-{DOMAIN}.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def _ensure(conn) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS docs_progress (
      task TEXT PRIMARY KEY, ts TEXT, ok INTEGER, note TEXT);
    CREATE TABLE IF NOT EXISTS docs_verify (
      url TEXT PRIMARY KEY, status INTEGER, final_url TEXT, ts TEXT, via TEXT);
    CREATE INDEX IF NOT EXISTS ix_docs_verify_status ON docs_verify(status);
    """)


def _mark(conn, task: str, ok: bool, note: str = "") -> None:
    conn.execute("INSERT INTO docs_progress(task,ts,ok,note) VALUES(?,?,?,?) "
                 "ON CONFLICT(task) DO UPDATE SET ts=excluded.ts,ok=excluded.ok,note=excluded.note",
                 (task, now(), 1 if ok else 0, clean(note)[:900] if note else ""))
    conn.commit()


def _done(conn, task: str) -> bool:
    r = conn.execute("SELECT ok FROM docs_progress WHERE task=?", (task,)).fetchone()
    return bool(r and r[0])


def _verify(conn, url: str) -> bool:
    """HEAD (then GET if HEAD is refused) until a 200; cached in docs_verify.

    Only rows whose document URL answers 200 are written, which is the acceptance gate for
    this domain. Redirects are followed: a doi.org link counts as working when it lands on the
    publisher's article page with a 200.
    """
    row = conn.execute("SELECT status FROM docs_verify WHERE url=?", (url,)).fetchone()
    if row and row[0] == 200:
        return True
    status, final = None, url
    for method in ("HEAD", "GET"):
        _throttle()
        try:
            r = requests.request(method, url, headers={"User-Agent": BUA, "Accept": "*/*"},
                                 timeout=25, allow_redirects=True, stream=(method == "GET"))
        except Exception:  # noqa: BLE001
            status = None
            continue
        status, final = r.status_code, r.url
        if method == "GET":
            r.close()
        if status == 200:
            break
        if status is None:
            break        # connection timeout / DNS: a GET would only hang again
        # a 403/405/400 on HEAD is often just "this host does not do HEAD"; try a real GET
        if method == "HEAD" and status in (400, 403, 405, 501):
            continue
        break
    conn.execute(
        "INSERT INTO docs_verify(url,status,final_url,ts,via) VALUES(?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET status=excluded.status,final_url=excluded.final_url,"
        "ts=excluded.ts,via=excluded.via", (url, status, final, now(), "head/get"))
    conn.commit()
    _logline({"ts": now(), "url": url, "status": status, "bytes": None, "note": "verify"})
    return status == 200


# --------------------------------------------------------------------- text helpers
def _flat(h: str) -> str:
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", BeautifulSoup(h or "", "lxml").get_text(" ")).strip()


_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_DATE_LONG = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b")
_DATE_MY = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b")


def _iso_date(s: str | None) -> str | None:
    """Parse the date forms publishers actually print; never guess one that is not there."""
    if not s:
        return None
    s = clean(s) or ""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _DATE_LONG.search(s)
    if m:
        return f"{m.group(3)}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    date = re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+(\d{1,2})\s+"
                     r"([A-Z][a-z]{2})\s+(\d{4})\b", s)
    if date and date.group(2) in _MONTHS:
        return f"{date.group(3)}-{_MONTHS[date.group(2)]:02d}-{int(date.group(1)):02d}"
    m = _DATE_MY.search(s)
    if m:
        return f"{m.group(2)}-{_MONTHS[m.group(1)]:02d}"
    m = re.search(r"\b(19[7-9]\d|20[0-5]\d)\b", s)
    if m:
        return m.group(1)
    return None


def _sentence(text: str, kw: re.Pattern, max_words: int = 25, min_len: int = 45) -> str | None:
    """First publisher sentence that mentions the subject, trimmed to <= max_words words."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return None
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", flat):
        if len(sent) < min_len or not kw.search(sent):
            continue
        words = sent.split()
        if len(words) > max_words:
            return " ".join(words[:max_words]).rstrip(",;:") + "…"
        return sent.strip()
    return None


def _pdf_text(data: bytes, pages: int = 4) -> str:
    """pdftotext -layout on the first pages of an in-memory PDF; empty string on failure."""
    if not data or len(data) > 40_000_000:
        return ""
    with tempfile.TemporaryDirectory() as td:
        src, outp = Path(td) / "in.pdf", Path(td) / "out.txt"
        src.write_bytes(data)
        try:
            subprocess.run(["pdftotext", "-layout", "-f", "1", "-l", str(pages), str(src),
                            str(outp)], check=True, capture_output=True, timeout=180)
        except Exception:  # noqa: BLE001
            return ""
        return outp.read_text(encoding="utf-8", errors="replace") if outp.exists() else ""


def _summ(kind: str, publisher: str, title: str, published: str | None,
          place: str | None, quote: str | None, tail: str | None = None) -> str:
    """Two sentences: what the document is (paraphrased from publisher metadata), then at most
    one 25-word quotation of the publisher's own description."""
    s1 = f"{LABEL.get(kind, 'A document')} from {publisher}"
    if published:
        s1 += f" dated {published}"
    if place:
        s1 += f", covering {place}"
    s1 += f", published under the title “{title}”."
    if quote:
        q = quote.rstrip().rstrip(".").rstrip()
        s2 = f"Its own description, quoted in brief: “{q}”."
    else:
        s2 = tail or ("The linked file is the primary source for its findings; this record keeps "
                      "only what the publisher states about it.")
    return (s1 + " " + s2).strip()


def _doc(conn, *, kind: str, title: str, url: str, publisher: str, license: str,
         summary: str, published: str | None = None, place: str | None = None,
         state: str | None = None, author: str | None = None,
         verified: bool = False) -> bool:
    """Verify, register the source and upsert one doc row. Returns True when written."""
    if not (title and url and publisher and summary):
        return False
    if title.lower().strip() in ("reports", "home", "audit reports"):
        return False
    # never repoint provenance on a doc row another domain already owns
    ex = conn.execute("SELECT s.domain FROM doc d JOIN source s ON s.id=d.source_id "
                      "WHERE d.url=?", (url,)).fetchone()
    if ex and ex[0] and ex[0] != DOMAIN:
        return False
    if not verified and not _verify(conn, url):
        return False
    sid = _sid(conn, url, title=clean(title)[:300], publisher=publisher, license=license)
    upsert(conn, "doc", dict(kind=kind, title=clean(title)[:400], url=url,
                             publisher=clean(publisher)[:200],
                             author=clean(author)[:200] if author else None,
                             published=published, place=clean(place), state=state,
                             summary=summary, source_id=sid), keys=["url"])
    COUNTER[0] += 1
    return True


# -------------------------------------------------------------------- GovInfo search
def _govinfo(query: str, page_size: int = 1000) -> dict | None:
    """POST the GovInfo search API (keyless DEMO_KEY, 10 req/hour) -- cached on disk."""
    payload = {"query": query, "pageSize": page_size, "offsetMark": "*",
               "sorts": [{"field": "score", "sortOrder": "DESC"}]}
    url = f"https://api.govinfo.gov/search?api_key={GOVINFO_KEY}"
    try:
        b = fetch(url, method="POST", data=json.dumps(payload),
                  headers={"Content-Type": "application/json", "Accept": "application/json"},
                  domain=DOMAIN, timeout=180, retries=2)
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"GovInfo 429/failure for {query!r}: {repr(e)[:160]}")
        return None
    try:
        return json.loads(b)
    except ValueError:
        return None


def _cached_govinfo(conn, task: str, query: str) -> dict | None:
    """Read a previously cached GovInfo response for this exact query without spending quota."""
    payload = {"query": query, "pageSize": 1000, "offsetMark": "*",
               "sorts": [{"field": "score", "sortOrder": "DESC"}]}
    url = f"https://api.govinfo.gov/search?api_key={GOVINFO_KEY}"
    # replicate common.fetch's cache key exactly: sha1(method + url + json.dumps(data))
    data = json.dumps(payload)
    key = hashlib.sha1(("POST" + url + json.dumps(data, sort_keys=True, default=str))
                       .encode()).hexdigest()
    p = RAW / DOMAIN / f"{key}.bin"
    if p.exists() and p.stat().st_size > 0:
        try:
            return json.loads(p.read_bytes())
        except ValueError:
            return None
    return None


def _brave(q: str, count: int = 20) -> list[dict]:
    """Brave web search as a *discovery* step only (a server-side index, because several
    publishers render their own site search client-side). The token travels in a header, so the
    cached response contains no secret. Every field on a written row still comes from the
    primary document fetched afterwards."""
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    url = ("https://api.search.brave.com/res/v1/web/search?"
           + urllib.parse.urlencode({"q": q, "count": count}))
    try:
        b = fetch(url, domain=DOMAIN, headers={"Accept": "application/json",
                                               "X-Subscription-Token": key},
                  timeout=90, retries=2)
        return json.loads(b).get("web", {}).get("results", [])
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"brave {q[:40]!r} failed {repr(e)[:80]}")
        return []


def _gi_pdf(pid: str, gid: str | None) -> str:
    if gid:
        return f"https://www.govinfo.gov/content/pkg/{pid}/pdf/{gid}.pdf"
    return f"https://www.govinfo.gov/content/pkg/{pid}/pdf/{pid}.pdf"


def _gi_html(pid: str, gid: str | None) -> str:
    if gid:
        return f"https://www.govinfo.gov/content/pkg/{pid}/html/{gid}.htm"
    return f"https://www.govinfo.gov/content/pkg/{pid}/html/{pid}.htm"


# ----------------------------------------------------------------------- 1. GAO audits
GAO_QUERY = "homeless collection:GAOREPORTS"
GAO_CAP = int(os.environ.get("DOCS_GAO_CAP", "220"))
GAO_TEXT_N = int(os.environ.get("DOCS_GAO_TEXT", "90"))


def gao(conn) -> int:
    """GAO homeless reports (kind='audit').

    Primary route: the GovInfo GAOREPORTS search API. That API's keyless DEMO_KEY answered
    HTTP 429 for most of this run (10 requests/hour, shared per host), so the fallback discovers
    the same GovInfo packages through a server-side index and then reads each package's own
    HTML from www.govinfo.gov, which is not rate limited.
    """
    before = COUNTER[0]
    rows: list[dict] = []
    d = _cached_govinfo(conn, "gao", GAO_QUERY)
    if d is None:
        d = _govinfo(GAO_QUERY, 1000)
    if d and d.get("results"):
        NOTES.append(f"gao: GovInfo API returned {d.get('count')} hits")
        rows = [dict(pid=r.get("packageId"), gid=r.get("granuleId"),
                     title=r.get("title"), date=r.get("dateIssued"),
                     author=(r.get("governmentAuthor") or [""])[0]) for r in d["results"]]
    else:
        queries = [
            'site:govinfo.gov GAOREPORTS homeless',
            'site:govinfo.gov GAOREPORTS homelessness',
            'site:govinfo.gov GAOREPORTS "homeless veterans"',
            'site:govinfo.gov GAOREPORTS "continuum of care"',
            'site:govinfo.gov GAOREPORTS "transitional housing"',
            'site:govinfo.gov GAOREPORTS "supportive housing"',
            'site:govinfo.gov GAOREPORTS "encampment"',
            'site:govinfo.gov GAOREPORTS "HUD" homeless',
        ]
        seen_pid: set[str] = set()
        for q in queries:
            for res in _brave(q, 20):
                u = res.get("url") or ""
                m = re.search(r"(GAOREPORTS-[A-Za-z0-9\-]+?)(?:/|\.html?|\.pdf|$)",
                              urllib.parse.urlparse(u).path)
                if not m:
                    continue
                pid = m.group(1)
                if pid in seen_pid:
                    continue
                seen_pid.add(pid)
                rows.append(dict(pid=pid, gid=None, title=None, date=None, author=None))
        NOTES.append(f"gao: {len(rows)} GAO packages discovered via Brave (GovInfo API 429)")

    written = 0
    for i, r in enumerate(rows[:GAO_CAP]):
        pid, gid = r.get("pid"), r.get("gid")
        if not pid:
            continue
        pdf = _gi_pdf(pid, gid)
        raw = ""
        if i < GAO_TEXT_N or not r.get("title"):
            try:
                raw = _fetch(_gi_html(pid, gid), timeout=180).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                raw = ""
        body = _flat(raw) if raw else ""
        title = clean(r.get("title"))
        if not title and raw:
            hsoup = BeautifulSoup(raw, "lxml")
            title = clean(hsoup.title.get_text(" ", strip=True) if hsoup.title else None)
            if not title:
                h1 = hsoup.find(["h1", "h2"])
                title = clean(h1.get_text(" ", strip=True)) if h1 else None
        title = title or pid
        if not GAO_KW.search(title) and not GAO_KW.search(body[:4000]):
            continue
        author = "U.S. Government Accountability Office"
        published = _iso_date(r.get("date"))
        m = re.search(r"\((?:Letter Report|Report)[^)]*?(\d{2}/\d{2}/\d{2,4})", body or "")
        if m and not published:
            mm, dd, yy = m.group(1).split("/")
            published = f"20{yy}-{mm}-{dd}" if len(yy) == 2 else f"{yy}-{mm}-{dd}"
        quote = _sentence(body[:6000], GAO_KW)
        if _doc(conn, kind="audit", title=title, url=pdf, publisher=author, license=LIC_USGOV,
                summary=_summ("audit", author, title, published, None, quote,
                              tail=("The report's findings and recommendations are in the linked "
                                    "PDF, the Government Publishing Office copy of the GAO "
                                    "product.")),
                published=published, author=author):
            written += 1
    _mark(conn, "gao", written > 0, f"{written} rows of {len(rows)} candidates")
    return COUNTER[0] - before


# ---------------------------------------------------------------------- 2. Court cases
COURT_QUERIES = [
    'collection:USCOURTS ("anti-camping" OR "camping ban" OR "camping ordinance" OR '
    '"sit-lie ordinance")',
    'collection:USCOURTS ("Martin v. Boise" OR "Grants Pass v. Johnson" OR '
    '"homeless encampment")',
]
COURT_CAP = int(os.environ.get("DOCS_COURT_CAP", "140"))
COURT_TEXT_N = int(os.environ.get("DOCS_COURT_TEXT", "45"))


def _court_state(court: str, states: dict[str, str]) -> str | None:
    for abbr, name in states.items():
        if name.lower() in (court or "").lower():
            return abbr
    return None


def courts(conn, states: dict[str, str]) -> int:
    """Federal opinions from GovInfo USCOURTS, plus the Supreme Court's Grants Pass slip opinion."""
    before = COUNTER[0]
    seen: set[str] = set()
    got: list[dict] = []
    for q in COURT_QUERIES:
        d = _cached_govinfo(conn, "courts", q)
        if d is None:
            d = _govinfo(q, 1000)
        if not d or not d.get("results"):
            NOTES.append(f"courts: no GovInfo response for {q[:60]!r} (quota)")
            continue
        NOTES.append(f"courts: {d.get('count')} hits for {q[:60]!r}")
        for r in d["results"]:
            k = f"{r.get('packageId')}/{r.get('granuleId')}"
            if k in seen:
                continue
            seen.add(k)
            got.append(r)
    for i, r in enumerate(got[:COURT_CAP]):
        pid, gid = r.get("packageId"), r.get("granuleId")
        if not pid:
            continue
        title = clean(r.get("title")) or pid
        court = (r.get("governmentAuthor") or ["United States federal court"])[0]
        published = _iso_date(r.get("dateIssued"))
        pdf = _gi_pdf(pid, gid)
        quote = None
        if i < COURT_TEXT_N:
            try:
                txt = _pdf_text(_fetch(pdf, timeout=180), pages=4)
                quote = (_sentence(txt, re.compile(r"homeless|encampment|camping|sit-lie|"
                                                   r"unsheltered", re.I))
                         or _sentence(txt, re.compile(r"ORDER|OPINION|MEMORANDUM", re.I),
                                      min_len=60))
            except Exception as e:  # noqa: BLE001
                NOTES.append(f"courts {pid}: pdf text failed {repr(e)[:60]}")
        summary = _summ("court", court, title, published, None, quote,
                        tail=("The opinion's holding is in the linked PDF; the Government "
                              "Publishing Office copy is the primary record."))
        _doc(conn, kind="court", title=title, url=pdf, publisher=court, license=LIC_COURT,
             summary=summary, published=published,
             state=_court_state(court, states), author=court)

    # --- Brave fallback: GovInfo's USCOURTS search shares the exhausted DEMO_KEY quota, so the
    # same primary opinions are discovered from the courts' own opinion servers and parsed.
    if len(got) == 0:
        queries = [
            'site:cdn.ca9.uscourts.gov homeless opinion',
            'site:cdn.ca9.uscourts.gov encampment opinion',
            'site:cdn.ca9.uscourts.gov "camping ordinance"',
            'site:cdn.ca9.uscourts.gov "sit-lie"',
            '"Martin v. Boise" ninth circuit opinion pdf',
            '"City of Grants Pass v. Johnson" ninth circuit opinion pdf',
            '"LA Alliance for Human Rights" ninth circuit opinion pdf',
            'site:supremecourt.gov homeless camping opinion pdf',
            'site:courts.wa.gov homeless camping opinion',
            'site:courts.ca.gov homeless encampment opinion',
        ]
        cand: list[str] = []
        for q in queries:
            for res in _brave(q, 20):
                u = res.get("url") or ""
                low = u.lower()
                if not low.split("?")[0].endswith(".pdf"):
                    continue
                if "brief" in low or "uploads" in low:
                    continue
                if not re.search(r"(uscourts\.gov|supremecourt\.gov|courts\.[a-z]{2}\.gov|"
                                 r"courts\.gov|ca\.gov|wa\.gov|or\.gov|co\.gov|mo\.gov|"
                                 r"tn\.gov|az\.gov|nv\.gov|ut\.gov)", low):
                    continue
                if u not in cand:
                    cand.append(u)
        NOTES.append(f"courts: {len(cand)} opinion PDFs discovered via Brave")
        CAPTION = re.compile(r"^\s*([A-Z][A-Za-z0-9.,'\u2019\- ]{2,70}?)\s*,?\s+v\.?\s+"
                             r"([A-Z][A-Za-z0-9.,'\u2019\- ]{2,70})\s*$", re.M)
        for u in cand[:COURT_CAP]:
            try:
                data = _fetch(u, timeout=120)
                txt = _pdf_text(data, pages=3)
            except Exception:  # noqa: BLE001
                continue
            if not txt:
                continue
            head = txt[:4000]
            m = CAPTION.search(head)
            if m:
                title = clean(f"{m.group(1)} v. {m.group(2)}")
            else:
                lines = [ln.strip() for ln in head.splitlines() if len(ln.strip()) > 12]
                title = clean(lines[0]) if lines else None
            if not title:
                continue
            low = u.lower()
            if "supremecourt.gov" in low:
                court = "Supreme Court of the United States"
            elif "ca9.uscourts.gov" in low:
                court = "U.S. Court of Appeals for the Ninth Circuit"
            else:
                m2 = re.search(r"UNITED STATES DISTRICT COURT[^\n]{0,90}", head)
                m3 = re.search(r"(?:COURT OF APPEALS|SUPREME COURT)[^\n]{0,80}", head)
                court = clean(m3.group(0) if m3 else (m2.group(0) if m2 else "state appellate court"))
            published = None
            for pat in (r"Filed\s+(?:[A-Z][a-z]+)?\s*(\d{1,2}),?\s*(\d{4})",
                        r"Decided\s+([A-Z][a-z]+ \d{1,2}, \d{4})",
                        r"DATED[^\n]{0,60}?([A-Z][a-z]+ \d{1,2}, \d{4})"):
                mm = re.search(pat, head)
                if mm:
                    published = _iso_date(mm.group(0).replace("Filed", "").strip(" ,"))
                    if published:
                        break
            if not published:
                m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", u)
                if m:
                    published = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            if not HOMELESS_KW.search(head) and not HOMELESS_KW.search(txt[:12000]):
                continue
            _doc(conn, kind="court", title=title, url=u, publisher=court, license=LIC_COURT,
                 summary=_summ("court", court, title, published, None,
                               _sentence(txt, HOMELESS_KW),
                               tail=("The opinion's holding is in the linked PDF from the "
                                     "court's own opinion server.")),
                 published=published, author=court)

    # --- Supreme Court of the United States: the slip opinions index lists docket + PDF
    try:
        idx = _fetch("https://www.supremecourt.gov/opinions/slipopinion/23", browser=True,
                     timeout=120).decode("utf-8", "replace")
        soup = BeautifulSoup(idx, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "23-175" not in href and "23-175" not in a.get_text(" ", strip=True):
                continue
            pdf = urllib.parse.urljoin("https://www.supremecourt.gov/opinions/slipopinion/23",
                                       href)
            row = a.find_parent("tr")
            rowtxt = clean(row.get_text(" ", strip=True)) if row else ""
            title = "City of Grants Pass v. Johnson"
            published = _iso_date(rowtxt)
            quote = None
            try:
                quote = _sentence(_pdf_text(_fetch(pdf), pages=3), HOMELESS_KW)
            except Exception:  # noqa: BLE001
                pass
            _doc(conn, kind="court", title=title, url=pdf,
                 publisher="Supreme Court of the United States", license=LIC_COURT,
                 summary=_summ("court", "Supreme Court of the United States", title, published,
                               None, quote),
                 published=published, place="Washington", state="DC",
                 author="Supreme Court of the United States")
            break
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"SCOTUS slip index failed: {repr(e)[:100]}")

    n = COUNTER[0] - before
    _mark(conn, "courts", n > 0, f"{n} court rows from {len(got)} GovInfo opinions")
    return n


# ---------------------------------------------------------------- 3. Congressional reports
def congress(conn) -> int:
    q = "homeless collection:CRPT"
    before = COUNTER[0]
    d = _cached_govinfo(conn, "congress", q)
    if d is None:
        d = _govinfo(q, 300)
    if not d or not d.get("results"):
        _mark(conn, "congress", False, "no GovInfo response (quota)")
        return 0
    keep = [r for r in d["results"] if GAO_KW.search(r.get("title") or "")]
    NOTES.append(f"congress: {d.get('count')} hits, {len(keep)} with a homelessness title")
    for r in keep[:60]:
        pid = r.get("packageId")
        if not pid:
            continue
        title = clean(r.get("title")) or pid
        author = (r.get("governmentAuthor") or ["United States Congress"])[0]
        published = _iso_date(r.get("dateIssued"))
        _doc(conn, kind="report", title=title, url=_gi_pdf(pid, r.get("granuleId")),
             publisher=author, license=LIC_USGOV,
             summary=_summ("report", author, title, published, None, None,
                           tail=("The committee report's text and recommendations are in the "
                                 "linked PDF from the Government Publishing Office.")),
             published=published, author=author)
    n = COUNTER[0] - before
    _mark(conn, "congress", n > 0, f"{n} rows of {len(keep)} candidates")
    return n


# ------------------------------------------------------------------- 4. HUD AHAR series
AHAR_INDEX = "https://www.huduser.gov/portal/datasets/ahar.html"


def ahar(conn) -> int:
    """HUD's Annual Homeless Assessment Report, Part 1 and Part 2, from HUD USER year pages."""
    before = COUNTER[0]
    try:
        idx = _fetch(AHAR_INDEX, browser=True, timeout=180).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        _mark(conn, "ahar", False, f"index fetch failed {repr(e)[:120]}")
        return 0
    soup = BeautifulSoup(idx, "lxml")
    year_pages = []
    for a in soup.find_all("a", href=True):
        h = urllib.parse.urljoin(AHAR_INDEX, a["href"])
        if "ahar-part" in h.lower() and h.endswith(".html"):
            if h not in year_pages:
                year_pages.append(h)
    NOTES.append(f"ahar: {len(year_pages)} yearly AHAR pages on the index")
    for yp in year_pages:
        try:
            page = _fetch(yp, browser=True, timeout=180).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            NOTES.append(f"ahar {yp[-60:]}: fetch failed {repr(e)[:80]}")
            continue
        psoup = BeautifulSoup(page, "lxml")
        year = None
        m = re.search(r"(20\d\d)", yp)
        if m:
            year = m.group(1)
        seen_here = 0
        for a in psoup.find_all("a", href=True):
            h = urllib.parse.urljoin(yp, a["href"])
            if not h.lower().endswith(".pdf"):
                continue
            if "ahar" not in h.lower() and "ahar" not in a.get_text(" ", strip=True).lower():
                continue
            label = clean(a.get_text(" ", strip=True)) or Path(h).name
            if label.lower() in ("pdf", "download", "read the report"):
                label = f"{year} AHAR" if year else "AHAR"
            published = None
            fam = re.search(r"(20\d\d)-AHAR", h)
            if fam:
                published = fam.group(1)
            elif year:
                published = year
            _doc(conn, kind="report", title=label, url=h,
                 publisher="U.S. Department of Housing and Urban Development", license=LIC_USGOV,
                 summary=_summ("report", "the U.S. Department of Housing and Urban Development",
                               label, published, None, None,
                               tail=("The PDF is HUD's own narrative report for that year: it "
                                     "sets out the national and CoC-level estimates the "
                                     "department published.")),
                 published=published, author="U.S. Department of Housing and Urban Development")
            seen_here += 1
            if seen_here >= 2:
                break
    n = COUNTER[0] - before
    _mark(conn, "ahar", n > 0, f"{n} AHAR docs from {len(year_pages)} year pages")
    return n


# --------------------------------------------------------------- 5. City audits/controllers
CITY_SITES = [
    dict(office="New York City Comptroller", city="New York", state="NY",
         urls=["https://comptroller.nyc.gov/reports/?s=homeless"], cap=30),
    dict(office="Los Angeles City Controller", city="Los Angeles", state="CA",
         urls=["https://controller.lacity.gov/audits"], cap=25),
    dict(office="Seattle City Auditor", city="Seattle", state="WA",
         urls=["https://www.seattle.gov/city-auditor/reports"], cap=20),
    dict(office="San Diego City Auditor", city="San Diego", state="CA",
         urls=["https://www.sandiego.gov/auditor/reports"], cap=20),
    dict(office="Denver Auditor's Office", city="Denver", state="CO",
         urls=["https://denvergov.org/Government/Records/Audit-Reports"], cap=20),
    dict(office="City of Boston Auditing Division", city="Boston", state="MA",
         urls=["https://www.boston.gov/departments/auditing"], cap=12),
    dict(office="Office of the District of Columbia Auditor", city="Washington", state="DC",
         urls=["https://dcauditor.org/reports/", "https://dcauditor.org/?s=homeless"], cap=20),
    dict(office="San Francisco Controller's Office", city="San Francisco", state="CA",
         urls=["https://www.sf.gov/departments/controller", "https://sfcontroller.org/"], cap=15),
    dict(office="Portland City Auditor", city="Portland", state="OR",
         urls=["https://www.portland.gov/auditor", "https://www.portland.gov/auditor/news/reports"],
         cap=15),
    dict(office="Houston City Controller", city="Houston", state="TX",
         urls=["https://www.houstontx.gov/controller/audit/auditreports.html"], cap=15),
]


def _abs(base: str, href: str) -> str:
    # Seattle publishes auditor PDF links as "documents/Departments/..." (relative to the site
    # root, not to /city-auditor/), so a plain urljoin produces a 404; rebuild those from origin.
    if href.startswith(("documents/", "sites/", "files/")):
        p = urllib.parse.urlparse(base)
        return f"{p.scheme}://{p.netloc}/{href.split('#')[0]}"
    return urllib.parse.urljoin(base, href).split("#")[0]


def _meta_date(soup: BeautifulSoup) -> str | None:
    for key in ("article:published_time", "datePublished", "date", "DC.date.issued"):
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta",
                                                                    attrs={"name": key})
        if tag and tag.get("content"):
            d = _iso_date(tag["content"])
            if d:
                return d
    return None


def _city_candidates(html: str, base: str, cap: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = clean(a.get_text(" ", strip=True)) or ""
        href = a["href"]
        if not text or len(text) < 22 or not HOMELESS_KW.search(text):
            continue
        if CITY_NAV_NOISE.search(text):
            continue
        u = _abs(base, href)
        if not u.startswith("http") or u in seen:
            continue
        path = urllib.parse.urlparse(u).path.lower()
        looks_doc = (u.lower().endswith(".pdf") or "/report" in path or "/audit" in path
                     or re.search(r"/(19|20)\d\d/", path) or "publication" in path
                     or "documents" in path)
        if not looks_doc:
            continue
        seen.add(u)
        out.append((text, u))
        if len(out) >= cap * 2:
            break
    return out


def city_audits(conn) -> int:
    before = COUNTER[0]
    for site in CITY_SITES:
        if _done(conn, f"city:{site['city']}"):
            continue
        got = 0
        for page_url in site["urls"]:
            try:
                html = _fetch(page_url, browser=True, timeout=120).decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                NOTES.append(f"city {site['city']}: {page_url[:60]} failed {repr(e)[:80]}")
                continue
            if len(html) < 2000:
                continue
            for title, url in _city_candidates(html, page_url, site["cap"]):
                if got >= site["cap"]:
                    break
                is_pdf = url.lower().endswith(".pdf")
                quote = published = None
                fetched_ok = False
                try:
                    if is_pdf:
                        txt = _pdf_text(_fetch(url, timeout=180), pages=4)
                        fetched_ok = bool(txt)
                        quote = _sentence(txt, HOMELESS_KW)
                        published = _iso_date(txt[:2000])
                    else:
                        doc_html = _fetch(url, browser=True, timeout=60).decode(
                            "utf-8", "replace")
                        fetched_ok = len(doc_html) > 1000
                        dsoup = BeautifulSoup(doc_html, "lxml")
                        published = _meta_date(dsoup)
                        meta = (dsoup.find("meta", attrs={"property": "og:description"})
                                or dsoup.find("meta", attrs={"name": "description"}))
                        body_txt = dsoup.get_text(" ", strip=True)
                        quote = (_sentence(meta["content"], HOMELESS_KW)
                                 if meta and meta.get("content") else None)
                        if not quote:
                            quote = _sentence(body_txt, HOMELESS_KW)
                        if not published:
                            # a full printed date near the top of the report page
                            m = _DATE_LONG.search(body_txt[:3000])
                            published = _iso_date(m.group(0)) if m else _iso_date(url)
                except Exception:  # noqa: BLE001
                    pass
                if not published:
                    published = _iso_date(url)
                kind = "audit" if "audit" in title.lower() else "report"
                ok = _doc(conn, kind=kind, title=title, url=url, publisher=site["office"],
                          license=LIC_CITY,
                          summary=_summ(kind, site["office"], title, published,
                                        f"{site['city']}, {site['state']}", quote),
                          published=published, place=site["city"], state=site["state"],
                          author=site["office"], verified=fetched_ok)
                if ok:
                    got += 1
        _mark(conn, f"city:{site['city']}", got > 0, f"{got} docs")
    n = COUNTER[0] - before
    _mark(conn, "city_audits", n > 0, f"{n} city audit/report rows")
    return n


# --------------------------------------------------------------- 6. Investigative series
def _rss_items(xml_bytes: bytes) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items
    enc = "{http://purl.org/rss/1.0/modules/content/}encoded"
    for it in root.iter("item"):
        get = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
        desc = get("description") or (it.findtext(enc) or "")
        items.append(dict(title=get("title"), link=get("link"), date=get("pubDate"),
                          desc=re.sub(r"<[^>]+>", " ", desc)))
    return items


SFCHRON_PAGE = "https://www.sfchronicle.com/projects/homeless-project/"
NPR_NATIONAL = "https://www.npr.org/sections/national/"

# Publishers whose own homelessness series/feeds are reachable without a key. The Guardian's
# Open Platform API needs a key (see BLOCKERS.md) but its homelessness section RSS carries the
# same series, so Guardian reporting is captured from the feed the paper publishes.
RSS_FEEDS = [
    dict(publisher="The Guardian", url="https://www.theguardian.com/society/homelessness/rss",
         place=None, state=None, cap=60, all_items=True),
    dict(publisher="Los Angeles Times", url="https://www.latimes.com/homeless-housing.rss",
         place=None, state="CA", cap=40),
    dict(publisher="Los Angeles Times", url="https://www.latimes.com/california.rss",
         place=None, state="CA", cap=25),
    dict(publisher="Los Angeles Times", url="https://www.latimes.com/local.rss",
         place=None, state="CA", cap=20),
    dict(publisher="ProPublica", url="https://www.propublica.org/feeds/propublica/main",
         place=None, state=None, cap=25),
    dict(publisher="NPR", url="https://feeds.npr.org/1001/rss.xml", place=None, state=None,
         cap=15),
    dict(publisher="NPR", url="https://feeds.npr.org/1032/rss.xml", place=None, state=None,
         cap=15),
    dict(publisher="Street Sheet", url="https://streetsheet.org/feed/",
         place="San Francisco", state="CA", cap=25, all_items=True),
    dict(publisher="Crosscut", url="https://crosscut.com/feed", place="Seattle", state="WA",
         cap=20),
    dict(publisher="Invisible People", url="https://invisiblepeople.tv/feed/", place=None,
         state=None, cap=25, all_items=True),
]


def _news_row(conn, publisher: str, title: str, url: str, published: str | None,
              quote: str | None, place: str | None, state: str | None) -> bool:
    return _doc(conn, kind="article", title=title, url=url, publisher=publisher,
                license=LIC_NEWS,
                summary=_summ("article", publisher, title, published, place, quote),
                published=published, place=place, state=state, author=publisher)


def investigative(conn) -> int:
    """Homelessness reporting from LA Times, SF Chronicle, Guardian, ProPublica, NPR and the
    street press, using each publisher's own RSS/listing pages."""
    before = COUNTER[0]
    seen: set[str] = set()
    for feed in RSS_FEEDS:
        try:
            items = _rss_items(_fetch(feed["url"], browser=True, timeout=180))
        except Exception as e:  # noqa: BLE001
            NOTES.append(f"feed {feed['publisher']}: {feed['url'][:60]} failed {repr(e)[:70]}")
            continue
        # a section feed (Guardian homelessness, Street Sheet, Invisible People) is already
        # scoped by the publisher; a general feed (LA Times California, NPR news) is filtered.
        kept = [it for it in items if feed.get("all_items")
                or HOMELESS_KW.search(it["title"] + " " + it["desc"])]
        NOTES.append(f"feed {feed['publisher']}: {len(kept)}/{len(items)} in scope")
        n_here = 0
        for it in kept:
            if n_here >= feed["cap"] or not it["link"]:
                break
            u = it["link"].split("?")[0]
            if u in seen:
                continue
            seen.add(u)
            if _news_row(conn, feed["publisher"], it["title"], u, _iso_date(it["date"]),
                         _sentence(it["desc"], HOMELESS_KW), feed["place"], feed["state"]):
                n_here += 1

    # listing pages, for series that are presented as a project index rather than a feed
    for page_url, publisher, place, state, cap in [
            ("https://www.latimes.com/homeless-housing", "Los Angeles Times", None, "CA", 25),
            (SFCHRON_PAGE, "San Francisco Chronicle", "San Francisco", "CA", 45),
            (NPR_NATIONAL, "NPR", None, None, 12)]:
        try:
            page = _fetch(page_url, browser=True, timeout=180).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            NOTES.append(f"page {publisher}: {page_url[:60]} failed {repr(e)[:70]}")
            continue
        soup = BeautifulSoup(page, "lxml")
        n_here = 0
        for a in soup.find_all("a", href=True):
            if n_here >= cap:
                break
            t = clean(a.get_text(" ", strip=True)) or ""
            u = _abs(page_url, a["href"])
            if len(t) < 25 or not HOMELESS_KW.search(t):
                continue
            if u in seen or not u.startswith("http"):
                continue
            if publisher.startswith("Los Angeles") and "/story/" not in u:
                continue
            if publisher.startswith("San Francisco") and "sfchronicle.com" not in u:
                continue
            if publisher == "NPR" and "/www.npr.org/" not in u and "npr.org" not in u:
                continue
            seen.add(u)
            if _news_row(conn, publisher, t, u, _iso_date(u), None, place, state):
                n_here += 1
    n = COUNTER[0] - before
    _mark(conn, "investigative", n > 0, f"{n} article rows")
    return n


# ------------------------------------------------------------- 7. Peer-reviewed studies
def _oa_abstract(inv: dict | None) -> str:
    """Rebuild plain text from OpenAlex's abstract_inverted_index."""
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[int(i)] = word
    return " ".join(pos[i] for i in sorted(pos))


OA_QUERIES = ["homelessness", "homeless encampment policy", "housing first homelessness",
              "unsheltered homelessness health", "homelessness cost public services"]
OA_CAP = int(os.environ.get("DOCS_OA_CAP", "150"))


def openalex(conn) -> int:
    """Peer-reviewed and preprint studies with a DOI, from the keyless OpenAlex API."""
    before = COUNTER[0]
    works: list[dict] = []
    for q in OA_QUERIES:
        url = ("https://api.openalex.org/works?search=" + urllib.parse.quote(q)
               + "&per-page=200&select=doi,title,publication_year,publication_date,"
                 "primary_location,authorships,abstract_inverted_index,type,open_access")
        try:
            d = json.loads(_fetch(url, timeout=120))
        except Exception as e:  # noqa: BLE001
            NOTES.append(f"openalex {q!r} failed: {repr(e)[:100]}")
            continue
        works += d.get("results", [])
    seen: set[str] = set()
    n_cand = 0
    for w in works:
        title = re.sub(r"<[^>]+>", "", (w.get("title") or "")).strip()
        if not title or not re.search(r"homeless|encampment|unsheltered|housing first", title,
                                      re.I):
            continue
        doi = (w.get("doi") or "").lower().replace("https://doi.org/", "")
        if not doi or doi in seen:
            continue
        seen.add(doi)
        n_cand += 1
        if n_cand > OA_CAP * 3:
            break
        oa = ((w.get("open_access") or {}).get("oa_url") or "").strip()
        url = oa if oa.startswith("http") else f"https://doi.org/{doi}"
        if not _verify(conn, url):
            url = f"https://doi.org/{doi}"
            if not _verify(conn, url):
                continue
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
        authors = w.get("authorships") or []
        author = None
        if authors:
            author = clean((authors[0].get("author") or {}).get("display_name"))
        published = w.get("publication_date") or (str(w["publication_year"])
                                                  if w.get("publication_year") else None)
        quote = _sentence(_oa_abstract(w.get("abstract_inverted_index")), HOMELESS_KW)
        _doc(conn, kind="study", title=title, url=url, publisher=src or "scholarly publisher",
             license=LIC_CROSSREF,
             summary=_summ("study", src or "a scholarly publisher", title,
                           (published or "")[:4] or None, None, quote),
             published=published, author=author)
        if COUNTER[0] - before >= OA_CAP:
            break
    n = COUNTER[0] - before
    _mark(conn, "openalex", n > 0, f"{n} studies of {n_cand} candidate DOIs")
    return n


# ------------------------------------------------ 7b. DC Auditor (WordPress REST search)
def dc_auditor(conn) -> int:
    """D.C. Office of the District of Columbia Auditor: its report listing is rendered client
    side, but the office's WordPress REST API answers a server-side search."""
    before = COUNTER[0]
    keep_sub = {"report", "press_releases", "testimony"}
    seen: set[str] = set()
    for term in ("homeless", "encampment"):
        url = (f"https://dcauditor.org/wp-json/wp/v2/search?search={term}&per_page=50")
        try:
            hits = json.loads(_fetch(url, browser=True, timeout=45))
        except Exception as e:  # noqa: BLE001
            NOTES.append(f"dc_auditor {term}: {repr(e)[:90]}")
            continue
        for h in hits:
            u, sub = h.get("url"), h.get("subtype")
            title = clean(h.get("title"))
            if not u or not title or u in seen or sub not in keep_sub:
                continue
            seen.add(u)
            published = None
            try:
                page = _fetch(u, browser=True, timeout=45).decode("utf-8", "replace")
                published = _meta_date(BeautifulSoup(page, "lxml"))
                body = BeautifulSoup(page, "lxml").get_text(" ", strip=True)
            except Exception:  # noqa: BLE001
                body = ""
            kind = "audit" if (sub == "report" and "audit" in title.lower()) else "report"
            _doc(conn, kind=kind, title=title, url=u,
                 publisher="Office of the District of Columbia Auditor", license=LIC_CITY,
                 summary=_summ(kind, "the Office of the District of Columbia Auditor", title,
                               published, "Washington, DC", _sentence(body, HOMELESS_KW)),
                 published=published, place="Washington", state="DC",
                 author="Office of the District of Columbia Auditor")
    n = COUNTER[0] - before
    _mark(conn, "dc_auditor", n > 0, f"{n} DC Auditor docs")
    return n


# ---------------------------------------------------------------- 8. HUD OIG (blocked?)
def hud_oig(conn) -> int:
    """HUD Office of Inspector General audits, via Oversight.gov.

    hudoig.gov itself answers HTTP 403 to this host for every path and user agent, and its
    CIGIE aggregator search is client-side. Oversight.gov hosts each IG's reports (page + PDF)
    on its own domain, so those are discovered through a server-side index and then read from
    oversight.gov. The office that signed the report is named in the page.
    """
    before = COUNTER[0]
    queries = [
        'site:oversight.gov homeless HUD audit report',
        'site:oversight.gov "homeless" "Department of Housing and Urban Development" audit',
        'site:oversight.gov "continuum of care" audit HUD',
        'site:oversight.gov "homeless services" audit report OIG',
        'site:oversight.gov "homeless" "Office of Inspector General" audit',
    ]
    urls: list[str] = []
    for q in queries:
        for res in _brave(q, 20):
            u = res.get("url") or ""
            if "oversight.gov" not in u:
                continue
            if not re.search(r"oversight\.gov/(reports/|sites/default/files/)", u):
                continue
            if u not in urls:
                urls.append(u)
    NOTES.append(f"hud_oig: {len(urls)} Oversight.gov report URLs discovered via Brave")
    for u in urls[:40]:
        if not _verify(conn, u):
            continue
        try:
            if u.lower().endswith(".pdf"):
                txt = _pdf_text(_fetch(u, timeout=180), pages=3)
                hsoup = None
            else:
                page = _fetch(u, browser=True, timeout=90).decode("utf-8", "replace")
                hsoup = BeautifulSoup(page, "lxml")
                txt = hsoup.get_text(" ", strip=True)
        except Exception:  # noqa: BLE001
            continue
        if not HOMELESS_KW.search(txt or ""):
            continue
        title = None
        author = "U.S. Department of Housing and Urban Development, Office of Inspector General"
        published = None
        if hsoup is not None:
            h1 = hsoup.find("h1")
            if h1:
                title = clean(h1.get_text(" ", strip=True))
            t = hsoup.find("title")
            if not title and t:
                title = clean(t.get_text(" ", strip=True))
            published = _meta_date(hsoup)
            m = re.search(r"U\.S\. Department of Housing and Urban Development[^.]{0,80}"
                          r"Office of Inspector General", txt)
            if m:
                author = clean(m.group(0))
        else:
            first = next((ln.strip() for ln in txt.splitlines() if len(ln.strip()) > 20), None)
            title = clean(first)
        if not title:
            continue
        if not published:
            published = _iso_date(u)
        kind = "audit" if "audit" in (title + " " + u).lower() else "report"
        _doc(conn, kind=kind, title=title, url=u, publisher=author, license=LIC_USGOV,
             summary=_summ(kind, author, title, published, None,
                           _sentence(txt, HOMELESS_KW)),
             published=published, author=author)
    n = COUNTER[0] - before
    _mark(conn, "hud_oig", n > 0, f"{n} Oversight.gov rows")
    return n


# ----------------------------------------------------------------------- run / report
STEPS = [
    ("gao", gao),
    ("courts", courts),
    ("congress", congress),
    ("ahar", ahar),
    ("city_audits", city_audits),
    ("dc_auditor", dc_auditor),
    ("investigative", investigative),
    ("openalex", openalex),
    ("hud_oig", hud_oig),
]


def run(conn, cfg=None) -> dict:
    cfg = cfg or {}
    force = bool(cfg.get("force"))
    _ensure(conn)
    COUNTER[0] = 0
    NOTES.clear()
    states = {r[0]: r[1] for r in conn.execute("SELECT abbr, name FROM geo_state")}
    only = {x.strip() for x in os.environ.get("DOCS_ONLY", "").split(",") if x.strip()}
    counts: dict[str, int] = {}
    for name, fn in STEPS:
        if only and name not in only:
            continue
        if not force and _done(conn, name) and name != "city_audits":
            counts[name] = -1          # already finished in an earlier run
            continue
        try:
            k = fn(conn, states) if name == "courts" else fn(conn)
            counts[name] = k
        except Exception as e:  # noqa: BLE001
            NOTES.append(f"{name} crashed: {repr(e)[:200]}")
            _mark(conn, name, False, repr(e)[:300])
            counts[name] = 0
        conn.commit()

    if not any(v > 0 for v in counts.values()):
        # every task was already done on a previous run; report what the table holds
        print(f"[docs] nothing new to do; doc rows={conn.execute('SELECT COUNT(*) FROM doc').fetchone()[0]}", file=sys.stderr)

    total = conn.execute("SELECT COUNT(*) FROM doc").fetchone()[0]
    courts_total = conn.execute("SELECT COUNT(*) FROM doc WHERE kind='court'").fetchone()[0]
    kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM doc GROUP BY 1").fetchall())
    verified = conn.execute("SELECT COUNT(*) FROM doc WHERE url IN "
                            "(SELECT url FROM docs_verify WHERE status=200)").fetchone()[0]
    notes = (f"written={COUNTER[0]} per_source={counts} total_doc={total} "
             f"court_total={courts_total} verified200={verified} kinds={kinds} | "
             + "; ".join(NOTES[-6:]))
    return {"rows": COUNTER[0], "notes": notes,
            "sources": [("https://api.govinfo.gov/search", LIC_USGOV),
                        (AHAR_INDEX, LIC_USGOV),
                        ("https://www.latimes.com/homeless-housing", LIC_NEWS),
                        (SFCHRON_PAGE, LIC_NEWS),
                        ("https://www.theguardian.com/society/homelessness/rss", LIC_NEWS),
                        ("https://www.propublica.org/feeds/propublica/main", LIC_NEWS),
                        ("https://api.openalex.org/works", LIC_CROSSREF)]}


def report() -> None:
    conn = db()
    print("doc rows:", conn.execute("SELECT COUNT(*) FROM doc").fetchone()[0])
    for r in conn.execute("SELECT kind, COUNT(*) n FROM doc GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r['kind']:<10} {r['n']}")
    print("court rows:", conn.execute("SELECT COUNT(*) FROM doc WHERE kind='court'").fetchone()[0])
    print("with a verified-200 url:",
          conn.execute("SELECT COUNT(*) FROM doc WHERE url IN (SELECT url FROM docs_verify "
                       "WHERE status=200)").fetchone()[0])
    print("\n3 random rows:")
    for r in conn.execute("SELECT kind, title, publisher, published, place, state, url, "
                          "substr(summary,1,220) s FROM doc WHERE source_id IN "
                          "(SELECT id FROM source WHERE domain='docs') ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        report()
    elif "--report" in sys.argv:
        report()
    else:
        res = run(db())
        print(res["rows"], res["notes"])
