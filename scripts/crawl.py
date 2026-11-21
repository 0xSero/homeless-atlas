#!/usr/bin/env python3
"""Crawl the built site: dead internal links, missing assets, viewport/mobile readiness,
search coverage. Exit 0 only when the site is structurally sound.

Run: python3 scripts/crawl.py [--quiet]
"""
from __future__ import annotations
import json, re, sys, time
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
QUIET = "--quiet" in sys.argv


class P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.ids, self.imgs, self.h1, self.has_viewport, self.has_search = [], set(), [], 0, False, False
        self.scripts, self.in_title, self.title = [], False, ""
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag in ("img", "script", "link") and (a.get("src") or a.get("href")):
            self.scripts.append((tag, a.get("src") or a.get("href")))
            if tag == "img":
                self.imgs.append(a.get("src"))
        elif tag == "meta" and (a.get("name") or "").lower() == "viewport":
            self.has_viewport = True
        elif tag == "h1":
            self.h1 += 1
        elif tag == "input" and (a.get("type") or "") in ("search", ""):
            self.has_search = True
        if tag == "title":
            self.in_title = True
    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
    def handle_data(self, data):
        if self.in_title:
            self.title += data


def resolve(base: Path, href: str):
    """Return a site-relative Path for an internal href, or None if external."""
    u = urlparse(href)
    if u.scheme in ("http", "https", "mailto", "data", "tel"):
        return None
    if href.startswith("#"):
        return ("anchor", base, href[1:])
    path = unquote(u.path)
    if not path:
        return ("anchor", base, u.fragment)
    target = (SITE / path.lstrip("/")) if path.startswith("/") else (base.parent / path)
    target = target.resolve()
    if path.endswith("/"):
        target = target / "index.html"
    if u.fragment:
        return ("file+anchor", target, u.fragment)
    return ("file", target, None)


def main(attempt=1):
    # the publisher swaps site -> site.prev -> site; wait out that window instead of failing
    for _ in range(50):
        if (SITE / "index.html").exists():
            break
        time.sleep(0.1)
    pages = sorted(SITE.rglob("*.html"))
    if not pages:
        print("no pages built"); return 1
    dead, missing_asset, no_viewport, no_h1, no_search = [], [], [], [], []
    ids = {}
    parsed = {}
    for p in pages:
        try:
            pr = P()
            pr.feed(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            dead.append((str(p.relative_to(SITE)), f"parse error {e}"))
            continue
        parsed[p] = pr
        ids[p] = pr.ids
        if not pr.has_viewport and len(pages) > 1:
            no_viewport.append(p)
        if pr.h1 != 1:
            no_h1.append((str(p.relative_to(SITE)), pr.h1))
        if not pr.has_search and p.name not in ("robots.txt",):
            no_search.append(p)

    internal = 0
    for p in pages:
        pr = parsed.get(p)
        if pr is None:      # a concurrent atomic publish swapped the tree under us
            continue
        for href in pr.links:
            r = resolve(p, href)
            if r is None:
                continue
            internal += 1
            kind, target, frag = r
            if kind == "anchor":
                if frag and frag not in ids.get(p, set()):
                    dead.append((str(p.relative_to(SITE)), f"#{frag}"))
                continue
            tp = Path(target)
            if not tp.exists() and not tp.suffix:
                tp = tp.with_suffix(".html")
            if not tp.exists():
                dead.append((str(p.relative_to(SITE)), href))
                continue
            if frag and tp.suffix == ".html" and tp in ids and frag not in ids[tp]:
                dead.append((str(p.relative_to(SITE)), f"{href} (missing anchor #{frag})"))
        for tag, src in pr.scripts:
            if not src or src.startswith(("http", "//", "data:")):
                continue
            if tag == "link" and "icon" in str(src):
                continue
            r = resolve(p, src)
            if r and r[0] in ("file", "file+anchor") and not Path(r[1]).exists():
                missing_asset.append((str(p.relative_to(SITE)), src))

    # search index coverage
    idx_ok, idx_total, shards = True, 0, 0
    mf = SITE / "data" / "idx" / "manifest.json"
    if mf.exists():
        m = json.loads(mf.read_text())
        idx_total = m.get("total", 0)
        shards = len(m.get("shards", {}))
        for k in m.get("shards", {}):
            if not (SITE / "data" / "idx" / f"{k}.json").exists():
                idx_ok = False
    else:
        idx_ok = False

    # reachability from home
    seen, stack = set(), [SITE / "index.html"]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in parsed:
            continue
        seen.add(cur)
        for href in parsed[cur].links:
            r = resolve(cur, href)
            if r and r[0] in ("file", "file+anchor"):
                tgt = Path(r[1])
                if not tgt.exists() and not tgt.suffix:
                    tgt = tgt.with_suffix(".html")
                if tgt.exists() and tgt.suffix == ".html":
                    stack.append(tgt)
    # 404.html is served by the worker for unknown paths, so nothing links to it on purpose
    UNLINKED = {"404.html"}
    orphans = [p for p in pages if p not in seen and str(p.relative_to(SITE)) not in UNLINKED]

    print(f"pages: {len(pages)}  internal links: {internal}  reachable from home: {len(seen)}"
          f"  orphans: {len(orphans)}")
    print(f"search index: {idx_total:,} records in {shards} shards {'OK' if idx_ok else 'MISSING'}")
    print(f"dead links: {len(dead)}   missing assets: {len(missing_asset)}")
    print(f"pages missing viewport meta: {len(no_viewport)}   pages without exactly one h1: {len(no_h1)}"
          f"   pages without a search field: {len(no_search)}")
    if not QUIET:
        for d in dead[:25]:
            print("  DEAD", d[0], "->", d[1])
        for m in missing_asset[:10]:
            print("  ASSET", m[0], "->", m[1])
        for h in no_h1[:10]:
            print("  H1", h)
        for o in orphans[:10]:
            print("  ORPHAN", o.relative_to(SITE))
    ok = not dead and not missing_asset and idx_ok and not no_viewport and not no_search and not orphans
    # a publish can swap the tree under the crawl; if almost everything looks orphaned, that is what
    # happened, so try again against the new generation instead of reporting a phantom disaster
    if not ok and attempt < 4 and (len(orphans) > max(50, len(pages) // 2) or not pages):
        print(f"  (publish detected mid-crawl — retrying, attempt {attempt})")
        time.sleep(3)
        return main(attempt + 1)
    print("SITE OK" if ok else "SITE HAS PROBLEMS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
