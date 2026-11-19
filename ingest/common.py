"""Shared ingest helpers. Every domain ingestor imports from here."""
from __future__ import annotations
import hashlib, json, os, re, sqlite3, sys, time, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "atlas.db"
RAW = ROOT / "data" / "raw"
LOGS = ROOT / "logs"
UA = "homeless-atlas/1.0 (public-data research; contact: 0xsero@users.noreply.github.com)"

_last = [0.0]
RATE = 1.0  # seconds between requests, global per process


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=300)
    conn.row_factory = sqlite3.Row
    # durability over speed: this is a public-record archive, not a cache
    conn.execute("PRAGMA busy_timeout=300000")
    conn.execute("PRAGMA synchronous=FULL")
    with open(ROOT / "ingest" / "schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _throttle():
    dt = time.time() - _last[0]
    if dt < RATE:
        time.sleep(RATE - dt)
    _last[0] = time.time()


def _log(domain: str, url: str, status, nbytes, note=""):
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / f"ingest-{domain}.jsonl", "a") as f:
        f.write(json.dumps({"ts": now(), "url": url, "status": status, "bytes": nbytes, "note": note}) + "\n")


def fetch(url: str, *, method="GET", data=None, headers=None, domain="misc", force=False,
          retries=3, timeout=180, binary=False):
    """GET/POST with disk cache in data/raw/<domain>/, 1 req/s, retries."""
    import requests
    RAW_D = RAW / domain
    RAW_D.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1((method + url + json.dumps(data or {}, sort_keys=True, default=str)).encode()).hexdigest()
    ext = ".bin"
    p = RAW_D / f"{key}{ext}"
    meta = p.with_suffix(".meta.json")
    if p.exists() and not force and p.stat().st_size > 0:
        return p.read_bytes()
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        _throttle()
        try:
            if method == "POST":
                r = requests.post(url, data=data, headers=h, timeout=timeout)
            else:
                r = requests.get(url, headers=h, timeout=timeout)
            _log(domain, url, r.status_code, len(r.content))
            if r.status_code == 200:
                p.write_bytes(r.content)
                meta.write_text(json.dumps({"url": url, "status": 200, "at": now(), "method": method,
                                            "data": data if isinstance(data, (str, dict)) else None}, default=str))
                return r.content
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(2 ** i * 2)
                continue
            raise RuntimeError(f"{url} -> HTTP {r.status_code}: {r.text[:300]}")
        except Exception as e:  # noqa
            last = repr(e)
            time.sleep(2 ** i * 2)
    raise RuntimeError(f"fetch failed after {retries}: {url}: {last}")


def fetch_json(url, **kw):
    b = fetch(url, **kw)
    return json.loads(b)


def fetch_post_json(url, payload, **kw):
    b = fetch(url, method="POST", data=json.dumps(payload),
              headers={"Content-Type": "application/json"}, **kw)
    return json.loads(b)


def save_raw(domain: str, name: str, content: bytes) -> Path:
    d = RAW / domain
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(content)
    return p


def source_id_for(conn, url: str, domain: str, *, title=None, publisher=None, license=None) -> int:
    sha = None
    key = hashlib.sha1(url.encode()).hexdigest()
    p = RAW / domain / f"{key}.bin"
    nbytes = None
    if p.exists():
        nbytes = p.stat().st_size
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
    cur = conn.execute(
        """INSERT INTO source(domain,url,title,publisher,license,retrieved_at,bytes,sha256)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(domain,url) DO UPDATE SET retrieved_at=excluded.retrieved_at,
             bytes=COALESCE(excluded.bytes,source.bytes), sha256=COALESCE(excluded.sha256,source.sha256),
             title=COALESCE(excluded.title,source.title), license=COALESCE(excluded.license,source.license)
           RETURNING id""", (domain, url, title, publisher, license, now(), nbytes, sha))
    return cur.fetchone()[0]


def upsert(conn, table: str, row: dict, keys: list[str]) -> None:
    row = {k: v for k, v in row.items() if v is not None or k in keys}
    if not row:
        return
    cols = list(row)
    ph = ",".join("?" * len(cols))
    sets = ",".join(f"{c}=COALESCE(excluded.{c},{table}.{c})" for c in cols if c not in keys)
    sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({ph}) ON CONFLICT({','.join(keys)}) DO UPDATE SET {sets or cols[0]+'='+cols[0]}"
    conn.execute(sql, [row[c] for c in cols])


_ORGSUF = re.compile(r"\b(INC|INCORPORATED|LLC|L\.?L\.?C|CORP|CORPORATION|CO|COMPANY|ASSOC|ASSOCIATION|"
                     r"FOUNDATION|FDN|THE|OF|FOR|A|AN|AND|DBA|NONPROFIT|NON-PROFIT|USA|US|"
                     r"INTERNATIONAL|NATIONAL|SOCIETY|ORGANIZATION|ORG|GROUP|SERVICES|SERVICE)\b")


def name_norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = _ORGSUF.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return s
    s = re.sub(r"\s+", " ", str(s).replace("\u00a0", " ")).strip()
    return s or None


def slug(s: str, maxlen=80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:maxlen] or "x"


def to_int(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int,)):
        return v
    if isinstance(v, float):
        return int(round(v))
    s = re.sub(r"[^0-9.\-]", "", str(v))
    if not s or s in ("-", "."):
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def selftest_report(conn, domain, table, where="1=1", n=3):
    rows = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()[0]
    print(f"[{domain}] rows in {table}: {rows}")
    for r in conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY RANDOM() LIMIT {n}"):
        print(dict(r))


def main(domain: str, fn, selftest_table: str | None = None):
    conn = db()
    t0 = time.time()
    try:
        res = fn(conn, {})
        conn.commit()
        rows = res.get("rows", 0) if isinstance(res, dict) else 0
        notes = res.get("notes", "") if isinstance(res, dict) else ""
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                     (domain, now(), rows, notes))
        conn.commit()
        print(f"[{domain}] OK rows={rows} {time.time()-t0:.1f}s {notes}")
        if selftest_table:
            selftest_report(conn, domain, selftest_table)
    except Exception as e:
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,0)",
                     (domain, now(), 0, repr(e)[:2000]))
        conn.commit()
        raise
