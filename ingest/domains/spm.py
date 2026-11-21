"""spm — HUD System Performance Measures (SPM) per Continuum of Care, FY2015–FY2024.

What this domain answers: does the money change outcomes?  For every CoC and every year HUD has
published SPM data, this ingestor writes

  * the seven SPM measure families (length of time homeless, returns to homelessness,
    first-time homelessness, income growth, exits to permanent housing, PH retention,
    street-outreach outcomes) exactly as HUD published them, plus
  * the **CoC Program award amount** for that CoC-year (the money), from the same workbook, plus
  * derived ratios that put the two side by side (returns per 100 exits, dollars per exit,
    dollars per exit to permanent housing).

Source (fetched through `common.fetch`, cached in `data/raw/spm/`):

  https://files.hudexchange.info/resources/documents/System-Performance-Measures-Data.xlsx
      "CoC System Performance Measures Data Since FY 2015" — published January 2026 on
      https://www.hudexchange.info/resource/5691/system-performance-measures-data-since-fy-2015/
      2,530,373 bytes, one sheet per fiscal year, 2015 … 2024, one row per CoC, 46–96 columns.
      Column names change between sheets (HUD renamed blocks several times), so every column is
      resolved by its *sub-header text* and, where the text repeats (the six identical
      "Total Persons Returns in N mths" blocks), by its position inside the anchor block that
      starts with "Total Persons Exited (SO|ES|TH|SH|PH|SO+ES+TH+SH+PH)".

Everything written to `metric` has entity_type='coc', entity_id=<HUD CoC number as published in
that year's sheet>, year=<fiscal year>, and source_id = the workbook URL above.  Nothing is
interpolated, renamed or gap-filled: cells that HUD publishes as "NA" / "N/A" / "#N/A" / "*" become
absent rows, never zero.

CoC codes are *as published per year*: MO-604M (2015–2022) is the same physical CoC as MO-604
(2023–) after HUD's renumbering, and MO-604K (Kansas City/Wyandotte County) is a separate CoC that
exists in 2015–2016 and again in 2024.  No remapping is done, on purpose — the alternative would
silently move published numbers from one entity to another.

Metric names written (unit in brackets).  `is_est=1` marks the six derived rows at the bottom;
every other row is one published cell.  The widest sheets (FY2019-FY2024) carry 87 measure columns
and every one of them maps to exactly one metric name; FY2018 has 72 and FY2015-FY2017 have 42 (a
subset).  Anything the resolver could not map would be listed in the ingest note — nothing is
(the module also raises if a required column disappears from a sheet).

  SPM 1 - length of time homeless
    spm_avg_days_homeless [days]              ES+SH+TH average days homeless
    spm_median_days_homeless [days]           ES+SH+TH median days homeless
    spm_avg_days_es_sh [days]                 ES+SH average days homeless
    spm_median_days_es_sh [days]              ES+SH median days homeless
  bed coverage (the HMIS denominator HUD prints beside SPM 1)
    spm_beds_es_th_non_dv [beds]              non-DV ES+TH beds on that year's HIC
    spm_beds_hmis_es_th_non_dv [beds]         ... of which recorded on HMIS
    spm_bed_coverage_rate [ratio]             HMIS bed coverage, ES+TH combined
  SPM 2 - returns to homelessness (per exit system, 2018+; all-program block every year)
    spm_exits_count [persons]                 universe: persons exited (SO+ES+TH+SH+PH)
    spm_return_{6,12,24}mo_count / _rate      returns within 6 / 12 / 24 months, all programs
    spm_return_{6,12,24}mo_{count,rate}_{so,es,th,sh,ph}   the same, per exiting program
  SPM 3 - persons who became homeless for the first time
    spm_first_time_count [persons]            ES+SH+TH universe
    spm_first_time_incl_ph_count [persons]    ES+SH+TH+PH universe
    spm_hmis_count [persons]                  "Total HMIS Count" (persons in the HMIS universe)
  SPM 4 - employment and income growth
    spm_income_stayers_count [persons]        persons who stayed in a project all year
    spm_income_stayers_{earned,other,total}_{count,rate}    ... whose income rose
    spm_income_leavers_count [persons]        persons who left during the year
    spm_income_leavers_{earned,other,total}_{count,rate}    ... whose income rose
  SPM 7 - successful placement in or retention of permanent housing
    spm_so_exits_count [persons]              persons who exited street outreach
    spm_so_exit_temp_count [persons]          ... to temporary destinations
    spm_so_exit_perm_count [persons]          ... to permanent destinations
    spm_so_exit_perm_rate [ratio]             successful street-outreach outcome
    spm_exits_es_th_sh_rrh_count [persons]    universe: exits from ES+TH+SH+PH-RRH
    spm_exit_to_ph_count [persons]            ... of whom to permanent housing
    spm_exit_to_ph_rate [ratio]               successful exit rate  <-- "exit to PH rate"
    spm_ph_retention_count [persons]          persons who exited PH or stayed to period end
    spm_ph_retention_success_count [persons]  ... successful retention or exit
    spm_ph_retention_rate [ratio]             PH retention rate
  HMIS data quality (2019+ only) - how much of the exit universe has no destination recorded
    spm_hmis_dq_exits_{so,es,th,rrh,psh}          exits in the DQ universe
    spm_hmis_dq_unknown_count_{so,es,th,rrh,psh}  of those, unknown destination
    spm_hmis_dq_unknown_rate_{so,es,th,rrh,psh}   share unknown
  money
    spm_coc_award_amount [USD]                FY CoC Program award amount for that CoC-year
  derived (is_est=1, formula in `note`)
    spm_returns_per_100_exits [per 100 exits] 100 * return_6mo_count / exits_count
    spm_first_time_rate [ratio]               first_time_count / hmis_count
    spm_exit_not_ph_count / _rate             exits that were NOT to permanent housing
    spm_award_per_exit [USD]                  award / exits_count
    spm_award_per_exit_to_ph [USD]            award / exit_to_ph_count

Known gap, recorded rather than filled: **HUD does not publish a per-CoC count of the people who
leave to *homeless* destinations.**  The SPM workbook has the exit universe and the exits to
permanent housing, the temporary/permanent destination split for street outreach, and the count of
exits with *unknown* destination, but no homeless-destination column.  `spm_exit_not_ph_count`
(difference of two published cells) is therefore the closest published bound and its note says so;
it is not relabelled "exits to homelessness".

Idempotent: every row is an upsert on `metric`'s natural key (entity_type, entity_id, metric,
year).  Re-running rewrites the same rows.  Per-year progress is recorded in the private table
`spm_progress` for observability; a full sweep is one HTTP request plus 10 sheets (~4 s), so no
resume machinery is needed.
"""
from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import sys
import time

import openpyxl

from ingest.common import db, fetch, now, save_raw, source_id_for, upsert

DOMAIN = "spm"

SRC_PAGE = "https://www.hudexchange.info/resource/5691/system-performance-measures-data-since-fy-2015/"
SRC_XLSX = ("https://files.hudexchange.info/resources/documents/"
            "System-Performance-Measures-Data.xlsx")
SRC_TITLE = "CoC System Performance Measures Data Since FY 2015"
SRC_PUBLISHER = "U.S. Department of Housing and Urban Development, HUD Exchange"
SRC_LICENSE = "US Government work — public domain"

# hudexchange.info 404s the plain ingest user-agent; the very same public file is served to a
# browser UA (same finding as research/hud_pit.md and research/hic.md).
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

GROUP_RE = re.compile(r"^(spm \d{1,2}( \([a-z ]+\))?|bed coverage|hmis dq)$", re.I)
COC_RE = re.compile(r"^[A-Z]{2}-\d{3}[A-Z]?$")

# --------------------------------------------------------------------------- cell parsing
_NULLS = {"", "na", "n/a", "#n/a", "-", "--", ".", "*", "n.a."}


def nz(v) -> str:
    """Collapse whitespace in a header/cell string (headers carry embedded newlines)."""
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def num(v):
    """Cell -> float, or None.  HUD writes NA / N/A / #N/A / '*' for 'not published'."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.lower() in _NULLS:
        return None
    s = s.replace(",", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------- column resolution
def header_map(rows) -> dict[int, tuple[str, str]]:
    """(column index) -> (sub-header, group label) for the two header rows of a year sheet.

    HUD moves the sub-header text between row 1 and row 2 from year to year (2019/2020 split one
    block across both), so both rows are read and the group label ("SPM 1", "Bed Coverage",
    "HMIS DQ") is separated from the name that says what the column actually is.
    """
    a = [nz(c) for c in (rows[0] if len(rows) > 0 else [])]
    b = [nz(c) for c in (rows[1] if len(rows) > 1 else [])]
    out: dict[int, tuple[str, str]] = {}
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else ""
        y = b[i] if i < len(b) else ""
        grp, hdr = "", ""
        if x and GROUP_RE.match(x):
            grp, hdr = x, y
        elif y and GROUP_RE.match(y):
            grp, hdr = y, x
        elif x and y:
            # only the award column ever lands here (row 1 "HUD CoC Number", row 2 the amount)
            hdr = y if ("award" in y.lower() or re.search(r"\d{4}", y)) else x
        else:
            hdr = x or y
        if hdr:
            out[i] = (hdr, grp)
    return out


REQUIRED = {
    "SPM 1 (average days)": "es-sh-th avg (days)",
    "SPM 2 (exits universe)": "total persons exited (so+es+th+sh+ph)",
    "SPM 7 (exits to PH)": "total persons exiting es, th, sh, ph-rrh to permanent housing",
    "CoC award amount": "coc award amount",
}

# published single cells: lower-cased header -> (metric, unit)
ROLES = {
    "total hmis count": ("spm_hmis_count", "persons"),
    "es-sh avg (days)": ("spm_avg_days_es_sh", "days"),
    "es-sh median (days)": ("spm_median_days_es_sh", "days"),
    "es-sh-th avg (days)": ("spm_avg_days_homeless", "days"),
    "es-sh-th median (days)": ("spm_median_days_homeless", "days"),
    "total persons exiting es, th, sh, ph-rrh": ("spm_exits_es_th_sh_rrh_count", "persons"),
    "total persons exiting es, th, sh, ph-rrh to permanent housing": ("spm_exit_to_ph_count", "persons"),
    "percent with successful exit": ("spm_exit_to_ph_rate", "ratio"),
    "percent with successful es, th, sh, ph-rrh exit": ("spm_exit_to_ph_rate", "ratio"),
    "percent with successful retention or exit": ("spm_ph_retention_rate", "ratio"),
    "percent with successful ph retention or exit": ("spm_ph_retention_rate", "ratio"),
    "total persons exiting ph (but not including ph-rrh)": ("spm_ph_retention_count", "persons"),
    "total persons exiting ph or remaining in ph at end of reporting period (measure excludes ph-rrh)":
        ("spm_ph_retention_count", "persons"),
    "total persons exiting ph (but not including ph-rrh) residing in ph for 6mos or more or exiting to permanent housing":
        ("spm_ph_retention_success_count", "persons"),
    "total persons exited ph to permanent destinations or remained in ph for 6+ mos (measure excludes ph-rrh)":
        ("spm_ph_retention_success_count", "persons"),
    "total persons exiting street outreach": ("spm_so_exits_count", "persons"),
    "total persons exiting street outreach (so)": ("spm_so_exits_count", "persons"),
    "total persons exited to temporary housing": ("spm_so_exit_temp_count", "persons"),
    "total persons exited so to temporary destinations": ("spm_so_exit_temp_count", "persons"),
    "total persons exited to permanent housing": ("spm_so_exit_perm_count", "persons"),
    "total persons exited so to permanent destinations": ("spm_so_exit_perm_count", "persons"),
    "percent with successful so outcome": ("spm_so_exit_perm_rate", "ratio"),
    "es-sh-th 1st time homeless": ("spm_first_time_count", "persons"),
    "es-sh-th-ph 1st time homeless": ("spm_first_time_incl_ph_count", "persons"),
    "total stayers (persons)": ("spm_income_stayers_count", "persons"),
    "total stayers increased earned income": ("spm_income_stayers_earned_count", "persons"),
    "percent stayers increased earned income": ("spm_income_stayers_earned_rate", "ratio"),
    "total stayers increased non-employment cash income": ("spm_income_stayers_other_count", "persons"),
    "percent stayers increased non-employment cash income": ("spm_income_stayers_other_rate", "ratio"),
    "total stayers increased total income": ("spm_income_stayers_total_count", "persons"),
    "percent stayers increased total income": ("spm_income_stayers_total_rate", "ratio"),
    "total leavers (persons)": ("spm_income_leavers_count", "persons"),
    "total leavers increased earned income": ("spm_income_leavers_earned_count", "persons"),
    "percent leavers increased earned income": ("spm_income_leavers_earned_rate", "ratio"),
    "total leavers increased non-employment cash income": ("spm_income_leavers_other_count", "persons"),
    "percent leavers increased non-employment cash income": ("spm_income_leavers_other_rate", "ratio"),
    "total leavers increased total income": ("spm_income_leavers_total_count", "persons"),
    "percent leavers increased total income": ("spm_income_leavers_total_rate", "ratio"),
}

RETURN_BLOCK = {1: ("count", "Total Persons Returns in 6 mths"),
                2: ("count", "Total Persons Returns in 12 mths"),
                3: ("count", "Total Persons Returns in 24 mths"),
                4: ("rate", "Percent Returns in 6 mths"),
                5: ("rate", "Percent Returns in 12 mths"),
                6: ("rate", "Percent Returns in 24 mths")}
RETURN_ANCHOR = re.compile(r"^total persons exited \((so|es|th|sh|ph|so\+es\+th\+sh\+ph)\)$")
RETURN_METRIC = {  # program -> (count metric pattern, rate metric pattern)
    "so+es+th+sh+ph": ("spm_return_{n}mo_count", "spm_return_{n}mo_rate"),
    "so": ("spm_return_{n}mo_count_so", "spm_return_{n}mo_rate_so"),
    "es": ("spm_return_{n}mo_count_es", "spm_return_{n}mo_rate_es"),
    "th": ("spm_return_{n}mo_count_th", "spm_return_{n}mo_rate_th"),
    "sh": ("spm_return_{n}mo_count_sh", "spm_return_{n}mo_rate_sh"),
    "ph": ("spm_return_{n}mo_count_ph", "spm_return_{n}mo_rate_ph"),
}


def resolve_columns(hdr: dict[int, tuple[str, str]]):
    """-> (roles: {col: (metric, unit, header)}, blocks: [(anchor_col, program)], unmapped: set)"""
    roles, blocks, unmapped = {}, [], set()
    for i in sorted(hdr):
        header, _grp = hdr[i]
        hl = header.lower()
        if "coc award amount" in hl:
            roles[i] = ("spm_coc_award_amount", "USD", header)
        elif hl.startswith("total non-dv hmis beds on"):
            roles[i] = ("spm_beds_hmis_es_th_non_dv", "beds", header)
        elif hl.startswith("total non-dv beds on"):
            roles[i] = ("spm_beds_es_th_non_dv", "beds", header)
        elif "bed coverage percent on hmis" in hl:
            roles[i] = ("spm_bed_coverage_rate", "ratio", header)
        elif hl in ROLES:
            m, u = ROLES[hl]
            roles[i] = (m, u, header)
        elif RETURN_ANCHOR.match(hl):
            blocks.append((i, RETURN_ANCHOR.match(hl).group(1)))
        elif hl.startswith("hmis dq "):
            m = re.match(r"^hmis dq exits universe - (so|es|th|rrh|psh)$", hl)
            if m:
                roles[i] = (f"spm_hmis_dq_exits_{m.group(1)}", "persons", header)
                continue
            m = re.match(r"^hmis dq unknown destinations - (so|es|th|rrh|psh)$", hl)
            if m:
                roles[i] = (f"spm_hmis_dq_unknown_count_{m.group(1)}", "persons", header)
                continue
            m = re.match(r"^hmis dq - (so|es|th|rrh|psh) percent unknown destinations$", hl)
            if m:
                roles[i] = (f"spm_hmis_dq_unknown_rate_{m.group(1)}", "ratio", header)
                continue
            unmapped.add(header)
        elif hl in ("state", "continuum of care (coc)", "hud coc number",
                    "ahar part 1 coc category"):
            pass  # identity / label columns, not metrics
        else:
            unmapped.add(header)
    # positional resolution of the repeated returns blocks
    for anchor, prog in blocks:
        if prog not in RETURN_METRIC:
            continue
        got = 0
        for off, (kind, want) in RETURN_BLOCK.items():
            j = anchor + off
            if j not in hdr or not hdr[j][0].lower().startswith(want.lower()):
                break
            n = want.split("in ")[1].split(" ")[0]           # "6", "12", "24"
            m = RETURN_METRIC[prog][0 if kind == "count" else 1].format(n=n)
            roles[j] = (m, "persons" if kind == "count" else "ratio", hdr[j][0])
            got += 1
        if got == 6 and prog == "so+es+th+sh+ph":
            roles[anchor] = ("spm_exits_count", "persons", hdr[anchor][0])
    # headers consumed by the positional pass (the six identical returns blocks) are resolved
    consumed = {h for (h, _g) in hdr.values() if any(hh == h for _m, _u, hh in roles.values())}
    unmapped = {h for h in unmapped if h not in consumed}
    return roles, unmapped


# --------------------------------------------------------------------------- derived rows
DERIVED = ("spm_returns_per_100_exits", "spm_first_time_rate", "spm_exit_not_ph_count",
           "spm_exit_not_ph_rate", "spm_award_per_exit", "spm_award_per_exit_to_ph")


def derived(vals: dict) -> dict:
    """Derived ratios: metric -> (value, unit, note).  All carry is_est=1."""
    out = {}

    def put(metric, value, unit, note):
        if value is not None:
            out[metric] = (value, unit, note)

    ex = vals.get("spm_exits_count")
    r6 = vals.get("spm_return_6mo_count")
    if ex and r6 is not None and ex > 0:
        put("spm_returns_per_100_exits", round(100.0 * r6 / ex, 4), "per 100 exits",
            "derived: 100 * spm_return_6mo_count / spm_exits_count (both published cells, "
            "same CoC-year)")

    ft = vals.get("spm_first_time_count")
    hm = vals.get("spm_hmis_count")
    if ft is not None and hm and hm > 0:
        put("spm_first_time_rate", round(ft / hm, 6), "ratio",
            "derived: spm_first_time_count / spm_hmis_count (both published cells, same CoC-year)")

    ex7 = vals.get("spm_exits_es_th_sh_rrh_count")
    ph = vals.get("spm_exit_to_ph_count")
    if ex7 is not None and ph is not None:
        rest = ex7 - ph
        if rest >= 0:
            put("spm_exit_not_ph_count", rest, "persons",
                "derived: spm_exits_es_th_sh_rrh_count - spm_exit_to_ph_count = exits NOT to "
                "permanent housing (homeless, institutional, doubled-up, other or unknown "
                "destination). HUD publishes no per-CoC count of exits to homeless destinations.")
            if ex7 > 0:
                put("spm_exit_not_ph_rate", round(rest / ex7, 6), "ratio",
                    "derived: (spm_exits_es_th_sh_rrh_count - spm_exit_to_ph_count) / "
                    "spm_exits_es_th_sh_rrh_count; upper bound on the share of exits to "
                    "homeless or unknown destinations")

    award = vals.get("spm_coc_award_amount")
    if award is not None:
        if ex and ex > 0:
            put("spm_award_per_exit", round(award / ex, 2), "USD",
                "derived: spm_coc_award_amount / spm_exits_count = FY CoC Program dollars per "
                "person who exited (SO+ES+TH+SH+PH)")
        if ph and ph > 0:
            put("spm_award_per_exit_to_ph", round(award / ph, 2), "USD",
                "derived: spm_coc_award_amount / spm_exit_to_ph_count = FY CoC Program dollars "
                "per exit to permanent housing")
    return out


# --------------------------------------------------------------------------- main parse
def run(conn: sqlite3.Connection, cfg: dict) -> dict:
    conn.execute("""CREATE TABLE IF NOT EXISTS spm_progress (
                      year INTEGER PRIMARY KEY, sheet TEXT, cocs INTEGER, rows INTEGER,
                      cols_mapped INTEGER, done INTEGER, ts TEXT)""")

    fetch(SRC_PAGE, domain=DOMAIN, headers={"User-Agent": UA})
    blob = fetch(SRC_XLSX, domain=DOMAIN, headers={"User-Agent": UA}, force=bool(cfg.get("force")))
    path = save_raw(DOMAIN, "System-Performance-Measures-Data.xlsx", blob)
    # second, byte-identical copy under the name `source_id_for` looks for, so the `source` row
    # also gets bytes + sha256 for the provenance ledger
    save_raw(DOMAIN, hashlib.sha1(SRC_XLSX.encode()).hexdigest() + ".bin", blob)
    sid = source_id_for(conn, SRC_XLSX, DOMAIN, title=SRC_TITLE, publisher=SRC_PUBLISHER,
                        license=SRC_LICENSE)
    source_id_for(conn, SRC_PAGE, DOMAIN, title=SRC_TITLE + " (resource page)",
                  publisher=SRC_PUBLISHER, license=SRC_LICENSE)

    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    total_rows = 0
    per_year, unmapped_all = {}, {}
    neg_cells = []
    for sheet in wb.sheetnames:
        m = re.match(r"^(\d{4})$", sheet.strip())
        if not m:
            continue
        year = int(m.group(1))
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        hdr = header_map(rows[:2])
        roles, unmapped = resolve_columns(hdr)
        for u in unmapped:
            unmapped_all.setdefault(u, set()).add(year)
        lowered = [h.lower() for _i, (h, _g) in hdr.items()]
        for label, needle in REQUIRED.items():
            if not any(needle in h for h in lowered):
                raise RuntimeError(f"{year}: required column missing ({label}: {needle!r})")

        # columns holding identity + metric values, whatever their position
        idx_coc = next((i for i, (h, _g) in hdr.items() if h.lower() == "hud coc number"), None)
        if idx_coc is None:
            raise RuntimeError(f"{year}: no 'HUD CoC Number' column")

        n_rows = 0
        seen = set()
        for raw in rows[2:]:
            coc = nz(raw[idx_coc]) if idx_coc < len(raw) else ""
            if not coc or not COC_RE.match(coc):
                continue
            if coc in seen:                      # a CoC appearing twice in one sheet is a bug
                raise RuntimeError(f"{year}: duplicate CoC row {coc}")
            seen.add(coc)

            # metric -> (value, unit, note, is_est)
            cells = {}
            for col, (metric, unit, header) in roles.items():
                if col >= len(raw):
                    continue
                v = num(raw[col])
                if v is not None and v < 0:
                    # HUD's "no data" sentinel: -1 (and one -534.0 percent in FY2016 NY-511).
                    # Four such cells in ten years; stored as absent, never as a number.
                    neg_cells.append((year, coc, header, v))
                    v = None
                if v is None:
                    continue
                cells[metric] = (v, unit, f"SPM FY{year} workbook, column \"{header}\"", 0)
            for metric, (value, unit, note) in derived({m: c[0] for m, c in cells.items()}).items():
                cells[metric] = (value, unit, note, 1)

            for metric, (value, unit, note, is_est) in cells.items():
                upsert(conn, "metric", {
                    "entity_type": "coc", "entity_id": coc, "metric": metric,
                    "value": value, "unit": unit, "year": year, "is_est": is_est,
                    "note": note, "source_id": sid,
                }, keys=["entity_type", "entity_id", "metric", "year"])
                n_rows += 1
        upsert(conn, "spm_progress", {"year": year, "sheet": sheet, "cocs": len(seen),
                                      "rows": n_rows, "cols_mapped": len(roles),
                                      "done": 1, "ts": now()}, keys=["year"])
        conn.commit()
        per_year[year] = (len(seen), n_rows)
        total_rows += n_rows

    notes = ("years=" + ",".join(str(y) for y in sorted(per_year)) +
             "; cocs per year=" + ",".join(f"{y}:{per_year[y][0]}" for y in sorted(per_year)))
    notes += f"; negative/no-data cells dropped={len(neg_cells)}"
    if neg_cells:
        notes += " [" + "; ".join(f"{y}/{c}:{h}={v:g}" for y, c, h, v in neg_cells) + "]"
    if unmapped_all:
        notes += ("; unmapped columns: " +
                  ", ".join(f"{h} ({min(y)}-{max(y)})" for h, y in sorted(unmapped_all.items())))
    return {"rows": total_rows, "notes": notes,
            "sources": [(SRC_XLSX, SRC_LICENSE), (SRC_PAGE, SRC_LICENSE)],
            "_detail": per_year, "_unmapped": sorted(unmapped_all),
            "_saved": str(path)}


# --------------------------------------------------------------------------- selftest
def _selftest(conn):
    print("metric rows written by this domain:")
    for r in conn.execute("""SELECT metric, COUNT(*) n, MIN(year) y0, MAX(year) y1,
                                    SUM(is_est) est
                             FROM metric WHERE source_id IN
                               (SELECT id FROM source WHERE domain='spm')
                             GROUP BY metric ORDER BY metric"""):
        print(f"  {r['metric']:<34} {r['n']:>7}  {r['y0']}-{r['y1']}  derived={r['est']}")
    print("per-year coverage (spm_progress):")
    for r in conn.execute("SELECT year, cocs, rows, cols_mapped FROM spm_progress ORDER BY year"):
        print(f"  {r['year']}  cocs={r['cocs']:>4}  metric rows={r['rows']:>6}  "
              f"columns mapped={r['cols_mapped']}")
    print("3 random rows:")
    for r in conn.execute("""SELECT entity_type,entity_id,metric,value,unit,year,is_est,note
                             FROM metric WHERE source_id IN
                               (SELECT id FROM source WHERE domain='spm')
                             ORDER BY RANDOM() LIMIT 3"""):
        print("  ", dict(r))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    conn = db()
    t0 = time.time()
    selftest = "--selftest" in argv
    cfg = {"force": "--force" in argv}
    try:
        res = run(conn, cfg)
        conn.commit()
        conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                     (DOMAIN, now(), res["rows"], res["notes"][:2000]))
        conn.commit()
        print(f"[{DOMAIN}] OK rows={res['rows']} {time.time()-t0:.1f}s")
        print("  file:", res["_saved"])
        print("  detail:", res["_detail"])
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
