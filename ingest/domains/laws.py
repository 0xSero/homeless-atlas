"""laws -- the legal landscape per state (and per surveyed large city), with citations.

Where the rows go
-----------------
Everything lands in the shared `law` table:
    jurisdiction  "US" | two-letter state/DC abbr | "<City>, <ST>" (municipal survey rows)
    topic         camping_ban | sit_lie | property | drug | mental_health_hold |
                  encampment_clearance | funding | housing_first | other
    citation      the statute section where the source prints one, otherwise a pinpoint
                  locator into the source table the value was read from
    title/summary status (enacted|enjoined|repealed|proposed) year url source_id

Sources actually used (every response cached under data/raw/laws/, 1 req/s via common.fetch)
--------------------------------------------------------------------------------------------
1. Treatment Advocacy Center state pages, https://www.tac.org/map_directory/<state>/
   51 pages (50 states + DC), fetched one per jurisdiction = the per-state research pass.
   Each page reproduces the *text of that state's own statutes* in an accordion:
     "Emergency evaluation"                -> mental_health_hold, citation e.g. "ALA. CODE § 22-52-91(a)"
     "Inpatient [and/or] commitment law"   -> mental_health_hold, separate statute section
     "Outpatient commitment law"           -> other (assisted outpatient treatment)
     "Recommended updates to treatment laws" -> other, status=proposed (the amendments TAC asks for)
     substance-use words in the quoted text -> drug, citation = the section they appear in
   Citation strings and quoted text are taken verbatim from the page; nothing is filled in
   from memory.

2. National Homelessness Law Center, *Housing Not Handcuffs 2021: State Law Supplement* (PDF)
   Appendix matrix: 51 jurisdictions x 12 statewide restriction categories (sleeping/camping,
   sitting/lying, vehicle lodging, loitering/vagrancy, roadway standing, panhandling).
   The parser is validated against the report's own printed Total row
   (1/3/4/15/6/4/16/24/36/6/24/7); if the parse does not reproduce it exactly, no rows are
   written and the run reports the mismatch.

3. National Homelessness Law Center, *Housing Not Handcuffs* (2019), Appendix A
   "Prohibited Conduct Chart" -- 187 surveyed cities x 11 municipal ordinance categories
   (2016 chart reproduced in the 2019 report). Validated against the chart's printed totals
   (34/50/61/93/88/73/60/100/50/114/12); same fail-loud rule as the state matrix.

4. National Homelessness Law Center, *One year since Grants Pass* (May 2025 update, PDF)
   Per-state status of 2024-25 statewide bills: Cicero-model bills enacted / defeated /
   pending, Missouri's struck-down Cicero statute, non-Cicero measures passed or defeated,
   the Gloria Johnson Act state list, "drug-free service zone" bills, workhouse bills,
   California AB 20 and the state Housing First mandate.

5. NLIHC, *Successfully Fighting Against State and Local Homelessness Criminalization Bills*
   State-specific bill facts with bill numbers (GA SB 62, KS HB 2430, MO HB 1606 struck down,
   MO SB 1336, IN/IA bills) and the states where criminalization bills were defeated.

6. Cornell LII: *City of Grants Pass v. Johnson* (2024, No. 23-175), 24 CFR 578.7 (what the CoC
   program regulation actually requires -- written standards; note that Part 578 never uses the
   words "Housing First": that requirement arrives through HUD's annual CoC NOFO), and
   42 U.S.C. § 11302 (the McKinney-Vento definition of "homeless individual").

7. NLIHC "Housing First" issue page -- the federal/state adoption summary (context row; the only
   *sourced* statements about Housing First adoption in this dataset).

Nothing here is scraped from behind a login. Blocked/absent sources are recorded in
research/laws.md (with the reason) and BLOCKERS.md.

Idempotency: upsert on (jurisdiction, citation, title); rerunning rewrites the same rows.
Resumability: per-task progress in `laws_progress`, so a killed run resumes where it stopped
(pass {"force": 1} to redo everything).

Run:  python3 -m ingest.domains.laws --selftest
      python3 -m ingest.run laws
"""
from __future__ import annotations

import hashlib
import html as htmllib
import os
import re
import subprocess
import sys
import tempfile

from ingest.common import (RAW, clean, db, fetch, now, save_raw, source_id_for, to_int,
                           upsert)

DOMAIN = "laws"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

TAC_PAGE = "https://www.tac.org/map_directory/{slug}/"

URL_HNH2021 = "https://homelesslaw.org/wp-content/uploads/2022/02/2021-HNH-State-Crim-Supplement.pdf"
URL_HNH2019 = "https://homelesslaw.org/wp-content/uploads/2018/10/Housing-Not-Handcuffs.pdf"
URL_HNH2025 = ("https://housingnothandcuffs.org/wp-content/uploads/2025/06/"
               "Cicero-Homeless-Criminalization-Summary-May-2025.pdf")
URL_NLIHC_FIGHT = ("https://nlihc.org/sites/default/files/"
                   "Successfully-Fighting-Against-State-and-Local-Homelessness-Criminalization-Bills.pdf")
URL_GRANTS_PASS = "https://www.law.cornell.edu/supremecourt/text/23-175"
URL_CFR_578_7 = "https://www.law.cornell.edu/cfr/text/24/578.7"
URL_USC_11302 = "https://www.law.cornell.edu/uscode/text/42/11302"
URL_NLIHC_HF = "https://nlihc.org/explore-issues/housing-programs/housing-first"
URL_NLIHC_HTF = "https://nlihc.org/housing-trust-fund-project"

T_HNH2021 = "Housing Not Handcuffs 2021: State Law Supplement"
P_HNH2021 = "National Homelessness Law Center"
T_HNH2019 = ("Housing Not Handcuffs: Ending the Criminalization of Homelessness in U.S. Cities "
             "(2019), Appendix A")
P_HNH2019 = "National Law Center on Homelessness & Poverty (now National Homelessness Law Center)"
T_HNH2025 = "One year since Grants Pass: increase in laws that make it a crime to be homeless"
P_HNH2025 = "National Homelessness Law Center / Housing Not Handcuffs"
T_NLIHC_FIGHT = ("Successfully Fighting Against State and Local Homelessness Criminalization Bills")
P_NLIHC_FIGHT = "National Low Income Housing Coalition"

LIC_TAC = ("Treatment Advocacy Center state pages; the statutory text they quote is public record; "
           "page reproduced with attribution (no explicit licence published)")
LIC_NHLC = ("Public advocacy report; statutory citations are public record. "
            "Cited and linked, not reproduced in bulk.")
LIC_LII = ("Public record (U.S. court opinion / Code of Federal Regulations / U.S. Code); "
           "Cornell LII terms permit linking")
LIC_NLIHC = "Public advocacy report; quoted with attribution, not reproduced in bulk"

# ---------------------------------------------------------------------------
# Housing Not Handcuffs 2021: the 12 statewide categories, left to right as printed.
HNH2021_COLS = [
    ("Sleeping in public -- statewide", "camping_ban"),
    ("Sleeping in public -- particular public places", "camping_ban"),
    ("Camping in public -- statewide", "camping_ban"),
    ("Camping in public -- particular public places", "camping_ban"),
    ("Sitting / lying down in public -- particular public places", "sit_lie"),
    ("Lodging, living or sleeping in a vehicle (or parking a vehicle used as lodging)", "property"),
    ("Loitering / loafing / vagrancy -- statewide", "other"),
    ("Loitering / loafing in particular public places", "other"),
    ("Standing in roadway (solicitation / pedestrian)", "panhandling"),
    ("Panhandling -- statewide", "panhandling"),
    ("Panhandling -- particular public places", "panhandling"),
    ("Panhandling -- particular ways", "panhandling"),
]
HNH2021_TOTALS = [1, 3, 4, 15, 6, 4, 16, 24, 36, 6, 24, 7]

# Housing Not Handcuffs 2019 city chart: 11 municipal categories, left to right as printed.
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

# name fragments that appear on their own line when a two-word state name wraps
CONT = {"Columbia", "Hampshire", "Mexico", "Jersey", "Carolina", "Dakota", "Island"}
PREFIX = {"New", "North", "South", "Rhode", "West", "District"}

STATE_NAMES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]

WROTE = [0]
NOTES: list[str] = []


# --------------------------------------------------------------------- plumbing
def _fetch(url: str, *, binary=False, timeout=300) -> bytes:
    """fetch via common (1 req/s, disk cache) and mirror the bytes under sha1(url).bin so
    source_id_for() can record size + sha256 for this exact URL."""
    b = fetch(url, domain=DOMAIN, headers={"User-Agent": UA, "Accept": "*/*"},
              timeout=timeout, binary=binary)
    p = RAW / DOMAIN / (hashlib.sha1(url.encode()).hexdigest() + ".bin")
    if not p.exists() or p.stat().st_size == 0:
        p.write_bytes(b)
    return b


def _sid(conn, url: str, *, title: str, publisher: str, license: str) -> int:
    return source_id_for(conn, url, DOMAIN, title=title, publisher=publisher, license=license)


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _clip(s: str | None, n=900) -> str | None:
    s = clean(s)
    if not s:
        return None
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def _law(conn, *, jurisdiction, topic, citation, title, summary, status, url, sid,
         year=None) -> bool:
    """Upsert one law row. Refuses rows without a citation or a url (provenance gate)."""
    if not (citation and url and jurisdiction and title):
        return False
    upsert(conn, "law", dict(jurisdiction=jurisdiction, topic=topic,
                             citation=_clip(clean(citation), 400),
                             title=_clip(clean(title), 400), summary=_clip(summary),
                             status=status, year=to_int(year), url=url, source_id=sid),
           keys=["jurisdiction", "citation", "title"])
    WROTE[0] += 1
    return True


def _ensure(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='laws_progress'"
                       ).fetchone()
    if row and "PRIMARY KEY" not in (row[0] or "").upper():
        # an earlier build created this without a key; ON CONFLICT would fail
        conn.execute("DROP TABLE laws_progress")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS laws_progress (
      task TEXT PRIMARY KEY, ts TEXT, ok INTEGER, note TEXT);
    """)


def _mark(conn, task, ok, note=""):
    conn.execute("INSERT INTO laws_progress(task,ts,ok,note) VALUES(?,?,?,?) "
                 "ON CONFLICT(task) DO UPDATE SET ts=excluded.ts,ok=excluded.ok,note=excluded.note",
                 (task, now(), 1 if ok else 0, _ws(note)[:900]))
    conn.commit()


def _done(conn, task) -> bool:
    r = conn.execute("SELECT ok FROM laws_progress WHERE task=?", (task,)).fetchone()
    return bool(r and r[0])


# ------------------------------------------------------------------- HTML helpers
def _flat(h: str) -> str:
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h or "", flags=re.S | re.I)
    return _ws(htmllib.unescape(re.sub(r"<[^>]+>", " ", h)))


# "ALA. CODE § 22-52-91(a)", "N.Y. MENTAL HYG. LAW § 9.27", "DEL. CODE ANN. tit. 16 § 5001(8)",
# "MASS. GEN. LAWS ANN. ch. 123 § 12(a)", "405 ILL. COMP. STAT. 5/3-600" (no section sign)
_CITE_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9.,'\u2019&/\- ]{3,70}?)\s*§+\s*(\d[0-9A-Za-z.\-:]*)")
_CITE_NOSIGN = re.compile(r"\b(\d{1,3}\s+[A-Z]{2,5}\.\s*COMP\.\s*STAT\.\s*\d[0-9A-Za-z./\-]*)")
_CITE_LEAD = re.compile(r"^(?:AND|OR|SEE|SEE ALSO|AMEND|ADD|REPEAL|ENACT|EFFECTIVE|NOTE)\s+", re.I)
SUD_WORDS = re.compile(r"substance use|substance-use|alcoholi|alcohol abuse|drug depend|"
                       r"intoxicat|chemical depend|substance abuse", re.I)


_CODE_WORDS = {"TIT", "CH", "SEC", "SUBSEC", "ANN", "CODE", "STAT", "STATS", "LAWS", "LAW",
               "COMP", "CONS", "REV", "GEN", "WELF", "CIV", "PROC", "PENAL", "HEALTH", "SAFETY",
               "HYG", "MENTAL", "INS", "EDUC", "FAM", "GOV", "JUV", "CODIFIED", "CIVIL", "ADMIN"}
_STAT_WORDS = {"STAT", "STATS", "CODE", "LAWS", "LAW", "ANN", "COMP", "REV", "CONS"}


def _is_code_token(t: str) -> bool:
    """Does this token look like part of a statute citation ('34-B', 'N.Y.', 'CODE', 'Code')?"""
    if not t:
        return False
    if re.match(r"^[0-9][0-9A-Za-z.\-:/]*$", t):        # 34-B, 43A, 5/3-600, 7504
        return True
    if re.match(r"^[A-Z0-9][A-Za-z0-9.\-:&]*\.$", t):    # abbreviations: N.Y. / FLA. / tit.
        return True
    if re.match(r"^[A-Z]{2,9}$", t) or t == "&":         # CODE / LAWS / MENTAL
        return True
    return t.upper().rstrip(".") in _CODE_WORDS           # 'Code', 'Stat.'


def _trim_code(code: str) -> str:
    """Walk back from the section sign keeping only code-shaped tokens, so a citation starts
    at the code and not at the prose before it:
      'Criteria for Inpatient Commitment FLA. STAT.' -> 'FLA. STAT.'
      'screening ... the person. Alaska Stat.'      -> 'Alaska Stat.'
      'Amend Miss. Code Ann.'                       -> 'Miss. Code Ann.'
    """
    keep: list[str] = []
    nxt_stat = False
    for raw in reversed(code.split()):
        t = raw.rstrip(",;")
        base = t.upper().rstrip(".")
        if _is_code_token(t):
            keep.append(t)
            nxt_stat = base in _STAT_WORDS
        elif re.match(r"^[A-Z][a-z]{2,12}$", t) and nxt_stat:
            # a Capitalised jurisdiction name glued to a statute word ("Alaska Stat.")
            keep.append(t)
            nxt_stat = False
        else:
            break
    return " ".join(reversed(keep[-9:])).strip(" ,;:")


def _cites(text: str) -> list[str]:
    """Statute citations exactly as printed, in document order, e.g.
    'WYO. STAT. ANN. § 25-10-110(a)' or '405 ILL. COMP. STAT. 5/3-701(a)'."""
    res: list[tuple[int, str]] = []
    for m in _CITE_RE.finditer(text or ""):
        code = _trim_code(_CITE_LEAD.sub("", m.group(1).strip()))
        if len(code) < 4 or "§" in code:
            continue
        sub = re.match(r"((?:\([0-9a-zA-Z]{1,4}\))+)", (text or "")[m.end():m.end() + 12])
        res.append((m.start(), f"{code} § {m.group(2)}{sub.group(1) if sub else ''}"))
    for m in _CITE_NOSIGN.finditer(text or ""):
        res.append((m.start(), m.group(1)))
    res.sort()
    out: list[str] = []
    for _, c in res:
        c = c.rstrip(".,;:")
        if c and c not in out:
            out.append(c)
    return out


# --------------------------------------------------------------- 1. TAC state pass
def tac_state(conn, abbr: str, name: str) -> int:
    """One state's Treatment Advocacy Center page -> up to 5 law rows."""
    slug = "district-of-columbia" if abbr == "DC" else name.lower().replace(" ", "-")
    url = TAC_PAGE.format(slug=slug)
    title = f"{name} Serious Mental Illness Resources & Helpful Info"
    before = WROTE[0]
    try:
        page = _fetch(url, timeout=180).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"{abbr}: TAC page failed ({repr(e)[:90]})")
        _mark(conn, f"tac:{abbr}", False, repr(e)[:180])
        return 0
    if "commitment" not in page.lower():
        NOTES.append(f"{abbr}: TAC page carried no treatment-law content")
        _mark(conn, f"tac:{abbr}", False, "no content")
        return 0

    sid = _sid(conn, url, title=title, publisher="Treatment Advocacy Center", license=LIC_TAC)

    # --- statute sections (accordion items). Everything after the accordion group
    # (grade sheets, TAC's recommendations, footer) is parsed separately below, so cut
    # the page at the first 'map__single-laws-list' block to keep section bodies clean.
    acc_page = page.split('map__single-laws-list')[0]
    found: dict[str, str] = {}
    for item in re.split(r'<div class="accordion__item">', acc_page)[1:]:
        m = re.search(r"<h2[^>]*>(.*?)</h2>", item, re.S)
        head = _flat(m.group(1)) if m else ""
        body = _flat(item)
        if head and body.startswith(head):
            body = body[len(head):].strip()
        hl = head.lower()
        if "emergency evaluation" in hl:
            found.setdefault("emergency", body)
        elif "inpatient" in hl and "commitment" in hl:
            # "Inpatient commitment law", "Inpatient and outpatient commitment law",
            # "Inpatient or outpatient commitment law" -- check this BEFORE 'outpatient',
            # otherwise the combined heading is swallowed by the outpatient branch.
            found.setdefault("inpatient", body)
        elif "outpatient commitment" in hl:
            found.setdefault("outpatient", body)

    specs = [
        ("emergency", "mental_health_hold", "Emergency evaluation / emergency detention statute",
         "enacted"),
        ("inpatient", "mental_health_hold", "Inpatient civil commitment statute", "enacted"),
        ("outpatient", "other", "Outpatient (assisted outpatient) commitment statute", "enacted"),
    ]
    for key, topic, rtitle, status in specs:
        body = found.get(key)
        if not body:
            NOTES.append(f"{abbr}: TAC section '{key}' missing")
            continue
        cs = _cites(body)
        if not cs:
            NOTES.append(f"{abbr}: TAC section '{key}' had no printed statute citation")
            continue
        _law(conn, jurisdiction=abbr, topic=topic, citation=cs[0], title=rtitle,
             summary=(f"{name}. Statute section as published by the Treatment Advocacy Center, "
                      f"with the state's statutory text quoted on the page: “{body[:620]}”"),
             status=status, url=url, sid=sid)

    # --- substance use as a commitment basis: only when the quoted statute names it
    for key in ("inpatient", "emergency", "outpatient"):
        body = found.get(key) or ""
        m = SUD_WORDS.search(body)
        cs = _cites(body)
        if not (m and cs):
            continue
        snippet = body[max(0, m.start() - 200): m.start() + 380]
        _law(conn, jurisdiction=abbr, topic="drug", citation=cs[0],
             title="Substance use disorder named as a basis for civil commitment",
             summary=(f"{name}: the statute text quoted on this page includes substance use — "
                      f"“…{snippet}…”"),
             status="enacted", url=url, sid=sid)
        break

    # --- TAC's own recommended amendments (status=proposed)
    i = page.find("Recommended updates to treatment laws")
    if i > 0:
        block = page[i:i + 30000]
        end = block.find("map__single-footer-boxes")
        if end > 0:
            block = block[:end]
        rec = _flat(re.sub(r"</li>", "; ", block))
        cs = _cites(rec)
        if cs:
            _law(conn, jurisdiction=abbr, topic="other",
                 citation="; ".join(cs[:4]),
                 title="Treatment Advocacy Center recommended statutory amendments",
                 summary=f"{name}. The Center's published recommendations for this state: “{rec[:700]}”",
                 status="proposed", url=url, sid=sid)
        else:
            _law(conn, jurisdiction=abbr, topic="other",
                 citation=f"TAC state page, section 'Recommended updates to treatment laws' ({abbr})",
                 title="Treatment Advocacy Center recommended statutory amendments",
                 summary=f"{name}. The Center's published recommendations for this state: “{rec[:700]}”",
                 status="proposed", url=url, sid=sid)

    n = WROTE[0] - before
    _mark(conn, f"tac:{abbr}", n > 0, f"{n} rows; sections={sorted(found)}")
    return n


# --------------------------------------------------------------------- PDF helpers
def _pdf_text(conn, url: str, name: str, *, title: str, publisher: str, license: str) -> str:
    """download (cached) + pdftotext -layout; keep the .txt next to the pdf for reuse."""
    pdf = _fetch(url, binary=True)
    p = save_raw(DOMAIN, name, pdf)
    txt_path = p.with_suffix(".txt")
    if not txt_path.exists() or txt_path.stat().st_size == 0:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            outp = tf.name
        try:
            subprocess.run(["pdftotext", "-layout", str(p), outp], check=True,
                           capture_output=True, timeout=600)
            with open(outp, "rb") as f:
                txt_path.write_bytes(f.read())
        finally:
            try:
                os.unlink(outp)
            except OSError:
                pass
    return txt_path.read_text(encoding="utf-8", errors="replace")


def _xpos(line: str) -> list[int]:
    return [m.start() for m in re.finditer(r"(?:(?<=\s)|^)[xX](?=\s|$)", line)]


def _cluster(pos: list[int], tol: int) -> list[int]:
    groups: list[list[int]] = []
    for p in sorted(pos):
        if groups and p - groups[-1][-1] <= tol:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [round(sum(g) / len(g)) for g in groups]


def _col_of(centres: list[int], p: int) -> int:
    return min(range(len(centres)), key=lambda k: abs(centres[k] - p))


# --------------------------------------- 2. HNH 2021 state criminalization matrix
def hnh2021(conn, states: dict[str, str]) -> int:
    before = WROTE[0]
    text = _pdf_text(conn, URL_HNH2021, "hnh2021-state-law-supplement.pdf",
                     title=T_HNH2021, publisher=P_HNH2021, license=LIC_NHLC)
    sid = _sid(conn, URL_HNH2021, title=T_HNH2021, publisher=P_HNH2021, license=LIC_NHLC)

    i = text.find("APPENDIX: Criminalization of Homelessness")
    if i < 0:
        NOTES.append("HNH2021: appendix heading not found")
        _mark(conn, "hnh2021", False, "no appendix")
        return 0
    block = text[i:].split("\n")
    start = next((k for k, l in enumerate(block) if l.strip().startswith("Alabama")), None)
    if start is None:
        NOTES.append("HNH2021: no data rows found")
        _mark(conn, "hnh2021", False, "no rows")
        return 0
    body = block[start:]
    centres = _cluster([p for l in body for p in _xpos(l)], tol=6)
    if len(centres) != len(HNH2021_COLS):
        NOTES.append(f"HNH2021: parsed {len(centres)} columns, expected {len(HNH2021_COLS)}")
        _mark(conn, "hnh2021", False, f"{len(centres)} columns")
        return 0

    by_name: dict[str, list[int]] = {}
    pending: tuple[str, list[int]] | None = None
    for line in body:
        marks = _xpos(line)
        head = line[: marks[0]].strip() if marks else line.strip()
        if not marks:
            if pending and head in CONT:
                by_name[(pending[0] + " " + head).replace("District of", "District")] = pending[1]
                pending = None
            elif head in STATE_NAMES:
                by_name[head] = []
            elif head in PREFIX:
                pending = (head, [])
            continue
        word = head.replace(" of", "").split()[0] if head else ""
        if pending and head in CONT:
            by_name[(pending[0] + " " + head).replace("District of", "District")] = marks
            pending = None
        elif head in STATE_NAMES:
            by_name[head] = marks
            pending = None
        elif word in PREFIX:
            pending = (head, marks)
        else:
            NOTES.append(f"HNH2021: unrecognised row {line[:40]!r}")

    name2abbr = {v: k for k, v in states.items()}
    name2abbr["District Columbia"] = "DC"
    per_col = [0] * len(HNH2021_COLS)
    plan: list[tuple[str, int, str]] = []
    for nm, marks in by_name.items():
        abbr = name2abbr.get(nm)
        if not abbr:
            continue
        for c in sorted({_col_of(centres, p) for p in marks}):
            per_col[c] += 1
            plan.append((abbr, c, nm))

    if per_col != HNH2021_TOTALS:
        # the matrix is the whole point of this source: never ship a bad parse
        NOTES.append(f"HNH2021 CHECKSUM FAILED: parsed {per_col} vs printed {HNH2021_TOTALS}; "
                     f"rows not written")
        _mark(conn, "hnh2021", False, f"checksum {per_col}")
        return 0

    for abbr, c, nm in plan:
        label, topic = HNH2021_COLS[c]
        _law(conn, jurisdiction=abbr, topic=topic,
             citation=f"HNH 2021 State Law Supplement, Appendix column {c + 1} ({label})",
             title=f"Statewide restriction: {label}",
             summary=(f"{nm}: the Law Center's 50-state + DC review of state codes marks this "
                      f"jurisdiction as having a statewide statute restricting "
                      f"{label.lower().replace('--', '—')} (appendix column {c + 1}, printed "
                      f"“x”). The report does not print the underlying section number for "
                      f"this cell; the state's code is the place to confirm the text. The "
                      f"report notes it does not claim these statutes are enforced."),
             status="enacted", year=2021, url=URL_HNH2021, sid=sid)
    rows = len(plan)
    _mark(conn, "hnh2021", rows > 0, f"{rows} rows, per-column {per_col} == printed totals")
    NOTES.append(f"HNH2021 matrix checksum OK {per_col} ({rows} rows, {len(by_name)} jurisdictions)")
    return WROTE[0] - before


# --------------------------------------------- 3. HNH 2019 municipal conduct chart
def hnh2019(conn) -> int:
    before = WROTE[0]
    text = _pdf_text(conn, URL_HNH2019, "housing-not-handcuffs-2019.pdf",
                     title="Housing Not Handcuffs: Ending the Criminalization of Homelessness "
                           "in U.S. Cities", publisher=P_HNH2019, license=LIC_NHLC)
    sid = _sid(conn, URL_HNH2019, title=T_HNH2019, publisher=P_HNH2019, license=LIC_NHLC)

    a = text.find("APPENDIX A: Prohibited Conduct Chart")
    b = text.find("APPENDIX B:", a)
    if a < 0:
        NOTES.append("HNH2019: appendix A not found")
        _mark(conn, "hnh2019", False, "no appendix")
        return 0
    block = text[a: b if b > 0 else a + 400000].split("\n")
    start = next((k for k, l in enumerate(block) if re.match(r"^\s*[A-Z]{2}\s{2,}\S", l)), None)
    if start is None:
        NOTES.append("HNH2019: no city rows found")
        _mark(conn, "hnh2019", False, "no rows")
        return 0
    body = block[start:]
    centres = _cluster([p for l in body for p in _xpos(l)], tol=4)
    if len(centres) != len(HNH2019_COLS):
        NOTES.append(f"HNH2019: parsed {len(centres)} columns, expected {len(HNH2019_COLS)}")
        _mark(conn, "hnh2019", False, f"{len(centres)} columns")
        return 0

    cities: dict[tuple[str, str], set[int]] = {}
    for line in body:
        marks = _xpos(line)
        if not marks:
            continue
        m = re.match(r"^\s*([A-Z]{2})\s{2,}(.+?)\s*$", line[: marks[0]])
        if not m:
            continue
        st, city = m.group(1), _ws(m.group(2))
        if not city or len(city) > 40 or st in ("To", "St"):
            continue
        cities[(st, city)] = {_col_of(centres, p) for p in marks}

    per_col = [0] * len(HNH2019_COLS)
    plan: list[tuple[str, str, int]] = []
    for (st, city), cols in cities.items():
        for c in sorted(cols):
            per_col[c] += 1
            plan.append((st, city, c))

    if per_col != HNH2019_TOTALS:
        NOTES.append(f"HNH2019 CHECKSUM FAILED: parsed {per_col} vs printed {HNH2019_TOTALS}; "
                     f"rows not written")
        _mark(conn, "hnh2019", False, f"checksum {per_col}")
        return 0

    for st, city, c in plan:
        label, topic = HNH2019_COLS[c]
        _law(conn, jurisdiction=f"{city}, {st}", topic=topic,
             citation=f"HNH 2019 Prohibited Conduct Chart, column {c + 1} ({label})",
             title=f"Municipal ordinance: {label}",
             summary=(f"{city}, {st}: the Law Center's survey of municipal codes marks this "
                      f"city as having an ordinance restricting "
                      f"{label.lower().replace('--', '—')} (2016 chart reproduced in the 2019 "
                      f"report, column {c + 1}). City code section is not printed by the "
                      f"source; the chart states that enforcement varies widely."),
             status="enacted", year=2016, url=URL_HNH2019, sid=sid)
    rows = len(plan)
    _mark(conn, "hnh2019", rows > 0,
          f"{rows} rows over {len(cities)} cities, per-column {per_col} == printed totals")
    NOTES.append(f"HNH2019 chart checksum OK {per_col} ({rows} rows over {len(cities)} cities)")
    return WROTE[0] - before


# ---------------------------------------- 4. HNH 2025 (one year since Grants Pass)
def _names_in(frag: str, name2abbr: dict[str, str]) -> list[str]:
    out = []
    for part in re.split(r",\s*|\s+and\s+", frag):
        p = _ws(part).strip(" .;:“”")
        p = re.sub(r"\s+", " ", p)
        if p in name2abbr:
            out.append(p)
    return out


def hnh2025(conn, states: dict[str, str]) -> int:
    before = WROTE[0]
    text = _pdf_text(conn, URL_HNH2025, "hnh-2025-grants-pass-update.pdf",
                     title=T_HNH2025, publisher=P_HNH2025, license=LIC_NHLC)
    sid = _sid(conn, URL_HNH2025, title=T_HNH2025, publisher=P_HNH2025, license=LIC_NHLC)
    flat = _ws(text)

    name2abbr = {v: k for k, v in states.items()}
    name2abbr["Washington State"] = name2abbr.get("Washington")
    name2abbr["North Carolina"] = "NC"

    specs = [
        # (regex over the flattened report, status, topic, what)
        (r"pushed through harmful, backwards cookie-cutter bills that make homelessness worse in "
         r"([^.]+)\.", "enacted", "encampment_clearance",
         "Cicero-model statewide anti-homeless bill reported enacted since 2021"),
        (r"This year, advocates defeated bills in ([^.]+)\.", "proposed", "encampment_clearance",
         "statewide anti-homeless bill reported defeated"),
        (r"These non-Cicero measures criminalizing homelessness have passed in ([^.]+)\.",
         "enacted", "encampment_clearance",
         "non-Cicero statewide anti-homeless measure reported passed"),
        (r"Similar measures were defeated in ([^.]+)\.", "proposed", "encampment_clearance",
         "statewide anti-homeless measure reported defeated"),
        (r"anti-criminalization legislation, many based on the Law Center[’']s template Gloria "
         r"Johnson Act, has been introduced in 11 states: ([^.]+)\.", "proposed", "other",
         "Gloria Johnson Act-style anti-criminalization bill introduced"),
        (r"It has advanced through committee in ([^.]+)\.", "proposed", "other",
         "Gloria Johnson Act-style bill reported advanced through committee"),
        (r"Bills in ([^.]+?) create “drug-free service zones”", "proposed", "drug",
         "bill creating “drug-free service zones” with added penalties for service providers"),
        (r"Bills creating “workhouses” for homeless individuals where they are required to work as "
         r"punishment for not being able to afford rent, like in ([^.]+)", None, "other",
         "bill creating “workhouses” as a punishment for being unable to afford rent"),
        (r"Bills banning homeless services in entire communities or the creation of large homeless "
         r"exclusion zones around certain facilities, like in ([^.]+)", None, "other",
         "bill banning homeless services or creating homelessness exclusion zones"),
        (r"Standalone anti-camping legislation, like in ([^.]+)", None, "camping_ban",
         "standalone anti-camping bill"),
    ]
    rows = 0
    for pat, status, topic, what in specs:
        m = re.search(pat, flat, re.I)
        if not m:
            NOTES.append(f"HNH2025: pattern not found -> {what}")
            continue
        quote = _ws(m.group(0))
        for nm in _names_in(m.group(1), name2abbr):
            abbr = name2abbr[nm]
            _law(conn, jurisdiction=abbr, topic=topic,
                 citation=f"HNH 2025 Grants Pass update — {what}",
                 title=f"2024-25 statewide legislation: {what}",
                 summary=(f"{nm}: the report states: “{quote}”. The report does not print the "
                          f"bill number for this item; status is as reported by the source and "
                          f"was not independently re-checked at the state legislature."),
                 status=status, year=2025, url=URL_HNH2025, sid=sid)
            rows += 1

    one_offs = [
        (r"Missouri[’']s Cicero law was struck down by the state Supreme Court\.", "MO",
         "encampment_clearance", "enjoined",
         "Cicero-model statute struck down by the state Supreme Court"),
        (r"Cicero bills in North\s*Carolina are still pending", "NC", "encampment_clearance",
         "proposed", "Cicero-model bill still pending"),
        (r"One is pending in Pennsylvania\.", "PA", "encampment_clearance", "proposed",
         "anti-homeless measure pending"),
    ]
    for pat, abbr, topic, status, what in one_offs:
        m = re.search(pat, flat, re.I)
        if not m:
            NOTES.append(f"HNH2025: one-off not found -> {what}")
            continue
        _law(conn, jurisdiction=abbr, topic=topic,
             citation=f"HNH 2025 Grants Pass update — {what}", title=what.capitalize(),
             summary=(f"The report states: “{_ws(m.group(0))}”. No bill or docket number is "
                      f"printed by the source."),
             status=status, year=2025, url=URL_HNH2025, sid=sid)
        rows += 1

    # California: the state Housing First mandate, and the defeated repeal bill (AB 20)
    m = re.search(r"advocates defeated a standalone bill attempting to make it a crime to sleep "
                  r"outside and repeal the state[’']s Housing First mandate in the state legislature "
                  r"\(CA AB 20\)", flat, re.I)
    if m:
        quote = f"The report states: “{_ws(m.group(0))}”."
        _law(conn, jurisdiction="CA", topic="housing_first",
             citation="CA Housing First mandate — repeal bill AB 20 (2025) defeated",
             title="Housing First mandate in state law, with the 2025 repeal attempt defeated",
             summary=(quote + " The sentence records both that California has a Housing First "
                      "mandate in state law and that AB 20 would have repealed it; the enabling "
                      "section is not named by the source."),
             status="enacted", year=2025, url=URL_HNH2025, sid=sid)
        _law(conn, jurisdiction="CA", topic="housing_first",
             citation="CA AB 20 (2025) — bill to repeal the Housing First mandate",
             title="Bill to repeal the Housing First mandate and criminalize sleeping outside "
                   "(defeated)",
             summary=quote, status="proposed", year=2025, url=URL_HNH2025, sid=sid)
        rows += 2
    else:
        NOTES.append("HNH2025: CA AB 20 sentence not found")

    # federal: Housing Not Handcuffs Act introduced in Congress
    m = re.search(r"a similar bill named the Housing Not Handcuffs Act was introduced into "
                  r"Congress on ([A-Z][a-z]+ \d{1,2}(?:st|nd|rd|th)?)", flat)
    if m:
        _law(conn, jurisdiction="US", topic="other",
             citation="Housing Not Handcuffs Act (bill introduced in Congress, "
                      f"{_ws(m.group(1))})",
             title="Anti-criminalization bill introduced in Congress to mark the Grants Pass "
                   "anniversary",
             summary=(f"The report states: “{_ws(m.group(0))}”. The report prints no bill number."),
             status="proposed", year=2025, url=URL_HNH2025, sid=sid)
        rows += 1

    _mark(conn, "hnh2025", rows > 0, f"{rows} rows")
    return WROTE[0] - before


# ------------------------------- 5. NLIHC brief: state criminalization bill fight-back
def nlihc_fight(conn, states: dict[str, str]) -> int:
    before = WROTE[0]
    text = _pdf_text(conn, URL_NLIHC_FIGHT, "nlihc-fighting-criminalization-bills.pdf",
                     title=T_NLIHC_FIGHT, publisher=P_NLIHC_FIGHT, license=LIC_NLIHC)
    sid = _sid(conn, URL_NLIHC_FIGHT, title=T_NLIHC_FIGHT, publisher=P_NLIHC_FIGHT,
               license=LIC_NLIHC)
    flat = _ws(text)
    name2abbr = {v: k for k, v in states.items()}

    # states where criminalization measures were introduced and failed
    m = re.search(r"policy measures to criminalize homelessness have failed in ([^.]+?) including "
                  r"([^.]+)\.", flat, re.I)
    rows = 0
    if m:
        quote = _ws(m.group(0))
        for nm in _names_in(m.group(2), name2abbr):
            _law(conn, jurisdiction=name2abbr[nm], topic="encampment_clearance",
                 citation="NLIHC, Successfully Fighting... Criminalization Bills — state where a "
                          "criminalization bill was introduced and failed",
                 title="Statewide criminalization bill introduced and not passed",
                 summary=(f"{nm}: “{quote}”"),
                 status="proposed", url=URL_NLIHC_FIGHT, sid=sid)
            rows += 1
    else:
        NOTES.append("NLIHC brief: 'failed in ... states' sentence not found")

    # four named bills with numbers
    bills = [
        (r"Georgia advocates[^.]*?ensured that (SB 62) was stripped of its most harmful "
         r"provisions before it was enacted", "GA", "encampment_clearance", "enacted", 2024),
        (r"testify against (HB 2430) at a hearing held by the House Welfare Reform Committee",
         "KS", "encampment_clearance", "proposed", 2024),
        (r"Missouri lawmakers passed (HB 1606) in 2022, which banned camping", "MO",
         "camping_ban", "enjoined", 2022),
        (r"a similar bill, (SB 1336), was introduced in the Missouri legislature in 2024", "MO",
         "encampment_clearance", "proposed", 2024),
    ]
    for pat, abbr, topic, status, year in bills:
        m = re.search(pat, flat, re.I)
        if not m:
            NOTES.append(f"NLIHC brief: bill sentence not found for {abbr} ({pat[:40]})")
            continue
        bill = m.group(1)
        # widen the quote to the sentence containing the bill
        i = flat.find(m.group(0))
        head = flat.rfind(". ", 0, i)
        tail = flat.find(". ", i + len(m.group(0)))
        quote = _ws(flat[head + 2 if head > 0 else 0: tail + 1 if tail > 0 else len(flat)])
        extra = ""
        if abbr == "MO" and bill == "HB 1606":
            mm = re.search(r"was unanimously struck down by the State Supreme Court", flat)
            if mm:
                extra = (" The brief also reports: “the bill was later challenged on procedural "
                         "grounds and was unanimously struck down by the State Supreme Court.”")
        _law(conn, jurisdiction=abbr, topic=topic, citation=f"{abbr} {bill}",
             title=f"Statewide bill {bill}: {('camping ban' if topic == 'camping_ban' else 'criminalization / encampment measure')}",
             summary=(f"{abbr} {bill}. The brief states: “{quote}”.{extra}"),
             status=status, year=year, url=URL_NLIHC_FIGHT, sid=sid)
        rows += 1

    # Indiana and Iowa bills introduced, no bill numbers printed
    m = re.search(r"Both (Indiana) and (Iowa) had bills introduced in their current legislative "
                  r"sessions, neither of which seems to be moving forward", flat, re.I)
    if m:
        for abbr in ("IN", "IA"):
            _law(conn, jurisdiction=abbr, topic="encampment_clearance",
                 citation=f"NLIHC brief — statewide bill introduced in {abbr} (no number printed)",
                 title="Statewide criminalization bill introduced, not advancing",
                 summary=f"The brief states: “{_ws(m.group(0))}”.",
                 status="proposed", year=2024, url=URL_NLIHC_FIGHT, sid=sid)
            rows += 1
    else:
        NOTES.append("NLIHC brief: Indiana/Iowa sentence not found")

    _mark(conn, "nlihc_fight", rows > 0, f"{rows} rows")
    return WROTE[0] - before


# ----------------------------------------------------------------- 6. federal rows
def federal(conn) -> int:
    before = WROTE[0]

    # --- City of Grants Pass v. Johnson (2024)
    try:
        page = _fetch(URL_GRANTS_PASS, timeout=180).decode("utf-8", "replace")
        title = "City of Grants Pass v. Johnson, 603 U.S. ___ (2024)"
        sid = _sid(conn, URL_GRANTS_PASS, title=title,
                   publisher="Supreme Court of the United States (via Cornell LII)", license=LIC_LII)
        txt = _flat(page)
        cite = None
        m = re.search(r"No\.?\s*23[\u2013\-]175\.?\s*Argued ([A-Z][a-z]+ \d{1,2}, \d{4})[\u2014\-]+Decided "
                      r"([A-Z][a-z]+ \d{1,2}, \d{4})", txt)
        argued = decided = None
        if m:
            argued, decided = _ws(m.group(1)), _ws(m.group(2))
        if decided:
            cite = f"City of Grants Pass v. Johnson, No. 23-175 (U.S., decided {decided})"
        else:
            cite = "City of Grants Pass v. Johnson, No. 23-175 (U.S.)"
        hold = ""
        mh = re.search(r"(The (?:Eighth Amendment|Constitution)[^.]{40,420}\.)", txt)
        if mh:
            hold = _ws(mh.group(1))
        _law(conn, jurisdiction="US", topic="camping_ban", citation=cite,
             title="Outdoor-sleeping / camping ordinances are not barred by the Eighth Amendment",
             summary=("Supreme Court of the United States, No. 23-175"
                      + (f", argued {argued}" if argued else "")
                      + (f", decided {decided}" if decided else "")
                      + ". " + (f"Opinion text: “{hold}” " if hold else "")
                      + "Effect: the Court reversed the Ninth Circuit and rejected the rule that "
                        "punishing people who sleep outdoors when no shelter is available is cruel "
                        "and unusual punishment, so state and municipal camping bans are "
                        "enforceable on federal Eighth Amendment grounds after this decision."),
             status="enacted", year=to_int((decided or "").split()[-1]), url=URL_GRANTS_PASS, sid=sid)

        mb = re.search(r"Martin v\. Boise,?\s*920\s*F\.\s*3d\s*584[^)]{0,40}", txt)
        if mb:
            _law(conn, jurisdiction="US", topic="camping_ban",
                 citation=_ws(mb.group(0)).rstrip(",;"),
                 title="Punishing sleeping outdoors without available shelter (Ninth Circuit rule, "
                       "abrogated)",
                 summary=("The Ninth Circuit rule addressed in City of Grants Pass v. Johnson "
                          "(2024): punishing a person for sleeping outdoors on public property when "
                          "no indoor alternative is available violated the Eighth Amendment, "
                          "binding in the nine western states from 2018 until the Supreme Court "
                          "rejected that approach in 2024. Citation and holding read from the "
                          "Supreme Court's opinion page; the circuit opinion itself was not "
                          "fetched (see research/laws.md)."),
                 status="repealed", year=2019, url=URL_GRANTS_PASS, sid=sid)
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"Grants Pass fetch/parse failed: {repr(e)[:120]}")

    # --- 24 CFR 578.7: what the CoC-program regulation actually says about written standards
    #     (note: Part 578 does not use the words "Housing First"; that requirement arrives via
    #     HUD's annual CoC NOFO, which is not reproduced here -- see research/laws.md)
    try:
        page = _fetch(URL_CFR_578_7, timeout=180).decode("utf-8", "replace")
        title = "24 CFR 578.7 -- Responsibilities of the continuum of care"
        sid = _sid(conn, URL_CFR_578_7, title=title,
                   publisher="Code of Federal Regulations (via Cornell LII)", license=LIC_LII)
        txt = _flat(page)
        m = re.search(r"(\(9\) In consultation with recipients of Emergency Solutions Grants[^.]{0,400}\.)", txt)
        ws_text = _ws(m.group(1)) if m else ""
        credit = re.search(r"\[\s*(\d+)\s*FR\s*\d+\s*,\s*([A-Z][a-z]+ \d{1,2}, \d{4})[^\]]*\]", txt)
        _law(conn, jurisdiction="US", topic="other", citation="24 C.F.R. § 578.7",
             title="Continuum of Care responsibilities, including written standards for assistance",
             summary=("Federal rule for HUD Continuum of Care program governance: the CoC must adopt "
                      "and follow written standards for providing CoC assistance, and has the other "
                      "operating duties listed at § 578.7(a). "
                      + (f"Regulatory text as published: “{ws_text}”" if ws_text else "")
                      + " The regulation's own text does not use the phrase 'Housing First'. "
                      + (f"Source credit on the page: “{_ws(credit.group(0))}”." if credit else "")),
             status="enacted", year=to_int(credit.group(2).split()[-1]) if credit else None,
             url=URL_CFR_578_7, sid=sid)
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"24 CFR 578.7 fetch/parse failed: {repr(e)[:120]}")

    # --- 42 U.S.C. § 11302: who counts as homeless (the statutory definition programs use)
    try:
        page = _fetch(URL_USC_11302, timeout=180).decode("utf-8", "replace")
        title = "42 U.S.C. § 11302 -- General definition of homeless individual"
        sid = _sid(conn, URL_USC_11302, title=title,
                   publisher="U.S. Code (via Cornell LII)", license=LIC_LII)
        txt = _flat(page)
        m = re.search(r"(\(1\) an individual or family who lacks a fixed, regular, and adequate "
                      r"nighttime residence)", txt)
        body = _ws(m.group(1)) if m else ""
        credit = re.search(r"\(Pub\. L\. [^)]{0,160}?(\d{4})\)", txt)
        _law(conn, jurisdiction="US", topic="other", citation="42 U.S.C. § 11302",
             title="Statutory definition of a homeless individual (McKinney-Vento Act)",
             summary=("The definition of 'homeless individual' used by HUD, HHS and other federal "
                      "homelessness programs: “" + (body or "(text not parsed)") + "”. "
                      + (f"Source credit on the page: “{_ws(credit.group(0))}”." if credit else
                         "The LII page carried no source-credit line at fetch time.")),
             status="enacted", year=to_int(credit.group(1)) if credit else None,
             url=URL_USC_11302, sid=sid)
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"42 USC 11302 fetch/parse failed: {repr(e)[:120]}")

    # --- NLIHC Housing First page (context: who adopts Housing First, and the pushback)
    try:
        page = _fetch(URL_NLIHC_HF, timeout=180).decode("utf-8", "replace")
        title = "Housing First"
        sid = _sid(conn, URL_NLIHC_HF, title=title, publisher="National Low Income Housing Coalition",
                   license=LIC_NLIHC)
        txt = _flat(page)
        m = re.search(r"(The Housing First model is one of the best strategies[^.]{0,300}\.)", txt)
        q = _ws(m.group(1)) if m else ""
        m2 = re.search(r"(Many states|It has also been adopted by[^.]{0,200}\.)", txt)
        q2 = _ws(m2.group(0)) if m2 else ""
        _law(conn, jurisdiction="US", topic="housing_first",
             citation="NLIHC issue page, 'Housing First' (policy summary)",
             title="Housing First: federal adoption and state/local uptake (advocacy summary)",
             summary=("NLIHC's summary of the Housing First model and its adoption by federal "
                      "agencies and state/local governments. "
                      + (f"“{q}” " if q else "") + (f"“{q2}”" if q2 else "")
                      + " This row is context for the statutory rows in this domain, not a statute."),
             status=None, url=URL_NLIHC_HF, sid=sid)
    except Exception as e:  # noqa: BLE001
        NOTES.append(f"NLIHC Housing First page failed: {repr(e)[:120]}")

    _mark(conn, "federal", WROTE[0] > before, f"{WROTE[0] - before} rows")
    return WROTE[0] - before


# --------------------------------- 7. NLIHC Housing Trust Fund Project: funding statutes
def _html_lines(page: str) -> list[str]:
    """Readable lines (paragraph-aware) from an HTML page."""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page or "", flags=re.S | re.I)
    s = re.sub(r"</(p|li|h[1-6]|div)>|<br\s*/?>", "\n", s, flags=re.I)
    s = htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
    out = []
    for line in s.split("\n"):
        line = _ws(line)
        if line:
            out.append(line)
    return out


_BILL_RE = re.compile(
    r"\b(?:[A-Z]{2,6}\.?\s?R\.?S\.?|RCW|H\.?B\.?|S\.?B\.?|Act No\.?|Public Law|P\.L\.|Proposition|"
    r"Section|Art\.|Article)\s*[0-9][0-9A-Za-z.\-]*|\b(?:H|S)\s?\d{3,4}\b|\bAct\s+\d{1,3}\b")
_ACT_NAME_RE = re.compile(r"(the [A-Z][A-Za-z’' ]{4,70}?Act(?: of \d{4}| in \d{4})?)")


def nlihc_htf(conn, states: dict[str, str]) -> int:
    """NLIHC's Housing Trust Fund Project page -> one `funding` row per state it describes.

    The page walks through the three kinds of state legislation that support local housing
    trust funds (enabling legislation, an enabled revenue source, direct state funds), state by
    state, and prints the statutes involved. Only the states the page actually writes a
    paragraph for get a row.
    """
    before = WROTE[0]
    page = _fetch(URL_NLIHC_HTF, timeout=180).decode("utf-8", "replace")
    title = "State & Local Housing Trust Fund Project"
    sid = _sid(conn, URL_NLIHC_HTF, title=title, publisher="National Low Income Housing Coalition",
               license=LIC_NLIHC)
    lines = _html_lines(page)
    name2abbr = {v: k for k, v in states.items()}

    SECTIONS = {
        "State Enabling Legislation for Local Housing Trust Funds":
            ("funding", "State legislation enabling local housing trust funds"),
        "Enabling a Revenue Source Option for Local Housing Trust Funds":
            ("funding", "State legislation enabling a dedicated revenue source for local housing "
                         "trust funds"),
        "State Funds to Support Local Housing Trust Funds":
            ("funding", "State fund supporting local housing trust funds"),
    }
    rows = 0
    section = None
    cur_state = None
    para: list[str] = []

    def flush(st_name, st_abbr, text, sec_label, sec_title):
        nonlocal rows
        body = _ws(" ".join(text))
        if not body:
            return
        cites = _BILL_RE.findall(body)
        names = _ACT_NAME_RE.findall(body)
        if cites:
            citation = f"{st_abbr}: {'; '.join(dict.fromkeys(cites))[:160]}"
        elif names:
            citation = f"{st_abbr}: {names[0]}"
        else:
            citation = f"NLIHC Housing Trust Fund Project, section '{st_name}'"
        # the enactment year: only years that sit within 60 characters of an enactment verb,
        # and only when the paragraph points at a single one (otherwise leave it None rather
        # than guess which of several acts in the paragraph the row is about)
        cand = set()
        for ym in re.finditer(r"\b(19[5-9]\d|20[0-4]\d)\b", body):
            win = body[max(0, ym.start() - 60): ym.end() + 60]
            if re.search(r"\b(?:enact|pass|create|establish|adopt|amend|authoriz|approv)\w*", win, re.I) \
                    and not re.search(r"(?:as of|As of|by|since)\s*$",
                                      body[max(0, ym.start() - 12): ym.start()]):
                cand.add(int(ym.group(1)))
        years = [cand.pop()] if len(cand) == 1 else []
        _law(conn, jurisdiction=st_abbr, topic="funding", citation=citation,
             title=sec_title,
             summary=(f"{st_name}: “{body[:700]}”"),
             status="enacted", year=max(years) if years else None, url=URL_NLIHC_HTF, sid=sid)
        rows += 1

    for line in lines:
        if line in SECTIONS:
            if cur_state and section:
                flush(cur_state[0], cur_state[1], para, section[0], section[1])
            section = SECTIONS[line]
            cur_state, para = None, []
            continue
        if section is None:
            continue
        if line in name2abbr:
            if cur_state:
                flush(cur_state[0], cur_state[1], para, section[0], section[1])
            cur_state, para = (line, name2abbr[line]), []
        elif line.startswith(("City Housing Trust Funds", "County Housing Trust Funds",
                              "Regional Housing Trust Funds", "State Housing Trust Funds",
                              "Best Practice", "Definitions", "State & Local Housing Trust Fund")):
            if cur_state:
                flush(cur_state[0], cur_state[1], para, section[0], section[1])
            section, cur_state, para = None, None, []
        elif cur_state:
            para.append(line)
    if cur_state and section:
        flush(cur_state[0], cur_state[1], para, section[0], section[1])

    _mark(conn, "nlihc_htf", rows > 0, f"{rows} funding rows")
    return WROTE[0] - before


# ---------------------------------------------------------------------------- run
def _cleanup_stale(conn) -> None:
    """Drop rows an earlier, rejected build of this same ingestor left behind.

    That build wrote HNH-2021 rows with the citation prefix 'HNH 2021 State Law Supplement,
    App. col. N' and a source_id that no longer resolves (the source table was rebuilt).
    The current build cites the same cells as '... Appendix column N (...)', so those exact
    old-format rows are stale duplicates with dead provenance -- delete only them.
    """
    n = conn.execute(
        "DELETE FROM law WHERE citation LIKE 'HNH 2021 State Law Supplement, App. col.%'").rowcount
    if n:
        NOTES.append(f"removed {n} stale rows left by the earlier rejected build")
    conn.commit()


def _states(conn) -> dict[str, str]:
    rows = conn.execute("SELECT abbr, name FROM geo_state ORDER BY abbr").fetchall()
    skip = {"AS", "GU", "MP", "PR", "VI"}
    return {r[0]: r[1] for r in rows if r[0] not in skip}


def run(conn, cfg=None, *, probe=False) -> dict:
    cfg = cfg or {}
    force = bool(cfg.get("force"))
    _ensure(conn)
    WROTE[0] = 0
    NOTES.clear()
    _cleanup_stale(conn)
    states = _states(conn)
    if len(states) < 50:
        NOTES.append(f"geo_state has only {len(states)} jurisdictions -- run `places` first")

    def step(task, fn, *a):
        if not force and _done(conn, task):
            if probe:
                print(f"  = {task} already done (skipping)", file=sys.stderr)
            return 0
        n = fn(conn, *a)
        conn.commit()
        return n

    n2021 = step("hnh2021", hnh2021, states)
    n2019 = step("hnh2019", hnh2019)
    n2025 = step("hnh2025", hnh2025, states)
    nfight = step("nlihc_fight", nlihc_fight, states)
    nhtf = step("nlihc_htf", nlihc_htf, states)
    nfed = step("federal", federal)

    n_states = 0
    for abbr in sorted(states):
        if not force and _done(conn, f"tac:{abbr}"):
            continue
        k = tac_state(conn, abbr, states[abbr])
        n_states += k
        conn.commit()
        if probe:
            print(f"  {abbr} {states[abbr]:<20} {k} rows", file=sys.stderr)

    conn.commit()
    rows = conn.execute("SELECT COUNT(*) FROM law").fetchone()[0]
    jurs = conn.execute("SELECT COUNT(DISTINCT jurisdiction) FROM law").fetchone()[0]
    notes = (f"matrix2021={n2021} city_chart2019={n2019} hnh2025={n2025} nlihc_brief={nfight} "
             f"nlihc_htf={nhtf} federal={nfed} state_pages={n_states} | total law rows={rows} "
             f"over {jurs} jurisdictions | " + "; ".join(NOTES[-5:]))
    return {"rows": rows, "notes": notes,
            "sources": [(URL_HNH2021, LIC_NHLC), (URL_HNH2019, LIC_NHLC), (URL_HNH2025, LIC_NHLC),
                        (URL_NLIHC_FIGHT, LIC_NLIHC), (URL_NLIHC_HTF, LIC_NLIHC),
                        (URL_GRANTS_PASS, LIC_LII),
                        (URL_CFR_578_7, LIC_LII), (URL_USC_11302, LIC_LII),
                        (URL_NLIHC_HF, LIC_NLIHC)]}


def report() -> None:
    conn = db()
    print("law rows:", conn.execute("SELECT COUNT(*) FROM law").fetchone()[0])
    print("jurisdictions:", conn.execute("SELECT COUNT(DISTINCT jurisdiction) FROM law").fetchone()[0])
    print("\nby topic:")
    for r in conn.execute("SELECT topic, COUNT(*) n, COUNT(DISTINCT jurisdiction) j FROM law "
                          "GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r['topic']:<20} {r['n']:>5} rows  {r['j']:>4} jurisdictions")
    print("\nstates/DC with rows and distinct topics:")
    for r in conn.execute("SELECT jurisdiction, COUNT(*) n, COUNT(DISTINCT topic) t FROM law "
                          "WHERE LENGTH(jurisdiction)=2 GROUP BY 1 ORDER BY 1"):
        print(f"  {r['jurisdiction']}  rows={r['n']:>2}  topics={r['t']}")
    print("\njurisdictions with >=5 distinct topics:",
          conn.execute("SELECT COUNT(*) FROM (SELECT jurisdiction FROM law GROUP BY 1 "
                       "HAVING COUNT(DISTINCT topic)>=5)").fetchone()[0])
    print("jurisdictions with >=5 rows:",
          conn.execute("SELECT COUNT(*) FROM (SELECT jurisdiction FROM law GROUP BY 1 "
                       "HAVING COUNT(*)>=5)").fetchone()[0])
    print("\n3 random rows:")
    for r in conn.execute("SELECT * FROM law ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        report()
    elif "--probe" in sys.argv:
        run(db(), probe=True)
    else:
        res = run(db())
        print(res["rows"], res["notes"])
