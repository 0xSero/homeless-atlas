#!/usr/bin/env bash
# Retry the site build until the data is stable enough to publish (the build refuses when a table
# is mid-transaction), then commit and push. Runs detached; exits on first success or on timeout.
set -uo pipefail
R=/home/ser/projects/homeless-atlas
cd "$R" || exit 1
TRIES=${TRIES:-20}
for i in $(seq 1 "$TRIES"); do
  if python3 scripts/build_site.py >> logs/build.log 2>&1; then
    if python3 scripts/crawl.py --quiet >> logs/crawl.log 2>&1; then
      python3 scripts/audit.py --fix >> logs/audit.log 2>&1
      git add -A >/dev/null 2>&1
      if ! git diff --cached --quiet; then
        git commit -q -m "site: publish (build attempt $i, $(date -u +%H:%MZ))" && git push -q origin master
      fi
      echo "[$(date -Is)] published on attempt $i"
      exit 0
    else
      echo "[$(date -Is)] attempt $i: build ok but crawl failed, see logs/crawl.log"
    fi
  else
    echo "[$(date -Is)] attempt $i: build deferred (data mid-transaction)"
  fi
  sleep 120
done
echo "[$(date -Is)] gave up after $TRIES attempts"
exit 1
