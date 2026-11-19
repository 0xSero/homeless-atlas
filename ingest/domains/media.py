"""media -- public videos and photographs about homelessness, mapped to a place.

What this domain owns
---------------------
`media` rows only (`kind='video'` for YouTube, `kind='image'` for Wikimedia Commons), plus
`metric` rows `entity_type='media' / 'youtube_views'` for the view counts (the `media` table has
no numeric column for them and the schema is owned elsewhere).

Provenance model
----------------
One `source` row per *search response we actually fetched*:

* YouTube  -- the yt-dlp search response for `ytsearch50:<query>`, cached at
  `data/raw/media/<sha1(search_url)>.bin`, with `url` = the human-openable
  `https://www.youtube.com/results?search_query=...` page. Every video row produced by that
  search carries that `source_id`. The search feed itself contains the title/channel/duration/
  view-count/description we store, so nothing is inferred.
* Commons  -- the MediaWiki `action=query&generator=search` JSON, fetched through
  `common.fetch` (1 req/s, retries, cache, `source`-log), `url` = the exact API URL.
* Per-video detail -- the second pass re-reads each watch page through yt-dlp (for upload date,
  exact duration, view count and the uploader's own licence when it is Creative Commons). Those
  responses are cached one file per video under `data/raw/media/detail/` and indexed in
  `media_video_detail`. They *update* existing rows; they never invent a new one.

Fields a publisher does not give stay NULL. `license` is a string for every row: Commons rows
carry the file's `LicenseShortName`, YouTube rows carry the uploader-declared licence when there
is one and the literal `unknown` otherwise (YouTube "standard" is not an open licence -- see
research/media.md).

Tools
-----
Needs yt-dlp. The harness advertises `~/.local/bin/yt-dlp`, but on this host that entry point is
broken (`ModuleNotFoundError: yt_dlp`) because the module is not installed for python3.10; a
working yt-dlp exists in another venv. `_ytdlp()` therefore probes candidates (env override
`MEDIA_YTDLP`, `PATH`, `~/.local/bin`, a couple of known venvs) and uses the first one whose
`--version` succeeds. Nothing else is required beyond `requests`.

Resumable: every network response is cached, so a re-run re-parses the cache. The expensive
second pass is capped per run (`MEDIA_DETAIL_MAX`, default 600 videos) and skips ids already in
`media_video_detail`, so N runs enrich N*600 videos without repeating work.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

from ingest.common import (RAW, clean, db, fetch, now, source_id_for, upsert)

DOMAIN = "media"

# --------------------------------------------------------------------------- config
CITIES_N = int(os.environ.get("MEDIA_CITIES", "12"))
DETAIL_MAX = int(os.environ.get("MEDIA_DETAIL_MAX", "600"))
SEARCH_N = 50            # `ytsearch50:` per the assignment
COMMONS_LIMIT = 50       # gsrlimit
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

YT_LICENSE_UNKNOWN = "unknown"
YT_LICENSE_NOTE = "YouTube: no licence declared by uploader"
COMMONS_LICENSE_UNKNOWN = "unknown"

# The four query shapes from the assignment. `{c}` is filled with the place name taken from
# `geo_city` (never typed by hand); the city-less one runs once, nationally.
YT_CITY_QUERIES = [
    "homeless encampment {c}",
    "tent city {c}",
    "homeless sweep {c}",
]
YT_NATIONAL_QUERIES = ["skid row documentary"]

COMMONS_CITY_QUERIES = [
    "homeless {c}",
    "homeless encampment {c}",
    "tent city {c}",
]
COMMONS_TOPIC_QUERIES = [
    "homelessness in the United States",
    "skid row Los Angeles",
    "tent city",
    "Hooverville",
    "homeless camp",
    "homeless shelter",
    "Homelessness in California",
    "street medicine",
]

# Place names are pulled from the Census place layer already in the DB; these are the
# homelessness-reporting cities that are worth a dedicated sweep even when they are not in the
# top-N by population. Resolved against `geo_city` by (name, state) -- if absent they are skipped.
EXTRA_CITIES = [
    ("Seattle", "WA"), ("Portland", "OR"), ("San Francisco", "CA"), ("Oakland", "CA"),
    ("Sacramento", "CA"), ("Denver", "CO"), ("Boston", "MA"), ("Washington", "DC"),
    ("Honolulu", "HI"), ("Minneapolis", "MN"), ("Las Vegas", "NV"), ("Albuquerque", "NM"),
]

# --- relevance gates --------------------------------------------------------
# A search engine answer is not a curation: YouTube returns the band Skid Row for "skid row
# documentary" and Commons returns bus shelters for "homeless <city>". Rows whose own text has no
# homelessness term are dropped, and the drop counts are reported.
RE_TOPIC = re.compile(
    r"homeless|encamp\w*|tent city|skid row|unshelter\w*|shantytown|shanty ?town|hooverville|"
    r"vagran\w*|panhandl\w*|rv ?dwell\w*|vehicle ?dwell\w*|street ?dwell\w*|homelessn\w*|"
    r"tent ?(?:camp|village|encampment)|safe ?sleep\w*|navigation cent\w*",
    re.I)
# text that is clearly not about the topic even when a keyword matches (music, games, sport)
RE_NOISE = re.compile(
    r"\b(guitar|bassist|drummer|album|vinyl|riff|band|music video|lyrics|warcraft|guild|"
    r"raid boss|karaoke|playlist)\b", re.I)
RE_CORE = re.compile(r"homeless|encamp|tent city|skid row|unshelter|shelter|hooverville", re.I)

IMG_OK_EXT = (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp")


# --------------------------------------------------------------------------- helpers
def _log(url: str, status, nbytes, note: str = "") -> None:
    d = Path(__file__).resolve().parent.parent.parent / "logs"
    d.mkdir(exist_ok=True)
    with open(d / f"ingest-{DOMAIN}.jsonl", "a") as f:
        f.write(json.dumps({"ts": now(), "url": url, "status": status,
                            "bytes": nbytes, "note": note}) + "\n")


def _sha(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def _raw_path(url: str) -> Path:
    """Cache path `source_id_for` looks for, so bytes/sha256 land on the source row."""
    d = RAW / DOMAIN
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_sha(url)}.bin"


def _source(conn, url, *, title, publisher, license) -> int:
    """Register the source row and fill bytes/sha256 from the cache file for that url."""
    sid = source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)
    p = _raw_path(url)
    if p.exists() and p.stat().st_size > 0:
        b = p.read_bytes()
        conn.execute("UPDATE source SET bytes=?, sha256=?, retrieved_at=? WHERE id=?",
                     (len(b), hashlib.sha256(b).hexdigest(), now(), sid))
    return sid


def _strip_html(s) -> str | None:
    if s is None:
        return None
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _date_iso(s) -> str | None:
    """`2015-04-07 23:32` / `20150407` -> `2015-04-07`; anything else -> None."""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _ytdlp() -> list[str] | None:
    """First yt-dlp invocation that actually answers `--version`."""
    cands: list[list[str]] = []
    env = os.environ.get("MEDIA_YTDLP")
    if env:
        cands.append([env])
    for name in ("yt-dlp",):
        w = shutil.which(name)
        if w:
            cands.append([w])
    cands.append([str(Path.home() / ".local" / "bin" / "yt-dlp")])
    home = Path.home()
    for pat in ("*/tools/.venv/bin/yt-dlp", "*/.venv/bin/yt-dlp", "*/*/.venv/bin/yt-dlp"):
        for p in sorted(home.glob(pat))[:4]:
            cands.append([str(p)])
    seen = set()
    for cmd in cands:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        try:
            r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=90)
        except Exception:
            continue
        if r.returncode == 0 and re.match(r"^\d{4}\.", (r.stdout or "").strip()):
            return cmd
    return None


def _yt_json(cmd, args: list[str], url_for_cache: str, *, timeout=600, note=""):
    """Run yt-dlp for one search, cache the raw JSON at the source-cache path, return it."""
    p = _raw_path(url_for_cache)
    if p.exists() and p.stat().st_size > 0:
        return json.loads(p.read_bytes())
    r = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not (r.stdout or "").strip():
        _log(url_for_cache, r.returncode, 0, (r.stderr or "")[-300:])
        raise RuntimeError(f"yt-dlp failed ({r.returncode}): {(r.stderr or '')[-300:]}")
    p.write_bytes(r.stdout.encode())
    _log(url_for_cache, 200, len(r.stdout), note)
    return json.loads(r.stdout)


def _cities(conn) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen = set()
    for r in conn.execute(
            "SELECT name, state, pop FROM geo_city WHERE kind='place' AND pop IS NOT NULL "
            "ORDER BY pop DESC LIMIT ?", (CITIES_N,)):
        k = (r["name"], r["state"])
        if k not in seen:
            seen.add(k)
            out.append(k)
    for name, st in EXTRA_CITIES:
        row = conn.execute("SELECT name, state FROM geo_city WHERE kind='place' AND name=? "
                           "AND state=? ORDER BY pop DESC LIMIT 1", (name, st)).fetchone()
        if row and (row["name"], row["state"]) not in seen:
            seen.add((row["name"], row["state"]))
            out.append((row["name"], row["state"]))
    return out


def _relevant(text: str) -> bool:
    if not text or not RE_TOPIC.search(text):
        return False
    if RE_NOISE.search(text) and not re.search(r"homeless|encamp", text, re.I):
        return False
    return True


# --------------------------------------------------------------------------- schema (private)
DDL = """
CREATE TABLE IF NOT EXISTS media_progress (
  step     TEXT PRIMARY KEY,
  n_items  INTEGER,
  n_kept   INTEGER,
  ok       INTEGER,
  ts       TEXT
);
CREATE TABLE IF NOT EXISTS media_video_detail (
  video_id    TEXT PRIMARY KEY,
  status      TEXT,          -- ok | gone | error
  upload_date TEXT,
  duration    INTEGER,
  views       INTEGER,
  license     TEXT,
  ts          TEXT
);
CREATE TABLE IF NOT EXISTS media_query (
  query     TEXT PRIMARY KEY,
  place     TEXT,
  state     TEXT,
  kind      TEXT,            -- video | image
  n_found   INTEGER,
  n_kept    INTEGER,
  source_id INTEGER
);
"""


def _put(conn, table, row, keys):
    upsert(conn, table, row, keys)


def _progress(conn, step, n_items, n_kept, ok=1):
    _put(conn, "media_progress",
         {"step": step, "n_items": n_items, "n_kept": n_kept, "ok": ok, "ts": now()}, ["step"])


# --------------------------------------------------------------------------- YouTube search
def ingest_youtube(conn, cmd, stats) -> None:
    cities = _cities(conn)
    queries: list[tuple[str, str | None, str | None]] = []
    for nq in YT_NATIONAL_QUERIES:
        queries.append((nq, None, None))
    for city, st in cities:
        for tpl in YT_CITY_QUERIES:
            queries.append((tpl.format(c=city), city, st))

    for q, place, state in queries:
        url = ("https://www.youtube.com/results?search_query="
               + urllib.parse.quote_plus(q))
        args = ["--dump-single-json", "--flat-playlist", "--no-warnings", "--ignore-config",
                f"ytsearch{SEARCH_N}:{q}"]
        try:
            js = _yt_json(cmd, args, url, note=f"ytsearch{SEARCH_N}:{q}")
        except Exception as e:                                    # keep going, one query lost
            stats["errors"].append(f"yt {q}: {e}")
            _progress(conn, f"yt:{q}", 0, 0, ok=0)
            conn.commit()
            continue
        # registered *after* the response is cached so the source row gets bytes + sha256
        sid = _source(conn, url,
                      title=f"yt-dlp search response for ytsearch{SEARCH_N}:{q}",
                      publisher="YouTube (via yt-dlp)",
                      license="YouTube terms of service; individual video licences recorded per row")
        entries = js.get("entries") or []
        kept = 0
        for e in entries:
            vid = e.get("id")
            title = clean(e.get("title"))
            if not vid or not title:
                continue
            desc = clean(e.get("description"))
            if not _relevant(f"{title} {desc or ''}"):
                stats["yt_dropped"] += 1
                continue
            watch = f"https://www.youtube.com/watch?v={vid}"
            thumbs = e.get("thumbnails") or []
            thumb = None
            if thumbs:
                t = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
                thumb = t.get("url")
            _put(conn, "media", {
                "kind": "video",
                "title": title,
                "url": watch,
                "thumbnail": thumb,
                "publisher": clean(e.get("channel") or e.get("uploader")),
                "author": clean(e.get("uploader")),
                "published": _date_iso(e.get("upload_date")),
                "place": place,
                "state": state,
                "license": YT_LICENSE_UNKNOWN,
                "duration": e.get("duration") if isinstance(e.get("duration"), int) else None,
                "summary": (desc[:600] if desc else None),
                "source_id": sid,
            }, ["url"])
            kept += 1
            # view count: the media table has no column, so it lands in `metric`
            vc = e.get("view_count")
            if isinstance(vc, int):
                _put_views(conn, watch, vc, None, sid)
            stats["videos"] += 1
        _progress(conn, f"yt:{q}", len(entries), kept)
        _put(conn, "media_query", {"query": q, "place": place, "state": state, "kind": "video",
                                   "n_found": len(entries), "n_kept": kept, "source_id": sid},
             ["query"])
        conn.commit()
        print(f"  yt  {kept:3d}/{len(entries):2d}  {q}", flush=True)
    stats["cities"] = cities


def _put_views(conn, entity_id: str, views: int, year: int | None, sid: int) -> None:
    """`metric.youtube_views` is keyed (entity, metric, year); NULL year would duplicate."""
    if year is None:
        conn.execute("DELETE FROM metric WHERE entity_type='media' AND entity_id=? "
                     "AND metric='youtube_views' AND year IS NULL", (entity_id,))
    upsert(conn, "metric", {
        "entity_type": "media", "entity_id": entity_id, "metric": "youtube_views",
        "value": float(views), "unit": "views", "year": year, "is_est": 0,
        "note": f"view count as served by YouTube at retrieval ({now()[:10]})",
        "source_id": sid,
    }, ["entity_type", "entity_id", "metric", "year"])


# --------------------------------------------------------------------------- YouTube detail pass
def ingest_youtube_detail(conn, cmd, stats) -> None:
    """Second pass: per-video metadata (upload date, exact duration, views, CC licence).

    Resumable: ids recorded in `media_video_detail` are skipped, batches are cached as NDJSON
    under data/raw/media/detail/. Capped at DETAIL_MAX new videos per run.
    """
    if not cmd or DETAIL_MAX <= 0:
        return
    done = {r[0] for r in conn.execute("SELECT video_id FROM media_video_detail")}
    todo = []
    for r in conn.execute("SELECT url, source_id FROM media WHERE kind='video' "
                          "AND url LIKE 'https://www.youtube.com/watch%' ORDER BY url"):
        vid = r[0].rsplit("=", 1)[-1]
        if vid not in done:
            todo.append((vid, r[0], r[1]))
    todo = todo[:DETAIL_MAX]
    stats["detail_pending"] = max(0, len(todo))
    ddir = RAW / DOMAIN / "detail"
    ddir.mkdir(parents=True, exist_ok=True)
    B = 15
    for i in range(0, len(todo), B):
        batch = todo[i:i + B]
        ids = [b[0] for b in batch]
        key = _sha(",".join(ids))
        cache = ddir / f"{key}.jsonl"
        if cache.exists():
            lines = cache.read_text().splitlines()
        else:
            cmdv = cmd + ["--dump-json", "--skip-download", "--no-warnings", "--ignore-config",
                          "--ignore-errors", "--no-playlist"]
            r = subprocess.run(cmdv + [b[1] for b in batch], capture_output=True, text=True,
                               timeout=900)
            lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
            cache.write_text("\n".join(lines) + ("\n" if lines else ""))
            _log(f"yt-dlp detail batch {ids[0]}..{ids[-1]}", r.returncode,
                 len(r.stdout or ""), f"{len(ids)} videos")
        got = {}
        for ln in lines:
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("id"):
                got[o["id"]] = o
        for vid, watch, sid in batch:
            o = got.get(vid)
            if not o:
                _put(conn, "media_video_detail",
                     {"video_id": vid, "status": "gone", "ts": now()}, ["video_id"])
                stats["detail_gone"] += 1
                continue
            pub = clean(_date_iso(o.get("upload_date")))
            dur = o.get("duration") if isinstance(o.get("duration"), int) else None
            vc = o.get("view_count") if isinstance(o.get("view_count"), int) else None
            lic = clean(o.get("license"))
            year = int(pub[:4]) if pub else None
            row = {"published": pub, "duration": dur, "license": lic, "url": watch}
            if pub or dur or lic:                    # upsert keys on url; never insert a ghost
                _put(conn, "media", row, ["url"])
            if vc is not None:
                _put_views(conn, watch, vc, year, sid)
            _put(conn, "media_video_detail",
                 {"video_id": vid, "status": "ok", "upload_date": pub, "duration": dur,
                  "views": vc, "license": lic, "ts": now()}, ["video_id"])
            stats["detail_ok"] += 1
        conn.commit()
        print(f"  detail {i + len(batch)}/{len(todo)} ok={stats['detail_ok']}", flush=True)


# --------------------------------------------------------------------------- Commons
def commons_query(conn, q: str, place, state, stats) -> None:
    params = {
        "action": "query", "generator": "search", "gsrsearch": q, "gsrnamespace": "6",
        "gsrlimit": str(COMMONS_LIMIT), "prop": "imageinfo",
        "iiprop": "url|extmetadata|size|mime", "iiurlwidth": "640",
        "iiextmetadatafilter": ("LicenseShortName|License|Artist|DateTimeOriginal|"
                                "ImageDescription|Credit|UsageTerms|ObjectName"),
        "format": "json", "formatversion": "2",
    }
    url = COMMONS_API + "?" + urllib.parse.urlencode(params)
    # provenance bytes live at the source-cache path; reuse it if this run already has it
    p = _raw_path(url)
    try:
        if p.exists() and p.stat().st_size > 0:
            js = json.loads(p.read_bytes())
        else:
            b = fetch(url, domain=DOMAIN)
            p.write_bytes(b)
            js = json.loads(b)
    except Exception as e:
        stats["errors"].append(f"commons {q}: {e}")
        _progress(conn, f"commons:{q}", 0, 0, ok=0)
        conn.commit()
        return
    sid = _source(conn, url,
                  title=f"Wikimedia Commons search: {q} (namespace 6, {COMMONS_LIMIT} files)",
                  publisher="Wikimedia Commons (MediaWiki API)",
                  license="per file: extmetadata LicenseShortName (Commons hosts free licences only)")
    pages = (js.get("query") or {}).get("pages") or []
    kept = 0
    for pg in pages:
        ii = (pg.get("imageinfo") or [{}])[0]
        title = clean(pg.get("title"))
        furl = ii.get("url")
        if not title or not furl:
            continue
        if not title.lower().endswith(IMG_OK_EXT):
            stats["img_dropped"] += 1
            continue
        meta = ii.get("extmetadata") or {}

        def mv(k):
            v = (meta.get(k) or {}).get("value")
            return _strip_html(v)

        desc = mv("ImageDescription") or mv("ObjectName")
        if not _relevant(f"{title} {desc or ''}"):
            stats["img_dropped"] += 1
            continue
        lic = mv("LicenseShortName") or mv("UsageTerms") or COMMONS_LICENSE_UNKNOWN
        _put(conn, "media", {
            "kind": "image",
            "title": title[len("File:"):] if title.startswith("File:") else title,
            "url": furl,
            "thumbnail": ii.get("thumburl") or furl,
            "publisher": "Wikimedia Commons",
            "author": mv("Artist") or mv("Credit"),
            "published": _date_iso(mv("DateTimeOriginal")),
            "place": place,
            "state": state,
            "license": lic,
            "duration": None,
            "summary": (desc[:600] if desc else None),
            "source_id": sid,
        }, ["url"])
        kept += 1
        stats["images"] += 1
    _progress(conn, f"commons:{q}", len(pages), kept)
    _put(conn, "media_query", {"query": q, "place": place, "state": state, "kind": "image",
                               "n_found": len(pages), "n_kept": kept, "source_id": sid},
         ["query"])
    conn.commit()
    print(f"  img {kept:3d}/{len(pages):2d}  {q}", flush=True)


def ingest_commons(conn, stats) -> None:
    for q in COMMONS_TOPIC_QUERIES:
        commons_query(conn, q, None, None, stats)
    for city, st in _cities(conn):
        for tpl in COMMONS_CITY_QUERIES:
            commons_query(conn, tpl.format(c=city), city, st, stats)


# --------------------------------------------------------------------------- run
def run(conn, cfg=None) -> dict:
    conn.executescript(DDL)
    stats = {"videos": 0, "images": 0, "yt_dropped": 0, "img_dropped": 0,
             "detail_ok": 0, "detail_gone": 0, "detail_pending": 0,
             "errors": [], "cities": []}
    cmd = _ytdlp()
    if not cmd:
        stats["errors"].append("no working yt-dlp found (see BLOCKERS.md ## media)")
        print("  !! no working yt-dlp; skipping YouTube", flush=True)
    else:
        print(f"  yt-dlp: {' '.join(cmd)}", flush=True)
        ingest_youtube(conn, cmd, stats)
        ingest_youtube_detail(conn, cmd, stats)
    ingest_commons(conn, stats)
    conn.commit()

    n_vid = conn.execute("SELECT COUNT(*) FROM media WHERE kind='video'").fetchone()[0]
    n_img = conn.execute("SELECT COUNT(*) FROM media WHERE kind='image'").fetchone()[0]
    n_unlic = conn.execute("SELECT COUNT(*) FROM media WHERE license IS NULL OR license='' "
                           "OR license='unknown'").fetchone()[0]
    notes = (f"media rows video={n_vid} image={n_img} license_unknown={n_unlic}; "
             f"this run wrote videos={stats['videos']} images={stats['images']}; "
             f"detail ok={stats['detail_ok']} gone={stats['detail_gone']} "
             f"pending={stats['detail_pending']}; "
             f"dropped off-topic yt={stats['yt_dropped']} img={stats['img_dropped']}; "
             f"cities={len(stats['cities'])}"
             + ("; ERRORS: " + " || ".join(stats["errors"]) if stats["errors"] else ""))
    sources = [(r[0], r[1]) for r in conn.execute(
        "SELECT url, license FROM source WHERE domain=? ORDER BY id DESC LIMIT 400", (DOMAIN,))]
    queri = conn.execute("SELECT COUNT(*) FROM media_query").fetchone()[0]
    return {"rows": n_vid + n_img, "notes": notes,
            "detail": {"videos": n_vid, "images": n_img, "queries": queri,
                       "this_run": stats["videos"] + stats["images"],
                       "errors": stats["errors"]}, "sources": sources}


# --------------------------------------------------------------------------- cli
def _selftest():
    conn = db()
    res = run(conn, {})
    conn.commit()
    print(json.dumps({"rows": res["rows"], "detail": res["detail"]}, indent=1))
    print("--- sample media rows ---")
    for r in conn.execute("SELECT kind,title,publisher,place,state,license,published,duration,"
                          "substr(url,1,70) url FROM media ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))
    print("--- by kind/licence ---")
    for r in conn.execute("SELECT kind, COALESCE(NULLIF(license,''),'NULL') lic, COUNT(*) n "
                          "FROM media GROUP BY 1,2 ORDER BY n DESC LIMIT 12"):
        print(dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        conn = db()
        res = run(conn, {})
        conn.commit()
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                     (DOMAIN, now(), res["rows"], res["notes"][:2000]))
        conn.commit()
        print(json.dumps({"rows": res["rows"], "detail": res["detail"]}, indent=1))
