#!/usr/bin/env python3
"""WTF (Where's The Funds?) flyers for every county, built from atlas data.

One letter-size, black-and-white page that puts a county's homelessness money next to what
people can see on the street, and gives them one phone number to call. The design, the copy
limits and the checks come from the WTF flyer kit; /flyers.html on the site has the guidelines.

  python3 scripts/flyer.py 42049                 out/flyers/42049-erie-pa.{json,html,pdf,png}
  python3 scripts/flyer.py 42049 06037 --out d   several counties, into d/
  python3 scripts/flyer.py 42049 --json          print the county file only
  python3 scripts/flyer.py --from my.json        render a hand-edited county file
  python3 scripts/flyer.py 42049 --force         render even when the checks complain

Without data/atlas.db the county file is read from the live site (/api/flyer/<fips>.json).
PDF and PNG need Google Chrome or Chromium.

Where each number comes from, for a county that is its own Continuum of Care (or holds whole
CoCs, like Los Angeles County):
  money      USASpending.gov obligations for federal homelessness programs performed in the
             county, latest fiscal year with any (metric federal_homeless_awards)
  counts     HUD point-in-time counts for the county's CoC(s) (table pit)
  beds       HUD Housing Inventory Count: permanent supportive, rapid rehousing and other
             permanent housing beds (metrics hic_beds_psh, _rrh, _oph)
  income     Census ACS five-year median household income for the county
  call       the county research file's verified office line (for: [call], else the office
             answerable for coc_funding or county_budget), else the U.S. Representative's office
  help       the research file's first verified help line, else 211
Most counties share a CoC with their neighbours. HUD does not count by county, so their flyer
covers the whole CoC area and says so ("this 9-county area"); money and counts then cover the
same counties. A verified research-file claim can bring a newer local count (local_pit_total,
local_pit_unsheltered) or HUD's own CoC award total (local_coc_award_total) onto the flyer.
Nothing on a flyer is an estimate. Every number is printed in the sources line with its year.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("ATLAS_DB", ROOT / "data" / "atlas.db"))
FONTS = ROOT / "site_src" / "fonts"
QRJS = ROOT / "site_src" / "qrcode.js"
BASE_URL = "https://homeless.sybilsolutions.ai"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]

# Copy limits from the kit. They keep every county's flyer readable from across the street.
LIMITS = {"look_line": 16, "look_sub": 125, "paid_label": 66, "hero_note": 28, "punch_line": 50,
          "chart_title": 66, "chart_note": 100, "scale_line": 215, "call_who": 40, "call_say": 85,
          "then_line": 140, "help_line": 22, "qr_label": 24, "sources": 470}
REQUIRED = ["county", "state", "month", "qr_url", "qr_label", "look_line", "look_sub", "paid_label",
            "hero_amount", "hero_note", "punch_line", "stats", "chart_title", "chart_years", "chart_values",
            "chart_note", "scale_line", "call_label", "call_number", "call_who", "call_say", "then_line",
            "help_line", "help_number", "help_digits", "sources"]
PLACEHOLDERS = ("YourCounty", "YEAR", "Month Year", "000-000-0000", "official in charge", "list every source")
SUFFIXES = (" City and Borough", " Census Area", " Municipality", " Borough", " Parish", " County", " city")


# ------------------------------------------------------------------ small helpers
def rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def has_table(conn, name):
    return bool(rows(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def short_name(name):
    for s in SUFFIXES:
        if name.endswith(s) and len(name) > len(s):
            return name[: -len(s)]
    return name


def city_cap(name):
    """Census writes Virginia's independent cities and Baltimore as 'Richmond city'; print 'Richmond City'."""
    return name[:-5] + " City" if name.endswith(" city") else name


def local_name(conn, fips, name, st):
    """The short name people use ('Erie'), unless it would be ambiguous: a county that shares its
    short name with another place in the state (Baltimore County and Baltimore City), and every
    independent city, keep the full name."""
    short = short_name(name)
    if name.endswith(" city"):
        return city_cap(name)
    twins = [r for r in rows(conn, "SELECT name FROM geo_city WHERE kind='county' AND state=? AND geoid<>?", (st, fips))
             if short_name(r["name"]) == short]
    return name if twins else short


def phone_fmt(p):
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else ""


def dollars(v):
    return f"${int(math.floor(v)):,}"


def dollars_short(v):
    """Rounded down, never up: $3,478,959 is '$3.4 million'."""
    v = math.floor(v)
    if v >= 1e9:
        return f"${math.floor(v / 1e8) / 10:g} billion"
    if v >= 1e8:
        return f"${math.floor(v / 1e6):,} million"
    if v >= 1e6:
        return f"${math.floor(v / 1e5) / 10:g} million"
    if v >= 1e4:
        return f"${math.floor(v / 1e3) * 1000:,}"
    return f"${v:,}"


def round_down_2sig(n):
    n = int(n)
    if n < 100:
        return n
    p = 10 ** (len(str(n)) - 2)
    return n // p * p


def plain_len(s):
    return len(html.unescape(re.sub(r"<[^>]+>", "", str(s))))


def fit(*options, limit):
    """First option that fits its copy limit."""
    for o in options:
        if o and plain_len(o) <= limit:
            return o
    return options[-1]


def source_name(conn, sid):
    if sid is None:
        return None
    r = rows(conn, "SELECT url, publisher, title FROM source WHERE id=?", (sid,))
    return r[0] if r else None


# ------------------------------------------------------------------ the area a flyer covers
SWITCHBOARD = "202-224-3121"
TOLERANCE = 0.02  # money and counts must cover the same people to within 2%
_LINKS: dict = {}


def _links(conn):
    """county -> {coc: people}, coc -> {county: people}, from the tract crosswalk (cached per connection)."""
    key = id(conn)
    if key not in _LINKS:
        by_c, by_k = {}, {}
        for r in rows(conn, "SELECT county_fips, coc_num, pop_in FROM county_coc WHERE coc_num<>'(none)' AND pop_in>0"):
            by_c.setdefault(r["county_fips"], {})[r["coc_num"]] = r["pop_in"]
            by_k.setdefault(r["coc_num"], {})[r["county_fips"]] = r["pop_in"]
        _LINKS[key] = (by_c, by_k)
    return _LINKS[key]


def area_for(conn, fips):
    """{'kind', 'cocs', 'counties', 'mismatch'} or None.

    HUD counts by Continuum of Care; USASpending places money in counties. A flyer may only set
    them side by side when both cover the same people. Start from the county and keep adding the
    county or CoC that holds the most people on one side but not the other, until the two sides
    differ by at most TOLERANCE of the people counted. 'county' means the county alone already
    matches (its own CoC, or CoCs that lie wholly inside it, like Los Angeles County)."""
    by_c, by_k = _links(conn)
    if fips not in by_c:
        return None
    C, K = {fips}, set()
    while True:
        # Close over whatever already lies (almost) wholly inside the area, like Pasadena's CoC
        # inside Los Angeles County: adding it can only shrink the difference.
        grew = True
        while grew and K:
            grew = False
            for c in {x for k in K for x in by_k[k]} - C:
                if sum(n for k, n in by_c[c].items() if k in K) >= 0.95 * sum(by_c[c].values()):
                    C.add(c); grew = True
            for k in {x for c in C for x in by_c[c]} - K:
                if sum(n for c, n in by_k[k].items() if c in C) >= 0.95 * sum(by_k[k].values()):
                    K.add(k); grew = True
        miss, worst = 0, None
        for c in C:
            for k, n in by_c.get(c, {}).items():
                if k not in K:
                    miss += n
                    if not worst or n > worst[0]:
                        worst = (n, "k", k)
        for k in K:
            for c, n in by_k.get(k, {}).items():
                if c not in C:
                    miss += n
                    if not worst or n > worst[0]:
                        worst = (n, "c", c)
        counted = sum(sum(by_k[k].values()) for k in K)
        if (K and miss <= TOLERANCE * counted) or worst is None:
            break
        (K if worst[1] == "k" else C).add(worst[2])
    return {"kind": "county" if C == {fips} else "region", "cocs": sorted(K), "counties": sorted(C),
            "mismatch": round(miss / counted, 4) if counted else 0}


def area_label(conn, fips, area, st):
    """('Fulton and DeKalb counties', 'counties'), ('Butler and 16 nearby counties', 'counties'),
    ('Fairfax and 2 nearby counties and cities', ...). The noun follows what the places are."""
    names = {r["geoid"]: r["name"] for r in rows(
        conn, f"SELECT geoid, name FROM geo_city WHERE kind='county' AND geoid IN ({','.join('?' * len(area['counties']))})",
        area["counties"])}
    others = [names.get(c, c) for c in area["counties"] if c != fips]
    mine = names.get(fips, fips)

    def noun_for(group):
        cities = sum(1 for n in group if n.endswith(" city"))
        if st == "AK":
            return "boroughs and census areas"
        if st == "LA":
            return "parishes"
        return "cities" if group and cities == len(group) else "counties and cities" if cities else "counties"
    noun = noun_for([mine, *others])  # what the whole area is: "these 3 counties and cities"
    me = local_name(conn, fips, mine, st)
    if len(others) == 1:
        other_fips = next(c for c in area["counties"] if c != fips)
        them = local_name(conn, other_fips, others[0], st)
        for suf, plural in ((" County", "counties"), (" Parish", "parishes")):
            if mine.endswith(suf) and others[0].endswith(suf) and me == short_name(mine) and them == short_name(others[0]):
                return f"{me} and {them} {plural}", noun
        return f"{mine if me == short_name(mine) else me} and {city_cap(others[0])}", noun
    return f"{me} and {len(others)} nearby {noun_for(others)}", noun


def pit_series(conn, cocs):
    """{year: {total, unsheltered, source_id}} summed over the CoCs, only years all of them report."""
    by: dict[int, list] = {}
    for c in cocs:
        rs = rows(conn, "SELECT year, total, unsheltered, source_id FROM pit WHERE coc_num=? AND total IS NOT NULL", (c,))
        outside = {int(r["year"]): r["unsheltered"] for r in rs}
        for r in rs:
            # HUD let CoCs skip the 2021 street count (COVID); its file shows those as 0 outside.
            # A 2021 zero between nonzero 2020 or 2022 counts is a skipped count, not an empty street.
            if int(r["year"]) == 2021 and r["unsheltered"] == 0 and ((outside.get(2020) or 0) > 0 or (outside.get(2022) or 0) > 0):
                r = {**r, "unsheltered": None}
            by.setdefault(int(r["year"]), []).append(r)
    out = {}
    for y, rs in by.items():
        if len(rs) != len(cocs):
            continue
        uns = None if any(r["unsheltered"] is None for r in rs) else sum(int(r["unsheltered"]) for r in rs)
        out[y] = {"total": sum(int(r["total"]) for r in rs), "unsheltered": uns, "source_id": rs[0]["source_id"]}
    return out


def local_claim(conn, fips, metric):
    """Newest verified research-file claim for this county, or None."""
    r = rows(conn, """SELECT value, year, source_id FROM metric WHERE entity_type='county' AND entity_id=?
                      AND metric=? ORDER BY year DESC LIMIT 1""", (fips, metric))
    return r[0] if r and r[0]["value"] is not None else None


def money_for(conn, area):
    """The area's federal homelessness obligations in the latest fiscal year the atlas holds. An older
    year printed beside a current count would mislead, and a net of zero or less (de-obligations)
    is no answer to "how much came here", so both mean no flyer."""
    latest = rows(conn, "SELECT MAX(year) y FROM metric WHERE entity_type='county' AND metric='federal_homeless_awards'")
    if not latest or latest[0]["y"] is None:
        return None
    qs = ",".join("?" * len(area["counties"]))
    rs = rows(conn, f"""SELECT year, SUM(value) v, MIN(source_id) sid FROM metric WHERE entity_type='county'
                        AND metric='federal_homeless_awards' AND year=? AND entity_id IN ({qs}) GROUP BY year""",
              [latest[0]["y"], *area["counties"]])
    return rs[0] if rs and (rs[0]["v"] or 0) > 0 else None


def beds_for(conn, cocs):
    """Permanent housing beds (PSH + RRH + OPH) in the latest HIC year all the CoCs report."""
    if not cocs:
        return None
    qs = ",".join("?" * len(cocs))
    rs = rows(conn, f"""SELECT year, SUM(value) v, COUNT(DISTINCT entity_id) n, MIN(source_id) sid FROM metric
                        WHERE entity_type='coc' AND entity_id IN ({qs})
                        AND metric IN ('hic_beds_psh','hic_beds_rrh','hic_beds_oph') GROUP BY year
                        HAVING COUNT(DISTINCT entity_id)=? ORDER BY year DESC LIMIT 1""", [*cocs, len(cocs)])
    return rs[0] if rs and rs[0]["v"] else None


def call_for(conn, fips):
    """(number, who-line, source) for the one phone number on the flyer."""
    if has_table(conn, "local_sources_responsible"):
        rs = rows(conn, """SELECT office, role, title, phone, for_keys, url, elected FROM local_sources_responsible
                           WHERE county_fips=? AND phone_ok=1 AND phone IS NOT NULL""", (fips,))

        def rank(r):
            keys = set((r.get("for_keys") or "").split(","))
            return (0 if "call" in keys else 1 if "coc_funding" in keys else 2 if "county_budget" in keys else 9,
                    0 if r.get("elected") else 1)
        # Only an office that answers for this money: the contributor's pick (call), then whoever
        # runs the Continuum of Care's funding, then the county budget. Others fall through.
        for r in sorted((r for r in rs if rank(r)[0] < 9), key=rank):
            num = phone_fmt(r["phone"])
            if not num:
                continue
            who = fit(f"Ask for the {r.get('title')}. Say:" if r.get("title") else "",
                      f"Ask for the {r.get('office')}. Say:", "Ask who handles this money. Say:", limit=LIMITS["call_who"])
            return num, who, "research:" + urlparse(r.get("url") or "").netloc.removeprefix("www.")
    if has_table(conn, "officials_county"):
        rs = rows(conn, """SELECT p.phone, oc.share FROM officials_county oc JOIN officials_person p ON p.id=oc.person_or_office_id
                           WHERE oc.county_fips=? AND oc.level='federal_house' ORDER BY oc.share DESC""", (fips,))
        if rs and (rs[0]["share"] or 0) >= 0.95 and phone_fmt(rs[0]["phone"]):
            return phone_fmt(rs[0]["phone"]), "Your U.S. Representative's office. Say:", "house_office"
    # The county is split between House districts (or none is on file): the U.S. Capitol switchboard
    # connects to every member's office. Published on house.gov/representatives/find-your-representative.
    return SWITCHBOARD, "Ask for your U.S. Representative. Say:", "house_switchboard"


def help_for(conn, fips):
    if has_table(conn, "local_sources_help"):
        rs = rows(conn, """SELECT name, what, phone FROM local_sources_help WHERE county_fips=? AND phone_ok=1
                           AND phone IS NOT NULL ORDER BY rowid""", (fips,))
        rs.sort(key=lambda r: 0 if re.search(r"shelter|bed", f"{r['name']} {r['what']}", re.I) else 1)
        for r in rs:
            num = phone_fmt(r["phone"])
            if num:
                return num, r["name"]
    return "211", None


def law_name(conn, state):
    if not has_table(conn, "records_law"):
        return None
    rs = rows(conn, "SELECT data_json FROM records_law WHERE jurisdiction=?", (state,))
    try:
        return (json.loads(rs[0]["data_json"]) or {}).get("law_name") if rs else None
    except ValueError:
        return None


# ------------------------------------------------------------------ the county file
def flyer_data(conn, fips, month=None):
    """(county file dict in the kit's format, [problems]). Problems mean no flyer is made."""
    c = rows(conn, "SELECT geoid, name, state FROM geo_city WHERE kind='county' AND geoid=?", (fips,))
    if not c:
        return None, ["unknown county"]
    name, st = c[0]["name"], c[0]["state"]
    short = local_name(conn, fips, name, st)
    area = area_for(conn, fips)
    if not area:
        return None, ["no Continuum of Care covers this county in the crosswalk"]
    series = pit_series(conn, area["cocs"])
    count_src = "HUD"
    if area["kind"] == "county":
        lt, lu = local_claim(conn, fips, "local_pit_total"), local_claim(conn, fips, "local_pit_unsheltered")
        if lt and lu and int(lu["year"]) == int(lt["year"]) and series and int(lt["year"]) > max(series):
            series[int(lt["year"])] = {"total": int(lt["value"]),
                                       "unsheltered": int(lu["value"]),
                                       "source_id": lt["source_id"]}
            count_src = "local"
    # In some years (2021, for COVID) many CoCs counted only people in shelters. HUD's "total" for
    # such a year leaves out everyone outside, so charting it next to full counts would show a
    # false drop. Leave out every year without a street count, and say so in the sources line.
    skipped = sorted(y for y, r in series.items() if r["unsheltered"] is None)
    if any(r["unsheltered"] is not None for r in series.values()):
        series = {y: r for y, r in series.items() if r["unsheltered"] is not None}
    else:
        skipped = []
    if len(series) < 4:
        return None, ["fewer than 4 years of point-in-time counts"]
    money = money_for(conn, area)
    if not money:
        return None, ["no federal homelessness money for this area in the latest fiscal year"]
    fy, amount = int(money["year"]), float(money["v"])
    if fy < max(series) - 1:
        return None, [f"money year {fy} is more than a year behind the {max(series)} count"]
    hero_src = "usaspending"
    if area["kind"] == "county":
        hud = local_claim(conn, fips, "local_coc_award_total")
        if hud and int(hud["year"]) >= fy:
            fy, amount, hero_src = int(hud["year"]), float(hud["value"]), "hud_award"
    income = local_claim(conn, fips, "median_household_income")
    call = call_for(conn, fips)
    help_num, help_name = help_for(conn, fips)
    beds = beds_for(conn, area["cocs"])

    years = sorted(series)[-9:]
    last = years[-1]
    cur = series[last]
    n_cty = len(area["counties"])
    region = area["kind"] == "region"
    label, noun = area_label(conn, fips, area, st) if region else (city_cap(name), "counties")
    hero = dollars(amount)
    month = month or datetime.now(timezone.utc).strftime("%B %Y")

    # the count, and how it moved over three years
    base_year = last - 3 if (last - 3) in series else years[0]
    b = series[base_year]["total"]
    if b:
        ch = (cur["total"] - b) / b * 100
        move = (f"Up {math.floor(ch)}% since {base_year}." if ch >= 1 else
                f"Down {math.floor(-ch)}% since {base_year}." if ch <= -1 else f"About the same as in {base_year}.")
    else:
        move = ""
    count_text = f"Counted on one night in January {last}." + (f" {move}" if move else "")
    if region:
        count_text = f"Counted across {n_cty} {noun} one night in January {last}." + (f" {move}" if move else "")
    stats = [[f"{cur['total']:,}", "still homeless", count_text]]
    u = cur.get("unsheltered")
    if u is None:
        return None, ["the latest count has no street count"]
    if u:
        f_year = years[0]
        f_u = series[f_year].get("unsheltered")
        peak_year = max((y for y in years if series[y].get("unsheltered") is not None), key=lambda y: (series[y]["unsheltered"], -y))
        if f_u is not None and f_year != last:
            t = f"There were {f_u:,} in {f_year}."
            if peak_year not in (last, f_year):
                t += f" It peaked at {series[peak_year]['unsheltered']:,} in {peak_year}."
            elif peak_year == last:
                t += f" This is the most since at least {f_year}."
        else:
            t = f"Counted outside on one night in January {last}."
        stats.append([f"{u:,}", "sleeping outside", t])
    else:
        stats.append([f"{cur['total']:,}", "in a shelter tonight", "Shelters, transitional housing and safe havens. Not a home."])

    look_sub = (fit("" if region else f"Tonight, someone will sleep outside in {short}. No bed. No heat. No bathroom. That is our neighbor.",
                    "Tonight, someone near here will sleep outside. No bed. No heat. No bathroom. That is our neighbor.",
                    limit=LIMITS["look_sub"]) if u else
                fit("" if region else f"Tonight, someone in {short} will sleep in a shelter, not a home. That is our neighbor.",
                    "Tonight, someone near here will sleep in a shelter, not a home. That is our neighbor.",
                    limit=LIMITS["look_sub"]))
    paid = (fit(f"In {fy}, {short} got this much of your tax money for homelessness:",
                f"In {fy}, {short} got this much federal money for homelessness:",
                f"Federal homelessness money here in {fy}:", limit=LIMITS["paid_label"]) if not region else
            fit(f"In {fy}, {label} got this much for homelessness:",
                f"Homelessness money for {label} in {fy}:",
                f"In {fy}, these {n_cty} {noun} got this much for homelessness:",
                f"Federal homelessness money here in {fy}:", limit=LIMITS["paid_label"]))
    hero_note = "HUD awards. One year." if hero_src == "hud_award" else "Federal grants. One year."
    # A shared area's money is not all spent on this street, so a shared-area flyer asks how much came here.
    punch = (fit(f"How much of {dollars_short(amount)} reached {short}?", f"How much of {dollars_short(amount)} reached us?",
                 limit=LIMITS["punch_line"]) if region else
             fit(f"Does this street look like {dollars_short(amount)}?", "Does this street look like it?", limit=LIMITS["punch_line"]))
    chart_title = fit(f"Homeless in {city_cap(name)}, counted one night each January" if not region else
                      f"Homeless in {label}, counted each January",
                      f"Homeless in these {n_cty} {noun}, counted one night each January",
                      "Homeless here, counted one night each January", limit=LIMITS["chart_title"])
    if beds:
        chart_note = fit(f"{'This area' if region else short} also has about {round_down_2sig(beds['v']):,} beds in housing programs. "
                         f"People in them are not in this count.",
                         f"There are about {round_down_2sig(beds['v']):,} beds in housing programs here. They are not in this count.",
                         limit=LIMITS["chart_note"])
    else:
        chart_note = "This is one night's count. People already in housing programs are not in it."
    if income and income["value"]:
        yrs_pay = math.floor(amount / float(income["value"]))
        scale = fit(f"A typical household in {html.escape(city_cap(name), quote=False)} earns <b>{dollars(income['value'])}</b> a year. "
                    f"{'The area' + chr(39) + 's' if region else 'This'} one year of grants equals <b>{yrs_pay:,} years</b> of that pay.",
                    f"A typical household here earns <b>{dollars(income['value'])}</b> a year. "
                    f"This money equals <b>{yrs_pay:,} years</b> of that pay.", limit=LIMITS["scale_line"])
    else:
        yrs_pay, scale = None, ""
    call_num, call_who, call_office = call
    say = (fit(f"I live in {city_cap(name)}. How much of the {hero} came here? How many did it house?",
               f"I live in {short}. How much of the {hero} came here? How many did it house?",
               f"I live here. How much of the {hero} came here? How many did it house?",
               f"I live here. How much of the {dollars_short(amount)} came here? How many did it house?",
               limit=LIMITS["call_say"]) if region else
           fit(f"I live in {city_cap(name)}. Where did the {hero} go? How many people did it house?",
               f"I live in {short}. Where did the {hero} go? How many people did it house?",
               f"I live here. Where did the {hero} go? How many people did it house?",
               f"I live here. Where did the {dollars_short(amount)} go? How many people did it house?",
               limit=LIMITS["call_say"]))
    law = law_name(conn, st)
    lead = "<b>Call on a weekday. It takes two minutes. They work for you.</b> No answer? "
    law_h = html.escape(law, quote=False) if law else ""
    then = fit(f"{lead}Put it in writing: file a {law_h} request. Scan the code." if law else "",
               f"{lead}File a {law_h} request. Scan the code." if law else "",
               f"{lead}Put it in writing: file a public records request. Scan the code.", limit=LIMITS["then_line"])

    # sources, with every year, and the arithmetic
    s = []
    s.append(f"HUD Continuum of Care awards, {fy}" if hero_src == "hud_award" else
             f"USASpending.gov, federal homelessness grants for work in {label}, fiscal year {fy}")
    pit_label = "HUD Point-in-Time counts" + (f", {years[0]} to {last if count_src == 'HUD' else years[-2]}")
    gaps = [y for y in skipped if years[0] < y < last]
    if gaps:
        pit_label += f" ({' and '.join(map(str, gaps))} left out: no street count)"
    s.append(f"{pit_label}, " + (", ".join(area["cocs"]) if len(area["cocs"]) <= 4 else f"{len(area['cocs'])} Continuums of Care"))
    if count_src == "local":
        src = source_name(conn, series[last]["source_id"]) or {}
        s.append(f"{src.get('publisher') or 'Local count'}, {last}")
    if beds:
        s.append(f"HUD Housing Inventory Count, {beds['year']}")
    if income:
        s.append(f"U.S. Census Bureau ACS {int(income['year']) - 4} to {int(income['year'])}")
    if call_office == "house_office":
        s.append("Phone: the House member's office line, congress-legislators project")
    elif call_office == "house_switchboard":
        s.append("Phone: U.S. House switchboard, house.gov")
    elif call_office.startswith("research:"):
        s.append(f"Phone: as published at {call_office.split(':', 1)[1]}" if call_office != "research:" else
                 "Phone: the office's own published line")
    math_line = f" {hero} &divide; {dollars(income['value'])} = {yrs_pay:,}." if income and yrs_pay is not None else ""
    sources = "Sources: " + ". ".join(html.escape(p, quote=False) for p in s) + "." + math_line

    d = {
        "county": html.escape(short, quote=False), "state": st, "month": month,
        "qr_url": f"{BASE_URL}/county/{fips}.html", "qr_label": "Scan to see the records",
        "look_line": "Look around.", "look_sub": html.escape(look_sub, quote=False),
        "paid_label": html.escape(paid, quote=False), "hero_amount": hero, "hero_note": hero_note,
        "punch_line": html.escape(punch, quote=False), "stats": stats,
        "chart_title": html.escape(chart_title, quote=False),
        "chart_years": [str(y) for y in years], "chart_values": [series[y]["total"] for y in years],
        "chart_note": html.escape(chart_note, quote=False), "scale_line": scale,
        "call_label": "Call", "call_number": call_num, "call_who": html.escape(call_who, quote=False),
        "call_say": html.escape(say, quote=False), "then_line": then,
        "help_line": "Need a bed tonight?", "help_number": help_num, "help_digits": "",
        "sources": sources,
        "_meta": {"fips": fips, "county_name": name, "area": area, "money_source": hero_src,
                  "count_source": count_src, "help_from": help_name or "211", "call_from": call_office,
                  "area_label": label},
    }
    if not scale:
        return d, ["no median household income for the scale line"]
    return d, problems(d)


def problems(d):
    """The kit's checks: the mistakes that make a flyer weak or easy to dismiss."""
    out = [f"missing field: {k}" for k in REQUIRED if k not in d]
    if len(d.get("chart_years", [])) != len(d.get("chart_values", [])):
        out.append("chart_years and chart_values must be the same length")
    if not 4 <= len(d.get("chart_values", [])) <= 10:
        out.append("chart needs 4 to 10 years")
    if not max(d.get("chart_values") or [0]):
        out.append("chart has no counts above zero")
    if len(d.get("stats", [])) != 2:
        out.append("stats needs exactly 2 items")
    for k, n in LIMITS.items():
        if plain_len(d.get(k, "")) > n:
            out.append(f"{k} is {plain_len(d.get(k, ''))} characters; keep it to {n}")
    if d.get("hero_amount") in ("$0", None):
        out.append("hero_amount is $0")
    text = json.dumps({k: v for k, v in d.items() if k != "_meta"})
    out += [f'placeholder text still in the file: "{w}"' for w in PLACEHOLDERS if w in text]
    return out


BANNED = re.compile(r"\b(stolen|steal\w*|fraud\w*|corrupt\w*|embezzl\w*)\b", re.I)
DASHES = re.compile("[\u2013\u2014]")
PHONE = re.compile(r"^(\d{3}-\d{3}-\d{4}|211)$")


def _money_of(text):
    m = re.search(r"\$([\d.,]+) (million|billion)", text)
    return float(m.group(1).replace(",", "")) * (1e6 if m.group(2) == "million" else 1e9) if m else None


def broken_rules(d):
    """The kit's "Never" list as checks. A flyer that breaks one is not published, anywhere."""
    bad = []
    meta = d.get("_meta") or {}
    area = meta.get("area") or {}
    plain = html.unescape(re.sub(r"<[^>]+>", "", " ".join(str(v) for k, v in d.items() if k != "_meta")))
    if area.get("mismatch", 0) > TOLERANCE:
        bad.append(f"money and counts cover different people: {area['mismatch']:.1%} apart, over {TOLERANCE:.0%}")
    if d["stats"][0][0] != f"{d['chart_values'][-1]:,}":
        bad.append("the big count is not the chart's last bar")
    amount = int(d["hero_amount"].strip("$").replace(",", ""))
    short = _money_of(d["punch_line"])
    if short is not None and short > amount:
        bad.append(f"the punch line rounds up: {d['punch_line']} for {d['hero_amount']}")
    if area.get("kind") == "region" and "street look like" in d["punch_line"]:
        bad.append("a shared area's money is put on this street")
    m = re.search(r"\$([\d,]+) &divide; \$([\d,]+) = ([\d,]+)\.", d["sources"])
    if not m:
        bad.append("the sources line lacks the years-of-pay division")
    else:
        a, b, q = (int(x.replace(",", "")) for x in m.groups())
        if a != amount or q != math.floor(a / b) or f"{q:,} years" not in d["scale_line"]:
            bad.append("the years-of-pay arithmetic does not match the scale line")
    yr = re.search(r"\b(20\d\d)\b", d["paid_label"])
    if yr and yr.group(1) not in d["sources"]:
        bad.append(f"the money year {yr.group(1)} is not in the sources line")
    if hit := BANNED.search(plain):
        bad.append(f"a word that needs a public finding: {hit.group(0)}")
    if DASHES.search(plain):
        bad.append("an em or en dash in the copy")
    for k in ("call_number", "help_number"):
        if not PHONE.match(d[k]):
            bad.append(f"{k} is not a phone number: {d[k]}")
    if meta.get("fips") and not d["qr_url"].endswith(f"/county/{meta['fips']}.html"):
        bad.append("the QR code does not point at this county's page")
    return bad


# ------------------------------------------------------------------ the page
CSS = """
@page{size:8.5in 11in;margin:0}
*{margin:0;padding:0;box-sizing:border-box}
@media print{html{zoom:.64}body{zoom:1!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}.tools{display:none!important}}
body{width:1275px;height:1650px;background:#fff;color:#000;position:relative;overflow:hidden;
  font-family:'Inter',Helvetica,Arial,sans-serif;font-size:18px;line-height:1.4}
.d{font-family:'Anton',Impact,'Arial Narrow',sans-serif;font-weight:400;text-transform:uppercase;line-height:.92;white-space:nowrap}
b{font-weight:800}
.top{background:#000;color:#fff;height:290px;padding:0 80px;display:flex;justify-content:space-between;align-items:center}
.lock{display:flex;align-items:stretch;gap:30px;height:210px}
.wtf{font-size:244px;line-height:210px;letter-spacing:.01em;display:block;height:210px}
.tag{display:flex;flex-direction:column;justify-content:space-between;height:210px;padding:0}
.tag span{display:block;font-size:74px;line-height:64px;letter-spacing:.015em}
.badge{background:#fff;color:#000;width:184px;padding:9px 9px 8px;text-align:center}
.badge svg{display:block;margin:0 auto}
.badge .c{margin-top:5px;letter-spacing:.03em;height:34px;display:flex;align-items:center;justify-content:center}
.badge .c span{display:inline-block}
.badge .s{font-size:13px;font-weight:800;margin-top:3px}
.main{padding:28px 80px 0}
.look{display:inline-block}
.looksub{font-size:25.5px;line-height:1.32;margin-top:8px;max-width:1000px;text-wrap:balance}
.paid{margin-top:20px;font-size:22px;font-weight:800}
.hero{display:inline-block;letter-spacing:-.005em}
.heronote{font-size:22px;font-weight:800;margin-top:2px;text-align:right}
.punch{border-top:7px solid #000;border-bottom:7px solid #000;margin-top:12px;padding:14px 0 11px;text-align:center}
.punch span{display:inline-block}
.proof{display:grid;grid-template-columns:236px 236px 1fr;gap:30px;margin-top:22px}
.stat .n{display:inline-block}
.stat .l{font-size:21px;font-weight:800;margin-top:4px}
.stat .t{font-size:17px;line-height:1.34;margin-top:3px}
.ct{font-size:16px;font-weight:800}
.chart{display:flex;gap:9px;align-items:flex-end;margin-top:10px;border-bottom:3px solid #000}
.col{flex:1;display:flex;flex-direction:column}
.col .v{font-size:15px;font-weight:800;text-align:center;margin-bottom:3px}
.col .bar{background:#000}
.yrs{display:flex;gap:9px;margin-top:4px}
.yrs div{flex:1;text-align:center;font-size:13.5px;color:#333}
.cn{font-size:15.5px;line-height:1.34;color:#222;margin-top:6px}
.scale{text-wrap:pretty;font-size:19px;line-height:1.4;margin-top:16px;padding-top:12px;border-top:1.5px solid #000}
.act{position:absolute;left:0;right:0;bottom:150px;height:204px;background:#000;color:#fff;padding:18px 80px 0}
.row{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:36px;align-items:center}
.callbox{min-width:0}
.call{font-size:124px;display:inline-block}
.sayit{font-size:20.5px;line-height:1.3}
.sayit b{display:block;font-size:17px;margin-bottom:4px}
.then{font-size:17px;line-height:1.35;margin-top:10px;padding-top:9px;border-top:1.5px solid #fff;white-space:nowrap;overflow:hidden}
.bed{position:absolute;left:80px;right:80px;bottom:92px;height:58px;display:flex;align-items:center;justify-content:space-between}
.bed .q{font-size:38px}
.bed .num{font-size:38px}
.bed .dig{font-family:'Inter',sans-serif;font-weight:800;font-size:23px;text-transform:none;margin-left:12px;letter-spacing:0}
.foot{position:absolute;left:80px;right:80px;bottom:40px;height:52px;border-top:1.5px solid #000;padding-top:6px;
  font-size:12px;line-height:1.3;color:#333}
.tools{position:fixed;right:16px;bottom:16px;z-index:9;display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;
  font:600 15px/1 system-ui,sans-serif;zoom:var(--unzoom,1)}
.tools a,.tools button{background:#000;color:#fff;border:2px solid #fff;outline:2px solid #000;padding:10px 14px;
  text-decoration:none;cursor:pointer;font:inherit}
"""

JS = """
function fit(el){
  var p = el.parentElement, cs = getComputedStyle(p);
  var width = p.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  el.style.fontSize = '100px';
  var w = el.getBoundingClientRect().width;
  el.style.fontSize = Math.min(+el.dataset.max, Math.floor(1000 * width / w) / 10) + 'px';
}
function wtfQR(box){
  var url = box.dataset.url, size = +box.dataset.size, q;
  try { q = qrcode(6, 'H'); q.addData(url); q.make(); } catch (e) { q = qrcode(0, 'H'); q.addData(url); q.make(); }
  var n = q.getModuleCount(), hole = Math.round(n * 0.26) | 1, lo = (n - hole) >> 1, hi = lo + hole, r = '';
  for (var y = 0; y < n; y++) for (var x = 0; x < n; x++)
    if (q.isDark(y, x) && !(x >= lo && x < hi && y >= lo && y < hi)) r += '<rect x="' + x + '" y="' + y + '" width="1.02" height="1.02"/>';
  var c = n / 2;
  box.innerHTML = '<svg viewBox="-2 -2 ' + (n + 4) + ' ' + (n + 4) + '" width="' + size + '" height="' + size +
    '" shape-rendering="crispEdges" role="img" aria-label="QR code to ' + url + '"><g fill="#000">' + r + '</g>' +
    '<rect x="' + (lo + .5) + '" y="' + (lo + .5) + '" width="' + (hole - 1) + '" height="' + (hole - 1) + '" fill="#000"/>' +
    '<text x="' + c + '" y="' + (c + .3) + '" text-anchor="middle" dominant-baseline="central" fill="#fff" ' +
    'font-family="Anton,Impact,sans-serif" font-size="' + (hole * .5).toFixed(2) + '">WTF</text></svg>';
}
document.querySelectorAll('[data-qr]').forEach(wtfQR);
document.fonts.ready.then(function(){
  document.querySelectorAll('[data-max]').forEach(fit);
  // the line under the phone number stays on one line: shrink it a little rather than wrap into the bed band
  document.querySelectorAll('.then').forEach(function(t){
    var px = 17;
    while (t.scrollWidth > t.clientWidth && px > 13) { px -= .5; t.style.fontSize = px + 'px'; }
  });
  var ns = Array.from(document.querySelectorAll('.stat .n'));
  var size = Math.min.apply(null, ns.map(function(n){ return parseFloat(n.style.fontSize); }));
  ns.forEach(function(n){ n.style.fontSize = size + 'px'; });
  if (document.querySelector('.tools')) {
    var s = Math.min(1, window.innerWidth / 1275);
    if (s < 1) { document.body.style.zoom = s; document.body.style.setProperty('--unzoom', 1 / s); }
  }
  document.body.dataset.ready = '1';
});
"""


def _font_css(embed):
    faces = [("Anton", "anton.woff2", 400), ("Inter", "inter-400.woff2", 400),
             ("Inter", "inter-700.woff2", 700), ("Inter", "inter-800.woff2", 800)]
    out = []
    for fam, f, w in faces:
        src = (f"data:font/woff2;base64,{base64.b64encode((FONTS / f).read_bytes()).decode()}" if embed else f"/assets/fonts/{f}")
        out.append(f"@font-face{{font-family:'{fam}';font-weight:{w};font-display:block;src:url({src}) format('woff2')}}")
    return "".join(out)


def build_html(d, site=False):
    """The flyer page. site=True links fonts and the QR script from /assets and adds the screen-only
    toolbar; otherwise everything is inlined so the file renders alone (PDF and PNG)."""
    e = html.escape
    top = max(d["chart_values"])
    bars = "".join(f'<div class="col"><div class="v">{v:,}</div><div class="bar" style="height:{round(v / top * 104)}px"></div></div>'
                   for v in d["chart_values"])
    yrs = "".join(f"<div>{e(str(y))}</div>" for y in d["chart_years"])
    stats = "".join(f'<div class="stat"><div><span class="n d" data-max="108">{n}</span></div><div class="l">{lab}</div>'
                    f'<div class="t">{t}</div></div>' for n, lab, t in d["stats"])
    meta = d.get("_meta") or {}
    fips = meta.get("fips", "")
    place = f'{e(meta.get("county_name") or (d["county"] + " County"))}, {e(d["state"])}. {e(d["month"])}.'
    qr = (f'<div data-qr data-url="{e(d["qr_url"])}" data-size="166" style="width:166px;height:166px;margin:0 auto"></div>')
    qrlib = '<script src="/assets/qrcode.js"></script>' if site else f"<script>{QRJS.read_text()}</script>"
    head = ('<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            if site else "")
    tools = (f'<div class="tools"><button type="button" onclick="window.print()">Print</button>'
             f'<a href="/flyers.html">How to post it</a><a href="/api/flyer/{e(fips)}.json">County file</a>'
             f'<a href="/county/{e(fips)}.html">Back</a></div>' if site else "")
    help_dig = f'<span class="dig">{d["help_digits"]}</span>' if d.get("help_digits") else ""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">{head}
<title>WTF: Where's The Funds? {e(d["county"])}, {e(d["state"])}</title>
<style>{_font_css(not site)}{CSS}</style></head><body>
<div class="top">
  <div class="lock"><span class="wtf d">WTF</span><div class="tag d"><span>Where's</span><span>The</span><span>Funds?</span></div></div>
  <div class="badge">{qr}<div class="c"><span class="d" data-max="36">{e(d["county"])}</span></div><div class="s">{d["qr_label"]}</div></div>
</div>
<div class="main">
  <div><span class="look d" data-max="112">{d["look_line"]}</span></div>
  <p class="looksub">{d["look_sub"]}</p>
  <p class="paid">{d["paid_label"]}</p>
  <div><span class="hero d" data-max="300">{d["hero_amount"]}</span></div>
  <p class="heronote">{d["hero_note"]}</p>
  <div class="punch"><span class="d" data-max="76">{d["punch_line"]}</span></div>
  <div class="proof">{stats}
    <div><p class="ct">{d["chart_title"]}</p><div class="chart">{bars}</div><div class="yrs">{yrs}</div>
    <p class="cn">{d["chart_note"]}</p></div>
  </div>
  <p class="scale">{d["scale_line"]}</p>
</div>
<div class="act">
  <div class="row"><div class="callbox"><span class="call d" data-max="132">{d["call_label"]} {d["call_number"]}</span></div>
    <p class="sayit"><b>{d["call_who"]}</b>&ldquo;{d["call_say"]}&rdquo;</p></div>
  <p class="then">{d["then_line"]}</p>
</div>
<div class="bed"><div class="q d">{d["help_line"]}</div>
  <div class="num d">Call {d["help_number"]}{help_dig}</div></div>
<div class="foot">{place} {d["sources"]}</div>
{tools}
{qrlib}
<script>{JS}</script>
</body></html>"""


def no_flyer_html(fips, name, reasons):
    e = html.escape
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>No flyer yet: {e(name)}</title>
<style>body{{font:17px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 16px}}</style></head><body>
<h1>No flyer for {e(name)} yet</h1>
<p>A WTF flyer prints only numbers that trace to a public record. This county is missing: {e("; ".join(reasons))}.</p>
<p>Adding the county's research file fills most gaps: <a href="/county/{e(fips)}.html">back to {e(name)}</a>.</p>
</body></html>"""


# ------------------------------------------------------------------ command line
def chrome():
    for b in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if shutil.which(b):
            return shutil.which(b)
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(mac):
        return mac
    sys.exit("Install Google Chrome or Chromium to make the PDF and PNG. The .html and .json files are already written.")


def render(page, png, pdf):
    base = [chrome(), "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--virtual-time-budget=5000"]
    for extra in (["--window-size=1275,1650", "--force-device-scale-factor=2", f"--screenshot={png}"],
                  ["--no-pdf-header-footer", f"--print-to-pdf={pdf}"]):
        subprocess.run(base + extra + [page.as_uri()], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def from_live(fips):
    with urllib.request.urlopen(f"{BASE_URL}/api/flyer/{fips}.json", timeout=30) as r:
        return json.load(r)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Make WTF (Where's The Funds?) flyers from atlas data.",
                                 epilog="Read /flyers.html (or the kit's GUIDELINES) before you post one.")
    ap.add_argument("fips", nargs="*", help="5-digit county FIPS, e.g. 42049 for Erie County, PA")
    ap.add_argument("--from", dest="src", help="render a county file (JSON in the kit's format) instead")
    ap.add_argument("--out", default=str(ROOT / "out" / "flyers"), help="output folder (default out/flyers)")
    ap.add_argument("--json", action="store_true", help="print the county file and stop")
    ap.add_argument("--no-render", action="store_true", help="write .json and .html only, no PDF or PNG")
    ap.add_argument("--force", action="store_true", help="render even when the checks complain")
    a = ap.parse_args(argv)
    if not a.fips and not a.src:
        ap.error("give a county FIPS or --from a county file")
    jobs = []
    if a.src:
        d = json.loads(Path(a.src).read_text())
        jobs.append((Path(a.src).stem, d, problems(d)))
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True) if DB.exists() else None
    for f in a.fips:
        if not re.fullmatch(r"\d{5}", f):
            ap.error(f"{f} is not a 5-digit county FIPS")
        if conn:
            d, probs = flyer_data(conn, f)
        else:
            try:
                d = from_live(f)
                probs = problems(d)
            except Exception as ex:
                d, probs = None, [f"no local database and the live county file could not be read ({ex})"]
        slug = f"{f}-{re.sub(r'[^a-z0-9]+', '-', (d or {}).get('county', 'county').lower()).strip('-')}-{((d or {}).get('state') or '').lower()}"
        jobs.append((slug, d, probs))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    code = 0
    for slug, d, probs in jobs:
        for p in probs:
            print(f"  ! {slug}: {p}")
        rules = broken_rules(d) if d else []
        for r in rules:
            print(f"  ! {slug}: breaks a rule: {r}")
        if rules:  # --force never prints a flyer that breaks the kit's Never list
            print(f"  skipped {slug}. These rules hold even with --force.")
            code = 1
            continue
        if d is None or (probs and not a.force):
            print(f"  skipped {slug}." + (" Fix the items above, or add --force." if d else ""))
            code = 1
            continue
        if a.json:
            print(json.dumps(d, indent=1))
            continue
        page = out / f"{slug}.html"
        (out / f"{slug}.json").write_text(json.dumps(d, indent=1))
        page.write_text(build_html(d))
        if a.no_render:
            print(f"  wrote {page}")
            continue
        png, pdf = out / f"{slug}.png", out / f"{slug}.pdf"
        render(page.resolve(), png, pdf)
        print(f"  made {pdf}\n       {png}")
    return code


if __name__ == "__main__":
    sys.exit(main())
