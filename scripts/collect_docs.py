#!/usr/bin/env python3
"""Collect audits, court decisions, controller reviews and studies into `doc` using a search API.

Every document is URL-verified (HTTP 200 + a document-ish content type) before it is stored, and the
search provider is recorded as the source so the trail is honest: the URL is the document, the query
is how we found it.

Run: python3 scripts/collect_docs.py [--max 200]
"""
from __future__ import annotations
import argparse, json, os, re, sqlite3, sys, time, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch, now, source_id_for  # noqa: E402

DOMAIN = "docs"
BRAVE = "https://api.search.brave.com/res/v1/web/search"

# (query, kind)
QUERIES = [
    ("homelessness audit site:gao.gov", "audit"),
    ("homeless audit report site:oig.hud.gov", "audit"),
    ("homelessness inspector general report site:oversight.gov", "audit"),
    ("homelessness report site:controller.sfgov.org", "audit"),
    ("homeless audit site:lacontroller.org", "audit"),
    ("homeless services audit site:comptroller.nyc.gov", "audit"),
    ("homelessness audit site:seattle.gov", "audit"),
    ("homelessness city auditor report encampment", "audit"),
    ("civil grand jury report homelessness california", "report"),
    ("homelessness point in time count report continuum of care", "report"),
    ("Martin v. Boise Ninth Circuit opinion camping public property", "court"),
    ("Grants Pass v. Johnson Supreme Court opinion homeless", "court"),
    ("LA Alliance for Human Rights v County of Los Angeles ruling", "court"),
    ("homeless encampment injunction federal court ruling city", "court"),
    ("cost of homelessness per person report", "study"),
    ("housing first evidence review randomized", "study"),
    ("homelessness overdose mortality study", "study"),
    ("encampment sweep health outcome study", "study"),
]


def brave(query, count=20):
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        raise RuntimeError("BRAVE_API_KEY not set")
    url = f"{BRAVE}?q={urllib.parse.quote(query)}&count={count}&country=us"
    b = fetch(url, domain="docs_search", headers={"X-Subscription-Token": key,
                                                  "Accept": "application/json"})
    return json.loads(b)


def verify_url(url):
    """True when the URL answers 200 with a document-ish content type."""
    try:
        import requests
        r = requests.get(url, headers={"User-Agent": "homeless-atlas/1.0"}, timeout=25,
                         stream=True, allow_redirects=True)
        ctype = (r.headers.get("content-type") or "").lower()
        ok = r.status_code == 200 and any(t in ctype for t in
                                         ("text/html", "application/pdf", "text/plain", "application/xhtml"))
        r.close()
        return ok, r.status_code, ctype
    except Exception:
        return False, 0, ""


def publisher_of(url):
    host = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    return host


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=200)
    a = ap.parse_args()
    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row
    added = skipped = failed = 0
    seen = {r[0] for r in c.execute("SELECT url FROM doc WHERE url IS NOT NULL")}
    for query, kind in QUERIES:
        if added >= a.max:
            break
        try:
            d = brave(query)
        except Exception as e:
            print(f"query failed: {query}: {e}", flush=True)
            continue
        results = (d.get("web") or {}).get("results") or []
        src = source_id_for(c, f"brave:{query}", DOMAIN,
                            title=f"Brave web search: {query}", publisher="Brave Search API",
                            license="search index; documents belong to their publishers")
        c.commit()
        print(f"[{kind}] {query}: {len(results)} results", flush=True)
        for r in results:
            if added >= a.max:
                break
            url = r.get("url")
            title = (r.get("title") or "").strip()
            if not url or not title or url in seen:
                skipped += 1
                continue
            ok, status, ctype = verify_url(url)
            if not ok:
                failed += 1
                continue
            age = r.get("age") or ""
            m = re.search(r"(\d{4})", r.get("page_age") or age or "")
            published = f"{m.group(1)}-01-01" if m else None
            c.execute("""INSERT OR IGNORE INTO doc(kind,title,url,publisher,author,published,place,state,summary,source_id)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (kind, title[:400], url, publisher_of(url),
                       (r.get("profile") or {}).get("name") if isinstance(r.get("profile"), dict) else None,
                       published, None, None, None, src))
            c.commit()
            seen.add(url)
            added += 1
            time.sleep(0.6)
    c.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
              ("docs_collect", now(), added, f"added={added} skipped={skipped} unverified={failed}"))
    c.commit()
    total = c.execute("SELECT COUNT(*) FROM doc").fetchone()[0]
    print(json.dumps({"added": added, "skipped": skipped, "unverified": failed, "doc_total": total}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
