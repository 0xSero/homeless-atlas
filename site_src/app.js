/* Homeless Atlas client: sharded autocomplete, sortable tables, MapLibre helpers. */
(function () {
  "use strict";
  var cache = {};
  function lc(s) { return (s || "").toLowerCase(); }

  /* ---------------- sharded search index ----------------
     data/idx/manifest.json -> {shards:{a:1234,...}, total:n}
     data/idx/<char>.json  -> [[type,title,sub,url], ...]                          */
  function shardKey(q) {
    var m = lc(q).match(/[a-z0-9]/);
    return m ? m[0] : "_";
  }
  /* a letter is split into ten buckets so a phone fetches ~10 small files instead of one big one */
  function loadShard(k, cb) {
    if (cache[k]) return cb(cache[k]);
    var want = [];
    for (var i = 0; i < 10; i++) want.push(k + "-" + i);
    Promise.all(want.map(function (name) {
      return fetch("/data/idx/" + name + ".json")
        .then(function (r) { return r.ok ? r.json() : []; })
        .catch(function () { return []; });
    })).then(function (parts) {
      var all = [];
      parts.forEach(function (p) { for (var i = 0; i < p.length; i++) all.push(p[i]); });
      cache[k] = all;
      cb(all);
    });
  }
  function score(item, q) {
    /* every whitespace-separated token of the query must appear somewhere in the record */
    var t = lc(item[1]), s = lc(item[2] || ""), hay = t + " " + s;
    var toks = q.split(/\s+/).filter(Boolean);
    for (var i = 0; i < toks.length; i++) if (hay.indexOf(toks[i]) < 0) return 0;
    if (t === q) return 2000;
    if (t.indexOf(q) === 0) return 1500 - t.length;
    if (t.indexOf(q) > 0) return 1200 - t.indexOf(q);
    if (t.indexOf(toks[0]) === 0) return 900 - t.length;
    if (s.indexOf(q) >= 0) return 600;
    return 200;
  }
  function doSearch(q, cb) {
    q = lc(q).trim();
    if (q.length < 2) return cb([]);
    loadShard(shardKey(q), function (items) {
      var out = [];
      for (var i = 0; i < items.length; i++) {
        var sc = score(items[i], q);
        if (sc > 0) out.push([sc, items[i]]);
      }
      out.sort(function (a, b) { return b[0] - a[0]; });
      cb(out.slice(0, 40).map(function (x) { return x[1]; }));
    });
  }
  window.atlasSearch = doSearch;

  /* ---------------- header autocomplete ---------------- */
  function initAutocomplete() {
    var box = document.querySelector(".searchbox");
    if (!box) return;
    var input = box.querySelector("input");
    var ac = box.querySelector(".ac");
    if (!input || !ac) return;
    var sel = -1, results = [], timer = null;
    function render() {
      if (!results.length) { ac.innerHTML = ""; ac.classList.remove("open"); return; }
      ac.innerHTML = results.map(function (r, i) {
        return '<a href="' + r[3] + '" class="' + (i === sel ? "sel" : "") + '">' +
          '<span class="badge">' + r[0] + '</span><div class="t">' + esc(r[1]) + '</div>' +
          (r[2] ? '<div class="s">' + esc(r[2]) + '</div>' : '') + '</a>';
      }).join("");
      ac.classList.add("open");
    }
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var q = input.value;
      timer = setTimeout(function () {
        doSearch(q, function (r) { results = r; sel = -1; render(); });
      }, 110);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { sel = Math.min(sel + 1, results.length - 1); render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, -1); render(); e.preventDefault(); }
      else if (e.key === "Enter") {
        if (sel >= 0 && results[sel]) { location.href = results[sel][3]; }
        else if (input.value.trim()) { location.href = "/search.html?q=" + encodeURIComponent(input.value.trim()); }
      } else if (e.key === "Escape") { ac.classList.remove("open"); }
    });
    document.addEventListener("click", function (e) { if (!box.contains(e.target)) ac.classList.remove("open"); });
    document.addEventListener("keydown", function (e) {
      if ((e.key === "/" || (e.key === "k" && (e.metaKey || e.ctrlKey))) &&
          document.activeElement !== input && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        input.focus(); e.preventDefault();
      }
    });
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  window.atlasEsc = esc;

  /* ---------------- mobile nav ---------------- */
  function initNav() {
    var b = document.querySelector(".navbtn"), n = document.querySelector("nav.main");
    if (b && n) b.addEventListener("click", function () { n.classList.toggle("open"); });
  }

  /* ---------------- sortable tables ---------------- */
  function initSort() {
    document.querySelectorAll("table.sortable").forEach(function (t) {
      var ths = t.querySelectorAll("th");
      ths.forEach(function (th, idx) {
        if (th.classList.contains("nosort")) return;
        th.addEventListener("click", function () {
          var rows = Array.prototype.slice.call(t.tBodies[0].rows);
          var dir = th.dataset.dir === "asc" ? -1 : 1;
          th.dataset.dir = th.dataset.dir === "asc" ? "desc" : "asc";
          rows.sort(function (a, b) {
            var x = a.cells[idx] ? a.cells[idx].dataset.v || a.cells[idx].textContent : "";
            var y = b.cells[idx] ? b.cells[idx].dataset.v || b.cells[idx].textContent : "";
            var nx = parseFloat(String(x).replace(/[^0-9.\-]/g, "")), ny = parseFloat(String(y).replace(/[^0-9.\-]/g, ""));
            if (!isNaN(nx) && !isNaN(ny)) return (nx - ny) * dir;
            return String(x).localeCompare(String(y)) * dir;
          });
          rows.forEach(function (r) { t.tBodies[0].appendChild(r); });
        });
      });
    });
  }

  /* ---------------- maps ---------------- */
  /* sequential warm ramp on paper; a cooler ramp when the reader prefers a dark scheme */
  var DARK_UI = document.documentElement.dataset.theme === "dark";
  var RAMP_LIGHT = [[0, "#EFE6D6"], [0.02, "#E4CDA9"], [0.08, "#D2A576"], [0.2, "#BD8250"], [0.45, "#A75832"], [0.7, "#8A3A1E"], [1, "#6A2410"]];
  var RAMP_DARK  = [[0, "#242119"], [0.02, "#3A2E2A"], [0.08, "#6B3A32"], [0.2, "#93452F"], [0.45, "#C05A32"], [0.7, "#E8834B"], [1, "#F7B267"]];
  var RAMP = DARK_UI ? RAMP_DARK : RAMP_LIGHT;
  var PAPER = DARK_UI ? "#131210" : "#FCFBF7";
  var BASE_STYLE = DARK_UI
    ? "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
    : "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
  function rateColorExpr(prop) {
    /* stops are homeless people per 10,000 residents, not fractions */
    return ["interpolate", ["linear"], ["coalesce", ["feature-state", "rate"], ["get", prop], 0],
      0, RAMP[0][1], 3, RAMP[1][1], 8, RAMP[2][1], 15, RAMP[3][1], 28, RAMP[4][1], 45, RAMP[5][1], 70, RAMP[6][1]];
  }
  function baseMap(el, opts) {
    opts = opts || {};
    return new maplibregl.Map({
      container: el,
      style: opts.style || BASE_STYLE,
      center: opts.center || [-96, 38.2],
      zoom: opts.zoom || 3.1,
      minZoom: 1, maxZoom: 11, attributionControl: false,
      dragRotate: false, pitchWithRotate: false
    });
  }
  function addPops(map, geojson, layer, colorProp, labelProp) {
    map.addSource(layer, { type: "geojson", data: geojson });
    map.addLayer({ id: layer, type: "circle", source: layer, paint: {
      "circle-color": rateColorExpr(colorProp),
      "circle-radius": ["interpolate", ["linear"], ["sqrt", ["coalesce", ["get", "v"], 1]], 1, 3, 20, 26],
      "circle-opacity": 0.88, "circle-stroke-color": PAPER, "circle-stroke-width": 0.6
    } });
    var pop = new maplibregl.Popup({ closeButton: true, maxWidth: "300px" });
    map.on("click", layer, function (e) {
      var f = e.features[0], p = f.properties;
      pop.setLngLat(f.geometry.coordinates).setHTML(p.__html || esc(p[labelProp])).addTo(map);
    });
    map.on("mouseenter", layer, function () { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", layer, function () { map.getCanvas().style.cursor = ""; });
  }
  function addChoropleth(map, geojson, id) {
    map.addSource(id, { type: "geojson", data: geojson, promoteId: "abbr" });
    map.addLayer({ id: id + "-fill", type: "fill", source: id, paint: { "fill-color": rateColorExpr("rate"), "fill-opacity": 0.72 } });
    map.addLayer({ id: id + "-line", type: "line", source: id, paint: { "line-color": PAPER, "line-width": 0.8 } });
    var pop = new maplibregl.Popup({ maxWidth: "320px" });
    map.on("click", id + "-fill", function (e) {
      var p = e.features[0].properties;
      pop.setLngLat(e.lngLat).setHTML(p.__html || "<b>" + esc(p.name) + "</b>").addTo(map);
    });
    map.on("mouseenter", id + "-fill", function () { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", id + "-fill", function () { map.getCanvas().style.cursor = ""; });
  }
  window.atlasMap = { base: baseMap, pops: addPops, choro: addChoropleth, ramp: RAMP, baseStyle: BASE_STYLE, paper: PAPER };

  /* ---------------- search page ---------------- */
  function initSearchPage() {
    var out = document.getElementById("results");
    if (!out) return;
    var input = document.getElementById("q");
    var params = new URLSearchParams(location.search);
    if (params.get("q")) input.value = params.get("q");
    var typeFilter = "";
    var MANIFEST = window.__manifest || {};
    function run() {
      var q = input.value;
      if (q.trim().length < 2) { out.innerHTML = '<p class="dim">Type at least two characters.</p>'; return; }
      doSearch(q, function (r) {
        if (typeFilter) r = r.filter(function (x) { return x[0] === typeFilter; });
        if (!r.length) { out.innerHTML = '<p class="dim">No match for “' + esc(q) + '”.</p>'; return; }
        out.innerHTML = r.map(function (x) {
          return '<a class="r" href="' + x[3] + '"><div class="b">' + x[0] + '</div><div class="t">' +
            esc(x[1]) + '</div>' + (x[2] ? '<div class="s">' + esc(x[2]) + '</div>' : '') + '</a>';
        }).join("");
      });
    }
    input.addEventListener("input", run);
    document.querySelectorAll(".filters button").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll(".filters button").forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on"); typeFilter = b.dataset.type || ""; run();
      });
    });
    if (input.value) run();
    input.focus();
  }

  function boot() { initAutocomplete(); initNav(); initSort(); initSearchPage(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
