#!/usr/bin/env python3
"""Which upstream sources published something new since we last ingested them?

The atlas is re-ingested by hand, and a full re-ingest of every domain takes hours of polite
fetching. This script asks each publisher one cheap question per source (ingest/probes.py) and
compares the answer with the fingerprint we accepted after the last good ingest, which lives in
data/freshness.json (committed, so a diff shows exactly which source moved and when).

    python3 scripts/refresh.py                     # --check: print the table, exit 0 or 10
    python3 scripts/refresh.py --check --json watch.json --markdown watch.md
    python3 scripts/refresh.py --accept hud_pit     # after a good re-ingest of hud_pit
    python3 scripts/refresh.py --accept             # accept every domain as it is now
    python3 scripts/refresh.py --run                # re-ingest CHANGED domains one at a time
    python3 scripts/refresh.py --domains health,sf  # any mode, limited to these domains
    python3 scripts/refresh.py --state /tmp/f.json  # compare against another state file
    python3 scripts/refresh.py --watch --interval 21600 --max-rss-mb 900 --then-build

Exit codes: 0 nothing changed, 10 at least one probe CHANGED, 2 crash or bad usage.
`--run` exits 0 when every re-ingest it started succeeded, 1 when one failed.
A probe that errors is shown as `error` and never counts as a change: an outage is not news.

`--run` never runs two ingestors at once: the database allows one writer (common.db() takes an
exclusive lock), and concurrent writers corrupted it once.

`--watch` is the same thing on a timer, for a small always-on box (500 MB to 1 GB of RAM): check,
re-ingest what changed one child process at a time, and when something was re-ingested
optionally rebuild the site (`--then-build`: build_site.py, then crawl.py) and run
`--publish-cmd` (never by default). Every child gets RLIMIT_AS = --max-rss-mb where the OS honours
it, and is also polled with `ps -o rss=` every 5 s and killed (logged) above the cap, because
macOS ignores RLIMIT_AS. One JSON line per cycle goes to logs/refresh.jsonl. `touch STOP` in the
repo root ends the loop between steps (the scripts/loop.sh convention).
"""
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import DB, now  # noqa: E402
from ingest.probes import PROBES, all_probes, compare_view, fingerprint  # noqa: E402

STATE = ROOT / "data" / "freshness.json"
STOP = ROOT / "STOP"
WATCH_LOG = ROOT / "logs" / "refresh.jsonl"
POLL_SECONDS = 5
EXIT_SAME, EXIT_CHANGED, EXIT_CRASH = 0, 10, 2


# --------------------------------------------------------------------------- state file
def load_state(path: Path) -> dict:
    if path.exists():
        s = json.loads(path.read_text())
        s.setdefault("domains", {})
        return s
    return {"checked_at": None, "domains": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True, indent=1, ensure_ascii=False) + "\n")


def last_ingest(domain: str):
    """Latest successful ingest_run.ts for the domain, read-only; None when there is no DB."""
    if not DB.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        try:
            r = c.execute("SELECT MAX(ts) FROM ingest_run WHERE domain=? AND ok=1",
                          (domain,)).fetchone()
        finally:
            c.close()
        return r[0] if r else None
    except sqlite3.Error:
        return None


# --------------------------------------------------------------------------- checking
def _short(v, n=40):
    s = "null" if v is None else str(v)
    return s if len(s) <= n else s[: n - 3] + "..."


def _summary(probe, fp):
    k = probe["kind"]
    if k == "http":
        return f"len={fp.get('content_length')} lm={_short(fp.get('last_modified'))}"
    if k == "socrata":
        return f"rowsUpdatedAt={fp.get('rows_updated_at')}"
    if k == "arcgis":
        return f"lastEditDate={fp.get('last_edit_date')}"
    if k == "json":
        return f"{probe.get('field')}={_short(fp.get('value'))}"
    if k == "next_vintage":
        return (f"{fp.get('next_year')} {'PUBLISHED' if fp.get('next_exists') else 'not yet'}"
                f" (HTTP {fp.get('next_status')})")
    if k == "listing":
        return f"{fp.get('count')} files sha={str(fp.get('sha256'))[:12]}"
    return ""


def _diff(probe, old, new):
    a, b = compare_view(probe, old), compare_view(probe, new)
    parts = [f"{k}: {_short(a[k], 32)} -> {_short(b[k], 32)}" for k in a if a[k] != b[k]]
    if probe["kind"] == "listing" and isinstance(old.get("hrefs"), list) and isinstance(
            new.get("hrefs"), list):
        added = sorted(set(new["hrefs"]) - set(old["hrefs"]))
        removed = sorted(set(old["hrefs"]) - set(new["hrefs"]))
        parts += [f"+ {h.rsplit('/', 1)[-1]}" for h in added]
        parts += [f"- {h.rsplit('/', 1)[-1]}" for h in removed]
    return "; ".join(parts)


def check(state: dict, domains=None) -> list[dict]:
    out = []
    for dom, probe in all_probes(domains):
        fp = fingerprint(probe)
        ts = now()
        prev = state["domains"].get(dom, {}).get("probes", {}).get(probe["name"])
        res = {"domain": dom, "probe": probe["name"], "kind": probe["kind"],
               "url": probe["url"], "checked_at": ts,
               "fingerprint": {k: v for k, v in fp.items() if k != "ok"},
               "previous": (prev or {}).get("fingerprint")}
        if not fp.get("ok"):
            res["status"], res["detail"] = "error", fp.get("error", "unknown error")
        elif not prev or not prev.get("fingerprint") or prev.get("kind") != probe["kind"] \
                or prev.get("url") != probe["url"]:
            res["status"], res["detail"] = "new", _summary(probe, fp)
        elif compare_view(probe, prev["fingerprint"]) != compare_view(probe, fp):
            res["status"], res["detail"] = "CHANGED", _diff(probe, prev["fingerprint"], fp)
        else:
            res["status"], res["detail"] = "same", _summary(probe, fp)
        out.append(res)
    return out


def print_table(results: list[dict]) -> None:
    w_dom = max([len("domain")] + [len(r["domain"]) for r in results])
    w_pr = max([len("probe")] + [len(r["probe"]) for r in results])
    print(f"{'domain':<{w_dom}}  {'probe':<{w_pr}}  {'status':<7}  detail")
    print(f"{'-' * w_dom}  {'-' * w_pr}  {'-' * 7}  {'-' * 6}")
    for r in results:
        print(f"{r['domain']:<{w_dom}}  {r['probe']:<{w_pr}}  {r['status']:<7}  {r['detail']}")
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def markdown(results: list[dict]) -> str:
    def cell(s):
        return str(s).replace("|", "\\|").replace("\n", " ")
    changed = [r for r in results if r["status"] == "CHANGED"]
    errors = [r for r in results if r["status"] == "error"]
    lines = [f"The weekly source watch found {len(changed)} upstream source(s) that changed since "
             f"the fingerprints in `data/freshness.json` were last accepted.",
             "", f"Checked at {results[0]['checked_at'] if results else now()}.", "",
             "| domain | probe | what changed | url |", "|---|---|---|---|"]
    lines += [f"| {cell(r['domain'])} | {cell(r['probe'])} | {cell(r['detail'])} | {r['url']} |"
              for r in changed]
    lines += ["", "Refresh the changed domains (one writer at a time) and accept the new "
              "fingerprints, then commit `data/freshness.json`:", "", "```bash",
              "python3 scripts/refresh.py --run", "```"]
    if errors:
        lines += ["", "Probes that could not be read this run (not counted as changes):", "",
                  "| domain | probe | error |", "|---|---|---|"]
        lines += [f"| {cell(r['domain'])} | {cell(r['probe'])} | {cell(r['detail'])} |"
                  for r in errors]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- accepting
def accept(state: dict, domains, results=None) -> list[str]:
    """Store the current fingerprints for `domains`. Errored probes keep their old entry."""
    by_key = {(r["domain"], r["probe"]): r for r in (results or [])}
    warnings = []
    ts = now()
    for dom in sorted(domains):
        entry = state["domains"].setdefault(dom, {"probes": {}})
        old = entry.get("probes", {})
        new = {}
        for probe in PROBES.get(dom, []):
            r = by_key.get((dom, probe["name"]))
            if r is None:
                fp = fingerprint(probe)
                r = {"fingerprint": {k: v for k, v in fp.items() if k != "ok"},
                     "status": "ok" if fp.get("ok") else "error",
                     "detail": fp.get("error"), "checked_at": now()}
            if r["status"] == "error":
                warnings.append(f"{dom}/{probe['name']}: not accepted ({r['detail']})")
                if probe["name"] in old:
                    new[probe["name"]] = old[probe["name"]]
                continue
            new[probe["name"]] = {"kind": probe["kind"], "url": probe["url"],
                                  "fingerprint": r["fingerprint"],
                                  "checked_at": r["checked_at"], "accepted_at": ts}
        entry["probes"] = new
        entry["last_ingest"] = last_ingest(dom)
    state["checked_at"] = ts
    return warnings


# --------------------------------------------------------------------------- child processes
def _rss_mb(pid: int):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        return int(out) / 1024 if out else None      # ps reports KiB
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def run_child(cmd, max_mb=None, shell=False) -> dict:
    """Run one child in the foreground, memory-capped when max_mb is set. Never raises."""
    def limit():
        try:
            import resource
            cap = int(max_mb or 0) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        except Exception:  # noqa: BLE001  best effort: macOS refuses or ignores RLIMIT_AS
            pass
    t0, peak, killed = time.time(), 0.0, None
    try:
        child = subprocess.Popen(cmd, cwd=ROOT, shell=shell,
                                 preexec_fn=limit if max_mb else None)
    except OSError as e:
        return {"cmd": cmd, "rc": 127, "seconds": 0, "peak_rss_mb": None, "error": repr(e)}
    while True:
        try:
            rc = child.wait(timeout=POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            pass
        rss = _rss_mb(child.pid)
        if rss:
            peak = max(peak, rss)
        if max_mb and rss and rss > max_mb:
            killed = f"RSS {rss:.0f} MB over the {max_mb} MB cap; killed"
            print(f"ERROR {cmd}: {killed}", file=sys.stderr, flush=True)
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
            rc = child.wait()
            break
    out = {"cmd": cmd, "rc": rc, "seconds": round(time.time() - t0, 1),
           "peak_rss_mb": round(peak, 1) if peak else None}
    if killed:
        out["error"] = killed
    return out


def run_changed(state, state_path, results, max_mb=None) -> list[dict]:
    """Re-ingest each CHANGED domain, strictly one at a time; accept only those that exit 0."""
    changed = sorted({r["domain"] for r in results if r["status"] == "CHANGED"})
    done = []
    for dom in changed:
        if STOP.exists():
            print("STOP present: not starting further ingests", flush=True)
            break
        print(f"\n=== re-ingesting {dom}: python3 -m ingest.run {dom}", flush=True)
        res = run_child([sys.executable, "-m", "ingest.run", dom], max_mb)
        res["domain"] = dom
        if res["rc"] == 0:
            for w in accept(state, {dom}, [r for r in results if r["domain"] == dom]):
                print("WARN", w)
            save_state(state_path, state)
        done.append(res)
    return done


def _log_cycle(rec: dict) -> None:
    WATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCH_LOG, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def watch(a, domains) -> int:
    max_mb = a.max_rss_mb or 900
    cycle = 0
    while True:
        if STOP.exists():
            _log_cycle({"ts": now(), "event": "stop", "cycles": cycle})
            print(f"STOP file present ({STOP}); watch loop ends after {cycle} cycle(s)")
            return EXIT_SAME
        cycle += 1
        t0 = time.time()
        rec = {"ts": now(), "event": "cycle", "cycle": cycle, "domains": sorted(domains or PROBES),
               "max_rss_mb": max_mb}
        try:
            state = load_state(a.state)
            results = check(state, domains)
            print_table(results)
            rec["probes"] = len(results)
            rec["changed"] = sorted({r["domain"] for r in results if r["status"] == "CHANGED"})
            rec["errors"] = [f"{r['domain']}/{r['probe']}" for r in results if r["status"] == "error"]
            rec["ingests"] = run_changed(state, a.state, results, max_mb)
            ok_ingest = any(i["rc"] == 0 for i in rec["ingests"])
            if a.then_build and ok_ingest and not STOP.exists():
                rec["build"] = run_child([sys.executable, "scripts/build_site.py"], max_mb)
                if rec["build"]["rc"] == 0:
                    rec["crawl"] = run_child([sys.executable, "scripts/crawl.py", "--quiet"], max_mb)
                    if a.publish_cmd and rec["crawl"]["rc"] == 0:
                        rec["publish"] = run_child(a.publish_cmd, max_mb, shell=True)
        except Exception as e:  # noqa: BLE001  one bad cycle must not end a months-long loop
            rec["error"] = repr(e)[:500]
        rec["seconds"] = round(time.time() - t0, 1)
        _log_cycle(rec)
        print(f"cycle {cycle} done in {rec['seconds']}s; next in {a.interval}s", flush=True)
        deadline = time.time() + a.interval
        while time.time() < deadline and not STOP.exists():
            time.sleep(min(POLL_SECONDS, max(0.1, deadline - time.time())))


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare with the state file (default)")
    mode.add_argument("--accept", nargs="*", metavar="DOMAIN",
                      help="store current fingerprints for these domains (none = all)")
    mode.add_argument("--run", action="store_true",
                      help="re-ingest each CHANGED domain in turn, accept it if it exits 0")
    mode.add_argument("--watch", action="store_true",
                      help="loop: check, re-ingest changed domains, optionally build; until STOP")
    ap.add_argument("--domains", help="comma-separated domain filter")
    ap.add_argument("--state", type=Path, default=STATE, help="state file (default data/freshness.json)")
    ap.add_argument("--json", type=Path, help="also write the check result as JSON here")
    ap.add_argument("--markdown", type=Path, help="also write an issue body for CHANGED probes here")
    ap.add_argument("--interval", type=float, default=21600, help="--watch: seconds between cycles")
    ap.add_argument("--max-rss-mb", type=int, default=None,
                    help="memory cap per child process (--watch default 900; --run uncapped)")
    ap.add_argument("--then-build", action="store_true",
                    help="--watch: after a successful re-ingest run build_site.py then crawl.py")
    ap.add_argument("--publish-cmd", help="--watch: shell command run after a clean build+crawl "
                                          "(for example ./scripts/publish_cf.sh); default none")
    a = ap.parse_args(argv)

    domains = set(filter(None, (a.domains or "").split(","))) or None
    if a.accept:
        domains = (domains or set()) | set(a.accept)
    unknown = sorted(d for d in (domains or ()) if d not in PROBES)
    if unknown:
        print(f"unknown domain(s) with no probes: {', '.join(unknown)}; "
              f"known: {', '.join(sorted(PROBES))}", file=sys.stderr)
        return EXIT_CRASH
    if a.watch:
        return watch(a, domains)
    state = load_state(a.state)

    if a.accept is not None:
        warnings = accept(state, domains or set(PROBES))
        save_state(a.state, state)
        for w in warnings:
            print("WARN", w)
        print(f"accepted {', '.join(sorted(domains or PROBES))} into {a.state}")
        return EXIT_SAME

    results = check(state, domains)
    print_table(results)
    if a.json:
        a.json.write_text(json.dumps({"checked_at": now(), "state": str(a.state),
                                      "changed": sum(r["status"] == "CHANGED" for r in results),
                                      "results": results}, indent=1, sort_keys=True) + "\n")
    if a.markdown:
        a.markdown.write_text(markdown(results))
    changed = sorted({r["domain"] for r in results if r["status"] == "CHANGED"})

    if not a.run:
        return EXIT_CHANGED if changed else EXIT_SAME

    if not changed:
        print("\nnothing changed upstream; nothing to re-ingest")
        return EXIT_SAME
    done = run_changed(state, a.state, results, a.max_rss_mb)
    print("\nsummary")
    for r in done:
        msg = "ok, fingerprints accepted" if r["rc"] == 0 else f"FAILED rc={r['rc']}, not accepted"
        print(f"  {r['domain']:<14} {msg} ({r['seconds']}s, peak {r['peak_rss_mb']} MB)"
              + (f" {r['error']}" if r.get("error") else ""))
    return 0 if done and all(r["rc"] == 0 for r in done) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"refresh.py crashed: {e!r}", file=sys.stderr)
        sys.exit(EXIT_CRASH)
