"""Run one or more domain ingestors: python3 -m ingest.run hud_pit usaspending ..."""
import importlib, sys, traceback
from . import common

def run(names):
    ok, fail = [], []
    for n in names:
        mod = importlib.import_module(f"ingest.domains.{n}")
        conn = common.db()
        try:
            res = mod.run(conn, {})
            conn.commit()
            rows = res.get("rows", 0) if isinstance(res, dict) else 0
            notes = res.get("notes", "") if isinstance(res, dict) else ""
            conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                         (n, common.now(), rows, notes))
            conn.commit()
            print(f"[{n}] OK rows={rows} {notes}")
            ok.append(n)
        except Exception:
            traceback.print_exc()
            conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,0)",
                         (n, common.now(), 0, traceback.format_exc()[-1500:]))
            conn.commit()
            print(f"[{n}] FAILED")
            fail.append(n)
    print(f"done ok={ok} fail={fail}")
    return 0 if not fail else 1

if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
