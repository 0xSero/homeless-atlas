"""Public records request pages for the Homeless Atlas site.

Called from site_county.build(). Writes:

  records.html                    every state's records law in one table, plus the federal FOIA
  records/<ST>.html               one state's law in plain words, each fact quoted from the official text
  county/<fips>-records.html      ready letters for one county: fill in your name once, then email,
                                  copy, print or open the agency portal; due dates, follow-up and appeal
  api/records/<fips>.json         the same letters as data, with placeholders, for agents
  api/records-laws.json           every jurisdiction's verified law fields

The letters come from ingest/records_requests.py and the law facts from the records_law table
(ingest/domains/records_laws.py). Only facts whose quote was found in the official text are shown;
the verifier marks the rest and this module leaves them out. If either piece is missing in a partial
database, nothing here is written and the county page shows no records section.

Nothing the visitor types is sent anywhere: site_src/records.js keeps it in this browser only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

B: Any = None  # the build_site module
RR: Any = None  # ingest.records_requests, when present

FIELD_LABELS = [
    ("who_may_request", "Who may ask"),
    ("custodian_term", "Who you write to"),
    ("response", "How long they have"),
    ("written_required", "Must it be in writing"),
    ("form_required", "Is a special form required"),
    ("must_cite_statute", "Must you name the law"),
    ("fees", "Fees"),
    ("fee_waiver", "Fee waiver"),
    ("deemed_denial", "If they do not answer"),
    ("appeal", "If they say no"),
]
UNITS = {"business": "business days", "calendar": "calendar days"}
PLACEHOLDERS = {
    "{{requester_name}}": "[Your name]",
    "{{requester_address}}": "[Your mailing address]",
    "{{requester_email}}": "[Your email]",
    "{{date}}": "[Date]",
}


# ------------------------------------------------------------------ data access
def available():
    global RR
    if not B.q(B.CONN, "SELECT name FROM sqlite_master WHERE type='table' AND name='records_law'"):
        return False
    if RR is None:
        root = str(Path(__file__).resolve().parent.parent)  # build_site.py runs from scripts/
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            import ingest.records_requests as rr
        except Exception as ex:  # say so: a silent skip once hid every records page
            print(f"records pages skipped: ingest.records_requests did not import ({ex!r})", file=sys.stderr)
            return False
        RR = rr
    return True


def laws():
    """{jurisdiction: data dict} with only verified fields kept."""
    out = {}
    for r in B.q(B.CONN, "SELECT jurisdiction, data_json, fields_total, fields_verified, verified_at FROM records_law"):
        try:
            d = json.loads(r["data_json"] or "{}")
        except ValueError:
            continue
        d["_fields_total"], d["_fields_verified"], d["_verified_at"] = r["fields_total"], r["fields_verified"], r["verified_at"]
        out[r["jurisdiction"]] = d
    return out


def verified(field):
    """A law field is shown only when the verifier found its quote. Fields without a quote (plain
    names and citations) are shown as given."""
    if field is None:
        return False
    if isinstance(field, dict):
        if "verified" in field:
            return bool(field["verified"])
        if "quote" in field:
            return False
    return True


def plain(field):
    """Human text for one law field."""
    if isinstance(field, dict):
        for k in ("text", "summary", "explain", "value", "body"):
            v = field.get(k)
            if isinstance(v, bool):
                return ("Yes" if v else "No") + (f'. {field["note"]}' if field.get("note") else "")
            if v not in (None, ""):
                return str(v)
        if "days" in field:
            if field["days"] is None:  # the law sets no number of days
                return "Promptly; no fixed number of days" if field.get("day_type") == "promptly" else "No fixed deadline"
            unit = UNITS.get(field.get("day_type") or "", "days")
            s = f'{field["days"]} {unit}'
            if field.get("clock_starts"):
                s += f', counted from {field["clock_starts"]}'
            ext = field.get("extension") or {}
            if isinstance(ext, dict) and ext.get("days"):
                s += f'. They may extend by {ext["days"]} days'
                if ext.get("conditions"):
                    s += f' ({ext["conditions"]})'
            return s
        return ""
    if field is True:
        return "Yes"
    if field is False:
        return "No"
    return "" if field is None else str(field)


def quote_line(field):
    if not isinstance(field, dict) or not field.get("quote"):
        return ""
    url = field.get("quote_url") or ""
    q = B.esc(field["quote"])
    return (f'<br><span class="dim" style="font-size:.8rem">\u201c{q}\u201d'
            + (f' (<a href="{B.esc(url)}" rel="noopener">official text</a>)' if url else "") + "</span>")


def fill_preview(text):
    for k, v in PLACEHOLDERS.items():
        text = text.replace(k, v)
    return text


_CACHE: dict[str, list] = {}


def requests(fips):
    """The ready letters for one county, computed once per build (the county page and the records
    page both use them)."""
    if fips not in _CACHE:
        try:
            _CACHE[fips] = RR.requests_for_county(B.CONN, fips) or []
        except Exception as e:  # one county's missing data must not stop the whole build
            print(f"  records: {fips}: {e}")
            _CACHE[fips] = []
    return _CACHE[fips]


# ------------------------------------------------------------------ pieces
def due_text(rq):
    d = rq.get("deadline") or {}
    return d.get("text") or d.get("plain") or ""


def send_by(rq):
    """How to deliver, as HTML."""
    a = rq.get("agency") or {}
    dv = rq.get("delivery") or {}
    mode, val = dv.get("mode") or dv.get("method"), dv.get("value")
    parts = []
    if rq.get("portal_url") or mode == "portal":
        u = rq.get("portal_url") or val
        parts.append(f'Online portal: <a href="{B.esc(u)}" rel="noopener">{B.esc(u)}</a>')
    if a.get("email"):
        parts.append(f'Email: <a href="mailto:{B.esc(a["email"])}">{B.esc(a["email"])}</a>')
    if a.get("address"):
        parts.append(f'Mail: {B.esc(a["address"])}')
    if a.get("phone"):
        parts.append(f'Questions: {B.esc(a["phone"])}')
    return "<br>".join(parts) or '<span class="dim">No published records contact yet. Use the agency\'s main office address.</span>'


def letter_block(rq, i, kind, subject, body, to_email):
    """One letter: an editable text box and the buttons that act on it."""
    if not body:
        return ""
    rid = f"{rq.get('id') or i}-{kind}"
    btns = []
    if to_email:
        btns.append(f'<button type="button" data-act="email" data-for="{rid}">Email it</button>')
    btns.append(f'<button type="button" data-act="copy" data-for="{rid}">Copy letter</button>')
    if kind == "request" and rq.get("portal_url"):
        btns.append(f'<a href="{B.esc(rq["portal_url"])}" rel="noopener" data-act="portal" data-for="{rid}">Open the portal</a>')
    btns.append(f'<button type="button" data-act="print" data-for="{rid}">Print</button>')
    if kind == "request":
        btns.append(f'<button type="button" data-act="sent" data-for="{rid}">I sent it today</button>')
    return (f'<textarea class="letter" id="{B.esc(rid)}" rows="14" spellcheck="true" data-req="{i}" data-kind="{kind}" '
            f'data-to="{B.esc(to_email or "")}" data-subject="{B.esc(subject or "")}" '
            f'aria-label="{B.esc(subject or "Letter")}">{B.esc(fill_preview(body))}</textarea>'
            f'<div class="layertoggle actions">{"".join(btns)}</div>'
            f'<p class="status dim" data-status="{B.esc(rid)}" aria-live="polite"></p>')


def request_section(rq, i):
    a = rq.get("agency") or {}
    law = rq.get("law") or {}
    dl = [("To", B.esc(a.get("name") or "") + (f'<br>{B.esc(a["contact"])}' if a.get("contact") else "")),
          ("Send it", send_by(rq)),
          ("Law", f'{B.esc(law.get("name") or "")}, {B.esc(law.get("citation") or "")}'
                  + (f' · <a href="/records/{B.esc(law["jurisdiction"])}.html">how it works</a>'
                     if law.get("jurisdiction") and law["jurisdiction"] != "US" else
                     ' · <a href="/records/US.html">how it works</a>' if law.get("jurisdiction") == "US" else ""))]
    due = due_text(rq)
    if due:
        dl.append(("Answer due", B.esc(due) + f' <span class="due" data-due="{i}"></span>'))
    for c in rq.get("cautions") or []:
        dl.append(("Note", B.esc(c)))
    if rq.get("records"):
        dl.append(("Asks for", "<ul style='margin:0'>" + "".join(f"<li>{B.esc(x)}</li>" for x in rq["records"]) + "</ul>"))
    facts = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in dl)
    email = a.get("email") if rq.get("mailto_ok", bool(a.get("email"))) else ""
    out = [f'<section class="req" id="{B.esc(rq.get("id") or str(i))}">',
           f'<h2>{i + 1}. {B.esc(rq.get("title") or "")}</h2>',
           f'<p>{B.esc(rq.get("plain") or "")}</p>' if rq.get("plain") else "",
           f'<dl class="facts">{facts}</dl>',
           "<h3>The letter</h3>",
           letter_block(rq, i, "request", rq.get("subject"), rq.get("body"), email)]
    fu = rq.get("followup") or {}
    if fu.get("body"):
        out.append('<details><summary>No answer by the due date? Send this follow-up</summary>'
                   + letter_block(rq, i, "followup", fu.get("subject") or ("Follow-up: " + (rq.get("subject") or "")), fu["body"], email)
                   + "</details>")
    ap = rq.get("appeal") or {}
    if ap.get("body"):
        how = ap.get("how") or {}
        ap_email = how.get("value") if (how.get("mode") == "email") else ""
        facts = []
        if ap.get("body_name"):
            facts.append(("Appeal to", B.esc(ap["body_name"]) + (f' · <a href="{B.esc(ap["url"])}" rel="noopener">their page</a>' if ap.get("url") else "")))
        if ap.get("deadline_days"):
            unit = UNITS.get(ap.get("deadline_day_type") or "", "days")
            facts.append(("Deadline", f'{ap["deadline_days"]} {unit} after the denial (or after the answer was due, if none came)'))
        if how.get("value"):
            facts.append(("Send it", B.esc(how["value"]) if how.get("mode") != "portal" else
                          f'<a href="{B.esc(how["value"])}" rel="noopener">{B.esc(how["value"])}</a>'))
        out.append('<details><summary>Denied, or still no answer? File the appeal</summary>'
                   + (f'<dl class="facts">{"".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts)}</dl>' if facts else "")
                   + letter_block(rq, i, "appeal", ap.get("subject") or ("Appeal: " + (rq.get("subject") or "")), ap["body"], ap_email)
                   + "</details>")
    out.append("</section>")
    return "".join(out)


# ------------------------------------------------------------------ pages
def county_section(c, rqs):
    """The short list on the county page. Each row links to the ready letter."""
    if not rqs:
        return ""
    rows = []
    for i, rq in enumerate(rqs):
        a = rq.get("agency") or {}
        law = rq.get("law") or {}
        anchor = rq.get("id") or str(i)
        rows.append([f'<a href="/county/{c["fips"]}-records.html#{B.esc(anchor)}">{B.esc(rq.get("title") or "")}</a>', "", rq.get("title") or "",
                     B.esc(a.get("name") or ""), "", a.get("name") or "",
                     B.esc(law.get("name") or ""), "", law.get("name") or "",
                     B.esc(due_text(rq)) or NA(), "", ""])
    return ("<h2>Request the records</h2>"
            f'<p>Contracts, payments and outcome reports are public records. These letters are ready to send: '
            f'<a href="/county/{c["fips"]}-records.html">add your name and send them</a>. It takes about a minute each.</p>'
            + B.table([("Request", ""), ("Sent to", ""), ("Law", ""), ("Answer due", "")], rows, cls="nosort"))


def NA():
    return B.num(None)


def page_county_records(c, stname, rqs, total):
    fips, name, st = c["fips"], c["name"], c["state"]
    data = [{"id": rq.get("id") or str(i),
             "deadline_calc": rq.get("deadline_calc"),
             "title": rq.get("title"),
             "agency": (rq.get("agency") or {}).get("name")} for i, rq in enumerate(rqs)]
    st_law = (rqs[0].get("law") or {}) if rqs else {}
    sub = (f'{len(rqs)} ready letters, under {B.esc(the(st_law.get("name")) or "your state\'s open records law")} '
           f'and the federal Freedom of Information Act. Fill in your details once and every letter updates. '
           f'What you type stays in this browser; nothing is sent until you send it.')
    who = """<h2>Your details</h2>
<p>Agencies use these to send you the records. Leave a field blank and the letter shows a gap for you to fill by hand.</p>
<form class="who" autocomplete="on" onsubmit="return false">
  <label>Your name<input name="requester_name" autocomplete="name"></label>
  <label>Mailing address<input name="requester_address" autocomplete="street-address"></label>
  <label>Email<input name="requester_email" type="email" autocomplete="email"></label>
  <button type="button" data-act="forget" class="forget">Forget my details</button>
</form>"""
    steps = """<ol class="steps">
<li>Fill in your details below.</li>
<li>Read the letter. Edit it if you like.</li>
<li>Press <b>Email it</b>, or <b>Open the portal</b> and paste, or <b>Print</b> and mail it.</li>
<li>Press <b>I sent it today</b>. The page shows the date the answer is due and can put it on your calendar.</li>
<li>No answer by then? Send the follow-up. Denied? File the appeal. Both letters are below each request.</li>
</ol>"""
    body = (B.banner(f"Records requests · {stname}", f"Ask {B.esc(name)} for the records", sub,
                     crumbs=f'<a href="/index.html">Atlas</a> / <a href="/records.html">Records</a> / '
                            f'<a href="/records/{B.esc(st)}.html">{B.esc(stname)}</a> / '
                            f'<a href="/county/{fips}.html">{B.esc(name)}</a>', small=True)
            + f"""
<div class="wrap">
  {steps}
  {who}
  {"".join(request_section(rq, i) for i, rq in enumerate(rqs))}
  <h2>Before you send</h2>
  <p>These are templates, not legal advice. Each legal fact on this page was checked against the official text of the law;
  see the <a href="/records/{B.esc(st)}.html">{B.esc(stname)} guide</a> for the quotes and links.
  Due dates skip weekends but not holidays; add a day for each holiday in between.
  Agencies may charge copying fees; every letter asks them to tell you first if fees would pass $25.</p>
  <p class="dim" style="font-size:.8rem">The same letters as data: <a href="/api/records/{fips}.json">/api/records/{fips}.json</a>.</p>
</div>
<script type="application/json" id="req-data">{json.dumps(data).replace("</", "<\\/")}</script>
<script src="/assets/records.js" defer></script>""")
    head = f'<link rel="alternate" type="application/json" href="/api/records/{fips}.json">'
    return B.shell("../", f"Records requests, {name}, {stname}", "records", body, head=head, manifest_total=total,
                   desc=f"Ready-to-send public records requests for {name}, {stname}: homelessness contracts, payments and "
                        f"outcome reports, under {st_law.get('name') or 'state law'} and the federal FOIA.")


def law_rows(d):
    rows = []
    for key, label in FIELD_LABELS:
        f = d.get(key)
        if not verified(f):
            continue
        text = plain(f)
        if key == "who_may_request" and isinstance(f, dict):
            text = ". ".join(x for x in [who_short(f), f.get("explain") or ""] if x)
        if key == "appeal" and isinstance(f, dict):
            text = " ".join(x for x in [f.get("body") or "", f'within {f["deadline_days"]} days' if f.get("deadline_days") else "",
                                         ] if x)
        if not text:
            continue
        rows.append(f"<dt>{label}</dt><dd>{B.esc(text)}{quote_line(f)}</dd>")
    return "".join(rows)


def page_state(st, d, counties, total):
    stname = "the federal government" if st == "US" else B.esc(next((c["stname"] for c in counties), st))
    title = ("Federal Freedom of Information Act requests" if st == "US" else
             f"Public records requests in {stname}")
    law = f'{B.esc(d.get("law_name") or "")}, ' + (
        f'<a href="{B.esc(d["statute_url"])}" rel="noopener">{B.esc(d.get("citation") or "")}</a>' if d.get("statute_url")
        else B.esc(d.get("citation") or ""))
    clist = ""
    if counties:
        clist = ("<h2>Ready letters by county</h2><ul class='cols'>"
                 + "".join(f'<li><a href="/county/{c["fips"]}-records.html">{B.esc(c["name"])}</a></li>'
                           for c in sorted(counties, key=lambda x: x["name"]))
                 + "</ul>")
    basis = ("Checked against the official guide published by the state, because the statute site could not be read."
             if d.get("basis") == "official_guide" else "Each fact is quoted from the official text and checked by machine.")
    body = B.banner("Records requests", title, f"The law: {law}. {basis} "
                    f"{d.get('_fields_verified') or 0} of {d.get('_fields_total') or 0} facts verified, last on "
                    f"{B.esc(str(d.get('_verified_at') or '')[:10])}.", small=True,
                    crumbs='<a href="/index.html">Atlas</a> / <a href="/records.html">Records</a> / ' + (stname if st != "US" else "Federal")) + f"""
<div class="wrap">
  <dl class="facts">{law_rows(d)}</dl>
  {clist}
  <p class="dim" style="font-size:.8rem;margin-top:2em">This summarizes the law; it is not legal advice. Facts that could not be
  checked against the official text are left off this page. All jurisdictions as data:
  <a href="/api/records-laws.json">/api/records-laws.json</a>.</p>
</div>"""
    return B.shell("../", title, "records", body, manifest_total=total,
                   desc=(f"How to file a public records request in {stname}: {d.get('law_name') or ''} "
                         f"({d.get('citation') or ''}), who may ask, deadlines, fees and appeals, quoted from the law."))


def who_short(who):
    """One-cell answer to "who may ask", for the index table. The state page carries the full text."""
    v, why = (who or {}).get("value"), ((who or {}).get("explain") or "").lower()
    if v == "any_person":
        return "Anyone"
    if v == "state_residents_only":
        return "State residents only"
    if "legal resident of the united states" in why:
        return "U.S. residents"
    if "age of majority" in why:
        return "Any adult"
    if "citizen" in why:
        return "Citizens (term not defined)"
    return plain(who)


def the(law):
    """'the Right-to-Know Law', 'the Alabama Open Records Act'; but 'Utah GRAMA', 'Nebraska public records'."""
    return f"the {law}" if law and law.split()[-1] in ("Act", "Law") else law


def page_index(ls, total):
    rows = []
    for st, d in sorted(ls.items(), key=lambda kv: (kv[0] != "US", kv[1].get("_stname") or kv[0])):
        resp = d.get("response")
        who = d.get("who_may_request")
        ap = d.get("appeal")
        rows.append([f'<a href="/records/{B.esc(st)}.html">{"Federal (FOIA)" if st == "US" else B.esc(d.get("_stname") or st)}</a>', "", st,
                     B.esc(d.get("law_name") or ""), "", d.get("law_name") or "",
                     B.esc(who_short(who)) if verified(who) else NA(), "", "",
                     B.esc(plain({k: v for k, v in (resp or {}).items() if k in ("days", "day_type", "text")}))
                     if (isinstance(resp, dict) and verified(resp)) else NA(), "", (resp or {}).get("days") if isinstance(resp, dict) else 0,
                     (B.esc(ap.get("body") or "") if verified(ap) else NA()) if ap else '<span class="dim">Not recorded yet</span>',
                     "", ""])
    body = B.banner("Records requests", "Ask for the records",
                    "Contracts, payments and outcome reports for homelessness programs are public records. Every state has a law "
                    "that lets you ask for them, and the federal Freedom of Information Act covers HUD and the VA. Pick your "
                    "county and the letters are already written.") + f"""
<div class="wrap">
  <p>Find your county: open your state below, or search for the county name at the top of any page and choose
  <b>Request the records</b> on its page.</p>
  {B.table([("Jurisdiction", ""), ("Law", ""), ("Who may ask", ""), ("Time to answer", "r"), ("Appeal to", "")], rows,
           note="Only facts checked against the official text are shown. This is a summary of the law, not legal advice.")}
</div>"""
    return B.shell("", "Records requests", "records", body, manifest_total=total,
                   desc="Ready-to-send public records and FOIA requests for every U.S. county: homelessness contracts, payments "
                        "and outcomes, with each state's deadlines and appeal route quoted from the law.")


# ------------------------------------------------------------------ entry points
def build(build_site_module, counties, state_names, total):
    """Write every records page. counties: [{fips, name, state}]. Returns pages written."""
    global B
    B = build_site_module
    if not available():
        return 0
    ls = laws()
    for st, d in ls.items():
        d["_stname"] = state_names.get(st, st)
    n = 0
    by_state: dict[str, list] = {}
    for c in counties:
        rqs = requests(c["fips"])
        if not rqs:
            continue
        stname = state_names.get(c["state"], c["state"])
        by_state.setdefault(c["state"], []).append({"fips": c["fips"], "name": c["name"], "stname": stname})
        B.write(f"county/{c['fips']}-records.html", page_county_records(c, stname, rqs, total)); n += 1
        B.write(f"api/records/{c['fips']}.json", json.dumps({
            "county_fips": c["fips"], "county": c["name"], "state": c["state"], "generated": B.NOW,
            "placeholders": list(PLACEHOLDERS), "not_legal_advice": True, "requests": rqs}, indent=1, default=str)); n += 1
    for st, d in ls.items():
        B.write(f"records/{st}.html", page_state(st, d, by_state.get(st, []), total)); n += 1
    B.write("records.html", page_index(ls, total)); n += 1
    B.write("api/records-laws.json", json.dumps({"generated": B.NOW, "jurisdictions": ls}, indent=1, default=str)); n += 1
    return n


def county_section_for(build_site_module, c):
    """Called by site_county.page_county."""
    global B
    B = build_site_module
    if not available():
        return ""
    return county_section(c, requests(c["fips"]))
