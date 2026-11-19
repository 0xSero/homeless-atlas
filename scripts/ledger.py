#!/usr/bin/env python3
"""Append a ledger row: python3 scripts/ledger.py <domain> <verdict> <rows> <evidence> [note]

verdict ∈ KEEP | REJECT | PARKED | DONE | BLOCKED
Writes JSONL to ledger.jsonl and a human table row to LEDGER.md.
"""
import json, sys, os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
J = ROOT / "ledger.jsonl"
M = ROOT / "LEDGER.md"


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    dom, verdict, rows, evidence = sys.argv[1:5]
    note = " ".join(sys.argv[5:]) if len(sys.argv) > 5 else ""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec = {"ts": ts, "skill": "homeless-atlas", "domain": dom, "verdict": verdict,
           "rows": int(rows) if str(rows).lstrip("-").isdigit() else rows,
           "evidence": evidence, "note": note}
    with open(J, "a") as f:
        f.write(json.dumps(rec) + "\n")
    if not M.exists():
        M.write_text("# Ledger — homeless-atlas\n\n| ts | domain | verdict | rows | evidence | note |\n"
                     "|---|---|---|---|---|---|\n")
    with open(M, "a") as f:
        f.write(f"| {ts} | {dom} | {verdict} | {rows} | {evidence} | {note} |\n")
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
