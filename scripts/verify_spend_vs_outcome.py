#!/usr/bin/env python3
"""Independent, live re-check of the five CoC-years quoted in research/spend_vs_outcome.md.

For each CoC-year it
  1. re-downloads the SPM workbook (forced) and re-reads the exits-to-permanent-housing cell
     with openpyxl using the spm domain's own column resolver;
  2. re-reads the point-in-time total from HUD's PIT workbook with pyxlsb;
  3. re-fetches a couple of the underlying USASpending award records and compares
     amount_obligated + place of performance against the award rows that were summed;
  4. recomputes both metrics and prints DB value vs recomputed value.
Run: python3 scripts/verify_spend_vs_outcome.py  (1 req/s, ~20 requests, caches in
data/raw/spend_vs_outcome/; writes no metric rows — it only compares)
"""
from __future__ import annotations
import hashlib, io, json, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import openpyxl  # noqa: E402
from ingest.common import DB, fetch, save_raw  # noqa: E402
from ingest.domains import spm as spm_mod  # noqa: E402
from ingest.domains.hud_pit import read_pit_workbook  # noqa: E402

CASES = [("NY-600", 2024), ("CA-501", 2024), ("TX-607", 2020), ("AK-500", 2019), ("MI-501", 2024)]
AUTHOR_URL = "https://api.usaspending.gov/api/v2/awards/{}"
DOMAIN = "spend_vs_outcome"

conn = sqlite3.connect(DB, timeout=300)
conn.row_factory = sqlite3.Row

# ---------------------------------------------------------------- 1. SPM workbook, forced re-download
URL_SPM = spm_mod.SRC_XLSX
cached = (ROOT / "data/raw/spm" / "System-Performance-Measures-Data.xlsx").read_bytes()
live = fetch(URL_SPM, domain=DOMAIN, headers={"User-Agent": spm_mod.UA}, force=True)
print(f"SPM workbook re-downloaded: {len(live):,} bytes, sha256 {hashlib.sha256(live).hexdigest()}")
print(f"  cached copy identical: {hashlib.sha256(live).hexdigest() == hashlib.sha256(cached).hexdigest()}")

wb = openpyxl.load_workbook(io.BytesIO(live), read_only=True, data_only=True)
sheets = {}
for coc, year in CASES:
    ws = wb[str(year)]
    rows = list(ws.iter_rows(values_only=True))
    hdr = spm_mod.header_map(rows[:2])
    roles, _unmapped = spm_mod.resolve_columns(hdr)
    idx_coc = next(i for i, (h, _g) in hdr.items() if h.lower() == "hud coc number")
    col_exit = next(c for c, (m, _u, _h) in roles.items() if m == "spm_exit_to_ph_count")
    col_award = next(c for c, (m, _u, _h) in roles.items() if m == "spm_coc_award_amount")
    hit = None
    for n, raw in enumerate(rows[2:], start=3):
        if raw and str(raw[idx_coc] or "").strip() == coc:
            hit = (n, raw[col_exit], raw[col_award])
            break
    sheets[(coc, year)] = hit
    print(f"  SPM FY{year} sheet row {hit[0] if hit else '?'}: {coc} exits-to-PH={hit[1]!r} "
          f"CoC-award={hit[2]!r}")

# ---------------------------------------------------------------- 2. HUD PIT workbook (live)
URL_PIT = "https://www.huduser.gov/portal/sites/default/files/xls/2007-2025-PIT-Counts-by-CoC.xlsb"
pit_live = fetch(URL_PIT, domain=DOMAIN, headers={"User-Agent": spm_mod.UA}, force=True)
p = save_raw(DOMAIN, "2007-2025-PIT-Counts-by-CoC.xlsb", pit_live)
_cached_pit = (ROOT / "data/raw/hud_pit" / "2007-2025-PIT-Counts-by-CoC.xlsb").read_bytes()
print(f"  PIT cached copy identical: {hashlib.sha256(pit_live).hexdigest() == hashlib.sha256(_cached_pit).hexdigest()}")
pit_rows, _names, _notes = read_pit_workbook(p)
print(f"PIT workbook re-downloaded: {len(pit_live):,} bytes, {len(pit_rows):,} CoC-years parsed")

# ---------------------------------------------------------------- 3. award detail, live
AWARD_URL = "https://api.usaspending.gov/api/v2/awards/{}/"


def award_detail(key):
    return json.loads(fetch(AWARD_URL.format(key), domain=DOMAIN))


print("\n%-8s %-5s %-14s %14s %10s %14s %14s" %
      ("CoC", "FY", "metric", "obligated", "exits", "DB value", "recomputed"))
for coc, year in CASES:
    spend = conn.execute("SELECT obligated, awards, from_county, from_city FROM spend_vs_outcome_spend "
                         "WHERE coc_num=? AND fy=?", (coc, year)).fetchone()
    met = {r["metric"]: r["value"] for r in conn.execute(
        """SELECT metric, value FROM metric WHERE entity_type='coc' AND entity_id=? AND year=?""",
        (coc, year))}
    hit = sheets[(coc, year)]
    exits = spm_mod.num(hit[1]) if hit else None
    oblig = spend["obligated"] if spend else None
    recomputed = round(oblig / exits, 2) if oblig and exits else None
    print("%-8s %-5s %-14s %14.2f %10s %14s %14s   %s" %
          (coc, year, "spend_per_exit", oblig or 0, exits,
           met.get("spend_per_exit_to_housing"), recomputed,
           "MATCH" if met.get("spend_per_exit_to_housing") == recomputed else "DIFF"))

    p0 = pit_rows.get((coc, year), {}).get("total")
    p1 = pit_rows.get((coc, year + 1), {}).get("total")
    net = (p0 - p1) if (p0 is not None and p1 is not None) else None
    rec2 = round(oblig / net, 2) if oblig and net and net > 0 else None
    print("%-8s %-5s %-14s %14.2f %10s %14s %14s   (pit %s -> %s = %s)" %
          (coc, year, "spend_per_person", oblig or 0, net,
           met.get("spend_per_person_placed"), rec2,
           p0, p1, net))

    # live award re-checks: the two largest award rows that THIS CoC's place set attributes here
    from scripts.build_outcome_metrics import base_place
    keys = {r["place_key"] for r in conn.execute(
        """SELECT place_key FROM spend_vs_outcome_place WHERE coc_num=? AND place_kind='county'""", (coc,))}
    pkeys = {r["place_key"] for r in conn.execute(
        """SELECT place_key FROM spend_vs_outcome_place WHERE coc_num=? AND place_kind='place'""", (coc,))}
    cand = []
    for r in conn.execute(
            """SELECT award_key, amount_obligated, place_county, place_city
                 FROM award WHERE award_type!='subaward' AND fy=? AND amount_obligated IS NOT NULL
                  AND place_state=?""", (str(year), coc[:2])):
        if (r["place_county"] and base_place(r["place_county"]) in keys) or \
           (r["place_city"] and base_place(r["place_city"]) in pkeys):
            cand.append(dict(r))
    cand.sort(key=lambda x: -x["amount_obligated"])
    shown = 0
    for r in cand:
        if shown >= 2:
            break
        try:
            d = award_detail(r["award_key"])
        except Exception as e:            # noqa: BLE001
            print(f"   ! detail fetch failed {r['award_key']}: {e}")
            continue
        pop = d.get("place_of_performance") or {}
        amt = d.get("total_obligation")
        same = (pop.get("county_name") or "").upper() == (r["place_county"] or "").upper()
        print(f"   award {r['award_key']}: live total_obligation={amt} county={pop.get('county_name')!r} "
              f"city={pop.get('city_name')!r} | db {r['amount_obligated']} {r['place_county']!r}/"
              f"{r['place_city']!r} -> amount_equal={amt == r['amount_obligated']} county_equal={same}")
        shown += 1
