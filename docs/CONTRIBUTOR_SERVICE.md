# The county service (optional)

Sources move. A county report gets a new URL, a shelter changes its intake number, a news site
moves its archive. The county service re-checks the counties you contributed on a schedule, so
a broken claim or a stale phone number on a street flyer is found within a day instead of when
someone calls it.

You do not need it to contribute. `python3 scripts/county.py check <FIPS>` does the same check
once, by hand.

## What it does

Every cycle (default: every 24 hours):

1. Checks the tamper rules below. If any fails, the cycle logs which one and exits with code 3.
   Nothing else happens.
2. Fetches upstream `master` (github.com/0xSero/homeless-atlas) over https and fast-forwards its
   own clone to it.
3. Runs `scripts/county.py check <FIPS>` for each configured county, one at a time, in a child
   process capped at 1000 MB of memory. Each source goes through the fetch ladder again: live
   first, then the publisher's own alternates, then the Internet Archive. Contacts count only from
   the live official page.
4. Writes a JSON report to `~/.local/state/homeless-atlas/reports/`: the upstream commit and tree
   hash, every tamper rule result, every claim, contact and help line with its result and how the
   page was fetched, and a sha256 over the whole report (`signed_off.sha256`; recompute it by
   removing `signed_off` and hashing the JSON with sorted keys).
5. Compares the result with the previous report. A change is a claim that verified before and does
   not now, a source that moved from a live copy to an archive-only copy, or an office phone or email
   that is no longer on the official page.
6. Only if you set `open_prs = true`: for each county with a change, opens a **draft** pull request
   from your fork against upstream. The pull request changes one file, that county's research file:
   it sets `updated:` to today and adds a short `## Changes found` note. The report is attached in
   the pull request body. A person reviews it; the service never merges and never marks it ready.

## What it does not do

- It never merges, approves or pushes to `0xSero/homeless-atlas`. It pushes only to the fork you
  name, and only with `open_prs = true`.
- It never edits any file other than the one county file, and only inside a throwaway worktree for
  that pull request. Its own clone stays exactly at upstream `master`.
- It never touches your working copy of the repository, `data/atlas.db`, or anything outside the
  paths listed next.
- It never logs in to websites, solves a CAPTCHA, reads around a paywall, or sends credentials
  (the fetch ladder's hard limits; see `research/county/README.md`).

## What it can touch

| path | what |
|---|---|
| `~/.config/homeless-atlas/service.toml` | your settings |
| `~/.config/homeless-atlas/run-cycle.sh` | the launcher the scheduler runs (read-only to the service) |
| `~/.local/share/homeless-atlas/worktree/` | its own sparse clone of upstream: `scripts`, `ingest`, `research/county`, `docs` |
| `~/.local/state/homeless-atlas/` | `reports/`, `service.jsonl` (one line per cycle), `install.json`, `STOP` |
| `~/.config/systemd/user/homeless-atlas-county.{service,timer}` | Linux only |
| `~/Library/LaunchAgents/org.homeless-atlas.county-service.plist` | macOS only |

Network: GitHub (fetch, and push to your fork when `open_prs = true`), the sites your county file
cites, and the Internet Archive.

## Install

Requirements: `git`, Python 3.11 or newer with `requests` and `pyyaml` importable under
`python3 -I` (the service runs isolated, so packages in your user site directory do not count;
install them system-wide, with Homebrew's Python, or in a virtualenv and pass `--python`), and `gh`
signed in if you want draft pull requests.

```bash
python3 scripts/county.py service install --county 42049 --dry-run   # read every file it would write
python3 scripts/county.py service install --county 42049             # add --county again for more
python3 scripts/county.py service run-once                           # one cycle now
```

Draft pull requests are off by default. To turn them on:

```bash
python3 scripts/county.py service install --county 42049 --open-prs --fork <you>/homeless-atlas
```

or set `open_prs = true` and `fork = "<you>/homeless-atlas"` in `service.toml` later.

Settings in `~/.config/homeless-atlas/service.toml`:

| key | default | meaning |
|---|---|---|
| `counties` | (required) | list of quoted 5-digit FIPS codes |
| `interval_hours` | 24 | hours between cycles (written into the timer or agent at install) |
| `open_prs` | false | open draft pull requests when something changed |
| `fork` | none | `<you>/homeless-atlas`, required with `open_prs = true` |
| `max_rss_mb` | 1000 | memory cap for each check child |
| `upstream` | `https://github.com/0xSero/homeless-atlas.git` | must be https |
| `python` | the interpreter that ran install | runs each cycle with `-I -B` |

On Linux the timer runs while you are logged in; `loginctl enable-linger` keeps it running after
you log out.

## Check on it

```bash
python3 scripts/county.py service status     # settings, scheduler state, worktree commit, last cycle
python3 scripts/county.py service logs -n 20 # one line per cycle: ok, refused rules, report path
ls ~/.local/state/homeless-atlas/reports/     # full JSON reports
touch ~/.local/state/homeless-atlas/STOP     # pause: cycles are skipped until you delete it
```

Linux: `systemctl --user list-timers homeless-atlas-county.timer` and
`journalctl --user -u homeless-atlas-county`. macOS: `launchctl print gui/$(id -u)/org.homeless-atlas.county-service`
and `~/.local/state/homeless-atlas/launchd.log`.

## Uninstall

```bash
python3 scripts/county.py service uninstall          # prints exactly what it will delete
python3 scripts/county.py service uninstall --yes    # unloads, deletes, then confirms nothing is left
```

It unloads the timer or agent, deletes every path in the table above (reports and logs included),
removes the empty directories install created, and then checks each path again and reports any that
remain.

## Why it is safe to leave running

The service runs code from the internet on a timer, so it has to make sure that code is exactly
upstream `master` and nothing else. At the start of every cycle, before any check runs, it verifies
and logs:

| rule | why |
|---|---|
| `worktree_clean`: `git status --porcelain` is empty | an edited or added file in its clone means someone changed the code it is about to run |
| `no_tracked_modifications`: `git diff HEAD` and `git diff --cached` are empty | same, for staged changes |
| `no_unexpected_ignored_files`: only `data/raw/` and `logs/` | a planted `__pycache__/*.pyc` would run instead of the reviewed source |
| `git_config_safe`: no hooks, `insteadOf`, credential, proxy, alias or filter keys in the clone's git config | these would redirect fetches or run commands |
| `origin_is_configured_upstream` and `upstream_is_https` | it fetches only from the upstream you configured, over https |
| `on_master`, `fast_forward_only` | it only ever moves forward along upstream history; if upstream history was rewritten, it stops and logs instead of following |
| `head_is_upstream`: HEAD equals the commit just fetched from upstream | the code that runs is the code upstream publishes right now |

The launcher repeats the clean-tree check in plain `sh` and `git` before any Python from the clone
starts, and removes ignored files (bytecode) from the code directories. Every check runs as
`python3 -I -B`: isolated mode ignores `PYTHON*` environment variables and your user site
directory, and imports come only from the clone. If a rule fails, the cycle exits 3 and does
nothing else.

Limits around each cycle:

- **Linux** (systemd user unit): `MemoryMax=1G`, `CPUQuota=50%`, `Nice=10`, idle I/O priority,
  `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=read-only` with write access only to
  `~/.local/share/homeless-atlas` and `~/.local/state/homeless-atlas`, `PrivateTmp=yes`, and
  `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`.
- **macOS** (launchd user agent): `ProcessType=Background`, `LowPriorityIO`, `Nice=10`. launchd has
  no memory cap, so `run-once` enforces one: it polls each check child's resident memory every
  second and kills it above `max_rss_mb` (1000 MB), and records the peak in the report. macOS has no
  equivalent of the systemd filesystem sandbox for user agents; the tamper rules are the guard.
