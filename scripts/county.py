#!/usr/bin/env python3
"""county.py: add your county to the Homeless Atlas, check it, preview it and open a pull request.

The atlas gets county facts from one research file per county,
research/county/<ST>/<FIPS>-<slug>.md (see research/county/README.md). This tool does the
mechanical parts so a contributor, or their AI agent, only has to find and read local sources.

    python3 scripts/county.py new 42049            # start the Erie County, PA file from the template
    python3 scripts/county.py check 42049          # re-fetch every source, verify claims and contacts
    python3 scripts/county.py preview 42049        # build the site into a temp dir and show the page
    python3 scripts/county.py pr 42049 --dry-run   # show the branch, commit and pull request it would open
    python3 scripts/county.py service install --county 42049   # optional: re-check it every day

Nothing here needs data/atlas.db (the 1 GB build database): `new` falls back to the live site's
JSON and then the Census gazetteer, and `check` never touches a database.

The optional background service is described in docs/CONTRIBUTOR_SERVICE.md. It runs from its own
clone of upstream master, refuses to run if that clone was modified, re-checks the configured
counties, writes a JSON report and, only if you turn it on, opens a DRAFT pull request that
touches nothing but that county's file. It never merges anything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # `python3 -I` drops the script directory; imports come from this tree only

RESEARCH = ROOT / "research" / "county"
TEMPLATE = RESEARCH / "TEMPLATE.md"
UPSTREAM_REPO = "0xSero/homeless-atlas"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_REPO}.git"
SITE = "https://homeless.sybilsolutions.ai"
GAZ_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
           "2024_Gaz_counties_national.zip")
GAZ_CACHE = ROOT / "data" / "raw" / "county_tool" / "2024_Gaz_counties_national.zip"

STATE_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_TAMPER, EXIT_CHILD = 0, 1, 2, 3, 4


class UsageError(Exception):
    pass


def today() -> str:
    return dt.date.today().isoformat()


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def http_get(url: str, timeout: int = 60):
    """(status code or None, bytes or None, note). Never raises."""
    try:
        import requests
        from ingest.common import UA
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        return r.status_code, (r.content if r.status_code == 200 else None), f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001  offline is a normal case for this tool
        return None, None, f"{type(e).__name__}: {str(e)[:120]}"


def check_fips(fips: str) -> str:
    if not re.fullmatch(r"\d{5}", fips or ""):
        raise UsageError(f"FIPS must be 5 digits, like 42049 (Erie County, PA); got {fips!r}")
    if fips[:2] not in STATE_FIPS:
        raise UsageError(f"{fips}: {fips[:2]} is not a state FIPS code")
    return fips


def county_slug(name: str) -> str:
    base = re.sub(r"\s+(County|Parish|Borough|Census Area|City and Borough|Municipality|"
                  r"Planning Region|Municipio)$", "", name.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "county"


def find_county_file(target: str) -> Path:
    """A 5-digit FIPS (looked up under research/county) or a path to a county file."""
    if re.fullmatch(r"\d{5}", target):
        hits = sorted(RESEARCH.glob(f"*/{target}-*.md"))
        if not hits:
            raise UsageError(f"no research file for {target} under {RESEARCH.relative_to(ROOT)}/. "
                             f"Start one with: python3 scripts/county.py new {target}")
        if len(hits) > 1:
            raise UsageError(f"more than one file for {target}: " + ", ".join(str(h) for h in hits))
        return hits[0]
    p = Path(target).expanduser().resolve()
    if not p.is_file():
        raise UsageError(f"{target}: no such file")
    return p


def show(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# =========================================================================== new: prefill sources
def db_path() -> Path:
    env = os.environ.get("ATLAS_DB")
    return Path(env).expanduser() if env else ROOT / "data" / "atlas.db"


def prefill_from_db(fips: str):
    """Read-only. Returns a prefill dict or (None, why)."""
    p = db_path()
    if not p.is_file() or p.stat().st_size == 0:
        return None, f"no database at {p}"
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT name, state FROM geo_city WHERE kind='county' AND geoid=?",
                         (fips,)).fetchone()
    except sqlite3.Error as e:
        return None, f"database {p} unreadable: {e}"
    if not r:
        return None, f"{fips} not in {p} (geo_city)"

    def rows(sql, args):
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
    cocs = [x["coc_num"] for x in rows(
        "SELECT coc_num FROM county_coc WHERE county_fips=? AND coc_num GLOB '[A-Z][A-Z]-[0-9]*' "
        "ORDER BY county_share DESC, coc_num", (fips,))]
    offices = [dict(x) for x in rows(
        "SELECT kind, id, name, phone, url FROM officials_office WHERE kind IN ('pha','coc_lead') "
        "AND (county_fips=? OR id IN (SELECT person_or_office_id FROM officials_county WHERE county_fips=?)) "
        "ORDER BY kind, name", (fips, fips))]
    people = [dict(x) for x in rows(
        "SELECT c.level, p.name, p.title, p.body, p.district, p.phone, p.url FROM officials_county c "
        "JOIN officials_person p ON p.id=c.person_or_office_id WHERE c.county_fips=? "
        "ORDER BY CASE c.level WHEN 'federal_senate' THEN 1 WHEN 'federal_house' THEN 2 "
        "WHEN 'state_upper' THEN 3 WHEN 'state_lower' THEN 4 ELSE 5 END, p.name", (fips,))]
    conn.close()
    return {"source": f"local database {show(p)} (tables geo_city, county_coc, officials_office, "
                      "officials_person, officials_county)",
            "county": r["name"], "state": r["state"], "coc": cocs, "coc_known": True,
            "offices": offices, "people": people}, None


def prefill_from_site(fips: str):
    code, body, note = http_get(f"{SITE}/api/county/{fips}.json", timeout=30)
    if body is None:
        return None, f"{SITE}/api/county/{fips}.json: {note}"
    try:
        d = json.loads(body)
    except ValueError:
        return None, f"{SITE}/api/county/{fips}.json: not JSON"
    if d.get("county_fips") != fips or not d.get("county") or not d.get("state"):
        return None, f"{SITE}/api/county/{fips}.json: unexpected content"
    st = d["state"]
    # the county JSON has no CoC field; CoC numbers appear in figure labels and source notes
    cocs = sorted(set(re.findall(rf"\b{re.escape(st)}-\d{{3}}\b", body.decode("utf-8", "replace"))))
    offices = []
    rcode, rbody, _ = http_get(f"{SITE}/api/records/{fips}.json", timeout=30)
    if rbody:
        try:
            for rq in json.loads(rbody).get("requests") or []:
                a = rq.get("agency") or {}
                if a.get("name") and a["name"] not in {o["name"] for o in offices}:
                    offices.append({"kind": "records", "id": rq.get("id"), "name": a["name"],
                                    "phone": a.get("phone"), "url": a.get("url")})
        except (ValueError, AttributeError):
            pass
    src = f"live site {SITE}/api/county/{fips}.json"
    src += f" and /api/records/{fips}.json" if rbody else f" (/api/records/{fips}.json: HTTP {rcode})"
    return {"source": src, "county": d["county"], "state": st, "coc": cocs, "coc_known": bool(cocs),
            "offices": offices, "people": []}, None


def prefill_from_gazetteer(fips: str):
    if GAZ_CACHE.is_file() and GAZ_CACHE.stat().st_size > 1000:
        blob = GAZ_CACHE.read_bytes()
    else:
        code, blob, note = http_get(GAZ_URL, timeout=120)
        if blob is None:
            return None, f"Census gazetteer: {note}"
        try:
            GAZ_CACHE.parent.mkdir(parents=True, exist_ok=True)
            GAZ_CACHE.write_bytes(blob)
        except OSError:
            pass
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = next(n for n in z.namelist() if n.endswith(".txt"))
            text = z.read(name).decode("utf-8", "replace")
    except (zipfile.BadZipFile, StopIteration) as e:
        return None, f"Census gazetteer unreadable: {e!r}"
    lines = text.splitlines()
    head = [h.strip() for h in lines[0].split("\t")]
    for ln in lines[1:]:
        row = dict(zip(head, (c.strip() for c in ln.split("\t"))))
        if row.get("GEOID") == fips:
            return {"source": "Census Bureau 2024 Gazetteer, counties (" + GAZ_URL + ")",
                    "county": row["NAME"], "state": row["USPS"], "coc": [], "coc_known": False,
                    "offices": [], "people": []}, None
    return None, f"{fips} is not in the Census 2024 county gazetteer"


def prefill(fips: str, only: str | None = None):
    tried = []
    for name, fn in (("db", prefill_from_db), ("site", prefill_from_site), ("gazetteer", prefill_from_gazetteer)):
        if only and name != only:
            continue
        got, why = fn(fips)
        if got:
            got["tried"] = tried
            return got
        tried.append(why)
    raise UsageError(f"could not find county {fips}: " + "; ".join(tried))


# =========================================================================== new: render
def _title(s: str | None) -> str:
    if not s:
        return ""
    if s.upper() != s:
        return s
    small = {"of", "the", "and", "for", "in", "on", "a", "an"}
    words = s.lower().split()
    return " ".join(w if (i and w in small) else w.capitalize() for i, w in enumerate(words))


def _yq(s) -> str:
    """A YAML double-quoted scalar."""
    return json.dumps("" if s is None else str(s))


def _template_blocks():
    """The template's front matter split at its top-level keys, and its body."""
    text = TEMPLATE.read_text(encoding="utf-8")
    m = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n(.*)\Z", text, re.S)
    if not m:
        raise UsageError(f"{show(TEMPLATE)} has no front matter")
    blocks, key = {}, None
    for ln in m.group(1).splitlines():
        k = re.match(r"^([a-z_]+):", ln)
        if k:
            key = k.group(1)
        if key:
            blocks.setdefault(key, []).append(ln)
    return blocks, m.group(2)


def render_new(fips: str, pf: dict) -> str:
    blocks, body = _template_blocks()

    def commented(key):
        out = []
        for ln in blocks.get(key, []):
            body = ln[2:] if ln.startswith("  ") else ln
            out.append(("  # " + body).rstrip() if body.strip() else "  #")
        while out and out[-1] == "  #":
            out.pop()
        return out
    L = ["---",
         f"# Started by `python3 scripts/county.py new {fips}` on {today()}.",
         f"# Prefill source: {pf['source']}.",
         "# Check every prefilled value against an official page before you open a pull request.",
         f"# When you are done, run: python3 scripts/county.py check {fips}",
         f"county_fips: \"{fips}\"",
         f"county: {pf['county']}",
         f"state: {pf['state']}"]
    if pf["coc"]:
        L.append(f"coc: [{', '.join(pf['coc'])}]" + ("" if pf["coc_known"] else
                                                     "   # TODO: confirm; read from the site JSON"))
    else:
        L.append("coc: []   # TODO: add the HUD Continuum(s) of Care covering this county, e.g. [PA-605]. "
                 "Find yours at https://www.hudexchange.info/programs/coc/")
    L += [f"updated: {today()}", "",
          "sources:   # add at least one; the examples below show every field",
          *commented("sources")[1:], "",
          "responsible:   # optional; examples below, then offices the atlas already lists",
          *commented("responsible")[1:]]
    starters = []
    for o in pf["offices"]:
        role = {"pha": "Public housing agency", "coc_lead": "Lead agency, Continuum of Care",
                "records": "Public records office"}.get(o["kind"], o["kind"])
        if o.get("id") and str(o["id"]).startswith("pha:"):
            role = (role or "") + f" (HUD code {str(o['id'])[4:]})"
        starters.append(["  # - for: [" + ("housing_authority" if o["kind"] == "pha" else
                                           "pit_count, coc_funding" if o["kind"] == "coc_lead" else "") + "]",
                         f"  #   office: {_yq(_title(o['name']))}",
                         f"  #   role: {_yq(role)}",
                         *([f"  #   phone: {_yq(o['phone'])}   # office main line, as HUD lists it"]
                           if o.get("phone") else []),
                         f"  #   url: {o['url'] or 'TODO the office' + chr(39) + 's own contact page'}",
                         "  #   elected: false"])
    for p in pf["people"]:
        role = p["title"] or p["level"] or ""
        if p["level"] == "federal_senate":
            role += f" for {pf['state']}"
        elif p.get("district"):
            role += f", district {p['district']}"
        starters.append(["  # - for: []   # which figures this office answers for",
                         f"  #   office: {_yq('Office of ' + (p['title'] or '') + ' ' + (p['name'] or ''))}",
                         f"  #   role: {_yq(role)}",
                         f"  #   name: {_yq(p['name'])}",
                         f"  #   title: {_yq(p['title'])}",
                         *([f"  #   phone: {_yq(p['phone'])}"] if p.get("phone") else []),
                         f"  #   url: {p['url'] or 'TODO the official page with this office line'}",
                         "  #   elected: true"])
    L += ["  #",
          f"  # Offices the atlas already lists for this county (from the {pf['source'].split(' (')[0]}).",
          "  # Uncomment one only after you find the same details on the office's own page, and set url",
          "  # to that page. Office lines only: no personal cell numbers, no staff members' own addresses",
          "  # (staff email addresses from federal lists are left out on purpose).",
          "  # Then add the local offices nobody has listed yet: the county executive or commissioners,",
          "  # the human services department, the CoC lead agency, the city government."]
    if not starters:
        L.append("  # (none found in the prefill source)")
    for s in starters:
        L += ["  #"] + s
    L += ["", "help:   # optional service lines for street flyers; example below", *commented("help")[1:],
          "---", ""]
    return "\n".join(L) + "\n" + body.lstrip("\n")


def cmd_new(a) -> int:
    fips = check_fips(a.fips)
    pf = prefill(fips, a.source)
    text = render_new(fips, pf)
    st = pf["state"]
    if STATE_FIPS[fips[:2]] != st:
        raise UsageError(f"{fips} starts with {fips[:2]} ({STATE_FIPS[fips[:2]]}) but the source says {st}")
    dest = RESEARCH / st / f"{fips}-{a.slug or county_slug(pf['county'])}.md"
    existing = sorted(RESEARCH.glob(f"*/{fips}-*.md"))
    for why in pf.get("tried") or []:
        print(f"note: skipped a prefill source: {why}", file=sys.stderr)
    if a.dry_run:
        print(f"# would write {show(dest)} (prefill: {pf['source']})")
        if existing:
            print(f"# note: {show(existing[0])} already exists, so a real run would refuse")
        print(text)
        return EXIT_OK
    if existing:
        raise UsageError(f"{show(existing[0])} already exists; not overwriting it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "x", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {show(dest)} (prefill: {pf['source']})")
    print(f"next: add sources and claims, then run: python3 scripts/county.py check {fips}")
    return EXIT_OK


# =========================================================================== check
_FAKE_HOSTS = {"example.com", "example.org", "example.net", "example.gov", "example.edu", "localhost",
               "test.com", "foo.com", "bar.com", "domain.com", "website.com", "yourcounty.gov"}
_FAKE_TLDS = (".example", ".invalid", ".test", ".localhost", ".local", ".internal")


def lint_url(url: str) -> str | None:
    """Why a URL looks invented, or None."""
    try:
        u = urlsplit(url)
    except ValueError:
        return "not a URL"
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        return "not an http(s) URL"
    if re.search(r"todo|xxx|placeholder|\.\.\.", url, re.I):
        return "placeholder text in the URL"
    if host in _FAKE_HOSTS or host.endswith(tuple("." + h for h in _FAKE_HOSTS)) or host.endswith(_FAKE_TLDS):
        return f"{host} is a placeholder domain"
    if re.fullmatch(r"[\d.]+|\[[0-9a-f:]+\]", host) or "." not in host:
        return f"{host} is not a public domain name"
    return None


def lint_phone(phone: str) -> str | None:
    d = re.sub(r"\D", "", phone)
    d = d[1:] if len(d) == 11 and d.startswith("1") else d
    if len(d) not in (3, 7, 10):           # 211, 988 and 911 are real lines
        return "not a 10-digit US phone number"
    if len(d) == 10 and d[3:6] == "555" and d[6:8] == "01":
        return "555-01xx numbers are fictional (template value?)"
    return None


def lint_entry(path: Path, entry: dict | None, probs: list[str]):
    """Offline checks: (errors, warnings, urls that look invented)."""
    errors, warnings, bad = list(probs), [], set()
    if entry is None:
        return errors, warnings, bad
    rel = entry["path"]
    fips, st = entry["fips"], entry["state"]
    if fips == "00000" or entry["county"] == "Example County":
        errors.append(f"{rel}: still the template values (county_fips/county)")
    elif fips[:2] not in STATE_FIPS:
        errors.append(f"{rel}: county_fips {fips}: {fips[:2]} is not a state FIPS code")
    elif STATE_FIPS[fips[:2]] != st:
        errors.append(f"{rel}: county_fips {fips} is in {STATE_FIPS[fips[:2]]}, but state is {st}")
    for c in entry["coc"]:
        if not re.fullmatch(r"[A-Z]{2}-\d{3}", c) or c[:2] != st:
            warnings.append(f"{rel}: coc {c!r} does not look like a {st} CoC number ({st}-123)")
    items = ([("sources", s["url"], s) for s in entry["sources"]] +
             [("responsible", r["url"], r) for r in entry["responsible"]] +
             [("help", h["url"], h) for h in entry["help"]])
    for kind, url, it in items:
        why = lint_url(url)
        if why:
            bad.add(url)
            errors.append(f"{rel}: {kind}: {url}: {why}")
        elif url.startswith("http://"):
            warnings.append(f"{rel}: {kind}: {url}: plain http; use the https address if there is one")
        if it.get("phone") and lint_phone(it["phone"]):
            errors.append(f"{rel}: {kind}: phone {it['phone']!r}: {lint_phone(it['phone'])}")
        em = it.get("email")
        if em and (not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", em, re.I) or lint_url("https://" + em.split("@")[-1])):
            errors.append(f"{rel}: {kind}: email {em!r} looks invalid or invented")
        if it.get("name") in ("Jane Doe", "John Doe"):
            errors.append(f"{rel}: {kind}: name {it['name']!r} is the template value")
    return errors, warnings, bad


def fetched_how(res) -> str:
    from ingest import fetch_ladder
    if res.ok and res.archived_at:
        return f"Wayback copy, {str(res.archived_at)[:10]}" + (" (fresh save)" if res.via == "wayback_fresh" else "")
    if res.ok:
        return "live" if res.via == "direct" else f"live ({res.via})"
    return "not fetched: " + (fetch_ladder.attempts_summary(res.attempts)[:100] or "no attempts")


def run_check(path: Path, offline: bool = False) -> dict:
    """Verify one county file. Reads no database and writes none; the fetch ladder caches page bytes
    under data/raw/local_sources/ (gitignored) and logs to logs/ingest-local_sources.jsonl."""
    from ingest.domains import local_sources as LS
    try:
        entry, probs = LS.parse_file(path)
    except Exception as e:  # noqa: BLE001
        entry, probs = None, [f"{path}: could not be read: {e!r}"[:300]]
    errors, warnings, bad_urls = lint_entry(path, entry, probs)
    for pre in {show(path) + ": ", str(path) + ": "}:
        errors = [x[len(pre):] if x.startswith(pre) else x for x in errors]
        warnings = [x[len(pre):] if x.startswith(pre) else x for x in warnings]
    out = {"file": show(path), "checked_at": utcnow(), "offline": offline, "errors": errors,
           "warnings": warnings, "rows": [], "entry": entry}
    if entry is None:
        out.update(passed=False, verdict="FAIL: the file could not be parsed; fix the errors above")
        return out
    rows = out["rows"]
    pages = {}

    def page(url):
        if url not in pages:
            if offline or url in bad_urls:
                pages[url] = (None, None, "skipped (offline)" if offline else "skipped (invented-looking URL)")
            else:
                res = LS.get(url)
                text = None
                if res.ok and res.body is not None:
                    text, _title, _why = LS.extract(res.body)
                pages[url] = (res, LS.normalize(text) if text else None, fetched_how(res))
        return pages[url]

    for s in entry["sources"]:
        res, ntext, how = page(s["url"])
        status = ("skipped" if res is None else
                  "text_unavailable" if res.ok and ntext is None else res.status)
        rows.append({"kind": "source", "item": s["title"], "field": s["url"], "result": status,
                     "fetched": how, "ok": status in ("ok", "archived"), "counts": False})
        for c in s["claims"]:
            if ntext is None:
                result = status
            else:
                result = LS.check_claim(c, ntext)
            rows.append({"kind": "claim", "item": f"{c['metric']} {c['year']} = {c['value']:g}",
                         "field": c["quote"], "result": result, "fetched": how,
                         "ok": result == "verified", "counts": True})
    for kind, items in (("contact", entry["responsible"]), ("help", entry["help"])):
        for r in items:
            res, ntext, how = page(r["url"])
            live = ntext if (res is not None and res.status == "ok") else None
            flags = LS.check_contact(r, live)
            label = (f"{r['office']} / {r['role']}" if kind == "contact" else r["name"])
            for field in ("name", "phone", "email", "address"):
                if r.get(field) is None:
                    continue
                v = flags.get(field + "_ok")
                if v == 1:
                    result = "verified"
                elif v == 0:
                    result = "not_on_page"
                elif res is None:
                    result = "skipped"
                elif res.status == "archived":
                    result = "archive-only (contacts need the live page)"
                else:
                    result = "text_unavailable" if res.ok else res.status
                rows.append({"kind": kind, "item": label, "field": f"{field}: {r[field]}", "result": result,
                             "fetched": how, "ok": result == "verified",
                             "counts": field in ("phone", "email")})
    counted = [r for r in rows if r["counts"]]
    bad = [r for r in counted if not r["ok"]]
    for r in rows:
        if r["kind"] == "source" and not r["ok"]:
            warnings.append(f"source {r['field']}: {r['result']} ({r['fetched']})")
        elif not r["counts"] and not r["ok"] and r["kind"] != "source":
            warnings.append(f"{r['kind']} {r['item']}: {r['field']} {r['result']}")
    n_claims = sum(1 for r in counted if r["kind"] == "claim")
    n_ok_claims = sum(1 for r in counted if r["kind"] == "claim" and r["ok"])
    n_cont = len(counted) - n_claims
    n_ok_cont = sum(1 for r in counted if r["kind"] != "claim" and r["ok"])
    tally = f"{n_ok_claims}/{n_claims} claims verified, {n_ok_cont}/{n_cont} phones and emails verified"
    if offline:
        out["passed"] = not errors
        out["verdict"] = ("OFFLINE OK: the file is well formed (nothing was fetched)" if not errors
                          else f"FAIL: {len(errors)} problem(s) in the file")
    elif errors or bad:
        out["passed"] = False
        named = "; ".join(f"{r['kind']} {r['item']} ({r['result']})" for r in bad[:6])
        out["verdict"] = (f"FAIL: {tally}" + (f"; {len(errors)} problem(s) in the file" if errors else "")
                          + (f". Not verified: {named}" if named else ""))
    else:
        out["passed"] = True
        out["verdict"] = f"PASS: {tally}" + ("" if counted else " (nothing to verify: add claims)")
    return out


def _clip(s, n):
    s = re.sub(r"\s+", " ", str(s))
    return s if len(s) <= n else s[: n - 3] + "..."


def check_text(res: dict) -> str:
    L = [f"county file: {res['file']}"]
    if res["errors"]:
        L += ["", "Problems in the file (fix these first):"] + [f"  - {e}" for e in res["errors"]]
    if res["rows"]:
        L += ["", f"{'KIND':<8} {'ITEM':<38} {'CHECKED':<34} {'RESULT':<22} FETCHED"]
        for r in res["rows"]:
            L.append(f"{r['kind']:<8} {_clip(r['item'], 38):<38} {_clip(r['field'], 34):<34} "
                     f"{_clip(r['result'], 22):<22} {_clip(r['fetched'], 60)}")
    if res["warnings"]:
        L += ["", "Warnings (do not fail the check):"] + [f"  - {w}" for w in res["warnings"]]
    L += ["", res["verdict"]]
    return "\n".join(L)


def _md(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def check_markdown(res: dict) -> str:
    e = res.get("entry") or {}
    head = f"{e.get('county', '?')}, {e.get('state', '?')} ({e.get('fips', '?')})" if e else res["file"]
    L = [f"### {'PASS' if res['passed'] else 'FAIL'}: {head}", "", f"`{res['file']}`, checked {res['checked_at']}", ""]
    if res["errors"]:
        L += ["**Problems in the file**", ""] + [f"- {_md(x)}" for x in res["errors"]] + [""]
    if res["rows"]:
        L += ["| kind | item | checked | result | fetched |", "|---|---|---|---|---|"]
        L += [f"| {r['kind']} | {_md(_clip(r['item'], 70))} | {_md(_clip(r['field'], 90))} | "
              f"{'' if r['ok'] else '**'}{_md(r['result'])}{'' if r['ok'] else '**'} | {_md(r['fetched'])} |"
              for r in res["rows"]] + [""]
    if res["warnings"]:
        L += ["Warnings:", ""] + [f"- {_md(w)}" for w in res["warnings"]] + [""]
    L.append(f"**{_md(res['verdict'])}**")
    return "\n".join(L) + "\n"


def cmd_check(a) -> int:
    rc = EXIT_OK
    results = []
    for t in a.targets:
        res = run_check(find_county_file(t), offline=a.offline)
        results.append(res)
        if not res["passed"]:
            rc = EXIT_FAIL
        if a.markdown:
            print(check_markdown(res))
        elif not a.json:
            print(check_text(res))
            print()
    if a.json:
        print(json.dumps(results[0] if len(results) == 1 else results, indent=1, default=str))
    return rc


# =========================================================================== preview
def cmd_preview(a) -> int:
    fips = check_fips(a.fips)
    if not db_path().is_file():
        raise UsageError(f"preview builds the whole site and needs the database at {db_path()}, "
                         "which a fresh clone does not have. `check` and `pr` work without it.")
    out = Path(a.out).resolve() if a.out else Path(tempfile.mkdtemp(prefix="atlas-preview-"))
    cmd = [sys.executable, str(ROOT / "scripts" / "build_site.py"), "--fast", "--force", "--out", str(out)]
    print("$ " + shlex.join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    page, flyer = out / "county" / f"{fips}.html", out / "county" / f"{fips}-print.html"
    for label, p in (("county page", page), ("flyer", flyer)):
        print(f"{label}: {p}" + ("" if p.is_file() else "  (not built)"))
    return rc if rc else (EXIT_OK if page.is_file() else EXIT_FAIL)


# =========================================================================== pr
def _gh_owner_repo(url: str):
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$",
                 url.strip())
    return (m.group(1), m.group(2)) if m else None


def detect_fork(prefer: str | None = None):
    """(remote, owner, repo) of the contributor's fork of 0xSero/homeless-atlas, or None."""
    r = subprocess.run(["git", "-C", str(ROOT), "remote", "-v"], capture_output=True, text=True)
    remotes = {}
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[2] == "(push)":
            remotes[parts[0]] = parts[1]
    order = sorted(remotes, key=lambda n: (n != (prefer or "fork"), n))
    for name in order:
        if prefer and name != prefer:
            continue
        orp = _gh_owner_repo(remotes[name])
        if not orp or f"{orp[0]}/{orp[1]}".lower() == UPSTREAM_REPO.lower():
            continue
        if shutil.which("gh"):
            v = subprocess.run(["gh", "repo", "view", f"{orp[0]}/{orp[1]}", "--json", "isFork,parent"],
                               capture_output=True, text=True, timeout=60)
            if v.returncode == 0:
                d = json.loads(v.stdout or "{}")
                parent = d.get("parent") or {}
                pname = f"{(parent.get('owner') or {}).get('login', '')}/{parent.get('name', '')}"
                if not d.get("isFork") or pname.lower() != UPSTREAM_REPO.lower():
                    print(f"note: remote {name} ({orp[0]}/{orp[1]}) is not a fork of {UPSTREAM_REPO}; skipped",
                          file=sys.stderr)
                    continue
        return name, orp[0], orp[1]
    return None


def pr_body(res: dict, rel: str, draft: bool) -> str:
    e = res["entry"]
    res = {**res, "file": rel}          # never show the contributor's local path
    srcrow = {r["field"]: r for r in res["rows"] if r["kind"] == "source"}
    L = [f"Adds the county research file for **{e['county']}, {e['state']} ({e['fips']})**: `{rel}`. "
         "No other file changes.", ""]
    if draft and not res["passed"]:
        L += ["> Draft: `county.py check` does not pass yet. See the table below.", ""]
    L += ["## What was verified", "", check_markdown(res).split("\n", 2)[2], "## Sources", "",
          "| # | publisher | title | published | kind | fetched | claims |", "|---|---|---|---|---|---|---|"]
    for i, s in enumerate(e["sources"], 1):
        r = srcrow.get(s["url"], {})
        L.append(f"| {i} | {_md(s['publisher'])} | [{_md(s['title'])}]({s['url']}) | {s['published'] or ''} | "
                 f"{s['kind']} | {_md(r.get('fetched', ''))} | {len(s['claims'])} |")
    L += ["", "## Contacts and help lines", ""]
    cont = [r for r in res["rows"] if r["kind"] in ("contact", "help")]
    if cont:
        L += [f"- {_md(r['item'])}: {_md(r['field'])}: **{_md(r['result'])}**" for r in cont]
    else:
        L.append("None in this file.")
    arch = [r for r in res["rows"] if r["kind"] == "source" and r["fetched"].startswith("Wayback")]
    L += ["", "## Archive-only sources", ""]
    L += [f"- {r['field']}: {r['fetched']}" for r in arch] or ["None: every source was read live."]
    L += ["", "## How to re-check", "", "```bash", "pip install requests pyyaml",
          f"python3 scripts/county.py check {e['fips']}", "```", "",
          "The county-check workflow runs the same command on this pull request.", "",
          "## Checklist (the contributor ticks these)", "",
          "- [ ] Public sources only, readable without an account",
          "- [ ] Every number in `claims` carries an exact quote from its source",
          "- [ ] Contacts are official office lines published on the cited page",
          "- [ ] No personal numbers or home addresses",
          "- [ ] No individual experiencing homelessness is named or pictured",
          "- [ ] `county.py check` passes (output above)",
          "", "Opened with `python3 scripts/county.py pr`."]
    return "\n".join(L) + "\n"


def cmd_pr(a) -> int:
    path = find_county_file(a.file or a.fips)
    print(f"checking {show(path)} (this re-fetches every source)...", file=sys.stderr)
    res = run_check(path)
    e = res["entry"]
    if e is None:
        print(check_text(res))
        raise UsageError("the file does not parse; fix it before opening a pull request")
    if a.file is None and e["fips"] != a.fips:
        raise UsageError(f"{show(path)} says county_fips {e['fips']}, not {a.fips}")
    if not res["passed"] and not a.draft:
        print(check_text(res))
        print("\nRefusing to open a pull request: the check failed. Fix it, or pass --draft to open a "
              "draft pull request that shows the failures.")
        return EXIT_FAIL
    fork = detect_fork(a.fork_remote)
    dry = a.dry_run
    if fork is None:
        if not dry:
            print("No fork remote found (a remote pointing at <you>/homeless-atlas). Showing what would run.\n"
                  "Make one with: gh repo fork 0xSero/homeless-atlas --remote --remote-name fork\n")
        dry = True
        fork = (a.fork_remote or "fork", "<your-github-user>", "homeless-atlas")
    remote, owner, _repo = fork
    slug = path.stem.split("-", 1)[1] if "-" in path.stem else county_slug(e["county"])
    branch = f"county/{e['fips']}-{slug}"
    rel = f"research/county/{e['state']}/{path.name}"
    title = f"county: {e['county']}, {e['state']} ({e['fips']})"
    body = pr_body(res, rel, a.draft)
    tmp = Path(tempfile.mkdtemp(prefix="county-pr-"))
    wt, body_file = tmp / "worktree", tmp / "pr-body.md"
    git = ["git", "-C", str(ROOT)]
    steps = [
        ("run", git + ["fetch", "origin", "master"]),
        ("run", git + ["worktree", "add", "--detach", str(wt), "origin/master"]),
        ("run", ["git", "-C", str(wt), "switch", "-c", branch]),
        ("copy", ["cp", str(path), str(wt / rel)]),
        ("run", ["git", "-C", str(wt), "add", "--", rel]),
        ("run", ["git", "-C", str(wt), "commit", "-m", title]),
        ("only", ["git", "-C", str(wt), "diff", "--name-only", "origin/master", "HEAD"]),
        ("run", ["git", "-C", str(wt), "push", remote, f"HEAD:refs/heads/{branch}"]),
        ("run", ["gh", "pr", "create", "--repo", UPSTREAM_REPO, "--base", "master", "--head",
                 f"{owner}:{branch}", "--title", title, "--body-file", str(body_file)]
         + (["--draft"] if a.draft else [])),
        ("run", git + ["worktree", "remove", "--force", str(wt)]),
    ]
    if dry:
        print(f"# dry run: nothing below is executed. Fork remote: {remote} ({owner}). Never pushes to origin.")
        for kind, cmd in steps:
            print(("# then confirm the only changed file is " + rel + ":\n") * (kind == "only") + "$ " + shlex.join(cmd))
        print(f"\n# pull request title: {title}\n# pull request body ({body_file}):\n")
        print(body)
        shutil.rmtree(tmp, ignore_errors=True)
        return EXIT_OK
    body_file.write_text(body)
    try:
        for kind, cmd in steps:
            print("$ " + shlex.join(cmd), flush=True)
            if kind == "copy":
                (wt / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, wt / rel)
                continue
            r = subprocess.run(cmd, capture_output=(kind == "only"), text=True)
            if r.returncode:
                raise UsageError(f"command failed ({r.returncode}): {shlex.join(cmd)}")
            if kind == "only" and r.stdout.split() != [rel]:
                raise UsageError(f"refusing to push: the commit changes {r.stdout.split()}, not only {rel}")
    finally:
        if wt.exists():
            subprocess.run(git + ["worktree", "remove", "--force", str(wt)], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return EXIT_OK


# =========================================================================== service: paths and config
SERVICE_NAME = "homeless-atlas-county"
LAUNCHD_LABEL = "org.homeless-atlas.county-service"
SPARSE = ["scripts", "ingest", "research/county", "docs"]
ALLOWED_IGNORED = ("data/raw/", "logs/")
_UNSAFE_GIT_KEYS = re.compile(r"insteadof|fsmonitor|hookspath|sshcommand|askpass|^include|credential|"
                              r"gitproxy|^alias\.|^filter\.|textconv|^core\.pager|^core\.editor|"
                              r"uploadpack|receivepack|^http\.|proxy", re.I)


class Paths:
    def __init__(self, home: str | None):
        h = home or os.environ.get("HOMELESS_ATLAS_HOME") or str(Path.home())
        self.home = Path(h).expanduser().resolve()
        self.custom_home = self.home != Path.home().resolve()
        self.config_dir = self.home / ".config" / "homeless-atlas"
        self.config = self.config_dir / "service.toml"
        self.launcher = self.config_dir / "run-cycle.sh"
        self.data_dir = self.home / ".local" / "share" / "homeless-atlas"
        self.worktree = self.data_dir / "worktree"
        self.state_dir = self.home / ".local" / "state" / "homeless-atlas"
        self.reports = self.state_dir / "reports"
        self.log = self.state_dir / "service.jsonl"
        self.manifest = self.state_dir / "install.json"
        self.stop = self.state_dir / "STOP"
        self.unit_dir = self.home / ".config" / "systemd" / "user"
        self.service_unit = self.unit_dir / f"{SERVICE_NAME}.service"
        self.timer_unit = self.unit_dir / f"{SERVICE_NAME}.timer"
        self.plist = self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    def owned(self, plat):
        files = [self.service_unit, self.timer_unit] if plat == "linux" else [self.plist]
        return files + [self.config_dir, self.data_dir, self.state_dir]


def platform_name(a) -> str:
    p = getattr(a, "platform", None) or ("macos" if sys.platform == "darwin" else
                                         "linux" if sys.platform.startswith("linux") else sys.platform)
    if p not in ("linux", "macos"):
        raise UsageError(f"the service supports Linux (systemd) and macOS (launchd), not {p}")
    return p


def toml_config(cfg: dict) -> str:
    def v(x):
        if isinstance(x, bool):
            return "true" if x else "false"
        if isinstance(x, (int, float)):
            return str(x)
        if isinstance(x, list):
            return "[" + ", ".join(v(i) for i in x) + "]"
        return json.dumps(str(x))
    L = ["# Homeless Atlas county service. Edit, then wait for the next cycle (or run",
         "# `python3 scripts/county.py service run-once`). docs/CONTRIBUTOR_SERVICE.md explains every key."]
    L += [f"{k} = {v(x)}" for k, x in cfg.items()]
    return "\n".join(L) + "\n"


def load_config(paths: Paths) -> dict:
    if not paths.config.is_file():
        raise UsageError(f"no service config at {paths.config}; run `service install` first")
    cfg = tomllib.loads(paths.config.read_text())
    counties = cfg.get("counties")
    if not isinstance(counties, list) or not counties or not all(
            isinstance(c, str) and re.fullmatch(r"\d{5}", c) for c in counties):
        raise UsageError(f"{paths.config}: counties must be a list of quoted 5-digit FIPS, like [\"42049\"]")
    cfg.setdefault("interval_hours", 24)
    cfg.setdefault("open_prs", False)
    cfg.setdefault("max_rss_mb", 1000)
    cfg.setdefault("upstream", UPSTREAM_URL)
    cfg.setdefault("allow_local_upstream", False)
    if not isinstance(cfg["interval_hours"], int) or not 1 <= cfg["interval_hours"] <= 24 * 14:
        raise UsageError(f"{paths.config}: interval_hours must be a whole number from 1 to 336")
    if not str(cfg["upstream"]).startswith("https://") and not cfg["allow_local_upstream"]:
        raise UsageError(f"{paths.config}: upstream must be an https:// URL")
    if cfg["open_prs"] and not re.fullmatch(r"[\w.-]+/[\w.-]+", str(cfg.get("fork", ""))):
        raise UsageError(f"{paths.config}: open_prs = true needs fork = \"<you>/homeless-atlas\"")
    if cfg["open_prs"] and str(cfg["fork"]).lower() == UPSTREAM_REPO.lower():
        raise UsageError(f"{paths.config}: fork must be your fork, not {UPSTREAM_REPO}")
    return cfg


def sgit(wt: Path, *args, check=False, timeout=600):
    """git in the service worktree with hooks and fsmonitor disabled from the command line."""
    cmd = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
           "-c", "status.showUntrackedFiles=all", "-C", str(wt), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{shlex.join(cmd)} -> {r.returncode}: {r.stderr.strip()[:300]}")
    return r


def launcher_text(paths: Paths, python: str) -> str:
    wt, q = paths.worktree, shlex.quote
    g = f"git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c status.showUntrackedFiles=all -C {q(str(wt))}"
    return f"""#!/bin/sh
# Homeless Atlas county service: one cycle. Written by `county.py service install`.
# Gate before any Python from the worktree runs: drop ignored files (bytecode) from the code
# directories, then refuse unless `git status` is empty. run-once repeats these checks and logs them.
set -eu
{g} clean -ffdXq -- {' '.join(SPARSE)}
if [ -n "$({g} status --porcelain)" ]; then
  echo "county service: the worktree {wt} is modified; refusing to run. See docs/CONTRIBUTOR_SERVICE.md" >&2
  exit {EXIT_TAMPER}
fi
exec {q(python)} -I -B {q(str(wt / 'scripts' / 'county.py'))} service run-once --home {q(str(paths.home))}
"""


def systemd_units(paths: Paths, cfg: dict):
    def esc(p):           # systemd expands % specifiers; paths are double-quoted
        return '"' + str(p).replace("%", "%%") + '"'
    service = f"""[Unit]
Description=Homeless Atlas county checker (re-checks your county files; never merges)
Documentation=https://github.com/{UPSTREAM_REPO}/blob/master/docs/CONTRIBUTOR_SERVICE.md
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/sh {esc(paths.launcher)}
WorkingDirectory={esc(paths.worktree)}
MemoryMax=1G
CPUQuota=50%
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={esc(paths.data_dir)} {esc(paths.state_dir)}
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
TimeoutStartSec=3h
"""
    timer = f"""[Unit]
Description=Run the Homeless Atlas county checker every {cfg['interval_hours']} h

[Timer]
OnBootSec=15min
OnUnitActiveSec={cfg['interval_hours']}h
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
"""
    return service, timer


def launchd_plist(paths: Paths, cfg: dict) -> bytes:
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": ["/bin/sh", str(paths.launcher)],
        "StartInterval": int(cfg["interval_hours"]) * 3600,
        "RunAtLoad": False,
        "LowPriorityIO": True,
        "ProcessType": "Background",
        "Nice": 10,
        "WorkingDirectory": str(paths.worktree),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"},
        "StandardOutPath": str(paths.state_dir / "launchd.log"),
        "StandardErrorPath": str(paths.state_dir / "launchd.log"),
    })


# =========================================================================== service: install / uninstall
def _created_dirs(paths: Paths, targets) -> list[str]:
    """Directories between HOME and each target that do not exist yet (removed again on uninstall)."""
    out = set()
    for t in targets:
        p = Path(t).parent
        while p != paths.home and paths.home in p.parents and not p.exists():
            out.add(str(p))
            p = p.parent
    return sorted(out, key=len, reverse=True)


def cmd_service_install(a) -> int:
    paths, plat = Paths(a.home), platform_name(a)
    if paths.data_dir.exists() or paths.config.exists():
        raise UsageError(f"already installed under {paths.home} (found {paths.data_dir if paths.data_dir.exists() else paths.config}). "
                         "Use `service status`, or `service uninstall --yes` first.")
    if not a.county and not a.dry_run:
        raise UsageError("name at least one county: --county 42049")
    a.county = a.county or []
    for c in a.county:
        check_fips(c)
    if a.open_prs and not a.fork:
        raise UsageError("--open-prs needs --fork <you>/homeless-atlas")
    load = not a.no_load
    if load and paths.custom_home:
        raise UsageError("--home points away from your real home directory; add --no-load "
                         "(systemd and launchd only read units from the real one)")
    python = str(Path(a.python or sys.executable).absolute())   # not resolved: survives interpreter upgrades
    cfg = {"counties": a.county, "interval_hours": a.interval_hours, "open_prs": bool(a.open_prs),
           "max_rss_mb": 1000, "upstream": a.upstream or UPSTREAM_URL, "python": python}
    if a.fork:
        cfg["fork"] = a.fork
    if a.allow_local_upstream:
        cfg["allow_local_upstream"] = True
    if not cfg["upstream"].startswith("https://") and not a.allow_local_upstream:
        raise UsageError("--upstream must be an https:// URL")

    files = {paths.config: toml_config(cfg), paths.launcher: launcher_text(paths, python)}
    if plat == "linux":
        svc, tmr = systemd_units(paths, cfg)
        files[paths.service_unit], files[paths.timer_unit] = svc, tmr
    else:
        files[paths.plist] = launchd_plist(paths, cfg).decode()
    https = cfg["upstream"].startswith("https://")
    clone = ["git", "clone", "--no-checkout", "--single-branch", "--branch", "master", "--no-tags"]
    clone += (["--filter=blob:none"] if https else []) + [cfg["upstream"], str(paths.worktree)]
    cmds = [["git", "-c", "core.hooksPath=/dev/null", *clone[1:]],
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(paths.worktree), "sparse-checkout", "set", "--cone", *SPARSE],
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(paths.worktree), "checkout", "master"]]
    if load and plat == "linux":
        cmds += [["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.timer"]]
    elif load:
        cmds += [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(paths.plist)]]
    dirs = _created_dirs(paths, list(files) + [paths.worktree, paths.reports])

    if a.dry_run:
        print(f"# dry run: service install for {plat}, home {paths.home}. Nothing is written.\n")
        for p, text in files.items():
            print(f"=== would write {p}\n{text.rstrip()}\n")
        print(f"=== would create directories: {paths.data_dir}, {paths.state_dir}, {paths.reports}")
        print("=== would run:")
        for c in cmds:
            print("$ " + shlex.join(c))
        if not load:
            print("# --no-load: the unit/agent is written but not loaded")
        return EXIT_OK

    chk = subprocess.run([python, "-I", "-c", "import requests, yaml"], capture_output=True, text=True)
    if chk.returncode:
        raise UsageError(f"{python} -I cannot import requests and yaml (the service runs isolated, without "
                         f"user site-packages). Install them for that interpreter or pass --python.\n{chk.stderr[-300:]}")
    for d in (paths.data_dir, paths.state_dir, paths.reports):
        d.mkdir(parents=True, exist_ok=True)
    manifest = {"installed_at": utcnow(), "platform": plat, "loaded": False,
                "files": [str(p) for p in files], "dirs_created": dirs, "owned": [str(p) for p in paths.owned(plat)]}
    paths.manifest.write_text(json.dumps(manifest, indent=1))
    for p, text in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        print(f"wrote {p}")
    paths.launcher.chmod(0o500)
    for c in cmds:
        print("$ " + shlex.join(c), flush=True)
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode:
            print(r.stderr.strip()[-600:], file=sys.stderr)
            raise UsageError(f"install step failed; remove the partial install with: "
                             f"python3 scripts/county.py service uninstall --yes --home {paths.home}")
        if c[0] in ("systemctl", "launchctl"):
            manifest["loaded"] = True
            paths.manifest.write_text(json.dumps(manifest, indent=1))
    head = sgit(paths.worktree, "rev-parse", "HEAD").stdout.strip()
    _log(paths, {"event": "install", "platform": plat, "commit": head, "loaded": manifest["loaded"]})
    print(f"\ninstalled: worktree at upstream {head[:12]}, counties {', '.join(a.county)}, every "
          f"{a.interval_hours} h, open_prs={'true' if a.open_prs else 'false'}, "
          f"{'loaded' if manifest['loaded'] else 'NOT loaded (--no-load)'}")
    if plat == "linux" and load:
        lg = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                            capture_output=True, text=True)
        if "Linger=yes" not in lg.stdout:
            print("note: lingering is off, so the timer runs only while you are logged in. "
                  "`loginctl enable-linger` keeps it running.")
    print(f"try one cycle now: python3 scripts/county.py service run-once{' --home ' + str(paths.home) if paths.custom_home else ''}")
    return EXIT_OK


def _du(p: Path) -> str:
    if p.is_file():
        return f"{p.stat().st_size} bytes"
    n = sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())
    return f"{n / 1e6:.1f} MB"


def cmd_service_uninstall(a) -> int:
    paths, plat = Paths(a.home), platform_name(a)
    manifest = {}
    if paths.manifest.is_file():
        try:
            manifest = json.loads(paths.manifest.read_text())
            plat = manifest.get("platform", plat)
        except ValueError:
            pass
    targets = [Path(p) for p in manifest.get("owned", [])] or paths.owned(plat)
    for extra in paths.owned("linux") + paths.owned("macos"):
        if extra not in targets:
            targets.append(extra)
    present = [p for p in targets if p.exists() or p.is_symlink()]
    dirs = [Path(d) for d in manifest.get("dirs_created", []) if Path(d) not in targets]
    unload = []
    if manifest.get("loaded", not paths.custom_home):
        if plat == "linux" and shutil.which("systemctl"):
            unload = [["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.timer"],
                      ["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"]]
        elif plat == "macos" and shutil.which("launchctl"):
            unload = [["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]]
    print(f"service uninstall ({plat}, home {paths.home})")
    for c in unload:
        print("  will run:    " + shlex.join(c))
    for p in present:
        print(f"  will delete: {p}  ({_du(p)})")
    for d in dirs:
        print(f"  will remove if empty: {d}")
    if not present and not unload:
        print("  nothing installed")
    if not a.yes:
        print("\nNothing deleted. Re-run with --yes to do this.")
        return EXIT_FAIL if present else EXIT_OK
    for c in unload:
        subprocess.run(c, capture_output=True)
    if plat == "linux" and unload:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    for p in present:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    for d in dirs:
        try:
            d.rmdir()
        except OSError:
            pass
    left = [p for p in targets + dirs if p.exists() or p.is_symlink()]
    if left:
        print("\nNOT clean: still present: " + ", ".join(map(str, left)))
        return EXIT_FAIL
    print(f"\nremoved. Verified absent: {', '.join(str(p) for p in targets)}" +
          (f"; empty directories removed: {', '.join(map(str, dirs))}" if dirs else ""))
    return EXIT_OK


# =========================================================================== service: one cycle
def _log(paths: Paths, rec: dict):
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    rec = {"ts": utcnow(), **rec}
    with open(paths.log, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def tamper_rules(paths: Paths, cfg: dict) -> tuple[list[dict], str | None]:
    """Run every rule; returns (results, upstream commit or None). Fast-forwards when allowed."""
    wt, rules = paths.worktree, []

    def rule(name, ok, detail=""):
        rules.append({"rule": name, "ok": bool(ok), "detail": detail})
        return ok
    if not rule("worktree_exists", (wt / ".git").exists(), str(wt)):
        return rules, None
    top = sgit(wt, "rev-parse", "--show-toplevel").stdout.strip()
    rule("worktree_is_own_repo", top and Path(top).resolve() == wt.resolve(), top)
    cfgl = sgit(wt, "config", "--local", "--list").stdout.splitlines()
    unsafe = [ln.split("=", 1)[0] for ln in cfgl if _UNSAFE_GIT_KEYS.search(ln.split("=", 1)[0])]
    rule("git_config_safe", not unsafe, ", ".join(unsafe) or "no hooks, insteadOf, credential or proxy keys")
    origin = sgit(wt, "config", "--get", "remote.origin.url").stdout.strip()
    rule("origin_is_configured_upstream", origin == cfg["upstream"], origin)
    https = str(cfg["upstream"]).startswith("https://")
    rule("upstream_is_https", https or cfg.get("allow_local_upstream"),
         cfg["upstream"] if https else f"TEST MODE: allow_local_upstream, {cfg['upstream']}")
    st = sgit(wt, "status", "--porcelain").stdout
    rule("worktree_clean", not st.strip(), _clip(st.strip(), 200) or "git status --porcelain is empty")
    d1, d2 = sgit(wt, "diff", "--quiet", "HEAD").returncode, sgit(wt, "diff", "--cached", "--quiet").returncode
    rule("no_tracked_modifications", d1 == 0 and d2 == 0, "git diff HEAD and git diff --cached are empty"
         if d1 == 0 and d2 == 0 else "tracked files differ from HEAD")
    ign = [p for p in sgit(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory").stdout.split()
           if not p.startswith(ALLOWED_IGNORED)]
    rule("no_unexpected_ignored_files", not ign, ", ".join(ign[:10]) or "only data/raw/ and logs/")
    branch = sgit(wt, "symbolic-ref", "--short", "HEAD").stdout.strip()
    rule("on_master", branch == "master", branch or "detached")
    if not all(r["ok"] for r in rules):
        return rules, None
    f = sgit(wt, "fetch", "--no-tags", "origin", "master", timeout=900)
    if not rule("fetch_upstream", f.returncode == 0, _clip(f.stderr.strip(), 200) or "fetched origin master"):
        return rules, None
    up = sgit(wt, "rev-parse", "FETCH_HEAD").stdout.strip()
    head = sgit(wt, "rev-parse", "HEAD").stdout.strip()
    if head != up:
        anc = sgit(wt, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD").returncode == 0
        if not rule("fast_forward_only", anc, f"{head[:12]} -> {up[:12]}" if anc else
                    f"upstream history was rewritten: {head[:12]} is not an ancestor of {up[:12]}; stopped"):
            return rules, None
        m = sgit(wt, "merge", "--ff-only", "--no-edit", "FETCH_HEAD")
        rule("fast_forward_applied", m.returncode == 0, _clip(m.stderr.strip(), 200) or f"now {up[:12]}")
    else:
        rule("fast_forward_only", True, "already at upstream")
    head = sgit(wt, "rev-parse", "HEAD").stdout.strip()
    rule("head_is_upstream", head == up, f"HEAD {head[:12]}, origin/master {up[:12]}")
    st = sgit(wt, "status", "--porcelain").stdout
    rule("worktree_clean_after_update", not st.strip(), _clip(st.strip(), 200) or "clean")
    return rules, (up if all(r["ok"] for r in rules) else None)


def _rss_mb(pid: int):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        return int(out) / 1024 if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def run_capped(cmd, *, cwd, max_mb, timeout, out_path: Path, err_path: Path) -> dict:
    """Run a child with an RSS cap: RLIMIT_AS on Linux, plus polling (macOS ignores RLIMIT_AS)."""
    def limit():
        if sys.platform.startswith("linux"):
            import resource
            cap = int(max_mb) * 1024 * 1024 * 2      # address space runs well above RSS for Python
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "LANG": "C.UTF-8"}
    if os.environ.get("ATLAS_BROWSERLESS_URL"):
        env["ATLAS_BROWSERLESS_URL"] = os.environ["ATLAS_BROWSERLESS_URL"]
    t0, peak, killed = time.time(), 0.0, None
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        child = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=err, env=env, preexec_fn=limit,
                                 start_new_session=True)
        while child.poll() is None:
            rss = _rss_mb(child.pid)
            peak = max(peak, rss or 0)
            if rss and rss > max_mb:
                killed = f"RSS {rss:.0f} MB over the {max_mb} MB cap; killed"
            elif time.time() - t0 > timeout:
                killed = f"ran over {timeout} s; killed"
            if killed:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                break
            time.sleep(1)
    return {"rc": child.returncode, "seconds": round(time.time() - t0, 1),
            "peak_rss_mb": round(peak, 1) if peak else None, "killed": killed}


def _row_key(r):
    # a claim is the same claim while its metric, year and value stay; its quote may be re-copied
    return f"{r['kind']}|{r['item']}" + ("" if r["kind"] == "claim" else f"|{r['field']}")


def find_changes(prev: dict | None, cur: dict) -> list[str]:
    """What got worse since the previous report: a claim broke, a source went archive-only, a phone
    or email is no longer on the official page."""
    if not prev:
        return []
    before = {_row_key(r): r for r in prev.get("rows") or []}
    out = []
    for r in cur.get("rows") or []:
        b = before.get(_row_key(r))
        if not b:
            continue
        if b["ok"] and not r["ok"]:
            what = {"claim": "claim", "source": "source", "contact": "office contact", "help": "help line"}[r["kind"]]
            out.append(f"{what} {r['item']}: {r['field'][:120]}: now {r['result']} (was {b['result']})")
        elif r["kind"] == "source" and b["fetched"].startswith("live") and r["fetched"].startswith("Wayback"):
            out.append(f"source {r['field']}: moved to archive-only ({r['fetched']})")
    return out


def _latest_report(paths: Paths):
    reps = sorted(paths.reports.glob("*.json"))
    if not reps:
        return None
    try:
        return json.loads(reps[-1].read_text())
    except ValueError:
        return None


def service_pr(paths: Paths, cfg: dict, fips: str, res: dict, changes: list[str], report_path: Path,
               digest: str, commit: str) -> dict:
    """Open a DRAFT pull request from the contributor's fork that touches only this county file."""
    wt = paths.worktree
    rel = res["file"]
    if not re.fullmatch(r"research/county/[A-Z]{2}/\d{5}-[a-z0-9-]+\.md", rel):
        return {"ok": False, "why": f"unexpected file path {rel}"}
    e = res["entry"]
    owner = cfg["fork"].split("/")[0]
    branch = f"county-service/{fips}-{today()}"
    tmp = Path(tempfile.mkdtemp(prefix="pr-", dir=paths.state_dir))
    pwt = tmp / "wt"
    try:
        r = sgit(wt, "worktree", "add", "--detach", str(pwt), "HEAD")
        if r.returncode:
            return {"ok": False, "why": r.stderr.strip()[:300]}
        f = pwt / rel
        text = f.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^updated:[^\n]*$", f"updated: {today()}", text, count=1)
        note = (f"Found by the county service on {today()} at upstream commit {commit[:12]} "
                f"(report sha256 {digest[:16]}):\n\n" + "\n".join(f"- {c}" for c in changes) + "\n")
        if "\n## Changes found\n" in text:
            text = text.replace("\n## Changes found\n", "\n## Changes found\n\n" + note, 1)
        elif "\n## Sources\n" in text:
            text = text.replace("\n## Sources\n", "\n## Changes found\n\n" + note + "\n## Sources\n", 1)
        else:
            text = text.rstrip("\n") + "\n\n## Changes found\n\n" + note
        f.write_text(text, encoding="utf-8")
        title = f"county: {e['county']}, {e['state']} ({fips}): changes found {today()}"
        for args in (["switch", "-c", branch], ["add", "--", rel], ["commit", "-m", title]):
            r = sgit(pwt, *args)
            if r.returncode:
                return {"ok": False, "why": f"git {args[0]}: {r.stderr.strip()[:300]}"}
        changed = sgit(pwt, "diff", "--name-only", "HEAD~1", "HEAD").stdout.split()
        if changed != [rel]:
            return {"ok": False, "why": f"refused: commit changes {changed}, not only {rel}"}
        push = sgit(pwt, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                    "push", f"https://github.com/{cfg['fork']}.git", f"HEAD:refs/heads/{branch}")
        if push.returncode:
            return {"ok": False, "why": f"push: {push.stderr.strip()[:300]}"}
        report = report_path.read_text()
        body = ("The county service found changes in this county's sources. This draft updates only "
                f"`{rel}` (`updated:` and a `## Changes found` note). Nobody should merge it before a person "
                "re-reads the sources.\n\n## Changes found\n\n" + "\n".join(f"- {c}" for c in changes) +
                f"\n\n## Check now\n\n{check_markdown(res)}\n## Report\n\nsha256 `{digest}`, upstream commit `{commit}`."
                "\n\n<details><summary>Full report (JSON)</summary>\n\n```json\n" + report[:45000] +
                ("\n... (truncated)" if len(report) > 45000 else "") + "\n```\n</details>\n")
        bf = tmp / "body.md"
        bf.write_text(body)
        gh = subprocess.run(["gh", "pr", "create", "--draft", "--repo", UPSTREAM_REPO, "--base", "master",
                             "--head", f"{owner}:{branch}", "--title", title, "--body-file", str(bf)],
                            capture_output=True, text=True, timeout=120)
        return {"ok": gh.returncode == 0, "url": gh.stdout.strip(), "why": gh.stderr.strip()[:300]}
    finally:
        sgit(wt, "worktree", "remove", "--force", str(pwt))
        sgit(wt, "worktree", "prune")
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_service_run_once(a) -> int:
    paths = Paths(a.home)
    cfg = load_config(paths)
    if paths.stop.exists():
        _log(paths, {"event": "cycle", "ok": True, "skipped": f"STOP file {paths.stop} present"})
        print(f"STOP file present ({paths.stop}); skipping this cycle")
        return EXIT_OK
    rules, commit = tamper_rules(paths, cfg)
    for r in rules:
        print(f"  [{'ok' if r['ok'] else 'FAIL'}] {r['rule']}: {r['detail']}")
    if commit is None:
        failed = [r["rule"] for r in rules if not r["ok"]]
        _log(paths, {"event": "cycle", "ok": False, "refused": failed, "rules": rules})
        print(f"REFUSED: tamper rule(s) failed: {', '.join(failed)}. Nothing was checked or changed.")
        return EXIT_TAMPER
    prev = _latest_report(paths)
    report = {"version": 1, "generated_at": utcnow(), "tool": "scripts/county.py service run-once",
              "upstream": cfg["upstream"], "commit": commit,
              "tree": sgit(paths.worktree, "rev-parse", "HEAD^{tree}").stdout.strip(),
              "isolated_python": bool(sys.flags.isolated), "max_rss_mb": cfg["max_rss_mb"],
              "tamper_rules": rules, "counties": {}}
    rc = EXIT_OK
    paths.reports.mkdir(parents=True, exist_ok=True)
    for fips in cfg["counties"]:
        out, err = paths.state_dir / f".check-{fips}.json", paths.state_dir / f".check-{fips}.err"
        cmd = [cfg.get("python") or sys.executable, "-I", "-B", str(paths.worktree / "scripts" / "county.py"),
               "check", fips, "--json"]
        run = run_capped(cmd, cwd=paths.worktree, max_mb=int(cfg["max_rss_mb"]), timeout=3600,
                         out_path=out, err_path=err)
        try:
            res = json.loads(out.read_text())
        except ValueError:
            res = {"passed": False, "verdict": "no result: " + _clip(err.read_text(errors="replace")[-300:], 300),
                   "rows": [], "errors": [], "warnings": []}
        for p in (out, err):
            p.unlink(missing_ok=True)
        if run["killed"] or (run["rc"] not in (0, 1)):
            rc = EXIT_CHILD
        res["run"] = run
        res["changes"] = find_changes((prev or {}).get("counties", {}).get(fips), res)
        report["counties"][fips] = res
        print(f"  {fips}: {res['verdict']}  (rc {run['rc']}, {run['seconds']} s, peak {run['peak_rss_mb']} MB"
              f"{', ' + run['killed'] if run['killed'] else ''})")
        for c in res["changes"]:
            print(f"    change: {c}")
    body = json.dumps(report, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode()).hexdigest()
    report["signed_off"] = {"sha256": digest, "by": "homeless-atlas county service",
                            "covers": "sha256 of this report without the signed_off key, keys sorted",
                            "commit": commit}
    rp = paths.reports / f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{commit[:12]}.json"
    rp.write_text(json.dumps(report, indent=1, default=str))
    prs = {}
    for fips, res in report["counties"].items():
        if res.get("changes") and cfg["open_prs"] and res.get("entry"):
            prs[fips] = service_pr(paths, cfg, fips, res, res["changes"], rp, digest, commit)
            print(f"  {fips}: draft PR {'opened ' + prs[fips].get('url', '') if prs[fips]['ok'] else 'NOT opened: ' + prs[fips]['why']}")
        elif res.get("changes"):
            print(f"  {fips}: changes found; open_prs is false, so no pull request")
    _log(paths, {"event": "cycle", "ok": rc == EXIT_OK, "commit": commit, "report": str(rp), "sha256": digest,
                 "counties": {f: {"passed": r.get("passed"), "changes": len(r.get("changes") or []),
                                  "peak_rss_mb": r["run"]["peak_rss_mb"], "killed": r["run"]["killed"]}
                              for f, r in report["counties"].items()}, "prs": prs})
    print(f"report: {rp}\nsha256: {digest}")
    return rc


# =========================================================================== service: status / logs
def cmd_service_status(a) -> int:
    paths, plat = Paths(a.home), platform_name(a)
    print(f"home:       {paths.home}")
    print(f"config:     {paths.config} ({'present' if paths.config.is_file() else 'missing'})")
    if paths.config.is_file():
        try:
            cfg = load_config(paths)
            print(f"            counties {', '.join(cfg['counties'])}, every {cfg['interval_hours']} h, "
                  f"open_prs={str(cfg['open_prs']).lower()}, upstream {cfg['upstream']}")
        except UsageError as e:
            print(f"            INVALID: {e}")
    unit = [paths.service_unit, paths.timer_unit] if plat == "linux" else [paths.plist]
    for u in unit:
        print(f"unit:       {u} ({'present' if u.is_file() else 'missing'})")
    loaded = "unknown"
    if plat == "linux" and shutil.which("systemctl") and not paths.custom_home:
        loaded = subprocess.run(["systemctl", "--user", "is-active", f"{SERVICE_NAME}.timer"],
                                capture_output=True, text=True).stdout.strip()
    elif plat == "macos" and shutil.which("launchctl") and not paths.custom_home:
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], capture_output=True)
        loaded = "loaded" if r.returncode == 0 else "not loaded"
    elif paths.manifest.is_file():
        loaded = "loaded" if json.loads(paths.manifest.read_text()).get("loaded") else "not loaded (--no-load)"
    print(f"scheduler:  {loaded}")
    if (paths.worktree / ".git").exists():
        h = sgit(paths.worktree, "log", "-1", "--format=%h %cs %s").stdout.strip()
        dirty = sgit(paths.worktree, "status", "--porcelain").stdout.strip()
        print(f"worktree:   {paths.worktree} at {h}{'  MODIFIED (cycles will refuse)' if dirty else ''}")
    else:
        print(f"worktree:   {paths.worktree} (missing)")
    print(f"STOP file:  {'present: cycles are skipped' if paths.stop.exists() else 'absent'}")
    last = None
    if paths.log.is_file():
        for ln in paths.log.read_text().splitlines():
            if '"event": "cycle"' in ln:
                last = ln
    if last:
        d = json.loads(last)
        print(f"last cycle: {d['ts']} ok={d.get('ok')} " + (f"refused={d['refused']}" if d.get("refused") else
                                                             f"report {d.get('report', d.get('skipped', ''))}"))
    else:
        print("last cycle: none yet")
    return EXIT_OK


def cmd_service_logs(a) -> int:
    paths = Paths(a.home)
    if not paths.log.is_file():
        print(f"no log yet at {paths.log}")
        return EXIT_OK
    lines = paths.log.read_text().splitlines()[-a.lines:]
    for ln in lines:
        if a.json:
            print(ln)
            continue
        d = json.loads(ln)
        extra = {k: v for k, v in d.items() if k not in ("ts", "event", "rules")}
        print(f"{d['ts']}  {d.get('event'):<8} {json.dumps(extra, default=str)[:300]}")
    return EXIT_OK


# =========================================================================== CLI
def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(
        prog="county.py", formatter_class=fmt,
        description="Add your county to the Homeless Atlas in a few commands.\n\n"
                    "Each county is one Markdown file, research/county/<ST>/<FIPS>-<name>.md, listing the\n"
                    "public sources you read, the numbers you quote from them and the offices responsible.\n"
                    "A FIPS code is the 5-digit county number (Erie County, PA is 42049); search\n"
                    "\"<county name> FIPS code\" if you do not know yours.",
        epilog="Typical session:\n"
               "  python3 scripts/county.py new 42049        # make the file, prefilled\n"
               "  (edit research/county/PA/42049-erie.md)\n"
               "  python3 scripts/county.py check 42049      # fix what it reports, repeat\n"
               "  python3 scripts/county.py pr 42049 --dry-run\n"
               "  python3 scripts/county.py pr 42049\n\n"
               "Run `python3 scripts/county.py <command> --help` for details. Rules for sources and\n"
               "contacts: research/county/README.md. The optional service: docs/CONTRIBUTOR_SERVICE.md.")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("new", formatter_class=fmt, help="start a county file from the template, prefilled",
                       description="Create research/county/<ST>/<FIPS>-<name>.md from TEMPLATE.md, prefilled with the\n"
                                   "county name, state, HUD Continuum(s) of Care and a commented starter list of\n"
                                   "offices the atlas already knows. Never overwrites an existing file.\n\n"
                                   "Prefill comes from the first source that answers:\n"
                                   "  1. data/atlas.db, if you have it (set ATLAS_DB to use another path)\n"
                                   "  2. the live site's /api/county/<FIPS>.json and /api/records/<FIPS>.json\n"
                                   "  3. the Census Bureau 2024 county gazetteer (name and state; CoC left as a TODO)\n"
                                   "The file's first comment says which one was used.")
    p.add_argument("fips", help="5-digit county FIPS code, e.g. 42049")
    p.add_argument("--dry-run", action="store_true", help="print the file instead of writing it")
    p.add_argument("--slug", help="file name part after the FIPS (default: from the county name)")
    p.add_argument("--source", choices=["db", "site", "gazetteer"], help="use only this prefill source")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("check", formatter_class=fmt, help="verify one county file against its live sources",
                       description="Re-fetch every source in a county file (through the fetch ladder: live, browser\n"
                                   "headers, publisher alternates, then the Internet Archive) and verify:\n"
                                   "  - each claim's exact quote and value appear in its document\n"
                                   "  - each office's and help line's phone, email, name and address appear on the\n"
                                   "    live official page cited for it (never an archive copy)\n"
                                   "Problems in the file itself (bad FIPS, missing fields, placeholder URLs) are\n"
                                   "found without the network and reported first. Needs no database.\n\n"
                                   "Exit 0 only when every claim and every phone and email verified.")
    p.add_argument("targets", nargs="+", metavar="fips|path", help="a 5-digit FIPS or a path to a county file")
    p.add_argument("--offline", action="store_true", help="only check the file's structure; fetch nothing")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="print the full result as JSON")
    g.add_argument("--markdown", action="store_true", help="print a Markdown table (for CI summaries)")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("preview", formatter_class=fmt, help="build the site locally and show the county page",
                       description="Run scripts/build_site.py --fast --force --out <temp dir> and print the paths of\n"
                                   "county/<FIPS>.html and its printable flyer. Needs data/atlas.db.")
    p.add_argument("fips", help="5-digit county FIPS code")
    p.add_argument("--out", help="build into this directory instead of a new temp dir")
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("pr", formatter_class=fmt, help="open a pull request with just this county file",
                       description="Check the file, then from a clean temporary worktree of origin/master: add only\n"
                                   "this county file, commit `county: <County>, <ST> (<FIPS>)`, push branch\n"
                                   "county/<FIPS>-<name> to YOUR fork (never to origin) and open a pull request\n"
                                   f"against {UPSTREAM_REPO} with gh. The body lists what was verified, every source,\n"
                                   "the contacts, archive-only sources and how a reviewer re-runs the check.\n\n"
                                   "Refuses when the check fails, unless --draft. Without a fork remote it only\n"
                                   "prints what it would do (make one: gh repo fork 0xSero/homeless-atlas --remote\n"
                                   "--remote-name fork).")
    p.add_argument("fips", help="5-digit county FIPS code")
    p.add_argument("--file", help="use this county file instead of looking it up under research/county")
    p.add_argument("--dry-run", action="store_true", help="print every git and gh command and the body; run nothing")
    p.add_argument("--draft", action="store_true", help="open a draft pull request even if the check fails")
    p.add_argument("--fork-remote", help="git remote of your fork (default: detected, preferring `fork`)")
    p.set_defaults(fn=cmd_pr)

    p = sub.add_parser("service", formatter_class=fmt, help="optional background checker for your counties",
                       description="A small background job that re-checks your counties every day from its own clone\n"
                                   "of upstream master, writes a JSON report, and (only with --open-prs) opens a\n"
                                   "DRAFT pull request when a source or contact changed. It never merges.\n"
                                   "Linux: a systemd user timer with memory, CPU and filesystem limits.\n"
                                   "macOS: a launchd user agent; the 1 GB memory cap is enforced by run-once.\n"
                                   "Everything it does and owns: docs/CONTRIBUTOR_SERVICE.md.")
    ssub = p.add_subparsers(dest="scmd", metavar="<action>")
    home = argparse.ArgumentParser(add_help=False)
    home.add_argument("--home", help="act on this home directory instead of yours (or set HOMELESS_ATLAS_HOME); "
                                     "for tests")
    home.add_argument("--platform", choices=["linux", "macos"], help="override the detected platform")

    q = ssub.add_parser("install", parents=[home], formatter_class=fmt, help="set up the service",
                        description="Write ~/.config/homeless-atlas/service.toml, clone upstream master into\n"
                                    "~/.local/share/homeless-atlas/worktree, write the systemd unit and timer (Linux)\n"
                                    "or launchd agent (macOS) and load it.")
    q.add_argument("--county", action="append", metavar="FIPS", help="a county to check (repeat for more)")
    q.add_argument("--interval-hours", type=int, default=24, help="hours between cycles (default 24)")
    q.add_argument("--open-prs", action="store_true", help="open DRAFT pull requests when something changed")
    q.add_argument("--fork", metavar="OWNER/REPO", help="your fork, needed with --open-prs")
    q.add_argument("--upstream", help=f"upstream git URL (default {UPSTREAM_URL}; must be https)")
    q.add_argument("--allow-local-upstream", action="store_true", help=argparse.SUPPRESS)
    q.add_argument("--python", help="interpreter to run the service with (default: this one)")
    q.add_argument("--no-load", action="store_true", help="write the unit or agent but do not load it")
    q.add_argument("--dry-run", action="store_true", help="print every file and command; write nothing")
    q.set_defaults(fn=cmd_service_install)

    q = ssub.add_parser("uninstall", parents=[home], formatter_class=fmt, help="remove the service completely",
                        description="Unload the timer or agent and delete the unit files, the worktree, the config and\n"
                                    "the state directory (reports and logs). Prints exactly what it will delete; needs\n"
                                    "--yes to do it, then confirms nothing is left.")
    q.add_argument("--yes", action="store_true", help="really delete")
    q.set_defaults(fn=cmd_service_uninstall)

    q = ssub.add_parser("status", parents=[home], help="show config, scheduler state and the last cycle")
    q.set_defaults(fn=cmd_service_status)

    q = ssub.add_parser("run-once", parents=[home], formatter_class=fmt, help="run one cycle now",
                        description="One cycle: check the tamper rules (clean worktree, HEAD is upstream master fetched\n"
                                    "over https now, fast-forward only), then check each county in a child process\n"
                                    "capped at max_rss_mb, write a report to ~/.local/state/homeless-atlas/reports/,\n"
                                    "and open draft pull requests if open_prs is true. Exit 3 when a tamper rule\n"
                                    "fails (nothing else happens), 4 when a check child crashed or was killed.")
    q.set_defaults(fn=cmd_service_run_once)

    q = ssub.add_parser("logs", parents=[home], help="print the service's cycle log")
    q.add_argument("-n", "--lines", type=int, default=20, help="how many entries (default 20)")
    q.add_argument("--json", action="store_true", help="raw JSON lines")
    q.set_defaults(fn=cmd_service_logs)
    p.set_defaults(fn=lambda a: (p.print_help(), EXIT_USAGE)[1])
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return EXIT_USAGE
    try:
        return a.fn(a)
    except UsageError as e:
        print(f"county.py: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
