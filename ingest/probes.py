"""Cheap upstream change detection: one small request per source tells us whether to re-ingest.

Re-ingesting a domain costs minutes to hours of polite 1 req/s fetching, and most weeks nothing
upstream has moved. Every publisher we use already exposes a cheap signal of change: HTTP
validators on a static file, Socrata's `rowsUpdatedAt`, ArcGIS's `editingInfo.lastEditDate`, a
directory for next year's vintage that starts answering 200, or a new link on an index page. A
probe reads that signal and nothing else; `scripts/refresh.py` compares it with the fingerprint
accepted after the last good ingest (data/freshness.json).

Rules that shaped this file:

* `fingerprint()` never raises. A probe that errors is reported as an error, and an error is
  never mistaken for a change (or for "same").
* Only the fields in `COMPARE` decide CHANGED. Anything noisy (status codes, Content-Range
  spelling) is kept out of the fingerprint so a quiet source stays quiet.
* Every URL here answered from the project's machine when it was added; research/refresh.md
  records the date and the result. Hosts that challenge the project UA (HUD USER's AWS WAF,
  hudexchange) get `ua: "browser"`, the same browser UA their domain ingestor already uses.
* 1 request per second, shared with ingest.common's throttle.

Probe kinds
-----------
http          HEAD (ranged GET `bytes=0-0` when HEAD is refused): Last-Modified, ETag, length.
socrata       GET https://<host>/api/views/<id>.json: rowsUpdatedAt, viewLastModified.
arcgis        GET <layer>?f=json: editingInfo.lastEditDate (+ dataLastEditDate when present).
json          GET a JSON endpoint and record one field (USASpending's last_updated date).
next_vintage  does the URL template answer 200 for `current + 1`? True means a new vintage.
listing       GET an index page; sha256 of the sorted set of hrefs matching `pattern`.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from urllib.parse import urljoin

from ingest.common import UA, _throttle

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 45

# The fields that decide CHANGED, per kind. Everything else in a fingerprint is context.
COMPARE = {
    "http": ("last_modified", "etag", "content_length"),
    "socrata": ("rows_updated_at", "view_last_modified"),
    "arcgis": ("last_edit_date", "data_last_edit_date"),
    "json": ("value",),
    "next_vintage": ("next_exists",),
    "listing": ("sha256",),
}

ACS_SF = ("https://www2.census.gov/programs-surveys/acs/summary_file/{year}/"
          "table-based-SF/data/5YRData/")
GAZETTEER = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/"
POPEST = "https://www2.census.gov/programs-surveys/popest/datasets/2020-{year}/"
HUDUSER_XLS = "https://www.huduser.gov/portal/sites/default/files/xls/"
AHAR_2025 = ("https://www.huduser.gov/portal/datasets/ahar/"
             "2025-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html")
USASPENDING_UPDATED = "https://api.usaspending.gov/api/v2/awards/last_updated/"
COC_LAYER_HUD_PIT = ("https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
                     "Continuum_of_Care_Grantee_Areas/FeatureServer/0")
COC_LAYER_GEO = ("https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
                 "CoC_Geo_Type/FeatureServer/0")
SUB_EST = ("https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/"
           "cities/totals/sub-est2024.csv")


def _soc(name, host, ident):
    return {"name": name, "kind": "socrata", "host": host, "id": ident,
            "url": f"https://{host}/api/views/{ident}.json"}


PROBES: dict[str, list[dict]] = {
    "hud_pit": [
        {"name": "pit_workbook", "kind": "http", "ua": "browser",
         "url": HUDUSER_XLS + "2007-2025-PIT-Counts-by-CoC.xlsb"},
        {"name": "pit_next_year", "kind": "next_vintage", "ua": "browser", "current": 2025,
         "url": HUDUSER_XLS + "2007-{year}-PIT-Counts-by-CoC.xlsb"},
        {"name": "ahar_2025_files", "kind": "listing", "ua": "browser", "url": AHAR_2025,
         "pattern": r"(?i)\.(xlsx|xlsb|csv|pdf)$"},
        {"name": "coc_grantee_layer", "kind": "arcgis", "url": COC_LAYER_HUD_PIT},
        {"name": "gazetteer_next", "kind": "next_vintage", "current": 2024, "url": GAZETTEER},
    ],
    "hic": [
        {"name": "hic_workbook", "kind": "http", "ua": "browser",
         "url": HUDUSER_XLS + "2007-2025-HIC-Counts-by-CoC.xlsx"},
        {"name": "hic_projects_csv", "kind": "http", "ua": "browser",
         "url": HUDUSER_XLS + "2025-HIC-Counts-by-State.csv"},
        {"name": "hic_next_year", "kind": "next_vintage", "ua": "browser", "current": 2025,
         "url": HUDUSER_XLS + "2007-{year}-HIC-Counts-by-CoC.xlsx"},
    ],
    "health": [
        _soc("nchs_state", "data.cdc.gov", "xbxb-epbu"),
        _soc("vsrr_state", "data.cdc.gov", "xkb8-kh2a"),
        _soc("vsrr_county", "data.cdc.gov", "gb4e-yj24"),
        _soc("sf_dph", "data.sfgov.org", "jxrr-bmra"),
        _soc("ct_ocme", "data.ct.gov", "rybz-nyjw"),
        _soc("brfss", "data.cdc.gov", "dttw-5yxu"),
    ],
    "coc_geometry": [
        {"name": "coc_geo_type_layer", "kind": "arcgis", "url": COC_LAYER_GEO},
    ],
    "usaspending": [
        {"name": "awards_last_updated", "kind": "json", "url": USASPENDING_UPDATED,
         "field": "last_updated"},
    ],
    "places": [
        {"name": "sub_est2024", "kind": "http", "url": SUB_EST},
        {"name": "popest_next", "kind": "next_vintage", "current": 2024, "url": POPEST},
        {"name": "gazetteer_next", "kind": "next_vintage", "current": 2023, "url": GAZETTEER},
    ],
    "sf": [
        _soc("budget", "data.sfgov.org", "xdgd-c79v"),
        _soc("budget_fte", "data.sfgov.org", "4zfx-f2ts"),
        _soc("supplier_contracts", "data.sfgov.org", "cqi5-hm2d"),
    ],
    "uk": [
        {"name": "rough_sleeping_page", "kind": "listing",
         "url": "https://www.gov.uk/government/statistical-data-sets/tables-on-rough-sleeping",
         "pattern": r"(?i)assets\.publishing\.service\.gov\.uk/.+\.(ods|xlsx|csv)$"},
        {"name": "live_tables_page", "kind": "listing",
         "url": "https://www.gov.uk/government/statistical-data-sets/live-tables-on-homelessness",
         "pattern": r"(?i)assets\.publishing\.service\.gov\.uk/.+\.(ods|xlsx|csv)$"},
        {"name": "ni_bulletin_tables", "kind": "http", "ua": "browser",
         "url": ("https://www.communities-ni.gov.uk/system/files/2026-06/"
                 "ni-homelessness-bulletin-oct-mar-2026-tables.xlsx")},
    ],
    "canada": [
        {"name": "statcan_98100252", "kind": "http",
         "url": "https://www150.statcan.gc.ca/n1/tbl/csv/98100252-eng.zip"},
        {"name": "bc_count_2024_25", "kind": "http",
         "url": "https://hsa-bc.ca/_Library/2024_25_HC/2024-2025_Report_on_Homeless_Counts_in_BC.pdf"},
    ],
    "australia": [
        {"name": "abs_cube_state", "kind": "http",
         "url": ("https://www.abs.gov.au/statistics/people/housing/estimating-homelessness-census/"
                 "2021/20490do001_2021.xlsx")},
        {"name": "rogs_homelessness_csv", "kind": "http",
         "url": ("https://assets.pc.gov.au/2026-01/rogs-2026-partg-section19-homelessness-"
                 "services-dataset.csv?VersionId=oou7gCfLtIUjLTYmqVgZgevEBezJo44g")},
    ],
    "county": [
        {"name": "acs5_next", "kind": "next_vintage", "current": 2024, "url": ACS_SF},
        _soc("cdc_places_county", "data.cdc.gov", "swc5-untb"),
        # HRSA regenerates this file every day (Last-Modified moves daily and differs between
        # its servers) and its HEAD answers a bogus 20-byte length, so: ranged GET, size only.
        {"name": "hrsa_hpsa_mental_health", "kind": "http", "head": False,
         "compare": ["content_length"],
         "url": "https://data.hrsa.gov/DataDownload/DD_Files/BCD_HPSA_FCT_DET_MH.csv"},
        {"name": "usaspending_last_updated", "kind": "json", "url": USASPENDING_UPDATED,
         "field": "last_updated"},
    ],
}


# --------------------------------------------------------------------------- plumbing
def _headers(probe, extra=None):
    h = {"User-Agent": BROWSER_UA if probe.get("ua") == "browser" else UA, "Accept": "*/*"}
    if extra:
        h.update(extra)
    return h


def _req(method, url, probe, **kw):
    import requests
    _throttle()
    return requests.request(method, url, headers=_headers(probe, kw.pop("headers", None)),
                            timeout=TIMEOUT, allow_redirects=True, **kw)


def _total_from_range(value):
    m = re.search(r"/(\d+)\s*$", value or "")
    return int(m.group(1)) if m else None


def _http(probe):
    url = probe["url"]
    r, how = None, "HEAD"
    if probe.get("head", True):
        r = _req("HEAD", url, probe)
    if r is None or r.status_code in (400, 403, 405, 501) or (r.status_code == 200 and not (
            r.headers.get("Last-Modified") or r.headers.get("ETag"))):
        # some servers refuse HEAD, and some answer it without validators; ask for one byte
        g = _req("GET", url, probe, headers={"Range": "bytes=0-0"}, stream=True)
        g.close()
        if r is None or g.status_code in (200, 206):
            r, how = g, "GET bytes=0-0"
    if r.status_code not in (200, 206):
        return {"ok": False, "error": f"HTTP {r.status_code} ({how})"}
    length = (_total_from_range(r.headers.get("Content-Range")) if r.status_code == 206
              else r.headers.get("Content-Length"))
    return {"ok": True, "last_modified": r.headers.get("Last-Modified"),
            "etag": r.headers.get("ETag"),
            "content_length": int(length) if length not in (None, "") else None, "via": how}


def _get_json(probe, url):
    r = _req("GET", url, probe, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


def _socrata(probe):
    j = _get_json(probe, probe["url"])
    if "rowsUpdatedAt" not in j and "viewLastModified" not in j:
        return {"ok": False, "error": "no rowsUpdatedAt/viewLastModified in view metadata"}
    return {"ok": True, "rows_updated_at": j.get("rowsUpdatedAt"),
            "view_last_modified": j.get("viewLastModified"), "name": j.get("name")}


def _arcgis(probe):
    j = _get_json(probe, probe["url"] + "?f=json")
    if "error" in j:
        return {"ok": False, "error": f"arcgis error {j['error']}"[:300]}
    ei = j.get("editingInfo") or {}
    if "lastEditDate" not in ei:
        return {"ok": False, "error": "layer has no editingInfo.lastEditDate"}
    return {"ok": True, "last_edit_date": ei.get("lastEditDate"),
            "data_last_edit_date": ei.get("dataLastEditDate"), "name": j.get("name")}


def _json(probe):
    j = _get_json(probe, probe["url"])
    v = j
    for part in probe["field"].split("."):
        v = v[part]
    return {"ok": True, "value": v}


def _next_vintage(probe):
    nxt = int(probe["current"]) + 1
    url = probe["url"].format(year=nxt)
    r = _req("HEAD", url, probe)
    if r.status_code in (403, 405, 501):
        g = _req("GET", url, probe, headers={"Range": "bytes=0-0"}, stream=True)
        g.close()
        r = g
    if r.status_code >= 500:
        return {"ok": False, "error": f"HTTP {r.status_code} for {url}"}
    exists = r.status_code in (200, 206)
    return {"ok": True, "current": int(probe["current"]), "next_year": nxt,
            "next_exists": exists, "next_status": r.status_code}


HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


def _listing(probe):
    r = _req("GET", probe["url"], probe)
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    pat = re.compile(probe["pattern"])
    hrefs = set()
    for h in HREF.findall(r.text):
        full = urljoin(r.url, html.unescape(h).strip())
        full = full.split("#", 1)[0]
        if pat.search(full):
            hrefs.add(full)
    if not hrefs:
        return {"ok": False, "error": "no hrefs matched the pattern (page layout changed?)"}
    ordered = sorted(hrefs)
    out = {"ok": True, "count": len(ordered),
           "sha256": hashlib.sha256("\n".join(ordered).encode()).hexdigest()}
    if len(ordered) <= 40:
        out["hrefs"] = ordered          # small enough to diff by eye in data/freshness.json
    return out


KINDS = {"http": _http, "socrata": _socrata, "arcgis": _arcgis, "json": _json,
         "next_vintage": _next_vintage, "listing": _listing}


def fingerprint(probe: dict) -> dict:
    """Read one probe's change signal. Never raises: failures come back as ok=False."""
    try:
        fn = KINDS.get(probe.get("kind") or "")
        if fn is None:
            return {"ok": False, "error": f"unknown probe kind {probe.get('kind')!r}"}
        return fn(probe)
    except Exception as e:  # noqa: BLE001  (a probe must never take the run down)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


def compare_view(probe: dict, fp: dict | None) -> dict:
    """The subset of a fingerprint that decides CHANGED (a probe may narrow it with `compare`)."""
    fp = fp or {}
    return {k: fp.get(k) for k in (probe.get("compare") or COMPARE.get(probe.get("kind") or "", ()))}


def all_probes(domains=None):
    for dom in sorted(PROBES):
        if domains and dom not in domains:
            continue
        for p in PROBES[dom]:
            yield dom, p


if __name__ == "__main__":
    import sys
    want = set(sys.argv[1:]) or None
    for d, p in all_probes(want):
        print(d, p["name"], json.dumps(fingerprint(p), sort_keys=True))
