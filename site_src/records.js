/* Records request pages: fill letters with the requester's details, send, track due dates.
   Everything stays in this browser (localStorage). Nothing is sent to the atlas. */
(function () {
  "use strict";
  var KEY_WHO = "atlas-requester", KEY_SENT = "atlas-sent";
  var PH = { requester_name: "[Your name]", requester_address: "[Your mailing address]", requester_email: "[Your email]" };
  var data = [];
  try { data = JSON.parse(document.getElementById("req-data").textContent); } catch (e) {}

  function load(k) { try { return JSON.parse(localStorage.getItem(k) || "{}"); } catch (e) { return {}; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  var DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function longDate(d) { return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear(); }
  function shortDate(d) { return DAYS[d.getDay()] + ", " + MONTHS[d.getMonth()].slice(0, 3) + " " + d.getDate() + ", " + d.getFullYear(); }
  function iso(d) { return d.getFullYear() + String(d.getMonth() + 1).padStart(2, "0") + String(d.getDate()).padStart(2, "0"); }
  function parseDay(s) { var p = s.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function dayKey(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }

  /* Due date from the statute's count. Business days skip Saturday and Sunday; holidays are not known here. */
  function addDays(from, calc) {
    if (!calc || !calc.days) return null;
    var d = new Date(from.getFullYear(), from.getMonth(), from.getDate());
    if (calc.day_type === "business") {
      var left = calc.days;
      while (left > 0) { d.setDate(d.getDate() + 1); if (d.getDay() !== 0 && d.getDay() !== 6) left--; }
    } else {
      d.setDate(d.getDate() + calc.days);
    }
    return d;
  }

  var form = document.querySelector("form.who");
  var who = load(KEY_WHO);
  var boxes = Array.prototype.slice.call(document.querySelectorAll("textarea.letter"));
  boxes.forEach(function (t) {
    t.dataset.tpl = t.value;
    t.addEventListener("input", function () { t.dataset.edited = "1"; });
  });

  function fill(tpl) {
    var out = tpl;
    Object.keys(PH).forEach(function (k) { if (who[k]) out = out.split(PH[k]).join(who[k]); });
    return out.split("[Date]").join(longDate(new Date()));
  }
  function render() { boxes.forEach(function (t) { if (!t.dataset.edited) t.value = fill(t.dataset.tpl); }); }

  if (form) {
    Object.keys(PH).forEach(function (k) {
      var el = form.elements[k];
      if (!el) return;
      el.value = who[k] || "";
      el.addEventListener("input", function () { who[k] = el.value.trim(); save(KEY_WHO, who); render(); });
    });
  }
  render();

  function status(rid, html) {
    var el = document.querySelector('[data-status="' + rid + '"]');
    if (el) el.innerHTML = html;
  }
  function copy(text, done) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(fallback()); });
    } else { done(fallback()); }
    function fallback() {
      var t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
      document.body.appendChild(t); t.select();
      var ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(t); return ok;
    }
  }
  function missing(text) {
    var gaps = [];
    Object.keys(PH).forEach(function (k) { if (text.indexOf(PH[k]) >= 0) gaps.push(PH[k].slice(1, -1).toLowerCase()); });
    return gaps.length ? " The letter still has a gap for " + gaps.join(", ") + "." : "";
  }
  function ics(title, due, agency) {
    var end = new Date(due); end.setDate(end.getDate() + 1);
    var lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Homeless Atlas//Records//EN", "BEGIN:VEVENT",
      "UID:" + iso(due) + "-" + Math.random().toString(36).slice(2) + "@homeless-atlas",
      "DTSTAMP:" + new Date().toISOString().replace(/[-:]/g, "").slice(0, 15) + "Z",
      "DTSTART;VALUE=DATE:" + iso(due), "DTEND;VALUE=DATE:" + iso(end),
      "SUMMARY:Records request answer due: " + title.replace(/[,;\\]/g, " "),
      "DESCRIPTION:" + (agency || "").replace(/[,;\\]/g, " ") + ". If there is no answer, send the follow-up letter: " + location.href.split("#")[0],
      "END:VEVENT", "END:VCALENDAR"];
    return URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/calendar" }));
  }
  function showSent(rid) {
    var sent = load(KEY_SENT)[rid];
    if (!sent) return;
    var t = document.getElementById(rid);
    var info = data[+t.dataset.req] || {};
    var on = parseDay(sent), due = addDays(on, info.deadline_calc);
    var html = "Sent " + shortDate(on) + ".";
    if (due) {
      var late = due < new Date(new Date().toDateString());
      html += " Answer due by <b>" + shortDate(due) + "</b> (weekends skipped; add a day for each holiday). " +
        '<a download="records-request-due.ics" href="' + ics(info.title || "", due, info.agency) + '">Add to calendar</a>.' +
        (late ? " <b>The due date has passed.</b> Send the follow-up below." : "");
    }
    status(rid, html);
  }

  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-act]");
    if (!b) return;
    var act = b.dataset.act;
    if (act === "forget") {
      who = {}; save(KEY_WHO, who);
      try { localStorage.removeItem(KEY_SENT); } catch (e) {}
      Object.keys(PH).forEach(function (k) { if (form && form.elements[k]) form.elements[k].value = ""; });
      boxes.forEach(function (t) { delete t.dataset.edited; });
      render();
      document.querySelectorAll("[data-status]").forEach(function (s) { s.innerHTML = ""; });
      return;
    }
    var rid = b.dataset.for, t = document.getElementById(rid);
    if (!t) return;
    var text = t.value, subject = t.dataset.subject || "", to = t.dataset.to || "";
    if (act === "copy") {
      copy(text, function (ok) { status(rid, ok ? "Copied." + missing(text) : "Select the letter and copy it by hand."); });
    } else if (act === "email") {
      var url = "mailto:" + encodeURIComponent(to).replace(/%40/g, "@") + "?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(text);
      if (url.length > 1900) {
        /* Some mail apps cut long mailto links. Put the letter on the clipboard and open a short draft. */
        copy(text, function () {
          status(rid, "Your mail app is opening. The letter is on your clipboard: paste it into the message." + missing(text));
          location.href = "mailto:" + encodeURIComponent(to).replace(/%40/g, "@") + "?subject=" + encodeURIComponent(subject) +
            "&body=" + encodeURIComponent("[Paste the letter here]");
        });
      } else {
        status(rid, "Your mail app is opening with the letter filled in." + missing(text));
        location.href = url;
      }
    } else if (act === "portal") {
      copy(text, function (ok) { status(rid, ok ? "Letter copied. Paste it into the request box on the portal." + missing(text) : ""); });
    } else if (act === "print") {
      var w = window.open("", "_blank");
      if (!w) { window.print(); return; }
      var esc = function (s) { return s.replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); };
      w.document.write('<!doctype html><meta charset="utf-8"><title>' + esc(subject) + "</title>" +
        '<style>@page{margin:1in}body{font:12pt/1.5 Georgia,serif;white-space:pre-wrap;margin:0}</style>' + esc(text));
      w.document.close(); w.focus(); w.print();
    } else if (act === "sent") {
      var s = load(KEY_SENT); s[rid] = dayKey(new Date()); save(KEY_SENT, s); showSent(rid);
    }
  });

  /* "If sent today" date next to each deadline, and any request already marked sent. */
  document.querySelectorAll(".due[data-due]").forEach(function (el) {
    var info = data[+el.dataset.due] || {}, due = addDays(new Date(), info.deadline_calc);
    if (due) el.textContent = "(by " + shortDate(due) + " if sent today)";
  });
  boxes.forEach(function (t) { if (t.dataset.kind === "request") showSent(t.id); });
})();
