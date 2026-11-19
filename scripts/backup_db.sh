#!/usr/bin/env bash
# Snapshot the database with VACUUM INTO (consistent even while writers are active) and keep the
# last 12. SQLite corruption once destroyed a full day of ingest; a 15-minute snapshot bounds the loss.
set -uo pipefail
R=/home/ser/projects/homeless-atlas
cd "$R" || exit 1
mkdir -p data/backups
TS=$(date -u +%Y%m%dT%H%M%SZ)
python3 - "$TS" <<'PY'
import sqlite3, sys, os
ts = sys.argv[1]
dest = f"data/backups/atlas-{ts}.db"
c = sqlite3.connect("data/atlas.db", timeout=120)
c.execute(f"VACUUM INTO '{dest}'")
c.close()
sz = os.path.getsize(dest)
print(f"backup {dest} {sz/1e6:.1f} MB")
PY
ls -1t data/backups/atlas-*.db | tail -n +13 | xargs -r rm -f
