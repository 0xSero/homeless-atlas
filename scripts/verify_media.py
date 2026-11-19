#!/usr/bin/env python3
"""Re-verify every video and image record and retire the ones that no longer resolve.

A link that 404s or a YouTube video that has been removed is not public media any more; presenting it
is a broken claim. Retired rows keep their record but get kind='video_unavailable'/'image_unavailable'
and are excluded from the site and the search index.

Run: python3 scripts/verify_media.py [--limit N]
"""
from __future__ import annotations
import argparse, json, re, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, fetch_json, now  # noqa: E402


def youtube_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    c = sqlite3.connect(DB, timeout=300)
    c.execute("PRAGMA busy_timeout=300000")
    c.row_factory = sqlite3.Row
    rows = c.execute("""SELECT id, kind, url, title FROM media
                        WHERE kind IN ('video','image') AND url LIKE 'http%'""").fetchall()
    if a.limit:
        rows = rows[:a.limit]
    ok = dead = 0
    for i, r in enumerate(rows, 1):
        good = False
        vid = youtube_id(r["url"])
        if vid:
            try:
                d = fetch_json(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json",
                               domain="media")
                good = bool(d.get("title"))
            except Exception:
                good = False
        else:
            try:
                import requests
                resp = requests.get(r["url"], timeout=25, stream=True,
                                    headers={"User-Agent": "homeless-atlas/1.0"})
                good = resp.status_code == 200
                resp.close()
            except Exception:
                good = False
        if good:
            ok += 1
        else:
            dead += 1
            newkind = "video_unavailable" if r["kind"] == "video" else "image_unavailable"
            c.execute("UPDATE media SET kind=? WHERE id=?", (newkind, r["id"]))
            c.commit()
        if i % 100 == 0:
            print(f"  {i}/{len(rows)} ok={ok} dead={dead}", flush=True)
    c.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
              ("media_verify", now(), ok, f"ok={ok} retired={dead} checked={len(rows)}"))
    c.commit()
    print(json.dumps({"checked": len(rows), "ok": ok, "retired": dead}))


if __name__ == "__main__":
    main()
