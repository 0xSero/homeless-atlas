"""A fetch ladder: get a public document even when the first plain request does not.

Local government and local news sites are where county-level facts live, and they are the sites
most likely to answer a script with a 403, a Cloudflare "Just a moment" page, a JavaScript shell,
or a 404 because the CMS moved. A citation that says "blocked" tells a reader nothing. So we
climb, stopping at the first rung that returns a real document (HTTP 200 and not an
interstitial):

  1 direct           the project UA, redirects followed
  2 retry            only after a 429/503: honour Retry-After (capped at 120 s), back off
  3 browser_headers  a desktop Chrome UA with normal Accept headers in a cookie-keeping
                     session (HUD USER already needs this; many .gov and news sites do too)
  4 alternate        forms the publisher itself serves: AMP, the WordPress REST API, the
                     site's RSS/Atom item for this URL, a linked PDF alternate
  5 wayback          the Internet Archive's closest 200 capture, raw bytes (`id_`)
  6 wayback_fresh    no capture yet: ask the archive to save one (at most 1 per 10 s and 20 per
                     run), poll, fetch it
  7 render           ATLAS_BROWSERLESS_URL (Browserless /content) or local Playwright, for
                     JS-only pages and non-interactive challenges; else `skipped (no renderer)`

Hard limits, by design and not configurable: no login forms or credentials; no CAPTCHA solving
or outsourcing (an interactive challenge ends the render rung); no paywall bypass (a paywall stub
sends us to the next live rung, and an archive copy of a paywalled article is never used: the
result is `paywalled`). robots.txt is honoured for our own requests: when it disallows our UA
we skip rungs 1 to 4 and 7 for that host and only read the archive (the archive did the fetching,
not us), recorded as `robots: archive_only`.

Every attempt is kept in `Result.attempts` as (rung, status, note), so a reader can see why a
citation is a Wayback copy. Bodies are cached under data/raw/<domain>/<sha1(url)>.bin, the path
common.source_id_for hashes, so the source row records the bytes we actually read. 1 request per
second per host; never more than 3 tries per rung.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote, urljoin, urlsplit

from ingest.common import RAW, UA, _log, now

CHROME_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like "
             "Gecko) Chrome/131.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = 60
MAX_TRIES = 3
RETRY_AFTER_CAP = 120
SAVE_GAP = 10          # seconds between Wayback save requests
SAVE_MAX = 20          # Wayback save requests per process (one ingest run)
MIN_TEXT = 1000        # visible characters below which an HTML 200 is an interstitial


@dataclass
class Result:
    ok: bool
    http: int | None = None              # status of the response that produced `body`
    body: bytes | None = None
    content_type: str | None = None
    via: str | None = None               # the rung that produced the document
    final_url: str | None = None
    archived_at: str | None = None       # ISO time of the archive capture, for wayback rungs
    attempts: list = field(default_factory=list)   # [(rung, status, note)]
    status: str = "error"                # ok | archived | paywalled | blocked | gone | robots_disallowed | error
    robots: str = "allowed"              # allowed | archive_only
    live_http: int | None = None         # what the publisher's own server answered first

    def meta(self) -> dict:
        d = asdict(self)
        d.pop("body")
        return d


# --------------------------------------------------------------------------- plumbing
_last_by_host: dict[str, float] = {}
_sessions: dict[str, object] = {}
_robots: dict[str, bool | urllib.robotparser.RobotFileParser] = {}
_saves = {"n": 0, "last": 0.0}


def _host(url):
    return urlsplit(url).netloc.lower()


def _throttle(url):
    h = _host(url)
    dt = time.time() - _last_by_host.get(h, 0.0)
    if dt < 1.0:
        time.sleep(1.0 - dt)
    _last_by_host[h] = time.time()


def _request(domain, url, *, session=None, headers=None, method="GET", json_body=None,
             timeout=TIMEOUT, rung=""):
    import requests
    _throttle(url)
    client = session or requests
    h = {"User-Agent": UA, "Accept": "*/*"} if headers is None else headers
    r = client.request(method, url, headers=h, json=json_body, timeout=timeout,
                       allow_redirects=True)
    _log(domain, url, r.status_code, len(r.content), rung)
    return r


def _retry_after(r, attempt):
    v = r.headers.get("Retry-After")
    if v:
        try:
            return min(RETRY_AFTER_CAP, max(0, int(v)))
        except ValueError:
            try:
                return min(RETRY_AFTER_CAP, max(0, parsedate_to_datetime(v).timestamp() - time.time()))
            except (TypeError, ValueError):
                pass
    return min(RETRY_AFTER_CAP, 2 ** (attempt + 1))


def robots_allowed(url: str) -> bool:
    """robots.txt for our UA, per RFC 9309: 4xx means no rules, 5xx means stay out."""
    import requests
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        try:
            _throttle(base)
            r = requests.get(base + "/robots.txt", headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                _robots[base] = rp
            else:
                _robots[base] = r.status_code < 500
        except Exception:  # noqa: BLE001  unreachable robots.txt: the fetch itself will say why
            _robots[base] = True
    rule = _robots[base]
    return rule if isinstance(rule, bool) else rule.can_fetch(UA, url)


# --------------------------------------------------------------------------- what is a document
# Challenge markers count only on short pages: Cloudflare injects its challenge-platform script
# into ordinary pages too (hud.gov's homepage carries it), so a marker alone proves nothing.
_CHALLENGE = re.compile(
    r"just a moment\.\.\.|attention required!? \| cloudflare|enable javascript and cookies to "
    r"continue|cf-browser-verification|cf_chl_opt|checking your browser|"
    r"pardon our interruption|request unsuccessful\. incapsula|px-captcha|are you a robot|"
    r"verify you are human|ddos protection by", re.I)
_CHALLENGE_TITLE = re.compile(r"just a moment|attention required|access denied|pardon our "
                              r"interruption|are you a robot|security check|request rejected", re.I)
_CAPTCHA = re.compile(r"g-recaptcha|h-captcha|hcaptcha\.com|cf-turnstile|px-captcha|"
                      r"verify you are human|captcha-delivery", re.I)
_PAYWALL = re.compile(
    r"subscribe (?:now )?to (?:continue|keep) reading|to continue reading,? (?:please )?"
    r"(?:subscribe|log ?in|sign in)|already a subscriber\?|this (?:article|story|content) is "
    r"(?:only )?(?:available|for) (?:to )?(?:subscribers|members)|you have reached your "
    r"(?:free )?article limit", re.I)
_FREE_FALSE = re.compile(r'"isAccessibleForFree"\s*:\s*"?false"?', re.I)


def _is_html(body, ctype):
    head = body[:2048].lstrip().lower()
    return "html" in (ctype or "").lower() or head.startswith((b"<!doctype html", b"<html"))


def visible_text(html: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1\s*>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def judge(body: bytes | None, ctype: str | None, want_text=True):
    """(verdict, note): verdict is document | interstitial | captcha | paywall | empty."""
    if not body:
        return "empty", "empty body"
    if body.lstrip()[:5] == b"%PDF-":
        return "document", "pdf"
    if not _is_html(body, ctype):
        if want_text and not (ctype or "").lower().startswith(("text/", "application/json",
                                                              "application/xml", "application/rss",
                                                              "application/atom")):
            return "document", f"binary {ctype}"
        return "document", ctype or "non-html"
    html = body[:3_000_000].decode("utf-8", errors="replace")
    text = visible_text(html)
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = unescape(m.group(1)).strip() if m else ""
    short = len(text) < 3000
    if short and _CAPTCHA.search(html):
        return "captcha", "interactive challenge"
    if (len(title) < 80 and _CHALLENGE_TITLE.search(title)) or (short and _CHALLENGE.search(html[:200_000])):
        return "interstitial", f"bot challenge page ({title[:40] or 'no title'})"
    if _FREE_FALSE.search(html) or (len(text) < 6000 and _PAYWALL.search(text)):
        return "paywall", "paywall stub"
    if want_text and len(text) < MIN_TEXT:
        return "interstitial", f"only {len(text)} chars of text"
    return "document", f"{len(text)} chars"


def _classify_code(code):
    if code in (404, 410):
        return "gone"
    if code in (202, 401, 403, 429, 503):
        return "blocked"
    return "error"


# --------------------------------------------------------------------------- rungs
def _try(res, rung, fn):
    """Run one request; record it; return (response or None)."""
    try:
        r = fn()
    except Exception as e:  # noqa: BLE001
        res.attempts.append((rung, "error", f"{type(e).__name__}: {str(e)[:120]}"))
        return None
    return r


def _accept(res, rung, r, want_text, partial, *, via=None, archived_at=None, body=None,
            ctype=None):
    """Judge a response; on a real document fill `res` and return True."""
    body = r.content if body is None else body
    ctype = r.headers.get("Content-Type") if ctype is None else ctype
    if r.status_code != 200:
        res.attempts.append((rung, str(r.status_code), ""))
        if _is_html(body or b"", ctype):
            partial.append(body[:500_000].decode("utf-8", "replace"))
        return False
    verdict, note = judge(body, ctype, want_text)
    res.attempts.append((rung, "200" if verdict == "document" else verdict, note))
    if verdict == "paywall":
        res.status = "paywalled"
    if verdict != "document":
        if _is_html(body, ctype):
            partial.append(body[:500_000].decode("utf-8", "replace"))
        return False
    res.ok, res.http, res.body, res.content_type = True, 200, body, ctype
    res.via, res.final_url, res.archived_at = via or rung, r.url, archived_at
    return True


def _direct(domain, url, res, want_text, partial):
    r = _try(res, "direct", lambda: _request(domain, url, rung="direct"))
    if r is None:
        return False, None
    res.live_http = r.status_code
    return _accept(res, "direct", r, want_text, partial), r


def _retry(domain, url, res, want_text, partial, last):
    """Only after a 429/503: wait as told (capped), at most MAX_TRIES more requests."""
    if last is None or last.status_code not in (429, 503):
        return False
    for i in range(MAX_TRIES):
        time.sleep(_retry_after(last, i))
        r = _try(res, "retry", lambda: _request(domain, url, rung="retry"))
        if r is None:
            continue
        if _accept(res, "retry", r, want_text, partial):
            return True
        if r.status_code not in (429, 503):
            return False
        last = r
    return False


def _session(url):
    import requests
    h = _host(url)
    if h not in _sessions:
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        _sessions[h] = s
    return _sessions[h]


def _browser(domain, url, res, want_text, partial):
    s = _session(url)
    for i in range(MAX_TRIES):
        r = _try(res, "browser_headers",
                 lambda: _request(domain, url, session=s, headers={}, rung="browser_headers"))
        if r is None:
            return False
        if _accept(res, "browser_headers", r, want_text, partial):
            return True
        if r.status_code not in (429, 503) or i == MAX_TRIES - 1:
            return False
        time.sleep(_retry_after(r, i))
    return False


def _links(html_pages, rel, typ=None):
    out = []
    for html in html_pages:
        for tag in re.findall(r"(?is)<link\b[^>]*>", html):
            a = dict((k.lower(), unescape(v)) for k, v in
                     re.findall(r"""([a-zA-Z\-]+)\s*=\s*["']([^"']*)["']""", tag))
            if rel in (a.get("rel") or "").lower().split() and a.get("href") and (
                    typ is None or (a.get("type") or "").lower() == typ):
                out.append(a["href"])
    return out


def _wrap(title, html_body, url):
    return (f"<!doctype html><html><head><title>{title}</title><link rel=canonical href=\"{url}\">"
            f"</head><body>{html_body}</body></html>").encode()


def _same(a, b):
    return a.rstrip("/").split("#")[0] == b.rstrip("/").split("#")[0]


def _alternate(domain, url, res, want_text, partial):
    s = _session(url)
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    tried = set()

    def get(u, note):
        if u in tried:
            return None
        tried.add(u)
        r = _try(res, "alternate", lambda: _request(domain, u, session=s, headers={},
                                                    rung="alternate"))
        if r is not None and r.status_code != 200:
            res.attempts.append(("alternate", str(r.status_code), note))
            return None
        return r

    # publisher-declared alternates first: AMP and PDF links from any HTML we already saw
    cands = [(urljoin(url, h), "amphtml link") for h in _links(partial, "amphtml")]
    cands += [(urljoin(url, h), "pdf alternate") for h in _links(partial, "alternate", "application/pdf")]
    cands += [(url.rstrip("/") + "/amp", "/amp"),
              (url + ("&" if "?" in url else "?") + "outputType=amp", "outputType=amp")]
    for u, note in cands:
        r = get(u, note)
        if r is not None:
            if _accept(res, "alternate", r, want_text, [], via=f"alternate:{note}"):
                return True

    slug = [p for p in parts.path.split("/") if p]
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug[-1]) if slug else ""
    if slug:
        for kind in ("posts", "pages"):
            r = get(f"{base}/wp-json/wp/v2/{kind}?slug={quote(slug)}", f"wp-json {kind}")
            if r is None:
                continue
            try:
                items = r.json()
            except ValueError:
                res.attempts.append(("alternate", "not json", f"wp-json {kind}"))
                continue
            for it in items if isinstance(items, list) else []:
                link = it.get("link") or ""
                if _same(link, url) or it.get("slug") == slug:
                    body = _wrap(unescape(((it.get("title") or {}).get("rendered") or "")),
                                 (it.get("content") or {}).get("rendered") or "", url)
                    if _accept(res, "alternate", r, want_text, [], via=f"alternate:wp-json {kind}",
                               body=body, ctype="text/html"):
                        return True
            res.attempts.append(("alternate", "no match", f"wp-json {kind}"))

    feeds = [urljoin(url, h) for t in ("application/rss+xml", "application/atom+xml")
             for h in _links(partial, "alternate", t)] or [base + "/feed/"]
    for f in feeds[:3]:
        r = get(f, "feed")
        if r is None:
            continue
        body = _feed_item(r.content, url)
        if body is None:
            res.attempts.append(("alternate", "no match", f"feed {f}"))
            continue
        if _accept(res, "alternate", r, want_text, [], via="alternate:feed", body=body,
                   ctype="text/html"):
            return True
    return False


def _feed_item(xml_bytes, url):
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        link, title, content = None, "", None
        for ch in item:
            t = ch.tag.rsplit("}", 1)[-1]
            if t == "link":
                link = (ch.text or ch.get("href") or "").strip()
            elif t == "title":
                title = ch.text or ""
            elif t in ("encoded", "content") and ch.text:
                content = ch.text
            elif t in ("description", "summary") and ch.text and content is None:
                content = ch.text
        if link and _same(link, url) and content:
            return _wrap(title, content, url)
    return None


def _closest(domain, url, res, rung):
    r = _try(res, rung, lambda: _request(
        domain, "https://archive.org/wayback/available?url=" + quote(url, safe=""), rung=rung))
    if r is None or r.status_code != 200:
        if r is not None:
            res.attempts.append((rung, str(r.status_code), "availability API"))
        return None
    try:
        snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
    except ValueError:
        return None
    if snap.get("available") and str(snap.get("status")) == "200" and snap.get("timestamp"):
        return snap["timestamp"]
    return None


def _iso_ts(ts):
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}Z"


def _fetch_capture(domain, url, ts, res, rung, want_text):
    raw = f"https://web.archive.org/web/{ts}id_/{url}"
    for i in range(MAX_TRIES):
        r = _try(res, rung, lambda: _request(domain, raw, rung=rung))
        if r is None:
            continue
        if r.status_code in (429, 503) and i < MAX_TRIES - 1:
            res.attempts.append((rung, str(r.status_code), "capture; backing off"))
            time.sleep(_retry_after(r, i))
            continue
        if r.status_code == 200 and judge(r.content, r.headers.get("Content-Type"), want_text)[0] == "paywall":
            res.attempts.append((rung, "paywall", f"capture {ts} is a paywalled article; not used"))
            res.status = "paywalled"
            return False
        return _accept(res, rung, r, want_text, [], via=rung, archived_at=_iso_ts(ts))
    return False


def _wayback(domain, url, res, want_text):
    ts = _closest(domain, url, res, "wayback")
    if not ts:
        res.attempts.append(("wayback", "none", "no 200 capture"))
        return False
    return _fetch_capture(domain, url, ts, res, "wayback", want_text)


def _wayback_fresh(domain, url, res, want_text):
    if _saves["n"] >= SAVE_MAX:
        res.attempts.append(("wayback_fresh", "skipped", f"save limit {SAVE_MAX} per run reached"))
        return False
    gap = SAVE_GAP - (time.time() - _saves["last"])
    if gap > 0:
        time.sleep(gap)
    _saves["n"] += 1
    _saves["last"] = time.time()
    r = _try(res, "wayback_fresh", lambda: _request(
        domain, "https://web.archive.org/save/" + url, rung="wayback_fresh", timeout=120))
    res.attempts.append(("wayback_fresh", str(r.status_code) if r is not None else "error",
                         "save request"))
    for _ in range(4):
        time.sleep(SAVE_GAP)
        ts = _closest(domain, url, res, "wayback_fresh")
        if ts:
            return _fetch_capture(domain, url, ts, res, "wayback_fresh", want_text)
    res.attempts.append(("wayback_fresh", "none", "capture did not appear"))
    return False


def _render(domain, url, res, want_text):
    endpoint = os.environ.get("ATLAS_BROWSERLESS_URL")
    html = None
    if endpoint:
        ep = endpoint.rstrip("/")
        ep = ep if "/content" in ep else ep + "/content"
        r = _try(res, "render", lambda: _request(domain, ep, method="POST", json_body={"url": url},
                                                 headers={"Content-Type": "application/json"},
                                                 rung="render", timeout=90))
        if r is None:
            return False
        if r.status_code != 200:
            res.attempts.append(("render", str(r.status_code), "browserless"))
            return False
        html = r.content
    else:
        try:
            from playwright.sync_api import sync_playwright  # pyright: ignore[reportMissingImports]
        except ImportError:
            res.attempts.append(("render", "skipped", "skipped (no renderer)"))
            return False
        try:
            _throttle(url)
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                try:
                    pg = b.new_page(user_agent=CHROME_UA)
                    pg.goto(url, timeout=30_000)
                    try:
                        pg.wait_for_load_state("networkidle", timeout=20_000)
                    except Exception:  # noqa: BLE001  network never idle: take what rendered
                        pass
                    html = pg.content().encode()
                finally:
                    b.close()
        except Exception as e:  # noqa: BLE001
            res.attempts.append(("render", "error", f"{type(e).__name__}: {str(e)[:120]}"))
            return False
    verdict, note = judge(html, "text/html", want_text)
    if verdict == "captcha":
        res.attempts.append(("render", "captcha", "interactive challenge; stopped (never solved)"))
        return False
    if verdict != "document":
        res.attempts.append(("render", verdict, note))
        if verdict == "paywall":
            res.status = "paywalled"
        return False
    res.attempts.append(("render", "200", note))
    res.ok, res.http, res.body, res.content_type = True, 200, html, "text/html"
    res.via, res.final_url = "render", url
    return True


# --------------------------------------------------------------------------- entry point
def _cache_paths(domain, url):
    d = RAW / domain
    key = hashlib.sha1(url.encode()).hexdigest()
    return d / f"{key}.bin", d / f"{key}.ladder.json"


def get(url: str, *, domain: str, want_text: bool = True, force: bool = False) -> Result:
    """Climb the ladder for one URL. Never raises. Cached unless force=True."""
    p, meta = _cache_paths(domain, url)
    if not force and p.exists() and meta.exists() and p.stat().st_size:
        m = json.loads(meta.read_text())
        m["attempts"] = [tuple(a) for a in m.get("attempts", [])] + [("cache", "hit", m.get("at", ""))]
        m.pop("at", None)
        return Result(body=p.read_bytes(), **m)

    res = Result(ok=False)
    partial: list[str] = []
    try:
        if robots_allowed(url):
            got, last = _direct(domain, url, res, want_text, partial)
            got = got or _retry(domain, url, res, want_text, partial, last)
            got = got or _browser(domain, url, res, want_text, partial)
            got = got or _alternate(domain, url, res, want_text, partial)
        else:
            res.robots = "archive_only"
            res.attempts.append(("robots", "disallowed", "archive_only: rungs 1-4 and 7 skipped"))
            got = False
        if not got and res.status == "paywalled":
            res.attempts.append(("wayback", "skipped", "paywalled article: archive copy not used"))
        else:
            got = got or _wayback(domain, url, res, want_text)
            live = [a[1] for a in res.attempts if a[0] in ("direct", "retry", "browser_headers")]
            plainly_gone = bool(live) and all(c in ("404", "410") for c in live)
            had_capture = any(a[0] == "wayback" and a[1] != "none" and a[2] != "availability API"
                              for a in res.attempts)
            if not got and res.status != "paywalled" and not plainly_gone and not had_capture:
                # nothing archived yet; saving a page the publisher says is gone captures a 404
                got = _wayback_fresh(domain, url, res, want_text)
            elif not got and plainly_gone:
                res.attempts.append(("wayback_fresh", "skipped", "publisher answers 404/410"))
            if not got and res.robots == "allowed":
                got = _render(domain, url, res, want_text)
    except Exception as e:  # noqa: BLE001  the ladder never takes a run down
        res.attempts.append(("ladder", "error", f"{type(e).__name__}: {str(e)[:160]}"))
        got = res.ok

    if res.ok:
        res.status = "archived" if (res.via or "").startswith("wayback") else "ok"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(res.body or b"")
        m = res.meta()
        m["attempts"] = [list(a) for a in m["attempts"]]
        m["at"] = now()
        meta.write_text(json.dumps(m, default=str))
    elif res.status != "paywalled":
        if res.robots == "archive_only":
            res.status = "robots_disallowed"
        else:
            live = [int(a[1]) for a in res.attempts
                    if a[0] in ("direct", "retry", "browser_headers") and a[1].isdigit()]
            soft = [a[1] for a in res.attempts if a[1] in ("interstitial", "captcha")]
            if live and all(c in (404, 410) for c in live):
                res.status = "gone"
            elif soft or any(_classify_code(c) == "blocked" for c in live):
                res.status = "blocked"
            else:
                res.status = "error"
    return res


def attempts_summary(attempts) -> str:
    """Compact one-line history, e.g. `direct 404 > browser_headers 404 > wayback 200`."""
    return " > ".join(f"{a[0]} {a[1]}" for a in attempts if a[0] != "cache")


if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        r = get(u, domain="misc", force=True)
        print(json.dumps({k: v for k, v in r.meta().items()}, default=str, indent=1))
