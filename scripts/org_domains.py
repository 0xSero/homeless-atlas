#!/usr/bin/env python3
"""org_domains — recover organisations' own websites from the Common Crawl domain rank list.

No search API is involved and no key is needed: the rank list is a plain text file naming every
domain Common Crawl has seen, most-linked first. An organisation whose name is distinctive enough to
appear in its own domain ("pinestreetinn.org", "homewardboundaz.org") can be found by name.

Three guards, because a wrong website is worse than none:
  1. the name key must be distinctive — at least 10 letters once legal suffixes are stripped, and it
     must map to exactly one organisation (so "catholiccharities" matches nobody);
  2. the domain label must equal the key or contain it as a whole word, not merely share letters;
  3. the candidate must answer HTTP 200 and the page must mention the organisation's name tokens or
     its city, otherwise it is discarded.

Writes org.website for organisations that have none, and records every attempt in org_domain_mine so
a re-run never repeats work.

Run: python3 scripts/org_domains.py [--scan] [--verify N] [--dry-run]
  --scan    stream the rank list and write candidates to data/raw/org_domains/candidates.json
  --verify  fetch and check candidates, writing the ones that pass
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch, now  # noqa: E402

RANKS = ROOT / "data" / "raw" / "org_domains" / "cc-domain-ranks.txt.gz"
CAND = ROOT / "data" / "raw" / "org_domains" / "candidates.json"
SUFFIX = {"inc", "incorporated", "llc", "co", "corp", "corporation", "ltd", "the", "of", "and",
          "a", "an", "dba", "usa", "us"}
GENERIC_WORD = {"inc", "the", "of", "and", "for", "services", "service", "center", "centre",
                "health", "hospital", "hospitals", "childrens", "children", "community", "church",
                "school", "academy", "university", "college", "foundation", "association",
                "society", "housing", "shelter", "home", "homes", "family", "families", "program",
                "programs", "project", "projects", "group", "institute", "corporation", "company",
                "council", "agency", "alliance", "network", "partners", "coalition", "fund"}
GENERIC = {"catholiccharities", "communityaction", "unitedway", "salvationarmy", "goodwill",
           "habitatforhumanity", "boysandgirlsclub", "ymca", "ywca", "redcross", "vfw"}
# domains that are never an organisation's own site
BLOCK = ("facebook.", "linkedin.", "instagram.", "twitter.", "x.com", "youtube.", "tiktok.",
         "wikipedia.", "wikimedia.", "guidestar.", "candid.", "charitynavigator.", "indeed.",
         "glassdoor.", "yelp.", "mapquest.", "bbb.", "amazon.", "google.", "apple.", "microsoft.",
         "godaddy.", "wix.", "squarespace.", "weebly.", "blogspot.", "wordpress.com", "yumpu.",
         "issuu.", "scribd.", "indd.adobe.", "paypal.", "networkforgood.", "gofundme.")


def key_of(name: str) -> str:
    n = html.unescape(name or "").lower().replace("&", " and ")
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    toks = [t for t in n.split() if t and t not in SUFFIX]
    return "".join(toks)


TLD = {"com", "org", "net", "gov", "edu", "us", "io", "info", "biz", "co", "uk", "ca", "au", "de",
       "fr", "nl", "se", "nz", "ie", "za", "in", "jp", "cn", "es", "it", "me", "tv", "cc", "ly",
       "xyz", "site", "online", "care", "support", "help", "ngo", "one", "app", "dev"}
SUB = {"www", "m", "web", "main", "home", "en"}


def label_of(host_rev: str) -> str:
    """'com.pinestreetinn' -> 'pinestreetinn'. Reversed-domain aware, separators removed."""
    parts = host_rev.split(".")
    while parts and parts[-1] in SUB:
        parts.pop()
    if len(parts) > 2 and parts[-2] in TLD:      # e.g. uk.co.example
        parts = parts[:-2] + [parts[-1]]
    return re.sub(r"[^a-z0-9]", "", parts[-1]) if parts else ""


def scan(conn, dry: bool) -> int:
    rows = conn.execute("SELECT id, name, city, state FROM org WHERE website IS NULL AND name IS NOT NULL").fetchall()
    want: dict[str, list] = {}
    for r in rows:
        k = key_of(r["name"])
        if len(k) < 10 or k in GENERIC:
            continue
        want.setdefault(k, []).append((r["id"], r["name"], r["city"], r["state"]))
    uniq = {k: v[0] for k, v in want.items() if len(v) == 1}
    print(f"distinctive, unambiguous name keys: {len(uniq):,} (from {len(rows):,} orgs without a website)")

    exact = dict(uniq)                                    # label == key
    buckets: dict[str, list] = {}                          # first 12 chars -> [(key, org)]
    for k, v in uniq.items():
        if len(k) >= 12:
            buckets.setdefault(k[:12], []).append((k, v))

    hits: dict[int, dict] = {}
    n = 0
    with gzip.open(RANKS, "rt", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            n += 1
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            host_rev = parts[4]
            if any(b in host_rev for b in BLOCK):
                continue
            lab = label_of(host_rev)
            if not lab or len(lab) < 8:
                continue
            hit = exact.get(lab)
            if hit is None and len(lab) >= 12:
                for k, v in buckets.get(lab[:12], ()):
                    if abs(len(k) - len(lab)) <= 6 and (k in lab or lab in k):
                        hit = v
                        break
            if hit is not None:
                oid, name, city, state = hit
                if oid not in hits:
                    hits[oid] = {"org_id": oid, "name": name, "city": city, "state": state,
                                 "domain": ".".join(reversed(host_rev.split("."))), "rank": n}
            if n % 10_000_000 == 0:
                print(f"  scanned {n:,} domains, {len(hits)} candidates", flush=True)
    print(f"scanned {n:,} domains -> {len(hits):,} candidates")
    if not dry:
        CAND.parent.mkdir(parents=True, exist_ok=True)
        CAND.write_text(json.dumps(sorted(hits.values(), key=lambda h: h["rank"]), indent=0))
    return len(hits)


def _check(c: dict, path: str = "") -> tuple[dict, int, int, str]:
    """Fetch one candidate and decide whether the page is that organisation's. No DB access."""
    name_key = key_of(c["name"])
    toks = [t for t in re.split(r"[^a-z0-9]+", (c["name"] or "").lower()) if len(t) > 3]
    distinct = [t for t in toks if t not in GENERIC_WORD]
    site = "https://" + c["domain"]
    status, matched = 0, 0
    body = ""
    try:
        body = fetch(site + path, domain="org_domains", timeout=12).decode("utf-8", "replace")
        status = 200
    except Exception:
        return c, 0, 0, ""
    low = re.sub(r"[^a-z0-9 ]", " ", body[:500_000].lower())
    flat = low.replace(" ", "")
    city = (c.get("city") or "").lower().strip()
    st = (c.get("state") or "").lower().strip()
    city_hit = bool(city) and city in low
    state_hit = bool(st) and re.search(r"\b" + re.escape(st) + r"\b", low) is not None
    all_distinct = bool(distinct) and all(t in low for t in distinct)
    if not distinct:
        matched = 0
    elif name_key and name_key in flat and (city_hit or not city):
        matched = 1
    elif all_distinct and city_hit:
        matched = 1
    elif all_distinct and len(distinct) >= 2 and state_hit:
        matched = 1
    return c, status, matched, (site + path if matched else "")


def verify(conn, limit: int | None, dry: bool, shard: str | None = None, workers: int = 6) -> None:
    cands = json.loads(CAND.read_text())
    if shard:
        n, m = (int(x) for x in shard.split("/"))
        cands = [c for i, c in enumerate(cands) if i % m == n]
    conn.execute("""CREATE TABLE IF NOT EXISTS org_domain_mine(
        org_id INTEGER PRIMARY KEY, domain TEXT, status INTEGER, matched INTEGER,
        page TEXT, checked_at TEXT)""")
    conn.commit()
    seen = {r[0] for r in conn.execute("SELECT org_id FROM org_domain_mine")}
    cands = [c for c in cands if c["org_id"] not in seen]
    if limit:
        cands = cands[:limit]
    print(f"to verify: {len(cands):,} with {workers} fetch workers, one writer")
    ok = 0
    from concurrent.futures import ThreadPoolExecutor
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for c, status, matched, page in pool.map(lambda x: _check(x), cands):
            done += 1
            if matched and not dry:
                conn.execute("UPDATE org SET website=?, updated_at=? WHERE id=?",
                             ("https://" + c["domain"], now(), c["org_id"]))
                ok += 1
            if not dry:
                conn.execute("INSERT OR REPLACE INTO org_domain_mine VALUES(?,?,?,?,?,?)",
                             (c["org_id"], c["domain"], status, matched, page, now()))
                if done % 25 == 0:
                    conn.commit()
                    print(f"  [{done}/{len(cands)}] verified={ok}", flush=True)
    if not dry:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM org WHERE website IS NOT NULL").fetchone()[0]
    print(json.dumps({"candidates": len(cands), "written": ok, "websites_total_now": total}))
    return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--verify", type=int, nargs="?", const=0, default=None,
                    help="verify candidates (optionally cap the count)")
    ap.add_argument("--shard", default=None, help="N/M — verify only candidates where index %% M == N (%%)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=300)
    conn.execute("PRAGMA busy_timeout=300000")
    conn.row_factory = sqlite3.Row
    if a.scan:
        scan(conn, a.dry_run)
    if a.verify is not None or (not a.scan and CAND.exists()):
        verify(conn, a.verify, a.dry_run, a.shard)


if __name__ == "__main__":
    main()
