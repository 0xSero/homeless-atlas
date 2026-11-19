"""canada -- Canada: Everyone Counts point-in-time counts, census shelter costs, federal money.

Three publishers, three readers, nothing merged across them:

  National   Housing, Infrastructure and Communities Canada (HICC), "Everyone Counts" --
             the nationally coordinated Point-in-Time (PiT) Counts.  One reader per report
             vintage: 2016 highlights, 2018 highlights, 2020-2022 results, 2024 highlights
             part 1 (PDF, which also carries Annex A: the 74 participating communities) and
             2025 results.  Stores the published national enumeration per cycle.

  Community  Homelessness Services Association of BC / BC Housing / Ministry of Housing --
             "Report on Homeless Counts in B.C." for the 2020-21, 2023 and 2024-25 cycles
             (the 2020-21 report also prints the 2018 per-community total).  These are the
             only per-community PiT results published as a *table* by any Canadian
             jurisdiction (see research/canada.md); 34 British Columbia communities with up
             to four cycles.  Everything else is published community-by-community as PDFs.

  Census     Statistics Canada table 98-10-0252 (2021 Census), "Shelter-cost-to-income
             ratio by tenure: Canada, provinces and territories, census metropolitan areas
             and census agglomerations".  Streamed out of the published CSV inside the
             48 MB zip; only the Canada and province rows are kept.

Storage (everything in the shared generic tables, quarantined from the US rows):

  geo_city   kind='ca_city', geoid='CA-<prov>-<slug>', state=<province name>,
             pop/lat/lon NULL (no published figure in any source fetched here)
  metric     entity_type='ca_city'  entity_id=<geoid>      PIT counts per community
             entity_type='canada'   entity_id='Canada'     national PIT counts,
                                                            census households/shelter cost,
                                                            and one Reaching Home funding figure
             entity_type='canada'   entity_id='Canada:<Province>'  provincial census rows

  Metrics written (no modelled values; every number is printed in a fetched document):
    pit_total / pit_sheltered / pit_unsheltered / pit_transitional   people on count night
    pit_encampments / pit_institutional                              sub-lines the reports print
    pit_total_panel72                                                the 72-community panel series
    households_total_2021census
    households_paying_30pct_plus_shelter_cost
    share_paying_30pct_plus_shelter_cost
    reaching_home_funding                                           federal programme envelope

Notes on the host: housing-infrastructure.canada.ca serves an incomplete TLS chain (it omits
the "Entrust OV TLS Issuing RSA CA 2" intermediate), so every request to it fails
CERTIFICATE_VERIFY_FAILED with a stock CA bundle.  `_gc_ca_bundle()` builds a bundle of the
certifi roots plus that intermediate (fetched from the CA's own http CRL host) into
data/raw/canada/ and points REQUESTS_CA_BUNDLE at it for this process only.

1 request/s (ingest.common.fetch), every response cached under data/raw/canada/, every row
carries the source_id of the exact URL it came from.  Idempotent: replays upsert on
(geoid) / (entity_type, entity_id, metric, year).
"""
from __future__ import annotations

import csv
import html as htmllib
import io
import json
import os
import re
import ssl
import subprocess
import tempfile
import zipfile
from pathlib import Path

from ingest.common import (RAW, clean, db, fetch, save_raw, source_id_for, to_int, upsert)

DOMAIN = "canada"

HICC = "Housing, Infrastructure and Communities Canada"
HSABC = "Homelessness Services Association of BC (with BC Housing and the Ministry of Housing)"
STATCAN = "Statistics Canada"

OGL_CA = "Open Government Licence - Canada (https://open.canada.ca/en/open-government-licence-canada)"
# hsabc.ca / bchousing.org carry no explicit licence; the reports are public provincial
# statistics.  Recorded as such rather than guessed at.
PUBLIC_BC = "Public provincial statistics report (no explicit licence stated on the publisher's site)"

GC = "https://housing-infrastructure.canada.ca"

# --- HICC "Everyone Counts" report URLs (all verified 200 on 2026-11-19) ----------------
EC2016 = f"{GC}/homelessness-sans-abri/reports-rapports/pit-counts-dp-eng.html"
EC2016_T = "Highlights - 2016 Coordinated Point-in-Time Count of Homelessness in Canadian Communities"
EC2018 = f"{GC}/homelessness-sans-abri/reports-rapports/pit-counts-dp-2018-highlights-eng.html"
EC2018_T = "Everyone Counts 2018: Highlights"
EC2022 = f"{GC}/homelessness-sans-abri/reports-rapports/pit-counts-dp-2020-2022-results-resultats-eng.html"
EC2022_T = ("Everyone Counts 2020-2022: Results from the Third Nationally Coordinated "
            "Point-in-Time Counts of Homelessness in Canada")
EC2024 = f"{GC}/homelessness-sans-abri/reports-rapports/pit-counts-dp-2024-highlights-p1-eng.html"
EC2024_T = "Everyone Counts 2024 - Highlights Report Part 1 - Enumeration of Homelessness"
EC2024_PDF = (f"{GC}/alt-format/pdf/homelessness-sans-abri/reports-rapports/"
              "pit-counts-dp-2024-highlights-p1-en.pdf")
EC2025 = f"{GC}/homelessness-sans-abri/reports-rapports/everyone-counts-2025-tout-monde-compte-eng.html"
EC2025_T = "Everyone Counts 2025: Results from the Annual Enumeration of Homelessness"
RH_PROG = f"{GC}/homelessness-sans-abri/prog-info-eng.html"
RH_PROG_T = "Reaching Home program information"

# --- HSABC / BC provincial count reports (all verified 200 on 2026-11-19) ---------------
BC2021 = "https://hsa-bc.ca/_Library/2020_HC/2020-21-BC-Homeless-Counts.pdf"
BC2021_T = "2020/21 Report on Homeless Counts in B.C."
BC2023 = "https://hsa-bc.ca/_Library/2023_HC/2023-BC-Homeless-Counts.pdf"
BC2023_T = "2023 Report on Homeless Counts in B.C."
BC2025 = "https://hsa-bc.ca/_Library/2024_25_HC/2024-2025_Report_on_Homeless_Counts_in_BC.pdf"
BC2025_T = "2024-2025 Report on Homeless Counts in B.C."

# --- Statistics Canada table 98-10-0252 (2021 Census) -----------------------------------
STC255_URL = "https://www150.statcan.gc.ca/n1/tbl/csv/98100252-eng.zip"
STC255_T = ("Shelter-cost-to-income ratio by tenure: Canada, provinces and territories, "
            "census metropolitan areas and census agglomerations (table 98-10-0252-01)")

PROVINCES = {
    "Newfoundland and Labrador": "NL", "Prince Edward Island": "PE", "Nova Scotia": "NS",
    "New Brunswick": "NB", "Quebec": "QC", "Ontario": "ON", "Manitoba": "MB",
    "Saskatchewan": "SK", "Alberta": "AB", "British Columbia": "BC", "Yukon": "YT",
    "Northwest Territories": "NT", "Nunavut": "NU",
}
ABBR_TO_PROV = {v: k for k, v in PROVINCES.items()}
# HICC prints provinces as "British Columbia"; the census CSV uses the same names.

BROWSER = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
           "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}

_CA_READY = [False]


# --------------------------------------------------------------------------- TLS plumbing
def _gc_ca_bundle() -> None:
    """Make GC hosts verifiable for this process (they omit an intermediate)."""
    if _CA_READY[0]:
        return
    try:
        import certifi
        base = Path(certifi.where()).read_bytes()
    except Exception:                                    # noqa: BLE001
        base = b""
    try:
        r = __import__("requests").get(
            "http://crt.sectigo.com/EntrustOVTLSIssuingRSACA2.crt",
            headers={"User-Agent": BROWSER["User-Agent"]}, timeout=60)
        der = r.content
        extra = b""
        if r.status_code == 200 and der[:1] == b"\x30":
            extra = ssl.DER_cert_to_PEM_cert(der).encode()
        if extra:
            p = RAW / DOMAIN / "ca_bundle_gc.pem"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base + b"\n" + extra)
            os.environ["REQUESTS_CA_BUNDLE"] = str(p)
    except Exception:                                    # noqa: BLE001
        pass                                             # fall back to the stock bundle
    _CA_READY[0] = True


def _get(url: str, **kw) -> bytes:
    if url.startswith(GC):
        _gc_ca_bundle()
        kw.setdefault("headers", BROWSER)
    return fetch(url, domain=DOMAIN, **kw)


# --------------------------------------------------------------------------- text helpers
_TAG = re.compile(r"<script.*?</script>|<style.*?</style>", re.S)
_BRK = re.compile(r"<(?:br|/p|/li|/td|/th|/tr|/h[1-6]|/div|/caption)\s*/?>", re.I)


def html_text(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    s = _TAG.sub(" ", s)
    s = _BRK.sub("\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s).replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return "\n".join(x.strip() for x in s.split("\n"))


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def html_tables(b: bytes) -> list[list[list[str]]]:
    s = b.decode("utf-8", "replace")
    out = []
    for t in re.findall(r"<table.*?</table>", s, re.S):
        rows = []
        for r in re.findall(r"<tr.*?</tr>", t, re.S):
            cells = [squash(htmllib.unescape(re.sub(r"<[^>]+>", "", c))).strip()
                     for c in re.findall(r"<t[dh][^>]*>.*?</t[dh]>", r, re.S)]
            rows.append(cells)
        out.append(rows)
    return out


def pdf_text(name: str, blob: bytes) -> str:
    """pdftotext -layout from a cached PDF (poppler; present in this environment)."""
    p = RAW / DOMAIN / (re.sub(r"[^a-z0-9]+", "_", name.lower())[:60] + ".pdf")
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists() or p.stat().st_size != len(blob):
        p.write_bytes(blob)
    out = subprocess.run(["pdftotext", "-layout", str(p), "-"],
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"pdftotext failed for {p}: {out.stderr[:300]}")
    return out.stdout


def num(s):
    """'11,352' / '+411' / '- 23' / 'N/A' -> int or None."""
    if s is None:
        return None
    t = str(s).replace(",", "").replace("\u2212", "-").strip()
    t = re.sub(r"\s+", "", t)
    if not re.search(r"\d", t):
        return None
    t = t.replace("+", "")
    try:
        return int(round(float(t)))
    except ValueError:
        return None


# --------------------------------------------------------------------------- geo_city ids
def city_geoid(name: str, prov_abbr: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return f"CA-{prov_abbr}-{slug}"


class Geo:
    """Collects (name, province) -> geoid so metric rows can point at a geo_city row."""

    def __init__(self, conn, sid_by_url):
        self.conn = conn
        self.sid_by_url = sid_by_url
        self.seen = {}
        self.rows = 0

    def ensure(self, name: str, prov_abbr: str, url: str) -> str | None:
        name = clean(name)
        if not name or prov_abbr not in ABBR_TO_PROV:
            return None
        geoid = city_geoid(name, prov_abbr)
        if geoid in self.seen:
            return geoid
        upsert(self.conn, "geo_city", {
            "geoid": geoid, "kind": "ca_city", "name": name,
            "state": ABBR_TO_PROV[prov_abbr], "source_id": self.sid_by_url[url],
        }, keys=["geoid"])
        self.seen[geoid] = name
        self.rows += 1
        return geoid


# --------------------------------------------------------------------------- metric writer
def put_metric(conn, entity_type, entity_id, metric, value, year, sid, unit=None,
               note=None, is_est=0):
    if value is None:
        return 0
    upsert(conn, "metric", {
        "entity_type": entity_type, "entity_id": entity_id, "metric": metric,
        "value": float(value), "unit": unit, "year": year, "is_est": is_est,
        "note": note, "source_id": sid,
    }, keys=["entity_type", "entity_id", "metric", "year"])
    return 1


# =================================================================== HICC national counts
def hicc_national(conn, sid, geo_universe):
    """National PiT enumeration per cycle, from the five HICC report vintages."""
    rows = 0
    notes = []

    def S(url):
        return sid[url]

    # ---- 2016: 32 communities, exact components printed ------------------------------
    t16 = squash(html_text(_get(EC2016)))
    m = re.search(r"a total of ([\d,]+) people were identified as experiencing homelessness in the "
                  r"core populations", t16)
    tot16 = m.group(1) if m else None
    m = re.search(r"([\d,]+) people \(24%\) were enumerated in unsheltered locations, ([\d,]+) people "
                  r"\(47%\) were enumerated in shelters and ([\d,]+) people \(29%\) were enumerated in "
                  r"transitional facilities", t16)
    uns16, shel16, trans16 = (m.groups() if m else (None, None, None))
    if tot16:
        categories = {"pit_total": num(tot16), "pit_sheltered": num(shel16),
                      "pit_unsheltered": num(uns16), "pit_transitional": num(trans16)}
        for k, v in categories.items():
            rows += put_metric(conn, "canada", "Canada", k, v, 2016, S(EC2016), "people",
                               "2016 Coordinated PiT Count, 32 participating communities; "
                               "shelters/transitional printed as separate components")
        notes.append(f"2016 total {num(tot16):,}")

    # ---- 2018: 61 communities ------------------------------------------------------
    t18 = squash(html_text(_get(EC2018)))
    m = re.search(r"Across the 61 communities on the night of the count, ([\d,]+) people were "
                  r"identified as experiencing homelessness", t18)
    tot18 = num(m.group(1)) if m else None
    m = re.search(r"([\d,]+) people across 61 communities were experiencing absolute homelessness in "
                  r"shelters or unsheltered locations\. An additional ([\d,]+) people were in a "
                  r"transitional program", t18)
    abs18, trans18 = (num(x) for x in m.groups()) if m else (None, None)
    if tot18:
        rows += put_metric(conn, "canada", "Canada", "pit_total", tot18, 2018, S(EC2018), "people",
                           f"2018 Coordinated PiT Count, 61 communities. Also printed: {abs18:,} in "
                           f"shelters or unsheltered locations and {trans18:,} in transitional "
                           f"programs; the shelters/unsheltered split is printed only as a "
                           f"percentage in this report, so it is not stored."
                           if abs18 else "2018 Coordinated PiT Count, 61 communities")
        rows += put_metric(conn, "canada", "Canada", "pit_transitional", trans18, 2018, S(EC2018),
                           "people", "2018 Coordinated PiT Count, transitional programs")
        notes.append(f"2018 total {tot18:,}")

    # ---- 2020-2022: 72 communities --------------------------------------------------
    t22 = squash(html_text(_get(EC2022)))
    m = re.search(r"A total of ([\d,]+) people were enumerated across 72 communities and regions", t22)
    tot22 = num(m.group(1)) if m else None
    m = re.search(r"This includes ([\d,]+) experiencing homelessness in sheltered and unsheltered "
                  r"locations[^,]*, and ([\d,]+) people who were identified as", t22)
    abs22, trans22 = (num(x) for x in m.groups()) if m else (None, None)
    if tot22:
        rows += put_metric(conn, "canada", "Canada", "pit_total", tot22, 2022, S(EC2022), "people",
                           "2020-2022 Coordinated PiT Count, 72 communities and regions that "
                           "reported comparable enumeration data (counts ran Mar 2020 - Dec 2022)")
        rows += put_metric(conn, "canada", "Canada", "pit_transitional", trans22, 2022, S(EC2022),
                           "people", "2020-2022 cycle, people provisionally accommodated in "
                           "transitional housing")
        notes.append(f"2022 total {tot22:,}")

    # ---- the three-cycle comparable series printed in the 2025 report ----------------
    t25 = squash(html_text(_get(EC2025)))
    ann = {}

    def series(label, pattern):
        m = re.search(pattern, t25)
        if not m:
            return
        got = [num(x) for x in m.groups()]
        if len(got) == 3 and all(v is not None for v in got):
            ann[label] = dict(zip((2022, 2024, 2025), got))

    series("pit_total",
           r"total enumeration increased from ([\d,]+) people in the 2020-2022 PiT Counts to "
           r"([\d,]+) people in the 2023-2024 PiT Counts, then decreased slightly to ([\d,]+) people "
           r"in the 2025")
    series("pit_unsheltered",
           r"unsheltered enumeration increased from ([\d,]+) people[^.]*?in 2020-2022, to ([\d,]+) "
           r"people[^.]*?in 2023-2024, to ([\d,]+) people")
    series("pit_sheltered",
           r"enumeration in shelters increased from ([\d,]+) people[^.]*?in 2020-2022 to ([\d,]+) "
           r"people[^.]*?in 2023-2024, then decreased to ([\d,]+) people")
    series("pit_transitional",
           r"transitional housing increased from ([\d,]+) people[^.]*?in 2020-2022, to ([\d,]+) "
           r"people[^.]*?in 2023-2024, to ([\d,]+) people")
    for year in (2022, 2024, 2025):
        if "pit_total" not in ann:
            break
        if year in ann["pit_unsheltered"] and year in ann["pit_sheltered"]:
            tot = ann["pit_total"][year]
            parts = (ann["pit_sheltered"][year] + ann["pit_unsheltered"][year]
                     + ann["pit_transitional"].get(year, 0))
            if parts != tot:
                notes.append(f"WARNING panel72 {year}: parts {parts} != total {tot}")
        note = (f"{year} cycle, 72-community panel restated in the Everyone Counts 2025 report "
                f"(current PiT Count year comparison)")
        rows += put_metric(conn, "canada", "Canada", "pit_total_panel72", ann["pit_total"].get(year),
                           year, S(EC2025), "people", note)
        rows += put_metric(conn, "canada", "Canada", "pit_sheltered", ann["pit_sheltered"].get(year),
                           year, S(EC2025), "people", note)
        rows += put_metric(conn, "canada", "Canada", "pit_unsheltered",
                           ann["pit_unsheltered"].get(year), year, S(EC2025), "people", note)
        rows += put_metric(conn, "canada", "Canada", "pit_transitional",
                           ann["pit_transitional"].get(year), year, S(EC2025), "people", note)

    # ---- 2024: 74 communities, components printed individually ----------------------
    t24 = squash(html_text(_get(EC2024)))
    m = re.search(r"the enumeration included ([\d,]+) people experiencing homelessness in shelters, "
                  r"([\d,]+) in unsheltered locations \(of whom, ([\d,]+) were in encampments\), and "
                  r"([\d,]+) who were provisionally accommodated in transitional housing", t24)
    if m:
        shel24, uns24, enc24, trans24 = (num(x) for x in m.groups())
        note24 = ("2024 cycle, 74 participating communities; total is the sum of the components "
                  "printed in the report (the report itself says 'nearly 60,000')")
        rows += put_metric(conn, "canada", "Canada", "pit_total",
                           shel24 + uns24 + trans24, 2024, S(EC2024), "people", note24)
        rows += put_metric(conn, "canada", "Canada", "pit_sheltered", shel24, 2024, S(EC2024),
                           "people", "2024 cycle, 74 communities, people in shelters")
        rows += put_metric(conn, "canada", "Canada", "pit_unsheltered", uns24, 2024, S(EC2024),
                           "people", "2024 cycle, 74 communities, people in unsheltered locations")
        rows += put_metric(conn, "canada", "Canada", "pit_transitional", trans24, 2024, S(EC2024),
                           "people", "2024 cycle, 74 communities, provisionally accommodated in "
                           "transitional housing")
        rows += put_metric(conn, "canada", "Canada", "pit_encampments", enc24, 2024, S(EC2024),
                           "people", "2024 cycle, people enumerated in outdoor encampments "
                           "(subset of pit_unsheltered)")
        notes.append(f"2024 total {shel24 + uns24 + trans24:,} (74 cdty)")

    # ---- 2025: 75 communities -------------------------------------------------------
    m = re.search(r"the enumeration included ([\d,]+) \(56%\) people experiencing homelessness in "
                  r"shelters, ([\d,]+) \(27%\) in unsheltered locations \(of whom, ([\d,]+) or 27% "
                  r"were in encampments\), and ([\d,]+) \(17%\) who were provisionally accommodated "
                  r"in transitional housing", t25)
    if m:
        shel25, uns25, enc25, trans25 = (num(x) for x in m.groups())
        note25 = ("2025 cycle, 75 communities; total is the sum of the components printed in the "
                  "report (the report says 'over 65,000')")
        rows += put_metric(conn, "canada", "Canada", "pit_total",
                           shel25 + uns25 + trans25, 2025, S(EC2025), "people", note25)
        rows += put_metric(conn, "canada", "Canada", "pit_sheltered", shel25, 2025, S(EC2025),
                           "people", "2025 cycle, 75 communities, people in shelters")
        rows += put_metric(conn, "canada", "Canada", "pit_unsheltered", uns25, 2025, S(EC2025),
                           "people", "2025 cycle, 75 communities, people in unsheltered locations")
        rows += put_metric(conn, "canada", "Canada", "pit_transitional", trans25, 2025, S(EC2025),
                           "people", "2025 cycle, 75 communities, provisionally accommodated in "
                           "transitional housing")
        rows += put_metric(conn, "canada", "Canada", "pit_encampments", enc25, 2025, S(EC2025),
                           "people", "2025 cycle, people enumerated in outdoor encampments "
                           "(subset of pit_unsheltered)")
        notes.append(f"2025 total {shel25 + uns25 + trans25:,} (75 cdty)")
    m = re.search(r"an additional ([\d,]+) people were counted in (\d+) communities spending the night "
                  r"in facilities of public health, treatment, and correctional systems", t25)
    if m:
        rows += put_metric(conn, "canada", "Canada", "pit_institutional", num(m.group(1)), 2025,
                           S(EC2025), "people",
                           f"2025 supplementary count, {m.group(2)} communities, people in health/"
                           f"treatment/correctional facilities facing imminent discharge to no fixed "
                           f"address")
    return rows, notes, t24 is not None


def hicc_annex_a(conn, geo, sid):
    """Annex A of the 2024 report: the 74 participating communities, as published."""
    blob = _get(EC2024_PDF)
    txt = pdf_text("ec2024_annex_a", blob)
    i = txt.find("Annex A")
    if i < 0:
        return 0
    seg = txt[i:i + 6000]
    n = 0
    for m in re.finditer(r"([A-Z][A-Za-z .'\-()/]+?),\s*(BC|AB|SK|MB|ON|QC|NB|NS|PE|NL|YT|NT|NU)\b",
                         seg):
        name = clean(m.group(1))
        name = re.sub(r"^(?:The following \d+ communities.*?|.*?based:\s*)", "", name or "")
        if not name or len(name) < 3:
            continue
        if geo.ensure(name, m.group(2), EC2024_PDF):
            n += 1
    return n


# =================================================================== BC per-community counts
# HSABC prints the same count area under slightly different names across vintages; the
# "change from previous count" column in each report confirms these are the same place
# (e.g. Cowichan Valley 2023 = 230 and its printed +101 change lands exactly on the
# "Duncan" 2020/21 value of 129).
BC_ALIAS = {
    "duncan": "Cowichan Valley",
    "metro vancouver": "Greater Vancouver",
    "parksville/qualicum": "Parksville/Qualicum Beach",
    "sechelt/gibsons": "Sechelt/Gibsons",
    "sechelt/ gibsons": "Sechelt/Gibsons",
}
BC_SKIP = {"other communities", "non-count communities", "total", "homeless category",
           "community name", "b.c. count communities", "reaching home and other"}


def _bc_norm(name: str) -> str:
    n = squash(name).strip(" .")
    return BC_ALIAS.get(n.lower(), n)


def bc_table_2023(txt: str) -> list[tuple]:
    """Table 2.2 of the 2023 provincial report: name, sheltered, unsheltered, total."""
    out = []
    i = txt.rfind("Table 2.2: Total Homeless, Change, and Proportion")
    if i < 0:
        return out
    for line in txt[i:].split("\n")[1:]:
        if "Non-Count Communities" in line:
            break
        m = re.match(r"^\s*([A-Za-z][A-Za-z ./'\-()&]+?)\s{2,}([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+"
                     r"(?:[+-]?[\d,]+|N/A)\s+[\d.N/A]+\s*$", line.rstrip())
        if not m:
            continue
        name = _bc_norm(m.group(1))
        if name.lower() in BC_SKIP:
            continue
        shel, uns, tot = (num(m.group(k)) for k in (2, 3, 4))
        out.append((name, shel, uns, tot))
    return out


def bc_table_2025(txt: str) -> list[tuple]:
    """Table 3 of the 2024-2025 report: name, sheltered, unsheltered, total."""
    out = []
    i = txt.rfind("Table 3: Total Number Experiencing Homelessness by Community")
    if i < 0:
        return out
    for line in txt[i:].split("\n")[1:]:
        if "Non-Count Communities" in line:
            break
        m = re.match(r"^\s*([A-Za-z][A-Za-z ./'\-()&]+?)\s{2,}([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+"
                     r"(?:[+-]\s?[\d,]+|N/A)\s*$", line.rstrip())
        if not m:
            continue
        name = _bc_norm(m.group(1))
        if name.lower() in BC_SKIP:
            continue
        shel, uns, tot = (num(m.group(k)) for k in (2, 3, 4))
        out.append((name, shel, uns, tot))
    return out


def bc_table_2021(txt: str) -> list[tuple]:
    """Table 3.1 of the 2020/21 report: name, sheltered, unsheltered, total, 2018 total."""
    out = []
    i = txt.find("Table 3.1: Sheltered and Unsheltered Homeless")
    if i < 0:
        return out
    for line in txt[i:].split("\n")[1:]:
        if "Other Communities" in line or "Total " in line and "5,333" in line:
            break
        m = re.match(r"^\s*([A-Za-z][A-Za-z ./'\-()&]+?)\s{2,}([\d,]+)\s+\d+%\s+([\d,]+)\s+\d+%\s+"
                     r"([\d,]+)\s+100%\s+([\d,]+|N/A)\s+(?:[+-]?\d+%?|N/A)\s*$", line.rstrip())
        if not m:
            continue
        name = _bc_norm(m.group(1))
        if name.lower() in BC_SKIP:
            continue
        shel, uns, tot, y2018 = (num(m.group(k)) for k in (2, 3, 4, 5))
        out.append((name, shel, uns, tot, y2018))
    return out


def bc_counts(conn, geo, sid):
    rows, notes = 0, []

    # 2020/21 cycle (counts ran 2020 and 2021) + the 2018 column of the same table
    txt = pdf_text("bc_2020_21_report", _get(BC2021))
    t = bc_table_2021(txt)
    for name, shel, uns, tot, y2018 in t:
        geoid = geo.ensure(name, "BC", BC2021)
        if not geoid:
            continue
        note = "2020-21 Everyone Counts cycle in British Columbia (provincial report table 3.1)"
        for metric, val in (("pit_total", tot), ("pit_sheltered", shel), ("pit_unsheltered", uns)):
            rows += put_metric(conn, "ca_city", geoid, metric, val, 2021, sid[BC2021], "people", note)
        if y2018 is not None:
            rows += put_metric(conn, "ca_city", geoid, "pit_total", y2018, 2018, sid[BC2021], "people",
                               "2018 count total for this community as reprinted in the 2020/21 "
                               "provincial report (table 3.1, '2018' column)")
    notes.append(f"BC 2020/21: {len(t)} communities")

    # 2023 cycle
    txt = pdf_text("bc_2023_report", _get(BC2023))
    t = bc_table_2023(txt)
    for name, shel, uns, tot in t:
        geoid = geo.ensure(name, "BC", BC2023)
        if not geoid:
            continue
        note = "2023 Everyone Counts cycle in British Columbia (2023 provincial report table 2.2)"
        for metric, val in (("pit_total", tot), ("pit_sheltered", shel), ("pit_unsheltered", uns)):
            rows += put_metric(conn, "ca_city", geoid, metric, val, 2023, sid[BC2023], "people", note)
    notes.append(f"BC 2023: {len(t)} communities")

    # 2024/25 cycle
    txt = pdf_text("bc_2024_25_report", _get(BC2025))
    t = bc_table_2025(txt)
    for name, shel, uns, tot in t:
        geoid = geo.ensure(name, "BC", BC2025)
        if not geoid:
            continue
        note = ("2024-2025 Everyone Counts cycle in British Columbia (2024-2025 provincial report "
                "table 3)")
        for metric, val in (("pit_total", tot), ("pit_sheltered", shel), ("pit_unsheltered", uns)):
            rows += put_metric(conn, "ca_city", geoid, metric, val, 2025, sid[BC2025], "people", note)
    notes.append(f"BC 2024/25: {len(t)} communities")
    return rows, notes


# =================================================================== Statistics Canada
def statcan_shelter_cost(conn, sid):
    """Table 98-10-0252: households by shelter-cost-to-income ratio, Canada + provinces."""
    blob = _get(STC255_URL)
    z = zipfile.ZipFile(io.BytesIO(blob))
    member = next(n for n in z.namelist() if re.match(r"^98\d+\.csv$", n))
    keep = {"Canada": "Canada"} | {p: f"Canada:{p}" for p in PROVINCES}
    want = {
        "Total - Shelter-cost-to-income ratio": "households_total_2021census",
        "Spending 30% or more of income on shelter costs": "households_paying_30pct_plus_shelter_cost",
    }
    got = {}
    total_seen = {}
    rows = 0
    with z.open(member) as fh:
        rd = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace"))
        head = next(rd)
        # locate the tenure columns by header text, not by position
        i_tot = next((i for i, h in enumerate(head) if "Tenure (3):Total" in h), 10)
        i_ren = next((i for i, h in enumerate(head) if "Tenure (3):Renter" in h), 14)
        i_stir = next((i for i, h in enumerate(head) if h.lower().startswith("shelter-cost-to-income")), 8)
        year = None
        for r in rd:
            if len(r) <= max(i_tot, i_stir):
                continue
            g = r[1].strip()
            if g not in keep:
                continue
            year = year or r[0].strip()
            # only the cross-tab "Total" cells (income groups, household type, suitability, condition)
            if not all(r[k].startswith("Total") for k in (3, 4, 5, 6)):
                continue
            stir = r[i_stir].strip()
            if stir not in want:
                continue
            v = num(r[i_tot])
            rn = num(r[i_ren])
            if v is None:
                continue
            got.setdefault(keep[g], {})[want[stir]] = (v, rn)
    yr = num(re.sub(r"^(\d{4}).*", r"\1", str(year or ""))) or 2021
    for ent, d in got.items():
        t = d.get("households_total_2021census")
        p = d.get("households_paying_30pct_plus_shelter_cost")
        note = ("2021 Census, table 98-10-0252-01, private households in private occupied dwellings, "
                "tenure = total")
        if t:
            rows += put_metric(conn, "canada", ent, "households_total_2021census", t[0], yr,
                               sid[STC255_URL], "households", note)
        if p:
            rows += put_metric(conn, "canada", ent, "households_paying_30pct_plus_shelter_cost", p[0],
                               yr, sid[STC255_URL], "households",
                               note + "; households spending 30% or more of total income on shelter")
            if t and t[0]:
                rows += put_metric(conn, "canada", ent, "share_paying_30pct_plus_shelter_cost",
                                   round(100.0 * p[0] / t[0], 2), yr, sid[STC255_URL], "percent",
                                   note + "; share computed from the two published counts above")
    return rows, len(got)


# =================================================================== Reaching Home money
def reaching_home(conn, sid):
    """The dollar figures HICC prints on its Reaching Home programme page.

    HICC does not publish per-community Reaching Home allocations in bulk (see BLOCKERS.md);
    the only money printed on the public programme pages is the programme-initiatives line
    below, which is captured here with its own publication year.
    """
    b = _get(RH_PROG)
    t = squash(html_text(b))
    rows = 0
    m = re.search(r"In Budget (\d{4}), the Government of Canada announced \$([\d.,]+) million "
                  r"(?:towards|toward) action research over (\w+) years", t)
    if m:
        val = int(round(float(m.group(2).replace(",", "")) * 1e6))
        rows += put_metric(conn, "canada", "Canada", "reaching_home_action_research_funding",
                           val, int(m.group(1)), sid[RH_PROG], "CAD",
                           f"Action Research on Chronic Homelessness: Budget {m.group(1)} "
                           f"announced ${m.group(2)} million over {m.group(3)} years "
                           f"(Reaching Home programme page)")
    return rows, t


# =============================================================================== run
def run(conn, cfg):
    sid = {}

    urls = [
        (EC2016, EC2016_T), (EC2018, EC2018_T), (EC2022, EC2022_T),
        (EC2024, EC2024_T), (EC2024_PDF, EC2024_T + " (PDF)"), (EC2025, EC2025_T),
        (BC2021, BC2021_T), (BC2023, BC2023_T), (BC2025, BC2025_T),
        (RH_PROG, RH_PROG_T),
    ]
    for url, title in urls:
        _get(url)
        sid[url] = source_id_for(conn, url, DOMAIN, title=title, publisher=HICC
                                 if url.startswith(GC) else HSABC,
                                 license=OGL_CA if url.startswith(GC) else PUBLIC_BC)
    _get(STC255_URL)
    sid[STC255_URL] = source_id_for(conn, STC255_URL, DOMAIN, title=STC255_T,
                                    publisher=STATCAN, license=OGL_CA)

    geo = Geo(conn, sid)
    total = 0
    notes = []

    n, ns, _ = hicc_national(conn, sid, geo)
    total += n
    notes += ns

    n = hicc_annex_a(conn, geo, sid)
    notes.append(f"annex A communities listed: {n}")

    n, ns = bc_counts(conn, geo, sid)
    total += n
    notes += ns

    n, kept = statcan_shelter_cost(conn, sid)
    total += n
    notes.append(f"statcan geographies with a value: {kept}")

    n, _ = reaching_home(conn, sid)
    total += n
    notes.append(f"reaching-home money rows: {n}")

    total += geo.rows
    notes.append(f"geo_city rows touched: {len(geo.seen)}")
    return {"rows": total, "notes": "; ".join(notes),
            "sources": [(u, OGL_CA) for u, _ in urls] + [(STC255_URL, OGL_CA)]}


def selftest(conn):
    print("[canada] geo_city kind='ca_city':",
          conn.execute("SELECT COUNT(*) FROM geo_city WHERE kind='ca_city'").fetchone()[0])
    print("[canada] metric ca_city rows:",
          conn.execute("SELECT COUNT(*) FROM metric WHERE entity_type='ca_city'").fetchone()[0])
    print("[canada] metric canada rows:",
          conn.execute("SELECT COUNT(*) FROM metric WHERE entity_type='canada'").fetchone()[0])
    print("-- communities with a PIT count:")
    for r in conn.execute("""SELECT COUNT(DISTINCT entity_id) c, COUNT(*) n, MIN(year), MAX(year)
                             FROM metric WHERE entity_type='ca_city' AND metric='pit_total'"""):
        print(dict(r))
    print("-- sample rows:")
    for r in conn.execute("""SELECT entity_type, entity_id, metric, value, year, note
                             FROM metric WHERE entity_type IN ('ca_city','canada')
                             ORDER BY RANDOM() LIMIT 3"""):
        print(dict(r))
    for r in conn.execute("SELECT * FROM geo_city WHERE kind='ca_city' ORDER BY RANDOM() LIMIT 3"):
        print(dict(r))


def main():
    import sys
    from ingest.common import main as common_main
    if "--selftest" in sys.argv:
        conn = db()
        res = run(conn, {})
        conn.commit()
        print("[canada] run:", res["rows"], "rows;", res["notes"])
        selftest(conn)
    else:
        common_main(DOMAIN, run)


if __name__ == "__main__":
    main()
