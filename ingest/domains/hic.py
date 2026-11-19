"""hic — HUD Housing Inventory Count (HIC): shelter and housing capacity per CoC, per year.

What this domain answers: how many beds and units exist, by program type (ES, TH, SH, PSH,
RRH, OPH, DV, youth, veteran, chronic), in every Continuum of Care, every year HUD published
a HIC — plus a derived beds-per-homeless-person ratio.

Sources (both fetched through `common.fetch`, cached in `data/raw/hic/`):

1. `2007-2025-HIC-Counts-by-CoC.xlsx` — HUD USER / AHAR Part 1 data files, one sheet per
   year, one column per bed type. This is the primary source: every number written here
   comes from a cell in this workbook (or is a stated arithmetic combination of such cells).
   Discovered from
   https://www.huduser.gov/portal/datasets/ahar/2025-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html
2. `2025-HIC-Counts-by-State.csv` — HUD's project-level HIC inventory for the newest year
   (one row per HUD-listed project: CoC, project type, beds, inventory period, target
   population).
   Used (a) to count projects per CoC, (b) to cross-check the workbook's per-CoC bed totals
   cell-for-cell, and (c) to verify that DV beds (published total − published non-DV total)
   really are the domestic-violence-targeted beds. It is *not* used to overwrite workbook
   values.

Metric rows (table `metric`, entity_type='coc', entity_id=<CoC number>, year=<HIC year>):

  hic_beds_es / _th / _sh / _psh / _rrh / _oph / _dem   published year-round beds per program
  hic_beds_total                                        sum of the program columns above
  hic_beds_dv                                           domestic-violence beds = HUD's published
                                                        year-round program total minus HUD's
                                                        published non-DV total
  hic_beds_youth / hic_beds_veteran                     dedicated youth / veteran beds (ES,TH,SH)
  hic_beds_chronic_psh                                  dedicated chronically homeless PSH beds
  hic_beds_seasonal_es / hic_beds_overflow_es           ES seasonal and overflow beds
  hic_units_family_es/_th/_sh/_rrh/_psh/_oph           published units *for households with
                                                        children* (HIC publishes no all-household
                                                        unit count); name says so on purpose
  hic_beds_per_homeless                                 derived: hic_beds_total / `pit.total`
  hic_projects                                          HUD-listed projects (newest year, CSV)

`is_est` is 0 for a single published cell, for plain sums of published cells, and for the
DV difference (an exact partition of two published cells, re-verified at project level);
1 only for the beds-per-homeless ratio, which divides by the PIT count.

Idempotent: every row is an upsert on `metric`'s natural key (entity_type, entity_id, metric,
year), so re-running rewrites the same rows. Re-run: `python3 -m ingest.run hic`.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import sys
import time

from ingest.common import (RAW, clean, db, fetch, now, save_raw, source_id_for,
                           to_int, upsert)

DOMAIN = "hic"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

# HUD USER serves these files only to a browser-ish UA (hudexchange.info 404s the plain
# ingest UA); same public files either way.
URL_HIC_XLSX = ("https://www.huduser.gov/portal/sites/default/files/xls/"
                "2007-2025-HIC-Counts-by-CoC.xlsx")
XLSX_NAME = "2007-2025-HIC-Counts-by-CoC.xlsx"
URL_HIC_CSV = ("https://www.huduser.gov/portal/sites/default/files/xls/"
               "2025-HIC-Counts-by-State.csv")
CSV_NAME = "2025-HIC-Counts-by-State.csv"
URL_INDEX = ("https://www.huduser.gov/portal/datasets/ahar/"
             "2025-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html")

COC_RX = re.compile(r"^([A-Z]{2}-\d{3})([a-z]?)$")   # MO-604a = HUD's cross-state footnote row

# --------------------------------------------------------------------------- header mapping
# HUD renames and reorders columns every few years (and 2025 finally lost the blank spacer
# row above the header). Match on a normalised header string, newest spelling first.
def hnorm(h) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", str(h).upper())).strip()


COLS: dict[str, list[str]] = {
    # year-round beds per program (ES 2011-2013 spell the ES total "excluding seasonal+overflow",
    # which is the same year-round figure: seasonal and overflow beds are not year-round)
    "beds_es": ["TOTAL YEAR ROUND BEDS ES", "TOTAL YEAR ROUND ES BEDS",
                "TOTAL EMERGENCY SHELTER ES BEDS EXCLUDING SEASONAL OVERFLOW",
                "TOTAL EMERGENCY SHELTER ES BEDS EXCLUDING SEASONAL O V"],
    "nondv_es": ["TOTAL NON DV YEAR ROUND BEDS ES", "TOTAL NON DV YEAR ROUND ES BEDS"],
    "beds_th": ["TOTAL YEAR ROUND BEDS TH", "TOTAL YEAR ROUND TH BEDS", "TOTAL TH BEDS"],
    "nondv_th": ["TOTAL NON DV YEAR ROUND BEDS TH", "TOTAL NON DV YEAR ROUND TH BEDS"],
    "beds_sh": ["TOTAL YEAR ROUND BEDS SH", "TOTAL YEAR ROUND SH BEDS", "TOTAL SH BEDS"],
    "nondv_sh": ["TOTAL NON DV YEAR ROUND BEDS SH", "TOTAL NON DV YEAR ROUND SH BEDS"],
    "beds_rrh": ["TOTAL YEAR ROUND BEDS RRH", "TOTAL YEAR ROUND RRH BEDS", "TOTAL RRH BEDS"],
    "nondv_rrh": ["TOTAL NON DV YEAR ROUND BEDS RRH", "TOTAL NON DV YEAR ROUND RRH BEDS"],
    "beds_psh": ["TOTAL YEAR ROUND BEDS PSH", "TOTAL YEAR ROUND PSH BEDS", "TOTAL PSH BEDS"],
    "nondv_psh": ["TOTAL NON DV YEAR ROUND BEDS PSH", "TOTAL NON DV YEAR ROUND PSH BEDS"],
    "beds_oph": ["TOTAL YEAR ROUND BEDS OPH"],
    "nondv_oph": ["TOTAL NON DV YEAR ROUND BEDS OPH"],
    # DEM = HUD's "Demonstration" projects, 2014-2016 only, folded into OPH afterwards
    "beds_dem": ["TOTAL YEAR ROUND BEDS DEM"],
    "nondv_dem": ["TOTAL NON DV YEAR ROUND BEDS DEM"],
    "seasonal_es": ["TOTAL SEASONAL BEDS ES", "TOTAL SEASONAL ES BEDS"],
    "overflow_es": ["TOTAL OVERFLOW BEDS ES", "TOTAL OVERFLOW ES BEDS",
                    "TOTAL OVERFLOW VOUCHER ES BEDS"],
    "chronic_psh": ["DEDICATED CHRONICALLY HOMELESS BEDS PSH", "TOTAL CHRONIC HOMELESS PSH BEDS"],
    "veteran": ["DEDICATED VETERAN BEDS ES TH SH"],
    "youth": ["DEDICATED YOUTH BEDS ES TH SH"],
    "units_es": ["TOTAL UNITS FOR HOUSEHOLDS WITH CHILDREN ES",
                 "TOTAL ES UNITS FOR HOUSEHOLDS WITH CHILDREN"],
    "units_th": ["TOTAL UNITS FOR HOUSEHOLDS WITH CHILDREN TH",
                 "TOTAL TH UNITS FOR HOUSEHOLDS WITH CHILDREN"],
    "units_sh": ["TOTAL UNITS FOR HOUSEHOLDS WITH CHILDREN SH"],
    "units_rrh": ["TOTAL UNITS FOR HOUSEHOLDS WITH CHILDREN RRH",
                  "TOTAL RRH UNITS FOR HOUSEHOLDS WITH CHILDREN"],
    "units_psh": ["TOTAL UNITS FOR HOUSEHOLDS WITH CHILDREN PSH",
                  "TOTAL PSH UNITS FOR HOUSEHOLDS WITH CHILDREN"],
    "units_oph": ["TOTAL UNITS FOR HOUSEHOLDS WITH CHILDREN OPH"],
}

PROGRAMS = ("es", "th", "sh", "rrh", "psh", "oph", "dem")

# metric name, source column key, unit, is_est, note
PLAIN_METRICS = [
    ("hic_beds_es", "beds_es", "beds", 0, "HIC total year-round beds, emergency shelter (ES)"),
    ("hic_beds_th", "beds_th", "beds", 0, "HIC total year-round beds, transitional housing (TH)"),
    ("hic_beds_sh", "beds_sh", "beds", 0, "HIC total year-round beds, safe haven (SH)"),
    ("hic_beds_psh", "beds_psh", "beds", 0, "HIC total year-round beds, permanent supportive housing (PSH)"),
    ("hic_beds_rrh", "beds_rrh", "beds", 0, "HIC total year-round beds, rapid re-housing (RRH)"),
    ("hic_beds_oph", "beds_oph", "beds", 0, "HIC total year-round beds, other permanent housing (OPH)"),
    ("hic_beds_dem", "beds_dem", "beds", 0, "HIC total year-round beds, HUD demonstration projects (2014-2016 only)"),
    ("hic_beds_seasonal_es", "seasonal_es", "beds", 0, "HIC total seasonal beds, emergency shelter (ES)"),
    ("hic_beds_overflow_es", "overflow_es", "beds", 0, "HIC total overflow beds, emergency shelter (ES)"),
    ("hic_beds_veteran", "veteran", "beds", 0, "HIC dedicated veteran beds (ES, TH, SH); HUD publishes this from 2017"),
    ("hic_beds_youth", "youth", "beds", 0, "HIC dedicated youth beds (ES, TH, SH); HUD publishes this from 2017"),
    ("hic_beds_chronic_psh", "chronic_psh", "beds", 0, "HIC dedicated chronically homeless beds, PSH"),
    ("hic_units_family_es", "units_es", "units", 0, "HIC units for households WITH CHILDREN only, ES (HIC has no all-household unit count)"),
    ("hic_units_family_th", "units_th", "units", 0, "HIC units for households WITH CHILDREN only, TH"),
    ("hic_units_family_sh", "units_sh", "units", 0, "HIC units for households WITH CHILDREN only, SH"),
    ("hic_units_family_rrh", "units_rrh", "units", 0, "HIC units for households WITH CHILDREN only, RRH"),
    ("hic_units_family_psh", "units_psh", "units", 0, "HIC units for households WITH CHILDREN only, PSH"),
    ("hic_units_family_oph", "units_oph", "units", 0, "HIC units for households WITH CHILDREN only, OPH"),
]

DV_NOTE = ("domestic-violence beds = HUD's published year-round program total minus HUD's "
           "published non-DV year-round total for the same program; for the newest year this "
           "difference was re-checked cell-for-cell against the project-level inventory file's "
           "Target Population = DV projects")


def _get(url: str, name: str) -> bytes:
    b = fetch(url, domain=DOMAIN, headers={"User-Agent": UA})
    save_raw(DOMAIN, name, b)
    return b


# --------------------------------------------------------------------------- workbook parse
def read_workbook(notes: list[str]):
    """-> data{(coc, year): {colkey: int}}, resolved{year: {colkey: header}}"""
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("openpyxl is required to read HUD's .xlsx HIC workbook") from e
    wb = openpyxl.load_workbook(str(RAW / DOMAIN / XLSX_NAME), read_only=True, data_only=True)
    data: dict[tuple[str, int], dict[str, int]] = {}
    resolved: dict[int, dict[str, str]] = {}
    suffixed = 0
    for sheet in wb.sheetnames:
        if not (sheet.isdigit() and len(sheet) == 4):
            continue                      # 'Revisions' sheet carries no CoC inventory
        year = int(sheet)
        ws = wb[sheet]
        it = ws.iter_rows(values_only=True)
        hdr, idx = None, {}
        for _ in range(4):                # header is row 1 (2025) or row 2 (2007-2024)
            try:
                cand = next(it)
            except StopIteration:
                break
            if cand and hnorm(cand[0]) in ("COC NUMBER", "COC"):
                hdr = cand
                break
        if hdr is None:
            notes.append(f"{year}: no header row found, sheet skipped")
            continue
        for i, h in enumerate(hdr):
            if h is not None:
                idx.setdefault(hnorm(h), i)
        col_of: dict[str, int] = {}
        resolved[year] = {}
        for key, cands in COLS.items():
            for cand in cands:
                if cand in idx:
                    col_of[key] = idx[cand]
                    resolved[year][key] = cand
                    break
        if "beds_es" not in col_of:
            notes.append(f"{year}: no ES bed column, sheet skipped")
            continue
        n = 0
        for r in it:
            if not r:
                continue
            cell = r[0]
            raw = clean(cell) if isinstance(cell, str) else None
            if not raw:
                continue
            m = COC_RX.match(raw.strip())
            if not m:
                continue                  # 'Total' label row, blank rows, state headers
            coc = m.group(1)
            if m.group(2):
                suffixed += 1             # MO-604a -> MO-604 (cross-state footnote row)
            rec = {}
            for key, i in col_of.items():
                rec[key] = to_int(r[i]) if i < len(r) else None
            data[(coc, year)] = rec
            n += 1
        if n == 0:
            notes.append(f"{year}: no CoC rows parsed")
    if suffixed:
        notes.append(f"normalised {suffixed} 'NN-###x' row(s) (HUD's cross-state footnote form)")
    return data, resolved


# --------------------------------------------------------------- project-level inventory CSV
# HUD's inventory file has two inventory periods per project: 'C' (current, the one HUD counts
# in the CoC workbook) and 'U' (a second/nonexistent period). Only 'C' rows reproduce the
# workbook's per-CoC bed totals — see the cross-check in run(). DV beds are the rows whose
# Target Population is 'DV' (2,635 of them in 2025; the Victim Service Provider flag is a
# different thing — HMIS participation — and does NOT delimit the DV inventory).
def read_project_csv(notes: list[str]):
    """-> ({coc: {sums}}, projects{coc: int}) from HUD's newest-year project-level inventory."""
    raw = (RAW / DOMAIN / CSV_NAME).read_bytes().decode("utf-8-sig", errors="replace")
    rd = csv.DictReader(io.StringIO(raw))
    need = {"HudNum", "Project Type", "Year-Round Beds", "Inventory Type", "Target Population"}
    if not need.issubset(set(rd.fieldnames or ())):
        notes.append("project inventory CSV: expected columns missing, cross-check skipped")
        return {}, {}
    sums: dict[str, dict[str, float]] = {}
    projects: dict[str, int] = {}
    for row in rd:
        if (clean(row.get("Inventory Type")) or "").upper() != "C":
            continue
        coc = clean(row.get("HudNum"))
        m = COC_RX.match(coc or "")
        if not m:
            continue
        coc = m.group(1)
        pt = (clean(row.get("Project Type")) or "").lower()
        if pt not in PROGRAMS:
            continue
        s = sums.setdefault(coc, {})
        projects[coc] = projects.get(coc, 0) + 1
        yr = to_int(row.get("Year-Round Beds")) or 0
        s["yr_" + pt] = s.get("yr_" + pt, 0) + yr
        if (clean(row.get("Target Population")) or "").upper() == "DV":
            s["dv_yr"] = s.get("dv_yr", 0) + yr
    return sums, projects


# ----------------------------------------------------------------------------------- writer
def run(conn: sqlite3.Connection, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    notes: list[str] = []
    src_xlsx = source_id_for(
        conn, URL_HIC_XLSX, DOMAIN,
        title="Housing Inventory Count (HIC) counts by CoC, 2007-2025",
        publisher="US Department of Housing and Urban Development (HUD USER)",
        license="US Government work, public domain")
    src_csv = source_id_for(
        conn, URL_HIC_CSV, DOMAIN,
        title="HIC 2025 project-level housing inventory by state/CoC (2025-HIC-Counts-by-State.csv)",
        publisher="US Department of Housing and Urban Development (HUD USER)",
        license="US Government work, public domain")
    source_id_for(conn, URL_INDEX, DOMAIN,
                  title="AHAR Part 1 2025 — PIT and HIC data files index page",
                  publisher="US Department of Housing and Urban Development (HUD USER)",
                  license="US Government work, public domain")
    _get(URL_HIC_XLSX, XLSX_NAME)
    _get(URL_HIC_CSV, CSV_NAME)

    data, _resolved = read_workbook(notes)
    csv_sums, csv_projects = read_project_csv(notes)
    if not data:
        raise RuntimeError("no HIC rows parsed from the workbook")

    # ---- per-CoC-year metric rows -------------------------------------------------------
    n_bed = n_unit = n_dv = n_total = n_ratio = n_proj = 0
    years = sorted({y for (_c, y) in data})
    newest = max(years)
    per_year: dict[int, dict[str, int]] = {}

    def put(entity_type, entity_id, metric, value, unit, year, is_est, note, src):
        upsert(conn, "metric", {
            "entity_type": entity_type, "entity_id": entity_id, "metric": metric,
            "value": value, "unit": unit, "year": year, "is_est": is_est,
            "note": note, "source_id": src,
        }, ["entity_type", "entity_id", "metric", "year"])

    # PIT totals are the denominator of the capacity ratio; read them once.
    pit_total: dict[tuple[str, int], int] = {}
    try:
        for r in conn.execute("SELECT coc_num, year, total FROM pit WHERE total IS NOT NULL"):
            pit_total[(r[0], r[1])] = r[2]
    except sqlite3.OperationalError:
        notes.append("pit table unreadable; beds-per-homeless ratio skipped")

    for (coc, year), rec in sorted(data.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        vals = {k: v for k, v in rec.items() if v is not None}
        if not vals:
            continue
        ytot = per_year.setdefault(year, {})
        for metric, key, unit, is_est, note in PLAIN_METRICS:
            v = rec.get(key)
            if v is None:
                continue
            put("coc", coc, metric, v, unit, year, is_est, note, src_xlsx)
            if unit == "beds":
                n_bed += 1
                ytot[metric] = ytot.get(metric, 0) + v
            else:
                n_unit += 1
                ytot[metric] = ytot.get(metric, 0) + v

        # total year-round residential beds across every program HUD lists for that year
        tot = None
        for p in PROGRAMS:
            v = rec.get("beds_" + p)
            if v is not None:
                tot = (tot or 0) + v
        if tot is not None:
            put("coc", coc, "hic_beds_total", tot, "beds", year, 0,
                "sum of HUD's published year-round bed totals for ES, TH, SH, RRH, PSH and "
                "OPH (plus 2014-2016 demonstration projects where HUD listed them)", src_xlsx)
            n_total += 1
            ytot["hic_beds_total"] = ytot.get("hic_beds_total", 0) + tot

        # DV beds: published total minus published non-DV total, per program
        dv_tot = None
        for p in PROGRAMS:
            b, nd = rec.get("beds_" + p), rec.get("nondv_" + p)
            if b is None or nd is None:
                continue
            dv = b - nd
            if dv < 0:
                notes.append(f"{coc} {year} {p}: non-DV {nd} > total {b}, DV cell skipped")
                continue
            dv_tot = (dv_tot or 0) + dv
        if dv_tot is not None:
            put("coc", coc, "hic_beds_dv", dv_tot, "beds", year, 0,
                DV_NOTE + "; summed over the programs HUD lists for that year", src_xlsx)
            n_dv += 1
            ytot["hic_beds_dv"] = ytot.get("hic_beds_dv", 0) + dv_tot

        # derived capacity ratio against the same-year PIT count
        if tot is not None:
            homeless = pit_total.get((coc, year))
            if homeless:
                put("coc", coc, "hic_beds_per_homeless", round(tot / homeless, 4), "ratio",
                    year, 1,
                    "derived: HIC year-round beds (ES+TH+SH+RRH+PSH+OPH) divided by the HUD "
                    "PIT total homeless count for the same CoC and year", src_xlsx)
                n_ratio += 1

    # ---- projects per CoC (newest year, project-level inventory file) ---------------------
    for coc, n in csv_projects.items():
        if (coc, newest) not in data:
            continue
        put("coc", coc, "hic_projects", n, "projects", newest, 0,
            f"HUD-listed residential project inventory records (current inventory period) in "
            f"the HIC project file for {newest}; types ES/TH/SH/RRH/PSH/OPH", src_csv)
        n_proj += 1

    # ---- national sums (entity_type='us') ------------------------------------------------
    n_us = 0
    for year, ytot in sorted(per_year.items()):
        for metric, v in ytot.items():
            put("us", "US", metric, v, "beds" if not metric.startswith("hic_units") else "units",
                year, 0, "sum of the per-CoC values ingested in this domain for the same year",
                src_xlsx)
            n_us += 1

    # ---- cross-check the workbook against the project-level inventory (newest year) ------
    if csv_sums:
        xcheck = []
        for p in ("es", "th", "sh", "rrh", "psh", "oph"):
            bad = nmatch = 0
            for (coc, year), rec in data.items():
                if year != newest or rec.get("beds_" + p) is None:
                    continue
                nmatch += 1
                s = csv_sums.get(coc, {})
                if abs(s.get("yr_" + p, 0) - rec["beds_" + p]) > 0:
                    bad += 1
            xcheck.append(f"{p}:{nmatch - bad}/{nmatch}")
        dv_bad = dv_n = 0
        for (coc, year), rec in data.items():
            if year != newest:
                continue
            wb_dv = 0
            for p in PROGRAMS:
                b, nd = rec.get("beds_" + p), rec.get("nondv_" + p)
                if b is not None and nd is not None:
                    wb_dv += b - nd
            dv_n += 1
            if abs(wb_dv - csv_sums.get(coc, {}).get("dv_yr", 0)) > 0:
                dv_bad += 1
        notes.append(f"cross-check {newest} workbook vs project-level inventory, CoCs "
                     "agreeing exactly: " + " ".join(xcheck) + f" dv:{dv_n - dv_bad}/{dv_n}")
    n_cocs_newest = sum(1 for (c, y) in data if y == newest)
    notes.append(f"{len(years)} years {min(years)}-{newest}, {n_cocs_newest} CoCs in {newest}, "
                 f"{len({c for (c, _y) in data})} distinct CoCs, "
                 f"{len(PLAIN_METRICS)} published series + total/DV/ratio")

    rows = n_bed + n_unit + n_dv + n_total + n_ratio + n_proj + n_us
    return {
        "rows": rows,
        "notes": "; ".join(notes)[:4000],
        "sources": [(URL_HIC_XLSX, "public domain"), (URL_HIC_CSV, "public domain"),
                    (URL_INDEX, "public domain")],
        "_detail": {"beds": n_bed, "units": n_unit, "dv": n_dv, "total": n_total,
                    "ratio": n_ratio,
                    "projects": n_proj, "us": n_us, "years": years},
    }


# --------------------------------------------------------------------------------- selftest
def _selftest(conn):
    print("metric rows written by this domain:")
    for r in conn.execute("""SELECT metric, COUNT(*) n, MIN(year) y0, MAX(year) y1
                             FROM metric WHERE metric LIKE 'hic\\_%' ESCAPE '\\'
                             GROUP BY metric ORDER BY metric"""):
        print(f"  {r['metric']:<26} {r['n']:>7}  {r['y0']}-{r['y1']}")
    tot = conn.execute("SELECT COUNT(*) FROM metric WHERE metric LIKE 'hic%'").fetchone()[0]
    print(f"total hic metric rows: {tot}")
    print("3 random rows:")
    for r in conn.execute("""SELECT * FROM metric WHERE metric LIKE 'hic%'
                             ORDER BY RANDOM() LIMIT 3"""):
        print("  ", dict(r))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    conn = db()
    t0 = time.time()
    selftest = "--selftest" in argv
    try:
        res = run(conn, {})
        conn.commit()
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                     (DOMAIN, now(), res["rows"], res["notes"][:2000]))
        conn.commit()
        print(f"[{DOMAIN}] OK rows={res['rows']} {time.time()-t0:.1f}s")
        print("  detail:", res.get("_detail"))
        print("  notes:", res["notes"])
        if selftest:
            _selftest(conn)
    except Exception as e:
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,0)",
                     (DOMAIN, now(), 0, repr(e)[:2000]))
        conn.commit()
        raise


if __name__ == "__main__":
    main()
