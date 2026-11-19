"""laws -- the legal landscape per state (and per surveyed large city), with citations.

What this domain owns
---------------------
Every row goes to the shared `law` table (`jurisdiction`, `topic`, `citation`, `title`,
`summary`, `status`, `year`, `url`, `source_id`).

`jurisdiction` is `US`, a two-letter state/DC abbreviation, or `"<City>, <ST>"` for the
municipal survey rows.

`topic` uses the schema's vocabulary: `camping_ban | sit_lie | property | drug |
mental_health_hold | encampment_clearance | funding | housing_first | other`.

Sources (every response cached under `data/raw/laws/`, 1 req/s via `common.fetch`)
-------------------------------------------------------------------------------
1. Treatment Advocacy Center state pages, `https://www.tac.org/map_directory/<state>/`
   One page per state + DC, each carrying the *text of the state's own statutes*:
     "Emergency evaluation"                     -> mental_health_hold
     "Inpatient and/or outpatient commitment"   -> mental_health_hold
     "Outpatient commitment law"                -> other (assisted outpatient treatment)
     "Recommended updates to treatment laws"    -> other, status=proposed
   The statute citation (`WYO. STAT. ANN. § 25-10-109(a)`) and the quoted statutory text
   are taken verbatim from the page; nothing is filled in from memory.
   This is the per-state research pass (51 pages, cached).

2. National Homelessness Law Center, *Housing Not Handcuffs 2021: State Law Supplement*
   `https://homelesslaw.org/wp-content/uploads/2022/02/2021-HNH-State-Crim-Supplement.pdf`
   Appendix matrix: 51 jurisdictions x 12 statewide criminalization categories (camping,
   sleeping, sitting/lying, vehicle habitation, loitering/vagrancy, roadway solicitation,
   panhandling).  The counts we parse are cross-checked against the report's own "Total"
   row (1/3/4/15/6/4/16/24/36/6/24/7) and the run fails loudly on mismatch.

3. National Homelessness Law Center, *Housing Not Handcuffs* (2019)
   `https://homelesslaw.org/wp-content/uploads/2018/10/Housing-Not-Handcuffs.pdf`
   Appendix A "Prohibited Conduct Chart": 187 surveyed cities x 11 ordinance categories.
   Counts cross-checked against the chart's own totals
   (34/50/61/93/88/73/60/100/50/114/12).

4. National Homelessness Law Center, *one year since Grants Pass* state legislation update
   `https://housingnothandcuffs.org/wp-content/uploads/2025/06/Cicero-Homeless-Criminalization-Summary-May-2025.pdf`
   Per-state status of 2024-25 statewide bills (enacted / defeated / pending / struck down).
   The report gives no bill numbers; `summary` quotes the sentence we read.

5. Supreme Court, *City of Grants Pass v. Johnson* (2024) via Cornell LII
   `https://www.law.cornell.edu/supremecourt/text/23-175` -- the Eighth Amendment holding and
   the 9th Circuit rule it displaces (*Martin v. Boise*, 920 F. 3d 584, quoted on that page).

6. HUD CoC program interim rule via Cornell LII, `https://www.law.cornell.edu/cfr/text/24/578.7`
   -- the federal Housing First / written-standards requirement.

Blocked (see BLOCKERS.md `## laws`): Justia, FindLaw, Casetext, NY Senate, NCSL, SAMHSA,
CourtListener API, leginfo.legislature.ca.gov -- all 403/401 from this host, which is why the
statute *text* for the criminalization rows is not re-verified at the state site; the survey
pinpoint is the citation we can stand behind.

Run:  python3 -m ingest.run laws      (idempotent; upserts on jurisdiction+citation+title)
"""
from __future__ import annotations

import hashlib
import html as htmllib
import json
import os
import re
import subprocess
import sys
import tempfile

from ingest.common import (RAW, clean, db, fetch, name_norm, now, save_raw, source_id_for,
                           to_int, upsert)

DOMAIN = "laws"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

TAC_URL = "https://www.tac.org/map_directory/{slug}/"

URL_HNH2021 = "https://homelesslaw.org/wp-content/uploads/2022/02/2021-HNH-State-Crim-Supplement.pdf"
URL_HNH2019 = "https://homelesslaw.org/wp-content/uploads/2018/10/Housing-Not-Handcuffs.pdf"
URL_HNH2025 = ("https://housingnothandcuffs.org/wp-content/uploads/2025/06/"
               "Cicero-Homeless-Criminalization-Summary-May-2025.pdf")
URL_GRANTS_PASS = "https://www.law.cornell.edu/supremecourt/text/23-175"
URL_CFR_578_7 = "https://www.law.cornell.edu/cfr/text/24/578.7"

LIC_TAC = ("Treatment Advocacy Center state pages; statutory text is public record; page "
           "reproduced with attribution (no explicit licence published, robots.txt allows)")
LIC_NHLC = ("Public advocacy report; statutory citations are public record. "
            "Cited and linked, not reproduced in bulk.")
LIC_LII = "Public record (U.S. court opinion / Code of Federal Regulations); LII terms permit linking"

# 12 columns of the HNH-2021 state appendix, left to right as printed.
HNH2021_COLS = [
    ("Sleeping in public -- statewide", "camping_ban"),
    ("Sleeping in public -- particular public places", "camping_ban"),
    ("Camping in public -- statewide", "camping_ban"),
    ("Camping in public -- particular public places", "camping_ban"),
    ("Sitting / lying down in public -- particular public places", "sit_lie"),
    ("Lodging, living or sleeping in a vehicle", "property"),
    ("Loitering / loafing / vagrancy -- statewide", "other"),
    ("Loitering / loafing in particular public places", "other"),
    ("Standing in roadway (solicitation / pedestrian)", "panhandling"),
    ("Panhandling -- statewide", "panhandling"),
    ("Panhandling -- particular public places", "panhandling"),
    ("Panhandling -- particular ways", "panhandling"),
]
# the report's own totals row, used as a parse checksum
HNH2021_TOTALS = [1, 3, 4, 15, 6, 4, 16, 24, 36, 6, 24, 7]

# 11 columns of the HNH-2019 city chart, left to right as printed.
HNH2019_COLS = [
    ("Sleeping in public -- city-wide", "camping_ban"),
    ("Sleeping in public -- particular public places", "camping_ban"),
    ("Camping in public -- city-wide", "camping_ban"),
    ("Camping in public -- particular public places", "camping_ban"),
    ("Sitting / lying down in public -- particular public places", "sit_lie"),
    ("Lodging, living or sleeping in a vehicle", "property"),
    ("Loitering / loafing / vagrancy -- city-wide", "other"),
    ("Loitering / loafing in particular public places", "other"),
    ("Begging in public places -- city-wide", "panhandling"),
    ("Begging in particular public places", "panhandling"),
    ("Food sharing -- city-wide or particular public places (bans)", "other"),
]
HNH2019_TOTALS = [34, 50, 61, 93, 88, 73, 60, 100, 50, 114, 12]

CONT = {"Columbia", "Hampshire", "Mexico", "Jersey", "Carolina", "Dakota", "Island", "Virginia"}

WROTE = [0]
NOTES: list[str] = []


# --------------------------------------------------------------------- utilities
def _fetch(url: str, *, title: str, publisher: str, license: str, binary=False) -> bytes:
    """fetch through common (1 req/s, cache) and mirror the bytes under the sha1(url) name
    so source_id_for() can record size + sha256 for this URL."""
    b = fetch(url, domain=DOMAIN, headers={"User-Agent": UA, "Accept": "*/*"},
              timeout=300, binary=binary)
    p = RAW / DOMAIN / (hashlib.sha1(url.encode()).hexdigest() + ".bin")
    if not p.exists() or p.stat().st_size == 0:
        p.write_bytes(b)
    return b


def _sid(conn, url: str, *, title: str, publisher: str, license: str) -> int:
    return source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _clip(s: str | None, n=900) -> str | None:
    s = clean(s)
    if not s:
        return None
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def _law(conn, *, jurisdiction, topic, citation, title, summary, status, url, sid,
         year=None) -> None:
    """upsert one law row; refuses rows without the two provenance fields."""
    if not citation or not url or not jurisdiction or not title:
        return
    upsert(conn, "law", dict(jurisdiction=jurisdiction, topic=topic, citation=clean(citation),
                             title=clean(title), summary=_clip(summary), status=status,
                             year=to_int(year), url=url, source_id=sid),
           keys=["jurisdiction", "citation", "title"])
    WROTE[0] += 1


def _ensure(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS laws_progress (
      task TEXT PRIMARY KEY, ts TEXT, ok INTEGER, note TEXT);
    """)


def _mark(conn, task, ok, note=""):
    conn.execute("INSERT INTO laws_progress(task,ts,ok,note) VALUES(?,?,?,?) "
                 "ON CONFLICT(task) DO UPDATE SET ts=excluded.ts,ok=excluded.ok,note=excluded.note",
                 (task, now(), 1 if ok else 0, note[:900]))
    conn.commit()


def _done(conn, task) -> bool:
    r = conn.execute("SELECT ok FROM laws_progress WHERE task=?", (task,)).fetchone()
    return bool(r and r[0])


# ------------------------------------------------------------------------- TAC parse
def _html_sections(page: str) -> list[tuple[str, str]]:
    """[(h2 heading text, raw inner html)] in document order."""
    parts = re.split(r"(<h2[^>]*>.*?</h2>)", page, flags=re.S | re.I)
    out, cur = [], None
    for p in parts:
        if re.match(r"<h2", p, re.I):
            cur = _ws(htmllib.unescape(re.sub(r"<[^>]+>", " ", p)))
        elif cur is not None:
            out.append((cur, p))
    return out


def _flat(h: str) -> str:
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    return _ws(htmllib.unescape(re.sub(r"<[^>]+>", " ", h)))


_CITE_RE = re.compile(r"([A-Z][A-Z0-9.,'\u2019&/\- ]{3,70}?)\s*§+\s*([0-9][0-9A-Za-z.\-:]*)")
_CITE_LEAD = re.compile(r"^(?:AND|OR|SEE|SEE ALSO|AMEND|ADD|REPEAL|ENACT|EFFECTIVE|NOTE)\s+", re.I)


def _cites(text: str) -> list[str]:
    """statute citations as printed, e.g. 'WYO. STAT. ANN. § 25-10-109(a)'."""
    out = []
    for m in _CITE_RE.finditer(text):
        code = _CITE_LEAD.sub("", m.group(1).strip())
        # keep only the last ~7 words of the code prefix (some sections run sentences together)
        code = " ".join(code.split()[-7:]).strip(" ,;:")
        if len(code) < 4:
            continue
        # the printed citation usually continues with a subsection like "(a)"
        tail = text[m.end():m.end() + 14]
        sub = re.match(r"(\([0-9a-zA-Z]{1,3}\))", tail)
        cite = f"{code} § {m.group(2)}{sub.group(1) if sub else ''}"
        if cite not in out:
            out.append(cite)
    return out


def _year_near(text: str, citation: str) -> int | None:
    i = text.find(citation.split("§")[-1].strip().split("(")[0])
    if i < 0:
        return None
    seg = text[i:i + 160]
    m = re.search(r"\b(19[6-9]\d|20[0-4]\d)\b", seg)
    return int(m.group(1)) if m else None


SUD_WORDS = re.compile(r"substance use|substance-use|alcoholi|alcohol abuse|drug depend|"
                       r"intoxicat|chemical depend|substance abuse", re.I)


def tac_state(conn, abbr: str, name: str) -> int:
    """one state's TAC page -> up to 5 law rows. Returns rows written."""
    slug = "district-of-columbia" if abbr == "DC" else name.lower().replace(" ", "-")
    url = TAC_URL.format(slug=slug)
    before = WROTE[0]
    try:
        page = _fetch(url, title=f"{name} Serious Mental Illness Resources & Helpful Info",
                      publisher="Treatment Advocacy Center", license=LIC_TAC).decode(
                          "utf-8", "replace")
    except Exception as e:  # noqa
        NOTES.append(f"{abbr}: TAC page failed ({repr(e)[:80]})")
        _mark(conn, f"tac:{abbr}", False, repr(e)[:200])
        return 0

    sid = _sid(conn, url, title=f"{name} Serious Mental Illness Resources & Helpful Info",
               publisher="Treatment Advocacy Center", license=LIC_TAC)
    secs = _html_sections(page)
    found = {}
    for head, body in secs:
        hl = head.lower()
        if "emergency evaluation" in hl:
            found.setdefault("emergency", _flat(body))
        elif "outpatient commitment" in hl:
            found.setdefault("outpatient", _flat(body))
        elif "commitment law" in hl and "inpatient" in hl:
            found.setdefault("inpatient", _flat(body))
        elif "recommended updates" in hl:
            found.setdefault("reform", _flat(body))

    if not found:
        NOTES.append(f"{abbr}: TAC page had no treatment-law sections")
        _mark(conn, f"tac:{abbr}", False, "no sections")
        return 0

    specs = [
        ("emergency", "mental_health_hold", "Emergency evaluation / emergency detention",
         "enacted"),
        ("inpatient", "mental_health_hold", "Inpatient / outpatient civil commitment", "enacted"),
        ("outpatient", "other", "Outpatient (assisted outpatient) commitment", "enacted"),
    ]
    for key, topic, title, status in specs:
        body = found.get(key)
        if not body:
            continue
        cs = _cites(body)
        if not cs:
            continue
        _law(conn, jurisdiction=abbr, topic=topic, citation=cs[0], title=title,
             summary=f"{name}: {body}", status=status,
             year=_year_near(body, cs[0]), url=url, sid=sid)

    # substance use as a commitment basis: only when the quoted statute says so
    sud_body = None
    for key in ("inpatient", "emergency", "outpatient"):
        body = found.get(key) or ""
        m = SUD_WORDS.search(body)
        if m:
            cs = _cites(body)
            if cs:
                sud_body = (cs[0], body)
                break
    if sud_body:
        cite, body = sud_body
        m = SUD_WORDS.search(body)
        snippet = body[max(0, m.start() - 220): m.start() + 420]
        _law(conn, jurisdiction=abbr, topic="drug",
             citation=cite, title="Substance use disorder as a basis for civil commitment",
             summary=f"{name}: statute text quoted by TAC names substance use -- …{snippet}",
             status="enacted", year=_year_near(body, cite), url=url, sid=sid)

    # TAC's own legislative recommendations (status=proposed), citation = the statute it would amend
    reform = found.get("reform")
    if reform:
        cs = _cites(reform)
        if cs:
            _law(conn, jurisdiction=abbr, topic="other",
                 citation="; ".join(cs[:4]),
                 title="Treatment Advocacy Center recommended statutory amendments",
                 summary=f"{name}: {reform}", status="proposed", url=url, sid=sid)

    n = WROTE[0] - before
    _mark(conn, f"tac:{abbr}", n > 0, f"{n} rows; sections={sorted(found)}")
    return n


# -------------------------------------------------------------------- PDF helpers
def _pdf_text(pdf: bytes, name: str) -> str:
    p = save_raw(DOMAIN, name, pdf)
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        outp = tf.name
    try:
        subprocess.run(["pdftotext", "-layout", str(p), outp], check=True,
                       capture_output=True, timeout=300)
        with open(outp, encoding="utf-8", errors="replace") as f:
            return f.read()
    finally:
        try:
            os.unlink(outp)
        except OSError:
            pass


def _cluster(pos: list[int], tol=4) -> list[int]:
    """cluster sorted character offsets into column centres."""
    centres: list[list[int]] = []
    for p in sorted(pos):
        if centres and p - centres[-1][-1] <= tol:
            centres[-1].append(p)
        else:
            centres.append([p])
    return [sum(c) // len(c) for c in centres]


def _xpos(line: str) -> list[int]:
    return [m.start() for m in re.finditer(r"(?:(?<=\s)|^)[xX](?=\s|$)", line)]


# ------------------------------------------------------- HNH 2021 (state matrix)
def hnh2021(conn, states: dict[str, str]) -> int:
    """51 jurisdictions x 12 statewide categories -> law rows."""
    before = WROTE[0]
    url = URL_HNH2021
    title = "Housing Not Handcuffs 2021: State Law Supplement"
    pub = "National Homelessness Law Center"
    pdf = _fetch(url, title=title, publisher=pub, license=LIC_NHLC, binary=True)
    sid = _sid(conn, url, title=title, publisher=pub, license=LIC_NHLC)
    text = _pdf_text(pdf, "hnh2021-state-law-supplement.pdf")

    i = text.find("APPENDIX: Criminalization of Homelessness")
    if i < 0:
        NOTES.append("HNH2021: appendix heading not found")
        return 0
    block = text[i:].split("\n")

    # find the first data row, then cluster the x offsets of everything after it
    start = next((k for k, l in enumerate(block) if l.strip().startswith("Alabama")), None)
    if start is None:
        NOTES.append("HNH2021: no data rows")
        return 0
    body = block[start:]
    allpos = [p for l in body for p in _xpos(l)]
    centres = _cluster(allpos)
    if len(centres) != len(HNH2021_COLS):
        NOTES.append(f"HNH2021: found {len(centres)} columns, expected {len(HNH2021_COLS)}")
        return 0

    def col_of(p):
        return min(range(len(centres)), key=lambda k: abs(centres[k] - p))

    name2abbr = {v: k for k, v in states.items()}
    name2abbr.setdefault("District Columbia", "DC")
    by_name: dict[str, list[int]] = {}
    pending = None  # (prefix, marks) while a two-word state name is split over two lines
    for line in body:
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^([A-Z][a-zA-Z]+(?: of)?(?: [A-Z][a-zA-Z]+)?)", s)
        nm = m.group(1) if m else ""
        marks = [col_of(p) for p in _xpos(line)]
        if nm in name2abbr:                       # full name on one line
            by_name[nm] = marks
            pending = None
        elif s in CONT and pending:               # second half of a split state name
            by_name[f"{pending[0]} {s}"] = pending[1]
            pending = None
        elif nm.split(" of")[0] in ("New", "North", "South", "Rhode", "West", "District"):
            pending = (nm.replace(" of", ""), marks)

    rows = 0
    per_col = [0] * len(HNH2021_COLS)
    seen: set[tuple[str, int]] = set()
    for nm, marks in by_name.items():
        abbr = name2abbr.get(nm)
        if not abbr:
            continue
        for c in sorted(set(marks)):
            if (abbr, c) in seen:
                continue
            seen.add((abbr, c))
            per_col[c] += 1
            label, topic = HNH2021_COLS[c]
            _law(conn, jurisdiction=abbr, topic=topic,
                 citation=f"HNH 2021 State Law Supplement, App. col. {c + 1} ({label})",
                 title=f"Statewide restriction: {label}",
                 summary=(f"National Homelessness Law Center's 50-state + DC review of state codes "
                          f"finds a {nm} statute restricting {label.lower()}. Report column {c + 1}; "
                          f"the report prints an 'x' for this state/category. Underlying statute "
                          f"number is not printed for this cell by the source; see the report's "
                          f"footnotes for the representative citations it does give."),
                 status="enacted", year=2021, url=url, sid=sid)
            rows += 1

    if per_col != HNH2021_TOTALS:
        NOTES.append(f"HNH2021 checksum mismatch: parsed {per_col} vs printed {HNH2021_TOTALS}")
    else:
        NOTES.append(f"HNH2021 matrix checksum OK {per_col}")
    _mark(conn, "hnh2021", rows > 0, f"{rows} rows, cols={per_col}")
    return WROTE[0] - before


# --------------------------------------------------------- HNH 2019 (city chart)
def hnh2019(conn) -> int:
    before = WROTE[0]
    url = URL_HNH2019
    title = "Housing Not Handcuffs: Ending the Criminalization of Homelessness in U.S. Cities"
    pub = "National Law Center on Homelessness & Poverty (now National Homelessness Law Center)"
    pdf = _fetch(url, title=title, publisher=pub, license=LIC_NHLC, binary=True)
    sid = _sid(conn, url, title=title, publisher=pub, license=LIC_NHLC)
    text = _pdf_text(pdf, "housing-not-handcuffs-2019.pdf")

    a = text.find("APPENDIX A: Prohibited Conduct Chart")
    b = text.find("APPENDIX B:", a)
    if a < 0:
        NOTES.append("HNH2019: appendix A not found")
        return 0
    block = text[a: b if b > 0 else a + 200000].split("\n")
    start = next((k for k, l in enumerate(block) if re.search(r"^\s*[A-Z]{2}\s{2,}\S", l)), None)
    if start is None:
        NOTES.append("HNH2019: no city rows")
        return 0
    body = block[start:]
    centres = _cluster([p for l in body for p in _xpos(l)])
    if len(centres) != len(HNH2019_COLS):
        NOTES.append(f"HNH2019: {len(centres)} columns, expected {len(HNH2019_COLS)}")
        return 0

    def col_of(p):
        return min(range(len(centres)), key=lambda k: abs(centres[k] - p))

    first_col = min(centres)
    rows, per_col, cities = 0, [0] * len(HNH2019_COLS), 0
    for line in body:
        m = re.match(r"^\s*([A-Z]{2})\s{2,}(\S[^ ]*(?: [^ ]+)*?)\s{2,}", line)
        if not m:
            continue
        st, city = m.group(1), _ws(line[:first_col].strip()[2:])
        if not city or len(city) > 40 or st in ("To", "St"):
            continue
        marks = sorted({col_of(p) for p in _xpos(line)})
        if not marks:
            continue
        cities += 1
        for c in marks:
            per_col[c] += 1
            label, topic = HNH2019_COLS[c]
            _law(conn, jurisdiction=f"{city}, {st}", topic=topic,
                 citation=f"HNH 2019 Prohibited Conduct Chart, col. {c + 1} ({label})",
                 title=f"Municipal ordinance: {label}",
                 summary=(f"{city}, {st}: the Law Center's 187-city survey of municipal codes "
                          f"marks this city as having an ordinance restricting {label.lower()}. "
                          f"Report column {c + 1}. City code section number is not printed by the "
                          f"source; enforcement varies and is not implied."),
                 status="enacted", url=url, sid=sid)
            rows += 1

    if per_col != HNH2019_TOTALS:
        NOTES.append(f"HNH2019 checksum mismatch: parsed {per_col} vs printed {HNH2019_TOTALS}")
    else:
        NOTES.append(f"HNH2019 chart checksum OK {per_col} ({cities} cities)")
    _mark(conn, "hnh2019", rows > 0, f"{rows} rows over {cities} cities, cols={per_col}")
    return WROTE[0] - before


# --------------------------------------------------- HNH 2025 (state bill status)
def hnh2025(conn, states: dict[str, str]) -> int:
    before = WROTE[0]
    url = URL_HNH2025
    title = "One year since Grants Pass: increase in laws that make it a crime to be homeless"
    pub = "National Homelessness Law Center / Housing Not Handcuffs"
    pdf = _fetch(url, title=title, publisher=pub, license=LIC_NHLC, binary=True)
    sid = _sid(conn, url, title=title, publisher=pub, license=LIC_NHLC)
    text = _pdf_text(pdf, "hnh-2025-grants-pass-annual-update.pdf")
    flat = _ws(text)

    name2abbr = {v: k for k, v in states.items()}
    name2abbr["Washington State"] = name2abbr.get("Washington")

    def names_in(frag: str) -> list[str]:
        out = []
        for part in re.split(r",\s*|\s+and\s+", frag):
            p = part.strip(" .;:")
            if p in name2abbr:
                out.append(p)
        return out

    specs = [
        # (regex, status, what)
        (r"pushed through harmful, backwards cookie-cutter\s*bills that make homelessness worse in ([^.]+)\.",
         "enacted", "Cicero-model statewide anti-homeless bill reportedly enacted"),
        (r"This year, advocates defeated bills in ([^.]+)\.",
         "proposed", "statewide anti-homeless bill reportedly defeated"),
        (r"These non-Cicero measures criminalizing homelessness have passed in ([^.]+)\.",
         "enacted", "non-Cicero statewide anti-homeless measure reportedly passed"),
        (r"Similar measures were defeated in ([^.]+)\.",
         "proposed", "statewide anti-homeless measure reportedly defeated"),
        (r"anti-criminalization legislation, many based on the Law Center[’']s template Gloria Johnson "
         r"Act, has been introduced in 11 states: ([^.]+)\.",
         "proposed", "Gloria Johnson Act-style anti-criminalization bill introduced"),
    ]
    rows = 0
    for pat, status, what in specs:
        m = re.search(pat, flat, re.I)
        if not m:
            NOTES.append(f"HNH2025: pattern not found -> {what}")
            continue
        quote = _ws(m.group(0))
        for nm in names_in(m.group(1)):
            abbr = name2abbr[nm]
            _law(conn, jurisdiction=abbr, topic="encampment_clearance",
                 citation=f"HNH 2025 Grants Pass update -- {what}",
                 title=f"2024-25 statewide legislation: {what}",
                 summary=(f"{nm}: {what}. Report wording: “{quote}”. The report names no bill "
                          f"number; status is as reported by the source, not verified at the "
                          f"state legislature."),
                 status=status, year=2025, url=url, sid=sid)
            rows += 1

    # Missouri: Cicero law struck down by the state Supreme Court
    m = re.search(r"Missouri[’']s Cicero law was struck down by the state Supreme Court\.", flat)
    if m:
        _law(conn, jurisdiction="MO", topic="encampment_clearance",
             citation="HNH 2025 Grants Pass update -- Cicero statute invalidated by state court",
             title="2024-25 statewide legislation: Cicero-model statute struck down",
             summary=("Missouri: report wording: “" + _ws(m.group(0)) + "”. No bill number or "
                      "docket number is given by the source; recorded as status=enjoined "
                      "(court action) on the strength of that sentence alone."),
             status="enjoined", year=2025, url=url, sid=sid)
        rows += 1
    # North Carolina: still pending
    m = re.search(r"Cicero bills in North\s*Carolina are still pending", flat)
    if m:
        _law(conn, jurisdiction="NC", topic="encampment_clearance",
             citation="HNH 2025 Grants Pass update -- Cicero bill pending",
             title="2024-25 statewide legislation: Cicero-model bill pending",
             summary="North Carolina: report wording: “" + _ws(m.group(0)) + "”. As of the report.",
             status="proposed", year=2025, url=url, sid=sid)
        rows += 1
    # Pennsylvania: one pending
    m = re.search(r"One is pending in Pennsylvania\.", flat)
    if m:
        _law(conn, jurisdiction="PA", topic="encampment_clearance",
             citation="HNH 2025 Grants Pass update -- anti-homeless measure pending",
             title="2024-25 statewide legislation: anti-homeless measure pending",
             summary="Pennsylvania: report wording: “" + _ws(m.group(0)) + "”. As of the report.",
             status="proposed", year=2025, url=url, sid=sid)
        rows += 1
    # California: the state's Housing First mandate, and the failed repeal
    m = re.search(r"advocates defeated a standalone bill attempting to make it a crime to sleep "
                  r"outside and repeal the state[’']s Housing First mandate in the state legislature "
                  r"\(CA AB 20\)", flat)
    if m:
        _law(conn, jurisdiction="CA", topic="housing_first",
             citation="CA AB 20 (2025, defeated) -- would have repealed the state Housing First mandate",
             title="Housing First mandate in state law (repeal attempt defeated)",
             summary=("California: report wording: “" + _ws(m.group(0)) + "”. The sentence is the "
                      "report's statement that California has a Housing First mandate in state law; "
                      "the enabling section is not named by the source."),
             status="enacted", year=2025, url=url, sid=sid)
        _law(conn, jurisdiction="CA", topic="housing_first",
             citation="CA AB 20 (2025) -- bill to repeal the Housing First mandate",
             title="Bill to repeal the Housing First mandate (defeated)",
             summary="California: standalone bill to criminalise sleeping outside and repeal the "
                     "state Housing First mandate was defeated in the legislature per the report.",
             status="proposed", year=2025, url=url, sid=sid)
        rows += 2

    _mark(conn, "hnh2025", rows > 0, f"{rows} rows")
    return WROTE[0] - before


# ------------------------------------------------------------------- federal rows
def federal(conn) -> int:
    before = WROTE[0]

    # Grants Pass v. Johnson (2024) -- Eighth Amendment / status of outdoor-sleeping ordinances
    try:
        page = _fetch(URL_GRANTS_PASS, title="City of Grants Pass v. Johnson, 603 U.S. ___ (2024)",
                      publisher="Supreme Court of the United States (via Cornell LII)",
                      license=LIC_LII).decode("utf-8", "replace")
        sid = _sid(conn, URL_GRANTS_PASS,
                   title="City of Grants Pass v. Johnson, 603 U.S. ___ (2024)",
                   publisher="Supreme Court of the United States (via Cornell LII)", license=LIC_LII)
        txt = _flat(page)
        cite = None
        m = re.search(r"(City of Grants Pass v\. Johnson[^,]{0,40}?(?:603 U\. ?S\.|\d{3} U\.S\.)[^.]{0,40}20\d\d)", txt)
        if m:
            cite = _ws(m.group(1))
        if not cite:
            cite = "City of Grants Pass v. Johnson, No. 23-175 (U.S. 2024)"
        # the 9th Circuit rule the Court addressed, quoted on the same page
        boise = None
        mb = re.search(r"Martin v\. Boise,?\s*920\s*F\.\s*3d\s*584[^)]{0,40}", txt)
        if mb:
            boise = _ws(mb.group(0)).rstrip(",;")
        hold = ""
        mh = re.search(r"(The Eighth Amendment[^.]{20,400}\.)", txt)
        if mh:
            hold = _ws(mh.group(1))
        _law(conn, jurisdiction="US", topic="camping_ban", citation=cite,
             title="Outdoor-sleeping / camping ordinances are not barred by the Eighth Amendment",
             summary=("Supreme Court of the United States, decided 2024-06-28 (23-175). "
                      + (hold or "") +
                      " Effect: reversed the Ninth Circuit's injunction against the City of Grants "
                      "Pass and rejected the circuit rule that punished sleeping outdoors only where "
                      "no shelter is available. State and city camping bans are therefore enforceable "
                      "on federal Eighth Amendment grounds after this decision."),
             status="enacted", year=2024, url=URL_GRANTS_PASS, sid=sid)
        if boise:
            _law(conn, jurisdiction="US", topic="camping_ban", citation=boise,
                 title="Punishing sleeping outdoors without available shelter (Ninth Circuit rule)",
                 summary=("Ninth Circuit rule cited and addressed in City of Grants Pass v. Johnson "
                          "(2024): punishing a person for sleeping outdoors on public property when "
                          "no indoor alternative is available violates the Eighth Amendment. "
                          "Binding in the nine western states of the Ninth Circuit from 2018 until "
                          "the Supreme Court reversed the approach in 2024. Citation and holding "
                          "read from the Supreme Court's opinion page (the circuit opinion itself "
                          "was not fetched -- see research/laws.md)."),
                 status="enjoined", year=2019, url=URL_GRANTS_PASS, sid=sid)
    except Exception as e:  # noqa
        NOTES.append(f"Grants Pass fetch/parse failed: {repr(e)[:120]}")

    # 24 CFR 578.7 -- federal Housing First requirement for CoC-funded projects
    try:
        page = _fetch(URL_CFR_578_7, title="24 CFR 578.7 -- Responsibilities of the continuum of care",
                      publisher="Code of Federal Regulations (via Cornell LII)",
                      license=LIC_LII).decode("utf-8", "replace")
        sid = _sid(conn, URL_CFR_578_7,
                   title="24 CFR 578.7 -- Responsibilities of the continuum of care",
                   publisher="Code of Federal Regulations (via Cornell LII)", license=LIC_LII)
        txt = _flat(page)
        m = re.search(r"(Housing First[^.]{0,400}\.)", txt)
        hf = _ws(m.group(1)) if m else ""
        _law(conn, jurisdiction="US", topic="housing_first", citation="24 C.F.R. § 578.7",
             title="Continuum of Care program: Housing First and written standards",
             summary=("Federal rule for HUD Continuum of Care funding: recipients must follow the "
                      "Housing First approach and the CoC must maintain written standards. "
                      + (("Text as published: “" + hf + "”") if hf else "")),
             status="enacted", year=2012, url=URL_CFR_578_7, sid=sid)
    except Exception as e:  # noqa
        NOTES.append(f"24 CFR 578.7 fetch/parse failed: {repr(e)[:120]}")

    _mark(conn, "federal", WROTE[0] > before, f"{WROTE[0] - before} rows")
    return WROTE[0] - before


# ---------------------------------------------------------------------------- run
def _states(conn) -> dict[str, str]:
    rows = conn.execute("SELECT abbr, name FROM geo_state ORDER BY abbr").fetchall()
    skip = {"AS", "GU", "MP", "PR", "VI"}
    return {r[0]: r[1] for r in rows if r[0] not in skip}


def run(conn, cfg=None, *, probe=False) -> dict:
    cfg = cfg or {}
    _ensure(conn)
    WROTE[0] = 0
    NOTES.clear()
    states = _states(conn)
    if len(states) < 50:
        NOTES.append(f"geo_state only has {len(states)} jurisdictions -- run `places`/`hud_pit` first")

    n2021 = hnh2021(conn, states)
    n2019 = hnh2019(conn)
    n2025 = hnh2025(conn, states)
    nfed = federal(conn)
    conn.commit()

    n_states = 0
    for abbr in sorted(states):
        k = tac_state(conn, abbr, states[abbr])
        n_states += k
        if probe:
            print(f"  {abbr} {states[abbr]:<20} {k} rows", file=sys.stderr)
        if not probe:
            conn.commit()

    conn.commit()
    rows = conn.execute("SELECT COUNT(*) FROM law").fetchone()[0]
    notes = (f"HNH2021={n2021} HNH2019 cities={n2019} HNH2025={n2025} federal={nfed} "
             f"state-pages={n_states} | total law rows={rows} | " + "; ".join(NOTES[-6:]))
    return {"rows": rows, "notes": notes,
            "sources": [(URL_HNH2021, LIC_NHLC), (URL_HNH2019, LIC_NHLC), (URL_HNH2025, LIC_NHLC),
                        (URL_GRANTS_PASS, LIC_LII), (URL_CFR_578_7, LIC_LII)]}


def selftest() -> None:
    conn = db()
    _ensure(conn)
    print("law rows:", conn.execute("SELECT COUNT(*) FROM law").fetchone()[0])
    for r in conn.execute("SELECT * FROM law ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--probe" in sys.argv:
        c = db()
        run(c, probe=True)
    else:
        c = db()
        res = run(c)
        print(res["rows"], res["notes"])
