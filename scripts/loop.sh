#!/usr/bin/env bash
# homeless-atlas loop engine.
# Takes OPEN items from queue/, runs each as an independent `pi` worker (tmux window,
# model homelab/deepseek-v4.1-flash-1017), then verifies, rebuilds the site and commits.
#
#   MAX_PAR=4 ./scripts/loop.sh        # foreground
#   nohup ./scripts/loop.sh ...        # (prefer bg_task from the agent)
#
# Stop with: touch STOP   (or `bg_task stop`)
set -uo pipefail

R=/home/ser/projects/homeless-atlas
cd "$R" || exit 1
MAX_PAR=${MAX_PAR:-4}
MODEL=${MODEL:-homelab/deepseek-v4.1-flash-1017}
THINKING=${THINKING:-high}
SESSION=${SESSION:-atlas}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}
SLEEP=${SLEEP:-20}
WORKER_TIMEOUT=${WORKER_TIMEOUT:-7200}   # 2h wall clock per worker, then requeue
W=$R/logs/workers
mkdir -p "$W" queue/state

# ---------------------------------------------------------------- single instance
LOCK=$R/logs/loop.lock
exec 9>"$LOCK"
if ! flock -n 9; then echo "loop already running (lock $LOCK)"; exit 0; fi

log() { echo "[$(date -Is)] $*" | tee -a "$R/logs/loop.log"; }

status_of() { cat "queue/state/$1.status" 2>/dev/null || echo MISSING; }
set_status() { echo "$2" > "queue/state/$1.status"; log "status $1 -> $2"; }

domains() { for f in queue/[0-9]*.md; do d=$(basename "$f" .md); echo "${d#*-}"; done; }

open_items() { for d in $(domains); do [ "$(status_of "$d")" = OPEN ] && echo "$d"; done; }

# DELEGATE = an external agent owns this domain; the loop must never touch it.
# DONE / PARKED / BLOCKED are terminal for the loop.

# ---------------------------------------------------------------- prompt assembly
make_prompt() {
  local dom=$1 item=$2 out=$W/$1.prompt.txt
  {
    echo "You are a worker agent on the homeless-atlas project. Repo: $R (your cwd)."
    echo "Python 3.10 + requests + pandas are installed. There is no sqlite3 CLI: use python3 -c with sqlite3."
    echo
    echo "STEP 0 - read these three files in full before doing anything else:"
    echo "  docs/INGEST_CONTRACT.md   (the interface you must implement)"
    echo "  ingest/schema.sql         (the tables)"
    echo "  ingest/common.py          (the helpers: fetch, fetch_json, upsert, source_id_for, name_norm)"
    echo "Also read research/*.md for domains already done, so you do not duplicate work."
    echo
    echo "YOUR ASSIGNMENT:"
    sed -n '1,200p' "$item"
    echo
    echo "RULES (violating these fails the task):"
    echo "1. Never invent a number, name, date or URL. Every value must come from a fetched response that is cached under data/raw/<domain>/. If a field is unknown write None."
    echo "2. Every row you insert needs source_id = common.source_id_for(conn, url, '<domain>', title=..., publisher=..., license=...)."
    echo "3. Idempotent: use common.upsert(conn, table, row, keys=[...]). Re-running must not duplicate."
    echo "4. Write ONLY inside: ingest/domains/<domain>.py, data/raw/<domain>/, research/<domain>.md, docs/SOURCES.md (append one row), logs/. Never edit site/, scripts/, ingest/schema.sql or another domain's files."
    echo "5. Public data only. Organisation-level and published-role professional info only. No personal home addresses or mobile numbers."
    echo "6. 1 request/second via common.fetch. If a source 403s or needs a key you do not have, stop that source, write it in BLOCKERS.md under '## <domain>' with the exact key + signup URL, and continue with the rest."
    echo "7. Finish by actually running it: python3 -m ingest.run <domain>. Paste the row counts you got. Then append '## Verified' (5 rows you re-checked live) and '## Coverage' (what fraction of the universe you captured, what is missing) to research/<domain>.md."
    echo "8. Do not touch queue/state/*. Do not git commit. Leave the working tree with your files saved."
    echo "9. If the full sweep takes longer than 10 minutes, make the ingestor resumable (progress table), run it in the background with bg_task, poll its log, and report the partial counts."
  } > "$out"
}

launch() {
  local dom=$1
  # never run two workers for the same domain (an orphaned worker from a previous loop can survive)
  if pgrep -f -- "atlas-$dom" >/dev/null 2>&1; then
    log "worker for $dom already alive; not launching a second one"
    return 0
  fi
  local item=$R/queue/$(ls queue | grep -- "-$dom.md$" | head -1)
  make_prompt "$dom" "$item"
  set_status "$dom" RUNNING
  echo $(( $(cat "queue/state/$dom.attempts" 2>/dev/null || echo 0) + 1 )) > "queue/state/$dom.attempts"
  rm -f "$W/$dom.exit"
  cat > "$W/$dom.run.sh" <<RUN
#!/usr/bin/env bash
cd $R || exit 1
{ timeout $WORKER_TIMEOUT pi -p --model $MODEL --thinking $THINKING --name "atlas-$dom" \
    "\$(cat $W/$dom.prompt.txt)" 2>&1; echo \$? > $W/$dom.exit; } | tee -a $R/logs/workers/$dom.log
RUN
  chmod +x "$W/$dom.run.sh"
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux new-window -t "$SESSION" -n "$dom" "bash $W/$dom.run.sh" 2>/dev/null \
      || tmux respawn-window -k -t "$SESSION:$dom" "bash $W/$dom.run.sh"
  else
    tmux new-session -d -s "$SESSION" -n "$dom" "bash $W/$dom.run.sh"
  fi
  log "launched $dom (attempt $(cat queue/state/$dom.attempts))"
}

finish() {
  local dom=$1 rc=$2
  local rows
  rows=$(python3 - <<PY
import sqlite3
try:
    c=sqlite3.connect("data/atlas.db")
    r=c.execute("select rows from ingest_run where domain='$dom' order by id desc limit 1").fetchone()
    print(r[0] if r else 0)
except Exception:
    print(0)
PY
)
  if [ "$rc" = "0" ]; then
    set_status "$dom" DONE
    python3 scripts/ledger.py "$dom" DONE "$rows" "research/$dom.md + ingest/domains/$dom.py" "worker exit 0" >/dev/null
  else
    local a; a=$(cat "queue/state/$dom.attempts" 2>/dev/null || echo 1)
    if [ "$a" -ge "$MAX_ATTEMPTS" ]; then
      set_status "$dom" PARKED
      python3 scripts/ledger.py "$dom" PARKED "$rows" "logs/workers/$dom.log" "failed $a attempts, needs review" >/dev/null
    else
      set_status "$dom" OPEN
      python3 scripts/ledger.py "$dom" REJECT "$rows" "logs/workers/$dom.log" "attempt $a failed rc=$rc" >/dev/null
    fi
  fi
  tmux kill-window -t "$SESSION:$dom" 2>/dev/null
}

checkpoint() {
  cd "$R" || return
  git add -A >/dev/null 2>&1
  if ! git diff --cached --quiet; then
    git commit -q -m "data: loop checkpoint $(date -u +%Y-%m-%dT%H:%MZ)" && log "committed"
  fi
  if ! python3 -c "
import sqlite3,sys
c=sqlite3.connect('data/atlas.db',timeout=120)
r=c.execute('pragma quick_check').fetchone()[0]
sys.exit(0 if r=='ok' else 1)" 2>/dev/null; then
    log "DATABASE INTEGRITY FAILURE — stopping the loop; restore from data/backups/ and investigate"
    touch STOP
    break
  fi
  ./scripts/backup_db.sh >> logs/backup.log 2>&1 || log "backup failed"
  python3 scripts/audit.py --fix >> logs/audit.log 2>&1 || log "AUDIT: problems remain (see logs/audit.log)"
  python3 scripts/verify.py >> logs/verify.log 2>&1 && log "VERIFY: ALL GATES PASS"
  if [ -f scripts/build_site.py ]; then
    python3 scripts/build_site.py >> logs/build.log 2>&1 && log "site rebuilt" || log "site build FAILED (see logs/build.log)"
  fi
}

log "loop start MAX_PAR=$MAX_PAR MODEL=$MODEL"
while :; do
  [ -f STOP ] && { log "STOP file present, exiting"; break; }
  avail=$(df -Pk /home | awk 'NR==2{print int($4/1048576)}')
  [ "${avail:-99}" -lt 20 ] && { log "disk ${avail}G < 20G, exiting"; break; }

  # reap finished workers
  for d in $(domains); do
    if [ "$(status_of "$d")" = RUNNING ]; then
      if [ -f "$W/$d.exit" ]; then
        finish "$d" "$(cat "$W/$d.exit")"
        checkpoint
      elif ! pgrep -f -- "atlas-$d" >/dev/null 2>&1; then
        log "worker $d is not alive and left no exit marker; requeueing"
        finish "$d" 1
        checkpoint
      fi
    fi
  done

  running=0
  for d in $(domains); do [ "$(status_of "$d")" = RUNNING ] && running=$((running+1)); done

  if [ "$running" -eq 0 ] && [ -z "$(open_items)" ]; then
    log "no OPEN items left; final checkpoint"
    checkpoint
    python3 scripts/verify.py 2>&1 | tail -3 | tee -a logs/loop.log
    break
  fi

  for d in $(open_items); do
    [ "$running" -ge "$MAX_PAR" ] && break
    launch "$d"
    running=$((running+1))
  done
  sleep "$SLEEP"
done
log "loop end"
