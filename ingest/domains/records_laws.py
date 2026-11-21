"""records_laws: verify the hand-curated public records law table against the official text.

`ingest/records_laws.yaml` holds one entry per jurisdiction (50 states, DC, and US for the
federal FOIA). Every legal fact in it carries a short exact `quote` and the `quote_url` of the
official page that contains it (the legislature, the state code site, or, where the code site is
unusable, the attorney general's or open records office's own guide, marked
`basis: official_guide`). This module never edits the YAML. It fetches each quote_url through
ingest.fetch_ladder.get, extracts the text (pdftotext for PDFs), normalises whitespace, quote
marks, dashes and section symbols on both sides, and checks that every quote appears.

Tables
------
records_law(jurisdiction PK, data_json, fields_total, fields_verified, verified_at, source_id)
    data_json is the YAML entry plus, on every quoted field, `verified` (bool), `via` (the ladder
    rung that produced the text) and `verified_at`. A field whose quote fails is kept but marked
    verified=false; the site and the letter generator use only verified fields.
records_law_check(jurisdiction, field, quote_url, via, result, checked_at, PK(jurisdiction, field))
    one row per quoted field: result is `ok` or the reason it failed.

Run
---
    python3 -m ingest.domains.records_laws            # verify all, write tables, print table
    python3 -m ingest.domains.records_laws --only PA,CA
    python3 -m ingest.domains.records_laws --force    # refetch instead of using the cache
    python3 -m ingest.domains.records_laws --selftest

Exit status is non-zero when a jurisdiction is missing or any jurisdiction has an unverified
`who_may_request` or `response` field.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from html import unescape
from pathlib import Path

import yaml

from ingest.common import ROOT, now, source_id_for

DOMAIN = "records_laws"
YAML_PATH = ROOT / "ingest" / "records_laws.yaml"
REQUIRED = ("who_may_request", "response")
STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
          "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY US").split()

# fields that may carry quotes; anything else in an entry is plain metadata
QUOTED_FIELDS = ("law_name", "custodian_term", "who_may_request", "response", "written_required",
                 "must_cite_statute", "form_required", "fees", "fee_waiver", "deemed_denial",
                 "appeal", "letter_requirements", "fee_categories", "route_hud", "route_va", "route_hhs",
                 "appeal_hud", "appeal_va", "appeal_hhs", "hud_coc_public_posting")


# --------------------------------------------------------------------------- text
_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"), "-")
_QUOTES = {ord(c): "'" for c in "‘’‚‛′´`"}
_QUOTES.update({ord(c): '"' for c in "“”„‟″"})


def norm(s: str) -> str:
    """Comparison form: NFKC, straight quotes, ASCII hyphens, no section or paragraph symbols,
    no soft hyphens, single spaces, lower case."""
    s = unicodedata.normalize("NFKC", unescape(s or ""))
    s = s.translate(_DASHES).translate(_QUOTES)
    s = s.replace("­", "").replace("§", " ").replace("¶", " ")
    s = re.sub(r"\s+", " ", s)
    # spacing around punctuation differs between HTML renderings and PDF text layers
    s = re.sub(r"\s+([,.;:)\]])", r"\1", s)
    s = re.sub(r"([(\[])\s+", r"\1", s)
    return s.strip().lower()


def _cf_email(hexstr: str) -> str:
    k = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i + 2], 16) ^ k) for i in range(2, len(hexstr), 2))


def _html_text(html: str) -> str:
    # Cloudflare email protection replaces addresses with "[email protected]"; decode them back
    html = re.sub(r'(?is)<(a|span)\b[^>]*data-cfemail="([0-9a-f]+)"[^>]*>.*?</\1>',
                  lambda m: _cf_email(m.group(2)), html)
    html = re.sub(r'(?i)/cdn-cgi/l/email-protection#([0-9a-f]+)',
                  lambda m: "mailto:" + _cf_email(m.group(1)), html)
    t = re.sub(r"(?is)<(script|style|noscript|template|svg|head)\b.*?</\1\s*>", " ", html)
    t = re.sub(r"(?i)<br\s*/?>|</?(p|div|li|tr|td|h\d)\b[^>]*>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", "", t)  # inline tags joined without a space (<i>x</i>y)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def _json_strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _json_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_strings(v, out)


def extract_text(body: bytes, content_type: str | None) -> str:
    """Readable text of a fetched document: PDF via pdftotext, JSON string values, else HTML."""
    if not body:
        return ""
    if body.lstrip()[:5] == b"%PDF-":
        exe = shutil.which("pdftotext")
        if not exe:
            return ""
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(body)
            f.flush()
            out = subprocess.run([exe, "-enc", "UTF-8", f.name, "-"], capture_output=True,
                                 timeout=120)
        return out.stdout.decode("utf-8", "replace")
    text = body.decode("utf-8", "replace")
    if "json" in (content_type or "").lower() or text.lstrip()[:1] in "[{":
        try:
            parts: list[str] = []
            _json_strings(json.loads(text), parts)
            return _html_text(" ".join(parts))
        except ValueError:
            pass
    return _html_text(text)


_TEXT_CACHE: dict[str, tuple[str, str, str]] = {}


def page_text(url: str, force: bool = False) -> tuple[str, str, str]:
    """(normalised text, via, status) for a URL, fetched once per process."""
    if url in _TEXT_CACHE and not force:
        return _TEXT_CACHE[url]
    from ingest.fetch_ladder import attempts_summary, get
    r = get(url, domain=DOMAIN, want_text=True, force=force)
    if not r.ok:
        out = ("", "", f"fetch {r.status}: {attempts_summary(r.attempts)[:200]}")
    else:
        out = (norm(extract_text(r.body or b"", r.content_type)), r.via or "", r.status)
    _TEXT_CACHE[url] = out
    return out


# --------------------------------------------------------------------------- checking
def quotes_of(field: dict) -> list[tuple[str, str]]:
    """Every (quote, quote_url) pair a field carries, nested ones included."""
    out = []

    def walk(d):
        if isinstance(d, dict):
            if d.get("quote") and d.get("quote_url"):
                out.append((d["quote"], d["quote_url"]))
            for k, v in d.items():
                if k not in ("quote", "quote_url"):
                    walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)
    walk(field)
    return out


def check_field(field: dict, force: bool = False) -> tuple[bool, str, str, str]:
    """(ok, first quote_url, via, result) for one quoted field."""
    pairs = quotes_of(field)
    if not pairs:
        return False, "", "", "no quote"
    vias, problems = [], []
    for i, (q, url) in enumerate(pairs, 1):
        if len(q.split()) > 40:
            problems.append(f"quote {i} over 40 words")
            continue
        text, via, status = page_text(url, force)
        vias.append(via or status)
        if not text:
            problems.append(f"quote {i}: {status}")
        elif norm(q) not in text:
            problems.append(f"quote {i} not found at {url}")
    return (not problems, pairs[0][1], ",".join(dict.fromkeys(v for v in vias if v)),
            "ok" if not problems else "; ".join(problems))


def load_yaml(path: Path = YAML_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def verify_entry(jur: str, entry: dict, force: bool = False) -> tuple[dict, list[tuple]]:
    """Return the entry with verification flags added, and the check rows."""
    data = json.loads(json.dumps(entry))  # deep copy, JSON-safe
    checks, ts = [], now()
    for name in QUOTED_FIELDS:
        f = data.get(name)
        if not isinstance(f, dict) or not quotes_of(f):
            continue
        ok, url, via, result = check_field(f, force)
        f["verified"], f["via"], f["verified_at"] = ok, via, ts if ok else None
        checks.append((jur, name, url, via, result, ts))
    # Consumers (the flyer, the state guide) read `law_name` as a short string.
    ln = data.get("law_name")
    if isinstance(ln, dict):
        data["law_name_detail"] = ln
        data["law_name"] = ln.get("short") or ln.get("value")
    return data, checks


SCHEMA = """
CREATE TABLE IF NOT EXISTS records_law(
  jurisdiction TEXT PRIMARY KEY, data_json TEXT, fields_total INTEGER, fields_verified INTEGER,
  verified_at TEXT, source_id INTEGER);
CREATE TABLE IF NOT EXISTS records_law_check(
  jurisdiction TEXT, field TEXT, quote_url TEXT, via TEXT, result TEXT, checked_at TEXT,
  PRIMARY KEY(jurisdiction, field));
"""


def run(conn, cfg) -> dict:
    cfg = cfg or {}
    conn.executescript(SCHEMA)
    laws = load_yaml()
    only = cfg.get("only") or []
    out_rows, problems = [], []
    for jur in STATES:
        if only and jur not in only:
            continue
        entry = laws.get(jur)
        if not entry:
            problems.append(f"{jur}: no entry")
            continue
        data, checks = verify_entry(jur, entry, cfg.get("force", False))
        total = len(checks)
        good = sum(1 for c in checks if c[4] == "ok")
        sid = None
        if data.get("statute_url"):
            sid = source_id_for(conn, data["statute_url"], DOMAIN,
                                title=f"{data.get('law_name')} ({data.get('citation')})",
                                publisher=data.get("publisher"),
                                license="Public domain (government edict)")
        ts = now()
        conn.execute("""INSERT INTO records_law(jurisdiction, data_json, fields_total,
                          fields_verified, verified_at, source_id) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(jurisdiction) DO UPDATE SET data_json=excluded.data_json,
                          fields_total=excluded.fields_total,
                          fields_verified=excluded.fields_verified,
                          verified_at=excluded.verified_at, source_id=excluded.source_id""",
                     (jur, json.dumps(data, sort_keys=True), total, good, ts, sid))
        conn.execute("DELETE FROM records_law_check WHERE jurisdiction=?", (jur,))
        conn.executemany("""INSERT INTO records_law_check(jurisdiction, field, quote_url, via,
                              result, checked_at) VALUES(?,?,?,?,?,?)""", checks)
        conn.commit()
        bad_req = [n for n in REQUIRED if not (data.get(n) or {}).get("verified")]
        if bad_req:
            problems.append(f"{jur}: unverified {', '.join(bad_req)}")
        out_rows.append((jur, good, total, [c for c in checks if c[4] != "ok"]))
    return {"rows": len(out_rows), "table": out_rows, "problems": problems,
            "notes": f"{len(out_rows)} jurisdictions; {len(problems)} problems"}


def main(argv=None) -> int:
    import argparse
    from ingest.common import db
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--only", default="", help="comma-separated jurisdictions")
    ap.add_argument("--force", action="store_true", help="refetch every quote_url")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="print each failed check")
    ap.add_argument("--dry", action="store_true", help="check quotes only; write nothing")
    a = ap.parse_args(argv)
    only = [s.strip().upper() for s in a.only.split(",") if s.strip()]
    if a.dry:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript((ROOT / "ingest" / "schema.sql").read_text())
    else:
        conn = db()
    res = run(conn, {"only": only, "force": a.force})
    tg = tt = 0
    print(f"{'jur':4} {'verified/total':>14}")
    for jur, good, total, fails in res["table"]:
        tg, tt = tg + good, tt + total
        print(f"{jur:4} {good:>6}/{total:<7}" + ("" if not fails else
              "  unverified: " + ", ".join(f[1] for f in fails)))
        if a.verbose:
            for f in fails:
                print(f"       {f[1]}: {f[4]}")
    print(f"{len(res['table'])} jurisdictions, {tg}/{tt} quoted fields verified")
    conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,?)",
                 (DOMAIN, now(), res["rows"], res["notes"], int(not res["problems"])))
    conn.commit()
    if a.selftest:
        for r in conn.execute("SELECT jurisdiction, fields_verified, fields_total, verified_at "
                              "FROM records_law ORDER BY RANDOM() LIMIT 3"):
            print(dict(r))
    if not only:
        missing = [j for j in STATES if j not in {r[0] for r in res["table"]}]
        if missing:
            print("missing:", ", ".join(missing))
    for p in res["problems"]:
        print("PROBLEM", p)
    return 1 if res["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
