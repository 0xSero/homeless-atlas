#!/usr/bin/env python3
"""Gate for the WTF street flyers: every flyer the atlas can make obeys the kit's rules.

  python3 scripts/verify_flyers.py            all counties; exit 1 on any broken rule
  python3 scripts/verify_flyers.py 42049      one or more counties

The rules live in flyer.broken_rules(); the site build and the CLI refuse any flyer that breaks one.
This gate makes the refusal loud: a county whose data would produce a rule-breaking flyer fails here,
so the fix lands in the code or the research file instead of the county silently losing its flyer.
It also checks the rounding helpers, since every short dollar figure goes through them.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flyer  # noqa: E402


def main(argv):
    conn = sqlite3.connect(f"file:{flyer.DB}?mode=ro", uri=True)
    units = [(flyer.dollars_short(3_478_959), "$3.4 million"), (flyer.dollars_short(96_638_494), "$96.6 million"),
             (flyer.dollars_short(298_822_397), "$298 million"), (flyer.dollars_short(1_999_999_999), "$1.9 billion"),
             (flyer.round_down_2sig(589), 580), (flyer.round_down_2sig(49_999), 49_000), (flyer.round_down_2sig(87), 87)]
    fails = [f"rounding helper gave {got!r}, want {want!r}" for got, want in units if got != want]
    fips = argv or [r[0] for r in conn.execute("SELECT geoid FROM geo_city WHERE kind='county' AND length(geoid)=5")]
    made, why_none = 0, Counter()
    for f in fips:
        d, probs = flyer.flyer_data(conn, f)
        if d is None or probs:
            why_none[(probs or ["no data"])[0].split(":")[0]] += 1
            continue
        bad = flyer.broken_rules(d)
        made += not bad
        fails += [f"{f}: {b}" for b in bad]
    for line in fails[:50]:
        print("FAIL", line)
    print(f"{made} flyers pass across {len(fips)} counties; {len(fails)} broken rule(s)")
    for why, n in why_none.most_common():
        print(f"  no flyer, {n:>4}: {why}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
