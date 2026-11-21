"""Public records request letters for one county.

    python3 -m ingest.records_requests 42049          # print the letters
    python3 -m ingest.records_requests 42049 --json   # the request dicts as JSON
    python3 -m ingest.records_requests 42049 --all    # every housing authority and CoC

Built from geo_city, county_recipient, award, officials_office (kind='pha'), county_coc,
geo_coc, local_sources_responsible (when present) and records_law (written by
`python3 -m ingest.domains.records_laws`). Reads the database read-only.

Rule: only a legal fact whose quote the verifier matched against the official text
(`verified: true` in records_law.data_json) may appear in a letter. An unverified fact is
left out, never guessed. Placeholders left for the requester are exactly
{{requester_name}}, {{requester_address}}, {{requester_email}} and {{date}}.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

from ingest.common import DB

FOOTER = "This is a template, not legal advice."
PLACEHOLDERS = ("{{requester_name}}", "{{requester_address}}", "{{requester_email}}", "{{date}}")
SINCE = "January 1, 2021"           # county_recipient covers FY2021 to FY2025
FEE_NOTICE = 25                     # dollars: ask to be told before fees above this
VA_CFDA = ("64.024", "64.033")      # VA homeless providers grant and per diem; SSVF
HHS_CFDA = ("93.150", "93.623")     # PATH; Runaway and Homeless Youth
LAW_FIELDS = ("law_name", "custodian_term", "who_may_request", "response", "written_required",
              "must_cite_statute", "form_required", "fees", "fee_waiver", "deemed_denial", "appeal",
              "letter_requirements", "fee_categories", "route_hud", "route_va", "route_hhs",
              "appeal_hud", "appeal_va", "appeal_hhs", "hud_coc_public_posting")


# --------------------------------------------------------------------------- db helpers
def connect_ro(path=DB) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _rows(conn, sql: str, args=()) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _law(conn, jur: str) -> dict | None:
    if not _has_table(conn, "records_law"):
        return None
    r = conn.execute("SELECT data_json FROM records_law WHERE jurisdiction=?", (jur,)).fetchone()
    return json.loads(r[0]) if r and r[0] else None


def _ok(law: dict | None, field: str) -> dict | None:
    """The field when the verifier matched its quote(s); otherwise None."""
    f = (law or {}).get(field)
    return f if isinstance(f, dict) and f.get("verified") is True else None


def _name_ok(law: dict | None) -> bool:
    """The law's name may be used: either it carries no quote (a label) or its quote verified."""
    d = (law or {}).get("law_name_detail")
    if not isinstance(d, dict):
        return bool((law or {}).get("law_name"))
    return d.get("verified") is True or ("quote" not in d and "quotes" not in d)


def _strip(f: dict) -> dict:
    return {k: v for k, v in f.items() if k not in ("verified", "via", "verified_at")}


# --------------------------------------------------------------------------- law summary
def law_summary(conn, state_abbr: str) -> dict | None:
    """Verified facts for one jurisdiction (two-letter state, 'DC' or 'US'), for a guide page.

    Unverified fields are listed by name in `unverified` and carry no values."""
    law = _law(conn, state_abbr.upper())
    if not law:
        return None
    fields, unverified, absent = {}, [], []
    for name in LAW_FIELDS:
        f = law.get(name)
        if name == "law_name":
            f = law.get("law_name_detail") or f
        if f is None:
            absent.append(name)
        elif isinstance(f, dict) and "verified" in f:
            if f["verified"]:
                fields[name] = _strip(f)
            else:
                unverified.append(name)
    return {"jurisdiction": state_abbr.upper(), "name": law.get("name"),
            "law_name": law.get("law_name") if _name_ok(law) else None,
            "citation": law.get("citation"), "statute_url": law.get("statute_url"),
            "basis": law.get("basis"), "basis_note": law.get("basis_note"),
            "publisher": law.get("publisher"),
            "deadline_calc": _deadline_calc(law, local=False),
            "fields": fields, "unverified": unverified, "not_in_law": absent}


# --------------------------------------------------------------------------- deadlines
def _deadline_calc(law: dict | None, local: bool = True) -> dict | None:
    """{days, day_type} when a verified numeric deadline applies, else None."""
    resp = _ok(law, "response")
    if not resp or not isinstance(resp.get("days"), int):
        return None
    if local and resp.get("scope") == "state_agencies":
        return None
    if resp.get("day_type") not in ("business", "calendar"):
        return None
    out = {"days": resp["days"], "day_type": resp["day_type"]}
    nr = resp.get("nonresident")
    if isinstance(nr, dict) and isinstance(nr.get("days"), int):
        out["nonresident"] = {"days": nr["days"], "day_type": nr.get("day_type")}
    return out


def _days(n: int, kind: str | None) -> str:
    unit = {"business": "business day", "calendar": "calendar day"}.get(kind or "", "day")
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _deadline(law: dict | None, local: bool) -> dict:
    resp = _ok(law, "response")
    calc = _deadline_calc(law, local)
    if not resp:
        return {"text": None, "verified": False, "calc": None, "quote_url": None}
    if resp.get("scope") == "state_agencies" and local:
        return {"text": "The fixed deadline in this law applies to state agencies only; local "
                        "bodies must respond in a reasonable time.", "verified": True,
                "calc": None, "quote_url": resp.get("quote_url")}
    if calc:
        text = f"{_days(calc['days'], calc['day_type'])} from {resp.get('clock_starts') or 'receipt'}"
        if calc.get("nonresident"):
            text += (f" ({_days(calc['nonresident']['days'], calc['nonresident']['day_type'])}"
                     " if you do not live in the state)")
    else:
        text = resp.get("note") or "No fixed number of days."
    return {"text": text, "verified": True, "calc": calc,
            "quote_url": resp.get("quote_url") or (resp.get("quotes") or [{}])[0].get("quote_url")}


# --------------------------------------------------------------------------- county facts
def _county(conn, fips: str) -> dict:
    r = conn.execute("SELECT name, state FROM geo_city WHERE geoid=? AND kind='county'",
                     (fips,)).fetchone()
    if not r:
        raise SystemExit(f"no county {fips} in geo_city")
    return {"fips": fips, "name": r[0], "state": r[1]}


def _core(county_name: str) -> list[str]:
    return [t for t in _tokens(county_name) if t not in ("county", "parish", "borough", "city",
                                                          "and", "census", "area", "municipality")]


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if t not in ("of", "the", "inc", "a")]


def _recipients(conn, fips: str) -> list[dict]:
    if not _has_table(conn, "county_recipient"):
        return []
    return _rows(conn, """SELECT recipient_name, recipient_uei, org_id, amount, period
                          FROM county_recipient WHERE county_fips=? ORDER BY rank""", (fips,))


def _award_agencies(conn, recips: list[dict]) -> dict[str, set]:
    """recipient_name -> set of (agency, cfda) from the award table, by UEI or org_id."""
    out: dict[str, set] = {}
    if not recips or not _has_table(conn, "award"):
        return out
    for r in recips:
        rows = _rows(conn, """SELECT DISTINCT agency, cfda FROM award
                              WHERE (recipient_uei=? AND recipient_uei IS NOT NULL)
                                 OR (org_id=? AND org_id IS NOT NULL)""",
                     (r["recipient_uei"], r["org_id"]))
        out[r["recipient_name"]] = {(x["agency"], x["cfda"]) for x in rows}
    return out


def _phas(conn, county: dict) -> list[dict]:
    if not _has_table(conn, "officials_office"):
        return []
    rows = _rows(conn, """SELECT id, name, phone, email, address, url, exec_name, source_id
                          FROM officials_office WHERE kind='pha' AND county_fips=?
                          ORDER BY name""", (county["fips"],))
    core = set(_core(county["name"]))

    def rank(p):
        t = set(_tokens(p["name"]))
        if core and core <= t and "county" in t:
            return 0
        if core and core <= t:
            return 1
        return 2
    return sorted(rows, key=lambda p: (rank(p), p["name"]))


def _cocs(conn, fips: str) -> list[dict]:
    if not _has_table(conn, "county_coc"):
        return []
    return _rows(conn, """SELECT c.coc_num, c.county_share, g.name, g.state
                          FROM county_coc c LEFT JOIN geo_coc g ON g.coc_num=c.coc_num
                          WHERE c.county_fips=? AND c.coc_num LIKE '__-%'
                          ORDER BY c.county_share DESC, c.coc_num""", (fips,))


def _records_officers(conn, fips: str) -> list[dict]:
    if not _has_table(conn, "local_sources_responsible"):
        return []
    out = []
    for r in _rows(conn, "SELECT * FROM local_sources_responsible WHERE county_fips=?", (fips,)):
        keys = {k.strip().lower() for k in (r.get("for_keys") or "").split(",")}
        if "records" in keys:
            out.append(r)
    return out


def _match_officer(officers: list[dict], kind: str, county: dict, agency_name: str = "") -> dict | None:
    core = set(_core(county["name"]))
    for o in officers:
        t = set(_tokens(o.get("office") or ""))
        if kind == "county":
            if "housing" in t or (core and not core <= t):
                continue
            if "city" in t and "county" not in t:
                continue
            return o
        if kind == "pha":
            a = set(_tokens(agency_name))
            if "housing" in t and (t == a or t <= a or a <= t):
                return o
    return None


def _apply_officer(agency: dict, o: dict | None) -> dict:
    if not o:
        agency["contact"] = "No published records officer yet"
        return agency
    name = o.get("name") if o.get("name_ok") == 1 else None
    title = o.get("title") or o.get("role")
    agency["contact"] = ", ".join(x for x in (name, title) if x) or "Records officer"
    if o.get("email_ok") == 1 and o.get("email"):
        agency["email"], agency["email_verified"] = o["email"], True
    if o.get("phone_ok") == 1 and o.get("phone"):
        agency["phone"] = o["phone"]
    if o.get("address_ok") == 1 and o.get("address"):
        agency["address"] = o["address"]
    if o.get("url"):
        agency["url"] = o["url"]
    agency["records_officer"] = {"office": o.get("office"), "source": "county research file"}
    return agency


# --------------------------------------------------------------------------- letter parts
def _who_cautions(law: dict | None) -> list[str]:
    who = _ok(law, "who_may_request")
    if not who:
        return ["Who may use this state's law is not verified here; check the statute before relying on it."]
    if who.get("caution"):
        return [who["caution"]]
    if who.get("value") in ("other", "state_residents_only") and who.get("explain"):
        return [f"Who may request: {who['explain']}"]
    return []


def _letter_lines(law: dict | None) -> tuple[list[str], list[str]]:
    """(statements for the letter, cautions) from verified letter_requirements."""
    lr = _ok(law, "letter_requirements")
    if not lr:
        return [], []
    lines, cautions = [], []
    for it in lr.get("items") or []:
        if it.get("text"):
            lines.append(it["text"])
        if it.get("caution"):
            cautions.append(it["caution"])
    return lines, cautions


def _title(s: str) -> str:
    small = {"of", "the", "and", "for", "a", "an", "to"}
    words = s.split(" ")
    return " ".join(w if i and w in small else
                    "-".join(p[:1].upper() + p[1:] for p in w.split("-"))
                    for i, w in enumerate(words))


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(items, 1))


def _signoff() -> str:
    return ("Thank you for your help.\n\nSincerely,\n\n{{requester_name}}\n{{requester_address}}\n"
            "{{requester_email}}")


def _header(agency: dict) -> str:
    lines = ["{{date}}", "", agency["name"]]
    if agency.get("contact") and agency["contact"] != "No published records officer yet":
        lines.append(agency["contact"])
    if agency.get("address"):
        lines.append(agency["address"])
    if agency.get("email"):
        lines.append(agency["email"])
    return "\n".join(lines)


def _law_ref(law: dict | None, jur: str) -> dict:
    return {"jurisdiction": jur, "name": (law or {}).get("law_name") if _name_ok(law) else None,
            "citation": (law or {}).get("citation"), "statute_url": (law or {}).get("statute_url"),
            "basis": (law or {}).get("basis")}


def _state_letter(law, jur, agency, subject, records, purpose):
    ref = _law_ref(law, jur)
    cust = _ok(law, "custodian_term")
    hello = f"Dear {_title(cust['value'])}:" if cust else "Dear Records Officer:"
    req_lines, _ = _letter_lines(law)
    name = ref["name"] or "your state's public records law"
    cite = f", {ref['citation']}," if ref["name"] and ref["citation"] else ""
    parts = [_header(agency), "", f"Re: {name} request: {subject}", "", hello, "",
             f"Under the {name}{cite} I request copies of the following records:", "",
             _numbered(records), ""]
    if req_lines:
        parts += [" ".join(req_lines), ""]
    parts.append(f"I am asking as a member of the public to understand {purpose}. "
                 "Please send the records electronically, as native files (such as spreadsheets) "
                 "where they exist or as searchable PDFs, by email to {{requester_email}}. "
                 f"If fees will be more than ${FEE_NOTICE}, please tell me the amount before you "
                 "do the work.")
    if _ok(law, "fee_waiver"):
        parts.append("\nI ask that you waive or reduce fees because these records concern public "
                     "spending on homelessness and housing, and my request is not for a commercial "
                     "purpose.")
    parts.append("\nIf you withhold any record or part of a record, please cite the specific "
                 "exemption for each item and release every part that can reasonably be separated.")
    dl = _deadline(law, local=True)
    if dl["calc"]:
        parts.append(f"\nI understand the law provides for a response within {dl['text']}.")
    parts += ["", _signoff()]
    return "\n".join(parts)


def _followup(law, jur, subject, agency_name, local=True, federal=False):
    ref = _law_ref(law, jur)
    name = ref["name"] or "the public records law"
    dl = _deadline(law, local)
    dd = _ok(law, "deemed_denial")
    ap = _ok(law, "appeal")
    lines = ["{{date}}", "", agency_name, "", f"Re: Follow-up: {subject}", "", "Hello,", "",
             f"I have not yet received a response to my {name} request about {subject}. "
             "A copy of my request is attached."]
    if dl["calc"]:
        lines.append(f"\nUnder {ref['citation']}, a response was due within {dl['text']}.")
    if dd:
        eff = dd.get("effect")
        if federal:
            lines.append("\nUnder 5 U.S.C. 552(a)(6)(C)(i), when an agency misses the time limit the "
                         "requester is treated as having exhausted administrative remedies.")
        elif eff == "approved":
            lines.append(f"\nUnder {ref['citation']}, when no determination is sent in time the "
                         "request is considered approved for records that are not exempt.")
        elif eff == "presumed_public":
            lines.append(f"\nUnder {ref['citation']}, if the agency does not ask the Attorney General "
                         "for a ruling in time, the information is presumed public and must be released.")
        else:
            tail = f", which allows me to appeal to the {ap['body']}" if ap and "court" not in (ap.get("body") or "").lower() else ""
            lines.append(f"\nUnder {ref['citation']}, a request that is not answered in time is "
                         f"treated as denied{tail}. I would much rather receive the records than appeal.")
    lines.append("\nPlease let me know when I can expect the records, or tell me if you need anything "
                 "from me to finish the request.")
    lines += ["", _signoff()]
    return {"subject": f"Follow-up: {subject}", "body": "\n".join(lines)}


def _appeal_state(law, jur, subject, agency_name):
    ref = _law_ref(law, jur)
    ap = _ok(law, "appeal")
    dd = _ok(law, "deemed_denial")
    if not ap:
        return {"body_name": None, "deadline_days": None, "deadline_day_type": None, "url": None,
                "how": None, "subject": None, "body": None,
                "note": "No appeal route is verified for this state here."}
    out = {"body_name": ap.get("body"), "deadline_days": ap.get("deadline_days"),
           "deadline_day_type": ap.get("day_type"), "url": ap.get("url"),
           "how": _how(ap), "note": ap.get("note")}
    if "court" in (ap.get("body") or "").lower() and not any(
            w in ap["body"].lower() for w in ("commission", "office", "attorney general", "council",
                                               "examiners", "ombudsman", "head", "chief")):
        out.update(subject=None, body=None,
                   note=((ap.get("note") or "") + " " if ap.get("note") else "") +
                   "In this state the remedy is a court case, not an appeal letter. The state's open "
                   "government office or a lawyer can explain the steps.")
        return out
    subj = f"Appeal: {subject}"
    name = ref["name"] or "public records law"
    when = ""
    if isinstance(ap.get("deadline_days"), int):
        when = f" I am filing within the {_days(ap['deadline_days'], ap.get('day_type'))} the law allows."
    why = ("denied my request, or did not respond in time, which the law treats as a denial"
           if dd and dd.get("effect") in (None, "denial") else "denied my request")
    lines = ["{{date}}", "", ap.get("body") or "Appeals officer", "", f"Re: {subj}", "",
             "Hello,", "",
             f"I appeal under the {name}, {ref['citation']}. On the date shown in the attached copy I "
             f"asked {agency_name} for records ({subject}). The agency {why}.{when}", "",
             "A copy of my request and any response I received are attached. The records describe "
             "how public money for homelessness and housing was spent, and I ask that you direct the "
             "agency to release them, withholding only what a specific exemption covers and "
             "releasing every part that can reasonably be separated.", "", _signoff()]
    out.update(subject=subj, body="\n".join(lines))
    return out


def _how(ap: dict) -> dict | None:
    if ap.get("how") == "email" and ap.get("email"):
        return {"method": "email", "value": ap["email"]}
    if ap.get("how") == "portal" and ap.get("portal"):
        return {"method": "portal", "value": ap["portal"]}
    if ap.get("how") == "mail":
        return {"method": "mail", "value": ap.get("address")}
    if ap.get("url"):
        return {"method": "see_url", "value": ap["url"]}
    return None


# --------------------------------------------------------------------------- builders
def _county_request(conn, county, law, recips, officers):
    st = county["state"]
    agency = {"name": f"{county['name']}, {st}", "kind": "county_government", "contact": None,
              "email": None, "email_verified": False, "address": None, "phone": None, "url": None}
    _apply_officer(agency, _match_officer(officers, "county", county))
    core = set(_core(county["name"]))
    names = [r["recipient_name"].rstrip(". ") for r in recips
             if not (core <= set(_tokens(r["recipient_name"])) and "county" in _tokens(r["recipient_name"]))][:6]
    who = f", including payments to {'; '.join(names)}" if names else ""
    records = [
        f"Grant agreements, amendments and subrecipient agreements for federal homelessness and "
        f"housing funds the county received from HUD (such as Emergency Solutions Grants and "
        f"Continuum of Care grants), from {SINCE} to the date of this request.",
        f"Records showing each payment of those funds to another organization (payee, amount, date "
        f"and purpose), from {SINCE} to the date of this request{who}.",
        "The county's Consolidated Annual Performance and Evaluation Reports (CAPER) and Emergency "
        f"Solutions Grants (Sage) reports filed with HUD for program years since {SINCE}.",
        "Monitoring reports, findings or corrective action letters about the organizations "
        f"receiving those funds, from {SINCE} to the date of this request.",
        "Summary counts (no names or personal details) of people served and of people who moved "
        f"into permanent housing through these programs, by year, since {SINCE}.",
    ]
    subject = f"homelessness grant records, {county['name']}"
    body = _state_letter(law, st, agency, subject, records,
                         "how federal homelessness funds were spent in the county")
    return _assemble(f"{county['fips']}-county", f"{county['name']} government", agency, law, st,
                     records, subject, body, local=True,
                     plain=f"This asks {county['name']} for the papers that show where its homeless-help "
                           "money went and how many people got a home, so you can see if it worked.",
                     extra_cautions=[] if agency.get("email") else [
                         f"No records officer is on file for {county['name']} yet. Look on the "
                         "county website for its open records officer or request form, and send "
                         "this letter there."])


def _pha_request(conn, county, law, pha, officers):
    st = county["state"]
    agency = {"name": pha["name"], "kind": "housing_authority", "contact": None,
              "email": pha.get("email"), "email_verified": False, "address": pha.get("address"),
              "phone": pha.get("phone"), "url": pha.get("url"), "hud_id": pha["id"].split(":")[-1]}
    o = _match_officer(officers, "pha", county, pha["name"])
    _apply_officer(agency, o)   # officer fields marked ok replace HUD's; the rest fall back
    records = [
        f"Monthly counts of Housing Choice Vouchers authorized and leased, from {SINCE} to the date "
        "of this request, including Emergency Housing Vouchers and other special-purpose vouchers.",
        "Waiting list statistics by month (number of applicants, number with a homeless preference, "
        f"number pulled from the list), from {SINCE} to the date of this request. No names or "
        "personal details.",
        "The current Administrative Plan and Admissions and Continued Occupancy Policy sections on "
        "preferences for people experiencing homelessness.",
        f"Agreements or memoranda of understanding with the local Continuum of Care or homeless "
        f"service providers in effect at any time since {SINCE}.",
    ]
    subject = f"voucher and waiting list records, {pha['name'].title()}"
    body = _state_letter(law, st, agency, subject, records,
                         "how housing assistance reaches people experiencing homelessness")
    cautions = []
    if agency.get("email") and not agency.get("email_verified"):
        cautions.append("The email above comes from HUD's housing authority contact list. It may not "
                        "be the records officer; ask the authority where records requests go.")
    return _assemble(f"{county['fips']}-pha-{agency['hud_id']}", f"{pha['name'].title()}", agency,
                     law, st, records, subject, body, local=True, extra_cautions=cautions,
                     plain="This asks the housing authority how many rent vouchers it has, how many "
                           "are used, and how long people wait, so you can see if people are getting homes.")


def _federal_request(conn, county, us, key, recips, aw, coc):
    route = _ok(us, f"route_{key}")
    if not route:
        return None
    agency_name = route["agency"]
    agency = {"name": agency_name, "kind": "federal_agency", "contact": "FOIA Office",
              "email": route.get("email"), "email_verified": bool(route.get("email")),
              "address": route.get("address"), "phone": route.get("phone"), "url": route.get("url"),
              "portal_url": route.get("portal")}
    place = f"{county['name']}, {county['state']}"
    if key == "hud":
        names = [r["recipient_name"] for r in recips
                 if any("Housing and Urban" in (a or "") for a, _ in aw.get(r["recipient_name"], ()))] \
            or [r["recipient_name"] for r in recips]
        names = names[:6]
        target = ("the following recipients: " + "; ".join(names)) if names else \
            f"recipients located in {place}"
        records = [
            f"Grant agreements and amendments for HUD homelessness awards (Continuum of Care and "
            f"Emergency Solutions Grants) to {target}, from {SINCE} to the date of this request.",
            "The most recent Annual Performance Reports or CAPER/Sage reports HUD received for those "
            "awards.",
            f"Monitoring letters, findings and corrective action correspondence HUD sent to those "
            f"recipients, from {SINCE} to the date of this request.",
        ]
        if coc:
            records.append(f"HUD's scoring or debriefing records for the {coc['coc_num']} "
                           f"({coc['name']}) Continuum of Care competition applications, FY 2023 "
                           "to the date of this request.")
        plain = (f"This asks the federal housing agency for the grant papers and reports for money "
                 f"sent to {county['name']}, so you can check what was promised and what was done.")
    elif key == "va":
        names = [r["recipient_name"] for r in recips
                 if any("Veterans" in (a or "") for a, _ in aw.get(r["recipient_name"], ()))
                 or "VETERAN" in r["recipient_name"].upper()][:4]
        target = "; ".join(names) if names else f"recipients located in {place}"
        records = [
            f"Grant agreements and amendments for Supportive Services for Veteran Families and Grant "
            f"and Per Diem awards to {target}, from {SINCE} to the date of this request.",
            "The most recent annual or quarterly performance reports VA received for those awards, "
            "with aggregate counts of veterans served and housed (no personal details).",
            f"VA monitoring or inspection reports on those grantees, from {SINCE} to the date of "
            "this request.",
        ]
        plain = ("This asks Veterans Affairs for the grant papers and results for local programs that "
                 "help homeless veterans, so you can see how many veterans got housed.")
    else:  # hhs
        names = [r["recipient_name"] for r in recips
                 if any(c in HHS_CFDA for _, c in aw.get(r["recipient_name"], ()))][:4]
        target = "; ".join(names) if names else f"recipients located in {place}"
        records = [
            f"Grant agreements and amendments for Projects for Assistance in Transition from "
            f"Homelessness (PATH) and Runaway and Homeless Youth awards to {target}, from {SINCE} "
            "to the date of this request.",
            "The most recent performance reports HHS received for those awards, with aggregate counts "
            "only (no personal details).",
        ]
        plain = ("This asks the federal health agency for the grant papers and results for local "
                 "homeless programs it pays for, so you can see what the money did.")
    subject = f"{key.upper()} homelessness grant records, {place}"
    body = _foia_letter(us, agency, subject, records, county)
    req = _assemble(f"{county['fips']}-{key}", f"{agency_name} (FOIA)", agency, us, "US", records,
                    subject, body, local=False, plain=plain, federal=True)
    req["delivery"] = {"method": "email" if agency["email"] else "portal",
                       "email": agency["email"], "portal_url": agency["portal_url"],
                       "address": agency["address"]}
    req["mailto_ok"] = bool(agency["email"])
    req["portal_url"] = agency["portal_url"]
    req["appeal"] = _appeal_federal(us, key, subject, agency_name)
    return req


def _foia_letter(us, agency, subject, records, county):
    ref = _law_ref(us, "US")
    dl = _deadline(us, local=False)
    parts = [_header(agency), "", f"Re: Freedom of Information Act request: {subject}", "",
             "Dear FOIA Officer:", "",
             f"Under the Freedom of Information Act ({ref['citation']}), I request copies of the "
             "following records:", "", _numbered(records), "",
             "For fee purposes, I am a member of the public making a non-commercial request (the "
             "\"all other requesters\" category)."]
    if _ok(us, "fee_waiver"):
        parts.append(
            "\nI request a fee waiver under 5 U.S.C. 552(a)(4)(A)(iii). Disclosure is in the public "
            "interest because it is likely to contribute significantly to public understanding of how "
            f"federal homelessness funds are used in {county['name']}, {county['state']}. I will share "
            "what I learn with the public, and the request is not in my commercial interest.")
    parts.append(f"\nIf fees will be more than ${FEE_NOTICE}, please tell me before you do the work. "
                 "Please send records electronically, as native files where they exist or as "
                 "searchable PDFs, to {{requester_email}}.")
    parts.append("\nIf you withhold any record or part of a record, please cite the specific FOIA "
                 "exemption for each item and release all reasonably segregable portions.")
    if dl["calc"]:
        parts.append(f"\nI understand FOIA provides for a determination within {dl['text']}.")
    parts += ["", _signoff()]
    return "\n".join(parts)


def _appeal_federal(us, key, subject, agency_name):
    ap = _ok(us, f"appeal_{key}")
    gen = _ok(us, "appeal")
    if not ap and not gen:
        return {"body_name": None, "deadline_days": None, "deadline_day_type": None, "url": None,
                "how": None, "subject": None, "body": None, "note": "No verified appeal route."}
    src = ap or {}
    days = src.get("deadline_days") if src else None
    dtype = src.get("day_type") if src else None
    note = src.get("note")
    if days is None and gen:
        note = ((note + " ") if note else "") + ("The statute requires an appeal period of at least "
                                                 "90 days after the adverse determination.")
    subj = f"FOIA appeal: {subject}"
    lines = ["{{date}}", "", src.get("body") or "FOIA Appeals Officer", agency_name, "",
             f"Re: {subj}", "", "Hello,", "",
             "This is an administrative appeal under 5 U.S.C. 552(a)(6)(A) of the agency's response "
             f"to my Freedom of Information Act request about {subject}. A copy of the request and the "
             "agency's response are attached; please match this appeal to the request by my name and "
             "the subject line.", "",
             "I ask that you release the records, withholding only what a specific exemption covers and "
             "releasing all reasonably segregable portions. If the appeal concerns fees, I ask that "
             "you grant the fee waiver I requested under 5 U.S.C. 552(a)(4)(A)(iii).", "", _signoff()]
    return {"body_name": src.get("body") or "head of the agency", "deadline_days": days,
            "deadline_day_type": dtype, "url": src.get("url"), "how": _how(src) if src else None,
            "note": note, "subject": subj, "body": "\n".join(lines)}


def _coc_request(conn, county, law, us, coc):
    st = county["state"]
    cname = coc["name"] or coc["coc_num"]
    cfull = cname if re.search(r"\b(CoC|Continuum of Care)\b", cname) else f"{cname} Continuum of Care"
    agency = {"name": f"{cfull} ({coc['coc_num']}), lead agency",
              "kind": "coc_lead", "contact": "No published records officer yet", "email": None,
              "email_verified": False, "address": None, "phone": None, "url": None}
    ref = _law_ref(law, st)
    posting = _ok(us, "hud_coc_public_posting")
    records = [
        f"The CoC-approved Consolidated Application (CoC Application and Project Priority Listing) "
        f"for {coc['coc_num']} for each HUD competition from FY 2023 to the date of this request.",
        "The CoC's written project ranking and review procedures and the scores given to each project "
        "in those competitions.",
        "The CoC's System Performance Measures reports and Housing Inventory Count submitted to HUD "
        "for 2023 to the date of this request (aggregate data only).",
        "Minutes of CoC board meetings where funding or ranking decisions were made, from FY 2023 to "
        "the date of this request.",
    ]
    subject = f"Continuum of Care records, {coc['coc_num']}"
    name = ref["name"] or "the state public records law"
    cite = f", {ref['citation']}" if ref["name"] and ref["citation"] else ""
    lines = [_header(agency), "", f"Re: Records request: {subject}", "", "Hello,", "",
             f"I am writing to ask for the following records of the {cfull} ({coc['coc_num']}):",
             "", _numbered(records), ""]
    if posting:
        lines.append("HUD's CoC competition instructions award points only when the CoC posts its "
                     "CoC-approved Consolidated Application on its own or a partner's website, so I "
                     "expect item 1 is already public; a link is enough.\n")
    lines.append(f"If your agency is a public body, please treat this as a request under the {name}{cite}. "
                 "Please send the records electronically, as native files where they exist or as "
                 f"searchable PDFs, to {{{{requester_email}}}}. If fees will be more than ${FEE_NOTICE}, "
                 "please tell me first. If you withhold anything, please tell me the reason and "
                 "release every part that can reasonably be separated.")
    req_lines, _ = _letter_lines(law)
    if req_lines:
        lines += ["", " ".join(req_lines)]
    lines += ["", _signoff()]
    req = _assemble(f"{county['fips']}-coc-{coc['coc_num']}", f"{cfull} lead agency",
                    agency, law, st, records, subject, "\n".join(lines), local=True,
                    plain="This asks the group that plans homeless help for your area for its funding "
                          "application and scores, so you can see how it picks which programs get money.",
                    extra_cautions=[
                        "The lead agency for this Continuum of Care is not in our data yet. Find it on "
                        "HUD's CoC contacts list or the CoC's website.",
                        "Many CoC lead agencies are nonprofits, which public records laws may not cover. "
                        "Asking politely still often works; the state deadline and appeal apply only if "
                        "the lead agency is a public body."])
    req["law"]["applies"] = "only if the lead agency is a public body"
    req["deadline_calc"] = None
    if req["deadline"]["text"]:
        req["deadline"]["text"] = "Only if the lead agency is a public body: " + req["deadline"]["text"]
    if req["appeal"]:
        req["appeal"]["note"] = ((req["appeal"].get("note") or "") +
                                 " Only if the lead agency is a public body.").strip()
    if posting:
        req["verified"]["hud_coc_public_posting"] = True
    return req


def _assemble(rid, title, agency, law, jur, records, subject, body, *, local, plain,
              extra_cautions=(), federal=False):
    ref = _law_ref(law, jur)
    dl = _deadline(law, local)
    req_lines, lr_cautions = _letter_lines(law)
    cautions = ([] if federal else _who_cautions(law)) + lr_cautions + list(extra_cautions)
    wr = _ok(law, "written_required")
    if not federal and wr and wr.get("note") and wr.get("value") is not None:
        cautions.append(wr["note"])
    verified = {f: bool(_ok(law, f)) for f in ("law_name", "custodian_term", "who_may_request",
                                               "response", "fee_waiver", "deemed_denial", "appeal",
                                               "letter_requirements")
                if (law or {}).get(f if f != "law_name" else "law_name_detail") is not None}
    email = agency.get("email") if agency.get("email_verified") else None
    portal = agency.get("portal_url")
    delivery = {"method": "email" if email else ("portal" if portal else
                                                  ("email_unverified" if agency.get("email") else
                                                   ("mail" if agency.get("address") else "unknown"))),
                "email": agency.get("email"), "portal_url": portal, "address": agency.get("address")}
    return {
        "id": rid, "title": title, "agency": agency, "law": ref, "records": records,
        "subject": subject, "body": body, "footer": FOOTER,
        "deadline": {"text": dl["text"], "verified": dl["verified"], "quote_url": dl["quote_url"]},
        "deadline_calc": dl["calc"],
        "delivery": delivery, "mailto_ok": bool(email), "portal_url": portal,
        "cautions": cautions, "verified": verified, "plain": plain,
        "followup": _followup(law, jur, subject, agency["name"], local=local, federal=federal),
        "appeal": None if federal else _appeal_state(law, jur, subject, agency["name"]),
    }


# --------------------------------------------------------------------------- public api
def requests_for_county(conn, fips: str, *, max_phas: int | None = 2,
                        max_cocs: int | None = 1) -> list[dict]:
    """Request dicts for one county. By default the two housing authorities that best match the
    county name and the CoC covering the largest share of it; pass None for every one."""
    fips = str(fips).zfill(5)
    county = _county(conn, fips)
    law = _law(conn, county["state"])
    us = _law(conn, "US")
    recips = _recipients(conn, fips)
    aw = _award_agencies(conn, recips)
    officers = _records_officers(conn, fips)
    phas = _phas(conn, county)
    cocs = _cocs(conn, fips)
    out: list = [_county_request(conn, county, law, recips, officers)]
    for p in phas[:max_phas] if max_phas is not None else phas:
        out.append(_pha_request(conn, county, law, p, officers))
    lead_coc = cocs[0] if cocs else None
    if us:
        out.append(_federal_request(conn, county, us, "hud", recips, aw, lead_coc))
        all_pairs = set().union(*aw.values()) if aw else set()
        va = any("Veterans" in (a or "") or c in VA_CFDA for a, c in all_pairs) or \
            any("VETERAN" in r["recipient_name"].upper() for r in recips)
        hhs = any(c in HHS_CFDA for _, c in all_pairs)
        if va:
            out.append(_federal_request(conn, county, us, "va", recips, aw, lead_coc))
        if hhs:
            out.append(_federal_request(conn, county, us, "hhs", recips, aw, lead_coc))
    for c in cocs[:max_cocs] if max_cocs is not None else cocs:
        out.append(_coc_request(conn, county, law, us, c))
    out = [r for r in out if r]
    skipped = {"housing_authorities": [p["name"] for p in phas[max_phas:]] if max_phas is not None else [],
               "cocs": [c["coc_num"] for c in cocs[max_cocs:]] if max_cocs is not None else []}
    for r in out:
        r["county"] = {"fips": fips, "name": county["name"], "state": county["state"]}
        r["also_in_county"] = skipped
    return out


def unfilled(text: str) -> list[str]:
    """Placeholders or blanks other than the four requester ones."""
    left = text
    for p in PLACEHOLDERS:
        left = left.replace(p, "")
    return re.findall(r"\{\{[^}]*\}\}|\[[^\]]*\]|_{3,}|\bTODO\b|\bNone\b", left)


def _print(reqs: list[dict]) -> None:
    for i, r in enumerate(reqs, 1):
        print("=" * 78)
        print(f"[{i}/{len(reqs)}] {r['title']}  ({r['id']})")
        print(f"Plain: {r['plain']}")
        d = r["delivery"]
        print(f"Send by: {d['method']}" + (f" {d['email']}" if d.get("email") else "")
              + (f" portal {d['portal_url']}" if d.get("portal_url") else "")
              + f"   mailto_ok={r['mailto_ok']}")
        print(f"Contact: {r['agency'].get('contact')}")
        print(f"Deadline: {r['deadline']['text'] or 'not verified'}   calc={r['deadline_calc']}")
        for c in r["cautions"]:
            print(f"Caution: {c}")
        print("-" * 78)
        print(r["body"])
        print(f"\n{r['footer']}")
        ap = r["appeal"] or {}
        print(f"\n[follow-up] {r['followup']['subject']}")
        print(f"[appeal] {ap.get('body_name')}  deadline={ap.get('deadline_days')} "
              f"{ap.get('deadline_day_type') or ''}  how={ap.get('how')}  letter={'yes' if ap.get('body') else 'no'}")
    if reqs and (reqs[0]["also_in_county"]["housing_authorities"] or reqs[0]["also_in_county"]["cocs"]):
        a = reqs[0]["also_in_county"]
        print("=" * 78)
        print(f"Also in this county (use --all for letters): {len(a['housing_authorities'])} more "
              f"housing authorities; CoCs {', '.join(a['cocs']) or 'none'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or " ").splitlines()[0])
    ap.add_argument("fips")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="every housing authority and CoC")
    a = ap.parse_args(argv)
    conn = connect_ro()
    kw = {"max_phas": None, "max_cocs": None} if a.all else {}
    reqs = requests_for_county(conn, a.fips, **kw)
    if a.json:
        json.dump(reqs, sys.stdout, indent=2)
        print()
    else:
        _print(reqs)
    bad = [(r["id"], u) for r in reqs for t in (r["body"], r["followup"]["body"],
                                                (r["appeal"] or {}).get("body") or "")
           for u in unfilled(t)]
    if bad:
        print(f"UNFILLED: {bad}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
