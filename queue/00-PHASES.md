# Queue conventions

One file per work item: `queue/<NN>-<domain>.md`. First line: `# <domain> — <one line goal>`.
State lives in `queue/state/<domain>.status` ∈ OPEN | RUNNING | DONE | PARKED | BLOCKED.
`BLOCKED` items must have a `## Unblock` section naming the exact key/URL/decision needed.

Items are ordered by value. The loop takes OPEN items in filename order, up to MAX_PAR (default 4),
and runs each with an independent pi worker on `homelab/deepseek-v4.1-flash-1017`.
Each worker reads `docs/INGEST_CONTRACT.md` first and must leave the repo committed-or-clean.
